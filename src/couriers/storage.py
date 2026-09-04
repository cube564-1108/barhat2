"""
Хранилище модуля «Оплата курьерам».

Своя база (couriers.db), а не общая barhat.db: сюда пишет фоновый синк из
RetailCRM, и его нагрузка не должна задевать логин и остальные модули —
в общую базу писали уже дважды, и оба раза от этого тормозил весь сайт.

Путь к файлу берётся из storage_paths (см. комментарий там): на Amvera
постоянный диск — /data, относительный путь означает потерю базы на следующей
сборке.
"""

import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlite_conn import connect as sqlite_connect
from storage_paths import resolve as resolve_data_path

logger = logging.getLogger(__name__)

DB_PATH = resolve_data_path("COURIERS_DB_PATH", "couriers.db")

# Статус RetailCRM, который считается оплачиваемым: ровно «Выполнен».
# Не вся группа complete — «Заказ доставлен» и «Удержание» в неё тоже входят,
# но платить по ним владелец не хочет (решение от 2026-08-24).
COMPLETED_STATUS = "complete"

# Условие «заказ участвует в выплате курьерам»: либо курьер указан, либо
# потрачена себестоимость доставки. Раньше этот отбор стоял на ЗАПИСИ — самовывоз
# в базу не попадал вовсе. Для показателей салонов нужны все выполненные заказы
# («Улица» это в основном самовывоз), поэтому витрина хранит всё, а отбор
# переехал сюда, в чтение. Любой новый запрос модуля выплат обязан его добавлять.
PAYOUT_FILTER = "(courier_id IS NOT NULL OR net_cost > 0)"

# Такси-службы: Яндекс Доставка (2), Максим Такси (12), Драйв такси (169).
# Сид для нового флага; дальше значение правится в интерфейсе.
TAXI_COURIER_IDS = (2, 12, 169)

# Коды типа доставки «Доставка курьером» в RetailCRM. Три записи с одним
# названием: две неактивные, оставшиеся от исторических заказов.
COURIER_DELIVERY_CODES = ("dostavka-kurerom", "courier", "2")

# Канал «Улица» — это способ оформления offline в RetailCRM («Заказ в салоне»).
# Код, а не название: названия в справочнике переименовывают.
STREET_ORDER_METHOD = "offline"


def _ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)


def _add_column_if_missing(conn, table: str, column: str, ddl: str) -> None:
    """
    Идемпотентная миграция колонки.

    На проде 2 воркера gunicorn стартуют одновременно, оба видят «колонки нет» и
    оба выполняют ALTER. Оба штатных исхода гонки (duplicate column name,
    database is locked) означают, что колонку создаёт сосед, — цель достигнута,
    ронять старт воркера нельзя.
    """
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column in existing:
        return
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    except sqlite3.OperationalError as e:
        message = str(e).lower()
        if "duplicate column" not in message and "locked" not in message:
            raise
        logger.info(f"Миграция {table}.{column}: колонку создаёт другой воркер ({e})")


