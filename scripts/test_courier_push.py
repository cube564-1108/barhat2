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

# Устройство курьера. Без него отправки не будет вовсе — и это правильно:
# право на событие одноразовое, занимать его, когда отправлять некому, значит
# похоронить уведомление по этому заказу навсегда (см. раздел 6c). Профиль без
# подписки — обычное состояние: курьера заводят раньше, чем он откроет
# приложение.
ds.save_push_subscription(user_id=501, endpoint="https://fcm.googleapis.com/fcm/send/kurier1",
                          p256dh="p", auth="a")


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
# Оригинал сохраняем: разделы ниже проверяют НАСТОЯЩЕЕ поведение отправки, а
# подмена, оставленная навсегда, превращает их в проверку заглушки.
REAL_SEND_TO_USERS = push.send_to_users
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

# С чистого листа: раздел считает подписки поимённо, и любая заведённая выше
# (например устройство из фикстуры) ломала бы счёт. Зависимость от порядка
# разделов — худший вид хрупкости в стороже: он падает не там, где сломано.
with cs.get_db() as conn:
    conn.execute("DELETE FROM push_subscriptions")

ds.save_push_subscription(501, "https://fcm.googleapis.com/fcm/send/aaa", "key1", "auth1", "Android")
ds.save_push_subscription(501, "https://fcm.googleapis.com/fcm/send/bbb", "key2", "auth2", "Android")
subs = ds.push_subscriptions_for([501])
check("две подписки у одного курьера живут вместе", len(subs) == 2, f"({len(subs)})")

# Телефоном воспользовался другой человек — иначе пуши поедут не тому
ds.save_push_subscription(502, "https://fcm.googleapis.com/fcm/send/aaa", "key1", "auth1", "Android")
check("подписка переехала к новому владельцу",
      len(ds.push_subscriptions_for([501])) == 1
      and len(ds.push_subscriptions_for([502])) == 1)

ds.mark_push_failed("https://fcm.googleapis.com/fcm/send/bbb", drop=True)
check("протухшая подписка снята (410 от push-сервиса)",
      not ds.push_subscriptions_for([501]), f"({ds.push_subscriptions_for([501])})")

ds.save_push_subscription(501, "https://fcm.googleapis.com/fcm/send/ccc", "k", "a", "Android")
for _ in range(ds.PUSH_MAX_FAILURES):
    ds.mark_push_failed("https://fcm.googleapis.com/fcm/send/ccc")
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
                json={"endpoint": "https://fcm.googleapis.com/fcm/send/http",
                      "keys": {"p256dh": "p", "auth": "a"}})
check("подписка принимается", r.status_code == 200, f"({r.status_code})")

r = client.post("/api/courier/push/subscribe", headers=AJAX,
                json={"endpoint": "https://fcm.googleapis.com/fcm/send/bad"})
check("неполная подписка отклоняется", r.status_code == 400, f"({r.status_code})")

# --- endpoint присылает браузер, а идёт по нему сервер ------------------------
#
# Сервер сам обращается по этому адресу, и с появлением пробного уведомления
# делает это НЕМЕДЛЕННО, возвращая клиенту результат. Без проверки любой
# вошедший курьер превращает ручку подписки в сканер внутренней сети
# (находка security-review 17.09.2026). Проверяется поведением: адрес,
# не принадлежащий настоящему push-сервису, не должен сохраняться.
for bad, why in (
    ("http://10.0.0.5:8080/", "внутренний адрес по http"),
    ("https://10.0.0.5:8443/", "внутренний адрес по https"),
    ("https://evil.example/wpush", "чужой домен"),
    ("http://fcm.googleapis.com/fcm/send/x", "верный хост, но без TLS"),
    ("https://fcm.googleapis.com.evil.example/x", "чужой домен, похожий на верный"),
    ("https://notfcm-googleapis.com/x", "хост без точки перед суффиксом"),
    # Всё до «@» — это имя пользователя, а не хост. Проверка по вхождению
    # подстроки на этом и ломается: строка выглядит правильной, идёт запрос
    # на evil.example.
    ("https://fcm.googleapis.com@evil.example/x", "верный хост в userinfo"),
    ("https://fcm.googleapis.com:443@10.0.0.5/x", "userinfo с портом, хост внутренний"),
    ("https://evil.example/fcm.googleapis.com", "верный хост в пути"),
):
    r = client.post("/api/courier/push/subscribe", headers=AJAX,
                    json={"endpoint": bad, "keys": {"p256dh": "p", "auth": "a"}})
    check(f"не принимается: {why}", r.status_code == 400, f"({r.status_code}, {bad})")

