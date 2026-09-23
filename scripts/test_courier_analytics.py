"""
Сторож расчёта аналитики по курьерам (Фаза 1 плана 2026-09-23).

Что здесь ловится — и почему только прогоном, а не чтением кода:

1. **Пояс салона.** `delivered_at` в UTC, интервал доставки — в стенных часах
   салона. Код без пересчёта выглядит рабочим и считает вовремя доставленным
   всё подряд: сравнение «09:30 UTC против 14:00» проходит по любому салону.
   Поэтому в данных ДВА города с разным смещением, и есть заказ, вердикт по
   которому меняется на противоположный без учёта пояса.
2. **Непосчитанное превращается в ноль.** Заказ без интервала и салон без
   часового пояса не имеют права попасть ни в числитель доли, ни в
   знаменатель: «неизвестно» и «успел» — разные вещи.
3. **Доставленное раньше окна.** Опоздание считается только от конца
   интервала; минус сорок минут в среднем компенсировали бы чужое опоздание.
4. **Среднее опоздание — по опоздавшим.** Усреднение с нулями превращает
   40 минут в 6 и прячет проблему.
5. **Две брони на одном заказе.** Взял → снял → взял другой: это ДВЕ брони и
   один заказ, иначе не сойдётся ни одно слагаемое.
6. **Переименованный курьер** не должен разъезжаться на две строки: группируем
   по `courier_user_id`, имя показываем последнее.
7. **Пустой справочник типов доставки** = «ничего не показываем», а не
   «показываем все заказы периода».
8. **План запроса.** Фильтр по двум полям требует составного индекса —
   одиночные не складываются (CLAUDE.md). Проверяется `EXPLAIN QUERY PLAN`,
   а не временем: на тестовой базе разница незаметна.

Запуск: python scripts/test_courier_analytics.py
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

WORK_DIR = tempfile.mkdtemp(prefix="courier_analytics_")
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

from couriers import analytics  # noqa: E402
from couriers import delivery_storage as ds  # noqa: E402
from couriers import storage as cs  # noqa: E402

cs.init_couriers_tables()
ds.init_delivery_tables()

DAY = "2026-09-10"
OTHER_DAY = "2026-09-11"

# Два города с РАЗНЫМ смещением плюс салон, которому пояс не задали.
SITES = [
    ("ekb", "ЕКБ Бажова 89", "Екатеринбург", 5),
    ("nsk", "НСК Восход 3", "Новосибирск", 7),
    ("new", "Новый салон", "Пермь", None),
]

with cs.get_db() as conn:
    for code, name, city, offset in SITES:
        conn.execute(
            "INSERT OR REPLACE INTO courier_sites (code, name, city, utc_offset) "
            "VALUES (?, ?, ?, ?)", (code, name, city, offset))
    for code, name, is_courier in (("dostavka-kurerom", "Доставка курьером", 1),
                                   ("ya-dostavka", "Яндекс Доставка", 0)):
        conn.execute(
            "INSERT OR REPLACE INTO delivery_types (code, name, counts_as_courier, active) "
            "VALUES (?, ?, ?, 1)", (code, name, is_courier))


def add_order(order_id, site="ekb", day=DAY, time_from="12:00", time_to="14:00",
              code="dostavka-kurerom", net_cost=300, status="complete"):
    with cs.get_db() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO courier_orders
                (retailcrm_order_id, order_number, delivery_date, site_code,
                 city, status, delivery_code, net_cost,
                 delivery_time_from, delivery_time_to)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (order_id, str(order_id), day, site,
              dict((s[0], s[2]) for s in SITES)[site], status, code, net_cost,
              time_from, time_to))


def add_claim(order_id, user_id, name, state, delivered_at=None, reason=None,
              claimed_at=f"{DAY} 06:00:00", picked_up_at=None, released_at=None):
    with cs.get_db() as conn:
        conn.execute("""
            INSERT INTO delivery_assignments
                (retailcrm_order_id, courier_user_id, courier_name, state,
                 claimed_at, picked_up_at, delivered_at, released_at, release_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (order_id, user_id, name, state, claimed_at, picked_up_at,
              delivered_at, released_at, reason))


