"""
Офлайн-тесты постраничного списка заявок на списание — без сети.

19.09.2026 таблица заявок отдавалась одним куском (limit=200 по умолчанию):
на каждую строку — коррелированный подзапрос за числом позиций, а человек
всё равно смотрит первые строки. Стало: 25 строк на страницу + total под
фильтром, по которому фронт рисует номера.

Проверяет:
  1. Страница отдаёт ровно limit строк и total по ВСЕМУ отбору
  2. Страницы не пересекаются и вместе дают весь набор в том же порядке
  3. total считается по тем же фильтрам, что и строки (точка, статус, даты)
  4. Пустой список точек — 0 строк и total 0, без похода в базу за всем
  5. Цена: страница + total — ОДНО соединение (медленный /data)
  6. HTTP: ручка отдаёт total/limit/offset, зажимает limit и отрицательный offset
  7. HTTP: страница не видит чужие точки — ни в строках, ни в total

Запуск: python scripts/test_writeoffs_pagination.py
"""

import io
import os
import socket
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone

if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')


# Сеть отключаем ДО импорта приложения: локальный прогон читает боевой .env
# (load_dotenv при импорте) и иначе может уйти в боевые МойСклад/RetailCRM.
class NoNetwork(socket.socket):
    def connect(self, *a, **k):
        raise OSError("сеть отключена намеренно")

    def connect_ex(self, *a, **k):
        raise OSError("сеть отключена намеренно")


socket.socket = NoNetwork

_tmp_dir = tempfile.mkdtemp()
os.environ["BARHAT_DB_PATH"] = os.path.join(_tmp_dir, "test_writeoffs_pagination.db")
os.environ["WRITEOFF_ATTACHMENTS_DIR"] = os.path.join(_tmp_dir, "attachments")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from writeoffs.storage import (  # noqa: E402
    get_db,
    init_writeoffs_tables,
    list_writeoffs_page,
)

failures = []


def check(name, condition, detail=""):
    mark = "OK  " if condition else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(name)


init_writeoffs_tables()

# 60 заявок: 40 на точке 1, 20 на точке 2; каждая пятая — отклонённая.
# Даты идут по одной в день, чтобы порядок был однозначным и проверяемым.
TOTAL = 60
conn = get_db()
try:
    for i in range(TOTAL):
        store_id = 1 if i < 40 else 2
        status = "rejected" if i % 5 == 0 else "on_approval"
        created = f"2026-07-{(i % 28) + 1:02d}T{(i % 24):02d}:00:00"
        conn.execute(
            """INSERT INTO writeoffs (id, store_id, status, created_by, created_at)
               VALUES (?, ?, ?, 'florist', ?)""",
            (i + 1, store_id, status, created),
        )
        conn.execute(
            """INSERT INTO writeoff_positions
               (writeoff_id, moysklad_product_id, moysklad_product_href, product_name, quantity, uom_name)
               VALUES (?, 'p', 'h', 'Роза', 1, 'шт')""",
            (i + 1,),
        )
    conn.commit()
finally:
    conn.close()


print("\n=== 1. Страница отдаёт срез, total — весь отбор ===")
first = list_writeoffs_page(limit=25, offset=0)
check("Строк на странице — 25", len(first["items"]) == 25, f"{len(first['items'])}")
check(f"total = {TOTAL}", first["total"] == TOTAL, f"{first['total']}")
check("Число позиций в строке есть", first["items"][0].get("positions_count") == 1)

last = list_writeoffs_page(limit=25, offset=50)
check("Последняя страница — остаток 10 строк", len(last["items"]) == 10, f"{len(last['items'])}")
check("total на последней странице тот же", last["total"] == TOTAL, f"{last['total']}")

beyond = list_writeoffs_page(limit=25, offset=200)
check("За последней страницей — пусто, но total известен",
      beyond["items"] == [] and beyond["total"] == TOTAL)


print("\n=== 2. Страницы не пересекаются и покрывают весь набор ===")
paged = []
for offset in range(0, TOTAL, 25):
    paged.extend(w["id"] for w in list_writeoffs_page(limit=25, offset=offset)["items"])

whole = [w["id"] for w in list_writeoffs_page(limit=1000, offset=0)["items"]]
check("Дублей между страницами нет", len(paged) == len(set(paged)), f"{len(paged)} строк")
check("Собранные страницы = весь список в том же порядке", paged == whole)
check("Ничего не потеряно", len(paged) == TOTAL, f"{len(paged)}")


print("\n=== 3. total считается по тем же фильтрам, что и строки ===")
store1 = list_writeoffs_page(store_ids=[1], limit=25, offset=0)
check("Точка 1: total = 40", store1["total"] == 40, f"{store1['total']}")
check("Точка 1: чужих строк нет", all(w["store_id"] == 1 for w in store1["items"]))

rejected = list_writeoffs_page(status="rejected", limit=25, offset=0)
check("Статус rejected: total = 12", rejected["total"] == 12, f"{rejected['total']}")
check("Статус rejected: строк 12", len(rejected["items"]) == 12, f"{len(rejected['items'])}")

