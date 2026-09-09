"""
Сторож раздела «Загрузка салонов» (Фазы 4–5).

Проверяет то, что ломается молча:
  - «ёмкость не задана» ≠ «ноль»: процент не считается, а не показывает ∞%;
  - «салон закрыт» ≠ «загрузка 0%» — это разные состояния ячейки;
  - исключение на дату важнее недельного графика;
  - заказы без часа готовности и без склада не подмешиваются в сетку, а идут
    отдельными строками;
  - самовывоз считается вторым счётчиком, но из общего веса не выпадает;
  - права режутся на бэкенде: менеджер видит только свои салоны, чужой
    store_id даёт 403, а учётка БЕЗ салонов получает 200 и пустую сетку,
    а не 500 (ветка «свои салоны» при пустом списке не исполняется).

ВАЖНО: прогон читает боевой .env, поэтому сеть глушится до импорта приложения.

Запуск: python scripts/test_salon_load.py
"""

import os
import socket
import sqlite3
import ssl  # noqa: F401  — импортировать до патча сокета
import sys
import tempfile
from datetime import date, timedelta

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))
os.chdir(REPO)

for var in ("PYRUS_SYNC_SCHEDULER", "MOYSKLAD_SYNC_SCHEDULER", "COURIERS_SYNC_SCHEDULER",
            "LINKWATCH_SCHEDULER", "INVOICES_CARD_SYNC_SCHEDULER", "INVOICES_BANKS_SCHEDULER"):
    os.environ[var] = "0"


class NetworkBlocked(Exception):
    pass


def _blocked(*args, **kwargs):
    raise NetworkBlocked("прогон не должен ходить в боевые внешние API")


socket.socket.connect = _blocked

TMP = tempfile.mkdtemp(prefix="salon_load_")
os.environ["COURIERS_DB_PATH"] = os.path.join(TMP, "couriers.db")
os.environ["BARHAT_DB_PATH"] = os.path.join(TMP, "barhat.db")

from couriers import retailcrm, storage as couriers_storage  # noqa: E402
from salonkpi import storage as salonkpi_storage  # noqa: E402
from salonload import metrics, storage  # noqa: E402

assert storage.DB_PATH.endswith(os.path.join(TMP, "barhat.db")) or storage.DB_PATH == os.path.join(
    TMP, "barhat.db"), f"тест пишет не в свою базу: {storage.DB_PATH}"

# Даты прогона считаются от сегодняшней, а не задаются константами.
#
# Раньше здесь стояло «2026-09-10 (четверг)», и 09.09.2026 прогон развалился:
# test_alerts пишет заказы на ЗАВТРА, завтра совпало с этой датой, и окно
# витрины затёрлось вместе с данными setup_data. Проверки при этом падали в
# совсем другом месте — в разделе «нераспределённые», — и выглядело это как
# сломанный код, а не как календарь.
#
# Все вспомогательные дни отсчитываются от DAY, чтобы ни один тест не мог
# наехать окном на чужие данные ни сегодня, ни в любой другой день.
_TODAY = date.today()
DAY = (_TODAY + timedelta(days=30)).isoformat()
WEEKDAY = (_TODAY + timedelta(days=30)).weekday()
DAY_SLOT_MOVED = (_TODAY + timedelta(days=45)).isoformat()
DAY_REVIEW_A = (_TODAY + timedelta(days=51)).isoformat()
DAY_REVIEW_B = (_TODAY + timedelta(days=52)).isoformat()
DAY_MINUTES = (_TODAY + timedelta(days=66)).isoformat()
STORE_ID = 1
STORE_KEY = "test-salon"
OTHER_STORE_ID = 2

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"  OK   {name}")
    else:
        print(f"  FAIL {name}{': ' + detail if detail else ''}")
        failures.append(name)


def order(order_id, hour=None, store=STORE_KEY, pickup=False, items=None, status="at-work"):
    payload = {
        "id": order_id,
        "number": str(order_id),
        "site": STORE_KEY,
        "status": status,
        "summ": 5000,
        "shipmentStore": store,
        "delivery": {"date": DAY, "code": "self-delivery" if pickup else "dostavka-kurerom"},
        "customFields": {},
        "items": items or [{"quantity": 1, "offer": {"id": 1, "displayName": "Букет"}}],
    }
    if hour is not None:
        payload["customFields"]["order_availability_time"] = f"{hour:02d}:00"
    else:
        payload["customFields"]["order_availability_time"] = "уточ"
    return payload


def setup_data():
    """Своя база: два салона, справочник статусов, заказы на один день."""
    couriers_storage.init_couriers_tables()

    conn = sqlite3.connect(os.environ["BARHAT_DB_PATH"])
    conn.execute("""CREATE TABLE IF NOT EXISTS stores (
        id INTEGER PRIMARY KEY, name TEXT NOT NULL, is_active INTEGER NOT NULL DEFAULT 1)""")
    conn.execute("INSERT OR REPLACE INTO stores (id, name, is_active) VALUES (?, ?, 1)",
                 (STORE_ID, "НСК Восход, 3"))
    conn.execute("INSERT OR REPLACE INTO stores (id, name, is_active) VALUES (?, ?, 1)",
                 (OTHER_STORE_ID, "Томск Дальне-Ключевская, 16а"))
    conn.commit()
    conn.close()

    salonkpi_storage.init_salonkpi_tables()
    salonkpi_storage.set_link(salonkpi_storage.SOURCE_CRM_STORE, STORE_KEY, STORE_ID)
    salonkpi_storage.set_link(salonkpi_storage.SOURCE_CRM_STORE, "tomsk-key", OTHER_STORE_ID)
    storage.init_salonload_tables()

    couriers_storage.upsert_order_statuses([
        {"code": "at-work", "name": "В работе", "group_code": "new", "active": True},
        {"code": "cancel-other", "name": "Отменен", "group_code": "cancel", "active": True},
    ])

    rows = [
        retailcrm.parse_order(order(1, hour=10), {}),                      # 1 ед.
        retailcrm.parse_order(order(2, hour=10, pickup=True), {}),         # 1 ед., самовывоз
        retailcrm.parse_order(order(3, hour=14), {}),
        retailcrm.parse_order(order(4, hour=None), {}),                    # без времени
        retailcrm.parse_order(order(5, hour=11, store=None), {}),          # без склада
        retailcrm.parse_order(order(6, hour=11, store="chuzhoi-sklad"), {}),  # непривязанный
        retailcrm.parse_order(order(7, hour=10, status="cancel-other"), {}),  # отменён
    ]
    couriers_storage.replace_orders_window(DAY, DAY, rows)


