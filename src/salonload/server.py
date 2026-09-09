"""
JSON API раздела «Загрузка салонов».

Blueprint регистрируется в src/pyrus/server.py — тот же паттерн, что
salonkpi_bp / couriers_bp.

Доступ режется по данным, а не по меню: `store_id` приходит от клиента, и без
проверки принадлежности флорист прочитал бы чужой салон простым подбором
числа. Поэтому проверка стоит на КАЖДОЙ ручке, а не только в списке.
"""

import logging
import os
import sys
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from flask import Blueprint, jsonify, request
from flask_login import current_user

auth_path = os.path.join(os.path.dirname(__file__), "../")
sys.path.insert(0, auth_path)
from auth import log_action, require_ajax_header, role_required, section_required  # noqa: E402

# Окно, по которому считается покрытие разметки перед переходом на минуты, и
# доля недосчитанных заказов, выше которой переключение требует подтверждения.
# Порог предупреждает, а не запрещает: в потоке всегда есть хвост, и жёсткая
# блокировка означала бы «никогда» (К16 критики плана).
COVERAGE_DAYS = 60
COVERAGE_THRESHOLD = 10.0

from . import metrics, storage  # noqa: E402

logger = logging.getLogger(__name__)

salonload_bp = Blueprint("salonload", __name__, url_prefix="/api/salon-load")

# Сетка открывается и переоткрывается часто, а каждый запрос к /data стоит
# 90–700 мс и трогает две базы. 60 секунд — компромисс между «не долбить диск»
# и «правка ёмкости видна сразу».
CACHE_TTL_SECONDS = 60

# Максимум дней в календаре: защита от «покажи год» одним запросом.
MAX_WEEK_DAYS = 42

_cache: Dict[str, Any] = {}
_cache_lock = threading.Lock()
# Версия справочников. Правка ёмкости поднимает её, и старый ответ из кэша
# больше не отдаётся: иначе человек правит ёмкость, минуту видит старые
# проценты и решает, что не сохранилось.
#
# На проде воркеров два, и соседний про эту правку не знает — там ответ
# обновится по TTL. Это осознанный предел: разделяемая инвалидация стоила бы
# ещё одной записи в общую базу на каждое изменение.
_version = 0


def error_response(message: str, status: int = 400):
    return jsonify({"success": False, "error": message}), status


def success_response(payload: dict):
    data = {"success": True}
    data.update(payload)
    return jsonify(data)


def _bump_version() -> None:
    global _version
    with _cache_lock:
        _version += 1
        _cache.clear()


def _cached(key: str, builder):
    now = time.monotonic()
    full_key = f"{_version}:{key}"
    with _cache_lock:
        hit = _cache.get(full_key)
        if hit and hit["expires"] > now:
            return hit["value"]

    value = builder()

    with _cache_lock:
        _cache[full_key] = {"value": value, "expires": now + CACHE_TTL_SECONDS}
        # Кэш маленький по смыслу (день × набор салонов), но чистим накопленное:
        # процесс живёт сутками, а ключей за день набегает много.
        if len(_cache) > 200:
            for stale_key in [k for k, v in _cache.items() if v["expires"] <= now]:
                _cache.pop(stale_key, None)
    return value


def _allowed_store_ids() -> Optional[List[int]]:
    """
    Салоны, доступные текущему пользователю.

    None — все (администратор). Пустой список — салонов не привязано; это
    отдельное состояние, а не «нет данных»: у учётки из Пульса привязок нет по
    умолчанию, и ветка «свои салоны» при пустом списке не исполняется —
    так уже ловили 500 на соседнем разделе.
    """
    if getattr(current_user, "role", None) == "admin":
        return None
    try:
        from cashshifts.storage import get_user_stores
        return get_user_stores(current_user.username)
    except Exception as e:
        logger.error(f"Не удалось получить салоны пользователя: {e}")
        return []


def _check_store_access(store_id: int) -> Optional[tuple]:
    """None — доступ есть; иначе готовый ответ 403."""
    allowed = _allowed_store_ids()
    if allowed is not None and store_id not in allowed:
        return error_response("Нет доступа к этому салону", 403)
    return None


def _cache_key(prefix: str, store_ids: Optional[List[int]], *parts) -> str:
    scope = "all" if store_ids is None else ",".join(str(i) for i in sorted(store_ids))
    return ":".join([prefix, scope, *(str(p) for p in parts)])


def _is_admin() -> bool:
    return getattr(current_user, "role", None) == "admin"


