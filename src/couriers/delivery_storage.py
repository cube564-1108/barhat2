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
import time
from typing import Any, Dict, List, Optional

from datetime import datetime, timedelta

from sqlite_conn import connect as sqlite_connect

from . import salon_time
from .storage import DB_PATH, _add_column_if_missing, get_db

logger = logging.getLogger(__name__)

# --- состояния брони -------------------------------------------------------
STATE_CLAIMED = "claimed"        # забронирован, ещё не забран
STATE_PICKED_UP = "picked_up"    # курьер забрал заказ из салона
STATE_DELIVERED = "delivered"    # вручён
STATE_RELEASED = "released"      # отпущен (сам, админом, автоматом)
STATE_PROBLEM = "problem"        # недозвон, перенос, отказ

# Живые состояния: пока бронь в одном из них, заказ другим курьерам не отдаётся.
ACTIVE_STATES = (STATE_CLAIMED, STATE_PICKED_UP)

# Состояния, в которых работа по заказу ЗАКОНЧЕНА, но заказ всё равно не
# возвращается в общий список.
#
# 16.09.2026: доставленный заказ снова становился свободным и уходил на второй
# круг «бронь → забор → доставка». Причина — проверки смотрели только на живые
# состояния, а доставка из них выходит. То же и с проблемой: букет физически у
# курьера, и пока человек не разобрался, отдавать заказ второму нельзя.
TERMINAL_STATES = (STATE_DELIVERED, STATE_PROBLEM)

# Всё, что запрещает новую бронь. В общий список заказ возвращают только
# released и expired — то есть явное решение (отказ, снятие) или сгоревший срок.
BLOCKING_STATES = ACTIVE_STATES + TERMINAL_STATES

# Те же состояния литералами для запросов, которые собираются f-строкой.
BLOCKING_STATES_SQL = ", ".join(f"'{state}'" for state in BLOCKING_STATES)

# --- причины снятия брони --------------------------------------------------
RELEASE_SELF = "self"            # курьер отказался сам
RELEASE_EXPIRED = "expired"      # сгорела по времени
RELEASE_ADMIN = "admin"          # снял управляющий
RELEASE_OUTSOURCED = "outsourced"  # заказ передали службе доставки
RELEASE_ORDER_GONE = "order_gone"  # заказ отменён или ушёл из видимых статусов

