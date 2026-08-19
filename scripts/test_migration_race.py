"""
Регрессия на аварию 2026-08-19: вход в дашборд висел после деплоя.

Причина: два воркера gunicorn стартуют одновременно и выполняют один и тот же
ALTER TABLE. Проигравший гонку получал "database is locked", исключение летело
наружу из init_cashshifts_tables ДО conn.close(), соединение утекало открытым
на всю жизнь воркера и мешало записи в общую БД. Логин при этом доходил до
INSERT в audit_log и висел, а чтение работало — поэтому снаружи сервер
выглядел полностью здоровым.

Проверяет:
  1. Миграция под заблокированной БД не выбрасывает исключение
  2. init_cashshifts_tables закрывает соединение даже при ошибке внутри
  3. Повторный запуск идемпотентен
  4. Запись в БД остаётся возможной после сорванной миграции

Запуск: python scripts/test_migration_race.py
"""

import os
import sys
import io
import sqlite3
import tempfile

if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

_tmp_db = os.path.join(tempfile.mkdtemp(), "test_migration_race.db")
os.environ["BARHAT_DB_PATH"] = _tmp_db

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cashshifts import storage
from cashshifts.storage import init_cashshifts_tables, _add_column_if_missing

failures = []


def check(condition, message):
    if condition:
        print(f"   [ok] {message}")
    else:
        print(f"   [FAIL] {message}")
        failures.append(message)


def main():
    print("=== Регрессия: гонка миграций между воркерами ===\n")

    print("1. Обычная инициализация")
    init_cashshifts_tables()
    conn = storage.get_db()
    columns = [r[1] for r in conn.execute("PRAGMA table_info(cash_shifts)").fetchall()]
    conn.close()
    check("cash_orders_synced_at" in columns, "колонка создана")

    print("\n2. Миграция новой колонки под ЭКСКЛЮЗИВНОЙ блокировкой БД")
    # Имитируем соседний воркер, который прямо сейчас держит write-лок
    locker = sqlite3.connect(_tmp_db, isolation_level=None)
    locker.execute("PRAGMA busy_timeout=100")
    locker.execute("BEGIN EXCLUSIVE")

    victim = sqlite3.connect(_tmp_db, timeout=0.3)
    victim.execute("PRAGMA busy_timeout=300")
    try:
        _add_column_if_missing(victim, "cash_shifts", "race_probe_column", "TEXT")
        check(True, "заблокированный ALTER не выбросил исключение (раньше — падал)")
    except Exception as e:
        check(False, f"миграция упала с {type(e).__name__}: {e}")
    finally:
        victim.close()

    locker.rollback()
    locker.close()

    print("\n3. Соединение закрывается даже при ошибке внутри инициализации")

    class TrackingConnection:
        """Соединение, которое считает close() и умеет падать на нужном запросе."""

        def __init__(self, real):
            self._real = real
            self.closed = False

        def execute(self, sql, *a, **kw):
            if "CREATE TABLE IF NOT EXISTS cash_collections" in sql:
                raise sqlite3.OperationalError("искусственный сбой схемы")
            return self._real.execute(sql, *a, **kw)

        def commit(self):
            return self._real.commit()

        def close(self):
            self.closed = True
            return self._real.close()

    tracked = TrackingConnection(sqlite3.connect(_tmp_db))
    original_get_db = storage.get_db
    storage.get_db = lambda: tracked
    try:
        init_cashshifts_tables()
        check(False, "ожидалось, что искусственный сбой прокинется наружу")
    except sqlite3.OperationalError:
        check(True, "ошибка схемы прокидывается наружу (её глушить нельзя)")
    finally:
        storage.get_db = original_get_db

    check(tracked.closed, "соединение ЗАКРЫТО несмотря на исключение (суть регрессии)")

    print("\n4. База осталась пригодной для записи после сорванной миграции")
    conn = storage.get_db()
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS _probe (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO _probe (id) VALUES (1)")
        conn.commit()
        conn.execute("DROP TABLE _probe")
        conn.commit()
        check(True, "запись в БД работает — вход в дашборд не встанет")
    except Exception as e:
        check(False, f"запись в БД сломана: {e}")
    finally:
        conn.close()

    print("\n5. Повторная инициализация идемпотентна")
    init_cashshifts_tables()
    init_cashshifts_tables()
    check(True, "два повторных запуска подряд не падают")

    print("\n" + "=" * 50)
    if failures:
        print(f"ПРОВАЛЕНО ПРОВЕРОК: {len(failures)}")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("Все проверки пройдены")
    return 0


if __name__ == "__main__":
    sys.exit(main())
