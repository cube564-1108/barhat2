"""
Расчёт загрузки салонов: нагрузка / ёмкость / процент по слотам.

Правила, из-за которых этот файл выглядит сложнее, чем «поделить одно на
другое»:

  - «ёмкость не задана» — это None, а не ноль. Деление на ноль превращается в
    «перегруз ∞%», и на такой экран перестают смотреть на второй день;
  - «салон закрыт» — не ноль загрузки, а отдельное состояние ячейки;
  - заказы без часа готовности и заказы с непривязанным складом не
    выбрасываются и не размазываются по сетке: они отдаются отдельными
    строками, которые разбирает человек;
  - цвет ячейки дублируется числом (это уже в интерфейсе), а сюда кладётся
    доля веса, посчитанного «по умолчанию»: 140% и 140%-из-которых-60%-догадка
    — разные основания для того, чтобы звонить клиенту.
"""

import logging
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from couriers import storage as couriers_storage
from salonkpi import storage as salonkpi_storage

from . import storage

logger = logging.getLogger(__name__)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Пороги загрузки. Не ровные десятки: смысл, а не красота — «впритык» это когда
# запаса почти нет, «перегруз» когда салон физически не успевает. Значения
# заведомо будут двигаться по факту, поэтому лежат здесь одним местом, а не
# размазаны по CSS.
THRESHOLD_TIGHT = 85
THRESHOLD_OVER = 100


def valid_date(value: str) -> bool:
    return bool(value and _DATE_RE.match(value))


def today_iso() -> str:
    return date.today().isoformat()


def _percent(units: float, capacity: Optional[float]) -> Optional[float]:
    if capacity is None or capacity <= 0:
        return None
    return round(100.0 * units / capacity, 1)


def _level(percent: Optional[float], closed: bool) -> str:
    """Состояние ячейки одним словом — цвет выбирает интерфейс."""
    if closed:
        return "closed"
    if percent is None:
        return "unknown"
    if percent >= THRESHOLD_OVER:
        return "over"
    if percent >= THRESHOLD_TIGHT:
        return "tight"
    return "ok"


