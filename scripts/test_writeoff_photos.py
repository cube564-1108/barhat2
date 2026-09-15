"""
Офлайн-тесты фото списаний: привязка к ЗАЯВКЕ, а не к позиции — без сети.

Обращение #7 от 2026-09-06: флористы снимают несколько позиций одним кадром, а
форма требовала фото к каждой строке. Один и тот же файл заливался N раз (по
3-5 МБ на медленный /data), при обрыве загрузки позиция оставалась без фото, и
заявка становилась непроводимой — дозалить фото было нечем.

Проверяет:
  1. Таблица writeoff_photos и UNIQUE(stored_filename) создаются
  2. Бэкфилл схлопывает дубли: один снимок на шести позициях -> одна запись
  3. Бэкфилл идемпотентен и отмечается в writeoff_migrations
  4. Бэкфилл переживает гонку двух воркеров (20 потоков — без дублей)
  5. Позиции больше не тянут вложения отдельным запросом на каждую
  6. Цена чтения: head — 1 запрос, заявка — 3, фото с заявкой — 1
  7. Загрузка: тип файла, пустой файл, размер, HEIC с айфона
  8. Удаление: последнее фото удалить нельзя, файл на диске остаётся
  9. Гонка на удалении: 10 потоков на 10 фото — выживает одно

Запуск: python scripts/test_writeoff_photos.py
"""

import io
import os
import socket
import sqlite3
import sys
import tempfile
import threading
from datetime import datetime, timezone

if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')


# Сеть отключаем ДО импорта приложения: локальный прогон читает боевой .env
# (load_dotenv при импорте) и иначе может уйти в боевые МойСклад/RetailCRM.
class NoNetwork(socket.socket):
    def connect(self, *a, **k):
        raise OSError("сеть отключена намеренно")

    def connect_ex(self, *a, **k):
        raise OSError("сеть отключена намеренно")


socket.socket = NoNetwork

# Временная БД — до импорта storage, путь читается на уровне модуля
_tmp_dir = tempfile.mkdtemp()
os.environ["BARHAT_DB_PATH"] = os.path.join(_tmp_dir, "test_writeoff_photos.db")
os.environ["WRITEOFF_ATTACHMENTS_DIR"] = os.path.join(_tmp_dir, "attachments")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from writeoffs import storage  # noqa: E402
from writeoffs.storage import (  # noqa: E402
    LastPhotoError,
    add_writeoff_photo,
    delete_writeoff_photo,
    get_db,
    get_writeoff_by_id,
    get_writeoff_head,
    get_writeoff_photo_by_id,
    get_writeoff_photos,
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
# Точки и привязки пользователей живут в cashshifts — списания переиспользуют
# их справочник, а не заводят свой.
from cashshifts.storage import init_cashshifts_tables  # noqa: E402

init_cashshifts_tables()

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
    "Позиции больше не тянут вложения (был запрос НА КАЖДУЮ позицию)",
    all("attachments" not in p for p in writeoff["positions"]),
)
check("Заявки без фото отдают пустой список", get_writeoff_photos(999) == [])
check("get_writeoff_by_id на несуществующей заявке — None", get_writeoff_by_id(999) is None)


print("\n=== 6. Цена проверки доступа ===")
storage.sqlite_connect = tracing_connect
try:
    executed.clear()
    head = get_writeoff_head(10)
    cheap = [s for s in executed if s.upper().startswith("SELECT")]

    executed.clear()
    get_writeoff_by_id(10)
    heavy = [s for s in executed if s.upper().startswith("SELECT")]

    executed.clear()
    get_writeoff_photo_by_id(writeoff["photos"][0]["id"])
    joined = [s for s in executed if s.upper().startswith("SELECT")]
finally:
    storage.sqlite_connect = original_connect

check("get_writeoff_head вернул точку, статус и автора",
      head and head["store_id"] == 1 and head["status"] == "on_approval"
      and head["created_by"] == "florist", str(head))
check("get_writeoff_head — ровно один SELECT", len(cheap) == 1, f"запросов: {len(cheap)}")
check(
    "Заявка из шести позиций читается тремя запросами, а не девятью",
    len(heavy) == 3,
    f"запросов: {len(heavy)}",
)
check(
    "get_writeoff_photo_by_id забирает фото и заявку одним запросом",
    len(joined) == 1,
    f"запросов: {len(joined)}",
)
check("get_writeoff_head на несуществующей заявке — None", get_writeoff_head(999) is None)


