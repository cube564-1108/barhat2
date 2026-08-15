"""
Модуль работы с SQLite для счетов на оплату БАРХАТ.

Создаёт таблицы, заполняет seed-данными, предоставляет функции доступа.
Проект (=салон, точка продаж) — переиспользуем таблицу stores из cashshifts,
чтобы не дублировать справочник.

Распределение суммы счёта по проектам/статьям расхода — многострочное
(invoice_line_items), потому что в ПланФакт один платёж штатно разбивается
на несколько частей с разными статьями/проектами (см. план, раздел
"Уточнения от владельца после исследования API ПланФакт"). Однострочный
счёт — частный случай с одной строкой.
"""

import logging
import os
import sqlite3
import uuid
from typing import Any, Dict, List, Optional

from .seed_data import EXPENSE_CATEGORIES

logger = logging.getLogger(__name__)

# Путь к БД из переменной окружения или дефолт — та же база, что у auth/cashshifts
DB_PATH = os.environ.get("BARHAT_DB_PATH", "barhat.db")

# Куда сохранять вложения (скрины/сканы счетов). Отдельная переменная окружения,
# чтобы не зависеть от того, где смонтирован постоянный диск на Amvera.
ATTACHMENTS_DIR = os.environ.get("INVOICE_ATTACHMENTS_DIR", "invoice_attachments")

ALLOWED_ATTACHMENT_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".pdf"}
MAX_ATTACHMENT_SIZE_BYTES = 15 * 1024 * 1024  # 15 МБ

try:
    from cashshifts.storage import get_all_stores, get_store_by_id
except ImportError:
    logger.warning("Модуль cashshifts недоступен — салоны для счетов не будут получены")

    def get_all_stores() -> List[Dict[str, Any]]:
        return []

    def get_store_by_id(store_id: int) -> Optional[Dict[str, Any]]:
        return None


STATUSES = ("on_approval", "approved", "rejected", "sent_to_bank", "paid")

# Простые справочники вида (id, name, is_active, created_at) — статьи расхода,
# города, плательщики (юрлица/ИП Бархата), варианты НДС. Все редактируются
# только админом, у всех одинаковый CRUD — не дублируем его 4 раза.
REFERENCE_TABLES = {
    "categories": "invoice_expense_categories",
    "cities": "invoice_cities",
    "payers": "invoice_payers",
    "vat_options": "invoice_vat_options",
}


def get_db():
    """
    Получить соединение с БД. Таймаут увеличен против дефолтных 5с — gunicorn
    на проде поднимает 2 воркера (`amvera.yml`), которые независимо друг от
    друга инициализируют таблицы при старте и могут одновременно писать
    в один и тот же файл SQLite; без запаса воркер получает
    "database is locked" вместо того, чтобы просто дождаться своей очереди.
    """
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    # WAL вместо дефолтного rollback-journal: читатели не блокируют писателя
    # и наоборот — резко меньше "database is locked" при нескольких воркерах
    # на одном файле. Настройка хранится в самом файле БД, но выставляем на
    # каждом соединении — дёшево и идемпотентно, если уже включено.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=20000")
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row["name"] == column for row in rows)


