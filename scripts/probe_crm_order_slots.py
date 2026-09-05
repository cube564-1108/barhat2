"""
Разведка структуры заказов RetailCRM для модуля «Загрузка салонов» (Фаза 0).

ТОЛЬКО ЧТЕНИЕ: скрипт делает исключительно GET-запросы, ничего не пишет ни в
CRM, ни в наши базы.

Отвечает на вопросы плана plans/2026-09-04-загрузка-салонов-слоты.md:
  0.3 — где лежит время готовности (delivery.time.* / адрес / customFields);
  0.4 — в каком часовом поясе оно приходит;
  0.5 — как называется склад-исполнитель и на какой доле заказов он заполнен;
  0.6 — что в позициях заказа и каким ключом цеплять справочник весов;
  0.7 — объём: заказов в сутки, позиций в заказе.

ПРИВАТНОСТЬ (К1 из критики): в теле заказа лежат ФИО, телефон и адрес клиента.
Поэтому:
  - сырой JSON пишется в TMP_DIR (по умолчанию c:/tmp), НИКОГДА не в репозиторий;
  - в консоль полные значения печатаются только для полей из WHITELIST,
    для остальных — путь, тип и доля заполненности, без значений.

Запуск: python scripts/probe_crm_order_slots.py
"""

import json
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))

from dotenv import load_dotenv

load_dotenv(os.path.join(REPO, ".env"))

import requests

from russian_ca import trust_russian_ca

RETAILCRM_URL = os.environ.get("RETAILCRM_URL")
RETAILCRM_API_KEY = os.environ.get("RETAILCRM_API_KEY")

# Выгрузка сырых ответов — вне репозитория. См. К1.
TMP_DIR = os.environ.get("PROBE_TMP_DIR", "c:/tmp")

TIMEOUT = 30
PAGE_LIMIT = 100          # RetailCRM принимает только 20/50/100
PAGE_PAUSE = 0.2
MAX_PAGES = 40            # потолок вежливости: 4000 заказов на окно

# Поля, которые можно печатать значениями: в них нет персональных данных.
WHITELIST_PREFIXES = (
    "status", "site", "orderMethod", "orderType", "shipmentDate", "shipped",
    "delivery.code", "delivery.date", "delivery.time", "delivery.service",
    "delivery.cost", "delivery.netCost", "delivery.address.region",
    "delivery.address.city", "shipmentStore", "store", "warehouse",
    "items.offer", "items.quantity", "items.productName", "items.externalIds",
    "customFields",
)

# Ключи, которые ищем отдельно: в них может прятаться время готовности.
TIME_HINTS = ("time", "hour", "interval", "ready", "готов", "срок")
STORE_HINTS = ("store", "warehouse", "склад", "shop")


def get(endpoint, params=None):
    url = f"{RETAILCRM_URL.rstrip('/')}/{endpoint.lstrip('/')}"
    response = SESSION.get(url, params=params, timeout=TIMEOUT)
    if not response.ok:
        raise SystemExit(f"CRM {response.status_code} на {endpoint}: {response.text[:500]}")
    data = response.json()
    if not data.get("success", True):
        raise SystemExit(f"CRM отклонил запрос {endpoint}: {data.get('errorMsg')}")
    return data, response.headers.get("Date")


def iter_orders(date_from, date_to, extra_params=None):
    """Страницы заказов по дате ДОСТАВКИ, без фильтра по статусу."""
    page = 1
    while page <= MAX_PAGES:
        params = {
            "filter[deliveryDateFrom]": date_from,
            "filter[deliveryDateTo]": date_to,
            "limit": PAGE_LIMIT,
            "page": page,
        }
        if extra_params:
            params.update(extra_params)
        data, _ = get("api/v5/orders", params)
        orders = data.get("orders", [])
        if not orders:
            return
        yield orders
        if len(orders) < PAGE_LIMIT:
            return
        page += 1
        time.sleep(PAGE_PAUSE)


