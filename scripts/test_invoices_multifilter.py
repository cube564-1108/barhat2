"""
Проверка мультивыбора в фильтрах счетов (список значений в одном фильтре).

Пишет в throwaway SQLite-файл, боевую базу не трогает. Проверяет, что:
1. Скалярное значение фильтра работает ровно как раньше (старый раздел счетов
   и скрипты зовут API одним значением).
2. Список значений в одном фильтре — это ИЛИ (город Москва ИЛИ Казань).
3. Разные фильтры между собой по-прежнему И.
4. Фильтры по строкам распределения (салон, статья) на списке не задваивают
   счёт, у которого под условие подходит несколько строк.
5. Пустой список = фильтр не задан.
"""

import io
import os
import sys

if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), '_test_invoices_multifilter.db')
TEST_ATTACHMENTS_DIR = os.path.join(os.path.dirname(__file__), '_test_multifilter_attachments')
if os.path.exists(TEST_DB_PATH):
    os.remove(TEST_DB_PATH)
os.environ['BARHAT_DB_PATH'] = TEST_DB_PATH
os.environ['INVOICE_ATTACHMENTS_DIR'] = TEST_ATTACHMENTS_DIR

from cashshifts.storage import init_cashshifts_tables, get_all_stores
from invoices.storage import (
    init_invoices_tables,
    create_city,
    create_payer,
    create_invoice,
    count_invoices,
    get_all_expense_categories,
    list_invoices,
)

failures = []


def check(title, condition, detail=""):
    if condition:
        print(f"   OK  {title}")
    else:
        print(f"   FAIL {title} {detail}")
        failures.append(title)


def ids(invoices):
    return {inv["id"] for inv in invoices}


init_cashshifts_tables()
init_invoices_tables()

stores = get_all_stores()[:2]
categories = get_all_expense_categories()[:2]
moscow = create_city("Мультитест Москва")
kazan = create_city("Мультитест Казань")
perm = create_city("Мультитест Пермь")
payer_a = create_payer("Мультитест ООО А")
payer_b = create_payer("Мультитест ООО Б")

print("=== Подготовка данных ===")


def make(city_id, payer_id, author, line_items, amount=1000.0):
    return create_invoice(
        created_by=author,
        city_id=city_id,
        payer_id=payer_id,
        due_date="2026-09-01",
        amount=amount,
        payment_purpose="Мультитест",
        counterparty_name="Мультитест поставщик",
        line_items=line_items,
    )["id"]


one_store = [{"store_id": stores[0]["id"], "expense_category_id": categories[0]["id"], "amount": 1000.0}]
other_store = [{"store_id": stores[1]["id"], "expense_category_id": categories[1]["id"], "amount": 1000.0}]
both_stores = [
    {"store_id": stores[0]["id"], "expense_category_id": categories[0]["id"], "amount": 500.0},
    {"store_id": stores[1]["id"], "expense_category_id": categories[1]["id"], "amount": 500.0},
]

inv_msk = make(moscow, payer_a, "author_one", one_store)
inv_kzn = make(kazan, payer_b, "author_two", other_store)
inv_perm = make(perm, payer_a, "author_three", both_stores)
print(f"   Счета: Москва={inv_msk}, Казань={inv_kzn}, Пермь={inv_perm}")

print("=== 1. Скаляр работает как раньше ===")
check("город одним значением", ids(list_invoices(city_id=moscow)) == {inv_msk})
check("плательщик одним значением", ids(list_invoices(payer_id=payer_b)) == {inv_kzn})
check("автор одним значением", ids(list_invoices(created_by="author_two")) == {inv_kzn})
check("статус одним значением", ids(list_invoices(status="on_approval")) == {inv_msk, inv_kzn, inv_perm})

print("=== 2. Список значений — это ИЛИ ===")
check("два города", ids(list_invoices(city_id=[moscow, kazan])) == {inv_msk, inv_kzn})
check("два плательщика", ids(list_invoices(payer_id=[payer_a, payer_b])) == {inv_msk, inv_kzn, inv_perm})
check("два автора", ids(list_invoices(created_by=["author_one", "author_three"])) == {inv_msk, inv_perm})
check("два статуса", ids(list_invoices(status=["on_approval", "paid"])) == {inv_msk, inv_kzn, inv_perm})

print("=== 3. Разные фильтры между собой — И ===")
check(
    "город (Москва|Казань) И плательщик А",
    ids(list_invoices(city_id=[moscow, kazan], payer_id=[payer_a])) == {inv_msk},
)
check(
    "город (Москва|Казань) И автор author_three",
    list_invoices(city_id=[moscow, kazan], created_by=["author_three"]) == [],
)

print("=== 4. Строки распределения не задваивают счёт ===")
both_store_ids = [stores[0]["id"], stores[1]["id"]]
rows = list_invoices(store_id=both_store_ids)
check("салоны списком: счёт с двумя строками один раз", len(rows) == len(ids(rows)))
check("салоны списком: найдены все три", ids(rows) == {inv_msk, inv_kzn, inv_perm})

cat_rows = list_invoices(expense_category_id=[categories[0]["id"], categories[1]["id"]])
check("статьи списком: без дублей", len(cat_rows) == len(ids(cat_rows)))
check("статьи списком: найдены все три", ids(cat_rows) == {inv_msk, inv_kzn, inv_perm})

totals = count_invoices(store_id=both_store_ids, is_archived=False)
check(
    "итог считает столько же, сколько отдал список",
    totals["count"] == len(rows),
    f"(итог={totals['count']}, список={len(rows)})",
)

print("=== 5. Пустой список = фильтр не задан ===")
check("пустой список городов", ids(list_invoices(city_id=[])) == {inv_msk, inv_kzn, inv_perm})
check("пустая строка в списке", ids(list_invoices(city_id=[moscow, "", None])) == {inv_msk})
check("дубли значений не ломают запрос", ids(list_invoices(city_id=[moscow, moscow])) == {inv_msk})

print()
if failures:
    print(f"=== ПРОВАЛЕНО проверок: {len(failures)} ===")
    for name in failures:
        print(f"   - {name}")
    sys.exit(1)
print("=== Мультивыбор в фильтрах работает корректно! ===")
