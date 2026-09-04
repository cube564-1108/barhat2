"""
Витрина негативной обратной связи (форма Pyrus 1291124 «Негативная ОС по заказу»).

Устроена как витрина качества (quality.py): одна строка на задачу, поля формы
разложены по колонкам, индексы по дате и салону. Заполняется при синхронизации,
а не при чтении, — разбирать JSON тысяч задач на каждый запрос к отчёту нельзя,
на сетевом диске Amvera это секунды.

Показатель раздела «Показатели салонов» — количество ПОДТВЕРЖДЁННЫХ обращений:
объективность проставляет человек при разборе, и пока он этого не сделал,
обращение висит в «Необходимо разбираться». Эти две цифры показываются рядом:
иначе неразобранная очередь выглядит как отсутствие проблем.
"""

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from sqlite_conn import connect as sqlite_connect

logger = logging.getLogger(__name__)

NOS_FORM_ID = 1291124

# ID полей формы (сверено с /forms/1291124 2026-09-04)
FIELD_DATE = 2           # Дата негативной ОС
FIELD_ORDER = 5          # Номер заказа в CRM
FIELD_SALON = 16         # Салон
FIELD_CATEGORY = 18      # Категория ОС
FIELD_SOURCE = 20        # Откуда претензия
FIELD_OBJECTIVITY = 22   # Объективность проблемы
FIELD_STATUS = 26        # Статус работы с отзывом
FIELD_CRITICAL = 28      # Критично
FIELD_COMPENSATION = 15  # Компенсация

# Значения поля «Объективность проблемы».
# ВНИМАНИЕ: в Pyrus они приходят с висячим пробелом («Подтверждено »), поэтому
# при записи значение обязательно проходит strip(). Сравнение по сырой строке
# молча даёт ноль подтверждённых — показатель выглядит как «жалоб нет».
OBJECTIVITY_CONFIRMED = "Подтверждено"
OBJECTIVITY_IN_REVIEW = "Необходимо разбираться"

_projection_lock = threading.Lock()
_projection_checked = False


def _db_path() -> str:
    return os.getenv("PYRUS_DB_PATH", "data/pyrus.db")


def _connect() -> sqlite3.Connection:
    return sqlite_connect(_db_path(), timeout=20)


