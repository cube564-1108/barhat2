"""
Офлайн-тесты фото списаний: привязка к ЗАЯВКЕ, а не к позиции — без сети.

Обращение #7 от 2026-09-06: флористы снимают несколько позиций одним кадром, а
форма требовала фото к каждой строке. Один и тот же файл заливался N раз (по
3-5 МБ на медленный /data), при обрыве загрузки позиция оставалась без фото, и
заявка становилась непроводимой — дозалить фото было нечем.

Фаза 1 (схема и бэкфилл) проверяет:
  1. Таблица writeoff_photos и UNIQUE(stored_filename) создаются
  2. Бэкфилл схлопывает дубли: один снимок на шести позициях -> одна запись
  3. Бэкфилл идемпотентен: второй прогон вставляет 0 строк
  4. Бэкфилл переживает гонку двух воркеров (20 потоков — без дублей)
  5. get_writeoff_by_id отдаёт photos, не ломая старый ключ attachments
  6. get_writeoff_store_id дешевле get_writeoff_by_id по числу запросов к БД

Запуск: python scripts/test_writeoff_photos.py
"""

import io
import os
import sqlite3
import sys
import tempfile
import threading

if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# Временная БД — до импорта storage, путь читается на уровне модуля
_tmp_dir = tempfile.mkdtemp()
os.environ["BARHAT_DB_PATH"] = os.path.join(_tmp_dir, "test_writeoff_photos.db")
os.environ["WRITEOFF_ATTACHMENTS_DIR"] = os.path.join(_tmp_dir, "attachments")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from writeoffs import storage  # noqa: E402
from writeoffs.storage import (  # noqa: E402
    get_db,
    get_writeoff_by_id,
    get_writeoff_photos,
    get_writeoff_store_id,
    init_writeoffs_tables,
)

failures = []


def check(name, condition, detail=""):
    mark = "OK  " if condition else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(name)


