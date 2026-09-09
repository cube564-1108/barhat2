"""
Ёмкость салонов: сколько работы салон успевает за час.

Ёмкость живёт в двух видах одновременно, и это временно (до Ф6 плана
«нагрузка в минутах»):

  - `capacity_units` — старая безразмерная модель «единиц в час». По ней
    сейчас считается сетка;
  - `florists` — число флористов в смене. Ёмкость в минутах = флористы × 60 ×
    `salon_settings.assembly_share`. По ней сетка начнёт считать после
    переключения модели.

Одно в другое НЕ конвертируется. «6 единиц» и «6 флористов» — разные вещи, и
молча превратить одно в другое значит соврать в цифре, по которой принимают
решения. Салон, у которого заполнено только старое поле, виден плашкой
«ёмкость задана в старых единицах — перезадайте» (`capacity_model_status`).

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

# Минут работы, которые даёт один флорист за час. Константа, а не настройка:
# час — это час. Всё, что делает флориста менее продуктивным (приём, выдача,
# звонки), живёт в `assembly_share` и вводится осознанно.
MINUTES_PER_FLORIST_HOUR = 60.0

# Доля часа, уходящая на сборку, когда салон её не задавал.
DEFAULT_ASSEMBLY_SHARE = 1.0

# Модель нагрузки. `orders` — старая безразмерная («заказ = 1 единица +
# надбавки»), `minutes` — минуты сборки против ёмкости в людях.
#
# Настройка, а не флаг в коде: откат обязан быть переключением, а не деплоем.
# Если после перехода цифры окажутся неправдоподобными, вернуться нужно за
# секунды, а не за сборку.
LOAD_MODEL_KEY = "load_model"
LOAD_MODEL_ORDERS = "orders"
LOAD_MODEL_MINUTES = "minutes"
LOAD_MODELS = (LOAD_MODEL_ORDERS, LOAD_MODEL_MINUTES)
DEFAULT_LOAD_MODEL = LOAD_MODEL_ORDERS


def get_db() -> sqlite3.Connection:
    """Соединение с общей базой. Настройки — в sqlite_conn (WAL один раз на файл)."""
    return sqlite_connect(DB_PATH, timeout=20)


def _add_column_if_missing(conn, table: str, column: str, ddl: str) -> None:
    """
    Идемпотентная миграция колонки.

    На проде 2 воркера gunicorn стартуют одновременно, оба видят «колонки нет» и
    оба выполняют ALTER. Оба штатных исхода гонки (duplicate column name,
    database is locked) означают, что колонку создаёт сосед, — цель достигнута,
    ронять старт воркера нельзя.
    """
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column in existing:
        return
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    except sqlite3.OperationalError as e:
        message = str(e).lower()
        if "duplicate column" not in message and "locked" not in message:
            raise
        logger.info(f"Миграция {table}.{column}: колонку создаёт другой воркер ({e})")


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

        # ====================================================================
        # Ф4 «нагрузка в минутах»: ёмкость задаётся числом флористов в смене,
        # а не абстрактными единицами. Минуты выводятся умножением на 60.
        #
        # REAL, а не INTEGER: полсмены, подмена, флорист на два салона — всё
        # это законные значения, и 0,5 флориста придётся вводить с первого дня.
        #
        # `capacity_units` остаётся рядом и НЕ конвертируется: «6 единиц» и
        # «6 флористов» — разные вещи, и молча превратить одно в другое значит
        # соврать. Пока модель не переключена (Ф6), сетка считает по старой
        # колонке, а плашка в интерфейсе просит перезадать.
        # ====================================================================
        _add_column_if_missing(conn, "salon_capacity", "florists", "REAL")
        _add_column_if_missing(conn, "salon_capacity_exceptions", "florists", "REAL")

        # Модель, в которой посчитано предупреждение. Числа в нём заморожены
        # в момент создания (INSERT OR IGNORE, строка не обновляется), поэтому
        # подписывать их единицей АКТИВНОЙ модели нельзя: после перехода на
        # минуты старое «7,4 из 6 ед.» превратилось бы в «7,4 из 6 мин»,
        # хотя минут там 74 из 60.
        _add_column_if_missing(conn, "salon_load_alerts", "model", "TEXT")

        # Доля часа, которая у флориста уходит именно на сборку: приём заказа,
        # выдача, звонки — это тоже его час. Один коэффициент на салон, а не
        # число на каждый час: последнее никто не заполнит.
        #
        # По умолчанию 1.0 и в интерфейс пока не выведен (К11 критики): этим
        # коэффициентом легко «починить» любое расхождение вместо того, чтобы
        # найти неверный тариф. Трогать — после сверки норм на живых заказах.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS salon_settings (
                store_id INTEGER PRIMARY KEY,
                assembly_share REAL NOT NULL DEFAULT 1.0,
                updated_by TEXT,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)

        # Настройки модуля, общие на всю сеть. Отдельная таблица от
        # `salon_settings`: там ключ — салон, здесь ключа нет вовсе.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS salonload_settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_by TEXT,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
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
                 percent: float, units: float, capacity: float,
                 model: Optional[str] = None) -> bool:
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
                   (store_id, date, hour, horizon, percent, units, capacity, model)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (store_id, date, hour, horizon, percent, units, capacity,
             model or DEFAULT_LOAD_MODEL),
        )
        conn.commit()
        return bool(cur.rowcount)
    finally:
        conn.close()


def active_alerts(store_ids: Optional[List[int]], date_from: str) -> List[Dict[str, Any]]:
    """Неснятые предупреждения от указанной даты и дальше."""
    query = ("SELECT id, store_id, date, hour, horizon, percent, units, capacity, "
             "       model, created_at "
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
            "SELECT weekday, hour, capacity_units, florists, pickup_capacity, is_closed "
            "FROM salon_capacity WHERE store_id = ?",
            (store_id,),
        ).fetchall()
    finally:
        conn.close()
    return {
        f"{row['weekday']}:{row['hour']}": {
            "capacity": row["capacity_units"],
            "florists": row["florists"],
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
            SELECT store_id, date, hour, capacity_units, florists, pickup_capacity,
                   is_closed, reason
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
            "florists": row["florists"],
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
            SELECT store_id, weekday, hour, capacity_units, florists, pickup_capacity, is_closed
              FROM salon_capacity WHERE store_id IN ({placeholders})
            """,
            tuple(store_ids),
        ).fetchall()
    finally:
        conn.close()
    return {
        f"{row['store_id']}:{row['weekday']}:{row['hour']}": {
            "capacity": row["capacity_units"],
            "florists": row["florists"],
            "pickup_capacity": row["pickup_capacity"],
            "closed": bool(row["is_closed"]),
        }
        for row in rows
    }


