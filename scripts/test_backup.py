"""
Офлайн-тесты резервных копий: снимок баз и вложений — без сети.

До 23.09.2026 у сервиса не было ни одной резервной копии: консоли контейнера
на тарифе Amvera нет, эндпоинта выгрузки не было. Любая миграция шла без
страховки, а перевезти данные между аккаунтами было нечем.

Проверяет:
  1. Снимок согласован при активном WAL — наивная копия файла НЕ согласована
  2. Права: админ да, управляющий нет, аноним нет
  3. Имя базы только из белого списка — обхода каталога нет
  4. Загрузка: не SQLite, пустой файл, битый файл, поверх непустой базы
  5. Загрузка на пустой экземпляр проходит, данные читаются
  6. Загрузка без заголовка защиты от межсайтовой подделки — 403
  7. Вложения выгружаются архивом, внутри те же файлы
  8. Скачивание и загрузка попадают в аудит

Запуск: python scripts/test_backup.py
"""

import io
import os
import socket
import sqlite3
import sys
import tempfile
import time
import zipfile
from datetime import datetime, timezone

if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')


# Сеть отключаем ДО импорта приложения: локальный прогон читает боевой .env
# и иначе может уйти в боевые МойСклад/RetailCRM/ПланФакт.
class NoNetwork(socket.socket):
    def connect(self, *a, **k):
        raise OSError("сеть отключена намеренно")

    def connect_ex(self, *a, **k):
        raise OSError("сеть отключена намеренно")


socket.socket = NoNetwork

# Пути к данным — до импорта модулей: они резолвятся на уровне модуля.
_tmp_dir = tempfile.mkdtemp()
os.environ["BARHAT_DB_PATH"] = os.path.join(_tmp_dir, "barhat.db")
os.environ["COURIERS_DB_PATH"] = os.path.join(_tmp_dir, "couriers.db")
os.environ["PYRUS_DB_PATH"] = os.path.join(_tmp_dir, "pyrus.db")
os.environ["MOYSKLAD_DB_PATH"] = os.path.join(_tmp_dir, "moysklad.db")
# Целевая база для проверки загрузки — её НЕ создаём, она должна быть пустой.
os.environ["LINKWATCH_DB_PATH"] = os.path.join(_tmp_dir, "linkwatch.db")
os.environ["INVOICE_ATTACHMENTS_DIR"] = os.path.join(_tmp_dir, "invoice_attachments")
os.environ["WRITEOFF_ATTACHMENTS_DIR"] = os.path.join(_tmp_dir, "writeoff_attachments")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from flask import Flask  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

from auth import auth_bp, init_auth_tables, login_manager  # noqa: E402
from backup.server import backup_bp  # noqa: E402
from sqlite_conn import connect as sqlite_connect  # noqa: E402

failures = []


