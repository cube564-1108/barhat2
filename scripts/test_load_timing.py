"""
Сторож расчёта минут сборки (Ф3 плана «нагрузка в минутах»).

Проверяет то, что ломается молча:
  - заказ из разбора считается ровно в 48,5 минут — цифра, на которой владелец
    сверял модель, и любое расхождение с ней означает, что формула поехала;
  - тариф выбирается по диапазону количества, а не «примерно»;
  - микс дороже монобукета, но только там, где микс вообще возможен: из 1–2
    цветков его не бывает;
  - количество больше последнего диапазона берёт последнюю строку, а не ноль
    и не выдуманную экстраполяцию;
  - готовый товар «за позицию» не умножается на количество — иначе набор,
    заведённый в граммах, даст ошибку в сотни раз;
  - дыра и пересечение в тарифной сетке прекращают расчёт с ошибкой, а не
    занижают загрузку молча;
  - позиция без нормы не превращается в ноль незаметно: заказ помечается.

ВАЖНО: прогон читает боевой .env, поэтому сеть глушится до импорта.

Запуск: python scripts/test_load_timing.py
"""

import os
import socket
import ssl  # noqa: F401  — импортировать до патча сокета
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))
os.chdir(REPO)


class NetworkBlocked(Exception):
    pass


def _blocked(*args, **kwargs):
    raise NetworkBlocked("прогон не должен ходить в боевые внешние API")


socket.socket.connect = _blocked

TMP = tempfile.mkdtemp(prefix="load_timing_")
os.environ["COURIERS_DB_PATH"] = os.path.join(TMP, "couriers.db")

from couriers import retailcrm, storage, timing  # noqa: E402

failures = []
DAY = "2026-09-20"

# Офферы: 10 — роза (цветок), 20 — клубника (вес), 30 — упаковка,
# 40 — шапка «Сборный клубнично-цветочный букет», 50 — каталожный набор,
# 60 — гвоздика (второй вид цветка), 70 — набор в граммах «за позицию».
NORMS = {
    10: {"role": "flower", "minutes": None, "basis": None, "berry_mode": None},
    20: {"role": "berry", "minutes": None, "basis": None, "berry_mode": None},
    30: {"role": "packaging", "minutes": None, "basis": None, "berry_mode": None},
    40: {"role": "none", "minutes": 0, "basis": None, "berry_mode": "bouquet"},
    45: {"role": "none", "minutes": 0, "basis": None, "berry_mode": "box"},
    50: {"role": "catalog", "minutes": 12, "basis": "unit", "berry_mode": None},
    60: {"role": "flower", "minutes": None, "basis": None, "berry_mode": None},
    70: {"role": "catalog", "minutes": 20, "basis": "line", "berry_mode": None},
}

FLOWERS = [dict(zip(("range_from", "range_to", "mono_minutes", "mix_minutes",
                     "ribbon_minutes", "package_minutes"), row))
           for row in storage.DEFAULT_FLOWER_TARIFFS]
BERRIES = {row[0]: {"mode": row[0], "minutes_per_100g": row[1], "package_minutes": row[2]}
           for row in storage.DEFAULT_BERRY_TARIFFS}


def check(name, condition, detail=""):
    if condition:
        print(f"  OK   {name}")
    else:
        print(f"  FAIL {name}{': ' + detail if detail else ''}")
        failures.append(name)


def minutes(items):
    return timing.order_minutes(items, NORMS, FLOWERS, BERRIES)


def item(offer_id, quantity):
    return {"offer_id": offer_id, "quantity": quantity}


def test_reference_order():
    print("\n1. Заказ из разбора: 500 г клубники + 7 роз + упаковка")
    result = minutes([item(40, 1), item(20, 500), item(10, 7), item(30, 1)])
    check("цветы: 7 × 0,5 = 3,5", result["flowers"] == 3.5, f"получено {result['flowers']}")
    check("упаковка цветов: строка упаковки есть → 10",
          result["packaging"] == 10, f"получено {result['packaging']}")
    check("клубника: 500/100 × 5 + упаковка букета 10 = 35",
          result["berries"] == 35, f"получено {result['berries']}")
    check("итого 48,5 минут", result["total"] == 48.5, f"получено {result['total']}")
    check("заказ посчитан полностью", result["without_norm"] == 0, f"получено {result}")


