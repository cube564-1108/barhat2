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
  - нагрузка меряется не количеством товара, а заказами: единица — это одна
    сборка, а надбавку сверху получают только те позиции, которым её проставили
    руками (см. couriers/storage.py, ORDER_BASE_UNITS). Количество в CRM живёт
    в разных единицах — букет в штуках, клубника в граммах, роза в стеблях, — и
    «Σ количество × вес» превращало один сборный заказ в 509 единиц.
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
    """
    Проверка даты, а не её формы. Регулярки мало: «2026-13-45» ей подходит,
    а дальше `strptime` внутри расчёта роняет запрос пятисоткой вместо
    внятного 400.
    """
    if not value or not _DATE_RE.match(value):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return True


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
                        weekly: Dict[str, Any], exceptions: Dict[str, Any],
                        shares: Optional[Dict[int, float]] = None) -> Dict[str, Any]:
    """
    Ёмкость слота: исключение на дату важнее недельного графика.

    Отсутствие обеих записей — это «не задана», а не ноль.

    Отдаётся сразу в двух видах: `capacity` (старые единицы, по ним сейчас
    считается процент) и `florists` + `capacity_minutes` (новая модель). Одно
    в другое не переводится: пока модель не переключена (Ф6), минуты — это
    справочная величина рядом, а не подмена процента.

    **Исключение перекрывает только то, о чём говорит.** Оно заводится про одну
    величину («14 февраля выходит три флориста»), а молчит про остальные — и
    молчание обязано означать «как в обычном графике», а не «не задано». Иначе
    запись про людей обнуляет старую ёмкость, и вся дата становится серой:
    процент не считается, предупреждения о перегрузе по ней не срабатывают.
    Ровно это и произошло бы 14 февраля, потому что форма исключений шлёт
    флористов, а сетка до Ф6 считает по старым единицам.
    """
    share = (shares or {}).get(store_id)
    regular = weekly.get(f"{store_id}:{weekday}:{hour}") or {}

    exception = exceptions.get(f"{store_id}:{day}:{hour}")
    if exception is not None:
        def stated(field):
            value = exception.get(field)
            return regular.get(field) if value is None else value

        return {
            "capacity": stated("capacity"),
            "florists": stated("florists"),
            "capacity_minutes": storage.capacity_minutes(stated("florists"), share),
            "pickup_capacity": stated("pickup_capacity"),
            "closed": exception["closed"],
            "source": "exception",
            "reason": exception.get("reason"),
        }
    if regular:
        return {
            "capacity": regular["capacity"],
            "florists": regular.get("florists"),
            "capacity_minutes": storage.capacity_minutes(regular.get("florists"), share),
            "pickup_capacity": regular["pickup_capacity"],
            "closed": regular["closed"],
            "source": "weekly",
            "reason": None,
        }
    return {"capacity": None, "florists": None, "capacity_minutes": None,
            "pickup_capacity": None, "closed": False, "source": None, "reason": None}


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


