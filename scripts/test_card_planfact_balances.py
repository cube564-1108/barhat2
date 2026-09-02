"""
Сторож вкладки «Остатки на картах» (задача владельца 02.09.2026).

Вкладка показывает остаток каждой рабочей карты ПО ДАННЫМ ПЛАНФАКТА рядом с
нашим подотчётом по заявкам. Проверяем то, из-за чего такие экраны ломаются:

  * видимость — управляющий видит только карты своих салонов, админ все;
  * внешний вызов ходит в ПланФакт ОДИН раз на все карты и прячется за кэшем:
    «безобидный GET для интерфейса» без кэша уже забирал оба воркера прода и
    клал сайт целиком (инцидент 2026-08-18);
  * повторное открытие вкладки в пределах TTL в ПланФакт не ходит;
  * кнопка «Обновить» не даёт долбить внешний API чаще раза в минуту;
  * когда ПланФакт недоступен — отдаём последние сохранённые остатки с
    пометкой, а не пустой экран и не 500;
  * расхождение с подотчётом считается на сервере.

ПланФакт подменён фейком — сеть не нужна и намеренно заблокирована.

Запуск: python scripts/test_card_planfact_balances.py
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

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), '_test_card_balances.db')
for path in (TEST_DB_PATH, TEST_DB_PATH + '-wal', TEST_DB_PATH + '-shm'):
    if os.path.exists(path):
        os.remove(path)
os.environ['BARHAT_DB_PATH'] = TEST_DB_PATH
os.environ['INVOICE_ATTACHMENTS_DIR'] = os.path.join(os.path.dirname(__file__),
                                                     '_test_card_balances_attachments')

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from flask import Flask  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

from auth import auth_bp, login_manager, init_auth_tables, get_db  # noqa: E402
from cashshifts.storage import init_cashshifts_tables, get_all_stores  # noqa: E402
from invoices.storage import (  # noqa: E402
    init_invoices_tables, get_all_expense_categories, approve_invoice, mark_invoice_paid,
)
from invoices import cards as cards_module  # noqa: E402
from invoices.server import invoices_bp  # noqa: E402

failures = []
calls = []          # сюда фейковый ПланФакт пишет каждый свой вызов


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


def install_fake_planfact(balances, fail=False):
    """Подменить живой вызов ПланФакта. Возвращает функцию-восстановитель."""
    original = cards_module.fetch_planfact_balances

    def fake(account_ids):
        calls.append(list(account_ids))
        if fail:
            raise RuntimeError("ПланФакт недоступен")
        return {account_id: balances.get(account_id, 0.0) for account_id in account_ids}

    cards_module.fetch_planfact_balances = fake
    return lambda: setattr(cards_module, 'fetch_planfact_balances', original)


def expire_cache(seconds=3600):
    """Состарить кэш остатков, чтобы не ждать TTL по-настоящему."""
    conn = get_db()
    conn.execute("UPDATE work_card_balances SET fetched_at = datetime('now', ?)",
                 (f'-{seconds} seconds',))
    conn.commit()
    conn.close()


def main():
    print("=== Остатки на рабочих картах из ПланФакта ===\n")

    init_cashshifts_tables()
    init_invoices_tables()
    init_auth_tables()

    stores = {store['name']: store['id'] for store in get_all_stores()}
    nsk_store = stores['НСК Восход, 3']
    chlb_store = stores['Челябинск Цвиллинга, 59']
    category_id = get_all_expense_categories()[0]['id']

    cards = {card['title']: card for card in cards_module.list_cards()}
    nsk_card = cards['Рабочая карта НСК']
    chlb_card = cards['Рабочая карта ЧЛБ ГПБ']

    make_user('admin_test', 'admin', ['invoices_v2'])
    make_user('nsk_manager', 'manager', ['invoices_v2'], store_ids=[nsk_store])

    app = make_app()
    admin = app.test_client()
    manager = app.test_client()
    login(admin, 'admin_test')
    login(manager, 'nsk_manager')

    # Живые остатки из «ПланФакта»: на карте НСК 42 000, на карте ЧЛБ 7 500
    planfact_money = {
        nsk_card['planfact_account_id']: 42000.0,
        chlb_card['planfact_account_id']: 7500.0,
    }
    restore = install_fake_planfact(planfact_money)

    print("1. Видимость карт")
    calls.clear()
    response = admin.get('/api/invoices/work-cards/balances')
    admin_rows = response.get_json()['cards']
    check(response.status_code == 200 and len(admin_rows) == len(cards),
          f"админ видит все карты: {len(admin_rows)} из {len(cards)}")

    response = manager.get('/api/invoices/work-cards/balances')
    manager_rows = response.get_json()['cards']
    check([row['title'] for row in manager_rows] == ['Рабочая карта НСК'],
          f"управляющий НСК видит только свою карту: {[r['title'] for r in manager_rows]}")
    check(all(row['planfact_balance'] is not None for row in manager_rows),
          "остаток по своей карте ему виден")

    print("\n2. Один внешний вызов на все карты, дальше — кэш")
    check(len(calls) == 1, f"в ПланФакт сходили один раз на оба запроса (вызовов: {len(calls)})")
    check(len(calls[0]) == len({card['planfact_account_id'] for card in cards.values()}),
          "и одним запросом на все счета сразу, а не по счёту на карту")

    calls.clear()
    for _ in range(5):
        admin.get('/api/invoices/work-cards/balances')
    check(not calls, "пять переоткрытий вкладки в пределах TTL в ПланФакт не ходят")

    print("\n3. Кнопка «Обновить»")
    calls.clear()
    response = admin.get('/api/invoices/work-cards/balances?refresh=1')
    check(not calls, "сразу после обновления кнопка внешний вызов не повторяет")
    check(response.get_json()['refreshed'] is False, "и честно отвечает, что не обновляла")

    expire_cache(120)          # старше антидребезга (60 с), но данные ещё есть
    calls.clear()
    response = admin.get('/api/invoices/work-cards/balances?refresh=1')
    check(len(calls) == 1, "через минуту кнопка снова идёт в ПланФакт")
    check(response.get_json()['refreshed'] is True, "и сообщает, что данные свежие")

    print("\n4. Устаревание и недоступность ПланФакта")
    expire_cache(3600)
    calls.clear()
    restore()
    restore = install_fake_planfact(planfact_money, fail=True)
    response = admin.get('/api/invoices/work-cards/balances')
    data = response.get_json()
    check(response.status_code == 200, "падение ПланФакта не роняет ручку")
    check('ПланФакт недоступен' in (data.get('error') or ''),
          f"ошибка названа прямо: {data.get('error')}")
    nsk_row = next(row for row in data['cards'] if row['title'] == 'Рабочая карта НСК')
    check(nsk_row['planfact_balance'] == 42000.0,
          "показан последний сохранённый остаток, а не пустота")
    check(nsk_row['stale'] is True, "и помечен как устаревший")

    restore()
    restore = install_fake_planfact(planfact_money)

    print("\n5. Расхождение с подотчётом")
    # Пополнили карту НСК на 50 000 и отчитались тратой на 3 000:
    # по нашим заявкам на карте 47 000, в ПланФакте 42 000 → расхождение -5 000
    topup = admin.post('/api/invoices', json={
        'kind': 'card_topup', 'card_id': nsk_card['id'], 'due_date': '2026-09-05',
        'amount': 50000, 'payment_purpose': 'Пополнение карты НСК',
    }).get_json()['invoice']
    # Выданным считается только переведённое пополнение, а перевести можно
    # лишь согласованное — тот же путь, что и в жизни
    approve_invoice(topup['id'], 'admin_test')
    mark_invoice_paid(topup['id'], 'admin_test')
    admin.post('/api/invoices', json={
        'kind': 'card_expense', 'card_id': nsk_card['id'], 'spent_at': '2026-09-01',
        'amount': 3000, 'payment_purpose': 'Упаковка',
        'line_items': [{'store_id': nsk_store, 'expense_category_id': category_id, 'amount': 3000}],
    })

    expire_cache(3600)
    response = admin.get('/api/invoices/work-cards/balances')
    nsk_row = next(row for row in response.get_json()['cards'] if row['title'] == 'Рабочая карта НСК')
    check(nsk_row['accountable']['balance'] == 47000.0,
          f"подотчёт по заявкам посчитан: {nsk_row['accountable']['balance']}")
    check(nsk_row['difference'] == -5000.0,
          f"расхождение с ПланФактом посчитано на сервере: {nsk_row['difference']}")

    chlb_row = next(row for row in response.get_json()['cards'] if row['title'] == 'Рабочая карта ЧЛБ ГПБ')
    check(chlb_row['difference'] == 7500.0,
          "по карте без заявок расхождение равно остатку ПланФакта")

    restore()

    print("\n=== Итог ===")
    if failures:
        print(f"ПРОВАЛОВ: {len(failures)}")
        for message in failures:
            print(' - ' + message)
        return 1
    print("Все проверки пройдены")
    return 0


if __name__ == '__main__':
    sys.exit(main())
