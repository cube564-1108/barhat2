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
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlite_conn import connect as sqlite_connect
from storage_paths import resolve as resolve_data_path

logger = logging.getLogger(__name__)

DB_PATH = resolve_data_path("COURIERS_DB_PATH", "couriers.db")

# Статус RetailCRM, который считается оплачиваемым: ровно «Выполнен».
# Не вся группа complete — «Заказ доставлен» и «Удержание» в неё тоже входят,
# но платить по ним владелец не хочет (решение от 2026-08-24).
#
# ВАЖНО: КАЖДОЕ чтение витрины обязано фильтровать статус явно. До 2026-09-05
# четыре запроса (list_cities, get_orders_date_range, shipments_data_range,
# health_snapshot) статус не проверяли — их случайно прикрывал PAYOUT_FILTER и
# то, что синк тянул только выполненные заказы. Модуль нагрузки салонов кладёт
# в ту же витрину будущие заказы всех статусов, и без явного фильтра «данные
# по такое-то число» в отчёте выплат уехало бы в будущее.
COMPLETED_STATUS = "complete"

# Условие «заказ участвует в выплате курьерам»: либо курьер указан, либо
# потрачена себестоимость доставки. Раньше этот отбор стоял на ЗАПИСИ — самовывоз
# в базу не попадал вовсе. Для показателей салонов нужны все выполненные заказы
# («Улица» это в основном самовывоз), поэтому витрина хранит всё, а отбор
# переехал сюда, в чтение. Любой новый запрос модуля выплат обязан его добавлять.
PAYOUT_FILTER = "(courier_id IS NOT NULL OR net_cost > 0)"

# Группа статусов «Отменён» в RetailCRM. Именно группа, а не один код: статусов
# отмены в справочнике несколько («Отменён», «Отменён клиентом», «Не дозвонились»),
# и отбор по одному коду молча терял бы остальные — ровно тот случай, когда
# поведение выводят из названия записи вместо данных (см. CLAUDE.md).
CANCEL_STATUS_GROUP = "cancel"

# Потолок строк в списках-исключениях отчёта («без курьера», «отменённые»).
# Это списки для разбора, а не выгрузка: сотни строк человек всё равно не
# прочитает, а читать их с диска на каждый показ страницы — лишняя работа.
DETAIL_LIST_LIMIT = 200

# ============================================================================
# Трудоёмкость заказа = база за сборку + надбавки по позициям.
#
# Базовая единица — ЗАКАЗ, а не позиция и не штука. Считать «Σ количество ×
# вес» нельзя: количество в CRM меряется в разных единицах. У букета это штуки,
# у клубники — граммы, у розы — стебли, и один сборный заказ (500 г клубники +
# 7 роз + упаковка) давал 509 единиц нагрузки вместо одной сборки. Замер на
# зеркале заказов 2026-09-07: «Клубника» — 3.7% позиций и 71% всей нагрузки,
# 6.6% заказов давали 75.5% нагрузки.
#
# Надбавка начисляется ТОЛЬКО товарам, которым вес проставлен руками
# (решение владельца 2026-09-07). Товара нет в справочнике — надбавки нет, а не
# «вес по умолчанию»: неизвестный товар не должен иметь возможности в одиночку
# съесть ёмкость дня. Справочник от этого становится необязательным — пустой
# справочник даёт честную нагрузку «в заказах».
#
# basis — за что начисляется надбавка. Отдельное поле, а не догадка по
# названию: «Виноград» и «Бананы» тоже продаются в граммах, и разбор названия
# сломался бы на них молча (CLAUDE.md, «параметр для внешней логики — данные»).
# ============================================================================
ORDER_BASE_UNITS = 1.0

# Тарифная сетка по умолчанию — таблица владельца от 2026-09-07.
# (от, до, монобукет мин/шт, микс мин/шт, только лента, упаковка)
DEFAULT_FLOWER_TARIFFS = [
    (1, 2, 1.0, None, 5.0, 10.0),
    (3, 17, 0.5, 0.6, 5.0, 10.0),
    (18, 35, 0.4, 0.6, 5.0, 12.0),
    (36, 51, 0.4, 0.6, 7.0, 15.0),
    (52, 101, 0.4, 0.6, 7.0, 20.0),
]
# (режим, минут на 100 г, упаковка на весь букет)
DEFAULT_BERRY_TARIFFS = [
    ("bouquet", 5.0, 10.0),
    ("box", 5.0, 2.0),
]

WEIGHT_BASIS_UNIT = "unit"    # вес × количество (штучный товар)
WEIGHT_BASIS_LINE = "line"    # вес за позицию, сколько бы в ней ни было
WEIGHT_BASIS_G100 = "g100"    # вес за каждые 100 единиц количества (граммы)
WEIGHT_BASES = (WEIGHT_BASIS_UNIT, WEIGHT_BASIS_LINE, WEIGHT_BASIS_G100)

# Как надбавка позиции считается в SQL. Одно место на все запросы: формула
# нужна и пересчёту весов, и разбору нагрузки на составляющие, и разойтись они
# не имеют права — иначе подпись под сеткой описывает не те числа, что в ней.
_ITEM_UNITS_SQL = f"""
    CASE w.basis
        WHEN '{WEIGHT_BASIS_LINE}' THEN w.weight
        WHEN '{WEIGHT_BASIS_G100}' THEN w.weight * i.quantity / 100.0
        ELSE w.weight * i.quantity
    END
"""

# Сколько заказов синк пропустил из-за отсутствия даты доставки. Живёт здесь,
# а не в server.py: ключ читает и диагностика витрины.
NO_DATE_ORDERS_KEY = "orders_without_delivery_date"

# Глубина окна для диагностики и для подписи «на какой момент данные».
# Считать по всей витрине нельзя: /data сетевой, и полный скан таблицы стоит
# секунды (замер 2026-09-07 — 6–9 с на /health при 2 мс на статику).
HEALTH_WINDOW_DAYS = 30

# Такси-службы: Яндекс Доставка (2), Максим Такси (12), Драйв такси (169).
# Сид для нового флага; дальше значение правится в интерфейсе.
TAXI_COURIER_IDS = (2, 12, 169)

# Коды типа доставки «Доставка курьером» в RetailCRM. Три записи с одним
# названием: две неактивные, оставшиеся от исторических заказов.
COURIER_DELIVERY_CODES = ("dostavka-kurerom", "courier", "2")

# Самовывоз. В нагрузке салона он считается отдельным счётчиком: букет всё
# равно собирает флорист (значит попадает в общий вес), но выдача — другой
# ресурс, и узкое место может оказаться на стойке, а не в цехе.
# Две записи с одним названием «Самовывоз», как и у курьерской доставки.
PICKUP_DELIVERY_CODES = ("self-delivery", "3")

# Канал «Улица» — это способ оформления offline в RetailCRM («Заказ в салоне»).
# Код, а не название: названия в справочнике переименовывают.
STREET_ORDER_METHOD = "offline"

# Сид часового пояса салона по городу — только для новых записей справочника
# (см. init_couriers_tables). Города берутся из CITY_ALIASES в retailcrm.py;
# смещения постоянные, летнего времени в России нет с 2014 года.
CITY_UTC_OFFSETS = {
    "Новосибирск": 7,
    "Томск": 7,
    "Барнаул": 7,
    "Екатеринбург": 5,
    "Челябинск": 5,
}