def test_capacity_states():
    print("\n1. Три состояния ячейки")
    grid = metrics.day_grid(DAY, [STORE_ID])
    cells = {c["hour"]: c for c in grid["stores"][0]["cells"]}

    check("без заданной ёмкости процент не считается", cells[10]["percent"] is None,
          f"получено {cells[10]['percent']}")
    check("состояние ячейки — «неизвестно», а не «в норме»", cells[10]["level"] == "unknown",
          f"получено {cells[10]['level']}")
    check("нагрузка при этом посчитана", cells[10]["units"] == 2.0, f"получено {cells[10]['units']}")

    storage.apply_working_hours(STORE_ID, 9, 20, capacity=4.0, username="tester")
    grid = metrics.day_grid(DAY, [STORE_ID])
    cells = {c["hour"]: c for c in grid["stores"][0]["cells"]}

    check("после задания ёмкости процент считается (2 из 4)", cells[10]["percent"] == 50.0,
          f"получено {cells[10]['percent']}")
    check("час вне графика помечен закрытым", cells[3]["closed"] is True, f"получено {cells[3]}")
    check("закрытый час — отдельное состояние, а не 0%", cells[3]["level"] == "closed",
          f"получено {cells[3]['level']}")
    check("у закрытого часа ёмкости нет", cells[3]["capacity"] is None, f"получено {cells[3]}")


def test_exception_wins():
    print("\n2. Исключение на дату важнее графика")
    storage.set_exception(STORE_ID, DAY, 10, capacity=1.0, reason="ремонт", username="tester")
    grid = metrics.day_grid(DAY, [STORE_ID])
    cell = {c["hour"]: c for c in grid["stores"][0]["cells"]}[10]

    check("ёмкость берётся из исключения", cell["capacity"] == 1.0, f"получено {cell['capacity']}")
    check("перегруз посчитан (2 из 1)", cell["percent"] == 200.0, f"получено {cell['percent']}")
    check("уровень «перегруз»", cell["level"] == "over", f"получено {cell['level']}")
    check("причина исключения видна", cell["reason"] == "ремонт", f"получено {cell['reason']}")
    check("источник ёмкости различим", cell["capacity_source"] == "exception",
          f"получено {cell['capacity_source']}")

    storage.set_exception(STORE_ID, DAY, 10, capacity=None, username="tester")
    cell = {c["hour"]: c for c in metrics.day_grid(DAY, [STORE_ID])["stores"][0]["cells"]}[10]
    check("снятие исключения возвращает обычный график", cell["capacity"] == 4.0,
          f"получено {cell['capacity']}")


def test_separate_rows():
    print("\n3. Заказы, которые нельзя молча разложить по сетке")
    grid = metrics.day_grid(DAY, None)
    store = next(s for s in grid["stores"] if s["store_id"] == STORE_ID)

    check("заказ без времени — отдельной строкой", store["no_time"]["orders"] == 1,
          f"получено {store['no_time']}")
    check("он же виден как «требует уточнения»", store["no_time"]["unparsed"] == 1,
          f"получено {store['no_time']}")
    check("заказы без склада и с чужим складом — в «нераспределённых»",
          grid["unassigned"]["orders"] == 2, f"получено {grid['unassigned']}")

    cells = {c["hour"]: c for c in store["cells"]}
    check("отменённый заказ в нагрузку не попал", cells[10]["orders"] == 2,
          f"получено {cells[10]['orders']}")
    check("заказ без времени не подмешан в час 0", cells[0]["orders"] == 0,
          f"получено {cells[0]['orders']}")


def test_pickup_counter():
    print("\n4. Самовывоз — второй счётчик, но из веса не выпадает")
    cells = {c["hour"]: c for c in metrics.day_grid(DAY, [STORE_ID])["stores"][0]["cells"]}
    check("самовывоз посчитан отдельно", cells[10]["pickup_orders"] == 1,
          f"получено {cells[10]['pickup_orders']}")
    check("и при этом входит в общий вес", cells[10]["units"] == 2.0,
          f"получено {cells[10]['units']}")


def test_free_slots():
    print("\n5. Свободные слоты")
    free = metrics.free_slots(STORE_ID, DAY, days=1, need_units=1.0)
    hours = {slot["hour"] for slot in free["slots"]}
    check("час 10 (2 из 4) остаётся свободным", 10 in hours, f"получено {sorted(hours)}")
    check("закрытые часы не предлагаются", 3 not in hours, f"получено {sorted(hours)}")

    free = metrics.free_slots(STORE_ID, DAY, days=1, need_units=4.0)
    hours = {slot["hour"] for slot in free["slots"]}
    check("при большом заказе занятый час не предлагается", 10 not in hours,
          f"получено {sorted(hours)}")


def test_week():
    print("\n6. Календарь недели")
    week = metrics.week_grid(DAY, days=3, store_ids=[STORE_ID])
    day_cell = next(d for d in week["stores"][0]["days"] if d["date"] == DAY)
    check("дневная нагрузка суммируется", day_cell["units"] == 4.0, f"получено {day_cell['units']}")
    check("дневная ёмкость — сумма часов графика (11 × 4)", day_cell["capacity"] == 44.0,
          f"получено {day_cell['capacity']}")


TEST_PASSWORD = "test-salon-load-2026"


def ensure_user(username, role, store_ids):
    """Завести тестовую учётку с нужными салонами (идемпотентно)."""
    from werkzeug.security import generate_password_hash

    conn = sqlite3.connect(os.environ["BARHAT_DB_PATH"])
    try:
        conn.execute(
            "INSERT OR IGNORE INTO users (username, full_name, password_hash, role, is_active, created_at) "
            "VALUES (?, ?, ?, ?, 1, datetime('now'))",
            (username, username, generate_password_hash(TEST_PASSWORD), role),
        )
        conn.execute(
            "UPDATE users SET role = ?, is_active = 1, password_hash = ? WHERE username = ?",
            (role, generate_password_hash(TEST_PASSWORD), username),
        )
        conn.execute("DELETE FROM user_stores WHERE username = ?", (username,))
        for store_id in store_ids:
            conn.execute("INSERT INTO user_stores (username, store_id) VALUES (?, ?)",
                         (username, store_id))
        conn.execute(
            "INSERT OR IGNORE INTO permissions (username, module_name, can_view) "
            "VALUES (?, 'salon_load', 1)",
            (username,),
        )
        conn.commit()
    finally:
        conn.close()


def login_as(client, username):
    """Войти штатной ручкой: сессию flask_login руками не подделать."""
    response = client.post("/api/auth/login",
                           json={"username": username, "password": TEST_PASSWORD})
    assert response.status_code == 200, f"вход {username} не удался: {response.data[:200]}"
    return client


def test_permissions():
    print("\n7. Права: сетку режет бэкенд")
    grid = metrics.day_grid(DAY, [])
    check("сетка для учётки без салонов пустая, а не с чужими салонами",
          grid["stores"] == [] and grid["no_stores"] is True, f"получено {grid['no_stores']}")

    grid = metrics.day_grid(DAY, [STORE_ID])
    check("менеджер видит только свой салон",
          [s["store_id"] for s in grid["stores"]] == [STORE_ID],
          f"получено {[s['store_id'] for s in grid['stores']]}")


