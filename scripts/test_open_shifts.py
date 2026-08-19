"""
Офлайн-тесты таблицы «Открытые смены» — без единого запроса к RetailCRM.

Проверяет:
  1. Миграцию колонки cash_orders_synced_at
  2. Фильтрацию get_open_shifts по точкам (менеджер видит только свои)
  3. TTL-логику _crm_data_is_stale
  4. _sync_open_shifts_from_crm на фейковом клиенте: кэширование, бюджет времени
  5. _edit_open_shift: права ролей, правку начального остатка и инкассаций

Запуск: python scripts/test_open_shifts.py
"""

import os
import sys
import io
import time
import sqlite3
import tempfile
from datetime import datetime, timedelta

if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# Временная БД — до импорта storage, путь читается на уровне модуля
_tmp_db = os.path.join(tempfile.mkdtemp(), "test_open_shifts.db")
os.environ["BARHAT_DB_PATH"] = _tmp_db

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from flask import Flask

from cashshifts import storage
from cashshifts.storage import (
    init_cashshifts_tables,
    get_db,
    create_cash_shift,
    create_collection,
    get_cash_shift_by_id,
    get_open_shifts,
    get_all_stores,
    get_all_categories,
    set_user_stores,
)
from cashshifts import server
from cashshifts.server import (
    _crm_data_is_stale,
    _sync_open_shifts_from_crm,
    _edit_open_shift,
)

app = Flask(__name__)

failures = []


def check(condition, message):
    if condition:
        print(f"   [ok] {message}")
    else:
        print(f"   [FAIL] {message}")
        failures.append(message)


class FakeCRMClient:
    """Клиент-заглушка: отдаёт заранее заданные платежи, сеть не трогает."""

    def __init__(self, payments_by_store=None, delay=0.0, fail_stores=()):
        self.payments_by_store = payments_by_store or {}
        self.delay = delay
        self.fail_stores = set(fail_stores)
        self.calls = []

    def get_cash_orders(self, store_code=None, datetime_start=None,
                        datetime_end=None, limit=100, deadline=None):
        self.calls.append(store_code)
        if self.delay:
            time.sleep(self.delay)
        if store_code in self.fail_stores:
            raise RuntimeError("CRM недоступна")
        return self.payments_by_store.get(store_code, [])


def patch_crm(client, monkey_store_code=lambda name: name):
    """
    Подменить импорт клиента внутри _sync_open_shifts_from_crm.

    Функция импортирует retailcrm_client лениво, поэтому подменяем атрибуты
    самого модуля — реальный сетевой клиент не создаётся.
    """
    from cashshifts import retailcrm_client
    retailcrm_client.get_fast_client = lambda: client
    retailcrm_client.get_store_code_from_name = monkey_store_code


def patch_user(role, username="tester"):
    server.get_current_user_role = lambda: role
    server.get_current_username = lambda: username