# ============================================================================
print("\n1. Пояс салона решает, опоздал курьер или нет")
# ============================================================================
#
# Интервал до 14:00 по стенным часам салона. Екатеринбург — UTC+5, значит
# дедлайн в UTC = 09:00. Доставка в 09:30 UTC — это 14:30 по салону, опоздание
# на 30 минут. Код БЕЗ пересчёта сравнит «09:30» с «14:00» и объявит заказ
# доставленным вовремя.

add_order(1, site="ekb")
add_claim(1, 11, "Иван", ds.STATE_DELIVERED, delivered_at=f"{DAY} 09:30:00")

# Тот же самый вердикт с другой стороны: Новосибирск UTC+7, дедлайн 07:00 UTC,
# доставка в 06:30 UTC = 13:30 по салону — вовремя.
add_order(2, site="nsk")
add_claim(2, 12, "Анна", ds.STATE_DELIVERED, delivered_at=f"{DAY} 06:30:00")

data = analytics.load_analytics(DAY, DAY)
late_ids = [o["retailcrm_order_id"] for o in data["late_orders"]]
check("заказ ЕКБ признан опоздавшим (без пояса был бы «вовремя»)",
      late_ids == [1], f"({late_ids})")
check("опоздание посчитано в минутах салона",
      data["late_orders"][0]["late_minutes"] == 30,
      f"({data['late_orders'][0]['late_minutes']})")
check("заказ НСК признан доставленным вовремя",
      data["totals"]["on_time"] == 1, f"({data['totals']})")

# ============================================================================
print("\n2. Раньше окна — это вовремя, а не «минус сорок минут»")
# ============================================================================

add_order(3, site="nsk")
add_claim(3, 12, "Анна", ds.STATE_DELIVERED, delivered_at=f"{DAY} 04:00:00")

data = analytics.load_analytics(DAY, DAY)
check("ранняя доставка засчитана как вовремя",
      data["totals"]["on_time"] == 2, f"({data['totals']['on_time']})")
check("ранняя доставка не уменьшила среднее опоздание",
      data["totals"]["late_minutes_avg"] == 30.0,
      f"({data['totals']['late_minutes_avg']})")

# ============================================================================
print("\n3. Непосчитанное не превращается в ноль")
# ============================================================================

add_order(4, site="ekb", time_to=None)                    # интервала нет
add_claim(4, 11, "Иван", ds.STATE_DELIVERED, delivered_at=f"{DAY} 09:00:00")
add_order(5, site="ekb", time_to="уточняется")            # интервал словами
add_claim(5, 11, "Иван", ds.STATE_DELIVERED, delivered_at=f"{DAY} 09:00:00")
add_order(6, site="new")                                  # салон без пояса
add_claim(6, 12, "Анна", ds.STATE_DELIVERED, delivered_at=f"{DAY} 09:00:00")

data = analytics.load_analytics(DAY, DAY)
not_counted = data["totals"]["not_counted"]
check("заказ без интервала не попал в долю",
      not_counted["no_interval"] == 2, f"({not_counted})")
check("«уточняется» не стало концом дня, а осталось неизвестным",
      data["totals"]["on_time"] + data["totals"]["late"] == 3,
      f"(посчитано {data['totals']['on_time'] + data['totals']['late']})")
check("салон без пояса посчитан отдельно",
      not_counted["no_timezone"] == 1, f"({not_counted})")
check("салон без пояса назван по имени",
      not_counted["sites_without_timezone"] == ["Новый салон"],
      f"({not_counted['sites_without_timezone']})")

# ============================================================================
print("\n4. Брони считаются бронями, курьер — идентификатором")
# ============================================================================
#
# Заказ 7: Иван взял и отказался, Анна взяла и довезла. Это ДВЕ брони и ОДИН
# заказ. Плюс у Ивана в поздней брони другое написание имени — человека
# переименовали, и на две строки он разъезжаться не должен.

add_order(7, site="nsk")
add_claim(7, 11, "Иван", ds.STATE_RELEASED, reason=ds.RELEASE_SELF,
          claimed_at=f"{DAY} 05:00:00", released_at=f"{DAY} 05:30:00")
add_claim(7, 12, "Анна", ds.STATE_DELIVERED, delivered_at=f"{DAY} 06:00:00",
          claimed_at=f"{DAY} 05:40:00")