def day_grid(day: str, store_ids: Optional[List[int]] = None,
             with_context: bool = True, model: Optional[str] = None) -> Dict[str, Any]:
    """
    Сетка «часы × салоны» за один день.

    with_context=False — не собирать свежесть данных. Это не украшательство:
    свежесть — отдельное обращение к базе, а внутренние вызовы (подбор
    свободных слотов, расчёт предупреждений) строят по несколько сеток на
    запрос. На диске `/data`, где запрос стоит 90–700 мс, разница получается в
    десятки обращений.

    model передаётся сверху по той же причине: настройка одна на всю сеть, а
    сеток на запрос бывает несколько, и перечитывать её на каждую — лишние
    обращения к тому же медленному диску.
    """
    model = model or storage.get_load_model()
    minutes_model = model == storage.LOAD_MODEL_MINUTES
    stores = _stores_for(store_ids)
    ids = [store["id"] for store in stores]
    key_to_store = {key: store["id"] for store in stores for key in store["keys"]}

    weekday = datetime.strptime(day, "%Y-%m-%d").date().weekday()
    weekly = storage.capacity_map(ids)
    exceptions = storage.exceptions_for(ids, day, day)
    shares = storage.assembly_share_map()

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
            bucket = no_time.setdefault(store_id,
                                        {"orders": 0, "units": 0.0, "minutes": 0.0, "unparsed": 0})
            bucket["orders"] += row["orders"]
            bucket["units"] += row["units"]
            bucket["minutes"] += row.get("minutes", 0.0)
            bucket["unparsed"] += row["unparsed_orders"]
            continue

        cell = loads.setdefault(f"{store_id}:{row['hour']}",
                                {"orders": 0, "units": 0.0, "minutes": 0.0,
                                 "orders_without_norm": 0,
                                 "pickup_orders": 0, "pickup_units": 0.0, "pickup_minutes": 0.0})
        cell["orders"] += row["orders"]
        cell["units"] += row["units"]
        cell["minutes"] += row.get("minutes", 0.0)
        cell["orders_without_norm"] += row.get("orders_without_norm", 0)
        cell["pickup_orders"] += row["pickup_orders"]
        cell["pickup_units"] += row["pickup_units"]
        cell["pickup_minutes"] += row.get("pickup_minutes", 0.0)

    grid = []
    for store in stores:
        cells = []
        day_units = 0.0
        day_capacity = 0.0
        has_capacity = False
        day_minutes = 0.0
        day_capacity_minutes = 0.0
        has_florists = False
        day_without_norm = 0
        for hour in storage.HOURS:
            load = loads.get(f"{store['id']}:{hour}", {})
            capacity = _effective_capacity(store["id"], day, weekday, hour,
                                           weekly, exceptions, shares)
            units = round(load.get("units", 0.0), 2)
            minutes = round(load.get("minutes", 0.0), 2)

            # Активная модель решает, что делить на что. Обе величины при этом
            # остаются в ячейке: разбор «51 из 60 мин» показывается рядом с
            # процентом, а после переключения нужно уметь объяснить, откуда
            # взялось прежнее число.
            value = minutes if minutes_model else units
            value_capacity = capacity["capacity_minutes"] if minutes_model else capacity["capacity"]
            percent = _percent(value, value_capacity)

            day_units += units
            day_minutes += minutes
            day_without_norm += load.get("orders_without_norm", 0)
            if capacity["capacity"] is not None and not capacity["closed"]:
                day_capacity += capacity["capacity"]
                has_capacity = True
            if capacity["capacity_minutes"] is not None and not capacity["closed"]:
                day_capacity_minutes += capacity["capacity_minutes"]
                has_florists = True

            cells.append({
                "hour": hour,
                "orders": load.get("orders", 0),
                "units": units,
                "minutes": minutes,
                "orders_without_norm": load.get("orders_without_norm", 0),
                "pickup_orders": load.get("pickup_orders", 0),
                "pickup_units": round(load.get("pickup_units", 0.0), 2),
                "pickup_minutes": round(load.get("pickup_minutes", 0.0), 2),
                "capacity": capacity["capacity"],
                "florists": capacity["florists"],
                "capacity_minutes": capacity["capacity_minutes"],
                "pickup_capacity": capacity["pickup_capacity"],
                "closed": capacity["closed"],
                "capacity_source": capacity["source"],
                "reason": capacity["reason"],
                # Что показывать и от чего считался процент — решено здесь, а
                # не в интерфейсе: иначе экран и предупреждения разъедутся.
                "load": value,
                "load_capacity": value_capacity,
                "percent": percent,
                "level": _level(percent, capacity["closed"]),
            })

        day_value = day_minutes if minutes_model else day_units
        day_value_capacity = ((day_capacity_minutes if has_florists else None) if minutes_model
                              else (day_capacity if has_capacity else None))
        grid.append({
            "store_id": store["id"],
            "store_name": store["name"],
            "city": store.get("city"),
            "cells": cells,
            "day_units": round(day_units, 2),
            "day_capacity": round(day_capacity, 2) if has_capacity else None,
            "day_minutes": round(day_minutes, 2),
            "day_capacity_minutes": round(day_capacity_minutes, 2) if has_florists else None,
            "day_load": round(day_value, 2),
            "day_load_capacity": day_value_capacity,
            "day_percent": _percent(day_value, day_value_capacity),
            "day_without_norm": day_without_norm,
            "no_time": no_time.get(store["id"]),
        })

    return {
        "date": day,
        "weekday": weekday,
        "hours": list(storage.HOURS),
        "stores": grid,
        # Нераспределённые — сводка по всей сети: у заказа не заполнен склад,
        # и чей он, неизвестно. Показываем только тому, кто видит все салоны:
        # флористу это чужие цифры, а разобрать их он всё равно не может.
        "unassigned": unassigned if (unassigned["orders"] and store_ids is None) else None,
        "thresholds": {"tight": THRESHOLD_TIGHT, "over": THRESHOLD_OVER},
        # Единица подписывается в ответе, а не выводится интерфейсом из
        # догадки: цифра без единицы измерения — ровно та ошибка, ради
        # которой весь модуль переделывается.
        "model": model,
        "unit": "мин" if minutes_model else "ед.",
        # Разбора нагрузки на базу и надбавки здесь больше нет: экран его не
        # показывает, а стоил он двух обращений к общему медленному диску на
        # каждый показ сетки. Разбор живёт в справочнике надбавок, где на него
        # и смотрят, — /api/couriers/weights.
        "freshness": freshness(day, day) if with_context else None,
        "no_stores": not stores,
    }