def test_http_access():
    print("\n8. HTTP: чужой салон даёт 403")
    from pyrus.server import app

    ensure_user("test-load-manager", "manager", [STORE_ID])
    ensure_user("test-load-nostores", "manager", [])

    with app.test_client() as client:
        login_as(client, "test-load-manager")

        response = client.get(f"/api/salon-load/day?date={DAY}")
        payload = response.get_json() or {}
        store_ids = [s["store_id"] for s in payload.get("data", {}).get("stores", [])]
        check("менеджер получает сетку только своего салона",
              response.status_code == 200 and store_ids == [STORE_ID],
              f"получено {response.status_code}, {store_ids}")
        check("менеджеру не отдаётся право правки",
              payload.get("data", {}).get("can_edit") is False)

        response = client.get(f"/api/salon-load/slot?date={DAY}&store_id={OTHER_STORE_ID}&hour=10")
        check("чужой store_id в /slot → 403", response.status_code == 403,
              f"получено {response.status_code}")

        response = client.get(f"/api/salon-load/slot?date={DAY}&store_id={STORE_ID}&hour=10")
        check("свой store_id → 200", response.status_code == 200,
              f"получено {response.status_code} {response.get_data(as_text=True)[:200]}")

        response = client.get(f"/api/salon-load/free-slots?store_id={OTHER_STORE_ID}")
        check("free-slots тоже проверяет салон", response.status_code == 403,
              f"получено {response.status_code}")

        response = client.get(f"/api/salon-load/capacity?store_id={OTHER_STORE_ID}")
        check("сетка ёмкости чужого салона недоступна", response.status_code == 403,
              f"получено {response.status_code}")

        response = client.post("/api/salon-load/capacity",
                               json={"store_id": STORE_ID,
                                     "slots": [{"weekday": 0, "hour": 10, "capacity": 9}]},
                               headers={"X-Requested-With": "XMLHttpRequest"})
        check("менеджер не может править ёмкость", response.status_code == 403,
              f"получено {response.status_code}")

    with app.test_client() as client:
        login_as(client, "test-load-nostores")
        response = client.get(f"/api/salon-load/day?date={DAY}")
        payload = response.get_json() or {}
        check("учётка без салонов: 200 и признак no_stores, а не 500",
              response.status_code == 200 and payload.get("data", {}).get("no_stores") is True,
              f"получено {response.status_code}, {payload.get('data', {}).get('no_stores')}")


