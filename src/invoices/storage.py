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

from storage_paths import resolve as resolve_data_path

from .seed_data import EXPENSE_CATEGORIES

logger = logging.getLogger(__name__)

# Путь к БД из переменной окружения или дефолт — та же база, что у auth/cashshifts
DB_PATH = os.environ.get("BARHAT_DB_PATH", "barhat.db")

# Куда сохранять вложения (скрины/сканы счетов). Путь берём из storage_paths,
# а не из os.environ напрямую: относительный дефолт клал файлы в /app, и они
# стирались каждой сборкой Amvera — запись в БД оставалась, а файла не было.
ATTACHMENTS_DIR = resolve_data_path("INVOICE_ATTACHMENTS_DIR", "invoice_attachments")

ALLOWED_ATTACHMENT_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".pdf"}
MAX_ATTACHMENT_SIZE_BYTES = 15 * 1024 * 1024  # 15 МБ

try:
    from cashshifts.storage import get_all_stores, get_store_by_id, get_user_stores, get_users_full_names
except ImportError:
    logger.warning("Модуль cashshifts недоступен — салоны для счетов не будут получены")

    def get_all_stores() -> List[Dict[str, Any]]:
        return []

    def get_store_by_id(store_id: int) -> Optional[Dict[str, Any]]:
        return None

    def get_user_stores(username: str) -> List[int]:
        return []

    def get_users_full_names(usernames: List[str]) -> Dict[str, str]:
        return {}


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

    # Фаза 5 (автоформирование платёжки в Модульбанк): у каждого плательщика
    # (компании из "На кого выставлен счёт") — свой расчётный счёт в банке,
    # один API-токен обслуживает весь кабинет сразу со всеми компаниями.
    # Реквизиты живут в самом справочнике плательщиков, а не в .env — иначе
    # пришлось бы городить отдельный конфиг на каждую компанию.
    _ensure_payer_bank_columns(conn)

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
            counterparty_kpp TEXT,
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
            paid_at TEXT,
            bank_send_error TEXT
        )
    """)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_invoices_status ON invoices(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_invoices_created ON invoices(created_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_invoices_archived ON invoices(is_archived)")

    # Фаза 5 (автоформирование платёжки в Модульбанк): счета, созданные до
    # этого поля, не имеют counterparty_kpp/bank_send_error — добавляем
    # ALTER TABLE тем же паттерном блокировки, что и _ensure_invoices_migrated
    # (см. её докстринг — на проде несколько gunicorn-воркеров стартуют
    # параллельно и могут столкнуться на "duplicate column name").
    _ensure_invoice_bank_columns(conn)

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

    # ========================================================================
    # 6. История изменений
    # ========================================================================
    conn.execute("""
        CREATE TABLE IF NOT EXISTS invoice_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id INTEGER NOT NULL REFERENCES invoices(id),
            changed_by TEXT NOT NULL,
            changed_at TEXT NOT NULL DEFAULT (datetime('now')),
            field_name TEXT NOT NULL,
            old_value TEXT,
            new_value TEXT
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_invoice_history_invoice
        ON invoice_history(invoice_id)
    """)

    # ========================================================================
    # 7. Сообщения (обсуждение счёта)
    # ========================================================================
    conn.execute("""
        CREATE TABLE IF NOT EXISTS invoice_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id INTEGER NOT NULL REFERENCES invoices(id),
            author TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_invoice_comments_invoice
        ON invoice_comments(invoice_id)
    """)

    # ========================================================================
    # 8. Сопоставление салонов с проектами ПланФакт (Фаза 6). Живёт в этом
    # модуле, а не в cashshifts.stores — не трогаем чужую таблицу, salon-
    # справочник модуля кассовых смен активно меняется отдельно.
    # ========================================================================
    conn.execute("""
        CREATE TABLE IF NOT EXISTS invoice_store_planfact_projects (
            store_id INTEGER PRIMARY KEY REFERENCES stores(id),
            planfact_project_id TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    # ========================================================================
    # 9. Лог синхронизации с ПланФакт (Фаза 6) — статус синка виден из БД,
    # общий для обоих gunicorn-воркеров, по аналогии с moysklad sync_log
    # ========================================================================
    conn.execute("""
        CREATE TABLE IF NOT EXISTS invoice_planfact_sync_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL DEFAULT (datetime('now')),
            finished_at TEXT,
            status TEXT NOT NULL DEFAULT 'started' CHECK (status IN ('started','completed','failed')),
            dry_run INTEGER NOT NULL DEFAULT 0,
            matched_count INTEGER NOT NULL DEFAULT 0,
            unmatched_count INTEGER NOT NULL DEFAULT 0,
            error_message TEXT
        )
    """)

    # ========================================================================
    # 10. Операции ПланФакт, которые не удалось однозначно сматчить/разнести
    # автоматически — не теряются молча, видны админу в дашборде
    # ========================================================================
    conn.execute("""
        CREATE TABLE IF NOT EXISTS invoice_planfact_unmatched (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            planfact_operation_id TEXT NOT NULL UNIQUE,
            match_code TEXT,
            invoice_id INTEGER REFERENCES invoices(id),
            reason TEXT NOT NULL,
            operation_amount REAL,
            operation_comment TEXT,
            detected_at TEXT NOT NULL DEFAULT (datetime('now')),
            resolved INTEGER NOT NULL DEFAULT 0,
            resolved_at TEXT
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_invoice_planfact_unmatched_resolved
        ON invoice_planfact_unmatched(resolved) WHERE resolved = 0
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


_PAYER_BANK_COLUMNS = ("inn", "kpp", "bank_account", "bank_name", "bank_bik", "bank_corr_account")


def _ensure_payer_bank_columns(conn: sqlite3.Connection):
    """
    Реквизиты расчётного счёта плательщика (Фаза 5) — своя запись у каждой
    компании ("На кого выставлен счёт": ООО Кофферс, ИП Насуленко и т.д.),
    все компании — в одном кабинете Модульбанка под одним токеном. Пустые
    реквизиты у плательщика означают "этот плательщик не проводится через
    банк-автоматику" — счёт закрывается вручную (mark-paid), без отдельного
    флага на этот случай.
    """
    if not _table_exists(conn, "invoice_payers"):
        return
    if all(_column_exists(conn, "invoice_payers", col) for col in _PAYER_BANK_COLUMNS):
        return

    conn.execute("BEGIN IMMEDIATE")
    try:
        for col in _PAYER_BANK_COLUMNS:
            if not _column_exists(conn, "invoice_payers", col):
                conn.execute(f"ALTER TABLE invoice_payers ADD COLUMN {col} TEXT")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _ensure_invoice_bank_columns(conn: sqlite3.Connection):
    """
    Добивка полей invoices, добавленных для Фазы 5 (автоформирование платёжки
    в Модульбанк): counterparty_kpp (обязателен в 1С-формате платёжки для
    юрлиц) и bank_send_error (текст последней ошибки отправки, чтобы сбой
    банка не терялся молча — см. план, Фаза 5).

    ADD COLUMN — лёгкая метаданная-операция в SQLite, но два gunicorn-
    воркера всё равно могут одновременно попытаться добавить одну и ту же
    колонку без эксклюзивной блокировки и упасть на "duplicate column name".
    """
    if not _table_exists(conn, "invoices"):
        return
    if _column_exists(conn, "invoices", "counterparty_kpp") and _column_exists(conn, "invoices", "bank_send_error"):
        return

    conn.execute("BEGIN IMMEDIATE")
    try:
        if not _column_exists(conn, "invoices", "counterparty_kpp"):
            conn.execute("ALTER TABLE invoices ADD COLUMN counterparty_kpp TEXT")
        if not _column_exists(conn, "invoices", "bank_send_error"):
            conn.execute("ALTER TABLE invoices ADD COLUMN bank_send_error TEXT")
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


def update_payer_bank_requisites(
    payer_id: int,
    inn: Optional[str],
    kpp: Optional[str],
    bank_account: Optional[str],
    bank_name: Optional[str],
    bank_bik: Optional[str],
    bank_corr_account: Optional[str],
) -> bool:
    """
    Реквизиты расчётного счёта плательщика для Фазы 5 (автоформирование
    платёжки в Модульбанк). Пустые реквизиты — сигнал, что этот плательщик
    не проводится через банк-автоматику (см. _ensure_payer_bank_columns).
    """
    conn = get_db()
    conn.execute(
        """
        UPDATE invoice_payers
        SET inn = ?, kpp = ?, bank_account = ?, bank_name = ?, bank_bik = ?, bank_corr_account = ?
        WHERE id = ?
        """,
        (inn, kpp, bank_account, bank_name, bank_bik, bank_corr_account, payer_id)
    )
    conn.commit()
    conn.close()
    return True


def get_payer_bank_requisites(payer_id: int) -> Optional[Dict[str, Any]]:
    """Возвращает {"name","inn","kpp","bank_account","bank_name","bank_bik","bank_corr_account"} или None."""
    payer = get_payer_by_id(payer_id)
    if not payer:
        return None
    return {
        "name": payer["name"],
        "inn": payer.get("inn"),
        "kpp": payer.get("kpp"),
        "account": payer.get("bank_account"),
        "bank_name": payer.get("bank_name"),
        "bank_bik": payer.get("bank_bik"),
        "bank_corr_account": payer.get("bank_corr_account"),
    }


def payer_has_bank_requisites(payer_id: int) -> bool:
    """Заполнены ли реквизиты банка у плательщика полностью (кроме kpp — у ИП его нет)."""
    req = get_payer_bank_requisites(payer_id)
    if not req:
        return False
    return all(req.get(f) for f in ("inn", "account", "bank_name", "bank_bik", "bank_corr_account"))


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


def update_expense_category_planfact_id(category_id: int, planfact_category_id: Optional[str]) -> bool:
    """Привязать статью расхода к id статьи в ПланФакт (для авторазноски, Фаза 6)."""
    conn = get_db()
    conn.execute(
        "UPDATE invoice_expense_categories SET planfact_category_id = ? WHERE id = ?",
        (planfact_category_id or None, category_id)
    )
    conn.commit()
    conn.close()
    return True


# =============================================================================
# СОПОСТАВЛЕНИЕ САЛОНОВ С ПРОЕКТАМИ ПЛАНФАКТ (Фаза 6)
# =============================================================================

def get_all_store_planfact_mappings() -> Dict[int, str]:
    """{store_id: planfact_project_id} для всех настроенных салонов."""
    conn = get_db()
    rows = conn.execute("SELECT store_id, planfact_project_id FROM invoice_store_planfact_projects").fetchall()
    conn.close()
    return {row["store_id"]: row["planfact_project_id"] for row in rows}


def set_store_planfact_project(store_id: int, planfact_project_id: str) -> bool:
    conn = get_db()
    conn.execute(
        """
        INSERT INTO invoice_store_planfact_projects (store_id, planfact_project_id, updated_at)
        VALUES (?, ?, datetime('now'))
        ON CONFLICT(store_id) DO UPDATE SET
            planfact_project_id = excluded.planfact_project_id,
            updated_at = excluded.updated_at
        """,
        (store_id, planfact_project_id)
    )
    conn.commit()
    conn.close()
    return True


def delete_store_planfact_project(store_id: int) -> bool:
    conn = get_db()
    conn.execute("DELETE FROM invoice_store_planfact_projects WHERE store_id = ?", (store_id,))
    conn.commit()
    conn.close()
    return True


# =============================================================================
# СИНХРОНИЗАЦИЯ С ПЛАНФАКТ — ЛОГ И НЕСМАТЧЕННЫЕ ОПЕРАЦИИ (Фаза 6)
# =============================================================================

def start_planfact_sync_log(dry_run: bool = False) -> int:
    conn = get_db()
    cursor = conn.execute(
        "INSERT INTO invoice_planfact_sync_log (status, dry_run) VALUES ('started', ?)",
        (1 if dry_run else 0,)
    )
    log_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return log_id


def finish_planfact_sync_log(log_id: int, matched_count: int, unmatched_count: int,
                              status: str = "completed", error_message: Optional[str] = None) -> None:
    conn = get_db()
    conn.execute(
        """
        UPDATE invoice_planfact_sync_log
        SET finished_at = datetime('now'), status = ?, matched_count = ?,
            unmatched_count = ?, error_message = ?
        WHERE id = ?
        """,
        (status, matched_count, unmatched_count, error_message, log_id)
    )
    conn.commit()
    conn.close()


def get_latest_planfact_sync_log() -> Optional[Dict[str, Any]]:
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM invoice_planfact_sync_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def record_planfact_unmatched(
    planfact_operation_id: str,
    reason: str,
    match_code: Optional[str] = None,
    invoice_id: Optional[int] = None,
    operation_amount: Optional[float] = None,
    operation_comment: Optional[str] = None,
) -> None:
    """
    Записать/обновить операцию, которую не удалось разнести автоматически.
    UPSERT по planfact_operation_id — повторный прогон синка по той же
    операции обновляет причину и "поднимает" её из resolved, а не плодит
    дубликаты в списке "Требует внимания".
    """
    conn = get_db()
    conn.execute(
        """
        INSERT INTO invoice_planfact_unmatched
            (planfact_operation_id, match_code, invoice_id, reason, operation_amount, operation_comment)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(planfact_operation_id) DO UPDATE SET
            match_code = excluded.match_code,
            invoice_id = excluded.invoice_id,
            reason = excluded.reason,
            operation_amount = excluded.operation_amount,
            operation_comment = excluded.operation_comment,
            detected_at = datetime('now'),
            resolved = 0,
            resolved_at = NULL
        """,
        (planfact_operation_id, match_code, invoice_id, reason, operation_amount, operation_comment)
    )
    conn.commit()
    conn.close()


def get_unresolved_planfact_unmatched() -> List[Dict[str, Any]]:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM invoice_planfact_unmatched WHERE resolved = 0 ORDER BY detected_at DESC"
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def resolve_planfact_unmatched(unmatched_id: int) -> bool:
    conn = get_db()
    conn.execute(
        "UPDATE invoice_planfact_unmatched SET resolved = 1, resolved_at = datetime('now') WHERE id = ?",
        (unmatched_id,)
    )
    conn.commit()
    conn.close()
    return True


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


def set_invoice_line_items(invoice_id: int, items: List[Dict[str, Any]], changed_by: str = "system") -> Dict[str, Any]:
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

    old_line_items = conn.execute(
        "SELECT * FROM invoice_line_items WHERE invoice_id = ?", (invoice_id,)
    ).fetchall()
    old_summary = ", ".join(f"салон {r['store_id']}: {r['amount']}" for r in old_line_items) or "—"
    new_summary = ", ".join(f"салон {i['store_id']}: {i['amount']}" for i in items) or "—"

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

    if old_summary != new_summary:
        add_invoice_history(invoice_id, changed_by, "распределение", old_summary, new_summary)

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

    add_invoice_history(invoice_id, uploaded_by, "вложение", None, original_filename)

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


def delete_attachment(attachment_id: int, changed_by: str = "system") -> bool:
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

    add_invoice_history(attachment["invoice_id"], changed_by, "вложение удалено", attachment["original_filename"], None)
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
    counterparty_kpp: Optional[str] = None,
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
            city_id, payer_id, vat_id, counterparty_name, counterparty_inn, counterparty_kpp,
            counterparty_bank_name, counterparty_bank_bik, counterparty_bank_account,
            counterparty_bank_corr_account, amount, payment_purpose, due_date,
            created_by, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'on_approval')
        """,
        (
            city_id, payer_id, vat_id, counterparty_name, counterparty_inn, counterparty_kpp,
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

    add_invoice_history(invoice_id, created_by, "счёт создан", None, invoice_number)

    return dict(row)


def get_invoice_by_id(invoice_id: int) -> Optional[Dict[str, Any]]:
    """Получить счёт по ID."""
    conn = get_db()
    row = conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def user_can_access_invoice(invoice: Dict[str, Any], username: str, role: str) -> bool:
    """
    Может ли пользователь видеть/трогать этот счёт.

    Админ — всегда. Автор счёта — всегда (иначе только что созданный счёт
    без распределения тут же исчезает из своего же списка). Остальные —
    только если распределение счёта задевает хотя бы один из салонов,
    к которым у пользователя есть доступ (user_stores, тот же справочник,
    что и в cashshifts).
    """
    if role == "admin" or invoice["created_by"] == username:
        return True

    allowed_store_ids = set(get_user_stores(username))
    if not allowed_store_ids:
        return False

    line_items = get_invoice_line_items(invoice["id"])
    return any(item["store_id"] in allowed_store_ids for item in line_items)


# Статусы, после которых деньги уже в движении/ушли — правки полей закрыты
# для всех, включая админа (см. план: "после того как счёт загружен в банк
# изменения в счёте закрыты").
_CLOSED_FOR_EDIT_STATUSES = ("sent_to_bank", "paid")


def can_edit_invoice_fields(invoice: Dict[str, Any], username: str, role: str) -> bool:
    """
    Может ли пользователь редактировать обычные поля счёта (не статус).

    До согласования — автор счёта или админ. После согласования — только
    админ. После отправки в банк/оплаты (или архивации) — никто.
    """
    if invoice["is_archived"] or invoice["status"] in _CLOSED_FOR_EDIT_STATUSES:
        return False
    if invoice["status"] == "on_approval":
        return role == "admin" or invoice["created_by"] == username
    return role == "admin"


def can_edit_invoice_status(invoice: Dict[str, Any], role: str) -> bool:
    """
    Смена статуса — отдельная, более широкая возможность админа: не все
    счета проходят через автозагрузку в банк, поэтому статус должен
    оставаться редактируемым дольше, чем остальные поля (иначе номерной
    статус "Загружен в банк" никогда нельзя было бы проставить руками).
    Единственная граница — архив (счёт, по которому работа закрыта).
    """
    return role == "admin" and not invoice["is_archived"]


def add_invoice_history(invoice_id: int, changed_by: str, field_name: str,
                         old_value: Optional[str], new_value: Optional[str]) -> None:
    conn = get_db()
    conn.execute(
        """
        INSERT INTO invoice_history (invoice_id, changed_by, field_name, old_value, new_value)
        VALUES (?, ?, ?, ?, ?)
        """,
        (invoice_id, changed_by, field_name, old_value, new_value)
    )
    conn.commit()
    conn.close()


def get_invoice_history(invoice_id: int) -> List[Dict[str, Any]]:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM invoice_history WHERE invoice_id = ? ORDER BY changed_at, id",
        (invoice_id,)
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def add_invoice_comment(invoice_id: int, author: str, message: str) -> Dict[str, Any]:
    conn = get_db()
    cursor = conn.execute(
        "INSERT INTO invoice_comments (invoice_id, author, message) VALUES (?, ?, ?)",
        (invoice_id, author, message)
    )
    comment_id = cursor.lastrowid
    conn.commit()
    row = conn.execute("SELECT * FROM invoice_comments WHERE id = ?", (comment_id,)).fetchone()
    conn.close()
    return dict(row)


def get_invoice_comments(invoice_id: int) -> List[Dict[str, Any]]:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM invoice_comments WHERE invoice_id = ? ORDER BY created_at, id",
        (invoice_id,)
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


# Поля счёта, которые можно менять через update_invoice (кроме статуса —
# у него отдельная функция update_invoice_status с более широким доступом)
_EDITABLE_INVOICE_FIELDS = (
    "city_id", "payer_id", "vat_id", "counterparty_name", "counterparty_inn", "counterparty_kpp",
    "counterparty_bank_name", "counterparty_bank_bik", "counterparty_bank_account",
    "counterparty_bank_corr_account", "amount", "payment_purpose", "due_date",
)


def update_invoice(invoice_id: int, changes: Dict[str, Any], changed_by: str) -> Dict[str, Any]:
    """
    Частично обновить поля счёта (без статуса, см. update_invoice_status).
    Каждое реально изменившееся поле пишется в invoice_history.
    """
    invoice = get_invoice_by_id(invoice_id)
    conn = get_db()

    # Сначала все UPDATE в одной транзакции, историю пишем ПОСЛЕ commit/close —
    # add_invoice_history открывает своё собственное соединение, и попытка
    # записать через него, пока это (conn) ещё держит незакоммиченную
    # транзакцию, падает с "database is locked"
    actually_changed = []
    for field, new_value in changes.items():
        if field not in _EDITABLE_INVOICE_FIELDS:
            continue
        old_value = invoice.get(field)
        if old_value == new_value:
            continue
        conn.execute(f"UPDATE invoices SET {field} = ? WHERE id = ?", (new_value, invoice_id))
        actually_changed.append((field, old_value, new_value))

    conn.commit()
    conn.close()

    for field, old_value, new_value in actually_changed:
        add_invoice_history(invoice_id, changed_by, field,
                             None if old_value is None else str(old_value),
                             None if new_value is None else str(new_value))

    return get_invoice_by_id(invoice_id)


def update_invoice_status(invoice_id: int, new_status: str, changed_by: str) -> Dict[str, Any]:
    """
    Прямая смена статуса счёта админом — в обход стандартных переходов
    approve/reject/mark_invoice_paid, для случаев, которые не укладываются
    в стандартный процесс (например, счёт не проводится через банк вовсе).

    Заполняет те же сопутствующие поля, что и выделенные функции перехода
    (approved_by/rejected_by/paid_at), чтобы история согласования не
    выглядела пустой при ручной правке статуса.
    """
    invoice = get_invoice_by_id(invoice_id)
    old_status = invoice["status"]
    if old_status == new_status:
        return invoice

    conn = get_db()
    conn.execute("UPDATE invoices SET status = ? WHERE id = ?", (new_status, invoice_id))

    if new_status == "approved" and not invoice["approved_by"]:
        conn.execute(
            "UPDATE invoices SET approved_by = ?, approved_at = datetime('now') WHERE id = ?",
            (changed_by, invoice_id)
        )
    elif new_status == "rejected" and not invoice["rejected_by"]:
        conn.execute(
            "UPDATE invoices SET rejected_by = ? WHERE id = ?",
            (changed_by, invoice_id)
        )
    elif new_status == "paid" and not invoice["paid_at"]:
        conn.execute(
            "UPDATE invoices SET paid_at = datetime('now') WHERE id = ?",
            (invoice_id,)
        )

    conn.commit()
    conn.close()

    add_invoice_history(invoice_id, changed_by, "status", old_status, new_status)

    if new_status == "paid":
        _auto_archive_if_ready(invoice_id)

    return get_invoice_by_id(invoice_id)


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
    restrict_username: Optional[str] = None,
    restrict_store_ids: Optional[List[int]] = None,
) -> List[Dict[str, Any]]:
    """
    Получить список счетов с фильтрами.

    restrict_username/restrict_store_ids — ограничение видимости для не-админов:
    показываем счёт, если его создал сам пользователь (restrict_username),
    ИЛИ распределение счёта задевает хотя бы один из его салонов
    (restrict_store_ids). Передавать оба вместе для не-админа, оба None —
    для админа (без ограничений).
    """

    query = "SELECT * FROM invoices WHERE is_archived = ?"
    params: List[Any] = [1 if is_archived else 0]

    if restrict_username is not None:
        if restrict_store_ids:
            placeholders = ",".join("?" * len(restrict_store_ids))
            query += f"""
                AND (created_by = ? OR id IN (
                    SELECT invoice_id FROM invoice_line_items WHERE store_id IN ({placeholders})
                ))
            """
            params.append(restrict_username)
            params.extend(restrict_store_ids)
        else:
            query += " AND created_by = ?"
            params.append(restrict_username)

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
        # Дашборд присылает готовую верхнюю границу с временем — конец суток
        # по часам сотрудника, пересчитанный в UTC (dayEndUtc в datetime.js).
        # Но голую дату (YYYY-MM-DD) поддерживаем по-прежнему: так зовут API
        # напрямую, и сравнение created_at <= '2026-08-20' отсекло бы все
        # счета этого дня, заведённые позже полуночи.
        if len(created_to.strip()) == 10:
            query += " AND created_at < datetime(?, '+1 day')"
        else:
            query += " AND created_at <= ?"
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

    add_invoice_history(invoice_id, approved_by, "status", "on_approval", "approved")
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

    add_invoice_history(invoice_id, rejected_by, "status", "on_approval", f"rejected ({reason})" if reason else "rejected")
    return True


def set_invoice_bank_send_error(invoice_id: int, error_message: Optional[str]) -> None:
    """
    Записать (error_message задан) или очистить (None) текст последней ошибки
    отправки платёжки в Модульбанк — чтобы сбой банка был виден в UI, а не
    терялся молча (см. план, Фаза 5).
    """
    conn = get_db()
    conn.execute("UPDATE invoices SET bank_send_error = ? WHERE id = ?", (error_message, invoice_id))
    conn.commit()
    conn.close()


def mark_invoice_paid(invoice_id: int, changed_by: str = "system") -> bool:
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
    old_status = row["status"]

    conn.execute(
        "UPDATE invoices SET status = 'paid', paid_at = datetime('now') WHERE id = ?",
        (invoice_id,)
    )
    conn.commit()
    conn.close()

    add_invoice_history(invoice_id, changed_by, "status", old_status, "paid")
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


def set_invoice_archived(invoice_id: int, archived: bool, changed_by: str = "system") -> bool:
    """Ручной перевод счёта в архив/из архива (доступно и вне авто-условий)."""
    conn = get_db()
    row = conn.execute("SELECT id, is_archived FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    if not row:
        conn.close()
        return False
    was_archived = bool(row["is_archived"])

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

    if was_archived != bool(archived):
        add_invoice_history(invoice_id, changed_by, "is_archived",
                             "1" if was_archived else "0", "1" if archived else "0")
    return True
