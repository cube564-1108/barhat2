"""
Сторож брони (Фаза 4): двое курьеров не увезут один букет.

Зачем отдельным прогоном. На проде 2 воркера gunicorn по 8 потоков — до 16
параллельных обработчиков, а запрос к базе на сетевом /data стоит 90-700 мс.
Между «свободен ли заказ» в одном соединении и INSERT в другом лежит окно
шириной в сотни миллисекунд, и в него проходят ВСЕ нажатия разом. Ровно так
29.08.26 открылись три дневные смены на одной точке: все три прочитали
«открытых смен нет».

Обычный тест этого не ловит: последовательный вызов всегда проходит. Ловит
только настоящая гонка, поэтому здесь 20 потоков стартуют по общему барьеру.

Проверяется:
1. 20 одновременных броней на один заказ дают ровно одну живую запись;
2. проигравшие получают внятный отказ с именем победителя, а не 500;
3. частичный уникальный индекс стоит на боевой схеме;
4. лимит броней на город и горизонт бронирования;
5. чужую бронь курьер снять не может, управляющий может;
6. автоснятие работает только из состояния claimed (гонка с «Забрал»);
7. бронь снимается, когда заказ отменили или отдали службе доставки;
8. HTTP: 409 занятому, 403 чужому городу, 400 не тому дню.

Запуск: python scripts/test_courier_claim_race.py
"""

import os
import socket
import sqlite3
import ssl  # noqa: F401  — импортировать до патча сокета
import sys
import tempfile
import threading
from datetime import datetime, timedelta

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))


class NetworkBlocked(Exception):
    pass


def _blocked(*args, **kwargs):
    raise NetworkBlocked("сторож не должен ходить в боевые внешние API")


socket.socket.connect = _blocked

WORK_DIR = tempfile.mkdtemp(prefix="claim_race_")
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

from couriers import delivery_storage as ds  # noqa: E402
from couriers import storage as cs  # noqa: E402

app.config["TESTING"] = True
with app.app_context():
    auth.init_auth_tables()
cs.init_couriers_tables()
ds.init_delivery_tables()

TODAY = datetime.utcnow().date()


def add_order(order_id, city="Новосибирск", status="send-to-florist",
              days=0, site="site-a", delivery_code="dostavka-kurerom"):
    with cs.get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO courier_orders "
            "  (retailcrm_order_id, order_number, delivery_date, delivery_time_from, "
            "   site_code, city, status, delivery_code) "
            "VALUES (?, ?, ?, '18:00', ?, ?, ?, ?)",
            (order_id, str(order_id), (TODAY + timedelta(days=days)).isoformat(),
             site, city, status, delivery_code),
        )


def set_site(code, city, offset):
    with cs.get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO courier_sites (code, name, city, utc_offset) "
            "VALUES (?, ?, ?, ?)",
            (code, "Салон " + code, city, offset),
        )


set_site("site-a", "Новосибирск", 7)
set_site("site-b", "Екатеринбург", 5)


# ============================================================================
print("\n1. Схема: последняя преграда на месте")
# ============================================================================

with cs.get_db() as conn:
    index = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_assign_one_active'"
    ).fetchone()
    columns = {row[1] for row in conn.execute("PRAGMA table_info(delivery_assignments)")}

check("уникальный индекс на живую бронь построен", index is not None)
check("индекс частичный — снятые брони не мешают взять заказ снова",
      index is not None and "WHERE" in (index["sql"] or "").upper(),
      f"({index and index['sql']})")
check("имя курьера лежит рядом с бронью", "courier_name" in columns,
      f"({sorted(columns)})")


# ============================================================================
print("\n2. Двадцать потоков на один заказ")
# ============================================================================

add_order(9001)

barrier = threading.Barrier(20)
results = {"ok": 0, "taken": 0, "other": []}
lock = threading.Lock()


def try_claim(index):
    barrier.wait()          # стартуем все разом, иначе гонки не будет
    try:
        ds.claim_order(9001, courier_user_id=100 + index,
                       courier_name=f"Курьер {index}", city="Новосибирск")
        with lock:
            results["ok"] += 1
    except ds.ClaimError as e:
        with lock:
            if e.code == "taken":
                results["taken"] += 1
            else:
                results["other"].append(f"{e.code}: {e}")
    except Exception as e:
        with lock:
            results["other"].append(f"{type(e).__name__}: {e}")


threads = [threading.Thread(target=try_claim, args=(i,)) for i in range(20)]
for t in threads:
    t.start()
