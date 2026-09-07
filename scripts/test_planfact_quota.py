"""
Сторож месячной квоты API ПланФакта (07.09.2026).

У тарифа ПланФакта лимит не посекундный, а МЕСЯЧНЫЙ: 2500 запросов, сброс
первого числа. К седьмому сентября он кончился, ПланФакт начал отвечать 403 на
любой запрос, и встало всё: остатки карт, разноска счетов, фоновая разноска
трат. В интерфейсе при этом было написано «ПланФакт не ответил» — то же самое,
что при обрыве сети.

Проверяем то, из-за чего это стало возможно:

  * остаток квоты читается из заголовков ЛЮБОГО ответа, включая ошибочный;
  * исчерпание названо словами и отличимо от «сервис молчит»;
  * при исчерпанной квоте клиент вообще не ходит в сеть — до момента сброса;
  * момент сброса снимает блокировку сам;
  * состояние переживает перезапуск процесса (общее для обоих воркеров);
  * справочники (счета/проекты/статьи) ходят наружу раз в 6 часов, а не на
    каждое открытие вкладки;
  * заявка, которая уже падала, не заставляет планировщик ходить в ПланФакт
    каждый тик, но кнопка «Разнести сейчас» её берёт;
  * прогон разноски при исчерпанной квоте останавливается до внешних вызовов.

Сеть отключена намеренно: ни один тест не имеет права уйти наружу.

Запуск: python scripts/test_planfact_quota.py
"""

import io
import json
import os
import socket
import sys
import time
from datetime import datetime, timedelta, timezone

if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')


class NoNetwork(socket.socket):
    def connect(self, *a, **k):
        raise OSError("сеть отключена намеренно")

    def connect_ex(self, *a, **k):
        raise OSError("сеть отключена намеренно")


socket.socket = NoNetwork

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), '_test_planfact_quota.db')
for path in (TEST_DB_PATH, TEST_DB_PATH + '-wal', TEST_DB_PATH + '-shm'):
    if os.path.exists(path):
        os.remove(path)
os.environ['BARHAT_DB_PATH'] = TEST_DB_PATH
os.environ['INVOICE_ATTACHMENTS_DIR'] = os.path.join(os.path.dirname(__file__),
                                                     '_test_planfact_quota_attachments')
# Ключ фиктивный и выставлен ДО импорта клиента: load_dotenv не перезаписывает
# уже заданные переменные, поэтому боевой ключ из .env сюда не попадёт.
os.environ['PLANFACT_API_KEY'] = 'test-key-not-real'
os.environ['CARD_SYNC_SCHEDULER'] = '0'

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from flask import Flask  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

from auth import auth_bp, login_manager, init_auth_tables, get_db  # noqa: E402
from cashshifts.storage import init_cashshifts_tables, get_all_stores  # noqa: E402
from invoices.storage import (  # noqa: E402
    init_invoices_tables, get_all_expense_categories, set_store_planfact_project,
)
from invoices import cards as cards_module  # noqa: E402
from invoices import cards_sync  # noqa: E402
from invoices import planfact_refs  # noqa: E402
from invoices.server import invoices_bp  # noqa: E402
from planfact import quota as planfact_quota  # noqa: E402
from planfact.client import PlanFactClient  # noqa: E402

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


class FakeResponse:
    """Ответ ПланФакта с заголовками квоты — то, что видит клиент."""

    def __init__(self, status_code, headers, payload):
        self.status_code = status_code
        self.headers = headers
        self._payload = payload
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.exceptions.HTTPError(f"{self.status_code} Client Error", response=self)


def quota_headers(limit=2500, used=100, remaining=None, reset_at=None):
    reset = reset_at or (datetime.now(timezone.utc) + timedelta(days=20))
    return {
        'X-RateLimit-Limit': '20',
        'X-Quota-Limit': str(limit),
        'X-Quota-Used': str(used),
        'X-Quota-Remaining': str(limit - used if remaining is None else remaining),
        'X-Quota-Reset': str(int(reset.timestamp())),
    }