def init_nos_tables() -> None:
    """Создать витрину (идемпотентно)."""
    conn = _connect()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS nos_feedback (
                task_id INTEGER PRIMARY KEY,
                form_id INTEGER NOT NULL,
                feedback_date TEXT,
                salon TEXT,
                category TEXT,
                source TEXT,
                objectivity TEXT,
                status TEXT,
                critical INTEGER NOT NULL DEFAULT 0,
                order_number TEXT,
                compensation TEXT,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_nos_date ON nos_feedback(feedback_date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_nos_salon ON nos_feedback(salon)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_nos_objectivity ON nos_feedback(objectivity)"
        )
        conn.commit()
    finally:
        conn.close()


# ===== Разбор задачи =====

def _choice(value) -> Optional[str]:
    """Значение поля-выбора. strip() обязателен — см. комментарий у констант."""
    if isinstance(value, dict):
        names = value.get("choice_names")
        if names and names[0] is not None:
            return str(names[0]).strip()
        return None
    if value is None:
        return None
    return str(value).strip() or None


def extract_task_data(task: Dict) -> Optional[Dict]:
    """
    Достать из задачи поля обращения.

    Returns:
        Словарь полей или None, если у задачи нет ни даты, ни даты создания —
        отнести её к периоду невозможно.
    """
    data = {
        "task_id": task.get("id"),
        "date": None,
        "salon": None,
        "category": None,
        "source": None,
        "objectivity": None,
        "status": None,
        "critical": 0,
        "order_number": None,
        "compensation": None,
    }

    for field in task.get("fields", []):
        field_id = field.get("id")
        value = field.get("value")

        if field_id == FIELD_DATE:
            data["date"] = value
        elif field_id == FIELD_SALON:
            data["salon"] = _choice(value)
        elif field_id == FIELD_CATEGORY:
            data["category"] = _choice(value)
        elif field_id == FIELD_SOURCE:
            data["source"] = _choice(value)
        elif field_id == FIELD_OBJECTIVITY:
            data["objectivity"] = _choice(value)
        elif field_id == FIELD_STATUS:
            data["status"] = _choice(value)
        elif field_id == FIELD_COMPENSATION:
            data["compensation"] = _choice(value)
        elif field_id == FIELD_ORDER:
            data["order_number"] = str(value).strip() if value is not None else None
        elif field_id == FIELD_CRITICAL:
            # checkmark приходит как 'checked' / 'unchecked'
            data["critical"] = 1 if str(value).strip().lower() == "checked" else 0

    if not data["date"] and task.get("create_date"):
        # Дату проставляют руками и иногда забывают — тогда считаем по дате
        # создания задачи, как в витрине качества
        data["date"] = task["create_date"][:10]

    if not data["task_id"] or not data["date"]:
        return None

    data["date"] = str(data["date"])[:10]
    return data


# ===== Запись =====

def _upsert_rows(cursor, tasks: List[Dict]) -> Tuple[int, int]:
    saved = 0
    skipped = 0

    for task in tasks:
        task_id = task.get("id")
        if task_id is None:
            continue

        parsed = extract_task_data(task)
        if not parsed:
            cursor.execute("DELETE FROM nos_feedback WHERE task_id = ?", (task_id,))
            skipped += 1
            continue

        cursor.execute("""
            INSERT OR REPLACE INTO nos_feedback
            (task_id, form_id, feedback_date, salon, category, source, objectivity,
             status, critical, order_number, compensation, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """, (
            parsed["task_id"], NOS_FORM_ID, parsed["date"], parsed["salon"], parsed["category"],
            parsed["source"], parsed["objectivity"], parsed["status"], parsed["critical"],
            parsed["order_number"], parsed["compensation"],
        ))
        saved += 1

    return saved, skipped


def upsert_tasks(tasks: List[Dict]) -> int:
    """Обновить витрину пачкой задач — одна транзакция на пачку."""
    if not tasks:
        return 0

    conn = _connect()
    try:
        cursor = conn.cursor()
        saved, skipped = _upsert_rows(cursor, tasks)
        conn.commit()
        if skipped:
            logger.info(f"Витрина НОС: записано {saved}, без даты {skipped}")
        return saved
    except Exception as e:
        conn.rollback()
        logger.error(f"Ошибка обновления витрины НОС: {e}")
        return 0
    finally:
        conn.close()


def rebuild_projection() -> Dict:
    """Пересобрать витрину из latest_tasks (первый запуск, смена разбора полей)."""
    started = datetime.utcnow()
    conn = _connect()
    try:
        cursor = conn.cursor()
        rows = cursor.execute(
            "SELECT raw_data FROM latest_tasks WHERE form_id = ?", (NOS_FORM_ID,)
        ).fetchall()

        tasks = []
        for row in rows:
            try:
                tasks.append(json.loads(row["raw_data"]))
            except (TypeError, ValueError):
                continue

        cursor.execute("DELETE FROM nos_feedback WHERE form_id = ?", (NOS_FORM_ID,))
        saved, skipped = _upsert_rows(cursor, tasks)
        conn.commit()

        seconds = round((datetime.utcnow() - started).total_seconds(), 2)
        logger.info(f"Витрина НОС пересобрана: {saved} обращений из {len(tasks)} задач за {seconds} с")
        return {"tasks": len(tasks), "rows": saved, "skipped": skipped, "seconds": seconds}
    except Exception as e:
        conn.rollback()
        logger.error(f"Ошибка пересборки витрины НОС: {e}")
        raise
    finally:
        conn.close()


def ensure_projection() -> None:
    """Один раз на процесс: витрина пуста, а задачи формы в базе есть — собрать."""
    global _projection_checked

    with _projection_lock:
        if _projection_checked:
            return
        _projection_checked = True

    try:
        init_nos_tables()
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT COUNT(*) FROM nos_feedback WHERE form_id = ?", (NOS_FORM_ID,)
            ).fetchone()[0]
            tasks = conn.execute(
                "SELECT COUNT(*) FROM latest_tasks WHERE form_id = ?", (NOS_FORM_ID,)
            ).fetchone()[0]
        finally:
            conn.close()

        if rows == 0 and tasks > 0:
            from .storage import get_storage
            store = get_storage()
            if store.try_acquire_sync_lock("nos-projection", 600):
                try:
                    rebuild_projection()
                finally:
                    store.release_sync_lock("nos-projection")
            else:
                logger.info("Витрину НОС собирает другой воркер — пропускаем")
    except Exception as e:
        logger.error(f"Не удалось подготовить витрину НОС: {e}")