# Пояс салона, для которого его не задали. Ставить «по умолчанию Москву» нельзя:
# это тихо сдвинет сроки брони на 4 часа. Поэтому None означает «неизвестно», и
# такие салоны видны отдельным списком в настройках, а расчёт по ним не врёт —
# он честно отказывается считать (см. salon_time.deadline_utc).
DEFAULT_UTC_OFFSET = None


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
    # SQLite приводит регистр только у латиницы: LOWER('Роза') возвращает
    # 'Роза', и поиск по русскому названию товара молча ничего не находит.
    conn.create_function("py_lower", 1, lambda text: text.lower() if text else text,
                         deterministic=True)
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
        # Поля модуля «Загрузка салонов» (план 2026-09-04).
        #
        # store_key — склад-исполнитель (shipmentStore), заполнен у 100%
        # заказов. Это не site_code: сайт говорит, откуда пришёл заказ, а
        # собирает букет склад.
        #
        # ready_time/ready_hour — время готовности из customFields
        # .order_availability_time, в стенных часах салона. Не время доставки:
        # поля расходятся у 70% заказов.
        #
        # ready_source — из какого поля взято значение (availability /
        # delivery_from / unparsed). Первый вопрос при съехавшей сетке.
        #
        # duration_slots — задел под крупный заказ, занимающий несколько часов.
        # Логики пока нет, но добавлять колонку потом — переписывать витрину.
        #
        # weight_units — трудоёмкость заказа, посчитанная при синке (см.
        # recalc_order_weights). Считать её join'ом позиций и весов на каждый
        # показ сетки — это лишняя работа на каждом открытии экрана.
        # ====================================================================
        _add_column_if_missing(conn, "courier_orders", "store_key", "TEXT")
        _add_column_if_missing(conn, "courier_orders", "ready_time", "TEXT")
        _add_column_if_missing(conn, "courier_orders", "ready_hour", "INTEGER")
        _add_column_if_missing(conn, "courier_orders", "ready_source", "TEXT")
        _add_column_if_missing(conn, "courier_orders", "duration_slots", "INTEGER NOT NULL DEFAULT 1")
        _add_column_if_missing(conn, "courier_orders", "weight_units", "REAL")
        # Когда у заказа последний раз менялся час готовности. Без этой отметки
        # нельзя ответить, помогло ли предупреждение о перегрузе: перенос заказа
        # виден только сравнением слота между прогонами синка.
        _add_column_if_missing(conn, "courier_orders", "slot_changed_at", "TEXT")

        # ====================================================================
        # Поля карточки курьера (модуль «Курьеры: доставка заказов», Фаза 2).
        #
        # Живут здесь, а не во второй витрине: это тот же самый заказ, и второй
        # набор строк означал бы два расходящихся ответа на вопрос «что везём».
        #
        # ВНИМАНИЕ: тут персональные данные клиента (имя, телефон, адрес).
        # Наружу они отдаются только курьеру, взявшему заказ, и только по его
        # городу — отбор делает бэкенд, не фронт.
        # ====================================================================
        _add_column_if_missing(conn, "courier_orders", "address_text", "TEXT")
        _add_column_if_missing(conn, "courier_orders", "delivery_time_from", "TEXT")
        _add_column_if_missing(conn, "courier_orders", "delivery_time_to", "TEXT")
        _add_column_if_missing(conn, "courier_orders", "recipient_name", "TEXT")
        _add_column_if_missing(conn, "courier_orders", "recipient_phone", "TEXT")
        _add_column_if_missing(conn, "courier_orders", "recipient_is_customer",
                               "INTEGER NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, "courier_orders", "do_not_contact_recipient",
                               "INTEGER NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, "courier_orders", "customer_name", "TEXT")
        _add_column_if_missing(conn, "courier_orders", "customer_phone", "TEXT")
        _add_column_if_missing(conn, "courier_orders", "manager_comment", "TEXT")
        _add_column_if_missing(conn, "courier_orders", "customer_comment", "TEXT")
        _add_column_if_missing(conn, "courier_orders", "note_text", "TEXT")
        _add_column_if_missing(conn, "courier_orders", "ready_planned_at", "TEXT")

        # Минуты сборки и их разбор (Ф3). Разбор хранится колонками, а не
        # считается на показ: «почему здесь 48 минут» спрашивают у ячейки, а
        # пересчитывать состав заказа на каждый клик по слоту — лишняя работа
        # на медленном диске. items_without_norm > 0 означает, что заказ
        # посчитан не полностью, и это должно быть видно на экране.
        for column in ("minutes_total", "minutes_flowers", "minutes_packaging",
                       "minutes_berries", "minutes_catalog"):
            _add_column_if_missing(conn, "courier_orders", column, "REAL")
        _add_column_if_missing(conn, "courier_orders", "items_without_norm",
                               "INTEGER NOT NULL DEFAULT 0")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_courier_orders_slot "
            "ON courier_orders(delivery_date, store_key, ready_hour)"
        )

        # Позиции заказа. delivery_date дублируется намеренно: окно витрины
        # чистится через DELETE по дате доставки, и без этого поля позиции
        # отменённых заказов остались бы навсегда, а вес слота рос бы сам.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS order_items (
                retailcrm_order_id INTEGER NOT NULL,
                offer_id INTEGER NOT NULL,
                delivery_date TEXT NOT NULL,
                product_name TEXT,
                article TEXT,
                quantity REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (retailcrm_order_id, offer_id)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_order_items_date ON order_items(delivery_date)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_order_items_offer ON order_items(offer_id)"
        )

        # Справочник надбавок за трудоёмкость. Ключ — offer.id (внутренний
        # идентификатор CRM): заполнен у 100% позиций и не меняется при
        # переименовании товара. Строки нет — надбавки нет (заказ считается
        # базой за сборку), поэтому справочник необязателен к заполнению.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS product_weights (
                offer_id INTEGER PRIMARY KEY,
                weight REAL NOT NULL,
                set_by TEXT,
                set_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        # basis появился 2026-09-07 вместе с базой за заказ. Существующим
        # строкам достаётся 'unit' — ровно та семантика, в которой их заводили
        # («вес × количество»), а не «наиболее вероятная».
        _add_column_if_missing(conn, "product_weights", "basis",
                               f"TEXT NOT NULL DEFAULT '{WEIGHT_BASIS_UNIT}'")

        # ====================================================================
        # Каталог номенклатуры RetailCRM (Ф1 плана «нагрузка в минутах»).
        #
        # Зачем он здесь, если модуль и так знает offer_id из позиций заказа:
        #   - `unit_code` отвечает на вопрос «в чём меряется количество»
        #     (`pc` у букета, `g` у клубники) — данными, а не разбором названия;
        #   - группы дают способ задать норму времени пачкой, а не 456 полями
        #     руками.
        #
        # Всё наполняется синком и руками не правится: норма времени лежит
        # отдельно (Ф2), чтобы синк никогда не затирал решения человека.
        # ====================================================================
        conn.execute("""
            CREATE TABLE IF NOT EXISTS crm_product_groups (
                id INTEGER PRIMARY KEY,
                parent_id INTEGER,
                name TEXT NOT NULL,
                -- Глубина в дереве. Считается при синке вторым проходом и
                -- хранится числом: правило «глубже значит точнее» иначе
                -- превращалось бы в обход дерева на каждый расчёт.
                depth INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                synced_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)

        # Торговое предложение — то, чем заказ ссылается на товар (offer.id).
        # Ключ именно оффер, а не товар: в позиции заказа приходит он.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS crm_offers (
                offer_id INTEGER PRIMARY KEY,
                product_id INTEGER,
                article TEXT,
                name TEXT,
                unit_code TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                synced_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_crm_offers_unit ON crm_offers(unit_code)"
        )

        # Связь «оффер → группы». Товар состоит в среднем в 20 группах
        # (витринные вперемешку с товарными), поэтому это именно многие-ко-многим,
        # а не колонка group_id у оффера.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS crm_offer_groups (
                offer_id INTEGER NOT NULL,
                group_id INTEGER NOT NULL,
                PRIMARY KEY (offer_id, group_id)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_crm_offer_groups_group "
            "ON crm_offer_groups(group_id)"
        )

        # ====================================================================
        # Нормы времени сборки (Ф2 плана «нагрузка в минутах»).
        #
        # Ключ составной: `scope` = 'group' или 'offer'. Норма на группу
        # закрывает пачку товаров (26 записей вместо 456), норма на товар —
        # точечное исключение, и она перебивает групповую.
        #
        # Роль и минуты — РАЗНЫЕ поля. У компонента-цветка своих минут нет:
        # его время даёт тарифная сетка по количеству, а роль лишь говорит,
        # в какой счётчик его класть.
        #
        # Таблица правится только человеком. Синк её не трогает никогда:
        # каталог приходит извне, а решение «сколько это стоит по времени» —
        # наше, и перетереть его обновлением справочника нельзя.
        # ====================================================================
        # ====================================================================
        # Тарифная сетка: время сборки от количества (Ф3).
        #
        # В БАЗЕ, а не константами в коде: в первой же редакции таблицы,
        # присланной владельцем, была опечатка (15 минут на 100 г клубники
        # вместо 5), и правка тарифа не должна стоить деплоя.
        #
        # mix_minutes = NULL у диапазона 1–2: из одного-двух цветков «букета из
        # разного цветка» не бывает, и выдумывать для него тариф нельзя.
        # ====================================================================
        conn.execute("""
            CREATE TABLE IF NOT EXISTS load_tariff_flowers (
                range_from INTEGER PRIMARY KEY,
                range_to INTEGER NOT NULL,
                mono_minutes REAL NOT NULL,
                mix_minutes REAL,
                ribbon_minutes REAL NOT NULL,
                package_minutes REAL NOT NULL,
                updated_by TEXT,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS load_tariff_berries (
                mode TEXT PRIMARY KEY,
                minutes_per_100g REAL NOT NULL,
                package_minutes REAL NOT NULL,
                updated_by TEXT,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        # Сидирование значениями владельца (таблица от 2026-09-07 с учётом
        # исправления по клубнике). INSERT OR IGNORE: правку человека
        # перезапуск воркера перетирать не должен.
        for row in DEFAULT_FLOWER_TARIFFS:
            conn.execute(
                "INSERT OR IGNORE INTO load_tariff_flowers "
                "(range_from, range_to, mono_minutes, mix_minutes, ribbon_minutes, package_minutes) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                row,
            )
        for row in DEFAULT_BERRY_TARIFFS:
            conn.execute(
                "INSERT OR IGNORE INTO load_tariff_berries "
                "(mode, minutes_per_100g, package_minutes) VALUES (?, ?, ?)",
                row,
            )

        conn.execute("""
            CREATE TABLE IF NOT EXISTS load_time_norms (
                scope TEXT NOT NULL,
                scope_id INTEGER NOT NULL,
                role TEXT,
                minutes REAL,
                basis TEXT,
                berry_mode TEXT,
                updated_by TEXT,
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (scope, scope_id)
            )
        """)

        # Справочник статусов: counts_as_load решает, попадает ли заказ в
        # нагрузку. Сидируется из группы CRM (cancel → не нагрузка), дальше
        # правится руками и синком НЕ перетирается — какой статус считать
        # работой, решает человек, а не название записи.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS order_statuses (
                code TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                group_code TEXT,
                counts_as_load INTEGER NOT NULL DEFAULT 1,
                active INTEGER NOT NULL DEFAULT 1,
                reviewed INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)

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
        # Часовой пояс салона — смещение от UTC в часах.
        #
        # Зачем: время в CRM (доставка, готовность) — это стенные часы салона,
        # а в наших базах всё в UTC. Любое правило вида «за 60 минут до окна
        # доставки» без пояса едет на два часа у половины сети: салоны живут
        # в UTC+5 (Екатеринбург, Челябинск) и UTC+7 (Новосибирск, Томск,
        # Барнаул). Замер 2026-09-08 это подтвердил на дисциплине статуса
        # «Заказ готов»: без поправки цифры по двум поясам расходились вдвое.
        #
        # Почему число, а не идентификатор зоны: Россия не переходит на летнее
        # время с 2014 года, смещения постоянные, а zoneinfo на Windows требует
        # отдельного пакета tzdata — лишняя зависимость ради константы.
        #
        # Почему поле, а не словарь городов в коде: пояс уходит в расчёт срока
        # брони, то есть ведёт себя как параметр внешнего мира (см. CLAUDE.md
        # про НДС). Город здесь только СИДИРУЕТ значение при первом появлении
        # салона; дальше правится руками и синком не перетирается.
        # ====================================================================
        _add_column_if_missing(conn, "courier_sites", "utc_offset", "INTEGER")
        for city, offset in CITY_UTC_OFFSETS.items():
            conn.execute(
                "UPDATE courier_sites SET utc_offset = ? "
                "WHERE city = ? AND utc_offset IS NULL",
                (offset, city),
            )

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

    _backfill_weight_model()
    _expand_group_norms()


# Разовый пересчёт нагрузки под модель «база за заказ + надбавки».
#
# Курсор, а не флаг «сделано»: пересчёт идёт кусками по датам, и каждый
# завершённый кусок сохраняется. Одним `UPDATE` по всей таблице делать нельзя —
# он держал бы write-лок общей базы на всё время работы прямо на старте
# воркера, а на `/data` это секунды, в которые встают и логин, и любая запись
# (тот же класс, что «фоновый синк кладёт весь сайт»).
WEIGHT_MODEL_KEY = "load_weight_model"
WEIGHT_MODEL_VERSION = "order_base_v1"
WEIGHT_MODEL_CURSOR_KEY = "load_weight_model_cursor"
WEIGHT_MODEL_CHUNK_DAYS = 30


def _backfill_weight_model() -> None:
    """
    Разово пересчитать нагрузку уже накопленных заказов под новую формулу.

    Без этого старые числа («Σ количество × вес», где 500 г клубники давали
    500 единиц) жили бы в витрине до следующего глубокого синка — то есть до
    суток. За это время «Сколько собирали на самом деле» предложило бы норму
    ёмкости, посчитанную по граммам, и её бы приняли.

    Три свойства, без которых это опасно запускать на старте воркера:

      - **кусками по датам** — короткая транзакция вместо одной длинной, между
        кусками лок отпускается, и соседние записи проходят;
      - **возобновляемо** — курсор сохраняется после каждого куска, поэтому
        обрыв на середине не заставляет начинать заново (а без курсора любой
        таймаут означал бы повтор всей тяжёлой работы при каждом рестарте);
      - **проверка курсора и запись — одна транзакция** под `BEGIN IMMEDIATE`:
        воркеры стартуют одновременно, и без write-лока оба взяли бы один и
        тот же кусок.

    Падать здесь нельзя ни при каких обстоятельствах — это старт воркера.
    """
    while True:
        conn = sqlite_connect(DB_PATH, timeout=30)
        conn.isolation_level = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            state = {
                row["key"]: row["value"]
                for row in conn.execute(
                    "SELECT key, value FROM sync_state WHERE key IN (?, ?)",
                    (WEIGHT_MODEL_KEY, WEIGHT_MODEL_CURSOR_KEY),
                )
            }
            if state.get(WEIGHT_MODEL_KEY) == WEIGHT_MODEL_VERSION:
                conn.execute("ROLLBACK")
                return

            bounds = conn.execute(
                "SELECT MIN(delivery_date) AS lo, MAX(delivery_date) AS hi FROM courier_orders"
            ).fetchone()
            start = state.get(WEIGHT_MODEL_CURSOR_KEY) or bounds["lo"]
            if start is None or bounds["hi"] is None or start > bounds["hi"]:
                # Витрина пуста или куски кончились — пересчитывать нечего.
                _mark_weight_model_done(conn)
                conn.execute("COMMIT")
                logger.info("Нагрузка пересчитана под модель «база за заказ + надбавки»")
                return

            chunk_end = (datetime.strptime(start, "%Y-%m-%d").date()
                         + timedelta(days=WEIGHT_MODEL_CHUNK_DAYS)).isoformat()
            conn.execute(
                f"""
                UPDATE courier_orders
                   SET weight_units = ? + COALESCE((
                           SELECT SUM({_ITEM_UNITS_SQL})
                             FROM order_items i
                             JOIN product_weights w ON w.offer_id = i.offer_id
                            WHERE i.retailcrm_order_id = courier_orders.retailcrm_order_id
                              AND i.delivery_date = courier_orders.delivery_date
                       ), 0)
                 WHERE delivery_date >= ? AND delivery_date < ?
                """,
                (ORDER_BASE_UNITS, start, chunk_end),
            )
            if chunk_end > bounds["hi"]:
                _mark_weight_model_done(conn)
                conn.execute("COMMIT")
                logger.info("Нагрузка пересчитана под модель «база за заказ + надбавки»")
                return

            _set_sync_state(conn, WEIGHT_MODEL_CURSOR_KEY, chunk_end)
            conn.execute("COMMIT")
            logger.info(f"Пересчёт нагрузки: {start}—{chunk_end} готов")
        except Exception as e:
            logger.warning(f"Пересчёт нагрузки под новую модель отложен: {e}")
            return
        finally:
            conn.close()


GROUP_NORMS_EXPANDED_KEY = "load_group_norms_expanded"


def _expand_group_norms() -> None:
    """
    Развернуть групповые нормы в товарные и убрать групповые.

    Групповые нормы отменены владельцем 2026-09-09: в одной группе CRM лежат
    товары с сильно разным временем сборки. Но то, что уже размечено, — это
    проделанная человеком работа, и стирать её нельзя: разворачиваем каждую
    групповую норму в нормы её товаров.

    Товар, у которого уже есть собственная норма, не трогаем: она точнее
    групповой, ради неё исключения и заводили.

    Порядок разворачивания важен: товар состоит в нескольких группах, и
    выигрывает та, что выигрывала раньше, — бóльшая глубина, при равной
    меньший id. Иначе после миграции числа поехали бы, а человек считал бы,
    что просто «убрали группы».

    Падать нельзя — это старт воркера.
    """
    conn = sqlite_connect(DB_PATH, timeout=30)
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        done = conn.execute("SELECT value FROM sync_state WHERE key = ?",
                            (GROUP_NORMS_EXPANDED_KEY,)).fetchone()
        if done and done["value"] == "1":
            conn.execute("ROLLBACK")
            return

        rows = conn.execute(
            """
            SELECT og.offer_id, n.role, n.minutes, n.basis, n.berry_mode
              FROM crm_offer_groups og
              JOIN crm_product_groups g ON g.id = og.group_id
              JOIN load_time_norms n ON n.scope = 'group' AND n.scope_id = og.group_id
         LEFT JOIN load_time_norms own ON own.scope = 'offer' AND own.scope_id = og.offer_id
             WHERE own.scope_id IS NULL
             ORDER BY og.offer_id, g.depth DESC, g.id ASC
            """
        ).fetchall()

        seen = set()
        expanded = []
        for row in rows:
            if row["offer_id"] in seen:
                continue
            seen.add(row["offer_id"])
            expanded.append((SCOPE_OFFER, row["offer_id"], row["role"], row["minutes"],
                             row["basis"], row["berry_mode"], "перенос с группы"))

        if expanded:
            conn.executemany(
                "INSERT OR IGNORE INTO load_time_norms "
                "(scope, scope_id, role, minutes, basis, berry_mode, updated_by, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))",
                expanded,
            )
        conn.execute("DELETE FROM load_time_norms WHERE scope = 'group'")
        _set_sync_state(conn, GROUP_NORMS_EXPANDED_KEY, "1")
        conn.execute("COMMIT")
        if expanded:
            logger.info(f"Групповые нормы развёрнуты в товарные: {len(expanded)} товаров")
    except Exception as e:
        logger.warning(f"Перенос групповых норм отложен: {e}")
    finally:
        conn.close()


def _set_sync_state(conn, key: str, value: str) -> None:
    """Запись служебного ключа ЧУЖИМ соединением — внутри уже открытой транзакции."""
    conn.execute(
        "INSERT INTO sync_state (key, value, updated_at) VALUES (?, ?, datetime('now')) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = datetime('now')",
        (key, value),
    )


def _mark_weight_model_done(conn) -> None:
    _set_sync_state(conn, WEIGHT_MODEL_KEY, WEIGHT_MODEL_VERSION)
    conn.execute("DELETE FROM sync_state WHERE key = ?", (WEIGHT_MODEL_CURSOR_KEY,))


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
        # Часы готовности до пересборки: окно переписывается целиком, поэтому
        # «заказ переехал в другой слот» видно только так. Без этого нельзя
        # ответить, помогло ли предупреждение о перегрузе (пункт 7.7 плана).
        previous = {
            row["retailcrm_order_id"]: (row["ready_hour"], row["slot_changed_at"])
            for row in conn.execute(
                "SELECT retailcrm_order_id, ready_hour, slot_changed_at FROM courier_orders "
                "WHERE delivery_date >= ? AND delivery_date <= ?",
                (date_from, date_to),
            )
        }

        # Позиции чистим ДО заказов и по своей дате доставки: связь по
        # retailcrm_order_id тут не поможет — удаляемых заказов после DELETE
        # уже не найти, и позиции отменённых заказов остались бы навсегда.
        conn.execute(
            "DELETE FROM order_items WHERE delivery_date >= ? AND delivery_date <= ?",
            (date_from, date_to),
        )
        # И позиции самих перезаливаемых заказов — по идентификатору. Заказ мог
        # переехать в это окно с даты, которая сейчас не пересобирается: его
        # старые позиции лежат под чужой датой и удалением по периоду не
        # ловятся, а вес слота от них растёт.
        if rows:
            ids = [row["retailcrm_order_id"] for row in rows]
            for start in range(0, len(ids), 400):   # потолок переменных SQLite
                chunk = ids[start:start + 400]
                conn.execute(
                    f"DELETE FROM order_items WHERE retailcrm_order_id IN "
                    f"({','.join('?' * len(chunk))})",
                    chunk,
                )
        conn.execute(
            "DELETE FROM courier_orders WHERE delivery_date >= ? AND delivery_date <= ?",
            (date_from, date_to),
        )
        conn.executemany(
            """
            INSERT OR REPLACE INTO courier_orders (
                retailcrm_order_id, order_number, delivery_date, courier_id, courier_name,
                net_cost, site_code, city, delivery_city, status,
                total_summ, order_method, delivery_code,
                store_key, ready_time, ready_hour, ready_source,
                address_text, delivery_time_from, delivery_time_to,
                recipient_name, recipient_phone, recipient_is_customer,
                do_not_contact_recipient, customer_name, customer_phone,
                manager_comment, customer_comment, note_text, ready_planned_at,
                synced_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
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
                    row.get("store_key"),
                    row.get("ready_time"),
                    row.get("ready_hour"),
                    row.get("ready_source"),
                    # Карточка курьера. Глубокий синк переписывает окно
                    # целиком, поэтому эти поля обязаны быть и здесь: иначе
                    # адрес и телефон обнулялись бы каждые полчаса, а лента
                    # изменений возвращала бы их только при следующей правке
                    # заказа в CRM.
                    row.get("address_text"),
                    row.get("delivery_time_from"),
                    row.get("delivery_time_to"),
                    row.get("recipient_name"),
                    row.get("recipient_phone"),
                    int(row.get("recipient_is_customer") or 0),
                    int(row.get("do_not_contact_recipient") or 0),
                    row.get("customer_name"),
                    row.get("customer_phone"),
                    row.get("manager_comment"),
                    row.get("customer_comment"),
                    row.get("note_text"),
                    row.get("ready_planned_at"),
                )
                for row in rows
            ],
        )
        conn.executemany(
            """
            INSERT OR REPLACE INTO order_items (
                retailcrm_order_id, offer_id, delivery_date, product_name, article, quantity
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    row["retailcrm_order_id"],
                    item["offer_id"],
                    row["delivery_date"],
                    item.get("product_name"),
                    item.get("article"),
                    float(item.get("quantity") or 0),
                )
                for row in rows
                for item in (row.get("items") or [])
            ],
        )

        # Перенос слота: час был и стал другим. Заказ, которого раньше не было,
        # переносом не считается — это новый заказ, а не разгрузка.
        moved = []
        for row in rows:
            before = previous.get(row["retailcrm_order_id"])
            if before is None:
                continue
            old_hour, changed_at = before
            if old_hour == row.get("ready_hour"):
                # Час не менялся — сохраняем прежнюю отметку, иначе она
                # обнулялась бы при каждой пересборке окна.
                if changed_at:
                    moved.append((changed_at, row["retailcrm_order_id"]))
                continue
            moved.append((datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                          row["retailcrm_order_id"]))
        if moved:
            conn.executemany(
                "UPDATE courier_orders SET slot_changed_at = ? WHERE retailcrm_order_id = ?",
                moved,
            )

        _recalc_weights(conn, date_from, date_to)

    # Минуты считаются ПОСЛЕ закрытия транзакции записи: расчёт читает нормы и
    # тарифы, то есть открывает своё соединение. Сделать это внутри — тот самый
    # вложенный коннект поверх незакрытой записи, которым уже вешали базу.
    #
    # Ошибка расчёта не должна отменять уже записанные заказы: витрина важнее
    # производной от неё величины, а минуты досчитаются следующим прогоном.
    try:
        recalc_minutes_range(date_from, date_to)
    except Exception as e:
        logger.error(f"Пересчёт минут за {date_from}—{date_to} не удался: {e}")

    return len(rows)


def _recalc_weights(conn, date_from: str, date_to: str) -> None:
    """
    Пересчитать трудоёмкость заказов за окно одним запросом.

    Считается при синке и хранится числом: собирать её join'ом позиций и весов
    на каждый показ сетки — лишняя работа на каждом открытии экрана, а диск
    /data и без того медленный.

    Формула: база за сборку + надбавки тех позиций, которым вес проставлен
    руками. JOIN, а не LEFT JOIN, — это и есть «товара нет в справочнике,
    надбавки нет». Заказ без позиций получает базу: это заказ, работа по нему
    есть, просто она не разложена по номенклатуре.
    """
    conn.execute(
        f"""
        UPDATE courier_orders
           SET weight_units = ? + COALESCE((
                   SELECT SUM({_ITEM_UNITS_SQL})
                     FROM order_items i
                     JOIN product_weights w ON w.offer_id = i.offer_id
                    WHERE i.retailcrm_order_id = courier_orders.retailcrm_order_id
                      -- Сверяем и дату: заказ мог переехать на другой день, а
                      -- его старые позиции остаться в неперезалитом окне.
                      -- Без этого условия вес слота тихо задваивался бы.
                      AND i.delivery_date = courier_orders.delivery_date
               ), 0)
         WHERE delivery_date >= ? AND delivery_date <= ?
        """,
        (ORDER_BASE_UNITS, date_from, date_to),
    )


def recalc_weights_range(date_from: str, date_to: str) -> None:
    """
    Пересчёт весов за период отдельным вызовом — для смены веса товара.

    Правка веса в справочнике меняет нагрузку задним числом, и без пересчёта
    экран показывал бы старые числа до следующего синка.
    """
    with get_db() as conn:
        _recalc_weights(conn, date_from, date_to)


def list_weight_catalog(date_from: str, date_to: str, only_missing: bool = False,
                        search: Optional[str] = None, limit: int = 300) -> List[Dict[str, Any]]:
    """
    Товары, встреченные в заказах за период, с их надбавкой за трудоёмкость.

    Сортировка по числу заказов, а не по алфавиту: надбавки нужно ставить
    начиная с того, что реально влияет на нагрузку, — иначе человек уходит в
    хвост справочника и бросает на середине.

    only_missing=True — только те, у кого надбавки нет.

    per_order (среднее количество на заказ) отдаётся не для красоты: по нему
    видно, в чём меряется количество. 480 «штук» на заказ — это граммы, и
    надбавку такому товару надо ставить за 100 г, а не за штуку. Без этой
    подсказки базу начисления выбирают наугад.
    """
    where = ["i.delivery_date >= ?", "i.delivery_date <= ?"]
    params: List[Any] = [date_from, date_to]
    if search:
        where.append("(py_lower(i.product_name) LIKE ? OR py_lower(COALESCE(i.article, '')) LIKE ?)")
        pattern = f"%{search.lower()}%"
        params.extend([pattern, pattern])
    if only_missing:
        where.append("w.offer_id IS NULL")

    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT i.offer_id,
                   MAX(i.product_name)              AS product_name,
                   MAX(i.article)                   AS article,
                   COUNT(DISTINCT i.retailcrm_order_id) AS orders,
                   COALESCE(SUM(i.quantity), 0)     AS quantity,
                   w.weight                         AS weight,
                   w.basis                          AS basis
              FROM order_items i
         LEFT JOIN product_weights w ON w.offer_id = i.offer_id
             WHERE {' AND '.join(where)}
          GROUP BY i.offer_id, w.weight, w.basis
          ORDER BY orders DESC, quantity DESC
             LIMIT ?
            """,
            (*params, limit),
        ).fetchall()

    return [
        {
            "offer_id": row["offer_id"],
            "product_name": row["product_name"],
            "article": row["article"],
            "orders": row["orders"],
            "quantity": round(row["quantity"] or 0, 2),
            "per_order": round((row["quantity"] or 0) / row["orders"], 1) if row["orders"] else 0,
            "weight": row["weight"],
            "basis": row["basis"] or WEIGHT_BASIS_UNIT,
        }
        for row in rows
    ]


def set_product_weights(weights: Dict[int, Any], username: Optional[str] = None) -> int:
    """
    Проставить надбавки пачкой. Значение None снимает надбавку — товар
    перестаёт добавлять что-либо к базе за сборку.

    Значение — либо число (база «за штуку», совместимость со старым вызовом),
    либо `{"weight": 0.2, "basis": "g100"}`.

    Пачкой, а не по одному: проставлять надбавку сотне товаров поштучно — тот
    самый ручной труд, ради устранения которого модуль и делается.
    """
    if not weights:
        return 0

    to_set = []
    to_clear = []
    for offer_id, value in weights.items():
        if value is None:
            to_clear.append((offer_id,))
            continue
        if isinstance(value, dict):
            weight = float(value.get("weight"))
            basis = value.get("basis") or WEIGHT_BASIS_UNIT
        else:
            weight, basis = float(value), WEIGHT_BASIS_UNIT
        if weight <= 0:
            raise ValueError("Надбавка должна быть больше нуля")
        if basis not in WEIGHT_BASES:
            raise ValueError(f"Неизвестная база начисления: {basis}")
        to_set.append((offer_id, weight, basis, username))

    with get_db() as conn:
        if to_set:
            conn.executemany(
                """
                INSERT INTO product_weights (offer_id, weight, basis, set_by, set_at)
                VALUES (?, ?, ?, ?, datetime('now'))
                ON CONFLICT(offer_id) DO UPDATE SET
                    weight = excluded.weight, basis = excluded.basis,
                    set_by = excluded.set_by, set_at = datetime('now')
                """,
                to_set,
            )
        if to_clear:
            conn.executemany("DELETE FROM product_weights WHERE offer_id = ?", to_clear)
    return len(weights)


def weights_coverage(date_from: str, date_to: str,
                     load_statuses: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    Из чего сложилась нагрузка: база за заказы и надбавки по позициям.

    Это разбор, а не предупреждение: приблизительных процентов больше нет —
    надбавка либо проставлена руками, либо её нет. Но вопрос «почему в этом
    часе 12 единиц на 5 заказов» возникает сразу, и отвечать на него чтением
    кода — потерянный час.

    Считается по тем же заказам, что и сама сетка: витрина хранит все статусы,
    и без фильтра разбор описывал бы другую совокупность, чем проценты, которые
    он поясняет.
    """
    statuses = load_statuses if load_statuses is not None else load_status_codes()
    empty = {"base_units": 0.0, "extra_units": 0.0, "total_units": 0.0, "extra_share": 0.0,
             "orders": 0, "products_weighted": 0, "products_total": 0,
             "order_base": ORDER_BASE_UNITS}
    if not statuses:
        return empty

    placeholders = ",".join("?" * len(statuses))
    with get_db() as conn:
        orders = conn.execute(
            f"""
            SELECT COUNT(*) AS orders
              FROM courier_orders
             WHERE delivery_date >= ? AND delivery_date <= ?
               AND status IN ({placeholders})
            """,
            (date_from, date_to, *statuses),
        ).fetchone()["orders"] or 0

        row = conn.execute(
            f"""
            SELECT COALESCE(SUM({_ITEM_UNITS_SQL}), 0) AS extra_units,
                   COUNT(DISTINCT w.offer_id) AS products_weighted,
                   COUNT(DISTINCT i.offer_id) AS products_total
              FROM order_items i
              JOIN courier_orders o ON o.retailcrm_order_id = i.retailcrm_order_id
                                   AND o.delivery_date = i.delivery_date
         LEFT JOIN product_weights w ON w.offer_id = i.offer_id
             WHERE i.delivery_date >= ? AND i.delivery_date <= ?
               AND o.status IN ({placeholders})
            """,
            (date_from, date_to, *statuses),
        ).fetchone()

    base = orders * ORDER_BASE_UNITS
    extra = row["extra_units"] or 0
    total = base + extra
    return {
        "base_units": round(base, 2),
        "extra_units": round(extra, 2),
        "total_units": round(total, 2),
        "extra_share": round(100.0 * extra / total, 1) if total else 0.0,
        "orders": orders,
        "products_weighted": row["products_weighted"] or 0,
        "products_total": row["products_total"] or 0,
        "order_base": ORDER_BASE_UNITS,
    }


# ============================================================================
# Каталог номенклатуры
# ============================================================================

class EmptyCatalogError(Exception):
    """CRM вернула пустой каталог. Это ошибка синка, а не «товаров нет»."""


def replace_catalog(groups: List[Dict[str, Any]], offers: List[Dict[str, Any]],
                    offer_groups: List[tuple]) -> Dict[str, int]:
    """
    Переписать каталог целиком: группы, офферы и связи между ними.

    Пересборка, а не UPSERT: товар мог сменить группы или уйти в архив, и при
    UPSERT старые связи остались бы навсегда — норма времени бралась бы от
    группы, в которой товара уже нет.

    **Пустой ответ CRM ничего не перезаписывает.** Одна неудачная
    синхронизация (сеть, тайм-аут, смена ключа) иначе обнулила бы каталог, а
    следом — и нагрузку по всей сети: без единиц измерения и групп каждая
    позиция уходит в «без нормы». Пустота — это исключение, а не ноль.

    Всё в одной транзакции: между `DELETE` и `INSERT` не должно быть момента,
    когда расчёт видит половину каталога.
    """
    if not groups or not offers:
        raise EmptyCatalogError(
            f"CRM вернула пустой каталог (групп: {len(groups)}, офферов: {len(offers)}) — "
            f"каталог не тронут"
        )

    depths = _group_depths(groups)

    with get_db() as conn:
        conn.execute("DELETE FROM crm_offer_groups")
        conn.execute("DELETE FROM crm_offers")
        conn.execute("DELETE FROM crm_product_groups")

        conn.executemany(
            "INSERT INTO crm_product_groups (id, parent_id, name, depth, active, synced_at) "
            "VALUES (?, ?, ?, ?, ?, datetime('now'))",
            [(g["id"], g.get("parent_id"), g["name"], depths.get(g["id"], 0),
              1 if g.get("active", True) else 0)
             for g in groups],
        )
        conn.executemany(
            "INSERT INTO crm_offers (offer_id, product_id, article, name, unit_code, active, synced_at) "
            "VALUES (?, ?, ?, ?, ?, ?, datetime('now'))",
            [(o["offer_id"], o.get("product_id"), o.get("article"), o.get("name"),
              o.get("unit_code"), 1 if o.get("active", True) else 0)
             for o in offers],
        )
        conn.executemany(
            "INSERT OR IGNORE INTO crm_offer_groups (offer_id, group_id) VALUES (?, ?)",
            offer_groups,
        )

    return {"groups": len(groups), "offers": len(offers), "links": len(offer_groups)}


def _group_depths(groups: List[Dict[str, Any]]) -> Dict[int, int]:
    """
    Глубина каждой группы в дереве.

    Вторым проходом, а не по ходу вставки: родитель может прийти в ответе
    ПОСЛЕ ребёнка, и наивный расчёт «глубина родителя + 1» дал бы ноль у
    половины дерева — а от глубины зависит, чья норма выиграет.

    Битая ссылка на несуществующего родителя и цикл не роняют синк: такая
    группа считается корневой. Уронить синк из-за кривого справочника — значит
    остаться вообще без каталога.
    """
    parents = {g["id"]: g.get("parent_id") for g in groups}
    depths: Dict[int, int] = {}

    for group_id in parents:
        depth = 0
        seen = {group_id}
        current = parents.get(group_id)
        while current is not None and current in parents and current not in seen:
            seen.add(current)
            depth += 1
            current = parents.get(current)
        depths[group_id] = depth
    return depths


def catalog_counts(conn) -> Dict[str, Any]:
    """
    Счётчики каталога ЧУЖИМ соединением.

    Отдельная функция ради `health_snapshot`: он собирает диагностику одним
    соединением, и вызвать оттуда `catalog_snapshot()` значило бы открыть
    второе поверх первого — тот самый вложенный коннект, которым уже вешали
    базу. Копия того же SQL разошлась бы с оригиналом при первой же правке.
    """
    row = conn.execute("""
        SELECT (SELECT COUNT(*) FROM crm_product_groups)               AS groups,
               (SELECT COUNT(*) FROM crm_offers)                       AS offers,
               (SELECT COUNT(*) FROM crm_offers WHERE unit_code = 'g') AS offers_weighted,
               (SELECT COUNT(*) FROM crm_offer_groups)                 AS links,
               (SELECT MAX(synced_at) FROM crm_offers)                 AS synced_at
    """).fetchone()
    return {
        "groups": row["groups"],
        "offers": row["offers"],
        "offers_weighted": row["offers_weighted"],
        "links": row["links"],
        "synced_at": row["synced_at"],
    }


def catalog_snapshot() -> Dict[str, Any]:
    """Состояние каталога: пустой каталог обнуляет нагрузку молча."""
    with get_db() as conn:
        return catalog_counts(conn)


def offer_units(offer_ids: Optional[List[int]] = None) -> Dict[int, str]:
    """Единица измерения по офферам. Пусто — каталог ещё не синкали."""
    with get_db() as conn:
        if offer_ids:
            placeholders = ",".join("?" * len(offer_ids))
            rows = conn.execute(
                f"SELECT offer_id, unit_code FROM crm_offers "
                f"WHERE unit_code IS NOT NULL AND offer_id IN ({placeholders})",
                offer_ids,
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT offer_id, unit_code FROM crm_offers WHERE unit_code IS NOT NULL"
            ).fetchall()
    return {row["offer_id"]: row["unit_code"] for row in rows}


# ============================================================================
# Нормы времени сборки
# ============================================================================

# Роль позиции в расчёте. Не «тип товара вообще», а именно роль в формуле:
# один и тот же цветок бывает и компонентом сборного букета, и готовым
# монобукетом — решает разметка, а не природа вещи.
ROLE_CATALOG = "catalog"      # готовый товар: время из нормы
ROLE_FLOWER = "flower"        # компонент-цветок: в счётчик цветов, время даёт тариф
ROLE_BERRY = "berry"          # весовой компонент: время по тарифу за 100 г
ROLE_PACKAGING = "packaging"  # упаковка: включает режим «Упаковка» вместо «Лента»
ROLE_NONE = "none"            # не создаёт нагрузки (открытки, топперы, шапки)
ROLES = (ROLE_CATALOG, ROLE_FLOWER, ROLE_BERRY, ROLE_PACKAGING, ROLE_NONE)

# База начисления для готового товара. Нужна и здесь, а не только у надбавок:
# «Секрет Бархата» заведён в граммах и заказывается штуками (267 заказов,
# медиана количества — 1). Умножение его времени на количество даёт ошибку в
# сотни раз — ровно ту, ради которой модель и переделывается.
BASIS_UNIT = "unit"   # время × количество
BASIS_LINE = "line"   # время один раз за позицию, сколько бы в ней ни было
BASES = (BASIS_UNIT, BASIS_LINE)

# Режим клубники: от него зависит только упаковка (10 мин против 2).
BERRY_BOUQUET = "bouquet"
BERRY_BOX = "box"
BERRY_MODES = (BERRY_BOUQUET, BERRY_BOX)

SCOPE_GROUP = "group"
SCOPE_OFFER = "offer"
SCOPES = (SCOPE_GROUP, SCOPE_OFFER)


def set_time_norm(scope: str, scope_id: int, role: Optional[str] = None,
                  minutes: Optional[float] = None, basis: Optional[str] = None,
                  berry_mode: Optional[str] = None,
                  username: Optional[str] = None) -> None:
    """
    Задать норму группе или товару. role=None удаляет запись целиком.

    Ноль минут — законное значение (`role='none'` у открытки), поэтому «нормы
    нет» выражается ОТСУТСТВИЕМ строки, а не нулём: иначе «не размечено» и
    «размечено как бесплатное» станут одним и тем же, и счётчик занижения
    замолчит.
    """
    if scope not in SCOPES:
        raise ValueError(f"Неизвестная область нормы: {scope}")

    if role is None:
        with get_db() as conn:
            conn.execute("DELETE FROM load_time_norms WHERE scope = ? AND scope_id = ?",
                         (scope, int(scope_id)))
        return

    if role not in ROLES:
        raise ValueError(f"Неизвестная роль: {role}")
    if minutes is not None and minutes < 0:
        raise ValueError("Время не может быть отрицательным")
    if role == ROLE_CATALOG and minutes is None:
        raise ValueError("У готового товара должно быть задано время")
    if basis is not None and basis not in BASES:
        raise ValueError(f"Неизвестная база начисления: {basis}")
    if berry_mode is not None and berry_mode not in BERRY_MODES:
        raise ValueError(f"Неизвестный режим клубники: {berry_mode}")

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO load_time_norms
                   (scope, scope_id, role, minutes, basis, berry_mode, updated_by, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(scope, scope_id) DO UPDATE SET
                role = excluded.role, minutes = excluded.minutes,
                basis = excluded.basis, berry_mode = excluded.berry_mode,
                updated_by = excluded.updated_by, updated_at = datetime('now')
            """,
            (scope, int(scope_id), role,
             None if minutes is None else float(minutes),
             basis or (BASIS_UNIT if role == ROLE_CATALOG else None),
             berry_mode, username),
        )


def resolve_offer_norms() -> Dict[int, Dict[str, Any]]:
    """
    Норма каждого товара. Только собственная — наследования от групп нет.

    Групповые нормы были отменены владельцем 2026-09-09 по опыту работы: в
    одной группе CRM лежат товары с сильно разным временем сборки, и норма на
    группу давала правдоподобное, но неверное число. А неверное правдоподобное
    хуже пустого: пустое видно счётчиком, ошибочное — нет.

    `source` в ответе сохранён: он нужен интерфейсу и станет осмысленным
    снова, если появится другой способ задавать норму пачкой (импорт файла —
    как раз такой способ).
    """
    with get_db() as conn:
        rows = conn.execute(
            "SELECT scope_id AS offer_id, role, minutes, basis, berry_mode "
            "FROM load_time_norms WHERE scope = ?",
            (SCOPE_OFFER,),
        ).fetchall()

    return {
        row["offer_id"]: {
            "role": row["role"], "minutes": row["minutes"], "basis": row["basis"],
            "berry_mode": row["berry_mode"],
            "source": "offer", "source_id": row["offer_id"], "source_name": None,
        }
        for row in rows
    }


def set_time_norms_bulk(rows: List[Dict[str, Any]], username: Optional[str] = None) -> Dict[str, Any]:
    """
    Проставить нормы пачкой — импорт файла.

    Валидация та же, что у одиночной записи, и это обязательно: иначе через
    файл в базу заезжает то, что руками ввести нельзя.

    Ошибочные строки не отменяют весь импорт, а возвращаются человеку списком.
    В файле 456 строк, и падение целиком из-за одной опечатки означало бы
    «начни сначала» — на практике это значит «не пользуйся импортом».

    Пустая роль в строке снимает норму: так в файле выражается «этот товар
    больше не размечен», и отдельная колонка «удалить» не нужна.
    """
    applied, cleared = [], []
    errors: List[str] = []

    for row in rows:
        try:
            offer_id = int(row["offer_id"])
        except (KeyError, TypeError, ValueError):
            errors.append(f"строка без идентификатора товара: {str(row)[:80]}")
            continue

        role = (row.get("role") or "").strip() or None
        if role is None:
            cleared.append((offer_id,))
            continue
        if role not in ROLES:
            errors.append(f"товар {offer_id}: неизвестная роль «{role}»")
            continue

        minutes = row.get("minutes")
        if minutes in ("", None):
            minutes = None
        else:
            try:
                # Запятая как разделитель: Excel в русской локали пишет «0,5».
                minutes = float(str(minutes).replace(",", "."))
            except (TypeError, ValueError):
                errors.append(f"товар {offer_id}: некорректное время «{row.get('minutes')}»")
                continue
            if minutes < 0:
                errors.append(f"товар {offer_id}: отрицательное время")
                continue

        if role == ROLE_CATALOG and minutes is None:
            errors.append(f"товар {offer_id}: у готового товара должно быть время")
            continue

        basis = (row.get("basis") or "").strip() or None
        if basis is not None and basis not in BASES:
            errors.append(f"товар {offer_id}: неизвестная база начисления «{basis}»")
            continue

        berry_mode = (row.get("berry_mode") or "").strip() or None
        if berry_mode is not None and berry_mode not in BERRY_MODES:
            errors.append(f"товар {offer_id}: неизвестный режим клубники «{berry_mode}»")
            continue

        applied.append((SCOPE_OFFER, offer_id, role, minutes,
                        basis or (BASIS_UNIT if role == ROLE_CATALOG else None),
                        berry_mode, username))

    with get_db() as conn:
        if applied:
            conn.executemany(
                """
                INSERT INTO load_time_norms
                       (scope, scope_id, role, minutes, basis, berry_mode, updated_by, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(scope, scope_id) DO UPDATE SET
                    role = excluded.role, minutes = excluded.minutes,
                    basis = excluded.basis, berry_mode = excluded.berry_mode,
                    updated_by = excluded.updated_by, updated_at = datetime('now')
                """,
                applied,
            )
        if cleared:
            conn.executemany(
                "DELETE FROM load_time_norms WHERE scope = 'offer' AND scope_id = ?", cleared)

    return {"applied": len(applied), "cleared": len(cleared), "errors": errors}


def norm_catalog(date_from: str, date_to: str, only_missing: bool = False,
                 search: Optional[str] = None, limit: int = 300,
                 role: Optional[str] = None, unit_code: Optional[str] = None,
                 in_catalog: Optional[bool] = None,
                 min_orders: Optional[int] = None,
                 max_orders: Optional[int] = None,
                 min_median: Optional[float] = None,
                 max_median: Optional[float] = None,
                 all_rows: bool = False) -> List[Dict[str, Any]]:
    """
    Товары из заказов за период с их нормой и — главное — с фактами о них.

    Рядом с ролью показывается **медиана количества за позицию** и единица
    измерения из CRM. Это не украшение: единица измерения врёт. У девяти
    товаров из четырнадцати с `unit = g` количество в заказе равно единице —
    это готовые наборы, а не весовые компоненты. Отличает их только факт.

    Медиана, а не среднее: у клубники разброс 26…2000, и среднее уводит
    в сторону ровно там, где решение важнее всего.
    """
    where = ["i.delivery_date >= ?", "i.delivery_date <= ?"]
    params: List[Any] = [date_from, date_to]
    if search:
        # Ищем и по артикулу каталога, и по артикулу из позиции заказа: у
        # позиции он бывает пустым, а у товара в каталоге заполнен — и наоборот
        # у товара, которого в каталоге уже нет.
        where.append(
            "(py_lower(i.product_name) LIKE ? OR py_lower(COALESCE(i.article, '')) LIKE ? "
            " OR py_lower(COALESCE(o.article, '')) LIKE ?)")
        pattern = f"%{search.lower()}%"
        params.extend([pattern, pattern, pattern])

    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT i.offer_id,
                   MAX(i.product_name)                  AS product_name,
                   -- Артикул из каталога надёжнее: в позиции заказа он
                   -- заполнен не всегда, а человек ищет именно по нему.
                   COALESCE(o.article, MAX(i.article))  AS article,
                   COUNT(DISTINCT i.retailcrm_order_id) AS orders,
                   COUNT(*)                             AS positions,
                   o.unit_code                          AS unit_code
              FROM order_items i
         LEFT JOIN crm_offers o ON o.offer_id = i.offer_id
             WHERE {' AND '.join(where)}
          GROUP BY i.offer_id, o.unit_code, o.article
          ORDER BY orders DESC
            """,
            params,
        ).fetchall()

        # LIMIT в SQL нет намеренно. Норма товара определяется наследованием от
        # групп, а его считает Python (resolve_offer_norms) — значит фильтр
        # «без нормы» применяется ПОСЛЕ выборки. Обрежь мы список до фильтра —
        # вкладка «Без нормы» пустела бы по мере разметки верхушки, хотя
        # неразмеченные товары остались бы ниже отсечки, и человек считал бы
        # работу законченной. Товаров за 60 дней порядка пятисот, читать их
        # целиком дешевле, чем врать.

        offer_ids = [row["offer_id"] for row in rows]
        medians = _quantity_medians(conn, offer_ids, date_from, date_to) if offer_ids else {}

    norms = resolve_offer_norms()
    result = []
    for row in rows:
        norm = norms.get(row["offer_id"])
        median = medians.get(row["offer_id"])
        row_in_catalog = row["unit_code"] is not None

        # Фильтры применяются здесь, а не в SQL: роль и наличие нормы живут в
        # другой таблице и резолвятся в Python, а разносить условия по двум
        # местам — верный способ получить фильтр, который врёт в одном из них.
        if only_missing and norm is not None:
            continue
        if role is not None and (norm or {}).get("role") != role:
            continue
        if unit_code is not None and (row["unit_code"] or "") != unit_code:
            continue
        if in_catalog is not None and row_in_catalog != in_catalog:
            continue
        if min_orders is not None and row["orders"] < min_orders:
            continue
        if max_orders is not None and row["orders"] > max_orders:
            continue
        if min_median is not None and (median is None or median < min_median):
            continue
        if max_median is not None and (median is None or median > max_median):
            continue

        result.append({
            "offer_id": row["offer_id"],
            "product_name": row["product_name"],
            "article": row["article"],
            "orders": row["orders"],
            "positions": row["positions"],
            "unit_code": row["unit_code"],
            "median_quantity": median,
            # Товара нет в каталоге CRM — его удалили, а заказ остался.
            # Размечать такое всё равно можно: ключ у нас есть.
            "in_catalog": row_in_catalog,
            "norm": norm,
        })
        # all_rows=True — выгрузка в файл: там отсечка не нужна, иначе человек
        # выгрузит 300 строк из пятисот и не заметит.
        if not all_rows and len(result) >= limit:
            break
    return result


def _quantity_medians(conn, offer_ids: List[int], date_from: str, date_to: str) -> Dict[int, float]:
    """Медиана количества по каждому офферу — одним запросом, оконными функциями."""
    placeholders = ",".join("?" * len(offer_ids))
    rows = conn.execute(
        f"""
        SELECT offer_id, AVG(quantity) AS median FROM (
            SELECT offer_id, quantity,
                   ROW_NUMBER() OVER (PARTITION BY offer_id ORDER BY quantity) AS rn,
                   COUNT(*)   OVER (PARTITION BY offer_id)                     AS cnt
              FROM order_items
             WHERE delivery_date >= ? AND delivery_date <= ?
               AND offer_id IN ({placeholders})
        ) WHERE rn IN ((cnt + 1) / 2, (cnt + 2) / 2)
        GROUP BY offer_id
        """,
        (date_from, date_to, *offer_ids),
    ).fetchall()
    return {row["offer_id"]: round(row["median"], 1) for row in rows}


def norms_coverage(date_from: str, date_to: str,
                   load_statuses: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    Насколько разметка покрывает реальный поток.

    Считается в ЗАКАЗАХ, а не в товарах: «14 товаров без нормы» ничего не
    говорит о занижении, а «в 12 заказах этого дня есть позиции без нормы» —
    говорит. Ровно этим счётчик отличается от прошлого «N товаров без веса»,
    который превратился в фон и перестал читаться.
    """
    statuses = load_statuses if load_statuses is not None else load_status_codes()
    empty = {"orders": 0, "orders_incomplete": 0, "share": 0.0,
             "offers_total": 0, "offers_without_norm": 0}
    if not statuses:
        return empty

    norms = resolve_offer_norms()
    placeholders = ",".join("?" * len(statuses))
    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT i.retailcrm_order_id AS order_id, i.offer_id
              FROM order_items i
              JOIN courier_orders o ON o.retailcrm_order_id = i.retailcrm_order_id
                                   AND o.delivery_date = i.delivery_date
             WHERE i.delivery_date >= ? AND i.delivery_date <= ?
               AND o.status IN ({placeholders})
            """,
            (date_from, date_to, *statuses),
        ).fetchall()

    orders = set()
    incomplete = set()
    offers = set()
    without = set()
    for row in rows:
        orders.add(row["order_id"])
        offers.add(row["offer_id"])
        if norms.get(row["offer_id"]) is None:
            incomplete.add(row["order_id"])
            without.add(row["offer_id"])

    return {
        "orders": len(orders),
        "orders_incomplete": len(incomplete),
        "share": round(100.0 * len(incomplete) / len(orders), 1) if orders else 0.0,
        "offers_total": len(offers),
        "offers_without_norm": len(without),
    }


# ============================================================================
# Тарифы и расчёт минут
# ============================================================================

def load_tariffs() -> tuple:
    """Тарифная сетка из базы: (цветы списком, клубника по режимам)."""
    with get_db() as conn:
        flowers = [dict(row) for row in conn.execute(
            "SELECT range_from, range_to, mono_minutes, mix_minutes, "
            "       ribbon_minutes, package_minutes "
            "  FROM load_tariff_flowers ORDER BY range_from")]
        berries = {row["mode"]: dict(row) for row in conn.execute(
            "SELECT mode, minutes_per_100g, package_minutes FROM load_tariff_berries")}
    return flowers, berries


def set_flower_tariff(range_from: int, range_to: int, mono_minutes: float,
                      mix_minutes: Optional[float], ribbon_minutes: float,
                      package_minutes: float, username: Optional[str] = None) -> None:
    """
    Правка строки тарифа. Полнота сетки проверяется ДО записи.

    Иначе дыра между диапазонами не выглядит ошибкой: заказ на 20 цветов
    просто получит ноль минут, и загрузка окажется занижена молча.
    """
    from . import timing

    candidate = {"range_from": int(range_from), "range_to": int(range_to),
                 "mono_minutes": float(mono_minutes),
                 "mix_minutes": None if mix_minutes is None else float(mix_minutes),
                 "ribbon_minutes": float(ribbon_minutes),
                 "package_minutes": float(package_minutes)}
    for value in ("mono_minutes", "ribbon_minutes", "package_minutes"):
        if candidate[value] < 0:
            raise ValueError("Время не может быть отрицательным")

    current, _ = load_tariffs()
    merged = [row for row in current if row["range_from"] != candidate["range_from"]]
    merged.append(candidate)
    timing.validate_flower_tariffs(merged)

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO load_tariff_flowers (range_from, range_to, mono_minutes, mix_minutes,
                                             ribbon_minutes, package_minutes, updated_by, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(range_from) DO UPDATE SET
                range_to = excluded.range_to, mono_minutes = excluded.mono_minutes,
                mix_minutes = excluded.mix_minutes, ribbon_minutes = excluded.ribbon_minutes,
                package_minutes = excluded.package_minutes,
                updated_by = excluded.updated_by, updated_at = datetime('now')
            """,
            (candidate["range_from"], candidate["range_to"], candidate["mono_minutes"],
             candidate["mix_minutes"], candidate["ribbon_minutes"],
             candidate["package_minutes"], username),
        )


def set_berry_tariff(mode: str, minutes_per_100g: float, package_minutes: float,
                     username: Optional[str] = None) -> None:
    """Правка тарифа по клубнике: время на 100 г и упаковка."""
    if mode not in BERRY_MODES:
        raise ValueError(f"Неизвестный режим клубники: {mode}")
    if minutes_per_100g < 0 or package_minutes < 0:
        raise ValueError("Время не может быть отрицательным")

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO load_tariff_berries (mode, minutes_per_100g, package_minutes,
                                             updated_by, updated_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT(mode) DO UPDATE SET
                minutes_per_100g = excluded.minutes_per_100g,
                package_minutes = excluded.package_minutes,
                updated_by = excluded.updated_by, updated_at = datetime('now')
            """,
            (mode, float(minutes_per_100g), float(package_minutes), username),
        )


# Шаг пересчёта минут. Пачками по датам, а не одним проходом по всей витрине:
# расчёт читает позиции окна в память, и один длинный проход держал бы
# write-лок общей базы на всё время работы — тем же способом уже роняли сайт.
MINUTES_CHUNK_DAYS = 30


def recalc_minutes_range(date_from: str, date_to: str) -> Dict[str, int]:
    """
    Пересчитать минуты сборки за период.

    Считается на Python, а не в SQL: диапазоны тарифа, моно/микс и две
    упаковки в SQL нечитаемы, а этот код читают при каждом споре о цифре.

    Тарифы и нормы читаются ОДИН раз на весь период: они одинаковы для всех
    заказов, а перечитывать их на каждую пачку — лишние обращения к диску.
    """
    from . import timing

    flowers, berries = load_tariffs()
    timing.validate_flower_tariffs(flowers)   # битая сетка не должна обнулить витрину
    norms = resolve_offer_norms()

    start = datetime.strptime(date_from, "%Y-%m-%d").date()
    end = datetime.strptime(date_to, "%Y-%m-%d").date()
    orders_done = 0
    incomplete = 0

    while start <= end:
        chunk_to = min(start + timedelta(days=MINUTES_CHUNK_DAYS - 1), end)
        chunk = _recalc_minutes_chunk(start.isoformat(), chunk_to.isoformat(),
                                      norms, flowers, berries)
        orders_done += chunk["orders"]
        incomplete += chunk["incomplete"]
        start = chunk_to + timedelta(days=1)

    return {"orders": orders_done, "incomplete": incomplete}


def _recalc_minutes_chunk(date_from: str, date_to: str, norms, flowers, berries) -> Dict[str, int]:
    """Одна пачка: чтение позиций, расчёт в памяти, одна запись."""
    from . import timing

    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT o.retailcrm_order_id AS order_id, i.offer_id, i.quantity
              FROM courier_orders o
         LEFT JOIN order_items i ON i.retailcrm_order_id = o.retailcrm_order_id
                                AND i.delivery_date = o.delivery_date
             WHERE o.delivery_date >= ? AND o.delivery_date <= ?
            """,
            (date_from, date_to),
        ).fetchall()

        by_order: Dict[int, List[Dict[str, Any]]] = {}
        for row in rows:
            items = by_order.setdefault(row["order_id"], [])
            # LEFT JOIN даёт строку с пустой позицией у заказа без состава:
            # это не ошибка, а заказ, заведённый одной суммой.
            if row["offer_id"] is not None:
                items.append({"offer_id": row["offer_id"], "quantity": row["quantity"]})

        updates = []
        incomplete = 0
        for order_id, items in by_order.items():
            result = timing.order_minutes(items, norms, flowers, berries)
            if result["without_norm"]:
                incomplete += 1
            updates.append((result["total"], result["flowers"], result["packaging"],
                            result["berries"], result["catalog"], result["without_norm"],
                            order_id))

        if updates:
            conn.executemany(
                "UPDATE courier_orders SET minutes_total = ?, minutes_flowers = ?, "
                "       minutes_packaging = ?, minutes_berries = ?, minutes_catalog = ?, "
                "       items_without_norm = ? "
                " WHERE retailcrm_order_id = ?",
                updates,
            )

    return {"orders": len(by_order), "incomplete": incomplete}


def upsert_order_statuses(statuses: List[Dict[str, Any]]) -> None:
    """
    Обновить справочник статусов.

    counts_as_load сидируется из группы CRM: `cancel` — не нагрузка, остальное
    нагрузка. Дальше значение правится руками и синхронизацией НЕ перетирается
    (тот же приём, что у is_service и counts_as_courier): какой статус считать
    работой флориста, решает человек.

    Новый статус приходит с reviewed=0 — чтобы его было видно в справочнике,
    а не чтобы он молча попал в расчёт с угаданным значением.
    """
    if not statuses:
        return
    with get_db() as conn:
        conn.executemany(
            """
            INSERT INTO order_statuses (code, name, group_code, counts_as_load, active, reviewed)
            VALUES (?, ?, ?, ?, ?, 0)
            ON CONFLICT(code) DO UPDATE SET
                name = excluded.name,
                group_code = excluded.group_code,
                active = excluded.active,
                updated_at = datetime('now')
            """,
            [
                (
                    status["code"],
                    status.get("name") or status["code"],
                    status.get("group_code"),
                    0 if status.get("group_code") == "cancel" else 1,
                    1 if status.get("active", True) else 0,
                )
                for status in statuses
                if status.get("code")
            ],
        )


def list_order_statuses() -> List[Dict[str, Any]]:
    """Справочник статусов для экрана: что считается нагрузкой салона."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT code, name, group_code, counts_as_load, active, reviewed "
            "FROM order_statuses ORDER BY counts_as_load DESC, group_code, name"
        ).fetchall()
    return [
        {
            "code": row["code"],
            "name": row["name"],
            "group": row["group_code"],
            "counts_as_load": bool(row["counts_as_load"]),
            "active": bool(row["active"]),
            "reviewed": bool(row["reviewed"]),
        }
        for row in rows
    ]


def set_order_status_load_flag(code: str, value: bool) -> bool:
    """
    Отметить статус как нагрузку. reviewed=1 — человек это значение видел,
    и оно больше не «угаданное из группы CRM».
    """
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE order_statuses SET counts_as_load = ?, reviewed = 1, "
            "updated_at = datetime('now') WHERE code = ?",
            (1 if value else 0, code),
        )
    return cur.rowcount > 0


