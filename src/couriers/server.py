"""
JSON API модуля «Оплата курьерам».

Blueprint регистрируется в src/pyrus/server.py (мастер-приложение) — тот же
паттерн, что cashshifts_bp / moysklad_bp / writeoffs_bp.

Отчёт читается ИСКЛЮЧИТЕЛЬНО из локальной базы: живые запросы к внешнему API
из обработчика уже дважды укладывали прод (воркеров всего 2). В CRM ходит
только фоновый синк под локом.
"""

import csv
import io
import logging
import os
import re
import sys
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from flask import Blueprint, Response, jsonify, request
from flask_login import current_user, login_required

# Импортируем модуль авторизации (как в cashshifts/server.py)
auth_path = os.path.join(os.path.dirname(__file__), '../')
sys.path.insert(0, auth_path)
from auth import log_action, section_required, role_required, require_ajax_header  # noqa: E402

from . import retailcrm, storage  # noqa: E402

logger = logging.getLogger(__name__)

couriers_bp = Blueprint("couriers", __name__, url_prefix="/api/couriers")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# ============================================================================
# Параметры синхронизации
# ============================================================================

SYNC_LOCK = "courier_orders"
SYNC_LOCK_TTL = 600

# Как часто продлевать лок. Раньше продление шло после КАЖДОЙ страницы CRM —
# то есть отдельное соединение, UPDATE и commit на общий диск /data по нескольку
# раз в минуту, пока идёт прогон. При TTL в 10 минут продлевать чаще раза в
# минуту незачем: запас десятикратный, а диск, за который дерутся все базы
# сразу (barhat.db — это авторизация всего сайта), заметно свободнее.
LOCK_RENEW_INTERVAL_SECONDS = 60

# Окно регулярного прогона: заказ доставляют сегодня, а статус «Выполнен» и
# себестоимость проставляют позже — вчерашние дни обязаны перечитываться.
RECENT_WINDOW_DAYS = 7

# Ночной прогон: за квартал заказ уже точно не переоформят.
DEEP_WINDOW_DAYS = 90

# Окно вперёд — для модуля «Загрузка салонов»: сетка нагрузки живёт на будущих
# заказах, которых в витрине раньше не было вовсе. 60 дней с запасом покрывают
# предзаказы к праздникам, а стоят копейки: разведка 2026-09-05 нашла на 60
# дней вперёд всего 79 заказов против ~180 в сутки на прошедших датах.
FUTURE_WINDOW_DAYS = 60

# Окно пересобирается кусками по неделе: DELETE+INSERT одного куска атомарен,
# поэтому отчёт никогда не видит полупустой период, а память не держит
# десятки тысяч заказов разом.
CHUNK_DAYS = 7

# Будущее пересобирается кусками покрупнее: заказов там единицы, а каждый кусок
# это отдельная запись на медленный общий диск (см. _sync_chunks).
FUTURE_CHUNK_DAYS = 30

# Потолок на длину периода в ручном запросе — защита от «загрузить за 10 лет»
MAX_MANUAL_PERIOD_DAYS = 400

# Бюджет времени на один прогон. Упереться в него лучше, чем держать поток
# сутками: следующий прогон продолжит с того же места (окно пересобирается).
SYNC_BUDGET_SECONDS = 25 * 60

SCHEDULER_INTERVAL_SECONDS = 30 * 60

# Задержка перед первым прогоном. У МойСклада интервал такой же (30 минут),
# и при одинаковой задержке два синка стартовали одновременно и оставались
# синхронными до самого рестарта — то есть били по общему диску /data одной
# волной. Разные задержки разводят их на 5 минут навсегда.
SCHEDULER_START_DELAY_SECONDS = 120
DEEP_SYNC_AT_KEY = "deep_sync_at"
DEEP_SYNC_NIGHT_HOURS_UTC = (21, 22, 23)

_scheduler_started = False


def error_response(message: str, status: int = 400):
    return jsonify({"success": False, "error": message}), status


def success_response(data: Any, meta: dict = None):
    payload = {"success": True, "data": data}
    if meta:
        payload["meta"] = meta
    return jsonify(payload)


def _valid_date(value: Optional[str]) -> bool:
    return bool(value and _DATE_RE.match(value))


# ============================================================================
# Синхронизация
# ============================================================================

def _chunks(date_from: str, date_to: str, chunk_days: int) -> List[tuple]:
    """Разбить период на куски по chunk_days включительно."""
    start = datetime.strptime(date_from, "%Y-%m-%d").date()
    end = datetime.strptime(date_to, "%Y-%m-%d").date()
    result = []
    while start <= end:
        chunk_end = min(start + timedelta(days=chunk_days - 1), end)
        result.append((start.isoformat(), chunk_end.isoformat()))
        start = chunk_end + timedelta(days=1)
    return result


def _sync_chunks(date_from: str, date_to: str) -> List[tuple]:
    """
    Куски окна: прошлое — по неделе, будущее — по месяцу.

    Каждый кусок это отдельная транзакция DELETE+INSERT на общий диск /data, за
    который дерутся все базы сразу (там же авторизация всего сайта). На
    прошедших датах неделя оправдана — там ~180 заказов в сутки и переписывать
    приходится много. На будущих датах заказов единицы (79 на 60 дней), и
    делить их на девять кусков значит девять лишних записей каждые полчаса
    ради одних и тех же сорока строк.
    """
    today = date.today().isoformat()
    if date_to <= today:
        return _chunks(date_from, date_to, CHUNK_DAYS)
    if date_from > today:
        return _chunks(date_from, date_to, FUTURE_CHUNK_DAYS)
    return (
        _chunks(date_from, today, CHUNK_DAYS)
        + _chunks((date.today() + timedelta(days=1)).isoformat(), date_to, FUTURE_CHUNK_DAYS)
    )


