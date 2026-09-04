"""
Сборка показателей салона за месяц из четырёх источников.

Здесь только сборка и арифметика: сами агрегаты считают модули-владельцы данных
(couriers, pyrus.nos, pyrus.quality, moysklad.warehouse), потому что лезть
SQL-запросами в чужую базу — верный способ узнать о смене схемы в проде.

Ключевое правило модуля: **непосчитанный показатель — это None с причиной,
а не ноль**. Ноль означает «данные есть, значение нулевое»; отсутствие плана,
отсутствие прихода на склад и списание, превысившее приход, — это разные
состояния, и все они читаются человеком по-разному.
"""

import calendar
import logging
import re
import threading
import time
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from . import storage

logger = logging.getLogger(__name__)

MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

# Кэш ответа: на /data каждый запрос к базе стоит 90–700 мс, а на экране
# четыре источника. TTL короткий — планы и связи правятся руками, и человек
# должен увидеть результат своей правки сразу.
CACHE_TTL_SECONDS = 60
_cache: Dict[tuple, tuple] = {}
_cache_lock = threading.Lock()

# Доля списания цветка, выше которой это уже вопрос к учёту, а не потери
FLOWER_LOSS_ALERT = 20.0
# Границы правдоподобия цены клубники, ₽/кг: вне их вероятнее всего сменилась
# единица измерения товара, а не цена закупки
BERRY_PRICE_MIN = 100.0
BERRY_PRICE_MAX = 10000.0


def invalidate_cache() -> None:
    """Сбросить кэш — зовётся после правки планов и связей."""
    with _cache_lock:
        _cache.clear()


def valid_month(month: str) -> bool:
    return bool(month and MONTH_RE.match(month))


def current_month() -> str:
    return date.today().strftime("%Y-%m")


def month_bounds(month: str) -> Tuple[str, str]:
    """Границы месяца. Конец — сегодня, если месяц ещё идёт."""
    year, mon = int(month[:4]), int(month[5:7])
    last_day = calendar.monthrange(year, mon)[1]
    start = date(year, mon, 1)
    end = date(year, mon, last_day)
    today = date.today()
    if end > today:
        end = today
    return start.isoformat(), end.isoformat()


def previous_month(month: str) -> str:
    year, mon = int(month[:4]), int(month[5:7])
    mon -= 1
    if mon == 0:
        mon = 12
        year -= 1
    return f"{year:04d}-{mon:02d}"


def comparable_bounds(month: str) -> Tuple[str, str]:
    """
    Границы прошлого месяца ДЛЯ СРАВНЕНИЯ: до того же числа, что прошло сейчас.

    Сравнивать 4 дня текущего месяца с полным прошлым бессмысленно — стрелка
    всегда показывала бы провал на 85%. Для закрытого месяца берётся он целиком.
    """
    prev = previous_month(month)
    year, mon = int(prev[:4]), int(prev[5:7])
    last_day = calendar.monthrange(year, mon)[1]

    _, end_current = month_bounds(month)
    day = min(int(end_current[8:10]), last_day)

    # Если текущий месяц уже закрыт, сравниваем полные месяцы
    cur_year, cur_mon = int(month[:4]), int(month[5:7])
    if date.today() > date(cur_year, cur_mon, calendar.monthrange(cur_year, cur_mon)[1]):
        day = last_day

    return date(year, mon, 1).isoformat(), date(year, mon, day).isoformat()


def month_progress(month: str) -> Dict[str, Any]:
    """Сколько дней месяца прошло — для темпа и метки «где надо быть сегодня»."""
    year, mon = int(month[:4]), int(month[5:7])
    days = calendar.monthrange(year, mon)[1]
    _, end = month_bounds(month)
    passed = int(end[8:10])
    today = date.today()
    running = (today.year, today.month) == (year, mon)
    return {"days": days, "passed": passed, "running": running}


def _safe_div(numerator: float, denominator: float) -> Optional[float]:
    return (numerator / denominator) if denominator else None


def _pct(numerator: float, denominator: float) -> Optional[float]:
    value = _safe_div(numerator, denominator)
    return round(value * 100, 1) if value is not None else None


# ============================================================================
# Сбор сырых данных
# ============================================================================

