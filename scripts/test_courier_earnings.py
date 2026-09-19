"""
Сторож расчёта заработка курьера (план 2026-09-19, Фаза 1).

Цифра на экране курьера — это его зарплата. Разойтись с тем, что посчитает
управляющий в «Оплате курьерам», она не имеет права: спор о деньгах выигрывает
расчёт выплаты, а приложение просто перестают открывать. Поэтому главная
проверка здесь не «работает ли функция», а «совпадает ли она с выплатой до
копейки на одних и тех же данных».

Что ещё ловится:

1. **Отсечение служб доставки.** Яндекс и Купер — не наши курьеры, в выплату
   они не входят, и в чей-то личный экран попасть не должны.
2. **Чужие заказы и чужие статусы.** Отменённый, невыполненный, чужого курьера.
3. **Нулевая себестоимость.** Заказ с курьером и `net_cost = 0` в выплате
   учтён как ноль. Он обязан быть виден отдельным числом, иначе курьер решит,
   что приложение потеряло сумму.
4. **Составной индекс.** Проверка «в плане нет SCAN» здесь бесполезна:
   `idx_courier_orders_courier` существует, и без составного индекса SQLite всё
   равно напишет `SEARCH ... USING INDEX`. Поэтому сверяется ИМЯ индекса, а сам
   сторож проверяется удалением индекса — на схеме без него проверка обязана
   провалиться.

Запуск: python scripts/test_courier_earnings.py
"""

import os
import socket
import ssl  # noqa: F401  — импортировать до патча сокета
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))


class NetworkBlocked(Exception):
    pass


def _blocked(*args, **kwargs):
    raise NetworkBlocked("сторож не должен ходить в боевые внешние API")


socket.socket.connect = _blocked

WORK_DIR = tempfile.mkdtemp(prefix="courier_earn_")
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


print(f"\nвременные базы: {WORK_DIR}")

from couriers import storage as cs  # noqa: E402

cs.init_couriers_tables()

OUR = 101          # наш курьер
OTHER = 102        # другой курьер
SERVICE = 103      # служба доставки

with cs.get_db() as conn:
    conn.execute("INSERT OR REPLACE INTO courier_sites (code, name, city) "
                 "VALUES ('site-a', 'Восход', 'Новосибирск')")
    for courier_id, name, is_service in ((OUR, "Шестаков", 0),
                                         (OTHER, "Второй", 0),
                                         (SERVICE, "Яндекс Доставка", 1)):
        conn.execute(
            "INSERT OR REPLACE INTO couriers (id, name, is_service) VALUES (?, ?, ?)",
            (courier_id, name, is_service))
    for code, name, group in (("complete", "Выполнен", "complete"),
                              ("call-courier", "Вызван курьер", "assembling"),
                              ("cancel-other", "Отменён", "cancel")):
        conn.execute("INSERT OR REPLACE INTO order_statuses (code, name, group_code) "
                     "VALUES (?, ?, ?)", (code, name, group))

order_id = [9000]


def add_order(date, courier_id, net_cost, status="complete"):
    order_id[0] += 1
    with cs.get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO courier_orders "
            "  (retailcrm_order_id, order_number, delivery_date, courier_id, "
            "   net_cost, site_code, city, status, delivery_code) "
            "VALUES (?, ?, ?, ?, ?, 'site-a', 'Новосибирск', ?, 'dostavka-kurerom')",
            (order_id[0], f"N{order_id[0]}", date, courier_id, net_cost, status))
    return order_id[0]


# --- данные ----------------------------------------------------------------
# Наш курьер: три дня, в одном из них заказ с нулевой себестоимостью.
add_order("2026-09-15", OUR, 300.0)
add_order("2026-09-15", OUR, 250.5)
add_order("2026-09-17", OUR, 400.0)
add_order("2026-09-17", OUR, 0.0)            # работа была, денег нет
add_order("2026-09-18", OUR, 350.0)

# Шум, который попасть в экран не должен
add_order("2026-09-16", OTHER, 500.0)                        # чужой курьер
add_order("2026-09-16", SERVICE, 700.0)                      # служба доставки
add_order("2026-09-16", OUR, 600.0, status="call-courier")   # ещё не выполнен
add_order("2026-09-16", OUR, 800.0, status="cancel-other")   # отменён
add_order("2026-08-01", OUR, 900.0)                          # вне периода

FROM, TO = "2026-09-15", "2026-09-19"


# ============================================================================
print("\n1. Сумма совпадает с расчётом выплаты до копейки")
# ============================================================================
# Главная проверка сторожа. Не «работает ли функция», а «то же ли это число,
# которое управляющий назовёт курьеру в день выплаты».

mine = cs.courier_earnings_by_day(OUR, FROM, TO)
report = cs.report_by_courier(date_from=FROM, date_to=TO)
payout = next((row for row in report["couriers"] if row["courier_id"] == OUR), None)

check("курьер есть в отчёте выплат", payout is not None, f"({report['couriers']})")
check("число заказов совпадает с выплатой",
      payout and mine["totals"]["orders_count"] == payout["orders_count"],
      f"(экран {mine['totals']['orders_count']} / выплата "
      f"{payout and payout['orders_count']})")