def _sync_catalog(client, deadline: float) -> None:
    """
    Каталог номенклатуры: группы, офферы, единицы измерения.

    Только в глубоком прогоне (раз в сутки): каталог меняется медленно, а
    6 запросов на каждый получасовой тик — это лишние обращения к CRM и лишнее
    время воркера.

    Ошибка здесь НЕ роняет синк заказов: заказы важнее каталога, а без свежего
    каталога расчёт отработает по вчерашнему. Пустой ответ каталог не
    перезаписывает — это проверяет `replace_catalog`.
    """
    try:
        groups = client.get_product_groups()
        offers = []
        links = []
        for page in client.iter_products(deadline=deadline):
            page_offers, page_links = retailcrm.parse_catalog_page(page)
            offers.extend(page_offers)
            links.extend(page_links)

        result = storage.replace_catalog(groups, offers, links)
        logger.info(f"Каталог RetailCRM: групп {result['groups']}, "
                    f"офферов {result['offers']}, связей {result['links']}")
    except Exception as e:
        logger.error(f"Синхронизация каталога не удалась (заказы это не затрагивает): {e}")


def _sync_range(date_from: str, date_to: str, deep: bool = False) -> int:
    """
    Пересобрать данные за период. Возвращает число записанных заказов.

    Справочники (курьеры, салоны) обновляются первыми: без них у заказа не
    определится город, а у курьера — признак службы доставки.
    """
    client = retailcrm.get_client()
    deadline = time.monotonic() + SYNC_BUDGET_SECONDS

    storage.upsert_couriers(client.get_couriers())
    storage.upsert_sites(client.get_sites())
    storage.upsert_delivery_types(client.get_delivery_types())
    storage.upsert_order_statuses(client.get_statuses())
    if deep:
        _sync_catalog(client, deadline)
    site_cities = storage.get_site_cities()

    total = 0
    # Заказы без даты доставки в витрину не попадают — по периоду их всё равно
    # не показать. Но и молчать про них нельзя: это не ноль, это «мы не знаем,
    # когда». Считаем и кладём в состояние, чтобы число было видно в /health.
    skipped_no_date = 0
    log_id = storage.start_sync_log()
    last_renew = time.monotonic()
    try:
        for chunk_from, chunk_to in _sync_chunks(date_from, date_to):
            rows: List[Dict[str, Any]] = []
            # Статус не фильтруем: витрина общая для выплат, показателей салонов
            # и загрузки салонов, а будущий заказ по определению не «Выполнен».
            # Отбор по статусу стоит в каждом чтении (см. storage.COMPLETED_STATUS).
            for page in client.iter_orders_by_delivery_date(
                chunk_from, chunk_to, deadline=deadline
            ):
                for order in page:
                    parsed = retailcrm.parse_order(order, site_cities)
                    if parsed is None:
                        skipped_no_date += 1
                        continue
                    if not parsed["retailcrm_order_id"]:
                        continue
                    # Пишем ВСЕ выполненные заказы, включая самовывоз.
                    #
                    # Раньше заказ без курьера и с нулевой себестоимостью здесь
                    # отбрасывался — он не нужен для выплат. Но это как раз
                    # самовывоз, а самовывоз — основа канала «Улица», и без него
                    # показатели салонов считать не из чего: доля «Улицы» не
                    # считалась бы вовсе, а сумма отгрузок была занижена вдвое.
                    # Отбор «за что платим курьеру» переехал в чтение —
                    # storage.PAYOUT_FILTER.
                    rows.append(parsed)
                # Долгий прогон обязан продлевать лок, иначе по TTL его
                # подхватит соседний воркер и оба пойдут качать одно и то же.
                # Но не на каждой странице — см. LOCK_RENEW_INTERVAL_SECONDS.
                if time.monotonic() - last_renew >= LOCK_RENEW_INTERVAL_SECONDS:
                    storage.renew_sync_lock(SYNC_LOCK, SYNC_LOCK_TTL)
                    last_renew = time.monotonic()

            storage.replace_orders_window(chunk_from, chunk_to, rows)
            total += len(rows)
            storage.update_sync_log_progress(log_id, total)
            logger.info(f"Курьеры: {chunk_from}—{chunk_to} → {len(rows)} заказов")

        storage.set_sync_state(storage.NO_DATE_ORDERS_KEY, str(skipped_no_date))
        storage.finish_sync_log(log_id, total, "completed")
        _scan_load_alerts()
        if skipped_no_date:
            logger.info(f"Курьеры: {skipped_no_date} заказов без даты доставки пропущено")
        return total
    except Exception as e:
        logger.error(f"Ошибка синхронизации заказов курьеров: {e}")
        storage.finish_sync_log(log_id, total, "failed", str(e))
        raise


