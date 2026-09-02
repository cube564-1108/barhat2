"""
Хранилище обратной связи от сотрудников.

Пользователь из любого экрана дашборда отправляет «что мешает», владелец
разбирает поток и превращает принятое в задачу бэклога (dashboard_tasks).

БАЗА ОБЩАЯ (barhat.db), а не своя, — намеренно:
  * `to_task` связывает обращение с `dashboard_tasks`, которая живёт здесь же;
  * счётчики для бейджа читаются рядом с учётками, без второго файла на
    медленном /data.
Пишем сюда единицы строк в день, фоновых синков у модуля нет — то есть той
нагрузки, из-за которой linkwatch и moysklad вынесли в отдельные файлы, тут
не возникает.

Приватность (решение из плана): человек видит только свои обращения. Чужие
показываются лишь как подсказка «похоже на это» при вводе — и только те, что
владелец уже разобрал (status != 'new'). Иначе первая же жалоба на коллегу
уедет к другому сотруднику. Имена авторов в подсказках не отдаются никогда.
"""

import json
import logging
import os
import re
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

from sqlite_conn import connect as sqlite_connect

logger = logging.getLogger(__name__)

# Та же база, что у auth/tasks/cashshifts/invoices
DB_PATH = os.environ.get("BARHAT_DB_PATH", "barhat.db")

# Тип обращения. Порядок важен: он же задаёт очерёдность разбора.
TYPE_BUG = "bug"                    # не работает
TYPE_INCONVENIENCE = "inconvenience"  # неудобно
TYPE_WISH = "wish"                  # хочу добавить
TYPES = (TYPE_BUG, TYPE_INCONVENIENCE, TYPE_WISH)

STATUS_NEW = "new"
STATUS_ACCEPTED = "accepted"
STATUS_DONE = "done"
STATUS_REJECTED = "rejected"
STATUS_NEED_INFO = "need_info"
STATUSES = (STATUS_NEW, STATUS_ACCEPTED, STATUS_DONE, STATUS_REJECTED, STATUS_NEED_INFO)

# Статусы, при которых владелец обязан оставить текст автору: отклонение без
# причины и вопрос без вопроса — это молчание, от которого поток и умирает.
STATUSES_REQUIRING_RESOLUTION = (STATUS_REJECTED, STATUS_NEED_INFO)

# Сколько обращений в час принимаем от одного человека. Это не инвариант,
# а вежливый лимит от случайной пачки повторов — гонку тут ловить не нужно
# (см. критику фазы 1 в плане: BEGIN IMMEDIATE берёт write-лок общей базы,
# на которой держится логин).
RATE_LIMIT_PER_HOUR = 10

MAX_TEXT_LENGTH = 4000
MAX_CONTEXT_LENGTH = 8000


@contextmanager
def get_db():
    """Соединение с общей базой. Коммит на выходе, откат и закрытие — всегда.

    Функция вида `conn = get_db(); ...; conn.close()` теряет соединение на любой
    ошибке, а незакрытое соединение с неоткатанной транзакцией держит write-лок
    barhat.db до перезапуска воркера — то есть роняет вход в дашборд всем сразу.
    """
    conn = sqlite_connect(DB_PATH, timeout=20)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_feedback_tables() -> None:
    """Создать таблицы. Зовётся при старте приложения, идемпотентна.

    Под gunicorn воркеры стартуют одновременно и могут столкнуться на CREATE
    INDEX — «database is locked» здесь не повод ронять воркер: таблицы либо уже
    есть, либо их доделает сосед.
    """
    try:
        with get_db() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS feedback_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    type TEXT NOT NULL CHECK (type IN ('bug','inconvenience','wish')),
                    module TEXT,
                    page_url TEXT,
                    text TEXT NOT NULL,
                    wish TEXT,
                    status TEXT NOT NULL DEFAULT 'new'
                        CHECK (status IN ('new','accepted','done','rejected','need_info')),
                    author_username TEXT NOT NULL,
                    author_role TEXT,
                    client_context TEXT,
                    resolution TEXT,
                    resolved_at TEXT,
                    status_changed_at TEXT,
                    status_seen_by_author_at TEXT,
                    merged_into_id INTEGER,
                    task_id INTEGER,
                    seen_by_owner_at TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_feedback_status ON feedback_items(status)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_feedback_author "
                "ON feedback_items(author_username, created_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_feedback_module ON feedback_items(module)"
            )

            # Поддержка «у меня тоже». Составной первичный ключ — та самая
            # преграда на уровне БД: один человек не может поддержать дважды,
            # даже если появится новый путь записи.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS feedback_supporters (
                    feedback_id INTEGER NOT NULL,
                    username TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    PRIMARY KEY (feedback_id, username)
                )
                """
            )
        logger.info("Таблицы обратной связи готовы")
    except Exception as e:
        logger.error(f"Не удалось инициализировать таблицы обратной связи: {e}")


# ---------------------------------------------------------------------------
# Чтение
# ---------------------------------------------------------------------------

# Число людей = автор + поддержавшие. Считаем подзапросом, чтобы не делать
# второй заход в базу на каждую карточку.
_SUPPORTERS_SQL = """
    (SELECT COUNT(*) FROM feedback_supporters s WHERE s.feedback_id = f.id) + 1
        AS people_count