def _effective_capacity(store_id: int, day: str, weekday: int, hour: int,
                        weekly: Dict[str, Any], exceptions: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ёмкость слота: исключение на дату важнее недельного графика.

    Отсутствие обеих записей — это «не задана», а не ноль.
    """
    exception = exceptions.get(f"{store_id}:{day}:{hour}")
    if exception is not None:
        return {
            "capacity": exception["capacity"],
            "pickup_capacity": exception["pickup_capacity"],
            "closed": exception["closed"],
            "source": "exception",
            "reason": exception.get("reason"),
        }
    regular = weekly.get(f"{store_id}:{weekday}:{hour}")
    if regular is not None:
        return {
            "capacity": regular["capacity"],
            "pickup_capacity": regular["pickup_capacity"],
            "closed": regular["closed"],
            "source": "weekly",
            "reason": None,
        }
    return {"capacity": None, "pickup_capacity": None, "closed": False,
            "source": None, "reason": None}


def _stores_for(store_ids: Optional[List[int]]) -> List[Dict[str, Any]]:
    """Салоны, у которых есть связь со складом CRM, — остальные в сетке не нужны."""
    links = salonkpi_storage.resolve_map(salonkpi_storage.SOURCE_CRM_STORE)
    by_store: Dict[int, List[str]] = {}
    for key, store_id in links.items():
        by_store.setdefault(store_id, []).append(key)

    stores = []
    for store in salonkpi_storage.list_stores(store_ids):
        keys = by_store.get(store["id"])
        if not keys:
            continue
        stores.append({**store, "keys": keys})
    return stores


def day_grid(day: str, store_ids: Optional[List[int]] = None) -> Dict[str, Any]:
    """Сетка «часы × салоны» за один день."""
    stores = _stores_for(store_ids)
    ids = [store["id"] for store in stores]
    key_to_store = {key: store["id"] for store in stores for key in store["keys"]}

    weekday = datetime.strptime(day, "%Y-%m-%d").date().weekday()
    weekly = storage.capacity_map(ids)
    exceptions = storage.exceptions_for(ids, day, day)

    statuses = couriers_storage.load_status_codes()
    rows = couriers_storage.load_by_slot(day, day, statuses)

    # Нагрузка по слотам своих салонов + две отдельные строки: «без времени»
    # и «нераспределённые». Ни та, ни другая не подмешивается в ячейки.
    loads: Dict[str, Dict[str, float]] = {}
    no_time: Dict[int, Dict[str, float]] = {}
    unassigned = {"orders": 0, "units": 0.0}

    for row in rows:
        store_id = key_to_store.get(row["store_key"]) if row["store_key"] else None
        if store_id is None:
            # Чужой салон в выборке — не наша строка; непривязанный склад и
            # пустой склад показываем одной строкой «нераспределённые».
            if row["store_key"] is None or row["store_key"] not in key_to_store:
                if store_ids is None or row["store_key"] is None:
                    unassigned["orders"] += row["orders"]
                    unassigned["units"] += row["units"]
            continue

        if row["hour"] is None:
            bucket = no_time.setdefault(store_id, {"orders": 0, "units": 0.0, "unparsed": 0})
            bucket["orders"] += row["orders"]
            bucket["units"] += row["units"]
            bucket["unparsed"] += row["unparsed_orders"]
            continue

        cell = loads.setdefault(f"{store_id}:{row['hour']}",
                                {"orders": 0, "units": 0.0, "pickup_orders": 0, "pickup_units": 0.0})
        cell["orders"] += row["orders"]
        cell["units"] += row["units"]
        cell["pickup_orders"] += row["pickup_orders"]
        cell["pickup_units"] += row["pickup_units"]

    grid = []
    for store in stores:
        cells = []
        day_units = 0.0
        day_capacity = 0.0
        has_capacity = False
        for hour in storage.HOURS:
            load = loads.get(f"{store['id']}:{hour}", {})
            capacity = _effective_capacity(store["id"], day, weekday, hour, weekly, exceptions)
            units = round(load.get("units", 0.0), 2)
            percent = _percent(units, capacity["capacity"])

            day_units += units
            if capacity["capacity"] is not None and not capacity["closed"]:
                day_capacity += capacity["capacity"]
                has_capacity = True

            cells.append({
                "hour": hour,
                "orders": load.get("orders", 0),
                "units": units,
                "pickup_orders": load.get("pickup_orders", 0),
                "pickup_units": round(load.get("pickup_units", 0.0), 2),
                "capacity": capacity["capacity"],
                "pickup_capacity": capacity["pickup_capacity"],
                "closed": capacity["closed"],
                "capacity_source": capacity["source"],
                "reason": capacity["reason"],
                "percent": percent,
                "level": _level(percent, capacity["closed"]),
            })

        grid.append({
            "store_id": store["id"],
            "store_name": store["name"],
            "city": store.get("city"),
            "cells": cells,
            "day_units": round(day_units, 2),
            "day_capacity": round(day_capacity, 2) if has_capacity else None,
            "day_percent": _percent(day_units, day_capacity if has_capacity else None),
            "no_time": no_time.get(store["id"]),
        })

    return {
        "date": day,
        "weekday": weekday,
        "hours": list(storage.HOURS),
        "stores": grid,
        "unassigned": unassigned if unassigned["orders"] else None,
        "thresholds": {"tight": THRESHOLD_TIGHT, "over": THRESHOLD_OVER},
        "coverage": couriers_storage.weights_coverage(day, day),
        "freshness": freshness(),
        "no_stores": not stores,
    }


def week_grid(date_from: str, days: int = 7, store_ids: Optional[List[int]] = None) -> Dict[str, Any]:
    """Дневная загрузка по салонам за период — календарь-heatmap."""
    start = datetime.strptime(date_from, "%Y-%m-%d").date()
    date_to = (start + timedelta(days=days - 1)).isoformat()

    stores = _stores_for(store_ids)
    ids = [store["id"] for store in stores]
    key_to_store = {key: store["id"] for store in stores for key in store["keys"]}

    weekly = storage.capacity_map(ids)
    exceptions = storage.exceptions_for(ids, date_from, date_to)
    rows = couriers_storage.load_by_slot(date_from, date_to)

    units: Dict[str, float] = {}
    orders: Dict[str, int] = {}
    for row in rows:
        store_id = key_to_store.get(row["store_key"]) if row["store_key"] else None
        if store_id is None:
            continue
        key = f"{store_id}:{row['date']}"
        units[key] = units.get(key, 0.0) + row["units"]
        orders[key] = orders.get(key, 0) + row["orders"]

    dates = [(start + timedelta(days=i)).isoformat() for i in range(days)]
    result = []
    for store in stores:
        cells = []
        for day in dates:
            weekday = datetime.strptime(day, "%Y-%m-%d").date().weekday()
            capacity_total = 0.0
            has_capacity = False
            closed_all = True
            for hour in storage.HOURS:
                capacity = _effective_capacity(store["id"], day, weekday, hour, weekly, exceptions)
                if not capacity["closed"]:
                    closed_all = False
                if capacity["capacity"] is not None and not capacity["closed"]:
                    capacity_total += capacity["capacity"]
                    has_capacity = True

            day_units = round(units.get(f"{store['id']}:{day}", 0.0), 2)
            percent = _percent(day_units, capacity_total if has_capacity else None)
            cells.append({
                "date": day,
                "units": day_units,
                "orders": orders.get(f"{store['id']}:{day}", 0),
                "capacity": round(capacity_total, 2) if has_capacity else None,
                "percent": percent,
                "closed": closed_all and has_capacity is False,
                "level": _level(percent, closed_all and not has_capacity),
            })
        result.append({
            "store_id": store["id"],
            "store_name": store["name"],
            "city": store.get("city"),
            "days": cells,
        })

    return {
        "from": date_from,
        "to": date_to,
        "dates": dates,
        "stores": result,
        "thresholds": {"tight": THRESHOLD_TIGHT, "over": THRESHOLD_OVER},
        "freshness": freshness(),
        "no_stores": not stores,
    }


def slot_orders(day: str, store_id: int, hour: Optional[int]) -> Dict[str, Any]:
    """Заказы одного слота — клик по ячейке."""
    links = salonkpi_storage.resolve_map(salonkpi_storage.SOURCE_CRM_STORE)
    keys = [key for key, sid in links.items() if sid == store_id]

    orders: List[Dict[str, Any]] = []
    for key in keys:
        orders.extend(couriers_storage.list_slot_orders(day, key, hour))
    orders.sort(key=lambda o: (o["ready_time"] or "", o["order_id"]))

    return {
        "date": day,
        "store_id": store_id,
        "hour": hour,
        "orders": orders,
        "units": round(sum(o["units"] or 0 for o in orders), 2),
    }


def free_slots(store_id: int, date_from: str, days: int = 3,
               need_units: float = 1.0) -> Dict[str, Any]:
    """
    Ближайшие слоты, где ещё есть запас.

    Нужны не сами по себе: предупреждение о перегрузе без альтернативы не
    меняет решений — человек не станет звонить клиенту, чтобы предложить
    «когда-нибудь потом».
    """
    grid_days = []
    start = datetime.strptime(date_from, "%Y-%m-%d").date()
    for i in range(days):
        day = (start + timedelta(days=i)).isoformat()
        grid_days.append(day_grid(day, [store_id]))

    slots = []
    for grid in grid_days:
        for store in grid["stores"]:
            for cell in store["cells"]:
                if cell["closed"] or cell["capacity"] is None:
                    continue
                free = cell["capacity"] - cell["units"]
                if free >= need_units:
                    slots.append({
                        "date": grid["date"],
                        "hour": cell["hour"],
                        "free_units": round(free, 2),
                        "percent": cell["percent"],
                    })
    return {"store_id": store_id, "from": date_from, "days": days, "slots": slots}


# Горизонты предупреждений. Сутки — чтобы успеть вывести ещё одного флориста,
# три часа — чтобы успеть перенести заказ. Раньше суток предупреждать
# бессмысленно: заказы ещё донесут, и слот всё равно пересчитается.
HORIZON_DAY = "day"
HORIZON_SOON = "soon"
HORIZON_SOON_HOURS = 3

# Синк не проходил дольше этого — сетка описывает не сегодняшний день, и
# считать по ней проценты достоверными нельзя.
STALE_SYNC_HOURS = 2


def salon_now(utc_offset: int) -> datetime:
    """Текущее время в салоне. Прод живёт в UTC, салоны — в UTC+5/+7."""
    return datetime.utcnow() + timedelta(hours=utc_offset)


def scan_alerts() -> Dict[str, Any]:
    """
    Найти перегруженные слоты на ближайшие сутки и закрыть те предупреждения,
    по которым слот уже разгрузился.

    Считается шагом синка, а не отдельным планировщиком: лишний фоновый поток —
    это лишние обращения к общему медленному диску.

    Салон без заданного часового пояса пропускается: «через 3 часа» без пояса
    посчиталось бы по времени сервера и приехало бы мимо на 5–7 часов.
    """
    offsets = storage.timezone_map()
    stores = _stores_for(None)
    created = 0
    resolved = 0
    skipped_no_tz = []

    # Сначала закрываем то, что разгрузилось: если человек перенёс заказ, он
    # не должен видеть предупреждение до конца дня.
    today_any = date.today().isoformat()
    grids: Dict[str, Dict[str, Any]] = {}
    for alert in storage.open_alerts_for_scan(today_any):
        grid = grids.get(alert["date"])
        if grid is None:
            grid = day_grid(alert["date"], None)
            grids[alert["date"]] = grid
        store = next((s for s in grid["stores"] if s["store_id"] == alert["store_id"]), None)
        if not store:
            continue
        cell = store["cells"][alert["hour"]] if alert["hour"] < len(store["cells"]) else None
        if cell and cell["percent"] is not None and cell["percent"] < THRESHOLD_OVER:
            storage.resolve_alert(alert["id"], cell["percent"])
            resolved += 1

    for store in stores:
        offset = offsets.get(store["id"])
        if offset is None:
            skipped_no_tz.append(store["name"])
            continue

        now = salon_now(offset)
        today = now.date().isoformat()
        tomorrow = (now.date() + timedelta(days=1)).isoformat()

        for day, horizon in ((today, HORIZON_SOON), (tomorrow, HORIZON_DAY)):
            grid = grids.get(day)
            if grid is None:
                grid = day_grid(day, None)
                grids[day] = grid
            row = next((s for s in grid["stores"] if s["store_id"] == store["id"]), None)
            if not row:
                continue

            for cell in row["cells"]:
                if cell["closed"] or cell["percent"] is None:
                    continue
                if cell["percent"] < THRESHOLD_OVER:
                    continue
                if horizon == HORIZON_SOON:
                    # Прошедший час не спасти, а дальше трёх часов — это уже
                    # горизонт «за сутки», второе предупреждение о том же.
                    if not (now.hour <= cell["hour"] <= now.hour + HORIZON_SOON_HOURS):
                        continue
                if storage.upsert_alert(store["id"], day, cell["hour"], horizon,
                                        cell["percent"], cell["units"], cell["capacity"]):
                    created += 1

    return {"created": created, "resolved": resolved, "no_timezone": skipped_no_tz}


def alerts(store_ids: Optional[List[int]] = None) -> Dict[str, Any]:
    """Активные предупреждения с альтернативой: куда переставить заказ."""
    today = date.today().isoformat()
    items = storage.active_alerts(store_ids, today)

    names = {store["id"]: store["name"] for store in salonkpi_storage.list_stores(store_ids)}
    result = []
    for item in items:
        if item["store_id"] not in names:
            continue
        # Альтернатива считается здесь же: предупреждение без ответа «куда
        # переносить» не меняет решений — человек не станет звонить клиенту,
        # чтобы предложить «когда-нибудь потом».
        free = free_slots(item["store_id"], item["date"], days=2, need_units=1.0)
        suggestions = [slot for slot in free["slots"]
                       if not (slot["date"] == item["date"] and slot["hour"] == item["hour"])][:3]
        result.append({**item,
                       "store_name": names[item["store_id"]],
                       "free_slots": suggestions})

    return {
        "items": result,
        "stats": storage.alerts_stats((date.today() - timedelta(days=30)).isoformat(), store_ids),
    }


def sync_is_stale() -> bool:
    """Синк давно не проходил — молчание модуля не значит «всё спокойно»."""
    info = freshness()
    stamp = info.get("last_sync_at")
    if not stamp:
        return True
    try:
        last = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return True
    return (datetime.utcnow() - last) > timedelta(hours=STALE_SYNC_HOURS)


def suggest_capacity(store_id: int, days: int = 30) -> Dict[str, Any]:
    """
    Предложить норму из факта: сколько салон реально собирал в час.

    Считаем по часам, когда салон работал и что-то делал. Медиана и 80-й
    перцентиль: среднее занижает норму хвостом пустых часов, максимум —
    завышает разовым праздником.

    Значение только предлагается. Применять его автоматически нельзя: занижение
    нормы превращается в постоянный ложный перегруз, и на модуль перестают
    смотреть — это первый пункт pre-mortem.
    """
    date_to = date.today().isoformat()
    date_from = (date.today() - timedelta(days=days)).isoformat()

    links = salonkpi_storage.resolve_map(salonkpi_storage.SOURCE_CRM_STORE)
    keys = {key for key, sid in links.items() if sid == store_id}
    if not keys:
        return {"store_id": store_id, "samples": 0, "median": None, "p80": None, "current": None}

    rows = [row for row in couriers_storage.load_by_slot(date_from, date_to)
            if row["store_key"] in keys and row["hour"] is not None]

    hourly: Dict[str, float] = {}
    for row in rows:
        key = f"{row['date']}:{row['hour']}"
        hourly[key] = hourly.get(key, 0.0) + row["units"]

    values = sorted(v for v in hourly.values() if v > 0)
    if not values:
        return {"store_id": store_id, "samples": 0, "median": None, "p80": None,
                "current": _current_capacity(store_id), "from": date_from, "to": date_to}

    def percentile(data, share):
        index = min(len(data) - 1, max(0, int(round((len(data) - 1) * share))))
        return round(data[index], 1)

    return {
        "store_id": store_id,
        "samples": len(values),
        "median": percentile(values, 0.5),
        "p80": percentile(values, 0.8),
        "max": round(values[-1], 1),
        "current": _current_capacity(store_id),
        "from": date_from,
        "to": date_to,
    }


def _current_capacity(store_id: int) -> Optional[float]:
    """Самая частая ёмкость в недельной сетке — то, что стоит сейчас."""
    grid = storage.weekly_grid(store_id)
    counts: Dict[float, int] = {}
    for value in grid.values():
        if value["capacity"] is None or value["closed"]:
            continue
        counts[value["capacity"]] = counts.get(value["capacity"], 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]


def freshness() -> Dict[str, Any]:
    """
    На какой момент данные. Пустая сетка одинаково выглядит и как «заказов
    нет», и как «синк упал два часа назад», — эти случаи обязаны различаться
    на экране, иначе модуль врёт молча.
    """
    try:
        health = couriers_storage.health_snapshot()
    except Exception as e:
        logger.warning(f"Состояние витрины недоступно: {e}")
        return {"error": str(e)}

    last_sync = health.get("last_sync") or {}
    load = health.get("load") or {}
    return {
        "last_sync_at": last_sync.get("finished_at") or last_sync.get("started_at"),
        "last_sync_status": last_sync.get("status"),
        "future_orders": load.get("future_orders"),
        "until": load.get("until_future"),
        "unparsed_ready": load.get("unparsed_ready"),
        "orders_without_date": load.get("orders_without_date"),
    }