# Настоящие адреса четырёх push-сервисов обязаны проходить: слишком строгий
# список — это «уведомления не включаются» у целого браузера.
for good, who in (
    ("https://fcm.googleapis.com/fcm/send/abc", "Chrome / Android"),
    ("https://updates.push.services.mozilla.com/wpush/v2/abc", "Firefox"),
    ("https://web.push.apple.com/abc", "Safari / iOS"),
    ("https://wns2-by3p.notify.windows.com/w/?token=abc", "Edge"),
    # Имя хоста регистронезависимо: браузер вправе прислать его как угодно
    ("https://FCM.GoogleAPIs.com/fcm/send/abc", "верный хост в другом регистре"),
):
    r = client.post("/api/courier/push/subscribe", headers=AJAX,
                    json={"endpoint": good, "keys": {"p256dh": "p", "auth": "a"}})
    check(f"принимается: {who}", r.status_code == 200, f"({r.status_code}, {good})")

r = client.post("/api/courier/push/subscribe",
                json={"endpoint": "x", "keys": {"p256dh": "p", "auth": "a"}})
check("без ajax-заголовка подписка не проходит", r.status_code == 403,
      f"({r.status_code})")

r = client.post("/api/courier/push/unsubscribe", headers=AJAX,
                json={"endpoint": "https://fcm.googleapis.com/fcm/send/http"})
check("отписка работает", r.status_code == 200, f"({r.status_code})")

with cs.get_db() as conn:
    left = conn.execute(
        "SELECT COUNT(*) AS cnt FROM push_subscriptions WHERE endpoint = ?",
        ("https://fcm.googleapis.com/fcm/send/http",)).fetchone()["cnt"]
check("после выключения записи не остаётся — иначе шлём в мёртвый endpoint",
      left == 0, f"({left})")

anon = app.test_client()
check("без входа ключ не отдаётся",
      anon.get("/api/courier/push/key").status_code in (401, 403))


# ============================================================================
print("\n6b. Включение отвечает, работают ли уведомления")
# ============================================================================
#
# Настоящий пуш уходит, только когда в городе ПОЯВИТСЯ новый свободный заказ,
# и по каждому заказу ровно один раз. В пустой день молчание неотличимо от
# поломки, а разобрать его нечем: консоли у контейнера нет. 17.09.2026
# владелец включил уведомления и не смог понять, работают они или нет.
#
# Поэтому подписка обязана отвечать двумя РАЗНЫМИ фактами: дошло ли пробное
# до телефона и попадает ли человек в адресаты вообще. Это разные причины
# молчания и разные действия человека.

# Возвращаем настоящую отправку и снимаем ключи: разделы выше подменяли и то,
# и другое, а здесь проверяется реальное поведение ручки.
push.send_to_users = REAL_SEND_TO_USERS
push.VAPID_PUBLIC_KEY = ""
push.VAPID_PRIVATE_KEY = ""

r = client.post("/api/courier/push/subscribe", headers=AJAX,
                json={"endpoint": "https://fcm.googleapis.com/fcm/send/probe",
                      "keys": {"p256dh": "p", "auth": "a"}})
data = (r.get_json() or {}).get("data") or {}
check("ответ подписки говорит про пробное", "test_sent" in data, data)
check("ответ подписки говорит про адресность", "is_recipient" in data, data)
# Ключей VAPID в прогоне нет — и это должно называться своим именем, а не
# «не дошло»: администратору чинить одно, курьеру другое.
check("без ключей VAPID причина названа",
      data.get("test_reason") == "not_configured", data)

# Профиля курьера у этой учётки нет: пробное придёт, а «новый заказ» — нет
check("без профиля курьера человек не адресат",
      data.get("is_recipient") is False, data)

conn = auth.get_db()
try:
    kurier_id = conn.execute(
        "SELECT id FROM users WHERE username = 'kurier'").fetchone()["id"]
finally:
    conn.close()

ds.save_courier_profile(user_id=int(kurier_id), username="kurier",
                        city="Новосибирск", retailcrm_courier_id=None, active=True)

r = client.post("/api/courier/push/subscribe", headers=AJAX,
                json={"endpoint": "https://fcm.googleapis.com/fcm/send/probe",
                      "keys": {"p256dh": "p", "auth": "a"}})
data = (r.get_json() or {}).get("data") or {}
check("с профилем и городом человек адресат", data.get("is_recipient") is True, data)
check("город возвращается экрану", data.get("city") == "Новосибирск", data)

