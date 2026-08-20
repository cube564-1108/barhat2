"""
Модуль работы с SQLite для кассовых смен БАРХАТ.

Создаёт таблицы, заполняет seed-данными, предоставляет функции доступа.
"""

import os
import sqlite3
from datetime import datetime
from typing import Optional, List, Dict, Any

from .seed_data import (
    STORES,
    EXPENSE_CATEGORIES,
    get_insert_stores_sql,
    get_insert_categories_sql,
)

# Путь к БД из переменной окружения или дефолт
DB_PATH = os.environ.get("BARHAT_DB_PATH", "barhat.db")


def get_db():
    """Получить соединение с БД."""
    # timeout увеличен против дефолтных 5с — 2 воркера gunicorn (amvera.yml)
    # пишут в один файл SQLite параллельно, см. auth.get_db()
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    # WAL — читатели не блокируют писателя и наоборот, см. auth.get_db()
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=20000")
    return conn


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, ddl: str):
    """
    Добавить колонку, если её ещё нет (идемпотентная миграция).

    На проде 2 воркера gunicorn стартуют одновременно и оба выполняют
    init_cashshifts_tables(). Оба исхода гонки за один и тот же ALTER штатные,
    и ни один не должен ронять старт воркера:

      duplicate column name — второй воркер успел закоммитить ALTER раньше;
      database is locked    — второй воркер держит write-лок прямо сейчас.

    В обоих случаях колонку создаёт сосед, цель достигнута. Раньше второй
    случай пробрасывался наружу: исключение вылетало из init_cashshifts_tables
    до conn.close(), соединение утекало открытым на всю жизнь воркера и
    блокировало запись в общую БД — вход висел на INSERT в audit_log, потому
    что чтение при этом работало нормально. Продержалось до первого рестарта.
    """
    cursor = conn.execute(f"PRAGMA table_info({table})")
    existing = [row[1] for row in cursor.fetchall()]
    if column in existing:
        return

    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        conn.commit()
    except sqlite3.OperationalError as e:
        message = str(e).lower()
        if "duplicate column name" in message or "locked" in message:
            print(f"[CashShifts] Миграцию {table}.{column} выполняет параллельный воркер: {e}")
            return
        raise


def init_cashshifts_tables():
    """Инициализация таблиц кассовых смен (вызывается при старте приложения).

    Соединение закрывается через try/finally: без него любое исключение внутри
    (гонка миграций между воркерами, битая схема) оставляло бы соединение
    открытым до перезапуска воркера. Открытое соединение к общей БД мешает
    записи — а на этой же БД живут пользователи и audit_log, поэтому ценой
    падения здесь становится неработающий вход во весь дашборд.
    """

    conn = get_db()
    try:
        _init_cashshifts_schema(conn)
    finally:
        conn.close()