def _scan_load_alerts() -> None:
    """
    Пересчитать предупреждения о перегрузе — шагом синка, а не своим
    планировщиком: лишний фоновый поток означает лишние обращения к общему
    медленному диску, за который дерутся все базы сразу.

    Модуль нагрузки может отсутствовать или упасть — синк заказов из-за этого
    падать не должен: выплаты и показатели салонов важнее сетки.
    """
    try:
        from salonload.metrics import scan_alerts
        result = scan_alerts()
        if result["created"] or result["resolved"]:
            logger.info(f"Загрузка салонов: предупреждений создано {result['created']}, "
                        f"снято по разгрузке {result['resolved']}")
        if result["no_timezone"]:
            logger.warning("Загрузка салонов: пояс не задан у салонов "
                           f"{', '.join(result['no_timezone'])} — предупреждения по ним не считаются")
    except ImportError:
        logger.debug("Модуль загрузки салонов недоступен — предупреждения не считаем")
    except Exception as e:
        logger.error(f"Не удалось пересчитать предупреждения о загрузке: {e}")


def _run_sync(date_from: str, date_to: str, deep: bool = False) -> bool:
    """Прогон под локом. False — синхронизация уже идёт (в этом или соседнем воркере)."""
    if not retailcrm.is_configured():
        logger.warning("RetailCRM не настроен — синхронизация курьеров пропущена")
        return False

    if not storage.try_acquire_sync_lock(SYNC_LOCK, SYNC_LOCK_TTL):
        logger.info("Синхронизация курьеров уже идёт — пропускаем запуск")
        return False

    try:
        _sync_range(date_from, date_to, deep=deep)
        if deep:
            storage.set_sync_state(DEEP_SYNC_AT_KEY, datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))
    except Exception:
        pass  # уже залогировано и записано в sync_log
    finally:
        storage.release_sync_lock(SYNC_LOCK)
    return True


def _window(days_back: int, days_forward: int = 0) -> tuple:
    today = date.today()
    return (
        (today - timedelta(days=days_back)).isoformat(),
        (today + timedelta(days=days_forward)).isoformat(),
    )


def _deep_sync_due() -> bool:
    last = storage.get_sync_state(DEEP_SYNC_AT_KEY)
    if not last:
        return True
    try:
        parsed = datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return True
    return (datetime.utcnow() - parsed) > timedelta(days=1)


def _scheduled_run() -> None:
    """Один тик планировщика: глубокий прогон ночью, обычный — в остальное время."""
    range_info = storage.get_orders_date_range()
    empty_db = not range_info.get("max_date")
    night = datetime.utcnow().hour in DEEP_SYNC_NIGHT_HOURS_UTC

    # Пустая база — данных нет вообще, отчёт показывать нечем: тянем квартал
    # сразу, не дожидаясь ночи. Это ~150 запросов и пара минут, а не десятки
    # минут, как полный ресинк МойСклада.
    if empty_db or (night and _deep_sync_due()):
        date_from, date_to = _window(DEEP_WINDOW_DAYS, FUTURE_WINDOW_DAYS)
        _run_sync(date_from, date_to, deep=True)
        return

    # Окно вперёд берётся и в обычном прогоне: заказ на послезавтра могли
    # оформить пять минут назад, а сетка нагрузки нужна именно на завтра.
    # Будущих заказов мало (79 на 60 дней), так что прогон почти не тяжелеет.
    date_from, date_to = _window(RECENT_WINDOW_DAYS, FUTURE_WINDOW_DAYS)
    _run_sync(date_from, date_to)


def _scheduler_loop() -> None:
    time.sleep(SCHEDULER_START_DELAY_SECONDS)
    while True:
        try:
            # Талон на тик берётся до всякой работы: планировщик крутится в
            # каждом воркере, и без этого второй воркер повторял весь прогон
            # заново через полминуты после первого (см. try_claim_scheduled_run).
            if storage.try_claim_scheduled_run(SYNC_LOCK, SCHEDULER_INTERVAL_SECONDS):
                _scheduled_run()
            else:
                logger.info("Тик синхронизации курьеров уже отработал соседний воркер")
        except Exception as e:
            logger.error(f"Ошибка планировщика синхронизации курьеров: {e}")
        time.sleep(SCHEDULER_INTERVAL_SECONDS)


def start_sync_scheduler() -> None:
    """
    Запустить фоновую синхронизацию заказов курьеров.

    Вызывается из pyrus/server.py после регистрации blueprint. Отключается
    переменной COURIERS_SYNC_SCHEDULER=0 (локальная разработка: не хочется,
    чтобы каждый запуск сервера лез в боевой RetailCRM).
    """
    global _scheduler_started

    if os.getenv("COURIERS_SYNC_SCHEDULER", "1") != "1":
        logger.info("Планировщик синхронизации курьеров отключён (COURIERS_SYNC_SCHEDULER=0)")
        return

    if _scheduler_started:
        return
    _scheduler_started = True

    thread = threading.Thread(target=_scheduler_loop, daemon=True, name="couriers-sync-scheduler")
    thread.start()
    logger.info(
        f"Планировщик синхронизации курьеров запущен "
        f"(интервал {SCHEDULER_INTERVAL_SECONDS // 60} мин)"
    )


# ============================================================================
# API отчёта
# ============================================================================