def load_status_codes() -> List[str]:
    """Статусы, которые считаются нагрузкой салона."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT code FROM order_statuses WHERE counts_as_load = 1"
        ).fetchall()
    return [row["code"] for row in rows]


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


def get_site_offsets() -> Dict[str, Optional[int]]:
    """
    {код салона: смещение от UTC в часах}. None — пояс не задан.

    Читается один раз на расчёт (сроки брони по пачке заказов), а не на каждый
    заказ: /data сетевой, и запрос на строку стоит дороже самого расчёта.
    """
    with get_db() as conn:
        rows = conn.execute("SELECT code, utc_offset FROM courier_sites").fetchall()
    return {row["code"]: row["utc_offset"] for row in rows}


def list_sites_timezones(only_missing: bool = False) -> List[Dict[str, Any]]:
    """
    Справочник салонов с поясом — для экрана настроек.

    only_missing=True отдаёт те, где пояс не задан: именно они ломают сроки
    брони, и человек должен видеть их отдельным коротким списком, а не искать
    глазами по всему справочнику.
    """
    sql = """
        SELECT code, name, city, utc_offset, updated_at
        FROM courier_sites
    """
    if only_missing:
        sql += " WHERE utc_offset IS NULL"
    sql += " ORDER BY city IS NULL, city, name"

    with get_db() as conn:
        rows = conn.execute(sql).fetchall()
    return [
        {
            "code": row["code"],
            "name": row["name"],
            "city": row["city"],
            "utc_offset": row["utc_offset"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]


def set_site_timezone(code: str, utc_offset: Optional[int]) -> bool:
    """
    Задать пояс салона руками. None — снять значение («пояс неизвестен»).

    Диапазон ограничен реальными поясами: опечатка «77» вместо «7» сдвинула бы
    сроки брони на трое суток, и заметили бы это по сгоревшим броням, а не по
    справочнику.
    """
    if utc_offset is not None and not (-12 <= int(utc_offset) <= 14):
        raise ValueError(f"Недопустимое смещение UTC: {utc_offset}")

    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE courier_sites SET utc_offset = ?, updated_at = datetime('now') "
            "WHERE code = ?",
            (int(utc_offset) if utc_offset is not None else None, code),
        )
    return cursor.rowcount > 0


# ============================================================================
# Чтение: отчёт и справочники
# ============================================================================

def _period_filter(date_from: Optional[str], date_to: Optional[str], alias: str = ""):
    """Кусок WHERE по дате доставки. delivery_date — календарная дата YYYY-MM-DD,
    поэтому сравнение строк и есть сравнение дат (без таймзон: RetailCRM отдаёт
    delivery.date без времени).

    alias — префикс таблицы для запросов с JOIN («o» → «o.delivery_date»).
    Раньше префикс навешивался поверх готовой строки через .replace(), и это
    ломалось бы на любом новом условии, где встретится то же слово."""
    prefix = f"{alias}." if alias else ""
    where, params = [], []
    if date_from:
        where.append(f"{prefix}delivery_date >= ?")
        params.append(date_from)
    if date_to:
        where.append(f"{prefix}delivery_date <= ?")
        params.append(date_to)
    return where, params


def _payout_scope(
    date_from: Optional[str],
    date_to: Optional[str],
    city: Optional[str],
    site_code: Optional[str],
    alias: str = "o",
):
    """
    Общий отбор строк витрины для отчёта выплат: период, город, салон и
    PAYOUT_FILTER.

    Статус сюда НЕ входит намеренно: части отчёта смотрят на разные статусы —
    выплата считает «Выполнен», отдельный блок — отменённые. Общим остаётся
    только то, что действительно общее, иначе один из блоков молча считал бы
    не ту совокупность, что подписан.
    """
    where, params = _period_filter(date_from, date_to, alias)
    if city:
        where.append(f"{alias}.city = ?")
        params.append(city)
    if site_code:
        where.append(f"{alias}.site_code = ?")
        params.append(site_code)
    # Витрина хранит ВСЕ заказы (нужны показателям и загрузке салонов), поэтому
    # отбор «за что вообще платим» ставится в чтении — см. PAYOUT_FILTER.
    where.append(
        f"({alias}.courier_id IS NOT NULL OR {alias}.net_cost > 0)"
    )
    return where, params


def report_by_courier(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    city: Optional[str] = None,
    site_code: Optional[str] = None,
    only_own: bool = True,
    list_limit: int = DETAIL_LIST_LIMIT,
) -> Dict[str, Any]:
    """
    Отчёт «сколько платить курьеру за период» и три разреза к нему.

      couriers        — строки выплаты по курьерам (статус «Выполнен»)
      sites           — та же сумма в разрезе салонов: чей это расход
      without_courier — заказы, где деньги на доставку потрачены, а курьер в
                        CRM не проставлен. Список с номерами заказов, а не одно
                        число: по числу нельзя пойти и починить данные в CRM
      cancelled       — отменённые заказы с потраченной доставкой. В сумму
                        выплаты они НЕ входят (за отменённое не платим по
                        умолчанию), но исчезать из виду не должны: курьер мог
                        съездить, и решение принимает человек

    only_own=True — исключить службы доставки (couriers.is_service = 1).

    Всё считается ОДНИМ соединением: на сетевом диске /data цену определяет
    число открытых соединений и число обращений, а не размер одного запроса
    (см. CLAUDE.md). Отдельная HTTP-ручка на каждый блок означала бы четыре
    соединения вместо одного на каждый показ страницы.
    """
    base_where, base_params = _payout_scope(date_from, date_to, city, site_code)
    base_sql = " AND ".join(base_where)

    completed_sql = base_sql + " AND o.status = ?"
    completed_params = [*base_params, COMPLETED_STATUS]

    # Для строк выплаты службы отсекаются целиком, а для разрезов, где есть
    # заказы без курьера, — только там, где курьер указан: иначе фильтр
    # «только свои» заодно прятал бы незаполненные заказы, которые как раз
    # и надо чинить.
    own_courier = " AND COALESCE(c.is_service, 0) = 0" if only_own else ""
    own_mixed = (
        " AND (o.courier_id IS NULL OR COALESCE(c.is_service, 0) = 0)" if only_own else ""
    )

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
            WHERE {completed_sql} AND o.courier_id IS NOT NULL{own_courier}
            GROUP BY o.courier_id, COALESCE(c.name, o.courier_name), COALESCE(c.is_service, 0)
            ORDER BY total_net_cost DESC
            """,
            completed_params,
        ).fetchall()

        # Распределение по салонам. Салон — site_code (сайт заказа): именно от
        # него считается город в фильтре, поэтому суммы разрезов сходятся между
        # собой. Склад-исполнитель (store_key) отвечает на другой вопрос — кто
        # собирал букет, а не чей это расход на доставку.
        site_rows = conn.execute(
            f"""
            SELECT
                o.site_code                                 AS site_code,
                COALESCE(MAX(s.name), o.site_code)          AS site_name,
                MAX(o.city)                                 AS city,
                COUNT(DISTINCT o.courier_id)                AS couriers_count,
                SUM(CASE WHEN o.courier_id IS NOT NULL THEN 1 ELSE 0 END)
                                                            AS orders_count,
                COALESCE(SUM(CASE WHEN o.courier_id IS NOT NULL
                                  THEN o.net_cost ELSE 0 END), 0)
                                                            AS total_net_cost,
                SUM(CASE WHEN o.courier_id IS NULL THEN 1 ELSE 0 END)
                                                            AS orders_without_courier,
                COALESCE(SUM(CASE WHEN o.courier_id IS NULL
                                  THEN o.net_cost ELSE 0 END), 0)
                                                            AS net_cost_without_courier
            FROM courier_orders o
            LEFT JOIN couriers c ON c.id = o.courier_id
            LEFT JOIN courier_sites s ON s.code = o.site_code
            WHERE {completed_sql}{own_mixed}
            GROUP BY o.site_code
            ORDER BY total_net_cost DESC
            """,
            completed_params,
        ).fetchall()

        # Заказы без курьера, но с потраченной себестоимостью доставки —
        # показатель качества заполнения CRM: деньги ушли, а кому платить,
        # из заказа не видно. Самовывоз (нулевая себестоимость) сюда не
        # попадает: его отсекает условие net_cost > 0 (раньше отсекала запись).
        missing = conn.execute(
            f"""
            SELECT COUNT(*) AS cnt, COALESCE(SUM(o.net_cost), 0) AS total
            FROM courier_orders o
            WHERE {completed_sql} AND o.courier_id IS NULL AND o.net_cost > 0
            """,
            completed_params,
        ).fetchone()

        missing_rows = conn.execute(
            f"""
            SELECT o.retailcrm_order_id, o.order_number, o.delivery_date,
                   o.net_cost, o.site_code,
                   COALESCE(s.name, o.site_code) AS site_name,
                   o.city, o.delivery_city
            FROM courier_orders o
            LEFT JOIN courier_sites s ON s.code = o.site_code
            WHERE {completed_sql} AND o.courier_id IS NULL AND o.net_cost > 0
            ORDER BY o.delivery_date DESC, o.retailcrm_order_id DESC
            LIMIT ?
            """,
            [*completed_params, list_limit],
        ).fetchall()

        # Отменённые. Отбор идёт по ГРУППЕ статуса из справочника, а не по коду
        # и не по названию: статусов отмены в CRM несколько, а названия
        # переименовывают.
        cancel_join = (
            "JOIN order_statuses st ON st.code = o.status "
            f"AND st.group_code = '{CANCEL_STATUS_GROUP}'"
        )
        cancelled = conn.execute(
            f"""
            SELECT COUNT(*) AS cnt, COALESCE(SUM(o.net_cost), 0) AS total
            FROM courier_orders o
            {cancel_join}
            LEFT JOIN couriers c ON c.id = o.courier_id
            WHERE {base_sql}{own_mixed}
            """,
            base_params,
        ).fetchone()

        cancelled_rows = conn.execute(
            f"""
            SELECT o.retailcrm_order_id, o.order_number, o.delivery_date,
                   o.status, COALESCE(st.name, o.status) AS status_name,
                   o.courier_id, COALESCE(c.name, o.courier_name) AS courier_name,
                   o.net_cost, o.total_summ, o.site_code,
                   COALESCE(s.name, o.site_code) AS site_name,
                   o.city, o.delivery_city
            FROM courier_orders o
            {cancel_join}
            LEFT JOIN couriers c ON c.id = o.courier_id
            LEFT JOIN courier_sites s ON s.code = o.site_code
            WHERE {base_sql}{own_mixed}
            ORDER BY o.delivery_date DESC, o.retailcrm_order_id DESC
            LIMIT ?
            """,
            [*base_params, list_limit],
        ).fetchall()

        # Сколько статусов отмены вообще известно справочнику. Без этого числа
        # пустой блок «Отменённые» одинаково означает и «отмен не было», и
        # «справочник статусов ещё не загружен» — а это разные поломки.
        cancel_statuses_known = conn.execute(
            "SELECT COUNT(*) AS c FROM order_statuses WHERE group_code = ?",
            (CANCEL_STATUS_GROUP,),
        ).fetchone()["c"]

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

    sites = [
        {
            "site_code": row["site_code"],
            "site_name": row["site_name"] or row["site_code"],
            "city": row["city"],
            "couriers_count": row["couriers_count"] or 0,
            "orders_count": row["orders_count"] or 0,
            "total_net_cost": round(row["total_net_cost"] or 0, 2),
            "orders_without_courier": row["orders_without_courier"] or 0,
            "net_cost_without_courier": round(row["net_cost_without_courier"] or 0, 2),
        }
        for row in site_rows
    ]

    missing_count = missing["cnt"] if missing else 0
    cancelled_count = cancelled["cnt"] if cancelled else 0

    return {
        "couriers": couriers,
        "sites": sites,
        "without_courier": {
            "orders": [dict(row) for row in missing_rows],
            "count": missing_count,
            "total_net_cost": round((missing["total"] if missing else 0) or 0, 2),
            "truncated": missing_count > len(missing_rows),
        },
        "cancelled": {
            "orders": [dict(row) for row in cancelled_rows],
            "count": cancelled_count,
            "total_net_cost": round((cancelled["total"] if cancelled else 0) or 0, 2),
            "truncated": cancelled_count > len(cancelled_rows),
            "statuses_known": cancel_statuses_known,
        },
        "totals": {
            "couriers_count": len(couriers),
            "orders_count": sum(c["orders_count"] for c in couriers),
            "total_net_cost": round(sum(c["total_net_cost"] for c in couriers), 2),
            "sites_count": len(sites),
            "orders_without_courier": missing_count,
            "net_cost_without_courier": round((missing["total"] if missing else 0) or 0, 2),
            "cancelled_orders": cancelled_count,
            "cancelled_net_cost": round((cancelled["total"] if cancelled else 0) or 0, 2),
        },
    }


