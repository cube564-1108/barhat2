"""
Модуль работы с SQLite для счетов на оплату БАРХАТ.

Создаёт таблицы, заполняет seed-данными, предоставляет функции доступа.
Салон = точка продаж — переиспользуем таблицу stores из cashshifts,
чтобы не дублировать справочник.
"""

import logging
import os
import sqlite3
from typing import Any, Dict, List, Optional

from .seed_data import EXPENSE_CATEGORIES

logger = logging.getLogger(__name__)

# Путь к БД из переменной окружения или дефолт — та же база, что у auth/cashshifts
DB_PATH = os.environ.get("BARHAT_DB_PATH", "barhat.db")

try:
    from cashshifts.storage import get_all_stores, get_store_by_id
except ImportError:
    logger.warning("Модуль cashshifts недоступен — салоны для счетов не будут получены")

    def get_all_stores() -> List[Dict[str, Any]]:
        return []

    def get_store_by_id(store_id: int) -> Optional[Dict[str, Any]]:
        return None


STATUSES = ("on_approval", "approved", "rejected", "sent_to_bank", "paid")


def get_db():
    """Получить соединение с БД."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_invoices_tables():
    """Инициализация таблиц счетов (вызывается при старте приложения)."""

    conn = get_db()

    # ========================================================================
    # 1. Статьи расхода (invoice_expense_categories)
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

    # ========================================================================
    # 2. Счета на оплату (invoices)
    # ========================================================================
    conn.execute("""
        CREATE TABLE IF NOT EXISTS invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_number TEXT UNIQUE,
            store_id INTEGER NOT NULL REFERENCES stores(id),
            expense_category_id INTEGER NOT NULL REFERENCES invoice_expense_categories(id),
            counterparty_name TEXT NOT NULL,
            counterparty_inn TEXT,
            counterparty_bank_name TEXT,
            counterparty_bank_bik TEXT,
            counterparty_bank_account TEXT,
            counterparty_bank_corr_account TEXT,
            amount REAL NOT NULL CHECK (amount > 0),
            description TEXT,
            payment_purpose TEXT,
            due_date TEXT,
            status TEXT NOT NULL DEFAULT 'on_approval' CHECK (status IN ('on_approval','approved','rejected','sent_to_bank','paid')),
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            approved_by TEXT,
            approved_at TEXT,
            rejected_by TEXT,
            rejected_reason TEXT,
            paid_at TEXT
        )
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_invoices_status
        ON invoices(status)
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_invoices_store
        ON invoices(store_id)
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_invoices_created
        ON invoices(created_at DESC)
    """)

    conn.commit()

    # ========================================================================
    # ЗАПОЛНЕНИЕ SEED-ДАННЫМИ (однократно)
    # ========================================================================
    _seed_categories_if_empty(conn)

    conn.close()


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
# СТАТЬИ РАСХОДА - CRUD
# =============================================================================

def get_all_expense_categories() -> List[Dict[str, Any]]:
    """Получить список всех активных статей расхода."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM invoice_expense_categories WHERE is_active = 1 ORDER BY name"
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_expense_category_by_id(category_id: int) -> Optional[Dict[str, Any]]:
    """Получить статью расхода по ID."""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM invoice_expense_categories WHERE id = ? AND is_active = 1",
        (category_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def create_expense_category(name: str) -> int:
    """Создать статью расхода. Возвращает ID."""
    conn = get_db()
    cursor = conn.execute(
        "INSERT INTO invoice_expense_categories (name, is_active) VALUES (?, 1)",
        (name,)
    )
    category_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return category_id


def update_expense_category(category_id: int, name: str) -> bool:
    """Обновить название статьи расхода."""
    conn = get_db()
    conn.execute(
        "UPDATE invoice_expense_categories SET name = ? WHERE id = ?",
        (name, category_id)
    )
    conn.commit()
    conn.close()
    return True


def delete_expense_category(category_id: int) -> bool:
    """Деактивировать статью расхода (не удалять)."""
    conn = get_db()
    conn.execute(
        "UPDATE invoice_expense_categories SET is_active = 0 WHERE id = ?",
        (category_id,)
    )
    conn.commit()
    conn.close()
    return True


# =============================================================================
# СЧЕТА НА ОПЛАТУ - CRUD
# =============================================================================

def create_invoice(
    store_id: int,
    expense_category_id: int,
    counterparty_name: str,
    amount: float,
    created_by: str,
    description: Optional[str] = None,
    counterparty_inn: Optional[str] = None,
    counterparty_bank_name: Optional[str] = None,
    counterparty_bank_bik: Optional[str] = None,
    counterparty_bank_account: Optional[str] = None,
    counterparty_bank_corr_account: Optional[str] = None,
    due_date: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Создать счёт на оплату. Статус сразу 'on_approval' — черновиков нет,
    как только сотрудник заполнил форму, счёт уходит на согласование.

    Номер счёта генерируется после вставки из ID (СЧ-000123), чтобы
    гарантировать уникальность без отдельного счётчика с race condition.
    Назначение платежа автоматически включает номер счёта — это ключ,
    по которому Фаза 6 плана будет матчить оплату из ПланФакт.

    Возвращает созданную запись целиком.
    """
    conn = get_db()

    cursor = conn.execute(
        """
        INSERT INTO invoices (
            store_id, expense_category_id, counterparty_name, amount,
            created_by, description, counterparty_inn, counterparty_bank_name,
            counterparty_bank_bik, counterparty_bank_account, counterparty_bank_corr_account,
            due_date, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'on_approval')
        """,
        (
            store_id, expense_category_id, counterparty_name, amount,
            created_by, description, counterparty_inn, counterparty_bank_name,
            counterparty_bank_bik, counterparty_bank_account, counterparty_bank_corr_account,
            due_date,
        )
    )
    invoice_id = cursor.lastrowid

    invoice_number = f"СЧ-{invoice_id:06d}"
    payment_purpose = f"Оплата по счёту {invoice_number}"
    if description:
        payment_purpose += f", {description}"

    conn.execute(
        "UPDATE invoices SET invoice_number = ?, payment_purpose = ? WHERE id = ?",
        (invoice_number, payment_purpose, invoice_id)
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
    """Получить счёт по номеру (для будущего матчинга с ПланФакт, Фаза 6)."""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM invoices WHERE invoice_number = ?", (invoice_number,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def list_invoices(
    status: Optional[str] = None,
    store_id: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """Получить список счетов с фильтрами."""

    query = "SELECT * FROM invoices WHERE 1=1"
    params: List[Any] = []

    if status:
        query += " AND status = ?"
        params.append(status)

    if store_id:
        query += " AND store_id = ?"
        params.append(store_id)

    if date_from:
        query += " AND created_at >= ?"
        params.append(date_from)

    if date_to:
        query += " AND created_at <= ?"
        params.append(date_to)

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
    """Отклонить счёт. Возвращает False, если счёт не в статусе on_approval."""
    conn = get_db()
    row = conn.execute("SELECT status FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    if not row or row["status"] != "on_approval":
        conn.close()
        return False

    conn.execute(
        """
        UPDATE invoices
        SET status = 'rejected', rejected_by = ?, rejected_reason = ?
        WHERE id = ?
        """,
        (rejected_by, reason, invoice_id)
    )
    conn.commit()
    conn.close()
    return True


def mark_invoice_paid(invoice_id: int) -> bool:
    """
    Пометить счёт оплаченным (используется Фазой 6 — авторазноска из ПланФакт).
    Отдельная функция уже сейчас, чтобы не трогать storage.py при подключении API ПФ.
    """
    conn = get_db()
    conn.execute(
        "UPDATE invoices SET status = 'paid', paid_at = datetime('now') WHERE id = ?",
        (invoice_id,)
    )
    conn.commit()
    conn.close()
    return True