def init_invoices_tables():
    """Инициализация таблиц счетов (вызывается при старте приложения)."""

    conn = get_db()

    # ========================================================================
    # 1. Справочники (статьи расхода, города, плательщики, НДС)
    # ========================================================================
    conn.execute("""
        CREATE TABLE IF NOT EXISTS invoice_expense_categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            is_active INTEGER NOT NULL DEFAULT 1,
            planfact_category_id TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_invoice_categories_active
        ON invoice_expense_categories(is_active) WHERE is_active = 1
    """)

    for table in ("invoice_cities", "invoice_payers", "invoice_vat_options"):
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {table} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_{table}_active
            ON {table}(is_active) WHERE is_active = 1
        """)

    conn.commit()

    # ========================================================================
    # 2. Миграция старой модели invoices (один store_id/category на счёт)
    #    на новую (распределение — в invoice_line_items)
    # ========================================================================
    _ensure_invoices_migrated(conn)

    # ========================================================================
    # 3. Счета на оплату (invoices) — новая схема
    # ========================================================================
    conn.execute("""
        CREATE TABLE IF NOT EXISTS invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_number TEXT UNIQUE,
            match_code TEXT UNIQUE,
            city_id INTEGER REFERENCES invoice_cities(id),
            payer_id INTEGER REFERENCES invoice_payers(id),
            vat_id INTEGER REFERENCES invoice_vat_options(id),
            counterparty_name TEXT,
            counterparty_inn TEXT,
            counterparty_bank_name TEXT,
            counterparty_bank_bik TEXT,
            counterparty_bank_account TEXT,
            counterparty_bank_corr_account TEXT,
            amount REAL NOT NULL CHECK (amount > 0),
            payment_purpose TEXT NOT NULL,
            due_date TEXT,
            status TEXT NOT NULL DEFAULT 'on_approval' CHECK (status IN ('on_approval','approved','rejected','sent_to_bank','paid')),
            is_archived INTEGER NOT NULL DEFAULT 0,
            archived_at TEXT,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            approved_by TEXT,
            approved_at TEXT,
            rejected_by TEXT,
            rejected_reason TEXT,
            paid_at TEXT
        )
    """)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_invoices_status ON invoices(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_invoices_created ON invoices(created_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_invoices_archived ON invoices(is_archived)")

    # ========================================================================
    # 4. Распределение по проектам/статьям (invoice_line_items)
    # ========================================================================
    conn.execute("""
        CREATE TABLE IF NOT EXISTS invoice_line_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id INTEGER NOT NULL REFERENCES invoices(id),
            store_id INTEGER NOT NULL REFERENCES stores(id),
            expense_category_id INTEGER NOT NULL REFERENCES invoice_expense_categories(id),
            amount REAL NOT NULL CHECK (amount > 0)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_invoice_line_items_invoice
        ON invoice_line_items(invoice_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_invoice_line_items_store
        ON invoice_line_items(store_id)
    """)

    # ========================================================================
    # 5. Вложения (скрины/сканы счетов)
    # ========================================================================
    conn.execute("""
        CREATE TABLE IF NOT EXISTS invoice_attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id INTEGER NOT NULL REFERENCES invoices(id),
            original_filename TEXT NOT NULL,
            stored_filename TEXT NOT NULL,
            uploaded_by TEXT NOT NULL,
            uploaded_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_invoice_attachments_invoice
        ON invoice_attachments(invoice_id)
    """)

    conn.commit()

    # ========================================================================
    # ЗАПОЛНЕНИЕ SEED-ДАННЫМИ И ДОБИВКА match_code (однократно/идемпотентно)
    # ========================================================================
    _seed_categories_if_empty(conn)
    _backfill_match_codes(conn)

    conn.close()


def _ensure_invoices_migrated(conn: sqlite3.Connection):
    """
    Переносит старую модель invoices (один store_id + expense_category_id
    NOT NULL на весь счёт) в новую (invoice_line_items, много строк на счёт).

    На проде gunicorn поднимает несколько воркеров (`amvera.yml`, --workers 2),
    которые независимо друг от друга вызывают init_invoices_tables() при
    старте — без защиты от гонки два воркера могли бы одновременно попытаться
    переименовать/создать одну и ту же таблицу. Поэтому:
    1) быстрая проверка без блокировки (частый случай — уже смигрировано,
       незачем брать лок на каждый рестарт);
    2) если похоже, что миграция нужна — берём эксклюзивную блокировку файла
       (BEGIN IMMEDIATE) и перепроверяем условие уже под ней;
    3) сама миграция резюмируема: если её уже начал и не доделал другой
       воркер (осталась invoices_old_v1), доделываем с того места, а не
       падаем и не теряем данные.
    """
    needs_check = _table_exists(conn, "invoices_old_v1") or (
        _table_exists(conn, "invoices") and not _column_exists(conn, "invoices", "city_id")
    )
    if not needs_check:
        return

    conn.execute("BEGIN IMMEDIATE")
    try:
        if _table_exists(conn, "invoices") and not _column_exists(conn, "invoices", "city_id") \
                and not _table_exists(conn, "invoices_old_v1"):
            logger.info("Миграция invoices: переименование старой таблицы...")
            conn.execute("ALTER TABLE invoices RENAME TO invoices_old_v1")

        if _table_exists(conn, "invoices_old_v1"):
            _finish_migration_from_old_table(conn, "invoices_old_v1")

        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _finish_migration_from_old_table(conn: sqlite3.Connection, old_table_name: str):
    """
    Доводит миграцию до конца из таблицы old_table_name (старая invoices,
    переименованная в рамках текущего вызова или оставленная прерванной
    попыткой другого воркера). Вставки идемпотентны (проверка "уже перенесено?"
    перед INSERT) — безопасно вызывать повторно на частично перенесённых данных.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS invoice_line_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id INTEGER NOT NULL REFERENCES invoices(id),
            store_id INTEGER NOT NULL REFERENCES stores(id),
            expense_category_id INTEGER NOT NULL REFERENCES invoice_expense_categories(id),
            amount REAL NOT NULL CHECK (amount > 0)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_number TEXT UNIQUE,
            match_code TEXT UNIQUE,
            city_id INTEGER REFERENCES invoice_cities(id),
            payer_id INTEGER REFERENCES invoice_payers(id),
            vat_id INTEGER REFERENCES invoice_vat_options(id),
            counterparty_name TEXT,
            counterparty_inn TEXT,
            counterparty_bank_name TEXT,
            counterparty_bank_bik TEXT,
            counterparty_bank_account TEXT,
            counterparty_bank_corr_account TEXT,
            amount REAL NOT NULL CHECK (amount > 0),
            payment_purpose TEXT NOT NULL,
            due_date TEXT,
            status TEXT NOT NULL DEFAULT 'on_approval' CHECK (status IN ('on_approval','approved','rejected','sent_to_bank','paid')),
            is_archived INTEGER NOT NULL DEFAULT 0,
            archived_at TEXT,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            approved_by TEXT,
            approved_at TEXT,
            rejected_by TEXT,
            rejected_reason TEXT,
            paid_at TEXT
        )
    """)

    old_rows = conn.execute(f"SELECT * FROM {old_table_name}").fetchall()
    migrated = 0

    for row in old_rows:
        if not conn.execute("SELECT 1 FROM invoices WHERE id = ?", (row["id"],)).fetchone():
            conn.execute(
                """
                INSERT INTO invoices (
                    id, invoice_number, counterparty_name, counterparty_inn,
                    counterparty_bank_name, counterparty_bank_bik, counterparty_bank_account,
                    counterparty_bank_corr_account, amount, payment_purpose, due_date,
                    status, created_by, created_at, approved_by, approved_at,
                    rejected_by, rejected_reason, paid_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"], row["invoice_number"], row["counterparty_name"], row["counterparty_inn"],
                    row["counterparty_bank_name"], row["counterparty_bank_bik"], row["counterparty_bank_account"],
                    row["counterparty_bank_corr_account"], row["amount"], row["payment_purpose"], row["due_date"],
                    row["status"], row["created_by"], row["created_at"], row["approved_by"], row["approved_at"],
                    row["rejected_by"], row["rejected_reason"], row["paid_at"],
                )
            )
            migrated += 1

        if not conn.execute("SELECT 1 FROM invoice_line_items WHERE invoice_id = ?", (row["id"],)).fetchone():
            conn.execute(
                """
                INSERT INTO invoice_line_items (invoice_id, store_id, expense_category_id, amount)
                VALUES (?, ?, ?, ?)
                """,
                (row["id"], row["store_id"], row["expense_category_id"], row["amount"])
            )

    conn.execute(f"DROP TABLE {old_table_name}")
    logger.info(f"Миграция invoices: перенесено {migrated} новых счетов из {len(old_rows)} в {old_table_name} (остальные уже были перенесены ранее)")


