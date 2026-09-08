"""
Сторож каталога номенклатуры (Ф1 плана «нагрузка в минутах»).

Проверяет то, что ломается молча:
  - пустой ответ CRM НЕ перезаписывает каталог: одна неудачная синхронизация
    иначе обнуляет единицы измерения и группы, а следом — нагрузку по всей
    сети (К2 критики);
  - глубина группы считается верно, даже если родитель пришёл в ответе ПОСЛЕ
    ребёнка: от глубины зависит, чья норма выиграет (К3);
  - битая ссылка на родителя и цикл не роняют синк — иначе кривой справочник
    оставит нас вообще без каталога;
  - пересборка убирает устаревшие связи: товар сменил группу, а старая связь
    осталась бы навсегда;
  - единица измерения доезжает от оффера до базы (`pc` против `g`) — ради неё
    всё и затевалось.

ВАЖНО: прогон читает боевой .env, поэтому сеть глушится до импорта.

Запуск: python scripts/test_catalog_sync.py
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

TMP = tempfile.mkdtemp(prefix="catalog_sync_")
os.environ["COURIERS_DB_PATH"] = os.path.join(TMP, "couriers.db")

from couriers import retailcrm, storage  # noqa: E402

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"  OK   {name}")
    else:
        print(f"  FAIL {name}{': ' + detail if detail else ''}")
        failures.append(name)


GROUPS = [
    # Ребёнок идёт ПЕРЕД родителем — так и приходит из CRM
    {"id": 5821, "parent_id": 5802, "name": "Клубничные букеты"},
    {"id": 5802, "parent_id": None, "name": "Клубника в шоколаде"},
    {"id": 5869, "parent_id": 5857, "name": "Цветы"},
    {"id": 5857, "parent_id": None, "name": "Товары МС"},
]

PRODUCTS = [
    {
        "id": 60830, "article": "f11", "name": "Роза одноголовая красная",
        "groups": [{"id": 5869}, {"id": 5857}],
        "offers": [{"id": 55698, "article": "f11", "name": "Роза одноголовая красная",
                    "unit": {"code": "pc", "name": "Штука", "sym": "шт."}}],
    },
    {
        "id": 60840, "article": "k1", "name": "Клубника",
        "groups": [{"id": 5861}],   # группы нет в справочнике — не должно падать
        "offers": [{"id": 55700, "article": "k1", "name": "Клубника",
                    "unit": {"code": "g", "name": "Грамм", "sym": "г"}}],
    },
    {
        "id": 60850, "article": "m25", "name": "Малиновый шоколад",
        "groups": [{"id": 5821}, {"id": 5802}],
        "offers": [{"id": 55928, "article": None, "name": None,
                    "unit": {"code": "pc"}}],
    },
]


def test_parse():
    print("\n1. Разбор страницы каталога")
    offers, links = retailcrm.parse_catalog_page(PRODUCTS)
    by_id = {o["offer_id"]: o for o in offers}

    check("офферы собраны", len(offers) == 3, f"получено {len(offers)}")
    check("единица измерения доехала: штука",
          by_id[55698]["unit_code"] == "pc", f"получено {by_id[55698]}")
    check("единица измерения доехала: грамм",
          by_id[55700]["unit_code"] == "g", f"получено {by_id[55700]}")
    check("артикул берётся с товара, если у оффера пуст",
          by_id[55928]["article"] == "m25", f"получено {by_id[55928]['article']}")
    check("название берётся с товара, если у оффера пусто",
          by_id[55928]["name"] == "Малиновый шоколад", f"получено {by_id[55928]['name']}")
    check("связи с группами собраны", len(links) == 5, f"получено {len(links)}")


def test_depth():
    print("\n2. Глубина группы")
    depths = storage._group_depths(GROUPS)
    check("родитель после ребёнка не ломает глубину",
          depths[5821] == 1 and depths[5802] == 0, f"получено {depths}")
    check("вторая ветка посчитана",
          depths[5869] == 1 and depths[5857] == 0, f"получено {depths}")

    broken = [{"id": 1, "parent_id": 999, "name": "Сирота"}]
    check("ссылка на несуществующего родителя не роняет расчёт",
          storage._group_depths(broken) == {1: 0}, "исключение или неверная глубина")

    cycle = [{"id": 1, "parent_id": 2, "name": "А"}, {"id": 2, "parent_id": 1, "name": "Б"}]
    result = storage._group_depths(cycle)
    check("цикл в дереве не зацикливает расчёт", result == {1: 1, 2: 1}, f"получено {result}")


def test_replace():
    print("\n3. Запись каталога")
    storage.init_couriers_tables()
    offers, links = retailcrm.parse_catalog_page(PRODUCTS)
    result = storage.replace_catalog(GROUPS, offers, links)
    check("каталог записан",
          result == {"groups": 4, "offers": 3, "links": 5}, f"получено {result}")

    units = storage.offer_units()
    check("единицы измерения читаются из базы",
          units.get(55698) == "pc" and units.get(55700) == "g", f"получено {units}")

    snapshot = storage.catalog_snapshot()
    check("в диагностике виден весовой товар",
          snapshot["offers_weighted"] == 1, f"получено {snapshot}")
    with storage.get_db() as conn:
        depth = conn.execute(
            "SELECT depth FROM crm_product_groups WHERE id = 5821").fetchone()["depth"]
    check("глубина дочерней группы записана", depth == 1, f"получено {depth}")


def test_empty_does_not_wipe():
    print("\n4. Пустой ответ не стирает каталог")
    before = storage.catalog_snapshot()

    for groups, offers, label in (
        ([], [{"offer_id": 1}], "пустые группы"),
        (GROUPS, [], "пустые офферы"),
        ([], [], "пустой ответ целиком"),
    ):
        try:
            storage.replace_catalog(groups, offers, [])
            check(f"{label}: отклонено", False, "исключения не было")
        except storage.EmptyCatalogError:
            check(f"{label}: отклонено", True)

    after = storage.catalog_snapshot()
    check("каталог остался прежним",
          after["offers"] == before["offers"] and after["groups"] == before["groups"],
          f"было {before}, стало {after}")


def test_rebuild_drops_stale():
    print("\n5. Пересборка убирает устаревшие связи")
    # Товар ушёл из группы «Клубничные букеты» в «Цветы»
    moved = [dict(PRODUCTS[2], groups=[{"id": 5869}])]
    offers, links = retailcrm.parse_catalog_page(PRODUCTS[:2] + moved)
    storage.replace_catalog(GROUPS, offers, links)

    with storage.get_db() as conn:
        rows = [r["group_id"] for r in conn.execute(
            "SELECT group_id FROM crm_offer_groups WHERE offer_id = 55928")]
    check("старая связь исчезла", 5821 not in rows, f"получено {rows}")
    check("новая связь на месте", 5869 in rows, f"получено {rows}")


def main():
    test_parse()
    test_depth()
    test_replace()
    test_empty_does_not_wipe()
    test_rebuild_drops_stale()

    print()
    if failures:
        print(f"ПРОВАЛЕНО: {len(failures)} — {', '.join(failures)}")
        sys.exit(1)
    print("Все проверки пройдены.")


if __name__ == "__main__":
    main()