def main():
    print("=== Офлайн-тесты «Открытые смены» ===\n")

    print("1. Миграция таблиц")
    init_cashshifts_tables()
    conn = get_db()
    columns = [r[1] for r in conn.execute("PRAGMA table_info(cash_shifts)").fetchall()]
    conn.close()
    check("cash_orders_synced_at" in columns, "колонка cash_orders_synced_at добавлена")

    # Повторный вызов не должен падать (2 воркера gunicorn стартуют параллельно)
    init_cashshifts_tables()
    check(True, "повторная инициализация не падает")

    stores = get_all_stores()
    categories = get_all_categories()
    check(len(stores) >= 3, f"seed-точек достаточно для теста ({len(stores)})")

    store_a, store_b, store_c = stores[0], stores[1], stores[2]

    print("\n2. Открытые смены и фильтр по точкам")
    now = datetime.now()
    shift_a = create_cash_shift(
        store_id=store_a["id"], shift_type="day",
        datetime_start=now.strftime("%Y-%m-%d %H:%M:%S"),
        opening_balance=1000.0, florist_username="florist_a"
    )
    shift_b = create_cash_shift(
        store_id=store_b["id"], shift_type="night",
        datetime_start=now.strftime("%Y-%m-%d %H:%M:%S"),
        opening_balance=2000.0, florist_username="florist_b"
    )
    # Смена на третьей точке — закрытая, в таблицу открытых попасть не должна
    shift_c = create_cash_shift(
        store_id=store_c["id"], shift_type="day",
        datetime_start=now.strftime("%Y-%m-%d %H:%M:%S"),
        opening_balance=500.0, florist_username="florist_c"
    )
    storage.update_cash_shift(shift_id=shift_c, status="closed", actual_balance=500.0)

    all_open = get_open_shifts()
    check(len(all_open) == 2, f"админ видит обе открытые смены (получено {len(all_open)})")
    check(
        all(s["status"] == "open" for s in all_open),
        "закрытая смена в выборку не попала"
    )
    check(
        all_open[0].get("store_name"), "store_name подтягивается из JOIN stores"
    )

    only_a = get_open_shifts([store_a["id"]])
    check(len(only_a) == 1 and only_a[0]["id"] == shift_a,
          "менеджер видит только смену своей точки")

    check(get_open_shifts([]) == [], "пустой список точек = пустая выборка (без утечки чужих смен)")

    print("\n3. TTL: когда идти в CRM")
    fresh = {"cash_orders_total": 100.0,
             "cash_orders_synced_at": now.strftime("%Y-%m-%d %H:%M:%S")}
    old = {"cash_orders_total": 100.0,
           "cash_orders_synced_at": (now - timedelta(seconds=server.OPEN_SHIFTS_CRM_TTL_SECONDS + 60))
           .strftime("%Y-%m-%d %H:%M:%S")}
    check(not _crm_data_is_stale(fresh), "свежие данные не перезапрашиваются")
    check(_crm_data_is_stale(old), "протухшие данные перезапрашиваются")
    check(_crm_data_is_stale({"cash_orders_total": None, "cash_orders_synced_at": None}),
          "смена без данных считается протухшей")
    check(_crm_data_is_stale({"cash_orders_total": 5.0, "cash_orders_synced_at": "мусор"}),
          "битая метка времени считается протухшей")

    print("\n4. Синхронизация с CRM (фейковый клиент)")
    shifts = get_open_shifts()
    codes = {s["store_name"]: s["store_name"] for s in shifts}
    client = FakeCRMClient(payments_by_store={
        shifts[0]["store_name"]: [
            {"retailcrm_order_id": 1, "amount": 1500.0, "paid_at": "2026-08-19 10:00:00", "order_data": None},
            {"retailcrm_order_id": 2, "amount": 500.0, "paid_at": "2026-08-19 11:00:00", "order_data": None},
        ],
        shifts[1]["store_name"]: [
            {"retailcrm_order_id": 3, "amount": 300.0, "paid_at": "2026-08-19 12:00:00", "order_data": None},
        ],
    })
    patch_crm(client)
    _sync_open_shifts_from_crm(shifts)

    check(shifts[0]["cash_orders_total"] == 2000.0,
          f"продажи первой смены просуммированы (получено {shifts[0]['cash_orders_total']})")
    check(shifts[1]["cash_orders_total"] == 300.0, "продажи второй смены просуммированы")

    from_db = get_cash_shift_by_id(shifts[0]["id"])
    check(from_db["cash_orders_total"] == 2000.0, "итог сохранён в БД")
    check(bool(from_db["cash_orders_synced_at"]), "метка синхронизации проставлена")

    cached = storage.get_shift_cash_orders(shifts[0]["id"])
    check(len(cached) == 2, f"заказы легли в кэш для журнала смены (получено {len(cached)})")

    # Повторный вызов не должен ходить в CRM: данные свежие
    calls_before = len(client.calls)
    _sync_open_shifts_from_crm(get_open_shifts())
    check(len(client.calls) == calls_before, "повторная загрузка страницы не дёргает CRM (TTL)")

    # force=True игнорирует TTL
    _sync_open_shifts_from_crm(get_open_shifts(), force=True)
    check(len(client.calls) > calls_before, "кнопка «Обновить» перезапрашивает CRM принудительно")

    # Кэш заказов не должен дублироваться при повторной синхронизации
    cached_again = storage.get_shift_cash_orders(shifts[0]["id"])
    check(len(cached_again) == 2, f"кэш заказов перезаписан, а не задублирован ({len(cached_again)})")

    print("\n5. Отказ CRM не роняет таблицу")
    failing = FakeCRMClient(fail_stores={shifts[0]["store_name"]}, payments_by_store={
        shifts[1]["store_name"]: [
            {"retailcrm_order_id": 9, "amount": 777.0, "paid_at": "2026-08-19 13:00:00", "order_data": None},
        ],
    })
    patch_crm(failing)
    shifts_after_fail = get_open_shifts()
    _sync_open_shifts_from_crm(shifts_after_fail, force=True)
    check(shifts_after_fail[0]["cash_orders_total"] == 2000.0,
          "упавшая точка сохранила прошлое значение")
    check(shifts_after_fail[1]["cash_orders_total"] == 777.0,
          "остальные точки обновились, несмотря на ошибку соседней")

    print("\n6. Бюджет времени на запросы к CRM")
    slow = FakeCRMClient(delay=0.4, payments_by_store={})
    patch_crm(slow)
    original_budget = server.OPEN_SHIFTS_CRM_BUDGET_SECONDS
    server.OPEN_SHIFTS_CRM_BUDGET_SECONDS = 0.3
    try:
        started = time.monotonic()
        _sync_open_shifts_from_crm(get_open_shifts(), force=True)
        elapsed = time.monotonic() - started
        check(len(slow.calls) == 1,
              f"после исчерпания бюджета остальные точки не опрашиваются (запросов: {len(slow.calls)})")
        check(elapsed < 1.5, f"общее время ограничено ({elapsed:.2f}с)")
    finally:
        server.OPEN_SHIFTS_CRM_BUDGET_SECONDS = original_budget

    print("\n7. Правка открытой смены: права ролей")
    # Возвращаем смене известные продажи: тест бюджета выше синхронизировал её
    # «медленным» клиентом с пустым ответом и обнулил cash_orders_total
    patch_crm(client)
    _sync_open_shifts_from_crm(get_open_shifts(), force=True)
    shift = get_cash_shift_by_id(shift_a)
    sales_a = shift["cash_orders_total"]
    check(sales_a == 2000.0, f"продажи смены восстановлены перед правкой ({sales_a})")

    patch_user("florist", "florist_a")
    set_user_stores("florist_a", [store_a["id"]])
    with app.test_request_context(json={"opening_balance": 4242.0}):
        try:
            _edit_open_shift(shift)
            check(False, "флорист не должен править открытую смену")
        except server.ShiftEditForbiddenError:
            check(True, "флористу правка открытой смены запрещена")

    patch_user("manager", "manager_other")
    set_user_stores("manager_other", [store_b["id"]])
    with app.test_request_context(json={"opening_balance": 4242.0}):
        try:
            _edit_open_shift(shift)
            check(False, "менеджер не должен править чужую точку")
        except server.ShiftEditForbiddenError:
            check(True, "менеджеру запрещена правка смены чужой точки")

    print("\n8. Правка открытой смены: начальный остаток")
    patch_user("manager", "manager_a")
    set_user_stores("manager_a", [store_a["id"]])
    with app.test_request_context(json={"opening_balance": 3333.0}):
        response = _edit_open_shift(shift)
    updated = get_cash_shift_by_id(shift_a)
    check(updated["opening_balance"] == 3333.0,
          f"менеджер поправил начальный остаток своей точки (стало {updated['opening_balance']})")
    check(updated["status"] == "open", "смена осталась открытой")
    check(updated["actual_balance"] is None, "фактический остаток не появился раньше закрытия")
    check(updated["discrepancy"] is None, "расхождение не считается до закрытия")
    check(updated["expected_balance"] == 3333.0 + sales_a,
          f"плановый остаток пересчитан от нового начального (стало {updated['expected_balance']})")

    print("\n9. Правка открытой смены: суммы инкассаций")
    collection_id = create_collection(
        shift_id=shift_a, amount=500.0, expense_category_id=categories[0]["id"],
        date=now.strftime("%Y-%m-%d %H:%M:%S"), created_by="florist_a"
    )
    patch_user("admin", "admin")
    with app.test_request_context(json={"collections": [{"id": collection_id, "amount": 800.0}]}):
        _edit_open_shift(get_cash_shift_by_id(shift_a))
    updated = get_cash_shift_by_id(shift_a)
    check(updated["collections_total"] == 800.0,
          f"сумма инкассаций пересчитана (стало {updated['collections_total']})")
    check(updated["expected_balance"] == 3333.0 + sales_a - 800.0,
          f"плановый остаток учёл инкассацию (стало {updated['expected_balance']})")

    print("\n10. Защита от мусора в теле запроса")
    with app.test_request_context(json={"actual_balance": 999.0}):
        result = _edit_open_shift(get_cash_shift_by_id(shift_a))
    check(isinstance(result, tuple) and result[1] == 400,
          "фактический остаток для открытой смены отклоняется с 400")

    with app.test_request_context(json={}):
        result = _edit_open_shift(get_cash_shift_by_id(shift_a))
    check(isinstance(result, tuple) and result[1] == 400,
          "пустое тело запроса отклоняется")

    with app.test_request_context(json={"collections": [{"id": 999999, "amount": 1.0}]}):
        result = _edit_open_shift(get_cash_shift_by_id(shift_a))
    check(isinstance(result, tuple) and result[1] == 404,
          "чужая инкассация отклоняется с 404")

    with app.test_request_context(json={"opening_balance": "не число"}):
        result = _edit_open_shift(get_cash_shift_by_id(shift_a))
    check(isinstance(result, tuple) and result[1] == 400,
          "нечисловой начальный остаток отклоняется, а не пишется в денежную колонку")

    with app.test_request_context(json={"collections": [{"id": collection_id, "amount": "abc"}]}):
        result = _edit_open_shift(get_cash_shift_by_id(shift_a))
    check(isinstance(result, tuple) and result[1] == 400,
          "нечисловая сумма инкассации отклоняется")

    survived = get_cash_shift_by_id(shift_a)
    check(isinstance(survived["opening_balance"], float),
          f"начальный остаток остался числом ({survived['opening_balance']!r})")
    check(storage.get_collections_total(shift_a) == 800.0,
          "инкассации не пострадали от отклонённых запросов")

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
