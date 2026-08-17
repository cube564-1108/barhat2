"""
Тестовый скрипт для синхронизации счетов с ПланФакт (Фаза 6 плана).

Пишет в throwaway SQLite-файл (не в прод БД). Реальный API ПланФакт НЕ
вызывается — client.get_client() подменяется на фейковый клиент с
предсказуемыми ответами, чтобы проверить логику матчинга/разноски
(src/invoices/server.py: _match_planfact_operation, _run_planfact_sync)
без похода в интернет и без риска для боевого аккаунта.

Проверяет:
1. dry_run=true находит совпадение, но не пишет в ПланФакт и не меняет статус счёта.
2. dry_run=false реально вызывает update_outcome_operation и переводит счёт в paid.
3. Повторный прогон по уже оплаченному счёту — операция пропускается (skip), не задваивается.
4. Несматченные случаи (нет счёта с таким кодом, счёт не распределён, нет
   сопоставления салона/статьи с ПланФакт) попадают в invoice_planfact_unmatched,
   а не теряются молча.
5. resolve_planfact_unmatched убирает запись из списка "Требует внимания".
"""

import os
import sys
import io

if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), '_test_planfact.db')
TEST_ATTACHMENTS_DIR = os.path.join(os.path.dirname(__file__), '_test_planfact_attachments')
if os.path.exists(TEST_DB_PATH):
    os.remove(TEST_DB_PATH)
os.environ['BARHAT_DB_PATH'] = TEST_DB_PATH
os.environ['INVOICE_ATTACHMENTS_DIR'] = TEST_ATTACHMENTS_DIR

from cashshifts.storage import init_cashshifts_tables, get_all_stores
from invoices.storage import (
    init_invoices_tables,
    get_all_expense_categories,
    update_expense_category_planfact_id,
    set_store_planfact_project,
    create_invoice,
    get_invoice_by_id,
    approve_invoice,
    get_unresolved_planfact_unmatched,
    resolve_planfact_unmatched,
)

import planfact.client as planfact_client_module


class FakePlanFactClient:
    """Подмена реального PlanFactClient — фиксированный список операций,
    учёт вызовов записи для проверки, что реальная запись происходит
    только когда её не должно быть заблокировано dry_run/skip-логикой."""

    def __init__(self, operations):
        self.operations = operations
        self.update_calls = []

    def list_operations(self, **kwargs):
        return self.operations

    def update_outcome_operation(self, operation_id, operation_date, account_id, comment, is_committed, items):
        self.update_calls.append({
            "operation_id": operation_id,
            "operation_date": operation_date,
            "account_id": account_id,
            "comment": comment,
            "is_committed": is_committed,
            "items": items,
        })
        return True


def set_fake_client(operations):
    fake = FakePlanFactClient(operations)
    planfact_client_module.get_client = lambda *a, **kw: fake
    return fake


def setup_db():
    init_cashshifts_tables()
    init_invoices_tables()


def test_match_dry_run_then_real():
    print("\n=== Тест 1: dry_run не пишет, реальный прогон пишет и оплачивает ===")
    setup_db()

    store = get_all_stores()[0]
    category = get_all_expense_categories()[0]
    update_expense_category_planfact_id(category["id"], "555")
    set_store_planfact_project(store["id"], "777")

    invoice = create_invoice(
        amount=1000.0,
        payment_purpose="Аренда за август",
        created_by="tester",
        due_date="2026-08-20",
        line_items=[{"store_id": store["id"], "expense_category_id": category["id"], "amount": 1000.0}],
    )
    approve_invoice(invoice["id"], "admin")
    invoice = get_invoice_by_id(invoice["id"])
    match_code = invoice["match_code"]

    op = {
        "operationId": "op-1",
        "comment": f"Оплата по счёту, назначение: {match_code} аренда",
        "operationDate": "2026-08-10",
        "accountId": 42,
        "isCommitted": True,
        "contrAgentId": 99,
        "amount": 1000.0,
    }
    fake = set_fake_client([op])

    from invoices.server import _run_planfact_sync

    dry_result = _run_planfact_sync(dry_run=True)
    assert len(dry_result["matched"]) == 1, f"dry_run должен найти совпадение: {dry_result}"
    assert len(fake.update_calls) == 0, "dry_run не должен писать в ПланФакт"
    invoice_after_dry = get_invoice_by_id(invoice["id"])
    assert invoice_after_dry["status"] == "approved", "dry_run не должен менять статус счёта"
    print("   ✓ dry_run нашёл совпадение и ничего не записал")

    real_result = _run_planfact_sync(dry_run=False)
    assert len(real_result["matched"]) == 1, f"реальный прогон должен найти совпадение: {real_result}"
    assert len(fake.update_calls) == 1, "реальный прогон должен вызвать запись один раз"
    call = fake.update_calls[0]
    assert call["operation_id"] == "op-1"
    assert call["items"] == [{
        "calculationDate": "2026-08-10",
        "isCalculationCommitted": True,
        "contrAgentId": 99,
        "operationCategoryId": 555,
        "projectId": 777,
        "value": 1000.0,
    }], f"неверная разбивка items: {call['items']}"
    invoice_after_real = get_invoice_by_id(invoice["id"])
    assert invoice_after_real["status"] == "paid", "счёт должен стать оплаченным"
    print("   ✓ реальный прогон записал разбивку в ПланФакт и пометил счёт оплаченным")

    repeat_result = _run_planfact_sync(dry_run=False)
    assert len(repeat_result["matched"]) == 0 and len(repeat_result["unmatched"]) == 0, \
        f"повторный прогон по уже оплаченному счёту не должен ничего находить: {repeat_result}"
    assert len(fake.update_calls) == 1, "повторный прогон не должен писать в ПланФакт заново"
    print("   ✓ повторный прогон по уже оплаченному счёту не задваивает разноску")