@contextmanager
def get_db():
    """
    Соединение с базой модуля.

    timeout/busy_timeout с запасом: на проде 2 воркера gunicorn, каждый при
    старте прогоняет миграции, а фоновый синк пишет пачками. Без запаса воркер
    получает "database is locked" вместо того, чтобы дождаться очереди.
    """
    _ensure_parent_dir(DB_PATH)
    conn = sqlite_connect(DB_PATH, timeout=30)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_couriers_tables() -> None:
    """Создать таблицы модуля (идемпотентно, зовётся при старте каждого воркера)."""
    with get_db() as conn:
        # ====================================================================
        # Выполненные заказы с доставкой — сырьё отчёта.
        #
        # Храним и заказы БЕЗ курьера (courier_id IS NULL): иначе дырка в
        # данных CRM (забыли проставить курьера) молча исчезает из отчёта,
        # а деньги по такому заказу всё равно потрачены.
        # ====================================================================
        conn.execute("""
            CREATE TABLE IF NOT EXISTS courier_orders (
                retailcrm_order_id INTEGER PRIMARY KEY,
                order_number TEXT,
                delivery_date TEXT NOT NULL,
                courier_id INTEGER,
                courier_name TEXT,
                net_cost REAL NOT NULL DEFAULT 0,
                site_code TEXT,
                city TEXT,
                delivery_city TEXT,
                status TEXT NOT NULL,
                synced_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_courier_orders_date ON courier_orders(delivery_date)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_courier_orders_courier ON courier_orders(courier_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_courier_orders_city ON courier_orders(city)"
        )

        # Поля для модуля «Показатели салонов»: сумма заказа, канал продаж и тип
        # доставки. Живут здесь, а не во второй витрине, потому что это тот же
        # самый набор заказов — второй синк означал бы двойную нагрузку на CRM и
        # два расходящихся ответа на вопрос «сколько отгрузили».
        _add_column_if_missing(conn, "courier_orders", "total_summ", "REAL NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, "courier_orders", "order_method", "TEXT")
        _add_column_if_missing(conn, "courier_orders", "delivery_code", "TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_courier_orders_site_date "
            "ON courier_orders(site_code, delivery_date)"
        )

        # ====================================================================
        # Справочник курьеров. is_service=1 — служба доставки/агрегатор
        # (Яндекс Доставка, Купер, Максим Такси...), их отделяем от штатных
        # курьеров переключателем в отчёте. Значение проставляется эвристикой
        # при синке ТОЛЬКО для новых записей — руками выставленный флаг
        # синхронизация не перетирает.
        # ====================================================================
        conn.execute("""
            CREATE TABLE IF NOT EXISTS couriers (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                is_service INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)

        # is_external_taxi — отдельный флаг, а не переиспользованный is_service:
        # тот шире и включает Купер, Flowwow, «Общий», а в показателе салонов
        # считаются только такси-службы (решение владельца 2026-09-04).
        _add_column_if_missing(conn, "couriers", "is_external_taxi", "INTEGER NOT NULL DEFAULT 0")
        for courier_id in TAXI_COURIER_IDS:
            conn.execute(
                "UPDATE couriers SET is_external_taxi = 1 WHERE id = ? AND is_external_taxi = 0",
                (courier_id,),
            )

        # ====================================================================
        # Типы доставки RetailCRM. counts_as_courier=1 — «Доставка курьером»:
        # именно от этого набора считается доля такси-служб. Флаг правится
        # руками и синхронизацией не перетирается — какой тип доставки считать
        # курьерским, решает человек, а не название записи.
        # ====================================================================
        conn.execute("""
            CREATE TABLE IF NOT EXISTS delivery_types (
                code TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                counts_as_courier INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        for code in COURIER_DELIVERY_CODES:
            conn.execute(
                "INSERT OR IGNORE INTO delivery_types (code, name, counts_as_courier) "
                "VALUES (?, ?, 1)",
                (code, "Доставка курьером"),
            )

        # ====================================================================
        # Салоны RetailCRM (site) и их города — по ним фильтр «город».
        # ====================================================================
        conn.execute("""
            CREATE TABLE IF NOT EXISTS courier_sites (
                code TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                city TEXT,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)

        # ====================================================================
        # Служебные ключи и лок синхронизации (паттерн moysklad/storage.py):
        # планировщик стартует в каждом воркере, а прогон должен идти один.
        # ====================================================================
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sync_state (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS sync_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL DEFAULT (datetime('now')),
                finished_at TEXT,
                records_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'started',
                error_message TEXT
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_courier_sync_log_started ON sync_log(started_at DESC)"
        )


# ============================================================================
# Запись данных синхронизации
# ============================================================================

def replace_orders_window(date_from: str, date_to: str, rows: List[Dict[str, Any]]) -> int:
    """
    Переписать окно дат доставки целиком: удалить всё за [date_from, date_to]
    и вставить то, что сейчас в CRM.

    Именно пересборка окна, а не UPSERT: за прошедшие часы заказ мог сменить
    статус (перестал быть «Выполнен»), сменить курьера или быть удалённым —
    при UPSERT такие записи навсегда остались бы в отчёте и раздули выплату.
    Всё в одной транзакции, чтобы отчёт никогда не читал полупустое окно.
    """
    with get_db() as conn:
        conn.execute(
            "DELETE FROM courier_orders WHERE delivery_date >= ? AND delivery_date <= ?",
            (date_from, date_to),
        )
        conn.executemany(
            """
            INSERT OR REPLACE INTO courier_orders (
                retailcrm_order_id, order_number, delivery_date, courier_id, courier_name,
                net_cost, site_code, city, delivery_city, status,
                total_summ, order_method, delivery_code, synced_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            """,
            [
                (
                    row["retailcrm_order_id"],
                    row.get("order_number"),
                    row["delivery_date"],
                    row.get("courier_id"),
                    row.get("courier_name"),
                    float(row.get("net_cost") or 0),
                    row.get("site_code"),
                    row.get("city"),
                    row.get("delivery_city"),
                    row.get("status") or COMPLETED_STATUS,
                    float(row.get("total_summ") or 0),
                    row.get("order_method"),
                    row.get("delivery_code"),
                )
                for row in rows
            ],
        )
    return len(rows)


def upsert_couriers(couriers: List[Dict[str, Any]]) -> None:
    """
    Обновить справочник курьеров.

    is_service пишется только при первой встрече курьера (DO UPDATE его не
    трогает): иначе следующая же синхронизация затирала бы ручную правку флага.
    """
    if not couriers:
        return
    with get_db() as conn:
        conn.executemany(
            """
            INSERT INTO couriers (id, name, is_service, active, updated_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                active = excluded.active,
                updated_at = datetime('now')
            """,
            [
                (
                    c["id"],
                    c.get("name") or f"Курьер {c['id']}",
                    1 if c.get("is_service") else 0,
                    1 if c.get("active", True) else 0,
                )
                for c in couriers
            ],
        )


def set_courier_service_flag(courier_id: int, is_service: bool) -> bool:
    """Пометить курьера службой доставки (или снять пометку). False — курьера нет."""
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE couriers SET is_service = ?, updated_at = datetime('now') WHERE id = ?",
            (1 if is_service else 0, courier_id),
        )
        return cursor.rowcount > 0


def upsert_sites(sites: List[Dict[str, Any]]) -> None:
    """Обновить справочник салонов (город определяется в retailcrm.py)."""
    if not sites:
        return
    with get_db() as conn:
        conn.executemany(
            """
            INSERT INTO courier_sites (code, name, city, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(code) DO UPDATE SET
                name = excluded.name,
                city = excluded.city,
                updated_at = datetime('now')
            """,
            [(s["code"], s.get("name") or s["code"], s.get("city")) for s in sites],
        )


def get_site_cities() -> Dict[str, Optional[str]]:
    """{код салона: город} — чтобы синк проставлял город без запроса на каждый заказ."""
    with get_db() as conn:
        rows = conn.execute("SELECT code, city FROM courier_sites").fetchall()
    return {row["code"]: row["city"] for row in rows}


# ============================================================================
# Чтение: отчёт и справочники
# ============================================================================

def _period_filter(date_from: Optional[str], date_to: Optional[str]):
    """Кусок WHERE по дате доставки. delivery_date — календарная дата YYYY-MM-DD,
    поэтому сравнение строк и есть сравнение дат (без таймзон: RetailCRM отдаёт
    delivery.date без времени)."""
    where, params = [], []
    if date_from:
        where.append("delivery_date >= ?")
        params.append(date_from)
    if date_to:
        where.append("delivery_date <= ?")
        params.append(date_to)
    return where, params


def report_by_courier(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    city: Optional[str] = None,
    only_own: bool = True,
) -> Dict[str, Any]:
    """
    Отчёт «сколько платить курьеру за период».

    only_own=True — исключить службы доставки (couriers.is_service = 1).
    Заказы без курьера в строки не попадают, но считаются отдельно
    (orders_without_courier) — их сумма иначе просто исчезала бы из виду.
    """
    where, params = _period_filter(date_from, date_to)
    where.append("o.status = ?")
    params.append(COMPLETED_STATUS)
    if city:
        where.append("o.city = ?")
        params.append(city)

    where_sql = " AND ".join(w.replace("delivery_date", "o.delivery_date") for w in where)
    # Витрина хранит ВСЕ выполненные заказы (нужны показателям салонов), поэтому
    # отбор «за что вообще платим» ставится здесь — см. PAYOUT_FILTER
    where_sql += " AND " + PAYOUT_FILTER.replace("courier_id", "o.courier_id").replace(
        "net_cost", "o.net_cost")

    own_filter = " AND COALESCE(c.is_service, 0) = 0" if only_own else ""

    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT
                o.courier_id                        AS courier_id,
                COALESCE(c.name, o.courier_name)    AS courier_name,
                COALESCE(c.is_service, 0)           AS is_service,
                COUNT(*)                            AS orders_count,
                COALESCE(SUM(o.net_cost), 0)        AS total_net_cost,
                GROUP_CONCAT(DISTINCT o.city)       AS cities
            FROM courier_orders o
            LEFT JOIN couriers c ON c.id = o.courier_id
            WHERE {where_sql} AND o.courier_id IS NOT NULL{own_filter}
            GROUP BY o.courier_id, COALESCE(c.name, o.courier_name), COALESCE(c.is_service, 0)
            ORDER BY total_net_cost DESC
            """,
            params,
        ).fetchall()

        # Заказы без курьера, но с потраченной себестоимостью доставки —
        # показатель качества заполнения CRM: деньги ушли, а кому платить,
        # из заказа не видно. Самовывоз (нулевая себестоимость) сюда не
        # попадает: его отсекает условие net_cost > 0 (раньше отсекала запись).
        missing = conn.execute(
            f"""
            SELECT COUNT(*) AS cnt, COALESCE(SUM(o.net_cost), 0) AS total
            FROM courier_orders o
            WHERE {where_sql} AND o.courier_id IS NULL AND o.net_cost > 0
            """,
            params,
        ).fetchone()

    couriers = [
        {
            "courier_id": row["courier_id"],
            "courier_name": row["courier_name"] or f"Курьер {row['courier_id']}",
            "is_service": bool(row["is_service"]),
            "orders_count": row["orders_count"],
            "total_net_cost": round(row["total_net_cost"] or 0, 2),
            "cities": [c for c in (row["cities"] or "").split(",") if c],
        }
        for row in rows
    ]

    return {
        "couriers": couriers,
        "totals": {
            "couriers_count": len(couriers),
            "orders_count": sum(c["orders_count"] for c in couriers),
            "total_net_cost": round(sum(c["total_net_cost"] for c in couriers), 2),
            "orders_without_courier": missing["cnt"] if missing else 0,
            "net_cost_without_courier": round((missing["total"] if missing else 0) or 0, 2),
        },
    }


def list_orders(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    city: Optional[str] = None,
    courier_id: Optional[int] = None,
    without_courier: bool = False,
    limit: int = 1000,
) -> List[Dict[str, Any]]:
    """Расшифровка отчёта по заказам — чтобы сумму можно было проверить, а не верить на слово."""
    where, params = _period_filter(date_from, date_to)
    where.append("status = ?")
    params.append(COMPLETED_STATUS)
    if city:
        where.append("city = ?")
        params.append(city)
    where.append(PAYOUT_FILTER)  # витрина шире отчёта, см. PAYOUT_FILTER
    if without_courier:
        where.append("courier_id IS NULL")
    elif courier_id is not None:
        where.append("courier_id = ?")
        params.append(courier_id)

    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT retailcrm_order_id, order_number, delivery_date, courier_id, courier_name,
                   net_cost, site_code, city, delivery_city
            FROM courier_orders
            WHERE {' AND '.join(where)}
            ORDER BY delivery_date DESC, retailcrm_order_id DESC
            LIMIT ?
            """,
            [*params, limit],
        ).fetchall()
    return [dict(row) for row in rows]