def list_orders(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    city: Optional[str] = None,
    site_code: Optional[str] = None,
    courier_id: Optional[int] = None,
    without_courier: bool = False,
    cancelled: bool = False,
    limit: int = 1000,
) -> List[Dict[str, Any]]:
    """Расшифровка отчёта по заказам — чтобы сумму можно было проверить, а не верить на слово.

    cancelled=True — вместо выполненных отдаются отменённые (группа статусов
    `cancel` из справочника). Это тот же список тех же полей, поэтому отдельная
    функция была бы копией с одной изменённой строкой."""
    where, params = _payout_scope(date_from, date_to, city, site_code)
    if cancelled:
        status_join = (
            "JOIN order_statuses st ON st.code = o.status "
            f"AND st.group_code = '{CANCEL_STATUS_GROUP}'"
        )
    else:
        status_join = ""
        where.append("o.status = ?")
        params.append(COMPLETED_STATUS)

    if without_courier:
        where.append("o.courier_id IS NULL")
    elif courier_id is not None:
        where.append("o.courier_id = ?")
        params.append(courier_id)

    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT o.retailcrm_order_id, o.order_number, o.delivery_date,
                   o.courier_id, COALESCE(c.name, o.courier_name) AS courier_name,
                   o.net_cost, o.status,
                   o.site_code, COALESCE(s.name, o.site_code) AS site_name,
                   o.city, o.delivery_city
            FROM courier_orders o
            {status_join}
            LEFT JOIN couriers c ON c.id = o.courier_id
            LEFT JOIN courier_sites s ON s.code = o.site_code
            WHERE {' AND '.join(where)}
            ORDER BY o.delivery_date DESC, o.retailcrm_order_id DESC
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
            f"WHERE status = ? AND city IS NOT NULL AND city != '' AND {PAYOUT_FILTER} "
            "ORDER BY city",
            (COMPLETED_STATUS,),
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
            f"FROM courier_orders WHERE status = ? AND {PAYOUT_FILTER}",
            (COMPLETED_STATUS,),
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