# date_to голой датой должен захватывать весь день (см. комментарий в storage)
day = list_writeoffs_page(date_from="2026-07-05", date_to="2026-07-05", limit=25, offset=0)
same_day = len([1 for i in range(TOTAL) if (i % 28) + 1 == 5])
check(f"Один день: total = {same_day}", day["total"] == same_day, f"{day['total']}")
check("Один день: строк столько же", len(day["items"]) == same_day)

combo = list_writeoffs_page(store_ids=[2], status="rejected", limit=5, offset=0)
combo_all = list_writeoffs_page(store_ids=[2], status="rejected", limit=1000, offset=0)
check("Точка + статус: total не зависит от limit",
      combo["total"] == combo_all["total"] == len(combo_all["items"]),
      f"{combo['total']} / {len(combo_all['items'])}")


print("\n=== 4. Пустой список точек — гарантированно пустой результат ===")
none_stores = list_writeoffs_page(store_ids=[], limit=25, offset=0)
check("Нет доступных точек: строк 0", none_stores["items"] == [])
check("Нет доступных точек: total 0", none_stores["total"] == 0, f"{none_stores['total']}")


print("\n=== 4а. Порядок однозначен: одна секунда на несколько заявок ===")
# created_at пишется с точностью до секунды. Без тай-брейкера по id заявки с
# одинаковым временем раскладываются по страницам как попало: одна попадает на
# обе страницы, другая не показывается вовсе.
SAME_SECOND_COUNT = 6
TOTAL_ALL = TOTAL + SAME_SECOND_COUNT   # дальше по тексту этих заявок уже 66

conn = get_db()
try:
    for i in range(SAME_SECOND_COUNT):
        conn.execute(
            """INSERT INTO writeoffs (id, store_id, status, created_by, created_at)
               VALUES (?, 3, 'on_approval', 'florist', '2026-08-01T12:00:00')""",
            (900 + i,),
        )
    conn.commit()
finally:
    conn.close()

# Проверяем АДМИНСКИЙ путь (без store_ids): у запроса по точке порядок внутри
# секунды удерживает составной индекс, а здесь в дело идёт idx_writeoffs_created,
# и без явного тай-брейкера SQLite вправе отдать строки как угодно.
same_second = []
for offset in (0, 2, 4):
    same_second.extend(
        w["id"] for w in list_writeoffs_page(
            date_from="2026-08-01", date_to="2026-08-01", limit=2, offset=offset
        )["items"]
    )
check("Заявки одной секунды не дублируются между страницами",
      len(same_second) == len(set(same_second)), f"{same_second}")
check("И ни одна не потеряна", sorted(same_second) == list(range(900, 906)), f"{same_second}")
check("Порядок устойчив между прогонами",
      same_second == [w["id"] for w in list_writeoffs_page(
          date_from="2026-08-01", date_to="2026-08-01", limit=10)["items"]],
      f"{same_second}")
# Порядок задан явно (id DESC внутри секунды), а не «как лягут строки»: без
# тай-брейкера он зависит от выбранного плана и меняется вместе с индексами
check("Внутри одной секунды порядок задан явно — новые сверху",
      same_second == sorted(same_second, reverse=True), f"{same_second}")


print("\n=== 4б. План запроса: страница точки идёт по составному индексу ===")
# Страница у не-админа — это «точка + свежие сверху». Одиночные индексы не
# складываются: без составного SQLite берёт store_id и досортировывает всю
# точку через TEMP B-TREE, и это платится на КАЖДЫЙ клик по странице
# (CLAUDE.md: проверять планом, а не временем — на тестовой базе разницы не видно).
conn = get_db()
try:
    conn.execute("ANALYZE")
    # Читаем именно detail: str(sqlite3.Row) печатает адрес объекта, и проверка
    # «нет TEMP B-TREE» проходила бы всегда — сторож, смотрящий не в то поле,
    # хуже отсутствующего.
    plan = "\n".join(
        r["detail"] for r in conn.execute(
            """EXPLAIN QUERY PLAN
               SELECT w.*,
                      (SELECT COUNT(*) FROM writeoff_positions p WHERE p.writeoff_id = w.id) AS positions_count
               FROM writeoffs w
               WHERE 1=1 AND store_id IN (?)
               ORDER BY created_at DESC, id DESC
               LIMIT ? OFFSET ?""",
            (1, 25, 0),
        ).fetchall()
    )
finally:
    conn.close()

check("Сортировка не через временное B-дерево", "TEMP B-TREE" not in plan.upper(), plan)
check("Использован индекс (store_id, created_at, id)",
      "idx_writeoffs_store_created" in plan, plan)


print("\n=== 5. Цена: страница и total — одно соединение ===")
import sqlite_conn as sqlite_conn_module  # noqa: E402

connects = []
_real_sqlite_connect = sqlite3.connect


def counting_connect(*args, **kwargs):
    connects.append(args[0] if args else kwargs.get("database"))
    return _real_sqlite_connect(*args, **kwargs)