# Значения по умолчанию для города, у которого настроек ещё нет. Ноль записей в
# `courier_city_settings` — нормальное состояние: заводить строку на каждый
# город руками не нужно, пока значения устраивают.
# Лимит одновременных броней — БЕЗ ОГРАНИЧЕНИЯ по умолчанию (решение владельца
# 21.09.2026). Раньше здесь стояла тройка, которую никто не выбирал: в плане
# записано только «лимит — настройка на город», а число появилось при
# реализации. Экрана настроек не было, поэтому все девять городов молча жили с
# тройкой — а в праздники курьер увозит шесть-восемь заказов и упирался в
# отказ. Ограничение включает администратор там, где оно нужно.
DEFAULT_MAX_ACTIVE_CLAIMS = None    # None = без ограничения
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
#
# «Вызван курьер» (`call-courier`) — роль visible, добавлен 19.09.2026. Его
# ставит оператор КЦ, и через него проходят 83% заказов, а в справочнике его не
# было вовсе: как только статус доезжал лентой, заказ пропадал из ленты у всех
# курьеров города и из сетки «Контроля доставки». У владельца брони он держался
# на спецветке «свой заказ виден всегда» — и исчезал вместе с бронью, когда та
# сгорала по таймеру. Именно visible, а не ready (решение владельца
# 19.09.2026): забор по-прежнему разрешает только отметка флориста «Заказ
# готов», иначе курьер поедет за букетом, который ещё собирают.
SEED_VISIBLE_STATUSES = (
    ("send-to-florist", ROLE_VISIBLE),
    ("correction", ROLE_VISIBLE),
    ("call-courier", ROLE_VISIBLE),
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
        # Когда курьер нажал «Я еду». Одно продление на бронь: предупреждение
        # «бронь скоро снимется» до 19.09.2026 звало к кнопке, которой не было
        # вовсе — единственным способом удержать заказ был «Забрал», а его
        # жмут уже в салоне. Продление одно, потому что смысл сгорания — успеть
        # перекинуть заказ другому, и бесконечное «я еду» его отменяет.
        _add_column_if_missing(conn, "delivery_assignments", "extended_at", "TEXT")

        # Последняя преграда инварианта «у заказа не больше одной живой брони»:
        # держит его, даже если появится новый путь записи. Частичный индекс —
        # снятые и доставленные записи не мешают взять заказ снова.
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_assign_one_active "
            "ON delivery_assignments(retailcrm_order_id) "
            f"WHERE state IN ('{STATE_CLAIMED}', '{STATE_PICKED_UP}')"
        )
        # Поиск брони по заказу — для ВСЕХ состояний, а не только живых.
        #
        # Лента и карточка берут последнюю блокирующую запись по заказу
        # (доставлен, проблема — тоже), и без этого индекса SQLite сканировал
        # таблицу броней целиком на КАЖДУЮ строку витрины. Замер 16.09.2026 на
        # 15 тыс. заказов и 12 тыс. броней: 395 мс против 33 мс с индексом, а
        # на сетевом /data это секунды — приложение «тупило» ровно поэтому.
        #
        # Существующие индексы тут не помогают: idx_assign_one_active
        # частичный (только claimed/picked_up), idx_assign_courier_state
        # начинается с курьера, а не с заказа.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_assign_order "
            "ON delivery_assignments(retailcrm_order_id, state)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_assign_courier_state "
            "ON delivery_assignments(courier_user_id, state)"
        )
        # Имя курьера кладём рядом с бронью, а не подтягиваем из users.
        # Учётки живут в barhat.db, брони — в couriers.db: это разные файлы,
        # JOIN между ними невозможен, а второе соединение в горячем пути стоит
        # 90-700 мс на сетевом /data. Плюс имя нужно ровно на момент брони:
        # человека переименуют, а «кто взял заказ 5 сентября» не меняется.
        _add_column_if_missing(conn, "delivery_assignments", "courier_name", "TEXT")

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

        # ====================================================================
        # Ссылки на фото товаров. Заполняются фоном (лента изменений), а не при
        # открытии карточки: внешний вызов из обработчика уже дважды укладывал
        # прод, а карточку курьер открывает на ходу.
        #
        # image_url = NULL означает «у товара фото нет» — и это ЗАПИСЬ, а не
        # её отсутствие. Без такой записи товар без фото становится вечным
        # кандидатом в очереди и заставляет ходить в CRM каждый тик; ровно так
        # выжигалась месячная квота ПланФакта (CLAUDE.md, раздел про квоты).
        # ====================================================================
        # ====================================================================
        # Действие курьера → код статуса в CRM. Заполняет человек.
        #
        # Ровно то правило CLAUDE.md, из-за которого счета уходили в банк без
        # НДС: то, что уходит во внешнюю систему, — данные, а не разбор
        # названия. Статусы в CRM переименовывают и заводят новые, и вывод
        # кода из названия сломается молча.
        #
        # Пустой status_code = действие заблокировано с внятным текстом.
        # Это лучше, чем отправить в CRM «что-нибудь похожее».
        # ====================================================================
        conn.execute("""
            CREATE TABLE IF NOT EXISTS courier_action_statuses (
                action TEXT PRIMARY KEY,
                status_code TEXT,
                updated_by TEXT,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)

        # ====================================================================
        # Что мы отправили в CRM и что она ответила. Очередь и журнал в одной
        # таблице.
        #
        # Отправка НЕ внутри запроса курьера: CRM может отвечать секундами
        # или лежать, а у нас два воркера на весь сайт. Курьер отмечает
        # «Забрал» — запись падает в базу мгновенно, наружу её уносит фон.
        #
        # Журнал не чистится: вопрос «что мы им отправили и что они ответили»
        # возникает всегда, и отвечать на него чтением кода — потерянный час.
        # ====================================================================
        conn.execute("""
            CREATE TABLE IF NOT EXISTS crm_status_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                retailcrm_order_id INTEGER NOT NULL,
                assignment_id INTEGER,
                action TEXT NOT NULL,
                target_status TEXT NOT NULL,
                courier_crm_id INTEGER,
                state TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT,
                sent_at TEXT,
                response_code INTEGER,
                error_message TEXT,
                created_by TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_outbox_pending "
            "ON crm_status_outbox(state, next_attempt_at)"
        )
        # Магазин заказа в CRM. Не косметика: в аккаунте с несколькими
        # магазинами (у нас их десять) RetailCRM отклоняет orders/{id}/edit
        # без этого параметра — «Parameter 'site' is missing», 400. До
        # 11.09.2026 наружу не уходила НИ ОДНА отметка курьера, и снаружи это
        # выглядело как «модуль не меняет статус»: очередь наполнялась,
        # экран работал, ошибка жила только в журнале отправок.
        #
        # Колонка, а не JOIN на витрину в момент отправки: что именно ушло во
        # внешнюю систему, обязано остаться в журнале (CLAUDE.md, история с
        # НДС в банк). Витрину синк перезаписывает кусками — через полчаса
        # ответа на вопрос «с каким site мы отправляли» уже не будет.
        _add_column_if_missing(conn, "crm_status_outbox", "site_code", "TEXT")

        # ====================================================================
        # Подписки на push. Одна строка на устройство: у курьера их бывает две
        # (телефон и планшет), и отписка одного не должна гасить второе.
        # ====================================================================
        conn.execute("""
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                endpoint TEXT NOT NULL UNIQUE,
                p256dh TEXT NOT NULL,
                auth TEXT NOT NULL,
                user_agent TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                last_ok_at TEXT,
                failed_count INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_push_user ON push_subscriptions(user_id)"
        )

        # ====================================================================
        # Журнал отправленных событий — защита от дублей (находка К6).
        #
        # Планировщик крутится в КАЖДОМ воркере, их два. Без этого журнала
        # «новый заказ в городе» уходит курьеру дважды, а повтор тика после
        # ошибки — ещё раз. Уникальный ключ «заказ + событие» делает отправку
        # ровно однократной, и держит это БД, а не аккуратность кода.
        # ====================================================================
        conn.execute("""
            CREATE TABLE IF NOT EXISTS push_events (
                retailcrm_order_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (retailcrm_order_id, event_type)
            )
        """)

        # ====================================================================
        # Что поменялось в заказе после того, как его увидел курьер.
        #
        # Отдельной таблицей, а не колонками витрины: витрину глубокий синк
        # перезаписывает кусками по дате доставки (DELETE + INSERT), и отметка
        # об изменении жила бы до ближайшего прогона — то есть полчаса.
        #
        # Зачем вообще: дату, время и адрес правят в CRM уже после того, как
        # заказ разобрали курьеры. Человек, который видел карточку утром,
        # поедет по старому адресу и к старому времени — узнать об этом он
        # обязан из ленты, а не от клиента (просьба владельца 16.09.2026).
        # ====================================================================
        conn.execute("""
            CREATE TABLE IF NOT EXISTS order_changes (
                retailcrm_order_id INTEGER PRIMARY KEY,
                fields TEXT NOT NULL,          -- date / time / address, через запятую
                changed_at TEXT NOT NULL DEFAULT (datetime('now')),
                seen_at TEXT,
                seen_by INTEGER
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS product_images (
                -- INTEGER, как offer_id в order_items и crm_offers: SQLite не
                -- приравнивает 1 к '1', и текстовый ключ молча не соединился бы
                offer_id INTEGER PRIMARY KEY,
                image_url TEXT,
                checked_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)

        _backfill_ready_seen_at(conn)


# Разовый бэкфилл отметки о сборке. Ключ в sync_state, а не проверка «есть ли
# пустые»: пустые будут всегда (заказ до сборки), и такая проверка означала бы
# скан витрины при каждом старте воркера — а их два.
READY_SEEN_BACKFILL_KEY = "courier_ready_seen_backfill"


def _backfill_ready_seen_at(conn) -> None:
    """
    Проставить `ready_seen_at` заказам, которые СЕЙЧАС в статусе «Заказ готов».

    Больше ниоткуда её взять нельзя: в витрине лежит только текущий статус, а
    история статусов живёт в CRM. Заказы, уже уехавшие дальше по цепочке
    («Вызван курьер», «Выполнен»), остаются без отметки — выдумывать её по
    догадке «наверное, собран» нельзя, это ровно тот случай из CLAUDE.md, где
    неразобранное честнее оставить пустым. Их досчитает лента при следующей
    правке, а новые заказы стамп получают штатно.

    Не роняет старт воркера: без отметки модуль работает как раньше (is_ready
    падает обратно на текущий статус), а вот упавший init оставил бы без
    таблиц весь модуль.
    """
    try:
        done = conn.execute("SELECT value FROM sync_state WHERE key = ?",
                            (READY_SEEN_BACKFILL_KEY,)).fetchone()
        if done:
            return
        codes = [row["status_code"] for row in conn.execute(
            "SELECT status_code FROM courier_visible_statuses WHERE role = ?",
            (ROLE_READY,))]
        if codes:
            conn.execute(
                f"UPDATE courier_orders "
                f"   SET ready_seen_at = COALESCE(synced_at, datetime('now')) "
                f" WHERE ready_seen_at IS NULL "
                f"   AND status IN ({','.join('?' * len(codes))})",
                codes,
            )
        conn.execute(
            "INSERT OR REPLACE INTO sync_state (key, value, updated_at) "
            "VALUES (?, datetime('now'), datetime('now'))",
            (READY_SEEN_BACKFILL_KEY,),
        )
    except Exception as e:
        logger.warning(f"Бэкфилл отметки о сборке не выполнен: {e}")


def city_today(city: Optional[str]) -> str:
    """
    Какое «сегодня» у курьера этого города.

    Дата берётся по стенным часам САЛОНА, а не по UTC и не по часам телефона.
    В 18:31 UTC в Новосибирске уже следующие сутки, и «сегодняшняя» лента по
    серверной дате показала бы вчерашние заказы. На этом обжигались трижды —
    в том числе сами сторожа модуля.

    Пояс не задан — отдаём дату UTC: это честнее, чем угадывать, и сразу видно
    по ленте, что салон не настроен.
    """
    offset = None
    if city:
        with get_db() as conn:
            row = conn.execute(
                "SELECT utc_offset FROM courier_sites "
                " WHERE city = ? AND utc_offset IS NOT NULL LIMIT 1",
                (city,)).fetchone()
        offset = row["utc_offset"] if row else None

    moment = datetime.utcnow()
    if offset is not None:
        moment = salon_time.utc_to_local(moment, offset)
    return moment.date().isoformat()


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
    Задать настройки города. Пустое значение — вернуть поле к умолчанию.

    **Пишутся только те поля, которые переданы.** Экран настроек шлёт три
    числовых поля, и запись «всех колонок сразу» молча обнуляла бы тихие часы
    при каждом сохранении. Отсутствие ключа и пустое значение — разные вещи:
    первое означает «не трогай», второе — «верни к умолчанию».

    Для лимита броней умолчание — это «без ограничения» (решение владельца
    21.09.2026), поэтому очистка поля и есть способ ограничение снять.

    Проверки здесь, а не в обработчике: ручку зовут и форма, и будущие массовые
    действия, а «ноль одновременных броней» означал бы молча выключенный
    модуль в одном городе — поэтому ноль запрещён, а «нет ограничения»
    выражается пустым полем.
    """
    limits = {
        "max_active_claims": (1, 50),
        "claim_horizon_days": (0, 14),
        "unclaimed_alert_minutes": (5, 24 * 60),
    }
    clean: Dict[str, Any] = {}
    for field, (low, high) in limits.items():
        if field not in values:
            continue
        value = values.get(field)
        if value in (None, ""):
            clean[field] = None
            continue
        number = int(value)
        if not (low <= number <= high):
            raise ValueError(f"{field}: допустимо от {low} до {high}, получено {number}")
        clean[field] = number

    for field in ("quiet_hours_from", "quiet_hours_to"):
        if field not in values:
            continue
        value = (values.get(field) or "").strip()
        if not value:
            clean[field] = None
            continue
        if len(value) != 5 or value[2] != ":" or not value.replace(":", "").isdigit():
            raise ValueError(f"{field}: ожидается ЧЧ:ММ, получено «{value}»")
        clean[field] = value

    if not clean:
        return

    columns = list(clean)
    placeholders = ", ".join("?" * (len(columns) + 2))
    updates = ", ".join(f"{name} = excluded.{name}" for name in columns)
    with get_db() as conn:
        conn.execute(
            f"""
            INSERT INTO courier_city_settings
                ({", ".join(["city", *columns, "updated_by"])}, updated_at)
            VALUES ({placeholders}, datetime('now'))
            ON CONFLICT(city) DO UPDATE SET
                {updates},
                updated_by = excluded.updated_by,
                updated_at = datetime('now')
            """,
            (city, *[clean[name] for name in columns], username),
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


def ready_status_codes() -> set:
    """
    Коды статусов, означающих «заказ собран».

    Пустое множество вместо исключения, когда справочника ещё нет: эту функцию
    зовёт глубокий синк витрины, а он обязан работать и до того, как поднялись
    таблицы модуля доставки (порядок init в pyrus/server.py — сначала витрина,
    потом доставка). Без отметки готовность падает обратно на текущий статус —
    то есть на поведение до 19.09.2026, а не на пустую витрину.
    """
    try:
        return set(visible_status_codes().get(ROLE_READY, []))
    except sqlite3.OperationalError:
        return set()


def ready_stamp(status: Optional[str], previous: Optional[str],
                ready_codes, now: Optional[str] = None) -> Optional[str]:
    """
    Отметка «заказ побывал собранным» — то, что пишется в `ready_seen_at`.

    Статус в CRM — это точка на линии, а не состояние: живой путь заказа
    («Передан флористу → Заказ готов → Вызван курьер → Выполнен») ведёт ДАЛЬШЕ
    отметки о сборке. Пока готовность считалась как «текущий статус равен
    order-complete», каждый следующий шаг оператора возвращал собранный заказ
    в «Собирают» и запрещал забор (разбор 19.09.2026, заказ 154553).

    Поэтому отметка ставится один раз и НЕ снимается: заказ, который однажды
    собрали, собранным и остаётся. Обратный ход («статус вернули назад, значит
    заказ разобрали») сознательно не поддерживается — он бывает у правок
    оператора, а букет от этого не рассыпается.
    """
    if previous:
        return previous
    if status and status in ready_codes:
        return now or datetime.utcnow().isoformat(sep=" ", timespec="seconds")
    return None


def mark_ready_seen(order_ids: List[int], now: Optional[str] = None) -> int:
    """
    Поставить отметку о сборке заказам, которые ПРОШЛИ через ready-статус.

    Зовётся лентой по записям истории: перечитанный заказ показывает только
    текущий статус, а он к этому моменту мог уехать дальше. Ставится там, где
    её ещё нет, — уже проставленную не двигаем, иначе «когда собрали» будет
    временем последней правки заказа.
    """
    if not order_ids:
        return 0
    stamp = now or datetime.utcnow().isoformat(sep=" ", timespec="seconds")
    ids = [int(order_id) for order_id in order_ids]
    marked = 0
    with get_db() as conn:
        for start in range(0, len(ids), 400):   # потолок переменных SQLite
            chunk = ids[start:start + 400]
            cursor = conn.execute(
                f"UPDATE courier_orders SET ready_seen_at = ? "
                f" WHERE ready_seen_at IS NULL "
                f"   AND retailcrm_order_id IN ({','.join('?' * len(chunk))})",
                [stamp, *chunk],
            )
            marked += cursor.rowcount or 0
    return marked


def is_ready_value(status: Optional[str], ready_seen_at: Optional[str],
                   ready_codes) -> bool:
    """
    Собран ли заказ — единственное место, где это решается.

    Отметка важнее статуса, но статус остаётся запасным путём: строки, до
    которых ещё не дошёл бэкфилл, и заказы, пришедшие мимо обоих путей записи,
    иначе выглядели бы несобранными.
    """
    return bool(ready_seen_at) or (status in ready_codes)


# ---------------------------------------------------------------------------
# Заказы для курьера
# ---------------------------------------------------------------------------

# Поля, которые видит любой курьер города: по ним он решает, брать заказ или нет.
PUBLIC_ORDER_FIELDS = (
    "retailcrm_order_id", "order_number", "delivery_date", "site_code", "city",
    "store_key",
    # Адрес отдаётся ЦЕЛИКОМ и до брони (решение владельца 23.09.2026). Он
    # приходит из CRM одной строкой, которую заводят руками, и формат у неё
    # разный по городам: «ул. Ленина, 45, кв. 12» в Новосибирске против
    # «Свердловская область, Екатеринбург, ул. Бажова, 89» в ЕКБ. Прежнее
    # сокращение резало первые две части — на екатеринбургских адресах это
    # оставляло «Свердловская область, Екатеринбург», то есть курьер не видел
    # ни улицы, ни дома и не мог решить, брать ли заказ, а «Маршрут» вёл в
    # центр города. Любой разбор такой строки ломается на следующем написании,
    # поэтому не разбираем её вовсе.
    "address_text",
    "delivery_time_from", "delivery_time_to",
    "ready_time", "ready_planned_at", "status",
    # «Не связываться с получателем» — не персональные данные, а указание, как
    # везти. Пока флаг лежал среди контактов, курьер узнавал о нём только
    # после брони: до неё карточка приходила без него, и сюрприз-доставку
    # было нечем отличить от обычной ровно в тот момент, когда решают, брать
    # заказ или нет.
    "do_not_contact_recipient",
)

# Поля с персональными данными: отдаются только тому, кто взял заказ (и
# управляющему). До брони курьеру хватает адреса и времени — телефоны,
# имена и комментарии он «просто просматривать» не должен.
PRIVATE_ORDER_FIELDS = (
    "recipient_name", "recipient_phone", "customer_name", "customer_phone",
    "manager_comment", "customer_comment", "note_text",
    "recipient_is_customer",
)


def _change_titles(fields: Optional[str]) -> List[str]:
    """«date,address» → ['дата', 'адрес'] — готовыми словами для экрана."""
    if not fields:
        return []
    return [CHANGE_TITLES[key] for key in fields.split(",") if key in CHANGE_TITLES]


def list_orders_for_courier(city: Optional[str], date_from: str, date_to: str,
                            courier_user_id: Optional[int] = None,
                            with_private: bool = False,
                            courier_delivery_codes: Optional[List[str]] = None,
                            timings: Optional[Dict[str, float]] = None
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

    `timings` — необязательный словарь, куда складывается цена каждого шага в
    миллисекундах. Нужен разбору «почему лента отвечает минуту»: 16.09.2026
    ручка стоила 53–113 секунд на проде при форме запроса, которая обязана
    укладываться в десятки миллисекунд, и по коду причину не видно. Правило
    CLAUDE.md: следующий разбор начинается с числа, а не с чтения кода.
    Словарь только заполняется — на поведение выборки он не влияет.
    """
    def _mark(name: str, started: float) -> float:
        """Запомнить цену шага и вернуть точку отсчёта для следующего."""
        now = time.monotonic()
        if timings is not None:
            timings[name] = round((now - started) * 1000, 1)
        return now

    step = time.monotonic()
    codes = visible_status_codes()
    step = _mark("visible_codes", step)
    visible = codes.get(ROLE_VISIBLE, []) + codes.get(ROLE_READY, [])
    if not visible:
        # Пустой справочник — это не «показать всё», а «настройка не сделана».
        # Показать всё означало бы вывалить курьеру отменённые и выполненные.
        return []
    ready = set(codes.get(ROLE_READY, []))

    # Свой забронированный заказ виден курьеру ВСЕГДА, даже когда его статус
    # ушёл из видимых. Живой путь заказа — «Передан флористу → Заказ готов →
    # Вызван курьер → Выполнен», и на третьем шаге заказ иначе пропадал бы из
    # «Моих» ровно у того, кто его везёт. Бронь при этом жива (см.
    # release_orphan_claims), и заказ без строки в ленте выглядел бы как сбой.
    status_condition = f"o.status IN ({','.join('?' * len(visible))})"
    params: List[Any] = [date_from, date_to, *visible]
    if courier_user_id is not None:
        status_condition = f"({status_condition} OR a.courier_user_id = ?)"
        params.append(courier_user_id)

    conditions = ["o.delivery_date >= ?", "o.delivery_date <= ?", status_condition]

    if city:
        conditions.append("o.city = ?")
        params.append(city)

    if courier_delivery_codes:
        conditions.append(
            f"o.delivery_code IN ({','.join('?' * len(courier_delivery_codes))})")
        params.extend(courier_delivery_codes)

    # Заказ, у которого в поле «курьер» стоит служба доставки, уехал аутсорсу.
    #
    # Оператор передаёт невзятый заказ Яндексу двумя способами: меняет тип
    # доставки (это лента ловила и раньше) или просто ставит службу курьером,
    # не трогая тип. Второй путь до 11.09.2026 не обрабатывался вовсе — заказ
    # оставался свободным, и наш курьер мог поехать за букетом, который уже
    # везёт Яндекс.
    #
    # Признак берётся из справочника (`couriers.is_service`), где его правит
    # человек, а не из разбора названия: агрегаторов заводят новых, и
    # регулярка сломается молча (CLAUDE.md).
    #
    # Своя бронь — исключение: заказ, уже забранный курьером, обязан остаться
    # у него на экране вместе с адресом. Бронь снимет уборка ленты, и курьер
    # узнает об этом пушем, а не исчезнувшей карточкой.
    outsourced_condition = "COALESCE(c.is_service, 0) = 0"
    if courier_user_id is not None:
        outsourced_condition = f"({outsourced_condition} OR a.courier_user_id = ?)"
        params.append(courier_user_id)
    conditions.append(outsourced_condition)

    sql = f"""
        SELECT o.*, s.name AS site_name, s.utc_offset,
               a.id AS assignment_id, a.state AS assignment_state,
               a.courier_user_id AS assignment_user_id,
               a.courier_name AS assignment_courier_name,
               ch.fields AS changed_fields, ch.changed_at AS changed_at
        FROM courier_orders o
        LEFT JOIN courier_sites s ON s.code = o.site_code
        LEFT JOIN couriers c ON c.id = o.courier_id
        LEFT JOIN order_changes ch
               ON ch.retailcrm_order_id = o.retailcrm_order_id
              AND ch.seen_at IS NULL
        LEFT JOIN delivery_assignments a
               ON a.id = (SELECT MAX(x.id) FROM delivery_assignments x
                           WHERE x.retailcrm_order_id = o.retailcrm_order_id
                             AND x.state IN ({BLOCKING_STATES_SQL}))
        WHERE {' AND '.join(conditions)}
        ORDER BY o.delivery_date, o.delivery_time_from IS NULL, o.delivery_time_from
    """

    # Открытие соединения и сам запрос меряются ПОРОЗНЬ: это два разных
    # диагноза. На сетевом /data одно открытие — это три файла (.db, -wal,
    # -shm) и десятки миллисекунд, а долгий SELECT означает либо объём чтения,
    # либо ожидание блокировки. По суммарному времени ручки их не различить.
    #
    # `get_db()` здесь — генератор-контекстменеджер (см. couriers/storage.py),
    # соединение он открывает на входе в блок и закрывает в своём finally.
    # Поэтому отметка стоит первой строкой ТЕЛА: до неё как раз уместился
    # connect.
    with get_db() as conn:
        step = _mark("connect", step)
        rows = [dict(row) for row in conn.execute(sql, params).fetchall()]
        step = _mark("query", step)

    result = []
    for row in rows:
        mine = (courier_user_id is not None
                and row.get("assignment_user_id") == courier_user_id)
        item = {field: row.get(field) for field in PUBLIC_ORDER_FIELDS}
        item.update({
            "site_name": row.get("site_name"),
            "utc_offset": row.get("utc_offset"),
            "is_ready": is_ready_value(row.get("status"),
                                       row.get("ready_seen_at"), ready),
            "assignment_state": row.get("assignment_state"),
            "is_mine": mine,
            "is_free": row.get("assignment_state") is None,
            # Имя того, кто взял заказ: «Забронирован (Иван)» вместо глухого
            # «Занят». Курьер видит, что происходит со всеми заказами города
            "assignment_courier_name": row.get("assignment_courier_name"),
            # Кто именно держит бронь. Не для экрана, а для рассылки:
            # уведомление «ваш заказ собран» адресуется владельцу брони, и без
            # этого поля оно не уходило вовсе — адресат получался пустым.
            "assignment_user_id": row.get("assignment_user_id"),
            # Что поменялось в заказе после того, как его увидели: дата, время
            # или адрес. Курьер планирует по ним ходку, и узнавать о правке от
            # клиента у двери — поздно.
            "changed_fields": _change_titles(row.get("changed_fields")),
            "changed_at": row.get("changed_at"),
        })
        # Контакты — только по своей брони либо управляющему.
        if with_private or mine:
            item.update({field: row.get(field) for field in PRIVATE_ORDER_FIELDS})
        result.append(item)

    _mark("serialize", step)
    if timings is not None:
        timings["rows"] = len(rows)
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
        row = conn.execute(f"""
            SELECT o.*, s.name AS site_name, s.utc_offset,
                   a.id AS assignment_id, a.state AS assignment_state,
                   a.courier_user_id AS assignment_user_id,
                   a.courier_name AS assignment_courier_name,
                   ch.fields AS changed_fields, ch.changed_at AS changed_at
            FROM courier_orders o
            LEFT JOIN courier_sites s ON s.code = o.site_code
            LEFT JOIN order_changes ch
                   ON ch.retailcrm_order_id = o.retailcrm_order_id
                  AND ch.seen_at IS NULL
            LEFT JOIN delivery_assignments a
                   ON a.id = (SELECT MAX(x.id) FROM delivery_assignments x
                               WHERE x.retailcrm_order_id = o.retailcrm_order_id
                                 AND x.state IN ({BLOCKING_STATES_SQL}))
            WHERE o.retailcrm_order_id = ?
        """, (order_id,)).fetchone()
        if not row:
            return None
        row = dict(row)
        # Фото приезжает готовой ссылкой из своей таблицы: карточку открывают
        # с телефона на ходу, и ходить за ней в CRM в этот момент нельзя.
        items = [dict(item) for item in conn.execute(
            "SELECT i.offer_id, i.product_name, i.article, i.quantity, p.image_url "
            "  FROM order_items i "
            "  LEFT JOIN product_images p ON p.offer_id = i.offer_id "
            " WHERE i.retailcrm_order_id = ? ORDER BY i.product_name",
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
        "is_ready": is_ready_value(row.get("status"),
                                   row.get("ready_seen_at"), ready),
        "assignment_state": row.get("assignment_state"),
        "is_mine": mine,
        "is_free": row.get("assignment_state") is None,
        "assignment_courier_name": row.get("assignment_courier_name"),
        "changed_fields": _change_titles(row.get("changed_fields")),
        "changed_at": row.get("changed_at"),
        "items": items,
        # Себестоимость доставки — это оплата курьеру за ходку. В ленте её
        # нет намеренно: список с ценниками превращает свободный захват в
        # разбор заказов по выгодности, а дальние и дешёвые повисают
        # (черри-пикинг, §3 плана). В карточке её видит тот, кто уже открыл
        # конкретный заказ.
        "net_cost": row.get("net_cost"),
    })
    if with_private or mine:
        card.update({field: row.get(field) for field in PRIVATE_ORDER_FIELDS})
    return card


# ---------------------------------------------------------------------------
# Бронь заказа (Фаза 4)
# ---------------------------------------------------------------------------

class ClaimError(Exception):
    """
    Бронь не состоялась по понятной причине.

    `code` нужен, чтобы ручка отдала правильный HTTP-статус: «занят» — это
    409 и предложение обновить список, «чужой город» — 403, «не тот день» —
    400. Один статус на все случаи заставил бы фронт разбирать текст.
    """

    def __init__(self, message: str, code: str = "conflict"):
        super().__init__(message)
        self.code = code


def _assignment_row(conn, order_id: int):
    """Живая бронь заказа, если она есть."""
    return conn.execute(
        "SELECT * FROM delivery_assignments "
        " WHERE retailcrm_order_id = ? AND state IN (?, ?)",
        (order_id, STATE_CLAIMED, STATE_PICKED_UP),
    ).fetchone()


def _blocking_assignment_row(conn, order_id: int):
    """
    Запись, из-за которой заказ нельзя забронировать заново.

    Шире живой брони: сюда входят доставленный заказ и заказ с отмеченной
    проблемой. Иначе заказ уходит на второй круг — 16.09.2026 доставленный
    заказ снова появлялся свободным в ленте.

    Берём последнюю: у заказа бывает история (взял → отказался → взял другой),
    и человеку важно последнее состояние, а не первое.
    """
    placeholders = ",".join("?" * len(BLOCKING_STATES))
    return conn.execute(
        f"SELECT * FROM delivery_assignments "
        f" WHERE retailcrm_order_id = ? AND state IN ({placeholders}) "
        f" ORDER BY id DESC LIMIT 1",
        (order_id, *BLOCKING_STATES),
    ).fetchone()


def claim_order(order_id: int, courier_user_id: int, courier_name: str,
                city: Optional[str], allow_any_city: bool = False,
                courier_crm_id: Optional[int] = None,
                username: Optional[str] = None) -> Dict[str, Any]:
    """
    Забронировать заказ за курьером.

    **Проверка и запись — одна транзакция под `BEGIN IMMEDIATE`** (правило
    CLAUDE.md). На проде до 16 параллельных обработчиков, а запрос к базе
    стоит 90-700 мс: между «свободен ли заказ» в одном соединении и `INSERT`
    в другом лежит окно шириной в сотни миллисекунд, и в него проходят все
    нажатия разом. Ровно так 29.08.26 открылись три смены на одной точке.

    Инвариант держат три уровня, и нужны все три: частичный уникальный индекс
    в схеме, эта транзакция и блокировка кнопки на фронте.
    """
    now = datetime.utcnow()
    conn = sqlite_connect(DB_PATH, timeout=30)
    conn.isolation_level = None          # транзакцией управляем сами
    try:
        conn.execute("BEGIN IMMEDIATE")  # write-лок ДО чтения

        order = conn.execute("""
            SELECT o.retailcrm_order_id, o.order_number, o.city, o.status,
                   o.delivery_date, o.delivery_time_from, s.utc_offset
              FROM courier_orders o
              LEFT JOIN courier_sites s ON s.code = o.site_code
             WHERE o.retailcrm_order_id = ?
        """, (order_id,)).fetchone()

        if order is None:
            raise ClaimError("Заказ не найден", "not_found")
        if not allow_any_city and order["city"] != city:
            raise ClaimError("Этот заказ не вашего города", "forbidden")

        # Тем же соединением, что и всё остальное в транзакции: вызов
        # visible_status_codes() открыл бы второе соединение к той же базе,
        # пока мы держим write-лок, — вложенным соединением здесь уже дважды
        # вешали запись.
        visible = {row["status_code"] for row in conn.execute(
            "SELECT status_code FROM courier_visible_statuses")}
        if order["status"] not in visible:
            # Статус мог уйти, пока экран не обновился: заказ отменили или
            # передали службе доставки
            raise ClaimError("Заказ больше не доступен для доставки", "gone")

        settings = _city_settings_locked(conn, order["city"])

        _check_horizon(order, settings["claim_horizon_days"], now)

        taken = _blocking_assignment_row(conn, order_id)
        if taken is not None:
            who = taken["courier_name"] or "другой курьер"
            mine = taken["courier_user_id"] == courier_user_id
            if taken["state"] == STATE_DELIVERED:
                raise ClaimError(
                    "Этот заказ уже доставлен" if mine
                    else f"Заказ уже доставил {who}", "delivered")
            if taken["state"] == STATE_PROBLEM:
                # Букет физически у курьера, и пока человек не разобрался,
                # отдавать заказ второму нельзя
                raise ClaimError(
                    "По заказу отмечена проблема — обратитесь к управляющему",
                    "problem")
            if mine:
                raise ClaimError("Этот заказ уже ваш", "already_mine")
            raise ClaimError(f"Заказ уже забрал {who}", "taken")

        # Лимит может быть не задан вовсе — это штатное состояние, а не ошибка
        # настройки: по умолчанию ограничения нет, его включает администратор
        # там, где оно нужно. Без этой проверки сравнение с None падало бы.
        limit = settings["max_active_claims"]
        if limit is not None:
            active = conn.execute(
                "SELECT COUNT(*) AS cnt FROM delivery_assignments "
                " WHERE courier_user_id = ? AND state IN (?, ?)",
                (courier_user_id, STATE_CLAIMED, STATE_PICKED_UP),
            ).fetchone()["cnt"]
            if active >= limit:
                raise ClaimError(
                    f"У вас уже {active} заказ(а) в работе — это предел для "
                    f"города. Завершите или отпустите один из них.", "limit")

        # Срока у брони НЕТ (решение владельца 21.09.2026): она держится, пока
        # курьер не откажется сам или её не снимет управляющий.
        #
        # Раньше здесь считался `expires_at`, и правило «сгорает за 60 минут до
        # окна, но живёт хотя бы 30 минут» на практике означало вот что: заказ,
        # взятый за 45 минут до доставки, сгорал за 15 минут до неё. А забрать
        # его курьер всё это время НЕ МОГ — «Заказ готов» флорист ставит в
        # момент начала окна. То есть модуль отбирал заказ у человека за то,
        # что тому нечего было нажать.
        #
        # Взамен таймера — сигнал управляющему: «забронирован, но не забран, а
        # окно близко» (см. dispatch_overview). Решение принимает человек,
        # кнопка снятия брони у него уже есть.
        cursor = conn.execute(
            "INSERT INTO delivery_assignments "
            "  (retailcrm_order_id, courier_user_id, courier_name, state, "
            "   claimed_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, NULL)",
            (order_id, courier_user_id, courier_name, STATE_CLAIMED,
             now.isoformat(sep=" ", timespec="seconds")),
        )

        # Бронь уходит в CRM (решение владельца 2026-09-11): оператор должен
        # видеть, кто повезёт заказ, не дожидаясь забора. Той же транзакцией —
        # иначе бывает бронь, о которой CRM не узнает.
        #
        # Уходит курьер и — если для брони выбран статус — статус. Статус
        # именно настройкой, а не константой: в это поле пишут ещё флорист и
        # оператор, и какой код ставить (и ставить ли вообще), решает человек,
        # а не код. Пустой статус бронь не блокирует.
        #
        # Тем же соединением, что и вся транзакция: отдельный вызов открыл бы
        # второе соединение под уже взятым write-локом.
        claim_status = (conn.execute(
            "SELECT status_code FROM courier_action_statuses WHERE action = ?",
            (ACTION_CLAIM,)).fetchone() or {"status_code": None})["status_code"]

        # Ни статуса, ни связки с курьером — отправлять нечего, и пустая
        # задача только занимала бы очередь
        if claim_status or courier_crm_id:
            _enqueue_locked(conn, order_id, cursor.lastrowid, ACTION_CLAIM,
                            claim_status,
                            int(courier_crm_id) if courier_crm_id else None,
                            username or courier_name)

        conn.execute("COMMIT")
        return {
            "retailcrm_order_id": order_id,
            "order_number": order["order_number"],
            "state": STATE_CLAIMED,
        }
    except ClaimError:
        conn.execute("ROLLBACK")
        raise
    except sqlite3.IntegrityError:
        # Уникальный индекс сработал: соседний запрос успел вставить бронь
        # между нашей проверкой и записью. Это штатный исход гонки, а не 500.
        conn.execute("ROLLBACK")
        raise ClaimError("Заказ только что забрал другой курьер", "taken")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def _city_settings_locked(conn, city: Optional[str]) -> Dict[str, Any]:
    """
    Настройки города тем же соединением, что и проверка.

    Отдельным вызовом `city_settings()` это было бы второе соединение внутри
    открытой транзакции — то самое вложенное соединение, которым уже дважды
    вешали запись в общую базу.
    """
    row = conn.execute(
        "SELECT * FROM courier_city_settings WHERE city = ?", (city,)
    ).fetchone() if city else None

    def pick(field, default):
        value = row[field] if row is not None else None
        return default if value is None else value

    return {
        "max_active_claims": pick("max_active_claims", DEFAULT_MAX_ACTIVE_CLAIMS),
        "claim_horizon_days": pick("claim_horizon_days", DEFAULT_CLAIM_HORIZON_DAYS),
    }


def _check_horizon(order, horizon_days: int, now: datetime) -> None:
    """
    Нельзя забить неделю с утра: бронировать можно сегодня и ещё N дней.

    «Сегодня» — по стенным часам САЛОНА, а не сервера. В UTC+7 рабочий день
    начинается, когда в UTC ещё вчера, и курьер из Новосибирска в девять утра
    получал бы «этот заказ на завтра».
    """
    offset = order["utc_offset"]
    if offset is None:
        return          # пояс не задан — не наказываем курьера за настройку
    today = salon_time.utc_to_local(now, offset).date()
    try:
        target = datetime.strptime(order["delivery_date"], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return
    if target < today:
        raise ClaimError("Дата доставки уже прошла", "horizon")
    if (target - today).days > horizon_days:
        raise ClaimError(
            f"Заказ на {target.strftime('%d.%m.%Y')} — бронировать можно "
            f"только ближайшие дни", "horizon")


def release_order(order_id: int, courier_user_id: int,
                  reason: str = RELEASE_SELF,
                  allow_any_courier: bool = False) -> Dict[str, Any]:
    """
    Отпустить бронь.

    Тоже под `BEGIN IMMEDIATE`: между «бронь ещё моя» и записью успевает
    пройти фоновое автоснятие (находка К3 критики плана).

    Запись не удаляется, а помечается: «кто и почему отпустил заказ» — вход
    для разговора с курьером, и стирать это нельзя.
    """
    conn = sqlite_connect(DB_PATH, timeout=30)
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _assignment_row(conn, order_id)
        if row is None:
            raise ClaimError("Бронь уже снята", "gone")
        if not allow_any_courier and row["courier_user_id"] != courier_user_id:
            raise ClaimError("Это чужая бронь", "forbidden")
        if row["state"] == STATE_PICKED_UP and not allow_any_courier:
            # Заказ физически у курьера: «отпустить» его кнопкой нельзя,
            # иначе букет уедет неизвестно куда
            raise ClaimError("Заказ уже забран — отпустить его может только "
                             "управляющий", "picked_up")

        conn.execute(
            "UPDATE delivery_assignments SET state = ?, released_at = ?, "
            "       release_reason = ? WHERE id = ?",
            (STATE_RELEASED, datetime.utcnow().isoformat(sep=" ", timespec="seconds"),
             reason, row["id"]),
        )
        conn.execute("COMMIT")
        return {"retailcrm_order_id": order_id, "state": STATE_RELEASED,
                "release_reason": reason}
    except Exception:
        # Откат в одном месте, а не у каждого raise: второй ROLLBACK по уже
        # закрытой транзакции сам бросает исключение и подменяет причину
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


# --- действия курьера, уходящие в CRM --------------------------------------
# Бронь стоит особняком (решение владельца 2026-09-11): она проставляет в CRM
# курьера и, если для неё выбран статус, ставит ещё и его.
#
# Отличие от остальных действий — в том, что пустой статус её НЕ блокирует.
# «Забрал» без настроенного статуса выполнять нельзя: смысл действия в том,
# чтобы CRM узнала о нём. Бронь же осмысленна и сама по себе — она живёт у
# нас, и курьер должен иметь возможность взять заказ, даже пока справочник
# не заполнен.
ACTION_CLAIM = "claim"
ACTION_PICKUP = "pickup"
ACTION_DELIVER = "deliver"
ACTION_NO_ANSWER = "no_answer"
ACTION_RESCHEDULE = "reschedule"
ACTION_REFUSED = "refused"
ACTION_WRONG_ADDRESS = "wrong_address"

# Причины «не получилось» — кнопками, а не вводом текста: набирать за рулём
# никто не будет, и данные разъедутся с жизнью (§3 плана).
PROBLEM_ACTIONS = {
    ACTION_NO_ANSWER: "Не дозвонился",
    ACTION_RESCHEDULE: "Просят привезти позже",
    ACTION_REFUSED: "Отказ от заказа",
    ACTION_WRONG_ADDRESS: "Адрес не тот",
}

ALL_ACTIONS = {
    ACTION_CLAIM: "Забронировал заказ",
    ACTION_PICKUP: "Забрал заказ",
    ACTION_DELIVER: "Доставил",
    **PROBLEM_ACTIONS,
}

# Действия, которые без настроенного статуса выполнять нельзя. Бронь сюда не
# входит: она живёт у нас и осмысленна сама по себе, а «Забрал» без статуса
# теряет смысл — CRM о заборе не узнает.
BLOCKED_WITHOUT_STATUS = tuple(a for a in ALL_ACTIONS if a != ACTION_CLAIM)

# Заголовки для журнала отправок.
OUTBOX_ACTION_TITLES = dict(ALL_ACTIONS)

OUTBOX_PENDING = "pending"
OUTBOX_SENT = "sent"
OUTBOX_FAILED = "failed"

# Сколько ждать перед повтором и сколько попыток делать. CRM может лежать
# минутами; курьер этого ждать не должен, а мы не должны долбить её в цикле.
OUTBOX_RETRY_SECONDS = 300
OUTBOX_MAX_ATTEMPTS = 8


def list_action_statuses() -> List[Dict[str, Any]]:
    """Справочник «действие → статус CRM» со всеми действиями, включая пустые."""
    with get_db() as conn:
        rows = {row["action"]: dict(row) for row in conn.execute(
            "SELECT * FROM courier_action_statuses")}
    return [
        {
            "action": action,
            "title": title,
            "status_code": (rows.get(action) or {}).get("status_code"),
            "updated_by": (rows.get(action) or {}).get("updated_by"),
            "updated_at": (rows.get(action) or {}).get("updated_at"),
            "is_problem": action in PROBLEM_ACTIONS,
            # Пустой статус блокирует не всякое действие: бронь без него
            # просто не меняет статус в CRM, а курьера проставляет
            "blocks_when_empty": action in BLOCKED_WITHOUT_STATUS,
        }
        for action, title in ALL_ACTIONS.items()
    ]


def set_action_status(action: str, status_code: Optional[str], username: str) -> None:
    if action not in ALL_ACTIONS:
        raise ValueError(f"Неизвестное действие: {action}")
    code = (status_code or "").strip() or None
    with get_db() as conn:
        if code is not None:
            known = conn.execute(
                "SELECT 1 FROM order_statuses WHERE code = ?", (code,)).fetchone()
            if known is None:
                # Код придумали руками — в CRM его нет, и отправка молча
                # потеряла бы статус. Это ровно история с НДС в банк.
                raise ValueError(f"Статуса «{code}» нет в справочнике CRM")
        conn.execute(
            "INSERT INTO courier_action_statuses (action, status_code, updated_by, updated_at) "
            "VALUES (?, ?, ?, datetime('now')) "
            "ON CONFLICT(action) DO UPDATE SET status_code = excluded.status_code, "
            "  updated_by = excluded.updated_by, updated_at = excluded.updated_at",
            (action, code, username),
        )


def _action_status_locked(conn, action: str) -> str:
    """
    Код статуса CRM для действия — или отказ с внятным текстом.

    Пустой маппинг ОСТАНАВЛИВАЕТ действие, а не отправляет в CRM что-нибудь
    похожее: параметр внешней системы — данные, а не догадка (CLAUDE.md).
    """
    row = conn.execute(
        "SELECT status_code FROM courier_action_statuses WHERE action = ?",
        (action,)).fetchone()
    code = row["status_code"] if row else None
    if not code:
        raise ClaimError(
            f"Для действия «{ALL_ACTIONS.get(action, action)}» не выбран статус "
            f"в CRM. Попросите администратора настроить справочник.",
            "not_configured")
    return code


def _enqueue_locked(conn, order_id: int, assignment_id: Optional[int], action: str,
                    status_code: Optional[str], courier_crm_id: Optional[int],
                    username: str) -> None:
    """
    Положить отправку в очередь тем же соединением, что и смену состояния.

    Магазин заказа читается здесь же, а не передаётся вызывающим: параметр
    обязателен для RetailCRM, и место, где о нём можно забыть, должно быть
    ровно одно. Тем же соединением — второе соединение под открытым
    write-локом здесь уже дважды вешало запись.

    Заказ без магазина наружу не уходит вовсе: задача сразу помечается
    неудачной с внятным текстом. Отправить запрос, который заведомо отклонят,
    — это занятый воркер и пустая трата попытки (CLAUDE.md про параметры
    внешних систем).
    """
    row = conn.execute(
        "SELECT site_code FROM courier_orders WHERE retailcrm_order_id = ?",
        (order_id,)).fetchone()
    site_code = ((row["site_code"] if row else None) or "").strip() or None

    state = OUTBOX_PENDING if site_code else OUTBOX_FAILED
    error = None if site_code else (
        "У заказа не определён магазин в CRM — отправка остановлена. "
        "Дождитесь синхронизации заказа и повторите.")

    conn.execute(
        "INSERT INTO crm_status_outbox "
        "  (retailcrm_order_id, assignment_id, action, target_status, "
        "   courier_crm_id, site_code, state, next_attempt_at, error_message, "
        "   created_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), ?, ?, datetime('now'))",
        # Пустая строка, а не NULL: колонка заведена NOT NULL, а задача без
        # статуса — штатный случай (бронь отправляет только курьера).
        # При отправке пустая строка снова становится «статус не трогаем».
        (order_id, assignment_id, action, status_code or "", courier_crm_id,
         site_code, state, error, username),
    )


def advance_assignment(order_id: int, courier_user_id: int, action: str,
                       username: str, courier_crm_id: Optional[int] = None,
                       problem_note: Optional[str] = None) -> Dict[str, Any]:
    """
    Отметка курьера: «Забрал», «Доставил» или проблема.

    Состояние меняется у нас и попадает в очередь отправки ОДНОЙ транзакцией.
    Если бы очередь наполнялась отдельно, падение между двумя записями давало
    бы либо доставленный заказ, о котором CRM не узнает, либо отправку статуса
    по несостоявшемуся действию.

    Курьер не ждёт CRM: наружу запись уносит фон. CRM отвечает секундами и
    иногда лежит, а воркеров у сайта два.
    """
    now = datetime.utcnow().isoformat(sep=" ", timespec="seconds")
    conn = sqlite_connect(DB_PATH, timeout=30)
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")

        row = _assignment_row(conn, order_id)
        if row is None:
            raise ClaimError("Заказ за вами не числится", "gone")
        if row["courier_user_id"] != courier_user_id:
            raise ClaimError("Это чужой заказ", "forbidden")

        status_code = _action_status_locked(conn, action)

        if action == ACTION_PICKUP:
            if row["state"] != STATE_CLAIMED:
                raise ClaimError("Заказ уже забран", "already")
            # Забрать можно только собранный заказ (решение владельца
            # 2026-09-11). До этого забор у неготового заказа проходил через
            # подтверждение «всё равно забираю» — теперь он запрещён совсем.
            #
            # Чем это грозит, знать полезно: по замеру 2026-09-08 статус
            # «Заказ готов» ставят в момент начала окна доставки, а у 34%
            # заказов уже после него. То есть запрет упирается не в курьера,
            # а в дисциплину отметки: пока флорист не щёлкнул статус, курьер
            # стоит в салоне. Доля таких случаев видна в метриках — если
            # окажется высокой, лечится это дисциплиной или настройкой, а не
            # возвратом к подтверждению.
            #
            # Готовность берётся из отметки `ready_seen_at`, а не из текущего
            # статуса: статус уходит дальше по цепочке («Вызван курьер»,
            # «Выполнен»), и сравнение с текущим запрещало забор у заказа,
            # который флорист давно собрал (разбор 19.09.2026).
            ready = {code["status_code"] for code in conn.execute(
                "SELECT status_code FROM courier_visible_statuses WHERE role = ?",
                (ROLE_READY,))}
            order_status = conn.execute(
                "SELECT status, ready_seen_at FROM courier_orders "
                " WHERE retailcrm_order_id = ?",
                (order_id,)).fetchone()
            is_ready = bool(order_status and is_ready_value(
                order_status["status"], order_status["ready_seen_at"], ready))
            if not is_ready:
                raise ClaimError(
                    "Заказ ещё не отмечен готовым — забрать его нельзя. "
                    "Дождитесь, пока флорист отметит сборку.", "not_ready")
            conn.execute(
                "UPDATE delivery_assignments SET state = ?, picked_up_at = ? "
                " WHERE id = ?",
                (STATE_PICKED_UP, now, row["id"]),
            )
        elif action == ACTION_DELIVER:
            if row["state"] != STATE_PICKED_UP:
                raise ClaimError("Сначала отметьте, что забрали заказ", "order")
            conn.execute(
                "UPDATE delivery_assignments SET state = ?, delivered_at = ? WHERE id = ?",
                (STATE_DELIVERED, now, row["id"]),
            )
        elif action in PROBLEM_ACTIONS:
            conn.execute(
                "UPDATE delivery_assignments SET state = ?, problem_code = ?, "
                "       problem_note = ? WHERE id = ?",
                (STATE_PROBLEM, action, problem_note, row["id"]),
            )
        else:
            raise ClaimError(f"Неизвестное действие: {action}", "bad_action")

        # courierId пишем только вместе с «Забрал»: бронь может слететь, а
        # факт «повёз» — уже нет. Если оператор проставил другого курьера,
        # наш перезапишет — поэтому и только в этот момент.
        _enqueue_locked(conn, order_id, row["id"], action, status_code,
                        courier_crm_id if action == ACTION_PICKUP else None,
                        username)

        conn.execute("COMMIT")
        return {"retailcrm_order_id": order_id, "action": action,
                "target_status": status_code}
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def take_outbox_batch(limit: int = 20) -> List[Dict[str, Any]]:
    """Что пора отправить в CRM."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM crm_status_outbox "
            " WHERE state = ? AND (next_attempt_at IS NULL OR next_attempt_at <= datetime('now')) "
            " ORDER BY id LIMIT ?",
            (OUTBOX_PENDING, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def mark_outbox_sent(outbox_id: int, response_code: Optional[int] = 200) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE crm_status_outbox SET state = ?, sent_at = datetime('now'), "
            "       attempts = attempts + 1, response_code = ?, error_message = NULL "
            " WHERE id = ?",
            (OUTBOX_SENT, response_code, outbox_id),
        )


def mark_outbox_failed(outbox_id: int, message: str,
                       response_code: Optional[int] = None,
                       retry: bool = True) -> None:
    """
    Отметить неудачу.

    Упавшая задача повторяется с ОТСРОЧКОЙ, а не каждым тиком: вечный
    кандидат в очереди — это тот самый механизм, которым выжгли месячную
    квоту ПланФакта. После OUTBOX_MAX_ATTEMPTS попыток задача перестаёт
    ходить наружу и ждёт человека — она видна в журнале.
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT attempts FROM crm_status_outbox WHERE id = ?", (outbox_id,)
        ).fetchone()
        attempts = (row["attempts"] if row else 0) + 1
        exhausted = attempts >= OUTBOX_MAX_ATTEMPTS or not retry
        conn.execute(
            "UPDATE crm_status_outbox SET state = ?, attempts = ?, "
            "       response_code = ?, error_message = ?, "
            "       next_attempt_at = CASE WHEN ? THEN NULL "
            f"            ELSE datetime('now', '+{OUTBOX_RETRY_SECONDS} seconds') END "
            " WHERE id = ?",
            (OUTBOX_FAILED if exhausted else OUTBOX_PENDING, attempts,
             response_code, message[:500], 1 if exhausted else 0, outbox_id),
        )


def retry_outbox(outbox_id: int) -> Dict[str, Any]:
    """
    Вернуть задачу в очередь после того, как человек починил причину.

    4xx намеренно не повторяется сам: заказ удалён, статус переименован, ключ
    отозван — повтор такой задачи это вечный кандидат в очереди, которым
    выжигают лимиты внешнего API. Значит отсрочку снимает тот, кто устранил
    причину, и делает это явным жестом — правило CLAUDE.md про квоты.

    Счётчик попыток обнуляется: предел в 8 попыток защищает от долбёжки по
    одной и той же причине, а причина теперь другая.

    Магазин перечитывается из витрины: ровно его отсутствие и было причиной
    отказа 11.09.2026, а к моменту повтора заказ уже мог досинхронизироваться.
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM crm_status_outbox WHERE id = ?", (outbox_id,)).fetchone()
        if row is None:
            raise ValueError("Отправка не найдена")
        if row["state"] == OUTBOX_SENT:
            raise ValueError("Эта отметка уже принята CRM — повторять нечего")

        site = conn.execute(
            "SELECT site_code FROM courier_orders WHERE retailcrm_order_id = ?",
            (row["retailcrm_order_id"],)).fetchone()
        site_code = (((site["site_code"] if site else None) or "").strip()
                     or (row["site_code"] or "").strip() or None)
        if not site_code:
            raise ValueError(
                "У заказа не определён магазин в CRM — отправлять нечем. "
                "Дождитесь синхронизации заказа.")

        conn.execute(
            "UPDATE crm_status_outbox "
            "   SET state = ?, attempts = 0, next_attempt_at = datetime('now'), "
            "       error_message = NULL, response_code = NULL, site_code = ? "
            " WHERE id = ?",
            (OUTBOX_PENDING, site_code, outbox_id),
        )
    return {"id": outbox_id, "state": OUTBOX_PENDING, "site_code": site_code}


def list_outbox(limit: int = 100, state: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Журнал отправок: что мы отправили и что CRM ответила.

    Вопрос «что мы им отправили» возникает всегда, и отвечать на него чтением
    кода — потерянный час (CLAUDE.md, история с НДС в банк).
    """
    sql = ("SELECT b.*, o.order_number FROM crm_status_outbox b "
           " LEFT JOIN courier_orders o ON o.retailcrm_order_id = b.retailcrm_order_id")
    params: List[Any] = []
    if state:
        sql += " WHERE b.state = ?"
        params.append(state)
    sql += " ORDER BY b.id DESC LIMIT ?"
    params.append(limit)

    with get_db() as conn:
        rows = [dict(row) for row in conn.execute(sql, params).fetchall()]
    for row in rows:
        row["action_title"] = OUTBOX_ACTION_TITLES.get(row["action"], row["action"])
    return rows


def release_orphan_claims(courier_delivery_codes: Optional[List[str]] = None
                          ) -> List[Dict[str, Any]]:
    """
    Снять брони с заказов, которых больше нет или которые уехали мимо нас.

    Долг Фазы 2 и находка К2: глубокий синк чистит окно витрины через DELETE
    по дате доставки. Заказ отменили — строка исчезла, а бронь осталась и
    висит у курьера в «моих» вечно.

    Три случая, и различать их надо: заказа нет в витрине или он ушёл из
    видимых статусов (`order_gone`), либо его передали службе доставки
    (`outsourced`). Причина видна курьеру и попадёт в пуш Фазы 6 —
    «заказ отозван» и «заказ передали Яндексу» это разные новости.
    """
    now = datetime.utcnow().isoformat(sep=" ", timespec="seconds")
    released: List[Dict[str, Any]] = []

    conn = sqlite_connect(DB_PATH, timeout=30)
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")

        # Аутсорс проверяем первым: заказ с чужим типом доставки тоже
        # «не в видимых статусах» не окажется, и без отдельной ветки причина
        # была бы всегда order_gone
        if courier_delivery_codes:
            placeholders = ",".join("?" * len(courier_delivery_codes))
            outsourced_where = (
                f" WHERE state = ? AND retailcrm_order_id IN ("
                f"     SELECT o.retailcrm_order_id FROM courier_orders o "
                f"      WHERE o.delivery_code IS NOT NULL "
                f"        AND o.delivery_code NOT IN ({placeholders}))")
            args = (STATE_CLAIMED, *courier_delivery_codes)
            # Кого снимаем — читаем до обновления: после него этих строк уже
            # не найти, а по ним уходят уведомления курьерам
            victims = [dict(row) for row in conn.execute(
                "SELECT id, retailcrm_order_id, courier_user_id "
                "  FROM delivery_assignments" + outsourced_where, args).fetchall()]
            conn.execute(
                "UPDATE delivery_assignments "
                "   SET state = ?, released_at = ?, release_reason = ?" + outsourced_where,
                (STATE_RELEASED, now, RELEASE_OUTSOURCED, *args),
            )
            for victim in victims:
                victim["release_reason"] = RELEASE_OUTSOURCED
            released.extend(victims)

        # Второй путь передачи аутсорсу: тип доставки оператор не трогал, а в
        # поле «курьер» поставил службу (решение владельца 2026-09-11 — снимать
        # бронь, а не только показывать). Заказ уже везёт Яндекс, и наш курьер
        # поехал бы за букетом, которого в салоне нет.
        #
        # Только из состояния `claimed`, как и остальные снятия: забранный
        # заказ физически у курьера, и отбирать его кнопкой нельзя.
        service_where = (
            " WHERE state = ? AND retailcrm_order_id IN ("
            "     SELECT o.retailcrm_order_id FROM courier_orders o "
            "       JOIN couriers c ON c.id = o.courier_id "
            "      WHERE COALESCE(c.is_service, 0) = 1)")
        victims = [dict(row) for row in conn.execute(
            "SELECT id, retailcrm_order_id, courier_user_id "
            "  FROM delivery_assignments" + service_where,
            (STATE_CLAIMED,)).fetchall()]
        conn.execute(
            "UPDATE delivery_assignments "
            "   SET state = ?, released_at = ?, release_reason = ?" + service_where,
            (STATE_RELEASED, now, RELEASE_OUTSOURCED, STATE_CLAIMED),
        )
        for victim in victims:
            victim["release_reason"] = RELEASE_OUTSOURCED
        released.extend(victims)

        # «Заказ пропал» — это исчез из витрины или отменён, а НЕ «статус ушёл
        # из видимых».
        #
        # Разница дорогая. Живой путь заказа сегодня — «Передан флористу →
        # Заказ готов → Вызван курьер → Выполнен», и `call-courier` проходят
        # 83% заказов. В справочнике видимых его нет, и по правилу «не виден —
        # значит пропал» бронь слетала бы у большинства заказов ровно в тот
        # момент, когда оператор двигает статус вперёд. Плюс Фаза 5 ставит
        # статусы сама («Передан курьеру»), и модуль отбирал бы заказ у
        # собственного курьера.
        #
        # Ошибиться здесь можно в две стороны, и они неравноценны: лишняя
        # живая бронь видна курьеру и снимается кнопкой, а лишнее снятие
        # отдаёт один букет двоим.
        gone_where = (
            " WHERE state = ? AND ("
            "     retailcrm_order_id NOT IN (SELECT retailcrm_order_id FROM courier_orders)"
            "     OR retailcrm_order_id IN ("
            "         SELECT o.retailcrm_order_id FROM courier_orders o "
            "           JOIN order_statuses s ON s.code = o.status "
            "          WHERE s.group_code = 'cancel'))")
        victims = [dict(row) for row in conn.execute(
            "SELECT id, retailcrm_order_id, courier_user_id "
            "  FROM delivery_assignments" + gone_where, (STATE_CLAIMED,)).fetchall()]
        conn.execute(
            "UPDATE delivery_assignments "
            "   SET state = ?, released_at = ?, release_reason = ?" + gone_where,
            (STATE_RELEASED, now, RELEASE_ORDER_GONE, STATE_CLAIMED),
        )
        for victim in victims:
            victim["release_reason"] = RELEASE_ORDER_GONE
        released.extend(victims)

        conn.execute("COMMIT")
        return released
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def _window_started(delivery_date: Optional[str], time_from: Optional[str],
                    utc_offset: Optional[int], now: datetime) -> bool:
    """
    Началось ли окно доставки. Пояс не задан или дата пуста — считаем, что нет.

    Отдельной функцией, потому что вопрос «пора ли уже» задают два экрана, а
    ошибка в нём выглядит не как исключение, а как ложная тревога у
    управляющего или её отсутствие там, где заказ реально стоит.
    """
    if not delivery_date or utc_offset is None:
        return False
    try:
        local = salon_time.parse_local(delivery_date, time_from)
        return salon_time.local_to_utc(local, utc_offset) <= now
    except (ValueError, salon_time.TimezoneUnknownError):
        return False


def list_active_assignments(city: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Живые брони с данными заказа — для экрана управляющего и разбора зависших.

    Отдаёт и зависшие (`is_overdue`): именно они и есть предмет разбора,
    когда у курьера сломалась машина, а заказ висит. С 21.09.2026 это не про
    срок брони (его нет), а про заказ: окно доставки началось, а он не забран.
    """
    # Именно datetime, а не строка: ниже идёт сравнение с моментом начала окна
    # доставки, который считается арифметикой над датами. Раньше здесь была
    # ISO-строка для сравнения с текстовым `expires_at` из базы.
    now = datetime.utcnow()
    sql = """
        SELECT a.*, o.order_number, o.city, o.delivery_date,
               o.delivery_time_from, o.status, s.name AS site_name,
               s.utc_offset
          FROM delivery_assignments a
          LEFT JOIN courier_orders o ON o.retailcrm_order_id = a.retailcrm_order_id
          LEFT JOIN courier_sites s ON s.code = o.site_code
         WHERE a.state IN (?, ?)
    """
    params: List[Any] = [STATE_CLAIMED, STATE_PICKED_UP]
    if city:
        sql += " AND o.city = ?"
        params.append(city)
    # По окну доставки, а не по сроку брони: срока у брони больше нет, и
    # разбирать список надо с того, что вот-вот повезут
    sql += " ORDER BY o.delivery_date, o.delivery_time_from IS NULL, o.delivery_time_from"

    with get_db() as conn:
        rows = [dict(row) for row in conn.execute(sql, params).fetchall()]

    for row in rows:
        # «Просрочена» больше не про срок брони (его нет), а про заказ: окно
        # доставки уже началось, а курьер его не забрал. Это и есть предмет
        # разбора — раньше о нём сообщал таймер, теперь сообщает сам список.
        row["is_overdue"] = bool(
            row["state"] == STATE_CLAIMED
            and _window_started(row.get("delivery_date"),
                                row.get("delivery_time_from"),
                                row.get("utc_offset"), now))
        # Заказа нет в витрине — его отменили или он уехал за окно синка
        row["order_missing"] = row.get("order_number") is None
    return rows


def courier_awaiting_close(courier_user_id: int, date_from: str,
                           date_to: str) -> int:
    """
    Сколько заказов курьер отвёз, а CRM их ещё не закрыла.

    Это ответ на вопрос «почему сумма меньше, чем я отвёз». Статус «Выполнен»
    ставит оператор, иногда через часы после доставки, и всё это время заказ не
    попадает ни в заработок (там только выполненные), ни в «живые брони» —
    состояние `delivered` терминальное. Без этого числа заказ выглядит для
    курьера пропавшим, и он идёт выяснять это к управляющему.

    Считается по дате доставки в том же периоде, что и заработок: заказ,
    зависший незакрытым с прошлой недели, — это деньги под угрозой, и увидеть
    его надо именно там, где курьер смотрит свои цифры.

    Только число. Сумму сюда не кладём намеренно: «вам должны ещё 600 ₽» о
    незакрытом заказе — обещание, которого модуль дать не может.

    **Отменённые заказы сюда не входят.** Экран обещает «попадут в сумму, когда
    оператор закроет заказ», а у отменённого этого не случится никогда: число
    висело бы вечно и обещало деньги, которых не будет. Отбор по ГРУППЕ статусов
    (`cancel`), а не по одному коду: кодов отмены в справочнике несколько
    («Отменён», «Отменён клиентом», «Не дозвонились»), и по одному коду
    остальные молча просочились бы.
    """
    from .storage import CANCEL_STATUS_GROUP, COMPLETED_STATUS

    with get_db() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS cnt
              FROM delivery_assignments a
              JOIN courier_orders o ON o.retailcrm_order_id = a.retailcrm_order_id
              LEFT JOIN order_statuses s ON s.code = o.status
             WHERE a.courier_user_id = ?
               AND a.state = ?
               AND o.status != ?
               AND COALESCE(s.group_code, '') != ?
               AND o.delivery_date >= ? AND o.delivery_date <= ?
            """,
            (courier_user_id, STATE_DELIVERED, COMPLETED_STATUS,
             CANCEL_STATUS_GROUP, date_from, date_to),
        ).fetchone()
    return (row["cnt"] if row else 0) or 0


def dispatch_overview(city: Optional[str], date_from: str, date_to: str,
                      courier_delivery_codes: Optional[List[str]] = None
                      ) -> Dict[str, Any]:
    """
    Где сейчас каждый заказ — экран управляющего.

    Один запрос на всю картину: доступность заказа считается из НАШЕЙ таблицы
    броней, а не из статуса CRM (находка К1) — между действием курьера и его
    отражением в CRM проходит до минуты, и экран не должен врать эту минуту.

    Заказы без брони, до окна доставки которых осталось меньше порога города,
    помечаются `unclaimed_alert`: это ровно тот случай, где решение «отдать
    аутсорсу» принимает человек, а следит агент (§7-бис).
    """
    codes = visible_status_codes()
    visible = codes.get(ROLE_VISIBLE, []) + codes.get(ROLE_READY, [])
    if not visible:
        return {"orders": [], "totals": {}, "unclaimed": []}

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

    with get_db() as conn:
        rows = [dict(row) for row in conn.execute(f"""
            SELECT o.retailcrm_order_id, o.order_number, o.city, o.status,
                   o.delivery_date, o.delivery_time_from, o.delivery_time_to,
                   o.net_cost, o.ready_seen_at, s.name AS site_name, s.utc_offset,
                   o.courier_name AS crm_courier_name,
                   COALESCE(c.is_service, 0) AS crm_courier_is_service,
                   a.state AS assignment_state, a.courier_name, a.courier_user_id,
                   a.claimed_at, a.picked_up_at, a.expires_at
              FROM courier_orders o
              LEFT JOIN courier_sites s ON s.code = o.site_code
              LEFT JOIN couriers c ON c.id = o.courier_id
              LEFT JOIN delivery_assignments a
                     ON a.retailcrm_order_id = o.retailcrm_order_id
                    AND a.state IN ('{STATE_CLAIMED}', '{STATE_PICKED_UP}', '{STATE_DELIVERED}')
             WHERE {' AND '.join(conditions)}
             ORDER BY o.delivery_date, o.delivery_time_from IS NULL, o.delivery_time_from
        """, params).fetchall()]

    ready = set(codes.get(ROLE_READY, []))
    totals = {"free": 0, "claimed": 0, "picked_up": 0, "delivered": 0,
              "unclaimed_alert": 0, "stuck_claim": 0}
    now = datetime.utcnow()
    settings_cache: Dict[Optional[str], Dict[str, Any]] = {}
    today_cache: Dict[int, str] = {}

    def still_ahead(row: Dict[str, Any]) -> bool:
        """
        По этому заказу ещё можно что-то сделать?

        Отделяет ФАКТ от ПРИЗЫВА К ДЕЙСТВИЮ, и это разные вещи.

        Факт («заказ так и не взяли», «взяли и не забрали») остаётся фактом и
        для вчерашнего заказа: он виден бейджем в строке и уезжает в выгрузку,
        ради которой период и сделали произвольным. Гасить его по дате значит
        молча отдавать пустой столбец на всей исторической части периода.

        А вот тревожный СПИСОК зовёт человека действовать, и для прошлого
        действия не существует. Пока период был жёстко «сегодня и завтра»,
        разницы не было; с произвольным периодом (23.09.2026) выбор «прошлый
        месяц» наполнил бы блок тысячами заказов, по которым решать нечего, —
        и его перестали бы читать вовсе.

        Сегодняшний день считаем по стенным часам САЛОНА, а не сервера:
        салоны в UTC+5 и UTC+7, и в полночь по UTC у них уже давно новый
        день. По UTC-дате заказы целого утра выпадали бы из тревог.
        """
        offset = row.get("utc_offset")
        if offset is None:
            return False
        if offset not in today_cache:
            today_cache[offset] = salon_time.utc_to_local(now, offset).date().isoformat()
        return (row.get("delivery_date") or "") >= today_cache[offset]

    for row in rows:
        state = row.get("assignment_state") or "free"
        row["state"] = state
        row["is_ready"] = is_ready_value(row.get("status"),
                                         row.get("ready_seen_at"), ready)
        totals[state] = totals.get(state, 0) + 1
        # Заказ уже отдали службе доставки — тип доставки при этом оператор
        # мог и не менять, признак здесь именно поле «курьер»
        row["outsourced"] = bool(row.get("crm_courier_is_service"))

        # Порог тревоги свой в каждом городе: города различаются размером и
        # числом курьеров, одно число на сеть будет либо шуметь, либо опаздывать.
        #
        # Отданный аутсорсу заказ не тревожит: он «свободен» только в наших
        # глазах, а решение по нему человек уже принял. Иначе список «никто не
        # взял» каждый вечер наполнялся бы заказами, которые давно везёт Яндекс,
        # и его перестали бы читать.
        row["unclaimed_alert"] = False
        if state == "free" and not row["outsourced"] and row.get("utc_offset") is not None:
            # Настройки города читаем один раз на город, а не на заказ.
            # city_settings() открывает СВОЁ соединение, и в цикле по ленте
            # это давало сотни обращений к общему медленному /data: замер
            # 16.09.2026 — 840 мс на 750 заказов против 40 мс с кэшем.
            city_key = row.get("city")
            if city_key not in settings_cache:
                settings_cache[city_key] = city_settings(city_key)
            minutes = settings_cache[city_key]["unclaimed_alert_minutes"]
            alert_at = salon_time.unclaimed_alert_at(
                row["delivery_date"], row.get("delivery_time_from"),
                row["utc_offset"], minutes)
            row["unclaimed_alert"] = alert_at <= now
            # Счётчик идёт в ногу со СПИСКОМ, а не с признаком: он подписывает
            # тревожный блок, а в блоке лежит только то, что ещё можно сделать
            if row["unclaimed_alert"] and still_ahead(row):
                totals["unclaimed_alert"] += 1

        # Забронирован, но не забран, а окно уже близко.
        #
        # Это замена автоснятию брони по таймеру (убрано 21.09.2026). Раньше
        # зависшую бронь снимал таймер — молча и заодно с теми, кто честно
        # ехал. Теперь её никто не снимает сам, и значит человек обязан о ней
        # УЗНАТЬ: иначе заказ числится взятым, не попадает в «никто не взял»
        # (он же не свободен) и тихо не едет до звонка клиента.
        #
        # Порог тот же, что у «никто не взял»: вопрос один и тот же — «до
        # доставки осталось столько-то, а заказ ещё в салоне», — и второй
        # настройки он не заслуживает.
        row["stuck_claim"] = False
        if state == "claimed" and row.get("utc_offset") is not None:
            city_key = row.get("city")
            if city_key not in settings_cache:
                settings_cache[city_key] = city_settings(city_key)
            minutes = settings_cache[city_key]["unclaimed_alert_minutes"]
            alert_at = salon_time.unclaimed_alert_at(
                row["delivery_date"], row.get("delivery_time_from"),
                row["utc_offset"], minutes)
            row["stuck_claim"] = alert_at <= now
            if row["stuck_claim"] and still_ahead(row):
                totals["stuck_claim"] += 1

    # Признак остаётся в строке и для прошлого — это факт, он виден бейджем и
    # уезжает в выгрузку. В тревожные СПИСКИ попадает только то, по чему ещё
    # можно действовать: список — это призыв, а не отчёт (см. still_ahead).
    return {
        "orders": rows,
        "totals": totals,
        "unclaimed": [row for row in rows
                      if row["unclaimed_alert"] and still_ahead(row)],
        "stuck": [row for row in rows
                  if row["stuck_claim"] and still_ahead(row)],
    }


def courier_mismatches(date_from: str, date_to: str,
                       city: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Заказы, где курьер в CRM разошёлся с нашей бронью.

    Курьера в CRM ставит не только модуль: оператор назначает его руками (68
    правок за день по разведке), и по этому же полю модуль «Оплата курьерам»
    считает выплаты. Значит расхождение — это чьи-то деньги, и увидеть его
    человек должен раньше, чем закроется месяц.

    Два случая, и они требуют разных действий, поэтому различаются явно:

    * `other_courier` — бронь живая, а в CRM стоит кто-то другой. Либо
      оператор переназначил заказ, либо ошибся строкой. Наш курьер об этом
      не знает и продолжает везти.
    * `stale_courier` — бронь снята (сама сгорела, отказался, снял
      управляющий), а курьер, которого поставили МЫ, в CRM остался. Заказ
      повезёт другой человек, а выплата уйдёт первому.

    Автоматически не чиним ни то, ни другое. Снять бронь из-за правки
    оператора — значит отдать букет второму курьеру из-за возможной опечатки;
    стереть курьера в CRM — значит затереть решение человека. Модуль
    показывает, человек решает (§7-бис плана).

    Окно дат обязательно: это экран, а не диагностика, и полный скан витрины
    на сетевом /data стоит секунды (CLAUDE.md).
    """
    live = f"('{STATE_CLAIMED}', '{STATE_PICKED_UP}')"
    params_a: List[Any] = [date_from, date_to]
    city_a = ""
    if city:
        city_a = " AND o.city = ?"
        params_a.append(city)

    with get_db() as conn:
        # 1. Живая бронь, а в CRM другой курьер.
        #    Курьеры без связки с CRM сюда не попадают: у них своё, более
        #    раннее предупреждение («не сопоставлен»), и дублировать его
        #    второй строкой про то же самое незачем.
        rows = [dict(row) for row in conn.execute(f"""
            SELECT o.retailcrm_order_id, o.order_number, o.city, o.delivery_date,
                   o.delivery_time_from, o.courier_id AS crm_courier_id,
                   o.courier_name AS crm_courier_name,
                   a.courier_name AS our_courier_name, a.state
              FROM delivery_assignments a
              JOIN courier_orders o ON o.retailcrm_order_id = a.retailcrm_order_id
              JOIN courier_profiles p ON p.user_id = a.courier_user_id
             WHERE a.state IN {live}
               AND o.delivery_date >= ? AND o.delivery_date <= ?
               AND o.courier_id IS NOT NULL
               AND p.retailcrm_courier_id IS NOT NULL
               AND o.courier_id != p.retailcrm_courier_id{city_a}
             ORDER BY o.delivery_date, o.delivery_time_from
        """, params_a).fetchall()]
        for row in rows:
            row["kind"] = "other_courier"

        # 2. Брони больше нет, а наш курьер в CRM остался.
        #    «Поставили мы» — это запись в журнале отправок с тем же курьером:
        #    без такой проверки сюда попал бы любой заказ, которому оператор
        #    сам назначил курьера, а бронь у нас просто сгорела.
        params_b: List[Any] = [date_from, date_to]
        city_b = ""
        if city:
            city_b = " AND o.city = ?"
            params_b.append(city)

        stale = [dict(row) for row in conn.execute(f"""
            SELECT o.retailcrm_order_id, o.order_number, o.city, o.delivery_date,
                   o.delivery_time_from, o.courier_id AS crm_courier_id,
                   o.courier_name AS crm_courier_name,
                   a.courier_name AS our_courier_name, a.state, a.release_reason
              FROM courier_orders o
              JOIN crm_status_outbox b
                    ON b.retailcrm_order_id = o.retailcrm_order_id
                   AND b.state = '{OUTBOX_SENT}'
                   AND b.courier_crm_id IS NOT NULL
                   AND b.courier_crm_id = o.courier_id
              LEFT JOIN delivery_assignments a ON a.id = (
                    SELECT MAX(x.id) FROM delivery_assignments x
                     WHERE x.retailcrm_order_id = o.retailcrm_order_id)
             WHERE o.delivery_date >= ? AND o.delivery_date <= ?
               AND o.courier_id IS NOT NULL
               AND NOT EXISTS (
                     SELECT 1 FROM delivery_assignments y
                      WHERE y.retailcrm_order_id = o.retailcrm_order_id
                        AND y.state IN ('{STATE_CLAIMED}', '{STATE_PICKED_UP}',
                                        '{STATE_DELIVERED}')){city_b}
             GROUP BY o.retailcrm_order_id
             ORDER BY o.delivery_date, o.delivery_time_from
        """, params_b).fetchall()]
        for row in stale:
            row["kind"] = "stale_courier"

    return rows + stale


def delivery_metrics(date_from: str, date_to: str,
                     city: Optional[str] = None) -> Dict[str, Any]:
    """
    Показатели работы курьеров за период.

    Непосчитанный показатель — это `None` с причиной, а не ноль: «нет данных»
    и «ноль минут» читаются человеком по-разному, и подменять одно другим
    нельзя.

    «Время от появления заказа до брони» здесь НЕ считается: момент, когда
    заказ стал виден курьерам, нигде не записан — есть только `synced_at`,
    а это время синка, а не появления. Показывать его под видом ожидания
    значит выдумать цифру.
    """
    where = ["o.delivery_date >= ?", "o.delivery_date <= ?"]
    params: List[Any] = [date_from, date_to]
    if city:
        where.append("o.city = ?")
        params.append(city)
    clause = " AND ".join(where)

    with get_db() as conn:
        claims = [dict(row) for row in conn.execute(f"""
            SELECT a.state, a.release_reason, a.claimed_at, a.picked_up_at,
                   a.delivered_at, o.delivery_time_to, o.delivery_date,
                   s.utc_offset
              FROM delivery_assignments a
              JOIN courier_orders o ON o.retailcrm_order_id = a.retailcrm_order_id
              LEFT JOIN courier_sites s ON s.code = o.site_code
             WHERE {clause}
        """, params).fetchall()]

        outsourced = conn.execute(f"""
            SELECT COUNT(*) AS cnt, COALESCE(SUM(o.net_cost), 0) AS amount
              FROM delivery_assignments a
              JOIN courier_orders o ON o.retailcrm_order_id = a.retailcrm_order_id
             WHERE a.release_reason = ? AND {clause}
        """, (RELEASE_OUTSOURCED, *params)).fetchone()

    def minutes_between(start: Optional[str], end: Optional[str]) -> Optional[float]:
        if not start or not end:
            return None
        try:
            a = datetime.fromisoformat(start)
            b = datetime.fromisoformat(end)
        except ValueError:
            return None
        return (b - a).total_seconds() / 60.0

    pickup_times = [m for m in (minutes_between(c["claimed_at"], c["picked_up_at"])
                                for c in claims) if m is not None and m >= 0]

    on_time, late = 0, 0
    # Формула «вовремя» одна на модуль — она же считает вкладку «Аналитика».
    # Две копии разъехались бы на первой правке, и тогда два экрана дашборда
    # отвечали бы на один вопрос разными числами.
    for claim in claims:
        minutes = salon_time.lateness_minutes(
            claim["delivered_at"],
            salon_time.deadline_utc(claim["delivery_date"],
                                    claim["delivery_time_to"],
                                    claim["utc_offset"]))
        if minutes is None:
            continue
        if minutes > 0:
            late += 1
        else:
            on_time += 1

    total = len(claims)
    # Броней, снятых ПО ТАЙМЕРУ, больше не бывает: сгорание убрано 21.09.2026.
    # Показатель заменён на «сняли руками» — это и есть живой сигнал о том, что
    # курьер взял и не поехал. Прежняя доля просроченных теперь всегда ноль, и
    # плитка с ней врала бы, что таких случаев нет.
    by_hand = sum(1 for c in claims
                  if c["release_reason"] in (RELEASE_SELF, RELEASE_ADMIN))

    def median(values: List[float]) -> Optional[float]:
        if not values:
            return None
        ordered = sorted(values)
        middle = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[middle]
        return (ordered[middle - 1] + ordered[middle]) / 2

    return {
        "period": {"from": date_from, "to": date_to},
        "claims_total": total,
        "released_by_hand_share": round(by_hand / total * 100, 1) if total else None,
        "released_by_hand_count": by_hand,
        # Медиана, а не среднее: одна ходка через весь город сдвигает среднее
        # так, что оно перестаёт описывать обычный день
        "minutes_to_pickup_median": round(median(pickup_times), 1) if pickup_times else None,
        "delivered_on_time": on_time,
        "delivered_late": late,
        "on_time_share": (round(on_time / (on_time + late) * 100, 1)
                          if (on_time + late) else None),
        "outsourced_after_release": outsourced["cnt"],
        "outsourced_amount": round(outsourced["amount"] or 0, 2),
        # Честно называем, чего не умеем: момент появления заказа в ленте
        # нигде не записан
        "not_measured": ["время от появления заказа до брони"],
    }


def my_active_claims(courier_user_id: int) -> List[Dict[str, Any]]:
    """Живые брони курьера — для лимита и экрана «Мои»."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM delivery_assignments "
            " WHERE courier_user_id = ? AND state IN (?, ?) ORDER BY claimed_at",
            (courier_user_id, STATE_CLAIMED, STATE_PICKED_UP),
        ).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Push: подписки и защита от дублей
# ---------------------------------------------------------------------------

EVENT_NEW_ORDER = "new_order"
EVENT_READY = "ready"
# Уведомления «бронь скоро снимется» больше нет: с 21.09.2026 бронь по времени
# не сгорает. Константа осталась — в журнале `push_events` лежат отправки за
# прошлые дни, и по ним ещё разбирают «почему курьер об этом узнал».
EVENT_CLAIM_EXPIRING = "claim_expiring"
EVENT_CLAIM_RELEASED = "claim_released"
EVENT_ORDER_GONE = "order_gone"

# Подписка, которая падает подряд столько раз, снимается сама. Push-сервис
# отвечает 410/404 на протухший endpoint, но бывает и молчание — очередь не
# должна копиться вечно.
PUSH_MAX_FAILURES = 5


def save_push_subscription(user_id: int, endpoint: str, p256dh: str, auth: str,
                           user_agent: Optional[str] = None) -> None:
    """
    Запомнить подписку устройства.

    Тот же endpoint у другого пользователя означает, что телефоном
    воспользовался другой человек: перезаписываем владельца, иначе пуши о
    заказах поедут не тому.
    """
    with get_db() as conn:
        conn.execute(
            "INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth, user_agent) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(endpoint) DO UPDATE SET user_id = excluded.user_id, "
            "  p256dh = excluded.p256dh, auth = excluded.auth, "
            "  user_agent = excluded.user_agent, failed_count = 0",
            (user_id, endpoint, p256dh, auth, user_agent),
        )


def delete_push_subscription(endpoint: str) -> None:
    with get_db() as conn:
        conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))