def health_snapshot() -> Dict[str, Any]:
    """
    Техническое состояние витрины для /health.

    Нужно, чтобы состояние прода можно было проверить снаружи, не заходя в
    интерфейс: консоли у контейнера нет, логи недоступны, а «показатели
    нулевые» — это одинаково и «синк не отработал», и «миграция не прошла»,
    и «данные ещё грузятся». Здесь только счётчики, даты и статусы: сумм и
    названий не отдаём, ручка публичная.
    """
    with get_db() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(courier_orders)")}
        # Колонки могло не быть: ALTER на старте воркера мог не пройти
        has_new = {"total_summ", "order_method", "delivery_code"} <= columns

        if has_new:
            row = conn.execute("""
                SELECT COUNT(*) AS rows,
                       SUM(CASE WHEN total_summ > 0 THEN 1 ELSE 0 END) AS with_summ,
                       SUM(CASE WHEN order_method IS NOT NULL THEN 1 ELSE 0 END) AS with_method,
                       MIN(delivery_date) AS since, MAX(delivery_date) AS until
                FROM courier_orders WHERE status = ?
            """, (COMPLETED_STATUS,)).fetchone()
            data = {
                "rows": row["rows"],
                "with_summ": row["with_summ"] or 0,
                "with_method": row["with_method"] or 0,
                "since": row["since"],
                "until": row["until"],
            }
        else:
            row = conn.execute(
                "SELECT COUNT(*) AS rows FROM courier_orders WHERE status = ?",
                (COMPLETED_STATUS,),
            ).fetchone()
            data = {"rows": row["rows"]}

        # Строки всех статусов — отдельным числом. Витрина шире отчёта (см.
        # PAYOUT_FILTER), а после Фазы 1 в ней появятся ещё и будущие заказы для
        # модуля нагрузки салонов. Смешивать их со счётчиком выполненных нельзя:
        # «строк много, а показатели нулевые» — это разные поломки.
        data["rows_all_statuses"] = conn.execute(
            "SELECT COUNT(*) AS rows FROM courier_orders"
        ).fetchone()["rows"]

        # Состояние данных модуля «Загрузка салонов». Пустая сетка одинаково
        # выглядит и как «заказов нет», и как «синк не дотянул будущее», и как
        # «время готовности не разобралось» — здесь эти случаи разделены.
        if "ready_hour" in columns:
            # Окно, а не вся витрина. Замер на проде 2026-09-07: полный скан
            # `courier_orders` (15 тыс. строк) плюс COUNT по `order_items`
            # (25 тыс.) держали /health по 6–9 секунд при 2 мс на статику —
            # диск /data сетевой, и цену определяет объём чтения. Диагностике
            # нужна свежая картина, а не история за все времена.
            window_from = (date.today() - timedelta(days=HEALTH_WINDOW_DAYS)).isoformat()
            load_row = conn.execute("""
                SELECT SUM(CASE WHEN delivery_date > date('now') THEN 1 ELSE 0 END) AS future_orders,
                       SUM(CASE WHEN ready_hour IS NOT NULL THEN 1 ELSE 0 END)      AS with_ready_hour,
                       SUM(CASE WHEN ready_source = 'unparsed' THEN 1 ELSE 0 END)   AS unparsed_ready,
                       SUM(CASE WHEN store_key IS NULL OR store_key = '' THEN 1 ELSE 0 END) AS without_store,
                       COUNT(*) AS rows_in_window
                FROM courier_orders WHERE delivery_date >= ?
            """, (window_from,)).fetchone()
            data["load"] = {
                "window_from": window_from,
                "rows_in_window": load_row["rows_in_window"] or 0,
                "future_orders": load_row["future_orders"] or 0,
                "with_ready_hour": load_row["with_ready_hour"] or 0,
                "unparsed_ready": load_row["unparsed_ready"] or 0,
                "without_store": load_row["without_store"] or 0,
                # MAX по индексированной колонке — чтение одной строки индекса,
                # а не скан таблицы.
                "until_future": conn.execute(
                    "SELECT MAX(delivery_date) AS d FROM courier_orders").fetchone()["d"],
                "items": conn.execute(
                    "SELECT COUNT(*) AS c FROM order_items WHERE delivery_date >= ?",
                    (window_from,),
                ).fetchone()["c"],
                "weights_set": conn.execute(
                    "SELECT COUNT(*) AS c FROM product_weights").fetchone()["c"],
                # Каталог номенклатуры. Пустой каталог не виден по нагрузке
                # никак — она просто станет ниже, — поэтому его состояние
                # обязано быть в диагностике. `offers_weighted` — сколько
                # товаров меряется НЕ штуками: если это ноль, синк каталога
                # либо не прошёл, либо пришёл без единиц измерения.
                # Тем же соединением: второе поверх незакрытого — вложенный
                # коннект, которым уже вешали базу.
                "catalog": catalog_counts(conn),
                "statuses_as_load": conn.execute(
                    "SELECT COUNT(*) AS c FROM order_statuses WHERE counts_as_load = 1"
                ).fetchone()["c"],
                # Читаем тем же соединением: открывать второе, пока это ещё
                # живо, — тот самый вложенный коннект, которым уже вешали базу.
                "orders_without_date": (
                    conn.execute("SELECT value FROM sync_state WHERE key = ?",
                                 (NO_DATE_ORDERS_KEY,)).fetchone() or {"value": None}
                )["value"],
            }

        types_row = conn.execute(
            "SELECT COUNT(*) AS total, SUM(counts_as_courier) AS courier FROM delivery_types"
        ).fetchone()
        taxi_row = conn.execute(
            "SELECT SUM(COALESCE(is_external_taxi, 0)) AS taxi FROM couriers"
        ).fetchone() if "is_external_taxi" in {
            r[1] for r in conn.execute("PRAGMA table_info(couriers)")} else None

    data["schema_migrated"] = has_new
    data["delivery_types"] = types_row["total"] if types_row else 0
    data["delivery_types_courier"] = (types_row["courier"] or 0) if types_row else 0
    data["taxi_couriers"] = (taxi_row["taxi"] or 0) if taxi_row else None

    last = get_latest_sync_log()
    if last:
        data["last_sync"] = {
            "status": last.get("status"),
            "started_at": last.get("started_at"),
            "finished_at": last.get("finished_at"),
            "records": last.get("records_count"),
            "error": (last.get("error_message") or "")[:300] or None,
        }
    return data


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
            "FROM courier_orders WHERE status = ? AND order_method IS NOT NULL",
            (COMPLETED_STATUS,),
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


