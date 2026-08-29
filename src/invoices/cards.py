"""
Справочник рабочих карт (план plans/2026-08-29-рабочие-карты.md, Фаза 0).

Рабочая карта — подотчётный счёт в ПланФакте: пополнение приходит на него
перемещением с расчётного счёта, траты списываются с него расходом. Карта
привязана к ГОРОДУ, а не к сотруднику: управляющие меняются, карта остаётся,
и одна карта обслуживает все салоны своего города (work_card_stores).

Живёт отдельным файлом, а не в storage.py: тот уже за 3000 строк, а параллельная
работа над модулем счетов и картами иначе постоянно упирается в один файл.
Соединение и хелперы берём оттуда — база одна и та же.
"""

import logging
import sqlite3
from typing import Any, Dict, List, Optional

from .storage import get_db, _table_exists, get_user_stores

logger = logging.getLogger(__name__)


# Пять реальных карт (данные владельца 2026-08-29). accountId подтверждены
# живым чтением GET /accounts в тот же день — по названию счета в рантайме НЕ
# ищем: в справочнике ПланФакта 75 счетов, и у четырёх карт есть неактивные
# тёзки-предшественники, причём старая «Рабочая карта ЧЛБ» принадлежит другому
# ИП, чем действующая «Рабочая карта ЧЛБ ГПБ». Поиск по подстроке отправил бы
# операции не на тот счёт.
#
# store_prefixes — только для ПЕРВОГО заполнения привязки салонов (тот же приём,
# что и CITY_ALIASES в src/couriers/retailcrm.py). Дальше привязка правится
# руками в справочнике и переcчёту не подлежит.
SEED_CARDS = [
    {
        "title": "Рабочая карта Томск (ГПБ)",
        "planfact_account_id": "474531",
        "planfact_account_title": "Рабочая карта  Томск (ГПБ)",
        "source_planfact_account_id": "749767",
        "source_planfact_account_title": "06 Альфа Бизнес Кваша",
        "store_prefixes": ("Томск",),
    },
    {
        "title": "Рабочая карта НСК",
        "planfact_account_id": "764679",
        "planfact_account_title": "Рабочая карта НСК",
        "source_planfact_account_id": "749767",
        "source_planfact_account_title": "06 Альфа Бизнес Кваша",
        "store_prefixes": ("НСК",),
    },
    {
        "title": "Рабочая карта ЕКБ (РСБ)",
        "planfact_account_id": "233428",
        "planfact_account_title": "Рабочая карта ЕКБ (РСБ)",
        "source_planfact_account_id": "316172",
        "source_planfact_account_title": "16 РСБ Насуленко",
        "store_prefixes": ("ЕКБ",),
    },
    {
        "title": "Рабочая карта Барнаул (РСБ)",
        "planfact_account_id": "568178",
        "planfact_account_title": "Рабочая карта Барнаул (РСБ)",
        "source_planfact_account_id": "316172",
        "source_planfact_account_title": "16 РСБ Насуленко",
        "store_prefixes": ("Барнаул",),
    },
    {
        "title": "Рабочая карта ЧЛБ ГПБ",
        "planfact_account_id": "557074",
        "planfact_account_title": "Рабочая карта ЧЛБ ГПБ ",
        "source_planfact_account_id": "316172",
        "source_planfact_account_title": "16 РСБ Насуленко",
        "store_prefixes": ("Челябинск", "ЧЛБ"),
    },
]


def init_cards_tables():
    """Создать таблицы рабочих карт и заполнить их, если пусто."""
    conn = get_db()
    try:
        # UNIQUE(title) сознательно НЕТ. В паре с мягким удалением
        # (is_active = 0) он уже дважды ломал справочники этого проекта:
        # деактивированная запись занимает имя, и завести карту с тем же
        # названием снова нельзя, а ошибка выглядит необъяснимой.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS work_cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                planfact_account_id TEXT NOT NULL,
                planfact_account_title TEXT,
                source_planfact_account_id TEXT NOT NULL,
                source_planfact_account_title TEXT,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_work_cards_active
            ON work_cards(is_active) WHERE is_active = 1
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS work_card_stores (
                card_id INTEGER NOT NULL REFERENCES work_cards(id),
                store_id INTEGER NOT NULL REFERENCES stores(id),
                PRIMARY KEY (card_id, store_id)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_work_card_stores_store
            ON work_card_stores(store_id)
        """)
        conn.commit()

        _seed_cards_if_empty(conn)
    finally:
        conn.close()


def _seed_cards_if_empty(conn: sqlite3.Connection):
    """
    Первое заполнение справочника пятью реальными картами.

    Идёт только на пустой таблице: сид не должен затирать правки, сделанные
    руками в интерфейсе. Блокировка — как в остальных миграциях модуля: на
    проде два gunicorn-воркера стартуют параллельно, и без BEGIN IMMEDIATE
    оба вставили бы свой комплект карт.
    """
    if conn.execute("SELECT COUNT(*) AS count FROM work_cards").fetchone()["count"]:
        return

    conn.execute("BEGIN IMMEDIATE")
    try:
        # Перепроверка уже под блокировкой — второй воркер мог успеть первым
        if conn.execute("SELECT COUNT(*) AS count FROM work_cards").fetchone()["count"]:
            conn.rollback()
            return

        stores = []
        if _table_exists(conn, "stores"):
            stores = conn.execute(
                "SELECT id, name FROM stores WHERE is_active = 1"
            ).fetchall()

        for card in SEED_CARDS:
            cursor = conn.execute(
                """
                INSERT INTO work_cards (
                    title, planfact_account_id, planfact_account_title,
                    source_planfact_account_id, source_planfact_account_title
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    card["title"], card["planfact_account_id"], card["planfact_account_title"],
                    card["source_planfact_account_id"], card["source_planfact_account_title"],
                ),
            )
            card_id = cursor.lastrowid

            prefixes = tuple(p.lower() for p in card["store_prefixes"])
            for store in stores:
                if (store["name"] or "").strip().lower().startswith(prefixes):
                    conn.execute(
                        "INSERT OR IGNORE INTO work_card_stores (card_id, store_id) VALUES (?, ?)",
                        (card_id, store["id"]),
                    )

        conn.commit()
        logger.info("[Cards] Справочник рабочих карт заполнен: %d карт", len(SEED_CARDS))
    except Exception:
        conn.rollback()
        raise


