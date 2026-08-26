"""
Сторож расписаний: тик планировщика отрабатывает ОДИН раз на всех воркеров.

Откуда взялось. На проде 2026-08-26 в логах:

    14:20:29 Курьеры: 2026-08-19—2026-08-25 → 442 заказов
    14:20:32 Курьеры: 2026-08-26—2026-08-26 → 51 заказов
    14:20:58 Курьеры: 2026-08-19—2026-08-25 → 442 заказов   <- то же самое
    14:21:01 Курьеры: 2026-08-26—2026-08-26 → 51 заказов    <- и это

Каждые полчаса вся работа делалась дважды. Лок этого не ловил: он отвечает на
вопрос «идёт ли прогон прямо сейчас» и освобождается сразу по завершении, а
планировщики двух воркеров gunicorn расходятся во времени на десятки секунд —
второй просыпался, видел лок свободным и честно повторял всё заново.

Проверяется у всех трёх модулей с расписанием: курьеры, МойСклад, Pyrus.

Запуск: python scripts/test_scheduler_claim.py
"""

import io
import os
import socket
import sys


if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')


class NoNetwork(socket.socket):
    def connect(self, *a, **k):
        raise OSError("сеть отключена намеренно")

    def connect_ex(self, *a, **k):
        raise OSError("сеть отключена намеренно")


socket.socket = NoNetwork

_HERE = os.path.dirname(__file__)
COURIERS_DB = os.path.join(_HERE, '_test_sched_couriers.db')
MOYSKLAD_DB = os.path.join(_HERE, '_test_sched_moysklad.db')
PYRUS_DB = os.path.join(_HERE, '_test_sched_pyrus.db')

for path in (COURIERS_DB, MOYSKLAD_DB, PYRUS_DB):
    for suffix in ('', '-wal', '-shm'):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)

os.environ['COURIERS_DB_PATH'] = COURIERS_DB
os.environ['MOYSKLAD_DB_PATH'] = MOYSKLAD_DB
os.environ['PYRUS_DB_PATH'] = PYRUS_DB

sys.path.insert(0, os.path.join(_HERE, '..', 'src'))

failures = []


def check(condition, message):
    print(('   OK   ' if condition else '   ПРОВАЛ ') + message)
    if not condition:
        failures.append(message)


def case(title, claim, acquire, release):
    """Один модуль: claim/acquire/release — уже привязанные к его хранилищу."""
    print(title)

    # Воркер A забирает тик
    check(claim('t_sync', 1800), "первый воркер занял тик")

    # Воркер B просыпается позже — работы для него нет
    check(not claim('t_sync', 1800),
          "второй воркер тик не получил (иначе вся работа шла бы дважды)")

    # И даже после того, как первый закончил и отпустил ЛОК, тик всё равно занят:
    # именно на этом ломалась старая схема.
    acquire('t_sync', 600)
    release('t_sync')
    check(not claim('t_sync', 1800),
          "тик занят и после освобождения лока — повтора не будет")

    # Истёкший талон снова свободен, иначе расписание встало бы навсегда.
    # Отдельный ключ: у 't_sync' талон занят до +1800 и истечь не успел.
    claim('t_expiry', 0)  # талон, истекающий сразу же
    check(claim('t_expiry', 1800), "по истечении интервала тик снова доступен")

    # Разные имена не мешают друг другу
    check(claim('t_other', 1800), "чужое расписание не заблокировано")
    print()


def main():
    print("=== Тик расписания отрабатывает один раз на всех воркеров ===\n")

    from couriers.storage import (
        init_couriers_tables, try_claim_scheduled_run,
        try_acquire_sync_lock, release_sync_lock,
    )
    init_couriers_tables()
    case("Курьеры", try_claim_scheduled_run, try_acquire_sync_lock, release_sync_lock)

    from moysklad.storage import get_storage as get_moysklad_storage
    ms = get_moysklad_storage(MOYSKLAD_DB)
    case("МойСклад", ms.try_claim_scheduled_run, ms.try_acquire_sync_lock,
         ms.release_sync_lock)

    from pyrus.storage import get_storage as get_pyrus_storage
    py = get_pyrus_storage(PYRUS_DB)
    case("Pyrus", py.try_claim_scheduled_run, py.try_acquire_sync_lock,
         py.release_sync_lock)

    if failures:
        print(f"=== ПРОВАЛОВ: {len(failures)} ===")
        for message in failures:
            print(f"  - {message}")
        return 1

    print("=== Расписания в порядке ===")
    return 0


if __name__ == '__main__':
    sys.exit(main())
