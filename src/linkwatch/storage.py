"""
Хранилище сторожа ссылок на товары.

Своя база (linkwatch.db), а не общая barhat.db: сюда пишет фоновая проверка,
и её нагрузка не должна задевать логин — в общую базу писали уже дважды, и оба
раза от этого тормозил весь сайт.

Пишем мало: одна строка на прогон плюс список проблемных товаров раз в сутки.
Путь к файлу — из storage_paths: на Amvera постоянный диск /data, относительный
путь означает потерю базы на следующей сборке.
"""

import logging
import os
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlite_conn import connect as sqlite_connect
from storage_paths import resolve as resolve_data_path

logger = logging.getLogger(__name__)

DB_PATH = resolve_data_path("LINKWATCH_DB_PATH", "linkwatch.db")

# Категории, на которые раскладывается результат проверки одной ссылки.
# Ровно те же, что в scripts/check_product_urls.py — чтобы отчёт из дашборда
# и ручной прогон говорили на одном языке.
STATUS_OK = "ok"
STATUS_REDIRECT = "redirect"
STATUS_WRONG_PAGE = "wrong_page"
STATUS_NOT_FOUND = "not_found"
STATUS_NO_URL = "no_url"
STATUS_ERROR = "error"

STATUS_TITLES = {
    STATUS_OK: "Ведёт точно на карточку",
    STATUS_REDIRECT: "Неканоническая — открывается через редирект",
    STATUS_WRONG_PAGE: "Товара нет на сайте — открывается раздел",
    STATUS_NOT_FOUND: "Страница не найдена (404)",
    STATUS_NO_URL: "Ссылки нет",
    STATUS_ERROR: "Ошибка запроса",
}

# «Ссылки нет» — это НЕ поломка: у товаров без публичной страницы («Товары МС»,
# снятые с публикации сезонные) пустая ссылка и есть правильное состояние, его
# специально добивается пост-обработчик выгрузки на сайте. Считать их битыми
# значило бы держать сторож вечно красным.
BROKEN_STATUSES = (STATUS_REDIRECT, STATUS_WRONG_PAGE, STATUS_NOT_FOUND, STATUS_ERROR)


def _ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)


