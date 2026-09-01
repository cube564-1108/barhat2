"""
Сторож правила «оплата с расчётного счёта» (решение владельца 01.09.2026).

Если счёт выставлен на юрлицо, которое платит с расчётного счёта (ИП Кваша,
ИП Насуленко, ООО Кофферс — BANK_TRANSFER_PAYER_KEYS в invoices/storage.py),
то по нему уйдёт платёжка в банк. Значит НДС и реквизиты контрагента должны
быть заполнены на вводе, а не всплывать через несколько дней у того, кто
отправляет счета в банк.

Проверяет на РЕАЛЬНОМ бэкенде (настоящие auth_bp и invoices_bp в минимальном
Flask-приложении на временной базе), что:
  * счёт на такого плательщика без реквизитов и НДС не создаётся, а ошибка
    перечисляет ровно недостающие поля;
  * с полным набором — создаётся;
  * КПП по-прежнему не обязателен (у контрагента-ИП его нет);
  * счёт на прочих плательщиков создаётся как раньше (регресс);
  * правка не может очистить обязательное поле, но перенос срока оплаты у
    старого счёта без реквизитов остаётся возможным;
  * справочник плательщиков отдаёт признак requires_bank_details — по нему
    форма ставит звёздочки, второго списка названий в JS нет.

Запуск: python scripts/test_bank_transfer_requisites.py
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

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), '_test_bank_transfer.db')
TEST_ATTACHMENTS_DIR = os.path.join(os.path.dirname(__file__), '_test_bank_transfer_attachments')
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
    create_vat_option, payer_requires_bank_details, payer_name_requires_bank_details,
)
from invoices.server import invoices_bp  # noqa: E402

failures = []

REQUISITES = {
    'counterparty_name': 'ООО Ромашка',
    'counterparty_inn': '7700000001',
    'counterparty_bank_name': 'АО «Модульбанк»',
    'counterparty_bank_bik': '044525092',
    'counterparty_bank_account': '40702810000000000001',
    'counterparty_bank_corr_account': '30101810645250000092',
}


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


def make_user(username, role, sections):
    conn = get_db()
    conn.execute(
        "INSERT INTO users (username, password_hash, role, full_name, is_active, created_at) "
        "VALUES (?,?,?,?,1,?)",
        (username, generate_password_hash('pass'), role, username, datetime.utcnow().isoformat()),
    )
    for section in sections:
        conn.execute("INSERT INTO permissions (username, module_name, can_view) VALUES (?,?,1)",
                     (username, section))
    conn.commit()
    conn.close()


def login(client, username):
    response = client.post('/api/auth/login', json={'username': username, 'password': 'pass'})
    assert response.status_code == 200, f'вход {username} не удался: {response.data}'


def main():
    print("=== Обязательные реквизиты при оплате с расчётного счёта ===\n")

    init_cashshifts_tables()
    init_invoices_tables()
    init_auth_tables()

    store_id = get_all_stores()[0]['id']
    category_id = get_all_expense_categories()[0]['id']
    city_id = create_city('Новосибирск')
    vat_id = create_vat_option('НДС 20%')
    bank_payer = create_payer('ИП Кваша Р. Е.')
    cash_payer = create_payer('ИП Тестовый')

    make_user('admin_test', 'admin', ['invoices_v2'])
    app = make_app()

    def payload(**overrides):
        data = {
            'city_id': city_id, 'payer_id': bank_payer, 'vat_id': vat_id,
            'due_date': '2026-09-10', 'amount': 1000,
            'payment_purpose': 'Цветы по счёту 5',
            'line_items': [{'store_id': store_id, 'expense_category_id': category_id, 'amount': 1000}],
        }
        data.update(REQUISITES)
        data.update(overrides)
        return data

    print("1. Правило считается по названию плательщика, а не по id")
    check(payer_requires_bank_details(bank_payer) is True, "ИП Кваша Р. Е. платит с расчётного счёта")
    check(payer_requires_bank_details(cash_payer) is False, "прочий плательщик под правило не подпадает")
    check(payer_name_requires_bank_details('ООО «Кофферс»') is True, "название в кавычках распознаётся")
    check(payer_name_requires_bank_details('ИП НАСУЛЕНКО') is True, "регистр названия роли не играет")
    check(payer_requires_bank_details(None) is False, "пустой плательщик реквизитов не требует")
    # Баг 01.09.2026: в справочнике есть плательщики-карты, и юрлицо у них
    # указано в скобках. Поиск фрагмента где угодно в строке требовал с них
    # реквизиты и НДС, хотя платят по ним картой.
    check(payer_name_requires_bank_details('Карта Насти Н. (Кофферс)') is False,
          "плательщик-карта с юрлицом в скобках реквизитов не требует")
    check(payer_name_requires_bank_details('Карта НСК (ИП Кваша)') is False,
          "и карта, у которой юрлицо названо полностью, — тоже")
    check(payer_name_requires_bank_details('Кваша Р.Е.') is True,
          "юрлицо без формы «ИП» в начале названия правило всё равно узнаёт")

    with app.test_client() as client:
        login(client, 'admin_test')

        print("\n2. Создание счёта")
        response = client.post('/api/invoices', json=payload(
            counterparty_name=None, counterparty_inn=None, counterparty_bank_name=None,
            counterparty_bank_bik=None, counterparty_bank_account=None,
            counterparty_bank_corr_account=None, vat_id=None,
        ))
        error = (response.get_json() or {}).get('error', '')
        check(response.status_code == 400, "счёт без реквизитов и НДС не создаётся")
        check(all(word in error for word in
                  ('НДС', 'Контрагент', 'ИНН', 'Банк', 'БИК', 'Расчётный счёт', 'Корр. счёт')),
              f"ошибка перечисляет все недостающие поля: {error}")

        response = client.post('/api/invoices', json=payload(vat_id=None))
        error = (response.get_json() or {}).get('error', '')
        check(response.status_code == 400 and 'НДС' in error and 'ИНН' not in error,
              f"не хватает только НДС — про него и сказано: {error}")

        response = client.post('/api/invoices', json=payload(counterparty_bank_corr_account='   '))
        check(response.status_code == 400 and 'Корр. счёт' in (response.get_json() or {}).get('error', ''),
              "поле из одних пробелов считается незаполненным")

        response = client.post('/api/invoices', json=payload())
        full_invoice = (response.get_json() or {}).get('invoice', {})
        check(response.status_code == 201, "с полным набором реквизитов и НДС счёт создаётся")
        check(response.status_code == 201 and not full_invoice.get('counterparty_kpp'),
              "КПП остаётся необязательным — у контрагента-ИП его нет")

        print("\n3. Регресс: прочие плательщики")
        response = client.post('/api/invoices', json=payload(
            payer_id=cash_payer, vat_id=None,
            counterparty_name=None, counterparty_inn=None, counterparty_bank_name=None,
            counterparty_bank_bik=None, counterparty_bank_account=None,
            counterparty_bank_corr_account=None,
        ))
        legacy_invoice = (response.get_json() or {}).get('invoice', {})
        check(response.status_code == 201, "счёт на прочего плательщика создаётся без реквизитов, как раньше")

        print("\n4. Правка счёта")
        invoice_id = full_invoice.get('id')
        response = client.put(f'/api/invoices/{invoice_id}', json={'counterparty_inn': ''})
        check(response.status_code == 400 and 'ИНН' in (response.get_json() or {}).get('error', ''),
              "очистить обязательный реквизит правкой нельзя")

        response = client.put(f'/api/invoices/{invoice_id}', json={'vat_id': None})
        check(response.status_code == 400 and 'НДС' in (response.get_json() or {}).get('error', ''),
              "снять НДС правкой нельзя")

        response = client.put(f'/api/invoices/{invoice_id}', json={'due_date': '2026-09-20'})
        check(response.status_code == 200, "срок оплаты правится как раньше")

        legacy_id = legacy_invoice.get('id')
        response = client.put(f'/api/invoices/{legacy_id}', json={'due_date': '2026-09-20'})
        check(response.status_code == 200,
              "у старого счёта без реквизитов перенос срока оплаты не блокируется")

        response = client.put(f'/api/invoices/{legacy_id}', json={'payer_id': bank_payer})
        check(response.status_code == 400,
              "перевод счёта на плательщика с расчётным счётом требует реквизиты сразу")

        print("\n5. Признак для формы")
        response = client.get('/api/invoices/payers')
        payers = {item['name']: item for item in (response.get_json() or {}).get('payers', [])}
        check(payers.get('ИП Кваша Р. Е.', {}).get('requires_bank_details') is True,
              "справочник отдаёт requires_bank_details=true для плательщика с расчётным счётом")
        check(payers.get('ИП Тестовый', {}).get('requires_bank_details') is False,
              "и false для прочих")

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
