"""
Прогон Фазы 10: `section_required` принимает несколько секций.

Проверяет на РЕАЛЬНОМ бэкенде (настоящие auth_bp и invoices_bp, поднятые в
минимальном Flask-приложении на временной базе), что ручки счетов пускают
и по старой секции `invoices`, и по новой `invoices_v2`, и что снятие обеих
по-прежнему закрывает доступ.

Это ключевая проверка перед переводом сотрудников: если ручка пускает только
по `invoices`, то в момент снятия старой секции новый раздел откроется пустым.

Запуск: python scripts/test_invoices_sections.py
"""

import io
import os
import socket
import sys
from datetime import datetime

# UTF-8 для Windows
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')


# Сеть отключаем ДО импорта приложения: локальный прогон читает боевой .env
# (load_dotenv при импорте) и иначе может уйти в боевые ПланФакт/Модульбанк.
class NoNetwork(socket.socket):
    def connect(self, *a, **k):
        raise OSError("сеть отключена намеренно")

    def connect_ex(self, *a, **k):
        raise OSError("сеть отключена намеренно")


socket.socket = NoNetwork

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), '_test_invoice_sections.db')
TEST_ATTACHMENTS_DIR = os.path.join(os.path.dirname(__file__), '_test_section_attachments')
if os.path.exists(TEST_DB_PATH):
    os.remove(TEST_DB_PATH)
os.environ['BARHAT_DB_PATH'] = TEST_DB_PATH
os.environ['INVOICE_ATTACHMENTS_DIR'] = TEST_ATTACHMENTS_DIR

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from flask import Flask  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

import auth  # noqa: E402
from auth import auth_bp, login_manager, init_auth_tables, get_db  # noqa: E402
from cashshifts.storage import init_cashshifts_tables  # noqa: E402
from invoices.storage import init_invoices_tables  # noqa: E402
from invoices.server import invoices_bp, INVOICE_SECTIONS  # noqa: E402

failures = []


def check(condition, message):
    print(('   OK   ' if condition else '   ПРОВАЛ ') + message)
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


def make_user(username, role, sections):
    """Заводит пользователя ровно с перечисленными секциями в permissions."""
    conn = get_db()
    conn.execute(
        "INSERT INTO users (username, password_hash, role, full_name, is_active, created_at) "
        "VALUES (?,?,?,?,1,?)",
        (username, generate_password_hash('pass'), role, username, datetime.utcnow().isoformat()),
    )
    conn.execute("DELETE FROM permissions WHERE username = ?", (username,))
    for section in sections:
        conn.execute(
            "INSERT INTO permissions (username, module_name, can_view) VALUES (?,?,1)",
            (username, section),
        )
    conn.commit()
    conn.close()


def login(client, username):
    response = client.post('/api/auth/login', json={'username': username, 'password': 'pass'})
    assert response.status_code == 200, f'вход {username} не удался: {response.data}'


# Ручки, которые обязаны открыться по любой из двух секций. Только чтение —
# прогон не должен трогать данные.
READ_ENDPOINTS = [
    '/api/invoices',
    '/api/invoices/summary',
    '/api/invoices/authors',
    '/api/invoices/stores',
    '/api/invoices/categories',
    '/api/invoices/cities',
    '/api/invoices/payers',
    '/api/invoices/vat-options',
    '/api/invoices/counterparties',
    '/api/invoices/templates',
]


def main():
    print("=== Фаза 10: section_required на две секции ===\n")

    init_cashshifts_tables()
    init_invoices_tables()
    init_auth_tables()

    print("0. Константа секций")
    check(INVOICE_SECTIONS == ('invoices', 'invoices_v2'),
          f"INVOICE_SECTIONS = {INVOICE_SECTIONS}")
    print()

    # init_auth_tables заводит дефолтного админа — свои учётки называем иначе
    make_user('t_old', 'manager', ['invoices'])                # как сейчас у сотрудника
    make_user('t_new', 'manager', ['invoices_v2'])             # после перевода: старой секции нет
    make_user('t_both', 'manager', ['invoices', 'invoices_v2'])  # переходный период
    # Ни одной секции счетов. Роль обязательно florist, а не manager: у manager
    # в ROLE_SECTIONS есть invoices, и сработал бы фоллбэк по роли — то есть
    # снятия секции в permissions мало, роль надо чистить тоже.
    make_user('t_none', 'florist', ['dashboard'])

    app = make_app()

    cases = [
        ('t_old', 200, 'только старая секция invoices'),
        ('t_new', 200, 'только новая секция invoices_v2'),
        ('t_both', 200, 'обе секции'),
    ]
    for username, expected, title in cases:
        print(f"{title} ({username})")
        with app.test_client() as client:
            login(client, username)
            for path in READ_ENDPOINTS:
                status = client.get(path).status_code
                check(status == expected, f"GET {path} -> {status} (ждём {expected})")
        print()

    print("без секций счетов (t_none) — доступа быть не должно")
    with app.test_client() as client:
        login(client, 't_none')
        for path in READ_ENDPOINTS:
            status = client.get(path).status_code
            check(status == 403, f"GET {path} -> {status} (ждём 403)")
    print()

    print("отказ пишется в аудит-лог с обеими секциями")
    conn = get_db()
    row = conn.execute(
        "SELECT details FROM audit_log WHERE username = 't_none' AND action = 'access_denied' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    check(row is not None and row['details'] == 'invoices / invoices_v2',
          f"details = {row['details'] if row else 'записи нет'}")
    print()

    print("фоллбэк на ROLE_SECTIONS перебивает пустые permissions (важно для перевода людей)")
    make_user('t_fallback', 'manager', [])  # permissions пустые, роль manager
    check('invoices' in auth.ROLE_SECTIONS['manager'],
          "у роли manager в ROLE_SECTIONS есть invoices")
    with app.test_client() as client:
        login(client, 't_fallback')
        status = client.get('/api/invoices').status_code
        check(status == 200, f"GET /api/invoices по роли -> {status} (ждём 200)")
    print()

    print("пустой вызов section_required() запрещён")
    try:
        auth.section_required()
        check(False, "исключения не было")
    except ValueError:
        check(True, "ValueError, как и задумано")
    print()

    if failures:
        print(f"ПРОВАЛОВ: {len(failures)}")
        for item in failures:
            print('  - ' + item)
        sys.exit(1)
    print("Все проверки прошли.")


if __name__ == '__main__':
    main()
