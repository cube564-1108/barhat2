"""
Сторож экрана управляющего (Фаза 8): обзор доставки, метрики, профили.

Что здесь ловится и почему именно тестом:

1. **Экран врёт минуту после каждого действия.** Состояние заказа обязано
   браться из НАШЕЙ таблицы броней, а не из статуса CRM: между отметкой
   курьера и её отражением в CRM проходит до минуты (находка К1 критики).
2. **«Никто не взял» молчит или шумит.** Порог свой в каждом городе, и
   считается он по стенным часам САЛОНА: одно число по времени сервера
   будет опаздывать на два часа у половины городов.
3. **Непосчитанный показатель выглядит нулём.** «Ноль минут» и «данных нет»
   читаются человеком по-разному, и подменять одно другим нельзя.
4. **Права.** «Снять чужую бронь» и «видеть весь город» — не курьерское
   право (К8): курьер не должен получить их вместе с доступом к ленте.
5. **Разметка и стили разъезжаются.** Классы, которых нет в CSS, ломают
   экран молча — node --check такого не видит.

Запуск: python scripts/test_courier_dispatch.py
"""

import os
import re
import socket
import ssl  # noqa: F401  — импортировать до патча сокета
import sys
import tempfile
from datetime import datetime, timedelta

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))
DASHBOARD = os.path.join(REPO, "src", "dashboard")


class NetworkBlocked(Exception):
    pass


def _blocked(*args, **kwargs):
    raise NetworkBlocked("сторож не должен ходить в боевые внешние API")


socket.socket.connect = _blocked

WORK_DIR = tempfile.mkdtemp(prefix="courier_disp_")
os.environ["BARHAT_DB_PATH"] = os.path.join(WORK_DIR, "barhat.db")
os.environ["COURIERS_DB_PATH"] = os.path.join(WORK_DIR, "couriers.db")
os.environ["PYRUS_DB_PATH"] = os.path.join(WORK_DIR, "pyrus.db")
os.environ["MOYSKLAD_DB_PATH"] = os.path.join(WORK_DIR, "moysklad.db")
os.environ["DISABLE_SCHEDULERS"] = "1"
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
from couriers import storage as cs  # noqa: E402

app.config["TESTING"] = True
with app.app_context():
    auth.init_auth_tables()
cs.init_couriers_tables()
ds.init_delivery_tables()

# «Сегодня» по стенным часам САЛОНА, а не по UTC.
#
# Разница не теоретическая: в 18:31 UTC в салоне UTC+7 уже следующие сутки,
# и заказ, датированный «сегодня» по UTC, для салона вчерашний — бронь его
# законно отвергает. Первая версия этого сторожа так и падала.
SALON_UTC_OFFSET = 7
TODAY = (datetime.utcnow() + timedelta(hours=SALON_UTC_OFFSET)).date()
CODES = ["dostavka-kurerom"]

with cs.get_db() as conn:
    conn.execute("INSERT OR REPLACE INTO courier_sites (code, name, city, utc_offset) "
                 "VALUES ('site-a', 'Восход', 'Новосибирск', 7)")
    for code, name in (("send-to-florist", "Передан флористу"),
                       ("order-complete", "Заказ готов"),
                       ("send-to-delivery", "Передан курьеру")):
        conn.execute("INSERT OR REPLACE INTO order_statuses (code, name) VALUES (?, ?)",
                     (code, name))


def add_order(order_id, time_from="23:30", status="send-to-florist", net_cost=0):
    with cs.get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO courier_orders "
            "  (retailcrm_order_id, order_number, delivery_date, delivery_time_from, "
            "   delivery_time_to, site_code, city, status, delivery_code, net_cost) "
            "VALUES (?, ?, ?, ?, '23:59', 'site-a', 'Новосибирск', ?, 'dostavka-kurerom', ?)",
            (order_id, f"N{order_id}", TODAY.isoformat(), time_from, status, net_cost))


# ============================================================================
print("\n1. Обзор: состояние из наших броней, а не из статуса CRM")
# ============================================================================