@couriers_bp.route("/report", methods=["GET"])
@section_required("courier_payouts")
def get_report():
    """
    Отчёт «сколько платить курьерам»: сумма себестоимости доставки по
    выполненным заказам за период, по курьерам.

    В том же ответе — распределение по салонам, список заказов без курьера и
    список отменённых. Одной ручкой, а не тремя: каждая ручка это отдельное
    соединение с базой, а на сетевом диске /data цену определяет именно их
    число (см. CLAUDE.md).

    Query: date_from, date_to (YYYY-MM-DD), city, site, only_own (1/0)
    """
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")

    for value, name in ((date_from, "date_from"), (date_to, "date_to")):
        if value and not _valid_date(value):
            return error_response(f"{name} должен быть в формате YYYY-MM-DD")

    city = request.args.get("city") or None
    site_code = request.args.get("site") or None
    only_own = request.args.get("only_own", "1") != "0"

    report = storage.report_by_courier(
        date_from=date_from, date_to=date_to, city=city,
        site_code=site_code, only_own=only_own,
    )
    return success_response(report, meta={"data_range": storage.get_orders_date_range()})


@couriers_bp.route("/report/orders", methods=["GET"])
@section_required("courier_payouts")
def get_report_orders():
    """
    Расшифровка суммы по заказам — чтобы выплату можно было проверить.

    Query: date_from, date_to, city, site, courier_id | without_courier=1
           | cancelled=1
    """
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")

    for value, name in ((date_from, "date_from"), (date_to, "date_to")):
        if value and not _valid_date(value):
            return error_response(f"{name} должен быть в формате YYYY-MM-DD")

    courier_id = request.args.get("courier_id")
    if courier_id:
        try:
            courier_id = int(courier_id)
        except (TypeError, ValueError):
            return error_response("courier_id должен быть числом")
    else:
        courier_id = None

    orders = storage.list_orders(
        date_from=date_from,
        date_to=date_to,
        city=request.args.get("city") or None,
        site_code=request.args.get("site") or None,
        courier_id=courier_id,
        without_courier=request.args.get("without_courier") == "1",
        cancelled=request.args.get("cancelled") == "1",
    )
    return success_response(orders)


@couriers_bp.route("/cities", methods=["GET"])
@section_required("courier_payouts")
def get_cities():
    """Города салонов, по которым есть данные."""
    return success_response(storage.list_cities())


@couriers_bp.route("/list", methods=["GET"])
@section_required("courier_payouts")
def get_couriers_list():
    """Справочник курьеров с флагом «служба доставки»."""
    only_active = request.args.get("only_active") == "1"
    return success_response(storage.list_couriers(only_active=only_active))


@couriers_bp.route("/<int:courier_id>/flag", methods=["POST"])
@role_required("admin")
def set_courier_flag(courier_id: int):
    """
    Пометить курьера службой доставки (или снять пометку).

    Признак «свой / служба» проставляется эвристикой по имени при первой
    загрузке справочника, а дальше правится только руками — синхронизация
    ручное решение не перетирает.
    """
    data = request.get_json(silent=True) or {}
    if "is_service" not in data:
        return error_response("Не передан is_service")

    if not storage.set_courier_service_flag(courier_id, bool(data["is_service"])):
        return error_response("Курьер не найден", 404)

    return success_response({"id": courier_id, "is_service": bool(data["is_service"])})


@couriers_bp.route("/<int:courier_id>/taxi-flag", methods=["POST"])
@role_required("admin")
@require_ajax_header
def set_courier_taxi(courier_id: int):
    """
    Пометить курьера внешней такси-службой — от этого флага считается показатель
    «доля заказов, отданных такси-службам» в разделе «Показатели салонов».

    Отдельно от is_service намеренно: тот флаг шире (Купер, Flowwow, «Общий»).
    """
    data = request.get_json(silent=True) or {}
    if "is_external_taxi" not in data:
        return error_response("Не передан is_external_taxi")

    value = bool(data["is_external_taxi"])
    if not storage.set_courier_taxi_flag(courier_id, value):
        return error_response("Курьер не найден", 404)

    return success_response({"id": courier_id, "is_external_taxi": value})


@couriers_bp.route("/delivery-types", methods=["GET"])
@section_required("courier_payouts")
def get_delivery_types():
    """Типы доставки с флагом «считается курьерской»."""
    return success_response(storage.list_delivery_types())


@couriers_bp.route("/delivery-types/<path:code>/flag", methods=["POST"])
@role_required("admin")
@require_ajax_header
def set_delivery_type(code: str):
    """Отметить тип доставки как курьерский (или снять отметку)."""
    data = request.get_json(silent=True) or {}
    if "counts_as_courier" not in data:
        return error_response("Не передан counts_as_courier")

    value = bool(data["counts_as_courier"])
    if not storage.set_delivery_type_flag(code, value):
        return error_response("Тип доставки не найден", 404)

    return success_response({"code": code, "counts_as_courier": value})


# ============================================================================
# Справочник весов товаров (модуль «Загрузка салонов»)
#
# Живёт в этом модуле, а не в отдельном: веса лежат в одной базе с позициями
# заказов, и пересчёт нагрузки после правки веса — один SQL. В отдельной базе
# он превратился бы в выгрузку тысяч строк в Python.
# ============================================================================

# Окно, за которое собирается справочник: товар, не встречавшийся в заказах
# два месяца, взвешивать незачем — ассортимент меняется.
WEIGHTS_WINDOW_DAYS = 60

