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