print("\n=== 7. Загрузка фото ===")
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 200

ok = add_writeoff_photo(10, "second.jpg", JPEG, "manager")
check("Второе фото добавляется к заявке", ok["ok"] and ok["photo"]["writeoff_id"] == 10)
check(
    "Файл лёг на диск",
    os.path.exists(os.path.join(storage.ATTACHMENTS_DIR, ok["photo"]["stored_filename"])),
)
check("Каталог вложений создан при импорте", os.path.isdir(storage.ATTACHMENTS_DIR))

bad_ext = add_writeoff_photo(10, "smeta.pdf", JPEG, "manager")
check("Чужой тип файла отбит", not bad_ext["ok"], bad_ext["error"])

empty = add_writeoff_photo(10, "oborvalos.jpg", b"", "manager")
check(
    "Пустой файл отбит, а не записан как успешная загрузка",
    not empty["ok"],
    empty["error"],
)

big = add_writeoff_photo(10, "huge.jpg", b"0" * (storage.MAX_ATTACHMENT_SIZE_BYTES + 1), "manager")
check("Слишком большой файл отбит", not big["ok"], big["error"])

check(
    "HEIC с айфона принимается (клиентское сжатие могло не отработать)",
    ".heic" in storage.ALLOWED_ATTACHMENT_EXTENSIONS,
)


print("\n=== 8. Удаление фото ===")
photos_before = get_writeoff_photos(10)
check("У заявки два фото", len(photos_before) == 2, f"фото: {len(photos_before)}")

removed = delete_writeoff_photo(photos_before[1]["id"])
check("Второе фото удалено", removed is not None)
check("Осталось одно", len(get_writeoff_photos(10)) == 1)
check(
    "Файл на диске не тронут (единственное подтверждение списания)",
    os.path.exists(os.path.join(storage.ATTACHMENTS_DIR, removed["stored_filename"])),
)

last_blocked = False
try:
    delete_writeoff_photo(get_writeoff_photos(10)[0]["id"])
except LastPhotoError:
    last_blocked = True
check("Последнее фото удалить нельзя — заявка стала бы непроводимой", last_blocked)
check("Фото осталось на месте", len(get_writeoff_photos(10)) == 1)
check("Удаление несуществующего фото — None", delete_writeoff_photo(999999) is None)


print("\n=== 9. Гонка на удалении ===")
# Два управляющих жмут крестики на РАЗНЫХ фото одновременно. Без BEGIN IMMEDIATE
# обе проверки читают «их двое, удалять можно» и сносят оба, оставляя заявку
# без фото и без возможности её согласовать.
for i in range(8):
    add_writeoff_photo(20, f"race-{i}.jpg", JPEG, "manager")

race_ids = [p["id"] for p in get_writeoff_photos(20)]
check("Заготовлено фото для гонки", len(race_ids) == 10, f"фото: {len(race_ids)}")

race_errors = []
barrier = threading.Barrier(len(race_ids))


def racer(photo_id):
    barrier.wait()
    try:
        delete_writeoff_photo(photo_id)
    except LastPhotoError:
        pass
    except Exception as e:  # noqa: BLE001
        race_errors.append(repr(e))


race_threads = [threading.Thread(target=racer, args=(pid,)) for pid in race_ids]
for t in race_threads:
    t.start()
for t in race_threads:
    t.join()

survivors = get_writeoff_photos(20)
check("Ни один поток не упал с ошибкой", not race_errors, "; ".join(race_errors[:3]))
check(
    "Заявка не осталась без фото: выжило ровно одно",
    len(survivors) == 1,
    f"фото: {len(survivors)}",
)


print("\n=== 10. HTTP: тупик «нет фото — согласовать нельзя» ===")
# Ради этого раздела всё и затевалось. Раньше выхода из него не было:
# проверка при согласовании требовала фото, а добавить его было нечем.

from io import BytesIO  # noqa: E402
from flask import Flask  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

