"""
Время сборки заказа в минутах (Ф3 плана «нагрузка в минутах»).

Здесь только арифметика: ни одного обращения к базе и ни одного запроса
наружу. Так сделано по двум причинам. Первая — этот код читают при каждом
споре «почему в ячейке 48 минут», и он должен читаться сверху вниз без
переходов в хранилище. Вторая — модуль `salonload` уже импортирует
`couriers`, и расчёт, живущий в `salonload`, замкнул бы импорты в кольцо.

Формула (решения владельца 2026-09-07):

    время заказа = цветы + упаковка цветов + клубника + упаковка клубники
                 + готовые товары

    цветы            — количество × тариф по диапазону и виду (моно/микс)
    упаковка цветов  — фиксированная по тому же диапазону: лента или упаковка
    клубника         — граммы / 100 × тариф
    упаковка клубники— фиксированная, зависит от режима (букет/коробочка)
    готовые товары   — норма товара × количество либо один раз за позицию

Что НЕ считается временем: позиции с ролью `none` (открытки, топперы,
позиции-шапки сборного букета) и позиции без нормы. Последние отдельно
пересчитываются в `without_norm` — заказ с ними посчитан не полностью, и это
обязано быть видно, а не спрятано в округлении.
"""

from typing import Any, Dict, List, Optional

# Роли берутся из storage, чтобы не разошлись two источника правды.
from .storage import (BASIS_LINE, BERRY_BOUQUET, ROLE_BERRY, ROLE_CATALOG,
                      ROLE_FLOWER, ROLE_PACKAGING)


class TariffError(ValueError):
    """
    Тарифная сетка непригодна для расчёта. Считать по ней нельзя.

    Наследуется от ValueError намеренно: битый тариф — это неверное значение,
    и ручки, которые и так возвращают 400 на ValueError, отвечают внятно без
    отдельной ветки.
    """


def validate_flower_tariffs(rows: List[Dict[str, Any]]) -> None:
    """
    Проверить сетку на дыры и пересечения.

    Сетка лежит в базе и правится человеком (К8 критики). Дыра в ней не
    выглядит как ошибка: заказ на 20 цветов просто получит ноль минут, и
    загрузка окажется занижена молча. Поэтому расчёт по битой сетке
    прекращается с внятной ошибкой, а не «по возможности».
    """
    if not rows:
        raise TariffError("Тарифная сетка по цветам пуста")

    ordered = sorted(rows, key=lambda r: r["range_from"])
    if ordered[0]["range_from"] != 1:
        raise TariffError(f"Сетка начинается не с одного цветка, а с {ordered[0]['range_from']}")

    for previous, current in zip(ordered, ordered[1:]):
        if previous["range_to"] < previous["range_from"]:
            raise TariffError(
                f"Диапазон {previous['range_from']}–{previous['range_to']} вывернут")
        if current["range_from"] <= previous["range_to"]:
            raise TariffError(
                f"Диапазоны пересекаются: {previous['range_from']}–{previous['range_to']} "
                f"и {current['range_from']}–{current['range_to']}")
        if current["range_from"] != previous["range_to"] + 1:
            raise TariffError(
                f"Дыра в сетке между {previous['range_to']} и {current['range_from']}")


def _flower_row(rows: List[Dict[str, Any]], count: int) -> Optional[Dict[str, Any]]:
    """
    Строка тарифа для такого количества цветов.

    Количество больше последнего диапазона — берётся последняя строка.
    Экстраполировать нельзя: 500 цветов это не букет, а ошибка ввода, и
    выдуманный для неё тариф был бы хуже крайнего известного.
    """
    if count <= 0:
        return None
    ordered = sorted(rows, key=lambda r: r["range_from"])
    for row in ordered:
        if row["range_from"] <= count <= row["range_to"]:
            return row
    return ordered[-1]


def order_minutes(items: List[Dict[str, Any]], norms: Dict[int, Dict[str, Any]],
                  flower_tariffs: List[Dict[str, Any]],
                  berry_tariffs: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """
    Минуты сборки одного заказа с разбором по составляющим.

    items — позиции заказа: `offer_id` и `quantity`.
    norms — эффективные нормы (см. storage.resolve_offer_norms).

    Разбор возвращается целиком, а не одной суммой: вопрос «почему столько»
    возникает при каждом споре, и отвечать на него повторным расчётом в голове
    — потерянное время.
    """
    validate_flower_tariffs(flower_tariffs)

    flower_count = 0.0
    flower_kinds = set()
    berry_grams = 0.0
    berry_modes = []
    has_packaging = False
    catalog_minutes = 0.0
    without_norm = 0

    for item in items:
        offer_id = item["offer_id"]
        quantity = float(item.get("quantity") or 0)
        norm = norms.get(offer_id)

        if norm is None:
            without_norm += 1
            continue

        role = norm.get("role")
        # Режим клубники объявляется позицией-шапкой («Сборная коробка»),
        # у которой своей нагрузки нет. Поэтому его собираем до разбора ролей.
        if norm.get("berry_mode"):
            berry_modes.append((offer_id, norm["berry_mode"]))

        if role == ROLE_FLOWER:
            flower_count += quantity
            flower_kinds.add(offer_id)
        elif role == ROLE_BERRY:
            berry_grams += quantity
        elif role == ROLE_PACKAGING:
            has_packaging = True
        elif role == ROLE_CATALOG:
            minutes = float(norm.get("minutes") or 0)
            # «За позицию» — время один раз, сколько бы в строке ни было.
            # Без этого готовый набор, заведённый в граммах, умножился бы на
            # свои же граммы: «Секрет Бархата» — 267 заказов, и ошибка была бы
            # в сотни раз.
            catalog_minutes += minutes if norm.get("basis") == BASIS_LINE else minutes * quantity

    flowers = 0.0
    packaging = 0.0
    row = _flower_row(flower_tariffs, int(flower_count))
    if row is not None:
        # Микс — когда в заказе больше одной номенклатуры цветов. Из одного-двух
        # цветков микса не бывает, поэтому там всегда монотариф (mix_minutes у
        # этого диапазона пуст).
        rate = row["mono_minutes"]
        if len(flower_kinds) > 1 and row.get("mix_minutes") is not None:
            rate = row["mix_minutes"]
        flowers = flower_count * rate
        packaging = row["package_minutes"] if has_packaging else row["ribbon_minutes"]

    berries = 0.0
    if berry_grams > 0:
        # При нескольких шапках берём наименьший offer_id — детерминированно,
        # а не «как повезёт с порядком позиций». Режим не объявлен — букет:
        # коробочку заводят отдельной позицией осознанно.
        mode = min(berry_modes)[1] if berry_modes else BERRY_BOUQUET
        tariff = berry_tariffs.get(mode) or berry_tariffs.get(BERRY_BOUQUET)
        if tariff is None:
            raise TariffError(f"Нет тарифа для режима клубники «{mode}»")
        berries = berry_grams / 100.0 * tariff["minutes_per_100g"] + tariff["package_minutes"]

    total = flowers + packaging + berries + catalog_minutes
    return {
        "total": round(total, 2),
        "flowers": round(flowers, 2),
        "packaging": round(packaging, 2),
        "berries": round(berries, 2),
        "catalog": round(catalog_minutes, 2),
        "without_norm": without_norm,
        "flower_count": flower_count,
        "flower_kinds": len(flower_kinds),
        "berry_grams": berry_grams,
    }
