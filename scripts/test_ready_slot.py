"""
Сторож слотов готовности (Фаза 1 плана «Загрузка салонов»).

Проверяет то, что ломается молча:
  - разбор времени готовности: «9:00» валидно, «уточ» — нет и не превращается
    в 00:00 (pre-mortem №3: слот, выведенный из головы, сдвигает всю сетку);
  - источник времени: availability против delivery.time.from — поля расходятся
    у 70% заказов, перепутать их значит считать нагрузку по времени доставки;
  - позиции заказа пишутся и чистятся вместе с окном: осиротевшие позиции
    завышают вес слота молча;
  - вес заказа считается при синке, товар без веса идёт по весу по умолчанию,
    а не нулём;
  - справочник статусов сидируется из группы CRM, а ручная правка признака
    «нагрузка» переживает синхронизацию.

Проверяется на временной базе, боевые данные не трогаются.

Запуск: python scripts/test_ready_slot.py
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

TMP_DB = os.path.join(tempfile.mkdtemp(prefix="ready_slot_"), "couriers.db")
os.environ["COURIERS_DB_PATH"] = TMP_DB

from couriers import retailcrm, storage  # noqa: E402  — после подмены пути к базе

assert storage.DB_PATH == TMP_DB, f"тест пишет не в свою базу: {storage.DB_PATH}"

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"  OK   {name}")
    else:
        print(f"  FAIL {name}{': ' + detail if detail else ''}")
        failures.append(name)


def order(order_id, delivery_date, availability=None, delivery_from=None,
          store="barkhat-ekb", status="at-work", items=None):
    """Заказ в том виде, в каком его отдаёт RetailCRM."""
    payload = {
        "id": order_id,
        "number": str(order_id),
        "site": "barkhat-ekb",
        "status": status,
        "summ": 5000,
        "shipmentStore": store,
        "delivery": {"date": delivery_date, "code": "dostavka-kurerom"},
        "customFields": {},
        "items": items or [],
    }
    if availability is not None:
        payload["customFields"]["order_availability_time"] = availability
    if delivery_from is not None:
        payload["delivery"]["time"] = {"from": delivery_from, "to": "23:00"}
    return payload


def item(offer_id, quantity, name="Букет"):
    return {"quantity": quantity, "offer": {"id": offer_id, "displayName": name, "article": "f1"}}


def test_parse_time():
    print("\n1. Разбор человеческой записи времени")
    cases_ok = {
        "10:20": "10:20", "9:00": "09:00", "09:00": "09:00", "9.00": "09:00",
        "9-00": "09:00", " 18:30 ": "18:30", "18": "18:00", "18ч": "18:00",
        "00:05": "00:05", "23:59": "23:59",
    }
    for raw, expected in cases_ok.items():
        got = retailcrm.parse_time_value(raw)
        check(f"{raw!r} → {expected}", got == expected, f"получено {got!r}")

    cases_none = ["уточ", "ут", "уточнить", "Ждем уточнений",
                  "уточнить заказ был на вчера до 00", "", None, "25:00", "10:75", "уточ /ндз"]
    for raw in cases_none:
        got = retailcrm.parse_time_value(raw)
        check(f"{raw!r} не время", got is None, f"получено {got!r}")


def test_ready_slot():
    print("\n2. Источник часа готовности")
    slot = retailcrm.ready_slot(order(1, "2026-09-10", availability="10:20", delivery_from="11:20"))
    check("время готовности берётся из availability, а не из доставки",
          slot["ready_time"] == "10:20" and slot["ready_source"] == "availability",
          f"получено {slot}")

    slot = retailcrm.ready_slot(order(2, "2026-09-10", delivery_from="11:20"))
    check("без availability берём delivery.time.from",
          slot["ready_time"] == "11:20" and slot["ready_source"] == "delivery_from",
          f"получено {slot}")

    slot = retailcrm.ready_slot(order(3, "2026-09-10", availability="уточ", delivery_from="11:20"))
    check("неразобранное availability не мешает взять доставку",
          slot["ready_time"] == "11:20", f"получено {slot}")

    slot = retailcrm.ready_slot(order(4, "2026-09-10", availability="уточ"))
    check("нечитаемое значение не превращается в 00:00", slot["ready_hour"] is None, f"получено {slot}")
    check("но отличается от «поля нет вовсе»", slot["ready_source"] == "unparsed", f"получено {slot}")

    slot = retailcrm.ready_slot(order(5, "2026-09-10"))
    check("заказ без времени — источник пустой", slot["ready_source"] is None, f"получено {slot}")

    slot = retailcrm.ready_slot(order(6, "2026-09-10", availability="9:00"))
    check("час слота берётся из разобранного времени", slot["ready_hour"] == 9, f"получено {slot}")


def test_parse_order():
    print("\n3. Заказ целиком")
    parsed = retailcrm.parse_order(
        order(10, "2026-09-10", availability="14:30", store="barkhat-tomsk",
              items=[item(100, 2), item(200, 1)]),
        {"barkhat-ekb": "Екатеринбург"},
    )
    check("склад-исполнитель взят из shipmentStore", parsed["store_key"] == "barkhat-tomsk",
          f"получено {parsed['store_key']}")
    check("час готовности разобран", parsed["ready_hour"] == 14, f"получено {parsed['ready_hour']}")
    check("позиции разобраны", len(parsed["items"]) == 2, f"получено {parsed['items']}")

    no_date = retailcrm.parse_order({"id": 11, "delivery": {}}, {})
    check("заказ без даты доставки отбрасывается", no_date is None, f"получено {no_date}")


def test_storage_round_trip():
    print("\n4. Запись окна: слоты, позиции, веса")
    storage.init_couriers_tables()
    site_cities = {"barkhat-ekb": "Екатеринбург"}

    rows = [
        retailcrm.parse_order(
            order(100, "2026-09-10", availability="10:00", items=[item(1, 2), item(2, 1)]),
            site_cities),
        retailcrm.parse_order(
            order(101, "2026-09-10", availability="уточ", items=[item(1, 1)]), site_cities),
    ]
    storage.replace_orders_window("2026-09-10", "2026-09-10", rows)

    with storage.get_db() as conn:
        saved = conn.execute(
            "SELECT ready_hour, ready_source, store_key, weight_units, duration_slots "
            "FROM courier_orders WHERE retailcrm_order_id = 100"
        ).fetchone()
        items_count = conn.execute("SELECT COUNT(*) AS c FROM order_items").fetchone()["c"]

    check("час слота записан", saved["ready_hour"] == 10, f"получено {saved['ready_hour']}")
    check("склад записан", saved["store_key"] == "barkhat-ekb", f"получено {saved['store_key']}")
    check("длительность заказа по умолчанию 1 слот", saved["duration_slots"] == 1,
          f"получено {saved['duration_slots']}")
    check("позиции записаны", items_count == 3, f"получено {items_count}")
    check("вес по умолчанию: 3 единицы товара × 1.0", saved["weight_units"] == 3.0,
          f"получено {saved['weight_units']}")

    print("\n5. Вес из справочника")
    with storage.get_db() as conn:
        conn.execute("INSERT INTO product_weights (offer_id, weight) VALUES (1, 4.0)")
    storage.recalc_weights_range("2026-09-10", "2026-09-10")
    with storage.get_db() as conn:
        weight = conn.execute(
            "SELECT weight_units FROM courier_orders WHERE retailcrm_order_id = 100"
        ).fetchone()["weight_units"]
    check("вес считается по справочнику, остальное по умолчанию (2×4 + 1×1)", weight == 9.0,
          f"получено {weight}")

    print("\n6. Пересборка окна чистит позиции")
    storage.replace_orders_window("2026-09-10", "2026-09-10", [
        retailcrm.parse_order(
            order(100, "2026-09-10", availability="10:00", items=[item(1, 1)]), site_cities),
    ])
    with storage.get_db() as conn:
        items_count = conn.execute("SELECT COUNT(*) AS c FROM order_items").fetchone()["c"]
        orphans = conn.execute("""
            SELECT COUNT(*) AS c FROM order_items i
             WHERE NOT EXISTS (SELECT 1 FROM courier_orders o
                                WHERE o.retailcrm_order_id = i.retailcrm_order_id)
        """).fetchone()["c"]
    check("позиции удалённого заказа исчезли", items_count == 1, f"получено {items_count}")
    check("осиротевших позиций нет", orphans == 0, f"получено {orphans}")


def test_statuses():
    print("\n7. Справочник статусов")
    storage.upsert_order_statuses([
        {"code": "at-work", "name": "В работе", "group_code": "new", "active": True},
        {"code": "cancel-other", "name": "Отменен", "group_code": "cancel", "active": True},
        {"code": "complete", "name": "Выполнен", "group_code": "complete", "active": True},
    ])
    codes = set(storage.load_status_codes())
    check("отменённые статусы не считаются нагрузкой", "cancel-other" not in codes, f"получено {codes}")
    check("рабочие статусы считаются нагрузкой", {"at-work", "complete"} <= codes, f"получено {codes}")

    # Ручная правка: владелец решил не считать «Выполнен» нагрузкой
    with storage.get_db() as conn:
        conn.execute("UPDATE order_statuses SET counts_as_load = 0 WHERE code = 'complete'")
    storage.upsert_order_statuses([
        {"code": "complete", "name": "Выполнен", "group_code": "complete", "active": True},
    ])
    codes = set(storage.load_status_codes())
    check("синхронизация не перетирает ручную правку", "complete" not in codes, f"получено {codes}")


def main():
    test_parse_time()
    test_ready_slot()
    test_parse_order()
    test_storage_round_trip()
    test_statuses()

    print()
    if failures:
        print(f"ПРОВАЛЕНО: {len(failures)} — {', '.join(failures)}")
        sys.exit(1)
    print("Все проверки пройдены.")


if __name__ == "__main__":
    main()
