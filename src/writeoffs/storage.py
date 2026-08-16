"""
Модуль работы с SQLite для списаний товара БАРХАТ.

Точка продаж (=салон) — переиспользуем таблицу stores из cashshifts,
чтобы не дублировать справочник. Доступ к точкам — через cashshifts.check_store_access.
"""

import logging
import os
import sqlite3
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Путь к БД из переменной окружения или дефолт — та же база, что у auth/cashshifts/invoices
DB_PATH = os.environ.get("BARHAT_DB_PATH", "barhat.db")

# Куда сохранять вложения (фото списанного товара). Отдельная переменная окружения,
# чтобы не зависеть от того, где смонтирован постоянный диск на Amvera.
ATTACHMENTS_DIR = os.environ.get("WRITEOFF_ATTACHMENTS_DIR", "writeoff_attachments")

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