def list_cities() -> List[str]:
    """Города, по которым реально есть данные (для выпадающего фильтра)."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT city FROM courier_orders "
            f"WHERE city IS NOT NULL AND city != '' AND {PAYOUT_FILTER} ORDER BY city"
        ).fetchall()
    return [row["city"] for row in rows]


def list_couriers(only_active: bool = False) -> List[Dict[str, Any]]:
    """Справочник курьеров (для экрана настройки флага «служба доставки»)."""
    query = "SELECT id, name, is_service, active, COALESCE(is_external_taxi, 0) AS is_external_taxi FROM couriers"
    if only_active:
        query += " WHERE active = 1"
    query += " ORDER BY is_service, name"
    with get_db() as conn:
        rows = conn.execute(query).fetchall()
    return [
        {
            "id": row["id"],
            "name": row["name"],
            "is_service": bool(row["is_service"]),
            "is_external_taxi": bool(row["is_external_taxi"]),
            "active": bool(row["active"]),
        }
        for row in rows
    ]


def get_orders_date_range() -> Dict[str, Optional[str]]:
    """Границы загруженных данных — чтобы на странице было видно, за что отчёт вообще есть."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT MIN(delivery_date) AS min_date, MAX(delivery_date) AS max_date "
            f"FROM courier_orders WHERE {PAYOUT_FILTER}"
        ).fetchone()
    return {"min_date": row["min_date"], "max_date": row["max_date"]}


