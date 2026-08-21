"""
Офлайн-тесты таблицы «Инкассации по салонам» — без сети и без прод-базы.

Проверяет:
  1. list_collections: салон подтягивается через смену, фильтры по точкам и датам
  2. get_collections_by_store: итоги считаются по всему периоду, а не по странице
  3. Эндпоинт /api/cash-shifts/collections: сужение выборки по ролям, лимит,
     ФИО автора инкассации

Запуск: python scripts/test_collections_report.py
"""

import os
import sys
import io
import json
import tempfile
from datetime import datetime, timedelta

if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# Временная БД — до импорта storage, путь читается на уровне модуля
_tmp_db = os.path.join(tempfile.mkdtemp(), "test_collections_report.db")
os.environ["BARHAT_DB_PATH"] = _tmp_db

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from flask import Flask

from cashshifts import storage
from cashshifts.storage import (
    init_cashshifts_tables,
    get_db,
    create_cash_shift,
    create_collection,
    get_all_stores,
    get_all_categories,
    list_collections,
    get_collections_by_store,
    set_user_stores,
)
from cashshifts import server

app = Flask(__name__)
# login_required пропускает запрос: аутентификацию здесь не тестируем,
# роль подменяется напрямую через patch_user
app.config["LOGIN_DISABLED"] = True

failures = []


def check(condition, message):
    if condition:
        print(f"   [ok] {message}")
    else:
        print(f"   [FAIL] {message}")
        failures.append(message)


def patch_user(role, username="tester"):
    server.get_current_user_role = lambda: role
    server.get_current_username = lambda: username


def call_endpoint(query_string):
    """Вызвать view напрямую и вернуть (данные, http-код)."""
    with app.test_request_context(f"/api/cash-shifts/collections?{query_string}"):
        result = server.list_all_collections()
        if isinstance(result, tuple):
            response, status = result
        else:
            response, status = result, 200
        return json.loads(response.get_data(as_text=True)), status