class FakeSession:
    """Считает походы в сеть. Настоящих запросов не делает."""

    def __init__(self, response_factory):
        self.calls = []
        self._factory = response_factory
        self.trust_env = False
        self.proxies = {}
        self.verify = True

    def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        return self._factory()

    def mount(self, *a, **k):
        pass


def client_with(response_factory):
    client = PlanFactClient()
    client.session = FakeSession(response_factory)
    return client


def clear_quota_state():
    """Полный сброс: и память процесса, и общее хранилище."""
    planfact_quota.reset_for_tests()
    cards_module.write_sync_state('planfact_quota', '')


def main():
    print("=== Месячная квота API ПланФакта ===\n")

    init_cashshifts_tables()
    init_invoices_tables()
    init_auth_tables()

    stores = {store['name']: store['id'] for store in get_all_stores()}
    nsk_store = stores['НСК Восход, 3']
    category_id = get_all_expense_categories()[0]['id']
    nsk_card = {card['title']: card for card in cards_module.list_cards()}['Рабочая карта НСК']

    make_user('admin_q', 'admin', ['invoices_v2'], store_ids=[nsk_store])
    app = make_app()
    admin = app.test_client()
    login(admin, 'admin_q')

    # ------------------------------------------------------------------
    print("1. Остаток квоты читается из успешного ответа")
    clear_quota_state()
    ok_client = client_with(lambda: FakeResponse(
        200, quota_headers(used=100), {"isSuccess": True, "data": {"items": [{"projectId": 1}]}}))
    ok_client.get_projects()
    state = planfact_quota.snapshot()
    check(state.get('known') and state.get('remaining') == 2400,
          f"после успешного запроса известен остаток: {state.get('remaining')} из {state.get('limit')}")
    check(state.get('blocked') is False, "работать не мешаем: квота есть")

    # ------------------------------------------------------------------
    print("\n2. 403 «лимит исчерпан» отличим от «сервис молчит»")
    clear_quota_state()
    reset_moment = datetime.now(timezone.utc) + timedelta(days=23)
    exhausted = client_with(lambda: FakeResponse(
        403, quota_headers(used=2500, remaining=0, reset_at=reset_moment),
        {"isSuccess": False,
         "errorMessage": "Использован лимит запросов к API. Обратитесь в поддержку"}))
    check(exhausted.get_projects() is None, "запрос при исчерпанной квоте данных не даёт")
    check(planfact_quota.is_blocked(), "состояние — «исчерпано»")
    text = planfact_quota.error_text() or ''
    check('лимит' in text.lower() and '2500' in text,
          f"причина названа словами: {text}")
    snapshot = planfact_quota.snapshot()
    check(snapshot.get('reset_at', '')[:10] == reset_moment.strftime('%Y-%m-%d'),
          f"момент сброса сохранён: {snapshot.get('reset_at')}")

    # ------------------------------------------------------------------
    print("\n3. При исчерпанной квоте в сеть не ходим вовсе")
    silent = client_with(lambda: FakeResponse(200, quota_headers(), {"isSuccess": True, "data": {}}))
    check(silent.get_accounts() is None, "вызов возвращает None, не пытаясь спросить")
    check(not silent.session.calls,
          f"походов в сеть нет (было {len(silent.session.calls)})")

    # ------------------------------------------------------------------
    print("\n4. Состояние переживает перезапуск процесса и виден соседнему воркеру")
    planfact_quota.reset_for_tests()          # как будто воркер только поднялся
    restored = planfact_quota.snapshot(reload=True)
    check(restored.get('known') and restored.get('blocked') is True,
          "новый процесс читает «квота исчерпана» из общего хранилища")
    check(restored.get('used') == 2500, f"с цифрами: использовано {restored.get('used')}")

    # ------------------------------------------------------------------
    print("\n5. Момент сброса снимает блокировку сам")
    clear_quota_state()
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    planfact_quota.record_response(quota_headers(used=2500, remaining=0, reset_at=past), 403,
                                   "Использован лимит запросов к API")
    check(not planfact_quota.is_blocked(), "после наступления сброса запросы снова разрешены")
    resumed = client_with(lambda: FakeResponse(200, quota_headers(used=3),
                                               {"isSuccess": True, "data": {"items": []}}))
    resumed.get_projects()
    check(len(resumed.session.calls) == 1, "и запрос действительно уходит")

    # ------------------------------------------------------------------
    print("\n6. Вкладка «Остатки на картах» говорит про лимит, а не «не ответил»")
    clear_quota_state()
    planfact_quota.record_response(quota_headers(used=2500, remaining=0), 403,
                                   "Использован лимит запросов к API")
    live_calls = []
    original_fetch = cards_module.fetch_planfact_balances

    def counting_fetch(account_ids):
        live_calls.append(list(account_ids))
        raise AssertionError("при исчерпанной квоте ходить в ПланФакт нельзя")

    cards_module.fetch_planfact_balances = counting_fetch
    try:
        response = admin.get('/api/invoices/work-cards/balances')
    finally:
        cards_module.fetch_planfact_balances = original_fetch

    data = response.get_json()
    check(response.status_code == 200, "ручка не падает")
    check(not live_calls, "внешний вызов не сделан")
    check('лимит' in (data.get('error') or '').lower(),
          f"текст про лимит, а не «не ответил»: {data.get('error')}")
    check((data.get('quota') or {}).get('blocked') is True,
          "в ответе есть состояние квоты — интерфейсу есть что показать")
    check((data.get('quota') or {}).get('limit') == 2500,
          "с лимитом и остатком, а не только флагом")

    # ------------------------------------------------------------------
    print("\n7. Квота видна и когда всё в порядке")
    clear_quota_state()
    planfact_quota.record_response(quota_headers(used=40), 200, "")
    # Фейк вместо живого вызова: проверяем показ квоты, а не поход наружу
    cards_module.fetch_planfact_balances = lambda account_ids: {a: 0.0 for a in account_ids}
    try:
        data = admin.get('/api/invoices/work-cards/balances').get_json()
    finally:
        cards_module.fetch_planfact_balances = original_fetch
    quota = data.get('quota') or {}
    check(quota.get('remaining') == 2460 and quota.get('blocked') is False,
          f"остаток показывается заранее: {quota.get('remaining')} из {quota.get('limit')}")

    # ------------------------------------------------------------------
    print("\n8. Справочники не ходят наружу на каждое открытие вкладки")
    clear_quota_state()
    fetches = []

    def fake_accounts():
        fetches.append(1)
        return [{"accountId": 764679, "title": "Рабочая карта НСК", "active": True}]

    planfact_refs.get_reference('accounts', fake_accounts)
    for _ in range(5):
        planfact_refs.get_reference('accounts', fake_accounts)
    check(len(fetches) == 1, f"шесть открытий — один поход наружу (было {len(fetches)})")

    result = planfact_refs.get_reference('accounts', fake_accounts, refresh=True)
    check(len(fetches) == 1, "кнопка «Обновить» сразу после загрузки квоту не тратит")
    check(result['items'] and result['items'][0]['accountId'] == 764679, "список отдан из кэша")

    conn = get_db()
    conn.execute("UPDATE planfact_reference_cache SET fetched_at = datetime('now', '-7 hours')")
    conn.commit()
    conn.close()
    planfact_refs.get_reference('accounts', fake_accounts)
    check(len(fetches) == 2, "через шесть часов список перечитывается")

    print("\n9. Устаревший справочник переживает исчерпание квоты")
    planfact_quota.record_response(quota_headers(used=2500, remaining=0), 403,
                                   "Использован лимит запросов к API")
    conn = get_db()
    conn.execute("UPDATE planfact_reference_cache SET fetched_at = datetime('now', '-9 hours')")
    conn.commit()
    conn.close()
    stale = planfact_refs.get_reference('accounts', fake_accounts)
    check(len(fetches) == 2, "наружу не пошли")
    check(stale['items'] and stale['stale'] is True, "старый список отдан с пометкой «устарел»")
    check('лимит' in (stale['error'] or '').lower(), f"с причиной: {stale['error']}")

    # ------------------------------------------------------------------
    print("\n10. Заявка, которая падает, не жжёт квоту каждый тик")
    clear_quota_state()
    response = admin.post('/api/invoices', json={
        'kind': 'card_expense', 'card_id': nsk_card['id'], 'spent_at': '2026-09-01',
        'amount': 1500, 'payment_purpose': 'Упаковка',
        'line_items': [{'store_id': nsk_store, 'expense_category_id': category_id, 'amount': 1500}],
    })
    invoice_id = response.get_json().get('invoice', {}).get('id') or response.get_json().get('id')
    check(invoice_id is not None, f"заявка создана: {invoice_id}")

    conn = get_db()
    conn.execute("UPDATE invoices SET status = 'approved' WHERE id = ?", (invoice_id,))
    conn.commit()
    conn.close()

    check(any(row['id'] == invoice_id for row in cards_sync.collect_candidates()),
          "новая заявка в очереди на разноску")

    cards_sync.set_invoice_planfact_error(invoice_id, "Не настроено сопоставление с ПланФакт")
    check(not any(row['id'] == invoice_id for row in cards_sync.collect_candidates()),
          "после падения заявка выпадает из очереди фонового прогона")
    check(any(row['id'] == invoice_id for row in cards_sync.collect_candidates(force=True)),
          "но кнопка «Разнести сейчас» её берёт")

    # Отсрочка не должна пережить починку того, из-за чего заявка падала
    set_store_planfact_project(nsk_store, '5001')
    check(any(row['id'] == invoice_id for row in cards_sync.collect_candidates()),
          "правка сопоставления снимает отсрочку — ждать шесть часов не нужно")

    cards_sync.set_invoice_planfact_error(invoice_id, "Не настроено сопоставление с ПланФакт")
    conn = get_db()
    conn.execute("UPDATE invoices SET planfact_attempted_at = datetime('now', '-7 hours') "
                 "WHERE id = ?", (invoice_id,))
    conn.commit()
    conn.close()
    check(any(row['id'] == invoice_id for row in cards_sync.collect_candidates()),
          "через шесть часов попытка повторяется сама")

    check(cards_sync.SCHEDULER_INTERVAL_SECONDS >= 3600,
          f"тик планировщика не чаще часа: {cards_sync.SCHEDULER_INTERVAL_SECONDS} с")

    # ------------------------------------------------------------------
    print("\n11. Прогон разноски при исчерпанной квоте не идёт в ПланФакт")
    planfact_quota.record_response(quota_headers(used=2500, remaining=0), 403,
                                   "Использован лимит запросов к API")
    marker_calls = []
    original_markers = cards_sync._known_markers

    def counting_markers(client, date_start):
        marker_calls.append(date_start)
        raise AssertionError("при исчерпанной квоте ходить в ПланФакт нельзя")

    cards_sync._known_markers = counting_markers
    try:
        outcome = cards_sync.run_card_sync(force=True)
    finally:
        cards_sync._known_markers = original_markers

    check(not marker_calls, "поиск маркеров не запускался")
    check('лимит' in (outcome.get('skipped') or '').lower(),
          f"прогон объясняет, почему отложен: {outcome.get('skipped')}")

    print("\n" + "=" * 60)
    if failures:
        print(f"ПРОВАЛОВ: {len(failures)}")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("ВСЁ ЗЕЛЁНОЕ")
    return 0


if __name__ == '__main__':
    code = main()
    for path in (TEST_DB_PATH, TEST_DB_PATH + '-wal', TEST_DB_PATH + '-shm'):
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
    sys.exit(code)
