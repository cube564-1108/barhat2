"""
Сторож цены диагностики: сколько стоит один запрос к /health.

Зачем отдельным прогоном. Замер прода 2026-09-08: `/health` отвечал 3,4–13,5
секунды при 2–3 мс на вход в дашборд. Одного тяжёлого запроса там не было —
была ручка, которая за раз обходила четыре базы на общем сетевом `/data`
(`barhat.db`, `couriers.db`, `pyrus.db` 196 МБ, `moysklad.db` 1,13 ГБ) и
считала счётчики по всей истории. Воркеров на проде два: два одновременных
таких вызова не оставляют сайту ни одного обработчика.

Тесты этого не ловят сами по себе — в них базы маленькие и локальные. Ловится
только тем, что здесь и проверяется: **сколько раз ручка открывает SQLite** и
**считает ли она по окну**. И то и другое тихо отрастает обратно: лишний
снимок в диагностике выглядит безобидно и стоит сотни миллисекунд на проде.

Проверяется:
1. дешёвый `/health` открывает базу считанное число раз;
2. дешёвый `/health` не тянет за собой витрины и инварианты схемы;
3. `/health?full=1` отдаёт их и замер по каждому блоку;
4. снимки витрин считаются по окну, а не по всей таблице;
5. границы дат берутся MIN/MAX по индексу, а не сканом.

Запуск: python scripts/test_health_cost.py
"""

import os
import socket
import sqlite3
import ssl  # noqa: F401  — импортировать до патча сокета
import sys
import tempfile
import threading

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))


class NetworkBlocked(Exception):
    pass


def _blocked(*args, **kwargs):
    raise NetworkBlocked("сторож не должен ходить в боевые внешние API")


socket.socket.connect = _blocked

WORK_DIR = tempfile.mkdtemp(prefix="health_cost_")
os.environ["BARHAT_DB_PATH"] = os.path.join(WORK_DIR, "barhat.db")
os.environ["COURIERS_DB_PATH"] = os.path.join(WORK_DIR, "couriers.db")
os.environ["PYRUS_DB_PATH"] = os.path.join(WORK_DIR, "pyrus.db")
os.environ["MOYSKLAD_DB_PATH"] = os.path.join(WORK_DIR, "moysklad.db")
os.environ["DISABLE_SCHEDULERS"] = "1"

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"  [ok] {name}")
    else:
        print(f"  [FAIL] {name} {detail}")
        failures.append(name)


# --- счётчик соединений ------------------------------------------------------
# Считаем только соединения из потока, который обрабатывает запрос: писатель
# аудита живёт в своём потоке и вынесен из горячего пути намеренно.

_real_connect = sqlite3.connect
_main_thread = threading.get_ident()
connect_calls = []


def counting_connect(*args, **kwargs):
    if threading.get_ident() == _main_thread:
        connect_calls.append(args[0] if args else kwargs.get("database"))
    return _real_connect(*args, **kwargs)


sqlite3.connect = counting_connect

print(f"\nвременные базы: {WORK_DIR}")

import auth  # noqa: E402
import sqlite_conn  # noqa: E402
from pyrus.server import app  # noqa: E402

sqlite_conn.sqlite3.connect = counting_connect

app.config["TESTING"] = True

with app.app_context():
    auth.init_auth_tables()

# Витрину движения товара заводит не storage, а сам модуль. Без неё снимок
# уходит по короткому пути «таблицы нет», и проверка окна становится пустой.
from moysklad import warehouse as _warehouse  # noqa: E402
from moysklad.server import get_db as _get_ms_db  # noqa: E402

_warehouse.init_warehouse_tables(_get_ms_db())

client = app.test_client()


# ============================================================================
print("\n1. Дешёвый /health: сколько раз открывается база")
# ============================================================================

connect_calls.clear()
response = client.get("/health")
cheap_connects = len(connect_calls)
cheap = response.get_json()

check("GET /health -> 200", response.status_code == 200, f"({response.status_code})")
# Потолок 2 = проба записи в barhat.db + чтение состояния квоты ПланФакта.
# Всё остальное (пять снимков витрин, инвариант схемы) ушло за ?full=1.
check(f"дешёвый вызов открыл базу {cheap_connects} раз(а) (потолок 2)",
      cheap_connects <= 2, f"открыто: {connect_calls}")


# ============================================================================
print("\n2. Дешёвый /health не тянет витрины и схему")
# ============================================================================

check("нет блока pipelines", "pipelines" not in cheap)
check("нет блока guarantees", "guarantees" not in cheap)
check("есть подсказка про ?full=1", "full=1" in (cheap.get("hint") or ""))
check("есть замер по блокам", isinstance(cheap.get("timings_ms"), dict) and cheap["timings_ms"])
check("есть суммарное время", isinstance(cheap.get("total_ms"), (int, float)),
      f"({cheap.get('total_ms')!r})")