def _init_cashshifts_schema(conn: sqlite3.Connection):
    """Создание таблиц, индексов и seed-данных. Соединением владеет вызывающий."""

    # ========================================================================
    # 1. Точки продаж (stores)
    # ========================================================================
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    # Индекс для быстрого поиска активных точек
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_stores_active
        ON stores(is_active) WHERE is_active = 1
    """)

    # ========================================================================
    # 2. Статьи расхода (expense_categories)
    # ========================================================================
    conn.execute("""
        CREATE TABLE IF NOT EXISTS expense_categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    # Индекс для быстрого поиска активных категорий
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_categories_active
        ON expense_categories(is_active) WHERE is_active = 1
    """)

    # ========================================================================
    # 3. Кассовые смены (cash_shifts)
    # ========================================================================
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cash_shifts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store_id INTEGER NOT NULL REFERENCES stores(id),
            shift_type TEXT NOT NULL CHECK (shift_type IN ('day', 'night')),
            datetime_start TEXT NOT NULL,
            datetime_end TEXT,
            florist_id INTEGER,
            florist_username TEXT,
            opening_balance REAL NOT NULL DEFAULT 0,
            cash_orders_total REAL,
            collections_total REAL,
            expected_balance REAL,
            actual_balance REAL,
            discrepancy REAL,
            status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            closed_at TEXT
        )
    """)

    # Миграции колонок cash_shifts:
    #   closed_by_username — кто закрыл смену (для журнала смены)
    #   cash_orders_synced_at — когда cash_orders_total последний раз обновляли
    #       из RetailCRM. Нужно для таблицы «Открытые смены»: по этой метке
    #       решаем, дёргать CRM живьём или отдать закэшированное значение.
    _add_column_if_missing(conn, "cash_shifts", "closed_by_username", "TEXT")
    _add_column_if_missing(conn, "cash_shifts", "cash_orders_synced_at", "TEXT")

    # Индексы для фильтров и поиска
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_shifts_store
        ON cash_shifts(store_id)
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_shifts_status
        ON cash_shifts(status)
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_shifts_datetime
        ON cash_shifts(datetime_start)
    """)

    # Композитный индекс для истории смен по точке и дате
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_shifts_store_datetime
        ON cash_shifts(store_id, datetime_start DESC)
    """)

    # ========================================================================
    # 4. Инкассации (cash_collections)
    # ========================================================================
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cash_collections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            shift_id INTEGER NOT NULL REFERENCES cash_shifts(id) ON DELETE CASCADE,
            date TEXT NOT NULL,
            amount REAL NOT NULL,
            expense_category_id INTEGER NOT NULL REFERENCES expense_categories(id),
            custom_comment TEXT,
            created_by TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    # Индекс для быстрого поиска инкассаций по смене
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_collections_shift
        ON cash_collections(shift_id)
    """)

    # ========================================================================
    # 5. Кэш заказов из CRM (cash_orders_cache)
    # ========================================================================
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cash_orders_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            shift_id INTEGER NOT NULL REFERENCES cash_shifts(id) ON DELETE CASCADE,
            retailcrm_order_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            paid_at TEXT NOT NULL,
            order_data TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    # Индекс для поиска по смене и ID заказа
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_orders_cache_shift
        ON cash_orders_cache(shift_id)
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_orders_cache_order_id
        ON cash_orders_cache(retailcrm_order_id)
    """)

    # ========================================================================
    # 6. Связь пользователей с точками (user_stores)
    # ========================================================================
    conn.execute("PRAGMA foreign_keys = ON")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_stores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            store_id INTEGER NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
            UNIQUE(username, store_id)
        )
    """)

    # Индекс для быстрого поиска точек пользователя
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_user_stores_username
        ON user_stores(username)
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_user_stores_store
        ON user_stores(store_id)
    """)

    conn.commit()

    # ========================================================================
    # ЗАПОЛНЕНИЕ SEED-ДАННЫМИ (однократно)
    # ========================================================================
    _seed_stores_if_empty(conn)
    _seed_categories_if_empty(conn)


def _seed_stores_if_empty(conn: sqlite3.Connection):
    """Заполнить таблицу stores если она пуста."""

    result = conn.execute("SELECT COUNT(*) as count FROM stores").fetchone()

    if result["count"] == 0:
        print("[CashShifts] Заполнение таблицы stores seed-данными...")

        for store in STORES:
            conn.execute(
                "INSERT INTO stores (name, is_active) VALUES (?, 1)",
                (store["name"],)
            )

        conn.commit()
        print(f"[CashShifts] Добавлено {len(STORES)} точек продаж")

    else:
        print(f"[CashShifts] Таблица stores уже содержит {result['count']} записей")


def _seed_categories_if_empty(conn: sqlite3.Connection):
    """Заполнить таблицу expense_categories если она пуста."""

    result = conn.execute("SELECT COUNT(*) as count FROM expense_categories").fetchone()

    if result["count"] == 0:
        print("[CashShifts] Заполнение таблицы expense_categories seed-данными...")

        for cat in EXPENSE_CATEGORIES:
            conn.execute(
                "INSERT INTO expense_categories (name, is_active) VALUES (?, 1)",
                (cat["name"],)
            )

        conn.commit()
        print(f"[CashShifts] Добавлено {len(EXPENSE_CATEGORIES)} категорий расходов")

    else:
        print(f"[CashShifts] Таблица expense_categories уже содержит {result['count']} записей")


# =============================================================================
# ФУНКЦИИ ДЛЯ РАБОТЫ С ДАННЫМИ
# =============================================================================

