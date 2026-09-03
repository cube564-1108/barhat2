"""
Разбор обращения #2 по invoices_v2: «не подкрепляются файлы к счетам»
плюс 500 на `GET /api/invoices/authors`.

Ключевое отличие от scripts/test_invoices_sections.py: там у тестовых
менеджеров НЕТ салонов, поэтому ветка видимости по салонам не исполняется
вовсе. На проде у менеджера салон есть — и именно эта ветка падает.

Проверяет на РЕАЛЬНОМ бэкенде (auth_bp + invoices_bp в минимальном Flask
на временной базе):
  1. `GET /api/invoices/authors` менеджером С САЛОНОМ отвечает 200;
  2. вложение грузится к своему счёту (multipart, поле `file`);
  3. вложение грузится к чужому счёту, видимому по салону;
  4. файл реально лёг на диск и виден в карточке счёта;
  5. отказы остаются отказами: чужой невидимый счёт — 403, .exe — 400.

Запуск: python scripts/test_invoice_attach_and_authors.py
"""

import io
import os
import shutil
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

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), '_test_attach_authors.db')
TEST_ATTACHMENTS_DIR = os.path.join(os.path.dirname(__file__), '_test_attach_authors_files')
for path in (TEST_DB_PATH, TEST_DB_PATH + '-wal', TEST_DB_PATH + '-shm'):
    if os.path.exists(path):
        os.remove(path)
shutil.rmtree(TEST_ATTACHMENTS_DIR, ignore_errors=True)
os.environ['BARHAT_DB_PATH'] = TEST_DB_PATH
os.environ['INVOICE_ATTACHMENTS_DIR'] = TEST_ATTACHMENTS_DIR

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from flask import Flask  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

from auth import auth_bp, login_manager, init_auth_tables, get_db  # noqa: E402
from cashshifts.storage import init_cashshifts_tables, get_all_stores  # noqa: E402
from invoices.storage import (  # noqa: E402
    init_invoices_tables, get_all_expense_categories, create_city, create_payer,
)
from invoices.server import invoices_bp  # noqa: E402

failures = []

# 1x1 PNG — настоящий файл, а не текст: сервер проверяет расширение,
# а браузер прислал бы именно картинку
PNG_BYTES = bytes.fromhex(
    '89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489'
    '0000000a49444154789c6360000002000100ffff03000006000557bfabd4000000'
    '0049454e44ae426082'
)


def check(condition, message):
    print(('   OK    ' if condition else '   ПРОВАЛ ') + message)
    if not condition:
        failures.append(message)


def make_app():
    app = Flask(__name__)
    app.secret_key = 'test-only'
    app.config['TESTING'] = True
    # Без этого падение вьюхи прилетает трейсбеком в прогон и останавливает
    # его на первой же проверке. Нам нужен ответ, который увидит браузер, —
    # то есть 500, как на проде.
    app.config['PROPAGATE_EXCEPTIONS'] = False
    login_manager.init_app(app)
    app.register_blueprint(auth_bp)
    app.register_blueprint(invoices_bp)
    return app


def make_user(username, role, sections, store_ids=()):
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO users (username, password_hash, role, full_name, is_active, created_at) "
            "VALUES (?,?,?,?,1,?)",
            (username, generate_password_hash('pass'), role, username, datetime.utcnow().isoformat()),
        )
        conn.execute("DELETE FROM permissions WHERE username = ?", (username,))
        for section in sections:
            conn.execute("INSERT INTO permissions (username, module_name, can_view) VALUES (?,?,1)",
                         (username, section))
        for store_id in store_ids:
            conn.execute("INSERT OR IGNORE INTO user_stores (username, store_id) VALUES (?, ?)",
                         (username, store_id))
        conn.commit()
    finally:
        conn.close()


def login(client, username):
    response = client.post('/api/auth/login', json={'username': username, 'password': 'pass'})
    assert response.status_code == 200, f'вход {username} не удался: {response.data}'


def create_invoice(client, city_id, payer_id, store_id, category_id, purpose):
    response = client.post('/api/invoices', json={
        'city_id': city_id, 'payer_id': payer_id, 'due_date': '2026-09-10',
        'amount': 1000, 'payment_purpose': purpose,
        'counterparty_name': 'ООО Ромашка',
        'line_items': [{'store_id': store_id, 'expense_category_id': category_id, 'amount': 1000}],
    })
    assert response.status_code == 201, f'счёт не создан: {response.data}'
    return response.get_json()['invoice']['id']


def upload(client, invoice_id, filename, payload=PNG_BYTES):
    return client.post(
        f'/api/invoices/{invoice_id}/attachments',
        data={'file': (io.BytesIO(payload), filename)},
        content_type='multipart/form-data',
    )