def test_unmatched_cases():
    print("\n=== Тест 2: несматченные случаи попадают в invoice_planfact_unmatched ===")
    setup_db()

    # Индекс [1], а не [0] — салон/статья [0] уже сопоставлены с ПланФакт в test_match_dry_run_then_real
    # (та же throwaway БД переиспользуется, не сбрасывается между тестами)
    store = get_all_stores()[1]
    category = get_all_expense_categories()[1]
    # Специально НЕ настраиваем planfact_category_id/project — проверяем ветку "нет сопоставления"

    # a) код есть, счёта с таким match_code нет
    op_no_invoice = {
        "operationId": "op-no-invoice",
        "comment": "Платёж REF-999999 без счёта в базе",
        "operationDate": "2026-08-10",
        "accountId": 1,
        "isCommitted": True,
        "amount": 500.0,
    }

    # b) счёт есть, но не распределён (без line_items)
    invoice_no_items = create_invoice(
        amount=200.0, payment_purpose="без распределения", created_by="tester",
    )
    approve_invoice(invoice_no_items["id"], "admin")
    invoice_no_items = get_invoice_by_id(invoice_no_items["id"])
    op_no_items = {
        "operationId": "op-no-items",
        "comment": f"Оплата {invoice_no_items['match_code']}",
        "operationDate": "2026-08-11",
        "accountId": 1,
        "isCommitted": True,
        "amount": 200.0,
    }

    # c) счёт распределён, но нет сопоставления салона/статьи с ПланФакт
    invoice_no_mapping = create_invoice(
        amount=300.0, payment_purpose="без маппинга", created_by="tester",
        line_items=[{"store_id": store["id"], "expense_category_id": category["id"], "amount": 300.0}],
    )
    approve_invoice(invoice_no_mapping["id"], "admin")
    invoice_no_mapping = get_invoice_by_id(invoice_no_mapping["id"])
    op_no_mapping = {
        "operationId": "op-no-mapping",
        "comment": f"Оплата {invoice_no_mapping['match_code']}",
        "operationDate": "2026-08-12",
        "accountId": 1,
        "isCommitted": True,
        "amount": 300.0,
    }

    fake = set_fake_client([op_no_invoice, op_no_items, op_no_mapping])

    from invoices.server import _run_planfact_sync
    result = _run_planfact_sync(dry_run=False)

    assert len(result["matched"]) == 0, f"не должно быть успешных совпадений: {result['matched']}"
    assert len(result["unmatched"]) == 3, f"все три случая должны быть unmatched: {result}"
    assert len(fake.update_calls) == 0, "запись в ПланФакт не должна происходить ни в одном из случаев"

    unresolved = get_unresolved_planfact_unmatched()
    unresolved_ids = {u["planfact_operation_id"] for u in unresolved}
    assert unresolved_ids == {"op-no-invoice", "op-no-items", "op-no-mapping"}, \
        f"все три операции должны быть в списке 'Требует внимания': {unresolved_ids}"
    print("   ✓ все три несматченных случая записаны с понятной причиной")

    target = next(u for u in unresolved if u["planfact_operation_id"] == "op-no-invoice")
    resolve_planfact_unmatched(target["id"])
    still_unresolved = {u["planfact_operation_id"] for u in get_unresolved_planfact_unmatched()}
    assert "op-no-invoice" not in still_unresolved, "resolve должен убрать запись из списка нерешённых"
    assert still_unresolved == {"op-no-items", "op-no-mapping"}
    print("   ✓ resolve_planfact_unmatched убирает запись из списка «Требует внимания»")


if __name__ == "__main__":
    test_match_dry_run_then_real()
    test_unmatched_cases()
    print("\n=== Все тесты синхронизации с ПланФакт пройдены успешно! ===")