def test_ui_contract():
    """
    Каждая ручка, которую дёргает salon-load.js, отвечает и отдаёт те поля,
    которые экран читает. Проверка руками не заменяется node --check: тот
    видит синтаксис, но не видит, что ручки нет или что поле называется иначе.
    """
    print("\n9. Контракт с экраном")
    from pyrus.server import app

    ensure_user("test-load-admin", "admin", [])

    with app.test_client() as client:
        login_as(client, "test-load-admin")
        # Заголовок AJAX нужен всем записям, а они идут вперемешку с чтениями.
        headers = {"X-Requested-With": "barhat-dashboard"}

        response = client.get(f"/api/salon-load/day?date={DAY}")
        payload = (response.get_json() or {}).get("data", {})
        check("/day отвечает", response.status_code == 200, f"получено {response.status_code}")
        for field in ("hours", "stores", "thresholds", "freshness", "can_edit"):
            check(f"/day отдаёт {field}", field in payload, f"есть: {sorted(payload)}")
        if payload.get("stores"):
            cell = payload["stores"][0]["cells"][10]
            for field in ("hour", "units", "capacity", "percent", "level", "closed", "orders"):
                check(f"ячейка отдаёт {field}", field in cell, f"есть: {sorted(cell)}")

        response = client.get(f"/api/salon-load/week?from={DAY}&days=7")
        check("/week отвечает", response.status_code == 200, f"получено {response.status_code}")

        response = client.get("/api/salon-load/stores")
        payload = response.get_json() or {}
        check("/stores отвечает и знает про заданную ёмкость",
              response.status_code == 200 and
              any(s.get("has_capacity") for s in payload.get("stores", [])),
              f"получено {response.status_code}, {payload.get('stores')}")

        response = client.get("/api/couriers/weights")
        payload = response.get_json() or {}
        check("справочник надбавок отвечает", response.status_code == 200,
              f"получено {response.status_code}")
        check("в мета есть разбор нагрузки",
              "coverage" in (payload.get("meta") or {}), f"получено {payload.get('meta')}")
        for field in ("basis", "per_order", "weight", "orders"):
            check(f"строка справочника отдаёт {field}",
                  all(field in row for row in payload.get("data", [])),
                  f"есть: {sorted((payload.get('data') or [{}])[0])}")

        # Нормы времени (Ф2). Групповых норм больше нет — только товары.
        check("ручки групповых норм больше нет",
              client.get("/api/couriers/time-norms/groups").status_code == 404,
              "ручка отвечает, хотя групповые нормы отменены")

        response = client.get("/api/couriers/time-norms/offers")
        payload = response.get_json() or {}
        for field in ("roles", "bases", "berry_modes", "catalog"):
            check(f"мета товаров отдаёт {field}", field in (payload.get("meta") or {}),
                  f"есть: {sorted((payload.get('meta') or {}))}")

        # Фильтры: экран шлёт их все, и ни один не должен ронять ручку
        response = client.get("/api/couriers/time-norms/offers?q=роза&role=catalog&unit=pc"
                              "&in_catalog=1&min_orders=1&max_orders=99"
                              "&min_median=0&max_median=100")
        check("список товаров принимает все фильтры", response.status_code == 200,
              f"получено {response.status_code} {response.get_data(as_text=True)[:160]}")

        # Выгрузка считается по окну «последние 60 дней», а заказы прогона лежат
        # в будущем — без заказа во вчерашнем дне файл вышел бы с одним
        # заголовком, и это ровно то, на что жаловался владелец.
        yesterday = (_TODAY - timedelta(days=1)).isoformat()
        couriers_storage.replace_orders_window(yesterday, yesterday, [
            retailcrm.parse_order(dict(order(900, hour=12),
                                       delivery={"date": yesterday, "code": "dostavka-kurerom"}), {}),
        ])
        client.post("/api/couriers/time-norms", headers=headers,
                    json={"scope": "offer", "scope_id": 1, "role": "catalog",
                          "minutes": 12, "basis": "unit"})

        response = client.get("/api/couriers/time-norms/export")
        check("выгрузка отдаёт файл", response.status_code == 200,
              f"получено {response.status_code}")
        check("выгрузка приходит как CSV-вложение",
              "attachment" in response.headers.get("Content-Disposition", "") and
              "csv" in response.headers.get("Content-Type", ""),
              f"получено {dict(response.headers)}")
        text = response.get_data(as_text=True)
        lines = text.split("\r\n")
        check("в файле есть BOM — иначе Excel ломает кириллицу",
              text.startswith("﻿"), f"получено {text[:20]!r}")
        check("в заголовке есть все правимые колонки",
              all(col in lines[0] for col in
                  ("offer_id", "Артикул", "Роль", "Минут", "За что", "Клубника")),
              f"получено {lines[0][:150]}")
        check("в файле есть строки товаров, а не только заголовок",
              len([line for line in lines if line.strip()]) > 1,
              f"строк: {len([l for l in lines if l.strip()])} — выгрузка пуста")
        check("роль пишется словами, а не кодом",
              "готовый товар" in text and "catalog" not in text,
              f"получено {text[:300]}")

        response = client.get("/api/couriers/time-norms/offers?only_missing=1")
        payload = response.get_json() or {}
        check("товары с нормами отвечают", response.status_code == 200,
              f"получено {response.status_code}")
        check("в мета есть покрытие разметки",
              "coverage" in (payload.get("meta") or {}), f"получено {payload.get('meta')}")
        for field in ("offer_id", "orders", "median_quantity", "unit_code", "norm", "in_catalog"):
            check(f"строка товара отдаёт {field}",
                  all(field in row for row in payload.get("data", [])),
                  f"есть: {sorted((payload.get('data') or [{}])[0])}")

        response = client.get("/api/couriers/order-statuses")
        payload = response.get_json() or {}
        check("справочник статусов отвечает", response.status_code == 200,
              f"получено {response.status_code}")
        check("у статуса есть признак нагрузки",
              all("counts_as_load" in s for s in payload.get("data", [])),
              f"получено {payload.get('data')}")

        # Запись: те же тела запроса, что шлёт экран
        response = client.post("/api/salon-load/capacity/working-hours", headers=headers,
                               json={"store_id": STORE_ID, "open_hour": 9, "close_hour": 21,
                                     "capacity": 6, "pickup_capacity": None})
        check("часы работы сохраняются", response.status_code == 200,
              f"получено {response.status_code} {response.get_data(as_text=True)[:160]}")

        response = client.post("/api/salon-load/exceptions", headers=headers,
                               json={"store_id": STORE_ID, "date": DAY, "hour": None,
                                     "capacity": 12, "closed": False, "reason": "8 марта"})
        check("исключение сохраняется", response.status_code == 200,
              f"получено {response.status_code} {response.get_data(as_text=True)[:160]}")

        # Тело запроса — то же, что шлёт экран: надбавка вместе с базой начисления.
        response = client.post("/api/couriers/weights", headers=headers,
                               json={"weights": {"1": {"weight": 3.5, "basis": "line"}}})
        check("надбавка с базой начисления сохраняется", response.status_code == 200,
              f"получено {response.status_code} {response.get_data(as_text=True)[:160]}")

        response = client.post("/api/couriers/weights", headers=headers,
                               json={"weights": {"1": 0}})
        check("нулевая надбавка отклоняется ручкой", response.status_code == 400,
              f"получено {response.status_code}")

        response = client.post("/api/couriers/weights", headers=headers,
                               json={"weights": {"1": {"weight": 2, "basis": "кг"}}})
        check("неизвестная база начисления отклоняется ручкой", response.status_code == 400,
              f"получено {response.status_code}")

        # Нормы времени: запись (Ф2)
        response = client.post("/api/couriers/time-norms", headers=headers,
                               json={"scope": "group", "scope_id": 1,
                                     "role": "catalog", "minutes": 12, "basis": "unit"})
        check("норма группы сохраняется", response.status_code == 200,
              f"получено {response.status_code} {response.get_data(as_text=True)[:160]}")

        response = client.post("/api/couriers/time-norms", headers=headers,
                               json={"scope": "group", "scope_id": 1,
                                     "role": "catalog", "minutes": None})
        check("готовый товар без времени отклоняется ручкой", response.status_code == 400,
              f"получено {response.status_code}")

        response = client.post("/api/couriers/time-norms", headers=headers,
                               json={"scope": "выдумка", "scope_id": 1, "role": "catalog"})
        check("неизвестная область нормы отклоняется ручкой", response.status_code == 400,
              f"получено {response.status_code}")

        response = client.post("/api/couriers/time-norms",
                               json={"scope": "group", "scope_id": 1, "role": None})
        check("норма без заголовка AJAX отклоняется", response.status_code == 403,
              f"получено {response.status_code}")

        # Импорт: то же тело, что шлёт разбор файла в браузере. Роль подписью —
        # именно так она выглядит в выгруженном файле, и именно так её впишет
        # человек.
        response = client.post("/api/couriers/time-norms/import", headers=headers,
                               json={"rows": [{"offer_id": 1, "role": "готовый товар",
                                               "minutes": "12,5", "basis": "за штуку"}]})
        check("роль подписью из файла принимается",
              response.status_code == 200 and
              (response.get_json() or {}).get("data", {}).get("applied") == 1,
              f"получено {response.status_code} {response.get_data(as_text=True)[:160]}")

        response = client.post("/api/couriers/time-norms/import", headers=headers,
                               json={"rows": [{"offer_id": 1, "role": "catalog", "minutes": "12,5"}]})
        payload = (response.get_json() or {}).get("data", {})
        check("импорт норм принимается", response.status_code == 200,
              f"получено {response.status_code} {response.get_data(as_text=True)[:160]}")
        check("импорт отчитывается о применённых строках", payload.get("applied") == 1,
              f"получено {payload}")

        response = client.post("/api/couriers/time-norms/import", headers=headers,
                               json={"rows": []})
        check("пустой импорт отклоняется", response.status_code == 400,
              f"получено {response.status_code}")

        response = client.post("/api/couriers/time-norms/import",
                               json={"rows": [{"offer_id": 1, "role": "none"}]})
        check("импорт без заголовка AJAX отклоняется", response.status_code == 403,
              f"получено {response.status_code}")

        # Ручное обновление каталога: без него после деплоя размечать нечего
        # до ночного прогона, а консоли у контейнера нет.
        response = client.post("/api/couriers/catalog/sync", headers=headers, json={})
        check("ручка обновления каталога есть и не 404/403",
              response.status_code in (200, 502, 503),
              f"получено {response.status_code} {response.get_data(as_text=True)[:120]}")

        response = client.post("/api/couriers/catalog/sync", json={})
        check("обновление каталога без заголовка AJAX отклоняется",
              response.status_code == 403, f"получено {response.status_code}")

        # Тарифная сетка (Ф3): читается экраном и правится без деплоя
        response = client.get("/api/couriers/time-norms/tariffs")
        payload = (response.get_json() or {}).get("data", {})
        check("тарифы отвечают", response.status_code == 200, f"получено {response.status_code}")
        check("сетка по цветам отдана", len(payload.get("flowers") or []) == 5,
              f"получено {payload.get('flowers')}")
        check("тарифы по клубнике отданы", set(payload.get("berries") or {}) == {"bouquet", "box"},
              f"получено {payload.get('berries')}")

        response = client.post("/api/couriers/time-norms/tariffs", headers=headers,
                               json={"kind": "berries", "mode": "bouquet",
                                     "minutes_per_100g": 5, "package_minutes": 10})
        check("тариф по клубнике сохраняется", response.status_code == 200,
              f"получено {response.status_code} {response.get_data(as_text=True)[:160]}")

        response = client.post("/api/couriers/time-norms/tariffs", headers=headers,
                               json={"kind": "flowers", "range_from": 3, "range_to": 40,
                                     "mono_minutes": 0.5, "mix_minutes": 0.6,
                                     "ribbon_minutes": 5, "package_minutes": 10})
        check("правка, ломающая сетку, отклоняется ручкой", response.status_code == 400,
              f"получено {response.status_code}")

        # Галочка «Круглосуточно» шлёт именно 0 и 24.
        response = client.post("/api/salon-load/capacity/working-hours", headers=headers,
                               json={"store_id": STORE_ID, "open_hour": 0, "close_hour": 24,
                                     "capacity": 6, "pickup_capacity": None})
        check("круглосуточный режим сохраняется ручкой", response.status_code == 200,
              f"получено {response.status_code} {response.get_data(as_text=True)[:160]}")

        response = client.post("/api/salon-load/capacity/working-hours",
                               json={"store_id": STORE_ID, "open_hour": 9, "close_hour": 21,
                                     "capacity": 6})
        check("запись без заголовка AJAX отклоняется", response.status_code == 403,
              f"получено {response.status_code}")

        # Ёмкость поменялась — сетка обязана это увидеть, а не отдать кэш
        response = client.get(f"/api/salon-load/day?date={DAY}")
        cells = (response.get_json() or {})["data"]["stores"][0]["cells"]
        check("правка ёмкости сразу видна в сетке (кэш сброшен)", cells[10]["capacity"] == 12.0,
              f"получено {cells[10]['capacity']}")


