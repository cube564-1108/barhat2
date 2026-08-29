"""
Прогон Фазы 1 плана plans/2026-08-29-рабочие-карты.md — три типа заявки.

Проверяет на РЕАЛЬНОМ бэкенде (настоящие auth_bp и invoices_bp в минимальном
Flask-приложении на временной базе), что:
  * счёт на оплату работает ровно как раньше (регресс),
  * трата с карты и пополнение карты создаются по своим правилам,
  * карточные заявки не уходят в банк и не попадают в старый REF-синк
    (иначе пополнение легло бы расходом и рубль учёлся бы дважды),
  * управляющий не может списать с чужой карты и отправить расход в чужой салон,
  * платёжные KPI-плитки не засоряются тратами с карты.

Запуск: python scripts/test_card_invoices.py
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

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), '_test_card_invoices.db')
TEST_ATTACHMENTS_DIR = os.path.join(os.path.dirname(__file__), '_test_card_invoices_attachments')
for path in (TEST_DB_PATH, TEST_DB_PATH + '-wal', TEST_DB_PATH + '-shm'):
    if os.path.exists(path):
        os.remove(path)
os.environ['BARHAT_DB_PATH'] = TEST_DB_PATH
os.environ['INVOICE_ATTACHMENTS_DIR'] = TEST_ATTACHMENTS_DIR

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from flask import Flask  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

from auth import auth_bp, login_manager, init_auth_tables, get_db  # noqa: E402
from cashshifts.storage import init_cashshifts_tables, get_all_stores  # noqa: E402
from invoices.storage import (  # noqa: E402
    init_invoices_tables, get_all_expense_categories, create_city, create_payer,
    get_invoice_by_id, get_invoices_summary, approve_invoice, user_can_access_invoice,
)
from invoices.server import invoices_bp, _send_invoice_to_bank, _match_planfact_operation  # noqa: E402
from invoices.cards import list_cards  # noqa: E402

failures = []


def check(condition, message):
    print(('   OK    ' if condition else '   ПРОВАЛ ') + message)
    if not condition:
        failures.append(message)


def make_app():
    app = Flask(__name__)
    app.secret_key = 'test-only'
    app.config['TESTING'] = True
    login_manager.init_app(app)
    app.register_blueprint(auth_bp)
    app.register_blueprint(invoices_bp)
    return app


def make_user(username, role, sections, store_ids=()):
    conn = get_db()
    conn.execute(
        "INSERT INTO users (username, password_hash, role, full_name, is_active, created_at) "
        "VALUES (?,?,?,?,1,?)",
        (username, generate_password_hash('pass'), role, username, datetime.utcnow().isoformat()),
    )
    for section in sections:
        conn.execute("INSERT INTO permissions (username, module_name, can_view) VALUES (?,?,1)",
                     (username, section))
    for store_id in store_ids:
        conn.execute("INSERT OR IGNORE INTO user_stores (username, store_id) VALUES (?, ?)",
                     (username, store_id))
    conn.commit()
    conn.close()


def login(client, username):
    response = client.post('/api/auth/login', json={'username': username, 'password': 'pass'})
    assert response.status_code == 200, f'вход {username} не удался: {response.data}'


def main():
    print("=== Фаза 1: три типа заявки ===\n")

    init_cashshifts_tables()
    init_invoices_tables()
    init_auth_tables()

    stores = {store['name']: store['id'] for store in get_all_stores()}
    category_id = get_all_expense_categories()[0]['id']
    # Города и плательщики seed-данными не заполняются — заводим сами
    city_id = create_city('Новосибирск')
    payer_id = create_payer('ИП Кваша')

    cards = {card['title']: card for card in list_cards()}
    nsk_card = cards['Рабочая карта НСК']
    chlb_card = cards['Рабочая карта ЧЛБ ГПБ']
    nsk_store = stores['НСК Восход, 3']
    chlb_store = stores['Челябинск Цвиллинга, 59']

    make_user('admin_test', 'admin', ['invoices_v2'])
    make_user('nsk_manager', 'manager', ['invoices_v2'], store_ids=[nsk_store])
    make_user('chlb_manager', 'manager', ['invoices_v2'], store_ids=[chlb_store])

    app = make_app()

    print("1. Регресс: обычный счёт не изменился")
    with app.test_client() as client:
        login(client, 'admin_test')
        response = client.post('/api/invoices', json={
            'city_id': city_id, 'payer_id': payer_id, 'due_date': '2026-09-01',
            'amount': 1000, 'payment_purpose': 'Цветы по счёту 5',
            'counterparty_name': 'ООО Ромашка',
            'line_items': [{'store_id': nsk_store, 'expense_category_id': category_id, 'amount': 1000}],
        })
        plain = response.get_json().get('invoice', {})
        check(response.status_code == 201 and plain.get('kind') == 'invoice',
              "счёт на оплату создаётся и получает kind='invoice'")

        response = client.post('/api/invoices', json={
            'city_id': city_id, 'payer_id': payer_id, 'amount': 1000,
            'payment_purpose': 'без срока',
            'line_items': [{'store_id': nsk_store, 'expense_category_id': category_id, 'amount': 1000}],
        })
        check(response.status_code == 400, "счёт без срока оплаты по-прежнему не создаётся")

    print("\n2. Трата с рабочей карты")
    with app.test_client() as client:
        login(client, 'nsk_manager')
        response = client.post('/api/invoices', json={
            'kind': 'card_expense', 'card_id': nsk_card['id'], 'spent_at': '2026-08-25',
            'amount': 1250, 'payment_purpose': 'Скотч и упаковка',
            'line_items': [{'store_id': nsk_store, 'expense_category_id': category_id, 'amount': 1250}],
        })
        expense = response.get_json().get('invoice', {})
        check(response.status_code == 201 and expense.get('kind') == 'card_expense',
              "трата создаётся без города, плательщика и срока оплаты")
        check(expense.get('spent_at') == '2026-08-25' and not expense.get('due_date'),
              "у траты заполнена дата траты, а срок оплаты пуст")

        response = client.post('/api/invoices', json={
            'kind': 'card_expense', 'card_id': nsk_card['id'],
            'amount': 100, 'payment_purpose': 'без даты',
            'line_items': [{'store_id': nsk_store, 'expense_category_id': category_id, 'amount': 100}],
        })
        check(response.status_code == 400, "трата без даты траты не создаётся")

        response = client.post('/api/invoices', json={
            'kind': 'card_expense', 'card_id': chlb_card['id'], 'spent_at': '2026-08-25',
            'amount': 100, 'payment_purpose': 'чужая карта',
            'line_items': [{'store_id': chlb_store, 'expense_category_id': category_id, 'amount': 100}],
        })
        check(response.status_code == 403, "с чужой карты списать нельзя даже подменив card_id")

        response = client.post('/api/invoices', json={
            'kind': 'card_expense', 'card_id': nsk_card['id'], 'spent_at': '2026-08-25',
            'amount': 100, 'payment_purpose': 'чужой салон',
            'line_items': [{'store_id': chlb_store, 'expense_category_id': category_id, 'amount': 100}],
        })
        check(response.status_code == 400 and 'не обслуживается' in response.get_json()['error'],
              "расход нельзя отправить в салон, который карта не обслуживает")

    print("\n3. Пополнение карты")
    with app.test_client() as client:
        login(client, 'nsk_manager')
        response = client.post('/api/invoices', json={
            'kind': 'card_topup', 'card_id': nsk_card['id'], 'due_date': '2026-08-30',
            'amount': 50000, 'payment_purpose': 'Пополнение карты НСК',
        })
        topup = response.get_json().get('invoice', {})
        check(response.status_code == 201 and topup.get('kind') == 'card_topup',
              "пополнение создаётся без распределения по статьям")

        response = client.post('/api/invoices', json={
            'kind': 'card_topup', 'card_id': nsk_card['id'], 'due_date': '2026-08-30',
            'amount': 50000, 'payment_purpose': 'с распределением',
            'line_items': [{'store_id': nsk_store, 'expense_category_id': category_id, 'amount': 50000}],
        })
        check(response.status_code == 400, "пополнение с распределением отвергается — это перемещение, а не расход")

        response = client.post('/api/invoices', json={
            'kind': 'нечто', 'amount': 1, 'payment_purpose': 'x', 'due_date': '2026-09-01',
        })
        check(response.status_code == 400, "неизвестный тип заявки отвергается")

    print("\n4. Карточные заявки не уходят в банк")
    expense_row = get_invoice_by_id(expense['id'])
    topup_row = get_invoice_by_id(topup['id'])
    approve_invoice(topup_row['id'], 'admin_test')
    result = _send_invoice_to_bank(get_invoice_by_id(topup_row['id']), sandbox=False, changed_by='admin_test')
    check(not result['ok'] and result['http_status'] == 409,
          "пополнение карты в банк не отправляется")
    result = _send_invoice_to_bank(expense_row, sandbox=True, changed_by='admin_test')
    check(not result['ok'], "трата с карты в банк не отправляется даже в песочнице")

    print("\n5. Старый REF-синк карточные заявки не трогает")
    operation = {
        'operationId': 777, 'comment': f"Оплата {topup_row['match_code']}",
        'operationDate': '2026-08-29', 'value': 50000, 'isCommitted': True,
        'account': {'accountId': 1},
    }
    outcome = _match_planfact_operation(operation, client=None, store_map={}, category_map={}, dry_run=True)
    check(outcome['status'] == 'skip',
          "операция с REF-кодом пополнения пропускается: её разнесёт другой синк")

    print("\n6. Отметить трату оплаченной нельзя")
    with app.test_client() as client:
        login(client, 'admin_test')
        response = client.post(f"/api/invoices/{expense_row['id']}/mark-paid")
        check(response.status_code == 409, "у траты с карты нет статуса «оплачен»")

    print("\n7. KPI-плитки не засоряются тратами")
    summary = get_invoices_summary(today='2026-08-30')
    check(summary['overdue']['count'] == 0,
          f"трата от 25.08 не попала в «просрочено» (там {summary['overdue']['count']})")
    check(summary['due_today']['count'] == 1,
          f"в «к оплате сегодня» только пополнение (там {summary['due_today']['count']})")
    check(summary['wait']['count'] == 2,
          f"в «ждут согласования» счёт и трата (там {summary['wait']['count']})")

    print("\n8. Видимость по салонам карты")
    check(user_can_access_invoice(get_invoice_by_id(topup['id']), 'nsk_manager', 'manager'),
          "автор видит своё пополнение")
    check(not user_can_access_invoice(get_invoice_by_id(topup['id']), 'chlb_manager', 'manager'),
          "управляющий другого города пополнение НСК не видит")

    # Второй управляющий того же города: пополнение без распределения раньше
    # было видно только автору — теперь доступ даёт карта
    make_user('nsk_manager2', 'manager', ['invoices_v2'], store_ids=[stores['НСК Блюхера, 61']])
    check(user_can_access_invoice(get_invoice_by_id(topup['id']), 'nsk_manager2', 'manager'),
          "второй управляющий НСК видит пополнение своей карты")

    print("\n9. Правка заявок")
    with app.test_client() as client:
        login(client, 'admin_test')
        response = client.put(f"/api/invoices/{expense['id']}", json={'spent_at': '2026-08-26'})
        check(response.status_code == 200 and response.get_json()['invoice']['spent_at'] == '2026-08-26',
              "дату траты можно поправить")
        response = client.put(f"/api/invoices/{topup['id']}", json={'amount': 60000})
        check(response.status_code == 200,
              "сумму пополнения можно поправить, распределение с него не требуют")

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
    # Windows держит файл открытым, пока живы соединения других модулей —
    # неудача уборки не должна выглядеть провалом прогона
    for path in (TEST_DB_PATH, TEST_DB_PATH + '-wal', TEST_DB_PATH + '-shm'):
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass
    sys.exit(code)
