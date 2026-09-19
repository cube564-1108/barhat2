"""
Офлайн-тесты постраничных таблиц модуля кассовых смен — без сети и прод-базы.

19.09.2026 «История смен» тянула 50 строк одним куском, «Инкассации по
салонам» — 200 с подсказкой «сузьте период, чтобы увидеть остальные».
Стало: 25 строк на страницу + число строк под фильтром, по которому
рисуются номера.

Проверяет:
  1. История смен: страница, total под фильтром, устойчивый порядок
  2. История смен: страницы не пересекаются и покрывают весь набор
  3. Инкассации: страница + итоги по салонам за ВЕСЬ период (не по странице)
  4. Инкассации: число строк (total_count) и деньги (total) — разные поля
  5. Цена: страница + счётчик — одно соединение на таблицу
  6. Планы запросов: обе страницы идут по индексу, без TEMP B-TREE
  7. HTTP: границы limit/offset, чужие точки не видны ни в строках, ни в счёте

Запуск: python scripts/test_cashshifts_pagination.py
"""

import io
import json
import os
import socket
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta

if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')


# Сеть отключаем ДО импорта приложения: локальный прогон читает боевой .env
# и иначе может уйти в боевой RetailCRM.
class NoNetwork(socket.socket):
    def connect(self, *a, **k):
        raise OSError("сеть отключена намеренно")

    def connect_ex(self, *a, **k):
        raise OSError("сеть отключена намеренно")


socket.socket = NoNetwork

os.environ["BARHAT_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test_cashshifts_pagination.db")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from flask import Flask  # noqa: E402

from cashshifts.storage import (  # noqa: E402
    create_cash_shift,
    create_collection,
    get_all_categories,
    get_all_stores,
    get_db,
    init_cashshifts_tables,
    list_cash_shifts_page,
    list_collections_page,
    set_user_stores,
)
from cashshifts import server  # noqa: E402

failures = []


def check(name, condition, detail=""):
    mark = "OK  " if condition else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(name)


app = Flask(__name__)
# login_required пропускает запрос: аутентификацию здесь не тестируем,
# роль подменяется напрямую (как в test_collections_report.py)
app.config["LOGIN_DISABLED"] = True


def patch_user(role, username="tester"):
    server.get_current_user_role = lambda: role
    server.get_current_username = lambda: username


def call(path, query=""):
    """Вызвать view напрямую и вернуть (данные, http-код)."""
    view = server.list_shifts if path == "shifts" else server.list_all_collections
    url = "/api/cash-shifts" + ("" if path == "shifts" else "/collections")
    with app.test_request_context(f"{url}?{query}"):
        result = view()
        if isinstance(result, tuple):
            response, status = result
        else:
            response, status = result, 200
        return json.loads(response.get_data(as_text=True)), status


# ============================================================================
# Подготовка данных
# ============================================================================
init_cashshifts_tables()

stores = get_all_stores()
categories = get_all_categories()
store_a, store_b = stores[0], stores[1]
category = categories[0]

SHIFTS_A = 60          # закрытых смен на точке A
SHIFTS_B = 10          # и на точке B
COLLECTIONS_PER_SHIFT = 1

base = datetime(2026, 7, 1, 8, 0, 0)
shift_ids_a = []

