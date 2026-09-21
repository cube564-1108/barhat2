"""
Прогон Фазы 3 плана plans/2026-08-29-рабочие-карты.md — разноска заявок по
рабочим картам в ПланФакт.

Клиент ПланФакта подменён заглушкой (сеть отключена), поэтому проверяется
именно наша логика: что уезжает, чем, с какой датой и что происходит при
падении между записью в ПФ и записью признака.

Ключевые вопросы, на которые отвечает прогон:
  * подтверждённая трата уходит расходом со счёта карты, пополнение —
    перемещением с нужного счёта-источника;
  * повторный прогон не создаёт дубль (ни по признаку, ни по маркеру);
  * упавший между записями процесс не приводит к дублю на следующем прогоне;
  * незаполненное сопоставление салона роняет одну заявку, а не весь прогон.

Запуск: python scripts/test_card_sync.py
"""

import io
import os
import socket
import sys
from datetime import datetime

if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')


class NoNetwork(socket.socket):
    def connect(self, *a, **k):
        raise OSError("сеть отключена намеренно")

    def connect_ex(self, *a, **k):
        raise OSError("сеть отключена намеренно")


socket.socket = NoNetwork

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), '_test_card_sync.db')
for path in (TEST_DB_PATH, TEST_DB_PATH + '-wal', TEST_DB_PATH + '-shm'):
    if os.path.exists(path):
        os.remove(path)
os.environ['BARHAT_DB_PATH'] = TEST_DB_PATH
os.environ['INVOICE_ATTACHMENTS_DIR'] = os.path.join(os.path.dirname(__file__), '_test_card_sync_att')

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cashshifts.storage import init_cashshifts_tables, get_all_stores  # noqa: E402
from invoices.storage import (  # noqa: E402
    init_invoices_tables, get_all_expense_categories, create_invoice, get_invoice_by_id,
    approve_invoice, mark_invoice_paid, set_store_planfact_project,
    update_expense_category_planfact_id, mark_invoice_planfact_synced,
)
from invoices.cards import list_cards  # noqa: E402
import invoices.cards_sync as sync  # noqa: E402

failures = []


def check(condition, message):
    print(('   OK    ' if condition else '   ПРОВАЛ ') + message)
    if not condition:
        failures.append(message)


class FakePlanFact:
    """
    Заглушка клиента ПланФакта: помнит созданные операции и умеет искать их
    по подстроке в комментарии — ровно так, как ведёт себя searchString.
    """

    def __init__(self):
        self.operations = []
        self.calls = []
        self.fail_next = False
        self._next_id = 9000
        # Настоящий клиент кладёт сюда объяснение отказа от ПланФакта; заявка
        # обязана показать его человеку, а не отсылку к логам сервера.
        self.last_error = None

    def list_operations(self, operation_type=None, search_string=None, **kwargs):
        self.calls.append(('list', search_string))
        return [op for op in self.operations
                if (not search_string or search_string in (op.get('comment') or ''))
                and (not operation_type or op['_type'] in operation_type)]

    def _create(self, kind, comment, **payload):
        if self.fail_next:
            self.fail_next = False
            self.last_error = 'Статья расходов не найдена (CategoryNotFound)'
            return None
        self.last_error = None
        self._next_id += 1
        operation = {'operationId': self._next_id, 'comment': comment, '_type': kind, **payload}
        self.operations.append(operation)
        self.calls.append((kind, payload))
        return operation

    def create_outcome_operation(self, account_id, operation_date, items, comment='',
                                 is_committed=True, external_id=None):
        return self._create('Outcome', comment, account_id=account_id,
                            operation_date=operation_date, items=items, external_id=external_id)

    def create_move_operation(self, debiting_account_id, admission_account_id, operation_date,
                              debiting_items, admission_items=None, comment='',
                              is_committed=True, external_id=None):
        return self._create('Move', comment, debiting_account_id=debiting_account_id,
                            admission_account_id=admission_account_id,
                            operation_date=operation_date, items=debiting_items,
                            external_id=external_id)


fake = FakePlanFact()


def install_fake_client():
    """Подменяем фабрику клиента: run_card_sync берёт её импортом внутри функции."""
    import planfact.client as planfact_client
    planfact_client.get_client = lambda *a, **k: fake


