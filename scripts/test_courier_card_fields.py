"""
Сторож карточки курьера (Фаза 2): поля доезжают от CRM до базы и переживают
глубокий синк.

Два места, где эти поля теряются молча:

1. **Разбор.** Кастомные поля правят в интерфейсе CRM, и галочка приходит то
   `True`, то `"true"`, то `"1"`. Проверка `is True` потеряла бы часть значений
   — а это флаг «не связываться с получателем», сюрприз-доставка.
2. **Глубокий синк.** `replace_orders_window` переписывает окно дат целиком.
   Если поле не попало в его INSERT, оно обнуляется каждые полчаса, и заметить
   это можно только на живом заказе.

Работает на временной базе, в сеть не ходит.

Запуск: python scripts/test_courier_card_fields.py
"""

import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))

TMP_DB = os.path.join(tempfile.mkdtemp(prefix="courier_card_"), "couriers.db")
os.environ["COURIERS_DB_PATH"] = TMP_DB

from couriers import retailcrm, storage  # noqa: E402

assert storage.DB_PATH == TMP_DB, f"тест пишет не в свою базу: {storage.DB_PATH}"

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"  [ok] {name}")
    else:
        print(f"  [FAIL] {name} {detail}")
        failures.append(name)


# Форма ответа — с боевой выгрузки 2026-09-08, значения выдуманы.
ORDER = {
    "id": 555001,
    "number": "12345",
    "status": "send-to-florist",
    "site": "nsk-voskhod-3",
    "shipmentStore": "sklad-nsk",
    "summ": 5400,
    "orderMethod": "offline",
    "managerComment": "Домофон не работает, звонить на телефон",
    "customerComment": "Пожалуйста, к 14:00",
    "customer": {
        "firstName": "Ирина",
        "phones": [{"number": "+79130000001"}, {"number": "+79130000002"}],
    },
    "delivery": {
        "date": "2026-09-10",
        "code": "dostavka-kurerom",
        "netCost": 320,
        "time": {"from": "14:00", "to": "15:00"},
        "address": {"city": "Новосибирск", "text": "ул. Ленина, 45, кв. 12"},
        "data": {"courierId": 101, "firstName": "Курьер 1"},
    },
    "customFields": {
        "recipient_name": "Евгения",
        "recipient_phone": "+79130000003",
        "recipient_customer": False,
        "ne_sviazyvatsia_s_poluchatelem": True,
        "note_text": "Оставить у консьержа",
        "data_i_vremia_gotovnosti": "2026-09-10 13:00:00",
        "order_availability_time": "13:00",
    },
    "items": [
        {"quantity": 1, "offer": {"id": 9001, "displayName": "Букет «Аврора»", "article": "A-1"}},
    ],
}

SITE_CITIES = {"nsk-voskhod-3": "Новосибирск"}

print("\n1. Разбор карточки из ответа CRM")

parsed = retailcrm.parse_order(ORDER, SITE_CITIES)

check("адрес одной строкой", parsed["address_text"] == "ул. Ленина, 45, кв. 12")
check("окно доставки", (parsed["delivery_time_from"], parsed["delivery_time_to"])
      == ("14:00", "15:00"))
check("получатель", (parsed["recipient_name"], parsed["recipient_phone"])
      == ("Евгения", "+79130000003"))
check("флаг «получатель = заказчик» снят", parsed["recipient_is_customer"] == 0)
check("флаг «не связываться» поднят", parsed["do_not_contact_recipient"] == 1)
check("заказчик и его первый телефон",
      (parsed["customer_name"], parsed["customer_phone"]) == ("Ирина", "+79130000001"))
check("оба комментария на месте",
      parsed["manager_comment"].startswith("Домофон")
      and parsed["customer_comment"].startswith("Пожалуйста"))
check("третий комментарий из кастомного поля", parsed["note_text"] == "Оставить у консьержа")
check("плановая готовность — как есть, без сдвигов пояса",
      parsed["ready_planned_at"] == "2026-09-10 13:00:00")