def test_weight_units_model():
    """
    Весовой товар не имеет права раздувать слот.

    Это тот самый баг 2026-09-07: количество в CRM меряется в разных единицах,
    и 600 г клубники считались шестьюстами заказами. Один сборный заказ съедал
    ёмкость всего дня — 6.6% заказов давали 75.5% нагрузки.
    """
    print("\n14. Нагрузка = база за заказ + надбавки")
    day = DAY_MINUTES
    items = [
        {"quantity": 600, "offer": {"id": 42, "displayName": "Клубника"}},
        {"quantity": 7, "offer": {"id": 43, "displayName": "Роза одноголовая"}},
        {"quantity": 1, "offer": {"id": 44, "displayName": "Упаковка"}},
    ]
    couriers_storage.replace_orders_window(day, day, [
        retailcrm.parse_order(dict(order(600, hour=12, items=items),
                                   delivery={"date": day, "code": "dostavka-kurerom"}), {}),
    ])
    cells = {c["hour"]: c for c in metrics.day_grid(day, [STORE_ID])["stores"][0]["cells"]}
    check("600 г клубники — это один заказ, а не 600 единиц",
          cells[12]["units"] == couriers_storage.ORDER_BASE_UNITS,
          f"получено {cells[12]['units']}")
    check("заказ в ячейке посчитан", cells[12]["orders"] == 1, f"получено {cells[12]['orders']}")

    # Надбавка «за 100 г»: клубника всё-таки тяжелее обычного, но в разумную
    # сторону — 600 г дают +1.2 ед., а не +600.
    couriers_storage.set_product_weights(
        {42: {"weight": 0.2, "basis": couriers_storage.WEIGHT_BASIS_G100}}, "tester")
    couriers_storage.recalc_weights_range(day, day)
    cells = {c["hour"]: c for c in metrics.day_grid(day, [STORE_ID])["stores"][0]["cells"]}
    check("надбавка «за 100 г» считается от массы, а не от штук",
          cells[12]["units"] == couriers_storage.ORDER_BASE_UNITS + 1.2,
          f"получено {cells[12]['units']}")

    coverage = couriers_storage.weights_coverage(day, day)
    check("разбор нагрузки различает базу и надбавки",
          coverage["base_units"] == 1.0 and coverage["extra_units"] == 1.2,
          f"получено {coverage}")

    couriers_storage.set_product_weights({42: None})
    couriers_storage.recalc_weights_range(day, day)

    # Разовый пересчёт накопленного: старые числа (Σ количество × вес) обязаны
    # смениться на новые. Проверяется и возобновляемость — курсор идёт кусками
    # по датам, и обрыв на середине не должен начинать всё заново.
    with couriers_storage.get_db() as conn:
        conn.execute("UPDATE courier_orders SET weight_units = 609 "
                     "WHERE retailcrm_order_id = 600")
        conn.execute("DELETE FROM sync_state WHERE key IN (?, ?)",
                     (couriers_storage.WEIGHT_MODEL_KEY,
                      couriers_storage.WEIGHT_MODEL_CURSOR_KEY))
    couriers_storage._backfill_weight_model()
    with couriers_storage.get_db() as conn:
        weight = conn.execute("SELECT weight_units FROM courier_orders "
                              "WHERE retailcrm_order_id = 600").fetchone()["weight_units"]
        done = conn.execute("SELECT value FROM sync_state WHERE key = ?",
                            (couriers_storage.WEIGHT_MODEL_KEY,)).fetchone()
    check("разовый пересчёт переводит накопленное на новую формулу",
          weight == couriers_storage.ORDER_BASE_UNITS, f"получено {weight}")
    check("пересчёт отмечается как выполненный и не повторяется",
          done and done["value"] == couriers_storage.WEIGHT_MODEL_VERSION, f"получено {done}")