"""


def get_item(item_id: int) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        row = conn.execute(
            f"SELECT f.*, {_SUPPORTERS_SQL} FROM feedback_items f WHERE f.id = ?",
            (item_id,),
        ).fetchone()
        return dict(row) if row else None


def list_by_author(username: str, limit: int = 50) -> List[Dict[str, Any]]:
    """Обращения одного человека — вкладка «Мои обращения»."""
    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT f.id, f.type, f.module, f.text, f.wish, f.status, f.resolution,
                   f.created_at, f.status_changed_at, f.task_id, {_SUPPORTERS_SQL}
              FROM feedback_items f
             WHERE f.author_username = ?
             ORDER BY f.created_at DESC
             LIMIT ?
            """,
            (username, limit),
        ).fetchall()
        return [dict(row) for row in rows]


def list_for_owner(
    status: Optional[str] = None,
    module: Optional[str] = None,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """Очередь разбора. Сначала «не работает», дальше по дате.

    Формулы веса здесь намеренно нет (см. критику фазы 6 в плане): при десятке
    обращений в неделю она вырождается в сортировку по типу.
    """
    query = f"""
        SELECT f.*, {_SUPPORTERS_SQL}
          FROM feedback_items f
         WHERE f.merged_into_id IS NULL
    """
    params: List[Any] = []

    if status:
        query += " AND f.status = ?"
        params.append(status)
    if module:
        query += " AND f.module = ?"
        params.append(module)

    query += """
         ORDER BY CASE f.type WHEN 'bug' THEN 0 WHEN 'inconvenience' THEN 1 ELSE 2 END,
                  f.created_at DESC
         LIMIT ?
    """
    params.append(limit)

    with get_db() as conn:
        rows = conn.execute(query, params).fetchall()
        items = [dict(row) for row in rows]

        # К объединённым обращениям подтягиваем, сколько под них подшили —
        # владельцу это единственный сигнал, что боль повторяется.
        for item in items:
            item["merged_count"] = conn.execute(
                "SELECT COUNT(*) FROM feedback_items WHERE merged_into_id = ?",
                (item["id"],),
            ).fetchone()[0]
        return items


def counters(username: str, is_owner: bool) -> Dict[str, int]:
    """Числа для бейджей: неразобранное у владельца, обновления статуса у автора.

    Одним соединением на оба счётчика — ручка лёгкая, но живёт на медленном
    диске, и открывать базу дважды ради двух чисел незачем.
    """
    with get_db() as conn:
        new_count = 0
        if is_owner:
            new_count = conn.execute(
                "SELECT COUNT(*) FROM feedback_items "
                " WHERE status = 'new' AND merged_into_id IS NULL"
            ).fetchone()[0]

        # Автору показываем точку, если владелец ответил после того, как автор
        # последний раз открывал вкладку «Мои обращения».
        updates = conn.execute(
            """
            SELECT COUNT(*) FROM feedback_items
             WHERE author_username = ?
               AND status != 'new'
               AND status_changed_at IS NOT NULL
               AND (status_seen_by_author_at IS NULL
                    OR status_seen_by_author_at < status_changed_at)
            """,
            (username,),
        ).fetchone()[0]

        return {"new_for_owner": new_count, "my_updates": updates}


def count_recent_by_author(username: str, hours: int = 1) -> int:
    with get_db() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM feedback_items "
            " WHERE author_username = ? AND created_at > datetime('now', ?)",
            (username, f"-{int(hours)} hours"),
        ).fetchone()[0]


