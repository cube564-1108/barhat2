"""
Разведка RetailCRM для модуля «Курьеры: доставка заказов» (Фаза 0).

ТОЛЬКО ЧТЕНИЕ: скрипт делает исключительно GET-запросы, ничего не пишет ни в
CRM, ни в наши базы.

Отвечает на вопросы Фазы 0 плана plans/2026-09-08-курьеры-доставка.md:
  1 — где лежат получатель, клиент, полный адрес, окно доставки, комментарии;
  2 — доля заказов с пустым получателем (от неё зависит текст «клиент/получатель»);
  3 — коды статусов: «Передан флористу», «Заказ готов», «Курьер забрал», «Доставлен»;
  4 — насколько дисциплинированно ставят «Заказ готов» и за сколько до доставки;
  5 — работает ли orders/history с sinceId и виден ли источник изменения (защита от эха);
  6 — есть ли картинки у товаров в каталоге CRM;
  7 — отдаёт ли CRM координаты адреса (тогда геокодер не нужен).

ПРИВАТНОСТЬ: в теле заказа лежат ФИО, телефон и адрес клиента. Поэтому:
  - сырой JSON пишется в TMP_DIR (по умолчанию c:/tmp), НИКОГДА не в репозиторий;
  - в консоль значения печатаются только для полей из WHITELIST_PREFIXES;
  - для персональных полей печатается «форма» значения (длина, маска телефона),
    а не само значение: этого хватает, чтобы понять формат поля.

Запуск:
    python scripts/probe_crm_courier_fields.py           # всё сразу
    python scripts/probe_crm_courier_fields.py --days 7  # другое окно
"""

import argparse
import json
import os
import re
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

# Выгрузка сырых ответов — вне репозитория.
TMP_DIR = os.environ.get("PROBE_TMP_DIR", "c:/tmp")

TIMEOUT = 30
PAGE_LIMIT = 100          # RetailCRM принимает только 20/50/100
PAGE_PAUSE = 0.2          # правила API: не чаще 10 запросов/сек с одного IP
MAX_ORDER_PAGES = 20      # потолок вежливости: 2000 заказов
MAX_HISTORY_PAGES = 30    # 3000 записей истории

# Поля, которые можно печатать значениями: персональных данных в них нет.
WHITELIST_PREFIXES = (
    "status", "site", "orderMethod", "orderType", "shipmentStore", "shipped",
    "delivery.code", "delivery.date", "delivery.time", "delivery.service",
    "delivery.cost", "delivery.netCost", "delivery.address.region",
    "delivery.address.city", "delivery.address.countryIso",
    "items.offer.id", "items.offer.article", "items.quantity",
    # Кастомные поля — ТОЧЕЧНО, а не блоком «customFields».
    # Первый прогон 2026-09-08 напечатал в консоль имя и телефон получателя:
    # в customFields лежат recipient_name и recipient_phone, а блок целиком
    # считался безопасным. Персональные поля печатаются только «формой».
    "customFields.order_availability_time",
    "customFields.recipient_customer",
)

# Что ищем: контакты получателя и заказчика, адрес, комментарии, координаты.
RECIPIENT_HINTS = ("recipient", "получат", "contact", "customer", "firstname",
                   "lastname", "patronymic", "phone", "email")
ADDRESS_HINTS = ("address", "адрес", "street", "building", "flat", "house",
                 "floor", "entrance", "index", "notes", "text")
COMMENT_HINTS = ("comment", "коммент", "note", "приме")
GEO_HINTS = ("lat", "lon", "lng", "coord", "geo", "point")

# Названия статусов, которые нас интересуют (только для подсказки человеку —
# в коде модуля маппинг будет по КОДУ из справочника, а не по названию).
STATUS_HINTS = {
    "передан флористу": "видимый: заказ появляется у курьера",
    "готов": "признак готовности букета",
    "курьер": "забрал / в пути",
    "достав": "доставлен",
    "самовывоз": "не для курьера, проверить что не попадёт в ленту",
}

PHONE_RE = re.compile(r"\d")

SESSION = requests.Session()
SESSION.headers.update({"X-API-KEY": RETAILCRM_API_KEY or ""})
SESSION.trust_env = False
trust_russian_ca(SESSION)


# ----------------------------------------------------------------------------
# Транспорт
# ----------------------------------------------------------------------------

def get(endpoint, params=None):
    if not RETAILCRM_URL or not RETAILCRM_API_KEY:
        raise SystemExit("Не задан RETAILCRM_URL / RETAILCRM_API_KEY в .env")
    url = f"{RETAILCRM_URL.rstrip('/')}/{endpoint.lstrip('/')}"
    response = SESSION.get(url, params=params, timeout=TIMEOUT)
    if not response.ok:
        raise SystemExit(f"CRM {response.status_code} на {endpoint}: {response.text[:500]}")
    data = response.json()
    if not data.get("success", True):
        raise SystemExit(f"CRM отклонил запрос {endpoint}: {data.get('errorMsg')}")
    return data