print("\n2. Галочка приходит в разных формах — распознаём все")

for raw, expected in ((True, 1), ("true", 1), ("1", 1), ("да", 1),
                      (False, 0), ("false", 0), ("", 0), (None, 0)):
    order = dict(ORDER)
    order["customFields"] = dict(ORDER["customFields"],
                                 ne_sviazyvatsia_s_poluchatelem=raw)
    got = retailcrm.parse_order(order, SITE_CITIES)["do_not_contact_recipient"]
    check(f"«не связываться» из {raw!r} → {expected}", got == expected, f"получено {got}")

print("\n3. Пустой получатель: курьер будет звонить заказчику")

no_recipient = dict(ORDER)
no_recipient["customFields"] = dict(ORDER["customFields"], recipient_name="",
                                    recipient_phone="", recipient_customer=True)
parsed_empty = retailcrm.parse_order(no_recipient, SITE_CITIES)
check("пустые поля получателя → None, а не пустая строка",
      parsed_empty["recipient_name"] is None and parsed_empty["recipient_phone"] is None)
check("флаг «получатель = заказчик» поднят", parsed_empty["recipient_is_customer"] == 1)
check("телефон заказчика на месте", parsed_empty["customer_phone"] == "+79130000001")

print("\n4. Заказ без блока времени и адреса не ломает разбор")

bare = dict(ORDER)
bare["delivery"] = {"date": "2026-09-10", "code": "ya-dostavka"}
bare["customFields"] = {}
bare["customer"] = {}
parsed_bare = retailcrm.parse_order(bare, SITE_CITIES)
check("адреса нет — None", parsed_bare["address_text"] is None)
check("времени нет — None", parsed_bare["delivery_time_from"] is None)
check("телефонов нет — None", parsed_bare["customer_phone"] is None)
check("флаги без данных — ноль, а не None",
      parsed_bare["do_not_contact_recipient"] == 0
      and parsed_bare["recipient_is_customer"] == 0)

print("\n5. Глубокий синк не теряет карточку")

storage.init_couriers_tables()
storage.upsert_sites([{"code": "nsk-voskhod-3", "name": "НСК Восход 3", "city": "Новосибирск"}])
storage.replace_orders_window("2026-09-10", "2026-09-10", [parsed])

with storage.get_db() as conn:
    row = dict(conn.execute(
        "SELECT * FROM courier_orders WHERE retailcrm_order_id = 555001").fetchone())

check("адрес сохранён", row["address_text"] == "ул. Ленина, 45, кв. 12")
check("окно доставки сохранено",
      (row["delivery_time_from"], row["delivery_time_to"]) == ("14:00", "15:00"))
check("телефон получателя сохранён", row["recipient_phone"] == "+79130000003")
check("флаг «не связываться» сохранён", row["do_not_contact_recipient"] == 1)
check("комментарии сохранены", row["manager_comment"].startswith("Домофон"))
check("плановая готовность сохранена", row["ready_planned_at"] == "2026-09-10 13:00:00")

# Повторный прогон окна — ровно то, что делает планировщик каждые 30 минут
storage.replace_orders_window("2026-09-10", "2026-09-10", [parsed])
with storage.get_db() as conn:
    again = dict(conn.execute(
        "SELECT * FROM courier_orders WHERE retailcrm_order_id = 555001").fetchone())
check("после повторного синка карточка на месте, а не обнулилась",
      again["address_text"] == row["address_text"]
      and again["recipient_phone"] == row["recipient_phone"]
      and again["do_not_contact_recipient"] == 1)

print("\n6. Старые поля витрины не пострадали")

check("выплата курьеру считается по-прежнему", again["net_cost"] == 320)
check("слот загрузки салона на месте", again["ready_hour"] == 13)
check("склад-исполнитель на месте", again["store_key"] == "sklad-nsk")

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} — {failures}")
    sys.exit(1)
print("Все проверки пройдены")