check("сумма совпадает с выплатой до копейки",
      payout and mine["totals"]["total_net_cost"] == payout["total_net_cost"],
      f"(экран {mine['totals']['total_net_cost']} / выплата "
      f"{payout and payout['total_net_cost']})")

# Дробные копейки: 300 + 250.5 + 400 + 0 + 350
check("сумма посчитана верно и по существу",
      mine["totals"]["total_net_cost"] == 1300.5,
      f"({mine['totals']['total_net_cost']})")


# ============================================================================
print("\n2. В экран не попадает чужое")
# ============================================================================

days = {day["date"]: day for day in mine["days"]}
check("дни только с заказами нашего курьера", sorted(days) ==
      ["2026-09-15", "2026-09-17", "2026-09-18"], f"({sorted(days)})")
check("чужой курьер, служба, невыполненный и отменённый отсеяны",
      "2026-09-16" not in days, f"({sorted(days)})")
check("заказ вне периода не считается", "2026-08-01" not in days)
check("дни без заказов в ответ не кладём — их дорисует экран",
      "2026-09-16" not in days and len(days) == 3)
check("дни идут от свежего к старому",
      [day["date"] for day in mine["days"]] ==
      ["2026-09-18", "2026-09-17", "2026-09-15"],
      f"({[d['date'] for d in mine['days']]})")

# Служба доставки, спросившая про себя, ничего не получает: ей платят не так
service = cs.courier_earnings_by_day(SERVICE, FROM, TO)
check("у службы доставки заработка нет", service["totals"]["orders_count"] == 0,
      f"({service['totals']})")


# ============================================================================
print("\n3. Заказ с нулевой стоимостью виден отдельно, а не растворяется")
# ============================================================================
# Работа была, денег за неё не будет. Молчаливый ноль в сумме курьер прочитает
# как потерю и пойдёт разбираться не туда.

check("день с нулевым заказом считает его в заказах",
      days["2026-09-17"]["orders_count"] == 2,
      f"({days['2026-09-17']})")
check("и называет его отдельным числом", days["2026-09-17"]["zero_cost"] == 1,
      f"({days['2026-09-17']})")
check("в днях без таких заказов ноль", days["2026-09-18"]["zero_cost"] == 0)
check("итог по периоду тоже знает про них", mine["totals"]["zero_cost"] == 1,
      f"({mine['totals']})")


# ============================================================================
print("\n4. Пустые случаи отвечают нулями, а не падают")
# ============================================================================

empty = cs.courier_earnings_by_day(OUR, "2026-07-01", "2026-07-31")
check("период без заказов — пустой список и нули",
      empty["days"] == [] and empty["totals"]["orders_count"] == 0
      and empty["totals"]["total_net_cost"] == 0, f"({empty})")

unknown = cs.courier_earnings_by_day(999, FROM, TO)
check("несуществующий курьер — тоже нули, без исключения",
      unknown["totals"]["orders_count"] == 0, f"({unknown['totals']})")


# ============================================================================
print("\n5. Запрос идёт по составному индексу, а не перебором")
# ============================================================================
# Проверять надо ИМЯ индекса: idx_courier_orders_courier уже существует, и без
# составного SQLite всё равно напишет «SEARCH ... USING INDEX» — проверка
# «нет SCAN» зеленела бы на худшем коде (правило CLAUDE.md про сторожа,
# который закрепляет ошибку).

COMPOSITE = "idx_courier_orders_courier_date"

SQL = """
    SELECT o.delivery_date, COUNT(*), SUM(o.net_cost)
      FROM courier_orders o
      LEFT JOIN couriers c ON c.id = o.courier_id
     WHERE o.delivery_date >= ? AND o.delivery_date <= ?
       AND (o.courier_id IS NOT NULL OR o.net_cost > 0)
       AND o.status = ? AND o.courier_id = ?
       AND COALESCE(c.is_service, 0) = 0
     GROUP BY o.delivery_date
"""


def plan_text():
    with cs.get_db() as conn:
        rows = conn.execute("EXPLAIN QUERY PLAN " + SQL,
                            (FROM, TO, "complete", OUR)).fetchall()
    return " | ".join(str(row["detail"]) for row in rows)


with_index = plan_text()
check("план запроса использует составной индекс", COMPOSITE in with_index,
      f"({with_index})")

# А теперь проверяем сам сторож: на схеме без индекса он обязан провалиться.
# Без этого шага проверка выше — просто слово, найденное в выводе.
with cs.get_db() as conn:
    conn.execute(f"DROP INDEX IF EXISTS {COMPOSITE}")
without_index = plan_text()
check("без индекса та же проверка НЕ проходит — значит она что-то проверяет",
      COMPOSITE not in without_index, f"({without_index})")
check("и SQLite в этом случае всё равно пишет SEARCH — «нет SCAN» бесполезно",
      "SEARCH" in without_index, f"({without_index})")

with cs.get_db() as conn:
    conn.execute(f"CREATE INDEX IF NOT EXISTS {COMPOSITE} "
                 f"ON courier_orders(courier_id, delivery_date)")
check("индекс восстановлен", COMPOSITE in plan_text())


print()
if failures:
    print(f"=== ПРОВАЛЕНО: {len(failures)} ===")
    for name in failures:
        print(f"  - {name}")
    sys.exit(1)
print("=== Заработок курьера считается как выплата ===")