def main():
    print("=== Фаза 3: разноска заявок по картам ===\n")

    init_cashshifts_tables()
    init_invoices_tables()
    install_fake_client()

    stores = {store['name']: store['id'] for store in get_all_stores()}
    nsk_store = stores['НСК Восход, 3']
    blucher_store = stores['НСК Блюхера, 61']
    category = get_all_expense_categories()[0]
    cards = {card['title']: card for card in list_cards()}
    nsk_card = cards['Рабочая карта НСК']

    # Сопоставление с ПланФактом: салон -> проект, статья -> статья ПФ
    set_store_planfact_project(nsk_store, '5001')
    update_expense_category_planfact_id(category['id'], '7001')

    def make_expense(amount, spent_at, store_id=nsk_store):
        invoice = create_invoice(
            amount=amount, payment_purpose='Упаковка', created_by='nsk_manager',
            kind='card_expense', card_id=nsk_card['id'], spent_at=spent_at,
            line_items=[{'store_id': store_id, 'expense_category_id': category['id'], 'amount': amount}],
        )
        approve_invoice(invoice['id'], 'admin')
        return get_invoice_by_id(invoice['id'])

    def make_topup(amount, due_date):
        invoice = create_invoice(
            amount=amount, payment_purpose='Пополнение карты НСК', created_by='nsk_manager',
            kind='card_topup', card_id=nsk_card['id'], due_date=due_date,
        )
        approve_invoice(invoice['id'], 'admin')
        mark_invoice_paid(invoice['id'], 'admin')
        return get_invoice_by_id(invoice['id'])

    print("1. Кандидаты")
    expense = make_expense(1250, '2026-08-25')
    topup = make_topup(50000, '2026-08-28')
    draft = create_invoice(amount=300, payment_purpose='Ещё не согласована',
                           created_by='nsk_manager', kind='card_expense',
                           card_id=nsk_card['id'], spent_at='2026-08-26',
                           line_items=[{'store_id': nsk_store,
                                        'expense_category_id': category['id'], 'amount': 300}])
    candidates = sync.collect_candidates()
    ids = {row['id'] for row in candidates}
    check(expense['id'] in ids and topup['id'] in ids, "подтверждённая трата и переведённое пополнение — кандидаты")
    check(draft['id'] not in ids, "трата на согласовании в ПланФакт не уезжает")

    print("\n2. Превью ничего не пишет")
    result = sync.run_card_sync(dry_run=True)
    check(len(result['created']) == 2 and not fake.operations,
          f"dry_run показывает 2 заявки и не создаёт операций (создано {len(fake.operations)})")
    check(get_invoice_by_id(expense['id'])['planfact_synced_at'] is None,
          "dry_run не проставляет признак разноски")

    print("\n3. Боевой прогон")
    result = sync.run_card_sync()
    check(len(result['created']) == 2, f"создано 2 операции (получено {len(result['created'])})")

    outcome = next(op for op in fake.operations if op['_type'] == 'Outcome')
    move = next(op for op in fake.operations if op['_type'] == 'Move')

    check(str(outcome['account_id']) == nsk_card['planfact_account_id'],
          "расход списан со счёта карты")
    check(outcome['operation_date'] == '2026-08-25T00:00:00',
          f"дата расхода — дата траты, а не сегодня (получено {outcome['operation_date']})")
    check(outcome['items'][0]['operationCategoryId'] == 7001
          and outcome['items'][0]['projectId'] == 5001,
          "в расходе проставлены статья и проект из сопоставления")
    check(f"[cardexp:{expense['id']}]" in outcome['comment'],
          "в комментарии расхода есть маркер заявки")

    check(str(move['debiting_account_id']) == nsk_card['source_planfact_account_id']
          and str(move['admission_account_id']) == nsk_card['planfact_account_id'],
          "перемещение идёт со счёта-источника на счёт карты")
    check('operationCategoryId' not in move['items'][0],
          "у перемещения нет статьи расхода")

    check(get_invoice_by_id(expense['id'])['planfact_synced_at'] is not None
          and get_invoice_by_id(topup['id'])['planfact_operation_id'],
          "у обеих заявок проставлен признак разноски и id операции")

    print("\n4. Повторный прогон не создаёт дублей")
    before = len(fake.operations)
    result = sync.run_card_sync()
    check(len(fake.operations) == before and not result['created'],
          f"второй прогон ничего не создал (было {before}, стало {len(fake.operations)})")

    print("\n5. Падение между записью в ПФ и записью признака")
    # Имитируем: операция в ПланФакте есть, а наша база о ней не знает
    conn_expense = make_expense(777, '2026-08-27')
    sync.run_card_sync()
    fake_op = fake.operations[-1]
    conn = __import__('invoices.storage', fromlist=['get_db']).get_db()
    conn.execute("UPDATE invoices SET planfact_synced_at = NULL, planfact_operation_id = NULL WHERE id = ?",
                 (conn_expense['id'],))
    conn.commit()
    conn.close()

    before = len(fake.operations)
    result = sync.run_card_sync()
    check(len(fake.operations) == before,
          "операция найдена по маркеру, вторая не создана")
    check(result['exists'] and str(get_invoice_by_id(conn_expense['id'])['planfact_operation_id'])
          == str(fake_op['operationId']),
          "признак проставлен по найденной операции")

    print("\n6. Ошибка одной заявки не рвёт прогон")
    broken = make_expense(500, '2026-08-28', store_id=blucher_store)  # у салона нет сопоставления
    good = make_expense(640, '2026-08-28')
    before = len(fake.operations)
    result = sync.run_card_sync()
    check(len(result['failed']) == 1 and len(result['created']) == 1,
          f"одна упала, вторая уехала (упало {len(result['failed'])}, создано {len(result['created'])})")
    check(len(fake.operations) == before + 1, "в ПланФакт ушла только исправная заявка")
    broken_row = get_invoice_by_id(broken['id'])
    check(broken_row['planfact_error'] and 'сопоставление' in broken_row['planfact_error'],
          f"причина видна в заявке: {broken_row['planfact_error']}")
    check(get_invoice_by_id(good['id'])['planfact_synced_at'] is not None,
          "исправная заявка разнесена, несмотря на соседнюю ошибку")

    print("\n7. Отказ ПланФакта не теряется молча")
    rejected = make_expense(999, '2026-08-29')
    fake.fail_next = True
    result = sync.run_card_sync()
    check(len(result['failed']) >= 1 and get_invoice_by_id(rejected['id'])['planfact_synced_at'] is None,
          "при отказе ПФ признак разноски не ставится")
    rejected_error = get_invoice_by_id(rejected['id'])['planfact_error'] or ''
    check(rejected_error, "причина отказа записана в заявку")
    # 15.09.2026: СЧ-000287 висел с текстом «ПланФакт не принял операцию
    # (подробности в логах сервера)». Консоли у контейнера на нашем тарифе
    # Amvera нет, то есть подробностей не было нигде, и чинить было нечего.
    check('Статья расходов не найдена' in rejected_error,
          f"и это объяснение ПланФакта, а не отсылка к логам: {rejected_error}")
    check('логах сервера' not in rejected_error,
          "к логам сервера человека не отправляем — он до них не доберётся")

    print("\n8. Ошибка снимается после успешной разноски")
    set_store_planfact_project(blucher_store, '5002')
    sync.run_card_sync()
    fixed = get_invoice_by_id(broken['id'])
    check(fixed['planfact_synced_at'] is not None and not fixed['planfact_error'],
          "починили сопоставление — заявка уехала, ошибка снята")

    print("\n9. Заявка с ненаступившей датой ждёт срока, а не числится сломанной")
    # 15.09.2026: СЧ-000287 и СЧ-000323 завели с датой оплаты 16 сентября.
    # ПланФакт отверг их: «Нельзя создать часть операции с подтвержденной в
    # будущем датой начисления (40)» — мы шлём isCalculationCommitted: true
    # всегда. Заявки висели красными «Ошибка разноски», хотя чинить было
    # нечего, и каждый прогон бился в тот же отказ.
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    tomorrow = (_dt.now(_tz.utc) + _td(days=1)).strftime('%Y-%m-%d')
    future = make_expense(1500, tomorrow)

    # Ошибка, записанная прошлым прогоном (до того, как мы научились ждать
    # даты), обязана сняться сразу, а не через шесть часов отсрочки: заявка с
    # ошибкой в обычную выборку кандидатов не попадает вовсе.
    sync.set_invoice_planfact_error(
        future['id'], 'ПланФакт не принял операцию: Нельзя создать часть операции '
                      'с подтвержденной в будущем датой начисления (40)')
    check(get_invoice_by_id(future['id'])['planfact_error'], "ошибка на заявке есть — как было на проде")

    calls_before = len(fake.calls)
    result = sync.run_card_sync(force=True)
    ids_touched = {item['invoice_id'] for group in ('created', 'exists', 'failed')
                   for item in result.get(group, [])}
    check(future['id'] not in ids_touched, "заявка завтрашним днём в прогон не берётся")
    check(len(fake.calls) == calls_before, "и наружу из-за неё не ходим — отказ гарантирован")

    stored = get_invoice_by_id(future['id'])
    check(not stored['planfact_error'],
          f"красной ошибки на ней нет: {stored['planfact_error']}")
    check(stored['planfact_synced_at'] is None, "и разнесённой она не считается")

    # Наступил день — заявка уходит обычным порядком, без вмешательства
    conn = __import__('invoices.storage', fromlist=['get_db']).get_db()
    try:
        conn.execute("UPDATE invoices SET spent_at = ? WHERE id = ?",
                     (_dt.now(_tz.utc).strftime('%Y-%m-%d'), future['id']))
        conn.commit()
    finally:
        conn.close()

    sync.run_card_sync(force=True)
    arrived = get_invoice_by_id(future['id'])
    check(arrived['planfact_synced_at'] is not None,
          "когда день наступил — уехала сама, без человека")

    print("\n10. Разнесённая трата переходит в «Оплачен»")
    # Жалоба владельца 21.09.2026: траты с карты навсегда висели в
    # «Согласован» и смешивались с теми, что правда ждут действия. Признак
    # разноски был только мелким бейджем.
    settled = make_expense(1800, '2026-08-30')
    check(get_invoice_by_id(settled['id'])['status'] == 'approved',
          "до разноски трата в «Согласован»")
    sync.run_card_sync(force=True)
    settled_row = get_invoice_by_id(settled['id'])
    check(settled_row['planfact_synced_at'] is not None, "трата уехала в ПланФакт")
    check(settled_row['status'] == 'paid',
          f"и стала «Оплачен» (получено «{settled_row['status']}»)")
    check(settled_row['paid_at'], "дата оплаты проставлена")

    # Пополнение приходит в разноску уже оплаченным — его статус не трогаем.
    settled_topup = make_topup(3000, '2026-08-30')
    sync.run_card_sync(force=True)
    check(get_invoice_by_id(settled_topup['id'])['status'] == 'paid',
          "у пополнения статус остаётся «Оплачен»")

    print("\n11. Отказ ПланФакта статус не двигает")
    # Обратный порядок («сначала статус, потом признак») оставил бы заявку
    # законченной на вид и не разнесённой на деле — то есть невидимой.
    not_pushed = make_expense(1900, '2026-08-30')
    fake.fail_next = True
    sync.run_card_sync(force=True)
    not_pushed_row = get_invoice_by_id(not_pushed['id'])
    check(not_pushed_row['planfact_synced_at'] is None, "операция в ПланФакт не ушла")
    check(not_pushed_row['status'] == 'approved',
          f"и статус остался «Согласован» (получено «{not_pushed_row['status']}»)")

    print("\n12. Трата, оплаченная руками до разноски, всё равно уезжает")
    # Статус «Оплачен» можно поставить рукой раньше синка. Если считать
    # кандидатами только «Согласован», такая трата не уедет никогда и молча.
    by_hand = make_expense(2100, '2026-08-30')
    mark_invoice_paid(by_hand['id'], 'admin')
    check(get_invoice_by_id(by_hand['id'])['status'] == 'paid',
          "трату отметили оплаченной до разноски")
    ids = {row['id'] for row in sync.collect_candidates(force=True)}
    check(by_hand['id'] in ids, "она остаётся кандидатом на разноску")
    sync.run_card_sync(force=True)
    check(get_invoice_by_id(by_hand['id'])['planfact_synced_at'] is not None,
          "и уезжает в ПланФакт обычным порядком")

    print("\n" + "=" * 60)
    if failures:
        print(f"ПРОВАЛОВ: {len(failures)}")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("Все проверки пройдены")
    return 0


if __name__ == '__main__':
    code = main()
    for path in (TEST_DB_PATH, TEST_DB_PATH + '-wal', TEST_DB_PATH + '-shm'):
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass
    sys.exit(code)