for t in threads:
    t.join()

check("бронь досталась ровно одному", results["ok"] == 1, f"({results})")
check("остальные девятнадцать получили «занято»", results["taken"] == 19,
      f"({results})")
check("никто не получил неожиданную ошибку", not results["other"],
      f"({results['other'][:3]})")

with cs.get_db() as conn:
    live = conn.execute(
        "SELECT COUNT(*) AS cnt FROM delivery_assignments "
        " WHERE retailcrm_order_id = 9001 AND state IN ('claimed','picked_up')"
    ).fetchone()["cnt"]
check("в базе одна живая бронь", live == 1, f"({live})")


# ============================================================================
print("\n3. Отказ с именем того, кто успел")
# ============================================================================

try:
    ds.claim_order(9001, courier_user_id=777, courier_name="Опоздавший",
                   city="Новосибирск")
    check("повторная бронь отклонена", False, "(прошла)")
except ds.ClaimError as e:
    check("повторная бронь отклонена", e.code == "taken", f"({e.code})")
    check("в тексте есть имя занявшего", "Курьер" in str(e), f"({e})")

winner = None
with cs.get_db() as conn:
    winner = conn.execute(
        "SELECT courier_user_id FROM delivery_assignments "
        " WHERE retailcrm_order_id = 9001 AND state = 'claimed'").fetchone()[0]

try:
    ds.claim_order(9001, courier_user_id=winner, courier_name="Он же",
                   city="Новосибирск")
    check("свой же заказ второй раз не бронируется", False, "(прошла)")
except ds.ClaimError as e:
    check("свой же заказ второй раз не бронируется", e.code == "already_mine",
          f"({e.code})")


# ============================================================================
print("\n4. Лимит на город и горизонт бронирования")
# ============================================================================

with cs.get_db() as conn:
    conn.execute(
        "INSERT OR REPLACE INTO courier_city_settings (city, max_active_claims, "
        "claim_horizon_days) VALUES ('Новосибирск', 2, 1)")

for order_id in (9002, 9003, 9004):
    add_order(order_id)

taken_ok = 0
limit_hit = None
for order_id in (9002, 9003, 9004):
    try:
        ds.claim_order(order_id, courier_user_id=500, courier_name="Лимитный",
                       city="Новосибирск")
        taken_ok += 1
    except ds.ClaimError as e:
        limit_hit = e

check("лимит города соблюдён", taken_ok == 2, f"(взято {taken_ok}, ждём 2)")
check("третья бронь отклонена по лимиту",
      limit_hit is not None and limit_hit.code == "limit",
      f"({limit_hit and limit_hit.code})")

add_order(9010, days=5)
try:
    ds.claim_order(9010, courier_user_id=600, courier_name="Дальний",
                   city="Новосибирск")
    check("нельзя забить неделю вперёд", False, "(бронь прошла)")
except ds.ClaimError as e:
    check("нельзя забить неделю вперёд", e.code == "horizon", f"({e.code})")

add_order(9011, days=1)
try:
    ds.claim_order(9011, courier_user_id=600, courier_name="Завтрашний",
                   city="Новосибирск")
    check("завтрашний заказ бронируется", True)
except ds.ClaimError as e:
    check("завтрашний заказ бронируется", False, f"({e.code}: {e})")


# ============================================================================
print("\n5. Чужой город и чужая бронь")
# ============================================================================

add_order(9020, city="Екатеринбург", site="site-b")
try:
    ds.claim_order(9020, courier_user_id=700, courier_name="Не свой",
                   city="Новосибирск")
    check("заказ чужого города не бронируется", False, "(прошла)")
except ds.ClaimError as e:
    check("заказ чужого города не бронируется", e.code == "forbidden", f"({e.code})")

try:
    ds.release_order(9001, courier_user_id=999)
    check("чужую бронь курьер снять не может", False, "(сняли)")
except ds.ClaimError as e:
    check("чужую бронь курьер снять не может", e.code == "forbidden", f"({e.code})")

released = ds.release_order(9001, courier_user_id=0, reason=ds.RELEASE_ADMIN,
                            allow_any_courier=True)
check("управляющий чужую бронь снимает", released["state"] == "released",
      f"({released})")

with cs.get_db() as conn:
    row = conn.execute(
        "SELECT state, release_reason FROM delivery_assignments "
        " WHERE retailcrm_order_id = 9001 ORDER BY id DESC LIMIT 1").fetchone()