add_order(6001)                      # свободен
add_order(6002)                      # забронирован
add_order(6003, status="order-complete")   # готов и забран

ds.claim_order(6002, courier_user_id=11, courier_name="Иван", city="Новосибирск")
ds.claim_order(6003, courier_user_id=11, courier_name="Иван", city="Новосибирск")
ds.set_action_status(ds.ACTION_PICKUP, "send-to-delivery", "admin")
ds.advance_assignment(6003, courier_user_id=11, action=ds.ACTION_PICKUP,
                      username="ivan")

overview = ds.dispatch_overview("Новосибирск", TODAY.isoformat(),
                                TODAY.isoformat(), CODES)
by_id = {o["retailcrm_order_id"]: o for o in overview["orders"]}

check("свободный заказ виден свободным", by_id[6001]["state"] == "free",
      f"({by_id[6001]['state']})")
check("забронированный — забронированным", by_id[6002]["state"] == "claimed",
      f"({by_id[6002]['state']})")
check("забранный — в пути", by_id[6003]["state"] == "picked_up",
      f"({by_id[6003]['state']})")
check("имя курьера рядом с заказом", by_id[6002]["courier_name"] == "Иван")
check("итоги сходятся", overview["totals"]["free"] == 1
      and overview["totals"]["claimed"] == 1
      and overview["totals"]["picked_up"] == 1, f"({overview['totals']})")

# Статус в CRM сдвинулся вперёд, а состояние у нас прежнее — экран не врёт
with cs.get_db() as conn:
    conn.execute("UPDATE courier_orders SET status = 'order-complete' "
                 " WHERE retailcrm_order_id = 6002")
overview = ds.dispatch_overview("Новосибирск", TODAY.isoformat(),
                                TODAY.isoformat(), CODES)
by_id = {o["retailcrm_order_id"]: o for o in overview["orders"]}
check("смена статуса CRM не меняет состояние брони",
      by_id[6002]["state"] == "claimed", f"({by_id[6002]['state']})")
check("готовность при этом видна отдельным признаком", by_id[6002]["is_ready"] is True)


# ============================================================================
print("\n2. «Никто не взял» — порог по стенным часам салона")
# ============================================================================

# Порог 90 минут по умолчанию. Заказ на 23:30 по салону: тревога включается
# только когда до окна осталось меньше порога, а не сразу.
add_order(6010, time_from="23:30")
overview = ds.dispatch_overview("Новосибирск", TODAY.isoformat(),
                                TODAY.isoformat(), CODES)
by_id = {o["retailcrm_order_id"]: o for o in overview["orders"]}
salon_hour = (datetime.utcnow() + timedelta(hours=SALON_UTC_OFFSET)).hour
expected = salon_hour >= 22          # до 23:30 осталось меньше 90 минут
check("тревога включается по порогу города, а не сразу",
      by_id[6010]["unclaimed_alert"] is expected,
      f"(сейчас по салону {salon_hour}:xx, ждём {expected})")

# Заказ, до которого точно меньше порога
add_order(6011, time_from=(datetime.utcnow()
                           + timedelta(hours=SALON_UTC_OFFSET)).strftime("%H:%M"))
overview = ds.dispatch_overview("Новосибирск", TODAY.isoformat(),
                                TODAY.isoformat(), CODES)
by_id = {o["retailcrm_order_id"]: o for o in overview["orders"]}
check("заказ «на сейчас» попадает в тревогу",
      by_id[6011]["unclaimed_alert"] is True, f"({by_id[6011]})")
check("список «никто не взял» непустой",
      any(o["retailcrm_order_id"] == 6011 for o in overview["unclaimed"]))
check("забронированные в тревогу не попадают",
      all(o["state"] == "free" for o in overview["unclaimed"]))


# ============================================================================
print("\n3. Метрики: непосчитанное — не ноль")
# ============================================================================