add_order(8, site="nsk")
add_claim(8, 11, "Иван Петров", ds.STATE_DELIVERED, delivered_at=f"{DAY} 06:00:00",
          claimed_at=f"{DAY} 07:00:00")

data = analytics.load_analytics(DAY, DAY)
rows = {row["courier_user_id"]: row for row in data["couriers"]}
check("две брони на одном заказе посчитаны обе",
      data["totals"]["claims"] == 9, f"({data['totals']['claims']})")
check("курьеров ровно двое, переименование не раздвоило",
      len(data["couriers"]) == 2, f"({[r['courier_name'] for r in data['couriers']]})")
check("показано последнее известное имя",
      rows[11]["courier_name"] == "Иван Петров", f"({rows[11]['courier_name']})")
check("отказ курьера попал в «снял руками»",
      data["totals"]["released_by_hand"] == 1 and data["totals"]["released_self"] == 1,
      f"({data['totals']})")

# ============================================================================
print("\n5. Аутсорс: после снятия брони и «никто не взял»")
# ============================================================================

add_order(9, site="ekb", code="ya-dostavka", net_cost=700)
add_claim(9, 11, "Иван Петров", ds.STATE_RELEASED, reason=ds.RELEASE_OUTSOURCED,
          released_at=f"{DAY} 08:00:00")
add_order(10, site="ekb", code="ya-dostavka")     # ушёл службе, броней не было
add_order(11, site="ekb", code="dostavka-kurerom")  # свой, но никто не взял

data = analytics.load_analytics(DAY, DAY)
check("ушедшее аутсорсу после брони посчитано",
      data["totals"]["outsourced_after_claim"] == 1,
      f"({data['totals']['outsourced_after_claim']})")
check("и сумма доставки тоже", data["totals"]["outsourced_amount"] == 700.0,
      f"({data['totals']['outsourced_amount']})")
check("в списке аутсорса есть номер заказа и курьер",
      data["outsourced_after_claim"][0]["order_number"] == "9"
      and data["outsourced_after_claim"][0]["courier_name"] == "Иван Петров",
      f"({data['outsourced_after_claim']})")

never = [o["retailcrm_order_id"] for o in data["outsourced_never_claimed"]]
check("заказ службы без броней попал в «не взяты никем»", never == [10], f"({never})")
check("свой заказ без броней туда НЕ попал", 11 not in never, f"({never})")
check("счётчик совпадает с длиной списка",
      data["totals"]["outsourced_never_claimed"] == len(never), f"({data['totals']})")

# ============================================================================
print("\n6. Пустой справочник типов = ничего не показываем")
# ============================================================================

with cs.get_db() as conn:
    conn.execute("UPDATE delivery_types SET counts_as_courier = 0")
empty = analytics.load_analytics(DAY, DAY)
check("без настройки справочника список «ушли службе» пуст, а не «все заказы»",
      empty["outsourced_never_claimed"] == [],
      f"({len(empty['outsourced_never_claimed'])} строк)")
with cs.get_db() as conn:
    conn.execute("UPDATE delivery_types SET counts_as_courier = 1 "
                 " WHERE code = 'dostavka-kurerom'")

# ============================================================================
print("\n7. Сходимость: из чего складываются брони")
# ============================================================================

data = analytics.load_analytics(DAY, DAY)
t = data["totals"]
parts = (t["delivered"] + t["released_by_hand"] + t["outsourced_after_claim"]
         + t["released_expired"] + t["order_gone"] + t["active"] + t["problem"])
check("слагаемые сходятся с числом броней", parts == t["claims"],
      f"({parts} против {t['claims']})")
check("разбивка «снял руками» сходится сама с собой",
      t["released_self"] + t["released_admin"] == t["released_by_hand"],
      f"({t['released_self']} + {t['released_admin']} ≠ {t['released_by_hand']})")

# ============================================================================
print("\n8. Фильтр салонов и периода")
# ============================================================================

# По НСК заказы 2, 3, 7 и 8, причём у седьмого ДВЕ брони — пять записей.
# Заказы Екатеринбурга и салона без пояса в выборку попасть не должны.
only_nsk = analytics.load_analytics(DAY, DAY, ["nsk"])
sites_in_late = {o["site_name"] for o in only_nsk["late_orders"]}
check("выбран один салон — чужих заказов нет",
      only_nsk["totals"]["claims"] == 5, f"({only_nsk['totals']['claims']})")
