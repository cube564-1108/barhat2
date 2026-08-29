"""
Прогон Фазы 0 плана plans/2026-08-29-рабочие-карты.md — справочник рабочих карт.

Проверяет на РЕАЛЬНОМ бэкенде (настоящие auth_bp и invoices_bp в минимальном
Flask-приложении на временной базе):
  * сид пяти карт с боевыми accountId и раскладкой девяти салонов по городам;
  * повторный init не задваивает карты;
  * права: справочник правит только админ, управляющий видит карты своих салонов;
  * серверную валидацию accountId (нечисловой id ронял прогон разноски);
  * что недоступность ПланФакта не роняет ручку списка счетов.

Запуск: python scripts/test_work_cards.py
"""

import io
import os
import socket
import sys
from datetime import datetime

if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')


# Сеть отключаем ДО импорта приложения: прогон читает боевой .env (load_dotenv
# при импорте) и иначе ушёл бы в боевой ПланФакт.
class NoNetwork(socket.socket):
    def connect(self, *a, **k):
        raise OSError("сеть отключена намеренно")

    def connect_ex(self, *a, **k):
        raise OSError("сеть отключена намеренно")


socket.socket = NoNetwork

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), '_test_work_cards.db')
TEST_ATTACHMENTS_DIR = os.path.join(os.path.dirname(__file__), '_test_work_cards_attachments')
if os.path.exists(TEST_DB_PATH):
    os.remove(TEST_DB_PATH)
os.environ['BARHAT_DB_PATH'] = TEST_DB_PATH
os.environ['INVOICE_ATTACHMENTS_DIR'] = TEST_ATTACHMENTS_DIR

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from flask import Flask  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

from auth import auth_bp, login_manager, init_auth_tables, get_db  # noqa: E402
from cashshifts.storage import init_cashshifts_tables, get_all_stores  # noqa: E402
from invoices.storage import init_invoices_tables  # noqa: E402
from invoices.server import invoices_bp  # noqa: E402
from invoices.cards import list_cards, SEED_CARDS  # noqa: E402

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
        conn.execute(
            "INSERT INTO permissions (username, module_name, can_view) VALUES (?,?,1)",
            (username, section),
        )
    for store_id in store_ids:
        conn.execute(
            "INSERT OR IGNORE INTO user_stores (username, store_id) VALUES (?, ?)",
            (username, store_id),
        )
    conn.commit()
    conn.close()


def login(client, username):
    response = client.post('/api/auth/login', json={'username': username, 'password': 'pass'})
    assert response.status_code == 200, f'вход {username} не удался: {response.data}'


