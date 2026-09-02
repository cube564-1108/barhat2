"""
Сторож модуля обратной связи (plans/2026-09-02-обратная-связь-от-пользователей.md).

Держит четыре решения, которые легко потерять при следующей правке:

  1. ПРИВАТНОСТЬ. Сотрудник видит только свои обращения. Подсказка «похоже на
     это» не показывает неразобранное чужое — иначе первая же жалоба на
     коллегу уедет другому сотруднику.
  2. CSRF. POST без заголовка X-Requested-With отвергается: у сессий из портала
     Пульс кука уходит с SameSite=None, и защиты Lax на них нет.
  3. ДЕДУП. Повторное «у меня тоже» не задваивает вес обращения.
  4. РАЗБОР. Отклонение без текста для автора запрещено; принятое уезжает в
     бэклог и связывается с задачей.

Проверяется на РЕАЛЬНОМ бэкенде (настоящие auth_bp, feedback_bp и tasks_bp
в минимальном Flask-приложении на временной базе).

Запуск: python scripts/test_feedback.py
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

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), '_test_feedback.db')
for path in (TEST_DB_PATH, TEST_DB_PATH + '-wal', TEST_DB_PATH + '-shm'):
    if os.path.exists(path):
        os.remove(path)
os.environ['BARHAT_DB_PATH'] = TEST_DB_PATH

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from flask import Flask  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

from auth import auth_bp, login_manager, init_auth_tables, get_db  # noqa: E402
from auth import AJAX_HEADER, AJAX_HEADER_VALUE  # noqa: E402
from feedback.server import feedback_bp  # noqa: E402
from feedback.storage import (  # noqa: E402
    RATE_LIMIT_PER_HOUR, init_feedback_tables, get_item,
)
from tasks.server import tasks_bp  # noqa: E402
from tasks.storage import init_tasks_tables  # noqa: E402

AJAX = {AJAX_HEADER: AJAX_HEADER_VALUE}

failures = []


def check(condition, message):
    print(('   OK     ' if condition else '   ПРОВАЛ ') + message)
    if not condition:
        failures.append(message)


def make_app():
    app = Flask(__name__)
    app.secret_key = 'test-only'
    app.config['TESTING'] = True
    login_manager.init_app(app)
    app.register_blueprint(auth_bp)
    app.register_blueprint(feedback_bp)
    app.register_blueprint(tasks_bp)
    return app


def make_user(username, role):
    conn = get_db()
    conn.execute(
        "INSERT INTO users (username, password_hash, role, full_name, is_active, created_at) "
        "VALUES (?,?,?,?,1,?)",
        (username, generate_password_hash('pass'), role, username, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def login(client, username):
    response = client.post('/api/auth/login', json={'username': username, 'password': 'pass'})
    assert response.status_code == 200, f'вход {username} не удался: {response.data}'


def send(client, text, type_='inconvenience', module='cash_shifts', **extra):
    payload = {'text': text, 'type': type_, 'module': module}
    payload.update(extra)
    return client.post('/api/feedback', json=payload, headers=AJAX)


def main():
    print("=== Обратная связь от сотрудников ===\n")

    init_auth_tables()
    init_tasks_tables()
    init_feedback_tables()

    make_user('owner_test', 'admin')
    make_user('florist_test', 'florist')
    make_user('manager_test', 'manager')

    app = make_app()
    owner = app.test_client()
    florist = app.test_client()
    manager = app.test_client()
    login(owner, 'owner_test')
    login(florist, 'florist_test')
    login(manager, 'manager_test')

    print("1. Отправка обращения")
    response = send(
        florist,
        'При закрытии смены сумма наличных сбрасывается, приходится вводить заново',
        type_='bug',
        page_url='/cash-shifts',
        client_context={
            'screen': '390x844',
            'errors': [{'path': '/api/cashshifts/close', 'status': '500',
                        'error': 'internal error', 'at': '12:31'}],
            'secret': 'это поле должно быть выброшено',
        },
    )
    check(response.status_code == 201, f"флорист отправляет обращение: {response.data[:120]}")
    item_id = response.get_json()['item']['id']

    stored = get_item(item_id)
    check('secret' not in (stored['client_context'] or ''),
          "неизвестные поля контекста в базу не попадают")
    check('cashshifts/close' in (stored['client_context'] or ''),
          "техконтекст с упавшим запросом сохранён")

    response = send(florist, 'коротко')
    check(response.status_code == 400, "слишком короткий текст отклонён")

    print("\n2. Защита от подделки запроса (CSRF)")
    response = florist.post('/api/feedback', json={'text': 'Достаточно длинный текст обращения'})
    check(response.status_code == 403, "POST без X-Requested-With отвергнут")

    print("\n3. Приватность")
    response = manager.get('/api/feedback/mine')
    check(response.get_json()['count'] == 0, "чужие обращения не видны в «Моих»")

    response = manager.get('/api/feedback')
    check(response.status_code == 403, "очередь разбора закрыта для не-владельца")

    response = manager.get('/api/feedback/similar',
                           query_string={'q': 'при закрытии смены сумма наличных сбрасывается',
                                         'module': 'cash_shifts'})
    check(response.get_json()['items'] == [],
          "неразобранное чужое обращение не показывается в подсказках")

    response = owner.get('/api/feedback')
    check(response.status_code == 200 and response.get_json()['count'] == 1,
          "владелец видит очередь")

    print("\n4. Дедуп через «у меня тоже»")
    # Владелец разобрал обращение — только теперь оно годится в подсказки
    response = owner.post(f'/api/feedback/{item_id}/status',
                          json={'status': 'accepted'}, headers=AJAX)
    check(response.status_code == 200, "владелец принял обращение")

    response = manager.get('/api/feedback/similar',
                           query_string={'q': 'сумма наличных сбрасывается при закрытии смены',
                                         'module': 'cash_shifts'})
    similar = response.get_json()['items']
    check(len(similar) == 1, f"похожее обращение найдено: {similar}")
    check('author_username' not in (similar[0] if similar else {}),
          "имя автора в подсказке не отдаётся")

    response = manager.post(f'/api/feedback/{item_id}/support', headers=AJAX)
    check(response.get_json()['added'] is True and response.get_json()['people_count'] == 2,
          "поддержка засчитана: людей стало двое")

    response = manager.post(f'/api/feedback/{item_id}/support', headers=AJAX)
    check(response.get_json()['added'] is False and response.get_json()['people_count'] == 2,
          "повторная поддержка вес не задваивает")

    response = florist.post(f'/api/feedback/{item_id}/support', headers=AJAX)
    check(response.get_json()['people_count'] == 2, "автор сам себя не поддерживает")

    print("\n5. Разбор владельцем")
    response = owner.post(f'/api/feedback/{item_id}/status',
                          json={'status': 'rejected'}, headers=AJAX)
    check(response.status_code == 400, "отклонение без текста для автора запрещено")

    response = owner.post(f'/api/feedback/{item_id}/to-task',
                          json={'title': 'Сумма наличных сбрасывается'}, headers=AJAX)
    check(response.status_code == 200, f"обращение уехало в бэклог: {response.data[:120]}")
    task_id = response.get_json()['task']['id']

    response = owner.get('/api/tasks')
    titles = [task['title'] for task in response.get_json()['tasks']]
    check('Сумма наличных сбрасывается' in titles, "задача появилась в разделе «Задачи»")

    response = owner.post(f'/api/feedback/{item_id}/to-task', json={}, headers=AJAX)
    check(response.status_code == 409, "повторная отправка в бэклог отклонена")

    response = florist.get('/api/feedback/mine')
    mine = response.get_json()['items'][0]
    check(mine['status'] == 'accepted' and mine['task_id'] == task_id,
          "автор видит, что обращение взято в работу")

    print("\n6. Объединение дублей")
    response = send(manager, 'Наличка в смене обнуляется после сохранения формы', type_='bug')
    dup_id = response.get_json()['item']['id']

    response = owner.post(f'/api/feedback/{dup_id}/merge',
                          json={'target_id': item_id}, headers=AJAX)
    check(response.status_code == 200, "дубль подшит к основному обращению")

    response = owner.get('/api/feedback')
    ids = [item['id'] for item in response.get_json()['items']]
    check(dup_id not in ids, "подшитый дубль ушёл из очереди")

    check(get_item(item_id)['people_count'] == 2,
          "автор дубля уже был сторонником — вес не задвоился")

    print("\n7. Экспорт промпта")
    response = owner.get(f'/api/feedback/{item_id}/export')
    markdown = response.get_json()['markdown']
    check('## Что мешает' in markdown and 'cashshifts/close' in markdown,
          "промпт содержит боль и технический контекст")
    check('Наличка в смене обнуляется' in markdown,
          "в промпт попала та же боль другими словами из дубля")

    print("\n8. Лимит частоты")
    hit_limit = False
    for index in range(RATE_LIMIT_PER_HOUR + 2):
        response = send(florist, f'Обращение номер {index} для проверки лимита частоты')
        if response.status_code == 429:
            hit_limit = True
            break
    check(hit_limit, f"лимит {RATE_LIMIT_PER_HOUR} обращений в час срабатывает")

    print("\n9. Счётчики для бейджей")
    response = owner.get('/api/feedback/counters')
    check(response.get_json()['new_for_owner'] > 0, "владелец видит число неразобранных")

    response = manager.get('/api/feedback/counters')
    check(response.get_json()['new_for_owner'] == 0,
          "сотруднику число чужих неразобранных не показывается")

    print("\n" + "=" * 60)
    if failures:
        print(f"ПРОВАЛЕНО проверок: {len(failures)}")
        for message in failures:
            print(f"  - {message}")
        return 1
    print("Все проверки пройдены")
    return 0


if __name__ == '__main__':
    sys.exit(main())
