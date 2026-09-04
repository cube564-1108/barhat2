"""
Хранилище модуля «Показатели салонов».

Живёт в общей barhat.db — здесь же лежат `stores` и `user_stores`, по которым
режется доступ, и join через две базы был бы невозможен.

Две таблицы:
  salon_links — соответствие «ключ источника → салон». Салон называется по-разному
                в RetailCRM, в двух формах Pyrus и на складе МойСклада, и все эти
                названия правятся людьми. Без справочника переименование салона
                молча обнуляет его показатель.
  salon_plans — план отгрузок по салону на месяц. Вводится человеком, взять неоткуда.
"""

import logging
import os
import sqlite3
from typing import Any, Dict, List, Optional

from sqlite_conn import connect as sqlite_connect

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("BARHAT_DB_PATH", "barhat.db")

# Источники, из которых приходят ключи салона
SOURCE_CRM = "crm_site"          # код сайта RetailCRM (nsk-voskhod-3)
SOURCE_NOS = "pyrus_nos"         # значение поля «Салон» в форме негативной ОС
SOURCE_QUALITY = "pyrus_quality"  # значение поля «Салон» в форме качества
SOURCE_MS_STORE = "ms_store"     # id склада МойСклада (UUID)

SOURCES = {
    SOURCE_CRM: "RetailCRM, сайт заказа",
    SOURCE_NOS: "Pyrus, негативная ОС",
    SOURCE_QUALITY: "Pyrus, качество сборки",
    SOURCE_MS_STORE: "МойСклад, склад",
}

# Стартовое соответствие на 9 действующих салонов. Значения сверены с боевыми
# данными 2026-09-04: коды сайтов — из справочника RetailCRM, названия Pyrus —
# из витрины quality_scores и реестра формы 1291124 (включая опечатки
# «Совесткая» и «Свердловскй» — они существуют именно в таком виде).
SEED_LINKS = {
    1: {SOURCE_CRM: "nsk-voskhod-3", SOURCE_NOS: "НСК В3", SOURCE_QUALITY: "НСК Восход"},
    2: {SOURCE_CRM: "barkhat-nsk-levyi", SOURCE_NOS: "НСК Б61", SOURCE_QUALITY: "НСК Левый"},
    3: {SOURCE_CRM: "barkhat-tomsk", SOURCE_NOS: "Томск Д-К16А", SOURCE_QUALITY: "Томск ДК"},
    4: {SOURCE_CRM: "barkhat-ekb", SOURCE_NOS: "ЕКБ Б89", SOURCE_QUALITY: "ЕКБ Бажова"},
    5: {SOURCE_CRM: "barkhat-barnaul", SOURCE_NOS: "БРН С7", SOURCE_QUALITY: "БРН Совесткая"},
    6: {SOURCE_CRM: "barkhat-barnaul2", SOURCE_NOS: "БРН Л1", SOURCE_QUALITY: "БРН Лазурная"},
    7: {SOURCE_CRM: "cheliabinsk-tsvillinga-59", SOURCE_NOS: "ЧЛБ Цвиллинга",
        SOURCE_QUALITY: "Челябинск Цвиллинга"},
    8: {SOURCE_CRM: "cheliabinsk-sverdl-pr-23", SOURCE_NOS: "ЧЛБ Свердловский",
        SOURCE_QUALITY: "Челябинск Свердловскй"},
    9: {SOURCE_CRM: "nsk-zheleznodorozhnaia-15-1", SOURCE_NOS: "НСК Ж15/1", SOURCE_QUALITY: "НСК ЖД"},
}

# Города салонов — по ним строится разрез «по городам». Берём из названия салона
# по первому слову, как в модуле курьеров (couriers/retailcrm.py::CITY_ALIASES).
CITY_ALIASES = {
    "нск": "Новосибирск",
    "новосибирск": "Новосибирск",
    "академ": "Новосибирск",
    "екб": "Екатеринбург",
    "екатеринбург": "Екатеринбург",
    "барнаул": "Барнаул",
    "брн": "Барнаул",
    "томск": "Томск",
    "челябинск": "Челябинск",
    "члб": "Челябинск",
}


def get_db() -> sqlite3.Connection:
    """Соединение с общей базой. Настройки — в sqlite_conn (WAL один раз на файл)."""
    return sqlite_connect(DB_PATH, timeout=20)


