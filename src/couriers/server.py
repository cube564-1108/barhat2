"""
JSON API модуля «Оплата курьерам».

Blueprint регистрируется в src/pyrus/server.py (мастер-приложение) — тот же
паттерн, что cashshifts_bp / moysklad_bp / writeoffs_bp.

Отчёт читается ИСКЛЮЧИТЕЛЬНО из локальной базы: живые запросы к внешнему API
из обработчика уже дважды укладывали прод (воркеров всего 2). В CRM ходит
только фоновый синк под локом.
"""

import logging
import os
import re
import sys
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from flask import Blueprint, jsonify, request
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


def _sync_range(date_from: str, date_to: str) -> int:
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
        _sync_range(date_from, date_to)
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

    Query: date_from, date_to (YYYY-MM-DD), city, only_own (1/0)
    """
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")

    for value, name in ((date_from, "date_from"), (date_to, "date_to")):
        if value and not _valid_date(value):
            return error_response(f"{name} должен быть в формате YYYY-MM-DD")

    city = request.args.get("city") or None
    only_own = request.args.get("only_own", "1") != "0"

    report = storage.report_by_courier(
        date_from=date_from, date_to=date_to, city=city, only_own=only_own
    )
    return success_response(report, meta={"data_range": storage.get_orders_date_range()})


@couriers_bp.route("/report/orders", methods=["GET"])
@section_required("courier_payouts")
def get_report_orders():
    """
    Расшифровка суммы по заказам — чтобы выплату можно было проверить.

    Query: date_from, date_to, city, courier_id | without_courier=1
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
        courier_id=courier_id,
        without_courier=request.args.get("without_courier") == "1",
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
    today = date.today()
    return (today - timedelta(days=WEIGHTS_WINDOW_DAYS)).isoformat(), today.isoformat()


@couriers_bp.route("/weights", methods=["GET"])
@section_required("salon_load")
def get_weights_catalog():
    """
    Справочник весов: товары из заказов за 60 дней с их трудоёмкостью.

    only_missing=1 — вкладка «требуют веса»: сортировка по числу заказов, чтобы
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
    Проставить вес пачкой: {"weights": {"55648": 4.0, "55925": null}}.

    null снимает вес — товар возвращается в «требуют веса» и считается по весу
    по умолчанию. Ноль запрещён: «работы нет» и «вес не задан» это разные
    вещи, и молчаливый ноль занижает нагрузку незаметно.
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
        try:
            weight = float(value)
        except (TypeError, ValueError):
            return error_response(f"Некорректный вес у товара {offer_id}: {value}")
        if weight <= 0:
            return error_response("Вес должен быть больше нуля: «работы нет» — это отсутствие товара, "
                                  "а не нулевой вес")
        weights[offer_id] = weight

    username = getattr(current_user, "username", None)
    storage.set_product_weights(weights, username)

    # Вес меняет нагрузку задним числом — без пересчёта сетка показывала бы
    # старые числа до следующего синка.
    date_from, date_to = _weights_window()
    storage.recalc_weights_range(date_from, date_to)

    log_action(username, "salon_load_weights", f"товаров: {len(weights)}")
    return success_response({"updated": len(weights),
                             "coverage": storage.weights_coverage(date_from, date_to)})


@couriers_bp.route("/weights/default", methods=["POST"])
@role_required("admin")
@require_ajax_header
def save_default_weight():
    """Вес товара без проставленной трудоёмкости. Задаётся в интерфейсе."""
    data = request.get_json(silent=True) or {}
    try:
        value = float(data.get("weight"))
    except (TypeError, ValueError):
        return error_response("Некорректный вес")
    if value <= 0:
        return error_response("Вес по умолчанию должен быть больше нуля")

    storage.set_default_weight(value)
    date_from, date_to = _weights_window()
    storage.recalc_weights_range(date_from, date_to)

    log_action(current_user.username, "salon_load_default_weight", f"вес: {value}")
    return success_response({"default_weight": value,
                             "coverage": storage.weights_coverage(date_from, date_to)})


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