def test_round_clock():
    """
    Круглосуточный и ночной режимы. Раньше `open < close` было жёстким
    требованием: 22 → 6 не сохранялось вовсе, и ночная точка оставалась с
    пустой сеткой, а «ближайшие 3 часа» не переходили через полночь.
    """
    print("\n15. Круглосуточный и ночной режим")
    storage.apply_working_hours(OTHER_STORE_ID, 0, 24, capacity=3.0, username="tester")
    grid = storage.weekly_grid(OTHER_STORE_ID)
    check("круглосуточно: закрытых часов нет",
          all(not grid[f"{WEEKDAY}:{h}"]["closed"] for h in range(24)),
          "есть закрытые часы")
    check("круглосуточно: ёмкость задана всем 24 часам",
          all(grid[f"{WEEKDAY}:{h}"]["capacity"] == 3.0 for h in range(24)),
          "не у всех часов есть ёмкость")

    storage.apply_working_hours(OTHER_STORE_ID, 22, 6, capacity=2.0, username="tester")
    grid = storage.weekly_grid(OTHER_STORE_ID)
    working = {h for h in range(24) if not grid[f"{WEEKDAY}:{h}"]["closed"]}
    check("ночной режим 22→6 сохраняется через полночь", working == {22, 23, 0, 1, 2, 3, 4, 5},
          f"получено {sorted(working)}")

    for bad in ((10, 10), (24, 6), (5, 25)):
        try:
            storage.apply_working_hours(OTHER_STORE_ID, bad[0], bad[1], capacity=2.0)
            check(f"часы {bad} отклоняются", False, "исключения не было")
        except ValueError:
            check(f"часы {bad} отклоняются", True)

    # Предупреждение «ближайшие часы» обязано перейти на следующие сутки:
    # в 23:00 ближайший час круглосуточной точки — это 00:00 завтра.
    from datetime import datetime, timedelta
    offset = 7
    storage.set_timezone(OTHER_STORE_ID, offset)
    now_local = datetime.utcnow() + timedelta(hours=offset)
    soon = now_local + timedelta(hours=2)
    soon_day = soon.date().isoformat()
    storage.apply_working_hours(OTHER_STORE_ID, 0, 24, capacity=1.0, username="tester")
    couriers_storage.replace_orders_window(soon_day, soon_day, [
        retailcrm.parse_order(dict(order(700, hour=soon.hour, store="tomsk-key"),
                                   delivery={"date": soon_day, "code": "dostavka-kurerom"}), {}),
        retailcrm.parse_order(dict(order(701, hour=soon.hour, store="tomsk-key"),
                                   delivery={"date": soon_day, "code": "dostavka-kurerom"}), {}),
    ])
    metrics.scan_alerts()
    horizons = {a["horizon"] for a in metrics.alerts([OTHER_STORE_ID])["items"]
                if a["date"] == soon_day and a["hour"] == soon.hour}
    check("перегруз через 2 часа виден как «ближайшие часы» даже за полночь",
          metrics.HORIZON_SOON in horizons,
          f"получено {horizons} (день {soon_day}, час {soon.hour})")


def test_alerts():
    """
    Предупреждения (Фаза 7). Главное здесь — не «оно считается», а:
      - о слоте не напоминают дважды;
      - разгруженный слот закрывается сам;
      - салон без часового пояса пропускается, а не получает сигнал по времени
        сервера (это сдвиг на 5–7 часов, заметный только по жалобе);
      - у предупреждения есть альтернатива, иначе оно не меняет решений.
    """
    print("\n10. Предупреждения о перегрузе")
    from datetime import datetime, timedelta

    # Перегруз на завтра: ёмкость 1 ед./час, а заказов на 2 ед.
    offset = 7
    storage.set_timezone(STORE_ID, offset)
    tomorrow = (datetime.utcnow() + timedelta(hours=offset) + timedelta(days=1)).date().isoformat()

    rows = [
        retailcrm.parse_order(dict(order(200, hour=12), delivery={"date": tomorrow,
                                                                  "code": "dostavka-kurerom"}), {}),
        retailcrm.parse_order(dict(order(201, hour=12), delivery={"date": tomorrow,
                                                                  "code": "dostavka-kurerom"}), {}),
    ]
    couriers_storage.replace_orders_window(tomorrow, tomorrow, rows)
    storage.set_exception(STORE_ID, tomorrow, 12, capacity=1.0, reason="проверка")

    result = metrics.scan_alerts()
    check("перегруженный слот попал в предупреждения", result["created"] >= 1,
          f"получено {result}")

    again = metrics.scan_alerts()
    check("повторно о том же слоте не напоминаем", again["created"] == 0, f"получено {again}")

    data = metrics.alerts([STORE_ID])
    alert = next((a for a in data["items"] if a["date"] == tomorrow and a["hour"] == 12), None)
    check("предупреждение видно в списке", alert is not None, f"получено {data['items']}")
    if alert:
        check("к предупреждению приложены свободные слоты", len(alert["free_slots"]) > 0,
              f"получено {alert['free_slots']}")
        check("свободный слот — не тот же самый час",
              all(not (s["date"] == tomorrow and s["hour"] == 12) for s in alert["free_slots"]),
              f"получено {alert['free_slots']}")

    # Слот разгрузили: подняли ёмкость — предупреждение обязано закрыться само
    storage.set_exception(STORE_ID, tomorrow, 12, capacity=10.0, reason="вывели флориста")
    resolved = metrics.scan_alerts()
    check("разгруженный слот закрывает предупреждение", resolved["resolved"] >= 1,
          f"получено {resolved}")
    check("и оно уходит из активных",
          all(not (a["date"] == tomorrow and a["hour"] == 12) for a in metrics.alerts([STORE_ID])["items"]),
          "предупреждение осталось активным")

    stats = storage.alerts_stats("2000-01-01")
    check("счётчик пользы считает разгруженные", stats["resolved"] >= 1, f"получено {stats}")

    # Салон без пояса: сигнал по времени сервера был бы мимо на 5-7 часов
    conn = sqlite3.connect(os.environ["BARHAT_DB_PATH"])
    conn.execute("DELETE FROM salon_timezones WHERE store_id = ?", (STORE_ID,))
    conn.commit()
    conn.close()
    skipped = metrics.scan_alerts()
    check("салон без часового пояса пропускается и виден",
          any("Восход" in name for name in skipped["no_timezone"]), f"получено {skipped}")
    storage.set_timezone(STORE_ID, offset)


def test_slot_moved():
    print("\n11. Перенос заказа виден")
    day = DAY_SLOT_MOVED
    couriers_storage.replace_orders_window(day, day, [
        retailcrm.parse_order(dict(order(300, hour=10),
                                   delivery={"date": day, "code": "dostavka-kurerom"}), {}),
    ])
    with couriers_storage.get_db() as conn:
        before = conn.execute("SELECT slot_changed_at FROM courier_orders "
                              "WHERE retailcrm_order_id = 300").fetchone()["slot_changed_at"]
    check("у нового заказа отметки переноса нет", before is None, f"получено {before}")

    couriers_storage.replace_orders_window(day, day, [
        retailcrm.parse_order(dict(order(300, hour=16),
                                   delivery={"date": day, "code": "dostavka-kurerom"}), {}),
    ])
    with couriers_storage.get_db() as conn:
        after = conn.execute("SELECT ready_hour, slot_changed_at FROM courier_orders "
                             "WHERE retailcrm_order_id = 300").fetchone()
    check("смена часа готовности отмечена", after["slot_changed_at"] is not None,
          f"получено {dict(after)}")

    couriers_storage.replace_orders_window(day, day, [
        retailcrm.parse_order(dict(order(300, hour=16),
                                   delivery={"date": day, "code": "dostavka-kurerom"}), {}),
    ])
    with couriers_storage.get_db() as conn:
        kept = conn.execute("SELECT slot_changed_at FROM courier_orders "
                            "WHERE retailcrm_order_id = 300").fetchone()["slot_changed_at"]
    check("пересборка окна не обнуляет отметку", kept == after["slot_changed_at"],
          f"было {after['slot_changed_at']}, стало {kept}")


