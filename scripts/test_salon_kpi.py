"""
Сторож раздела «Показатели салонов».

Проверяет то, что ломается молча:
  - агрегаты API сходятся с прямым SQL по витринам;
  - управляющий видит только свои салоны, чужой store_id даёт 403;
  - учётка без привязанных салонов получает 200 и признак no_stores,
    а не пустую таблицу и не 500;
  - месяц без плана даёт plan=null и отсутствие процента, а не 0%;
  - ключ, которого нет в справочнике, попадает в «несопоставленное»
    и не пропадает из общей суммы;
  - доля расходов не считается, если нет одной из двух частей.

ВАЖНО: прогон читает боевой .env, поэтому сеть глушится до импорта приложения —
иначе планировщики уйдут в реальные API.

Запуск: python scripts/test_salon_kpi.py
"""

import os
import socket
import sqlite3
import ssl  # noqa: F401  — импортировать до патча сокета
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))
os.chdir(REPO)

for var in ("PYRUS_SYNC_SCHEDULER", "MOYSKLAD_SYNC_SCHEDULER", "COURIERS_SYNC_SCHEDULER",
            "LINKWATCH_SCHEDULER", "INVOICES_CARD_SYNC_SCHEDULER", "INVOICES_BANKS_SCHEDULER"):
    os.environ[var] = "0"


class NetworkBlocked(Exception):
    pass


def _blocked(*args, **kwargs):
    raise NetworkBlocked("прогон не должен ходить в боевые внешние API")


socket.socket.connect = _blocked
socket.socket.connect_ex = _blocked
socket.create_connection = _blocked

from pyrus.server import app  # noqa: E402
from salonkpi import metrics, storage as kpi_storage  # noqa: E402

FAILURES = []
CHECKS = [0]


def check(condition, title, detail=""):
    CHECKS[0] += 1
    if condition:
        print(f"  OK   {title}")
    else:
        print(f"  FAIL {title}" + (f" — {detail}" if detail else ""))
        FAILURES.append(title)


def section(title):
    print(f"\n{title}")
    print("-" * len(title))


TEST_PASSWORD = "salon-kpi-test-only"


def login_as(client, username):
    """Войти штатной ручкой: сессию flask_login руками не подделать."""
    response = client.post("/api/auth/login",
                           json={"username": username, "password": TEST_PASSWORD})
    assert response.status_code == 200, f"вход {username} не удался: {response.data[:200]}"
    return client


def ensure_user(username, role, store_ids):
    """Завести тестовую учётку с нужными салонами (идемпотентно)."""
    from werkzeug.security import generate_password_hash

    conn = sqlite3.connect(os.environ.get("BARHAT_DB_PATH", "barhat.db"))
    try:
        conn.execute(
            "INSERT OR IGNORE INTO users (username, full_name, password_hash, role, is_active, created_at) "
            "VALUES (?, ?, ?, ?, 1, datetime('now'))",
            (username, username, generate_password_hash(TEST_PASSWORD), role),
        )
        conn.execute(
            "UPDATE users SET role = ?, is_active = 1, password_hash = ? WHERE username = ?",
            (role, generate_password_hash(TEST_PASSWORD), username),
        )
        conn.execute("DELETE FROM user_stores WHERE username = ?", (username,))
        for store_id in store_ids:
            conn.execute("INSERT INTO user_stores (username, store_id) VALUES (?, ?)",
                         (username, store_id))
        # Права раздела: у роли manager они выдаются миграцией, но тестовая
        # учётка могла появиться раньше
        conn.execute(
            "INSERT OR IGNORE INTO permissions (username, module_name, can_view) "
            "VALUES (?, 'salon_kpi', 1)",
            (username,),
        )
        conn.commit()
    finally:
        conn.close()


MONTH = os.environ.get("SALON_KPI_TEST_MONTH", "2026-08")