def test_ranges():
    print("\n2. Диапазоны тарифа")
    check("1 цветок: 1 мин/шт + лента 5", minutes([item(10, 1)])["total"] == 6.0,
          f"получено {minutes([item(10, 1)])}")
    check("два цветка: 2 × 1 + 5 = 7", minutes([item(10, 2)])["total"] == 7.0,
          f"получено {minutes([item(10, 2)])}")
    check("три цветка переходят в следующий диапазон: 3 × 0,5 + 5 = 6,5",
          minutes([item(10, 3)])["total"] == 6.5, f"получено {minutes([item(10, 3)])}")
    check("25 цветов: 25 × 0,4 + лента 5 = 15",
          minutes([item(10, 25)])["total"] == 15.0, f"получено {minutes([item(10, 25)])}")
    check("51 цветок: 51 × 0,4 + лента 7 = 27,4",
          minutes([item(10, 51)])["total"] == 27.4, f"получено {minutes([item(10, 51)])}")


def test_mono_vs_mix():
    print("\n3. Монобукет и микс")
    mono = minutes([item(10, 10)])
    mix = minutes([item(10, 5), item(60, 5)])
    check("монобукет из 10: 10 × 0,5 + 5 = 10", mono["total"] == 10.0, f"получено {mono}")
    check("микс из 10 дороже: 10 × 0,6 + 5 = 11", mix["total"] == 11.0, f"получено {mix}")
    check("виды цветка посчитаны", mix["flower_kinds"] == 2, f"получено {mix}")

    two_kinds_small = minutes([item(10, 1), item(60, 1)])
    check("из двух цветков микса не бывает — тариф монобукета (2 × 1 + 5)",
          two_kinds_small["total"] == 7.0, f"получено {two_kinds_small}")


def test_over_last_range():
    print("\n4. Количество больше последнего диапазона")
    result = minutes([item(10, 500)])
    check("берётся последняя строка, а не ноль: 500 × 0,4 + 7 = 207",
          result["total"] == 207.0, f"получено {result}")


def test_catalog_basis():
    print("\n5. База начисления у готового товара")
    check("за штуку: 3 × 12 = 36", minutes([item(50, 3)])["catalog"] == 36.0,
          f"получено {minutes([item(50, 3)])}")
    check("за позицию: 300 «граммов» дают 20, а не 6000",
          minutes([item(70, 300)])["catalog"] == 20.0, f"получено {minutes([item(70, 300)])}")


def test_berry_mode():
    print("\n6. Режим клубники задаёт упаковку")
    bouquet = minutes([item(40, 1), item(20, 100)])
    box = minutes([item(45, 1), item(20, 100)])
    check("букет: 5 + упаковка 10", bouquet["berries"] == 15.0, f"получено {bouquet}")
    check("коробочка: 5 + упаковка 2", box["berries"] == 7.0, f"получено {box}")

    default = minutes([item(20, 100)])
    check("режим не объявлен — считаем букетом", default["berries"] == 15.0,
          f"получено {default}")

    both = minutes([item(45, 1), item(40, 1), item(20, 100)])
    check("при двух шапках выбор детерминирован (меньший offer_id → букет)",
          both["berries"] == 15.0, f"получено {both}")


def test_without_norm():
    print("\n7. Позиция без нормы видна")
    result = minutes([item(10, 5), item(999, 1)])
    check("неразмеченная позиция посчитана отдельно", result["without_norm"] == 1,
          f"получено {result}")
    check("остальное посчитано: 5 × 0,5 + 5 = 7,5", result["total"] == 7.5,
          f"получено {result}")

    empty = minutes([])
    check("заказ без позиций весит ноль минут", empty["total"] == 0.0, f"получено {empty}")


