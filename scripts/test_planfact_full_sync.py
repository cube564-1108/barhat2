"""
Сторож объединённого прогона синхронизации с ПланФактом (фаза 1 плана
plans/2026-09-15-единая-синхронизация-планфакт.md).

До объединения разноска карт и разноска счетов были двумя механизмами с
кнопками на разных вкладках. Слить их в один прогон мало — надо удержать то,
что вскрыла критика плана:

  * оба этапа идут под ОДНИМ локом, и это тот же лок, что берёт почасовой
    планировщик: иначе кнопка и планировщик разносят одно и то же параллельно;
  * талон лока продлевается ВНУТРИ постраничного обхода операций, а не только
    между этапами — прогон за 60 дней длиннее TTL, а протухший талон пускает
    второй воркер и даёт вторую запись в ПланФакт (маркеры защищают только
    карты, счета матчатся по REF-);
  * падение первого этапа не отменяет второй — раньше это были разные кнопки,
    и объединение не должно делать их судьбу общей;
  * исчерпанная квота проверяется перед КАЖДЫМ этапом: лимит месячный и может
    кончиться между ними.

Сеть отключена намеренно: ни один тест не имеет права уйти наружу.

Запуск: python scripts/test_planfact_full_sync.py
"""

import io
import os
import socket
import sys
from datetime import datetime, timezone

if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')


class NoNetwork(socket.socket):
    def connect(self, *a, **k):
        raise OSError("сеть отключена намеренно")

    def connect_ex(self, *a, **k):
        raise OSError("сеть отключена намеренно")


socket.socket = NoNetwork

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), '_test_planfact_full_sync.db')
for path in (TEST_DB_PATH, TEST_DB_PATH + '-wal', TEST_DB_PATH + '-shm'):
    if os.path.exists(path):
        os.remove(path)
os.environ['BARHAT_DB_PATH'] = TEST_DB_PATH
os.environ['INVOICE_ATTACHMENTS_DIR'] = os.path.join(os.path.dirname(__file__),
                                                     '_test_planfact_full_sync_att')
os.environ['PLANFACT_API_KEY'] = 'test-key-not-real'
os.environ['CARD_SYNC_SCHEDULER'] = '0'

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cashshifts.storage import init_cashshifts_tables, get_all_stores  # noqa: E402
from invoices.storage import (  # noqa: E402
    init_invoices_tables, get_all_expense_categories, create_invoice, get_invoice_by_id,
    approve_invoice, set_store_planfact_project, update_expense_category_planfact_id,
)
from invoices.cards import list_cards, try_acquire_sync_lock, release_sync_lock  # noqa: E402
from planfact import quota as planfact_quota  # noqa: E402
import invoices.planfact_sync as planfact_sync  # noqa: E402
import invoices.planfact_run as planfact_run  # noqa: E402
import planfact.client as planfact_client_module  # noqa: E402

failures = []


def check(condition, message):
    print(('   OK    ' if condition else '   ПРОВАЛ ') + message)
    if not condition:
        failures.append(message)


class FakeClient:
    """
    Заглушка ПланФакта на оба этапа сразу.

    pages — что вернёт постраничный обход операций (этап счетов). Каждый вызов
    list_operations с непустым offset берёт следующую страницу.
    """

    def __init__(self, pages=None):
        self.pages = pages or [[]]
        self.created = []
        self.list_calls = []
        self._next_id = 7000

    def list_operations(self, operation_type=None, search_string=None,
                        operation_date_start=None, offset=0, limit=1000):
        self.list_calls.append({'search': search_string, 'offset': offset})
        index = offset // 1000
        return self.pages[index] if index < len(self.pages) else []

    def create_outcome_operation(self, account_id, operation_date, items, comment='',
                                 is_committed=True, external_id=None):
        self._next_id += 1
        operation = {'operationId': self._next_id, 'comment': comment}
        self.created.append(operation)
        return operation

    def create_move_operation(self, **kwargs):
        self._next_id += 1
        return {'operationId': self._next_id, 'comment': kwargs.get('comment', '')}

    def update_outcome_operation(self, *a, **k):
        return True


def install(client):
    planfact_client_module.get_client = lambda *a, **k: client
    return client