def week_grid(date_from: str, days: int = 7, store_ids: Optional[List[int]] = None,
              model: Optional[str] = None) -> Dict[str, Any]:
    """Дневная загрузка по салонам за период — календарь-heatmap."""
    model = model or storage.get_load_model()
    minutes_model = model == storage.LOAD_MODEL_MINUTES
    start = datetime.strptime(date_from, "%Y-%m-%d").date()
    date_to = (start + timedelta(days=days - 1)).isoformat()

    stores = _stores_for(store_ids)
    ids = [store["id"] for store in stores]
    key_to_store = {key: store["id"] for store in stores for key in store["keys"]}

    weekly = storage.capacity_map(ids)
    exceptions = storage.exceptions_for(ids, date_from, date_to)
    shares = storage.assembly_share_map()
    rows = couriers_storage.load_by_slot(date_from, date_to)

    values: Dict[str, float] = {}
    orders: Dict[str, int] = {}
    for row in rows:
        store_id = key_to_store.get(row["store_key"]) if row["store_key"] else None
        if store_id is None:
            continue
        key = f"{store_id}:{row['date']}"
        values[key] = values.get(key, 0.0) + (row["minutes"] if minutes_model else row["units"])
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
                capacity = _effective_capacity(store["id"], day, weekday, hour,
                                               weekly, exceptions, shares)
                if not capacity["closed"]:
                    closed_all = False
                value = capacity["capacity_minutes"] if minutes_model else capacity["capacity"]
                if value is not None and not capacity["closed"]:
                    capacity_total += value
                    has_capacity = True

            day_units = round(values.get(f"{store['id']}:{day}", 0.0), 2)
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
        "freshness": freshness(date_from, date_to),
        "no_stores": not stores,
        "model": model,
        "unit": "мин" if minutes_model else "ед.",
    }


def slot_orders(day: str, store_id: int, hour: Optional[int],
                model: Optional[str] = None) -> Dict[str, Any]:
    """Заказы одного слота — клик по ячейке."""
    model = model or storage.get_load_model()
    minutes_model = model == storage.LOAD_MODEL_MINUTES
    links = salonkpi_storage.resolve_map(salonkpi_storage.SOURCE_CRM_STORE)
    keys = [key for key, sid in links.items() if sid == store_id]

    orders: List[Dict[str, Any]] = []
    for key in keys:
        orders.extend(couriers_storage.list_slot_orders(day, key, hour))
    orders.sort(key=lambda o: (o["ready_time"] or "", o["order_id"]))

    for item in orders:
        item["load"] = (item["minutes"] or 0) if minutes_model else item["units"]

    return {
        "date": day,
        "store_id": store_id,
        "hour": hour,
        "orders": orders,
        "units": round(sum(o["units"] or 0 for o in orders), 2),
        "minutes": round(sum(o["minutes"] or 0 for o in orders), 2),
        "load": round(sum(o["load"] or 0 for o in orders), 2),
        "without_norm": sum(1 for o in orders if o["without_norm"]),
        "model": model,
        "unit": "мин" if minutes_model else "ед.",
    }