def get_all_stores() -> List[Dict[str, Any]]:
    """Получить список всех активных точек продаж."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM stores WHERE is_active = 1 ORDER BY name"
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_store_by_id(store_id: int) -> Optional[Dict[str, Any]]:
    """Получить точку по ID."""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM stores WHERE id = ? AND is_active = 1",
        (store_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def create_store(name: str) -> int:
    """
    Создать точку продаж (салон/офис). Возвращает ID.

    Реактивирует ранее деактивированную запись с тем же именем вместо
    INSERT — UNIQUE(name) действует на всю таблицу, а не только на активные
    строки, иначе повторно завести ранее удалённое название невозможно
    (тот же баг был найден и исправлен в invoices.storage._ref_create).

    Соединение закрывается через try/finally: имя-дубликат активной точки
    роняет INSERT на UNIQUE(name), а незакрытое соединение с неоткатанной
    транзакцией держит write-лок общей БД до перезапуска воркера — после
    этого висит и вход в дашборд, и любая запись (см. init_cashshifts_tables).
    """
    conn = get_db()
    try:
        existing = conn.execute("SELECT id, is_active FROM stores WHERE name = ?", (name,)).fetchone()
        if existing and not existing["is_active"]:
            conn.execute("UPDATE stores SET is_active = 1 WHERE id = ?", (existing["id"],))
            conn.commit()
            return existing["id"]

        cursor = conn.execute("INSERT INTO stores (name, is_active) VALUES (?, 1)", (name,))
        store_id = cursor.lastrowid
        conn.commit()
        return store_id
    finally:
        conn.close()


def update_store(store_id: int, name: str) -> bool:
    """Переименовать точку продаж. Та же ловушка с "мёртвым" именем, что и в create_store."""
    conn = get_db()
    try:
        ghost = conn.execute(
            "SELECT id FROM stores WHERE name = ? AND id != ? AND is_active = 0",
            (name, store_id)
        ).fetchone()
        if ghost:
            conn.execute("DELETE FROM stores WHERE id = ?", (ghost["id"],))

        conn.execute("UPDATE stores SET name = ? WHERE id = ?", (name, store_id))
        conn.commit()
        return True
    finally:
        conn.close()


def delete_store(store_id: int) -> bool:
    """Деактивировать точку продаж (мягкое удаление, is_active = 0)."""
    conn = get_db()
    try:
        conn.execute("UPDATE stores SET is_active = 0 WHERE id = ?", (store_id,))
        conn.commit()
        return True
    finally:
        conn.close()


def get_all_categories() -> List[Dict[str, Any]]:
    """Получить список всех активных категорий расходов."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM expense_categories WHERE is_active = 1 ORDER BY name"
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_categories_with_usage() -> List[Dict[str, Any]]:
    """
    Активные категории + сколько инкассаций на каждой (для справочника).

    Считаем отдельным запросом, а не в get_all_categories: тот дёргается при
    каждой загрузке страницы ради datalist, а счётчик нужен только в модалке
    справочника — там админ решает, можно ли категорию удалять.
    """
    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT ec.*, COUNT(cc.id) as usage_count
            FROM expense_categories ec
            LEFT JOIN cash_collections cc ON cc.expense_category_id = ec.id
            WHERE ec.is_active = 1
            GROUP BY ec.id
            ORDER BY ec.name
        """).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_category_by_id(category_id: int) -> Optional[Dict[str, Any]]:
    """Получить категорию по ID."""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM expense_categories WHERE id = ? AND is_active = 1",
        (category_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_user_stores(username: str) -> List[int]:
    """Получить список ID точек пользователя."""
    conn = get_db()
    rows = conn.execute(
        "SELECT store_id FROM user_stores WHERE username = ?",
        (username,)
    ).fetchall()
    conn.close()
    return [row["store_id"] for row in rows]


def check_store_access(username: str, store_id: int, user_role: str) -> bool:
    """
    Проверить доступ пользователя к точке.

    Args:
        username: Имя пользователя
        store_id: ID точки
        user_role: Роль пользователя

    Returns:
        True если есть доступ, False если нет
    """
    # Админ имеет доступ ко всем точкам
    if user_role == "admin":
        return True

    # Проверяем привязку к точке
    user_store_ids = get_user_stores(username)
    return store_id in user_store_ids


def get_users_full_names(usernames: List[str]) -> Dict[str, str]:
    """
    Получить ФИО пользователей по списку username (для журнала смены).

    Таблица users принадлежит модулю авторизации (auth.py), но живёт в той
    же БД (BARHAT_DB_PATH), поэтому читаем её напрямую без импорта auth.
    """
    if not usernames:
        return {}

    conn = get_db()
    try:
        placeholders = ",".join("?" * len(usernames))
        rows = conn.execute(
            f"SELECT username, full_name FROM users WHERE username IN ({placeholders})",
            usernames
        ).fetchall()
        return {row["username"]: row["full_name"] for row in rows}
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()


# =============================================================================
# ОРМ-ПОДОБНЫЕ ФУНКЦИИ ДЛЯ КАССОВЫХ СМЕН
# =============================================================================

def create_cash_shift(
    store_id: int,
    shift_type: str,
    datetime_start: str,
    opening_balance: float,
    florist_username: Optional[str] = None
) -> int:
    """Создать новую кассовую смену. Возвращает ID созданной смены."""
    conn = get_db()

    cursor = conn.execute(
        """
        INSERT INTO cash_shifts (
            store_id, shift_type, datetime_start,
            opening_balance, florist_username, status
        ) VALUES (?, ?, ?, ?, ?, 'open')
        """,
        (store_id, shift_type, datetime_start, opening_balance, florist_username)
    )

    shift_id = cursor.lastrowid
    conn.commit()
    conn.close()

    return shift_id


def get_open_shift(store_id: int) -> Optional[Dict[str, Any]]:
    """Получить открытую смену для точки."""
    conn = get_db()
    row = conn.execute(
        """
        SELECT * FROM cash_shifts
        WHERE store_id = ? AND status = 'open'
        ORDER BY datetime_start DESC
        LIMIT 1
        """,
        (store_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_open_shifts(store_ids: Optional[List[int]] = None) -> List[Dict[str, Any]]:
    """
    Получить все открытые смены (для сводной таблицы «Открытые смены»).

    Args:
        store_ids: если задан — только по этим точкам (для manager); None — по всем (admin)
    """
    conn = get_db()

    query = """
        SELECT cs.*, s.name as store_name
        FROM cash_shifts cs
        JOIN stores s ON s.id = cs.store_id
        WHERE cs.status = 'open'
    """
    params: List[Any] = []

    if store_ids is not None:
        if not store_ids:
            conn.close()
            return []
        placeholders = ", ".join("?" for _ in store_ids)
        query += f" AND cs.store_id IN ({placeholders})"
        params.extend(store_ids)

    query += " ORDER BY cs.datetime_start ASC"

    rows = conn.execute(query, params).fetchall()
    conn.close()

    return [dict(row) for row in rows]


def get_last_closed_shift(store_id: int) -> Optional[Dict[str, Any]]:
    """Получить последнюю закрытую смену для точки (для opening_balance)."""
    conn = get_db()
    row = conn.execute(
        """
        SELECT * FROM cash_shifts
        WHERE store_id = ? AND status = 'closed'
        ORDER BY datetime_end DESC
        LIMIT 1
        """,
        (store_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_cash_shift_by_id(shift_id: int) -> Optional[Dict[str, Any]]:
    """Получить смену по ID."""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM cash_shifts WHERE id = ?",
        (shift_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def list_cash_shifts(
    store_id: Optional[int] = None,
    status: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 100,
    offset: int = 0
) -> List[Dict[str, Any]]:
    """Получить список смен с фильтрами."""

    query = "SELECT * FROM cash_shifts WHERE 1=1"
    params = []

    if store_id:
        query += " AND store_id = ?"
        params.append(store_id)

    if status:
        query += " AND status = ?"
        params.append(status)

    if date_from:
        query += " AND datetime_start >= ?"
        params.append(date_from)

    if date_to:
        query += " AND datetime_start <= ?"
        params.append(date_to)

    query += " ORDER BY datetime_start DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    conn = get_db()
    rows = conn.execute(query, params).fetchall()
    conn.close()

    return [dict(row) for row in rows]


def delete_cash_shift(shift_id: int) -> bool:
    """
    Удалить смену вместе с её инкассациями и кэшем заказов из CRM.

    Дочерние строки удаляем явно: ON DELETE CASCADE в схеме есть, но
    PRAGMA foreign_keys включается не в каждом соединении, и без явного
    удаления в БД остались бы висячие инкассации и кэш заказов.
    Всё в одном соединении и одной транзакции — иначе при обрыве между
    запросами смена исчезнет, а её инкассации останутся.
    """
    conn = get_db()
    try:
        conn.execute("DELETE FROM cash_collections WHERE shift_id = ?", (shift_id,))
        conn.execute("DELETE FROM cash_orders_cache WHERE shift_id = ?", (shift_id,))
        cursor = conn.execute("DELETE FROM cash_shifts WHERE id = ?", (shift_id,))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def update_cash_shift(
    shift_id: int,
    opening_balance: Optional[float] = None,
    actual_balance: Optional[float] = None,
    cash_orders_total: Optional[float] = None,
    collections_total: Optional[float] = None,
    expected_balance: Optional[float] = None,
    discrepancy: Optional[float] = None,
    status: Optional[str] = None,
    datetime_end: Optional[str] = None,
    closed_at: Optional[str] = None,
    closed_by_username: Optional[str] = None,
    cash_orders_synced_at: Optional[str] = None
) -> bool:
    """Обновить поля смены."""

    updates = []
    params = []

    if opening_balance is not None:
        updates.append("opening_balance = ?")
        params.append(opening_balance)

    if actual_balance is not None:
        updates.append("actual_balance = ?")
        params.append(actual_balance)

    if cash_orders_total is not None:
        updates.append("cash_orders_total = ?")
        params.append(cash_orders_total)

    if collections_total is not None:
        updates.append("collections_total = ?")
        params.append(collections_total)

    if expected_balance is not None:
        updates.append("expected_balance = ?")
        params.append(expected_balance)

    if discrepancy is not None:
        updates.append("discrepancy = ?")
        params.append(discrepancy)

    if status is not None:
        updates.append("status = ?")
        params.append(status)

    if datetime_end is not None:
        updates.append("datetime_end = ?")
        params.append(datetime_end)

    if closed_at is not None:
        updates.append("closed_at = ?")
        params.append(closed_at)

    if closed_by_username is not None:
        updates.append("closed_by_username = ?")
        params.append(closed_by_username)

    if cash_orders_synced_at is not None:
        updates.append("cash_orders_synced_at = ?")
        params.append(cash_orders_synced_at)

    if not updates:
        return True

    params.append(shift_id)

    conn = get_db()
    conn.execute(
        f"UPDATE cash_shifts SET {', '.join(updates)} WHERE id = ?",
        params
    )
    conn.commit()
    conn.close()

    return True


# =============================================================================
# ИНКАССАЦИИ
# =============================================================================

def create_collection(
    shift_id: int,
    amount: float,
    expense_category_id: int,
    date: str,
    custom_comment: Optional[str] = None,
    created_by: Optional[str] = None
) -> int:
    """Создать инкассацию. Возвращает ID."""
    conn = get_db()

    cursor = conn.execute(
        """
        INSERT INTO cash_collections (
            shift_id, amount, expense_category_id, date,
            custom_comment, created_by
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (shift_id, amount, expense_category_id, date, custom_comment, created_by)
    )

    collection_id = cursor.lastrowid
    conn.commit()
    conn.close()

    return collection_id


