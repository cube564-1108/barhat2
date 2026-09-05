"""
Ёмкость салонов: сколько единиц трудоёмкости салон успевает за час.

База — barhat.db, рядом со `stores` и `salon_links`: таблицы крошечные
(9 салонов × 168 часов), а соединять их с заказами в SQL всё равно нельзя —
заказы живут в couriers.db. Значит выигрыш от «положить рядом с заказами»
нулевой, а связь со справочником салонов важнее.

Три состояния ячейки, и путать их нельзя:
  - ёмкость задана числом    → проценты считаются;
  - ёмкости нет (строки нет) → «не задана», проценты не считаются;
  - салон закрыт (is_closed) → это не ноль загрузки, это отсутствие работы.
"""

import logging
import os
import sqlite3
import sys
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlite_conn import connect as sqlite_connect
from storage_paths import resolve as resolve_data_path

logger = logging.getLogger(__name__)

# Путь резолвится в модуле при импорте и только через storage_paths: env с
# относительным дефолтом на Amvera кладёт базу на эфемерный /app, который
# пересоздаётся каждой сборкой.
DB_PATH = resolve_data_path("BARHAT_DB_PATH", "barhat.db")

# Часы, для которых вообще имеет смысл держать сетку. Салон не работает ночью,
# но заказ «на сейчас» в 01:08 в данных встречается — поэтому сетка полная,
# а не 9–22: закрытые часы это отдельное состояние, а не отсутствие строки.
HOURS = tuple(range(24))
WEEKDAYS = tuple(range(7))  # 0 — понедельник, как date.weekday()


def get_db() -> sqlite3.Connection:
    """Соединение с общей базой. Настройки — в sqlite_conn (WAL один раз на файл)."""
    return sqlite_connect(DB_PATH, timeout=20)


def init_salonload_tables() -> None:
    """Создать таблицы модуля (идемпотентно, зовётся при старте каждого воркера)."""
    conn = get_db()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS salon_capacity (
                store_id INTEGER NOT NULL,
                weekday INTEGER NOT NULL,
                hour INTEGER NOT NULL,
                capacity_units REAL,
                pickup_capacity REAL,
                is_closed INTEGER NOT NULL DEFAULT 0,
                updated_by TEXT,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_salon_capacity_slot "
            "ON salon_capacity(store_id, weekday, hour)"
        )

        # Исключения на конкретную дату: праздник, отпуск, поломка. Причина
        # текстом — вопрос «почему в этот день было столько» возникает всегда,
        # и отвечать на него по памяти не выйдет.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS salon_capacity_exceptions (
                store_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                hour INTEGER NOT NULL,
                capacity_units REAL,
                pickup_capacity REAL,
                is_closed INTEGER NOT NULL DEFAULT 0,
                reason TEXT,
                updated_by TEXT,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_salon_capacity_exc_slot "
            "ON salon_capacity_exceptions(store_id, date, hour)"
        )
        conn.commit()
    finally:
        conn.close()


# ============================================================================
# Чтение
# ============================================================================

