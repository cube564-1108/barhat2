"""
Сторож: чтения витрины заказов не видят невыполненные заказы.

Витрина `courier_orders` общая для трёх модулей: выплаты курьерам, показатели
салонов и (с Фазы 1 плана «Загрузка салонов») будущие заказы всех статусов.
До 2026-09-05 четыре чтения статус не фильтровали — их случайно прикрывал
PAYOUT_FILTER и то, что синк тянул только выполненные. Как только в витрине
появляются будущие заказы, «данные по такое-то число» в отчёте выплат уезжает
в будущее, а диагностика показывает строки, которых нет ни в одном отчёте.

Проверяется на временной базе, боевые данные не трогаются.

Запуск: python scripts/test_courier_reads_status_filter.py
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

TMP_DB = os.path.join(tempfile.mkdtemp(prefix="courier_reads_"), "couriers.db")
os.environ["COURIERS_DB_PATH"] = TMP_DB

from couriers import storage  # noqa: E402  — после подмены пути к базе

assert storage.DB_PATH == TMP_DB, f"тест пишет не в свою базу: {storage.DB_PATH}"

PAST_COMPLETE = {
    "retailcrm_order_id": 1, "order_number": "1", "delivery_date": "2026-09-01",
    "courier_id": 5, "courier_name": "Курьер 1", "net_cost": 300.0,
    "site_code": "barkhat-ekb", "city": "Екатеринбург", "delivery_city": "Екатеринбург",
    "status": "complete", "total_summ": 5000.0, "order_method": "offline",
    "delivery_code": "dostavka-kurerom",
}

# Будущий заказ: курьер уже назначен, поэтому PAYOUT_FILTER его пропускает —
# отсечь такой заказ может только явный фильтр по статусу.
FUTURE_IN_WORK = {
    "retailcrm_order_id": 2, "order_number": "2", "delivery_date": "2026-12-31",
    "courier_id": 5, "courier_name": "Курьер 1", "net_cost": 300.0,
    "site_code": "barkhat-tomsk", "city": "Томск", "delivery_city": "Томск",
    "status": "at-work", "total_summ": 7000.0, "order_method": "phone",
    "delivery_code": "dostavka-kurerom",
}

FUTURE_CANCELLED = dict(FUTURE_IN_WORK, retailcrm_order_id=3, delivery_date="2026-11-15",
                        status="cancel-other", city="Барнаул")

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"  OK   {name}")
    else:
        print(f"  FAIL {name}{': ' + detail if detail else ''}")
        failures.append(name)


def main():
    storage.init_couriers_tables()
    storage.replace_orders_window("2026-09-01", "2026-12-31",
                                  [PAST_COMPLETE, FUTURE_IN_WORK, FUTURE_CANCELLED])

    print("\nГраницы данных (get_orders_date_range)")
    data_range = storage.get_orders_date_range()
    check("max_date не уезжает в будущее", data_range["max_date"] == "2026-09-01",
          f"получено {data_range['max_date']}")
    check("min_date по выполненным", data_range["min_date"] == "2026-09-01",
          f"получено {data_range['min_date']}")

    print("\nГорода фильтра (list_cities)")
    cities = storage.list_cities()
    check("город невыполненного заказа не попал в фильтр", "Томск" not in cities,
          f"получено {cities}")
    check("город выполненного заказа на месте", "Екатеринбург" in cities,
          f"получено {cities}")

    print("\nДиапазон показателей салонов (shipments_data_range)")
    shipments = storage.shipments_data_range()
    check("until не уезжает в будущее", shipments["until"] == "2026-09-01",
          f"получено {shipments['until']}")

    print("\nДиагностика /health (health_snapshot)")
    health = storage.health_snapshot()
    check("счётчик строк считает только выполненные", health["rows"] == 1,
          f"получено {health['rows']}")
    check("until диагностики не уезжает в будущее", health["until"] == "2026-09-01",
          f"получено {health['until']}")
    check("строки всех статусов видны отдельным числом",
          health.get("rows_all_statuses") == 3,
          f"получено {health.get('rows_all_statuses')}")

    print("\nОтчёт выплат не изменился")
    report = storage.report_by_courier("2026-09-01", "2026-12-31", only_own=False)
    total = sum(row["orders_count"] for row in report["couriers"])
    check("в выплату попал только выполненный заказ", total == 1, f"получено {total}")

    print()
    if failures:
        print(f"ПРОВАЛЕНО: {len(failures)} — {', '.join(failures)}")
        sys.exit(1)
    print("Все проверки пройдены.")


if __name__ == "__main__":
    main()