def check(title, ok, detail=""):
    mark = "OK  " if ok else "ПРОВАЛ"
    print(f"  [{mark}] {title}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(title)


# Заголовок защиты от межсайтовой подделки (require_ajax_header в src/auth.py).
# Дашборд шлёт его со всех изменяющих запросов — тест обязан вести себя так же.
AJAX = {"X-Requested-With": "barhat-dashboard"}

app = Flask(__name__)
app.secret_key = "test-secret"
login_manager.init_app(app)
login_manager.login_view = None
app.register_blueprint(auth_bp)
app.register_blueprint(backup_bp)
with app.app_context():
    init_auth_tables()


def add_user(username, role):
    conn = sqlite_connect(os.environ["BARHAT_DB_PATH"])
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


add_user("admin_bk", "admin")
add_user("manager_bk", "manager")

admin = app.test_client()
manager = app.test_client()
anon = app.test_client()
login(admin, "admin_bk")
login(manager, "manager_bk")


print("\n=== 1. Снимок согласован при активном WAL ===")
# Ради этого раздела и написан весь модуль. Базы работают в WAL: только что
# принятые записи лежат в соседнем -wal и в сам файл ещё не попали. Копия
# файла на ходу приезжает БЕЗ них и выглядит целой — повреждение обнаружится
# в момент восстановления. Раздел падает, если снимок подменят на shutil.copy.

couriers_path = os.environ["COURIERS_DB_PATH"]
live = sqlite_connect(couriers_path)
live.execute("CREATE TABLE deliveries (id INTEGER PRIMARY KEY, note TEXT)")
live.commit()
for i in range(200):
    live.execute("INSERT INTO deliveries (note) VALUES (?)", (f"доставка {i}",))
live.commit()
# Соединение НЕ закрываем: пока оно живо, чекпоинт в основной файл не прошёл —
# ровно та ситуация, в которой работает прод (2 воркера по 8 потоков).

naive_copy = os.path.join(_tmp_dir, "naive.db")
with open(couriers_path, "rb") as src, open(naive_copy, "wb") as dst:
    dst.write(src.read())


def count_rows(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]
    except sqlite3.DatabaseError as e:
        return f"ошибка: {e}"
    finally:
        conn.close()


naive_rows = count_rows(naive_copy)
check("Наивная копия файла теряет данные из WAL", naive_rows != 200,
      f"в копии {naive_rows} из 200")

response = admin.get("/api/backup/db/couriers")
check("Снимок скачивается", response.status_code == 200, f"HTTP {response.status_code}")
snapshot = os.path.join(_tmp_dir, "snapshot.db")
with open(snapshot, "wb") as f:
    f.write(response.data)
check("В снимке все записи", count_rows(snapshot) == 200, f"строк {count_rows(snapshot)}")

probe = sqlite3.connect(snapshot)
try:
    verdict = probe.execute("PRAGMA integrity_check").fetchone()[0]
finally:
    probe.close()
check("Снимок проходит integrity_check", verdict == "ok", verdict)

disposition = response.headers.get("Content-Disposition", "")
check("Имя файла со штампом времени", "couriers-" in disposition and ".db" in disposition,
      disposition)

# Временный снимок не должен оставаться на диске: /data маленький, а базы
# большие — забытые копии съедят место молча. Ответ закрываем сами: браузер
# закрывает соединение, тестовый клиент — нет, а удаление висит на закрытии.
response.close()
leftovers = [n for n in os.listdir(_tmp_dir) if n.startswith(".backup-")]
check("Временный снимок удалён после отдачи", not leftovers, str(leftovers))

# Хвост от упавшего воркера: файл никто не закроет и не удалит. Его должна
# убрать следующая выгрузка, иначе такие хвосты копятся до конца места.
stale = os.path.join(_tmp_dir, ".backup-couriers-brosheno.db")
with open(stale, "wb") as f:
    f.write(b"x" * 1024)
os.utime(stale, (time.time() - 7200, time.time() - 7200))
admin.get("/api/backup/db/couriers").close()
check("Брошенный хвост убран следующей выгрузкой", not os.path.exists(stale))

live.close()


print("\n=== 2. Права ===")
check("Управляющему выгрузка запрещена",
      manager.get("/api/backup/db/couriers").status_code == 403)
check("Управляющему список целей запрещён",
      manager.get("/api/backup/targets").status_code == 403)
check("Анониму выгрузка запрещена",
      anon.get("/api/backup/db/couriers").status_code in (401, 302, 403))
check("Админу список целей доступен",
      admin.get("/api/backup/targets").status_code == 200)

targets = admin.get("/api/backup/targets").get_json()
names = {item["name"] for item in targets["databases"]}
check("В списке целей все базы", names == set(
    ["barhat", "couriers", "pyrus", "moysklad", "linkwatch"]), str(sorted(names)))
couriers_target = next(i for i in targets["databases"] if i["name"] == "couriers")
check("У базы показан размер", couriers_target["size_mb"] > 0, str(couriers_target["size_mb"]))
check("У базы есть подпись, что в ней лежит", bool(couriers_target["note"]))


print("\n=== 3. Имя базы только из белого списка ===")
# Параметр маршрута, подставленный в путь к файлу, — это обход каталога.
# Проверять «нет ли ..» бессмысленно: разбирать будет не наш код (правило
# про чужой парсер в CLAUDE.md). Поэтому здесь белый список, и точка.
for evil in ("../auth", "..%2f..%2fetc%2fpasswd", "barhat.db", "%2e%2e%2fbarhat"):
    r = admin.get(f"/api/backup/db/{evil}")
    check(f"Отказ на «{evil}»", r.status_code == 404, f"HTTP {r.status_code}")


print("\n=== 4. Загрузка: что отбивается ===")
from io import BytesIO  # noqa: E402


def restore(name, data, filename="dump.db", headers=AJAX, client=admin):
    return client.post(f"/api/backup/restore/{name}",
                       data={"file": (BytesIO(data), filename)},
                       headers=headers,
                       content_type="multipart/form-data")


r = restore("linkwatch", b"")
check("Пустой файл отбивается", r.status_code == 400, f"HTTP {r.status_code}")

r = restore("linkwatch", "это не база, а текст".encode("utf-8") * 10)
check("Не-SQLite отбивается", r.status_code == 400, f"HTTP {r.status_code}")

# Битый файл: правильная сигнатура, мусор внутри. Отличается от предыдущего
# тем, что проверку «это SQLite» он проходит, а integrity_check — нет.
with open(snapshot, "rb") as f:
    broken = bytearray(f.read())
for offset in range(4096, min(len(broken), 20000)):
    broken[offset] = (broken[offset] + 7) % 256
r = restore("linkwatch", bytes(broken))
check("Битый файл отбивается", r.status_code == 400, f"HTTP {r.status_code}")

with open(snapshot, "rb") as f:
    good = f.read()

r = restore("couriers", good)
check("Поверх непустой базы не пишем", r.status_code == 409, f"HTTP {r.status_code}")

r = restore("linkwatch", good, headers={})
check("Без заголовка защиты — 403", r.status_code == 403, f"HTTP {r.status_code}")

r = restore("linkwatch", good, client=manager)
check("Управляющему загрузка запрещена", r.status_code == 403, f"HTTP {r.status_code}")


print("\n=== 5. Загрузка на пустой экземпляр ===")
r = restore("linkwatch", good)
check("Загрузка принята", r.status_code == 200, f"HTTP {r.status_code}: {r.data[:200]}")
if r.status_code == 200:
    body = r.get_json()
    check("В ответе размер и число таблиц", body["size_mb"] > 0 and body["tables"] >= 1,
          f"{body['size_mb']} МБ, таблиц {body['tables']}")
    check("Данные читаются из загруженной базы",
          count_rows(os.environ["LINKWATCH_DB_PATH"]) == 200)

leftovers = [n for n in os.listdir(_tmp_dir) if n.startswith(".restore-")]
check("Временные файлы загрузки не остались", not leftovers, str(leftovers))


print("\n=== 6. Вложения архивом ===")
invoice_dir = os.environ["INVOICE_ATTACHMENTS_DIR"]
os.makedirs(invoice_dir, exist_ok=True)
for name in ("schet-1.pdf", "schet-2.pdf"):
    with open(os.path.join(invoice_dir, name), "wb") as f:
        f.write(b"%PDF-1.4 fake " + name.encode())

r = admin.get("/api/backup/attachments/invoices")
check("Архив вложений скачивается", r.status_code == 200, f"HTTP {r.status_code}")
archive_path = os.path.join(_tmp_dir, "attachments.zip")
with open(archive_path, "wb") as f:
    f.write(r.data)
r.close()
with zipfile.ZipFile(archive_path) as z:
    inside = sorted(z.namelist())
    check("Внутри те же файлы", inside == ["schet-1.pdf", "schet-2.pdf"], str(inside))
    check("Содержимое совпадает", z.read("schet-1.pdf").endswith(b"schet-1.pdf"))

check("Отказ на неизвестной папке",
      admin.get("/api/backup/attachments/secrets").status_code == 404)
check("Управляющему вложения запрещены",
      manager.get("/api/backup/attachments/invoices").status_code == 403)

leftovers = [n for n in os.listdir(invoice_dir) if n.startswith(".backup-")]
check("Временный архив удалён", not leftovers, str(leftovers))


print("\n=== 7. Аудит ===")
# Ручка отдаёт боевую базу целиком, включая хеши паролей. Без записи «кто и
# когда скачал» такую возможность нельзя держать открытой вообще.
# Аудит пишется фоновой очередью (см. log_action), поэтому ждём с повтором.
def audit_rows():
    conn = sqlite_connect(os.environ["BARHAT_DB_PATH"])
    try:
        return conn.execute(
            "SELECT action, details FROM audit_log WHERE action LIKE 'backup_%'").fetchall()
    except sqlite3.DatabaseError:
        return []
    finally:
        conn.close()


rows = []
for _ in range(50):
    rows = audit_rows()
    if any(r["action"] == "backup_download" for r in rows) and \
       any(r["action"] == "backup_restore" for r in rows):
        break
    time.sleep(0.1)

actions = [r["action"] for r in rows]
check("Скачивание записано в аудит", "backup_download" in actions, str(actions))
check("Загрузка записана в аудит", "backup_restore" in actions, str(actions))


print("\n" + "=" * 60)
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)}")
    for title in failures:
        print(f"  - {title}")
    sys.exit(1)
print("Все проверки пройдены")