# Потолок пачки: защита от «проставить вес всему справочнику одним запросом»,
# который на медленном диске займёт воркер на минуты.
MAX_WEIGHTS_BATCH = 500


def _weights_window() -> tuple:
    """Окно для СПИСКА товаров: какие позиции встречались в заказах."""
    today = date.today()
    return (today - timedelta(days=WEIGHTS_WINDOW_DAYS)).isoformat(), today.isoformat()


def _recalc_window() -> tuple:
    """
    Окно для ПЕРЕСЧЁТА минут после правки нормы или тарифа.

    Обязательно включает будущее. Сетка нагрузки показывает сегодня и
    ближайшие дни, то есть живёт целиком на будущих заказах, а пересчёт по
    окну «последние 60 дней» их не касался вовсе: человек сохранял норму,
    открывал экран и не видел никаких изменений — до следующего прогона
    синхронизации, то есть до получаса.
    """
    return _window(WEIGHTS_WINDOW_DAYS, FUTURE_WINDOW_DAYS)


@couriers_bp.route("/weights", methods=["GET"])
@section_required("salon_load")
def get_weights_catalog():
    """
    Справочник надбавок: товары из заказов за 60 дней с их трудоёмкостью.

    only_missing=1 — вкладка «без надбавки»: сортировка по числу заказов, чтобы
    человек начинал с того, что реально влияет на нагрузку.
    """
    only_missing = request.args.get("only_missing") in ("1", "true")
    search = (request.args.get("q") or "").strip() or None
    date_from, date_to = _weights_window()

    return success_response(
        storage.list_weight_catalog(date_from, date_to, only_missing=only_missing, search=search),
        meta={
            "period": {"from": date_from, "to": date_to},
            "coverage": storage.weights_coverage(date_from, date_to),
        },
    )


@couriers_bp.route("/weights", methods=["POST"])
@role_required("admin")
@require_ajax_header
def save_weights():
    """
    Проставить надбавки пачкой:
    {"weights": {"55648": {"weight": 0.2, "basis": "g100"}, "55925": null}}.

    Число вместо объекта — база «за штуку» (совместимость со старым вызовом).
    null снимает надбавку: товар перестаёт добавлять что-либо к базе за сборку.
    Ноль запрещён — «надбавки нет» выражается отсутствием строки, а не нулём.
    """
    data = request.get_json(silent=True) or {}
    raw = data.get("weights")
    if not isinstance(raw, dict) or not raw:
        return error_response("Не передан weights")
    if len(raw) > MAX_WEIGHTS_BATCH:
        return error_response(f"За раз можно проставить не больше {MAX_WEIGHTS_BATCH} товаров")

    weights = {}
    for key, value in raw.items():
        try:
            offer_id = int(key)
        except (TypeError, ValueError):
            return error_response(f"Некорректный идентификатор товара: {key}")
        if value is None:
            weights[offer_id] = None
            continue

        basis = storage.WEIGHT_BASIS_UNIT
        if isinstance(value, dict):
            basis = value.get("basis") or storage.WEIGHT_BASIS_UNIT
            value = value.get("weight")
        try:
            weight = float(value)
        except (TypeError, ValueError):
            return error_response(f"Некорректная надбавка у товара {offer_id}: {value}")
        if weight <= 0:
            return error_response("Надбавка должна быть больше нуля: «работы нет» — это пустая "
                                  "строка справочника, а не нулевая надбавка")
        if basis not in storage.WEIGHT_BASES:
            return error_response(f"Неизвестная база начисления у товара {offer_id}: {basis}")
        weights[offer_id] = {"weight": weight, "basis": basis}

    username = getattr(current_user, "username", None)
    storage.set_product_weights(weights, username)

    # Надбавка меняет нагрузку задним числом — без пересчёта сетка показывала бы
    # старые числа до следующего синка.
    date_from, date_to = _weights_window()
    storage.recalc_weights_range(date_from, date_to)

    log_action(username, "salon_load_weights", f"товаров: {len(weights)}")
    return success_response({"updated": len(weights),
                             "coverage": storage.weights_coverage(date_from, date_to)})


# ============================================================================
# Нормы времени сборки (Ф2 плана «нагрузка в минутах»)
# ============================================================================

def _norm_filters() -> dict:
    """
    Фильтры списка товаров из query-параметров.

    Нормы задаются только по товарам (групповые отменены владельцем
    2026-09-09: в одной группе CRM лежат товары с сильно разным временем
    сборки). Значит список длинный, и фильтры — не удобство, а единственный
    способ дойти до нужной строки.
    """
    def number(name, cast):
        raw = request.args.get(name)
        if raw in (None, ""):
            return None
        try:
            return cast(raw)
        except (TypeError, ValueError):
            return None

    in_catalog = request.args.get("in_catalog")
    return {
        "only_missing": request.args.get("only_missing") in ("1", "true"),
        "search": (request.args.get("q") or "").strip() or None,
        "role": (request.args.get("role") or "").strip() or None,
        "unit_code": (request.args.get("unit") or "").strip() or None,
        "in_catalog": None if in_catalog in (None, "") else in_catalog in ("1", "true"),
        "min_orders": number("min_orders", int),
        "max_orders": number("max_orders", int),
        "min_median": number("min_median", float),
        "max_median": number("max_median", float),
    }


