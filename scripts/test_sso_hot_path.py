"""
Вход из Пульса: повторный заход не должен ничего писать в базу.

Откуда взялось. Пульс выписывает свежий токен на КАЖДЫЙ переход по меню —
в логах прода 2026-08-26 один человек зашёл шесть раз за минуту сорок:

    14:51:17 sub=92 iat=1787755877
    14:51:28 sub=92 iat=1787755888
    14:51:51 sub=92 iat=1787755910
    ...

При таком потоке любая безусловная запись в горячем пути входа превращается в
постоянную нагрузку на общий диск /data. А там было две: UPDATE users на
каждом входе (даже когда имя не менялось) и синхронная диагностика
_log_sso_attempt("OK") со своим соединением и commit.

Проверяется:
1. первый вход заводит учётку и связывает её по email;
2. повторный вход тем же токеном НЕ пишет в базу вообще;
3. изменившееся имя всё-таки записывается — экономия не должна ломать данные;
4. диагностика sso_attempt на успешных входах не пишется, а на отказе пишется.

Запуск: python scripts/test_sso_hot_path.py
"""

import io
import os
import socket
import sqlite3
import sys
from datetime import datetime, timedelta

if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')


class NoNetwork(socket.socket):
    def connect(self, *a, **k):
        raise OSError("сеть отключена намеренно")

    def connect_ex(self, *a, **k):
        raise OSError("сеть отключена намеренно")


socket.socket = NoNetwork

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), '_test_sso_hot.db')
for suffix in ('', '-wal', '-shm'):
    if os.path.exists(TEST_DB_PATH + suffix):
        os.remove(TEST_DB_PATH + suffix)

os.environ['BARHAT_DB_PATH'] = TEST_DB_PATH
os.environ['BARKHAT_SSO_SECRET'] = 'test-secret-for-sso-hot-path'

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import jwt  # noqa: E402
from flask import Flask  # noqa: E402

from auth import (  # noqa: E402
    auth_bp, login_manager, init_auth_tables, get_db, flush_audit_log,
)
import sso as sso_module  # noqa: E402
from sso import sso_bp, SSO_ISSUER, SSO_AUDIENCE  # noqa: E402

failures = []


def check(condition, message):
    print(('   OK   ' if condition else '   ПРОВАЛ ') + message)
    if not condition:
        failures.append(message)


# ============================================================================
# Перехват записей в базу
# ============================================================================

writes = []
_real_connect = sqlite3.connect


class WatchingConnection(sqlite3.Connection):
    def execute(self, sql, *args, **kwargs):
        head = str(sql).strip().split()[0].upper() if str(sql).strip() else ''
        if head in ('INSERT', 'UPDATE', 'DELETE'):
            writes.append(' '.join(str(sql).split())[:80])
        return super().execute(sql, *args, **kwargs)


def watching_connect(*args, **kwargs):
    kwargs.setdefault('factory', WatchingConnection)
    return _real_connect(*args, **kwargs)


def make_token(email, name, ttl_seconds=300):
    now = datetime.utcnow()
    return jwt.encode(
        {
            'iss': SSO_ISSUER,
            'aud': SSO_AUDIENCE,
            'sub': '92',
            'email': email,
            'name': name,
            'iat': now,
            'exp': now + timedelta(seconds=ttl_seconds),
        },
        os.environ['BARKHAT_SSO_SECRET'],
        algorithm='HS256',
    )


def make_app():
    app = Flask(__name__)
    app.secret_key = 'test-only'
    app.config['TESTING'] = True
    login_manager.init_app(app)
    app.register_blueprint(auth_bp)
    app.register_blueprint(sso_bp)
    return app


def sso_writes(client, token):
    """Сколько записей в базу сделал один вход через Пульс."""
    flush_audit_log()
    writes.clear()
    response = client.get(f'/sso?token={token}', follow_redirects=False)
    flush_audit_log()
    # Аудит пишется фоновым потоком пачками — он вынесен из запроса намеренно
    # и к стоимости самого входа не относится.
    return response, [w for w in writes if 'audit_log' not in w]


def count_sso_attempts():
    conn = get_db()
    n = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action = 'sso_attempt'").fetchone()[0]
    conn.close()
    return n


def main():
    print("=== Вход из Пульса: стоимость повторного захода ===\n")

    init_auth_tables()
    sqlite3.connect = watching_connect
    sso_module.sqlite3 = sqlite3

    app = make_app()
    email = 'florist2_brn@barhatflowers.ru'

    print("1. Первый вход — учётку заводим")
    with app.test_client() as client:
        response, w = sso_writes(client, make_token(email, 'Флорист Второй'))
        check(response.status_code == 302, f"вход принят, редирект ({response.status_code})")
        check(len(w) > 0, f"первый вход пишет в базу ({len(w)} операций) — учётка создаётся")
    print()

    print("2. Повторный вход — писать нечего")
    with app.test_client() as client:
        response, w = sso_writes(client, make_token(email, 'Флорист Второй'))
        check(response.status_code == 302, f"вход принят ({response.status_code})")
        check(not w, f"повторный вход не пишет в базу (операций: {len(w)}; {w})")
    print()

    print("3. Имя изменилось — записываем")
    with app.test_client() as client:
        response, w = sso_writes(client, make_token(email, 'Флорист Второй Новый'))
        check(any('users' in item for item in w),
              f"смена имени доезжает до базы ({w})")
        conn = get_db()
        stored = conn.execute(
            "SELECT full_name FROM users WHERE email = ?", (email,)).fetchone()['full_name']
        conn.close()
        check(stored == 'Флорист Второй Новый', f"в базе имя «{stored}»")
    print()

    print("4. Диагностика sso_attempt — только на отказах")
    before = count_sso_attempts()
    with app.test_client() as client:
        client.get(f'/sso?token={make_token(email, "Флорист Второй Новый")}')
    flush_audit_log()
    check(count_sso_attempts() == before,
          "успешный вход диагностику не пишет")

    with app.test_client() as client:
        expired = make_token(email, 'Флорист Второй Новый', ttl_seconds=-60)
        response = client.get(f'/sso?token={expired}')
        check(response.status_code == 403, f"протухший токен отклонён ({response.status_code})")
    flush_audit_log()
    check(count_sso_attempts() > before,
          "отказ по-прежнему пишется — на нём диагностика и нужна")
    print()

    if failures:
        print(f"=== ПРОВАЛОВ: {len(failures)} ===")
        for message in failures:
            print(f"  - {message}")
        return 1

    print("=== Вход из Пульса больше не пишет впустую ===")
    return 0


if __name__ == '__main__':
    sys.exit(main())