def _shipments(date_from: str, date_to: str) -> Dict[str, Dict[str, Any]]:
    try:
        from couriers import storage as couriers_storage
        return couriers_storage.aggregate_shipments(date_from, date_to)
    except Exception as e:
        logger.error(f"Не удалось получить отгрузки CRM: {e}")
        return {}


def _nos(date_from: str, date_to: str) -> Dict[str, Dict[str, Any]]:
    try:
        from pyrus import nos
        return nos.counts_by_salon(date_from, date_to)
    except Exception as e:
        logger.error(f"Не удалось получить негативную ОС: {e}")
        return {}


def _quality(date_from: str, date_to: str) -> Dict[str, Dict[str, Any]]:
    """
    Качество по салонам с разбивкой на шкалы 14 и 18 баллов.

    Средние баллы разных шкал не смешиваются: у видов заказа разный максимум, и
    «средний балл» между ними зависел бы от состава заказов, а не от качества.
    Сравнимая величина одна — доля набранных баллов от возможных.
    """
    try:
        from pyrus import quality
        report = quality.generate_report(date_from, date_to)
    except Exception as e:
        logger.error(f"Не удалось получить качество сборки: {e}")
        return {}

    result = {}
    for salon, data in (report.get("salons") or {}).items():
        c14, c18 = data["cat14"]["count"], data["cat18"]["count"]
        s14 = data["cat14"]["avg_score"] * c14
        s18 = data["cat18"]["avg_score"] * c18
        result[salon] = {
            "avg14": data["cat14"]["avg_score"] if c14 else None,
            "count14": c14,
            "avg18": data["cat18"]["avg_score"] if c18 else None,
            "count18": c18,
            "percent": _pct(s14 + s18, c14 * 14 + c18 * 18),
            "count": c14 + c18,
            "florists": data.get("florists") or {},
        }
    return result


def _warehouse(date_from: str, date_to: str) -> Dict[str, Dict[str, Any]]:
    try:
        from moysklad import warehouse
        from moysklad.server import get_db as get_ms_db
        return warehouse.totals_by_store(get_ms_db(), date_from, date_to)
    except Exception as e:
        logger.error(f"Не удалось получить движение товара: {e}")
        return {}


# ============================================================================
# Показатели одного салона
# ============================================================================

def _blank() -> Dict[str, Any]:
    return {
        "fact": 0.0, "street": 0.0, "orders": 0, "courier_orders": 0, "taxi_orders": 0,
        "channels": {},
        "flower_in": 0.0, "flower_out": 0.0,
        "berry_in_sum": 0.0, "berry_in_qty": 0.0, "berry_out_qty": 0.0,
        "berry_qty_per_kg": None,
        "nos_confirmed": 0, "nos_in_review": 0, "nos_total": 0, "nos_categories": {},
        "quality": None,
    }


def _collect_raw(store: Dict[str, Any], links: Dict[str, Dict[str, int]],
                 sources: Dict[str, Dict]) -> Dict[str, Any]:
    """Сырые суммы одного салона из всех источников."""
    raw = _blank()
    store_id = store["id"]

    for site, data in sources["shipments"].items():
        if links["crm"].get(site) != store_id:
            continue
        raw["fact"] += data["fact"]
        raw["street"] += data["street"]
        raw["orders"] += data["orders"]
        raw["courier_orders"] += data["courier_orders"]
        raw["taxi_orders"] += data["taxi_orders"]
        for method, amount in data["channels"].items():
            raw["channels"][method] = raw["channels"].get(method, 0) + amount

    for salon, data in sources["nos"].items():
        if links["nos"].get(salon) != store_id:
            continue
        raw["nos_confirmed"] += data["confirmed"]
        raw["nos_in_review"] += data["in_review"]
        raw["nos_total"] += data["total"]
        for category, count in data["categories"].items():
            raw["nos_categories"][category] = raw["nos_categories"].get(category, 0) + count

    quality_parts = [data for salon, data in sources["quality"].items()
                     if links["quality"].get(salon) == store_id]
    if quality_parts:
        c14 = sum(p["count14"] for p in quality_parts)
        c18 = sum(p["count18"] for p in quality_parts)
        s14 = sum((p["avg14"] or 0) * p["count14"] for p in quality_parts)
        s18 = sum((p["avg18"] or 0) * p["count18"] for p in quality_parts)
        florists: Dict[str, Dict[str, Any]] = {}
        for part in quality_parts:
            for name, fdata in part["florists"].items():
                cell = florists.setdefault(name, {"score": 0.0, "count14": 0, "count18": 0})
                cell["score"] += fdata["avg_score"] * fdata["count"]
                cell["count14"] += fdata.get("cat14_count", 0)
                cell["count18"] += fdata.get("cat18_count", 0)
        raw["quality"] = {
            "avg14": round(s14 / c14, 2) if c14 else None, "count14": c14,
            "avg18": round(s18 / c18, 2) if c18 else None, "count18": c18,
            "percent": _pct(s14 + s18, c14 * 14 + c18 * 18),
            "count": c14 + c18,
            "florists": florists,
        }

    for ms_store_id, data in sources["warehouse"].items():
        if links["ms"].get(ms_store_id) != store_id:
            continue
        raw["flower_in"] += data["flower_in"]
        raw["flower_out"] += data["flower_out"]
        raw["berry_in_sum"] += data["berry_in_sum"]
        raw["berry_in_qty"] += data["berry_in_qty"]
        raw["berry_out_qty"] += data["berry_out_qty"]
        if data["berry_qty_per_kg"]:
            raw["berry_qty_per_kg"] = data["berry_qty_per_kg"]

    return raw