def seed_legacy_data():
    """
    База в состоянии «до перехода»: заявка с шестью позициями, к каждой прикреплён
    ОДИН И ТОТ ЖЕ снимок — шесть строк с разными stored_filename (uuid на загрузку)
    и одинаковым original_filename. Плюс вторая заявка с двумя разными фото.
    """
    conn = get_db()
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS stores (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT OR IGNORE INTO stores (id, name) VALUES (1, 'Тестовая точка')")
        conn.execute("INSERT OR IGNORE INTO stores (id, name) VALUES (2, 'Вторая точка')")

        conn.execute(
            "INSERT INTO writeoffs (id, store_id, created_by) VALUES (10, 1, 'florist')"
        )
        conn.execute(
            "INSERT INTO writeoffs (id, store_id, created_by) VALUES (20, 2, 'florist')"
        )

        # Заявка 10: шесть позиций, один общий кадр
        for i in range(6):
            conn.execute(
                """INSERT INTO writeoff_positions
                   (id, writeoff_id, moysklad_product_id, moysklad_product_href,
                    product_name, quantity, uom_name)
                   VALUES (?, 10, ?, ?, ?, ?, 'шт')""",
                (100 + i, f"prod-{i}", f"href-{i}", f"Роза {i}", 1),
            )
            conn.execute(
                """INSERT INTO writeoff_attachments
                   (position_id, original_filename, stored_filename, uploaded_by)
                   VALUES (?, 'IMG_0042.jpg', ?, 'florist')""",
                (100 + i, f"uuid-copy-{i}.jpg"),
            )

        # Заявка 20: две позиции, два РАЗНЫХ файла — схлопывать нечего
        for i in range(2):
            conn.execute(
                """INSERT INTO writeoff_positions
                   (id, writeoff_id, moysklad_product_id, moysklad_product_href,
                    product_name, quantity, uom_name)
                   VALUES (?, 20, ?, ?, ?, ?, 'г')""",
                (200 + i, f"berry-{i}", f"bhref-{i}", f"Клубника {i}", 600),
            )
            conn.execute(
                """INSERT INTO writeoff_attachments
                   (position_id, original_filename, stored_filename, uploaded_by)
                   VALUES (?, ?, ?, 'florist')""",
                (200 + i, f"IMG_100{i}.jpg", f"uuid-uniq-{i}.jpg"),
            )
        conn.commit()
    finally:
        conn.close()


def table_columns(table):
    conn = get_db()
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    finally:
        conn.close()


def count(sql, params=()):
    conn = get_db()
    try:
        return conn.execute(sql, params).fetchone()[0]
    finally:
        conn.close()


print("\n=== 1. Схема ===")
# Порядок как на проде: схема есть, база накопилась старым кодом, и только
# ПОТОМ приезжает деплой с бэкфиллом. Поэтому здесь создаём таблицы напрямую,
# без init_writeoffs_tables() — иначе бэкфилл отметился бы на пустой базе.
_schema_conn = get_db()
try:
    storage._create_writeoffs_tables(_schema_conn)
finally:
    _schema_conn.close()
seed_legacy_data()

cols = table_columns("writeoff_photos")
check("Таблица writeoff_photos создана", bool(cols), f"колонки: {', '.join(cols)}")
check("Есть writeoff_id", "writeoff_id" in cols)
check("position_id сохранён как след бэкфилла", "position_id" in cols)

conn = get_db()
try:
    nullable = {r[1]: r[3] for r in conn.execute("PRAGMA table_info(writeoff_photos)").fetchall()}
    dup_blocked = False
    conn.execute(
        """INSERT INTO writeoff_photos (writeoff_id, original_filename, stored_filename, uploaded_by)
           VALUES (10, 'a.jpg', 'unique-probe.jpg', 'tester')"""
    )
    try:
        conn.execute(
            """INSERT INTO writeoff_photos (writeoff_id, original_filename, stored_filename, uploaded_by)
               VALUES (10, 'b.jpg', 'unique-probe.jpg', 'tester')"""
        )
    except sqlite3.IntegrityError:
        dup_blocked = True
    conn.execute("DELETE FROM writeoff_photos WHERE stored_filename = 'unique-probe.jpg'")
    conn.commit()
finally:
    conn.close()

check("position_id необязателен", nullable.get("position_id") == 0)
check("UNIQUE(stored_filename) отбивает второй файл с тем же именем", dup_blocked)


print("\n=== 2. Бэкфилл: схлопывание дублей ===")
# Деплой нового кода поверх накопленной базы
init_writeoffs_tables()

photos_10 = get_writeoff_photos(10)
photos_20 = get_writeoff_photos(20)

check(
    "Шесть копий одного кадра свернулись в одну запись",
    len(photos_10) == 1,
    f"записей: {len(photos_10)}",
)
check(
    "Осталась запись с наименьшим id (первая загрузка)",
    photos_10 and photos_10[0]["stored_filename"] == "uuid-copy-0.jpg",
    photos_10[0]["stored_filename"] if photos_10 else "нет записей",
)
check(
    "Разные файлы одной заявки не схлопываются",
    len(photos_20) == 2,
    f"записей: {len(photos_20)}",
)
check(
    "Фото привязано к своей заявке",
    all(p["writeoff_id"] == 10 for p in photos_10)
    and all(p["writeoff_id"] == 20 for p in photos_20),
)
check(
    "Старая таблица не тронута",
    count("SELECT COUNT(*) FROM writeoff_attachments") == 8,
    f"строк: {count('SELECT COUNT(*) FROM writeoff_attachments')}",
)


print("\n=== 3. Идемпотентность ===")
before = count("SELECT COUNT(*) FROM writeoff_photos")
for _ in range(3):
    init_writeoffs_tables()
after = count("SELECT COUNT(*) FROM writeoff_photos")
check("Три повторных прогона не вставили ни строки", before == after, f"{before} -> {after}")
check(
    "Отметка о миграции проставлена",
    count("SELECT COUNT(*) FROM writeoff_migrations WHERE key = ?", (storage.PHOTOS_BACKFILL_KEY,)) == 1,
)

# Короткое замыкание: в устоявшемся состоянии бэкфилл не должен открывать
# транзакцию записи — проверяем, что он вообще не доходит до INSERT
executed = []
original_connect = storage.sqlite_connect


def tracing_connect(*args, **kwargs):
    # set_trace_callback, а не подмена conn.execute: атрибуты sqlite3.Connection
    # только для чтения. Заодно видно операторы, которые SQLite выполняет сам.
    conn = original_connect(*args, **kwargs)
    conn.set_trace_callback(lambda sql: executed.append(" ".join(sql.split())[:60]))
    return conn


storage.sqlite_connect = tracing_connect
try:
    executed.clear()
    conn = storage.get_db()
    try:
        storage._backfill_writeoff_photos(conn)
    finally:
        conn.close()
    inserts = [s for s in executed if s.upper().startswith("INSERT")]
finally:
    storage.sqlite_connect = original_connect

check("Устоявшийся бэкфилл не выполняет INSERT", not inserts, f"INSERT'ов: {len(inserts)}")


print("\n=== 4. Гонка двух воркеров ===")
errors = []


def worker():
    try:
        init_writeoffs_tables()
    except Exception as e:  # noqa: BLE001 — падение старта воркера и есть баг
        errors.append(repr(e))


threads = [threading.Thread(target=worker) for _ in range(20)]
for t in threads:
    t.start()
for t in threads:
    t.join()

check("Ни один параллельный старт не упал", not errors, "; ".join(errors[:3]))
check(
    "Дублей после гонки нет",
    count("SELECT COUNT(*) FROM writeoff_photos") == after,
    f"строк: {count('SELECT COUNT(*) FROM writeoff_photos')}",
)


print("\n=== 5. Чтение заявки ===")
writeoff = get_writeoff_by_id(10)
check("photos отдаются в заявке", len(writeoff.get("photos", [])) == 1)
check(
    "Старый ключ attachments у позиций пока жив (на него смотрит approve)",
    all("attachments" in p for p in writeoff["positions"]),
)
check("Заявки без фото отдают пустой список", get_writeoff_photos(999) == [])
check("get_writeoff_by_id на несуществующей заявке — None", get_writeoff_by_id(999) is None)


print("\n=== 6. Цена проверки доступа ===")
storage.sqlite_connect = tracing_connect
try:
    executed.clear()
    store_id = get_writeoff_store_id(10)
    cheap = [s for s in executed if s.upper().startswith("SELECT")]

    executed.clear()
    get_writeoff_by_id(10)
    heavy = [s for s in executed if s.upper().startswith("SELECT")]
finally:
    storage.sqlite_connect = original_connect

check("get_writeoff_store_id вернул точку", store_id == 1, f"store_id={store_id}")
check(
    "get_writeoff_store_id — ровно один SELECT",
    len(cheap) == 1,
    f"запросов: {len(cheap)}",
)
check(
    "get_writeoff_by_id ощутимо дороже (потому ручкам вложений он и не нужен)",
    len(heavy) > len(cheap),
    f"{len(heavy)} против {len(cheap)}",
)
check("get_writeoff_store_id на несуществующей заявке — None", get_writeoff_store_id(999) is None)


print("\n" + "=" * 60)
if failures:
    print(f"ПРОВАЛЕНО проверок: {len(failures)}")
    for name in failures:
        print(f"  - {name}")
    sys.exit(1)
print("Все проверки прошли.")