# ============================================================================
# Сетка нагрузки
# ============================================================================

@salonload_bp.route("/day", methods=["GET"])
@section_required("salon_load")
def get_day():
    """Сетка «часы × салоны» за день."""
    day = request.args.get("date") or metrics.today_iso()
    if not metrics.valid_date(day):
        return error_response("date должен быть в формате YYYY-MM-DD")

    store_ids = _allowed_store_ids()
    data = _cached(_cache_key("day", store_ids, day), lambda: metrics.day_grid(day, store_ids))
    return success_response({"data": {**data, "can_edit": _is_admin()}})


@salonload_bp.route("/week", methods=["GET"])
@section_required("salon_load")
def get_week():
    """Календарь дневной загрузки."""
    date_from = request.args.get("from") or metrics.today_iso()
    if not metrics.valid_date(date_from):
        return error_response("from должен быть в формате YYYY-MM-DD")

    try:
        days = int(request.args.get("days", 7))
    except (TypeError, ValueError):
        return error_response("days должен быть числом")
    if not (1 <= days <= MAX_WEEK_DAYS):
        return error_response(f"days должен быть от 1 до {MAX_WEEK_DAYS}")

    store_ids = _allowed_store_ids()
    data = _cached(_cache_key("week", store_ids, date_from, days),
                   lambda: metrics.week_grid(date_from, days, store_ids))
    return success_response({"data": data})


@salonload_bp.route("/slot", methods=["GET"])
@section_required("salon_load")
def get_slot():
    """Заказы одного слота. hour не передан — заказы без времени."""
    day = request.args.get("date")
    if not metrics.valid_date(day):
        return error_response("date должен быть в формате YYYY-MM-DD")

    try:
        store_id = int(request.args.get("store_id"))
    except (TypeError, ValueError):
        return error_response("Не передан store_id")

    denied = _check_store_access(store_id)
    if denied:
        return denied

    hour_raw = request.args.get("hour")
    hour = None
    if hour_raw not in (None, "", "null"):
        try:
            hour = int(hour_raw)
        except (TypeError, ValueError):
            return error_response("hour должен быть числом")
        if not 0 <= hour <= 23:
            return error_response("hour должен быть от 0 до 23")

    return success_response({"data": metrics.slot_orders(day, store_id, hour)})


@salonload_bp.route("/free-slots", methods=["GET"])
@section_required("salon_load")
def get_free_slots():
    """Ближайшие слоты с запасом — что предложить клиенту вместо перегруженного."""
    try:
        store_id = int(request.args.get("store_id"))
    except (TypeError, ValueError):
        return error_response("Не передан store_id")

    denied = _check_store_access(store_id)
    if denied:
        return denied

    date_from = request.args.get("from") or metrics.today_iso()
    if not metrics.valid_date(date_from):
        return error_response("from должен быть в формате YYYY-MM-DD")

    try:
        days = int(request.args.get("days", 3))
        need_units = float(request.args.get("units", 1))
    except (TypeError, ValueError):
        return error_response("days и units должны быть числами")
    if not (1 <= days <= 14):
        return error_response("days должен быть от 1 до 14")

    return success_response({"data": metrics.free_slots(store_id, date_from, days, need_units)})


@salonload_bp.route("/stores", methods=["GET"])
@section_required("salon_load")
def get_stores():
    """Салоны, доступные пользователю, с признаком «ёмкость задана»."""
    store_ids = _allowed_store_ids()
    from salonkpi import storage as salonkpi_storage

    with_capacity = set(storage.stores_with_capacity())
    stores = [
        {**store, "has_capacity": store["id"] in with_capacity}
        for store in salonkpi_storage.list_stores(store_ids, only_linked=True)
    ]
    return success_response({"stores": stores, "can_edit": _is_admin()})


# ============================================================================
# Ёмкость
# ============================================================================

@salonload_bp.route("/alerts", methods=["GET"])
@section_required("salon_load")
def get_alerts():
    """
    Активные предупреждения о перегрузе с альтернативой «куда перенести».

    Плюс признак «синк давно не проходил»: молчание модуля не значит «всё
    спокойно», и на экране это должно читаться по-разному.
    """
    store_ids = _allowed_store_ids()
    data = metrics.alerts(store_ids)
    data["stale_sync"] = metrics.sync_is_stale()
    return success_response({"data": data})