def main():
    print("=== Офлайн-тесты «Инкассации по салонам» ===\n")

    print("1. Подготовка данных")
    init_cashshifts_tables()

    stores = get_all_stores()
    categories = get_all_categories()
    check(len(stores) >= 2, f"seed-точек достаточно для теста ({len(stores)})")
    check(len(categories) >= 2, f"seed-категорий достаточно ({len(categories)})")

    store_a, store_b = stores[0], stores[1]
    cat_x, cat_y = categories[0], categories[1]

    today = datetime(2026, 8, 20, 12, 0, 0)
    old_day = today - timedelta(days=10)

    shift_a = create_cash_shift(
        store_id=store_a["id"], shift_type="day",
        datetime_start=today.strftime("%Y-%m-%d %H:%M:%S"),
        opening_balance=1000.0, florist_username="florist_a"
    )
    shift_b = create_cash_shift(
        store_id=store_b["id"], shift_type="night",
        datetime_start=today.strftime("%Y-%m-%d %H:%M:%S"),
        opening_balance=2000.0, florist_username="florist_b"
    )

    # ФИО автора: таблица users принадлежит auth.py, но живёт в той же БД
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            full_name TEXT
        )
    """)
    conn.execute(
        "INSERT OR IGNORE INTO users (username, full_name) VALUES (?, ?)",
        ("florist_a", "Анна Иванова")
    )
    conn.commit()
    conn.close()

    create_collection(
        shift_id=shift_a, amount=500.0, expense_category_id=cat_x["id"],
        date=today.strftime("%Y-%m-%d %H:%M:%S"),
        custom_comment="сдача в банк", created_by="florist_a"
    )
    create_collection(
        shift_id=shift_a, amount=300.0, expense_category_id=cat_y["id"],
        date=old_day.strftime("%Y-%m-%d %H:%M:%S"), created_by="florist_a"
    )
    create_collection(
        shift_id=shift_b, amount=1200.0, expense_category_id=cat_x["id"],
        date=today.strftime("%Y-%m-%d %H:%M:%S"), created_by="florist_b"
    )

    print("\n2. list_collections: салон, статья, автор")
    rows = list_collections()
    check(len(rows) == 3, f"видны все инкассации по всем салонам ({len(rows)})")
    check(
        all(r["store_name"] for r in rows),
        "салон подтянут через смену (JOIN cash_shifts → stores)"
    )
    check(
        all(r["category_name"] for r in rows),
        "статья инкассации подтянута из справочника"
    )
    check(rows[0]["date"] >= rows[-1]["date"], "сортировка по дате: свежие сверху")

    print("\n3. Фильтр по салонам")
    only_a = list_collections(store_ids=[store_a["id"]])
    check(len(only_a) == 2, f"по одному салону только его инкассации ({len(only_a)})")
    check(
        all(r["store_id"] == store_a["id"] for r in only_a),
        "чужие салоны в выборку не попали"
    )
    check(
        list_collections(store_ids=[]) == [],
        "пустой список точек = пустая выборка (без утечки чужих инкассаций)"
    )

    print("\n4. Фильтр по периоду")
    day_start = today.strftime("%Y-%m-%d 00:00:00")
    day_end = today.strftime("%Y-%m-%d 23:59:59")
    in_period = list_collections(date_from=day_start, date_to=day_end)
    check(len(in_period) == 2, f"за день попали только его инкассации ({len(in_period)})")
    check(
        all(day_start <= r["date"] <= day_end for r in in_period),
        "инкассация десятидневной давности отфильтрована"
    )

    print("\n5. Итоги по салонам")
    totals = get_collections_by_store()
    by_id = {row["store_id"]: row for row in totals}
    check(by_id[store_a["id"]]["total"] == 800.0,
          f"итог первого салона (получено {by_id[store_a['id']]['total']})")
    check(by_id[store_a["id"]]["count"] == 2, "количество инкассаций первого салона")
    check(by_id[store_b["id"]]["total"] == 1200.0, "итог второго салона")
    check(totals[0]["total"] >= totals[-1]["total"], "салоны отсортированы по сумме")

    period_totals = get_collections_by_store(date_from=day_start, date_to=day_end)
    period_by_id = {row["store_id"]: row for row in period_totals}
    check(period_by_id[store_a["id"]]["total"] == 500.0,
          "итог по салону считается с учётом периода")

    print("\n6. Эндпоинт: админ видит все салоны")
    patch_user("admin", "admin")
    data, status = call_endpoint("")
    check(status == 200 and data["success"], "ответ 200")
    check(len(data["collections"]) == 3, f"админу отданы все инкассации ({len(data['collections'])})")
    check(data["total"] == 2000.0, f"итоговая сумма за период ({data['total']})")
    check(len(data["by_store"]) == 2, "итоги по обоим салонам")
    author = next(c for c in data["collections"] if c["created_by"] == "florist_a")
    check(author["created_by_full_name"] == "Анна Иванова", "ФИО автора подставлено")
    unknown = next(c for c in data["collections"] if c["created_by"] == "florist_b")
    check(unknown["created_by_full_name"] is None,
          "автор без учётки не ломает выборку (фронт покажет username)")

    print("\n7. Эндпоинт: не-админ ограничен своими салонами")
    patch_user("manager", "manager_a")
    set_user_stores("manager_a", [store_a["id"]])
    data, status = call_endpoint("")
    check(status == 200, "ответ 200 для менеджера")
    check(len(data["collections"]) == 2, f"менеджер видит только свой салон ({len(data['collections'])})")
    check(data["total"] == 800.0, f"итог посчитан только по своим салонам ({data['total']})")

    patch_user("manager", "manager_nowhere")
    data, status = call_endpoint("")
    check(data["collections"] == [] and data["total"] == 0,
          "менеджер без привязок не видит ничего")

    print("\n8. Эндпоинт: чужой салон в фильтре")
    patch_user("manager", "manager_a")
    data, status = call_endpoint(f"store_id={store_b['id']}")
    check(status == 403, f"запрос чужого салона отклонён (получено {status})")

    data, status = call_endpoint(f"store_id={store_a['id']}")
    check(status == 200 and len(data["collections"]) == 2, "свой салон в фильтре работает")

    print("\n9. Эндпоинт: лимит и период")
    patch_user("admin", "admin")
    data, _ = call_endpoint("limit=1")
    check(len(data["collections"]) == 1, "лимит режет строки")
    check(data["total"] == 2000.0,
          f"итоги считаются по всему периоду, а не по обрезанной странице ({data['total']})")
    check(data["limit"] == 1, "лимит возвращается фронтенду (для подсказки об обрезке)")

    data, _ = call_endpoint("limit=99999")
    check(data["limit"] == 1000, "лимит сверху ограничен, БД не выгружается целиком")

    data, _ = call_endpoint(f"date_from={day_start}&date_to={day_end}")
    check(len(data["collections"]) == 2 and data["total"] == 1700.0,
          f"фильтр по периоду работает через эндпоинт ({data['total']})")

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