def _backfill_match_codes(conn: sqlite3.Connection):
    """Проставить match_code существующим счетам, у которых его ещё нет."""
    rows = conn.execute("SELECT id FROM invoices WHERE match_code IS NULL").fetchall()
    for row in rows:
        conn.execute(
            "UPDATE invoices SET match_code = ? WHERE id = ?",
            (f"REF-{row['id']:06d}", row["id"])
        )
    if rows:
        conn.commit()


def _seed_categories_if_empty(conn: sqlite3.Connection):
    """Заполнить таблицу invoice_expense_categories если она пуста."""

    result = conn.execute("SELECT COUNT(*) as count FROM invoice_expense_categories").fetchone()

    if result["count"] == 0:
        logger.info("Заполнение invoice_expense_categories seed-данными...")

        for cat in EXPENSE_CATEGORIES:
            conn.execute(
                "INSERT INTO invoice_expense_categories (name, is_active) VALUES (?, 1)",
                (cat["name"],)
            )

        conn.commit()
        logger.info(f"Добавлено {len(EXPENSE_CATEGORIES)} статей расхода")


# =============================================================================
# СПРАВОЧНИКИ (статьи расхода, города, плательщики, НДС) — общий CRUD
# =============================================================================

def _ref_list_active(table: str) -> List[Dict[str, Any]]:
    conn = get_db()
    rows = conn.execute(f"SELECT * FROM {table} WHERE is_active = 1 ORDER BY name").fetchall()
    conn.close()
    return [dict(row) for row in rows]