@salonload_bp.route("/alerts/<int:alert_id>/dismiss", methods=["POST"])
@section_required("salon_load")
@require_ajax_header
def dismiss_alert(alert_id: int):
    """Снять предупреждение: разобрались, больше не показывать."""
    if not storage.dismiss_alert(alert_id, _allowed_store_ids()):
        return error_response("Предупреждение не найдено или относится к чужому салону", 404)
    _bump_version()
    return success_response({"dismissed": alert_id})


@salonload_bp.route("/alerts/scan", methods=["POST"])
@role_required("admin")
@require_ajax_header
def run_alert_scan():
    """Пересчитать предупреждения руками — обычно это делает синк."""
    result = metrics.scan_alerts()
    _bump_version()
    return success_response({"data": result})


@salonload_bp.route("/capacity/suggest", methods=["GET"])
@section_required("salon_load")
def get_capacity_suggestion():
    """
    Норма из факта: сколько салон реально собирал в час за месяц.

    Только предложение. Применяет человек: заниженная норма — это постоянный
    ложный перегруз, после которого на модуль перестают смотреть.
    """
    try:
        store_id = int(request.args.get("store_id"))
    except (TypeError, ValueError):
        return error_response("Не передан store_id")

    denied = _check_store_access(store_id)
    if denied:
        return denied

    return success_response({"data": metrics.suggest_capacity(store_id)})


@salonload_bp.route("/timezones", methods=["POST"])
@role_required("admin")
@require_ajax_header
def save_timezone():
    """Часовой пояс салона: от него считается «перегруз через 3 часа»."""
    data = request.get_json(silent=True) or {}
    try:
        store_id = int(data.get("store_id"))
        offset = int(data.get("utc_offset"))
    except (TypeError, ValueError):
        return error_response("Нужны store_id и utc_offset")

    try:
        storage.set_timezone(store_id, offset, getattr(current_user, "username", None))
    except ValueError as e:
        return error_response(str(e))

    log_action(current_user.username, "salon_load_timezone", f"салон {store_id}: UTC+{offset}")
    return success_response({"store_id": store_id, "utc_offset": offset})


@salonload_bp.route("/capacity", methods=["GET"])
@section_required("salon_load")
def get_capacity():
    """Недельная сетка ёмкости салона. Менеджер видит, правит только админ."""
    try:
        store_id = int(request.args.get("store_id"))
    except (TypeError, ValueError):
        return error_response("Не передан store_id")

    denied = _check_store_access(store_id)
    if denied:
        return denied

    return success_response({
        "data": {
            "store_id": store_id,
            "hours": list(storage.HOURS),
            "grid": storage.weekly_grid(store_id),
            "can_edit": _is_admin(),
        }
    })


@salonload_bp.route("/model", methods=["GET"])
@section_required("salon_load")
def get_load_model():
    """
    Активная модель нагрузки и готовность к переходу на минуты.

    Покрытие считается здесь же: переключать модель вслепую нельзя — 76%
    потока это каталожные товары, и без разметки экран станет пустым.
    """
    from couriers import storage as couriers_storage

    date_to = date.today().isoformat()
    date_from = (date.today() - timedelta(days=COVERAGE_DAYS)).isoformat()
    coverage = couriers_storage.norms_coverage(date_from, date_to)

    status = storage.capacity_model_status()
    florist_stores = sum(1 for value in status.values() if value["florist_hours"])

    return success_response({
        "data": {
            "model": storage.get_load_model(),
            "can_edit": _is_admin(),
            "coverage": {**coverage, "from": date_from, "to": date_to,
                         "days": COVERAGE_DAYS, "threshold": COVERAGE_THRESHOLD},
            "stores_with_florists": florist_stores,
            "stores_total": len(status),
        }
    })


@salonload_bp.route("/model", methods=["POST"])
@role_required("admin")
@require_ajax_header
def save_load_model():
    """
    Переключить модель нагрузки: {"model": "minutes"}.

    Порог покрытия ПРЕДУПРЕЖДАЕТ, но не запрещает (К16 критики плана): в потоке
    всегда есть неразмеченный хвост, и жёсткий порог заблокировал бы переход
    навсегда. Решение принимает человек — но подтверждением, а не случайно,
    поэтому ниже порога требуется явный `confirm`.
    """
    data = request.get_json(silent=True) or {}
    model = (data.get("model") or "").strip()

    if model == storage.LOAD_MODEL_MINUTES and not data.get("confirm"):
        from couriers import storage as couriers_storage

        date_to = date.today().isoformat()
        date_from = (date.today() - timedelta(days=COVERAGE_DAYS)).isoformat()
        coverage = couriers_storage.norms_coverage(date_from, date_to)
        if coverage["share"] > COVERAGE_THRESHOLD:
            return error_response(
                f"За {COVERAGE_DAYS} дней в {coverage['share']}% заказов есть позиции без нормы "
                f"({coverage['offers_without_norm']} товаров). Загрузка будет занижена. "
                "Подтвердите переключение, если это осознанное решение.", 409)

    try:
        storage.set_load_model(model, getattr(current_user, "username", None))
    except ValueError as e:
        return error_response(str(e))

    _bump_version()
    log_action(current_user.username, "salon_load_model", f"модель нагрузки: {model}")
    return success_response({"model": model})