# ---------------------------------------------------------------------------
# Поиск похожих (дедуп на вводе)
# ---------------------------------------------------------------------------

# Слова короче четырёх букв («это», «не», «при») ничего не различают, а союзы
# есть в каждом обращении. Обрезка до пяти символов — грубая замена стеммингу:
# «сохраняется» и «сохранить» дают общий корень «сохра».
_WORD_RE = re.compile(r"[a-zA-Zа-яА-ЯёЁ0-9]{4,}")
_STOP_WORDS = {
    "если", "чтобы", "когда", "нужно", "надо", "очень", "можно", "хочу",
    "после", "потом", "тоже", "этот", "эта", "там", "тут", "всё", "все",
    "будет", "было", "быть", "меня", "нас", "она", "они", "нельзя",
}


def _tokens(text: str) -> set:
    words = [w.lower() for w in _WORD_RE.findall(text or "")]
    return {w[:5] for w in words if w not in _STOP_WORDS}


def _similarity(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    common = a & b
    if len(common) < 2:
        return 0.0
    return len(common) / min(len(a), len(b))


def find_similar(
    text: str,
    module: Optional[str],
    username: str,
    limit: int = 4,
) -> List[Dict[str, Any]]:
    """Похожие обращения для подсказки при вводе.

    Кандидаты — только разобранные владельцем (status != 'new') плюс свои
    собственные. Причина в плане: приватность важнее мгновенного дедупа, а
    неразобранное обращение может содержать что угодно про коллег.
    Имя автора наружу не отдаётся.
    """
    query_tokens = _tokens(text)
    if len(query_tokens) < 2:
        return []

    sql = f"""
        SELECT f.id, f.type, f.text, f.status, f.author_username, {_SUPPORTERS_SQL}
          FROM feedback_items f
         WHERE f.merged_into_id IS NULL
           AND (f.status != 'new' OR f.author_username = ?)
    """
    params: List[Any] = [username]
    if module:
        sql += " AND f.module = ?"
        params.append(module)
    # Смотрим только свежий срез: обращение годовой давности как подсказка
    # бесполезно, а перебирать всю таблицу на медленном диске незачем.
    sql += " ORDER BY f.created_at DESC LIMIT 200"

    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()

    scored = []
    for row in rows:
        score = _similarity(query_tokens, _tokens(row["text"]))
        if score >= 0.34:
            scored.append((score, row))

    scored.sort(key=lambda pair: pair[0], reverse=True)

    result = []
    for score, row in scored[:limit]:
        result.append({
            "id": row["id"],
            "type": row["type"],
            "text": row["text"],
            "status": row["status"],
            "people_count": row["people_count"],
            "mine": row["author_username"] == username,
        })
    return result


# ---------------------------------------------------------------------------
# Запись
# ---------------------------------------------------------------------------

def create_item(
    author_username: str,
    author_role: str,
    type_: str,
    text: str,
    wish: Optional[str] = None,
    module: Optional[str] = None,
    page_url: Optional[str] = None,
    client_context: Optional[dict] = None,
) -> Dict[str, Any]:
    context_json = None
    if client_context:
        context_json = json.dumps(client_context, ensure_ascii=False)[:MAX_CONTEXT_LENGTH]

    wish_value = wish[:MAX_TEXT_LENGTH] if wish else None

    with get_db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO feedback_items
                (type, module, page_url, text, wish, author_username, author_role,
                 client_context)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                type_,
                module,
                page_url,
                text[:MAX_TEXT_LENGTH],
                wish_value,
                author_username,
                author_role,
                context_json,
            ),
        )
        item_id = cursor.lastrowid
        row = conn.execute(
            f"SELECT f.*, {_SUPPORTERS_SQL} FROM feedback_items f WHERE f.id = ?",
            (item_id,),
        ).fetchone()
        return dict(row)


