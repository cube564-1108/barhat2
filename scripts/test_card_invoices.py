"""
Прогон Фаз 1-2 плана plans/2026-08-29-рабочие-карты.md — три типа заявки.

Проверяет на РЕАЛЬНОМ бэкенде (настоящие auth_bp и invoices_bp в минимальном
Flask-приложении на временной базе), что:
  * счёт на оплату работает ровно как раньше (регресс),
  * трата с карты и пополнение карты создаются по своим правилам,
  * карточные заявки не уходят в банк и не попадают в старый REF-синк
    (иначе пополнение легло бы расходом и рубль учёлся бы дважды),
  * управляющий не может списать с чужой карты (а вот распределить трату по
    салонам любых городов — может: карта это источник денег, а не адресат),
  * платёжные KPI-плитки не засоряются тратами с карты,
  * остаток подотчёта считается верно, а фильтры по типу и карте работают,
  * пополнение без распределения видно в списке всем управляющим своего города.

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
    mark_invoice_paid, get_invoice_line_items,
)
from invoices.server import invoices_bp, _send_invoice_to_bank, _match_planfact_operation  # noqa: E402
from invoices.cards import list_cards, get_cards_balances  # noqa: E402

# Парная половина require_ajax_header: эти ручки — POST без тела, то есть
# простой запрос, который отправит форма с чужого сайта. Ходим с заголовком,
# а отсутствие его проверяется отдельно (правило CLAUDE.md).
AJAX = {"X-Requested-With": "barhat-dashboard"}

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
    # Плательщик здесь намеренно НЕ из тех, кто платит с расчётного счёта
    # (ИП Кваша и т.п.): у тех обязательны реквизиты и НДС, а этот тест про
    # карты — см. scripts/test_bank_transfer_requisites.py.
    payer_id = create_payer('ИП Тестовый')

    cards = {card['title']: card for card in list_cards()}
    nsk_card = cards['Рабочая карта НСК']
    chlb_card = cards['Рабочая карта ЧЛБ ГПБ']
    nsk_store = stores['НСК Восход, 3']
    chlb_store = stores['Челябинск Цвиллинга, 59']

    make_user('admin_test', 'admin', ['invoices_v2'])
    make_user('nsk_manager', 'manager', ['invoices_v2'], store_ids=[nsk_store])
    make_user('chlb_manager', 'manager', ['invoices_v2'], store_ids=[chlb_store])
    # Роль без особых прав: распределение траты на салон другого города должно
    # работать и у неё — ограничение снято для всех, а не выдано избранным.
    make_user('nsk_florist', 'florist', ['invoices_v2'], store_ids=[nsk_store])

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

    print("\n6. Несогласованную трату оплаченной не отмечают")
    # До 21.09.2026 здесь стоял отказ «у траты с карты нет статуса оплачен» —
    # по ТИПУ заявки. Из-за него трата навсегда оставалась в «Согласован».
    # Теперь отказ только по СТАТУСУ: несогласованное платить нечем.
    with app.test_client() as client:
        login(client, 'admin_test')
        response = client.post(f"/api/invoices/{expense_row['id']}/mark-paid",
                               headers=AJAX)
        check(response.status_code == 409, "трата на согласовании оплаченной не становится")
        reason = (response.get_json() or {}).get('error') or ''
        check('статус' in reason.lower(),
              f"и отказ объясняет себя статусом, а не типом заявки: {reason}")

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

    print("\n10. Остаток подотчёта по карте")
    # Пополнение 60 000 переведено, трата 1 250 ждёт подтверждения
    mark_invoice_paid(topup['id'], 'admin_test')
    balances = get_cards_balances()
    nsk = balances.get(nsk_card['id'], {})
    check(nsk.get('issued') == 60000, f"выдано 60000 (получено {nsk.get('issued')})")
    check(nsk.get('spent_pending') == 1250, f"ждёт подтверждения 1250 (получено {nsk.get('spent_pending')})")
    check(nsk.get('balance') == 58750, f"остаток 58750 (получено {nsk.get('balance')})")

    with app.test_client() as client:
        login(client, 'nsk_manager')
        response = client.get('/api/invoices/work-cards?with_balance=true')
        card = response.get_json()['work_cards'][0]
        check(card.get('balance', {}).get('balance') == 58750,
              "остаток приходит в списке карт для формы")

    print("\n11. Фильтры списка по типу и карте")
    with app.test_client() as client:
        login(client, 'admin_test')
        response = client.get('/api/invoices?kind=card_expense')
        rows = response.get_json()['invoices']
        check(len(rows) == 1 and rows[0]['kind'] == 'card_expense',
              f"фильтр по типу отдаёт только траты (получено {len(rows)})")

        response = client.get(f"/api/invoices?card_id={nsk_card['id']}")
        rows = response.get_json()['invoices']
        check(len(rows) == 2, f"фильтр по карте отдаёт обе её заявки (получено {len(rows)})")

        response = client.get('/api/invoices?kind=invoice')
        rows = response.get_json()['invoices']
        check(len(rows) == 1 and rows[0]['kind'] == 'invoice',
              "обычные счета отбираются отдельно от карточных")

    print("\n12. Пополнение видно в СПИСКЕ второму управляющему города")
    with app.test_client() as client:
        login(client, 'nsk_manager2')
        rows = client.get('/api/invoices').get_json()['invoices']
        check(any(row['id'] == topup['id'] for row in rows),
              "заявку без распределения находит видимость по карте, а не по строкам")

    with app.test_client() as client:
        login(client, 'chlb_manager')
        rows = client.get('/api/invoices').get_json()['invoices']
        check(not any(row['id'] == topup['id'] for row in rows),
              "управляющему другого города чужое пополнение в списке не показывается")

    print("\n13. Распределение траты не зависит от карты")
    # Последним разделом сознательно: создаёт ещё одну заявку, а разделы 7,
    # 10 и 11 считают счета и остаток по карте — вставленный выше, он сдвигал
    # их все сразу и выглядел бы шестью разными поломками.
    with app.test_client() as client:
        login(client, 'nsk_manager')
        response = client.post('/api/invoices', json={
            'kind': 'card_expense', 'card_id': nsk_card['id'], 'spent_at': '2026-08-25',
            'amount': 100, 'payment_purpose': 'салон другого города',
            'line_items': [{'store_id': chlb_store, 'expense_category_id': category_id, 'amount': 100}],
        })
        check(response.status_code == 201,
              "менеджер может развести трату с карты НСК на салон Челябинска")
        cross = response.get_json().get('invoice', {})
        cross_items = get_invoice_line_items(cross['id']) if cross.get('id') else []
        check(any(item['store_id'] == chlb_store for item in cross_items),
              "салон другого города сохраняется в распределении, а не теряется по пути")

        # Правка такой заявки не должна упираться в ту же проверку: иначе
        # трату можно завести, но нельзя поправить в ней сумму.
        response = client.put('/api/invoices/' + str(cross['id']), json={
            'line_items': [{'store_id': chlb_store,
                            'expense_category_id': category_id, 'amount': 120}],
            'amount': 120,
        })
        check(response.status_code == 200,
              "заведённую трату на чужой город можно поправить")

        # Ручка распределения — третий вход, и вести себя должна так же.
        response = client.put('/api/invoices/' + str(cross['id']) + '/line-items', json={
            'items': [{'store_id': chlb_store,
                       'expense_category_id': category_id, 'amount': 120}],
        })
        check(response.status_code == 200,
              "менеджер меняет распределение траты на чужой город через /line-items")

        # Случай владельца целиком: одна покупка с карты одного города
        # закрывает салоны нескольких городов, и каждый рубль ложится в свой
        # проект. Ради этого ограничение и снималось.
        response = client.post('/api/invoices', json={
            'kind': 'card_expense', 'card_id': nsk_card['id'], 'spent_at': '2026-08-26',
            'amount': 300, 'payment_purpose': 'покупка на два города',
            'line_items': [
                {'store_id': nsk_store, 'expense_category_id': category_id, 'amount': 100},
                {'store_id': chlb_store, 'expense_category_id': category_id, 'amount': 200},
            ],
        })
        check(response.status_code == 201,
              "одна трата делится между салонами разных городов")
        split = response.get_json().get('invoice', {})
        split_items = get_invoice_line_items(split['id']) if split.get('id') else []
        by_store = {item['store_id']: item['amount'] for item in split_items}
        check(by_store.get(nsk_store) == 100 and by_store.get(chlb_store) == 200,
              "каждый салон получает свою сумму, а не сумму карты")

    # Ограничение снято для всех, а не выдано избранным ролям. Проверяем на
    # СВОЕЙ заявке флориста: чужую он не правит по другой причине (право
    # редактировать чужой счёт), и та ветка увела бы проверку не туда.
    with app.test_client() as client:
        login(client, 'nsk_florist')
        response = client.post('/api/invoices', json={
            'kind': 'card_expense', 'card_id': nsk_card['id'], 'spent_at': '2026-08-26',
            'amount': 50, 'payment_purpose': 'флорист, чужой город',
            'line_items': [{'store_id': chlb_store,
                            'expense_category_id': category_id, 'amount': 50}],
        })
        check(response.status_code == 201,
              "распределение на другой город не упирается в роль")

    # Управляющий принимающего города видит трату по своей строке
    # распределения, но чужой картой не владеет. Правка не должна упираться в
    # «Нет доступа к этой карте»: карта нужна, чтобы её сменить, а не чтобы
    # сохранить форму с той же самой.
    with app.test_client() as client:
        login(client, 'chlb_manager')
        response = client.put('/api/invoices/' + str(cross['id']), json={
            'card_id': nsk_card['id'], 'payment_purpose': 'уточнил назначение',
        })
        check(response.status_code == 200,
              "управляющий принимающего города правит трату, не владея её картой")

        response = client.put('/api/invoices/' + str(cross['id']), json={
            'card_id': chlb_card['id'],
        })
        check(response.status_code == 403,
              "но переставить трату на свою карту он всё равно не может")

    print("\n14. Трата с карты доходит до «Оплачен» и статус двигается руками")
    # Последним разделом: меняет статус заявки, которую считают разделы 7 и 10.
    # Жалоба владельца 21.09.2026: траты с карты навсегда оставались в
    # «Согласован» — кнопка «Отметить оплаченным» отвечала 409 по типу заявки,
    # а больше статус не двигал никто.
    approve_invoice(expense_row['id'], 'admin_test')
    with app.test_client() as client:
        login(client, 'admin_test')
        response = client.post(f"/api/invoices/{expense_row['id']}/mark-paid",
                               headers=AJAX)
        check(response.status_code == 200,
              f"согласованную трату отмечаем оплаченной (получено {response.status_code})")
        paid_row = get_invoice_by_id(expense_row['id'])
        check(paid_row['status'] == 'paid', f"статус стал «Оплачен» (получено {paid_row['status']})")

        # Без заголовка ручка обязана отвечать 403: POST без тела — простой
        # запрос, его отправит обычная форма с чужого сайта, а CSRF-токенов в
        # проекте нет. 21.09.2026 правка сняла отказ по типу заявки, то есть
        # расширила, что можно перещёлкнуть извне, — декоратор стал обязателен.
        naked = client.post(f"/api/invoices/{expense_row['id']}/mark-paid")
        check(naked.status_code == 403,
              f"без X-Requested-With оплата не проходит (получено {naked.status_code})")
        naked_archive = client.post(f"/api/invoices/{expense_row['id']}/archive")
        check(naked_archive.status_code == 403,
              f"и архивация тоже (получено {naked_archive.status_code})")
        check(paid_row['paid_at'], "дата оплаты проставлена — иначе история перехода пустая")

        # Ручная смена статуса: нужна там, где процесс не сработал вовсе.
        response = client.put(f"/api/invoices/{expense_row['id']}/status",
                              json={'status': 'approved'}, headers=AJAX)
        check(response.status_code == 200
              and get_invoice_by_id(expense_row['id'])['status'] == 'approved',
              "админ возвращает статус назад руками")
        response = client.put(f"/api/invoices/{expense_row['id']}/status",
                              json={'status': 'такого статуса нет'}, headers=AJAX)
        check(response.status_code == 400, "выдуманный статус не принимается")

        # Ручка переключает ЛЮБОЙ статус, включая «Оплачен», и с 21.09.2026 у
        # неё есть вход из интерфейса. PUT с чужого сайта не уйдёт и так
        # (нужна CORS-предпроверка), но декоратор снимает вопрос на случай,
        # если ручку когда-нибудь продублируют POST'ом.
        naked_status = client.put(f"/api/invoices/{expense_row['id']}/status",
                                  json={'status': 'paid'})
        check(naked_status.status_code == 403,
              f"без X-Requested-With статус не переключить (получено {naked_status.status_code})")

    with app.test_client() as client:
        login(client, 'nsk_manager')
        # С заголовком: иначе 403 пришёл бы от require_ajax_header, и проверка
        # роли не исполнилась бы вовсе — сторож зеленел бы не по той причине.
        response = client.put(f"/api/invoices/{expense_row['id']}/status",
                              json={'status': 'paid'}, headers=AJAX)
        check(response.status_code == 403, "управляющий статус руками не двигает")

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