check("непосчитанное чужого салона тоже отфильтровано",
      only_nsk["totals"]["not_counted"]["sites_without_timezone"] == [],
      f"({only_nsk['totals']['not_counted']})")
check("опоздания чужого салона отфильтрованы", sites_in_late == set(),
      f"({sites_in_late})")

add_order(20, site="ekb", day=OTHER_DAY)
add_claim(20, 11, "Иван Петров", ds.STATE_DELIVERED,
          delivered_at=f"{OTHER_DAY} 09:30:00", claimed_at=f"{OTHER_DAY} 06:00:00")
check("заказ соседнего дня не попал в период",
      analytics.load_analytics(DAY, DAY)["totals"]["claims"] == t["claims"])
check("и попадает, когда период расширили",
      analytics.load_analytics(DAY, OTHER_DAY)["totals"]["claims"] == t["claims"] + 1)

# ============================================================================
print("\n9. «Мало данных» у курьера с парой доставок")
# ============================================================================

data = analytics.load_analytics(DAY, OTHER_DAY)
rows = {row["courier_user_id"]: row for row in data["couriers"]}
check("у курьера с парой посчитанных доставок стоит пометка",
      rows[11]["low_data"] is True, f"({rows[11]})")
check("доля при этом всё равно показана, а не спрятана",
      rows[11]["on_time_share"] is not None, f"({rows[11]['on_time_share']})")
check("курьер без единой посчитанной доставки не делит на ноль",
      all(r["on_time_share"] is None or isinstance(r["on_time_share"], float)
          for r in data["couriers"]))

# ============================================================================
print("\n10. План запроса: фильтр по двум полям идёт по составному индексу")
# ============================================================================
#
# Одиночные индексы не складываются: SQLite берёт на таблицу ОДИН. Проверяется
# планом, а не временем — на тестовой базе в десяток строк разница незаметна,
# а на объёме прода это секунды (CLAUDE.md, раздел про составные индексы).

clause, params = analytics._site_filter(["ekb", "nsk"])
with cs.get_db() as conn:
    plan = " ".join(str(row[3]) for row in conn.execute(f"""
        EXPLAIN QUERY PLAN
        SELECT a.id
          FROM delivery_assignments a
          JOIN courier_orders o ON o.retailcrm_order_id = a.retailcrm_order_id
          LEFT JOIN courier_sites s ON s.code = o.site_code
         WHERE o.delivery_date >= ? AND o.delivery_date <= ?{clause}
    """, (DAY, DAY, *params)).fetchall())

check("выборка идёт по индексу, а не сканом витрины",
      "SCAN o" not in plan and "idx_courier_orders" in plan, f"({plan})")
check("бронь ищется по индексу заказа", "SCAN a" not in plan, f"({plan})")

# ============================================================================
print("\n11. Отчёт о цене запроса")
# ============================================================================

check("разбивка по шагам отдана", set(data["timings_ms"]) >= {
    "claims", "delivery_types", "never_claimed", "aggregate", "total"},
      f"({sorted(data['timings_ms'])})")
check("честно названо, чего не умеем",
      any("отметке курьера" in text for text in data["not_measured"]),
      f"({data['not_measured']})")

# ============================================================================
print("\n12. Проверка самого сторожа: на коде без пояса раздел 1 обязан упасть")
# ============================================================================
#
# Сторож, который зеленеет и на сломанном коде, закрепляет ошибку — так уже
# было с адресом доставки (CLAUDE.md). Убеждаемся ДЕЛОМ: подменяем формулу на
# наивную («сравним время как есть, пояс не при чём») и смотрим, меняется ли
# вердикт по заказу 1. Если не меняется — раздел 1 ничего не проверяет.

from datetime import datetime  # noqa: E402

original_deadline = analytics.salon_time.deadline_utc