def _sum_raw(parts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Сложить сырые данные нескольких салонов (город или «вся сеть»)."""
    total = _blank()
    quality_parts = [p["quality"] for p in parts if p.get("quality")]

    for part in parts:
        for key in ("fact", "street", "orders", "courier_orders", "taxi_orders",
                    "flower_in", "flower_out", "berry_in_sum", "berry_in_qty", "berry_out_qty",
                    "nos_confirmed", "nos_in_review", "nos_total"):
            total[key] += part[key]
        for method, amount in part["channels"].items():
            total["channels"][method] = total["channels"].get(method, 0) + amount
        for category, count in part["nos_categories"].items():
            total["nos_categories"][category] = total["nos_categories"].get(category, 0) + count
        if part["berry_qty_per_kg"]:
            total["berry_qty_per_kg"] = part["berry_qty_per_kg"]

    if quality_parts:
        c14 = sum(p["count14"] for p in quality_parts)
        c18 = sum(p["count18"] for p in quality_parts)
        s14 = sum((p["avg14"] or 0) * p["count14"] for p in quality_parts)
        s18 = sum((p["avg18"] or 0) * p["count18"] for p in quality_parts)
        florists: Dict[str, Dict[str, Any]] = {}
        for part in quality_parts:
            for name, fdata in part["florists"].items():
                cell = florists.setdefault(name, {"score": 0.0, "count14": 0, "count18": 0})
                cell["score"] += fdata["score"]
                cell["count14"] += fdata["count14"]
                cell["count18"] += fdata["count18"]
        total["quality"] = {
            "avg14": round(s14 / c14, 2) if c14 else None, "count14": c14,
            "avg18": round(s18 / c18, 2) if c18 else None, "count18": c18,
            "percent": _pct(s14 + s18, c14 * 14 + c18 * 18),
            "count": c14 + c18,
            "florists": florists,
        }

    return total


def _metrics(raw: Dict[str, Any], plan: Optional[float], progress: Dict[str, Any],
             previous_fact: Optional[float]) -> Dict[str, Any]:
    """Посчитать показатели из сырых сумм."""
    fact = raw["fact"]

    # 1. Отгрузки и план
    plan_done = _pct(fact, plan) if plan else None
    expected = plan * progress["passed"] / progress["days"] if plan else None
    days_left = progress["days"] - progress["passed"]
    need_per_day = None
    if plan and days_left > 0:
        need_per_day = max(0.0, (plan - fact) / days_left)

    delta = None
    if previous_fact:
        delta = round((fact - previous_fact) / previous_fact * 100, 1)

    # 5. Такси-службы
    taxi_share = _pct(raw["taxi_orders"], raw["courier_orders"])

    # 6. Списание цветка
    flower_loss = _pct(raw["flower_out"], raw["flower_in"])

    # 7. Средняя цена клубники. Знаменатель — вес, реально ушедший в дело;
    # он может стать нулём или отрицательным, если за период списали больше,
    # чем приняли (остаток прошлого месяца) — тогда цены нет, а не «минус».
    per_kg = raw["berry_qty_per_kg"] or 1
    berry_used = raw["berry_in_qty"] - raw["berry_out_qty"]
    berry_price = None
    berry_price_note = None
    if raw["berry_in_qty"] <= 0:
        berry_price_note = "нет оприходования за период"
    elif berry_used <= 0:
        berry_price_note = "списано больше, чем оприходовано"
    else:
        berry_price = round(raw["berry_in_sum"] / berry_used * per_kg, 2)
        if not (BERRY_PRICE_MIN <= berry_price <= BERRY_PRICE_MAX):
            berry_price_note = "проверьте единицу измерения товара"
    berry_buy_price = (round(raw["berry_in_sum"] / raw["berry_in_qty"] * per_kg, 2)
                       if raw["berry_in_qty"] else None)

    # 8. Доля расходов. Считается только когда есть обе части: приход из
    # МойСклада и отгрузки из CRM. Половинчатый маппинг салона иначе даёт либо
    # деление на ноль, либо бессмысленную долю.
    flower_cost = _pct(raw["flower_in"], fact) if fact else None
    berry_cost = _pct(raw["berry_in_sum"], fact) if fact else None
    raw_cost = _pct(raw["flower_in"] + raw["berry_in_sum"], fact) if fact else None

    quality = raw.get("quality")

    return {
        "shipments": {
            "fact": round(fact, 2),
            "orders": raw["orders"],
            "plan": plan,
            "plan_done": plan_done,
            "expected_now": round(expected, 2) if expected is not None else None,
            "need_per_day": round(need_per_day, 2) if need_per_day is not None else None,
            "per_day_now": round(fact / progress["passed"], 2) if progress["passed"] else None,
            "delta": delta,
            "previous_fact": round(previous_fact, 2) if previous_fact is not None else None,
        },
        "street": {
            "amount": round(raw["street"], 2),
            "share": _pct(raw["street"], fact) if fact else None,
        },
        "nos": {
            "confirmed": raw["nos_confirmed"],
            "in_review": raw["nos_in_review"],
            "total": raw["nos_total"],
            "categories": raw["nos_categories"],
        },
        "quality": {
            "percent": quality["percent"] if quality else None,
            "avg14": quality["avg14"] if quality else None,
            "count14": quality["count14"] if quality else 0,
            "avg18": quality["avg18"] if quality else None,
            "count18": quality["count18"] if quality else 0,
            "count": quality["count"] if quality else 0,
        },
        "taxi": {
            "share": taxi_share,
            "taxi_orders": raw["taxi_orders"],
            "courier_orders": raw["courier_orders"],
        },
        "raw_cost": {
            "flower": flower_cost,
            "berry": berry_cost,
            "total": raw_cost,
            "flower_amount": round(raw["flower_in"], 2),
            "berry_amount": round(raw["berry_in_sum"], 2),
        },
        "flower_loss": {
            "share": flower_loss,
            "written_off": round(raw["flower_out"], 2),
            "received": round(raw["flower_in"], 2),
            "alert": flower_loss is not None and flower_loss >= FLOWER_LOSS_ALERT,
        },
        "berry_price": {
            "price": berry_price,
            "buy_price": berry_buy_price,
            "note": berry_price_note,
            "in_qty": round(raw["berry_in_qty"], 2),
            "out_qty": round(raw["berry_out_qty"], 2),
            "used_qty": round(berry_used, 2),
            "qty_per_kg": raw["berry_qty_per_kg"],
        },
        "channels": sorted(
            ({"name": name, "amount": round(amount, 2)} for name, amount in raw["channels"].items()),
            key=lambda x: -x["amount"],
        )[:8],
    }


# ============================================================================
# Сводка
# ============================================================================

def build_summary(month: str, store_ids: Optional[List[int]], scope: str = "salon") -> Dict[str, Any]:
    """
    Показатели за месяц по доступным салонам.

    store_ids=None — все салоны (админ); пустой список означает, что у человека
    нет привязанных салонов, и это отдельное состояние, а не пустая таблица.
    """
    cache_key = (month, scope, tuple(sorted(store_ids)) if store_ids is not None else None)
    with _cache_lock:
        cached = _cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < CACHE_TTL_SECONDS:
            return cached[1]

    stores = storage.list_stores(store_ids)
    date_from, date_to = month_bounds(month)
    prev_from, prev_to = comparable_bounds(month)
    progress = month_progress(month)

    links = {
        "crm": storage.resolve_map(storage.SOURCE_CRM),
        "nos": storage.resolve_map(storage.SOURCE_NOS),
        "quality": storage.resolve_map(storage.SOURCE_QUALITY),
        "ms": storage.resolve_map(storage.SOURCE_MS_STORE),
    }

    sources = {
        "shipments": _shipments(date_from, date_to),
        "nos": _nos(date_from, date_to),
        "quality": _quality(date_from, date_to),
        "warehouse": _warehouse(date_from, date_to),
    }
    previous_shipments = _shipments(prev_from, prev_to)
    plans = storage.get_plans(month)

    rows = []
    raw_by_store = {}
    for store in stores:
        raw = _collect_raw(store, links, sources)
        raw_by_store[store["id"]] = raw

        previous_fact = sum(
            data["fact"] for site, data in previous_shipments.items()
            if links["crm"].get(site) == store["id"]
        ) or None

        rows.append({
            "store_id": store["id"],
            "name": store["name"],
            "city": store["city"],
            "metrics": _metrics(raw, plans.get(store["id"]), progress, previous_fact),
        })

    if scope == "city":
        by_city: Dict[str, List[Dict]] = {}
        for store in stores:
            by_city.setdefault(store["city"] or "Без города", []).append(store)
        city_rows = []
        for city, members in sorted(by_city.items()):
            raw = _sum_raw([raw_by_store[s["id"]] for s in members])
            plan_parts = [plans.get(s["id"]) for s in members]
            plan = sum(p for p in plan_parts if p) if any(plan_parts) else None
            previous_fact = sum(
                data["fact"] for site, data in previous_shipments.items()
                if links["crm"].get(site) in {s["id"] for s in members}
            ) or None
            city_rows.append({
                "city": city,
                "stores": [s["name"] for s in members],
                "metrics": _metrics(raw, plan, progress, previous_fact),
            })
        rows_out = city_rows
    else:
        rows_out = rows

    # Итог по всем доступным салонам — для плиток
    total_raw = _sum_raw(list(raw_by_store.values())) if raw_by_store else _blank()
    total_plan_parts = [plans.get(s["id"]) for s in stores]
    total_plan = sum(p for p in total_plan_parts if p) if any(total_plan_parts) else None
    total_previous = sum(
        data["fact"] for site, data in previous_shipments.items()
        if links["crm"].get(site) in {s["id"] for s in stores}
    ) or None

    result = {
        "month": month,
        "period": {"from": date_from, "to": date_to},
        "previous_period": {"from": prev_from, "to": prev_to},
        "progress": progress,
        "scope": scope,
        "stores_count": len(stores),
        "no_stores": len(stores) == 0,
        "total": _metrics(total_raw, total_plan, progress, total_previous),
        "rows": rows_out,
        "plans_missing": [s["name"] for s in stores if plans.get(s["id"]) is None],
        "freshness": _freshness(),
    }

    with _cache_lock:
        _cache[cache_key] = (time.monotonic(), result)
    return result


def salon_details(store_id: int, month: str) -> Dict[str, Any]:
    """Детализация одного салона: флористы, категории негатива, каналы, склад."""
    stores = storage.list_stores([store_id])
    if not stores:
        return {}

    store = stores[0]
    date_from, date_to = month_bounds(month)
    links = {
        "crm": storage.resolve_map(storage.SOURCE_CRM),
        "nos": storage.resolve_map(storage.SOURCE_NOS),
        "quality": storage.resolve_map(storage.SOURCE_QUALITY),
        "ms": storage.resolve_map(storage.SOURCE_MS_STORE),
    }
    sources = {
        "shipments": _shipments(date_from, date_to),
        "nos": _nos(date_from, date_to),
        "quality": _quality(date_from, date_to),
        "warehouse": _warehouse(date_from, date_to),
    }
    raw = _collect_raw(store, links, sources)
    progress = month_progress(month)
    plans = storage.get_plans(month)

    florists = []
    quality = raw.get("quality") or {}
    for name, data in (quality.get("florists") or {}).items():
        count = data["count14"] + data["count18"]
        if not count:
            continue
        possible = data["count14"] * 14 + data["count18"] * 18
        florists.append({
            "name": name,
            "count14": data["count14"],
            "count18": data["count18"],
            "count": count,
            # Процент от максимума СВОИХ видов заказа: у флориста, собирающего
            # больше 18-балльных, сырой средний балл был бы выше просто из-за
            # состава заказов
            "percent": _pct(data["score"], possible),
        })
    florists.sort(key=lambda f: -(f["percent"] or 0))

    return {
        "store_id": store["id"],
        "name": store["name"],
        "city": store["city"],
        "month": month,
        "period": {"from": date_from, "to": date_to},
        "metrics": _metrics(raw, plans.get(store["id"]), progress, None),
        "florists": florists,
        "nos_categories": sorted(
            ({"name": k, "count": v} for k, v in raw["nos_categories"].items()),
            key=lambda x: -x["count"],
        ),
    }


def _freshness() -> Dict[str, Any]:
    """Когда данные обновлялись в последний раз и с какого числа они полны."""
    info: Dict[str, Any] = {}

    try:
        from couriers import storage as couriers_storage
        info["crm"] = couriers_storage.shipments_data_range()
        info["crm"]["last_sync"] = (couriers_storage.get_latest_sync_log(status="completed") or {}).get(
            "finished_at")
    except Exception as e:
        logger.warning(f"Не удалось получить свежесть данных CRM: {e}")

    try:
        from pyrus import nos
        info["nos"] = nos.data_range()
    except Exception as e:
        logger.warning(f"Не удалось получить свежесть данных НОС: {e}")

    try:
        from moysklad import warehouse
        from moysklad.server import get_db as get_ms_db
        info["warehouse"] = warehouse.data_range(get_ms_db())
    except Exception as e:
        logger.warning(f"Не удалось получить свежесть данных склада: {e}")

    return info


# ============================================================================
# Несопоставленное
# ============================================================================

def unmapped(month: str) -> Dict[str, Any]:
    """
    Ключи источников, встреченные в данных, но не привязанные ни к одному салону.

    Это защита от тихой потери: салон переименовали — его показатель обнулился,
    и без этого списка никто бы не узнал, почему.
    """
    date_from, date_to = month_bounds(month)
    links = {
        "crm": storage.resolve_map(storage.SOURCE_CRM),
        "nos": storage.resolve_map(storage.SOURCE_NOS),
        "quality": storage.resolve_map(storage.SOURCE_QUALITY),
        "ms": storage.resolve_map(storage.SOURCE_MS_STORE),
    }
    items: List[Dict[str, Any]] = []

    try:
        from couriers import storage as couriers_storage
        for row in couriers_storage.list_unmapped_sites(date_from, date_to, list(links["crm"])):
            items.append({
                "source": storage.SOURCE_CRM,
                "source_name": storage.SOURCES[storage.SOURCE_CRM],
                "key": row["key"],
                "meta": f"{row['orders']} заказов · {row['amount']:,.0f} ₽".replace(",", " "),
                "weight": row["amount"],
            })
    except Exception as e:
        logger.warning(f"Несопоставленные сайты CRM недоступны: {e}")

    try:
        from pyrus import nos, quality
        for row in nos.list_salons(date_from, date_to):
            if row["key"] not in links["nos"]:
                items.append({
                    "source": storage.SOURCE_NOS,
                    "source_name": storage.SOURCES[storage.SOURCE_NOS],
                    "key": row["key"],
                    "meta": f"{row['count']} обращений",
                    "weight": row["count"],
                })
        report = quality.generate_report(date_from, date_to)
        for salon, data in (report.get("salons") or {}).items():
            if salon and salon not in links["quality"]:
                items.append({
                    "source": storage.SOURCE_QUALITY,
                    "source_name": storage.SOURCES[storage.SOURCE_QUALITY],
                    "key": salon,
                    "meta": f"{data['total']['count']} оценок",
                    "weight": data["total"]["count"],
                })
    except Exception as e:
        logger.warning(f"Несопоставленные салоны Pyrus недоступны: {e}")

    try:
        from moysklad import warehouse
        from moysklad.server import get_db as get_ms_db
        for row in warehouse.list_stores_with_flows(get_ms_db(), date_from, date_to):
            if row["key"] not in links["ms"]:
                items.append({
                    "source": storage.SOURCE_MS_STORE,
                    "source_name": storage.SOURCES[storage.SOURCE_MS_STORE],
                    "key": row["key"],
                    "label": row["name"],
                    "meta": f"{row['docs']} документов · {row['amount']:,.0f} ₽".replace(",", " "),
                    "weight": row["amount"],
                })
    except Exception as e:
        logger.warning(f"Несопоставленные склады МойСклада недоступны: {e}")

    items.sort(key=lambda x: -(x.get("weight") or 0))
    for item in items:
        item["suggestion"] = _suggest_store(item.get("label") or item["key"])

    return {"month": month, "period": {"from": date_from, "to": date_to}, "items": items}


_NORMALIZE_RE = re.compile(r"[^a-zа-я0-9]+")

# Ключи RetailCRM записаны латиницей («barkhat-barnaul», «nsk-voskhod-3»), а
# салоны в справочнике — кириллицей. Без транслитерации подсказка для них не
# работала вовсе: общих токенов у «barkhat barnaul» и «барнаул советская» нет.
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh", "з": "z",
    "и": "i", "й": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p",
    "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch",
    "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "iu", "я": "ia",
}

# Сокращения городов, которыми пользуются в CRM и в формах Pyrus
_CITY_TOKENS = {
    "нск": "novosibirsk", "nsk": "novosibirsk", "новосибирск": "novosibirsk",
    "екб": "ekaterinburg", "ekb": "ekaterinburg", "екатеринбург": "ekaterinburg",
    "брн": "barnaul", "барнаул": "barnaul", "barnaul": "barnaul",
    "томск": "tomsk", "tomsk": "tomsk",
    "члб": "cheliabinsk", "челябинск": "cheliabinsk", "cheliabinsk": "cheliabinsk",
}


def _normalize(value: str) -> str:
    return _NORMALIZE_RE.sub(" ", (value or "").lower().replace("ё", "е")).strip()


def _translit(value: str) -> str:
    return "".join(_TRANSLIT.get(char, char) for char in value)


def _tokens(value: str) -> set:
    """Токены строки в латинице плюс нормализованные названия городов."""
    raw = _normalize(value).split()
    out = set()
    for token in raw:
        out.add(_translit(token))
        if token in _CITY_TOKENS:
            out.add(_CITY_TOKENS[token])
    return out


def _suggest_store(key: str) -> Optional[Dict[str, Any]]:
    """
    Предложить салон для непривязанного ключа.

    Подсказка выдаётся, только когда лучший вариант ОДИН. Ключ «barkhat-barnaul»
    одинаково похож на оба барнаульских салона — и такую подсказку показывать
    нельзя: человек подтвердит её не глядя, а ошибка сопоставления тихо
    переложит выручку между салонами. Лучше промолчать, чем угадать неверно.

    Связь всегда создаёт человек: автоматически — никогда.
    """
    key_tokens = _tokens(key)
    if not key_tokens:
        return None

    key_digits = {t for t in _normalize(key).split() if t.isdigit()}
    scored = []

    for store in storage.list_stores():
        store_tokens = _tokens(store["name"])
        common = key_tokens & store_tokens
        if not common:
            continue

        # Служебные слова ничего не значат: «barkhat» есть в половине кодов CRM
        meaningful = {t for t in common if t not in ("barkhat", "bh")}
        if not meaningful:
            continue

        score = len(meaningful)
        # Совпадение города — самостоятельный признак: «barkhat-tomsk» сходится
        # с «Томск Дальне-Ключевская» только по нему. Ничью это не создаёт: если
        # в городе два салона, оба получат одинаковый счёт и подсказки не будет
        if meaningful & set(_CITY_TOKENS.values()):
            score += 1
        # Совпадение номера дома — сильный признак: «Свердловский 23» и
        # «Челябинск пр-кт Свердловский, д 23»
        if key_digits & {t for t in _normalize(store["name"]).split() if t.isdigit()}:
            score += 2

        scored.append({"store_id": store["id"], "name": store["name"], "score": score})

    if not scored:
        return None

    scored.sort(key=lambda s: -s["score"])
    best = scored[0]
    if best["score"] < 2:
        return None
    # Ничья — значит непонятно, какой именно салон; подсказку не даём
    if len(scored) > 1 and scored[1]["score"] == best["score"]:
        return None

    return {"store_id": best["store_id"], "name": best["name"]}
