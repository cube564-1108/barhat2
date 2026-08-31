"""
Офлайн-тесты защиты от двух открытых смен на одной точке — без сети.

29.08.26 на точке «Челябинск пр-кт Свердловский, д 23» открылись ТРИ дневные
смены в одну минуту: проверка «нет ли открытой смены» и вставка шли отдельными
соединениями, а на проде 2 воркера gunicorn по 8 потоков. Все три запроса
прочитали «открытых смен нет» раньше, чем первый успел записаться.

Проверяет:
  1. Частичный уникальный индекс idx_shifts_one_open_per_store создаётся
  2. Индекс отбивает вставку второй открытой смены в обход бизнес-логики
  3. open_cash_shift() под нагрузкой: 20 потоков — ровно одна смена
  4. Начальный остаток берётся из последней закрытой смены (и NULL → 0)
  5. find_duplicate_open_shifts / resolve_duplicate_open_shifts на старой базе,
     где дубли уже лежат, а индекса ещё нет
  6. HTTP: POST /open отдаёт 409 вместо второй смены, админские ручки закрыты
     от остальных ролей

Запуск: python scripts/test_shift_open_race.py
"""

import os
import sys
import io
import sqlite3
import tempfile
import threading
from datetime import datetime, timedelta, timezone

if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# Временная БД — до импорта storage, путь читается на уровне модуля
_tmp_db = os.path.join(tempfile.mkdtemp(), "test_shift_open_race.db")
os.environ["BARHAT_DB_PATH"] = _tmp_db

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from werkzeug.security import generate_password_hash

from cashshifts import storage
from cashshifts.storage import (
    ONE_OPEN_SHIFT_INDEX,
    OpenShiftExistsError,
    create_cash_shift,
    create_collection,
    delete_cash_shift,
    ensure_one_open_shift_index,
    find_duplicate_open_shifts,
    get_all_categories,
    get_all_stores,
    get_db,
    init_cashshifts_tables,
    open_cash_shift,
    resolve_duplicate_open_shifts,
    set_user_stores,
    update_cash_shift,
)

failures = []


def make_app():
    """Минимальное приложение: авторизация + blueprint кассовых смен."""
    from flask import Flask
    from auth import auth_bp, login_manager, init_auth_tables
    from cashshifts.server import cashshifts_bp

    app = Flask(__name__)
    app.secret_key = "test-secret"
    login_manager.init_app(app)
    login_manager.login_view = None
    app.register_blueprint(auth_bp)
    app.register_blueprint(cashshifts_bp)

    with app.app_context():
        init_auth_tables()

    return app


