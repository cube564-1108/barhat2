"""
Хранилище модуля «Курьеры: доставка заказов» (план 2026-09-08).

Отдельный файл, а не продолжение `storage.py`, по трём причинам:

1. `storage.py` — это витрина заказов и отчёт по выплатам, он уже на 2500 строк
   и его правят параллельно. Новый модуль там утонет.
2. Таблицы здесь принадлежат нашей работе, а не CRM: бронь, очередь отправки
   статусов, настройки городов. Витрина перезаписывается синком целиком
   (`replace_orders_window` чистит окно по дате доставки) — состояние курьера
   не должно жить в таблице, которую регулярно удаляют кусками.
3. База та же (`couriers.db`) и соединение то же (`storage.get_db`): это один
   модуль с точки зрения данных, разделён только файл.

Что здесь НЕ хранится: сами заказы. Они в `courier_orders`, и второй витрины
не будет — иначе появится два расходящихся ответа на вопрос «что везём».
"""

import logging
import sqlite3
from typing import Any, Dict, List, Optional

from .storage import _add_column_if_missing, get_db

logger = logging.getLogger(__name__)

# --- состояния брони -------------------------------------------------------
STATE_CLAIMED = "claimed"        # забронирован, ещё не забран
STATE_PICKED_UP = "picked_up"    # курьер забрал заказ из салона
STATE_DELIVERED = "delivered"    # вручён
STATE_RELEASED = "released"      # отпущен (сам, админом, автоматом)
STATE_PROBLEM = "problem"        # недозвон, перенос, отказ

# Живые состояния: пока бронь в одном из них, заказ другим курьерам не отдаётся.
ACTIVE_STATES = (STATE_CLAIMED, STATE_PICKED_UP)

# --- причины снятия брони --------------------------------------------------
RELEASE_SELF = "self"            # курьер отказался сам
RELEASE_EXPIRED = "expired"      # сгорела по времени
RELEASE_ADMIN = "admin"          # снял управляющий
RELEASE_OUTSOURCED = "outsourced"  # заказ передали службе доставки
RELEASE_ORDER_GONE = "order_gone"  # заказ отменён или ушёл из видимых статусов

# Значения по умолчанию для города, у которого настроек ещё нет. Ноль записей в
# `courier_city_settings` — нормальное состояние: заводить строку на каждый
# город руками не нужно, пока значения устраивают.
DEFAULT_MAX_ACTIVE_CLAIMS = 3
DEFAULT_CLAIM_HORIZON_DAYS = 1      # сегодня и завтра
DEFAULT_UNCLAIMED_ALERT_MINUTES = 90
DEFAULT_QUIET_HOURS_FROM = "22:00"
DEFAULT_QUIET_HOURS_TO = "08:00"

# --- роли статусов CRM в глазах курьера ------------------------------------
ROLE_VISIBLE = "visible"   # заказ показывается курьеру
ROLE_READY = "ready"       # заказ собран (тоже показывается)
STATUS_ROLES = (ROLE_VISIBLE, ROLE_READY)

# Сид справочника: коды найдены разведкой 2026-09-08 на боевой CRM.
# «Передан флористу» — момент, с которого заказ имеет смысл показывать;
# «Заказ готов» — отметка флориста о сборке (её ставят у 85% заказов, но в
# момент начала окна доставки, поэтому она бейдж, а не пропуск — см. §4 плана).
SEED_VISIBLE_STATUSES = (
    ("send-to-florist", ROLE_VISIBLE),
    ("correction", ROLE_VISIBLE),
    ("order-complete", ROLE_READY),
)