conn = get_db()
try:
    for i in range(SHIFTS_A + SHIFTS_B):
        store = store_a if i < SHIFTS_A else store_b
        started = base + timedelta(hours=i)
        cur = conn.execute(
            """INSERT INTO cash_shifts
               (store_id, shift_type, status, datetime_start, closed_at,
                opening_balance, florist_username)
               VALUES (?, 'day', 'closed', ?, ?, 1000.0, 'florist_a')""",
            (store["id"], started.strftime("%Y-%m-%d %H:%M:%S"),
             (started + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")),
        )
        if i < SHIFTS_A:
            shift_ids_a.append(cur.lastrowid)
        conn.execute(
            """INSERT INTO cash_collections (shift_id, date, amount, expense_category_id, created_by)
               VALUES (?, ?, ?, ?, 'florist_a')""",
            (cur.lastrowid, started.strftime("%Y-%m-%d %H:%M:%S"), 100.0 + i, category["id"]),
        )
    conn.commit()
finally:
    conn.close()

TOTAL_SHIFTS = SHIFTS_A + SHIFTS_B
TOTAL_COLLECTIONS = TOTAL_SHIFTS * COLLECTIONS_PER_SHIFT


print("\n=== 1. История смен: страница и total ===")
first = list_cash_shifts_page(status="closed", limit=25, offset=0)
check("Строк на странице — 25", len(first["items"]) == 25, f"{len(first['items'])}")
check(f"total = {TOTAL_SHIFTS}", first["total"] == TOTAL_SHIFTS, f"{first['total']}")

by_store = list_cash_shifts_page(store_id=store_a["id"], status="closed", limit=25)
check(f"Точка A: total = {SHIFTS_A}", by_store["total"] == SHIFTS_A, f"{by_store['total']}")
check("Точка A: чужих строк нет",
      all(s["store_id"] == store_a["id"] for s in by_store["items"]))

beyond = list_cash_shifts_page(status="closed", limit=25, offset=500)
check("За последней страницей пусто, но total известен",
      beyond["items"] == [] and beyond["total"] == TOTAL_SHIFTS)


print("\n=== 2. История смен: страницы не пересекаются и покрывают набор ===")
paged = []
for offset in range(0, TOTAL_SHIFTS, 25):
    paged.extend(s["id"] for s in list_cash_shifts_page(status="closed", limit=25, offset=offset)["items"])
whole = [s["id"] for s in list_cash_shifts_page(status="closed", limit=1000)["items"]]
check("Дублей между страницами нет", len(paged) == len(set(paged)), f"{len(paged)} строк")
check("Страницы = весь список в том же порядке", paged == whole)
check("Ничего не потеряно", len(paged) == TOTAL_SHIFTS, f"{len(paged)}")

# Тай-брейкер: datetime_start с точностью до секунды, смены одной секунды
# без ", id DESC" расползаются по страницам как попало
conn = get_db()
try:
    for i in range(6):
        conn.execute(
            """INSERT INTO cash_shifts
               (id, store_id, shift_type, status, datetime_start, opening_balance, florist_username)
               VALUES (?, ?, 'day', 'closed', '2026-09-01 10:00:00', 0, 'florist_a')""",
            (900 + i, store_b["id"]),
        )
    conn.commit()
finally:
    conn.close()

def collect_same_second():
    out = []
    for off in (0, 2, 4):
        out.extend(s["id"] for s in list_cash_shifts_page(
            date_from="2026-09-01 00:00:00", date_to="2026-09-01 23:59:59",
            limit=2, offset=off)["items"])
    return out


same_second = collect_same_second()
check("Смены одной секунды не дублируются между страницами",
      len(same_second) == len(set(same_second)), f"{same_second}")
check("И ни одна не потеряна", sorted(same_second) == list(range(900, 906)), f"{same_second}")

# Порядок обязан держаться ПЛАНОМ-НЕЗАВИСИМО. Сравнить его с «новые сверху»
# мало: при удачном индексе SQLite и без тай-брейкера отдаёт строки в том же
# порядке, и проверка зеленеет на сломанном коде. Поэтому повторяем сбор,
# сняв индексы — план меняется на полный скан с сортировкой во временном
# B-дереве, и порядок расходится ровно тогда, когда тай-брейкера нет.
conn = get_db()
try:
    for idx in ("idx_shifts_store_datetime_id", "idx_shifts_store_datetime",
                "idx_shifts_datetime", "idx_shifts_store"):
        conn.execute(f"DROP INDEX IF EXISTS {idx}")
    conn.commit()
finally:
    conn.close()

same_second_scan = collect_same_second()
init_cashshifts_tables()   # индексы обратно: их проверяет раздел 6

check("Порядок не зависит от плана запроса",
      same_second == same_second_scan, f"{same_second} против {same_second_scan}")
check("Порядок задан явно — новые сверху",
      same_second == sorted(same_second, reverse=True), f"{same_second}")


print("\n=== 3. Инкассации: страница и итоги за весь период ===")
coll_first = list_collections_page(limit=25, offset=0)
check("Строк на странице — 25", len(coll_first["items"]) == 25, f"{len(coll_first['items'])}")
check(f"Число инкассаций под фильтром = {TOTAL_COLLECTIONS}",
      coll_first["total"] == TOTAL_COLLECTIONS, f"{coll_first['total']}")

# Итоги по салонам не должны зависеть от того, какую страницу смотрим
page3 = list_collections_page(limit=25, offset=50)
check("Итоги по салонам одинаковы на первой и третьей странице",
      coll_first["by_store"] == page3["by_store"])
check("Сумма денег не зависит от страницы",
      abs(coll_first["total_amount"] - page3["total_amount"]) < 0.001,
      f"{coll_first['total_amount']} / {page3['total_amount']}")
check("Сумма денег больше суммы показанной страницы",
      coll_first["total_amount"] > sum(c["amount"] for c in coll_first["items"]))

coll_paged = []
for offset in range(0, TOTAL_COLLECTIONS, 25):
    coll_paged.extend(c["id"] for c in list_collections_page(limit=25, offset=offset)["items"])
check("Инкассации: дублей между страницами нет",
      len(coll_paged) == len(set(coll_paged)), f"{len(coll_paged)} строк")
check("Инкассации: ничего не потеряно", len(coll_paged) == TOTAL_COLLECTIONS, f"{len(coll_paged)}")

empty = list_collections_page(store_ids=[], limit=25)
check("Нет доступных точек — пусто и по строкам, и по счётчику",
      empty["items"] == [] and empty["total"] == 0)


print("\n=== 5. Цена: страница + счётчик — одно соединение на таблицу ===")
import sqlite_conn as sqlite_conn_module  # noqa: E402

connects = []
_real_connect = sqlite3.connect


def counting_connect(*args, **kwargs):
    connects.append(args[0] if args else kwargs.get("database"))
    return _real_connect(*args, **kwargs)


sqlite3.connect = counting_connect
sqlite_conn_module.sqlite3.connect = counting_connect
try:
    connects.clear()
    list_cash_shifts_page(store_id=store_a["id"], status="closed", limit=25)
    shifts_connects = len(connects)

    connects.clear()
    list_collections_page(limit=25)
    collections_connects = len(connects)
finally:
    sqlite3.connect = _real_connect
    sqlite_conn_module.sqlite3.connect = _real_connect

check(f"История смен: {shifts_connects} соединение (потолок 1)", shifts_connects <= 1)
# Раньше строки и итоги по салонам считались двумя вызовами, то есть двумя
# походами на /data по 90-700 мс каждый
check(f"Инкассации: {collections_connects} соединение (потолок 1)", collections_connects <= 1)


print("\n=== 6. Планы запросов: без временного B-дерева ===")
conn = get_db()
try:
    conn.execute("ANALYZE")
    shifts_plan = "\n".join(r["detail"] for r in conn.execute(
        """EXPLAIN QUERY PLAN
           SELECT * FROM cash_shifts
           WHERE 1=1 AND store_id = ? AND status = ?
           ORDER BY datetime_start DESC, id DESC
           LIMIT ? OFFSET ?""",
        (store_a["id"], "closed", 25, 0),
    ).fetchall())

    collections_plan = "\n".join(r["detail"] for r in conn.execute(
        """EXPLAIN QUERY PLAN
           SELECT cc.id, cc.date, cc.amount, cs.store_id
           FROM cash_collections cc
           JOIN cash_shifts cs ON cs.id = cc.shift_id
           WHERE 1=1 AND cc.date >= ? AND cc.date <= ?
           ORDER BY cc.date DESC, cc.id DESC
           LIMIT ? OFFSET ?""",
        ("2026-07-01 00:00:00", "2026-07-31 23:59:59", 25, 0),
    ).fetchall())
finally:
    conn.close()

check("История смен идёт по составному индексу (точка + дата + id)",
      "idx_shifts_store_datetime_id" in shifts_plan, shifts_plan)
check("История смен: сортировка не через TEMP B-TREE",
      "TEMP B-TREE" not in shifts_plan.upper(), shifts_plan)
check("Инкассации идут по индексу даты",
      "idx_collections_date" in collections_plan, collections_plan)
check("Инкассации: сортировка не через TEMP B-TREE",
      "TEMP B-TREE" not in collections_plan.upper(), collections_plan)


print("\n=== 7. HTTP: границы limit/offset и права по точкам ===")
patch_user("admin", "admin_cs")

data, status = call("shifts", "status=closed&limit=25&offset=0")
check("История смен: 200", status == 200, f"код {status}")
check("В ответе есть total", data.get("total") == TOTAL_SHIFTS + 6, f"{data.get('total')}")
check("Строк на странице 25", len(data.get("shifts") or []) == 25)
check("count — длина страницы", data.get("count") == 25)
check("Название точки подставлено",
      bool((data["shifts"][0] or {}).get("store_name")) and data["shifts"][0]["store_name"] != "Unknown",
      str((data["shifts"][0] or {}).get("store_name")))

default_page, _ = call("shifts", "status=closed")
check("Без limit — страница, а не вся история",
      len(default_page["shifts"]) == server.PAGE_LIMIT_DEFAULT, f"{len(default_page['shifts'])}")

huge, _ = call("shifts", "status=closed&limit=100000")
# Сверяем сам зажатый limit, а не длину ответа: строк в тесте меньше потолка,
# и проверка «строк <= 200» прошла бы и без клампа
check(f"limit зажат до {server.PAGE_LIMIT_MAX}",
      huge["params"]["limit"] == server.PAGE_LIMIT_MAX, f"{huge['params']['limit']}")

zero, _ = call("shifts", "status=closed&limit=0")
check("limit=0 не обнуляет выдачу",
      zero["params"]["limit"] >= 1 and len(zero["shifts"]) >= 1, f"{zero['params']['limit']}")


neg, neg_status = call("shifts", "status=closed&offset=-5")
check("Отрицательный offset не роняет ручку", neg_status == 200, f"код {neg_status}")
check("Отрицательный offset выправлен в 0", neg["params"]["offset"] == 0,
      f"{neg['params']['offset']}")

garbage, garbage_status = call("shifts", "status=closed&limit=abc&offset=abc")
check("Мусор в limit/offset не роняет ручку", garbage_status == 200, f"код {garbage_status}")

coll, coll_status = call("collections", "limit=25&offset=0")
check("Инкассации: 200", coll_status == 200, f"код {coll_status}")
check("total_count — число строк",
      coll.get("total_count") == TOTAL_COLLECTIONS, f"{coll.get('total_count')}")
check("total — деньги, а не строки",
      coll.get("total") > TOTAL_COLLECTIONS, f"{coll.get('total')}")
check("offset возвращается фронтенду", coll.get("offset") == 0, f"{coll.get('offset')}")

coll_neg, coll_neg_status = call("collections", "offset=-10")
check("Инкассации: отрицательный offset выправлен",
      coll_neg_status == 200 and coll_neg["offset"] == 0, f"{coll_neg.get('offset')}")

# Права: у флориста только точка B
set_user_stores("florist_cs", [store_b["id"]])
patch_user("florist", "florist_cs")

mine, mine_status = call("collections", "limit=25")
check("Флорист: 200", mine_status == 200, f"код {mine_status}")
check("Флорист видит счётчик только своей точки",
      mine["total_count"] == SHIFTS_B, f"{mine['total_count']}")
check("Флорист не видит чужих строк",
      all(c["store_id"] == store_b["id"] for c in mine["collections"]))

forbidden, forbidden_status = call("collections", f"store_id={store_a['id']}")
check("Чужая точка в фильтре — 403", forbidden_status == 403, f"код {forbidden_status}")

shifts_mine, shifts_status = call("shifts", f"status=closed&store_id={store_b['id']}")
check("Флорист: своя точка в истории — 200", shifts_status == 200, f"код {shifts_status}")
check("И счётчик только по ней",
      shifts_mine["total"] == SHIFTS_B + 6, f"{shifts_mine['total']}")

shifts_forbidden, shifts_forbidden_status = call("shifts", f"status=closed&store_id={store_a['id']}")
check("Чужая точка в истории — 403", shifts_forbidden_status == 403,
      f"код {shifts_forbidden_status}")


print("\n=== 8. Цена ручки: имена точек одним запросом, а не на строку ===")
# Раньше история смен звала get_store_by_id на КАЖДУЮ строку, то есть
# открывала соединение на строку: 25 походов на /data по 90–700 мс.
patch_user("admin", "admin_cs")

sqlite3.connect = counting_connect
sqlite_conn_module.sqlite3.connect = counting_connect
try:
    connects.clear()
    call("shifts", "status=closed&limit=25&offset=0")
    endpoint_connects = len(connects)
finally:
    sqlite3.connect = _real_connect
    sqlite_conn_module.sqlite3.connect = _real_connect

# Потолок 3 — страница со счётчиком, имена точек и запас на проверку доступов.
# Двадцать пять строк в ответе не должны стоить двадцати пяти соединений
check(f"Ручка истории смен открыла базу {endpoint_connects} раз(а) (потолок 3)",
      endpoint_connects <= 3)


print("\n" + "=" * 60)
if failures:
    print(f"ПРОВАЛЕНО проверок: {len(failures)}")
    for name in failures:
        print(f"  - {name}")
    sys.exit(1)
print("Все проверки прошли.")
