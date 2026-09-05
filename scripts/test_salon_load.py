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

DAY = "2026-09-10"          # четверг
WEEKDAY = 3
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

        response = client.get(f"/api/salon-load/day?date={DAY}")
        payload = (response.get_json() or {}).get("data", {})
        check("/day отвечает", response.status_code == 200, f"получено {response.status_code}")
        for field in ("hours", "stores", "thresholds", "coverage", "freshness", "can_edit"):
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

        response = client.get("/api/couriers/weights?only_missing=1")
        payload = response.get_json() or {}
        check("справочник весов отвечает", response.status_code == 200,
              f"получено {response.status_code}")
        check("в мета есть покрытие весами",
              "coverage" in (payload.get("meta") or {}), f"получено {payload.get('meta')}")

        response = client.get("/api/couriers/order-statuses")
        payload = response.get_json() or {}
        check("справочник статусов отвечает", response.status_code == 200,
              f"получено {response.status_code}")
        check("у статуса есть признак нагрузки",
              all("counts_as_load" in s for s in payload.get("data", [])),
              f"получено {payload.get('data')}")

        # Запись: те же тела запроса, что шлёт экран
        headers = {"X-Requested-With": "barhat-dashboard"}
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

        response = client.post("/api/couriers/weights", headers=headers,
                               json={"weights": {"1": 3.5}})
        check("вес товара сохраняется", response.status_code == 200,
              f"получено {response.status_code} {response.get_data(as_text=True)[:160]}")

        response = client.post("/api/couriers/weights", headers=headers,
                               json={"weights": {"1": 0}})
        check("нулевой вес отклоняется ручкой", response.status_code == 400,
              f"получено {response.status_code}")

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
    day = "2026-09-25"
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

    print()
    if failures:
        print(f"ПРОВАЛЕНО: {len(failures)} — {', '.join(failures)}")
        sys.exit(1)
    print("Все проверки пройдены.")


if __name__ == "__main__":
    main()
