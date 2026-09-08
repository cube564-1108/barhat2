"""
Сторож доступа к разделам доставки (Фаза 1).

Проверяет то, что видно только на живом приложении: курьер заходит своей
учёткой и попадает в «Доставку», но не в «Контроль доставки», а страница
открывается и по прямой ссылке — не только кликом из меню. Ровно на этом
месте модуль счетов ловил 404 после F5, а раздел загрузки — невидимый пункт
меню у тех, у кого права уже были.

Работает на ВРЕМЕННЫХ базах и с заблокированной сетью: приложение при импорте
читает боевой .env и умеет ходить в RetailCRM/МойСклад, и сторож не должен
дёргать боевые API.

Запуск: python scripts/test_courier_access.py
"""

import os
import socket
import ssl  # noqa: F401  — импортировать до патча сокета
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))


class NetworkBlocked(Exception):
    pass


def _blocked(*args, **kwargs):
    raise NetworkBlocked("сторож не должен ходить в боевые внешние API")


socket.socket.connect = _blocked

WORK_DIR = tempfile.mkdtemp(prefix="courier_access_")
os.environ["BARHAT_DB_PATH"] = os.path.join(WORK_DIR, "barhat.db")
os.environ["COURIERS_DB_PATH"] = os.path.join(WORK_DIR, "couriers.db")
os.environ["PYRUS_DB_PATH"] = os.path.join(WORK_DIR, "pyrus.db")
os.environ["MOYSKLAD_DB_PATH"] = os.path.join(WORK_DIR, "moysklad.db")
# Планировщики синка стартуют отдельным флагом — в тесте они не нужны.
os.environ["DISABLE_SCHEDULERS"] = "1"

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"  [ok] {name}")
    else:
        print(f"  [FAIL] {name} {detail}")
        failures.append(name)


print(f"\nвременные базы: {WORK_DIR}")

import auth  # noqa: E402
from pyrus.server import app  # noqa: E402

assert auth.DB_PATH == os.environ["BARHAT_DB_PATH"], \
    f"тест пишет не в свою базу: {auth.DB_PATH}"

app.config["TESTING"] = True

# --- учётки: курьер, управляющий, флорист -----------------------------------
with app.app_context():
    auth.init_auth_tables()
    for username, full_name, role in (
        ("kurier-nsk", "Курьер Новосибирск", "courier"),
        ("upravl", "Управляющий", "manager"),
        ("florist", "Флорист", "florist"),
    ):
        conn = auth.get_db()
        try:
            from werkzeug.security import generate_password_hash
            conn.execute(
                "INSERT INTO users (username, full_name, password_hash, role, is_active, created_at) "
                "VALUES (?, ?, ?, ?, 1, datetime('now'))",
                (username, full_name, generate_password_hash("Parol12345"), role),
            )
            conn.commit()
        finally:
            conn.close()
    # Права раздаются той же миграцией, что и на проде, — иначе тест проверял бы
    # не то, что будет после деплоя.
    auth.migrate_permissions_for_existing_users()
    auth.migrate_new_module_permissions("courier_dispatch", ["admin", "manager"])


def login(client, username):
    return client.post("/api/auth/login",
                       json={"username": username, "password": "Parol12345"})


print("\n1. Курьер: своё приложение доступно, контроль — нет")

with app.test_client() as client:
    response = login(client, "kurier-nsk")
    check("курьер входит", response.status_code == 200, response.data[:200])

    me = client.get("/api/auth/me").get_json()
    sections = set((me.get("user") or me).get("sections") or [])
    check("в правах есть courier_app", "courier_app" in sections, sections)
    check("в правах НЕТ courier_dispatch", "courier_dispatch" not in sections, sections)
    check("курьер не получил лишнего (кассы, счета)",
          not sections & {"cash_shifts", "invoices_v2", "users_manage"}, sections)

    check("страница /courier-app открывается по прямой ссылке (не только кликом)",
          client.get("/courier-app").status_code == 200)

print("\n2. Управляющий: контроль доступен")

with app.test_client() as client:
    login(client, "upravl")
    me = client.get("/api/auth/me").get_json()
    sections = set((me.get("user") or me).get("sections") or [])
    check("у управляющего есть courier_dispatch", "courier_dispatch" in sections, sections)
    check("но нет приложения курьера", "courier_app" not in sections, sections)
    check("страница /courier-dispatch открывается",
          client.get("/courier-dispatch").status_code == 200)

print("\n3. Флорист: разделов доставки нет вовсе")

with app.test_client() as client:
    login(client, "florist")
    me = client.get("/api/auth/me").get_json()
    sections = set((me.get("user") or me).get("sections") or [])
    check("флористу доставка не досталась",
          not sections & {"courier_app", "courier_dispatch"}, sections)

print("\n4. Без входа страницы не отдаются")

with app.test_client() as client:
    for path in ("/courier-app", "/courier-dispatch"):
        response = client.get(path)
        check(f"{path} без авторизации уводит на вход",
              response.status_code in (301, 302) and "/login" in response.headers.get("Location", ""),
              f"{response.status_code} → {response.headers.get('Location')}")

print("\n5. Роль курьера принимается админкой (валидация по ROLE_SECTIONS)")

check("«courier» — известная роль", "courier" in auth.ROLE_SECTIONS)
check("секции курьера в общем списке модулей",
      {"courier_app", "courier_dispatch"} <= set(auth.ALL_MODULES))

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} — {failures}")
    sys.exit(1)
print("Все проверки пройдены")