def main():
    print("=== Объединённый прогон синхронизации с ПланФактом ===\n")

    init_cashshifts_tables()
    init_invoices_tables()
    planfact_quota.reset_for_tests()

    stores = {s['name']: s['id'] for s in get_all_stores()}
    store_id = stores['НСК Восход, 3']
    category = get_all_expense_categories()[0]
    card = {c['title']: c for c in list_cards()}['Рабочая карта НСК']

    set_store_planfact_project(store_id, '5001')
    update_expense_category_planfact_id(category['id'], '6001')

    def make_expense(amount):
        invoice = create_invoice(
            amount=amount, payment_purpose='Упаковка', created_by='admin',
            kind='card_expense', card_id=card['id'],
            spent_at=datetime.now(timezone.utc).strftime('%Y-%m-%d'),
            line_items=[{'store_id': store_id, 'expense_category_id': category['id'],
                         'amount': amount}],
        )
        approve_invoice(invoice['id'], 'admin')
        return get_invoice_by_id(invoice['id'])

    # ------------------------------------------------------------------
    print("1. Оба этапа проходят одним вызовом")
    expense = make_expense(1200)
    client = install(FakeClient(pages=[[]]))

    result = planfact_run.run_full_sync()
    check(len(result['cards']['created']) == 1,
          f"этап карт отработал: создано {len(result['cards']['created'])}")
    check(result['invoices'] is not None and 'matched' in result['invoices'],
          "этап счетов отработал в том же вызове")
    check(get_invoice_by_id(expense['id'])['planfact_synced_at'] is not None,
          "заявка по карте помечена разнесённой")

    summary = planfact_run.summarize(result)
    check(summary['cards_created'] == 1 and summary['invoices_matched'] == 0,
          f"сводка считает этапы раздельно: {summary}")

    # ------------------------------------------------------------------
    print("\n2. Лок — тот же, что у почасового планировщика")
    # Свой отдельный лок означал бы, что кнопка и планировщик не исключают друг
    # друга и разносят одно и то же параллельно.
    import invoices.cards_sync as cards_sync
    check(planfact_run.FULL_SYNC_LOCK == cards_sync.SYNC_LOCK,
          f"общий лок: {planfact_run.FULL_SYNC_LOCK}")

    taken = try_acquire_sync_lock(planfact_run.FULL_SYNC_LOCK, 60)
    check(taken, "лок захвачен снаружи — имитируем идущий прогон")
    busy = planfact_run.run_full_sync()
    check(busy.get('skipped') == 'Синхронизация уже идёт',
          f"второй прогон не начался: {busy.get('skipped')}")
    check(not busy['cards']['created'] and busy['invoices'] is None,
          "и ничего не сделал")
    release_sync_lock(planfact_run.FULL_SYNC_LOCK)

    # ------------------------------------------------------------------
    print("\n3. Талон продлевается внутри постраничного обхода")
    # Прогон за 60 дней длиннее TTL. Протухший на ходу талон пускает второй
    # воркер, и он обновит те же операции вторично.
    renewals = []
    original_renew = planfact_sync_renew_spy(renewals)

    page = [{'operationId': f'op-{i}', 'comment': 'нет кода', 'value': 100}
            for i in range(1000)]
    install(FakeClient(pages=[page, page, []]))
    planfact_run.run_full_sync()
    restore_renew(original_renew)

    check(len(renewals) >= 3,
          f"лок продлевался на каждой странице, не только между этапами: {len(renewals)} раз")

    # ------------------------------------------------------------------
    print("\n4. Падение первого этапа не отменяет второй")
    broken = install(FakeClient(pages=[[]]))
    original_cards = cards_sync.run_card_sync

    def exploding(*a, **k):
        raise RuntimeError("сопоставление не настроено")

    cards_sync.run_card_sync = exploding
    try:
        result = planfact_run.run_full_sync()
    finally:
        cards_sync.run_card_sync = original_cards

    check('не отработал' in (result['cards'].get('error') or ''),
          f"падение этапа карт названо словами: {result['cards'].get('error')}")
    check(result['invoices'] is not None,
          "этап счетов всё равно выполнен — у них разные причины падать")
    check(any(call['search'] == 'REF-' for call in broken.list_calls),
          "и он действительно ходил за операциями")

    # ------------------------------------------------------------------
    print("\n5. Исчерпанная квота останавливает прогон до внешних вызовов")
    quiet = install(FakeClient(pages=[[]]))
    planfact_quota.record_response(
        {'X-Quota-Limit': '2500', 'X-Quota-Used': '2500', 'X-Quota-Remaining': '0',
         'X-Quota-Reset': str(int(datetime.now(timezone.utc).timestamp()) + 86400)},
        status_code=403, body='лимит запросов',
    )
    make_expense(900)
    result = planfact_run.run_full_sync()
    check('лимит' in (result.get('skipped') or '').lower(),
          f"прогон объясняет, почему отложен: {result.get('skipped')}")
    check(not quiet.list_calls and not quiet.created,
          "наружу не ходили ни на одном этапе")
    planfact_quota.reset_for_tests()

    print("\n" + "=" * 60)
    if failures:
        print(f"ПРОВАЛОВ: {len(failures)}")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("ВСЁ ЗЕЛЁНОЕ")
    return 0


def planfact_sync_renew_spy(sink):
    """Подменить продление лока счётчиком, вернув оригинал."""
    original = planfact_run.renew_sync_lock

    def spy(name, ttl):
        sink.append(name)
        return original(name, ttl)

    planfact_run.renew_sync_lock = spy
    return original


def restore_renew(original):
    planfact_run.renew_sync_lock = original


if __name__ == '__main__':
    code = main()
    for path in (TEST_DB_PATH, TEST_DB_PATH + '-wal', TEST_DB_PATH + '-shm'):
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
    sys.exit(code)