def load_freshness(date_from: str, date_to: str) -> Dict[str, Any]:
    """
    Свежесть данных для экрана нагрузки — дёшево.

    Экран показывает эту подпись на каждом открытии, поэтому здесь нельзя
    ходить в `health_snapshot`: та собирает диагностику по всей витрине, и на
    сетевом диске это секунды. Берём только то, что реально нужно подписи, и
    только по показываемому периоду (индекс по delivery_date).
    """
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS orders,
                   SUM(CASE WHEN ready_source = 'unparsed' THEN 1 ELSE 0 END) AS unparsed_ready,
                   SUM(CASE WHEN store_key IS NULL OR store_key = '' THEN 1 ELSE 0 END)
                       AS without_store
              FROM courier_orders
             WHERE delivery_date >= ? AND delivery_date <= ?
            """,
            (date_from, date_to),
        ).fetchone()
        until_future = conn.execute(
            "SELECT MAX(delivery_date) AS d FROM courier_orders").fetchone()["d"]
        statuses = conn.execute(
            "SELECT COUNT(*) AS c FROM order_statuses WHERE counts_as_load = 1").fetchone()["c"]
        no_date = conn.execute(
            "SELECT value FROM sync_state WHERE key = ?", (NO_DATE_ORDERS_KEY,)).fetchone()
        last = conn.execute(
            "SELECT status, started_at, finished_at FROM sync_log ORDER BY id DESC LIMIT 1"
        ).fetchone()

    return {
        "orders": row["orders"] or 0,
        "unparsed_ready": row["unparsed_ready"] or 0,
        "without_store": row["without_store"] or 0,
        "until_future": until_future,
        "statuses_as_load": statuses,
        "orders_without_date": no_date["value"] if no_date else None,
        "last_sync_at": (last["finished_at"] or last["started_at"]) if last else None,
        "last_sync_status": last["status"] if last else None,
    }


def load_by_slot(date_from: str, date_to: str,
                 load_statuses: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """
    Нагрузка по слотам: дата × склад × час готовности.

    Агрегат считает модуль-владелец данных, а не SQL-запросы соседнего модуля
    по этой таблице: иначе изменение схемы витрины тихо ломает сетку нагрузки.

    Заказы без часа готовности возвращаются с hour=None — их не выбрасываем и
    не размазываем по сетке: это отдельная строка «без времени», которую
    разбирает человек.

    Самовывоз считается отдельным счётчиком: флорист и стойка выдачи — разные
    ресурсы, и узкое место может быть не там, где кажется.
    """
    statuses = load_statuses if load_statuses is not None else load_status_codes()
    if not statuses:
        return []

    placeholders = ",".join("?" * len(statuses))
    pickup_codes = ",".join("?" * len(PICKUP_DELIVERY_CODES))
    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT delivery_date, store_key, ready_hour,
                   COUNT(*)                                   AS orders,
                   COALESCE(SUM(weight_units), 0)             AS units,
                   SUM(CASE WHEN delivery_code IN ({pickup_codes}) THEN 1 ELSE 0 END)
                                                              AS pickup_orders,
                   COALESCE(SUM(CASE WHEN delivery_code IN ({pickup_codes})
                                     THEN weight_units ELSE 0 END), 0)
                                                              AS pickup_units,
                   SUM(CASE WHEN ready_source = 'unparsed' THEN 1 ELSE 0 END)
                                                              AS unparsed_orders
              FROM courier_orders
             WHERE delivery_date >= ? AND delivery_date <= ?
               AND status IN ({placeholders})
          GROUP BY delivery_date, store_key, ready_hour
            """,
            (*PICKUP_DELIVERY_CODES, *PICKUP_DELIVERY_CODES, date_from, date_to, *statuses),
        ).fetchall()

    return [
        {
            "date": row["delivery_date"],
            "store_key": row["store_key"],
            "hour": row["ready_hour"],
            "orders": row["orders"],
            "units": round(row["units"] or 0, 2),
            "pickup_orders": row["pickup_orders"] or 0,
            "pickup_units": round(row["pickup_units"] or 0, 2),
            "unparsed_orders": row["unparsed_orders"] or 0,
        }
        for row in rows
    ]