@couriers_bp.route("/time-norms/offers", methods=["GET"])
@section_required("salon_load")
def get_offer_norms():
    """
    Товары из заказов за 60 дней: норма и факты о товаре.

    only_missing=1 — вкладка «без нормы», отсортированная по числу заказов:
    размечать нужно начиная с того, что реально влияет на нагрузку.
    """
    date_from, date_to = _weights_window()
    return success_response(
        storage.norm_catalog(date_from, date_to, **_norm_filters()),
        meta={
            "period": {"from": date_from, "to": date_to},
            "coverage": storage.norms_coverage(date_from, date_to),
            "roles": list(storage.ROLES),
            "bases": list(storage.BASES),
            "berry_modes": list(storage.BERRY_MODES),
            "catalog": storage.catalog_snapshot(),
        },
    )


# Заголовки выгрузки. Первая колонка — ключ: по ней импорт находит товар, и
# без неё файл бесполезен. Остальные справочные, импорт их не читает.
NORMS_CSV_HEADERS = [
    "offer_id", "Артикул", "Товар", "Заказов", "Кол-во в позиции", "Ед.",
    "Роль", "Минут", "За что", "Клубника",
]
# Импорт читает колонки по ЗАГОЛОВКУ, а не по номеру (разбор файла — в
# salon-load.js, parseNormsCsv): человек в Excel переставляет столбцы и
# вставляет свои, и разбор по номеру начал бы писать время в поле роли.
# Заголовки берутся отсюда же, поэтому переименование колонки видно сразу.


def _csv_safe(value: Optional[str]) -> str:
    """
    Обезвредить значение, которое Excel примет за формулу.

    Название товара приходит из CRM, а туда его вводит человек. Значение,
    начинающееся с `=`, `+`, `-` или `@`, Excel исполняет как формулу при
    открытии файла — и открывает файл не тот, кто это ввёл. Апостроф впереди
    делает ячейку текстовой; на экране его не видно, а при обратном импорте он
    роли не играет: мы читаем оттуда только offer_id и служебные коды.
    """
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