@salonload_bp.route("/capacity/model", methods=["GET"])
@section_required("salon_load")
def get_capacity_model():
    """
    Кто ещё сидит на старых единицах ёмкости.

    Отсюда берётся плашка «ёмкость задана в старых единицах — перезадайте».
    Конвертировать «6 единиц» в «6 флористов» нельзя: это разные величины, и
    молчаливый перевод соврал бы в цифре, на которой стоит весь экран.
    """
    from salonkpi import storage as salonkpi_storage

    allowed = _allowed_store_ids()
    status = storage.capacity_model_status()
    names = {store["id"]: store["name"]
             for store in salonkpi_storage.list_stores(allowed, only_linked=True)}

    stale = [{"store_id": store_id, "store_name": names[store_id],
              "units_hours": value["units_hours"]}
             for store_id, value in sorted(status.items())
             if store_id in names and value["units_hours"] and not value["florist_hours"]]
    # Рабочие часы без старой ёмкости: до Ф6 они серые в сетке. Это не то же
    # самое, что «салон на старых единицах», и лечится другим действием —
    # заполнить «единиц в час», а не «флористов».
    gaps = [{"store_id": store_id, "store_name": names[store_id],
             "gap_hours": value["gap_hours"]}
            for store_id, value in sorted(status.items())
            if store_id in names and value["gap_hours"]]
    ready = [store_id for store_id, value in status.items()
             if store_id in names and value["florist_hours"]]

    return success_response({
        "data": {
            "stale": stale,
            "gaps": gaps,
            "ready_count": len(ready),
            "total": len(names),
            "minutes_per_florist_hour": storage.MINUTES_PER_FLORIST_HOUR,
        }
    })


@salonload_bp.route("/capacity", methods=["POST"])
@role_required("admin")
@require_ajax_header
def save_capacity():
    """
    Проставить ячейки недельной сетки:
    {"store_id": 1, "slots": [{"weekday": 0, "hour": 10, "capacity": 6}]}
    """
    data = request.get_json(silent=True) or {}
    try:
        store_id = int(data.get("store_id"))
    except (TypeError, ValueError):
        return error_response("Не передан store_id")

    slots = data.get("slots")
    if not isinstance(slots, list) or not slots:
        return error_response("Не переданы слоты")
    if len(slots) > 24 * 7:
        return error_response("За раз можно задать не больше недели")

    try:
        saved = storage.set_slots(store_id, slots, getattr(current_user, "username", None))
    except (ValueError, KeyError, TypeError) as e:
        return error_response(str(e))

    _bump_version()
    log_action(current_user.username, "salon_load_capacity", f"салон {store_id}: {saved} ячеек")
    return success_response({"saved": saved})


@salonload_bp.route("/capacity/working-hours", methods=["POST"])
@role_required("admin")
@require_ajax_header
def save_working_hours():
    """
    Часы работы салона одним движением: заполняет всю неделю.

    Это главный способ заполнения: сетка 7×24 на девять салонов — 1512 полей,
    и руками её никто не заполнит.
    """
    data = request.get_json(silent=True) or {}
    try:
        store_id = int(data.get("store_id"))
        open_hour = int(data.get("open_hour"))
        close_hour = int(data.get("close_hour"))
    except (TypeError, ValueError):
        return error_response("Нужны store_id, open_hour и close_hour")

    # Ёмкость приходит в флористах (новая модель) либо в старых единицах.
    # Обе необязательные по отдельности, но одна из двух обязана быть: без
    # этого форма молча заполнила бы неделю пустыми часами.
    try:
        capacity = None if data.get("capacity") in (None, "") else float(data["capacity"])
        florists = None if data.get("florists") in (None, "") else float(data["florists"])
    except (TypeError, ValueError):
        return error_response("Ёмкость и число флористов должны быть числами")
    if capacity is None and florists is None:
        return error_response("Укажите число флористов в смене")

    pickup = data.get("pickup_capacity")
    weekdays = data.get("weekdays")
    if weekdays is not None and (not isinstance(weekdays, list) or
                                 any(d not in storage.WEEKDAYS for d in weekdays)):
        return error_response("weekdays должен быть списком дней 0–6")

    try:
        saved = storage.apply_working_hours(
            store_id, open_hour, close_hour, capacity,
            None if pickup in (None, "") else float(pickup),
            weekdays, getattr(current_user, "username", None), florists)
    except ValueError as e:
        return error_response(str(e))

    _bump_version()
    amount = (f"{florists} флористов" if florists is not None else f"{capacity} ед/час")
    log_action(current_user.username, "salon_load_working_hours",
               f"салон {store_id}: {open_hour}:00–{close_hour}:00, {amount}")
    return success_response({"saved": saved})


