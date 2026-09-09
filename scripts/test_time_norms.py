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
    print("\n1. Норма задаётся по товару")
    # Групповые нормы отменены владельцем 2026-09-09: в одной группе CRM лежат
    # товары с сильно разным временем сборки, и общая норма давала
    # правдоподобное, но неверное число.
    storage.set_time_norm("offer", 100, role="catalog", minutes=30, username="tester")
    norms = storage.resolve_offer_norms()
    check("норма товара сохранена",
          norms[100]["minutes"] == 30 and norms[100]["source"] == "offer",
          f"получено {norms.get(100)}")
    check("соседний товар норму не унаследовал", 200 not in norms,
          f"получено {sorted(norms)}")

    storage.set_time_norm("offer", 100, role=None)
    norms = storage.resolve_offer_norms()
    check("снятие нормы убирает товар из размеченных", 100 not in norms,
          f"получено {sorted(norms)}")


def test_group_norms_expanded():
    print("\n1-бис. Старые групповые нормы разворачиваются в товарные")
    # Ставим норму группе напрямую (интерфейса для этого больше нет) и
    # проверяем, что миграция переносит её на товары, а не теряет.
    with storage.get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO load_time_norms "
                     "(scope, scope_id, role, minutes, basis) VALUES ('group', 5821, 'catalog', 12, 'unit')")
        conn.execute("INSERT OR REPLACE INTO load_time_norms "
                     "(scope, scope_id, role, minutes, basis) VALUES ('group', 5802, 'catalog', 20, 'unit')")
        conn.execute("DELETE FROM sync_state WHERE key = ?", (storage.GROUP_NORMS_EXPANDED_KEY,))
    # У товара 400 есть своя норма — миграция не должна её перебить
    storage.set_time_norm("offer", 400, role="catalog", minutes=7, username="tester")

    storage._expand_group_norms()
    norms = storage.resolve_offer_norms()
    check("товар получил норму своей группы (глубокая важнее родительской)",
          norms.get(100, {}).get("minutes") == 12, f"получено {norms.get(100)}")
    check("собственная норма товара не перебита",
          norms[400]["minutes"] == 7, f"получено {norms.get(400)}")
    with storage.get_db() as conn:
        left = conn.execute("SELECT COUNT(*) c FROM load_time_norms WHERE scope='group'").fetchone()["c"]
    check("групповых норм в базе не осталось", left == 0, f"получено {left}")

    storage._expand_group_norms()
    check("повторный запуск ничего не ломает",
          storage.resolve_offer_norms()[400]["minutes"] == 7)

    for offer_id in (100, 200, 400):
        storage.set_time_norm("offer", offer_id, role=None)


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
    # Нормы задаются по товарам. Размечаем всё, кроме 300 (клубника):
    # заказы 1, 4 и 5 содержат её и должны считаться неполными.
    storage.set_time_norm("offer", 100, role="catalog", minutes=12, username="tester")
    storage.set_time_norm("offer", 400, role="catalog", minutes=7, username="tester")
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


def test_filters():
    print("\n6. Фильтры по полям списка")
    rows = lambda **kw: {r["offer_id"] for r in storage.norm_catalog(DAY, DAY, **kw)}

    check("фильтр по единице измерения", rows(unit_code="g") == {300, 400},
          f"получено {rows(unit_code='g')}")
    check("фильтр по роли", rows(role="berry") == {300}, f"получено {rows(role='berry')}")
    check("фильтр по числу заказов", rows(min_orders=3) == {300},
          f"получено {rows(min_orders=3)}")
    check("фильтр по медиане количества", rows(min_median=10) == {300},
          f"получено {rows(min_median=10)}")
    check("фильтр по верхней границе количества", 300 not in rows(max_median=5),
          f"получено {rows(max_median=5)}")
    check("поиск по артикулу работает", rows(search="k1") == {300},
          f"получено {rows(search='k1')}")
    check("фильтры складываются", rows(unit_code="g", max_median=5) == {400},
          f"получено {rows(unit_code='g', max_median=5)}")

    check("выгрузка не режется отсечкой",
          len(storage.norm_catalog(DAY, DAY, all_rows=True, limit=1)) == 4,
          "all_rows не отменил limit")


def test_bulk_import():
    print("\n7. Импорт норм пачкой")
    result = storage.set_time_norms_bulk([
        {"offer_id": 100, "role": "catalog", "minutes": "15,5", "basis": "line"},
        {"offer_id": 300, "role": "berry"},
        {"offer_id": 400, "role": ""},                       # пустая роль снимает норму
        {"offer_id": 200, "role": "выдумка"},                # ошибка, но не рушит импорт
        {"offer_id": 999, "role": "catalog"},                # готовый товар без времени
        {"role": "catalog", "minutes": 5},                   # строка без ключа
    ], "tester")

    check("применённые строки посчитаны", result["applied"] == 2, f"получено {result}")
    check("снятые нормы посчитаны", result["cleared"] == 1, f"получено {result}")
    check("ошибочные строки собраны списком", len(result["errors"]) == 3,
          f"получено {result['errors']}")

    norms = storage.resolve_offer_norms()
    check("запятая как разделитель разобрана (Excel в русской локали)",
          norms[100]["minutes"] == 15.5, f"получено {norms.get(100)}")
    check("база начисления применена", norms[100]["basis"] == "line",
          f"получено {norms.get(100)}")
    check("пустая роль сняла норму", 400 not in norms, f"получено {sorted(norms)}")
    check("строка с неизвестной ролью не применилась",
          norms.get(200, {}).get("role") != "выдумка", f"получено {norms.get(200)}")


def main():
    setup()
    test_resolution()
    test_group_norms_expanded()
    test_zero_is_not_missing()
    test_validation()
    test_facts()
    test_coverage()
    test_filters()
    test_bulk_import()

    print()
    if failures:
        print(f"ПРОВАЛЕНО: {len(failures)} — {', '.join(failures)}")
        sys.exit(1)
    print("Все проверки пройдены.")


if __name__ == "__main__":
    main()