def main():
    print("=== Обращение #2: вложения и фильтр «Автор» ===\n")

    init_cashshifts_tables()
    init_invoices_tables()
    init_auth_tables()

    stores = {store['name']: store['id'] for store in get_all_stores()}
    store_a = stores['НСК Восход, 3']
    store_b = stores['Челябинск Цвиллинга, 59']
    category_id = get_all_expense_categories()[0]['id']
    city_id = create_city('Новосибирск')
    payer_id = create_payer('ИП Тестовый')

    make_user('adm', 'admin', ['invoices_v2'])
    # Менеджер С САЛОНОМ — как на проде
    make_user('mgr', 'manager', ['invoices_v2'], store_ids=[store_a])
    make_user('mgr2', 'manager', ['invoices_v2'], store_ids=[store_a])
    # Менеджер другого города: его счета первому видеть нечем
    make_user('other', 'manager', ['invoices_v2'], store_ids=[store_b])

    app = make_app()

    print("1. Фильтр «Автор»")
    with app.test_client() as client:
        login(client, 'adm')
        response = client.get('/api/invoices/authors')
        check(response.status_code == 200, f"админ: {response.status_code}")

    with app.test_client() as client:
        login(client, 'mgr')
        response = client.get('/api/invoices/authors')
        # Регресс 224b28c: _visibility_clause стал возвращать (условие, параметры)
        # и принимать username, а здесь остался старый вызов с одним аргументом
        check(response.status_code == 200,
              f"менеджер с салоном: {response.status_code}")
    print()

    print("2. Загрузка вложения")
    with app.test_client() as client:
        login(client, 'mgr')
        own_id = create_invoice(client, city_id, payer_id, store_a, category_id, 'Свой счёт')

        response = upload(client, own_id, 'скан счёта.png')
        check(response.status_code == 201, f"к своему счёту: {response.status_code}")

        response = client.get(f'/api/invoices/{own_id}')
        attachments = response.get_json().get('attachments', [])
        check(len(attachments) == 1, f"вложение видно в карточке: {len(attachments)} шт.")
        if attachments:
            stored = os.path.join(TEST_ATTACHMENTS_DIR, attachments[0]['stored_filename'])
            check(os.path.exists(stored) and os.path.getsize(stored) == len(PNG_BYTES),
                  "файл лёг на диск целиком")
            check(attachments[0]['original_filename'] == 'скан счёта.png',
                  f"имя файла сохранено: {attachments[0]['original_filename']}")

            response = client.get(f'/api/invoices/attachments/{attachments[0]["id"]}/download')
            check(response.status_code == 200 and response.data == PNG_BYTES,
                  f"файл отдаётся обратно: {response.status_code}")

        response = upload(client, own_id, 'счёт.pdf', b'%PDF-1.4 fake')
        check(response.status_code == 201, f"pdf принимается: {response.status_code}")

        response = upload(client, own_id, 'вирус.exe', b'MZ')
        check(response.status_code == 400, f".exe отвергается: {response.status_code}")

    with app.test_client() as client:
        login(client, 'mgr2')
        foreign_id = create_invoice(client, city_id, payer_id, store_a, category_id, 'Счёт коллеги')
    with app.test_client() as client:
        login(client, 'mgr')
        response = upload(client, foreign_id, 'скан.png')
        check(response.status_code == 201,
              f"к чужому счёту своего салона: {response.status_code}")

    with app.test_client() as client:
        login(client, 'other')
        hidden_id = create_invoice(client, city_id, payer_id, store_b, category_id, 'Чужой город')
    with app.test_client() as client:
        login(client, 'mgr')
        response = upload(client, hidden_id, 'скан.png')
        check(response.status_code == 403,
              f"к невидимому счёту доступа нет: {response.status_code}")
    print()

    print("3. Список счетов и плитки менеджером с салоном")
    with app.test_client() as client:
        login(client, 'mgr')
        for url in ('/api/invoices', '/api/invoices/summary?today=2026-09-03'):
            response = client.get(url)
            check(response.status_code == 200, f"{url}: {response.status_code}")

    print()
    if failures:
        print(f"Провалов: {len(failures)}")
        for item in failures:
            print('  - ' + item)
    else:
        print("Всё сошлось")
    return 1 if failures else 0


if __name__ == '__main__':
    code = main()
    for path in (TEST_DB_PATH, TEST_DB_PATH + '-wal', TEST_DB_PATH + '-shm'):
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
    shutil.rmtree(TEST_ATTACHMENTS_DIR, ignore_errors=True)
    sys.exit(code)
