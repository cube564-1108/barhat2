"""
Сторож справочника норм времени (Ф2 плана «нагрузка в минутах»).

Проверяет то, что ломается молча:
  - конфликт групп разрешается детерминированно: товар состоит в среднем в 25
    группах, и размеченных среди них бывает несколько. Побеждает бóльшая
    глубина, при равной — меньший id. Без второго правила одно и то же число
    объяснялось бы по-разному в разные дни (К4 критики);
  - норма товара перебивает норму любой его группы;
  - «нормы нет» — это отсутствие строки, а не ноль минут: иначе «не размечено»
    и «размечено как бесплатное» сольются, и счётчик занижения замолчит;
  - медиана количества считается по факту и отличает весовой компонент от
    готового набора с `unit = g` (девять таких товаров в живом каталоге);
  - покрытие считается в ЗАКАЗАХ, а не в товарах.

ВАЖНО: прогон читает боевой .env, поэтому сеть глушится до импорта.

Запуск: python scripts/test_time_norms.py
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

TMP = tempfile.mkdtemp(prefix="time_norms_")
os.environ["COURIERS_DB_PATH"] = os.path.join(TMP, "couriers.db")

from couriers import retailcrm, storage  # noqa: E402

failures = []
DAY = "2026-09-20"


def check(name, condition, detail=""):
    if condition:
        print(f"  OK   {name}")
    else:
        print(f"  FAIL {name}{': ' + detail if detail else ''}")
        failures.append(name)


# Дерево: «Клубника в шоколаде» (корень) → «Клубничные букеты» (глубина 1).
# Плюс две группы ОДНОЙ глубины на один товар — спорный случай.
GROUPS = [
    {"id": 5802, "parent_id": None, "name": "Клубника в шоколаде"},
    {"id": 5821, "parent_id": 5802, "name": "Клубничные букеты"},
    {"id": 5869, "parent_id": 5857, "name": "Цветы"},
    {"id": 5857, "parent_id": None, "name": "Товары МС"},
    {"id": 5876, "parent_id": 5813, "name": "Монобукеты"},
    {"id": 5813, "parent_id": None, "name": "Цветочные букеты"},
]

PRODUCTS = [
    {   # состоит и в родителе, и в дочерней — победить должна дочерняя
        "id": 1, "article": "m25", "name": "Малиновый шоколад",
        "groups": [{"id": 5802}, {"id": 5821}],
        "offers": [{"id": 100, "unit": {"code": "pc"}}],
    },
    {   # две группы ОДНОЙ глубины (5821 и 5876) — победить должна меньшая по id
        "id": 2, "article": "x1", "name": "Комбо-набор",
        "groups": [{"id": 5876}, {"id": 5821}],
        "offers": [{"id": 200, "unit": {"code": "pc"}}],
    },
    {   # весовой компонент
        "id": 3, "article": "k1", "name": "Клубника",
        "groups": [{"id": 5869}],
        "offers": [{"id": 300, "unit": {"code": "g"}}],
    },
    {   # unit=g, но заказывают штуками — готовый набор
        "id": 4, "article": "1169", "name": "Секрет Бархата",
        "groups": [{"id": 5821}],
        "offers": [{"id": 400, "unit": {"code": "g"}}],
    },
]


def setup():
    storage.init_couriers_tables()
    offers, links = retailcrm.parse_catalog_page(PRODUCTS)
    storage.replace_catalog(GROUPS, offers, links)

    storage.upsert_order_statuses([
        {"code": "at-work", "name": "В работе", "group_code": "new", "active": True},
    ])

    def order(order_id, items):
        return {
            "id": order_id, "number": str(order_id), "site": "s", "status": "at-work",
            "summ": 1000, "shipmentStore": "s",
            "delivery": {"date": DAY, "code": "dostavka-kurerom"},
            "customFields": {"order_availability_time": "12:00"},
            "items": items,
        }

    def item(offer_id, qty):
        return {"quantity": qty, "offer": {"id": offer_id, "displayName": f"Товар {offer_id}"}}

    rows = [
        retailcrm.parse_order(order(1, [item(100, 1), item(300, 500)]), {}),
        retailcrm.parse_order(order(2, [item(200, 1)]), {}),
        # «Секрет Бархата»: unit=g, а количество всегда 1
        retailcrm.parse_order(order(3, [item(400, 1)]), {}),
        retailcrm.parse_order(order(4, [item(400, 1), item(300, 60)]), {}),
        retailcrm.parse_order(order(5, [item(300, 26)]), {}),
    ]
    storage.replace_orders_window(DAY, DAY, rows)


def test_resolution():
    print("\n1. Чья норма выигрывает")
    storage.set_time_norm("group", 5802, role="catalog", minutes=20, username="tester")
    storage.set_time_norm("group", 5821, role="catalog", minutes=12, username="tester")
    storage.set_time_norm("group", 5876, role="catalog", minutes=8, username="tester")

    norms = storage.resolve_offer_norms()
    check("дочерняя группа точнее родительской",
          norms[100]["minutes"] == 12 and norms[100]["source_id"] == 5821,
          f"получено {norms.get(100)}")
    check("при равной глубине побеждает меньший id",
          norms[200]["source_id"] == 5821, f"получено {norms.get(200)}")
    check("видно, откуда взялось время",
          norms[100]["source"] == "group" and norms[100]["source_name"] == "Клубничные букеты",
          f"получено {norms.get(100)}")

    storage.set_time_norm("offer", 100, role="catalog", minutes=30, username="tester")
    norms = storage.resolve_offer_norms()
    check("норма товара перебивает групповую",
          norms[100]["minutes"] == 30 and norms[100]["source"] == "offer",
          f"получено {norms.get(100)}")

    storage.set_time_norm("offer", 100, role=None)
    norms = storage.resolve_offer_norms()
    check("снятие товарной нормы возвращает групповую",
          norms[100]["minutes"] == 12 and norms[100]["source"] == "group",
          f"получено {norms.get(100)}")


def test_zero_is_not_missing():
    print("\n2. Ноль минут — это не «нет нормы»")
    storage.set_time_norm("offer", 200, role="none", minutes=0, username="tester")
    norms = storage.resolve_offer_norms()
    check("роль «не создаёт нагрузки» сохранена",
          norms[200]["role"] == "none" and norms[200]["minutes"] == 0,
          f"получено {norms.get(200)}")

    missing = storage.norm_catalog(DAY, DAY, only_missing=True)
    check("товар с нулём не считается неразмеченным",
          all(row["offer_id"] != 200 for row in missing),
          f"получено {[r['offer_id'] for r in missing]}")


def test_validation():
    print("\n3. Проверки значений")
    for args, label in (
        (dict(role="catalog", minutes=None), "готовый товар без времени"),
        (dict(role="catalog", minutes=-5), "отрицательное время"),
        (dict(role="выдумка", minutes=1), "неизвестная роль"),
        (dict(role="catalog", minutes=1, basis="кг"), "неизвестная база"),
        (dict(role="berry", berry_mode="ведро"), "неизвестный режим клубники"),
    ):
        try:
            storage.set_time_norm("offer", 999, username="tester", **args)
            check(f"{label} отклоняется", False, "исключения не было")
        except ValueError:
            check(f"{label} отклоняется", True)

    try:
        storage.set_time_norm("никуда", 1, role="catalog", minutes=1)
        check("неизвестная область отклоняется", False, "исключения не было")
    except ValueError:
        check("неизвестная область отклоняется", True)


def test_facts():
    print("\n4. Факты о товаре отличают компонент от готового набора")
    rows = {row["offer_id"]: row for row in storage.norm_catalog(DAY, DAY)}

    check("медиана количества у весового компонента — десятки",
          rows[300]["median_quantity"] == 60, f"получено {rows[300]}")
    check("у готового набора с unit=g медиана равна 1",
          rows[400]["median_quantity"] == 1 and rows[400]["unit_code"] == "g",
          f"получено {rows[400]}")
    check("единица измерения из каталога доехала",
          rows[100]["unit_code"] == "pc", f"получено {rows[100]}")
    check("число заказов посчитано", rows[300]["orders"] == 3, f"получено {rows[300]}")


def test_coverage():
    print("\n5. Покрытие считается в заказах")
    # Норма есть у 100 (группа), 200 (товар), 400 (группа 5821). Нет у 300.
    coverage = storage.norms_coverage(DAY, DAY)
    check("заказы с неразмеченными позициями посчитаны",
          coverage["orders_incomplete"] == 3, f"получено {coverage}")
    check("всего заказов посчитано", coverage["orders"] == 5, f"получено {coverage}")
    check("доля занижения считается",
          coverage["share"] == 60.0, f"получено {coverage}")
    check("неразмеченных товаров ровно один",
          coverage["offers_without_norm"] == 1, f"получено {coverage}")

    storage.set_time_norm("offer", 300, role="berry", username="tester")
    after = storage.norms_coverage(DAY, DAY)
    check("после разметки счётчик обнуляется",
          after["orders_incomplete"] == 0 and after["share"] == 0.0, f"получено {after}")


def main():
    setup()
    test_resolution()
    test_zero_is_not_missing()
    test_validation()
    test_facts()
    test_coverage()

    print()
    if failures:
        print(f"ПРОВАЛЕНО: {len(failures)} — {', '.join(failures)}")
        sys.exit(1)
    print("Все проверки пройдены.")


if __name__ == "__main__":
    main()