def iter_orders(date_from, date_to, max_pages=MAX_ORDER_PAGES):
    """Страницы заказов по дате ДОСТАВКИ, без фильтра по статусу."""
    page = 1
    while page <= max_pages:
        data = get("api/v5/orders", {
            "filter[deliveryDateFrom]": date_from,
            "filter[deliveryDateTo]": date_to,
            "limit": PAGE_LIMIT,
            "page": page,
        })
        orders = data.get("orders", [])
        if not orders:
            return
        yield orders
        if len(orders) < PAGE_LIMIT:
            return
        page += 1
        time.sleep(PAGE_PAUSE)


def iter_history(params, max_pages=MAX_HISTORY_PAGES):
    """Страницы истории изменений заказов."""
    page = 1
    while page <= max_pages:
        query = dict(params)
        query.update({"limit": PAGE_LIMIT, "page": page})
        data = get("api/v5/orders/history", query)
        records = data.get("history", [])
        if not records:
            return
        yield records
        if len(records) < PAGE_LIMIT:
            return
        page += 1
        time.sleep(PAGE_PAUSE)


# ----------------------------------------------------------------------------
# Приватность и вывод
# ----------------------------------------------------------------------------

def printable(path):
    return any(path.startswith(prefix) for prefix in WHITELIST_PREFIXES)


def shape(value):
    """«Форма» значения вместо самого значения: формат виден, ПДн — нет."""
    if value is None:
        return "None"
    if isinstance(value, bool):
        return f"bool:{value}"
    if isinstance(value, (int, float)):
        return f"число (порядок {len(str(abs(int(value))))} знаков)"
    text = str(value)
    digits = len(PHONE_RE.findall(text))
    if digits >= 10 and digits >= len(text.replace(" ", "")) - 4:
        return f"телефон: {digits} цифр, начинается с {text.strip()[:2]}…"
    return f"строка: {len(text)} симв., слов {len(text.split())}"


def show(path, value):
    return repr(value) if printable(path) else shape(value)