def walk(node, prefix=""):
    """Развернуть вложенный словарь в плоские пути: delivery.time.from и т.п."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from walk(value, f"{prefix}.{key}" if prefix else key)
    elif isinstance(node, list):
        # у списков схлопываем индекс: items[0].offer.id → items.offer.id
        for item in node:
            yield from walk(item, prefix)
    else:
        yield prefix, node


def printable(path):
    return any(path.startswith(p) for p in WHITELIST_PREFIXES)


def collect(orders, stats):
    for order in orders:
        stats["total"] += 1
        stats["by_status"][order.get("status")] += 1
        stats["by_site"][order.get("site")] += 1
        stats["by_delivery_code"][(order.get("delivery") or {}).get("code")] += 1

        delivery = order.get("delivery") or {}
        if delivery.get("date"):
            stats["with_delivery_date"] += 1
            stats["by_date"][delivery["date"]] += 1
        time_block = delivery.get("time") or {}
        if time_block:
            stats["with_delivery_time"] += 1
            for key, value in time_block.items():
                stats["time_keys"][key] += 1
                if len(stats["time_samples"][key]) < 8:
                    stats["time_samples"][key].append(value)

        items = order.get("items") or []
        stats["items_total"] += len(items)
        stats["items_per_order"].append(len(items))
        for item in items:
            offer = item.get("offer") or {}
            for key in ("id", "externalId", "xmlId", "article"):
                if offer.get(key) not in (None, ""):
                    stats["offer_keys"][key] += 1
            if len(stats["offer_samples"]) < 5 and offer:
                stats["offer_samples"].append({
                    k: offer.get(k) for k in ("id", "externalId", "xmlId", "article", "displayName")
                })

        for path, value in walk(order):
            filled = value not in (None, "", [], {})
            stats["paths"][path] += 1 if filled else 0
            stats["paths_seen"][path] += 1
            if filled and printable(path) and len(stats["samples"][path]) < 5:
                if value not in stats["samples"][path]:
                    stats["samples"][path].append(value)


def new_stats():
    return {
        "total": 0,
        "with_delivery_date": 0,
        "with_delivery_time": 0,
        "items_total": 0,
        "items_per_order": [],
        "by_status": Counter(),
        "by_site": Counter(),
        "by_delivery_code": Counter(),
        "by_date": Counter(),
        "time_keys": Counter(),
        "time_samples": defaultdict(list),
        "offer_keys": Counter(),
        "offer_samples": [],
        "paths": Counter(),
        "paths_seen": Counter(),
        "samples": defaultdict(list),
    }


def share(part, whole):
    return f"{part}/{whole} ({100.0 * part / whole:.0f}%)" if whole else "нет данных"


def report(title, stats):
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")
    print(f"заказов: {stats['total']}")
    if not stats["total"]:
        return
    print(f"с датой доставки: {share(stats['with_delivery_date'], stats['total'])}")
    print(f"с блоком delivery.time: {share(stats['with_delivery_time'], stats['total'])}")

    print("\n-- ключи внутри delivery.time (вопрос 0.3)")
    for key, count in stats["time_keys"].most_common():
        print(f"   {key:<12} {share(count, stats['total']):<20} примеры: {stats['time_samples'][key][:5]}")
    if not stats["time_keys"]:
        print("   блока нет ни у одного заказа")

    print("\n-- поля, похожие на время готовности, вне delivery.time")
    for path, filled in sorted(stats["paths"].items()):
        low = path.lower()
        if path.startswith("delivery.time"):
            continue
        if any(hint in low for hint in TIME_HINTS) and filled:
            value = stats["samples"][path][:3] if printable(path) else "<не печатаем: могут быть перс. данные>"
            print(f"   {path:<40} {share(filled, stats['total']):<18} {value}")

    print("\n-- поля склада/магазина (вопрос 0.5)")
    found = False
    for path, filled in sorted(stats["paths"].items()):
        if any(hint in path.lower() for hint in STORE_HINTS):
            found = True
            value = stats["samples"][path][:5] if printable(path) else "<скрыто>"
            print(f"   {path:<40} {share(filled, stats['total']):<18} {value}")
    if not found:
        print("   ни одного поля со словом store/warehouse не найдено")

    print("\n-- позиции заказа (вопрос 0.6)")
    print(f"   позиций всего: {stats['items_total']}")
    per_order = stats["items_per_order"] or [0]
    print(f"   позиций в заказе: среднее {sum(per_order) / len(per_order):.1f}, максимум {max(per_order)}")
    for key, count in stats["offer_keys"].most_common():
        print(f"   offer.{key:<12} заполнен у {share(count, stats['items_total'])}")
    for sample in stats["offer_samples"]:
        print(f"   пример offer: {sample}")

    print("\n-- статусы (вопрос: что считать нагрузкой, К6)")
    for status, count in stats["by_status"].most_common(15):
        print(f"   {str(status):<28} {count}")

    print("\n-- типы доставки")
    for code, count in stats["by_delivery_code"].most_common(10):
        print(f"   {str(code):<28} {count}")

    print("\n-- сайты (для сверки со справочником салонов)")
    for site, count in stats["by_site"].most_common(20):
        print(f"   {str(site):<32} {count}")

    if stats["by_date"]:
        days = len(stats["by_date"])
        total = sum(stats["by_date"].values())
        peak_date, peak = stats["by_date"].most_common(1)[0]
        print(f"\n-- объём (вопрос 0.7): {total} заказов за {days} дней, "
              f"в среднем {total / days:.1f}/день, пик {peak} ({peak_date})")


def dump_raw(name, payload):
    os.makedirs(TMP_DIR, exist_ok=True)
    path = os.path.join(TMP_DIR, f"crm_probe_{name}_{datetime.now():%Y%m%d_%H%M%S}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"сырой ответ сохранён: {path}")
    return path


def detail():
    """
    Три вопроса, на которые общий обзор ответа не даёт (запуск: --detail).

    1. Что такое customFields.order_availability_time и как оно соотносится с
       delivery.time.from — это два разных времени или одно и то же.
    2. В каком поясе приходит createdAt: сверяем максимум по свежим заказам с
       истинным UTC из заголовка Date.
    3. Местное ли время у слота: гистограмма часов по салонам. Если у салонов
       из UTC+5 и UTC+7 рабочее окно совпадает — время местное (стенные часы
       салона). Если сдвинуто ровно на 2 часа — оно приведено к одному поясу.
    """
    today = date.today()
    past_from = (today - timedelta(days=14)).isoformat()
    past_to = today.isoformat()

    hours_by_store = defaultdict(Counter)
    same = diff = 0
    diff_samples = []
    non_time = Counter()
    availability_only = 0
    delivery_only = 0
    max_created = ""
    total = 0

    for page in iter_orders(past_from, past_to):
        for order in page:
            total += 1
            created = order.get("createdAt") or ""
            max_created = max(max_created, created)

            delivery = order.get("delivery") or {}
            time_block = delivery.get("time") or {}
            dfrom = (time_block.get("from") or "").strip()
            avail = ((order.get("customFields") or {}).get("order_availability_time") or "").strip()

            if avail and not _is_time(avail):
                non_time[avail] += 1
            if avail and not dfrom:
                availability_only += 1
            if dfrom and not avail:
                delivery_only += 1
            if avail and dfrom:
                if avail == dfrom:
                    same += 1
                else:
                    diff += 1
                    if len(diff_samples) < 15:
                        diff_samples.append((order.get("shipmentStore"), delivery.get("code"), dfrom, avail))

            store = order.get("shipmentStore")
            source = avail if _is_time(avail) else dfrom
            if store and _is_time(source):
                hours_by_store[store][int(source.split(":")[0])] += 1

    print(f"\n{'=' * 72}\nДЕТАЛИЗАЦИЯ по {total} заказам за {past_from} — {past_to}\n{'=' * 72}")

    print("\n### 1. order_availability_time против delivery.time.from")
    print(f"   совпадают:            {same}")
    print(f"   различаются:          {diff}")
    print(f"   только availability:  {availability_only}")
    print(f"   только delivery.time: {delivery_only}")
    print("   примеры расхождений (склад, тип доставки, delivery.from, availability):")
    for sample in diff_samples:
        print(f"      {sample}")
    print("   значения availability, не похожие на время:")
    for value, count in non_time.most_common(10):
        print(f"      {value!r}: {count}")

    print("\n### 2. Часовой пояс createdAt")
    _, http_date = get("api/v5/orders", {"limit": 20, "page": 1})
    print(f"   истинный UTC (заголовок Date): {http_date}")
    print(f"   максимальный createdAt:        {max_created}")
    print("   (заказы создаются постоянно, поэтому максимум должен быть в пределах")
    print("    минут от текущего времени в том поясе, в котором CRM его отдаёт)")

    print("\n### 3. Час готовности по салонам — местное время или один пояс")
    print("   салон                            9  10  11  12  13  14  15  16  17  18  19  20  21  22")
    for store, hours in sorted(hours_by_store.items(), key=lambda kv: -sum(kv[1].values())):
        if sum(hours.values()) < 20:
            continue
        row = "".join(f"{hours.get(h, 0):>4}" for h in range(9, 23))
        print(f"   {store:<30}{row}")
    print("   Салоны UTC+5 (ЕКБ, Челябинск) против UTC+7 (НСК, Томск, Барнаул):")
    print("   совпадающее окно = время местное; сдвиг на 2 часа = приведено к одному поясу.")


def _is_time(value):
    if not value or len(value) < 4 or ":" not in value:
        return False
    head = value.split(":")[0]
    return head.isdigit() and 0 <= int(head) <= 23


def main():
    if not RETAILCRM_URL or not RETAILCRM_API_KEY:
        raise SystemExit("RETAILCRM_URL / RETAILCRM_API_KEY не заданы в .env")

    if "--detail" in sys.argv:
        detail()
        return

    print(f"CRM: {RETAILCRM_URL}")
    print(f"выгрузка сырых ответов: {TMP_DIR} (в репозиторий не пишем — К1)")

    # ---- справочники -----------------------------------------------------
    print("\n### Справочники")
    for endpoint, key in (
        ("api/v5/reference/stores", "stores"),
        ("api/v5/reference/sites", "sites"),
        ("api/v5/reference/statuses", "statuses"),
        ("api/v5/reference/delivery-types", "deliveryTypes"),
    ):
        try:
            data, http_date = get(endpoint, None)
        except SystemExit as e:
            print(f"{key}: {e}")
            continue
        block = data.get(key) or {}
        # часть справочников CRM отдаёт словарём {code: {...}}, часть — списком
        pairs = list(block.items()) if isinstance(block, dict) else [
            ((item.get("code") if isinstance(item, dict) else item), item) for item in block
        ]
        print(f"\n{key}: {len(pairs)} записей")
        for code, item in pairs[:40]:
            name = item.get("name") if isinstance(item, dict) else item
            extra = ""
            if key == "statuses" and isinstance(item, dict):
                extra = f"  group={item.get('group')} ordering={item.get('ordering')}"
            print(f"   {str(code):<34} {str(name)[:40]}{extra}")
        dump_raw(key, data)

    # ---- часовой пояс: сверяем createdAt с HTTP Date ----------------------
    print("\n### Часовой пояс (вопрос 0.4)")
    today = date.today()
    data, http_date = get("api/v5/orders", {
        "filter[deliveryDateFrom]": today.isoformat(),
        "filter[deliveryDateTo]": (today + timedelta(days=1)).isoformat(),
        "limit": 20, "page": 1,
    })
    print(f"HTTP Date от CRM (истинный UTC): {http_date}")
    print(f"локальные часы этой машины:      {datetime.now():%Y-%m-%d %H:%M:%S}")
    for order in (data.get("orders") or [])[:10]:
        delivery = order.get("delivery") or {}
        print(f"   site={str(order.get('site'))[:28]:<28} createdAt={order.get('createdAt')} "
              f"delivery.date={delivery.get('date')} delivery.time={delivery.get('time')}")
    dump_raw("orders_today", data)

    # ---- будущие заказы --------------------------------------------------
    future_from = today.isoformat()
    future_to = (today + timedelta(days=60)).isoformat()
    future = new_stats()
    raw_future = []
    for page in iter_orders(future_from, future_to):
        collect(page, future)
        if len(raw_future) < 200:
            raw_future.extend(page)
    report(f"БУДУЩИЕ ЗАКАЗЫ {future_from} — {future_to} (все статусы)", future)
    dump_raw("orders_future", raw_future)

    # ---- прошлый месяц: доли заполненности на объёме (К2) ----------------
    past_from = (today - timedelta(days=30)).isoformat()
    past_to = (today - timedelta(days=1)).isoformat()
    past = new_stats()
    for page in iter_orders(past_from, past_to):
        collect(page, past)
    report(f"ПРОШЛЫЕ 30 ДНЕЙ {past_from} — {past_to} (все статусы)", past)

    print("\n### Итог для плана")
    print("Перенести в plans/2026-09-04-загрузка-салонов-слоты.md таблицу")
    print("«поле → путь в JSON → пример → доля заполненности» и вычеркнуть вопросы 1–3.")


if __name__ == "__main__":
    SESSION = requests.Session()
    SESSION.headers.update({"X-API-KEY": RETAILCRM_API_KEY or ""})
    trust_russian_ca(SESSION)
    main()
