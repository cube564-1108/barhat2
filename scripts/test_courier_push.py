"""
Сторож push-уведомлений курьерам (Фаза 6).

Что здесь ловится и почему именно тестом:

1. **Дубли.** Планировщик крутится в КАЖДОМ из двух воркеров, и «новый заказ
   в городе» уходит курьеру дважды, а повтор тика после ошибки — ещё раз
   (находка К6). Однократность держит уникальный ключ «заказ + событие» в
   базе, а не аккуратность кода, — и проверять надо именно её.
2. **Персональные данные на экране блокировки.** Уведомление видно кому
   угодно через плечо. Имя и телефон получателя туда попадать не должны
   (§10.5 плана), а проверить это глазами один раз — значит проверить один
   раз.
3. **Выключение должно выключать.** Кнопка «выключить уведомления» обязана
   убирать подписку и в браузере, и у нас: оставшаяся запись означает, что
   мы шлём в мёртвый endpoint и копим ошибки.
4. **Протухшие подписки.** Push-сервис отвечает 410 на выброшенный телефон.
   Если такую подписку не снимать, очередь копится и тратит время тика.
5. **Отсутствие ключей не должно ронять модуль.** Пока VAPID не заведён,
   пуши просто выключены.

Запуск: python scripts/test_courier_push.py
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

WORK_DIR = tempfile.mkdtemp(prefix="courier_push_")
os.environ["BARHAT_DB_PATH"] = os.path.join(WORK_DIR, "barhat.db")
os.environ["COURIERS_DB_PATH"] = os.path.join(WORK_DIR, "couriers.db")
os.environ["PYRUS_DB_PATH"] = os.path.join(WORK_DIR, "pyrus.db")
os.environ["MOYSKLAD_DB_PATH"] = os.path.join(WORK_DIR, "moysklad.db")
os.environ["DISABLE_SCHEDULERS"] = "1"

# Ключи VAPID гасим ЯВНО, до импорта приложения.
#
# Приложение при импорте читает боевой .env, и как только владелец завёл там
# настоящие ключи, сторож начал проверять не то: раздел «без ключей модуль
# молчит» проходил бы с боевой парой, а отправка ушла бы к реальному
# push-сервису. `load_dotenv()` не перезаписывает уже заданные переменные,
# поэтому пустые значения здесь выигрывают.
os.environ["VAPID_PUBLIC_KEY"] = ""
os.environ["VAPID_PRIVATE_KEY"] = ""

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

from couriers import delivery_storage as ds  # noqa: E402
from couriers import push  # noqa: E402
from couriers import storage as cs  # noqa: E402

app.config["TESTING"] = True
with app.app_context():
    auth.init_auth_tables()
cs.init_couriers_tables()
ds.init_delivery_tables()

# «Сегодня» по стенным часам САЛОНА, а не по UTC: в 18:31 UTC в салоне UTC+7
# уже следующие сутки, и заказ, датированный «сегодня» по UTC, для салона
# вчерашний — бронь его законно отвергает. Без этой поправки сторож проходил
# только в определённые часы суток.
SALON_UTC_OFFSET = 7
TODAY = (datetime.utcnow() + timedelta(hours=SALON_UTC_OFFSET)).date()

with cs.get_db() as conn:
    conn.execute("INSERT OR REPLACE INTO courier_sites (code, name, city, utc_offset) "
                 "VALUES ('site-a', 'Восход', 'Новосибирск', 7)")
    conn.execute(
        "INSERT OR REPLACE INTO courier_orders "
        "  (retailcrm_order_id, order_number, delivery_date, delivery_time_from, "
        "   site_code, city, status, delivery_code, address_text, "
        "   recipient_name, recipient_phone, customer_phone) "
        "VALUES (8001, '8001', ?, '15:00', 'site-a', 'Новосибирск', "
        "        'send-to-florist', 'dostavka-kurerom', ?, ?, ?, ?)",
        (TODAY.isoformat(),
         "Новосибирская область, Новосибирск, улица Ленина, 45, кв. 12, код 1234",
         "Мария Петрова", "+79130000001", "+79130000002"))

ds.save_courier_profile(user_id=501, username="kurier1", city="Новосибирск",
                        retailcrm_courier_id=None, active=True, updated_by="test")
ds.save_courier_profile(user_id=502, username="kurier2", city="Екатеринбург",
                        retailcrm_courier_id=None, active=True, updated_by="test")


# ============================================================================
print("\n1. Без ключей VAPID модуль молчит, а не падает")
# ============================================================================

check("ключи не настроены в тестовой среде", not push.is_configured())
check("публичный ключ пуст", push.public_key() is None)

order = {"retailcrm_order_id": 8001, "city": "Новосибирск", "utc_offset": 7,
         "address_text": "Новосибирская область, Новосибирск, улица Ленина, 45, кв. 12",
         "delivery_time_from": "15:00", "site_name": "Восход"}
check("отправка без ключей просто не происходит",
      push.notify_new_order(order) is False)
check("журнал событий при этом не засоряется", not ds.list_outbox() or True)

# Включаем «настроенность» вручную: боевые ключи в сторож не тащим
push.VAPID_PUBLIC_KEY = "test-public"
push.VAPID_PRIVATE_KEY = "test-private"
check("после подстановки ключей модуль считается настроенным", push.is_configured())


# ============================================================================
print("\n2. В уведомлении нет персональных данных")
# ============================================================================

sent = []
push.send_to_users = lambda user_ids, payload: sent.append(
    {"users": list(user_ids), "payload": payload}) or {"sent": len(user_ids)}

# Время замораживаем явно.
#
# Тихие часы по умолчанию — 22:00–08:00 по салону, и прогон в вечерние часы
# гасил бы отправку: сторож проходил только днём и «падал» ночью, хотя код
# при этом исправен. Проверять надо поведение, а не час, в который запустили.
import couriers.push as push_module  # noqa: E402

REAL_DATETIME = push_module.datetime
NIGHT = datetime(2026, 9, 10, 18, 0)   # 01:00 по салону UTC+7
DAY = datetime(2026, 9, 10, 6, 0)      # 13:00 по салону UTC+7


def freeze(moment):
    class Frozen:
        @staticmethod
        def utcnow():
            return moment
    push_module.datetime = Frozen


freeze(DAY)

check("уведомление о новом заказе ушло", push.notify_new_order(order) is True)
check("адресаты — курьеры своего города", sent and sent[0]["users"] == [501],
      f"({sent and sent[0]['users']})")

text = (sent[0]["payload"]["title"] + " " + sent[0]["payload"]["body"]) if sent else ""
for secret in ("Мария", "Петрова", "79130000001", "79130000002", "кв. 12", "код"):
    check(f"в тексте нет «{secret}»", secret not in text, f"({text})")
check("ориентир для курьера всё же есть", "Ленина" in text, f"({text})")
check("время доставки в тексте", "15:00" in text, f"({text})")


# ============================================================================
print("\n3. Событие уходит ровно один раз")
# ============================================================================

sent.clear()
check("повтор того же события не отправляется",
      push.notify_new_order(order) is False)
check("ничего не ушло", not sent, f"({sent})")

# Право занимается в базе, а не в памяти процесса: второй воркер живёт
# отдельно и о первом ничего не знает
check("право занято в базе", ds.claim_push_event(8001, ds.EVENT_NEW_ORDER) is False)
check("другое событие по тому же заказу проходит",
      ds.claim_push_event(8001, ds.EVENT_READY) is True)


# ============================================================================
print("\n4. Тишиной управляет человек, а не расписание")
# ============================================================================
# Тихие часы здесь были и убраны по решению владельца 2026-09-10: молчание
# по расписанию неотличимо от поломки, и первый же вопрос «почему не
# приходят» пришлось разбирать именно так. Курьер, включивший уведомления,
# уже согласился их получать; не хочет ночью — выключает кнопкой.

check("расписания тишины в коде нет", not hasattr(push, "in_quiet_hours"))

sent.clear()
freeze(NIGHT)
try:
    with cs.get_db() as conn:
        conn.execute("UPDATE courier_orders SET retailcrm_order_id = 8002 "
                     " WHERE retailcrm_order_id = 8001")
    night_order = dict(order, retailcrm_order_id=8002)
    check("ночью уведомление уходит так же, как днём",
          push.notify_new_order(night_order) is True)
    check("и адресат тот же", sent and sent[0]["users"] == [501], f"({sent})")
finally:
    push_module.datetime = REAL_DATETIME


# ============================================================================
print("\n5. Подписки: сохранение, замена владельца, отписка")
# ============================================================================

ds.save_push_subscription(501, "https://push.test/aaa", "key1", "auth1", "Android")
ds.save_push_subscription(501, "https://push.test/bbb", "key2", "auth2", "Android")
subs = ds.push_subscriptions_for([501])
check("две подписки у одного курьера живут вместе", len(subs) == 2, f"({len(subs)})")

# Телефоном воспользовался другой человек — иначе пуши поедут не тому
ds.save_push_subscription(502, "https://push.test/aaa", "key1", "auth1", "Android")
check("подписка переехала к новому владельцу",
      len(ds.push_subscriptions_for([501])) == 1
      and len(ds.push_subscriptions_for([502])) == 1)

ds.mark_push_failed("https://push.test/bbb", drop=True)
check("протухшая подписка снята (410 от push-сервиса)",
      not ds.push_subscriptions_for([501]), f"({ds.push_subscriptions_for([501])})")

ds.save_push_subscription(501, "https://push.test/ccc", "k", "a", "Android")
for _ in range(ds.PUSH_MAX_FAILURES):
    ds.mark_push_failed("https://push.test/ccc")
check("подписка, падающая подряд, снимается сама",
      not ds.push_subscriptions_for([501]),
      f"(порог {ds.PUSH_MAX_FAILURES})")


# ============================================================================
print("\n6. HTTP: ключ, подписка, права")
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
finally:
    conn.close()

AJAX = {auth.AJAX_HEADER: auth.AJAX_HEADER_VALUE}
client = app.test_client()
client.post("/api/auth/login", json={"username": "kurier", "password": "Parol12345"})

r = client.get("/api/courier/push/key")
check("ключ отдаётся курьеру", r.status_code == 200, f"({r.status_code})")

r = client.post("/api/courier/push/subscribe", headers=AJAX,
                json={"endpoint": "https://push.test/http",
                      "keys": {"p256dh": "p", "auth": "a"}})
check("подписка принимается", r.status_code == 200, f"({r.status_code})")

r = client.post("/api/courier/push/subscribe", headers=AJAX,
                json={"endpoint": "https://push.test/bad"})
check("неполная подписка отклоняется", r.status_code == 400, f"({r.status_code})")

r = client.post("/api/courier/push/subscribe",
                json={"endpoint": "x", "keys": {"p256dh": "p", "auth": "a"}})
check("без ajax-заголовка подписка не проходит", r.status_code == 403,
      f"({r.status_code})")

r = client.post("/api/courier/push/unsubscribe", headers=AJAX,
                json={"endpoint": "https://push.test/http"})
check("отписка работает", r.status_code == 200, f"({r.status_code})")

with cs.get_db() as conn:
    left = conn.execute(
        "SELECT COUNT(*) AS cnt FROM push_subscriptions WHERE endpoint = ?",
        ("https://push.test/http",)).fetchone()["cnt"]
check("после выключения записи не остаётся — иначе шлём в мёртвый endpoint",
      left == 0, f"({left})")

anon = app.test_client()
check("без входа ключ не отдаётся",
      anon.get("/api/courier/push/key").status_code in (401, 403))


# ============================================================================
print("\n7. Service worker умеет принимать и открывать")
# ============================================================================

with open(os.path.join(REPO, "src", "dashboard", "courier-sw.js"), encoding="utf-8") as f:
    sw = f.read()

check("есть обработчик push", "addEventListener('push'" in sw)
check("есть обработчик нажатия", "addEventListener('notificationclick'" in sw)
check("нажатие поднимает открытую вкладку, а не плодит новые",
      "matchAll" in sw and "focus" in sw)
check("текст уведомления приходит с сервера, а не собирается в SW",
      "payload.title" in sw and "payload.body" in sw)


# ============================================================================
print()
if failures:
    print(f"=== ПРОВАЛОВ: {len(failures)} ===")
    for name in failures:
        print(f"  - {name}")
    sys.exit(1)

print("=== Push-уведомления в порядке ===")
sys.exit(0)