def test_capacity_suggestion():
    print("\n12. Норма из факта")
    from datetime import date as _date, timedelta as _td

    # Норма считается по ПРОШЛОМУ: будущие заказы фактом ещё не стали.
    # Поэтому кладём несколько отработанных часов на прошедшие дни.
    for shift in (1, 2, 3):
        past = (_date.today() - _td(days=shift)).isoformat()
        couriers_storage.replace_orders_window(past, past, [
            retailcrm.parse_order(dict(order(400 + shift * 10 + i, hour=11 + i),
                                       delivery={"date": past, "code": "dostavka-kurerom"}), {})
            for i in range(3)
        ])

    suggestion = metrics.suggest_capacity(STORE_ID, days=60)
    check("подсказка считается по фактическим часам", suggestion["samples"] > 0,
          f"получено {suggestion}")
    check("медиана и перцентиль отдаются",
          suggestion["median"] is not None and suggestion["p80"] is not None,
          f"получено {suggestion}")
    check("текущая ёмкость показана рядом", suggestion["current"] is not None,
          f"получено {suggestion}")


def test_review_fixes():
    """Находки ревью 2026-09-05 — чтобы не вернулись."""
    print("\n13. Разбор ревью")
    from datetime import datetime, timedelta
    from pyrus.server import app

    # Прошедшие часы не предлагаем: «перенесите с 17:00 на 09:00 сегодня» —
    # совет, который невозможно выполнить.
    offset = 7
    storage.set_timezone(STORE_ID, offset)
    now_local = datetime.utcnow() + timedelta(hours=offset)
    today_local = now_local.date().isoformat()
    storage.apply_working_hours(STORE_ID, 0, 24, capacity=10.0)

    free = metrics.free_slots(STORE_ID, today_local, days=1)
    past = [slot for slot in free["slots"] if slot["hour"] <= now_local.hour]
    check("прошедшие часы не предлагаются", not past, f"получено {past}")

    # Некорректная дата — 400, а не 500
    with app.test_client() as client:
        login_as(client, "test-load-admin")
        for url in (f"/api/salon-load/day?date=2026-13-45",
                    f"/api/salon-load/week?from=2026-02-30",
                    f"/api/salon-load/exceptions?from=abc"):
            response = client.get(url)
            check(f"{url.split('?')[0]} на кривой дате отвечает 400",
                  response.status_code == 400, f"получено {response.status_code}")

    # Копия графика замещает, а не дополняет
    storage.apply_working_hours(OTHER_STORE_ID, 0, 24, capacity=3.0)
    storage.apply_working_hours(STORE_ID, 9, 12, capacity=5.0)
    storage.copy_week(STORE_ID, OTHER_STORE_ID)
    target = storage.weekly_grid(OTHER_STORE_ID)
    check("после копирования у приёмника график источника",
          target["0:20"]["closed"] is True and target["0:10"]["capacity"] == 5.0,
          f"получено {target['0:20']}, {target['0:10']}")

    # Покрытие весами считается по тем же статусам, что и сетка
    coverage_all = couriers_storage.weights_coverage(DAY, DAY)
    coverage_none = couriers_storage.weights_coverage(DAY, DAY, load_statuses=["nonexistent"])
    check("покрытие весами фильтруется статусами",
          coverage_all["total_units"] > 0 and coverage_none["total_units"] == 0,
          f"получено {coverage_all['total_units']} и {coverage_none['total_units']}")

    # Заказ переехал на другую дату — старые позиции не задваивают вес
    day_a, day_b = DAY_REVIEW_A, DAY_REVIEW_B
    couriers_storage.replace_orders_window(day_a, day_a, [
        retailcrm.parse_order(dict(order(500, hour=10),
                                   delivery={"date": day_a, "code": "dostavka-kurerom"}), {}),
    ])
    couriers_storage.replace_orders_window(day_b, day_b, [
        retailcrm.parse_order(dict(order(500, hour=10),
                                   delivery={"date": day_b, "code": "dostavka-kurerom"}), {}),
    ])
    with couriers_storage.get_db() as conn:
        weight = conn.execute("SELECT weight_units FROM courier_orders "
                              "WHERE retailcrm_order_id = 500").fetchone()["weight_units"]
        items = conn.execute("SELECT COUNT(*) AS c FROM order_items "
                             "WHERE retailcrm_order_id = 500").fetchone()["c"]
        # Ожидание берём из справочника, а не константой: вес товара к этому
        # моменту могли поменять предыдущие проверки, а суть здесь в другом —
        # позиция должна посчитаться ОДИН раз, а не два.
        row = conn.execute("SELECT weight FROM product_weights WHERE offer_id = 1").fetchone()
    extra = row["weight"] if row else 0.0
    expected = couriers_storage.ORDER_BASE_UNITS + extra
    check("переезд заказа не задваивает вес", weight == expected,
          f"получено {weight}, ожидалось {expected} "
          f"(задвоение дало бы {couriers_storage.ORDER_BASE_UNITS + extra * 2})")
    check("старые позиции переехавшего заказа удалены", items == 1, f"получено {items}")

    # Нечисловое значение кастомного поля не роняет разбор
    weird = retailcrm.parse_order({
        "id": 600, "number": "600", "site": STORE_KEY, "status": "at-work",
        "shipmentStore": STORE_KEY, "delivery": {"date": DAY, "code": "dostavka-kurerom"},
        "customFields": {"order_availability_time": 1000}, "items": [],
    }, {})
    check("нестроковое время готовности не роняет разбор", weird is not None,
          "parse_order упал")

    # Флорист не видит сводку по чужим салонам
    grid = metrics.day_grid(DAY, [STORE_ID])
    check("нераспределённые не показываются тому, кто видит не все салоны",
          grid["unassigned"] is None, f"получено {grid['unassigned']}")
    check("а администратору показываются",
          metrics.day_grid(DAY, None)["unassigned"] is not None, "сводка пропала у админа")