# ===== Чтение =====

def counts_by_salon(date_from: str, date_to: str) -> Dict[str, Dict]:
    """
    Обращения за период по салонам (ключ — название салона В ФОРМЕ, не store_id:
    сопоставление салонов живёт в salonkpi и здесь про него знать незачем).

    confirmed — объективность «Подтверждено», in_review — «Необходимо разбираться»,
    other — разобрано и не подтверждено, unset — объективность не проставлена.
    """
    conn = _connect()
    try:
        rows = conn.execute("""
            SELECT COALESCE(salon, '') AS salon,
                   COALESCE(objectivity, '') AS objectivity,
                   COALESCE(category, 'Без категории') AS category,
                   COUNT(*) AS cnt,
                   SUM(critical) AS critical
            FROM nos_feedback
            WHERE form_id = ? AND feedback_date >= ? AND feedback_date <= ?
            GROUP BY salon, objectivity, category
        """, (NOS_FORM_ID, date_from, date_to)).fetchall()
    finally:
        conn.close()

    result: Dict[str, Dict] = {}
    for row in rows:
        cell = result.setdefault(row["salon"], {
            "confirmed": 0, "in_review": 0, "other": 0, "unset": 0,
            "total": 0, "critical": 0, "categories": {},
        })
        cnt = row["cnt"]
        objectivity = row["objectivity"]

        if objectivity == OBJECTIVITY_CONFIRMED:
            cell["confirmed"] += cnt
            # Категории считаем только по подтверждённым: неразобранное обращение
            # ещё может оказаться необоснованным, и складывать их в одну кучу
            # значит завышать проблему салона
            cell["categories"][row["category"]] = cell["categories"].get(row["category"], 0) + cnt
        elif objectivity == OBJECTIVITY_IN_REVIEW:
            cell["in_review"] += cnt
        elif objectivity:
            cell["other"] += cnt
        else:
            cell["unset"] += cnt

        cell["total"] += cnt
        cell["critical"] += row["critical"] or 0

    return result


def list_salons(date_from: str, date_to: str) -> List[Dict]:
    """Салоны, встреченные в обращениях за период, — для поиска несопоставленных."""
    conn = _connect()
    try:
        rows = conn.execute("""
            SELECT COALESCE(salon, '') AS salon, COUNT(*) AS cnt
            FROM nos_feedback
            WHERE form_id = ? AND feedback_date >= ? AND feedback_date <= ?
            GROUP BY salon
        """, (NOS_FORM_ID, date_from, date_to)).fetchall()
    finally:
        conn.close()
    return [{"key": r["salon"], "count": r["cnt"]} for r in rows if r["salon"]]


def data_range() -> Dict[str, Optional[str]]:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT MIN(feedback_date) AS since, MAX(feedback_date) AS until "
            "FROM nos_feedback WHERE form_id = ?", (NOS_FORM_ID,)
        ).fetchone()
    finally:
        conn.close()
    return {"since": row["since"], "until": row["until"]}