def add_user(username, role):
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO users (username, full_name, password_hash, role, is_active, created_at)
            VALUES (?, ?, ?, ?, 1, ?)
            """,
            (username, username, generate_password_hash("secret"), role,
             datetime.now(timezone.utc).isoformat())
        )
        conn.commit()
    finally:
        conn.close()


def login(client, username):
    return client.post("/api/auth/login",
                       json={"username": username, "password": "secret"})


def check(condition, message):
    if condition:
        print(f"   [ok] {message}")
    else:
        print(f"   [FAIL] {message}")
        failures.append(message)


def index_exists() -> bool:
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name = ?",
            (ONE_OPEN_SHIFT_INDEX,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def drop_index():
    """Вернуть базу в состояние «до фикса», чтобы можно было насадить дубли."""
    conn = get_db()
    try:
        conn.execute(f"DROP INDEX IF EXISTS {ONE_OPEN_SHIFT_INDEX}")
        conn.commit()
    finally:
        conn.close()


def count_open(store_id: int) -> int:
    conn = get_db()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM cash_shifts WHERE store_id = ? AND status = 'open'",
            (store_id,)
        ).fetchone()[0]
    finally:
        conn.close()


def wipe_shifts():
    conn = get_db()
    try:
        conn.execute("DELETE FROM cash_collections")
        conn.execute("DELETE FROM cash_orders_cache")
        conn.execute("DELETE FROM cash_shifts")
        conn.commit()
    finally:
        conn.close()


def ts(minutes_ago: int = 0) -> str:
    return (datetime(2026, 8, 29, 10, 52) - timedelta(minutes=minutes_ago)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def main():
    print("=== Офлайн-тесты гонки при открытии смены ===\n")

    print("1. Схема и индекс")
    init_cashshifts_tables()
    check(index_exists(), f"индекс {ONE_OPEN_SHIFT_INDEX} создан при инициализации")

    # Повторный вызов не должен падать (2 воркера gunicorn стартуют параллельно)
    init_cashshifts_tables()
    check(index_exists(), "повторная инициализация не ломает индекс")

    stores = get_all_stores()
    check(len(stores) >= 3, f"seed-точек достаточно для теста ({len(stores)})")
    store_a, store_b, store_c = stores[0], stores[1], stores[2]

    print("\n2. Индекс отбивает вставку в обход бизнес-логики")
    wipe_shifts()
    create_cash_shift(store_id=store_a["id"], shift_type="day",
                      datetime_start=ts(), opening_balance=14209.0,
                      florist_username="florist_a")
    raised = False
    try:
        create_cash_shift(store_id=store_a["id"], shift_type="day",
                          datetime_start=ts(), opening_balance=14209.0,
                          florist_username="florist_a")
    except sqlite3.IntegrityError:
        raised = True
    check(raised, "вторая открытая смена на точке отбита на уровне БД")
    check(count_open(store_a["id"]) == 1, "в базе осталась одна открытая смена")

    # Смена на соседней точке индексу не мешает — он частичный, по store_id
    create_cash_shift(store_id=store_b["id"], shift_type="night",
                      datetime_start=ts(), opening_balance=0.0,
                      florist_username="florist_b")
    check(count_open(store_b["id"]) == 1, "открытая смена на другой точке разрешена")

    print("\n3. open_cash_shift(): 20 потоков на одну точку")
    wipe_shifts()
    results = []
    conflicts = []
    errors = []
    results_lock = threading.Lock()
    start = threading.Barrier(20)

    def worker():
        start.wait()  # все стартуют одновременно — иначе гонки не будет
        try:
            shift_id, balance = open_cash_shift(
                store_id=store_c["id"], shift_type="day",
                datetime_start=ts(), florist_username="florist_c"
            )
            with results_lock:
                results.append(shift_id)
        except OpenShiftExistsError:
            with results_lock:
                conflicts.append(1)
        except Exception as e:  # noqa: BLE001 — важен сам факт неожиданной ошибки
            with results_lock:
                errors.append(repr(e))

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    check(not errors, f"неожиданных ошибок нет (получено: {errors[:3]})")
    check(len(results) == 1, f"смену открыл ровно один поток (получено {len(results)})")
    check(len(conflicts) == 19, f"остальные 19 получили отказ (получено {len(conflicts)})")
    check(count_open(store_c["id"]) == 1,
          f"в базе одна открытая смена (получено {count_open(store_c['id'])})")

    print("\n4. Начальный остаток при открытии")
    wipe_shifts()
    closed = create_cash_shift(store_id=store_a["id"], shift_type="day",
                               datetime_start=ts(60), opening_balance=0.0)
    update_cash_shift(shift_id=closed, status="closed", actual_balance=14209.0,
                      datetime_end=ts(30))
    shift_id, balance = open_cash_shift(store_id=store_a["id"], shift_type="day",
                                        datetime_start=ts())
    check(balance == 14209.0, f"остаток взят из последней закрытой смены ({balance})")

    # Смену закрыли, не пересчитав кассу: actual_balance = NULL. Раньше это
    # значение уходило прямо в INSERT и падало на NOT NULL
    wipe_shifts()
    closed = create_cash_shift(store_id=store_b["id"], shift_type="day",
                               datetime_start=ts(60), opening_balance=0.0)
    update_cash_shift(shift_id=closed, status="closed", datetime_end=ts(30))
    shift_id, balance = open_cash_shift(store_id=store_b["id"], shift_type="day",
                                        datetime_start=ts())
    check(balance == 0.0, f"пустой остаток закрытой смены превращается в 0 ({balance})")

    print("\n5. Разбор дублей на базе, где они уже есть")
    wipe_shifts()
    drop_index()  # состояние прода до фикса

    # store_a: три пустые смены-близнеца, как на Свердловском, 23
    twin_first = create_cash_shift(store_id=store_a["id"], shift_type="day",
                                   datetime_start=ts(2), opening_balance=14209.0,
                                   florist_username="florist_a")
    twin_second = create_cash_shift(store_id=store_a["id"], shift_type="day",
                                    datetime_start=ts(1), opening_balance=14209.0,
                                    florist_username="florist_a")
    twin_third = create_cash_shift(store_id=store_a["id"], shift_type="day",
                                   datetime_start=ts(0), opening_balance=14209.0,
                                   florist_username="florist_a")

    # store_b: дубль, но инкассация внесена во ВТОРУЮ смену — её и оставляем
    b_empty = create_cash_shift(store_id=store_b["id"], shift_type="day",
                                datetime_start=ts(2), opening_balance=500.0)
    b_with_data = create_cash_shift(store_id=store_b["id"], shift_type="day",
                                    datetime_start=ts(1), opening_balance=500.0)
    category = get_all_categories()[0]
    create_collection(shift_id=b_with_data, amount=300.0,
                      expense_category_id=category["id"], date=ts(),
                      created_by="florist_b")

    # store_c: данные в обеих — автоматически такое сливать нельзя
    c_one = create_cash_shift(store_id=store_c["id"], shift_type="day",
                              datetime_start=ts(2), opening_balance=100.0)
    c_two = create_cash_shift(store_id=store_c["id"], shift_type="day",
                              datetime_start=ts(1), opening_balance=100.0)
    create_collection(shift_id=c_one, amount=50.0,
                      expense_category_id=category["id"], date=ts())
    create_collection(shift_id=c_two, amount=70.0,
                      expense_category_id=category["id"], date=ts())

    check(not index_exists(), "индекс поверх дублей не построен")
    conn = get_db()
    try:
        rebuilt = ensure_one_open_shift_index(conn)
    finally:
        conn.close()
    check(not rebuilt, "попытка создать индекс поверх дублей не роняет процесс")

    groups = find_duplicate_open_shifts()
    check(len(groups) == 3, f"найдены все три проблемные точки (получено {len(groups)})")
    by_store = {g["store_id"]: g for g in groups}
    check(len(by_store[store_a["id"]]["shifts"]) == 3, "на store_a видно три смены")
    check(all(s["store_name"] for s in groups[0]["shifts"]),
          "название точки подтягивается из stores")
    check(by_store[store_b["id"]]["shifts"][1]["has_data"] is True,
          "смена с инкассацией помечена как непустая")

    result = resolve_duplicate_open_shifts()

    kept_ids = {k["shift_id"] for k in result["kept"]}
    deleted_ids = {d["shift_id"] for d in result["deleted"]}

    check(twin_first in kept_ids,
          "из пустых близнецов оставлена самая ранняя смена")
    check(deleted_ids >= {twin_second, twin_third},
          "лишние пустые близнецы удалены")
    check(b_with_data in kept_ids,
          "на store_b оставлена смена с инкассацией, а не самая ранняя")
    check(b_empty in deleted_ids, "пустой дубль на store_b удалён")
    check(storage.get_collections_total(b_with_data) == 300.0,
          "инкассация уцелела")

    skipped_stores = {s["store_id"] for s in result["skipped"]}
    check(store_c["id"] in skipped_stores,
          "точка с данными в обеих сменах отправлена на ручной разбор")
    check(storage.get_cash_shift_by_id(c_one) is not None
          and storage.get_cash_shift_by_id(c_two) is not None,
          "ни одна смена спорной точки не удалена")

    check(result["index_created"] is False,
          "пока спорная точка не разобрана, индекс не строится")
    check(not index_exists(), "индекса действительно нет")

    # Разводим спорную точку руками и повторяем — защита включается без деплоя
    delete_cash_shift(c_two)
    result2 = resolve_duplicate_open_shifts()
    check(result2["index_created"] is True,
          "после ручного разбора индекс создаётся тем же эндпоинтом")
    check(index_exists(), f"индекс {ONE_OPEN_SHIFT_INDEX} на месте")
    check(not find_duplicate_open_shifts(), "дублей не осталось")

    print("\n6. HTTP: открытие смены и админские ручки")
    wipe_shifts()
    app = make_app()
    add_user("admin_race", "admin")
    add_user("florist_race", "florist")
    set_user_stores("florist_race", [store_a["id"]])

    client = app.test_client()
    check(login(client, "florist_race").status_code == 200, "флорист логинится")

    first = client.post("/api/cash-shifts/open",
                        json={"store_id": store_a["id"], "shift_type": "day"})
    check(first.status_code == 201, f"первая смена открыта (код {first.status_code})")

    second = client.post("/api/cash-shifts/open",
                         json={"store_id": store_a["id"], "shift_type": "day"})
    check(second.status_code == 409,
          f"вторая смена отбита с 409, а не создана (код {second.status_code})")
    check("уже есть открытая смена" in second.get_json().get("error", ""),
          "текст отказа объясняет причину")
    check(count_open(store_a["id"]) == 1, "в базе по-прежнему одна смена")

    denied = client.get("/api/cash-shifts/admin/duplicate-open-shifts")
    check(denied.status_code == 403,
          f"флористу разбор дублей запрещён (код {denied.status_code})")
    denied_post = client.post("/api/cash-shifts/admin/duplicate-open-shifts/resolve")
    check(denied_post.status_code == 403,
          f"и удаление тоже (код {denied_post.status_code})")

    admin_client = app.test_client()
    check(login(admin_client, "admin_race").status_code == 200, "админ логинится")
    listing = admin_client.get("/api/cash-shifts/admin/duplicate-open-shifts")
    check(listing.status_code == 200, f"админ читает список дублей (код {listing.status_code})")
    check(listing.get_json()["count"] == 0, "дублей нет — список пуст")

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