def walk(node, prefix=""):
    """Развернуть вложенный словарь в плоские пути: delivery.address.city и т.п."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from walk(value, f"{prefix}.{key}" if prefix else key)
    elif isinstance(node, list):
        for item in node:
            yield from walk(item, prefix)
    else:
        yield prefix, node


def share(part, whole):
    return f"{part}/{whole} ({100.0 * part / whole:.0f}%)" if whole else "нет данных"


def dump_raw(name, payload):
    os.makedirs(TMP_DIR, exist_ok=True)
    path = os.path.join(TMP_DIR, f"courier_probe_{name}_{datetime.now():%Y%m%d_%H%M%S}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"   сырой ответ сохранён: {path}")
    return path


def header(title):
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


# ----------------------------------------------------------------------------
# 1-2, 7. Поля заказа: получатель, клиент, адрес, комментарии, координаты
# ----------------------------------------------------------------------------

def probe_order_fields(days):
    today = date.today()
    date_from = (today - timedelta(days=days)).isoformat()
    date_to = (today + timedelta(days=2)).isoformat()

    paths_filled = Counter()
    paths_seen = Counter()
    samples = defaultdict(list)
    total = 0
    delivery_orders = 0
    recipient_filled = 0
    customer_filled = 0
    both_empty = 0
    time_window = Counter()
    first_order_dump = None

    for page in iter_orders(date_from, date_to):
        if first_order_dump is None and page:
            first_order_dump = page[0]
        for order in page:
            total += 1
            delivery = order.get("delivery") or {}
            code = delivery.get("code")
            is_delivery = code not in ("self-delivery", "3", None)
            if is_delivery:
                delivery_orders += 1

            for path, value in walk(order):
                paths_seen[path] += 1
                if value not in (None, "", [], {}):
                    paths_filled[path] += 1
                    if len(samples[path]) < 4:
                        rendered = show(path, value)
                        if rendered not in samples[path]:
                            samples[path].append(rendered)

            # Получатель против заказчика — вопрос 2.
            # Кандидаты на «получателя» ищем широко: точное имя поля выяснится
            # по выводу, здесь нам важна доля пустых.
            flat = dict(walk(order))
            recipient_keys = [k for k in flat
                              if "recipient" in k.lower() or "получат" in k.lower()]
            has_recipient = any(flat.get(k) not in (None, "", [], {}) for k in recipient_keys)
            has_customer = any(
                flat.get(k) not in (None, "", [], {})
                for k in ("customer.firstName", "firstName", "customer.phones", "phone")
            )
            if is_delivery:
                if has_recipient:
                    recipient_filled += 1
                if has_customer:
                    customer_filled += 1
                if not has_recipient and not has_customer:
                    both_empty += 1

            time_block = delivery.get("time") or {}
            if time_block:
                key = tuple(sorted(k for k, v in time_block.items() if v))
                time_window[key] += 1

    header(f"1-2, 7. ПОЛЯ ЗАКАЗА  ({total} заказов, доставка {date_from} — {date_to})")
    print(f"заказов всего: {total}, из них с доставкой (не самовывоз): {delivery_orders}")

    def section(title, hints, exclude=()):
        print(f"\n-- {title}")
        rows = []
        for path, filled in sorted(paths_filled.items()):
            low = path.lower()
            if any(x in low for x in exclude):
                continue
            if any(hint in low for hint in hints):
                rows.append((path, filled))
        if not rows:
            print("   ничего не найдено")
            return
        for path, filled in rows:
            print(f"   {path:<44} {share(filled, total):<18} {samples[path][:2]}")

    section("ПОЛУЧАТЕЛЬ и ЗАКАЗЧИК (вопрос 1)", RECIPIENT_HINTS)
    section("АДРЕС ДОСТАВКИ (вопрос 1)", ADDRESS_HINTS)
    section("КОММЕНТАРИИ (вопрос 1)", COMMENT_HINTS)
    section("КООРДИНАТЫ (вопрос 7)", GEO_HINTS)

    print("\n-- кастомные поля заказа (могут прятать получателя)")
    for path, filled in sorted(paths_filled.items()):
        if path.startswith("customFields"):
            print(f"   {path:<44} {share(filled, total):<18} {samples[path][:2]}")

    print("\n-- окно доставки: какие ключи delivery.time заполнены")
    for keys, count in time_window.most_common(10):
        print(f"   {str(keys):<44} {share(count, delivery_orders)}")

    print(f"\n-- вопрос 2: чем заполнять карточку курьера (только доставочные заказы)")
    print(f"   есть поля получателя: {share(recipient_filled, delivery_orders)}")
    print(f"   есть контакты клиента: {share(customer_filled, delivery_orders)}")
    print(f"   пусто и то и другое:   {share(both_empty, delivery_orders)}")

    if first_order_dump is not None:
        dump_raw("order_sample", first_order_dump)

    return total


# ----------------------------------------------------------------------------
# 3. Справочник статусов
# ----------------------------------------------------------------------------

def probe_statuses():
    data = get("api/v5/reference/statuses")
    statuses = data.get("statuses") or {}
    groups = get("api/v5/reference/status-groups").get("statusGroups") or {}

    header(f"3. СТАТУСЫ ЗАКАЗА ({len(statuses)} шт.)")
    by_group = defaultdict(list)
    for code, item in statuses.items():
        by_group[item.get("group")].append((code, item))

    for group, rows in sorted(by_group.items(), key=lambda kv: str(kv[0])):
        group_name = (groups.get(group) or {}).get("name", group)
        print(f"\n-- группа {group} ({group_name})")
        for code, item in sorted(rows, key=lambda r: r[1].get("ordering") or 0):
            active = "" if item.get("active", True) else "  [неактивен]"
            print(f"   {code:<34} {item.get('name')}{active}")

    print("\n-- кандидаты под действия модуля (по названию, для человека)")
    for hint, purpose in STATUS_HINTS.items():
        found = [(code, item.get("name"), item.get("group"))
                 for code, item in statuses.items()
                 if hint in (item.get("name") or "").lower()]
        print(f"\n   «{hint}» → {purpose}")
        if not found:
            print("      НЕ НАЙДЕНО — статус придётся завести в CRM")
        for code, name, group in found:
            print(f"      {code:<32} {name}   (группа {group})")

    dump_raw("statuses", statuses)
    return statuses


# ----------------------------------------------------------------------------
# 4-5. История изменений: sinceId, источник, дисциплина статуса «Заказ готов»
# ----------------------------------------------------------------------------

def status_code_of(value):
    """newValue у статуса приходит объектом {code, name} или строкой."""
    if isinstance(value, dict):
        return value.get("code") or value.get("name")
    return value


def probe_history(statuses, days):
    today = date.today()
    start = (today - timedelta(days=days)).strftime("%Y-%m-%d 00:00:00")
    end = today.strftime("%Y-%m-%d 23:59:59")

    header(f"4-5. ИСТОРИЯ ИЗМЕНЕНИЙ  (окно {start} — {end})")

    fields = Counter()
    sources = Counter()
    source_paths = Counter()
    status_moves = Counter()
    status_time = defaultdict(list)     # код статуса → время записи по заказам
    orders_touched = set()
    total_records = 0
    last_id = 0
    first_record = None

    for page in iter_history({"filter[startDate]": start, "filter[endDate]": end}):
        if first_record is None and page:
            first_record = page[0]
        for record in page:
            total_records += 1
            last_id = max(last_id, record.get("id") or 0)
            fields[record.get("field")] += 1

            # Откуда изменение: без этого нельзя отфильтровать эхо своих же
            # записей и мы получим бесконечный цикл.
            for key in ("source", "user", "apiKey", "manager", "trigger"):
                if record.get(key) not in (None, "", {}):
                    source_paths[key] += 1
            sources[record.get("source")] += 1

            order = record.get("order") or {}
            if order.get("id"):
                orders_touched.add(order["id"])

            if record.get("field") == "status":
                code = status_code_of(record.get("newValue"))
                status_moves[code] += 1
                status_time[code].append((order.get("id"), record.get("createdAt")))

    print(f"записей: {total_records}, затронуто заказов: {len(orders_touched)}")
    print(f"максимальный id записи (курсор): {last_id}")

    print("\n-- какие поля меняются чаще всего")
    for field, count in fields.most_common(15):
        print(f"   {str(field):<32} {count}")

    print("\n-- источник изменения (вопрос 5: чем отличать свои записи)")
    for key, count in source_paths.most_common():
        print(f"   поле {key:<28} присутствует у {share(count, total_records)}")
    for source, count in sources.most_common(10):
        print(f"   source={str(source):<26} {count}")

    print("\n-- переходы по статусам за окно")
    for code, count in status_moves.most_common(20):
        name = (statuses.get(code) or {}).get("name", "?")
        print(f"   {str(code):<34} {count:<6} {name}")

    # --- проверка курсора sinceId
    print("\n-- вопрос 5: работает ли filter[sinceId]")
    if last_id:
        probe_since = max(last_id - 200, 0)
        data = get("api/v5/orders/history",
                   {"filter[sinceId]": probe_since, "limit": PAGE_LIMIT, "page": 1})
        records = data.get("history", [])
        ids = [r.get("id") for r in records]
        print(f"   sinceId={probe_since} → получено {len(records)} записей, "
              f"диапазон id {min(ids) if ids else '-'}…{max(ids) if ids else '-'}")
        print(f"   пагинация: {data.get('pagination')}")
        ok = all(i > probe_since for i in ids if i is not None)
        print(f"   все id строго больше курсора: {'да' if ok else 'НЕТ — разобраться'}")
    else:
        print("   истории за окно нет, курсор проверить не на чем")

    if first_record is not None:
        dump_raw("history_sample", first_record)

    return status_moves, status_time


# ----------------------------------------------------------------------------
# 4. Дисциплина статуса «Заказ готов»
# ----------------------------------------------------------------------------

def probe_ready_discipline(statuses, status_time, days):
    """
    Какая доля доставочных заказов реально проходит через «Заказ готов»
    и за сколько минут до доставки его ставят.

    От этого зависит решение плана: блокировать кнопку «Забрал» у неготового
    заказа или только предупреждать. Статус, который ставят через раз, хуже
    отсутствия статуса — ему всё равно поверят.
    """
    ready_codes = [code for code, item in statuses.items()
                   if "готов" in (item.get("name") or "").lower()]

    header("4. ДИСЦИПЛИНА СТАТУСА «ЗАКАЗ ГОТОВ»")
    if not ready_codes:
        print("статус с «готов» в названии не найден — раздел пропущен")
        return
    for code in ready_codes:
        print(f"   найден статус: {code} — {statuses[code].get('name')}")

    ready_orders = {}
    for code in ready_codes:
        for order_id, created_at in status_time.get(code, []):
            if order_id and created_at:
                ready_orders.setdefault(order_id, created_at)

    today = date.today()
    date_from = (today - timedelta(days=days)).isoformat()
    date_to = today.isoformat()

    total = with_ready = 0
    lead_minutes = []
    for page in iter_orders(date_from, date_to):
        for order in page:
            delivery = order.get("delivery") or {}
            if delivery.get("code") in ("self-delivery", "3", None):
                continue
            total += 1
            marked_at = ready_orders.get(order.get("id"))
            if not marked_at:
                continue
            with_ready += 1

            # За сколько до доставки поставили готовность.
            # ВНИМАНИЕ: createdAt истории и время доставки живут в разных
            # шкалах (см. CLAUDE.md про пояс RetailCRM — величина сдвига под
            # вопросом). Поэтому цифра ниже — порядок величины, не факт.
            time_from = (delivery.get("time") or {}).get("from")
            if delivery.get("date") and time_from and re.match(r"^\d{1,2}:\d{2}", str(time_from)):
                try:
                    planned = datetime.strptime(
                        f"{delivery['date']} {str(time_from)[:5]}", "%Y-%m-%d %H:%M")
                    marked = datetime.strptime(marked_at[:19], "%Y-%m-%d %H:%M:%S")
                    lead_minutes.append((planned - marked).total_seconds() / 60.0)
                except ValueError:
                    pass

    print(f"\n   доставочных заказов за {date_from}—{date_to}: {total}")
    print(f"   из них помечены «готов»: {share(with_ready, total)}")
    if lead_minutes:
        lead_minutes.sort()
        def pct(p):
            return lead_minutes[min(int(len(lead_minutes) * p), len(lead_minutes) - 1)]
        print(f"   за сколько минут до доставки ставят готовность "
              f"(медиана {pct(0.5):.0f}, p10 {pct(0.1):.0f}, p90 {pct(0.9):.0f})")
        print("   ВНИМАНИЕ: сдвиг часового пояса между историей и временем доставки "
              "не выверен — цифра показывает порядок, а не точность")
    print("\n   → если доля заметно ниже 90%, кнопку «Забрал» у неготового заказа "
          "блокировать нельзя, только предупреждать (см. §4 плана)")


# ----------------------------------------------------------------------------
# 6. Картинки товаров
# ----------------------------------------------------------------------------

def probe_product_images(days):
    header("6. КАРТИНКИ ТОВАРОВ В КАТАЛОГЕ CRM")

    # Собираем offer.id из недавних заказов — проверяем именно те товары,
    # которые реально ездят курьерам, а не весь каталог.
    today = date.today()
    offer_ids = []
    for page in iter_orders((today - timedelta(days=3)).isoformat(), today.isoformat(),
                            max_pages=3):
        for order in page:
            for item in order.get("items") or []:
                offer_id = (item.get("offer") or {}).get("id")
                if offer_id and offer_id not in offer_ids:
                    offer_ids.append(offer_id)
        if len(offer_ids) >= 60:
            break
    offer_ids = offer_ids[:60]
    print(f"   проверяем {len(offer_ids)} товаров из недавних заказов")
    if not offer_ids:
        print("   позиций не нашлось — раздел пропущен")
        return

    data = get("api/v5/store/products", {
        "filter[offerIds][]": offer_ids,
        "limit": PAGE_LIMIT,
        "page": 1,
    })
    products = data.get("products", [])
    print(f"   каталог вернул {len(products)} карточек")

    with_image = 0
    image_paths = Counter()
    sample_url = None
    for product in products:
        flat = dict(walk(product))
        image_keys = [k for k in flat if "image" in k.lower() or "picture" in k.lower()]
        filled = [k for k in image_keys if flat.get(k) not in (None, "", [], {})]
        for key in filled:
            image_paths[key] += 1
        if filled:
            with_image += 1
            if sample_url is None:
                sample_url = flat[filled[0]]

    print(f"   с картинкой: {share(with_image, len(products))}")
    for path, count in image_paths.most_common():
        print(f"      {path:<40} {count}")
    if not sample_url:
        print("   → картинок в каталоге CRM нет, фото придётся брать из МойСклада")
    else:
        # Ссылки товаров из ICML Битрикса битые примерно у 30%, поэтому одной
        # проверки мало: заполненность поля и работоспособность ссылки — разные
        # вещи, и «фото есть у 98%» ничего не стоит, если треть из них 404.
        urls = []
        for product in products:
            flat = dict(walk(product))
            for key in ("imageUrl",):
                value = flat.get(key)
                if value and value not in urls:
                    urls.append(str(value))
        urls = urls[:30]
        print(f"   проверяем HEAD'ом {len(urls)} ссылок")
        codes = Counter()
        sizes = []
        for url in urls:
            try:
                head = requests.head(url, timeout=10, allow_redirects=True)
                codes[head.status_code] += 1
                if head.status_code == 200:
                    length = head.headers.get("Content-Length")
                    if length and length.isdigit():
                        sizes.append(int(length))
            except requests.RequestException as e:
                codes[type(e).__name__] += 1
            time.sleep(0.05)
        for code, count in codes.most_common():
            print(f"      {str(code):<24} {share(count, len(urls))}")
        if sizes:
            sizes.sort()
            print(f"      вес картинки: медиана {sizes[len(sizes) // 2] / 1024:.0f} КБ, "
                  f"максимум {max(sizes) / 1024:.0f} КБ")
            print("      (курьер сидит на мобильном интернете — если медиана в сотнях КБ, "
                  "нужна миниатюра, а не оригинал)")

    if products:
        dump_raw("product_sample", products[0])


# ----------------------------------------------------------------------------
# 8. Дыры, вскрывшиеся при первом прогоне
# ----------------------------------------------------------------------------

def probe_gaps(day):
    """
    Три вопроса, без ответа на которые нельзя верить цифрам выше:

    1. В каком поясе createdAt у истории. Без этого «готовность ставят за
       5 минут до доставки» — не факт, а разница поясов: время доставки в CRM
       это стенные часы салона (UTC+5/+7), а история может приходить в
       московском или аккаунтном времени.
    2. Где адрес у тех заказов, где delivery.address.text пуст (41% — слишком
       много, чтобы списать на ошибку оператора).
    3. Как отличить наши собственные записи в истории: поле apiKey пришло
       булевым, значит по нему ключ не опознать.
    """
    header("8. ДЫРЫ ПЕРВОГО ПРОГОНА")

    # --- 1. пояс истории
    print("-- пояс createdAt в истории")
    url = f"{RETAILCRM_URL.rstrip('/')}/api/v5/orders/history"
    response = SESSION.get(url, params={"limit": 100, "page": 1}, timeout=TIMEOUT)
    http_date = response.headers.get("Date")
    records = response.json().get("history", [])
    stamps = [r.get("createdAt") for r in records if r.get("createdAt")]
    print(f"   истинный UTC (заголовок Date):  {http_date}")
    print(f"   максимальный createdAt истории: {max(stamps) if stamps else '-'}")
    print("   (записи идут потоком, поэтому разница ≈ сдвиг пояса истории)")

    # То же по заказам: createdAt заказа и время готовности из кастомного поля
    orders_page = get("api/v5/orders", {"limit": 20, "page": 1}).get("orders", [])
    created = [o.get("createdAt") for o in orders_page if o.get("createdAt")]
    print(f"   максимальный createdAt заказа:  {max(created) if created else '-'}")

    # --- 2. заказы без адреса
    print(f"\n-- где адрес у доставочных заказов без delivery.address.text ({day})")
    no_text = 0
    total = 0
    by_code = Counter()
    by_site = Counter()
    other_fields = Counter()
    for page in iter_orders(day, day, max_pages=10):
        for order in page:
            delivery = order.get("delivery") or {}
            code = delivery.get("code")
            if code in ("self-delivery", "3", None):
                continue
            total += 1
            address = delivery.get("address") or {}
            if address.get("text"):
                continue
            no_text += 1
            by_code[code] += 1
            by_site[order.get("site")] += 1
            for key, value in address.items():
                if value not in (None, "", [], {}):
                    other_fields[key] += 1
            for key in ("customer.address.text", "contact.address.text"):
                flat = dict(walk(order))
                if flat.get(key):
                    other_fields[key] += 1
    print(f"   доставочных заказов: {total}, из них без delivery.address.text: "
          f"{share(no_text, total)}")
    print("   типы доставки у таких заказов:")
    for code, count in by_code.most_common(10):
        print(f"      {str(code):<32} {count}")
    print("   салоны/сайты у таких заказов:")
    for site, count in by_site.most_common(10):
        print(f"      {str(site):<32} {count}")
    print("   какие поля адреса у них всё-таки заполнены:")
    for key, count in other_fields.most_common(12):
        print(f"      {key:<32} {count}")

    # --- 3. структура записи истории
    print("\n-- ключи одной записи истории (значения не печатаем)")
    if records:
        sample = records[-1]
        for key, value in sample.items():
            kind = type(value).__name__
            if isinstance(value, dict):
                kind = f"dict{sorted(value.keys())[:8]}"
            print(f"   {key:<20} {kind}")
        dump_raw("history_record_keys", {k: str(type(v)) for k, v in sample.items()})


# ----------------------------------------------------------------------------
# 9. Уточнения после второго прогона
# ----------------------------------------------------------------------------

# Пояса салонов по коду сайта. UTC+7 — Новосибирск, Томск, Барнаул;
# UTC+5 — Екатеринбург, Челябинск. Нужны, чтобы сравнивать время истории
# (пояс аккаунта CRM) со стенными часами салона.
SITE_TZ = {
    "nsk": 7, "barkhat-nsk": 7, "tomsk": 7, "barnaul": 7, "akadem": 7,
    "ekb": 5, "cheliabinsk": 5, "chel": 5,
}


def site_offset(site_code):
    low = (site_code or "").lower()
    for hint, offset in SITE_TZ.items():
        if hint in low:
            return offset
    return None


def probe_gaps2(day):
    header("9. УТОЧНЕНИЯ")

    # --- 1. пояс истории: берём ХВОСТ, а не начало.
    # История отдаётся по возрастанию id, и первый вызов без курсора вернул
    # записи 2021 года. Это же означает: инициализировать курсор модуля нулём
    # нельзя — иначе он вычитает четыре года истории.
    # Глубокая пагинация истории запрещена: CRM отвечает
    # «Use the shift of the `filter[sinceId]` instead of `page` parameter».
    # Поэтому свежие записи берём узким окном по дате, а не последней страницей.
    print("-- пояс createdAt истории (узкое окно по времени)")
    probe = SESSION.get(f"{RETAILCRM_URL.rstrip('/')}/api/v5/orders/history",
                        params={"limit": PAGE_LIMIT, "page": 1}, timeout=TIMEOUT)
    http_date = probe.headers.get("Date")
    utc_now = datetime.strptime(http_date, "%a, %d %b %Y %H:%M:%S %Z") if http_date else datetime.utcnow()

    records = []
    for guess in (7, 3, 0):     # предполагаемый сдвиг пояса аккаунта
        start = (utc_now + timedelta(hours=guess) - timedelta(minutes=40))
        data = get("api/v5/orders/history",
                   {"filter[startDate]": start.strftime("%Y-%m-%d %H:%M:%S"),
                    "limit": PAGE_LIMIT, "page": 1})
        found = data.get("history", [])
        print(f"   окно от UTC{guess:+d} −40 мин ({start:%Y-%m-%d %H:%M}): записей {len(found)}")
        if found and not records:
            records = found
    stamps = [r.get("createdAt") for r in records if r.get("createdAt")]
    print(f"   истинный UTC (заголовок Date):  {http_date}")
    print(f"   свежайший createdAt истории:    {max(stamps) if stamps else '-'}")
    print("   (глубокая пагинация истории по page запрещена — только sinceId)")

    # --- 2. чем отличать свои записи
    print("\n-- структура apiKey у записей source=api")
    api_records = [r for r in records if r.get("source") == "api"]
    print(f"   записей с source=api на последней странице: {len(api_records)}")
    for record in api_records[:5]:
        key = record.get("apiKey")
        print(f"      apiKey={key!r}  field={record.get('field')}")
    if not api_records:
        print("      на этой странице их нет — проверить на большем объёме")

    # --- 3. готовность с поправкой на пояс салона
    print(f"\n-- «Заказ готов» относительно окна доставки, с разбивкой по поясу ({day})")
    ready_at = {}
    for page in iter_history({"filter[startDate]": f"{day} 00:00:00",
                              "filter[endDate]": f"{day} 23:59:59"}, max_pages=200):
        for record in page:
            if record.get("field") == "status" and status_code_of(record.get("newValue")) == "order-complete":
                order_id = (record.get("order") or {}).get("id")
                if order_id:
                    ready_at.setdefault(order_id, record.get("createdAt"))

    ACCOUNT_TZ = 7   # замер: createdAt заказа опережает UTC на ~7 часов
    lead_by_tz = defaultdict(list)
    address_by_code = Counter()
    total_by_code = Counter()
    for page in iter_orders(day, day, max_pages=10):
        for order in page:
            delivery = order.get("delivery") or {}
            code = delivery.get("code")
            if code in ("self-delivery", "3", None):
                continue
            total_by_code[code] += 1
            if (delivery.get("address") or {}).get("text"):
                address_by_code[code] += 1

            marked = ready_at.get(order.get("id"))
            time_from = (delivery.get("time") or {}).get("from")
            offset = site_offset(order.get("site"))
            if not marked or not time_from or offset is None:
                continue
            if not re.match(r"^\d{1,2}:\d{2}", str(time_from)):
                continue
            try:
                planned = datetime.strptime(f"{day} {str(time_from)[:5]}", "%Y-%m-%d %H:%M")
                stamped = datetime.strptime(marked[:19], "%Y-%m-%d %H:%M:%S")
                # время истории → стенные часы салона
                stamped += timedelta(hours=offset - ACCOUNT_TZ)
                lead_by_tz[offset].append((planned - stamped).total_seconds() / 60.0)
            except ValueError:
                pass

    for offset, values in sorted(lead_by_tz.items()):
        values.sort()
        def pct(p):
            return values[min(int(len(values) * p), len(values) - 1)]
        late = sum(1 for v in values if v < 0)
        print(f"   UTC+{offset}: замеров {len(values):<4} медиана {pct(0.5):>6.0f} мин до окна, "
              f"p10 {pct(0.1):>6.0f}, p90 {pct(0.9):>6.0f}, "
              f"после начала окна {share(late, len(values))}")

    # --- 4. адрес по типам доставки
    print(f"\n-- заполненность delivery.address.text по типу доставки ({day})")
    for code, total in total_by_code.most_common():
        print(f"   {str(code):<28} {share(address_by_code.get(code, 0), total)}")


# ----------------------------------------------------------------------------
# 4-бис. Точный замер по одному дню
# ----------------------------------------------------------------------------

def probe_day_audit(statuses, day):
    """
    Полный день без усечения: какой путь по статусам реально проходит заказ.

    Общий раздел истории берёт первые 3000 записей из десятков тысяч, и доля
    «помеченных готовыми» там получается заниженной в разы. Здесь окно узкое
    (один день), поэтому история вычитывается целиком, и цифре можно верить.
    """
    header(f"4-бис. ТОЧНЫЙ ЗАМЕР ЗА {day}")

    # --- история за день целиком
    moves = defaultdict(set)          # код статуса → id заказов
    ready_at = {}                     # id заказа → когда поставили «Заказ готов»
    api_keys = Counter()
    records_total = 0
    for page in iter_history({"filter[startDate]": f"{day} 00:00:00",
                              "filter[endDate]": f"{day} 23:59:59"},
                             max_pages=200):
        for record in page:
            records_total += 1
            if record.get("source") == "api":
                key = record.get("apiKey") or {}
                api_keys[str(key.get("current", key))] += 1
            if record.get("field") != "status":
                continue
            code = status_code_of(record.get("newValue"))
            order_id = (record.get("order") or {}).get("id")
            if code and order_id:
                moves[code].add(order_id)
                if code == "order-complete":
                    ready_at.setdefault(order_id, record.get("createdAt"))
    print(f"   записей истории за день: {records_total}")

    # --- заказы с доставкой в этот день
    delivery_orders = []
    for page in iter_orders(day, day, max_pages=10):
        for order in page:
            delivery = order.get("delivery") or {}
            if delivery.get("code") in ("self-delivery", "3", None):
                continue
            delivery_orders.append(order)
    total = len(delivery_orders)
    ids = {o.get("id") for o in delivery_orders}
    print(f"   доставочных заказов с доставкой в этот день: {total}")

    print("\n-- какой путь реально проходят эти заказы")
    for code in ("send-to-florist", "correction", "order-complete", "call-courier",
                 "send-to-delivery", "wait-client", "order-delivery-complete",
                 "complete", "order-delivery-fail"):
        hit = len(moves.get(code, set()) & ids)
        name = (statuses.get(code) or {}).get("name", code)
        print(f"   {code:<26} {name:<22} {share(hit, total)}")
    print("   (переход мог случиться накануне — для вечерних заказов доля занижена)")

    # --- за сколько до доставки ставят готовность
    lead = []
    for order in delivery_orders:
        marked = ready_at.get(order.get("id"))
        time_from = ((order.get("delivery") or {}).get("time") or {}).get("from")
        if not marked or not time_from or not re.match(r"^\d{1,2}:\d{2}", str(time_from)):
            continue
        try:
            planned = datetime.strptime(f"{day} {str(time_from)[:5]}", "%Y-%m-%d %H:%M")
            stamped = datetime.strptime(marked[:19], "%Y-%m-%d %H:%M:%S")
            lead.append((planned - stamped).total_seconds() / 60.0)
        except ValueError:
            pass
    if lead:
        lead.sort()
        def pct(p):
            return lead[min(int(len(lead) * p), len(lead) - 1)]
        print(f"\n-- «Заказ готов» ставят до начала окна доставки (минуты): "
              f"медиана {pct(0.5):.0f}, p10 {pct(0.1):.0f}, p90 {pct(0.9):.0f}, "
              f"замеров {len(lead)}")
        late = sum(1 for v in lead if v < 0)
        print(f"   поставлен уже ПОСЛЕ начала окна доставки: {share(late, len(lead))}")
        print("   (сдвиг пояса между историей и временем доставки не выверен — порядок, не точность)")

    # --- поля, которые реально пойдут в карточку курьера
    print("\n-- заполненность полей карточки (только доставочные заказы этого дня)")
    counters = Counter()
    for order in delivery_orders:
        custom = order.get("customFields") or {}
        delivery = order.get("delivery") or {}
        address = delivery.get("address") or {}
        time_block = delivery.get("time") or {}
        if str(custom.get("recipient_customer")).lower() in ("true", "1"):
            counters["recipient_customer=True"] += 1
        if custom.get("recipient_name"):
            counters["recipient_name"] += 1
        if custom.get("recipient_phone"):
            counters["recipient_phone"] += 1
        if custom.get("ne_sviazyvatsia_s_poluchatelem") in (True, "true", "1"):
            counters["не связываться с получателем"] += 1
        if ((order.get("customer") or {}).get("phones") or []):
            counters["телефон заказчика"] += 1
        if address.get("text"):
            counters["delivery.address.text"] += 1
        if time_block.get("from") and time_block.get("to"):
            counters["окно доставки from+to"] += 1
        if order.get("managerComment"):
            counters["managerComment"] += 1
        if order.get("customerComment"):
            counters["customerComment"] += 1
        if custom.get("note_text"):
            counters["customFields.note_text"] += 1
        if custom.get("data_i_vremia_gotovnosti"):
            counters["data_i_vremia_gotovnosti"] += 1
        if order.get("items"):
            counters["позиции есть"] += 1
    for label, count in counters.most_common():
        print(f"   {label:<32} {share(count, total)}")

    # Кому принадлежат записи source=api: от этого зависит защита от эха.
    print("\n-- записи с source=api за день (чем отличать свои)")
    for key, count in api_keys.most_common(10):
        print(f"   apiKey={key[:60]:<62} {count}")


# ----------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7,
                        help="окно разведки в днях (по умолчанию 7)")
    parser.add_argument("--skip", default="",
                        help="пропустить разделы: fields,statuses,history,ready,images,day")
    parser.add_argument("--day", default=(date.today() - timedelta(days=1)).isoformat(),
                        help="день для точного замера (по умолчанию вчера)")
    args = parser.parse_args()
    skip = {s.strip() for s in args.skip.split(",") if s.strip()}

    print(f"РАЗВЕДКА RetailCRM для модуля курьеров (только чтение)")
    print(f"CRM: {RETAILCRM_URL}")
    print(f"окно: {args.days} дней, сырые ответы → {TMP_DIR}")

    statuses = {}
    status_time = {}

    if "fields" not in skip:
        probe_order_fields(args.days)
    if "statuses" not in skip:
        statuses = probe_statuses()
    if "history" not in skip:
        _, status_time = probe_history(statuses, args.days)
    if "ready" not in skip and statuses:
        probe_ready_discipline(statuses, status_time, args.days)
    if "day" not in skip and statuses:
        probe_day_audit(statuses, args.day)
    if "gaps" not in skip:
        probe_gaps(args.day)
    if "gaps2" not in skip:
        probe_gaps2(args.day)
    if "images" not in skip:
        probe_product_images(args.days)

    print("\nГотово. Персональные данные в консоль не выводились; "
          f"сырые образцы — в {TMP_DIR}")


if __name__ == "__main__":
    main()
