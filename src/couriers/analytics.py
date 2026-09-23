"""
Аналитика по работе курьеров (вкладка «Аналитика» раздела «Контроль доставки»).

Отвечает на шесть вопросов управляющего за выбранный период и набор салонов:
сколько броней взяли, сколько довезли, какая доля вовремя, на сколько опаздывают,
сколько броней сняли руками и сколько заказов ушло службе доставки.

Три решения, которые определяют смысл всех чисел:

1. **Период считается по дате доставки заказа**, а не по дате брони. Тогда все
   метрики живут на одном множестве заказов и сходятся между собой: броней =
   доставлено + снято руками + аутсорс + остальное. По дате брони заказ,
   взятый 30.09 на 01.10, попал бы в сентябрь бронью и в октябрь доставкой.

2. **Считаем БРОНИ, а не заказы.** У одного заказа их бывает несколько подряд:
   взял → снял → взял другой курьер. «Броней 247» — это 247 нажатий «Взять».

3. **Курьер — это `courier_user_id`.** `courier_name` в брони — снимок имени на
   момент захвата: человека переименовали, и он разъехался бы на две строки.
   Показываем имя из самой свежей брони.

Формула «вовремя» живёт НЕ здесь, а в `salon_time` (`deadline_utc` +
`lateness_minutes`): её же считает сводка `delivery_metrics`, и две копии
разъехались бы на первой правке — два экрана дашборда отвечали бы на один
вопрос разными числами.

Непосчитанное здесь никогда не превращается в ноль: заказ без интервала и
заказ салона без часового пояса не идут ни в числитель доли, ни в знаменатель,
а попадают в отдельные счётчики с именами салонов. Иначе метрика тихо считает
их доставленными в срок и зовёт чинить то, что не сломано.
"""

import time
from typing import Any, Dict, List, Optional

from . import salon_time
from .delivery_storage import (
    RELEASE_ADMIN,
    RELEASE_EXPIRED,
    RELEASE_ORDER_GONE,
    RELEASE_OUTSOURCED,
    RELEASE_SELF,
    STATE_CLAIMED,
    STATE_DELIVERED,
    STATE_PICKED_UP,
    STATE_PROBLEM,
    STATE_RELEASED,
)
from .storage import get_db

# Ниже этого числа посчитанных доставок процент не описывает работу человека:
# «50,0 %» при двух доставках читается так же уверенно, как «86,4 %» при двух
# сотнях, и по нему примут решение о курьере. Процент показываем всё равно —
# прятать значит снова считать его в голове, — но помечаем.
LOW_DATA_DELIVERIES = 5


def _site_filter(site_codes: Optional[List[str]]) -> (str, List[Any]):
    """Условие по салонам. Пустой список и None означают «все салоны»."""
    codes = [code for code in (site_codes or []) if code]
    if not codes:
        return "", []
    marks = ",".join("?" for _ in codes)
    return f" AND o.site_code IN ({marks})", list(codes)


def _minutes_avg(values: List[float]) -> Optional[float]:
    """Среднее или None. Пустой список — это «не считали», а не ноль минут."""
    return round(sum(values) / len(values), 1) if values else None