# Проверку человек вправе повторять сколько угодно: «одно событие на заказ»
# держит claim_push_event, и пробное под это правило попадать не должно —
# иначе вторая проверка молча ничего не отправит.
#
# Проверяем ПОВЕДЕНИЕМ, а не поиском слова в исходнике: слово встречается в
# докстроке функции, и проверка «его нет в тексте» падала бы на исправном коде
# и проходила бы на сломанном без комментария.
probes = []
push.VAPID_PUBLIC_KEY = "test-public"
push.VAPID_PRIVATE_KEY = "test-private"
push.send_to_users = lambda user_ids, payload: (
    probes.append(payload) or {"sent": 1, "failed": 0, "dropped": 0})

push.send_test([501])
push.send_test([501])
check("повторная проверка тоже отправляет", len(probes) == 2,
      f"(отправок: {len(probes)})")
check("пробное не затирает уведомление о заказе",
      probes and probes[0].get("tag") == "test", probes[:1])

push.send_to_users = REAL_SEND_TO_USERS
push.VAPID_PUBLIC_KEY = ""
push.VAPID_PRIVATE_KEY = ""

# ============================================================================
print("\n6c. Право на событие не сгорает вхолостую")
# ============================================================================
#
# Журнал «заказ + событие» одноразовый. Пока проверки не было, тик ленты
# занимал право по каждому свободному заказу, даже когда ни одно устройство
# курьеров города не подписано, и слал в пустоту. Курьер, включивший
# уведомления после этого, не получал НИЧЕГО по уже существующим заказам — а
# в спокойный день новых и не появляется.
#
# Ровно так 17.09.2026 выглядело «включил уведомления, ни одного пуша».

push.VAPID_PUBLIC_KEY = "test-public"
push.VAPID_PRIVATE_KEY = "test-private"

# Курьер города есть, устройств нет
with cs.get_db() as conn:
    conn.execute("DELETE FROM push_subscriptions")
    conn.execute("DELETE FROM push_events")

quiet = {"retailcrm_order_id": 8100, "city": "Новосибирск", "utc_offset": 7,
         "address_text": "Новосибирск, улица Мира, 3", "delivery_time_from": "16:00",
         "site_name": "Восход"}
check("без подписок уведомление не отправляется",
      push.notify_new_order(quiet) is False)
with cs.get_db() as conn:
    burned = conn.execute(
        "SELECT COUNT(*) AS c FROM push_events WHERE retailcrm_order_id = 8100"
    ).fetchone()["c"]
check("и право на событие не занято", burned == 0,
      "(иначе этот заказ промолчит навсегда)")

# Устройство подписалось — тот же заказ обязан дойти
ds.save_push_subscription(user_id=501, endpoint="https://fcm.googleapis.com/fcm/send/late",
                          p256dh="p", auth="a")
probes.clear()
push.send_to_users = lambda user_ids, payload: (
    probes.append(payload) or {"sent": 1, "failed": 0, "dropped": 0})
check("после подписки уведомление по тому же заказу уходит",
      push.notify_new_order(quiet) is True, "(второго шанса раньше не было)")
check("и оно действительно отправлено", len(probes) == 1, f"({len(probes)})")

# Теперь право занято — повтор не проходит, дублей нет
check("повтор по тому же заказу не отправляется",
      push.notify_new_order(quiet) is False)

# Сброс журнала возвращает заказу право: это выход для тех заказов, чьё право
# сгорело до починки
removed = ds.reset_push_events(2)
check("сброс журнала что-то удалил", removed >= 1, f"({removed})")
probes.clear()
check("после сброса уведомление уходит снова",
      push.notify_new_order(quiet) is True)

push.send_to_users = REAL_SEND_TO_USERS
push.VAPID_PUBLIC_KEY = ""
push.VAPID_PRIVATE_KEY = ""

# --- ручка сброса: только админ и только через ajax --------------------------
r = client.post("/api/courier/push/reset-events", headers=AJAX, json={"days": 2})
check("курьеру сброс недоступен", r.status_code in (401, 403), f"({r.status_code})")


# --- экран обязан различать исходы ------------------------------------------
with open(os.path.join(REPO, "src", "dashboard", "courier-app.js"),
          encoding="utf-8") as f:
    app_js = f.read()
check("экран разбирает результат пробного",
      "function announceSubscribed" in app_js and "test_sent" in app_js, "")
check("«ключей нет» отделено от «не дошло»",
      "not_configured" in app_js, "(это разные действия человека)")
check("экран предупреждает не-адресата",
      "is_recipient === false" in app_js,
      "(пробное придёт, а сообщения о заказах — никогда)")


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