def stores_with_capacity() -> List[int]:
    """
    Салоны, у которых ёмкость вообще задана — для плашки «не задана».

    Задана в любой из двух моделей: салон, перезаданный в флористах, но ещё не
    имеющий старых единиц, — это заполненный салон, а не пустой.
    """
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT DISTINCT store_id FROM salon_capacity "
            " WHERE capacity_units IS NOT NULL OR florists IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()
    return [row["store_id"] for row in rows]


def capacity_model_status() -> Dict[int, Dict[str, int]]:
    """
    Сколько часов у каждого салона задано в старых единицах и сколько — в флористах.

    Отсюда берётся плашка «ёмкость задана в старых единицах — перезадайте».
    Молчаливой конвертации не будет: 9 салонов × одно значение — это пять минут
    работы человека, а неверная конвертация живёт месяцами и портит все цифры,
    которые на ней стоят.
    """
    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT store_id,
                   SUM(CASE WHEN capacity_units IS NOT NULL AND is_closed = 0
                            THEN 1 ELSE 0 END) AS units_hours,
                   SUM(CASE WHEN florists IS NOT NULL AND is_closed = 0
                            THEN 1 ELSE 0 END) AS florist_hours,
                   SUM(CASE WHEN capacity_units IS NULL AND is_closed = 0
                            THEN 1 ELSE 0 END) AS gap_hours,
                   SUM(CASE WHEN florists IS NULL AND is_closed = 0
                            THEN 1 ELSE 0 END) AS florist_gap_hours,
                   SUM(CASE WHEN is_closed = 0 THEN 1 ELSE 0 END) AS open_hours
              FROM salon_capacity
          GROUP BY store_id
            """
        ).fetchall()
    finally:
        conn.close()
    return {
        row["store_id"]: {
            "units_hours": row["units_hours"] or 0,
            "florist_hours": row["florist_hours"] or 0,
            "open_hours": row["open_hours"] or 0,
            # Рабочие часы БЕЗ старой ёмкости. До перехода процент считается по
            # ней, и такой час в сетке серый — «ёмкость не задана». Появляется
            # это само: у нового салона или когда расширили часы работы, а форма
            # шлёт только флористов. Молча — значит незаметно.
            "gap_hours": row["gap_hours"] or 0,
            # То же самое для новой модели. Салон, у которого флористы стоят
            # на одном часе из двенадцати, «готовым» не является: после
            # переключения одиннадцать часов станут серыми.
            "florist_gap_hours": row["florist_gap_hours"] or 0,
        }
        for row in rows
    }


def assembly_share_map() -> Dict[int, float]:
    """{store_id: доля часа на сборку}. Салона нет в таблице — значит 1.0."""
    conn = get_db()
    try:
        rows = conn.execute("SELECT store_id, assembly_share FROM salon_settings").fetchall()
    finally:
        conn.close()
    return {row["store_id"]: row["assembly_share"] for row in rows}


def set_assembly_share(store_id: int, share: float, username: Optional[str] = None) -> None:
    """
    Доля часа, уходящая на сборку.

    В интерфейс пока не выведена намеренно (К11 критики плана): этим
    коэффициентом можно «починить» любое расхождение вместо того, чтобы найти
    неверный тариф. Сначала нормы сверяются на живых заказах, и только потом
    появляется поле.
    """
    if not 0 < share <= 1:
        raise ValueError("Доля времени на сборку должна быть больше 0 и не больше 1")
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO salon_settings (store_id, assembly_share, updated_by, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(store_id) DO UPDATE SET
                assembly_share = excluded.assembly_share,
                updated_by = excluded.updated_by,
                updated_at = datetime('now')
            """,
            (store_id, float(share), username),
        )
        conn.commit()
    finally:
        conn.close()


