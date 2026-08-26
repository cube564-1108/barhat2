"""
Сторож горячего пути: сколько раз один HTTP-запрос открывает SQLite.

Зачем это отдельным прогоном. Постоянный диск Amvera (/data) сетевой: замер
прода 2026-08-26 показал 3 мс на отдачу статики и 90-700 мс на любой запрос,
коснувшийся базы, — при простаивающем Python. То есть цена запроса к сайту
определяется не кодом, а числом открытых соединений и числом fsync. Пока это
никто не мерит, оно тихо отрастает обратно: лишний get_db() в декораторе
незаметен в код-ревью и стоит десятки миллисекунд у каждого сотрудника.

Проверяется:
1. соединение настроено под сетевой диск (synchronous=NORMAL, busy_timeout);
2. journal_mode=WAL не переставляется на каждом соединении;
3. один запрос к защищённой ручке открывает базу считанное число раз;
4. аудит не пишется синхронно внутри запроса.

Запуск: python scripts/test_db_hot_path.py
"""

import io
import os
import socket
import sqlite3
import sys
import threading
from datetime import datetime

if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')


# Сеть отключаем ДО импорта приложения: локальный прогон читает боевой .env
# (load_dotenv при импорте) и иначе может уйти в боевые внешние API.
class NoNetwork(socket.socket):
    def connect(self, *a, **k):
        raise OSError("сеть отключена намеренно")

    def connect_ex(self, *a, **k):
        raise OSError("сеть отключена намеренно")


socket.socket = NoNetwork

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), '_test_hot_path.db')
for suffix in ('', '-wal', '-shm'):
    if os.path.exists(TEST_DB_PATH + suffix):
        os.remove(TEST_DB_PATH + suffix)
os.environ['BARHAT_DB_PATH'] = TEST_DB_PATH

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from flask import Flask  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

import sqlite_conn  # noqa: E402
from auth import (  # noqa: E402
    auth_bp, login_manager, init_auth_tables, get_db, flush_audit_log,
)
from cashshifts.storage import init_cashshifts_tables  # noqa: E402
from cashshifts.server import cashshifts_bp  # noqa: E402

failures = []


def check(condition, message):
    print(('   OK   ' if condition else '   ПРОВАЛ ') + message)
    if not condition:
        failures.append(message)


# ============================================================================
# Счётчик соединений
# ============================================================================

_real_connect = sqlite3.connect
_request_thread_id = threading.get_ident()
connect_calls = []
journal_pragmas = []


class CountingConnection(sqlite3.Connection):
    """Соединение, которое запоминает, кто трогал journal_mode."""

    def execute(self, sql, *args, **kwargs):
        if 'journal_mode' in str(sql).lower():
            journal_pragmas.append(sql)
        return super().execute(sql, *args, **kwargs)


def counting_connect(*args, **kwargs):
    # Считаем только соединения из потока, который обрабатывает запрос:
    # писатель аудита живёт в своём потоке и открывает базу тогда, когда
    # ему удобно — он как раз и вынесен из горячего пути, штрафовать за
    # него смысла нет.
    if threading.get_ident() == _request_thread_id:
        connect_calls.append(args[0] if args else kwargs.get('database'))
    kwargs.setdefault('factory', CountingConnection)
    return _real_connect(*args, **kwargs)


sqlite3.connect = counting_connect
sqlite_conn.sqlite3.connect = counting_connect


def make_app():
    app = Flask(__name__)
    app.secret_key = 'test-only'
    app.config['TESTING'] = True
    login_manager.init_app(app)
    app.register_blueprint(auth_bp)
    app.register_blueprint(cashshifts_bp)
    return app


def make_user(username, role, sections):
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
    conn.commit()
    conn.close()


def main():
    print("=== Горячий путь: обращения к SQLite на один запрос ===\n")

    init_cashshifts_tables()
    init_invoices_tables_if_present()
    init_auth_tables()
    make_user('hot_admin', 'admin', ['cash_shifts', 'dashboard'])

    print("1. Настройки соединения")
    conn = get_db()
    synchronous = conn.execute('PRAGMA synchronous').fetchone()[0]
    busy_timeout = conn.execute('PRAGMA busy_timeout').fetchone()[0]
    journal_mode = conn.execute('PRAGMA journal_mode').fetchone()[0]
    conn.close()
    check(synchronous == 1, f"synchronous = {synchronous} (ждём 1 = NORMAL, fsync только на чекпойнте)")
    check(busy_timeout == 20000, f"busy_timeout = {busy_timeout}")
    check(journal_mode == 'wal', f"journal_mode = {journal_mode}")
    print()

    print("2. journal_mode не переставляется на каждом соединении")
    journal_pragmas.clear()
    for _ in range(5):
        get_db().close()
    check(not journal_pragmas,
          f"пять новых соединений — {len(journal_pragmas)} PRAGMA journal_mode (ждём 0)")
    print()

    app = make_app()

    print("3. Сколько раз открывается база за один запрос")
    with app.test_client() as client:
        response = client.post('/api/auth/login',
                               json={'username': 'hot_admin', 'password': 'pass'})
        assert response.status_code == 200, response.data

        # Чтение защищённой ручки: тут работает и load_user, и проверка секции
        connect_calls.clear()
        status = client.get('/api/cash-shifts/categories').status_code
        read_connects = len(connect_calls)
        check(status == 200, f"GET /api/cash-shifts/categories -> {status}")
        # 2 = учётка с правами (одно соединение) + сам запрос данных.
        # Было 3: права догружались отдельным соединением.
        check(read_connects <= 2,
              f"чтение открыло базу {read_connects} раз(а) (потолок 2)")

        # Запись: раньше сюда добавлялся ещё и синхронный INSERT в audit_log
        connect_calls.clear()
        response = client.post('/api/cash-shifts/categories', json={'name': 'Тест горячего пути'})
        write_connects = len(connect_calls)
        check(response.status_code == 201, f"POST категории -> {response.status_code}")
        check(write_connects <= 3,
              f"запись открыла базу {write_connects} раз(а) (потолок 3, аудит уходит в очередь)")
    print()

    print("4. Аудит пишется вне запроса, но не теряется")
    check(flush_audit_log(timeout=5), "очередь аудита разошлась за 5 секунд")
    conn = get_db()
    actions = [r[0] for r in conn.execute(
        "SELECT action FROM audit_log WHERE username = 'hot_admin'").fetchall()]
    conn.close()
    check('create_collection_category' in actions,
          f"действие доехало в audit_log ({actions})")
    print()

    if failures:
        print(f"=== ПРОВАЛОВ: {len(failures)} ===")
        for message in failures:
            print(f"  - {message}")
        return 1

    print("=== Горячий путь в порядке ===")
    return 0


def init_invoices_tables_if_present():
    """Счета живут в той же базе и добавляют свои таблицы — если модуль есть."""
    try:
        from invoices.storage import init_invoices_tables
    except Exception:
        return
    init_invoices_tables()


if __name__ == '__main__':
    sys.exit(main())