@contextmanager
def get_db():
    """Соединение с базой модуля (через общий sqlite_conn — см. CLAUDE.md)."""
    _ensure_parent_dir(DB_PATH)
    conn = sqlite_connect(DB_PATH, timeout=30)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_tables() -> None:
    """Создать таблицы. Зовётся при регистрации blueprint."""
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,           -- running / done / failed
                checked INTEGER DEFAULT 0,
                ok INTEGER DEFAULT 0,
                broken INTEGER DEFAULT 0,
                no_url INTEGER DEFAULT 0,
                error TEXT
            )
            """
        )
        # Проблемные товары последнего завершённого прогона. Историю по каждому
        # товару не храним: она не нужна, а таблица на медленном /data растёт.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS broken_links (
                run_id INTEGER NOT NULL,
                product_id INTEGER,
                article TEXT,
                name TEXT,
                status TEXT NOT NULL,
                url TEXT,
                canonical_url TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_broken_run ON broken_links(run_id)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sync_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
    logger.info("Таблицы сторожа ссылок готовы")


# ---------------------------------------------------------------------------
# Лок и талон на прогон — тот же приём, что в couriers/moysklad: планировщик
# стартует в каждом воркере gunicorn, а работу должен делать один.
# ---------------------------------------------------------------------------

def try_acquire_lock(name: str, ttl_seconds: int) -> bool:
    """Захватить лок. False — держит кто-то другой. В value лежит срок истечения,
    поэтому умерший вместе с воркером держатель освобождает лок сам по TTL."""
    key = f"lock:{name}"
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    expires = (datetime.utcnow() + timedelta(seconds=ttl_seconds)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        with get_db() as conn:
            conn.execute("INSERT OR IGNORE INTO sync_state (key, value) VALUES (?, '')", (key,))
            cursor = conn.execute(
                """
                UPDATE sync_state SET value = ?, updated_at = datetime('now')
                WHERE key = ? AND (value = '' OR value < ?)
                """,
                (expires, key, now),
            )
            # Формат времени фиксированной ширины — лексикографическое сравнение
            # строк совпадает с хронологическим
            return cursor.rowcount > 0
    except Exception as e:
        logger.error(f"Ошибка захвата лока {name}: {e}")
        return False


def renew_lock(name: str, ttl_seconds: int) -> None:
    """Продлить свой лок. Прогон идёт полчаса — без продления TTL отдаст лок соседу."""
    expires = (datetime.utcnow() + timedelta(seconds=ttl_seconds)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        with get_db() as conn:
            conn.execute(
                "UPDATE sync_state SET value = ?, updated_at = datetime('now') WHERE key = ?",
                (expires, f"lock:{name}"),
            )
    except Exception as e:
        logger.error(f"Ошибка продления лока {name}: {e}")


def release_lock(name: str) -> None:
    try:
        with get_db() as conn:
            conn.execute(
                "UPDATE sync_state SET value = '', updated_at = datetime('now') WHERE key = ?",
                (f"lock:{name}",),
            )
    except Exception as e:
        logger.error(f"Ошибка освобождения лока {name}: {e}")


def try_claim_scheduled_run(name: str, interval_seconds: int) -> bool:
    """
    Талон на тик планировщика.

    Лок ловит только ОДНОВРЕМЕННЫЙ прогон, а тики двух воркеров разъезжаются во
    времени — без талона второй воркер повторял бы получасовой обход сайта
    сразу после первого.
    """
    key = f"claim:{name}"
    now = datetime.utcnow()
    threshold = (now - timedelta(seconds=interval_seconds)).strftime("%Y-%m-%d %H:%M:%S")
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with get_db() as conn:
            conn.execute("INSERT OR IGNORE INTO sync_state (key, value) VALUES (?, '')", (key,))
            cursor = conn.execute(
                """
                UPDATE sync_state SET value = ?, updated_at = datetime('now')
                WHERE key = ? AND (value = '' OR value < ?)
                """,
                (stamp, key, threshold),
            )
            return cursor.rowcount > 0
    except Exception as e:
        logger.error(f"Ошибка талона {name}: {e}")
        return False


# ---------------------------------------------------------------------------
# Прогоны
# ---------------------------------------------------------------------------

def start_run() -> int:
    with get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO runs (started_at, status) VALUES (datetime('now'), 'running')"
        )
        return cursor.lastrowid


def finish_run(run_id: int, counts: Dict[str, int], broken: List[Dict[str, Any]]) -> None:
    """Записать итог прогона и список проблемных товаров одной транзакцией."""
    with get_db() as conn:
        conn.execute(
            """
            UPDATE runs
               SET finished_at = datetime('now'), status = 'done',
                   checked = ?, ok = ?, broken = ?, no_url = ?
             WHERE id = ?
            """,
            (
                counts.get("checked", 0),
                counts.get("ok", 0),
                counts.get("broken", 0),
                counts.get("no_url", 0),
                run_id,
            ),
        )
        conn.executemany(
            """
            INSERT INTO broken_links
                (run_id, product_id, article, name, status, url, canonical_url)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    run_id,
                    item.get("product_id"),
                    item.get("article"),
                    item.get("name"),
                    item.get("status"),
                    item.get("url"),
                    item.get("canonical_url"),
                )
                for item in broken
            ],
        )
        # Держим результаты только последних прогонов: смысл имеет свежий срез,
        # а /data медленный и общий на все базы.
        conn.execute(
            """
            DELETE FROM broken_links
             WHERE run_id NOT IN (SELECT id FROM runs ORDER BY id DESC LIMIT 5)
            """
        )
        conn.execute(
            "DELETE FROM runs WHERE id NOT IN (SELECT id FROM runs ORDER BY id DESC LIMIT 30)"
        )


def fail_run(run_id: int, error: str) -> None:
    with get_db() as conn:
        conn.execute(
            """
            UPDATE runs SET finished_at = datetime('now'), status = 'failed', error = ?
             WHERE id = ?
            """,
            (error[:500], run_id),
        )


def get_last_run() -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM runs WHERE status != 'running' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def get_running() -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM runs WHERE status = 'running' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def get_broken(run_id: int) -> List[Dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT product_id, article, name, status, url, canonical_url
              FROM broken_links WHERE run_id = ?
             ORDER BY status, name
            """,
            (run_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def get_history(limit: int = 14) -> List[Dict[str, Any]]:
    """Последние прогоны — чтобы видеть, растёт число битых ссылок или падает."""
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT started_at, finished_at, status, checked, ok, broken, no_url, error
              FROM runs ORDER BY id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]