def add_supporter(item_id: int, username: str) -> bool:
    """«У меня тоже». True — поддержка засчитана впервые, False — уже была.

    Дубль ловит первичный ключ, поэтому INSERT OR IGNORE безопасен без чтения
    перед записью: между SELECT и INSERT на проде помещаются сотни миллисекунд
    и все параллельные запросы разом.
    """
    with get_db() as conn:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO feedback_supporters (feedback_id, username) VALUES (?, ?)",
            (item_id, username),
        )
        if cursor.rowcount:
            conn.execute(
                "UPDATE feedback_items SET updated_at = datetime('now') WHERE id = ?",
                (item_id,),
            )
        return bool(cursor.rowcount)


def set_status(item_id: int, status: str, resolution: Optional[str] = None) -> bool:
    """Сменить статус и оставить ответ автору."""
    with get_db() as conn:
        if not conn.execute("SELECT 1 FROM feedback_items WHERE id = ?", (item_id,)).fetchone():
            return False

        conn.execute(
            """
            UPDATE feedback_items
               SET status = ?,
                   resolution = COALESCE(?, resolution),
                   resolved_at = CASE WHEN ? IN ('done','rejected')
                                      THEN datetime('now') ELSE resolved_at END,
                   status_changed_at = datetime('now'),
                   seen_by_owner_at = COALESCE(seen_by_owner_at, datetime('now')),
                   updated_at = datetime('now')
             WHERE id = ?
            """,
            (status, resolution, status, item_id),
        )
        return True


def merge_into(item_id: int, target_id: int) -> bool:
    """Подшить обращение к другому: дубль уходит из очереди, а его автор
    получает статус целевого обращения, когда владелец до него доберётся."""
    if item_id == target_id:
        return False

    with get_db() as conn:
        target = conn.execute(
            "SELECT id FROM feedback_items WHERE id = ? AND merged_into_id IS NULL",
            (target_id,),
        ).fetchone()
        if not target:
            return False
        if not conn.execute("SELECT 1 FROM feedback_items WHERE id = ?", (item_id,)).fetchone():
            return False

        conn.execute(
            """
            UPDATE feedback_items
               SET merged_into_id = ?, seen_by_owner_at = datetime('now'),
                   updated_at = datetime('now')
             WHERE id = ?
            """,
            (target_id, item_id),
        )
        # Автор дубля становится сторонником целевого обращения — иначе вес
        # «сколько людей просили» теряется вместе с записью.
        author = conn.execute(
            "SELECT author_username FROM feedback_items WHERE id = ?", (item_id,)
        ).fetchone()
        if author:
            conn.execute(
                "INSERT OR IGNORE INTO feedback_supporters (feedback_id, username) "
                "VALUES (?, ?)",
                (target_id, author["author_username"]),
            )
        return True


def link_task(item_id: int, task_id: int) -> bool:
    """Связать обращение с задачей бэклога.

    Вызывается ПОСЛЕ того, как задача создана и закоммичена своим соединением
    (tasks.storage открывает собственное). Вложенные соединения уже дважды
    вешали запись в общую базу — здесь их быть не должно.
    """
    with get_db() as conn:
        if not conn.execute("SELECT 1 FROM feedback_items WHERE id = ?", (item_id,)).fetchone():
            return False
        conn.execute(
            """
            UPDATE feedback_items
               SET task_id = ?, status = 'accepted', status_changed_at = datetime('now'),
                   seen_by_owner_at = COALESCE(seen_by_owner_at, datetime('now')),
                   updated_at = datetime('now')
             WHERE id = ?
            """,
            (task_id, item_id),
        )
        return True


def list_merged_children(item_id: int) -> List[Dict[str, Any]]:
    """Обращения, подшитые к этому. Нужны в экспорте промпта: они показывают,
    сколькими словами описана одна и та же боль."""
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, type, text, author_role, created_at
              FROM feedback_items WHERE merged_into_id = ? ORDER BY created_at
            """,
            (item_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def mark_author_seen(username: str) -> None:
    """Автор открыл «Мои обращения» — гасим точку об обновлениях.

    Условие в WHERE не косметика: без него открытие вкладки писало бы в общую
    базу на каждом заходе, даже когда гасить нечего.
    """
    with get_db() as conn:
        conn.execute(
            """
            UPDATE feedback_items
               SET status_seen_by_author_at = datetime('now')
             WHERE author_username = ?
               AND status != 'new'
               AND status_changed_at IS NOT NULL
               AND (status_seen_by_author_at IS NULL
                    OR status_seen_by_author_at < status_changed_at)
            """,
            (username,),
        )