check("запись не удалена, а помечена причиной",
      row["state"] == "released" and row["release_reason"] == "admin",
      f"({dict(row)})")

# Снятая бронь освобождает заказ — ради этого индекс и частичный
ds.claim_order(9001, courier_user_id=800, courier_name="Следующий",
               city="Новосибирск")
check("после снятия заказ можно взять снова", True)


# ============================================================================
print("\n6. Автоснятие только из состояния claimed")
# ============================================================================
# Курьер может стоять в салоне и жать «Забрал» ровно в эту секунду. Отобрать
# у него заказ с букетом в руках нельзя — находка К3 критики плана.

past = (datetime.utcnow() - timedelta(hours=2)).isoformat(sep=" ", timespec="seconds")
with cs.get_db() as conn:
    conn.execute("UPDATE delivery_assignments SET expires_at = ? "
                 " WHERE retailcrm_order_id = 9001 AND state = 'claimed'", (past,))
    conn.execute("UPDATE delivery_assignments SET state = 'picked_up', expires_at = ? "
                 " WHERE retailcrm_order_id = 9002", (past,))

dropped = ds.expire_stale_claims()
# Снятые записи возвращаются списком: по ним уходят пуши «бронь снята»,
# и знать, КОГО сняли, надо не меньше, чем сколько
check("просроченная бронь снята", len(dropped) >= 1, f"({dropped})")
check("известно, кому уходит уведомление",
      all(row.get("courier_user_id") for row in dropped), f"({dropped})")

with cs.get_db() as conn:
    picked = conn.execute(
        "SELECT state FROM delivery_assignments WHERE retailcrm_order_id = 9002"
    ).fetchone()["state"]
    expired = conn.execute(
        "SELECT state, release_reason FROM delivery_assignments "
        " WHERE retailcrm_order_id = 9001 ORDER BY id DESC LIMIT 1").fetchone()
check("забранный заказ автоснятие не трогает", picked == "picked_up", f"({picked})")
check("снятая по таймеру помечена expired",
      expired["release_reason"] == "expired", f"({dict(expired)})")


# ============================================================================
print("\n7. Заказ отменили или отдали службе доставки")
# ============================================================================

# Отмена определяется по ГРУППЕ статуса из справочника, а не по тому, что
# статуса нет среди видимых: живой путь заказа проходит через «Вызван
# курьер», которого в видимых нет, и по правилу «не виден — значит пропал»
# бронь слетала бы у большинства заказов.
with cs.get_db() as conn:
    conn.execute("INSERT OR REPLACE INTO order_statuses (code, name, group_code) "
                 "VALUES ('cancel-other', 'Отменён', 'cancel')")
    conn.execute("INSERT OR REPLACE INTO order_statuses (code, name, group_code) "
                 "VALUES ('call-courier', 'Вызван курьер', 'assembling')")

add_order(9030)
ds.claim_order(9030, courier_user_id=900, courier_name="Отменённый",
               city="Новосибирск")
with cs.get_db() as conn:
    conn.execute("UPDATE courier_orders SET status = 'cancel-other' "
                 " WHERE retailcrm_order_id = 9030")

# Заказ, который оператор просто двинул вперёд, бронь терять НЕ должен
add_order(9032)
ds.claim_order(9032, courier_user_id=902, courier_name="Едущий",
               city="Новосибирск")
with cs.get_db() as conn:
    conn.execute("UPDATE courier_orders SET status = 'call-courier' "
                 " WHERE retailcrm_order_id = 9032")

add_order(9031)
ds.claim_order(9031, courier_user_id=901, courier_name="Аутсорсный",
               city="Новосибирск")
with cs.get_db() as conn:
    conn.execute("UPDATE courier_orders SET delivery_code = 'ya-dostavka' "
                 " WHERE retailcrm_order_id = 9031")

swept = ds.release_orphan_claims(["dostavka-kurerom"])
with cs.get_db() as conn:
    gone = conn.execute(
        "SELECT state, release_reason FROM delivery_assignments "
        " WHERE retailcrm_order_id = 9030 ORDER BY id DESC LIMIT 1").fetchone()
    outsourced = conn.execute(
        "SELECT state, release_reason FROM delivery_assignments "
        " WHERE retailcrm_order_id = 9031 ORDER BY id DESC LIMIT 1").fetchone()

check("бронь отменённого заказа снята",
      gone["release_reason"] == "order_gone", f"({dict(gone)}, {swept})")
check("бронь переданного аутсорсу снята с отдельной причиной",
      outsourced["release_reason"] == "outsourced", f"({dict(outsourced)}, {swept})")