def free_slots(store_id: int, date_from: str, days: int = 3, need_units: Optional[float] = None,
               grids: Optional[Dict[str, Dict[str, Any]]] = None,
               model: Optional[str] = None) -> Dict[str, Any]:
    """
    Ближайшие слоты, где ещё есть запас.

    Нужны не сами по себе: предупреждение о перегрузе без альтернативы не
    меняет решений — человек не станет звонить клиенту, чтобы предложить
    «когда-нибудь потом».

    Прошедшие часы не предлагаем. «Перенесите заказ с 17:00 на 09:00 сегодня» —
    это совет, который невозможно выполнить, и после пары таких подсказок
    экраном перестают пользоваться. Час считается по часам салона: сервер живёт
    в UTC, а салоны в UTC+5/+7.

    grids — общий кэш сеток на запрос. Каждая сетка это несколько обращений к
    медленному диску, а предупреждений на экране бывает десяток.

    need_units меряется в единицах активной модели. Умолчание поэтому тоже
    зависит от неё: «запас в одну единицу» и «запас в одну минуту» — разные
    требования, и второе пропустило бы забитый слот как свободный. Полчаса —
    это заметный кусок работы флориста, в который влезает средний заказ.
    """
    model = model or storage.get_load_model()
    if need_units is None:
        need_units = (storage.MINUTES_PER_FLORIST_HOUR / 2
                      if model == storage.LOAD_MODEL_MINUTES else 1.0)
    offset = storage.timezone_map().get(store_id)
    now_local = salon_now(offset) if offset is not None else None

    cache = grids if grids is not None else {}
    start = datetime.strptime(date_from, "%Y-%m-%d").date()
    slots = []

    for i in range(days):
        day = (start + timedelta(days=i)).isoformat()
        grid = cache.get(day)
        if grid is None:
            grid = day_grid(day, None, with_context=False, model=model)
            cache[day] = grid

        for store in grid["stores"]:
            if store["store_id"] != store_id:
                continue
            for cell in store["cells"]:
                if cell["closed"] or cell["load_capacity"] is None:
                    continue
                if now_local is not None and day == now_local.date().isoformat() \
                        and cell["hour"] <= now_local.hour:
                    continue
                free = cell["load_capacity"] - cell["load"]
                if free >= need_units:
                    slots.append({
                        "date": day,
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
    # Модель читается один раз на весь прогон: сеток здесь строится десяток,
    # а настройка одна на сеть.
    model = storage.get_load_model()
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
            grid = day_grid(alert["date"], None, with_context=False, model=model)
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
        tomorrow = (now.date() + timedelta(days=1)).isoformat()

        # Ближайшие часы считаются временем, а не номером часа: у круглосуточной
        # точки в 23:00 «ближайшие 3 часа» — это 00:00–02:00 СЛЕДУЮЩИХ суток.
        # Арифметика по `now.hour + 3` в пределах одного дня их не видела вовсе,
        # и ночная смена оставалась без предупреждения — а перенести заказ ночью
        # некуда, там как раз и нужен сигнал заранее.
        soon = {((now + timedelta(hours=shift)).date().isoformat(),
                 (now + timedelta(hours=shift)).hour)
                for shift in range(HORIZON_SOON_HOURS + 1)}

        # Горизонт «за сутки» не повторяет то, что уже сказано «ближайшими
        # часами»: одно и то же предупреждение дважды перестают читать.
        targets = [(day, hour, HORIZON_SOON) for day, hour in sorted(soon)]
        targets += [(tomorrow, hour, HORIZON_DAY) for hour in storage.HOURS
                    if (tomorrow, hour) not in soon]

        for day, hour, horizon in targets:
            grid = grids.get(day)
            if grid is None:
                grid = day_grid(day, None, with_context=False, model=model)
                grids[day] = grid
            row = next((s for s in grid["stores"] if s["store_id"] == store["id"]), None)
            if not row:
                continue

            cell = next((c for c in row["cells"] if c["hour"] == hour), None)
            if cell is None or cell["closed"] or cell["percent"] is None:
                continue
            if cell["percent"] < THRESHOLD_OVER:
                continue
            # В предупреждение пишутся числа АКТИВНОЙ модели: иначе после
            # переключения в тексте окажутся единицы, которых на экране уже нет.
            if storage.upsert_alert(store["id"], day, hour, horizon,
                                    cell["percent"], cell["load"], cell["load_capacity"]):
                created += 1

    return {"created": created, "resolved": resolved, "no_timezone": skipped_no_tz}


def alerts(store_ids: Optional[List[int]] = None) -> Dict[str, Any]:
    """Активные предупреждения с альтернативой: куда переставить заказ."""
    today = date.today().isoformat()
    items = storage.active_alerts(store_ids, today)

    model = storage.get_load_model()
    names = {store["id"]: store["name"] for store in salonkpi_storage.list_stores(store_ids)}
    result = []
    # Общий кэш сеток на весь ответ: без него десяток предупреждений строил бы
    # два десятка сеток, каждая — несколько обращений к общему медленному диску.
    grids: Dict[str, Dict[str, Any]] = {}
    for item in items:
        if item["store_id"] not in names:
            continue
        # Альтернатива считается здесь же: предупреждение без ответа «куда
        # переносить» не меняет решений — человек не станет звонить клиенту,
        # чтобы предложить «когда-нибудь потом».
        free = free_slots(item["store_id"], item["date"], days=2, grids=grids, model=model)
        suggestions = [slot for slot in free["slots"]
                       if not (slot["date"] == item["date"] and slot["hour"] == item["hour"])][:3]
        result.append({**item,
                       "store_name": names[item["store_id"]],
                       "free_slots": suggestions})

    return {
        "items": result,
        "stats": storage.alerts_stats((date.today() - timedelta(days=30)).isoformat(), store_ids),
        "model": model,
        "unit": "мин" if model == storage.LOAD_MODEL_MINUTES else "ед.",
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

    **Отдаётся в тех же единицах, что и поле ввода** — в минутах и в
    флористах. Подсказка «6,2 единицы» под полем «флористов в смене» не просто
    бесполезна: её применят как есть, и салон получит шесть флористов вместо
    одного. Старые единицы тоже возвращаются — по ним до Ф6 считается сетка.

    Значение только предлагается. Применять его автоматически нельзя: занижение
    нормы превращается в постоянный ложный перегруз, и на модуль перестают
    смотреть — это первый пункт pre-mortem.
    """
    date_to = date.today().isoformat()
    date_from = (date.today() - timedelta(days=days)).isoformat()
    share = storage.assembly_share_map().get(store_id, storage.DEFAULT_ASSEMBLY_SHARE)

    # Недельная сетка читается ОДИН раз на весь ответ. Раньше «текущее
    # значение» бралось отдельным вызовом на каждое поле, и один клик по
    # подсказке стоил четырёх одинаковых чтений с диска, где обращение — это
    # 90–700 мс.
    weekly = storage.weekly_grid(store_id)
    current = _most_common(weekly, "capacity")
    current_florists = _most_common(weekly, "florists")

    def empty():
        return {"store_id": store_id, "samples": 0, "median": None, "p80": None,
                "max": None, "median_minutes": None, "p80_minutes": None,
                "max_minutes": None, "median_florists": None, "p80_florists": None,
                "assembly_share": share, "current": current,
                "current_florists": current_florists,
                "from": date_from, "to": date_to}

    links = salonkpi_storage.resolve_map(salonkpi_storage.SOURCE_CRM_STORE)
    keys = {key for key, sid in links.items() if sid == store_id}
    if not keys:
        return empty()

    rows = [row for row in couriers_storage.load_by_slot(date_from, date_to)
            if row["store_key"] in keys and row["hour"] is not None]

    hourly_units: Dict[str, float] = {}
    hourly_minutes: Dict[str, float] = {}
    for row in rows:
        key = f"{row['date']}:{row['hour']}"
        hourly_units[key] = hourly_units.get(key, 0.0) + row["units"]
        hourly_minutes[key] = hourly_minutes.get(key, 0.0) + row.get("minutes", 0.0)

    units = sorted(v for v in hourly_units.values() if v > 0)
    minutes = sorted(v for v in hourly_minutes.values() if v > 0)
    if not units and not minutes:
        return empty()

    def percentile(data, quantile):
        if not data:
            return None
        index = min(len(data) - 1, max(0, int(round((len(data) - 1) * quantile))))
        return round(data[index], 1)

    def to_florists(value):
        """Минуты в час → люди. Округляем до половины: 0,5 флориста — реальность."""
        if value is None:
            return None
        per_person = storage.MINUTES_PER_FLORIST_HOUR * share
        if per_person <= 0:
            return None
        return round(value / per_person * 2) / 2

    median_minutes = percentile(minutes, 0.5)
    p80_minutes = percentile(minutes, 0.8)

    return {
        "store_id": store_id,
        "samples": len(minutes) or len(units),
        # Старые единицы — пока по ним считается сетка.
        "median": percentile(units, 0.5),
        "p80": percentile(units, 0.8),
        "max": round(units[-1], 1) if units else None,
        # Минуты и люди — то, в чём задаётся ёмкость с Ф4.
        "median_minutes": median_minutes,
        "p80_minutes": p80_minutes,
        "max_minutes": round(minutes[-1], 1) if minutes else None,
        "median_florists": to_florists(median_minutes),
        "p80_florists": to_florists(p80_minutes),
        "assembly_share": share,
        "current": current,
        "current_florists": current_florists,
        "from": date_from,
        "to": date_to,
    }


def _most_common(grid: Dict[str, Any], field: str) -> Optional[float]:
    """Самое частое значение в уже прочитанной недельной сетке."""
    counts: Dict[float, int] = {}
    for value in grid.values():
        if value.get(field) is None or value["closed"]:
            continue
        counts[value[field]] = counts.get(value[field], 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]


def freshness(date_from: Optional[str] = None, date_to: Optional[str] = None) -> Dict[str, Any]:
    """
    На какой момент данные. Пустая сетка одинаково выглядит и как «заказов
    нет», и как «синк упал два часа назад», — эти случаи обязаны различаться
    на экране, иначе модуль врёт молча.

    Считается по показываемому периоду и дешёвым запросом. Раньше здесь
    вызывался `health_snapshot()`, а он обходит всю витрину: замер на проде
    2026-09-07 дал 6–9 секунд на /health, и столько же платил бы каждый показ
    сетки.
    """
    day = date_from or today_iso()
    try:
        info = couriers_storage.load_freshness(day, date_to or day)
    except Exception as e:
        logger.warning(f"Состояние витрины недоступно: {e}")
        return {"error": str(e)}

    return {
        "last_sync_at": info.get("last_sync_at"),
        "last_sync_status": info.get("last_sync_status"),
        # Пустой справочник статусов = нагрузкой не считается ничего, и сетка
        # выглядит как честный ноль. Так будет сразу после первого деплоя, пока
        # синк не заполнил справочник, — экран обязан сказать об этом словами.
        "statuses_as_load": info.get("statuses_as_load"),
        "until": info.get("until_future"),
        "unparsed_ready": info.get("unparsed_ready"),
        "without_store": info.get("without_store"),
        "orders_without_date": info.get("orders_without_date"),
    }