def init_delivery_tables() -> None:
    """Создать таблицы модуля (идемпотентно, зовётся при старте воркера)."""
    with get_db() as conn:
        # ====================================================================
        # Профиль курьера: связка учётки с курьером в RetailCRM и городом.
        #
        # retailcrm_courier_id обязателен не для работы, а для ДЕНЕГ: модуль
        # «Оплата курьерам» считает выплаты по delivery.data.courierId, и если
        # при отметке «Забрал» мы не проставим курьера в CRM, его работа просто
        # не попадёт в оплату. Поэтому пустая связка не блокирует доставку, но
        # обязана быть видна отдельным предупреждением.
        # ====================================================================
        conn.execute("""
            CREATE TABLE IF NOT EXISTS courier_profiles (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                retailcrm_courier_id INTEGER,
                city TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                updated_by TEXT,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_courier_profiles_city "
            "ON courier_profiles(city, active)"
        )
        # Один курьер CRM — одна учётка. Иначе выплаты двух людей сольются в
        # одну строку отчёта, и разобрать это задним числом будет нечем.
        # Частичный индекс: NULL допускается сколько угодно (связка ещё не
        # заведена), дубли реальных id — нет.
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_courier_profiles_crm "
            "ON courier_profiles(retailcrm_courier_id) "
            "WHERE retailcrm_courier_id IS NOT NULL"
        )

        # ====================================================================
        # Настройки на город. Города различаются размером и числом курьеров —
        # одно число на всю сеть будет либо шуметь, либо опаздывать (решение
        # владельца 2026-09-08).
        #
        # NULL в любом поле означает «взять умолчание модуля», а не «ноль»:
        # ноль в max_active_claims запретил бы брать заказы вообще.
        # ====================================================================
        # ====================================================================
        # Какие статусы CRM что значат для курьера.
        #
        # Справочник, а не константы в коде: статусы в CRM заводят и
        # переименовывают, и вывод «по названию» ломается молча — это ровно тот
        # класс ошибки, из-за которого счета уходили в банк без НДС.
        #
        #   role = 'visible' — заказ показывается курьеру («Передан флористу»)
        #   role = 'ready'   — заказ собран («Заказ готов»); тоже видимый
        #
        # Сид — коды, найденные разведкой 2026-09-08. Дальше правится человеком
        # и синком не перетирается.
        # ====================================================================
        conn.execute("""
            CREATE TABLE IF NOT EXISTS courier_visible_statuses (
                status_code TEXT PRIMARY KEY,
                role TEXT NOT NULL DEFAULT 'visible',
                updated_by TEXT,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        for code, role in SEED_VISIBLE_STATUSES:
            conn.execute(
                "INSERT OR IGNORE INTO courier_visible_statuses (status_code, role) "
                "VALUES (?, ?)",
                (code, role),
            )

        # ====================================================================
        # Бронь заказа курьером.
        #
        # Отдельно от courier_orders намеренно: витрину синк перезаписывает
        # окнами по дате доставки (DELETE + INSERT), и состояние работы курьера
        # в ней не пережило бы ближайшие полчаса.
        #
        # Схема заводится здесь, а логика захвата — в Фазе 4. Так API чтения
        # сразу знает про бронь: «свободен / занят / мой» — это то, ради чего
        # курьер открывает список, и дописывать это в готовый экран потом
        # означало бы переделывать и запрос, и разметку.
        # ====================================================================
        conn.execute("""
            CREATE TABLE IF NOT EXISTS delivery_assignments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                retailcrm_order_id INTEGER NOT NULL,
                courier_user_id INTEGER NOT NULL,
                state TEXT NOT NULL,
                claimed_at TEXT NOT NULL DEFAULT (datetime('now')),
                picked_up_at TEXT,
                delivered_at TEXT,
                released_at TEXT,
                release_reason TEXT,
                problem_code TEXT,
                problem_note TEXT,
                expires_at TEXT,
                warned_at TEXT
            )
        """)
        # Последняя преграда инварианта «у заказа не больше одной живой брони»:
        # держит его, даже если появится новый путь записи. Частичный индекс —
        # снятые и доставленные записи не мешают взять заказ снова.
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_assign_one_active "
            "ON delivery_assignments(retailcrm_order_id) "
            f"WHERE state IN ('{STATE_CLAIMED}', '{STATE_PICKED_UP}')"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_assign_courier_state "
            "ON delivery_assignments(courier_user_id, state)"
        )

        conn.execute("""
            CREATE TABLE IF NOT EXISTS courier_city_settings (
                city TEXT PRIMARY KEY,
                max_active_claims INTEGER,
                claim_horizon_days INTEGER,
                unclaimed_alert_minutes INTEGER,
                quiet_hours_from TEXT,
                quiet_hours_to TEXT,
                updated_by TEXT,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)


def city_settings(city: Optional[str]) -> Dict[str, Any]:
    """
    Настройки города с подстановкой умолчаний.

    Отдаёт ещё и `is_default` по каждому полю — экран настроек должен
    показывать, что именно задано руками, а что взято по умолчанию: иначе
    непонятно, почему в двух городах разное поведение.
    """
    row = None
    if city:
        with get_db() as conn:
            row = conn.execute(
                "SELECT * FROM courier_city_settings WHERE city = ?", (city,)
            ).fetchone()

    def pick(field: str, default: Any) -> Any:
        value = row[field] if row is not None else None
        return default if value is None else value

    return {
        "city": city,
        "max_active_claims": pick("max_active_claims", DEFAULT_MAX_ACTIVE_CLAIMS),
        "claim_horizon_days": pick("claim_horizon_days", DEFAULT_CLAIM_HORIZON_DAYS),
        "unclaimed_alert_minutes": pick("unclaimed_alert_minutes",
                                        DEFAULT_UNCLAIMED_ALERT_MINUTES),
        "quiet_hours_from": pick("quiet_hours_from", DEFAULT_QUIET_HOURS_FROM),
        "quiet_hours_to": pick("quiet_hours_to", DEFAULT_QUIET_HOURS_TO),
        "has_own_settings": row is not None,
    }


def list_city_settings(cities: List[str]) -> List[Dict[str, Any]]:
    """Настройки по всем городам, включая те, у которых своей строки нет."""
    return [city_settings(city) for city in cities]


def set_city_settings(city: str, values: Dict[str, Any],
                      username: Optional[str] = None) -> None:
    """
    Задать настройки города. None в значении — вернуть поле к умолчанию.

    Проверки здесь, а не в обработчике: ручку зовут и форма, и будущие массовые
    действия, а «ноль одновременных броней» означал бы молча выключенный
    модуль в одном городе.
    """
    limits = {
        "max_active_claims": (1, 50),
        "claim_horizon_days": (0, 14),
        "unclaimed_alert_minutes": (5, 24 * 60),
    }
    clean: Dict[str, Any] = {}
    for field, (low, high) in limits.items():
        value = values.get(field)
        if value in (None, ""):
            clean[field] = None
            continue
        number = int(value)
        if not (low <= number <= high):
            raise ValueError(f"{field}: допустимо от {low} до {high}, получено {number}")
        clean[field] = number

    for field in ("quiet_hours_from", "quiet_hours_to"):
        value = (values.get(field) or "").strip()
        if not value:
            clean[field] = None
            continue
        if len(value) != 5 or value[2] != ":" or not value.replace(":", "").isdigit():
            raise ValueError(f"{field}: ожидается ЧЧ:ММ, получено «{value}»")
        clean[field] = value

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO courier_city_settings
                (city, max_active_claims, claim_horizon_days, unclaimed_alert_minutes,
                 quiet_hours_from, quiet_hours_to, updated_by, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(city) DO UPDATE SET
                max_active_claims = excluded.max_active_claims,
                claim_horizon_days = excluded.claim_horizon_days,
                unclaimed_alert_minutes = excluded.unclaimed_alert_minutes,
                quiet_hours_from = excluded.quiet_hours_from,
                quiet_hours_to = excluded.quiet_hours_to,
                updated_by = excluded.updated_by,
                updated_at = datetime('now')
            """,
            (city, clean["max_active_claims"], clean["claim_horizon_days"],
             clean["unclaimed_alert_minutes"], clean["quiet_hours_from"],
             clean["quiet_hours_to"], username),
        )


# ---------------------------------------------------------------------------
# Справочник статусов
# ---------------------------------------------------------------------------

def list_visible_statuses() -> List[Dict[str, Any]]:
    """Справочник «статус CRM → роль», с названиями из синка статусов."""
    with get_db() as conn:
        rows = conn.execute("""
            SELECT v.status_code, v.role, v.updated_by, v.updated_at,
                   s.name AS status_name, s.group_code
            FROM courier_visible_statuses v
            LEFT JOIN order_statuses s ON s.code = v.status_code
            ORDER BY v.role, v.status_code
        """).fetchall()
    return [dict(row) for row in rows]


def set_visible_status(status_code: str, role: Optional[str],
                       username: Optional[str] = None) -> None:
    """
    Задать роль статуса. role=None — убрать статус из справочника.

    Роль проверяется здесь, а не в обработчике: справочник правят и формой, и
    будущими массовыми действиями, а неизвестная роль означала бы статус,
    который не показывается никому и никак это не объясняет.
    """
    if role is not None and role not in STATUS_ROLES:
        raise ValueError(f"Неизвестная роль статуса: {role}. Доступны: {STATUS_ROLES}")

    with get_db() as conn:
        if role is None:
            conn.execute("DELETE FROM courier_visible_statuses WHERE status_code = ?",
                         (status_code,))
            return
        conn.execute(
            """
            INSERT INTO courier_visible_statuses (status_code, role, updated_by, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(status_code) DO UPDATE SET
                role = excluded.role,
                updated_by = excluded.updated_by,
                updated_at = datetime('now')
            """,
            (status_code, role, username),
        )


def visible_status_codes() -> Dict[str, List[str]]:
    """{'visible': [...], 'ready': [...]} — для отбора заказов и бейджа готовности."""
    result: Dict[str, List[str]] = {ROLE_VISIBLE: [], ROLE_READY: []}
    with get_db() as conn:
        for row in conn.execute("SELECT status_code, role FROM courier_visible_statuses"):
            result.setdefault(row["role"], []).append(row["status_code"])
    return result


# ---------------------------------------------------------------------------
# Заказы для курьера
# ---------------------------------------------------------------------------

# Поля, которые видит любой курьер города: по ним он решает, брать заказ или нет.
PUBLIC_ORDER_FIELDS = (
    "retailcrm_order_id", "order_number", "delivery_date", "site_code", "city",
    "store_key", "address_text", "delivery_time_from", "delivery_time_to",
    "ready_time", "ready_planned_at", "status",
)

# Поля с персональными данными: отдаются только тому, кто взял заказ (и
# управляющему). До брони курьеру хватает улицы и времени — а объём ПДн,
# который «просто просматривают», лишним быть не должен.
PRIVATE_ORDER_FIELDS = (
    "recipient_name", "recipient_phone", "customer_name", "customer_phone",
    "manager_comment", "customer_comment", "note_text", "do_not_contact_recipient",
    "recipient_is_customer",
)


def _short_address(address: Optional[str]) -> Optional[str]:
    """
    Улица и дом без квартиры, подъезда и кода домофона.

    Курьеру до брони нужно понять, далеко ли ехать, а не как попасть в
    квартиру. Режем по первой запятой после номера дома: адреса приходят
    строкой «ул. Ленина, 45, кв. 12, подъезд 2».
    """
    if not address:
        return None
    parts = [part.strip() for part in address.split(",")]
    return ", ".join(parts[:2]) if len(parts) > 2 else address


def list_orders_for_courier(city: Optional[str], date_from: str, date_to: str,
                            courier_user_id: Optional[int] = None,
                            with_private: bool = False,
                            courier_delivery_codes: Optional[List[str]] = None
                            ) -> List[Dict[str, Any]]:
    """
    Заказы, которые курьер видит в ленте.

    Один запрос к базе: экран открывают с телефона на ходу, и /data сетевой —
    каждое лишнее обращение это десятки миллисекунд в лучшем случае.

    Отбор целиком на сервере (город, тип доставки, статусы) — фронт фильтровать
    не может: он получает то, что ему положено видеть, и не больше.

    with_private=True отдаёт контакты и комментарии. Для курьера это включается
    только по его собственной брони, для управляющего — по секции
    courier_dispatch.
    """
    codes = visible_status_codes()
    visible = codes.get(ROLE_VISIBLE, []) + codes.get(ROLE_READY, [])
    if not visible:
        # Пустой справочник — это не «показать всё», а «настройка не сделана».
        # Показать всё означало бы вывалить курьеру отменённые и выполненные.
        return []
    ready = set(codes.get(ROLE_READY, []))

    conditions = ["o.delivery_date >= ?", "o.delivery_date <= ?",
                  f"o.status IN ({','.join('?' * len(visible))})"]
    params: List[Any] = [date_from, date_to, *visible]

    if city:
        conditions.append("o.city = ?")
        params.append(city)

    if courier_delivery_codes:
        conditions.append(
            f"o.delivery_code IN ({','.join('?' * len(courier_delivery_codes))})")
        params.extend(courier_delivery_codes)

    sql = f"""
        SELECT o.*, s.name AS site_name, s.utc_offset,
               a.id AS assignment_id, a.state AS assignment_state,
               a.courier_user_id AS assignment_user_id, a.expires_at
        FROM courier_orders o
        LEFT JOIN courier_sites s ON s.code = o.site_code
        LEFT JOIN delivery_assignments a
               ON a.retailcrm_order_id = o.retailcrm_order_id
              AND a.state IN ('{STATE_CLAIMED}', '{STATE_PICKED_UP}')
        WHERE {' AND '.join(conditions)}
        ORDER BY o.delivery_date, o.delivery_time_from IS NULL, o.delivery_time_from
    """

    with get_db() as conn:
        rows = [dict(row) for row in conn.execute(sql, params).fetchall()]

    result = []
    for row in rows:
        mine = (courier_user_id is not None
                and row.get("assignment_user_id") == courier_user_id)
        item = {field: row.get(field) for field in PUBLIC_ORDER_FIELDS}
        item.update({
            "site_name": row.get("site_name"),
            "utc_offset": row.get("utc_offset"),
            "is_ready": row.get("status") in ready,
            "assignment_state": row.get("assignment_state"),
            "is_mine": mine,
            "is_free": row.get("assignment_state") is None,
            "expires_at": row.get("expires_at"),
        })
        # Контакты — только по своей брони либо управляющему.
        if with_private or mine:
            item.update({field: row.get(field) for field in PRIVATE_ORDER_FIELDS})
        else:
            item["address_text"] = _short_address(row.get("address_text"))
        result.append(item)
    return result


def order_for_courier(order_id: int, city: Optional[str],
                      courier_user_id: Optional[int] = None,
                      with_private: bool = False) -> Optional[Dict[str, Any]]:
    """
    Карточка одного заказа.

    Город проверяется и здесь, а не только в списке: карточку открывают по
    прямой ссылке, и «фильтр стоит в ленте» не защищает ничего.
    """
    with get_db() as conn:
        row = conn.execute("""
            SELECT o.*, s.name AS site_name, s.utc_offset,
                   a.id AS assignment_id, a.state AS assignment_state,
                   a.courier_user_id AS assignment_user_id, a.expires_at
            FROM courier_orders o
            LEFT JOIN courier_sites s ON s.code = o.site_code
            LEFT JOIN delivery_assignments a
                   ON a.retailcrm_order_id = o.retailcrm_order_id
                  AND a.state IN (?, ?)
            WHERE o.retailcrm_order_id = ?
        """, (STATE_CLAIMED, STATE_PICKED_UP, order_id)).fetchone()
        if not row:
            return None
        row = dict(row)
        items = [dict(item) for item in conn.execute(
            "SELECT offer_id, product_name, article, quantity FROM order_items "
            "WHERE retailcrm_order_id = ? ORDER BY product_name",
            (order_id,)).fetchall()]

    if city and row.get("city") != city:
        return None

    ready = set(visible_status_codes().get(ROLE_READY, []))
    mine = (courier_user_id is not None
            and row.get("assignment_user_id") == courier_user_id)

    card = {field: row.get(field) for field in PUBLIC_ORDER_FIELDS}
    card.update({
        "site_name": row.get("site_name"),
        "utc_offset": row.get("utc_offset"),
        "is_ready": row.get("status") in ready,
        "assignment_state": row.get("assignment_state"),
        "is_mine": mine,
        "is_free": row.get("assignment_state") is None,
        "expires_at": row.get("expires_at"),
        "items": items,
    })
    if with_private or mine:
        card.update({field: row.get(field) for field in PRIVATE_ORDER_FIELDS})
    else:
        card["address_text"] = _short_address(row.get("address_text"))
    return card


# ---------------------------------------------------------------------------
# Точечное обновление витрины (лента изменений)
# ---------------------------------------------------------------------------

# Поля, которые лента пишет в courier_orders. Тот же набор, что кладёт глубокий
# синк в replace_orders_window, — расхождение проверяется тестом
# scripts/test_courier_feed.py: если один путь начнёт писать поле, а другой нет,
# карточка будет то полной, то пустой в зависимости от того, кто обновил заказ
# последним, и поймать это глазами невозможно.
FEED_ORDER_FIELDS = (
    "order_number", "delivery_date", "courier_id", "courier_name", "net_cost",
    "site_code", "city", "delivery_city", "status", "total_summ", "order_method",
    "delivery_code", "store_key", "ready_time", "ready_hour", "ready_source",
    "address_text", "delivery_time_from", "delivery_time_to",
    "recipient_name", "recipient_phone", "recipient_is_customer",
    "do_not_contact_recipient", "customer_name", "customer_phone",
    "manager_comment", "customer_comment", "note_text", "ready_planned_at",
)

# Поля, которые лента НЕ трогает: их считает глубокий синк, и затирать их
# точечным обновлением значило бы обнулять вес слота при каждой правке заказа
# в CRM.
FEED_PRESERVED_FIELDS = ("weight_units", "duration_slots", "slot_changed_at",
                         "minutes_total", "minutes_without_norm")


def _order_values(row: Dict[str, Any]) -> tuple:
    numeric = {"net_cost", "total_summ"}
    flags = {"recipient_is_customer", "do_not_contact_recipient"}
    values = []
    for field in FEED_ORDER_FIELDS:
        value = row.get(field)
        if field in numeric:
            values.append(float(value or 0))
        elif field in flags:
            values.append(int(value or 0))
        else:
            values.append(value)
    return tuple(values)


def upsert_orders_from_crm(rows: List[Dict[str, Any]]) -> int:
    """
    Обновить отдельные заказы в витрине (то, что принесла лента изменений).

    Не INSERT OR REPLACE: он стёр бы поля, которых лента не знает, — вес слота
    и отметку смены часа готовности считает глубокий синк, и обнулять их при
    каждой правке заказа в CRM нельзя. Поэтому ON CONFLICT DO UPDATE ровно по
    своим полям.

    Позиции заказа переписываются целиком: состав меняют, и «добавить новые, а
    старые оставить» означало бы вечно растущий букет.
    """
    if not rows:
        return 0

    columns = ("retailcrm_order_id",) + FEED_ORDER_FIELDS
    placeholders = ", ".join("?" * len(columns))
    updates = ", ".join(f"{field} = excluded.{field}" for field in FEED_ORDER_FIELDS)

    with get_db() as conn:
        conn.executemany(
            f"""
            INSERT INTO courier_orders ({", ".join(columns)}, synced_at)
            VALUES ({placeholders}, datetime('now'))
            ON CONFLICT(retailcrm_order_id) DO UPDATE SET
                {updates},
                synced_at = datetime('now')
            """,
            [(row["retailcrm_order_id"],) + _order_values(row) for row in rows],
        )

        for row in rows:
            order_id = row["retailcrm_order_id"]
            conn.execute("DELETE FROM order_items WHERE retailcrm_order_id = ?",
                         (order_id,))
            items = row.get("items") or []
            if items:
                conn.executemany(
                    """
                    INSERT INTO order_items
                        (retailcrm_order_id, offer_id, delivery_date,
                         product_name, article, quantity)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [(order_id, item["offer_id"], row["delivery_date"],
                      item.get("product_name"), item.get("article"),
                      float(item.get("quantity") or 0)) for item in items],
                )
    return len(rows)


def apply_orders_from_crm(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Записать заказы из CRM и пересчитать по ним нагрузку салона.

    Единая точка входа для всех, кто обновляет витрину точечно. Отдельно
    `upsert_orders_from_crm` вызывать не надо: витрину читает не только модуль
    доставки, но и «Загрузка салонов», а она смотрит на трудоёмкость заказа.
    Запись без пересчёта означала бы, что пришедший лентой заказ висит в сетке
    невесомым до следующего глубокого синка — то есть до получаса, ровно в тот
    момент, когда в сетку и смотрят.

    Пересчёт идёт по датам доставки (их у пачки одна-две), а не по заказам:
    такой интерфейс у функций пересчёта, и тарифы читаются один раз на период.
    Падение пересчёта не роняет запись: свежий заказ у курьера важнее цифры в
    сетке, а глубокий синк всё равно пересчитает всё окно.
    """
    written = upsert_orders_from_crm(rows)
    result = {"written": written, "recalc_dates": 0, "recalc_errors": 0}
    if not written:
        return result

    from . import storage as courier_storage

    for date in sorted({row["delivery_date"] for row in rows if row.get("delivery_date")}):
        try:
            courier_storage.recalc_weights_range(date, date)
            # Минуты сборки появились позже весов (модуль «нагрузка в минутах»),
            # и на старой версии кода функции может не быть.
            if hasattr(courier_storage, "recalc_minutes_range"):
                courier_storage.recalc_minutes_range(date, date)
            result["recalc_dates"] += 1
        except Exception as e:
            result["recalc_errors"] += 1
            logger.warning(f"Пересчёт нагрузки за {date} не удался: {e}")
    return result


# ---------------------------------------------------------------------------
# Профили курьеров
# ---------------------------------------------------------------------------

def list_courier_profiles(city: Optional[str] = None,
                          only_active: bool = False) -> List[Dict[str, Any]]:
    """Профили курьеров; city=None — все города."""
    sql = "SELECT * FROM courier_profiles"
    conditions, params = [], []
    if city:
        conditions.append("city = ?")
        params.append(city)
    if only_active:
        conditions.append("active = 1")
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY city IS NULL, city, username"

    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def get_courier_profile(user_id: int) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM courier_profiles WHERE user_id = ?", (user_id,)
        ).fetchone()
    return dict(row) if row else None


def save_courier_profile(user_id: int, username: Optional[str],
                         city: Optional[str],
                         retailcrm_courier_id: Optional[int],
                         active: bool = True,
                         updated_by: Optional[str] = None) -> None:
    """
    Завести или обновить профиль курьера.

    Дубль `retailcrm_courier_id` ловится уникальным индексом и превращается в
    понятную ошибку: «этот курьер CRM уже привязан к другой учётке». Пускать
    сюда 500 нельзя — связку заводят руками, и ошибка здесь штатная.
    """
    try:
        with get_db() as conn:
            conn.execute(
                """
                INSERT INTO courier_profiles
                    (user_id, username, retailcrm_courier_id, city, active,
                     updated_by, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    retailcrm_courier_id = excluded.retailcrm_courier_id,
                    city = excluded.city,
                    active = excluded.active,
                    updated_by = excluded.updated_by,
                    updated_at = datetime('now')
                """,
                (user_id, username, retailcrm_courier_id, city,
                 1 if active else 0, updated_by),
            )
    except sqlite3.IntegrityError as e:
        # SQLite называет в тексте КОЛОНКУ, а не индекс: «UNIQUE constraint
        # failed: courier_profiles.retailcrm_courier_id». Проверка по имени
        # индекса не срабатывала, и вместо понятного текста наружу летел 500.
        if "retailcrm_courier_id" in str(e):
            raise ValueError(
                f"Курьер CRM #{retailcrm_courier_id} уже привязан к другой учётной записи"
            ) from e
        raise


def profiles_without_crm_link(city: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Активные курьеры без связки с CRM — их работа не попадёт в выплаты.

    Отдельный запрос, а не флаг в общем списке: этот список должен быть
    коротким и попадаться на глаза, а не теряться среди прочих строк.
    """
    sql = ("SELECT * FROM courier_profiles "
           "WHERE active = 1 AND retailcrm_courier_id IS NULL")
    params: List[Any] = []
    if city:
        sql += " AND city = ?"
        params.append(city)
    with get_db() as conn:
        rows = conn.execute(sql + " ORDER BY city, username", params).fetchall()
    return [dict(row) for row in rows]