with cs.get_db() as conn:
    moved = conn.execute(
        "SELECT state FROM delivery_assignments "
        " WHERE retailcrm_order_id = 9032 ORDER BY id DESC LIMIT 1").fetchone()["state"]
check("статус двинули вперёд — бронь осталась", moved == "claimed", f"({moved})")

# И заказ обязан остаться видимым своему курьеру: иначе он пропадает из
# «Моих» ровно у того, кто его везёт
own = ds.list_orders_for_courier(
    city="Новосибирск", date_from="2000-01-01", date_to="2099-01-01",
    courier_user_id=902, courier_delivery_codes=["dostavka-kurerom"])
check("свой заказ виден и вне видимых статусов",
      any(o["retailcrm_order_id"] == 9032 and o["is_mine"] for o in own),
      f"({[o['retailcrm_order_id'] for o in own]})")


# ============================================================================
print("\n8. HTTP: коды ответов, а не 500")
# ============================================================================

from werkzeug.security import generate_password_hash  # noqa: E402


def make_user(username, role, sections):
    conn = auth.get_db()
    try:
        conn.execute(
            "INSERT INTO users (username, full_name, password_hash, role, is_active, created_at) "
            "VALUES (?, ?, ?, ?, 1, datetime('now'))",
            (username, username, generate_password_hash("Parol12345"), role),
        )
        for section in sections:
            conn.execute(
                "INSERT INTO permissions (username, module_name, can_view) VALUES (?, ?, 1)",
                (username, section))
        conn.commit()
    finally:
        conn.close()
    return conn


make_user("kurier1", "courier", ["courier_app"])
make_user("kurier2", "courier", ["courier_app"])

with auth.get_db() as conn:
    ids = {row["username"]: row["id"] for row in conn.execute(
        "SELECT id, username FROM users WHERE username IN ('kurier1','kurier2')")}
for username in ("kurier1", "kurier2"):
    ds.save_courier_profile(user_id=ids[username], username=username,
                            city="Новосибирск", retailcrm_courier_id=None,
                            active=True, updated_by="test")

# Значение берём из auth, а не переписываем строкой: разойдётся — и сторож
# начнёт проверять, что ручка отвечает 403 на всё подряд, а не свою логику
AJAX = {auth.AJAX_HEADER: auth.AJAX_HEADER_VALUE}


def login(username):
    client = app.test_client()
    response = client.post("/api/auth/login",
                           json={"username": username, "password": "Parol12345"})
    assert response.status_code == 200, response.data
    return client


c1, c2 = login("kurier1"), login("kurier2")

add_order(9040)
r1 = c1.post("/api/courier/orders/9040/claim", headers=AJAX)
r2 = c2.post("/api/courier/orders/9040/claim", headers=AJAX)
check("первый курьер получает 200", r1.status_code == 200, f"({r1.status_code})")
check("второй получает 409, а не 500", r2.status_code == 409,
      f"({r2.status_code}: {r2.get_data(as_text=True)[:120]})")
check("в ответе есть код причины",
      (r2.get_json() or {}).get("code") == "taken", f"({r2.get_json()})")

add_order(9041, city="Екатеринбург", site="site-b")
r3 = c1.post("/api/courier/orders/9041/claim", headers=AJAX)
check("чужой город — 403", r3.status_code == 403, f"({r3.status_code})")

add_order(9042, days=6)
r4 = c1.post("/api/courier/orders/9042/claim", headers=AJAX)
check("слишком дальний день — 400", r4.status_code == 400, f"({r4.status_code})")

r5 = c2.post("/api/courier/orders/9040/release", headers=AJAX)
check("чужую бронь через API не снять", r5.status_code == 403, f"({r5.status_code})")

r6 = c1.post("/api/courier/orders/9040/release", headers=AJAX)
check("свою бронь курьер снимает", r6.status_code == 200, f"({r6.status_code})")

r7 = c1.post("/api/courier/orders/9040/claim")
check("без ajax-заголовка запись не проходит", r7.status_code == 403,
      f"({r7.status_code})")

r8 = c1.get("/api/courier/assignments")
check("курьер не видит разбор броней управляющего", r8.status_code == 403,
      f"({r8.status_code})")


# ============================================================================
print()
if failures:
    print(f"=== ПРОВАЛОВ: {len(failures)} ===")
    for name in failures:
        print(f"  - {name}")
    sys.exit(1)

print("=== Бронь держится ===")
sys.exit(0)
