"""
Единственное место, где решается, КАК открывается соединение с SQLite.

Постоянный диск Amvera (/data) — сетевой и медленный. Замер прода 2026-08-26:
отдача статики занимала 3 мс, а любой запрос, коснувшийся базы на /data, —
от 90 до 700 мс, причём в спокойное время, без нагрузки. Python при этом
простаивал. То есть узкое место сайта — не код, а файловые операции и fsync.

Отсюда два правила, которые этот модуль удерживает за все модули сразу:

1. journal_mode=WAL выставляется ОДИН РАЗ на файл за жизнь процесса.
   Это персистентное свойство самого файла БД, а не соединения. Раньше PRAGMA
   шла на каждом `get_db()` — то есть по нескольку раз на каждый HTTP-запрос,
   и каждая была лишней записью в -shm на сетевой диск.

2. synchronous=NORMAL выставляется КАЖДОМУ соединению.
   Это, наоборот, свойство соединения, и в паре с WAL оно даёт fsync только
   на чекпойнте, а не на каждой транзакции. С дефолтным FULL один клик
   пользователя стоил нескольких fsync по 50-100 мс, а дисковую очередь,
   забитую фоновым синком, ждали вообще все: /data общий для barhat.db,
   pyrus.db, moysklad.db и couriers.db, поэтому подвисал и логин.

Так уже чинили МойСклад и Pyrus (см. moysklad/storage.py, pyrus/storage.py),
но остальные шесть модулей остались на старом режиме и продолжали грузить
общий диск. Чтобы это не разъезжалось снова — новый код открывает соединения
только отсюда.

ЛЮБОЙ новый модуль с собственной таблицей обязан звать connect() из этого
файла, а не sqlite3.connect() напрямую.
"""

import logging
import os
import sqlite3
import threading

logger = logging.getLogger("barhat.sqlite")

# Пути, для которых WAL в этом процессе уже включён. Ключ — абсолютный путь:
# barhat.db открывают пять модулей (auth, cashshifts, invoices, tasks,
# writeoffs), и все они должны попадать в одну запись кэша.
_wal_ready: set = set()
_wal_lock = threading.Lock()


def _ensure_wal(conn: sqlite3.Connection, db_path: str) -> None:
    """Включить WAL для файла, если в этом процессе он ещё не включался."""
    key = os.path.abspath(db_path)

    if key in _wal_ready:
        return

    with _wal_lock:
        if key in _wal_ready:
            return

        # Переключение журнала требует эксклюзивной блокировки и, в отличие от
        # обычной записи, НЕ ждёт по busy_timeout: если базу в этот момент
        # держит второй воркер gunicorn (оба стартуют одновременно), PRAGMA
        # падает с "database is locked". Проиграть эту гонку безобидно — либо
        # база уже в WAL, либо её переключает сосед. Ронять из-за этого
        # запрос нельзя: раньше такое исключение уносило весь init воркера.
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError as e:
            logger.info(f"journal_mode=WAL для {db_path} выставляет параллельный воркер: {e}")
            return

        _wal_ready.add(key)


def connect(db_path: str, timeout: int = 20) -> sqlite3.Connection:
    """Соединение с SQLite, настроенное под сетевой диск Amvera.

    Args:
        db_path: путь к файлу БД (получать через storage_paths.resolve).
        timeout: сколько секунд ждать снятия блокировки. То же значение идёт
            в busy_timeout: питоновский timeout покрывает не все пути внутри
            SQLite, PRAGMA — надёжнее.

    Дефолтные 5 секунд ожидания малы: на проде 2 воркера gunicorn по 8 потоков
    (amvera.yml) пишут в один файл, а фоновые синки пишут пачками. Без запаса
    параллельный запрос получает "database is locked" вместо того, чтобы
    просто дождаться очереди.
    """
    conn = sqlite3.connect(db_path, timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={timeout * 1000}")
    conn.execute("PRAGMA synchronous=NORMAL")
    _ensure_wal(conn, db_path)
    return conn
