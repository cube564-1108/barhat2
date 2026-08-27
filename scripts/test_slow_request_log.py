"""
Медленные запросы должны попадать в лог — иначе «сайт виснет» не разобрать.

Снаружи видно только общее время ответа, а какая именно ручка встала — нет.
Замеры прода 2026-08-26 дали верный диагноз про медленный диск, но виновника
пиков в 15-33 секунды не показали: мерили /health, а не то, что нажимает
человек. Хук в pyrus/server.py пишет в лог всё, что шло дольше порога.

Проверяется:
1. превышение порога попадает в лог — с методом, путём и статусом;
2. хук не ломает ответ;
3. соседний after_request (frame-ancestors) продолжает работать — хуки
   выполняются цепочкой, и новый не должен вытеснить старый. На затирании
   заголовка соседним хуком мы уже обжигались: CSP от вьюхи не доезжал до
   браузера, и вложения-PDF перестали открываться.

Запуск: python scripts/test_slow_request_log.py
"""

import io
import logging
import os
import socket
import sys
import tempfile

if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')


class NoNetwork(socket.socket):
    def connect(self, *a, **k):
        raise OSError("сеть отключена намеренно")

    def connect_ex(self, *a, **k):
        raise OSError("сеть отключена намеренно")


socket.socket = NoNetwork

# Всё на временные пути: прогон не должен трогать ни боевые, ни рабочие базы.
_TMP = tempfile.mkdtemp(prefix='barhat_slowlog_')
os.environ['BARHAT_DB_PATH'] = os.path.join(_TMP, 'barhat.db')
os.environ['PYRUS_DB_PATH'] = os.path.join(_TMP, 'pyrus.db')
os.environ['MOYSKLAD_DB_PATH'] = os.path.join(_TMP, 'moysklad.db')
os.environ['COURIERS_DB_PATH'] = os.path.join(_TMP, 'couriers.db')
os.environ['INVOICE_ATTACHMENTS_DIR'] = os.path.join(_TMP, 'invoice_attachments')
os.environ['WRITEOFF_ATTACHMENTS_DIR'] = os.path.join(_TMP, 'writeoff_attachments')

# Планировщики выключаем штатными флагами — иначе прогон полезет в боевые
# RetailCRM, МойСклад и Pyrus.
os.environ['PYRUS_SYNC_SCHEDULER'] = '0'
os.environ['MOYSKLAD_SYNC_SCHEDULER'] = '0'
os.environ['COURIERS_SYNC_SCHEDULER'] = '0'

# Порог читается при импорте модуля, поэтому ставим до него. 0 — логируем всё.
os.environ['SLOW_REQUEST_SECONDS'] = '0'

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from pyrus.server import app, SLOW_REQUEST_SECONDS  # noqa: E402

failures = []


def check(condition, message):
    print(('   OK   ' if condition else '   ПРОВАЛ ') + message)
    if not condition:
        failures.append(message)


class CapturingHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record.getMessage())


def main():
    print("=== Медленные запросы пишутся в лог ===\n")

    check(SLOW_REQUEST_SECONDS == 0.0,
          f"порог берётся из окружения (SLOW_REQUEST_SECONDS={SLOW_REQUEST_SECONDS})")

    handler = CapturingHandler()
    handler.setLevel(logging.WARNING)
    server_logger = logging.getLogger('pyrus.server')
    server_logger.addHandler(handler)
    server_logger.setLevel(logging.WARNING)

    print()
    print("1. Запрос дольше порога попадает в лог")
    with app.test_client() as client:
        response = client.get('/health')

    check(response.status_code == 200, f"/health отвечает ({response.status_code})")

    slow = [m for m in handler.records if m.startswith('Медленный запрос:')]
    check(len(slow) == 1, f"запись о медленном запросе одна ({len(slow)}): {slow}")
    if slow:
        check('GET' in slow[0] and '/health' in slow[0],
              f"в записи есть метод и путь: {slow[0]}")
        check('статус 200' in slow[0], f"в записи есть статус: {slow[0]}")
    print()

    print("2. Секреты из query-строки в лог не утекают")
    handler.records.clear()
    with app.test_client() as client:
        # Ровно тот случай: JWT-пропуск Пульса приезжает в query-строке, и
        # логировать его нельзя — это учётные данные.
        client.get('/sso?token=secret-jwt-value-must-not-be-logged&next=/')
    logged = ' | '.join(handler.records)
    check('secret-jwt-value-must-not-be-logged' not in logged,
          "токен из ?token= в лог не попал")
    check('/sso' in logged, f"путь при этом записан: {logged}")
    print()

    print("3. Соседний after_request не вытеснен")
    csp = response.headers.get('Content-Security-Policy') or ''
    check('frame-ancestors' in csp,
          f"frame-ancestors на месте: {csp!r}")
    print()

    print("4. Быстрый запрос при боевом пороге лог не засоряет")
    handler.records.clear()
    import pyrus.server as server_module
    original = server_module.SLOW_REQUEST_SECONDS
    server_module.SLOW_REQUEST_SECONDS = 60.0
    try:
        with app.test_client() as client:
            client.get('/health')
    finally:
        server_module.SLOW_REQUEST_SECONDS = original
    check(not [m for m in handler.records if m.startswith('Медленный запрос:')],
          "быстрый запрос в лог не попал")
    print()

    if failures:
        print(f"=== ПРОВАЛОВ: {len(failures)} ===")
        for message in failures:
            print(f"  - {message}")
        return 1

    print("=== Лог медленных запросов работает ===")
    return 0


if __name__ == '__main__':
    sys.exit(main())