def _ref_get_by_id(table: str, item_id: int) -> Optional[Dict[str, Any]]:
    conn = get_db()
    row = conn.execute(
        f"SELECT * FROM {table} WHERE id = ? AND is_active = 1", (item_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def _ref_create(table: str, name: str) -> int:
    """
    "Удалить" в справочниках — мягкое удаление (is_active=0), а name UNIQUE
    действует на всю таблицу, а не только на активные строки. Без этой
    проверки повторно завести ранее удалённое название (например, город)
    невозможно — INSERT падает на UNIQUE constraint, хотя в списке его
    уже не видно. Поэтому для неактивной записи с таким именем — просто
    реактивируем её вместо INSERT.
    """
    conn = get_db()
    existing = conn.execute(f"SELECT id, is_active FROM {table} WHERE name = ?", (name,)).fetchone()
    if existing and not existing["is_active"]:
        conn.execute(f"UPDATE {table} SET is_active = 1 WHERE id = ?", (existing["id"],))
        conn.commit()
        conn.close()
        return existing["id"]

    cursor = conn.execute(f"INSERT INTO {table} (name, is_active) VALUES (?, 1)", (name,))
    item_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return item_id


def _ref_update(table: str, item_id: int, name: str) -> bool:
    """
    Та же ловушка, что и в _ref_create: если целевое имя занято чужой
    неактивной (мягко удалённой) строкой, обычный UPDATE упадёт на UNIQUE
    constraint, хотя в списке этого имени не видно. "Мёртвая" запись никому
    не нужна — убираем её перед переименованием.
    """
    conn = get_db()
    ghost = conn.execute(
        f"SELECT id FROM {table} WHERE name = ? AND id != ? AND is_active = 0",
        (name, item_id)
    ).fetchone()
    if ghost:
        conn.execute(f"DELETE FROM {table} WHERE id = ?", (ghost["id"],))

    conn.execute(f"UPDATE {table} SET name = ? WHERE id = ?", (name, item_id))
    conn.commit()
    conn.close()
    return True


def _ref_deactivate(table: str, item_id: int) -> bool:
    conn = get_db()
    conn.execute(f"UPDATE {table} SET is_active = 0 WHERE id = ?", (item_id,))
    conn.commit()
    conn.close()
    return True


def get_all_expense_categories() -> List[Dict[str, Any]]:
    return _ref_list_active("invoice_expense_categories")


def get_expense_category_by_id(category_id: int) -> Optional[Dict[str, Any]]:
    return _ref_get_by_id("invoice_expense_categories", category_id)


def create_expense_category(name: str) -> int:
    return _ref_create("invoice_expense_categories", name)


def update_expense_category(category_id: int, name: str) -> bool:
    return _ref_update("invoice_expense_categories", category_id, name)


def delete_expense_category(category_id: int) -> bool:
    return _ref_deactivate("invoice_expense_categories", category_id)


def get_all_cities() -> List[Dict[str, Any]]:
    return _ref_list_active("invoice_cities")


def get_city_by_id(city_id: int) -> Optional[Dict[str, Any]]:
    return _ref_get_by_id("invoice_cities", city_id)


def create_city(name: str) -> int:
    return _ref_create("invoice_cities", name)


def update_city(city_id: int, name: str) -> bool:
    return _ref_update("invoice_cities", city_id, name)


def delete_city(city_id: int) -> bool:
    return _ref_deactivate("invoice_cities", city_id)


def get_all_payers() -> List[Dict[str, Any]]:
    return _ref_list_active("invoice_payers")


def get_payer_by_id(payer_id: int) -> Optional[Dict[str, Any]]:
    return _ref_get_by_id("invoice_payers", payer_id)


def create_payer(name: str) -> int:
    return _ref_create("invoice_payers", name)


def update_payer(payer_id: int, name: str) -> bool:
    return _ref_update("invoice_payers", payer_id, name)


def delete_payer(payer_id: int) -> bool:
    return _ref_deactivate("invoice_payers", payer_id)


def get_all_vat_options() -> List[Dict[str, Any]]:
    return _ref_list_active("invoice_vat_options")


def get_vat_option_by_id(vat_id: int) -> Optional[Dict[str, Any]]:
    return _ref_get_by_id("invoice_vat_options", vat_id)


def create_vat_option(name: str) -> int:
    return _ref_create("invoice_vat_options", name)


def update_vat_option(vat_id: int, name: str) -> bool:
    return _ref_update("invoice_vat_options", vat_id, name)


def delete_vat_option(vat_id: int) -> bool:
    return _ref_deactivate("invoice_vat_options", vat_id)


# =============================================================================
# РАСПРЕДЕЛЕНИЕ ПО ПРОЕКТАМ/СТАТЬЯМ (invoice_line_items)
# =============================================================================

def get_invoice_line_items(invoice_id: int) -> List[Dict[str, Any]]:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM invoice_line_items WHERE invoice_id = ? ORDER BY id",
        (invoice_id,)
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def is_invoice_fully_allocated(invoice_id: int) -> bool:
    """Сумма строк распределения сходится с суммой счёта (с учётом округления)."""
    conn = get_db()
    invoice = conn.execute("SELECT amount FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    if not invoice:
        conn.close()
        return False
    total = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) as total FROM invoice_line_items WHERE invoice_id = ?",
        (invoice_id,)
    ).fetchone()["total"]
    conn.close()
    return total > 0 and abs(total - invoice["amount"]) < 0.01


def set_invoice_line_items(invoice_id: int, items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Полностью заменить распределение счёта по проектам/статьям.

    items: [{"store_id": int, "expense_category_id": int, "amount": float}, ...]
    Если счёт в архиве — правка запрещена (архив закрывает работу по счёту).
    Возвращает {"ok": True/False, "error": str|None}.
    """
    conn = get_db()
    invoice = conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    if not invoice:
        conn.close()
        return {"ok": False, "error": "Счёт не найден"}

    if invoice["is_archived"]:
        conn.close()
        return {"ok": False, "error": "Счёт в архиве — распределение изменить нельзя"}

    if items:
        total = sum(item["amount"] for item in items)
        if abs(total - invoice["amount"]) >= 0.01:
            conn.close()
            return {"ok": False, "error": f"Сумма строк ({total}) не равна сумме счёта ({invoice['amount']})"}

    conn.execute("DELETE FROM invoice_line_items WHERE invoice_id = ?", (invoice_id,))
    for item in items:
        conn.execute(
            """
            INSERT INTO invoice_line_items (invoice_id, store_id, expense_category_id, amount)
            VALUES (?, ?, ?, ?)
            """,
            (invoice_id, item["store_id"], item["expense_category_id"], item["amount"])
        )
    conn.commit()
    conn.close()

    _auto_archive_if_ready(invoice_id)

    return {"ok": True, "error": None}


# =============================================================================
# ВЛОЖЕНИЯ
# =============================================================================

def add_invoice_attachment(invoice_id: int, original_filename: str, file_bytes: bytes, uploaded_by: str) -> Dict[str, Any]:
    """Сохранить файл на диск и запись о нём в БД. Возвращает {"ok", "error", "attachment"}."""
    ext = os.path.splitext(original_filename)[1].lower()
    if ext not in ALLOWED_ATTACHMENT_EXTENSIONS:
        return {"ok": False, "error": f"Недопустимый тип файла: {ext}", "attachment": None}
    if len(file_bytes) > MAX_ATTACHMENT_SIZE_BYTES:
        return {"ok": False, "error": "Файл слишком большой (максимум 15 МБ)", "attachment": None}

    os.makedirs(ATTACHMENTS_DIR, exist_ok=True)
    stored_filename = f"{uuid.uuid4().hex}{ext}"
    with open(os.path.join(ATTACHMENTS_DIR, stored_filename), "wb") as f:
        f.write(file_bytes)

    conn = get_db()
    cursor = conn.execute(
        """
        INSERT INTO invoice_attachments (invoice_id, original_filename, stored_filename, uploaded_by)
        VALUES (?, ?, ?, ?)
        """,
        (invoice_id, original_filename, stored_filename, uploaded_by)
    )
    attachment_id = cursor.lastrowid
    conn.commit()
    row = conn.execute("SELECT * FROM invoice_attachments WHERE id = ?", (attachment_id,)).fetchone()
    conn.close()

    return {"ok": True, "error": None, "attachment": dict(row)}


def get_invoice_attachments(invoice_id: int) -> List[Dict[str, Any]]:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM invoice_attachments WHERE invoice_id = ? ORDER BY uploaded_at",
        (invoice_id,)
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_attachment_by_id(attachment_id: int) -> Optional[Dict[str, Any]]:
    conn = get_db()
    row = conn.execute("SELECT * FROM invoice_attachments WHERE id = ?", (attachment_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def delete_attachment(attachment_id: int) -> bool:
    attachment = get_attachment_by_id(attachment_id)
    if not attachment:
        return False

    conn = get_db()
    conn.execute("DELETE FROM invoice_attachments WHERE id = ?", (attachment_id,))
    conn.commit()
    conn.close()

    try:
        os.remove(os.path.join(ATTACHMENTS_DIR, attachment["stored_filename"]))
    except OSError:
        logger.warning(f"Не удалось удалить файл вложения {attachment['stored_filename']} с диска")

    return True


# =============================================================================
# СЧЕТА НА ОПЛАТУ - CRUD
# =============================================================================

def create_invoice(
    amount: float,
    payment_purpose: str,
    created_by: str,
    city_id: Optional[int] = None,
    payer_id: Optional[int] = None,
    vat_id: Optional[int] = None,
    counterparty_name: Optional[str] = None,
    counterparty_inn: Optional[str] = None,
    counterparty_bank_name: Optional[str] = None,
    counterparty_bank_bik: Optional[str] = None,
    counterparty_bank_account: Optional[str] = None,
    counterparty_bank_corr_account: Optional[str] = None,
    due_date: Optional[str] = None,
    line_items: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Создать счёт на оплату. Статус сразу 'on_approval' — черновиков нет,
    как только сотрудник заполнил форму, счёт уходит на согласование.

    Номер счёта (человекочитаемый, СЧ-000123) и match_code (машиночитаемый,
    REF-000123) генерируются после вставки из ID, чтобы гарантировать
    уникальность без отдельного счётчика с race condition. match_code — это
    ключ, по которому Фаза 6 плана будет матчить оплату из ПланФакт (не
    invoice_number — он может потеряться/исказиться в свободном тексте
    назначения платежа при ручной правке в банке).

    Распределение по проектам/статьям (line_items) опционально уже на этом
    этапе — редактируется вплоть до архивации счёта.

    Возвращает созданную запись целиком.
    """
    conn = get_db()

    cursor = conn.execute(
        """
        INSERT INTO invoices (
            city_id, payer_id, vat_id, counterparty_name, counterparty_inn,
            counterparty_bank_name, counterparty_bank_bik, counterparty_bank_account,
            counterparty_bank_corr_account, amount, payment_purpose, due_date,
            created_by, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'on_approval')
        """,
        (
            city_id, payer_id, vat_id, counterparty_name, counterparty_inn,
            counterparty_bank_name, counterparty_bank_bik, counterparty_bank_account,
            counterparty_bank_corr_account, amount, payment_purpose, due_date,
            created_by,
        )
    )
    invoice_id = cursor.lastrowid

    invoice_number = f"СЧ-{invoice_id:06d}"
    match_code = f"REF-{invoice_id:06d}"

    conn.execute(
        "UPDATE invoices SET invoice_number = ?, match_code = ? WHERE id = ?",
        (invoice_number, match_code, invoice_id)
    )
    conn.commit()

    if line_items:
        for item in line_items:
            conn.execute(
                """
                INSERT INTO invoice_line_items (invoice_id, store_id, expense_category_id, amount)
                VALUES (?, ?, ?, ?)
                """,
                (invoice_id, item["store_id"], item["expense_category_id"], item["amount"])
            )
        conn.commit()

    row = conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    conn.close()

    return dict(row)


def get_invoice_by_id(invoice_id: int) -> Optional[Dict[str, Any]]:
    """Получить счёт по ID."""
    conn = get_db()
    row = conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_invoice_by_number(invoice_number: str) -> Optional[Dict[str, Any]]:
    """Получить счёт по номеру (человекочитаемый, для UI/бухгалтерии)."""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM invoices WHERE invoice_number = ?", (invoice_number,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_invoice_by_match_code(match_code: str) -> Optional[Dict[str, Any]]:
    """Получить счёт по match_code (для матчинга с ПланФакт, Фаза 6)."""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM invoices WHERE match_code = ?", (match_code,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def list_invoices(
    status: Optional[str] = None,
    store_id: Optional[int] = None,
    city_id: Optional[int] = None,
    created_by: Optional[str] = None,
    counterparty: Optional[str] = None,
    payment_purpose: Optional[str] = None,
    created_from: Optional[str] = None,
    created_to: Optional[str] = None,
    due_from: Optional[str] = None,
    due_to: Optional[str] = None,
    is_archived: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """Получить список счетов с фильтрами."""

    query = "SELECT * FROM invoices WHERE is_archived = ?"
    params: List[Any] = [1 if is_archived else 0]

    if status:
        query += " AND status = ?"
        params.append(status)

    if store_id:
        query += " AND id IN (SELECT invoice_id FROM invoice_line_items WHERE store_id = ?)"
        params.append(store_id)

    if city_id:
        query += " AND city_id = ?"
        params.append(city_id)

    if created_by:
        query += " AND created_by = ?"
        params.append(created_by)

    if counterparty:
        query += " AND counterparty_name LIKE ?"
        params.append(f"%{counterparty}%")

    if payment_purpose:
        query += " AND payment_purpose LIKE ?"
        params.append(f"%{payment_purpose}%")

    if created_from:
        query += " AND created_at >= ?"
        params.append(created_from)

    if created_to:
        # created_at хранит дату и время, а фильтр из формы — только дату (YYYY-MM-DD),
        # поэтому сравниваем "меньше начала следующего дня", иначе выпадают все счета
        # текущего дня, заведённые позже полуночи
        query += " AND created_at < datetime(?, '+1 day')"
        params.append(created_to)

    if due_from:
        query += " AND due_date >= ?"
        params.append(due_from)

    if due_to:
        query += " AND due_date <= ?"
        params.append(due_to)

    query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    conn = get_db()
    rows = conn.execute(query, params).fetchall()
    conn.close()

    return [dict(row) for row in rows]


def approve_invoice(invoice_id: int, approved_by: str) -> bool:
    """Согласовать счёт. Возвращает False, если счёт не в статусе on_approval."""
    conn = get_db()
    row = conn.execute("SELECT status FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    if not row or row["status"] != "on_approval":
        conn.close()
        return False

    conn.execute(
        """
        UPDATE invoices
        SET status = 'approved', approved_by = ?, approved_at = datetime('now')
        WHERE id = ?
        """,
        (approved_by, invoice_id)
    )
    conn.commit()
    conn.close()
    return True


def reject_invoice(invoice_id: int, rejected_by: str, reason: Optional[str] = None) -> bool:
    """
    Отклонить счёт. Возвращает False, если счёт не в статусе on_approval.
    Отклонённый счёт сразу архивируется — по нему больше нет работы.
    """
    conn = get_db()
    row = conn.execute("SELECT status FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    if not row or row["status"] != "on_approval":
        conn.close()
        return False

    conn.execute(
        """
        UPDATE invoices
        SET status = 'rejected', rejected_by = ?, rejected_reason = ?,
            is_archived = 1, archived_at = datetime('now')
        WHERE id = ?
        """,
        (rejected_by, reason, invoice_id)
    )
    conn.commit()
    conn.close()
    return True


def mark_invoice_paid(invoice_id: int) -> bool:
    """
    Пометить счёт оплаченным. Пока нет токена Модульбанк/ПланФакт (Фазы 5-6),
    вызывается вручную из дашборда админом; в будущем — автоматически при
    матчинге операции из ПланФакт.

    Если на момент оплаты распределение уже полностью проставлено —
    счёт сразу уходит в архив (работа по нему закончена).
    """
    conn = get_db()
    row = conn.execute("SELECT status FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    if not row or row["status"] not in ("approved", "sent_to_bank"):
        conn.close()
        return False

    conn.execute(
        "UPDATE invoices SET status = 'paid', paid_at = datetime('now') WHERE id = ?",
        (invoice_id,)
    )
    conn.commit()
    conn.close()

    _auto_archive_if_ready(invoice_id)
    return True


def _auto_archive_if_ready(invoice_id: int):
    """Оплаченный и полностью разнесённый счёт автоматически уходит в архив."""
    conn = get_db()
    row = conn.execute("SELECT status, is_archived FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    if not row or row["status"] != "paid" or row["is_archived"]:
        conn.close()
        return
    conn.close()

    if is_invoice_fully_allocated(invoice_id):
        set_invoice_archived(invoice_id, True)


def set_invoice_archived(invoice_id: int, archived: bool) -> bool:
    """Ручной перевод счёта в архив/из архива (доступно и вне авто-условий)."""
    conn = get_db()
    row = conn.execute("SELECT id FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    if not row:
        conn.close()
        return False

    conn.execute(
        """
        UPDATE invoices
        SET is_archived = ?, archived_at = CASE WHEN ? THEN datetime('now') ELSE NULL END
        WHERE id = ?
        """,
        (1 if archived else 0, 1 if archived else 0, invoice_id)
    )
    conn.commit()
    conn.close()
    return True