@couriers_bp.route("/time-norms/export", methods=["GET"])
@section_required("salon_load")
def export_norms():
    """
    Выгрузка норм в файл для Excel.

    CSV с разделителем «;» и BOM, а не xlsx: Excel открывает такой файл
    двойным кликом и сохраняет обратно в том же виде, а openpyxl ради двух
    кнопок на прод тащить не нужно (тот же приём, что в модуле «Ссылки
    товаров»).

    Выгружается ВСЁ, что попало под фильтры, без отсечки в 300 строк: человек
    иначе выгрузит верхушку, разметит её и решит, что закончил.
    """
    date_from, date_to = _weights_window()
    rows = storage.norm_catalog(date_from, date_to, all_rows=True, **_norm_filters())
    if not rows:
        # Пустой файл с одним заголовком выглядит как «выгрузка сломалась».
        # Говорим словами, что заказов в окне нет или фильтры слишком узкие.
        return error_response(
            f"За период {date_from} — {date_to} товаров не нашлось: "
            f"либо заказов ещё нет, либо фильтры слишком узкие", 404)

    buffer = io.StringIO()
    buffer.write("﻿")   # BOM: без него Excel читает кириллицу как «РўРѕРІР°СЂ»
    writer = csv.writer(buffer, delimiter=";", quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    writer.writerow(NORMS_CSV_HEADERS)
    for row in rows:
        norm = row.get("norm") or {}
        writer.writerow([
            row["offer_id"], _csv_safe(row.get("article")), _csv_safe(row.get("product_name")),
            row["orders"],
            "" if row.get("median_quantity") is None else str(row["median_quantity"]).replace(".", ","),
            row.get("unit_code") or "",
            # Подписи, а не коды: файл открывает человек, и «catalog» ему
            # ничего не говорит — пустую ячейку он заполнит тем, что видел на
            # экране. Загрузка принимает и подпись, и код.
            storage.ROLE_LABELS.get(norm.get("role"), ""),
            "" if norm.get("minutes") is None else str(norm["minutes"]).replace(".", ","),
            storage.BASIS_LABELS.get(norm.get("basis"), ""),
            storage.BERRY_LABELS.get(norm.get("berry_mode"), ""),
        ])

    stamp = date.today().isoformat()
    return Response(
        buffer.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="normy-vremeni-{stamp}.csv"'},
    )


@couriers_bp.route("/time-norms/import", methods=["POST"])
@role_required("admin")
@require_ajax_header
def import_norms():
    """
    Загрузка норм из файла: {"rows": [{"offer_id": 55648, "role": "catalog", ...}]}

    Файл разбирает браузер, сюда приходят уже строки. Так сделано намеренно:
    иначе пришлось бы принимать multipart и гадать о кодировке файла на
    сервере, а Excel сохраняет CSV то в UTF-8, то в cp1251.

    Ошибочные строки возвращаются списком и не отменяют остальные.
    """
    data = request.get_json(silent=True) or {}
    rows = data.get("rows")
    if not isinstance(rows, list) or not rows:
        return error_response("Не передан список строк")
    if len(rows) > MAX_WEIGHTS_BATCH:
        return error_response(f"За раз можно загрузить не больше {MAX_WEIGHTS_BATCH} строк")

    username = getattr(current_user, "username", None)
    result = storage.set_time_norms_bulk(rows, username)

    log_action(username, "salon_load_norms_import",
               f"применено {result['applied']}, снято {result['cleared']}, "
               f"ошибок {len(result['errors'])}")

    date_from, date_to = _recalc_window()
    try:
        storage.recalc_minutes_range(date_from, date_to)
    except Exception as e:
        logger.error(f"Пересчёт минут после импорта норм не удался: {e}")

    return success_response({**result,
                             "coverage": storage.norms_coverage(date_from, date_to)})


@couriers_bp.route("/time-norms", methods=["POST"])
@role_required("admin")
@require_ajax_header
def save_time_norm():
    """
    Задать норму группе или товару:
    {"scope": "group", "scope_id": 5876, "role": "catalog", "minutes": 12,
     "basis": "unit", "berry_mode": null}

    role=null снимает норму — запись удаляется целиком, и товар возвращается
    в «без нормы». Ноль минут снятием НЕ считается: «не размечено» и
    «размечено как бесплатное» — разные вещи, и счётчик занижения обязан их
    различать.
    """
    data = request.get_json(silent=True) or {}
    scope = data.get("scope")
    if scope not in storage.SCOPES:
        return error_response(f"Неизвестная область нормы: {scope}")
    try:
        scope_id = int(data.get("scope_id"))
    except (TypeError, ValueError):
        return error_response("Не передан scope_id")

    minutes = data.get("minutes")
    if minutes is not None:
        try:
            minutes = float(minutes)
        except (TypeError, ValueError):
            return error_response(f"Некорректное время: {data.get('minutes')}")

    username = getattr(current_user, "username", None)
    try:
        storage.set_time_norm(scope, scope_id, role=data.get("role"), minutes=minutes,
                              basis=data.get("basis"), berry_mode=data.get("berry_mode"),
                              username=username)
    except ValueError as e:
        return error_response(str(e))

    log_action(username, "salon_load_time_norm",
               f"{scope} {scope_id}: {data.get('role')} {minutes}")

    # Норма меняет время задним числом: без пересчёта экран показывал бы
    # старые минуты до следующего синка, и разметка выглядела бы бесполезной.
    date_from, date_to = _recalc_window()
    try:
        storage.recalc_minutes_range(date_from, date_to)
    except Exception as e:
        logger.error(f"Пересчёт минут после правки нормы не удался: {e}")

    return success_response({"coverage": storage.norms_coverage(date_from, date_to)})


@couriers_bp.route("/catalog/sync", methods=["POST"])
@role_required("admin")
@require_ajax_header
def sync_catalog_now():
    """
    Обновить каталог номенклатуры прямо сейчас.

    Штатно каталог тянется ночным глубоким прогоном — он меняется медленно, и
    6 запросов на каждый получасовой тик не нужны. Но в двух случаях ждать до
    ночи нельзя: сразу после первого деплоя (каталог пуст, размечать нечего) и
    когда в CRM завели новую группу, а норму нужно поставить сегодня.

    Синхронно, а не в фоне: 6 запросов и ~8 секунд на живых данных (замер
    2026-09-08), и человеку важно увидеть результат, а не «запущено».
    Консоли у контейнера на этом тарифе нет, поэтому разовая операция — ручка.
    """
    if not retailcrm.is_configured():
        return error_response("RetailCRM не настроен: задайте RETAILCRM_URL и RETAILCRM_API_KEY", 503)

    client = retailcrm.get_client()
    try:
        groups = client.get_product_groups()
        offers, links = [], []
        for page in client.iter_products(deadline=time.monotonic() + SYNC_BUDGET_SECONDS):
            page_offers, page_links = retailcrm.parse_catalog_page(page)
            offers.extend(page_offers)
            links.extend(page_links)
        result = storage.replace_catalog(groups, offers, links)
    except storage.EmptyCatalogError as e:
        # Пустой ответ каталог не перезаписывает — это ошибка, а не «товаров нет».
        return error_response(str(e), 502)
    except retailcrm.RetailCRMError as e:
        return error_response(f"CRM не ответила: {e}", 502)

    log_action(getattr(current_user, "username", None), "salon_load_catalog_sync",
               f"групп {result['groups']}, офферов {result['offers']}")
    return success_response({**result, "catalog": storage.catalog_snapshot()})


@couriers_bp.route("/time-norms/tariffs", methods=["GET"])
@section_required("salon_load")
def get_tariffs():
    """Тарифная сетка: время сборки от количества."""
    flowers, berries = storage.load_tariffs()
    return success_response({"flowers": flowers, "berries": berries})


@couriers_bp.route("/time-norms/tariffs", methods=["POST"])
@role_required("admin")
@require_ajax_header
def save_tariff():
    """
    Правка тарифа:
    {"kind": "flowers", "range_from": 3, "range_to": 17, "mono_minutes": 0.5,
     "mix_minutes": 0.6, "ribbon_minutes": 5, "package_minutes": 10}
    {"kind": "berries", "mode": "bouquet", "minutes_per_100g": 5, "package_minutes": 10}

    Сетка лежит в базе именно ради этой ручки: в первой же присланной таблице
    была опечатка, и правка тарифа не должна стоить деплоя.
    """
    data = request.get_json(silent=True) or {}
    username = getattr(current_user, "username", None)

    try:
        if data.get("kind") == "berries":
            storage.set_berry_tariff(data.get("mode"),
                                     float(data.get("minutes_per_100g")),
                                     float(data.get("package_minutes")), username)
        elif data.get("kind") == "flowers":
            mix = data.get("mix_minutes")
            storage.set_flower_tariff(int(data.get("range_from")), int(data.get("range_to")),
                                      float(data.get("mono_minutes")),
                                      None if mix in (None, "") else float(mix),
                                      float(data.get("ribbon_minutes")),
                                      float(data.get("package_minutes")), username)
        else:
            return error_response("Неизвестный вид тарифа")
    except (TypeError, ValueError) as e:
        return error_response(str(e))

    log_action(username, "salon_load_tariff", str(data)[:200])
    date_from, date_to = _recalc_window()
    try:
        storage.recalc_minutes_range(date_from, date_to)
    except Exception as e:
        logger.error(f"Пересчёт минут после правки тарифа не удался: {e}")

    flowers, berries = storage.load_tariffs()
    return success_response({"flowers": flowers, "berries": berries})


@couriers_bp.route("/order-statuses", methods=["GET"])
@section_required("salon_load")
def get_order_statuses():
    """Статусы заказов с признаком «считается нагрузкой салона»."""
    return success_response(storage.list_order_statuses())


@couriers_bp.route("/order-statuses/<path:code>/flag", methods=["POST"])
@role_required("admin")
@require_ajax_header
def set_order_status_flag(code: str):
    """Отметить статус как нагрузку (или снять отметку)."""
    data = request.get_json(silent=True) or {}
    if "counts_as_load" not in data:
        return error_response("Не передан counts_as_load")

    value = bool(data["counts_as_load"])
    if not storage.set_order_status_load_flag(code, value):
        return error_response("Статус не найден", 404)

    log_action(current_user.username, "salon_load_status_flag", f"{code}: {value}")
    return success_response({"code": code, "counts_as_load": value})


# ============================================================================
# Синхронизация: запуск и статус
# ============================================================================

@couriers_bp.route("/sync", methods=["POST"])
@role_required("admin")
def trigger_sync():
    """
    Запустить синхронизацию в фоновом потоке.

    Body: date_from / date_to (YYYY-MM-DD) — необязательные, по умолчанию
    последние RECENT_WINDOW_DAYS дней по дате доставки.
    """
    if not retailcrm.is_configured():
        return error_response("RetailCRM не настроен: задайте RETAILCRM_URL и RETAILCRM_API_KEY", 503)

    data = request.get_json(silent=True) or {}
    date_from = data.get("date_from")
    date_to = data.get("date_to")

    if date_from or date_to:
        if not (_valid_date(date_from) and _valid_date(date_to)):
            return error_response("date_from и date_to должны быть в формате YYYY-MM-DD")
        if date_from > date_to:
            return error_response("date_from позже date_to")
        span = (
            datetime.strptime(date_to, "%Y-%m-%d") - datetime.strptime(date_from, "%Y-%m-%d")
        ).days
        if span > MAX_MANUAL_PERIOD_DAYS:
            return error_response(f"Период больше {MAX_MANUAL_PERIOD_DAYS} дней")
    else:
        date_from, date_to = _window(RECENT_WINDOW_DAYS)

    last_log = storage.get_latest_sync_log()

    def sync_in_background():
        _run_sync(date_from, date_to)

    threading.Thread(target=sync_in_background, daemon=True).start()

    return jsonify({
        "success": True,
        "message": f"Синхронизация запущена: {date_from} — {date_to}",
        # Прогон за неделю укладывается в секунды, и первый же опрос статуса мог
        # застать ещё не начавшийся синк, увидеть прошлый завершённый лог и
        # отрапортовать ложное «готово». Фронтенд ждёт лога с id больше этого.
        "prev_log_id": last_log["id"] if last_log else 0,
    })


def _sync_log_is_stale(log: dict) -> bool:
    """Прогон в статусе started, но давно не подававший признаков жизни, —
    это поток, не переживший перезапуск воркера (деплой, OOM)."""
    started_at = log.get("started_at")
    if not started_at:
        return True
    try:
        started = datetime.strptime(started_at, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return True
    return (datetime.utcnow() - started) > timedelta(seconds=SYNC_BUDGET_SECONDS + 300)


@couriers_bp.route("/sync-status", methods=["GET"])
@login_required
def get_sync_status():
    """Статус синхронизации (из sync_log — общий для всех воркеров)."""
    last_log = storage.get_latest_sync_log()

    started = bool(last_log and last_log["status"] == "started")
    stale = started and _sync_log_is_stale(last_log)
    running = started and not stale
    error = last_log["error_message"] if last_log and last_log["status"] == "failed" else None
    if stale:
        error = "Синхронизация прервана (перезапуск сервера), запустите заново"

    if running:
        message = f"Загружено {last_log['records_count'] or 0} заказов..."
    elif error:
        message = f"Ошибка: {error}"
    elif last_log:
        message = f"Синхронизировано {last_log['records_count']} заказов"
    else:
        message = "Данные ещё не загружались"

    last_success = storage.get_latest_sync_log(status="completed")

    return jsonify({
        "success": True,
        "status": {
            "running": running,
            "error": error,
            "message": message,
            "log_id": last_log["id"] if last_log else 0,
            "last_sync": last_log["finished_at"] if last_log else None,
            "last_success": last_success["finished_at"] if last_success else None,
            "data_range": storage.get_orders_date_range(),
            "configured": retailcrm.is_configured(),
        },
    })
