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

from sqlite_conn import connect as sqlite_connect
from storage_paths import resolve as resolve_data_path

logger = logging.getLogger(__name__)

STATUSES = ("on_approval", "processing", "sent", "failed", "rejected", "cancelled")

# Путь к БД из переменной окружения или дефолт — та же база, что у auth/cashshifts/invoices
DB_PATH = os.environ.get("BARHAT_DB_PATH", "barhat.db")

# Куда сохранять вложения (фото списанного товара). Путь берём из storage_paths —
# см. комментарий там: относительный дефолт означал потерю файлов на каждой сборке.
ATTACHMENTS_DIR = resolve_data_path("WRITEOFF_ATTACHMENTS_DIR", "writeoff_attachments")

# Каталог создаётся один раз при импорте, а не на каждую загрузку: /data сетевой,
# и лишний syscall к нему стоит столько же, сколько запрос к базе.
os.makedirs(ATTACHMENTS_DIR, exist_ok=True)

# .heic/.heif — формат камеры iPhone по умолчанию. Клиент жмёт фото в JPEG перед
# отправкой, но если сжатие не отработало (старый браузер, отказ canvas), файл
# должен доехать оригиналом, а не упереться в «недопустимый тип».
ALLOWED_ATTACHMENT_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".heic", ".heif"}
MAX_ATTACHMENT_SIZE_BYTES = 15 * 1024 * 1024  # 15 МБ

# Ключ разовой миграции «фото с позиций -> на заявку» в writeoff_migrations.
# Версия в имени: если понадобится перепрогон по другим правилам — заводится
# новый ключ, а не сбрасывается старый.
PHOTOS_BACKFILL_KEY = "photos_from_position_attachments_v1"


