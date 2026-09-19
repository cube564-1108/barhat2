"""
Сторож: готовность заказа — факт, а не текущий статус CRM.

Что здесь ловится (разбор 19.09.2026, заказ 154553).

Оператор КЦ перевёл статус «Заказ готов» → «Вызван курьер», и собранный заказ
стал для модуля несобранным: готовность считалась как «текущий статус равен
order-complete». Кнопка «Забрал заказ» сменилась неактивной «Ждём отметки
Готов», забрать букет стало нечем, бронь сгорела по таймеру, а вместе с ней
исчезла и карточка — статуса «Вызван курьер» не было в справочнике видимых, и
заказ держался в ленте только живой бронью.

Проверяется весь путь, а не одна функция: отметку ставят ДВА пути записи
витрины (лента изменений и глубокий синк), а читают четыре места. Сторож,
проверяющий одно из них, пропустит расхождение между ними.

Запуск: python scripts/test_courier_ready_persists.py
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

WORK_DIR = tempfile.mkdtemp(prefix="courier_ready_")
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

from couriers import delivery_storage as ds  # noqa: E402
from couriers import storage as cs  # noqa: E402

cs.init_couriers_tables()
ds.init_delivery_tables()

# «Сегодня» по стенным часам салона: в 18:31 UTC в салоне UTC+7 уже следующие
# сутки, и заказ, датированный «сегодня» по UTC, для салона вчерашний.
SALON_UTC_OFFSET = 7
TODAY = (datetime.utcnow() + timedelta(hours=SALON_UTC_OFFSET)).date()
TOMORROW = TODAY + timedelta(days=1)
CODES = ["dostavka-kurerom"]

with cs.get_db() as conn:
    conn.execute("INSERT OR REPLACE INTO courier_sites (code, name, city, utc_offset) "
                 "VALUES ('site-a', 'Восход', 'Новосибирск', 7)")
    for code, name in (("send-to-florist", "Передан флористу"),
                       ("order-complete", "Заказ готов"),
                       ("call-courier", "Вызван курьер"),
                       ("send-to-delivery", "Передан курьеру")):
        conn.execute("INSERT OR REPLACE INTO order_statuses (code, name) VALUES (?, ?)",
                     (code, name))

ds.set_action_status(ds.ACTION_PICKUP, "send-to-delivery", "admin")


def order_row(order_id, status, date=None, time_from="23:30"):
    """Заказ в том виде, в каком его отдаёт разбор ответа CRM."""
    return {
        "retailcrm_order_id": order_id,
        "order_number": f"N{order_id}",
        "delivery_date": (date or TODAY).isoformat(),
        "status": status,
        "site_code": "site-a",
        "city": "Новосибирск",
        "delivery_city": "Новосибирск",
        "delivery_code": "dostavka-kurerom",
        "delivery_time_from": time_from,
        "delivery_time_to": "23:59",
        "address_text": "ул. Ленина, 45",
        "net_cost": 300,
        "total_summ": 3000,
        "items": [],
    }


def feed(order_id, status, date=None):
    """Точечная запись витрины — путь ленты изменений."""
    ds.apply_orders_from_crm([order_row(order_id, status, date)])


def stamp_of(order_id):
    """
    Отметка о сборке. На версии кода без колонки — None, а не исключение:
    сторож обязан ДОЙТИ до поведенческих проверок на сломанном коде и назвать
    сам баг, а не упасть на отсутствующем имени (правило CLAUDE.md).
    """
    try:
        with cs.get_db() as conn:
            row = conn.execute(
                "SELECT ready_seen_at FROM courier_orders WHERE retailcrm_order_id = ?",
                (order_id,)).fetchone()
    except Exception:
        return None
    return row["ready_seen_at"] if row else None


def feed_order(order_id, courier_user_id=None):
    """Как заказ выглядит в ленте курьера."""
    rows = ds.list_orders_for_courier(
        city="Новосибирск", date_from=TODAY.isoformat(),
        date_to=TOMORROW.isoformat(), courier_user_id=courier_user_id,
        courier_delivery_codes=CODES)
    for row in rows:
        if row["retailcrm_order_id"] == order_id:
            return row
    return None


# ============================================================================
print("\n1. «Вызван курьер» заведён в справочнике — и именно видимым")
# ============================================================================
# Роль visible, а не ready (решение владельца 19.09.2026): статус ставит
# оператор КЦ, и забор по нему разрешать нельзя — иначе курьер поедет за
# букетом, который ещё собирают.

roles = {row["status_code"]: row["role"] for row in ds.list_visible_statuses()}
check("«Вызван курьер» виден курьеру", roles.get("call-courier") == ds.ROLE_VISIBLE,
      f"({roles.get('call-courier')})")
ready_codes = ds.visible_status_codes().get(ds.ROLE_READY, [])
check("и не считается отметкой о сборке",
      "call-courier" not in ready_codes, f"({ready_codes})")


# ============================================================================
print("\n2. Статус уехал дальше отметки — заказ остался собранным")
# ============================================================================

feed(7001, "send-to-florist")
check("до сборки заказ не готов", feed_order(7001)["is_ready"] is False)
check("и отметки нет", stamp_of(7001) is None, f"({stamp_of(7001)})")

feed(7001, "order-complete")
check("флорист отметил сборку — заказ готов", feed_order(7001)["is_ready"] is True)
was = stamp_of(7001)
check("отметка проставлена", bool(was), f"({was})")

feed(7001, "call-courier")
after = feed_order(7001)
check("заказ не исчез из ленты на «Вызван курьер»", after is not None)
check("оператор перевёл статус вперёд — заказ ВСЁ ЕЩЁ готов",
      bool(after and after["is_ready"]), f"({after and after['is_ready']})")
check("отметка не переписана", stamp_of(7001) == was, f"({stamp_of(7001)} vs {was})")

feed(7001, "send-to-delivery")
# Через ленту его уже не увидеть — «Передан курьеру» в видимых нет намеренно,
# этот статус ставит сам модуль при заборе. Смотрим карточку.
check("и на следующем статусе тоже",
      ds.order_for_courier(7001, city="Новосибирск")["is_ready"] is True)
feed(7001, "call-courier")   # вернуть в состояние, с которым работают дальше


# ============================================================================
print("\n3. Заказ на «Вызван курьер» виден в ленте, а не только владельцу брони")
# ============================================================================
# До 19.09.2026 статуса не было в справочнике: заказ исчезал у всех курьеров
# города, а у владельца брони держался на спецветке «свой виден всегда» — и
# пропадал вместе с бронью, когда та сгорала.

feed(7002, "order-complete")
feed(7002, "call-courier")
check("свободный заказ виден любому курьеру города", feed_order(7002) is not None)
check("и виден свободным", (feed_order(7002) or {}).get("is_free") is True)


# ============================================================================
print("\n4. Забрать собранный заказ можно после перевода статуса")
# ============================================================================
# Тот самый отказ, который видел курьер: «Ждём отметки Готов» на заказе,
# который флорист собрал полчаса назад.

try:
    ds.claim_order(7001, courier_user_id=21, courier_name="Шестаков",
                   city="Новосибирск")
    ds.advance_assignment(7001, courier_user_id=21, action=ds.ACTION_PICKUP,
                          username="courier")
    picked = True
    reason = ""
except ds.ClaimError as e:
    # Ловим и бронь тоже: на версии без «Вызван курьер» в справочнике заказ
    # недоступен уже для брони, и падение сторожа скрыло бы, на чём именно.
    picked = False
    reason = str(e)
check("«Забрал заказ» проходит", picked, f"({reason})")


# ============================================================================
print("\n5. Дисциплина отметки сохранена: несобранный забрать нельзя")
# ============================================================================
# Обратная сторона: «Вызван курьер» сам по себе готовностью не считается.
# Оператор мог вызвать курьера к заказу, который ещё собирают.

feed(7003, "call-courier")
check("без отметки флориста заказ не готов",
      (feed_order(7003) or {"is_ready": False})["is_ready"] is False)
try:
    ds.claim_order(7003, courier_user_id=22, courier_name="Второй",
                   city="Новосибирск")
    ds.advance_assignment(7003, courier_user_id=22, action=ds.ACTION_PICKUP,
                          username="courier2")
    blocked = False
    detail = "забор прошёл"
except ds.ClaimError as e:
    blocked = e.code == "not_ready"
    detail = f"{e.code}: {e}"
check("забор отбит именно неготовностью", blocked, f"({detail})")


# ============================================================================
print("\n6. Отметку не теряет ни один из двух путей записи витрины")
# ============================================================================
# Глубокий синк пересобирает окно через DELETE + INSERT. Отметка наша, в CRM
# её нет — значит восстановить после удаления неоткуда.

before = stamp_of(7001)
cs.replace_orders_window(TODAY.isoformat(), TODAY.isoformat(),
                         [order_row(7001, "call-courier"),
                          order_row(7002, "call-courier")])
check("глубокий синк отметку сохранил", stamp_of(7001) == before,
      f"({stamp_of(7001)} vs {before})")
check("и заказ по-прежнему готов",
      bool((feed_order(7001) or {}).get("is_ready")))

# Заказ переехал на завтра: его строка лежит под старой датой, выборкой по
# окну не ловится, а INSERT OR REPLACE затрёт её по первичному ключу.
cs.replace_orders_window(TOMORROW.isoformat(), TOMORROW.isoformat(),
                         [order_row(7001, "call-courier", date=TOMORROW)])
check("перенос заказа на другую дату отметку не сбросил",
      stamp_of(7001) == before, f"({stamp_of(7001)} vs {before})")

# Сборка, увиденная только глубоким синком, тоже обязана оставить отметку.
cs.replace_orders_window(TODAY.isoformat(), TODAY.isoformat(),
                         [order_row(7004, "order-complete")])
deep = stamp_of(7004)
check("глубокий синк сам проставляет отметку", bool(deep), f"({deep})")
cs.replace_orders_window(TODAY.isoformat(), TODAY.isoformat(),
                         [order_row(7004, "call-courier")])
check("и не снимает её на следующем статусе", stamp_of(7004) == deep,
      f"({stamp_of(7004)} vs {deep})")


# ============================================================================
print("\n7. Экран управляющего видит то же самое")
# ============================================================================
# Четыре места читают готовность, и разойтись им нельзя: «Собирают» в сетке
# при живой кнопке «Забрал» — это разбор на полчаса.

# 7002 вымело пересборкой окна выше (её смысл в том и есть) — заводим заново
# тем же путём, каким заказ приходит в жизни: отметка флориста, потом вызов.
feed(7002, "order-complete")
feed(7002, "call-courier")

overview = ds.dispatch_overview("Новосибирск", TODAY.isoformat(),
                                TOMORROW.isoformat(), CODES)
grid = {row["retailcrm_order_id"]: row for row in overview["orders"]}
check("заказ на «Вызван курьер» есть в сетке", 7002 in grid, f"({sorted(grid)})")
check("и показан готовым", (grid.get(7002) or {}).get("is_ready") is True)

card = ds.order_for_courier(7002, city="Новосибирск")
check("карточка тоже показывает готовым", card and card["is_ready"] is True)


# ============================================================================
print("\n8. Бэкфилл проставляет отметку по накопленным заказам")
# ============================================================================
# Заказы, лежавшие в витрине до появления колонки, иначе выглядели бы
# несобранными до следующей правки в CRM.

with cs.get_db() as conn:
    try:
        conn.execute("UPDATE courier_orders SET ready_seen_at = NULL")
    except Exception:
        pass        # колонки ещё нет — проверки ниже это и покажут
    conn.execute("UPDATE courier_orders SET status = 'order-complete' "
                 " WHERE retailcrm_order_id = 7002")
    conn.execute("DELETE FROM sync_state WHERE key = ?",
                 (getattr(ds, "READY_SEEN_BACKFILL_KEY",
                          "courier_ready_seen_backfill"),))
ds.init_delivery_tables()
check("заказ в статусе «Заказ готов» получил отметку", bool(stamp_of(7002)),
      f"({stamp_of(7002)})")
check("а уехавший дальше остался пустым — догадок не выдумываем",
      stamp_of(7004) is None, f"({stamp_of(7004)})")


# ============================================================================
print("\n9. «Я еду» — бронь продлевается, но один раз")
# ============================================================================
# Пуш «бронь скоро снимется» до 19.09.2026 звал к кнопке, которой не было:
# удержать заказ мог только «Забрал», а его жмут в салоне.

feed(7005, "order-complete")
claim = ds.claim_order(7005, courier_user_id=31, courier_name="Третий",
                       city="Новосибирск")
before_exp = claim["expires_at"]
extended = ds.extend_claim(7005, courier_user_id=31)
check("срок отодвинут", extended["expires_at"] > before_exp,
      f"({before_exp} → {extended['expires_at']})")

in_feed = feed_order(7005, courier_user_id=31) or {}
check("лента говорит, что бронь продлевали", in_feed.get("claim_extended") is True)
check("и отдаёт новый срок", in_feed.get("expires_at") == extended["expires_at"])

# Талон на предупреждение возвращается: он выдаётся раз на «заказ + событие»,
# и без возврата курьер, честно нажавший «Я еду», не получил бы второго
# «бронь скоро снимется» — заказ ушёл бы у него молча.
with cs.get_db() as conn:
    conn.execute("INSERT OR REPLACE INTO push_events "
                 "  (retailcrm_order_id, event_type) VALUES (?, ?)",
                 (7005, ds.EVENT_CLAIM_EXPIRING))
with cs.get_db() as conn:
    # Право на продление уже израсходовано выше — возвращаем, чтобы проверить
    # именно возврат талона на предупреждение, а не отказ «уже продлевали»
    conn.execute("UPDATE delivery_assignments SET extended_at = NULL "
                 " WHERE retailcrm_order_id = ?", (7005,))
ds.extend_claim(7005, courier_user_id=31)
with cs.get_db() as conn:
    left_talon = conn.execute(
        "SELECT COUNT(*) AS c FROM push_events "
        " WHERE retailcrm_order_id = ? AND event_type = ?",
        (7005, ds.EVENT_CLAIM_EXPIRING)).fetchone()["c"]
check("продление возвращает право на предупреждение", left_talon == 0,
      f"({left_talon})")

try:
    ds.extend_claim(7005, courier_user_id=31)
    again, code = True, ""
except ds.ClaimError as e:
    again, code = False, e.code
check("второй раз продлить нельзя", not again, f"({code})")
check("и отказ назван своим кодом", code == "already_extended", f"({code})")

try:
    ds.extend_claim(7005, courier_user_id=99)
    alien, alien_code = True, ""
except ds.ClaimError as e:
    alien, alien_code = False, e.code
check("чужую бронь не продлить", not alien and alien_code == "forbidden",
      f"({alien_code})")

ds.advance_assignment(7005, courier_user_id=31, action=ds.ACTION_PICKUP,
                      username="third")
try:
    ds.extend_claim(7005, courier_user_id=31)
    taken, taken_code = True, ""
except ds.ClaimError as e:
    taken, taken_code = False, e.code
check("забранный заказ продлевать нечего", not taken and taken_code == "already",
      f"({taken_code})")

# Срок, который уже почти истёк, отсчитывается от СЕЙЧАС, а не от него:
# иначе «продлил на 30 минут» дало бы пять, и кнопка нажата впустую.
feed(7006, "order-complete")
ds.claim_order(7006, courier_user_id=32, courier_name="Четвёртый",
               city="Новосибирск")
almost = (datetime.utcnow() - timedelta(minutes=20)).isoformat(sep=" ",
                                                              timespec="seconds")
with cs.get_db() as conn:
    conn.execute("UPDATE delivery_assignments SET expires_at = ? "
                 " WHERE retailcrm_order_id = ?", (almost, 7006))
late = ds.extend_claim(7006, courier_user_id=32)
check("просроченный срок считается от текущего момента",
      late["expires_at"] > datetime.utcnow().isoformat(sep=" ", timespec="seconds"),
      f"({late['expires_at']})")


# ============================================================================
print("\n10. Кнопка, ручка и текст пуша говорят об одном и том же")
# ============================================================================
# Кнопка без обработчика и ручка без заголовка — два разных способа сделать
# «приложение ничего не делает». Проверяется связка, а не наличие слова.

with open(os.path.join(REPO, "src", "dashboard", "courier-app.js"),
          encoding="utf-8") as fh:
    app_js = fh.read()
check("кнопка «Я еду» есть в разметке", 'data-extend="' in app_js)
check("и её нажатие обработано", "closest('[data-extend]')" in app_js)
check("обработчик зовёт ручку продления",
      "/extend'" in app_js or '/extend"' in app_js)

from couriers import push  # noqa: E402

text = push.notify_claim_expiring.__doc__ or ""
check("пуш назван по той кнопке, что есть в приложении", "Я еду" in text,
      "(докстрока notify_claim_expiring)")

print("\n11. Ручка продления защищена от чужого сайта")

import auth  # noqa: E402
from pyrus.server import app  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

app.config["TESTING"] = True
with app.app_context():
    auth.init_auth_tables()
    conn = auth.get_db()
    try:
        conn.execute(
            "INSERT INTO users (username, full_name, password_hash, role, "
            "                   is_active, created_at) "
            "VALUES ('kurier', 'Курьер', ?, 'courier', 1, datetime('now'))",
            (generate_password_hash("Parol12345"),))
        conn.commit()
    finally:
        conn.close()
    auth.migrate_permissions_for_existing_users()
    conn = auth.get_db()
    try:
        user_id = conn.execute(
            "SELECT id FROM users WHERE username = 'kurier'").fetchone()["id"]
    finally:
        conn.close()

ds.save_courier_profile(user_id=user_id, username="kurier", city="Новосибирск",
                        retailcrm_courier_id=555, active=True, updated_by="test")
feed(7007, "order-complete")

AJAX = {"X-Requested-With": "barhat-dashboard"}
with app.test_client() as client:
    client.post("/api/auth/login",
                json={"username": "kurier", "password": "Parol12345"})
    client.post("/api/courier/orders/7007/claim", headers=AJAX)

    # Без заголовка запрос уходит простой формой с чужого сайта, а CSRF-токенов
    # в проекте нет: единственная защита — этот декоратор (правило CLAUDE.md).
    naked = client.post("/api/courier/orders/7007/extend")
    check("без заголовка ручка отвечает 403", naked.status_code == 403,
          f"({naked.status_code})")

    ok = client.post("/api/courier/orders/7007/extend", headers=AJAX)
    check("со своим заголовком продление проходит", ok.status_code == 200,
          f"({ok.status_code}: {ok.get_json()})")


print()
if failures:
    print(f"=== ПРОВАЛЕНО: {len(failures)} ===")
    for name in failures:
        print(f"  - {name}")
    sys.exit(1)
print("=== Готовность заказа держится ===")