def weekly_grid(store_id: int) -> Dict[str, Dict[str, Any]]:
    """Недельная сетка салона: {"weekday:hour": {...}}."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT weekday, hour, capacity_units, pickup_capacity, is_closed "
            "FROM salon_capacity WHERE store_id = ?",
            (store_id,),
        ).fetchall()
    finally:
        conn.close()
    return {
        f"{row['weekday']}:{row['hour']}": {
            "capacity": row["capacity_units"],
            "pickup_capacity": row["pickup_capacity"],
            "closed": bool(row["is_closed"]),
        }
        for row in rows
    }


def exceptions_for(store_ids: List[int], date_from: str, date_to: str) -> Dict[str, Dict[str, Any]]:
    """Исключения за период: {"store_id:date:hour": {...}}."""
    if not store_ids:
        return {}
    placeholders = ",".join("?" * len(store_ids))
    conn = get_db()
    try:
        rows = conn.execute(
            f"""
            SELECT store_id, date, hour, capacity_units, pickup_capacity, is_closed, reason
              FROM salon_capacity_exceptions
             WHERE store_id IN ({placeholders}) AND date >= ? AND date <= ?
            """,
            (*store_ids, date_from, date_to),
        ).fetchall()
    finally:
        conn.close()
    return {
        f"{row['store_id']}:{row['date']}:{row['hour']}": {
            "capacity": row["capacity_units"],
            "pickup_capacity": row["pickup_capacity"],
            "closed": bool(row["is_closed"]),
            "reason": row["reason"],
        }
        for row in rows
    }


def capacity_map(store_ids: List[int]) -> Dict[str, Dict[str, Any]]:
    """Недельные сетки нескольких салонов: {"store_id:weekday:hour": {...}}."""
    if not store_ids:
        return {}
    placeholders = ",".join("?" * len(store_ids))
    conn = get_db()
    try:
        rows = conn.execute(
            f"""
            SELECT store_id, weekday, hour, capacity_units, pickup_capacity, is_closed
              FROM salon_capacity WHERE store_id IN ({placeholders})
            """,
            tuple(store_ids),
        ).fetchall()
    finally:
        conn.close()
    return {
        f"{row['store_id']}:{row['weekday']}:{row['hour']}": {
            "capacity": row["capacity_units"],
            "pickup_capacity": row["pickup_capacity"],
            "closed": bool(row["is_closed"]),
        }
        for row in rows
    }


def stores_with_capacity() -> List[int]:
    """Салоны, у которых ёмкость вообще задана — для плашки «не задана»."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT DISTINCT store_id FROM salon_capacity WHERE capacity_units IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()
    return [row["store_id"] for row in rows]


# ============================================================================
# Запись
# ============================================================================

def set_slots(store_id: int, slots: List[Dict[str, Any]], username: Optional[str] = None) -> int:
    """
    Проставить ячейки недельной сетки пачкой.

    slot: {"weekday": 0-6, "hour": 0-23, "capacity": число|None,
           "pickup_capacity": число|None, "closed": bool}

    capacity=None и closed=False — это «ёмкость не задана»: строка удаляется,
    чтобы «не задана» и «ноль» не оказались одним и тем же числом в базе.
    """
    if not slots:
        return 0

    conn = get_db()
    try:
        for slot in slots:
            weekday, hour = int(slot["weekday"]), int(slot["hour"])
            if weekday not in WEEKDAYS or hour not in HOURS:
                raise ValueError(f"Некорректный слот: день {weekday}, час {hour}")

            capacity = slot.get("capacity")
            pickup = slot.get("pickup_capacity")
            closed = bool(slot.get("closed"))

            if capacity is None and pickup is None and not closed:
                conn.execute(
                    "DELETE FROM salon_capacity WHERE store_id = ? AND weekday = ? AND hour = ?",
                    (store_id, weekday, hour),
                )
                continue

            if capacity is not None and float(capacity) < 0:
                raise ValueError("Ёмкость не может быть отрицательной")

            conn.execute(
                """
                INSERT INTO salon_capacity
                       (store_id, weekday, hour, capacity_units, pickup_capacity, is_closed,
                        updated_by, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(store_id, weekday, hour) DO UPDATE SET
                    capacity_units = excluded.capacity_units,
                    pickup_capacity = excluded.pickup_capacity,
                    is_closed = excluded.is_closed,
                    updated_by = excluded.updated_by,
                    updated_at = datetime('now')
                """,
                (store_id, weekday, hour,
                 None if capacity is None else float(capacity),
                 None if pickup is None else float(pickup),
                 1 if closed else 0, username),
            )
        conn.commit()
        return len(slots)
    finally:
        conn.close()