# ============================================================================
# Чтение: показатели салонов (отгрузки, «Улица», такси-службы)
#
# Отдельные функции, а не параметр к отчёту выплат: набор заказов здесь другой
# (все выполненные, включая самовывоз) и группировка идёт по салону, а не по
# курьеру. Живут в модуле-владельце данных, чтобы salonkpi не лез SQL-запросами
# в чужую базу.
# ============================================================================

def aggregate_shipments(date_from: str, date_to: str) -> Dict[str, Dict[str, Any]]:
    """
    Отгрузки за период в разрезе сайтов RetailCRM.

    Возвращает {site_code: {fact, street, orders, courier_orders, taxi_orders}}:
      fact          — сумма заказов (стоимость товаров, без доставки)
      street        — из них с каналом «Улица» (offline)
      courier_orders — заказы с типом доставки «Доставка курьером»
      taxi_orders   — из них отданные внешним такси-службам
    """
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT
                o.site_code                                        AS site_code,
                COUNT(*)                                           AS orders,
                COALESCE(SUM(o.total_summ), 0)                     AS fact,
                COALESCE(SUM(CASE WHEN o.order_method = ?
                                  THEN o.total_summ ELSE 0 END), 0) AS street,
                SUM(CASE WHEN d.counts_as_courier = 1 THEN 1 ELSE 0 END) AS courier_orders,
                SUM(CASE WHEN d.counts_as_courier = 1
                          AND COALESCE(c.is_external_taxi, 0) = 1
                         THEN 1 ELSE 0 END)                        AS taxi_orders
            FROM courier_orders o
            LEFT JOIN delivery_types d ON d.code = o.delivery_code
            LEFT JOIN couriers c ON c.id = o.courier_id
            WHERE o.status = ? AND o.delivery_date >= ? AND o.delivery_date <= ?
            GROUP BY o.site_code
            """,
            (STREET_ORDER_METHOD, COMPLETED_STATUS, date_from, date_to),
        ).fetchall()

        channels = conn.execute(
            """
            SELECT site_code, COALESCE(order_method, 'не указан') AS method,
                   COALESCE(SUM(total_summ), 0) AS amount
            FROM courier_orders
            WHERE status = ? AND delivery_date >= ? AND delivery_date <= ?
            GROUP BY site_code, method
            """,
            (COMPLETED_STATUS, date_from, date_to),
        ).fetchall()

    result: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        result[row["site_code"]] = {
            "fact": round(row["fact"] or 0, 2),
            "street": round(row["street"] or 0, 2),
            "orders": row["orders"],
            "courier_orders": row["courier_orders"] or 0,
            "taxi_orders": row["taxi_orders"] or 0,
            "channels": {},
        }
    for row in channels:
        site = result.get(row["site_code"])
        if site is not None:
            site["channels"][row["method"]] = round(row["amount"] or 0, 2)
    return result


def shipments_data_range() -> Dict[str, Optional[str]]:
    """
    С какой даты в витрине заполнены поля показателей.

    Нужно, чтобы экран мог сказать «данные по каналам с такого-то числа», а не
    показывать честный ноль по периоду, который просто не перезалит после
    миграции.
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT MIN(delivery_date) AS since, MAX(delivery_date) AS until "
            "FROM courier_orders WHERE order_method IS NOT NULL"
        ).fetchone()
    return {"since": row["since"], "until": row["until"]}