def main():
    print(f"Сторож показателей салонов, месяц {MONTH}")

    kpi_storage.init_salonkpi_tables()
    date_from, date_to = metrics.month_bounds(MONTH)

    # ------------------------------------------------------------------
    section("1. Агрегаты API против прямого SQL по витринам")

    metrics.invalidate_cache()
    summary = metrics.build_summary(MONTH, None, "salon")
    rows = {row["store_id"]: row for row in summary["rows"]}

    links = kpi_storage.resolve_map(kpi_storage.SOURCE_CRM)
    # Путь берём у самого модуля: он резолвится через storage_paths (на проде это
    # постоянный диск /data), и угадывать его тут — верный способ молча пропустить
    # главную проверку
    from couriers.storage import DB_PATH as couriers_db  # noqa: E402

    if os.path.exists(couriers_db):
        conn = sqlite3.connect(couriers_db)
        try:
            for site, store_id in links.items():
                row = conn.execute(
                    "SELECT COALESCE(SUM(total_summ), 0), COUNT(*) FROM courier_orders "
                    "WHERE status = 'complete' AND site_code = ? "
                    "AND delivery_date >= ? AND delivery_date <= ?",
                    (site, date_from, date_to),
                ).fetchone()
                expected, orders = round(row[0], 2), row[1]
                actual = rows.get(store_id, {}).get("metrics", {}).get("shipments", {}).get("fact")
                if actual is None:
                    continue
                check(abs(actual - expected) < 0.5,
                      f"отгрузки {site}: API {actual:,.0f} = SQL {expected:,.0f}",
                      f"расхождение {actual - expected:,.2f} ₽ ({orders} заказов)")
        finally:
            conn.close()
    else:
        print("  (витрина курьеров не найдена — проверка пропущена)")

    # ------------------------------------------------------------------
    section("2. Показатель не посчитан — это null, а не ноль")

    for store_id, row in rows.items():
        m = row["metrics"]
        if m["shipments"]["plan"] is None:
            check(m["shipments"]["plan_done"] is None,
                  f"{row['name']}: нет плана → нет процента выполнения",
                  f"plan_done={m['shipments']['plan_done']}")
        if m["flower_loss"]["received"] == 0:
            check(m["flower_loss"]["share"] is None,
                  f"{row['name']}: нет прихода цветка → доля списания null",
                  f"share={m['flower_loss']['share']}")
        if m["berry_price"]["used_qty"] <= 0:
            check(m["berry_price"]["price"] is None,
                  f"{row['name']}: списано ≥ прихода → цена клубники null",
                  f"price={m['berry_price']['price']}")
        if m["shipments"]["fact"] == 0:
            check(m["raw_cost"]["total"] is None,
                  f"{row['name']}: нет отгрузок → доля расходов null",
                  f"total={m['raw_cost']['total']}")

    # ------------------------------------------------------------------
    section("3. Качество: две шкалы не смешиваются")

    for store_id, row in rows.items():
        q = row["metrics"]["quality"]
        if q["count14"] and q["count18"]:
            possible = q["count14"] * 14 + q["count18"] * 18
            got = q["avg14"] * q["count14"] + q["avg18"] * q["count18"]
            expected = round(got / possible * 100, 1)
            check(abs(q["percent"] - expected) < 0.15,
                  f"{row['name']}: процент качества = баллы/максимум",
                  f"{q['percent']} ≠ {expected}")
            break

    # ------------------------------------------------------------------
    section("4. Доступ: свои салоны, чужие — 403")

    all_stores = kpi_storage.list_stores()
    if len(all_stores) < 2:
        print("  (нужно минимум 2 салона — проверка пропущена)")
    else:
        own, foreign = all_stores[0]["id"], all_stores[1]["id"]
        ensure_user("test-kpi-manager", "manager", [own])
        ensure_user("test-kpi-nostores", "manager", [])

        with app.test_client() as client:
            login_as(client, "test-kpi-manager")

            response = client.get(f"/api/salon-kpi/summary?month={MONTH}")
            payload = response.get_json() or {}
            store_ids = [r["store_id"] for r in payload.get("data", {}).get("rows", [])]
            check(response.status_code == 200 and store_ids == [own],
                  "менеджер видит только свой салон", f"{response.status_code}, {store_ids}")
            check(payload.get("data", {}).get("can_edit") is False,
                  "менеджеру не отдаётся право правки")

            response = client.get(f"/api/salon-kpi/salon/{foreign}?month={MONTH}")
            check(response.status_code == 403, "чужой салон → 403", str(response.status_code))

            response = client.get(f"/api/salon-kpi/unmapped?month={MONTH}")
            check(response.status_code == 403, "справочник несопоставленного только админу",
                  str(response.status_code))

            response = client.post("/api/salon-kpi/plans",
                                   json={"month": MONTH, "plans": []},
                                   headers={"X-Requested-With": "XMLHttpRequest"})
            check(response.status_code == 403, "менеджер не может править планы",
                  str(response.status_code))

        with app.test_client() as client:
            login_as(client, "test-kpi-nostores")
            response = client.get(f"/api/salon-kpi/summary?month={MONTH}")
            payload = response.get_json() or {}
            check(response.status_code == 200 and payload.get("data", {}).get("no_stores") is True,
                  "менеджер без салонов: 200 и признак no_stores",
                  f"{response.status_code}, {payload.get('data', {}).get('no_stores')}")

    # ------------------------------------------------------------------
    section("5. Несопоставленный ключ виден и не теряется молча")

    unmapped = metrics.unmapped(MONTH)
    keys = {item["key"] for item in unmapped["items"]}
    known = set(kpi_storage.resolve_map(kpi_storage.SOURCE_CRM))
    check(not (keys & known), "привязанные ключи не попадают в несопоставленное",
          str(keys & known))

    # Отвязываем реальный ключ и проверяем, что он всплыл
    sample = next(iter(known), None)
    if sample:
        conn = kpi_storage.get_db()
        try:
            row = conn.execute(
                "SELECT id, store_id FROM salon_links WHERE source = ? AND external_key = ?",
                (kpi_storage.SOURCE_CRM, sample),
            ).fetchone()
            conn.execute("DELETE FROM salon_links WHERE id = ?", (row["id"],))
            conn.commit()
        finally:
            conn.close()

        metrics.invalidate_cache()
        after = metrics.unmapped(MONTH)
        check(sample in {i["key"] for i in after["items"]},
              f"отвязанный ключ «{sample}» появился в несопоставленном")

        suggestion = next((i.get("suggestion") for i in after["items"] if i["key"] == sample), None)
        check(suggestion is not None,
              f"для «{sample}» есть подсказка сопоставления", str(suggestion))

        kpi_storage.set_link(kpi_storage.SOURCE_CRM, sample, row["store_id"])
        metrics.invalidate_cache()
        restored = kpi_storage.resolve_map(kpi_storage.SOURCE_CRM)
        check(restored.get(sample) == row["store_id"], "связь восстановлена после проверки")

    # ------------------------------------------------------------------
    section("6. Сравнение с прошлым месяцем — по ту же дату")

    prev_from, prev_to = metrics.comparable_bounds(MONTH)
    check(prev_to[8:10] == metrics.month_bounds(MONTH)[1][8:10] or MONTH != metrics.current_month(),
          "период сравнения обрезан по текущее число",
          f"{prev_from} — {prev_to}")

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"ПРОВАЛЕНО {len(FAILURES)} из {CHECKS[0]} проверок:")
        for title in FAILURES:
            print("  -", title)
        return 1
    print(f"Все {CHECKS[0]} проверок пройдены.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