def test_broken_tariffs():
    print("\n8. Битая сетка прекращает расчёт")
    for rows, label in (
        ([], "пустая сетка"),
        ([{"range_from": 3, "range_to": 17, "mono_minutes": 1, "mix_minutes": 1,
           "ribbon_minutes": 1, "package_minutes": 1}], "сетка не с единицы"),
        ([FLOWERS[0], dict(FLOWERS[1], range_from=4)], "дыра между диапазонами"),
        ([FLOWERS[0], dict(FLOWERS[1], range_from=2)], "пересечение диапазонов"),
    ):
        try:
            timing.validate_flower_tariffs(rows)
            check(f"{label} отклонена", False, "исключения не было")
        except timing.TariffError:
            check(f"{label} отклонена", True)


def test_storage_roundtrip():
    print("\n9. Тарифы и пересчёт через базу")
    storage.init_couriers_tables()
    flowers, berries = storage.load_tariffs()
    check("сетка засеяна значениями владельца", len(flowers) == 5, f"получено {len(flowers)}")
    check("клубника засеяна двумя режимами",
          berries["bouquet"]["package_minutes"] == 10 and berries["box"]["package_minutes"] == 2,
          f"получено {berries}")

    try:
        storage.set_flower_tariff(3, 30, 0.5, 0.6, 5, 10)   # наедет на 18–35
        check("правка, ломающая сетку, отклонена", False, "исключения не было")
    except timing.TariffError:
        check("правка, ломающая сетку, отклонена", True)

    storage.set_flower_tariff(1, 2, 1.5, None, 5, 10, "tester")
    flowers, _ = storage.load_tariffs()
    check("корректная правка сохранена",
          flowers[0]["mono_minutes"] == 1.5, f"получено {flowers[0]}")
    storage.set_flower_tariff(1, 2, 1.0, None, 5, 10, "tester")

    # Полный круг: каталог → нормы → заказ → минуты в витрине
    storage.replace_catalog(
        [{"id": 1, "parent_id": None, "name": "Цветы"}],
        [{"offer_id": 10, "product_id": 1, "article": "f1", "name": "Роза", "unit_code": "pc"}],
        [(10, 1)],
    )
    storage.upsert_order_statuses(
        [{"code": "at-work", "name": "В работе", "group_code": "new", "active": True}])
    storage.set_time_norm("offer", 10, role="flower", username="tester")

    order = {
        "id": 1, "number": "1", "site": "s", "status": "at-work", "summ": 100,
        "shipmentStore": "s", "delivery": {"date": DAY, "code": "dostavka-kurerom"},
        "customFields": {"order_availability_time": "12:00"},
        "items": [{"quantity": 7, "offer": {"id": 10, "displayName": "Роза"}}],
    }
    storage.replace_orders_window(DAY, DAY, [retailcrm.parse_order(order, {})])

    with storage.get_db() as conn:
        row = conn.execute(
            "SELECT minutes_total, minutes_flowers, minutes_packaging, items_without_norm "
            "  FROM courier_orders WHERE retailcrm_order_id = 1").fetchone()
    check("минуты записаны при синке: 7 × 0,5 + лента 5 = 8,5",
          row["minutes_total"] == 8.5, f"получено {dict(row)}")
    check("разбор сохранён отдельно",
          row["minutes_flowers"] == 3.5 and row["minutes_packaging"] == 5.0,
          f"получено {dict(row)}")

    # Правка нормы пересчитывает задним числом
    storage.set_time_norm("offer", 10, role="catalog", minutes=4, basis="unit", username="tester")
    storage.recalc_minutes_range(DAY, DAY)
    with storage.get_db() as conn:
        total = conn.execute("SELECT minutes_total FROM courier_orders "
                             "WHERE retailcrm_order_id = 1").fetchone()["minutes_total"]
    check("смена роли пересчитывает витрину: 7 × 4 = 28", total == 28.0, f"получено {total}")


def main():
    test_reference_order()
    test_ranges()
    test_mono_vs_mix()
    test_over_last_range()
    test_catalog_basis()
    test_berry_mode()
    test_without_norm()
    test_broken_tariffs()
    test_storage_roundtrip()

    print()
    if failures:
        print(f"ПРОВАЛЕНО: {len(failures)} — {', '.join(failures)}")
        sys.exit(1)
    print("Все проверки пройдены.")


if __name__ == "__main__":
    main()