def list_unmapped_sites(date_from: str, date_to: str, known_sites: List[str]) -> List[Dict[str, Any]]:
    """Сайты CRM с отгрузками за период, которых нет в справочнике салонов."""
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT site_code, COUNT(*) AS orders, COALESCE(SUM(total_summ), 0) AS amount
            FROM courier_orders
            WHERE status = ? AND delivery_date >= ? AND delivery_date <= ?
            GROUP BY site_code
            """,
            (COMPLETED_STATUS, date_from, date_to),
        ).fetchall()

    known = set(known_sites)
    return [
        {"key": row["site_code"], "orders": row["orders"], "amount": round(row["amount"] or 0, 2)}
        for row in rows
        if row["site_code"] and row["site_code"] not in known
    ]


def list_unflagged_couriers(date_from: str, date_to: str) -> List[Dict[str, Any]]:
    """
    Курьеры периода, похожие на службу доставки, но без флага такси-службы.

    Появление нового агрегатора иначе выглядит как «доля такси упала»: заказы
    ушли наружу, а показатель их не считает. Это тот же класс тихой потери, что
    и переименование салона, — поэтому такие курьеры показываются человеку.
    """
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT o.courier_id, COALESCE(c.name, o.courier_name) AS name, COUNT(*) AS orders,
                   MIN(o.delivery_date) AS since
            FROM courier_orders o
            LEFT JOIN couriers c ON c.id = o.courier_id
            LEFT JOIN delivery_types d ON d.code = o.delivery_code
            WHERE o.status = ? AND o.delivery_date >= ? AND o.delivery_date <= ?
              AND o.courier_id IS NOT NULL
              AND d.counts_as_courier = 1
              AND COALESCE(c.is_external_taxi, 0) = 0
              AND COALESCE(c.is_service, 0) = 1
            GROUP BY o.courier_id, name
            ORDER BY orders DESC
            """,
            (COMPLETED_STATUS, date_from, date_to),
        ).fetchall()
    return [dict(row) for row in rows]