def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    conn = get_db()
    try:
        row = conn.execute("SELECT value FROM salonload_settings WHERE key = ?",
                           (key,)).fetchone()
    finally:
        conn.close()
    return default if row is None or row["value"] is None else row["value"]


def set_setting(key: str, value: str, username: Optional[str] = None) -> None:
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO salonload_settings (key, value, updated_by, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_by = excluded.updated_by,
                updated_at = datetime('now')
            """,
            (key, value, username),
        )
        conn.commit()
    finally:
        conn.close()


def get_load_model() -> str:
    """
    Активная модель нагрузки.

    Неизвестное значение в базе — это старая модель, а не падение: настройку
    правит человек, и опечатка не должна гасить экран всей сети.
    """
    value = get_setting(LOAD_MODEL_KEY, DEFAULT_LOAD_MODEL)
    return value if value in LOAD_MODELS else DEFAULT_LOAD_MODEL


def set_load_model(value: str, username: Optional[str] = None) -> None:
    if value not in LOAD_MODELS:
        raise ValueError(f"Модель нагрузки может быть {' или '.join(LOAD_MODELS)}")
    set_setting(LOAD_MODEL_KEY, value, username)


def model_health() -> Dict[str, Any]:
    """
    Состояние модели нагрузки для `/health?full=1` — ОДНИМ соединением.

    Снаружи иначе не проверить ни то, что миграция колонки прошла, ни то,
    какая модель сейчас активна: экран показывает проценты, а из чего они
    сложились — не показывает. Консоли у контейнера нет.

    Одно соединение, а не три вызова подряд: цену диагностики определяет число
    открытых соединений к общему сетевому диску, а не размер таблиц.
    """
    conn = get_db()
    try:
        row = conn.execute("SELECT value FROM salonload_settings WHERE key = ?",
                           (LOAD_MODEL_KEY,)).fetchone()
        model = row["value"] if row and row["value"] in LOAD_MODELS else DEFAULT_LOAD_MODEL
        counts = conn.execute(
            """
            SELECT COUNT(DISTINCT store_id) AS stores,
                   COUNT(DISTINCT CASE WHEN florists IS NOT NULL AND is_closed = 0
                                       THEN store_id END) AS stores_with_florists,
                   SUM(CASE WHEN capacity_units IS NULL AND is_closed = 0
                            THEN 1 ELSE 0 END) AS gap_hours,
                   SUM(CASE WHEN florists IS NULL AND is_closed = 0
                            THEN 1 ELSE 0 END) AS florist_gap_hours
              FROM salon_capacity
            """
        ).fetchone()
    finally:
        conn.close()
    return {
        "model": model,
        "stores": counts["stores"] or 0,
        "stores_with_florists": counts["stores_with_florists"] or 0,
        # Пробелы обеих моделей, а не только старой: дырой активной модели
        # оказывается серая сетка, и увидеть её снаружи больше нечем.
        "hours_without_old_capacity": counts["gap_hours"] or 0,
        "hours_without_florists": counts["florist_gap_hours"] or 0,
    }


def capacity_minutes(florists: Optional[float], assembly_share: Optional[float]) -> Optional[float]:
    """
    Ёмкость слота в минутах сборки. None — ёмкость не задана.

    Одно место на весь модуль: формула «флористы × 60 × доля» будет нужна и
    сетке, и подсказке, и предупреждениям, а разъехавшиеся копии одной формулы
    дают разные числа на соседних экранах.
    """
    if florists is None:
        return None
    share = DEFAULT_ASSEMBLY_SHARE if assembly_share is None else assembly_share
    return round(florists * MINUTES_PER_FLORIST_HOUR * share, 2)


# ============================================================================
# Запись
# ============================================================================

def set_slots(store_id: int, slots: List[Dict[str, Any]], username: Optional[str] = None) -> int:
    """
    Проставить ячейки недельной сетки пачкой.

    slot: {"weekday": 0-6, "hour": 0-23, "capacity": число|None,
           "florists": число|None, "pickup_capacity": число|None, "closed": bool}

    Всё пусто и closed=False — это «ёмкость не задана»: строка удаляется,
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
            florists = slot.get("florists")
            pickup = slot.get("pickup_capacity")
            closed = bool(slot.get("closed"))

            if capacity is None and florists is None and pickup is None and not closed:
                conn.execute(
                    "DELETE FROM salon_capacity WHERE store_id = ? AND weekday = ? AND hour = ?",
                    (store_id, weekday, hour),
                )
                continue

            if capacity is not None and float(capacity) < 0:
                raise ValueError("Ёмкость не может быть отрицательной")
            if florists is not None and float(florists) < 0:
                raise ValueError("Число флористов не может быть отрицательным")

            conn.execute(
                """
                INSERT INTO salon_capacity
                       (store_id, weekday, hour, capacity_units, florists, pickup_capacity,
                        is_closed, updated_by, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(store_id, weekday, hour) DO UPDATE SET
                    capacity_units = excluded.capacity_units,
                    florists = excluded.florists,
                    pickup_capacity = excluded.pickup_capacity,
                    is_closed = excluded.is_closed,
                    updated_by = excluded.updated_by,
                    updated_at = datetime('now')
                """,
                (store_id, weekday, hour,
                 None if capacity is None else float(capacity),
                 None if florists is None else float(florists),
                 None if pickup is None else float(pickup),
                 1 if closed else 0, username),
            )
        conn.commit()
        return len(slots)
    finally:
        conn.close()


def apply_working_hours(store_id: int, open_hour: int, close_hour: int,
                        capacity: Optional[float] = None,
                        pickup_capacity: Optional[float] = None,
                        weekdays: Optional[List[int]] = None,
                        username: Optional[str] = None,
                        florists: Optional[float] = None) -> int:
    """
    Заполнить сетку одним движением: часы работы + ёмкость в час.

    Без этого Фаза 4 — это 1512 полей руками на девять салонов, и она просто
    не будет заполнена, а модуль покажет пустоту. Часы вне окна помечаются
    закрытыми — это не нулевая загрузка, а отсутствие работы.

    Три режима, и все три реальны:
      - обычный: 9 → 21;
      - круглосуточный: 0 → 24 (в сети есть такие точки);
      - ночной, через полночь: 22 → 6. Здесь `close_hour <= open_hour`, и
        раньше такой график просто нельзя было задать — окно считалось
        «заданным неверно», а салон оставался с пустой сеткой.

    Заполнять можно числом флористов (новая модель), старыми единицами в час
    или обоими сразу. **Та модель, которую не передали, не затирается**: пока
    сетка считает по `capacity_units`, ввод флористов не имеет права обнулить
    работающий экран, а после переключения — наоборот. Прежние значения
    берутся одним чтением недельной сетки, а не запросом на каждый из 168 часов.
    """
    if not 0 <= open_hour <= 23:
        raise ValueError("Час открытия должен быть от 0 до 23")
    if not 1 <= close_hour <= 24:
        raise ValueError("Час закрытия должен быть от 1 до 24")
    if open_hour == close_hour:
        raise ValueError("Открытие и закрытие совпадают. "
                         "Для круглосуточного режима задайте 0 и 24")
    if capacity is None and florists is None:
        raise ValueError("Укажите число флористов в смене")
    if capacity is not None and capacity <= 0:
        raise ValueError("Ёмкость должна быть больше нуля")
    if florists is not None and florists <= 0:
        raise ValueError("Число флористов должно быть больше нуля")

    def is_working(hour: int) -> bool:
        if open_hour < close_hour:
            return open_hour <= hour < close_hour
        # Через полночь: рабочие часы — хвост суток и начало следующих.
        return hour >= open_hour or hour < close_hour

    previous = weekly_grid(store_id)
    days = weekdays if weekdays is not None else list(WEEKDAYS)
    slots = []
    for weekday in days:
        for hour in HOURS:
            working = is_working(hour)
            before = previous.get(f"{weekday}:{hour}") or {}
            slots.append({
                "weekday": weekday,
                "hour": hour,
                "capacity": (capacity if capacity is not None
                             else before.get("capacity")) if working else None,
                "florists": (florists if florists is not None
                             else before.get("florists")) if working else None,
                "pickup_capacity": pickup_capacity if working else None,
                "closed": not working,
            })
    return set_slots(store_id, slots, username)


def copy_week(source_store_id: int, target_store_id: int, username: Optional[str] = None) -> int:
    """
    Скопировать график другого салона — второй способ не заполнять 168 полей.

    Копия ЗАМЕЩАЕТ, а не дополняет: иначе часы, которых нет у источника,
    остались бы от прежнего графика приёмника, а интерфейс сказал бы «график
    скопирован» — и человек считал бы, что видит копию.
    """
    source = weekly_grid(source_store_id)
    if not source:
        return 0

    slots = []
    for weekday in WEEKDAYS:
        for hour in HOURS:
            value = source.get(f"{weekday}:{hour}")
            slots.append({
                "weekday": weekday,
                "hour": hour,
                "capacity": value["capacity"] if value else None,
                "florists": value["florists"] if value else None,
                "pickup_capacity": value["pickup_capacity"] if value else None,
                "closed": value["closed"] if value else False,
            })
    set_slots(target_store_id, slots, username)
    return len(source)


def set_exception(store_id: int, date: str, hour: Optional[int], capacity: Optional[float],
                  pickup_capacity: Optional[float] = None, closed: bool = False,
                  reason: Optional[str] = None, username: Optional[str] = None,
                  florists: Optional[float] = None) -> int:
    """
    Исключение на дату. hour=None — на весь день (все 24 часа).

    Всё пусто и closed=False снимает исключение: день возвращается к обычному
    графику.

    Флористы задаются здесь так же, как в недельном графике: 14 февраля в смене
    выходит не столько же людей, сколько во вторник, и без этого поля праздник
    после переключения модели (Ф6) остался бы с обычной ёмкостью.
    """
    hours = HOURS if hour is None else (int(hour),)
    conn = get_db()
    try:
        for h in hours:
            if capacity is None and florists is None and pickup_capacity is None and not closed:
                conn.execute(
                    "DELETE FROM salon_capacity_exceptions "
                    "WHERE store_id = ? AND date = ? AND hour = ?",
                    (store_id, date, h),
                )
                continue
            conn.execute(
                """
                INSERT INTO salon_capacity_exceptions
                       (store_id, date, hour, capacity_units, florists, pickup_capacity,
                        is_closed, reason, updated_by, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(store_id, date, hour) DO UPDATE SET
                    capacity_units = excluded.capacity_units,
                    florists = excluded.florists,
                    pickup_capacity = excluded.pickup_capacity,
                    is_closed = excluded.is_closed,
                    reason = excluded.reason,
                    updated_by = excluded.updated_by,
                    updated_at = datetime('now')
                """,
                (store_id, date, h,
                 None if capacity is None else float(capacity),
                 None if florists is None else float(florists),
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
                   MAX(capacity_units) AS max_capacity,
                   MIN(florists) AS min_florists,
                   MAX(florists) AS max_florists
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
            "min_florists": row["min_florists"],
            "max_florists": row["max_florists"],
        }
        for row in rows
    ]