def get_db():
    """
    Получить соединение с БД. Таймаут увеличен против дефолтных 5с — gunicorn
    на проде поднимает 2 воркера (`amvera.yml`), которые независимо друг от
    друга инициализируют таблицы при старте и могут одновременно писать
    в один и тот же файл SQLite; без запаса воркер получает
    "database is locked" вместо того, чтобы просто дождаться своей очереди.
    """
    return sqlite_connect(DB_PATH, timeout=20)


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """
    Идемпотентная миграция колонки — тот же приём, что в cashshifts/storage.py.

    На проде два воркера gunicorn стартуют одновременно и оба выполняют
    init_writeoffs_tables(). Оба исхода гонки штатные и не должны ронять старт:
    "duplicate column name" — сосед успел закоммитить ALTER, "database is
    locked" — держит write-лок прямо сейчас. В обоих случаях колонку создаёт
    сосед, цель достигнута.
    """
    existing = [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column in existing:
        return

    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        conn.commit()
    except sqlite3.OperationalError as e:
        message = str(e).lower()
        if "duplicate column name" in message or "locked" in message:
            logger.info("Миграцию %s.%s выполняет параллельный воркер: %s", table, column, e)
            return
        raise


def init_writeoffs_tables():
    """
    Инициализация таблиц модуля списаний (вызывается при старте приложения).

    Соединение закрывается в finally: упавший CREATE/ALTER оставлял бы открытое
    соединение с неоткатанной транзакцией, а оно держит write-лок общей barhat.db
    до перезапуска воркера — вместе с авторизацией (см. CLAUDE.md).
    """
    conn = get_db()
    try:
        _create_writeoffs_tables(conn)
        _backfill_writeoff_photos(conn)
    finally:
        conn.close()


def _create_writeoffs_tables(conn: sqlite3.Connection) -> None:
    """Создание и миграция таблиц модуля. Соединением владеет вызывающий."""

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
            uom_name TEXT,
            reason TEXT
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_writeoff_positions_writeoff
        ON writeoff_positions(writeoff_id)
    """)

    # Единица измерения из МойСклад, зафиксированная на момент заявки: у клубники,
    # бананов, винограда, фиников и чернослива это граммы, а не штуки. Храним
    # подпись рядом с количеством, чтобы карточка заявки не зависела от того,
    # доступен ли МойСклад сейчас и не поменяли ли товару единицу потом.
    # У заявок, заведённых до этой колонки, значения нет — показываем голое число.
    _add_column_if_missing(conn, "writeoff_positions", "uom_name", "TEXT")

    # ========================================================================
    # УСТАРЕЛО: фото по одной позиции.
    #
    # Таблица остаётся только как источник для бэкфилла и как архив — новый код
    # в неё не пишет и из неё не читает (см. writeoff_photos ниже). Не удаляем:
    # в ней лежат имена файлов, уже лежащих на диске.
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

    # ========================================================================
    # Фото списанного товара — на ЗАЯВКУ целиком.
    #
    # Флористы снимают несколько позиций одним кадром, а привязка фото к позиции
    # заставляла крепить один и тот же файл к каждой строке: шесть позиций —
    # шесть загрузок по 3-5 МБ и шесть копий на медленном /data (обращение 90-700 мс).
    # Обращение #7 от 2026-09-06.
    #
    # position_id — необязательный след бэкфилла (из какой позиции приехало старое
    # фото). Новый код его не заполняет и по нему не ищет.
    #
    # UNIQUE(stored_filename) — не украшение, а защита бэкфилла: его выполняют оба
    # воркера gunicorn на старте одновременно, и без уникальности каждый вставил бы
    # свою копию. Имена новых файлов — uuid4, столкнуться не могут.
    # ========================================================================
    conn.execute("""
        CREATE TABLE IF NOT EXISTS writeoff_photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            writeoff_id INTEGER NOT NULL REFERENCES writeoffs(id),
            position_id INTEGER,
            original_filename TEXT NOT NULL,
            stored_filename TEXT NOT NULL,
            uploaded_by TEXT NOT NULL,
            uploaded_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(stored_filename)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_writeoff_photos_writeoff
        ON writeoff_photos(writeoff_id)
    """)

    # ========================================================================
    # Отметки разовых миграций данных модуля.
    #
    # Нужна там, где «выполнено» нельзя вычислить по самим данным: бэкфилл фото
    # схлопывает дубли, поэтому часть исходных строк не имеет и не должна иметь
    # пары в writeoff_photos. Без отметки INSERT гонялся бы на старте каждого
    # воркера при каждом деплое.
    # ========================================================================
    conn.execute("""
        CREATE TABLE IF NOT EXISTS writeoff_migrations (
            key TEXT PRIMARY KEY,
            done_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    conn.commit()


def _backfill_writeoff_photos(conn: sqlite3.Connection) -> None:
    """
    Перенести старые фото из writeoff_attachments (привязка к позиции) в
    writeoff_photos (привязка к заявке). Разово, но идемпотентно: вызывается
    на старте каждого воркера и после первого раза не делает ничего.

    Один и тот же снимок, прикреплённый к шести позициям, лежит в старой таблице
    шестью записями с РАЗНЫМИ stored_filename (uuid на каждую загрузку) и
    ОДИНАКОВЫМ original_filename. Схлопываем по (writeoff_id, original_filename),
    оставляя запись с наименьшим id. Файлы-дубли на диске не трогаем — их разбор
    отдельной задачей, удалять без подтверждения нельзя.

    Отметка о выполнении — строкой в writeoff_migrations, а не вычислением
    «остались ли неперенесённые файлы». Такое вычисление здесь невозможно в
    принципе: пять свёрнутых копий из шести НИКОГДА не появятся в writeoff_photos,
    и признак «перенести нечего» не наступил бы никогда — INSERT гонялся бы на
    старте каждого воркера вечно.
    """
    if conn.execute(
        "SELECT 1 FROM writeoff_migrations WHERE key = ?", (PHOTOS_BACKFILL_KEY,)
    ).fetchone():
        return

    # OR IGNORE, а не NOT EXISTS в WHERE: оба воркера стартуют одновременно и
    # могут пройти проверку одновременно. Гонку разруливает UNIQUE в схеме.
    cursor = conn.execute("""
        INSERT OR IGNORE INTO writeoff_photos
            (writeoff_id, position_id, original_filename, stored_filename, uploaded_by, uploaded_at)
        SELECT p.writeoff_id, a.position_id, a.original_filename, a.stored_filename,
               a.uploaded_by, a.uploaded_at
        FROM writeoff_attachments a
        JOIN writeoff_positions p ON p.id = a.position_id
        WHERE a.id = (
            SELECT MIN(a2.id)
            FROM writeoff_attachments a2
            JOIN writeoff_positions p2 ON p2.id = a2.position_id
            WHERE p2.writeoff_id = p.writeoff_id
              AND a2.original_filename = a.original_filename
        )
    """)
    conn.execute(
        "INSERT OR IGNORE INTO writeoff_migrations (key) VALUES (?)", (PHOTOS_BACKFILL_KEY,)
    )
    conn.commit()
    logger.info("Бэкфилл фото списаний: перенесено записей — %s", cursor.rowcount)


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
                 "quantity", "uom_name", "reason"}, ...] — минимум одна позиция.
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
                    (writeoff_id, moysklad_product_id, moysklad_product_href, product_name,
                     quantity, uom_name, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    writeoff_id,
                    pos["moysklad_product_id"],
                    pos["moysklad_product_href"],
                    pos["product_name"],
                    pos["quantity"],
                    pos.get("uom_name"),
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


def get_writeoff_head(writeoff_id: int) -> Optional[Dict[str, Any]]:
    """
    Скалярные поля заявки — точка, статус, автор — без позиций и фото.

    Этого хватает на все три проверки ручек фото (доступ к точке, допустимость
    статуса, «своя ли заявка»), и стоит это одного запроса по первичному ключу
    вместо девяти у get_writeoff_by_id, который ради одного столбца вычитывал бы
    все позиции и все фото (см. CLAUDE.md, «экран не зависит от диагностики»).
    """
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id, store_id, status, created_by FROM writeoffs WHERE id = ?",
            (writeoff_id,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def get_writeoff_photos(writeoff_id: int) -> List[Dict[str, Any]]:
    """Фото заявки (одно на весь документ, но их может быть несколько)."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM writeoff_photos WHERE writeoff_id = ? ORDER BY uploaded_at, id",
            (writeoff_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def get_writeoff_by_id(writeoff_id: int) -> Optional[Dict[str, Any]]:
    """
    Заявка с вложенными позициями и фото документа.

    Позиции больше не тянут за собой вложения: раньше здесь был отдельный запрос
    НА КАЖДУЮ позицию, то есть открытие заявки из шести строк стоило девяти
    обращений к сетевому /data вместо трёх.
    """
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM writeoffs WHERE id = ?", (writeoff_id,)).fetchone()
        if not row:
            return None

        writeoff = dict(row)
        position_rows = conn.execute(
            "SELECT * FROM writeoff_positions WHERE writeoff_id = ? ORDER BY id",
            (writeoff_id,),
        ).fetchall()
        writeoff["positions"] = [dict(p) for p in position_rows]

        photo_rows = conn.execute(
            "SELECT * FROM writeoff_photos WHERE writeoff_id = ? ORDER BY uploaded_at, id",
            (writeoff_id,),
        ).fetchall()
        writeoff["photos"] = [dict(p) for p in photo_rows]
    finally:
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

    Каждая заявка отдаётся с positions_count — сами позиции здесь не грузим
    (это отдельный запрос на каждую строку списка), но число позиций таблица
    показывает в колонке «Позиций». Раньше его там не было вовсе: фронт считал
    длину writeoffs[].positions, которого в ответе этого эндпоинта нет, и
    колонка у всех заявок показывала 0.
    """
    query = """
        SELECT w.*,
               (SELECT COUNT(*) FROM writeoff_positions p WHERE p.writeoff_id = w.id) AS positions_count
        FROM writeoffs w
        WHERE 1=1
    """
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
# Фото списания — на заявку целиком.
#
# Паттерн хранения тот же, что у invoices/invoice_attachments: файл на диск в
# ATTACHMENTS_DIR под uuid-именем, запись в БД. Отличие — привязка к документу,
# а не к строке: флористы снимают несколько позиций одним кадром (обращение #7).
# ============================================================================

class LastPhotoError(Exception):
    """Попытка удалить единственное фото заявки — заявка станет непроводимой."""


def add_writeoff_photo(
    writeoff_id: int, original_filename: str, file_bytes: bytes, uploaded_by: str
) -> Dict[str, Any]:
    """Сохранить файл на диск и запись о нём в БД. Возвращает {"ok", "error", "photo"}."""
    ext = os.path.splitext(original_filename)[1].lower()
    if ext not in ALLOWED_ATTACHMENT_EXTENSIONS:
        return {"ok": False, "error": f"Недопустимый тип файла: {ext}", "photo": None}
    if not file_bytes:
        # Пустой файл доезжает при обрыве загрузки и выглядит как успешная
        # отправка: запись есть, фото нет, а согласование проходит.
        return {"ok": False, "error": "Файл пустой — загрузите фото заново", "photo": None}
    if len(file_bytes) > MAX_ATTACHMENT_SIZE_BYTES:
        return {"ok": False, "error": "Файл слишком большой (максимум 15 МБ)", "photo": None}

    stored_filename = f"{uuid.uuid4().hex}{ext}"
    with open(os.path.join(ATTACHMENTS_DIR, stored_filename), "wb") as f:
        f.write(file_bytes)

    conn = get_db()
    try:
        cursor = conn.execute(
            """
            INSERT INTO writeoff_photos (writeoff_id, original_filename, stored_filename, uploaded_by)
            VALUES (?, ?, ?, ?)
            """,
            (writeoff_id, original_filename, stored_filename, uploaded_by),
        )
        photo_id = cursor.lastrowid
        conn.commit()
        row = conn.execute("SELECT * FROM writeoff_photos WHERE id = ?", (photo_id,)).fetchone()
    finally:
        conn.close()

    return {"ok": True, "error": None, "photo": dict(row)}


def get_writeoff_photo_by_id(photo_id: int) -> Optional[Dict[str, Any]]:
    """
    Фото вместе с точкой, статусом и автором его заявки — ОДНИМ запросом.

    Ручкам скачивания и удаления нужно и то, и другое; отдельный поход за
    заявкой удваивал бы число обращений к сетевому /data на каждую картинку
    в карточке.
    """
    conn = get_db()
    try:
        row = conn.execute(
            """
            SELECT ph.*,
                   w.store_id   AS writeoff_store_id,
                   w.status     AS writeoff_status,
                   w.created_by AS writeoff_created_by
            FROM writeoff_photos ph
            JOIN writeoffs w ON w.id = ph.writeoff_id
            WHERE ph.id = ?
            """,
            (photo_id,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def delete_writeoff_photo(photo_id: int) -> Optional[Dict[str, Any]]:
    """
    Удалить запись о фото. Возвращает удалённую запись или None, если её уже нет.
    Поднимает LastPhotoError, если это единственное фото заявки.

    Проверка «не последнее» и удаление — ОДНА транзакция под BEGIN IMMEDIATE.
    На проде до 16 параллельных обработчиков, а запрос к базе стоит 90-700 мс:
    два клика по крестикам разных фото прочитали бы «их двое, удалять можно» и
    снесли бы оба, оставив заявку без фото и без возможности её согласовать
    (см. CLAUDE.md, «проверил -> записал — это одна транзакция»).

    Файл с диска НЕ удаляем: это единственное подтверждение списания, а разбор
    осиротевших файлов идёт отдельной задачей и под подтверждением.
    """
    conn = get_db()
    conn.isolation_level = None  # транзакцией управляем сами
    try:
        conn.execute("BEGIN IMMEDIATE")  # write-лок ДО чтения
        row = conn.execute("SELECT * FROM writeoff_photos WHERE id = ?", (photo_id,)).fetchone()
        if not row:
            conn.execute("ROLLBACK")
            return None

        remaining = conn.execute(
            "SELECT COUNT(*) FROM writeoff_photos WHERE writeoff_id = ?", (row["writeoff_id"],)
        ).fetchone()[0]
        if remaining <= 1:
            conn.execute("ROLLBACK")
            raise LastPhotoError(
                "Это единственное фото заявки. Сначала загрузите другое — без фото "
                "заявку нельзя согласовать."
            )

        conn.execute("DELETE FROM writeoff_photos WHERE id = ?", (photo_id,))
        conn.execute("COMMIT")
    finally:
        conn.close()

    return dict(row)