def is_valid_account_id(value: Any) -> bool:
    """
    accountId ПланФакта — целое число. Проверяем и здесь, а не только на вводе:
    нечисловой id уже ронял прогон разноски внутри цикла по операциям, обрывая
    обработку всех остальных записей (план 2026-08-26, Фаза 4).
    """
    return bool(str(value or "").strip().isdigit())


def _row_to_card(row: sqlite3.Row, store_ids: List[int]) -> Dict[str, Any]:
    card = dict(row)
    card["store_ids"] = store_ids
    return card


def list_cards(active_only: bool = True) -> List[Dict[str, Any]]:
    """Все карты со списком салонов. Один запрос на карты, один на привязки."""
    conn = get_db()
    try:
        where = "WHERE is_active = 1" if active_only else ""
        rows = conn.execute(
            f"SELECT * FROM work_cards {where} ORDER BY title"
        ).fetchall()

        links: Dict[int, List[int]] = {}
        for link in conn.execute("SELECT card_id, store_id FROM work_card_stores").fetchall():
            links.setdefault(link["card_id"], []).append(link["store_id"])

        return [_row_to_card(row, links.get(row["id"], [])) for row in rows]
    finally:
        conn.close()


def get_card_by_id(card_id: int) -> Optional[Dict[str, Any]]:
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM work_cards WHERE id = ?", (card_id,)).fetchone()
        if not row:
            return None
        store_ids = [
            link["store_id"] for link in conn.execute(
                "SELECT store_id FROM work_card_stores WHERE card_id = ?", (card_id,)
            ).fetchall()
        ]
        return _row_to_card(row, store_ids)
    finally:
        conn.close()


def create_card(
    title: str,
    planfact_account_id: str,
    source_planfact_account_id: str,
    store_ids: Optional[List[int]] = None,
    planfact_account_title: Optional[str] = None,
    source_planfact_account_title: Optional[str] = None,
) -> int:
    conn = get_db()
    try:
        cursor = conn.execute(
            """
            INSERT INTO work_cards (
                title, planfact_account_id, planfact_account_title,
                source_planfact_account_id, source_planfact_account_title
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (title, str(planfact_account_id).strip(), planfact_account_title,
             str(source_planfact_account_id).strip(), source_planfact_account_title),
        )
        card_id = cursor.lastrowid
        _replace_card_stores(conn, card_id, store_ids or [])
        conn.commit()
        return card_id
    finally:
        conn.close()


def update_card(card_id: int, values: Dict[str, Any], store_ids: Optional[List[int]] = None) -> bool:
    """Обновить поля карты и (если передан) список салонов."""
    allowed = (
        "title", "planfact_account_id", "planfact_account_title",
        "source_planfact_account_id", "source_planfact_account_title", "is_active",
    )
    changes = {key: values[key] for key in allowed if key in values}

    conn = get_db()
    try:
        if changes:
            assignments = ", ".join(f"{key} = ?" for key in changes)
            conn.execute(
                f"UPDATE work_cards SET {assignments} WHERE id = ?",
                (*changes.values(), card_id),
            )
        if store_ids is not None:
            _replace_card_stores(conn, card_id, store_ids)
        conn.commit()
        return True
    finally:
        conn.close()


def deactivate_card(card_id: int) -> bool:
    """
    Мягкое удаление: заявки прошлых месяцев ссылаются на карту, и физическое
    удаление оставило бы их без названия карты в истории.
    """
    conn = get_db()
    try:
        conn.execute("UPDATE work_cards SET is_active = 0 WHERE id = ?", (card_id,))
        conn.commit()
        return True
    finally:
        conn.close()


def _replace_card_stores(conn: sqlite3.Connection, card_id: int, store_ids: List[int]):
    conn.execute("DELETE FROM work_card_stores WHERE card_id = ?", (card_id,))
    for store_id in dict.fromkeys(store_ids):  # без дублей, порядок сохраняем
        conn.execute(
            "INSERT OR IGNORE INTO work_card_stores (card_id, store_id) VALUES (?, ?)",
            (card_id, int(store_id)),
        )


def get_cards_for_user(username: str, role: str) -> List[Dict[str, Any]]:
    """
    Карты, доступные пользователю: админу — все, остальным — те, чьи салоны
    пересекаются с его салонами (user_stores, тот же справочник, что в
    cashshifts). Держателя-сотрудника у карты нет: она привязана к городу.
    """
    cards = list_cards(active_only=True)
    if role == "admin":
        return cards

    allowed = set(get_user_stores(username))
    if not allowed:
        return []
    return [card for card in cards if allowed.intersection(card["store_ids"])]


def user_can_use_card(card_id: int, username: str, role: str) -> bool:
    """
    Проверка на сервере, а не только фильтр в интерфейсе: иначе управляющий,
    подменив card_id в запросе, спишет расход с чужой карты.
    """
    if role == "admin":
        return True
    return any(card["id"] == card_id for card in get_cards_for_user(username, role))
