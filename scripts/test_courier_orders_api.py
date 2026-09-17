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
from datetime import datetime, timedelta

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

    # «Сегодня» для выбора дня в приложении считается по стенным часам САЛОНА.
    # По серверной дате в 18:31 UTC курьер из Новосибирска получил бы вчера,
    # то есть пустую ленту в начале рабочего дня.
    salon_today = (datetime.utcnow() + timedelta(hours=7)).date().isoformat()
    check("сегодня отдаётся по поясу салона", profile["today"] == salon_today,
          f"({profile['today']} против {salon_today})")

print("\n9. Лента показывает выбранный день")

with app.test_client() as client:
    login(client, "kurier-nsk")
    one_day = client.get(f"/api/courier/orders?date_from={salon_today}"
                         f"&date_to={salon_today}").get_json()
    check("период уважается", one_day["meta"]["date_from"] == salon_today
          and one_day["meta"]["date_to"] == salon_today, one_day.get("meta"))
    check("в выдаче только этот день",
          all(row["delivery_date"] == salon_today for row in one_day["data"]),
          f"({[row['delivery_date'] for row in one_day['data']][:5]})")

print("\n10. Цена экрана не растёт вместе с числом заказов")
# 16.09.2026 приложение «стало тупить»: лента брала последнюю бронь заказа
# подзапросом, а индекса по retailcrm_order_id для всех состояний не было —
# SQLite сканировал таблицу броней на КАЖДУЮ строку витрины (395 мс против
# 33 мс). Экран управляющего читал настройки города своим соединением внутри
# цикла по заказам — 840 мс на 750 строк.
#
# Время на маленькой тестовой базе ничего не покажет, поэтому проверяем
# причину: план запроса и число обращений к базе.

import sqlite3 as _sqlite3  # noqa: E402

with storage.get_db() as conn:
    indexes = {row["name"] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' "
        " AND tbl_name = 'delivery_assignments'")}
check("есть индекс поиска брони по заказу", "idx_assign_order" in indexes,
      f"({sorted(indexes)})")

with storage.get_db() as conn:
    plan = " | ".join(row["detail"] for row in conn.execute(f"""
        EXPLAIN QUERY PLAN
        SELECT o.retailcrm_order_id FROM courier_orders o
        LEFT JOIN delivery_assignments a ON a.id = (
            SELECT MAX(x.id) FROM delivery_assignments x
             WHERE x.retailcrm_order_id = o.retailcrm_order_id
               AND x.state IN ({ds.BLOCKING_STATES_SQL}))
        WHERE o.delivery_date = ?""", (salon_today,)))
check("бронь ищется по индексу, а не сканом таблицы",
      "SCAN delivery_assignments" not in plan and "SCAN x" not in plan, f"({plan})")

# Число обращений к базе не должно зависеть от количества заказов: иначе
# каждый новый день работы делает экран медленнее
_real_connect = _sqlite3.connect
opened = []


def _counting_connect(*args, **kwargs):
    opened.append(args[0] if args else kwargs.get("database"))
    return _real_connect(*args, **kwargs)


def _count_calls(fn):
    del opened[:]
    fn()
    return len(opened)


# Пояс салона обязателен: без него ветка «никто не взял» не исполняется
# вовсе, и сторож проверял бы код, который не работает (тот же класс, что
# «тестовый менеджер без салонов»).
with storage.get_db() as conn:
    conn.execute("UPDATE courier_sites SET utc_offset = 7 WHERE code = 'nsk-voskhod-3'")

_sqlite3.connect = _counting_connect
try:
    few = _count_calls(lambda: ds.dispatch_overview(
        "Новосибирск", salon_today, salon_today, ["dostavka-kurerom"]))

    with storage.get_db() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO courier_orders (retailcrm_order_id, order_number, "
            "  delivery_date, delivery_time_from, site_code, city, status, delivery_code) "
            "VALUES (?, ?, ?, '18:00', 'nsk-voskhod-3', 'Новосибирск', 'send-to-florist', "
            "        'dostavka-kurerom')",
            [(90000 + i, str(90000 + i), salon_today) for i in range(60)])

    many = _count_calls(lambda: ds.dispatch_overview(
        "Новосибирск", salon_today, salon_today, ["dostavka-kurerom"]))
finally:
    _sqlite3.connect = _real_connect

# Допуск в два соединения — это настройки города (одно на город) и запас на
# будущие справочники. Шестьдесят новых заказов не должны добавлять шестьдесят
# обращений: именно так экран и стал медленным.
check("обзор управляющего не открывает соединение на каждый заказ",
      many <= few + 2, f"(было {few} на пустом дне, стало {many} на +60 заказов)")


print("\n11. Лента отчитывается, за что потратила время")
# 16.09.2026 ручка отвечала 53–113 секунд на проде при форме запроса, которая
# обязана укладываться в десятки миллисекунд. Общий сторож медленных запросов
# называл ручку и общее время, но не шаг — и причину найти не удалось.
# Разбор без числа стоил сорока минут простоя (CLAUDE.md: меряй раньше, чем
# чинишь), поэтому разложение по шагам теперь часть ручки и обязано жить.
#
# Проверяем СВЯЗКУ, а не наличие слова в коде: шаги должны реально считаться
# (а не приходить нулями-заглушками) и покрывать оба подозреваемых — открытие
# соединения на сетевом диске и сам SELECT.
with app.test_client() as client:
    login(client, "kurier-nsk")
    meta = client.get(f"/api/courier/orders?date_from={salon_today}"
                      f"&date_to={salon_today}").get_json()["meta"]
    timings = meta.get("timings_ms") or {}

    required = ["auth", "dispatch", "city", "delivery_codes",
                "visible_codes", "connect", "query", "serialize"]
    missing = [name for name in required if name not in timings]
    check("разбор по шагам отдаётся целиком", not missing, f"(нет: {missing})")

    # Ноль по всем шагам сразу означает, что меряет заглушка, а не код:
    # на любой машине хоть один шаг стоит доли миллисекунды.
    measured = [timings.get(name) for name in required if name in timings]
    check("шаги посчитаны, а не заполнены нулями",
          any(isinstance(v, (int, float)) and v > 0 for v in measured),
          f"({timings})")

    # Сумма шагов не может превышать общее время ручки: если превышает —
    # отметки расставлены внахлёст и числу верить нельзя.
    steps_sum = sum(v for name, v in timings.items()
                    if name in required and isinstance(v, (int, float)))
    check("сумма шагов не больше общего времени",
          steps_sum <= (meta.get("total_ms") or 0) + 1,
          f"(шаги {steps_sum} мс, всего {meta.get('total_ms')} мс)")

    # Сколько строк посчитали — без этого «query=400 мс» не с чем сравнить:
    # 400 мс на 12 заказов и на 12 тысяч это разные диагнозы.
    check("в разборе есть число строк", "rows" in timings, f"({timings})")


print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} — {failures}")
    sys.exit(1)
print("Все проверки пройдены")
