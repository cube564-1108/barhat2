"""
Тестовый скрипт для проверки миграций и CRUD модуля счетов на оплату.

Запускает инициализацию таблиц и проверяет основной сценарий:
создание счёта -> согласование / отклонение.
"""

import os
import sys
import io

# UTF-8 для Windows
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# Добавляем src в путь
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cashshifts.storage import init_cashshifts_tables, get_all_stores
from invoices.storage import (
    init_invoices_tables,
    get_all_expense_categories,
    create_invoice,
    get_invoice_by_id,
    get_invoice_by_number,
    list_invoices,
    approve_invoice,
    reject_invoice,
)
from invoices.seed_data import EXPENSE_CATEGORIES


def test_invoices():
    print("=== Тест миграций и CRUD Invoices ===\n")

    print("1. Инициализация таблиц...")
    init_cashshifts_tables()  # invoices зависит от stores из cashshifts
    init_invoices_tables()
    print("   ✓ Таблицы созданы\n")

    print("2. Проверка статей расхода:")
    categories = get_all_expense_categories()
    print(f"   Всего категорий: {len(categories)}")
    if len(categories) != len(EXPENSE_CATEGORIES):
        print(f"   ✗ Ошибка: ожидалось {len(EXPENSE_CATEGORIES)}, получено {len(categories)}")
        return False
    print(f"   ✓ Ожидаемое количество: {len(EXPENSE_CATEGORIES)}\n")

    stores = get_all_stores()
    if not stores:
        print("   ✗ Ошибка: нет точек продаж (модуль cashshifts должен быть проинициализирован первым)")
        return False

    store_id = stores[0]["id"]
    category_id = categories[0]["id"]

    print("3. Создание счёта:")
    invoice = create_invoice(
        store_id=store_id,
        expense_category_id=category_id,
        counterparty_name="ООО Тестовый поставщик",
        amount=15000.50,
        created_by="test_user",
        description="Тестовая закупка",
    )
    print(f"   Создан счёт {invoice['invoice_number']} на сумму {invoice['amount']}")

    if invoice["status"] != "on_approval":
        print(f"   ✗ Ошибка: ожидался статус on_approval, получен {invoice['status']}")
        return False
    if not invoice["invoice_number"] or invoice["invoice_number"] != f"СЧ-{invoice['id']:06d}":
        print(f"   ✗ Ошибка: неверный номер счёта {invoice['invoice_number']}")
        return False
    if invoice["invoice_number"] not in (invoice["payment_purpose"] or ""):
        print(f"   ✗ Ошибка: номер счёта отсутствует в назначении платежа")
        return False
    print("   ✓ Счёт создан с корректным номером и назначением платежа\n")

    print("4. Проверка поиска:")
    found_by_id = get_invoice_by_id(invoice["id"])
    found_by_number = get_invoice_by_number(invoice["invoice_number"])
    if not found_by_id or not found_by_number:
        print("   ✗ Ошибка поиска счёта")
        return False
    print("   ✓ Счёт находится по ID и по номеру\n")

    print("5. Согласование счёта:")
    ok = approve_invoice(invoice["id"], "admin_user")
    if not ok:
        print("   ✗ Ошибка: согласование не удалось")
        return False
    approved = get_invoice_by_id(invoice["id"])
    if approved["status"] != "approved" or approved["approved_by"] != "admin_user":
        print(f"   ✗ Ошибка: статус после согласования {approved['status']}")
        return False
    print("   ✓ Счёт согласован\n")

    print("6. Повторное согласование должно быть отклонено (уже не on_approval):")
    ok = approve_invoice(invoice["id"], "admin_user")
    if ok:
        print("   ✗ Ошибка: повторное согласование не должно проходить")
        return False
    print("   ✓ Повторное согласование корректно отклонено\n")

    print("7. Отклонение отдельного счёта:")
    invoice2 = create_invoice(
        store_id=store_id,
        expense_category_id=category_id,
        counterparty_name="ИП Второй поставщик",
        amount=5000,
        created_by="test_user",
    )
    ok = reject_invoice(invoice2["id"], "admin_user", "Дублирующий счёт")
    if not ok:
        print("   ✗ Ошибка: отклонение не удалось")
        return False
    rejected = get_invoice_by_id(invoice2["id"])
    if rejected["status"] != "rejected" or rejected["rejected_reason"] != "Дублирующий счёт":
        print(f"   ✗ Ошибка: статус после отклонения {rejected['status']}")
        return False
    print("   ✓ Счёт отклонён с причиной\n")

    print("8. Список счетов с фильтром по статусу:")
    approved_list = list_invoices(status="approved")
    rejected_list = list_invoices(status="rejected")
    if not any(i["id"] == invoice["id"] for i in approved_list):
        print("   ✗ Ошибка: согласованный счёт не найден в фильтре approved")
        return False
    if not any(i["id"] == invoice2["id"] for i in rejected_list):
        print("   ✗ Ошибка: отклонённый счёт не найден в фильтре rejected")
        return False
    print("   ✓ Фильтры по статусу работают\n")

    print("=== Все тесты пройдены успешно! ===")
    return True


if __name__ == "__main__":
    success = test_invoices()
    sys.exit(0 if success else 1)
