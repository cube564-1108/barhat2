"""
Сторож двух доработок от 01.09.2026 (решения владельца):

  1. Админ правит и удаляет счёт в ЛЮБОМ статусе — включая оплаченный,
     отправленный в банк и архивный. Раньше такой счёт был закрыт для всех, и
     ошибку в сумме или статье расхода нечем было починить: оставалось завести
     счёт заново и разводить отчётность руками.
  2. Поиск по номеру счёта: «123» находит СЧ-000123 — префикс и ведущие нули
     набирать не нужно.

Права остальных ролей при этом не расширяются — это проверяется отдельно:
менеджер и автор в закрытые статусы по-прежнему не лезут.

Проверяется на РЕАЛЬНОМ бэкенде (настоящие auth_bp и invoices_bp в минимальном
Flask-приложении на временной базе).

Запуск: python scripts/test_invoice_admin_and_search.py
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

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), '_test_admin_search.db')
TEST_ATTACHMENTS_DIR = os.path.join(os.path.dirname(__file__), '_test_admin_search_attachments')
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
    approve_invoice, mark_invoice_paid, set_invoice_archived, get_invoice_by_id,
    get_invoice_line_items,
)
from invoices.server import invoices_bp  # noqa: E402

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
    print("=== Права админа на закрытые счета и поиск по номеру ===\n")

    init_cashshifts_tables()
    init_invoices_tables()
    init_auth_tables()

    store_id = get_all_stores()[0]['id']
    category_id = get_all_expense_categories()[0]['id']
    city_id = create_city('Новосибирск')
    payer_id = create_payer('ИП Тестовый')

    make_user('admin_test', 'admin', ['invoices_v2'])
    make_user('manager_test', 'manager', ['invoices_v2'], store_ids=[store_id])

    app = make_app()

    def new_invoice(client, purpose='Цветы по счёту'):
        response = client.post('/api/invoices', json={
            'city_id': city_id, 'payer_id': payer_id, 'due_date': '2026-09-10',
            'amount': 1000, 'payment_purpose': purpose,
            'counterparty_name': 'ООО Ромашка',
            'line_items': [{'store_id': store_id, 'expense_category_id': category_id, 'amount': 1000}],
        })
        assert response.status_code == 201, response.data
        return response.get_json()['invoice']

    # Два клиента сразу (админ и менеджер), намеренно без `with`: вложенные
    # контексты запроса у Flask конфликтуют при выходе, а куки сессии клиент
    # держит и так.
    admin = app.test_client()
    manager = app.test_client()
    login(admin, 'admin_test')
    login(manager, 'manager_test')

    print("1. Оплаченный счёт")
    paid = new_invoice(admin)
    approve_invoice(paid['id'], 'admin_test')
    mark_invoice_paid(paid['id'], 'admin_test')
    check(get_invoice_by_id(paid['id'])['status'] == 'paid', "счёт подготовлен в статусе «оплачен»")

    response = admin.get(f"/api/invoices/{paid['id']}")
    check(response.get_json().get('can_edit_fields') is True,
          "админу счёт открыт для правки — кнопка «Редактировать» появится")

    response = admin.put(f"/api/invoices/{paid['id']}", json={'amount': 1500, 'line_items': [
        {'store_id': store_id, 'expense_category_id': category_id, 'amount': 1500}]})
    check(response.status_code == 200, f"админ правит оплаченный счёт: {response.data[:120]}")
    check(get_invoice_by_id(paid['id'])['amount'] == 1500, "новая сумма сохранилась")
    check(get_invoice_line_items(paid['id'])[0]['amount'] == 1500,
          "распределение оплаченного счёта тоже правится")

    response = manager.put(f"/api/invoices/{paid['id']}", json={'amount': 2000})
    check(response.status_code == 409, "менеджеру оплаченный счёт по-прежнему закрыт")
    check(get_invoice_by_id(paid['id'])['amount'] == 1500, "и сумма от его запроса не изменилась")

    print("\n2. Архивный счёт")
    archived = new_invoice(admin)
    set_invoice_archived(archived['id'], True, 'admin_test')
    response = admin.put(f"/api/invoices/{archived['id']}", json={
        'payment_purpose': 'Правка в архиве',
        'line_items': [{'store_id': store_id, 'expense_category_id': category_id, 'amount': 1000}],
    })
    check(response.status_code == 200, f"админ правит архивный счёт: {response.data[:120]}")
    check(get_invoice_by_id(archived['id'])['payment_purpose'] == 'Правка в архиве',
          "назначение платежа в архиве сохранилось")

    response = manager.put(f"/api/invoices/{archived['id']}", json={'payment_purpose': 'нельзя'})
    check(response.status_code == 409, "менеджеру архивный счёт закрыт")

    print("\n3. Удаление")
    response = manager.delete(f"/api/invoices/{paid['id']}")
    check(response.status_code == 403, "менеджер оплаченный счёт не удаляет")
    check(get_invoice_by_id(paid['id']) is not None, "счёт на месте")

    response = admin.delete(f"/api/invoices/{paid['id']}")
    check(response.status_code == 200, f"админ удаляет оплаченный счёт: {response.data[:120]}")
    check(get_invoice_by_id(paid['id']) is None, "счёт удалён насовсем")

    response = admin.delete(f"/api/invoices/{archived['id']}")
    check(response.status_code == 200, "архивный счёт админ тоже удаляет")

    print("\n4. Поиск по номеру счёта")
    first = new_invoice(admin, 'Первый')
    second = new_invoice(admin, 'Второй')
    numbers = (first['invoice_number'], second['invoice_number'])
    check(all(number and number.startswith('СЧ-') for number in numbers),
          f"номера выданы: {', '.join(str(n) for n in numbers)}")

    digits = first['invoice_number'].split('-')[1].lstrip('0')

    def found(query):
        response = admin.get('/api/invoices', query_string={'invoice_number': query,
                                                           'with_total': 'true'})
        data = response.get_json()
        return [inv['invoice_number'] for inv in data['invoices']], data.get('total')

    names, total = found(digits)
    check(names == [first['invoice_number']], f"поиск по «{digits}» находит ровно свой счёт: {names}")
    check(total == 1,
          "итог «найдено N» считается по тому же фильтру, что и список")

    names, _ = found(first['invoice_number'])
    check(names == [first['invoice_number']], "полный номер СЧ-000001 тоже находится")

    names, _ = found('  сч-' + digits + ' ')
    check(names == [first['invoice_number']], "регистр, префикс и пробелы не мешают")

    names, _ = found('СЧ')
    check(len(names) == 2, f"по одному префиксу видно оба счёта: {names}")

    names, _ = found('999999')
    check(names == [], "несуществующий номер не находит ничего")

    response = admin.get('/api/invoices/selection', query_string={'invoice_number': digits})
    if response.status_code == 200:
        selection = response.get_json().get('invoices', [])
        check([inv['invoice_number'] for inv in selection] == [first['invoice_number']],
              "«выбрать все по фильтру» отбирает тот же счёт")
    else:
        check(False, f"ручка выборки ответила {response.status_code}")

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