def get_shift_collections(shift_id: int) -> List[Dict[str, Any]]:
    """Получить все инкассации смены."""
    conn = get_db()
    rows = conn.execute(
        """
        SELECT cc.*, ec.name as category_name
        FROM cash_collections cc
        LEFT JOIN expense_categories ec ON cc.expense_category_id = ec.id
        WHERE cc.shift_id = ?
        ORDER BY cc.date
        """,
        (shift_id,)
    ).fetchall()
    conn.close()

    return [dict(row) for row in rows]


def get_collections_total(shift_id: int) -> float:
    """Получить сумму инкассаций смены."""
    conn = get_db()
    result = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) as total FROM cash_collections WHERE shift_id = ?",
        (shift_id,)
    ).fetchone()
    conn.close()

    return result["total"] if result else 0.0


def update_collection(collection_id: int, amount: float) -> bool:
    """Обновить сумму инкассации."""
    conn = get_db()
    conn.execute(
        "UPDATE cash_collections SET amount = ? WHERE id = ?",
        (amount, collection_id)
    )
    conn.commit()
    conn.close()

    return True


# =============================================================================
# КЭШ ЗАКАЗОВ
# =============================================================================

def cache_cash_orders(
    shift_id: int,
    orders: List[Dict[str, Any]]
) -> None:
    """
    Сохранить информацию о наличных заказах в кэш.

    Args:
        shift_id: ID смены
        orders: Список словарей {retailcrm_order_id, amount, paid_at, order_data?}
    """
    conn = get_db()

    for order in orders:
        conn.execute(
            """
            INSERT INTO cash_orders_cache (
                shift_id, retailcrm_order_id, amount, paid_at, order_data
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                shift_id,
                order["retailcrm_order_id"],
                order["amount"],
                order["paid_at"],
                order.get("order_data")
            )
        )

    conn.commit()
    conn.close()


def get_shift_cash_orders(shift_id: int) -> List[Dict[str, Any]]:
    """Получить кэшированные наличные заказы смены."""
    conn = get_db()
    rows = conn.execute(
        """
        SELECT * FROM cash_orders_cache
        WHERE shift_id = ?
        ORDER BY paid_at
        """,
        (shift_id,)
    ).fetchall()
    conn.close()

    return [dict(row) for row in rows]


def clear_shift_cache(shift_id: int) -> None:
    """Очистить кэш заказов смены."""
    conn = get_db()
    conn.execute(
        "DELETE FROM cash_orders_cache WHERE shift_id = ?",
        (shift_id,)
    )
    conn.commit()
    conn.close()


# =============================================================================
# УПРАВЛЕНИЕ ДОСТУПОМ ПОЛЬЗОВАТЕЛЕЙ К ТОЧКАМ
# =============================================================================

def set_user_stores(username: str, store_ids: List[int]) -> None:
    """
    Установить привязку пользователя к точкам.

    Заменяет существующие связи на новые.
    """
    conn = get_db()

    # Удаляем старые связи
    conn.execute(
        "DELETE FROM user_stores WHERE username = ?",
        (username,)
    )

    # Добавляем новые
    for store_id in store_ids:
        conn.execute(
            "INSERT INTO user_stores (username, store_id) VALUES (?, ?)",
            (username, store_id)
        )

    conn.commit()
    conn.close()


def get_user_stores_with_details(username: str) -> List[Dict[str, Any]]:
    """
    Получить точки пользователя с деталями (названия и т.д.).
    """
    conn = get_db()
    rows = conn.execute(
        """
        SELECT s.* FROM stores s
        JOIN user_stores us ON s.id = us.store_id
        WHERE us.username = ? AND s.is_active = 1
        ORDER BY s.name
        """,
        (username,)
    ).fetchall()
    conn.close()

    return [dict(row) for row in rows]


def delete_user_store(username: str, store_id: int) -> bool:
    """Удалить привязку пользователя к точке."""
    conn = get_db()
    conn.execute(
        "DELETE FROM user_stores WHERE username = ? AND store_id = ?",
        (username, store_id)
    )
    conn.commit()
    conn.close()

    return True


# =============================================================================
# КАТЕГОРИИ РАСХОДОВ - CRUD
# =============================================================================

def create_category(name: str) -> int:
    """
    Создать категорию расхода. Возвращает ID.

    Реактивирует ранее деактивированную запись с тем же именем вместо INSERT —
    UNIQUE(name) действует на всю таблицу, а не только на активные строки,
    поэтому без этого повторно завести удалённую категорию нельзя (тот же баг
    правился в create_store выше и в invoices.storage._ref_create).

    Соединение закрывается через try/finally: дубликат имени активной категории
    роняет INSERT на UNIQUE(name), а незакрытое соединение с неоткатанной
    транзакцией держит write-лок общей БД до перезапуска воркера — после этого
    висит и вход в дашборд, и любая запись (см. init_cashshifts_tables).
    """
    conn = get_db()
    try:
        existing = conn.execute(
            "SELECT id, is_active FROM expense_categories WHERE name = ?", (name,)
        ).fetchone()
        if existing and not existing["is_active"]:
            conn.execute("UPDATE expense_categories SET is_active = 1 WHERE id = ?", (existing["id"],))
            conn.commit()
            return existing["id"]

        cursor = conn.execute(
            "INSERT INTO expense_categories (name, is_active) VALUES (?, 1)",
            (name,)
        )

        category_id = cursor.lastrowid
        conn.commit()

        return category_id
    finally:
        conn.close()


def update_category(category_id: int, name: str) -> bool:
    """Переименовать категорию. Та же ловушка с "мёртвым" именем, что и в create_category."""
    conn = get_db()
    try:
        ghost = conn.execute(
            "SELECT id FROM expense_categories WHERE name = ? AND id != ? AND is_active = 0",
            (name, category_id)
        ).fetchone()
        if ghost:
            conn.execute("DELETE FROM expense_categories WHERE id = ?", (ghost["id"],))

        conn.execute(
            "UPDATE expense_categories SET name = ? WHERE id = ?",
            (name, category_id)
        )
        conn.commit()

        return True
    finally:
        conn.close()


def delete_category(category_id: int) -> bool:
    """
    Деактивировать категорию (не удалять).

    Помечает is_active = 0 вместо удаления.
    """
    conn = get_db()
    try:
        conn.execute(
            "UPDATE expense_categories SET is_active = 0 WHERE id = ?",
            (category_id,)
        )
        conn.commit()

        return True
    finally:
        conn.close()