def set_courier_taxi_flag(courier_id: int, is_taxi: bool) -> bool:
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE couriers SET is_external_taxi = ?, updated_at = datetime('now') WHERE id = ?",
            (1 if is_taxi else 0, courier_id),
        )
    return bool(cur.rowcount)


def list_delivery_types() -> List[Dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT code, name, counts_as_courier, active FROM delivery_types ORDER BY name, code"
        ).fetchall()
    return [
        {"code": r["code"], "name": r["name"],
         "counts_as_courier": bool(r["counts_as_courier"]), "active": bool(r["active"])}
        for r in rows
    ]


def set_delivery_type_flag(code: str, counts_as_courier: bool) -> bool:
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE delivery_types SET counts_as_courier = ?, updated_at = datetime('now') "
            "WHERE code = ?",
            (1 if counts_as_courier else 0, code),
        )
    return bool(cur.rowcount)


def upsert_delivery_types(types: List[Dict[str, Any]]) -> None:
    """
    Обновить справочник типов доставки из CRM.

    counts_as_courier пишется только при первой встрече кода: выставленный руками
    флаг синхронизация не перетирает (тот же принцип, что у is_service).
    """
    if not types:
        return
    with get_db() as conn:
        conn.executemany(
            """
            INSERT INTO delivery_types (code, name, counts_as_courier, active, updated_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT(code) DO UPDATE SET
                name = excluded.name,
                active = excluded.active,
                updated_at = datetime('now')
            """,
            [
                (
                    t["code"],
                    t.get("name") or t["code"],
                    1 if t["code"] in COURIER_DELIVERY_CODES else 0,
                    1 if t.get("active", True) else 0,
                )
                for t in types
                if t.get("code")
            ],
        )


# ============================================================================
# Лог и лок синхронизации (паттерн moysklad/storage.py)
# ============================================================================

def start_sync_log() -> int:
    with get_db() as conn:
        cursor = conn.execute("INSERT INTO sync_log DEFAULT VALUES")
        return cursor.lastrowid


def update_sync_log_progress(log_id: int, records_count: int) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE sync_log SET records_count = ? WHERE id = ?",
            (records_count, log_id),
        )


def finish_sync_log(log_id: int, records_count: int, status: str, error_message: str = None) -> None:
    with get_db() as conn:
        conn.execute(
            """
            UPDATE sync_log
            SET finished_at = datetime('now'), records_count = ?, status = ?, error_message = ?
            WHERE id = ?
            """,
            (records_count, status, error_message, log_id),
        )


def get_latest_sync_log(status: Optional[str] = None) -> Optional[Dict[str, Any]]:
    query = "SELECT * FROM sync_log"
    params: List[Any] = []
    if status:
        query += " WHERE status = ?"
        params.append(status)
    query += " ORDER BY id DESC LIMIT 1"
    with get_db() as conn:
        row = conn.execute(query, params).fetchone()
    return dict(row) if row else None