metrics = ds.delivery_metrics(TODAY.isoformat(), TODAY.isoformat(), "Новосибирск")
check("броней посчитано", metrics["claims_total"] >= 2, f"({metrics['claims_total']})")
check("доставок вовремя пока нет — None, а не 0",
      metrics["on_time_share"] is None, f"({metrics['on_time_share']})")
check("медиана до забора посчитана",
      metrics["minutes_to_pickup_median"] is not None,
      f"({metrics['minutes_to_pickup_median']})")
check("честно названо, что не измеряется",
      metrics["not_measured"] and "появления" in metrics["not_measured"][0],
      f"({metrics.get('not_measured')})")

# Доставили вовремя — доля появляется
ds.set_action_status(ds.ACTION_DELIVER, "send-to-delivery", "admin")
ds.advance_assignment(6003, courier_user_id=11, action=ds.ACTION_DELIVER,
                      username="ivan")
metrics = ds.delivery_metrics(TODAY.isoformat(), TODAY.isoformat(), "Новосибирск")
check("после доставки доля вовремя посчиталась",
      metrics["on_time_share"] is not None, f"({metrics['on_time_share']})")

# Аутсорс после снятия брони — прямой аргумент в найме курьеров
add_order(6020, net_cost=700)
ds.claim_order(6020, courier_user_id=11, courier_name="Иван", city="Новосибирск")
with cs.get_db() as conn:
    conn.execute("UPDATE courier_orders SET delivery_code = 'ya-dostavka' "
                 " WHERE retailcrm_order_id = 6020")
ds.release_orphan_claims(CODES)
metrics = ds.delivery_metrics(TODAY.isoformat(), TODAY.isoformat(), "Новосибирск")
check("ушедшее аутсорсу посчитано", metrics["outsourced_after_release"] == 1,
      f"({metrics['outsourced_after_release']})")
check("и сумма тоже", metrics["outsourced_amount"] == 700.0,
      f"({metrics['outsourced_amount']})")


# ============================================================================
print("\n4. Права: курьеру экран управляющего недоступен")
# ============================================================================

from werkzeug.security import generate_password_hash  # noqa: E402


def make_user(username, role, sections):
    conn = auth.get_db()
    try:
        conn.execute(
            "INSERT INTO users (username, full_name, password_hash, role, is_active, created_at) "
            "VALUES (?, ?, ?, ?, 1, datetime('now'))",
            (username, username, generate_password_hash("Parol12345"), role))
        for section in sections:
            conn.execute("INSERT INTO permissions (username, module_name, can_view) "
                         "VALUES (?, ?, 1)", (username, section))
        conn.commit()
    finally:
        conn.close()


make_user("kurier", "courier", ["courier_app"])
make_user("upravl", "manager", ["courier_dispatch"])


def login(username):
    client = app.test_client()
    r = client.post("/api/auth/login",
                    json={"username": username, "password": "Parol12345"})
    assert r.status_code == 200, r.data
    return client


courier = login("kurier")
manager = login("upravl")

for path in ("/api/courier/overview", "/api/courier/metrics",
             "/api/courier/assignments"):
    check(f"курьер не видит {path}",
          courier.get(path).status_code == 403,
          f"({courier.get(path).status_code})")
    check(f"управляющий видит {path}",
          manager.get(path).status_code == 200,
          f"({manager.get(path).status_code})")

AJAX = {auth.AJAX_HEADER: auth.AJAX_HEADER_VALUE}
r = courier.post("/api/courier/assignments/6002/release", headers=AJAX)
check("курьер не снимает чужую бронь через ручку управляющего",
      r.status_code == 403, f"({r.status_code})")
r = manager.post("/api/courier/assignments/6002/release", headers=AJAX)
check("управляющий снимает", r.status_code == 200,
      f"({r.status_code}: {r.get_data(as_text=True)[:120]})")

# Профили правит только админ: город решает, что курьер видит
r = manager.post("/api/courier/profiles/11", headers=AJAX,
                 json={"username": "kurier", "city": "Новосибирск"})
check("управляющий профиль не правит — это админское", r.status_code == 403,
      f"({r.status_code})")


