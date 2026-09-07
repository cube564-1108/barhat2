"""
Сторож: разрезы отчёта выплат курьерам считают то, что подписано.

Отчёт показывает четыре среза одних и тех же строк витрины: выплата по
курьерам, распределение по салонам, заказы без курьера и отменённые. У всех
четырёх общий отбор (период, город, салон, PAYOUT_FILTER) и разные условия по
статусу и курьеру — то есть ровно та конструкция, где расхождение никто не
заметит: сумма по салонам разойдётся с итогом на пару тысяч, и это спишут на
округление.

Здесь проверяется главное:
  - отменённые заказы НЕ входят в сумму к выплате;
  - сумма по салонам сходится с итогом отчёта;
  - «без курьера» — это список с номерами заказов, а не одно число;
  - фильтр «только свои курьеры» не прячет заказы без курьера, которые как раз
    и надо чинить в CRM.

Проверяется на временной базе, боевые данные не трогаются.

Запуск: python scripts/test_courier_payout_sections.py
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

TMP_DB = os.path.join(tempfile.mkdtemp(prefix="courier_sections_"), "couriers.db")
os.environ["COURIERS_DB_PATH"] = TMP_DB

from couriers import storage  # noqa: E402  — после подмены пути к базе

assert storage.DB_PATH == TMP_DB, f"тест пишет не в свою базу: {storage.DB_PATH}"

PERIOD = ("2026-09-01", "2026-09-30")

EKB = {"site_code": "barkhat-ekb", "city": "Екатеринбург", "delivery_city": "Екатеринбург"}
TOMSK = {"site_code": "barkhat-tomsk", "city": "Томск", "delivery_city": "Томск"}


def order(order_id, status, net_cost, site, courier_id=None, date="2026-09-10"):
    return {
        "retailcrm_order_id": order_id,
        "order_number": f"№{order_id}",
        "delivery_date": date,
        "courier_id": courier_id,
        "courier_name": "Курьер 1" if courier_id == 5 else None,
        "net_cost": net_cost,
        "status": status,
        "total_summ": 5000.0,
        "order_method": "phone",
        "delivery_code": "dostavka-kurerom",
        **site,
    }


ORDERS = [
    order(1, "complete", 300.0, EKB, courier_id=5),
    order(2, "complete", 200.0, TOMSK, courier_id=5),
    # Деньги потрачены, курьер не проставлен — это дырка в CRM, а не ноль.
    order(3, "complete", 150.0, EKB),
    # Самовывоз: ни курьера, ни себестоимости — в отчёт выплат не попадает вовсе.
    order(4, "complete", 0.0, EKB),
    # Отменён с назначенным курьером: в выплату не идёт, но видеть его надо.
    order(5, "cancel-other", 250.0, EKB, courier_id=5),
    # Отменён без курьера и без денег — показывать нечего.
    order(6, "cancel-other", 0.0, TOMSK),
    # Служба доставки: при «только свои» скрыта, при выключенном фильтре видна.
    order(7, "complete", 400.0, TOMSK, courier_id=2),
]

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"  OK   {name}")
    else:
        print(f"  FAIL {name}{': ' + detail if detail else ''}")
        failures.append(name)


def main():
    storage.init_couriers_tables()
    storage.upsert_couriers([
        {"id": 5, "name": "Курьер 1 ЕКБ", "is_service": False, "active": True},
        {"id": 2, "name": "Яндекс Доставка", "is_service": True, "active": True},
    ])
    storage.upsert_sites([
        {"code": "barkhat-ekb", "name": "ЕКБ Бажова", "city": "Екатеринбург"},
        {"code": "barkhat-tomsk", "name": "Томск ДК", "city": "Томск"},
    ])
    storage.upsert_order_statuses([
        {"code": "complete", "name": "Выполнен", "group_code": "complete", "active": True},
        {"code": "cancel-other", "name": "Отменён", "group_code": "cancel", "active": True},
    ])
    storage.replace_orders_window(*PERIOD, ORDERS)

    report = storage.report_by_courier(*PERIOD)
    totals = report["totals"]

    print("\nСумма к выплате")
    check("в выплату вошли только выполненные заказы своего курьера",
          totals["total_net_cost"] == 500.0, f"получено {totals['total_net_cost']}")
    check("доставок в выплате — две", totals["orders_count"] == 2,
          f"получено {totals['orders_count']}")

    print("\nОтменённые")
    cancelled = report["cancelled"]
    check("отменённый заказ найден по ГРУППЕ статуса", cancelled["count"] == 1,
          f"получено {cancelled['count']}")
    check("сумма отменённых посчитана отдельно", cancelled["total_net_cost"] == 250.0,
          f"получено {cancelled['total_net_cost']}")
    check("отменённые не попали в сумму к выплате", totals["total_net_cost"] == 500.0)
    check("отменённый без курьера и без денег не показывается",
          [o["retailcrm_order_id"] for o in cancelled["orders"]] == [5],
          f"получено {[o['retailcrm_order_id'] for o in cancelled['orders']]}")
    check("в строке отменённого есть номер заказа и название статуса",
          cancelled["orders"][0]["order_number"] == "№5"
          and cancelled["orders"][0]["status_name"] == "Отменён",
          f"получено {cancelled['orders'][0]}")
    check("справочник статусов отмены виден отдельным числом",
          cancelled["statuses_known"] == 1, f"получено {cancelled['statuses_known']}")

    print("\nЗаказы без курьера")
    missing = report["without_courier"]
    check("счётчик совпадает со старым полем totals",
          missing["count"] == totals["orders_without_courier"] == 1,
          f"получено {missing['count']} и {totals['orders_without_courier']}")
    check("отдаётся список с номерами заказов, а не одно число",
          [o["order_number"] for o in missing["orders"]] == ["№3"],
          f"получено {[o.get('order_number') for o in missing['orders']]}")
    check("самовывоз (нулевая себестоимость) в список не попал",
          all(o["retailcrm_order_id"] != 4 for o in missing["orders"]))
    check("в строке есть название салона",
          missing["orders"][0]["site_name"] == "ЕКБ Бажова",
          f"получено {missing['orders'][0].get('site_name')}")

    print("\nРаспределение по салонам")
    sites = report["sites"]
    check("салоны сошлись с итогом отчёта",
          round(sum(s["total_net_cost"] for s in sites), 2) == totals["total_net_cost"],
          f"получено {sum(s['total_net_cost'] for s in sites)} против {totals['total_net_cost']}")
    check("доставки сошлись с итогом отчёта",
          sum(s["orders_count"] for s in sites) == totals["orders_count"])
    by_code = {s["site_code"]: s for s in sites}
    check("название салона взято из справочника, а не код",
          by_code["barkhat-ekb"]["site_name"] == "ЕКБ Бажова",
          f"получено {by_code['barkhat-ekb']['site_name']}")
    check("незаполненный курьер виден в разрезе своего салона",
          by_code["barkhat-ekb"]["orders_without_courier"] == 1
          and by_code["barkhat-ekb"]["net_cost_without_courier"] == 150.0,
          f"получено {by_code['barkhat-ekb']}")
    check("салон без дырок показывает ноль незаполненных",
          by_code["barkhat-tomsk"]["orders_without_courier"] == 0,
          f"получено {by_code['barkhat-tomsk']['orders_without_courier']}")

    print("\nФильтр «только свои курьеры»")
    all_couriers = storage.report_by_courier(*PERIOD, only_own=False)
    check("служба доставки появляется при выключенном фильтре",
          all_couriers["totals"]["total_net_cost"] == 900.0,
          f"получено {all_couriers['totals']['total_net_cost']}")
    check("заказ без курьера виден и с включённым фильтром, и без него",
          all_couriers["totals"]["orders_without_courier"] == 1
          == totals["orders_without_courier"])
    check("салон службы доставки виден только при выключенном фильтре",
          {s["site_code"] for s in all_couriers["sites"]} == {"barkhat-ekb", "barkhat-tomsk"})

    print("\nФильтр по салону")
    only_ekb = storage.report_by_courier(*PERIOD, site_code="barkhat-ekb")
    check("сумма посчитана по одному салону", only_ekb["totals"]["total_net_cost"] == 300.0,
          f"получено {only_ekb['totals']['total_net_cost']}")
    check("отменённые тоже сузились до салона", only_ekb["cancelled"]["count"] == 1,
          f"получено {only_ekb['cancelled']['count']}")
    only_tomsk = storage.report_by_courier(*PERIOD, site_code="barkhat-tomsk")
    check("в чужом салоне отменённых нет", only_tomsk["cancelled"]["count"] == 0,
          f"получено {only_tomsk['cancelled']['count']}")

    print("\nРасшифровка по заказам (list_orders)")
    cancelled_list = storage.list_orders(*PERIOD, cancelled=True)
    check("cancelled=1 отдаёт отменённые, а не выполненные",
          [o["retailcrm_order_id"] for o in cancelled_list] == [5],
          f"получено {[o['retailcrm_order_id'] for o in cancelled_list]}")
    site_list = storage.list_orders(*PERIOD, site_code="barkhat-tomsk", courier_id=5)
    check("фильтр по салону дошёл до расшифровки",
          [o["retailcrm_order_id"] for o in site_list] == [2],
          f"получено {[o['retailcrm_order_id'] for o in site_list]}")
    check("в расшифровке есть название салона",
          site_list[0]["site_name"] == "Томск ДК", f"получено {site_list[0].get('site_name')}")

    print()
    if failures:
        print(f"ПРОВАЛЕНО: {len(failures)} — {', '.join(failures)}")
        sys.exit(1)
    print("Все проверки пройдены.")


if __name__ == "__main__":
    main()
