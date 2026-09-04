"""
Плательщик заявки на пополнение карты (решение владельца 04.09.2026).

«На кого выставлен счёт» у пополнения определяется КАРТОЙ, а не человеком:
Барнаул (РСБ), ЕКБ (РСБ), ЧЛБ ГПБ → «Рабочая карта Насуленко»;
НСК, Томск (ГПБ) → «Рабочая карта Кваша». Связка живёт полем
work_cards.topup_payer_id и правится в справочнике карт.

Проверяет на реальном бэкенде (настоящие auth_bp/invoices_bp, временная база):
1. Первичное заполнение: карты сида получают плательщика по раскладке
   владельца; выставленное руками повторный старт не перезаписывает.
2. Создание пополнения подставляет плательщика карты — даже если клиент
   присланного payer_id не передал или передал чужой.
3. Трата с карты остаётся без плательщика (там платят своими деньгами).
4. Смена карты у пополнения переставляет и плательщика.
5. Правкой payer_id у пополнения не переопределяется.
6. Карта без привязки не блокирует создание заявки.
7. Бэкфилл проставляет плательщика заявкам, созданным до этой связки.
8. Справочник карт отдаёт и принимает topup_payer_id.

Запуск: python scripts/test_card_topup_payer.py
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

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), '_test_card_topup_payer.db')
TEST_ATTACHMENTS_DIR = os.path.join(os.path.dirname(__file__), '_test_card_topup_payer_attachments')
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
    init_invoices_tables, create_payer, get_invoice_by_id,
    _backfill_topup_invoice_payers, get_db as invoices_get_db,
)
from invoices.server import invoices_bp  # noqa: E402
from invoices.cards import (  # noqa: E402
    list_cards, get_card_by_id, update_card, _backfill_topup_payers,
)

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
    print("=== Плательщик пополнения карты ===\n")

    # Плательщиков заводим ДО инициализации счетов: бэкфилл карт ищет их по
    # названию, и на проде они в справочнике уже есть.
    init_cashshifts_tables()
    init_invoices_tables()
    init_auth_tables()

    nasulenko_id = create_payer('Рабочая карта Насуленко')
    kvasha_id = create_payer('Рабочая карта Кваша')
    other_id = create_payer('ИП Тестовый')

    # Карты уже засеяны init_invoices_tables, но плательщиков тогда ещё не
    # было — прогоняем добивку ещё раз, как это сделает следующий старт.
    conn = invoices_get_db()
    try:
        _backfill_topup_payers(conn)
    finally:
        conn.close()

    cards = {card['title']: card for card in list_cards()}

    print("1. Раскладка владельца проставлена по названиям карт")
    expected = {
        'Рабочая карта Барнаул (РСБ)': nasulenko_id,
        'Рабочая карта ЕКБ (РСБ)': nasulenko_id,
        'Рабочая карта ЧЛБ ГПБ': nasulenko_id,
        'Рабочая карта НСК': kvasha_id,
        'Рабочая карта Томск (ГПБ)': kvasha_id,
    }
    for title, payer_id in expected.items():
        check(cards[title]['topup_payer_id'] == payer_id,
              f'{title} → {"Насуленко" if payer_id == nasulenko_id else "Кваша"}')

    print("\n2. Повторная добивка не перезаписывает выставленное руками")
    nsk_card = cards['Рабочая карта НСК']
    update_card(nsk_card['id'], {'topup_payer_id': other_id})
    conn = invoices_get_db()
    try:
        _backfill_topup_payers(conn)
    finally:
        conn.close()
    check(get_card_by_id(nsk_card['id'])['topup_payer_id'] == other_id,
          'ручная привязка карты НСК осталась (добивка трогает только пустые)')
    update_card(nsk_card['id'], {'topup_payer_id': kvasha_id})   # возвращаем как было

    stores = {store['name']: store['id'] for store in get_all_stores()}
    nsk_store = stores['НСК Восход, 3']
    chlb_card = cards['Рабочая карта ЧЛБ ГПБ']
    chlb_store = stores['Челябинск Цвиллинга, 59']

    make_user('admin_test', 'admin', ['invoices_v2'])
    make_user('nsk_manager', 'manager', ['invoices_v2'], store_ids=[nsk_store])
    app = make_app()

    print("\n3. Пополнение получает плательщика карты")
    with app.test_client() as client:
        login(client, 'admin_test')

        response = client.post('/api/invoices', json={
            'kind': 'card_topup', 'card_id': nsk_card['id'], 'due_date': '2026-09-10',
            'amount': 30000, 'payment_purpose': 'Пополнение карты НСК',
        })
        topup = (response.get_json() or {}).get('invoice', {})
        check(response.status_code == 201 and topup.get('payer_id') == kvasha_id,
              'пополнение НСК выставлено на «Рабочая карта Кваша» без участия человека')

        # Клиент прислал чужого плательщика — карта главнее
        response = client.post('/api/invoices', json={
            'kind': 'card_topup', 'card_id': chlb_card['id'], 'due_date': '2026-09-10',
            'amount': 15000, 'payment_purpose': 'Пополнение ЧЛБ', 'payer_id': other_id,
        })
        chlb_topup = (response.get_json() or {}).get('invoice', {})
        check(response.status_code == 201 and chlb_topup.get('payer_id') == nasulenko_id,
              'присланный клиентом payer_id не переопределяет карту')

        print("\n4. Трата с карты плательщика не получает")
        response = client.post('/api/invoices', json={
            'kind': 'card_expense', 'card_id': nsk_card['id'], 'spent_at': '2026-09-03',
            'amount': 500, 'payment_purpose': 'Упаковка',
            'line_items': [{'store_id': nsk_store,
                            'expense_category_id': _first_category(), 'amount': 500}],
        })
        expense = (response.get_json() or {}).get('invoice', {})
        check(response.status_code == 201 and expense.get('payer_id') is None,
              'у траты с карты плательщика нет — платили своими деньгами')

        print("\n5. Смена карты переставляет плательщика, ручная правка — нет")
        response = client.put(f"/api/invoices/{topup['id']}", json={'card_id': chlb_card['id']})
        check(response.status_code == 200
              and get_invoice_by_id(topup['id'])['payer_id'] == nasulenko_id,
              'переставили карту на ЧЛБ — плательщик стал «Насуленко»')

        response = client.put(f"/api/invoices/{topup['id']}", json={'payer_id': other_id})
        check(get_invoice_by_id(topup['id'])['payer_id'] == nasulenko_id,
              'правка payer_id у пополнения игнорируется — источник истины карта')

        print("\n6. Карта без привязки не блокирует заявку")
        update_card(chlb_card['id'], {'topup_payer_id': None})
        response = client.post('/api/invoices', json={
            'kind': 'card_topup', 'card_id': chlb_card['id'], 'due_date': '2026-09-11',
            'amount': 1000, 'payment_purpose': 'Пополнение без привязки',
        })
        unbound = (response.get_json() or {}).get('invoice', {})
        check(response.status_code == 201 and unbound.get('payer_id') is None,
              'заявка создалась без плательщика, а не упала с ошибкой')

        print("\n7. Бэкфилл проставляет плательщика старым пополнениям")
        # Возвращаем привязку карте и имитируем «заявка заведена до связки»
        update_card(chlb_card['id'], {'topup_payer_id': nasulenko_id})
        conn = invoices_get_db()
        try:
            conn.execute("UPDATE invoices SET payer_id = NULL WHERE id = ?", (unbound['id'],))
            conn.commit()
        finally:
            conn.close()
        _backfill_topup_invoice_payers()
        check(get_invoice_by_id(unbound['id'])['payer_id'] == nasulenko_id,
              'пополнение без плательщика добито по карте')

        # Трата с карты бэкфилла не касается
        check(get_invoice_by_id(expense['id'])['payer_id'] is None,
              'трату с карты бэкфилл не тронул')

        print("\n8. Справочник карт отдаёт и принимает привязку")
        response = client.get('/api/invoices/work-cards')
        listed = {card['title']: card for card in (response.get_json() or {}).get('work_cards', [])}
        check(listed['Рабочая карта ЧЛБ ГПБ']['topup_payer_id'] == nasulenko_id,
              'GET /work-cards отдаёт topup_payer_id')

        response = client.put(f"/api/invoices/work-cards/{chlb_card['id']}",
                              json={'topup_payer_id': kvasha_id})
        check(response.status_code == 200
              and get_card_by_id(chlb_card['id'])['topup_payer_id'] == kvasha_id,
              'PUT /work-cards меняет привязку')

        response = client.put(f"/api/invoices/work-cards/{chlb_card['id']}",
                              json={'topup_payer_id': 999999})
        check(response.status_code == 400,
              'несуществующий плательщик отклоняется с 400')

        response = client.put(f"/api/invoices/work-cards/{chlb_card['id']}",
                              json={'topup_payer_id': None})
        check(response.status_code == 200
              and get_card_by_id(chlb_card['id'])['topup_payer_id'] is None,
              'пустое значение снимает привязку')

    print("\n=== Итог ===")
    if failures:
        print(f"ПРОВАЛЕНО проверок: {len(failures)}")
        for message in failures:
            print(f"  - {message}")
        sys.exit(1)
    print("Все проверки пройдены")


def _first_category():
    from invoices.storage import get_all_expense_categories
    return get_all_expense_categories()[0]['id']


if __name__ == '__main__':
    main()
