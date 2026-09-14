"""
Офлайн-тесты правки статьи инкассации — без сети и без прод-базы.

Проверяет:
  1. Админ меняет статью инкассации в закрытой смене (PUT /api/cash-shifts/<id>)
  2. Новая статья сразу видна в сводной таблице «Инкассации по салонам»
  3. Сумма и итоги смены при смене статьи не меняются
  4. Не-админ статью сменить не может (флорист правит только суммы)
  5. Статья, удалённая из справочника, назначаться не может
  6. Ошибка в одной строке не применяет правки соседних (проверка до записи)
  7. То же правило работает и для открытой смены

Запуск: python scripts/test_collection_category_edit.py
"""

import os
import sys
import io
import json
import tempfile
from datetime import datetime

if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# Временная БД — до импорта storage, путь читается на уровне модуля
_tmp_db = os.path.join(tempfile.mkdtemp(), "test_collection_category_edit.db")
os.environ["BARHAT_DB_PATH"] = _tmp_db

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from flask import Flask

from cashshifts.storage import (
    init_cashshifts_tables,
    get_db,
    create_cash_shift,
    create_collection,
    get_shift_collections,
    get_all_stores,
    get_all_categories,
    list_collections,
    delete_category,
    update_cash_shift,
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


def put_shift(shift_id, body):
    """Вызвать PUT /api/cash-shifts/<id> напрямую и вернуть (данные, http-код)."""
    with app.test_request_context(
        f"/api/cash-shifts/{shift_id}",
        method="PUT",
        json=body
    ):
        result = server.edit_shift(shift_id)
        if isinstance(result, tuple):
            response, status = result
        else:
            response, status = result, 200
        return json.loads(response.get_data(as_text=True)), status


def category_of(collection_id):
    """Статья инкассации так, как её видит сводная таблица по салонам."""
    row = next(r for r in list_collections() if r["id"] == collection_id)
    return row["category_name"]


def amount_of(collection_id):
    row = next(r for r in list_collections() if r["id"] == collection_id)
    return row["amount"]


def main():
    print("=== Офлайн-тесты правки статьи инкассации ===\n")

    print("1. Подготовка данных")
    init_cashshifts_tables()

    stores = get_all_stores()
    categories = get_all_categories()
    check(len(stores) >= 1, f"seed-точек достаточно ({len(stores)})")
    check(len(categories) >= 3, f"seed-статей достаточно ({len(categories)})")

    store = stores[0]
    cat_x, cat_y, cat_z = categories[0], categories[1], categories[2]

    day = datetime(2026, 9, 10, 12, 0, 0).strftime("%Y-%m-%d %H:%M:%S")

    closed_shift = create_cash_shift(
        store_id=store["id"], shift_type="day",
        datetime_start=day, opening_balance=1000.0,
        florist_username="florist_a"
    )
    update_cash_shift(
        shift_id=closed_shift,
        status="closed",
        closed_at=day,
        actual_balance=1300.0,
        cash_orders_total=800.0,
        collections_total=500.0,
        expected_balance=1300.0,
        discrepancy=0.0
    )

    # Без привязки к салону флорист не пройдёт check_store_access, и ветка
    # «сумму правит, статью нет» просто не исполнится (проверка была бы холостой)
    set_user_stores("florist_a", [store["id"]])

    coll_a = create_collection(
        shift_id=closed_shift, amount=500.0, expense_category_id=cat_x["id"],
        date=day, created_by="florist_a"
    )
    check(category_of(coll_a) == cat_x["name"],
          f"стартовая статья инкассации — «{cat_x['name']}»")

    print("\n2. Админ меняет статью в закрытой смене")
    patch_user("admin", "admin")
    data, status = put_shift(closed_shift, {
        "actual_balance": 1300.0,
        "collections": [{"id": coll_a, "amount": 500.0,
                         "expense_category_id": cat_y["id"]}]
    })
    check(status == 200 and data["success"], f"ответ 200 (получено {status})")
    check(category_of(coll_a) == cat_y["name"],
          "новая статья видна в таблице «Инкассации по салонам»")
    check(
        get_shift_collections(closed_shift)[0]["category_name"] == cat_y["name"],
        "новая статья видна в журнале смены"
    )
    check(amount_of(coll_a) == 500.0, "сумма инкассации не пострадала")
    check(data["shift"]["collections_total"] == 500.0,
          "итог инкассаций смены не изменился от смены статьи")
    check(data["shift"]["discrepancy"] == 0.0,
          "расхождение по смене не изменилось от смены статьи")

    print("\n3. Статья и сумма правятся одним запросом")
    data, status = put_shift(closed_shift, {
        "actual_balance": 1200.0,
        "collections": [{"id": coll_a, "amount": 600.0,
                         "expense_category_id": cat_z["id"]}]
    })
    check(status == 200, f"ответ 200 (получено {status})")
    check(category_of(coll_a) == cat_z["name"] and amount_of(coll_a) == 600.0,
          "применились обе правки сразу")
    check(data["shift"]["collections_total"] == 600.0,
          "итог инкассаций пересчитан по новой сумме")

    print("\n4. Та же статья повторно — не ошибка")
    data, status = put_shift(closed_shift, {
        "actual_balance": 1200.0,
        "collections": [{"id": coll_a, "amount": 600.0,
                         "expense_category_id": cat_z["id"]}]
    })
    check(status == 200, f"повторная отправка той же статьи проходит ({status})")

    print("\n5. Не-админ статью сменить не может")
    patch_user("florist", "florist_a")
    data, status = put_shift(closed_shift, {
        "actual_balance": 1200.0,
        "collections": [{"id": coll_a, "amount": 600.0,
                         "expense_category_id": cat_x["id"]}]
    })
    check(status == 403, f"флористу отказано (получено {status})")
    check(category_of(coll_a) == cat_z["name"], "статья осталась прежней")

    data, status = put_shift(closed_shift, {
        "actual_balance": 1250.0,
        "collections": [{"id": coll_a, "amount": 650.0}]
    })
    check(status == 200 and amount_of(coll_a) == 650.0,
          "сумму флорист по-прежнему правит (последняя закрытая смена своей точки)")

    print("\n6. Статья, удалённая из справочника, не назначается")
    patch_user("admin", "admin")
    delete_category(cat_x["id"])
    data, status = put_shift(closed_shift, {
        "actual_balance": 1250.0,
        "collections": [{"id": coll_a, "amount": 650.0,
                         "expense_category_id": cat_x["id"]}]
    })
    check(status == 404, f"отказ по удалённой статье (получено {status})")
    check(category_of(coll_a) == cat_z["name"], "статья осталась прежней")

    print("\n7. Ошибка в одной строке не применяет правки соседних")
    coll_b = create_collection(
        shift_id=closed_shift, amount=100.0, expense_category_id=cat_y["id"],
        date=day, created_by="florist_a"
    )
    data, status = put_shift(closed_shift, {
        "actual_balance": 1250.0,
        "collections": [
            {"id": coll_a, "amount": 777.0, "expense_category_id": cat_y["id"]},
            {"id": coll_b, "amount": "не число"}
        ]
    })
    check(status == 400, f"запрос отклонён целиком (получено {status})")
    check(amount_of(coll_a) == 650.0 and category_of(coll_a) == cat_z["name"],
          "первая строка не применилась: правки проверяются до записи")

    print("\n8. Чужая инкассация в теле запроса")
    other_shift = create_cash_shift(
        store_id=store["id"], shift_type="night",
        datetime_start=day, opening_balance=0.0,
        florist_username="florist_a"
    )
    data, status = put_shift(other_shift, {
        "collections": [{"id": coll_a, "amount": 1.0,
                         "expense_category_id": cat_y["id"]}]
    })
    check(status == 404, f"инкассация чужой смены отклонена (получено {status})")
    check(amount_of(coll_a) == 650.0, "сумма чужой инкассации не изменилась")

    print("\n9. Открытая смена: то же правило про статью")
    coll_open = create_collection(
        shift_id=other_shift, amount=200.0, expense_category_id=cat_y["id"],
        date=day, created_by="florist_a"
    )
    patch_user("manager", "manager_a")
    conn = get_db()
    conn.execute(
        "INSERT OR IGNORE INTO user_stores (username, store_id) VALUES (?, ?)",
        ("manager_a", store["id"])
    )
    conn.commit()
    conn.close()

    data, status = put_shift(other_shift, {
        "opening_balance": 500.0,
        "collections": [{"id": coll_open, "amount": 200.0,
                         "expense_category_id": cat_z["id"]}]
    })
    check(status == 403, f"менеджеру отказано в смене статьи (получено {status})")
    check(category_of(coll_open) == cat_y["name"], "статья осталась прежней")

    patch_user("admin", "admin")
    data, status = put_shift(other_shift, {
        "opening_balance": 500.0,
        "collections": [{"id": coll_open, "amount": 250.0,
                         "expense_category_id": cat_z["id"]}]
    })
    check(status == 200, f"админ правит статью и в открытой смене ({status})")
    check(category_of(coll_open) == cat_z["name"] and amount_of(coll_open) == 250.0,
          "правки применились")

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
