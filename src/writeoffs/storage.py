"""
Модуль работы с SQLite для списаний товара БАРХАТ.

Точка продаж (=салон) — переиспользуем таблицу stores из cashshifts,
чтобы не дублировать справочник. Доступ к точкам — через cashshifts.check_store_access.
"""

import logging
import os
import sqlite3
import uuid
from typing import Any, Dict, List, Optional

from storage_paths import resolve as resolve_data_path

logger = logging.getLogger(__name__)

STATUSES = ("on_approval", "processing", "sent", "failed", "rejected", "cancelled")

# Путь к БД из переменной окружения или дефолт — та же база, что у auth/cashshifts/invoices
DB_PATH = os.environ.get("BARHAT_DB_PATH", "barhat.db")

# Куда сохранять вложения (фото списанного товара). Путь берём из storage_paths —
# см. комментарий там: относительный дефолт означал потерю файлов на каждой сборке.
ATTACHMENTS_DIR = resolve_data_path("WRITEOFF_ATTACHMENTS_DIR", "writeoff_attachments")

ALLOWED_ATTACHMENT_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
MAX_ATTACHMENT_SIZE_BYTES = 15 * 1024 * 1024  # 15 МБ


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
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=20000")
    return conn


def init_writeoffs_tables():
    """Инициализация таблиц модуля списаний (вызывается при старте приложения)."""

    conn = get_db()

    # ========================================================================
    # Таблица-связка: склад МойСклад (UUID) <-> точка продаж (cashshifts.stores)
    #
    # Названа нейтрально (не writeoff_*), чтобы план stock-monitoring
    # (plans/2026-08-15-stock-monitoring.md) мог переиспользовать без
    # повторной миграции.
    # ========================================================================
    conn.execute("""
        CREATE TABLE IF NOT EXISTS moysklad_store_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store_id INTEGER NOT NULL REFERENCES stores(id),
            moysklad_store_id TEXT NOT NULL,
            moysklad_store_href TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(store_id),
            UNIQUE(moysklad_store_id)
        )
    """)

    # ========================================================================
    # Связка: пользователь дашборда <-> сотрудник + отдел МойСклад
    #
    # Явная связка, а не вывод отдела по городу точки — люди (особенно
    # флористы) меняются, справочник заполняется постепенно и независимо
    # от того, заведён ли у товарища ещё аккаунт-сотрудник в МойСклад.
    # Без записи в этой таблице create_loss() просто не проставляет
    # owner/group — МойСклад подставит дефолт (токен API, "Основной").
    # ========================================================================
    conn.execute("""
        CREATE TABLE IF NOT EXISTS moysklad_employee_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            moysklad_employee_id TEXT NOT NULL,
            moysklad_employee_href TEXT NOT NULL,
            moysklad_group_id TEXT NOT NULL,
            moysklad_group_href TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(username)
        )
    """)

    # ========================================================================
    # Заявки на списание
    #
    # 'processing' — переходное состояние во время отправки в МойСклад,
    # захватывается атомарным UPDATE ... WHERE status = 'on_approval'/'failed'
    # (см. approve_writeoff/retry_writeoff) — защита от повторного клика
    # "Согласовать" и от гонки approve/reject/retry на одной заявке.
    # ========================================================================
    conn.execute("""
        CREATE TABLE IF NOT EXISTS writeoffs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store_id INTEGER NOT NULL REFERENCES stores(id),
            status TEXT NOT NULL DEFAULT 'on_approval'
                CHECK (status IN ('on_approval','processing','sent','failed','rejected','cancelled')),
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            approved_by TEXT,
            approved_at TEXT,
            rejected_by TEXT,
            rejected_reason TEXT,
            moysklad_loss_id TEXT,
            moysklad_error TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_writeoffs_store ON writeoffs(store_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_writeoffs_status ON writeoffs(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_writeoffs_created ON writeoffs(created_at DESC)")

    # ========================================================================
    # Позиции заявки (несколько товаров в одной заявке)
    # ========================================================================
    conn.execute("""
        CREATE TABLE IF NOT EXISTS writeoff_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            writeoff_id INTEGER NOT NULL REFERENCES writeoffs(id),
            moysklad_product_id TEXT NOT NULL,
            moysklad_product_href TEXT NOT NULL,
            product_name TEXT NOT NULL,
            quantity REAL NOT NULL CHECK (quantity > 0),
            reason TEXT
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_writeoff_positions_writeoff
        ON writeoff_positions(writeoff_id)
    """)

    # ========================================================================
    # Фото списанного товара — по одной позиции, а не по заявке в целом
    # ========================================================================
    conn.execute("""
        CREATE TABLE IF NOT EXISTS writeoff_attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            position_id INTEGER NOT NULL REFERENCES writeoff_positions(id),
            original_filename TEXT NOT NULL,
            stored_filename TEXT NOT NULL,
            uploaded_by TEXT NOT NULL,
            uploaded_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_writeoff_attachments_position
        ON writeoff_attachments(position_id)
    """)

    conn.commit()
    conn.close()


# ============================================================================
# Связка складов
# ============================================================================

def link_moysklad_store(store_id: int, moysklad_store_id: str, moysklad_store_href: str) -> None:
    """Сопоставить точку продаж складу МойСклад (перезаписывает существующую связку для точки)."""
    conn = get_db()
    conn.execute(
        """
        INSERT INTO moysklad_store_links (store_id, moysklad_store_id, moysklad_store_href)
        VALUES (?, ?, ?)
        ON CONFLICT(store_id) DO UPDATE SET
            moysklad_store_id = excluded.moysklad_store_id,
            moysklad_store_href = excluded.moysklad_store_href
        """,
        (store_id, moysklad_store_id, moysklad_store_href),
    )
    conn.commit()
    conn.close()


def get_moysklad_store(store_id: int) -> Optional[Dict[str, Any]]:
    """Получить связку склада МойСклад для точки продаж (или None, если не сопоставлена)."""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM moysklad_store_links WHERE store_id = ?", (store_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def list_moysklad_store_links() -> List[Dict[str, Any]]:
    """Получить все связки складов (для админ-скрипта/проверки)."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM moysklad_store_links ORDER BY store_id"
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


# ============================================================================
# Связка сотрудников (пользователь дашборда -> сотрудник + отдел МойСклад)
# ============================================================================

def link_moysklad_employee(
    username: str,
    moysklad_employee_id: str,
    moysklad_employee_href: str,
    moysklad_group_id: str,
    moysklad_group_href: str,
) -> None:
    """Сопоставить пользователя дашборда сотруднику и отделу МойСклад (перезаписывает существующую связку)."""
    conn = get_db()
    conn.execute(
        """
        INSERT INTO moysklad_employee_links
            (username, moysklad_employee_id, moysklad_employee_href, moysklad_group_id, moysklad_group_href)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(username) DO UPDATE SET
            moysklad_employee_id = excluded.moysklad_employee_id,
            moysklad_employee_href = excluded.moysklad_employee_href,
            moysklad_group_id = excluded.moysklad_group_id,
            moysklad_group_href = excluded.moysklad_group_href
        """,
        (username, moysklad_employee_id, moysklad_employee_href, moysklad_group_id, moysklad_group_href),
    )
    conn.commit()
    conn.close()


def get_moysklad_employee(username: str) -> Optional[Dict[str, Any]]:
    """Получить связку сотрудника/отдела МойСклад для пользователя (или None, если не сопоставлен)."""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM moysklad_employee_links WHERE username = ?", (username,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def list_moysklad_employee_links() -> List[Dict[str, Any]]:
    """Получить все связки сотрудников (для экрана сопоставления)."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM moysklad_employee_links ORDER BY username"
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


# ============================================================================
# Заявки на списание — создание и чтение
# ============================================================================

def create_writeoff(store_id: int, created_by: str, positions: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Создать заявку на списание с одной или несколькими позициями.

    positions: [{"moysklad_product_id", "moysklad_product_href", "product_name",
                 "quantity", "reason"}, ...] — минимум одна позиция.
    """
    if not positions:
        raise ValueError("Заявка на списание должна содержать хотя бы одну позицию")

    conn = get_db()
    try:
        cursor = conn.execute(
            "INSERT INTO writeoffs (store_id, created_by) VALUES (?, ?)",
            (store_id, created_by),
        )
        writeoff_id = cursor.lastrowid

        for pos in positions:
            conn.execute(
                """
                INSERT INTO writeoff_positions
                    (writeoff_id, moysklad_product_id, moysklad_product_href, product_name, quantity, reason)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    writeoff_id,
                    pos["moysklad_product_id"],
                    pos["moysklad_product_href"],
                    pos["product_name"],
                    pos["quantity"],
                    pos.get("reason"),
                ),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return get_writeoff_by_id(writeoff_id)


def get_writeoff_by_id(writeoff_id: int) -> Optional[Dict[str, Any]]:
    """Заявка с вложенными позициями (и вложениями к каждой позиции)."""
    conn = get_db()
    row = conn.execute("SELECT * FROM writeoffs WHERE id = ?", (writeoff_id,)).fetchone()
    if not row:
        conn.close()
        return None

    writeoff = dict(row)
    position_rows = conn.execute(
        "SELECT * FROM writeoff_positions WHERE writeoff_id = ? ORDER BY id",
        (writeoff_id,),
    ).fetchall()

    positions = []
    for prow in position_rows:
        position = dict(prow)
        attachment_rows = conn.execute(
            "SELECT * FROM writeoff_attachments WHERE position_id = ? ORDER BY uploaded_at",
            (position["id"],),
        ).fetchall()
        position["attachments"] = [dict(a) for a in attachment_rows]
        positions.append(position)

    writeoff["positions"] = positions
    conn.close()
    return writeoff


def get_writeoff_positions(writeoff_id: int) -> List[Dict[str, Any]]:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM writeoff_positions WHERE writeoff_id = ? ORDER BY id",
        (writeoff_id,),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def list_writeoffs(
    store_ids: Optional[List[int]] = None,
    status: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """
    Список заявок с фильтрами. store_ids=None означает "без ограничения по точкам"
    (роль admin) — передавайте [] явно, если нужно гарантированно пустой результат.
    """
    query = "SELECT * FROM writeoffs WHERE 1=1"
    params: List[Any] = []

    if store_ids is not None:
        if not store_ids:
            return []
        placeholders = ",".join("?" * len(store_ids))
        query += f" AND store_id IN ({placeholders})"
        params.extend(store_ids)

    if status:
        query += " AND status = ?"
        params.append(status)

    if date_from:
        query += " AND created_at >= ?"
        params.append(date_from)

    if date_to:
        # Дашборд присылает конец суток по часам сотрудника, пересчитанный в
        # UTC (dayEndUtc в datetime.js). Голую дату (YYYY-MM-DD) достраиваем до
        # конца дня сами: created_at <= '2026-08-20' отсекло бы все заявки
        # этого дня, заведённые позже полуночи.
        if len(date_to.strip()) == 10:
            query += " AND created_at < datetime(?, '+1 day')"
        else:
            query += " AND created_at <= ?"
        params.append(date_to)

    query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    conn = get_db()
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(row) for row in rows]


# ============================================================================
# Заявки на списание — переходы статуса (все атомарные: UPDATE ... WHERE status = ...)
# ============================================================================

def _atomic_status_transition(writeoff_id: int, from_statuses: tuple, updates: Dict[str, Any]) -> bool:
    """
    Атомарно перевести заявку из одного из from_statuses в новый статус.
    Возвращает False, если заявка уже не в одном из ожидаемых статусов —
    вызывающий код должен трактовать это как "кто-то другой уже её обработал",
    а не как ошибку.
    """
    conn = get_db()
    placeholders = ",".join("?" * len(from_statuses))
    set_clause = ", ".join(f"{key} = ?" for key in updates)
    params = list(updates.values()) + [writeoff_id, *from_statuses]

    cursor = conn.execute(
        f"UPDATE writeoffs SET {set_clause} WHERE id = ? AND status IN ({placeholders})",
        params,
    )
    conn.commit()
    changed = cursor.rowcount == 1
    conn.close()
    return changed


def cancel_writeoff(writeoff_id: int, username: str) -> bool:
    """Флорист отменяет свою же заявку, пока управляющий её не рассмотрел."""
    conn = get_db()
    row = conn.execute(
        "SELECT created_by FROM writeoffs WHERE id = ? AND status = 'on_approval'",
        (writeoff_id,),
    ).fetchone()
    conn.close()
    if not row or row["created_by"] != username:
        return False
    return _atomic_status_transition(writeoff_id, ("on_approval",), {"status": "cancelled"})


def lock_writeoff_for_sending(writeoff_id: int, approved_by: str) -> bool:
    """
    Захватить заявку под отправку в МойСклад: on_approval -> processing.
    True — захват удался, можно (и нужно) вызывать create_loss().
    False — заявка уже обработана (согласована/отклонена/отменена кем-то ещё) — ничего не отправлять.

    Не через _atomic_status_transition, т.к. approved_at нужен datetime('now') —
    SQL-выражение, а не Python-значение параметра.
    """
    conn = get_db()
    cursor = conn.execute(
        """
        UPDATE writeoffs
        SET status = 'processing', approved_by = ?, approved_at = datetime('now')
        WHERE id = ? AND status = 'on_approval'
        """,
        (approved_by, writeoff_id),
    )
    conn.commit()
    changed = cursor.rowcount == 1
    conn.close()
    return changed


def lock_writeoff_for_retry(writeoff_id: int) -> bool:
    """Захватить упавшую заявку под повторную отправку: failed -> processing."""
    return _atomic_status_transition(writeoff_id, ("failed",), {"status": "processing"})


def mark_writeoff_sent(writeoff_id: int, moysklad_loss_id: str) -> None:
    conn = get_db()
    conn.execute(
        "UPDATE writeoffs SET status = 'sent', moysklad_loss_id = ?, moysklad_error = NULL WHERE id = ?",
        (moysklad_loss_id, writeoff_id),
    )
    conn.commit()
    conn.close()


def mark_writeoff_failed(writeoff_id: int, error: str) -> None:
    conn = get_db()
    conn.execute(
        "UPDATE writeoffs SET status = 'failed', moysklad_error = ? WHERE id = ?",
        (error, writeoff_id),
    )
    conn.commit()
    conn.close()


def reject_writeoff(writeoff_id: int, rejected_by: str, reason: Optional[str] = None) -> bool:
    return _atomic_status_transition(
        writeoff_id,
        ("on_approval",),
        {"status": "rejected", "rejected_by": rejected_by, "rejected_reason": reason},
    )


# ============================================================================
# Вложения (фото списанного товара) — паттерн 1:1 с invoices/invoice_attachments,
# но привязаны к позиции заявки, а не к заявке целиком.
# ============================================================================

def add_writeoff_attachment(
    position_id: int, original_filename: str, file_bytes: bytes, uploaded_by: str
) -> Dict[str, Any]:
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
        INSERT INTO writeoff_attachments (position_id, original_filename, stored_filename, uploaded_by)
        VALUES (?, ?, ?, ?)
        """,
        (position_id, original_filename, stored_filename, uploaded_by),
    )
    attachment_id = cursor.lastrowid
    conn.commit()
    row = conn.execute("SELECT * FROM writeoff_attachments WHERE id = ?", (attachment_id,)).fetchone()
    conn.close()

    return {"ok": True, "error": None, "attachment": dict(row)}


def get_writeoff_attachments(position_id: int) -> List[Dict[str, Any]]:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM writeoff_attachments WHERE position_id = ? ORDER BY uploaded_at",
        (position_id,),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_writeoff_attachment_by_id(attachment_id: int) -> Optional[Dict[str, Any]]:
    conn = get_db()
    row = conn.execute("SELECT * FROM writeoff_attachments WHERE id = ?", (attachment_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_writeoff_position_by_id(position_id: int) -> Optional[Dict[str, Any]]:
    conn = get_db()
    row = conn.execute("SELECT * FROM writeoff_positions WHERE id = ?", (position_id,)).fetchone()
    conn.close()
    return dict(row) if row else None