@salonload_bp.route("/capacity/copy", methods=["POST"])
@role_required("admin")
@require_ajax_header
def copy_capacity():
    """Скопировать график одного салона в другой."""
    data = request.get_json(silent=True) or {}
    try:
        source_id = int(data.get("source_store_id"))
        target_id = int(data.get("target_store_id"))
    except (TypeError, ValueError):
        return error_response("Нужны source_store_id и target_store_id")
    if source_id == target_id:
        return error_response("Салон-источник и салон-приёмник совпадают")

    saved = storage.copy_week(source_id, target_id, getattr(current_user, "username", None))
    if not saved:
        return error_response("У салона-источника ёмкость не задана", 404)

    _bump_version()
    log_action(current_user.username, "salon_load_capacity_copy", f"{source_id} → {target_id}")
    return success_response({"saved": saved})


@salonload_bp.route("/exceptions", methods=["GET"])
@section_required("salon_load")
def get_exceptions():
    """Исключения на даты: праздник, отпуск, поломка."""
    date_from = request.args.get("from") or metrics.today_iso()
    # Сначала проверка, потом разбор: strptime на непроверенной строке падал
    # пятисоткой раньше, чем срабатывала валидация ниже.
    if not metrics.valid_date(date_from):
        return error_response("from должен быть в формате YYYY-MM-DD")

    date_to = request.args.get("to") or (
        datetime.strptime(date_from, "%Y-%m-%d").date() + timedelta(days=60)).isoformat()
    if not metrics.valid_date(date_to):
        return error_response("to должен быть в формате YYYY-MM-DD")

    store_ids = _allowed_store_ids()
    if store_ids is None:
        from salonkpi import storage as salonkpi_storage
        store_ids = [store["id"] for store in salonkpi_storage.list_stores(None, only_linked=True)]

    return success_response({
        "data": storage.list_exceptions(store_ids, date_from, date_to),
        "can_edit": _is_admin(),
    })


@salonload_bp.route("/exceptions", methods=["POST"])
@role_required("admin")
@require_ajax_header
def save_exception():
    """
    Исключение на дату: {"store_id": 1, "date": "2027-02-14", "capacity": 12,
    "hour": null, "reason": "14 февраля"}.

    hour=null — на весь день. capacity=null и closed=false снимает исключение.
    """
    data = request.get_json(silent=True) or {}
    try:
        store_id = int(data.get("store_id"))
    except (TypeError, ValueError):
        return error_response("Не передан store_id")

    day = data.get("date")
    if not metrics.valid_date(day):
        return error_response("date должен быть в формате YYYY-MM-DD")

    hour = data.get("hour")
    if hour is not None:
        try:
            hour = int(hour)
        except (TypeError, ValueError):
            return error_response("hour должен быть числом")
        if not 0 <= hour <= 23:
            return error_response("hour должен быть от 0 до 23")

    capacity = data.get("capacity")
    pickup = data.get("pickup_capacity")
    florists = data.get("florists")
    try:
        capacity = None if capacity in (None, "") else float(capacity)
        pickup = None if pickup in (None, "") else float(pickup)
        florists = None if florists in (None, "") else float(florists)
    except (TypeError, ValueError):
        return error_response("Ёмкость должна быть числом")
    if florists is not None and florists < 0:
        return error_response("Число флористов не может быть отрицательным")

    saved = storage.set_exception(store_id, day, hour, capacity, pickup,
                                  bool(data.get("closed")), (data.get("reason") or "").strip() or None,
                                  getattr(current_user, "username", None), florists)
    _bump_version()
    log_action(current_user.username, "salon_load_exception", f"салон {store_id}, {day}")
    return success_response({"saved": saved})
