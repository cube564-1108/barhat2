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