sqlite3.connect = counting_connect
sqlite_conn_module.sqlite3.connect = counting_connect
try:
    connects.clear()
    list_writeoffs_page(store_ids=[1], limit=25, offset=0)
    page_connects = len(connects)
finally:
    sqlite3.connect = _real_sqlite_connect
    sqlite_conn_module.sqlite3.connect = _real_sqlite_connect

# Отдельная count_writeoffs() открыла бы вторую базу на каждый показ таблицы,
# а обращение к /data стоит 90–700 мс независимо от размера ответа.
check(f"Соединений на страницу: {page_connects} (потолок 1)", page_connects <= 1)


print("\n=== 6-7. HTTP: total в ответе, границы limit/offset, чужие точки ===")
from flask import Flask  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

from auth import auth_bp, init_auth_tables, login_manager  # noqa: E402
from cashshifts.storage import init_cashshifts_tables, set_user_stores  # noqa: E402
from writeoffs import server as writeoff_server  # noqa: E402


def add_user(username, role):
    conn = get_db()
    try:
        conn.execute(
            """INSERT INTO users (username, full_name, password_hash, role, is_active, created_at)
               VALUES (?, ?, ?, ?, 1, ?)""",
            (username, username, generate_password_hash("secret"), role,
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


app = Flask(__name__)
app.secret_key = "test-secret"
login_manager.init_app(app)
login_manager.login_view = None
app.register_blueprint(auth_bp)
app.register_blueprint(writeoff_server.writeoffs_bp)
with app.app_context():
    init_auth_tables()
init_cashshifts_tables()  # user_stores — доступ к точкам живёт там

add_user("admin_wo", "admin")
add_user("florist_wo", "florist")
# Салон обязателен: без него ветка видимости не исполняется (см. память о
# тестовом менеджере без салонов) — а проверяем мы именно её
set_user_stores("florist_wo", [2])

admin = app.test_client()
florist = app.test_client()
admin.post("/api/auth/login", json={"username": "admin_wo", "password": "secret"})
florist.post("/api/auth/login", json={"username": "florist_wo", "password": "secret"})

res = admin.get("/api/writeoffs?limit=25&offset=0")
data = res.get_json()
check("Ручка отвечает 200", res.status_code == 200, f"код {res.status_code}")
check("В ответе есть total", data.get("total") == TOTAL_ALL, f"{data.get('total')}")
check("В ответе есть limit/offset", data.get("limit") == 25 and data.get("offset") == 0,
      f"{data.get('limit')} / {data.get('offset')}")
check("Строк на странице 25", len(data.get("writeoffs") or []) == 25)
check("count — это длина страницы, а не всего отбора", data.get("count") == 25)

second = admin.get("/api/writeoffs?limit=25&offset=25").get_json()
ids_first = {w["id"] for w in data["writeoffs"]}
ids_second = {w["id"] for w in second["writeoffs"]}
check("Вторая страница не повторяет первую", not (ids_first & ids_second))

default = admin.get("/api/writeoffs").get_json()
check("Без limit ручка отдаёт страницу, а не всю витрину",
      default["limit"] == writeoff_server.PAGE_LIMIT_DEFAULT
      and len(default["writeoffs"]) == writeoff_server.PAGE_LIMIT_DEFAULT,
      f"{default['limit']} / {len(default['writeoffs'])}")

huge = admin.get("/api/writeoffs?limit=100000").get_json()
check(f"limit зажат сверху до {writeoff_server.PAGE_LIMIT_MAX}",
      huge["limit"] == writeoff_server.PAGE_LIMIT_MAX, f"{huge['limit']}")

zero = admin.get("/api/writeoffs?limit=0").get_json()
check("limit=0 не обнуляет выдачу", zero["limit"] >= 1 and len(zero["writeoffs"]) >= 1,
      f"{zero['limit']}")

negative = admin.get("/api/writeoffs?offset=-5")
check("Отрицательный offset не роняет ручку (SQLite от него падает)",
      negative.status_code == 200, f"код {negative.status_code}")
check("Отрицательный offset выправлен в 0", negative.get_json()["offset"] == 0)

garbage = admin.get("/api/writeoffs?limit=abc&offset=abc")
check("Мусор в limit/offset не роняет ручку", garbage.status_code == 200,
      f"код {garbage.status_code}")

mine = florist.get("/api/writeoffs?limit=25&offset=0").get_json()
check("Флорист видит total только своей точки", mine["total"] == 20, f"{mine['total']}")
check("Флорист не видит чужих строк",
      all(w["store_id"] == 2 for w in mine["writeoffs"]))

forbidden = florist.get("/api/writeoffs?store_id=1")
check("Чужая точка в фильтре — 403", forbidden.status_code == 403,
      f"код {forbidden.status_code}")


print("\n" + "=" * 60)
if failures:
    print(f"ПРОВАЛЕНО проверок: {len(failures)}")
    for name in failures:
        print(f"  - {name}")
    sys.exit(1)
print("Все проверки прошли.")
