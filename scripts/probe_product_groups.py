"""
Разведка: чем связать товар из заказа с группой номенклатуры и в чём меряется
его количество.

Только чтение. Ни один запрос ничего не меняет.

Вопросы, на которые нужен ответ до проектирования модели «нагрузка в минутах»:

  0.1 Справочник групп товаров RetailCRM — есть ли нужные категории.
  0.2 Что приходит у товара и у оффера (ключи, структура `groups`, `unit`).
  0.3 Единица измерения у оффера: отличается ли штука от граммов на данных.
  0.4 Дойти от `offer.id` из позиции заказа до групп товара.
  0.5 Сколько групп у одного товара — годится ли группа как ОДИН признак типа.

Запуск: python scripts/probe_product_groups.py
"""

import json
import os
import sys
from collections import Counter
from datetime import date, timedelta

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))
os.chdir(REPO)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(REPO, ".env"))

from couriers import retailcrm  # noqa: E402

PAGE = 100  # RetailCRM принимает только 20, 50 или 100


def show(title):
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def main():
    client = retailcrm.get_client()

    show("0.1 Справочник групп товаров")
    group_names = {}
    try:
        data = client._get("api/v5/store/product-groups", {"limit": PAGE})
        groups = data.get("productGroup") or []
        for g in groups:
            group_names[g.get("id")] = (g.get("name"), g.get("parentId"))
        print(f"групп: {len(groups)} (всего {(data.get('pagination') or {}).get('totalCount')})")
        interesting = [g for g in groups
                       if any(w in (g.get("name") or "").lower()
                              for w in ("букет", "клубник", "цвет", "роз", "композ", "бокс",
                                        "коробк", "упаковк", "лент", "открыт", "топпер"))]
        print(f"похожих на нужные категории: {len(interesting)}")
        for g in interesting:
            parent = group_names.get(g.get("parentId"), ("—",))[0]
            print(f"   id={str(g.get('id')):<7} родитель={str(parent)[:22]:<24} {g.get('name')}")
    except Exception as e:
        print(f"ОШИБКА: {e}")

    show("0.2 Товар и оффер: структура")
    products = []
    try:
        data = client._get("api/v5/store/products", {"limit": PAGE})
        products = data.get("products") or []
        total = (data.get("pagination") or {}).get("totalCount")
        print(f"товаров на странице: {len(products)}, всего: {total}")
        if products:
            sample = products[0]
            print(f"ключи товара: {sorted(sample.keys())}")
            print(f"пример groups[0]: {json.dumps((sample.get('groups') or [{}])[0], ensure_ascii=False)}")
            offers = sample.get("offers") or []
            if offers:
                print(f"ключи оффера: {sorted(offers[0].keys())}")
                print(f"пример unit:  {json.dumps(offers[0].get('unit'), ensure_ascii=False)}")
    except Exception as e:
        print(f"ОШИБКА: {e}")

    show("0.3 Единица измерения у офферов")
    units = Counter()
    unit_examples = {}
    pages = 0
    try:
        page_num = 1
        while pages < 5:
            data = client._get("api/v5/store/products", {"limit": PAGE, "page": page_num})
            batch = data.get("products") or []
            if not batch:
                break
            for p in batch:
                for o in (p.get("offers") or []):
                    unit = o.get("unit") or {}
                    code = unit.get("code") or unit.get("sym") or "—"
                    units[code] += 1
                    unit_examples.setdefault(code, []).append(str(p.get("name"))[:38])
            pages += 1
            page_num += 1
            if page_num > ((data.get("pagination") or {}).get("totalPageCount") or 1):
                break
        print(f"просмотрено страниц: {pages}")
        for code, n in units.most_common():
            print(f"   {code:<10} {n:5}  например: {', '.join(unit_examples[code][:3])}")
    except Exception as e:
        print(f"ОШИБКА: {e}")

    show("0.4 От позиции заказа до групп товара")
    try:
        today = date.today().isoformat()
        week_ago = (date.today() - timedelta(days=7)).isoformat()
        page = next(iter(client.iter_orders_by_delivery_date(week_ago, today)), [])
        seen = {}
        for order in page:
            for item in (order.get("items") or []):
                offer = item.get("offer") or {}
                if offer.get("id") and offer["id"] not in seen:
                    seen[offer["id"]] = (offer.get("article"),
                                         offer.get("displayName") or offer.get("name"),
                                         item.get("quantity"))
        print(f"заказов: {len(page)}, уникальных offer.id: {len(seen)}")

        for offer_id, (article, name, qty) in list(seen.items())[:10]:
            res = client._get("api/v5/store/products",
                              {"limit": 20, "filter[offerIds][]": offer_id})
            found = res.get("products") or []
            names = []
            for p in found:
                for g in (p.get("groups") or []):
                    gid = g.get("id") if isinstance(g, dict) else g
                    names.append(group_names.get(gid, (str(gid),))[0])
            print(f"   offer={offer_id:<7} кол-во={str(qty):<7} {str(name)[:32]:<34} "
                  f"групп: {len(names)} → {names[:6]}")
    except Exception as e:
        print(f"ОШИБКА: {e}")

    show("0.5 Сколько групп у одного товара")
    if products:
        counts = [len(p.get("groups") or []) for p in products]
        counts.sort()
        print(f"   минимум {counts[0]}, медиана {counts[len(counts)//2]}, максимум {counts[-1]}")
        print("   → если у товара групп много, группа не может быть ЕДИНСТВЕННЫМ признаком типа")


if __name__ == "__main__":
    main()