def get_sync_state(key: str) -> Optional[str]:
    with get_db() as conn:
        row = conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_sync_state(key: str, value: str) -> None:
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO sync_state (key, value, updated_at) VALUES (?, ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = datetime('now')
            """,
            (key, value),
        )


def try_claim_scheduled_run(name: str, interval_seconds: int) -> bool:
    """
    Занять талон на очередной тик расписания. False — тик уже отработан.

    Лока для этого мало, и это стоило нам дорого. Лок отвечает на вопрос «идёт
    ли прогон прямо сейчас» и освобождается сразу по завершении. Планировщик же
    крутится в КАЖДОМ воркере, и их тики разъезжаются на десятки секунд: первый
    воркер отработал и отпустил лок, через полминуты просыпается второй, видит
    лок свободным и честно делает ровно ту же работу заново. В логах прода
    2026-08-26 это видно дословно:

        14:20:29 Курьеры: 2026-08-19—2026-08-25 → 442 заказов
        14:20:58 Курьеры: 2026-08-19—2026-08-25 → 442 заказов

    То есть каждые полчаса весь объём записи на общий диск /data шёл дважды.

    Здесь в value лежит время, раньше которого следующий тик не разрешён.
    Талон НЕ освобождается по завершении прогона — он истекает сам через
    interval_seconds. Захват атомарен: один UPDATE ... WHERE.

    Ручной запуск с дашборда сюда не заходит — он идёт через лок, и человек
    по-прежнему может обновить данные в любой момент.
    """
    key = f"schedule:{name}"
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    next_allowed = (
        datetime.utcnow() + timedelta(seconds=interval_seconds)
    ).strftime("%Y-%m-%d %H:%M:%S")
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO sync_state (key, value) VALUES (?, '')",
                (key,),
            )
            cursor = conn.execute(
                """
                UPDATE sync_state SET value = ?, updated_at = datetime('now')
                WHERE key = ? AND (value = '' OR value <= ?)
                """,
                (next_allowed, key, now),
            )
            return cursor.rowcount > 0
    except Exception as e:
        logger.error(f"Ошибка захвата тика расписания {name}: {e}")
        # Не смогли отметиться — считаем тик занятым. Пропустить прогон
        # безопаснее, чем сделать его дважды: следующий будет через интервал.
        return False


def try_acquire_sync_lock(name: str, ttl_seconds: int) -> bool:
    """
    Захватить лок синхронизации. False — держит кто-то другой.

    Проверка «последний лог в статусе started» для этого не годится: между
    чтением статуса и стартом потока влезает второй воркер, и оба качают одно
    и то же. Здесь захват — один UPDATE ... WHERE, атомарный на уровне SQLite.
    В value лежит срок истечения: держатель мог умереть вместе с воркером
    (деплой, OOM) и не позвать release — по TTL лок освободится сам.
    """
    key = f"lock:{name}"
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    expires = (datetime.utcnow() + timedelta(seconds=ttl_seconds)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO sync_state (key, value) VALUES (?, '')",
                (key,),
            )
            cursor = conn.execute(
                """
                UPDATE sync_state SET value = ?, updated_at = datetime('now')
                WHERE key = ? AND (value = '' OR value < ?)
                """,
                (expires, key, now),
            )
            # Формат времени фиксированной ширины — лексикографическое сравнение
            # строк совпадает с хронологическим
            return cursor.rowcount > 0
    except Exception as e:
        logger.error(f"Ошибка захвата лока {name}: {e}")
        return False


def renew_sync_lock(name: str, ttl_seconds: int) -> None:
    """Продлить свой лок (долгий прогон обязан это делать, иначе TTL отдаст лок соседу)."""
    expires = (datetime.utcnow() + timedelta(seconds=ttl_seconds)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        with get_db() as conn:
            conn.execute(
                "UPDATE sync_state SET value = ?, updated_at = datetime('now') WHERE key = ?",
                (expires, f"lock:{name}"),
            )
    except Exception as e:
        logger.error(f"Ошибка продления лока {name}: {e}")


def release_sync_lock(name: str) -> None:
    try:
        with get_db() as conn:
            conn.execute(
                "UPDATE sync_state SET value = '', updated_at = datetime('now') WHERE key = ?",
                (f"lock:{name}",),
            )
    except Exception as e:
        logger.error(f"Ошибка освобождения лока {name}: {e}")