def city_of(store_name: str) -> Optional[str]:
    """«НСК Восход, 3» → «Новосибирск»."""
    if not store_name:
        return None
    first = store_name.strip().split()[0].lower().strip(",.")
    return CITY_ALIASES.get(first)


# ============================================================================
# Схема
# ============================================================================

def init_salonkpi_tables() -> None:
    """Создать таблицы модуля (идемпотентно, зовётся при старте каждого воркера)."""
    conn = get_db()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS salon_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                external_key TEXT NOT NULL,
                store_id INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        # Инвариант «один ключ источника — один салон» держит индекс, а не код:
        # у записи будет несколько путей (сид, ручка, будущий импорт)
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_salon_links_source_key "
                "ON salon_links(source, external_key)"
            )
        except sqlite3.IntegrityError as e:
            # Индекс не построится поверх уже накопленных дублей. Ронять старт
            # воркера из-за этого нельзя — дубли разбираются админской ручкой
            logger.error(f"Не удалось создать уникальный индекс salon_links: {e}")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS salon_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                store_id INTEGER NOT NULL,
                month TEXT NOT NULL,
                amount REAL NOT NULL,
                updated_by TEXT,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_salon_plans_store_month "
                "ON salon_plans(store_id, month)"
            )
        except sqlite3.IntegrityError as e:
            logger.error(f"Не удалось создать уникальный индекс salon_plans: {e}")

        conn.commit()
    finally:
        conn.close()

    seed_links()


def seed_links() -> int:
    """
    Проставить стартовые связи. Идемпотентно: существующие не трогаем — их мог
    поправить человек, и перезаписывать его правку сидом нельзя.
    """
    added = 0
    conn = get_db()
    try:
        existing_stores = {row["id"] for row in conn.execute("SELECT id FROM stores")}

        for store_id, links in SEED_LINKS.items():
            if store_id not in existing_stores:
                continue
            for source, key in links.items():
                cur = conn.execute(
                    "INSERT OR IGNORE INTO salon_links (source, external_key, store_id) "
                    "VALUES (?, ?, ?)",
                    (source, key, store_id),
                )
                added += cur.rowcount or 0

        # Склады МойСклада уже сопоставлены модулем списаний — переиспользуем,
        # а не заставляем человека делать ту же работу второй раз
        try:
            ms_rows = conn.execute(
                "SELECT store_id, moysklad_store_id FROM moysklad_store_links"
            ).fetchall()
        except sqlite3.OperationalError:
            ms_rows = []
        for row in ms_rows:
            cur = conn.execute(
                "INSERT OR IGNORE INTO salon_links (source, external_key, store_id) VALUES (?, ?, ?)",
                (SOURCE_MS_STORE, row["moysklad_store_id"], row["store_id"]),
            )
            added += cur.rowcount or 0

        conn.commit()
        if added:
            logger.info(f"Справочник салонов: добавлено {added} связей")
        return added
    finally:
        conn.close()


# ============================================================================
# Салоны
# ============================================================================

def list_stores(
    store_ids: Optional[List[int]] = None,
    only_linked: bool = False,
) -> List[Dict[str, Any]]:
    """
    Активные записи справочника точек (при store_ids — только они), с городом.

    only_linked=True оставляет те, у которых есть хотя бы одно соответствие с
    источником данных. Таблица `stores` общая на весь дашборд, и кроме салонов
    в ней живут статьи расходов и подразделения — «ГО», «Налоги», «Маркетинг»,
    «Кофферс»: они нужны счетам, но салонами не являются и в показателях
    выглядят как девять пустых строк.

    Отбор идёт по данным (есть ли связь с CRM, Pyrus или складом), а не по
    списку названий в коде: названия правят руками, и разбор строки сломается
    на первой же новой записи. Новый салон появляется в отчёте сам, как только
    его привязали в «Сопоставлении».
    """
    query = "SELECT s.id, s.name FROM stores s WHERE s.is_active = 1"
    params: list = []
    if store_ids is not None:
        if not store_ids:
            return []
        query += f" AND s.id IN ({','.join('?' * len(store_ids))})"
        params.extend(store_ids)
    if only_linked:
        query += " AND EXISTS (SELECT 1 FROM salon_links l WHERE l.store_id = s.id)"
    query += " ORDER BY s.name"

    conn = get_db()
    try:
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()
    return [{"id": r["id"], "name": r["name"], "city": city_of(r["name"])} for r in rows]


