"""
Модуль работы с SQLite для раздела «Задачи» на дашборде БАРХАТ.

Личный бэклог владельца: идеи и фичи, которые планируются или уже в работе,
со свободным текстовым полем "на чём остановились". Раздел не отдельный пункт
меню — встроен в существующую страницу /dashboard, доступ только у admin
(гейтится в server.py через role_required("admin"), не через section_required).
"""

import logging
import os
import sqlite3
from typing import Any, Dict, List, Optional

from sqlite_conn import connect as sqlite_connect

logger = logging.getLogger(__name__)

# Путь к БД из переменной окружения или дефолт — та же база, что у auth/cashshifts/invoices
DB_PATH = os.environ.get("BARHAT_DB_PATH", "barhat.db")

STATUSES = ("idea", "in_progress", "done")

# Задачи, с которых стартует раздел (см. plans/2026-08-15-dashboard-tasks-tracker.md)
SEED_TASKS = [
    {
        "title": "ABC-анализ товаров",
        "description": "Модуль ABC-анализа товаров на основе данных МойСклад.",
        "status": "in_progress",
        "progress_notes": "Модуль подключён к проду и фронтенду. Осталось добавить переменные окружения МойСклад в Амвера.",
    },
    {
        "title": "Контроль остатков с уведомлениями управляющим",
        "description": "Мониторинг остатков товаров на складах МойСклад и уведомления управляющим салонов о снижающихся остатках.",
        "status": "idea",
        "progress_notes": "Черновой план: plans/2026-08-15-stock-monitoring.md",
    },
    {
        "title": "Форма списания для салонов",
        "description": "Форма для списания товаров салонами, предварительно через API МойСклад.",
        "status": "idea",
        "progress_notes": None,
    },
    {
        "title": "Модуль согласования счетов",
        "description": "Согласование счетов на оплату вместо формы в Pyrus.",
        "status": "in_progress",
        "progress_notes": "Текущий статус — см. plans/2026-08-14-invoice-approval-automation.md",
    },
]


def get_db():
    """
    Timeout увеличен против дефолтных 5с — на проде gunicorn поднимает несколько
    воркеров (amvera.yml), пишущих в один файл SQLite; без запаса можно словить
    "database is locked" вместо того, чтобы дождаться своей очереди.
    """
    return sqlite_connect(DB_PATH, timeout=20)


def init_tasks_tables():
    """Инициализация таблицы задач (вызывается при старте приложения)."""
    conn = get_db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS dashboard_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT,
            status TEXT NOT NULL DEFAULT 'idea' CHECK (status IN ('idea','in_progress','done')),
            progress_notes TEXT,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            completed_at TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dashboard_tasks_status ON dashboard_tasks(status)")
    conn.commit()

    _seed_tasks_if_empty(conn)

    conn.close()


def _seed_tasks_if_empty(conn: sqlite3.Connection):
    """Заполнить таблицу стартовыми задачами, если она пуста (однократно, идемпотентно)."""
    result = conn.execute("SELECT COUNT(*) as count FROM dashboard_tasks").fetchone()
    if result["count"] > 0:
        return

    logger.info("Заполнение dashboard_tasks стартовыми задачами...")
    for task in SEED_TASKS:
        conn.execute(
            """
            INSERT INTO dashboard_tasks (title, description, status, progress_notes, created_by)
            VALUES (?, ?, ?, ?, 'system')
            """,
            (task["title"], task["description"], task["status"], task["progress_notes"])
        )
    conn.commit()
    logger.info(f"Добавлено {len(SEED_TASKS)} задач")


def create_task(
    title: str,
    created_by: str,
    description: Optional[str] = None,
    status: str = "idea",
    progress_notes: Optional[str] = None,
) -> Dict[str, Any]:
    conn = get_db()
    cursor = conn.execute(
        """
        INSERT INTO dashboard_tasks (title, description, status, progress_notes, created_by)
        VALUES (?, ?, ?, ?, ?)
        """,
        (title, description, status, progress_notes, created_by)
    )
    task_id = cursor.lastrowid
    conn.commit()
    row = conn.execute("SELECT * FROM dashboard_tasks WHERE id = ?", (task_id,)).fetchone()
    conn.close()
    return dict(row)


def get_task_by_id(task_id: int) -> Optional[Dict[str, Any]]:
    conn = get_db()
    row = conn.execute("SELECT * FROM dashboard_tasks WHERE id = ?", (task_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_tasks(status: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Список задач. Активные (idea/in_progress) — сверху по последнему обновлению,
    выполненные (done) — внизу, чтобы актуальная работа не терялась под архивом.
    """
    query = "SELECT * FROM dashboard_tasks"
    params: List[Any] = []

    if status:
        query += " WHERE status = ?"
        params.append(status)

    query += " ORDER BY (status = 'done') ASC, updated_at DESC"

    conn = get_db()
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def update_task(
    task_id: int,
    title: Optional[str] = None,
    description: Optional[str] = None,
    progress_notes: Optional[str] = None,
) -> bool:
    """Частичное обновление редактируемых полей (не трогает статус — для этого set_task_status)."""
    conn = get_db()
    if not conn.execute("SELECT 1 FROM dashboard_tasks WHERE id = ?", (task_id,)).fetchone():
        conn.close()
        return False

    fields = []
    params: List[Any] = []
    if title is not None:
        fields.append("title = ?")
        params.append(title)
    if description is not None:
        fields.append("description = ?")
        params.append(description)
    if progress_notes is not None:
        fields.append("progress_notes = ?")
        params.append(progress_notes)

    if fields:
        fields.append("updated_at = datetime('now')")
        params.append(task_id)
        conn.execute(f"UPDATE dashboard_tasks SET {', '.join(fields)} WHERE id = ?", params)
        conn.commit()

    conn.close()
    return True


def set_task_status(task_id: int, status: str) -> bool:
    """Сменить статус. При переходе в 'done' проставляет completed_at, иначе — чистит."""
    if status not in STATUSES:
        return False

    conn = get_db()
    if not conn.execute("SELECT 1 FROM dashboard_tasks WHERE id = ?", (task_id,)).fetchone():
        conn.close()
        return False

    conn.execute(
        """
        UPDATE dashboard_tasks
        SET status = ?,
            updated_at = datetime('now'),
            completed_at = CASE WHEN ? = 'done' THEN datetime('now') ELSE NULL END
        WHERE id = ?
        """,
        (status, status, task_id)
    )
    conn.commit()
    conn.close()
    return True


def delete_task(task_id: int) -> bool:
    conn = get_db()
    if not conn.execute("SELECT 1 FROM dashboard_tasks WHERE id = ?", (task_id,)).fetchone():
        conn.close()
        return False
    conn.execute("DELETE FROM dashboard_tasks WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()
    return True