from auth import auth_bp, init_auth_tables, login_manager  # noqa: E402
from cashshifts.storage import set_user_stores  # noqa: E402
from writeoffs import server as writeoff_server  # noqa: E402
from writeoffs.storage import mark_writeoff_sent  # noqa: E402


def add_user(username, role):
    conn = get_db()
    try:
        conn.execute(
            """INSERT INTO users (username, full_name, password_hash, role, is_active, created_at)
               VALUES (?, ?, ?, ?, 1, ?)""",
            (username, username, generate_password_hash("secret"), role,
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def login(client, username):
    return client.post("/api/auth/login", json={"username": username, "password": "secret"})


def photo_upload(name="kadr.jpg"):
    return {"file": (BytesIO(JPEG), name)}


# Заголовок защиты от межсайтовой подделки (require_ajax_header в src/auth.py).
# Дашборд шлёт его со всех изменяющих запросов — тест обязан вести себя так же,
# иначе он проверяет не тот путь.
AJAX = {"X-Requested-With": "barhat-dashboard"}


app = Flask(__name__)
app.secret_key = "test-secret"
login_manager.init_app(app)
login_manager.login_view = None
app.register_blueprint(auth_bp)
app.register_blueprint(writeoff_server.writeoffs_bp)
with app.app_context():
    init_auth_tables()

# Отправку в МойСклад подменяем: наружу ходить нельзя, а проверить надо ровно
# то, что согласование ПРОХОДИТ, когда фото есть.
writeoff_server._send_to_moysklad = (
    lambda writeoff_id, *a, **k: mark_writeoff_sent(writeoff_id, "test-loss-id")
)

add_user("florist_wo", "florist")
add_user("manager_wo", "manager")
add_user("florist_other", "florist")
set_user_stores("florist_wo", [1])
set_user_stores("manager_wo", [1])
# Салон обязателен: без него ветка видимости не исполняется и 500 в ней не виден
set_user_stores("florist_other", [2])

conn = get_db()
try:
    conn.execute("INSERT INTO writeoffs (id, store_id, created_by) VALUES (30, 1, 'florist_wo')")
    conn.execute(
        """INSERT INTO writeoff_positions
           (writeoff_id, moysklad_product_id, moysklad_product_href, product_name, quantity, uom_name)
           VALUES (30, 'p1', 'h1', 'Роза', 3, 'шт')"""
    )
    conn.commit()
finally:
    conn.close()

florist = app.test_client()
manager = app.test_client()
stranger = app.test_client()
check("Флорист логинится", login(florist, "florist_wo").status_code == 200)
check("Управляющий логинится", login(manager, "manager_wo").status_code == 200)
check("Флорист чужой точки логинится", login(stranger, "florist_other").status_code == 200)

no_photo = manager.post("/api/writeoffs/30/approve", headers=AJAX)
check("Заявку без фото согласовать нельзя", no_photo.status_code == 400,
      f"код {no_photo.status_code}")
check("Текст отказа подсказывает, что делать",
      "Добавьте фото" in (no_photo.get_json() or {}).get("error", ""),
      (no_photo.get_json() or {}).get("error", ""))

alien = stranger.post("/api/writeoffs/30/photos", data=photo_upload(),
                      content_type="multipart/form-data", headers=AJAX)
check("Чужой точке загрузка запрещена", alien.status_code == 403, f"код {alien.status_code}")

# Межсайтовая подделка: multipart-POST — «простой» запрос, его отправила бы и
# чужая форма. У сотрудников из портала Пульс кука с SameSite=None, то есть
# защиты Lax у них нет вообще (см. require_ajax_header в src/auth.py).
csrf = florist.post("/api/writeoffs/30/photos", data=photo_upload(),
                    content_type="multipart/form-data")
check("Загрузка без заголовка защиты отбита", csrf.status_code == 403,
      f"код {csrf.status_code}")
csrf_approve = manager.post("/api/writeoffs/30/approve")
check("Согласование без заголовка защиты отбито", csrf_approve.status_code == 403,
      f"код {csrf_approve.status_code}")
csrf_del = manager.delete("/api/writeoffs/photos/1")
check("Удаление фото без заголовка защиты отбито", csrf_del.status_code == 403,
      f"код {csrf_del.status_code}")
check("И ничего не записалось", not get_writeoff_photos(30), f"фото: {len(get_writeoff_photos(30))}")

# Вот он, выход из тупика: фото доливается в УЖЕ СОЗДАННУЮ заявку
added = florist.post("/api/writeoffs/30/photos", data=photo_upload(),
                     content_type="multipart/form-data", headers=AJAX)
check("Фото дозаливается в существующую заявку", added.status_code == 201,
      f"код {added.status_code}")
photo_id = (added.get_json() or {}).get("photo", {}).get("id")

listing = manager.get("/api/writeoffs/30/photos")
check("Управляющий видит фото заявки",
      listing.status_code == 200 and len(listing.get_json()["photos"]) == 1)

download = manager.get(f"/api/writeoffs/photos/{photo_id}/download")
check("Фото скачивается и это тот же файл",
      download.status_code == 200 and download.data == JPEG,
      f"код {download.status_code}")

only_one = florist.delete(f"/api/writeoffs/photos/{photo_id}", headers=AJAX)
check("Единственное фото удалить нельзя (409, а не 500)", only_one.status_code == 409,
      f"код {only_one.status_code}")

approved = manager.post("/api/writeoffs/30/approve", headers=AJAX)
check("С фото согласование проходит", approved.status_code == 200, f"код {approved.status_code}")
check("Заявка ушла в МойСклад",
      get_writeoff_head(30)["status"] == "sent", get_writeoff_head(30)["status"])

late = florist.post("/api/writeoffs/30/photos", data=photo_upload(),
                    content_type="multipart/form-data", headers=AJAX)
check("В согласованную заявку фото уже не добавить", late.status_code == 409,
      f"код {late.status_code}")
late_del = manager.delete(f"/api/writeoffs/photos/{photo_id}", headers=AJAX)
check("И удалить из согласованной нельзя", late_del.status_code == 409,
      f"код {late_del.status_code}")


print("\n=== 11. Цена загрузки фото в запросе ===")
# Сторож против возврата дорогого пути: раньше проверка доступа тянула ВСЮ
# заявку со всеми позициями и вложениями ради одного store_id.
conn = get_db()
try:
    conn.execute("INSERT INTO writeoffs (id, store_id, created_by) VALUES (40, 1, 'florist_wo')")
    for i in range(6):
        conn.execute(
            """INSERT INTO writeoff_positions
               (writeoff_id, moysklad_product_id, moysklad_product_href, product_name, quantity, uom_name)
               VALUES (40, ?, ?, ?, 1, 'шт')""",
            (f"q{i}", f"qh{i}", f"Товар {i}"),
        )
    conn.commit()
finally:
    conn.close()

import sqlite_conn as sqlite_conn_module  # noqa: E402

connects = []
_real_sqlite_connect = sqlite3.connect


def counting_connect(*args, **kwargs):
    connects.append(args[0] if args else kwargs.get("database"))
    return _real_sqlite_connect(*args, **kwargs)


sqlite3.connect = counting_connect
sqlite_conn_module.sqlite3.connect = counting_connect
try:
    connects.clear()
    upload = florist.post("/api/writeoffs/40/photos", data=photo_upload("cost.jpg"),
                          content_type="multipart/form-data", headers=AJAX)
    upload_connects = len(connects)
finally:
    sqlite3.connect = _real_sqlite_connect
    sqlite_conn_module.sqlite3.connect = _real_sqlite_connect

check("Загрузка прошла", upload.status_code == 201, f"код {upload.status_code}")
# Потолок 4 — это учётка (user_loader), точки пользователя (check_store_access,
# общий путь всех модулей), заголовок заявки и вставка. Было пять: отдельно
# читалась позиция, а затем ВСЯ заявка — с запросом на каждую из шести позиций
# и на вложения каждой, то есть ~15 запросов вместо четырёх.
check(
    f"Загрузка фото открыла базу {upload_connects} раз(а) (потолок 4)",
    upload_connects <= 4,
)


print("\n" + "=" * 60)
if failures:
    print(f"ПРОВАЛЕНО проверок: {len(failures)}")
    for name in failures:
        print(f"  - {name}")
    sys.exit(1)
print("Все проверки прошли.")