def _median(values: List[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _local_time(moment: Optional[str], utc_offset: Optional[int]) -> Optional[str]:
    """
    Отметка UTC → «14:30» по стенным часам салона.

    `None` — если пояса нет: подставить сюда UTC значило бы показать время,
    которое выглядит настоящим и отличается от плана на пять часов.
    """
    from datetime import datetime

    if not moment or utc_offset is None:
        return None
    try:
        return salon_time.utc_to_local(
            datetime.fromisoformat(str(moment)), utc_offset).strftime("%H:%M")
    except (ValueError, TypeError):
        return None


def _minutes_between(start: Optional[str], end: Optional[str]) -> Optional[float]:
    from datetime import datetime

    if not start or not end:
        return None
    try:
        return (datetime.fromisoformat(str(end))
                - datetime.fromisoformat(str(start))).total_seconds() / 60.0
    except (ValueError, TypeError):
        return None


def load_analytics(date_from: str, date_to: str,
                   site_codes: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    Все числа вкладки за один поход в базу.

    Одно соединение на весь расчёт: диск `/data` сетевой, и цену ответа
    определяет число обращений к нему, а не объём арифметики. Разбивка по
    шагам уезжает в `timings_ms` — следующий разбор «почему медленно» обязан
    начинаться с числа, а не с чтения кода.
    """
    started = time.monotonic()
    timings: Dict[str, float] = {}

    def mark(name: str, since: float) -> float:
        now = time.monotonic()
        timings[name] = round((now - since) * 1000, 1)
        return now

    site_clause, site_params = _site_filter(site_codes)
    period = [date_from, date_to]
    step = started

    with get_db() as conn:
        claims = [dict(row) for row in conn.execute(f"""
            SELECT a.id, a.retailcrm_order_id, a.courier_user_id, a.courier_name,
                   a.state, a.release_reason, a.claimed_at, a.picked_up_at,
                   a.delivered_at, a.released_at,
                   o.order_number, o.delivery_date, o.delivery_time_from,
                   o.delivery_time_to, o.site_code, o.net_cost,
                   s.name AS site_name, s.utc_offset
              FROM delivery_assignments a
              JOIN courier_orders o ON o.retailcrm_order_id = a.retailcrm_order_id
              LEFT JOIN courier_sites s ON s.code = o.site_code
             WHERE o.delivery_date >= ? AND o.delivery_date <= ?{site_clause}
             ORDER BY a.claimed_at, a.id
        """, (*period, *site_params)).fetchall()]
        step = mark("claims", step)

        # Какие типы доставки считаются курьерскими, решает человек в
        # справочнике. Пустой справочник — это «ничего не показываем», а не
        # «показываем всё»: иначе блок «ушли службе» покажет вообще все заказы
        # периода и будет выглядеть катастрофой.
        courier_codes = [row["code"] for row in conn.execute(
            "SELECT code FROM delivery_types WHERE counts_as_courier = 1"
        ).fetchall()]
        step = mark("delivery_types", step)

        never_claimed: List[Dict[str, Any]] = []
        if courier_codes:
            marks = ",".join("?" for _ in courier_codes)
            never_claimed = [dict(row) for row in conn.execute(f"""
                SELECT o.retailcrm_order_id, o.order_number, o.delivery_date,
                       o.delivery_time_from, o.delivery_time_to,
                       o.site_code, o.status, s.name AS site_name
                  FROM courier_orders o
                  LEFT JOIN courier_sites s ON s.code = o.site_code
                 WHERE o.delivery_date >= ? AND o.delivery_date <= ?{site_clause}
                   AND (o.delivery_code IS NULL OR o.delivery_code NOT IN ({marks}))
                   AND NOT EXISTS (SELECT 1 FROM delivery_assignments a
                                    WHERE a.retailcrm_order_id = o.retailcrm_order_id)
                 ORDER BY o.delivery_date DESC, o.retailcrm_order_id DESC
            """, (*period, *site_params, *courier_codes)).fetchall()]
        step = mark("never_claimed", step)

    # --- разбор брони по смыслу ------------------------------------------
    #
    # Считается в Python, а не в SQL: опоздание требует пояса салона и разбора
    # строки времени, и переносить это в запрос значило бы писать ту же
    # формулу второй раз на другом языке.

    couriers: Dict[Any, Dict[str, Any]] = {}
    late_orders: List[Dict[str, Any]] = []
    outsourced_orders: List[Dict[str, Any]] = []

    totals = {
        "claims": len(claims),
        "delivered": 0,
        "released_by_hand": 0,
        "released_self": 0,
        "released_admin": 0,
        "released_expired": 0,
        "outsourced_after_claim": 0,
        "order_gone": 0,
        "active": 0,
        "problem": 0,
        "on_time": 0,
        "late": 0,
    }
    late_minutes: List[float] = []
    pickup_minutes: List[float] = []
    outsourced_amount = 0.0
    no_interval = 0
    sites_without_timezone: Dict[str, str] = {}

    for claim in claims:
        user_id = claim["courier_user_id"]
        row = couriers.setdefault(user_id, {
            "courier_user_id": user_id,
            "courier_name": claim["courier_name"],
            "claims": 0, "delivered": 0, "released_by_hand": 0,
            "outsourced": 0, "on_time": 0, "late": 0,
            "_late_minutes": [],
        })
        # Имя из самой свежей брони: выборка отсортирована по claimed_at, и
        # последняя запись перетирает прежнее написание.
        if claim["courier_name"]:
            row["courier_name"] = claim["courier_name"]
        row["claims"] += 1

        state = claim["state"]
        reason = claim["release_reason"]

        if state == STATE_DELIVERED:
            totals["delivered"] += 1
            row["delivered"] += 1
        elif state in (STATE_CLAIMED, STATE_PICKED_UP):
            totals["active"] += 1
        elif state == STATE_PROBLEM:
            totals["problem"] += 1
        elif state == STATE_RELEASED:
            if reason in (RELEASE_SELF, RELEASE_ADMIN):
                totals["released_by_hand"] += 1
                row["released_by_hand"] += 1
                if reason == RELEASE_SELF:
                    totals["released_self"] += 1
                else:
                    totals["released_admin"] += 1
            elif reason == RELEASE_EXPIRED:
                totals["released_expired"] += 1
            elif reason == RELEASE_ORDER_GONE:
                totals["order_gone"] += 1

        if reason == RELEASE_OUTSOURCED:
            totals["outsourced_after_claim"] += 1
            row["outsourced"] += 1
            outsourced_amount += float(claim["net_cost"] or 0)
            outsourced_orders.append({
                "retailcrm_order_id": claim["retailcrm_order_id"],
                "order_number": claim["order_number"],
                "courier_user_id": user_id,
                "courier_name": claim["courier_name"],
                "site_name": claim["site_name"] or claim["site_code"],
                "delivery_date": claim["delivery_date"],
                "released_at": claim["released_at"],
            })

        pickup = _minutes_between(claim["claimed_at"], claim["picked_up_at"])
        if pickup is not None and pickup >= 0:
            pickup_minutes.append(pickup)

        # --- вовремя или опоздал ---
        if state != STATE_DELIVERED:
            continue
        if claim["utc_offset"] is None:
            code = claim["site_code"] or ""
            sites_without_timezone[code] = claim["site_name"] or code
            continue
        deadline = salon_time.deadline_utc(claim["delivery_date"],
                                           claim["delivery_time_to"],
                                           claim["utc_offset"])
        if deadline is None:
            no_interval += 1
            continue
        minutes = salon_time.lateness_minutes(claim["delivered_at"], deadline)
        if minutes is None:
            no_interval += 1
            continue
        if minutes > 0:
            totals["late"] += 1
            row["late"] += 1
            late_minutes.append(minutes)
            row["_late_minutes"].append(minutes)
            late_orders.append({
                "retailcrm_order_id": claim["retailcrm_order_id"],
                "order_number": claim["order_number"],
                "courier_user_id": user_id,
                "courier_name": claim["courier_name"],
                "site_name": claim["site_name"] or claim["site_code"],
                "delivery_date": claim["delivery_date"],
                "time_from": claim["delivery_time_from"],
                "time_to": claim["delivery_time_to"],
                "delivered_at": claim["delivered_at"],
                # Факт — в стенных часах САЛОНА, потому что рядом с ним в
                # таблице стоит плановый интервал, а он в них же. Отдать сюда
                # UTC значило бы поставить рядом две шкалы: управляющий увидел
                # бы «план 12:00–14:00, факт 09:30» и решил, что привезли
                # раньше срока.
                "delivered_local": _local_time(claim["delivered_at"],
                                               claim["utc_offset"]),
                "late_minutes": round(minutes),
            })
        else:
            totals["on_time"] += 1
            row["on_time"] += 1

    counted = totals["on_time"] + totals["late"]
    totals["on_time_share"] = (round(totals["on_time"] / counted * 100, 1)
                               if counted else None)
    totals["late_minutes_avg"] = _minutes_avg(late_minutes)
    totals["minutes_to_pickup_median"] = (round(_median(pickup_minutes), 1)
                                          if pickup_minutes else None)
    totals["outsourced_amount"] = round(outsourced_amount, 2)
    totals["outsourced_never_claimed"] = len(never_claimed)
    totals["not_counted"] = {
        "no_interval": no_interval,
        "no_timezone": sum(1 for c in claims
                           if c["state"] == STATE_DELIVERED and c["utc_offset"] is None),
        "sites_without_timezone": sorted(sites_without_timezone.values()),
    }

    rows = []
    for row in couriers.values():
        row_counted = row["on_time"] + row["late"]
        row["on_time_share"] = (round(row["on_time"] / row_counted * 100, 1)
                                if row_counted else None)
        row["late_minutes_avg"] = _minutes_avg(row.pop("_late_minutes"))
        # «Мало данных» — это про число ПОСЧИТАННЫХ доставок, а не всех:
        # курьер с сорока доставками без интервала знает о своей доле столько
        # же, сколько курьер с двумя.
        row["low_data"] = row_counted < LOW_DATA_DELIVERIES
        row["counted"] = row_counted
        rows.append(row)

    # Кто больше возит — выше. Тай-брейкер по идентификатору: без него две
    # строки с равным числом доставок меняются местами между запросами, и
    # таблица «прыгает» на каждом обновлении.
    rows.sort(key=lambda r: (-r["delivered"], -r["claims"],
                             r["courier_user_id"] or 0))
    late_orders.sort(key=lambda r: (-r["late_minutes"], r["delivery_date"]))
    outsourced_orders.sort(key=lambda r: (r["delivery_date"], r["order_number"] or ""),
                           reverse=True)
    mark("aggregate", step)

    timings["total"] = round((time.monotonic() - started) * 1000, 1)
    return {
        "period": {"from": date_from, "to": date_to},
        "site_codes": [code for code in (site_codes or []) if code],
        "totals": totals,
        "couriers": rows,
        "late_orders": late_orders,
        "outsourced_after_claim": outsourced_orders,
        "outsourced_never_claimed": never_claimed,
        # Честно называем, чего не умеем: момент появления заказа в ленте нигде
        # не записан, а `delivered_at` — это отметка курьера, а не факт вручения.
        "not_measured": [
            "время от появления заказа до брони",
            "фактическое вручение (считаем по отметке курьера «Доставил»)",
        ],
        "timings_ms": timings,
    }