def naive_deadline(delivery_date, time_to, utc_offset):
    """Та же функция, но без пересчёта в UTC — ошибка, которую ловим."""
    text = (time_to or "").strip()
    if len(text) < 4 or ":" not in text:
        return None
    head, tail = text.split(":", 1)
    if not head.strip().isdigit() or not tail[:2].isdigit():
        return None
    try:
        day = datetime.strptime(delivery_date, "%Y-%m-%d")
    except (ValueError, TypeError):
        return None
    return day.replace(hour=int(head), minute=int(tail[:2]))


analytics.salon_time.deadline_utc = naive_deadline
try:
    broken = analytics.load_analytics(DAY, DAY)
    broken_late = [o["retailcrm_order_id"] for o in broken["late_orders"]]
finally:
    analytics.salon_time.deadline_utc = original_deadline

check("без пояса опоздавший заказ ЕКБ пропадает из списка",
      1 not in broken_late, f"({broken_late}) — раздел 1 не различает код с поясом и без")
check("после возврата формулы он снова на месте",
      1 in [o["retailcrm_order_id"]
            for o in analytics.load_analytics(DAY, DAY)["late_orders"]])

# ============================================================================
print("\n13. Ручка: право на раздел, период и фильтр салонов")
# ============================================================================
#
# Права проверяются ПРОГОНОМ, а не чтением декоратора: секция у курьера и
# секция у управляющего — разные ветки, и та, что не исполнялась ни разу,
# уже приносила 500 на проде (CLAUDE.md про тестового менеджера без салонов).

import auth  # noqa: E402
from pyrus.server import app  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

app.config["TESTING"] = True
with app.app_context():
    auth.init_auth_tables()
    for username, role in (("analytics-kurier", "courier"),
                           ("analytics-upravl", "manager")):
        conn = auth.get_db()
        try:
            conn.execute(
                "INSERT INTO users (username, full_name, password_hash, role, "
                "is_active, created_at) VALUES (?, ?, ?, ?, 1, datetime('now'))",
                (username, username, generate_password_hash("Parol12345"), role))
            conn.commit()
        finally:
            conn.close()
    auth.migrate_permissions_for_existing_users()
    auth.migrate_new_module_permissions("courier_dispatch", ["admin", "manager"])


def login(client, username):
    return client.post("/api/auth/login",
                       json={"username": username, "password": "Parol12345"})


with app.test_client() as client:
    login(client, "analytics-kurier")
    code = client.get(f"/api/courier/analytics?date_from={DAY}&date_to={DAY}").status_code
    check("курьеру аналитика не отдаётся", code == 403, f"({code})")

with app.test_client() as client:
    login(client, "analytics-upravl")
    body = client.get(f"/api/courier/analytics?date_from={DAY}&date_to={DAY}").get_json()
    check("управляющий получает числа",
          body["data"]["totals"]["claims"] == t["claims"],
          f"({body.get('data', {}).get('totals', {}).get('claims')})")
    check("справочник салонов приехал вместе с данными",
          {s["code"] for s in body["meta"]["sites"]} >= {"ekb", "nsk"},
          f"({body['meta'].get('sites')})")

    filtered = client.get(
        f"/api/courier/analytics?date_from={DAY}&date_to={DAY}&sites=nsk"
    ).get_json()["data"]
    check("фильтр салонов доехал до расчёта",
          filtered["totals"]["claims"] == 5, f"({filtered['totals']['claims']})")
    check("выбранные салоны названы в ответе",
          filtered["site_codes"] == ["nsk"], f"({filtered['site_codes']})")

    # Слишком длинный период отбивается с объяснением, а не сужается молча:
    # иначе человек получит не те данные, которые запросил.
    long_period = client.get(
        "/api/courier/analytics?date_from=2026-01-01&date_to=2026-12-31")
    check("год отбит с названной причиной", long_period.status_code == 400
          and "дней" in (long_period.get_json().get("error") or ""),
          f"({long_period.status_code}, {long_period.get_json()})")

    # Мусор в датах не роняет ручку и не отдаёт пустоту без объяснения.
    broken = client.get("/api/courier/analytics?date_from=вчера&date_to=сегодня")
    check("кривые даты не роняют ручку", broken.status_code in (200, 400),
          f"({broken.status_code})")

print("")
if failures:
    print(f"=== ПРОВАЛЕНО: {len(failures)} ===")
    for name in failures:
        print(f"  - {name}")
    sys.exit(1)
print("=== Аналитика курьеров считается верно ===")