def apply_working_hours(store_id: int, open_hour: int, close_hour: int, capacity: float,
                        pickup_capacity: Optional[float] = None,
                        weekdays: Optional[List[int]] = None,
                        username: Optional[str] = None) -> int:
    """
    Заполнить сетку одним движением: часы работы + ёмкость в час.

    Без этого Фаза 4 — это 1512 полей руками на девять салонов, и она просто
    не будет заполнена, а модуль покажет пустоту. Часы вне окна помечаются
    закрытыми — это не нулевая загрузка, а отсутствие работы.
    """
    if not (0 <= open_hour <= 23 and 1 <= close_hour <= 24 and open_hour < close_hour):
        raise ValueError("Часы работы заданы неверно")
    if capacity <= 0:
        raise ValueError("Ёмкость должна быть больше нуля")

    days = weekdays if weekdays is not None else list(WEEKDAYS)
    slots = []
    for weekday in days:
        for hour in HOURS:
            working = open_hour <= hour < close_hour
            slots.append({
                "weekday": weekday,
                "hour": hour,
                "capacity": capacity if working else None,
                "pickup_capacity": pickup_capacity if working else None,
                "closed": not working,
            })
    return set_slots(store_id, slots, username)


def copy_week(source_store_id: int, target_store_id: int, username: Optional[str] = None) -> int:
    """Скопировать график другого салона — второй способ не заполнять 168 полей."""
    source = weekly_grid(source_store_id)
    slots = []
    for key, value in source.items():
        weekday, hour = key.split(":")
        slots.append({
            "weekday": int(weekday),
            "hour": int(hour),
            "capacity": value["capacity"],
            "pickup_capacity": value["pickup_capacity"],
            "closed": value["closed"],
        })
    return set_slots(target_store_id, slots, username)


def set_exception(store_id: int, date: str, hour: Optional[int], capacity: Optional[float],
                  pickup_capacity: Optional[float] = None, closed: bool = False,
                  reason: Optional[str] = None, username: Optional[str] = None) -> int:
    """
    Исключение на дату. hour=None — на весь день (все 24 часа).

    capacity=None и closed=False снимает исключение: день возвращается к
    обычному графику.
    """
    hours = HOURS if hour is None else (int(hour),)
    conn = get_db()
    try:
        for h in hours:
            if capacity is None and pickup_capacity is None and not closed:
                conn.execute(
                    "DELETE FROM salon_capacity_exceptions "
                    "WHERE store_id = ? AND date = ? AND hour = ?",
                    (store_id, date, h),
                )
                continue
            conn.execute(
                """
                INSERT INTO salon_capacity_exceptions
                       (store_id, date, hour, capacity_units, pickup_capacity, is_closed,
                        reason, updated_by, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(store_id, date, hour) DO UPDATE SET
                    capacity_units = excluded.capacity_units,
                    pickup_capacity = excluded.pickup_capacity,
                    is_closed = excluded.is_closed,
                    reason = excluded.reason,
                    updated_by = excluded.updated_by,
                    updated_at = datetime('now')
                """,
                (store_id, date, h,
                 None if capacity is None else float(capacity),
                 None if pickup_capacity is None else float(pickup_capacity),
                 1 if closed else 0, reason, username),
            )
        conn.commit()
        return len(hours)
    finally:
        conn.close()


def list_exceptions(store_ids: List[int], date_from: str, date_to: str) -> List[Dict[str, Any]]:
    """Исключения периода, свёрнутые по дате — для экрана настроек."""
    if not store_ids:
        return []
    placeholders = ",".join("?" * len(store_ids))
    conn = get_db()
    try:
        rows = conn.execute(
            f"""
            SELECT store_id, date, COUNT(*) AS hours,
                   SUM(is_closed) AS closed_hours,
                   MAX(reason) AS reason,
                   MIN(capacity_units) AS min_capacity,
                   MAX(capacity_units) AS max_capacity
              FROM salon_capacity_exceptions
             WHERE store_id IN ({placeholders}) AND date >= ? AND date <= ?
          GROUP BY store_id, date
          ORDER BY date
            """,
            (*store_ids, date_from, date_to),
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "store_id": row["store_id"],
            "date": row["date"],
            "hours": row["hours"],
            "closed_hours": row["closed_hours"] or 0,
            "reason": row["reason"],
            "min_capacity": row["min_capacity"],
            "max_capacity": row["max_capacity"],
        }
        for row in rows
    ]