# ============================================================================
# Связи
# ============================================================================

def resolve_map(source: str) -> Dict[str, int]:
    """{ключ источника: store_id} — по нему синк раскладывает данные по салонам."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT external_key, store_id FROM salon_links WHERE source = ?", (source,)
        ).fetchall()
    finally:
        conn.close()
    return {r["external_key"]: r["store_id"] for r in rows}


def list_links() -> List[Dict[str, Any]]:
    """Все связи со названиями салонов — для экрана справочника."""
    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT l.id, l.source, l.external_key, l.store_id, s.name AS store_name
            FROM salon_links l
            LEFT JOIN stores s ON s.id = l.store_id
            ORDER BY l.source, s.name
        """).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


class LinkExistsError(Exception):
    """Ключ уже привязан к другому салону."""


def set_link(source: str, external_key: str, store_id: int) -> Dict[str, Any]:
    """
    Привязать ключ источника к салону.

    Проверка «а не занят ли ключ» и запись идут одним соединением под
    BEGIN IMMEDIATE: на проде до 16 параллельных обработчиков, и между SELECT в
    одном соединении и INSERT в другом лежит окно в сотни миллисекунд.
    """
    external_key = (external_key or "").strip()
    if not external_key:
        raise ValueError("Пустой ключ источника")
    if source not in SOURCES:
        raise ValueError(f"Неизвестный источник: {source}")

    conn = get_db()
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        store = conn.execute(
            "SELECT id, name FROM stores WHERE id = ? AND is_active = 1", (store_id,)
        ).fetchone()
        if not store:
            conn.execute("ROLLBACK")
            raise ValueError(f"Салон {store_id} не найден")

        existing = conn.execute(
            "SELECT id, store_id FROM salon_links WHERE source = ? AND external_key = ?",
            (source, external_key),
        ).fetchone()

        if existing:
            conn.execute(
                "UPDATE salon_links SET store_id = ?, updated_at = datetime('now') WHERE id = ?",
                (store_id, existing["id"]),
            )
            link_id = existing["id"]
        else:
            cur = conn.execute(
                "INSERT INTO salon_links (source, external_key, store_id) VALUES (?, ?, ?)",
                (source, external_key, store_id),
            )
            link_id = cur.lastrowid
        conn.execute("COMMIT")
        return {"id": link_id, "source": source, "external_key": external_key,
                "store_id": store_id, "store_name": store["name"]}
    except sqlite3.IntegrityError as e:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise LinkExistsError(str(e))
    finally:
        conn.close()


def delete_link(link_id: int) -> bool:
    conn = get_db()
    try:
        cur = conn.execute("DELETE FROM salon_links WHERE id = ?", (link_id,))
        conn.commit()
        return bool(cur.rowcount)
    finally:
        conn.close()


# ============================================================================
# Планы
# ============================================================================

def get_plans(month: str) -> Dict[int, float]:
    """{store_id: сумма плана} за месяц."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT store_id, amount FROM salon_plans WHERE month = ?", (month,)
        ).fetchall()
    finally:
        conn.close()
    return {r["store_id"]: r["amount"] for r in rows}


def set_plan(store_id: int, month: str, amount: Optional[float], username: str) -> None:
    """
    Задать план. amount=None удаляет план — «плана нет» это не то же самое, что
    «план нулевой»: в первом случае процент выполнения не считается вовсе.
    """
    conn = get_db()
    try:
        if amount is None:
            conn.execute(
                "DELETE FROM salon_plans WHERE store_id = ? AND month = ?", (store_id, month)
            )
        else:
            conn.execute("""
                INSERT INTO salon_plans (store_id, month, amount, updated_by, updated_at)
                VALUES (?, ?, ?, ?, datetime('now'))
                ON CONFLICT(store_id, month) DO UPDATE SET
                    amount = excluded.amount,
                    updated_by = excluded.updated_by,
                    updated_at = datetime('now')
            """, (store_id, month, float(amount), username))
        conn.commit()
    finally:
        conn.close()
