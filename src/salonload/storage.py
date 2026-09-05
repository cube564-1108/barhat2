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


# Часовые пояса городов. Нужны ровно для одного: понять, который сейчас час в
# салоне, — «перегруз через 3 часа» иначе посчитается по времени сервера (UTC)
# и приедет на 5–7 часов мимо.
#
# Значение сидируется по городу, но ХРАНИТСЯ полем и правится руками: город
# выводится из названия салона, а название живёт по своим законам. Новый город
# без записи в этой таблице не получает «наиболее вероятный» пояс — он просто
# не участвует в предупреждениях и виден в списке «пояс не задан».
CITY_OFFSETS = {
    "Новосибирск": 7,
    "Томск": 7,
    "Барнаул": 7,
    "Екатеринбург": 5,
    "Челябинск": 5,
}


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

        # Пояс салона: смещение от UTC в часах.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS salon_timezones (
                store_id INTEGER PRIMARY KEY,
                utc_offset INTEGER NOT NULL,
                updated_by TEXT,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)

        # Предупреждения о перегрузе. Уникальность по (салон, дата, час,
        # горизонт) — чтобы об одном и том же слоте не напоминать каждые
        # полчаса: предупреждение, которое повторяется, перестают читать.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS salon_load_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                store_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                hour INTEGER NOT NULL,
                horizon TEXT NOT NULL,
                percent REAL,
                units REAL,
                capacity REAL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                dismissed_at TEXT,
                resolved_at TEXT,
                resolved_percent REAL
            )
        """)
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_salon_load_alert_slot "
            "ON salon_load_alerts(store_id, date, hour, horizon)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_salon_load_alert_date ON salon_load_alerts(date)"
        )
        conn.commit()
    finally:
        conn.close()


def seed_timezones(stores: List[Dict[str, Any]]) -> int:
    """
    Проставить пояса по городу — один раз, существующие записи не трогаем.

    Салон, город которого мы не знаем, остаётся без пояса намеренно: угаданный
    пояс сдвинул бы предупреждения на несколько часов, и заметили бы это только
    по жалобе.
    """
    added = 0
    conn = get_db()
    try:
        for store in stores:
            offset = CITY_OFFSETS.get(store.get("city"))
            if offset is None:
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO salon_timezones (store_id, utc_offset) VALUES (?, ?)",
                (store["id"], offset),
            )
            added += cur.rowcount or 0
        conn.commit()
        return added
    finally:
        conn.close()


def timezone_map() -> Dict[int, int]:
    """{store_id: смещение от UTC}."""
    conn = get_db()
    try:
        rows = conn.execute("SELECT store_id, utc_offset FROM salon_timezones").fetchall()
    finally:
        conn.close()
    return {row["store_id"]: row["utc_offset"] for row in rows}


def set_timezone(store_id: int, utc_offset: int, username: Optional[str] = None) -> None:
    if not -12 <= utc_offset <= 14:
        raise ValueError("Смещение должно быть от -12 до +14 часов")
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO salon_timezones (store_id, utc_offset, updated_by, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(store_id) DO UPDATE SET
                utc_offset = excluded.utc_offset,
                updated_by = excluded.updated_by,
                updated_at = datetime('now')
            """,
            (store_id, utc_offset, username),
        )
        conn.commit()
    finally:
        conn.close()


# ============================================================================
# Предупреждения о перегрузе
# ============================================================================

def upsert_alert(store_id: int, date: str, hour: int, horizon: str,
                 percent: float, units: float, capacity: float) -> bool:
    """
    Записать предупреждение. False — про этот слот и горизонт уже говорили.

    Повторно об одном и том же не напоминаем: предупреждение, которое приходит
    каждые полчаса, перестают читать, и тогда молчит уже человек.
    """
    conn = get_db()
    try:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO salon_load_alerts
                   (store_id, date, hour, horizon, percent, units, capacity)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (store_id, date, hour, horizon, percent, units, capacity),
        )
        conn.commit()
        return bool(cur.rowcount)
    finally:
        conn.close()


def active_alerts(store_ids: Optional[List[int]], date_from: str) -> List[Dict[str, Any]]:
    """Неснятые предупреждения от указанной даты и дальше."""
    query = ("SELECT id, store_id, date, hour, horizon, percent, units, capacity, created_at "
             "FROM salon_load_alerts WHERE dismissed_at IS NULL AND resolved_at IS NULL "
             "AND date >= ?")
    params: List[Any] = [date_from]
    if store_ids is not None:
        if not store_ids:
            return []
        query += f" AND store_id IN ({','.join('?' * len(store_ids))})"
        params.extend(store_ids)
    query += " ORDER BY date, hour"

    conn = get_db()
    try:
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def open_alerts_for_scan(date_from: str) -> List[Dict[str, Any]]:
    """Предупреждения, по которым ещё не known, разгрузился слот или нет."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, store_id, date, hour, horizon, percent FROM salon_load_alerts "
            "WHERE resolved_at IS NULL AND date >= ?",
            (date_from,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def resolve_alert(alert_id: int, percent: Optional[float]) -> None:
    """Слот разгрузился — фиксируем факт: это единственная измеримая польза."""
    conn = get_db()
    try:
        conn.execute(
            "UPDATE salon_load_alerts SET resolved_at = datetime('now'), resolved_percent = ? "
            "WHERE id = ? AND resolved_at IS NULL",
            (percent, alert_id),
        )
        conn.commit()
    finally:
        conn.close()


def dismiss_alert(alert_id: int, store_ids: Optional[List[int]]) -> bool:
    """Снять предупреждение руками. Чужой салон снять нельзя."""
    conn = get_db()
    try:
        if store_ids is not None:
            if not store_ids:
                return False
            row = conn.execute("SELECT store_id FROM salon_load_alerts WHERE id = ?",
                               (alert_id,)).fetchone()
            if not row or row["store_id"] not in store_ids:
                return False
        cur = conn.execute(
            "UPDATE salon_load_alerts SET dismissed_at = datetime('now') "
            "WHERE id = ? AND dismissed_at IS NULL",
            (alert_id,),
        )
        conn.commit()
        return bool(cur.rowcount)
    finally:
        conn.close()


def alerts_stats(date_from: str, store_ids: Optional[List[int]] = None) -> Dict[str, Any]:
    """
    Сколько предупреждений было и сколько слотов после них разгрузилось.

    Это ответ на вопрос «работает ли модуль вообще». Если через месяц
    разгруженных ноль — предупреждения никто не читает, и это надо видеть
    цифрой, а не ощущением.
    """
    query = ("SELECT COUNT(*) AS total, "
             "SUM(CASE WHEN resolved_at IS NOT NULL THEN 1 ELSE 0 END) AS resolved, "
             "SUM(CASE WHEN dismissed_at IS NOT NULL THEN 1 ELSE 0 END) AS dismissed "
             "FROM salon_load_alerts WHERE date >= ?")
    params: List[Any] = [date_from]
    if store_ids is not None:
        if not store_ids:
            return {"total": 0, "resolved": 0, "dismissed": 0}
        query += f" AND store_id IN ({','.join('?' * len(store_ids))})"
        params.extend(store_ids)

    conn = get_db()
    try:
        row = conn.execute(query, params).fetchone()
    finally:
        conn.close()
    return {
        "total": row["total"] or 0,
        "resolved": row["resolved"] or 0,
        "dismissed": row["dismissed"] or 0,
    }


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