def main():
    print("=== Фаза 0: справочник рабочих карт ===\n")

    init_cashshifts_tables()
    init_invoices_tables()
    init_auth_tables()

    stores = {store['id']: store['name'] for store in get_all_stores()}

    print("1. Сид пяти карт")
    cards = list_cards()
    check(len(cards) == len(SEED_CARDS), f"карт заведено {len(cards)}, ожидалось {len(SEED_CARDS)}")

    by_title = {card['title']: card for card in cards}
    check(by_title.get('Рабочая карта НСК', {}).get('planfact_account_id') == '764679',
          "у карты НСК боевой accountId 764679")
    check(by_title.get('Рабочая карта ЕКБ (РСБ)', {}).get('source_planfact_account_id') == '316172',
          "ЕКБ пополняется с 16 РСБ Насуленко (юрлицо счёта роли не играет)")

    covered = set()
    for card in cards:
        covered.update(card['store_ids'])
    check(covered == set(stores), f"все {len(stores)} салонов разложены по картам, покрыто {len(covered)}")
    check(len(by_title.get('Рабочая карта НСК', {}).get('store_ids', [])) == 3,
          "карта НСК обслуживает три салона города")

    print("\n2. Повторная инициализация")
    init_invoices_tables()
    check(len(list_cards()) == len(SEED_CARDS), "повторный init не задваивает карты")

    print("\n3. Права")
    nsk_stores = [store_id for store_id, name in stores.items() if name.startswith('НСК')]
    make_user('admin_test', 'admin', ['invoices_v2'])
    make_user('nsk_manager', 'manager', ['invoices_v2'], store_ids=nsk_stores[:1])
    make_user('no_stores', 'manager', ['invoices_v2'])

    app = make_app()
    with app.test_client() as client:
        login(client, 'nsk_manager')
        response = client.get('/api/invoices/work-cards')
        visible = response.get_json()['work_cards']
        check(response.status_code == 200 and len(visible) == 1
              and visible[0]['title'] == 'Рабочая карта НСК',
              "управляющий НСК видит только карту своего города")

        response = client.post('/api/invoices/work-cards',
                               json={'title': 'Чужая', 'planfact_account_id': '1',
                                     'source_planfact_account_id': '2'})
        check(response.status_code == 403, "управляющий не может завести карту (403)")

    with app.test_client() as client:
        login(client, 'no_stores')
        response = client.get('/api/invoices/work-cards')
        check(response.get_json()['work_cards'] == [], "пользователь без салонов не видит карт")

    with app.test_client() as client:
        login(client, 'admin_test')
        response = client.get('/api/invoices/work-cards')
        check(len(response.get_json()['work_cards']) == len(SEED_CARDS), "админ видит все карты")

        print("\n4. Валидация accountId на сервере")
        response = client.post('/api/invoices/work-cards',
                               json={'title': 'Кривая', 'planfact_account_id': 'abc',
                                     'source_planfact_account_id': '316172'})
        check(response.status_code == 400 and 'числовой' in response.get_json()['error'],
              "нечисловой accountId отвергается с понятной ошибкой")

        response = client.post('/api/invoices/work-cards',
                               json={'title': '', 'planfact_account_id': '1',
                                     'source_planfact_account_id': '2'})
        check(response.status_code == 400, "карта без названия не создаётся")

        response = client.post('/api/invoices/work-cards',
                               json={'title': 'С левым салоном', 'planfact_account_id': '1',
                                     'source_planfact_account_id': '2', 'store_ids': [99999]})
        check(response.status_code == 400, "несуществующий салон в привязке отвергается")

        print("\n5. CRUD через API")
        response = client.post('/api/invoices/work-cards',
                               json={'title': 'Резервная карта', 'planfact_account_id': '999001',
                                     'source_planfact_account_id': '999002',
                                     'store_ids': list(stores)[:2]})
        created = response.get_json().get('work_card', {})
        check(response.status_code == 201 and len(created.get('store_ids', [])) == 2,
              "карта создаётся вместе с привязкой салонов")

        card_id = created['id']
        response = client.put(f'/api/invoices/work-cards/{card_id}',
                              json={'title': 'Резервная карта 2', 'store_ids': list(stores)[:1]})
        updated = response.get_json()['work_card']
        check(updated['title'] == 'Резервная карта 2' and len(updated['store_ids']) == 1,
              "правка меняет и поля, и список салонов")

        response = client.delete(f'/api/invoices/work-cards/{card_id}')
        check(response.status_code == 200, "карта деактивируется")
        response = client.get('/api/invoices/work-cards')
        check(all(card['id'] != card_id for card in response.get_json()['work_cards']),
              "деактивированная карта пропадает из списка")
        response = client.get('/api/invoices/work-cards?all=true')
        check(any(card['id'] == card_id for card in response.get_json()['work_cards']),
              "и остаётся видна админу в режиме all=true")

        response = client.post('/api/invoices/work-cards',
                               json={'title': 'Резервная карта 2', 'planfact_account_id': '999003',
                                     'source_planfact_account_id': '999004'})
        check(response.status_code == 201,
              "имя деактивированной карты можно занять снова (нет UNIQUE(title))")

        print("\n6. ПланФакт недоступен")
        response = client.get('/api/invoices/planfact/accounts')
        body = response.get_json()
        check(response.status_code == 200 and body['accounts'] == [] and body.get('error'),
              "падение ПланФакта не роняет ручку, а объясняет, что id вписать руками")

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
        if os.path.exists(path):
            os.remove(path)
    sys.exit(code)
