"""
Сторож ленты заказов курьера (Фаза 2, API).

Проверяет на живом приложении то, что нельзя проверить чтением кода:

  - курьер видит ТОЛЬКО свой город (фильтр на сервере, а не в интерфейсе);
  - до брони ему не отдают телефон и комментарии, а адрес урезан до улицы;
  - заказы аутсорса (Яндекс.Доставка) в ленту не попадают — у них и адреса нет;
  - в ленту не попадают статусы вне справочника (отменённые, выполненные);
  - курьер без города получает пустой список и объяснение, а не чужие заказы;
  - карточка по прямой ссылке проверяет город так же, как список.

Базы временные, сеть заблокирована.

Запуск: python scripts/test_courier_orders_api.py
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

WORK_DIR = tempfile.mkdtemp(prefix="courier_api_")
os.environ["BARHAT_DB_PATH"] = os.path.join(WORK_DIR, "barhat.db")
os.environ["COURIERS_DB_PATH"] = os.path.join(WORK_DIR, "couriers.db")
os.environ["PYRUS_DB_PATH"] = os.path.join(WORK_DIR, "pyrus.db")
os.environ["MOYSKLAD_DB_PATH"] = os.path.join(WORK_DIR, "moysklad.db")

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"  [ok] {name}")
    else:
        print(f"  [FAIL] {name} {detail}")
        failures.append(name)


import auth  # noqa: E402
from couriers import delivery_storage as ds  # noqa: E402
from couriers import storage  # noqa: E402
from pyrus.server import app  # noqa: E402

app.config["TESTING"] = True
print(f"\nвременные базы: {WORK_DIR}")

TODAY = "2026-09-10"

# --- данные ----------------------------------------------------------------
storage.init_couriers_tables()
ds.init_delivery_tables()
storage.upsert_sites([
    {"code": "nsk-voskhod-3", "name": "НСК Восход 3", "city": "Новосибирск"},
    {"code": "barkhat-ekb", "name": "ЕКБ Ленина", "city": "Екатеринбург"},
])
storage.upsert_delivery_types([
    {"code": "dostavka-kurerom", "name": "Доставка курьером", "active": True},
    {"code": "ya-dostavka", "name": "Яндекс Доставка", "active": True},
])
storage.upsert_order_statuses([
    {"code": "send-to-florist", "name": "Передан флористу", "group_code": "assembling", "active": True},
    {"code": "order-complete", "name": "Заказ готов", "group_code": "assembling", "active": True},
    {"code": "complete", "name": "Выполнен", "group_code": "complete", "active": True},
])


def order(order_id, city, site, status="send-to-florist", code="dostavka-kurerom"):
    return {
        "retailcrm_order_id": order_id, "order_number": str(order_id),
        "delivery_date": TODAY, "status": status, "net_cost": 300,
        "site_code": site, "city": city, "delivery_code": code,
        "address_text": "ул. Ленина, 45, кв. 12, подъезд 2, код 1234",
        "delivery_time_from": "14:00", "delivery_time_to": "15:00",
        "recipient_name": "Евгения", "recipient_phone": "+79130000003",
        "customer_name": "Ирина", "customer_phone": "+79130000001",
        "manager_comment": "Домофон не работает", "do_not_contact_recipient": 1,
        "ready_planned_at": f"{TODAY} 13:00:00", "items": [],
    }


storage.replace_orders_window(TODAY, TODAY, [
    order(1, "Новосибирск", "nsk-voskhod-3"),
    order(2, "Новосибирск", "nsk-voskhod-3", status="order-complete"),
    order(3, "Новосибирск", "nsk-voskhod-3", status="complete"),        # выполнен
    order(4, "Новосибирск", "nsk-voskhod-3", code="ya-dostavka"),       # аутсорс
    order(5, "Екатеринбург", "barkhat-ekb"),                            # чужой город
])

with app.app_context():
    auth.init_auth_tables()
    from werkzeug.security import generate_password_hash
    for username, role in (("kurier-nsk", "courier"), ("kurier-bez-goroda", "courier"),
                           ("upravl", "manager")):
        conn = auth.get_db()
        try:
            conn.execute(
                "INSERT INTO users (username, full_name, password_hash, role, is_active, created_at) "
                "VALUES (?, ?, ?, ?, 1, datetime('now'))",
                (username, username, generate_password_hash("Parol12345"), role))
            conn.commit()
        finally:
            conn.close()
    auth.migrate_permissions_for_existing_users()
    auth.migrate_new_module_permissions("courier_dispatch", ["admin", "manager"])

    conn = auth.get_db()
    try:
        rows = {r["username"]: r["id"] for r in conn.execute("SELECT id, username FROM users")}
    finally:
        conn.close()

ds.save_courier_profile(rows["kurier-nsk"], "kurier-nsk", "Новосибирск", 101)


def login(client, username):
    return client.post("/api/auth/login",
                       json={"username": username, "password": "Parol12345"})


print("\n1. Курьер видит только свой город и только курьерскую доставку")

with app.test_client() as client:
    login(client, "kurier-nsk")
    body = client.get(f"/api/courier/orders?date_from={TODAY}&date_to={TODAY}").get_json()
    ids = sorted(o["retailcrm_order_id"] for o in body["data"])
    check("в ленте только заказы своего города и своей доставки", ids == [1, 2],
          f"получено {ids}")
    check("выполненный заказ не показан", 3 not in ids)
    check("заказ Яндекс.Доставки не показан", 4 not in ids)
    check("чужой город не показан", 5 not in ids)
    check("город в ответе — свой", body["meta"]["city"] == "Новосибирск")

    ready = {o["retailcrm_order_id"]: o["is_ready"] for o in body["data"]}
    check("бейдж готовности: «Заказ готов» → готов", ready.get(2) is True)
    check("бейдж готовности: «Передан флористу» → не готов", ready.get(1) is False)
    check("счётчики в сводке", body["meta"]["free"] == 2 and body["meta"]["ready"] == 1,
          body["meta"])

print("\n2. До брони контактов нет, адрес урезан")

with app.test_client() as client:
    login(client, "kurier-nsk")
    first = client.get(f"/api/courier/orders?date_from={TODAY}&date_to={TODAY}"
                       ).get_json()["data"][0]
    check("телефона получателя нет в списке", "recipient_phone" not in first)
    check("телефона заказчика нет в списке", "customer_phone" not in first)
    check("комментария оператора нет в списке", "manager_comment" not in first)
    check("адрес урезан до улицы и дома", first["address_text"] == "ул. Ленина, 45",
          first["address_text"])
    check("время доставки видно (по нему и решают)",
          first["delivery_time_from"] == "14:00")

    card = client.get("/api/courier/orders/1").get_json()["data"]
    check("в карточке до брони телефона тоже нет", "recipient_phone" not in card)
    check("карточка отдаёт состав заказа", "items" in card)

print("\n3. Управляющий видит контакты и чужие города")

with app.test_client() as client:
    login(client, "upravl")
    body = client.get(f"/api/courier/orders?date_from={TODAY}&date_to={TODAY}").get_json()
    ids = sorted(o["retailcrm_order_id"] for o in body["data"])
    check("видит оба города", ids == [1, 2, 5], f"получено {ids}")
    first = [o for o in body["data"] if o["retailcrm_order_id"] == 1][0]
    check("контакты открыты", first.get("recipient_phone") == "+79130000003")
    check("полный адрес", first["address_text"].startswith("ул. Ленина, 45, кв. 12"))
    check("флаг «не связываться» виден", first.get("do_not_contact_recipient") == 1)

print("\n4. Курьер без города не получает чужих заказов")

with app.test_client() as client:
    login(client, "kurier-bez-goroda")
    body = client.get(f"/api/courier/orders?date_from={TODAY}&date_to={TODAY}").get_json()
    check("список пуст", body["data"] == [])
    check("объяснена причина", "город" in (body["meta"].get("warning") or "").lower(),
          body["meta"])
    check("карточка чужого заказа не отдаётся",
          client.get("/api/courier/orders/1").status_code == 403)

print("\n5. Карточка по прямой ссылке проверяет город")

with app.test_client() as client:
    login(client, "kurier-nsk")
    check("свой заказ открывается", client.get("/api/courier/orders/1").status_code == 200)
    check("заказ чужого города — 404, а не данные",
          client.get("/api/courier/orders/5").status_code == 404)
    check("несуществующий заказ — 404",
          client.get("/api/courier/orders/999").status_code == 404)

print("\n6. Пустой справочник статусов = ничего не показываем")

with storage.get_db() as conn:
    conn.execute("DELETE FROM courier_visible_statuses")

with app.test_client() as client:
    login(client, "kurier-nsk")
    body = client.get(f"/api/courier/orders?date_from={TODAY}&date_to={TODAY}").get_json()
    check("без настройки лента пуста, а не «показать всё»", body["data"] == [])

ds.init_delivery_tables()   # вернуть сид справочника

print("\n7. Настройки статусов — только админу")

with app.test_client() as client:
    login(client, "kurier-nsk")
    check("курьер не читает справочник статусов",
          client.get("/api/courier/statuses").status_code == 403)
    check("курьер не меняет справочник",
          client.post("/api/courier/statuses/send-to-florist",
                      json={"role": "visible"},
                      headers={"X-Requested-With": "XMLHttpRequest"}).status_code == 403)

print("\n8. Профиль: курьер видит предупреждение о связке с CRM")

with app.test_client() as client:
    login(client, "kurier-bez-goroda")
    profile = client.get("/api/courier/profile").get_json()["data"]
    check("нет связки с CRM — есть предупреждение про оплату",
          profile["warning"] and "оплат" in profile["warning"].lower(), profile)

with app.test_client() as client:
    login(client, "kurier-nsk")
    profile = client.get("/api/courier/profile").get_json()["data"]
    check("со связкой предупреждения нет", profile["warning"] is None, profile)
    check("настройки города отданы", profile["settings"]["max_active_claims"] >= 1)

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} — {failures}")
    sys.exit(1)
print("Все проверки пройдены")