# ============================================================================
print("\n4-бис. Удаление и отключение курьера")
# ============================================================================
# «Отключить» и «удалить» — разные действия. Отпуск не повод терять город и
# связку с CRM, которые заводили руками.

ds.save_courier_profile(user_id=11, username="kurier", city="Новосибирск",
                        retailcrm_courier_id=None, active=True, updated_by="test")
ds.save_courier_profile(user_id=12, username="zapas", city="Новосибирск",
                        retailcrm_courier_id=None, active=True, updated_by="test")

ds.set_profile_active(12, False, "admin")
check("отключённый курьер сохраняет город",
      ds.get_courier_profile(12)["city"] == "Новосибирск")
check("и помечен неактивным", ds.get_courier_profile(12)["active"] == 0)
check("отключённый не попадает в адресаты уведомлений",
      12 not in ds.courier_user_ids("Новосибирск"),
      f"({ds.courier_user_ids('Новосибирск')})")

ds.set_profile_active(12, True, "admin")
check("включается обратно", ds.get_courier_profile(12)["active"] == 1)

# Живую бронь заводим здесь же, а не полагаемся на разделы выше: там
# управляющий её как раз снимал, и проверка молча превращалась в пустую
add_order(6030)
ds.claim_order(6030, courier_user_id=11, courier_name="Иван", city="Новосибирск")

try:
    ds.delete_courier_profile(11)
    check("профиль с живыми бронями не удаляется", False, "(удалился)")
except ValueError as e:
    check("профиль с живыми бронями не удаляется", "в работе" in str(e), f"({e})")

ds.save_push_subscription(12, "https://push.test/del", "k", "a", "Android")
ds.delete_courier_profile(12)
check("свободный профиль удаляется", ds.get_courier_profile(12) is None)
check("подписки на пуши убраны вместе с ним",
      not ds.push_subscriptions_for([12]), f"({ds.push_subscriptions_for([12])})")

r = manager.delete("/api/courier/profiles/11", headers=AJAX)
check("управляющий профиль не удаляет — это админское", r.status_code == 403,
      f"({r.status_code})")
r = courier.delete("/api/courier/profiles/11", headers=AJAX)
check("курьер тем более", r.status_code == 403, f"({r.status_code})")


# ============================================================================
print("\n5. Разметка, стили и подключение раздела")
# ============================================================================

files = {}
for name in ("courier-dispatch.js", "courier-dispatch.css", "index.html", "script.js"):
    with open(os.path.join(DASHBOARD, name), encoding="utf-8") as f:
        files[name] = f.read()

js, css, html, script = (files["courier-dispatch.js"], files["courier-dispatch.css"],
                         files["index.html"], files["script.js"])

check("контейнер раздела есть в разметке", 'id="cdispRoot"' in html)
check("на контейнере неймспейс стилей", 'class="cdisp"' in html,
      "(без него токены --bx-* не подхватятся)")
check("скрипт подключён", "/courier-dispatch.js" in html)
check("стили подключены", "/courier-dispatch.css" in html)
check("раздел активируется из script.js", "CourierDispatchModule" in script)

# Классы, которых нет в CSS, ломают экран молча
used = set(re.findall(r'class="(cdisp[a-z0-9_ -]*)"', js))
missing = []
for group in used:
    for cls in group.split():
        if cls.startswith("cdisp") and "." + cls not in css:
            missing.append(cls)
check("все классы cdisp-* описаны в CSS", not missing, f"({sorted(set(missing))})")

check("нет нативных диалогов", not re.search(r"(?<![.\w])(alert|confirm|prompt)\(", js)
      or "BarhatUI" in js)
EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️]")
check("нет эмодзи в разделе", not EMOJI.findall(js) and not EMOJI.findall(css))


# ============================================================================
print()
if failures:
    print(f"=== ПРОВАЛОВ: {len(failures)} ===")
    for name in failures:
        print(f"  - {name}")
    sys.exit(1)

print("=== Экран управляющего в порядке ===")
sys.exit(0)