def push_subscriptions_for(user_ids: List[int]) -> List[Dict[str, Any]]:
    if not user_ids:
        return []
    placeholders = ",".join("?" * len(user_ids))
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM push_subscriptions WHERE user_id IN ({placeholders})",
            user_ids,
        ).fetchall()
    return [dict(row) for row in rows]


def mark_push_ok(endpoint: str) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE push_subscriptions SET last_ok_at = datetime('now'), failed_count = 0 "
            " WHERE endpoint = ?", (endpoint,))


def mark_push_failed(endpoint: str, drop: bool = False) -> None:
    """Протухший endpoint снимаем: иначе очередь копится и тратит время тика."""
    with get_db() as conn:
        if drop:
            conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
            return
        conn.execute(
            "UPDATE push_subscriptions SET failed_count = failed_count + 1 "
            " WHERE endpoint = ?", (endpoint,))
        conn.execute(
            "DELETE FROM push_subscriptions WHERE endpoint = ? AND failed_count >= ?",
            (endpoint, PUSH_MAX_FAILURES))


def claim_push_event(order_id: int, event_type: str) -> bool:
    """
    Занять право отправить событие. True — отправляем, False — уже отправлено.

    Держит уникальный ключ в БД, а не аккуратность кода: планировщик крутится
    в каждом из двух воркеров, и «новый заказ» иначе уходит дважды (К6).
    """
    with get_db() as conn:
        try:
            conn.execute(
                "INSERT INTO push_events (retailcrm_order_id, event_type) VALUES (?, ?)",
                (order_id, event_type),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def has_push_subscriptions(user_ids: List[int]) -> bool:
    """
    Есть ли хоть одно устройство у этих людей.

    Спрашивается ПЕРЕД тем, как занять право на событие. Иначе право сгорает
    вхолостую: курьер в городе заведён, устройство ещё не подписано, отправка
    уходит в пустоту — а журнал уже считает событие отправленным, и второго
    шанса у этого заказа не будет никогда.
    """
    if not user_ids:
        return False
    placeholders = ",".join("?" * len(user_ids))
    with get_db() as conn:
        row = conn.execute(
            f"SELECT 1 FROM push_subscriptions WHERE user_id IN ({placeholders}) LIMIT 1",
            list(user_ids)).fetchone()
    return row is not None


def reset_push_events(days: int = 2) -> int:
    """
    Забыть отправленные события за последние N дней — чтобы уведомления по
    этим заказам ушли заново.

    Нужно после починки: заказы, чьё право сгорело вхолостую, иначе молчат
    навсегда. Период ограничен намеренно — полная очистка на живых курьерах
    означала бы лавину повторных уведомлений по всей истории.
    """
    with get_db() as conn:
        cur = conn.execute(
            "DELETE FROM push_events WHERE created_at >= datetime('now', ?)",
            (f"-{int(days)} days",))
        return cur.rowcount


def sites_of_city(city: Optional[str]) -> List[Dict[str, Any]]:
    """
    Все салоны города — из справочника, а не из сегодняшней ленты.

    Фильтр салонов в приложении раньше строился по заказам: салон, из которого
    сегодня ничего не везут, в списке не появлялся. Выглядело это как «салон
    пропал», а на деле курьер просто не мог заранее отключить точку, куда не
    поедет, или включить ту, где заказы появятся через час.

    Список нужен постоянный: выбор — это намерение курьера на смену, и оно не
    должно зависеть от того, что в ленте прямо сейчас.
    """
    if not city:
        return []
    with get_db() as conn:
        rows = conn.execute(
            "SELECT code, name FROM courier_sites WHERE city = ? ORDER BY name",
            (city,)).fetchall()
    return [{"code": row["code"], "name": row["name"] or row["code"]} for row in rows]


def release_push_event(order_id: int, event_type: str) -> None:
    """
    Вернуть право на событие, если отправка не состоялась.

    Право занимается ДО отправки — иначе два воркера пошлют одно и то же
    дважды. Но если отправка провалилась, занятое право хоронит уведомление
    навсегда: следующий тик увидит «уже отправляли» и промолчит.

    Так 18.09.2026 девять заказов остались без уведомлений, пока разбирались
    с VAPID-ключом: каждая неудачная попытка не только не доходила, но и
    сжигала свой единственный шанс.
    """
    with get_db() as conn:
        conn.execute(
            "DELETE FROM push_events WHERE retailcrm_order_id = ? AND event_type = ?",
            (order_id, event_type))


def courier_user_ids(city: Optional[str]) -> List[int]:
    """Активные курьеры города — кому уходит «новый заказ»."""
    with get_db() as conn:
        if city:
            rows = conn.execute(
                "SELECT user_id FROM courier_profiles WHERE active = 1 AND city = ?",
                (city,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT user_id FROM courier_profiles WHERE active = 1").fetchall()
    return [row["user_id"] for row in rows]


# ---------------------------------------------------------------------------
# Фото товаров
# ---------------------------------------------------------------------------

# Через сколько дней перепроверять товар, у которого фото не было. Фото
# заводят задним числом, но редко: сутки — компромисс между «карточка пустая
# навсегда» и «ходим в CRM за одним и тем же каждый тик».
IMAGE_RECHECK_DAYS = 1

# Сколько офферов спрашиваем за один тик ленты. Это ровно одна страница CRM:
# лента обязана оставаться дешёвым тиком, а не превращаться во второй синк.
IMAGE_BATCH_SIZE = 100


def pending_image_offer_ids(limit: int = IMAGE_BATCH_SIZE) -> List[int]:
    """
    Офферы из заказов курьеров, для которых ссылку на фото ещё не спрашивали.

    Берём только позиции заказов, которые курьер реально может увидеть
    (доставка сегодня и позже): каталог целиком тянуть незачем — в нём тысячи
    позиций, а курьеру нужны десятки.

    Товар без фото сюда возвращается не раньше, чем через `IMAGE_RECHECK_DAYS`:
    иначе он становится вечным кандидатом и заставляет ходить наружу каждый
    тик — та самая ошибка, которой выжгли месячную квоту ПланФакта.
    """
    with get_db() as conn:
        rows = conn.execute("""
            SELECT DISTINCT i.offer_id
              FROM order_items i
              JOIN courier_orders o ON o.retailcrm_order_id = i.retailcrm_order_id
              LEFT JOIN product_images p ON p.offer_id = i.offer_id
             WHERE o.delivery_date >= date('now', '-1 day')
               AND (p.offer_id IS NULL
                    OR (p.image_url IS NULL
                        AND p.checked_at < datetime('now', ?)))
             LIMIT ?
        """, (f"-{IMAGE_RECHECK_DAYS} day", limit)).fetchall()
    return [row["offer_id"] for row in rows]


def save_product_images(images: Dict[int, Optional[str]]) -> int:
    """
    Запомнить ссылки на фото. Отсутствие фото сохраняется как NULL, а не
    пропускается: запись со `checked_at` — это и есть ответ «спрашивали, нет».
    """
    if not images:
        return 0
    with get_db() as conn:
        conn.executemany(
            "INSERT INTO product_images (offer_id, image_url, checked_at) "
            "VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(offer_id) DO UPDATE SET "
            "  image_url = excluded.image_url, checked_at = excluded.checked_at",
            [(int(offer_id), url or None) for offer_id, url in images.items()],
        )
    return len(images)


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

    # ready_seen_at стоит отдельно от FEED_ORDER_FIELDS, потому что у него
    # другое правило: остальные поля лента перезаписывает значением из CRM, а
    # отметку о сборке — только ставит. COALESCE на стороне базы, а не сравнение
    # в Python: тик ленты и глубокий синк ходят в витрину одновременно, и
    # «прочитал — решил — записал» здесь тот самый разрыв из CLAUDE.md.
    columns = ("retailcrm_order_id",) + FEED_ORDER_FIELDS + ("ready_seen_at",)
    placeholders = ", ".join("?" * len(columns))
    updates = ", ".join(f"{field} = excluded.{field}" for field in FEED_ORDER_FIELDS)
    updates += (", ready_seen_at = COALESCE(courier_orders.ready_seen_at, "
                "excluded.ready_seen_at)")

    ready_codes = ready_status_codes()
    ready_now = datetime.utcnow().isoformat(sep=" ", timespec="seconds")

    with get_db() as conn:
        # Что было ДО записи — чтобы поймать правку даты, времени или адреса.
        # Читаем тем же соединением и одним запросом: это тик ленты, а он
        # ходит на общий медленный диск каждую минуту.
        before = _watched_snapshot(conn, [row["retailcrm_order_id"] for row in rows])

        conn.executemany(
            f"""
            INSERT INTO courier_orders ({", ".join(columns)}, synced_at)
            VALUES ({placeholders}, datetime('now'))
            ON CONFLICT(retailcrm_order_id) DO UPDATE SET
                {updates},
                synced_at = datetime('now')
            """,
            [(row["retailcrm_order_id"],) + _order_values(row)
             + (ready_stamp(row.get("status"), None, ready_codes, ready_now),)
             for row in rows],
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

        _record_changes(conn, rows, before)
    return len(rows)


# Поля, правку которых курьер обязан заметить: по ним он планирует ходку.
# Состав и комментарии сюда не входят намеренно — их правят часто, и плашка
# «заказ изменён» на каждой мелочи перестанет читаться.
WATCHED_ORDER_FIELDS = {
    "delivery_date": "date",
    "delivery_time_from": "time",
    "delivery_time_to": "time",
    "address_text": "address",
}

CHANGE_TITLES = {"date": "дата", "time": "время", "address": "адрес"}


def _watched_snapshot(conn, order_ids: List[int]) -> Dict[int, Dict[str, Any]]:
    """Текущие значения отслеживаемых полей по списку заказов — одним запросом."""
    if not order_ids:
        return {}
    fields = ", ".join(WATCHED_ORDER_FIELDS)
    placeholders = ",".join("?" * len(order_ids))
    rows = conn.execute(
        f"SELECT retailcrm_order_id, {fields} FROM courier_orders "
        f" WHERE retailcrm_order_id IN ({placeholders})", order_ids).fetchall()
    return {row["retailcrm_order_id"]: dict(row) for row in rows}


def _record_changes(conn, rows: List[Dict[str, Any]],
                    before: Dict[int, Dict[str, Any]]) -> int:
    """
    Отметить заказы, у которых поменялись дата, время или адрес.

    Заказ, которого раньше не было в витрине, изменением не считается: он
    просто появился, и плашка «изменён» на новом заказе — ложная тревога.

    Отметка перезаписывается целиком: курьеру важно, что именно разошлось с
    тем, что он видел, а не вся история правок — её видно в самой CRM.
    """
    marked = 0
    for row in rows:
        order_id = row["retailcrm_order_id"]
        old = before.get(order_id)
        if not old:
            continue

        changed = set()
        for field, title in WATCHED_ORDER_FIELDS.items():
            if (old.get(field) or None) != (row.get(field) or None):
                changed.add(title)
        if not changed:
            continue

        # Порядок фиксированный, чтобы текст не прыгал между тиками
        fields = ",".join(t for t in ("date", "time", "address") if t in changed)
        conn.execute(
            "INSERT INTO order_changes (retailcrm_order_id, fields, changed_at) "
            "VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(retailcrm_order_id) DO UPDATE SET "
            "  fields = excluded.fields, changed_at = excluded.changed_at, "
            "  seen_at = NULL, seen_by = NULL",
            (order_id, fields),
        )
        marked += 1
    return marked


def mark_changes_seen(order_id: int, courier_user_id: Optional[int]) -> None:
    """
    Курьер открыл карточку — значит увидел, что изменилось.

    Гасим отметку только тому, кто заказ везёт: для остальных правка адреса
    остаётся новостью, а «просмотрено» от чужого человека ничего не значит.
    """
    if not courier_user_id:
        return
    with get_db() as conn:
        conn.execute(
            "UPDATE order_changes SET seen_at = datetime('now'), seen_by = ? "
            " WHERE retailcrm_order_id = ? AND seen_at IS NULL",
            (courier_user_id, order_id),
        )


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


def delete_courier_profile(user_id: int) -> None:
    """
    Убрать профиль курьера.

    **Профиль с живыми бронями не удаляется.** Заказы остались бы без
    владельца: записи брони живут своей таблицей и о профиле ничего не знают,
    а курьер без профиля не видит ленту — то есть заказ висел бы забронированным
    и недоступным никому, пока кто-нибудь не заметит. Сначала брони снимают.

    Учётную запись это не трогает: человек остаётся в системе, перестаёт быть
    курьером. История его доставок тоже остаётся — имя лежит рядом с бронью
    (`delivery_assignments.courier_name`), а не подтягивается отсюда.
    """
    with get_db() as conn:
        live = conn.execute(
            "SELECT COUNT(*) AS cnt FROM delivery_assignments "
            " WHERE courier_user_id = ? AND state IN (?, ?)",
            (user_id, STATE_CLAIMED, STATE_PICKED_UP),
        ).fetchone()["cnt"]
        if live:
            raise ValueError(
                f"У курьера {live} заказ(а) в работе. Снимите брони на экране "
                f"«Доставка сегодня», потом удаляйте профиль.")

        conn.execute("DELETE FROM courier_profiles WHERE user_id = ?", (user_id,))
        # Подписки на пуши тоже убираем: без профиля адресатом он всё равно
        # не станет, а мёртвые записи копят ошибки отправки
        conn.execute("DELETE FROM push_subscriptions WHERE user_id = ?", (user_id,))


def set_profile_active(user_id: int, active: bool, username: str) -> None:
    """
    Временно отключить курьера, не удаляя профиль.

    Отпуск и болезнь — не повод терять город и связку с CRM, которые заводили
    руками. Отключённый курьер не получает уведомлений и не считается
    адресатом, но настройки его ждут.
    """
    with get_db() as conn:
        conn.execute(
            "UPDATE courier_profiles SET active = ?, updated_by = ?, "
            "       updated_at = datetime('now') WHERE user_id = ?",
            (1 if active else 0, username, user_id),
        )


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