def test_capacity_in_florists():
    """Ф4: ёмкость задаётся людьми, старые единицы при этом не трогаются."""
    print("\n16. Ёмкость в флористах")
    from pyrus.server import app

    FLORIST_STORE = OTHER_STORE_ID

    # Салон живёт на старой модели: единицы есть, флористов нет.
    storage.apply_working_hours(FLORIST_STORE, 9, 21, capacity=6.0)
    status = storage.capacity_model_status()[FLORIST_STORE]
    # 12 рабочих часов × 7 дней недели: статус считается по всей сетке салона.
    check("салон на старых единицах виден как таковой",
          status["units_hours"] == 84 and status["florist_hours"] == 0, f"получено {status}")

    # Заполняем флористами — старая ёмкость обязана остаться на месте, иначе
    # экран, который до Ф6 считает по ней, обнулится прямо на глазах.
    storage.apply_working_hours(FLORIST_STORE, 9, 21, florists=1.5)
    grid = storage.weekly_grid(FLORIST_STORE)
    slot = grid["0:10"]
    check("флористы записаны", slot["florists"] == 1.5, f"получено {slot}")
    check("старая ёмкость не затёрта вводом флористов", slot["capacity"] == 6.0,
          f"получено {slot}")
    check("нерабочий час остался закрытым и пустым",
          grid["0:3"]["closed"] and grid["0:3"]["florists"] is None, f"получено {grid['0:3']}")

    # И симметрично: правка старой ёмкости не сносит уже заданных людей.
    storage.apply_working_hours(FLORIST_STORE, 9, 21, capacity=7.0)
    slot = storage.weekly_grid(FLORIST_STORE)["0:10"]
    check("флористы не затёрты правкой старой ёмкости",
          slot["capacity"] == 7.0 and slot["florists"] == 1.5, f"получено {slot}")

    status = storage.capacity_model_status()[FLORIST_STORE]
    check("салон больше не считается «в старых единицах»", status["florist_hours"] == 84,
          f"получено {status}")

    # Минуты выводятся из людей, а не задаются отдельно.
    check("ёмкость в минутах = флористы × 60",
          storage.capacity_minutes(1.5, None) == 90.0,
          f"получено {storage.capacity_minutes(1.5, None)}")
    check("доля времени на сборку уменьшает ёмкость",
          storage.capacity_minutes(1.5, 0.5) == 45.0,
          f"получено {storage.capacity_minutes(1.5, 0.5)}")
    check("ёмкость не задана — это None, а не ноль",
          storage.capacity_minutes(None, None) is None)

    storage.set_assembly_share(FLORIST_STORE, 0.75)
    check("доля времени на сборку сохранена",
          storage.assembly_share_map().get(FLORIST_STORE) == 0.75,
          f"получено {storage.assembly_share_map()}")
    for bad in (0, -1, 1.5):
        try:
            storage.set_assembly_share(FLORIST_STORE, bad)
            check(f"доля {bad} отклонена", False, "исключения не было")
        except ValueError:
            check(f"доля {bad} отклонена", True)
    storage.set_assembly_share(FLORIST_STORE, 1.0)

    # Сетка отдаёт обе величины рядом: процент до Ф6 считает старая модель.
    grid = metrics.day_grid(DAY, [FLORIST_STORE])
    store_row = grid["stores"][0]
    cell = next(c for c in store_row["cells"] if c["hour"] == 10)
    check("в ячейке есть и старая ёмкость, и флористы",
          cell["capacity"] == 7.0 and cell["florists"] == 1.5, f"получено {cell}")
    check("в ячейке есть ёмкость в минутах", cell["capacity_minutes"] == 90.0,
          f"получено {cell}")
    check("процент по-прежнему считается по старой модели",
          cell["percent"] == metrics._percent(cell["units"], 7.0), f"получено {cell}")

    # Исключение на дату тоже задаётся людьми: 14 февраля в смене выходит
    # не столько же, сколько во вторник.
    storage.set_exception(FLORIST_STORE, DAY, None, None, florists=3.0, reason="праздник")
    exceptions = storage.exceptions_for([FLORIST_STORE], DAY, DAY)
    check("исключение хранит флористов",
          exceptions[f"{FLORIST_STORE}:{DAY}:10"]["florists"] == 3.0,
          f"получено {exceptions.get(f'{FLORIST_STORE}:{DAY}:10')}")
    cell = next(c for c in metrics.day_grid(DAY, [FLORIST_STORE])["stores"][0]["cells"]
                if c["hour"] == 10)
    check("исключение важнее недельного графика и в минутах",
          cell["capacity_minutes"] == 180.0, f"получено {cell}")
    storage.set_exception(FLORIST_STORE, DAY, None, None)

    # Подсказка обязана быть в тех же единицах, что и поле.
    suggestion = metrics.suggest_capacity(STORE_ID, days=60)
    check("подсказка отдаёт минуты", "median_minutes" in suggestion, f"получено {suggestion}")
    check("подсказка отдаёт людей", "median_florists" in suggestion, f"получено {suggestion}")
    if suggestion["median_minutes"]:
        expected = round(suggestion["median_minutes"] / 60.0 * 2) / 2
        check("люди пересчитаны из минут, а не взяты из старых единиц",
              suggestion["median_florists"] == expected,
              f"получено {suggestion['median_florists']}, ожидалось {expected}")

    # HTTP: пустая форма не заполняет неделю пустыми часами.
    ensure_user("test-load-admin", "admin", [])
    with app.test_client() as client:
        login_as(client, "test-load-admin")
        response = client.post("/api/salon-load/capacity/working-hours",
                               json={"store_id": FLORIST_STORE, "open_hour": 9, "close_hour": 21},
                               headers={"X-Requested-With": "barhat-dashboard"})
        check("без флористов и без ёмкости ручка отвечает 400",
              response.status_code == 400, f"получено {response.status_code}")

        response = client.post("/api/salon-load/capacity/working-hours",
                               json={"store_id": FLORIST_STORE, "open_hour": 9,
                                     "close_hour": 21, "florists": 2},
                               headers={"X-Requested-With": "barhat-dashboard"})
        check("ручка принимает флористов", response.status_code == 200,
              f"получено {response.status_code} {response.get_data(as_text=True)[:200]}")

        payload = (client.get("/api/salon-load/capacity/model").get_json() or {}).get("data", {})
        check("статус модели отдаётся ручкой", "stale" in payload, f"получено {payload}")
        check("перезаданный салон не числится в старых единицах",
              all(s["store_id"] != FLORIST_STORE for s in payload.get("stale", [])),
              f"получено {payload}")


def main():
    setup_data()
    test_capacity_states()
    test_exception_wins()
    test_separate_rows()
    test_pickup_counter()
    test_free_slots()
    test_week()
    test_permissions()
    test_http_access()
    test_ui_contract()
    test_alerts()
    test_slot_moved()
    test_capacity_suggestion()
    test_review_fixes()
    test_weight_units_model()
    test_round_clock()
    test_capacity_in_florists()

    print()
    if failures:
        print(f"ПРОВАЛЕНО: {len(failures)} — {', '.join(failures)}")
        sys.exit(1)
    print("Все проверки пройдены.")


if __name__ == "__main__":
    main()
