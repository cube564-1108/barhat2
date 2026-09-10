"""
Сторож записи статусов в CRM (Фаза 5).

Что здесь ловится и почему именно тестом:

1. **Пустой маппинг молча пропускает действие.** Если код статуса выводить
   из названия или подставлять «похожий», отправка уходит в CRM бесполезной,
   а ошибки нет нигде — ровно так счета три недели уезжали в банк без НДС.
   Здесь пустой маппинг обязан БЛОКИРОВАТЬ действие с внятным текстом.
2. **Курьер ждёт CRM.** Отметка обязана сохраняться мгновенно и уходить
   наружу фоном: CRM отвечает секундами и иногда лежит, а воркеров два.
3. **Упавшая задача — вечный кандидат.** Повтор без отсрочки и без предела
   попыток выжигает лимиты внешнего API; на 4xx повторять бессмысленно вовсе.
4. **Флорист сбрасывает бронь.** Отметка «Заказ готов» приходит в тот же
   заказ, и она не должна ничего ломать у курьера.
5. **Курьер не сопоставлен с CRM** — доставка не попадёт в выплаты, и об
   этом надо предупредить, а не молчать до конца месяца.

Запуск: python scripts/test_courier_crm_status.py
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

WORK_DIR = tempfile.mkdtemp(prefix="crm_status_")
os.environ["BARHAT_DB_PATH"] = os.path.join(WORK_DIR, "barhat.db")
os.environ["COURIERS_DB_PATH"] = os.path.join(WORK_DIR, "couriers.db")
os.environ["PYRUS_DB_PATH"] = os.path.join(WORK_DIR, "pyrus.db")
os.environ["MOYSKLAD_DB_PATH"] = os.path.join(WORK_DIR, "moysklad.db")
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

from couriers import delivery_feed  # noqa: E402
from couriers import delivery_storage as ds  # noqa: E402
from couriers import storage as cs  # noqa: E402

app.config["TESTING"] = True
with app.app_context():
    auth.init_auth_tables()
cs.init_couriers_tables()
ds.init_delivery_tables()

TODAY = datetime.utcnow().date()

with cs.get_db() as conn:
    conn.execute("INSERT OR REPLACE INTO courier_sites (code, name, city, utc_offset) "
                 "VALUES ('site-a', 'Восход', 'Новосибирск', 7)")
    for code, name in (("send-to-florist", "Передан флористу"),
                       ("order-complete", "Заказ готов"),
                       ("send-to-delivery", "Передан курьеру"),
                       ("order-delivery-complete", "Заказ доставлен"),
                       ("order-delivery-fail", "Заказ НЕ доставлен")):
        conn.execute("INSERT OR REPLACE INTO order_statuses (code, name) VALUES (?, ?)",
                     (code, name))


def add_order(order_id, status="send-to-florist"):
    with cs.get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO courier_orders "
            "  (retailcrm_order_id, order_number, delivery_date, delivery_time_from, "
            "   site_code, city, status, delivery_code) "
            "VALUES (?, ?, ?, '18:00', 'site-a', 'Новосибирск', ?, 'dostavka-kurerom')",
            (order_id, str(order_id), TODAY.isoformat(), status),
        )


# ============================================================================
print("\n1. Пустой маппинг блокирует действие")
# ============================================================================

add_order(7001)
ds.claim_order(7001, courier_user_id=10, courier_name="Иван", city="Новосибирск")

try:
    ds.advance_assignment(7001, courier_user_id=10, action=ds.ACTION_PICKUP,
                          username="ivan")
    check("без настроенного статуса «Забрал» не проходит", False, "(прошло)")
except ds.ClaimError as e:
    check("без настроенного статуса «Забрал» не проходит",
          e.code == "not_configured", f"({e.code})")
    check("текст объясняет, что делать", "администратора" in str(e), f"({e})")

check("в очередь ничего не попало", not ds.take_outbox_batch(),
      f"({ds.take_outbox_batch()})")


# ============================================================================
print("\n2. Код статуса — из справочника CRM, а не выдуманный")
# ============================================================================

try:
    ds.set_action_status(ds.ACTION_PICKUP, "статус-которого-нет", "admin")
    check("выдуманный код не принимается", False, "(принят)")
except ValueError as e:
    check("выдуманный код не принимается", "нет в справочнике" in str(e), f"({e})")

ds.set_action_status(ds.ACTION_PICKUP, "send-to-delivery", "admin")
ds.set_action_status(ds.ACTION_DELIVER, "order-delivery-complete", "admin")
ds.set_action_status(ds.ACTION_NO_ANSWER, "order-delivery-fail", "admin")

mapping = {row["action"]: row["status_code"] for row in ds.list_action_statuses()}
check("маппинг сохранён", mapping.get(ds.ACTION_PICKUP) == "send-to-delivery",
      f"({mapping})")
check("ненастроенные действия видны как пустые",
      mapping.get(ds.ACTION_REFUSED) is None, f"({mapping})")


# ============================================================================
print("\n3. «Забрал» у неготового заказа: предупреждение, а не запрет")
# ============================================================================
# Разведка показала: статус «Заказ готов» ставят в момент начала окна
# доставки, а у трети заказов уже после него. Запрет заставил бы курьера
# стоять в салоне и ждать, пока флорист щёлкнет статус.

try:
    ds.advance_assignment(7001, courier_user_id=10, action=ds.ACTION_PICKUP,
                          username="ivan")
    check("неготовый заказ требует подтверждения", False, "(прошло без спроса)")
except ds.ClaimError as e:
    check("неготовый заказ требует подтверждения", e.code == "not_ready", f"({e.code})")

ds.advance_assignment(7001, courier_user_id=10, action=ds.ACTION_PICKUP,
                      username="ivan", force_not_ready=True)

with cs.get_db() as conn:
    row = conn.execute(
        "SELECT state, picked_up_at, problem_note FROM delivery_assignments "
        " WHERE retailcrm_order_id = 7001").fetchone()
check("после подтверждения заказ забран", row["state"] == "picked_up",
      f"({dict(row)})")
check("забор до готовности отмечен в журнале",
      row["problem_note"] and "готов" in row["problem_note"], f"({dict(row)})")


# ============================================================================
print("\n4. Отметка сохраняется сразу, наружу уходит фоном")
# ============================================================================

batch = ds.take_outbox_batch()
check("отправка легла в очередь", len(batch) == 1, f"({batch})")
check("в очереди тот статус, что настроен",
      batch and batch[0]["target_status"] == "send-to-delivery", f"({batch})")
check("состояние курьера уже изменилось, CRM не ждали",
      row["picked_up_at"] is not None)


class FakeClient:
    """Клиент CRM, который считает вызовы и умеет падать по заказу."""

    def __init__(self):
        self.calls = []
        self.fail_with = {}

    def edit_order(self, order_id, status=None, courier_id=None, site=None):
        self.calls.append({"order_id": order_id, "status": status,
                           "courier_id": courier_id})
        error = self.fail_with.get(order_id)
        if error:
            raise error
        return {"success": True}


client = FakeClient()
result = delivery_feed.push_status_outbox(client)
check("фон отправил задачу", result["sent"] == 1, f"({result})")
check("ушёл нужный статус",
      client.calls and client.calls[0]["status"] == "send-to-delivery",
      f"({client.calls})")
check("очередь опустела", not ds.take_outbox_batch())

journal = ds.list_outbox()
check("журнал показывает отправку",
      journal and journal[0]["state"] == "sent", f"({journal[:1]})")


# ============================================================================
print("\n5. Флорист отметил «Заказ готов» — у курьера ничего не сломалось")
# ============================================================================

with cs.get_db() as conn:
    conn.execute("UPDATE courier_orders SET status = 'order-complete' "
                 " WHERE retailcrm_order_id = 7001")
ds.expire_stale_claims()
ds.release_orphan_claims(["dostavka-kurerom"])

with cs.get_db() as conn:
    state_now = conn.execute(
        "SELECT state FROM delivery_assignments WHERE retailcrm_order_id = 7001"
    ).fetchone()["state"]
check("смена статуса в CRM не снимает бронь", state_now == "picked_up",
      f"({state_now})")


# ============================================================================
print("\n6. «Доставлено» только после «Забрал»")
# ============================================================================

add_order(7002)
ds.claim_order(7002, courier_user_id=10, courier_name="Иван", city="Новосибирск")
try:
    ds.advance_assignment(7002, courier_user_id=10, action=ds.ACTION_DELIVER,
                          username="ivan")
    check("нельзя доставить незабранный заказ", False, "(прошло)")
except ds.ClaimError as e:
    check("нельзя доставить незабранный заказ", e.code == "order", f"({e.code})")

ds.advance_assignment(7001, courier_user_id=10, action=ds.ACTION_DELIVER,
                      username="ivan")
with cs.get_db() as conn:
    delivered = conn.execute(
        "SELECT state, delivered_at FROM delivery_assignments "
        " WHERE retailcrm_order_id = 7001").fetchone()
check("доставка отмечена", delivered["state"] == "delivered", f"({dict(delivered)})")
check("время доставки записано", delivered["delivered_at"] is not None)


# ============================================================================
print("\n7. Упавшая отправка: отсрочка, предел попыток, 4xx без повтора")
# ============================================================================

delivery_feed.push_status_outbox(client)      # разобрать очередь доставки

add_order(7003)
ds.claim_order(7003, courier_user_id=10, courier_name="Иван", city="Новосибирск")
ds.advance_assignment(7003, courier_user_id=10, action=ds.ACTION_PICKUP,
                      username="ivan", force_not_ready=True)

network = Exception("CRM не отвечает")
client.fail_with[7003] = network
result = delivery_feed.push_status_outbox(client)
check("сетевая ошибка посчитана", result["failed"] == 1, f"({result})")

pending = ds.take_outbox_batch()
check("после сбоя задача ждёт отсрочки, а не долбит CRM", not pending,
      f"({pending})")

failed_row = [row for row in ds.list_outbox() if row["retailcrm_order_id"] == 7003][0]
check("попытка засчитана", failed_row["attempts"] == 1, f"({failed_row['attempts']})")
check("задача осталась в очереди на повтор", failed_row["state"] == "pending",
      f"({failed_row['state']})")
check("текст ответа CRM сохранён",
      "не отвечает" in (failed_row["error_message"] or ""),
      f"({failed_row['error_message']})")

# 4xx повторять бессмысленно: заказ удалён, статус переименован, ключ отозван
add_order(7004)
ds.claim_order(7004, courier_user_id=10, courier_name="Иван", city="Новосибирск")
ds.advance_assignment(7004, courier_user_id=10, action=ds.ACTION_PICKUP,
                      username="ivan", force_not_ready=True)
bad_request = Exception("Заказ не найден")
bad_request.status_code = 404
client.fail_with[7004] = bad_request
delivery_feed.push_status_outbox(client)

row4 = [row for row in ds.list_outbox() if row["retailcrm_order_id"] == 7004][0]
check("4xx уходит в «не ушло» без повторов", row4["state"] == "failed",
      f"({row4['state']})")
check("код ответа сохранён", row4["response_code"] == 404, f"({row4['response_code']})")

check("предел попыток задан", ds.OUTBOX_MAX_ATTEMPTS >= 3, f"({ds.OUTBOX_MAX_ATTEMPTS})")
check("отсрочка повтора задана", ds.OUTBOX_RETRY_SECONDS >= 60,
      f"({ds.OUTBOX_RETRY_SECONDS})")


# ============================================================================
print("\n8. Курьер в CRM и предупреждение о выплатах")
# ============================================================================

from werkzeug.security import generate_password_hash  # noqa: E402

conn = auth.get_db()
try:
    conn.execute(
        "INSERT INTO users (username, full_name, password_hash, role, is_active, created_at) "
        "VALUES ('kurier', 'Курьер', ?, 'courier', 1, datetime('now'))",
        (generate_password_hash("Parol12345"),))
    conn.execute("INSERT INTO permissions (username, module_name, can_view) "
                 "VALUES ('kurier', 'courier_app', 1)")
    conn.commit()
    user_id = conn.execute("SELECT id FROM users WHERE username = 'kurier'").fetchone()["id"]
finally:
    conn.close()

ds.save_courier_profile(user_id=user_id, username="kurier", city="Новосибирск",
                        retailcrm_courier_id=None, active=True, updated_by="test")

AJAX = {auth.AJAX_HEADER: auth.AJAX_HEADER_VALUE}
client_http = app.test_client()
client_http.post("/api/auth/login", json={"username": "kurier", "password": "Parol12345"})

add_order(7005, status="order-complete")
r = client_http.post("/api/courier/orders/7005/claim", headers=AJAX)
check("курьер забронировал через API", r.status_code == 200, f"({r.status_code})")

r = client_http.post("/api/courier/orders/7005/action", headers=AJAX,
                     json={"action": "pickup"})
body = r.get_json() or {}
check("«Забрал» проходит через API", r.status_code == 200,
      f"({r.status_code}: {body})")
check("без связки с CRM курьера предупреждают про выплату",
      "оплаты" in ((body.get("data") or {}).get("warning") or ""),
      f"({(body.get('data') or {}).get('warning')})")

with cs.get_db() as conn:
    sent_crm_id = conn.execute(
        "SELECT courier_crm_id FROM crm_status_outbox "
        " WHERE retailcrm_order_id = 7005").fetchone()["courier_crm_id"]
check("без связки курьер в CRM не отправляется", sent_crm_id is None,
      f"({sent_crm_id})")

ds.save_courier_profile(user_id=user_id, username="kurier", city="Новосибирск",
                        retailcrm_courier_id=42, active=True, updated_by="test")
add_order(7006, status="order-complete")
client_http.post("/api/courier/orders/7006/claim", headers=AJAX)
client_http.post("/api/courier/orders/7006/action", headers=AJAX,
                 json={"action": "pickup"})
with cs.get_db() as conn:
    sent_crm_id = conn.execute(
        "SELECT courier_crm_id FROM crm_status_outbox "
        " WHERE retailcrm_order_id = 7006").fetchone()["courier_crm_id"]
check("со связкой курьер уходит в CRM вместе со статусом", sent_crm_id == 42,
      f"({sent_crm_id})")

r = client_http.get("/api/courier/outbox")
check("журнал отправок курьеру не отдаётся", r.status_code == 403, f"({r.status_code})")


# ============================================================================
print()
if failures:
    print(f"=== ПРОВАЛОВ: {len(failures)} ===")
    for name in failures:
        print(f"  - {name}")
    sys.exit(1)

print("=== Запись статусов в CRM в порядке ===")
sys.exit(0)
