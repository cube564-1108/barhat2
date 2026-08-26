"""
Офлайн-тесты справочника категорий инкассации — без обращений к внешним API.

Проверяет:
  1. create_category реактивирует ранее удалённую категорию (soft-delete + UNIQUE(name))
  2. update_category умеет переименовать в имя удалённой категории
  3. get_categories_with_usage считает инкассации по категории
  4. Удалённая категория остаётся видимой в уже проведённых инкассациях
  5. HTTP: GET/POST/PUT/DELETE /api/cash-shifts/categories и права ролей

Запуск: python scripts/test_collection_categories.py
"""

import os
import sys
import io
import tempfile
from datetime import datetime

if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# Временная БД — до импорта storage/auth, путь читается на уровне модуля
_tmp_db = os.path.join(tempfile.mkdtemp(), "test_collection_categories.db")
os.environ["BARHAT_DB_PATH"] = _tmp_db

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from flask import Flask
from werkzeug.security import generate_password_hash

from auth import flush_audit_log

from cashshifts.storage import (
    init_cashshifts_tables,
    get_db,
    create_category,
    update_category,
    delete_category,
    get_category_by_id,
    get_all_categories,
    get_categories_with_usage,
    get_all_stores,
    create_cash_shift,
    create_collection,
    get_shift_collections,
    set_user_stores,
)

failures = []


def check(condition, message):
    if condition:
        print(f"   [ok] {message}")
    else:
        print(f"   [FAIL] {message}")
        failures.append(message)


def make_app():
    """Минимальное приложение: авторизация + blueprint кассовых смен."""
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
    conn.execute(
        """
        INSERT INTO users (username, full_name, password_hash, role, is_active, created_at)
        VALUES (?, ?, ?, ?, 1, ?)
        """,
        (username, username, generate_password_hash("secret"), role,
         datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()


def login(client, username):
    return client.post("/api/auth/login",
                       json={"username": username, "password": "secret"})


def main():
    print("=== Офлайн-тесты справочника категорий инкассации ===\n")

    print("1. Инициализация")
    init_cashshifts_tables()
    seeded = get_all_categories()
    check(len(seeded) > 0, f"seed-категории на месте ({len(seeded)})")

    print("\n2. Soft-delete + UNIQUE(name)")
    cid = create_category("Тестовая категория")
    check(get_category_by_id(cid)["name"] == "Тестовая категория", "категория создана")

    delete_category(cid)
    check(get_category_by_id(cid) is None, "удалённая категория пропала из активных")

    reused = create_category("Тестовая категория")
    check(reused == cid, f"повторное создание с тем же именем реактивирует запись (id {reused} == {cid})")

    ghost = create_category("Призрак")
    delete_category(ghost)
    update_category(cid, "Призрак")
    check(get_category_by_id(cid)["name"] == "Призрак",
          "переименование в имя удалённой категории проходит")
    check(get_category_by_id(ghost) is None, "запись-призрак вычищена")

    print("\n3. usage_count и сохранность истории")
    store = get_all_stores()[0]
    shift = create_cash_shift(
        store_id=store["id"], shift_type="day",
        datetime_start=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        opening_balance=0.0, florist_username="florist_test"
    )
    create_collection(shift, 500.0, cid, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    usage = {c["name"]: c["usage_count"] for c in get_categories_with_usage()}
    check(usage.get("Призрак") == 1, f"usage_count у использованной категории = 1 (получено {usage.get('Призрак')})")
    check(usage.get(seeded[0]["name"]) == 0, "неиспользованная категория считается нулём")

    delete_category(cid)
    collections = get_shift_collections(shift)
    check(collections and collections[0]["category_name"] == "Призрак",
          "инкассация сохраняет название удалённой категории")

    print("\n4. HTTP-эндпоинты и права ролей")
    app = make_app()
    add_user("admin_test", "admin")
    add_user("florist_test", "florist")
    set_user_stores("florist_test", [store["id"]])

    with app.test_client() as client:
        check(login(client, "admin_test").status_code == 200, "админ логинится")

        res = client.get("/api/cash-shifts/categories")
        check(res.status_code == 200 and "usage_count" not in res.get_json()["categories"][0],
              "GET без with_usage не считает использование")

        res = client.get("/api/cash-shifts/categories?with_usage=1")
        check(res.status_code == 200 and "usage_count" in res.get_json()["categories"][0],
              "GET ?with_usage=1 отдаёт usage_count")

        res = client.post("/api/cash-shifts/categories", json={"name": "  Новая статья  "})
        check(res.status_code == 201, f"POST создаёт категорию (код {res.status_code})")
        new_id = res.get_json()["id"]
        check(get_category_by_id(new_id)["name"] == "Новая статья", "имя обрезается по краям")

        res = client.post("/api/cash-shifts/categories", json={"name": "   "})
        check(res.status_code == 400, "POST с пустым именем отклоняется")

        res = client.post("/api/cash-shifts/categories", json={"name": "Новая статья"})
        check(res.status_code == 400 and "уже есть" in res.get_json()["error"],
              "дубликат активной категории даёт понятную ошибку, а не 500")

        res = client.put(f"/api/cash-shifts/categories/{new_id}", json={"name": "Новая статья 2"})
        check(res.status_code == 200 and get_category_by_id(new_id)["name"] == "Новая статья 2",
              "PUT переименовывает")

        res = client.put("/api/cash-shifts/categories/999999", json={"name": "Нет такой"})
        check(res.status_code == 404, "PUT по несуществующему id — 404")

        res = client.delete(f"/api/cash-shifts/categories/{new_id}")
        check(res.status_code == 200 and get_category_by_id(new_id) is None, "DELETE деактивирует")

        res = client.delete(f"/api/cash-shifts/categories/{new_id}")
        check(res.status_code == 404, "повторный DELETE — 404")

        # Аудит пишется фоновым потоком (auth.log_action), поэтому читать его
        # сразу после запроса можно только через flush — иначе тест плавающий.
        flush_audit_log()

        conn = get_db()
        actions = [r[0] for r in conn.execute(
            "SELECT action FROM audit_log WHERE username = 'admin_test'").fetchall()]
        conn.close()
        check({"create_collection_category", "update_collection_category",
               "delete_collection_category"}.issubset(set(actions)),
              f"правки справочника попали в audit_log ({actions})")

        client.post("/api/auth/logout")

    with app.test_client() as client:
        check(login(client, "florist_test").status_code == 200, "флорист логинится")

        res = client.get("/api/cash-shifts/categories")
        check(res.status_code == 200, "флорист читает справочник (нужен для инкассации)")

        res = client.post("/api/cash-shifts/categories", json={"name": "Флорист хулиганит"})
        check(res.status_code == 403, f"флористу запрещено создавать категории (код {res.status_code})")

        res = client.put("/api/cash-shifts/categories/1", json={"name": "Переименую"})
        check(res.status_code == 403, "флористу запрещено переименовывать")

        res = client.delete("/api/cash-shifts/categories/1")
        check(res.status_code == 403, "флористу запрещено удалять")

    print()
    if failures:
        print(f"=== ПРОВАЛЕНО ПРОВЕРОК: {len(failures)} ===")
        for f in failures:
            print(f"  - {f}")
        return False

    print("=== Все проверки пройдены ===")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