def list_slot_orders(date: str, store_key: str, hour: Optional[int],
                     load_statuses: Optional[List[str]] = None,
                     limit: int = 200) -> List[Dict[str, Any]]:
    """Заказы одного слота — для клика по ячейке. hour=None — «без времени»."""
    statuses = load_statuses if load_statuses is not None else load_status_codes()
    if not statuses:
        return []

    placeholders = ",".join("?" * len(statuses))
    hour_condition = "ready_hour IS NULL" if hour is None else "ready_hour = ?"
    params: List[Any] = [date, store_key]
    if hour is not None:
        params.append(hour)

    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT retailcrm_order_id, order_number, ready_time, ready_source,
                   delivery_code, status, weight_units, total_summ
              FROM courier_orders
             WHERE delivery_date = ? AND store_key = ? AND {hour_condition}
               AND status IN ({placeholders})
          ORDER BY ready_time, retailcrm_order_id
             LIMIT ?
            """,
            (*params, *statuses, limit),
        ).fetchall()

    return [
        {
            "order_id": row["retailcrm_order_id"],
            "number": row["order_number"],
            "ready_time": row["ready_time"],
            "ready_source": row["ready_source"],
            "delivery_code": row["delivery_code"],
            "is_pickup": row["delivery_code"] in PICKUP_DELIVERY_CODES,
            "status": row["status"],
            "units": row["weight_units"],
            "amount": row["total_summ"],
        }
        for row in rows
    ]


def list_unmapped_stores(date_from: str, date_to: str, known_keys: List[str],
                         load_statuses: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """
    Склады-исполнители за период, которых нет в справочнике салонов.

    В отличие от сайтов, считаем по ВСЕМ статусам нагрузки, а не только по
    выполненным: сетка нагрузки живёт на будущих заказах, и склад, который
    забыли привязать, обязан всплыть сегодня, а не через месяц, когда заказы
    станут выполненными.

    Заказы с пустым складом отдаются отдельной строкой с key=None — это
    «нераспределённые» из решения 5. Молча приписать их наиболее вероятному
    салону нельзя: ошибка сопоставления тихо перекладывает нагрузку.
    """
    statuses = load_statuses if load_statuses is not None else load_status_codes()
    if not statuses:
        return []

    placeholders = ",".join("?" * len(statuses))
    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT store_key,
                   COUNT(*) AS orders,
                   COALESCE(SUM(weight_units), 0) AS weight
              FROM courier_orders
             WHERE delivery_date >= ? AND delivery_date <= ?
               AND status IN ({placeholders})
             GROUP BY store_key
            """,
            (date_from, date_to, *statuses),
        ).fetchall()

    known = set(known_keys)
    result = []
    for row in rows:
        key = row["store_key"]
        if key and key in known:
            continue
        result.append({
            "key": key or None,
            "orders": row["orders"],
            "weight": round(row["weight"] or 0, 2),
        })
    return result


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