check("состояние баз на месте", isinstance(cheap.get("databases"), dict) and cheap["databases"])
check("проба записи на месте", isinstance(cheap.get("write_test"), dict))
# Число файлов во вложениях — перечисление каталога целиком, это диагностика
check("число файлов вложений не считается",
      all("files" not in v for v in cheap.get("attachments", {}).values()),
      f"({cheap.get('attachments')})")


# ============================================================================
print("\n3. /health?full=1 отдаёт диагностику и её цену")
# ============================================================================

connect_calls.clear()
response = client.get("/health?full=1")
full_connects = len(connect_calls)
data = response.get_json()

check("GET /health?full=1 -> 200", response.status_code == 200, f"({response.status_code})")
check("есть pipelines", isinstance(data.get("pipelines"), dict))
check("есть guarantees", isinstance(data.get("guarantees"), dict))
check("полный вызов дороже дешёвого по числу соединений",
      full_connects > cheap_connects, f"({full_connects} против {cheap_connects})")

pipelines = data.get("pipelines") or {}
check("у pipelines есть замер по блокам",
      isinstance(pipelines.get("timings_ms"), dict) and len(pipelines["timings_ms"]) == 5,
      f"({pipelines.get('timings_ms')})")
check("окно диагностики отдано в ответе",
      isinstance(pipelines.get("window_days"), int), f"({pipelines.get('window_days')})")


# ============================================================================
print("\n4. Снимки витрин считаются по окну, а не по всей таблице")
# ============================================================================

for name in ("nos", "warehouse", "quality"):
    snapshot = pipelines.get(name) or {}
    if "error" in snapshot:
        check(f"{name}: снимок собрался", False, f"({snapshot['error']})")
        continue
    check(f"{name}: окно указано в снимке",
          "window_from" in snapshot or "window_days" in snapshot, f"({sorted(snapshot)})")
    # Счётчик по всей таблице обязан называться иначе, чем счётчик по окну:
    # ключ `rows` без уточнения — ровно та цифра, которую считали сканом
    check(f"{name}: нет счётчика по всей таблице",
          "rows" not in snapshot, f"({sorted(snapshot)})")


# ============================================================================
print("\n5. Границы дат берутся MIN/MAX по индексу, а не сканом")
# ============================================================================
# SQLite сводит запрос к чтению крайней строки индекса только когда агрегат в
# запросе ОДИН. `SELECT MIN(x), MAX(x)` уже сканирует таблицу целиком — именно
# так и было написано в обоих снимках до 2026-09-09.

probe = _real_connect(":memory:")
probe.execute("CREATE TABLE flows (id INTEGER PRIMARY KEY, moment TEXT)")
probe.execute("CREATE INDEX idx_flows_moment ON flows(moment)")


def plan(sql):
    return " ".join(str(r[3]) for r in probe.execute("EXPLAIN QUERY PLAN " + sql))


single = plan("SELECT MAX(moment) FROM flows")
both = plan("SELECT MIN(moment) AS a, MAX(moment) AS b FROM flows")
probe.close()

check("одиночный MAX() идёт по индексу", "INDEX" in single.upper(), f"({single})")
check("MIN()+MAX() в одном запросе сканируют таблицу — так писать нельзя",
      "INDEX" not in both.upper(), f"({both})")

# И то, что этого приёма придерживается сам код
import ast  # noqa: E402
import inspect  # noqa: E402
import textwrap  # noqa: E402
from moysklad import warehouse  # noqa: E402
from pyrus import nos  # noqa: E402


def sql_literals(func):
    """Строковые литералы функции без докстроки.

    Разбирать исходник построчно нельзя: в докстроках обоих снимков `MIN()` и
    `MAX()` стоят рядом как раз потому, что там объяснено, почему так писать
    нельзя. Сторож должен смотреть на SQL, а не на объяснение про SQL.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    body = tree.body[0].body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    found = []
    for statement in body:
        for node in ast.walk(statement):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                found.append(node.value)
    return found


for module, func in (("moysklad.warehouse", warehouse.health_snapshot),
                     ("pyrus.nos", nos.health_snapshot)):
    check(f"{module}.health_snapshot: нет MIN и MAX в одном запросе",
          not any("MIN(" in sql.upper() and "MAX(" in sql.upper()
                  for sql in sql_literals(func)),
          "(MIN и MAX в одном SQL-литерале)")
    check(f"{module}.health_snapshot: принимает окно",
          "window_days" in inspect.signature(func).parameters)


# ============================================================================
print()
if failures:
    print(f"=== ПРОВАЛОВ: {len(failures)} ===")
    for name in failures:
        print(f"  - {name}")
    sys.exit(1)

print("=== Цена /health в порядке ===")
print(f"    дешёвый вызов: {cheap_connects} соединений, {cheap.get('total_ms')} мс (локально)")
print(f"    полный вызов:  {full_connects} соединений, {data.get('total_ms')} мс (локально)")
sys.exit(0)
