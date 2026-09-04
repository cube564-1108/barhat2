"""
Движение товара по складам салонов: оприходование и списание.

Нужно двум показателям раздела «Показатели салонов»:
  доля списания цветка   = сумма списанного цветка / сумма оприходованного;
  средняя цена клубники  = сумма оприходованной / (вес прихода − вес списания).

Хранится построчно (позиция документа), а не агрегатами по месяцам: документов
за месяц ~450, зато любой период считается без пересинхронизации, и цифру всегда
можно разложить до конкретного документа.

Группы товаров («цветок», «клубника») задаются папками каталога МойСклада и
хранятся в таблице, а не в коде: состав папок меняют без нас, а от него зависит
показатель. Там же коэффициент перевода в килограммы — у товара «Клубника»
единица измерения в МойСкладе не задана вообще, фактически это граммы, и зашитая
в код тысяча превратила бы 1 000 ₽/кг в миллион в день, когда её заведут в кг.
"""

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DOC_ENTER = "enter"   # Оприходование
DOC_LOSS = "loss"     # Списание

KIND_FLOWER = "flower"
KIND_BERRY = "berry"

# Стартовое соответствие папок группам (проверено на боевом каталоге 2026-09-04)
SEED_GROUPS = [
    {"path": "Товары МС/Цветы", "kind": KIND_FLOWER, "qty_per_kg": None},
    {"path": "Товары МС/Клубника", "kind": KIND_BERRY, "qty_per_kg": 1000},
]

# Окно регулярной подкачки: документы правят задним числом
SYNC_WINDOW_DAYS = 60

# Страница выгрузки. При limit > 100 МойСклад не разворачивает positions.rows —
# та же причина, что в синке заказов
PAGE_LIMIT = 50


# ============================================================================
# Схема
# ============================================================================

def init_warehouse_tables(storage) -> None:
    """Создать таблицы движения товара (идемпотентно)."""
    with storage._get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS warehouse_flows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                doc_id TEXT NOT NULL,
                doc_type TEXT NOT NULL,
                doc_name TEXT,
                moment TEXT NOT NULL,
                store_id TEXT,
                store_name TEXT,
                product_id TEXT,
                product_name TEXT,
                folder_id TEXT,
                quantity REAL NOT NULL DEFAULT 0,
                price REAL NOT NULL DEFAULT 0,
                total REAL NOT NULL DEFAULT 0,
                position_id TEXT,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_wf_moment ON warehouse_flows(moment)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_wf_store ON warehouse_flows(store_id, moment)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_wf_folder ON warehouse_flows(folder_id)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wf_doc ON warehouse_flows(doc_id)")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS warehouse_groups (
                folder_id TEXT PRIMARY KEY,
                folder_path TEXT,
                kind TEXT NOT NULL,
                qty_per_kg REAL,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.commit()


def folder_paths(storage) -> Dict[str, str]:
    """{folder_id: полный путь} — «Товары МС/Цветы» и так далее."""
    with storage._get_connection() as conn:
        rows = conn.execute("SELECT id, name, parent_id FROM folders").fetchall()

    by_id = {row["id"]: dict(row) for row in rows}

    def path_of(folder_id: str) -> str:
        parts = []
        current = by_id.get(folder_id)
        seen = set()
        while current and current["id"] not in seen:
            seen.add(current["id"])
            parts.append(current["name"])
            current = by_id.get(current["parent_id"])
        return "/".join(reversed(parts))

    return {fid: path_of(fid) for fid in by_id}


def seed_groups(storage) -> int:
    """
    Проставить группы по путям папок. Идемпотентно: существующие записи не
    трогаем — их мог поправить человек.
    """
    paths = folder_paths(storage)
    if not paths:
        logger.info("Папки каталога ещё не загружены — группы товаров не заданы")
        return 0

    added = 0
    with storage._get_connection() as conn:
        for seed in SEED_GROUPS:
            target = [fid for fid, path in paths.items()
                      if path == seed["path"] or path.startswith(seed["path"] + "/")]
            if not target:
                logger.warning(f"Папка «{seed['path']}» не найдена в каталоге МойСклада")
            for folder_id in target:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO warehouse_groups (folder_id, folder_path, kind, qty_per_kg) "
                    "VALUES (?, ?, ?, ?)",
                    (folder_id, paths[folder_id], seed["kind"], seed["qty_per_kg"]),
                )
                added += cur.rowcount or 0
        conn.commit()

    if added:
        logger.info(f"Группы товаров склада: добавлено {added} папок")
    return added


def list_groups(storage) -> List[Dict[str, Any]]:
    with storage._get_connection() as conn:
        rows = conn.execute(
            "SELECT folder_id, folder_path, kind, qty_per_kg FROM warehouse_groups ORDER BY kind, folder_path"
        ).fetchall()
    return [dict(row) for row in rows]


def set_group(storage, folder_id: str, kind: Optional[str], qty_per_kg: Optional[float],
              folder_path: Optional[str] = None) -> None:
    """kind=None убирает папку из расчёта."""
    with storage._get_connection() as conn:
        if kind is None:
            conn.execute("DELETE FROM warehouse_groups WHERE folder_id = ?", (folder_id,))
        else:
            conn.execute("""
                INSERT INTO warehouse_groups (folder_id, folder_path, kind, qty_per_kg, updated_at)
                VALUES (?, ?, ?, ?, datetime('now'))
                ON CONFLICT(folder_id) DO UPDATE SET
                    kind = excluded.kind,
                    qty_per_kg = excluded.qty_per_kg,
                    folder_path = COALESCE(excluded.folder_path, warehouse_groups.folder_path),
                    updated_at = datetime('now')
            """, (folder_id, folder_path, kind, qty_per_kg))
        conn.commit()


# ============================================================================
# Разбор документов
# ============================================================================

def _meta_id(obj: Optional[Dict]) -> Optional[str]:
    if not obj:
        return None
    href = (obj.get("meta") or {}).get("href", "")
    return href.rstrip("/").rsplit("/", 1)[-1] if href else None


def parse_document(doc: Dict, doc_type: str) -> List[Dict[str, Any]]:
    """Документ МойСклада → строки движения (по одной на позицию)."""
    store = doc.get("store") or {}
    store_id = store.get("id") or _meta_id(store)
    positions = (doc.get("positions") or {}).get("rows") or []

    rows = []
    for pos in positions:
        item = pos.get("assortment") or {}
        quantity = float(pos.get("quantity") or 0)
        # Цена в копейках, как везде в API МойСклада
        price = float(pos.get("price") or 0) / 100
        rows.append({
            "doc_id": doc.get("id"),
            "doc_type": doc_type,
            "doc_name": doc.get("name"),
            "moment": doc.get("moment"),
            "store_id": store_id,
            "store_name": store.get("name"),
            "product_id": item.get("id") or _meta_id(item),
            "product_name": item.get("name"),
            "folder_id": _meta_id(item.get("productFolder")),
            "quantity": quantity,
            "price": price,
            "total": round(quantity * price, 2),
            "position_id": pos.get("id"),
        })
    return rows


def replace_window(storage, date_from: str, date_to: str, rows: List[Dict[str, Any]]) -> int:
    """
    Переписать окно дат целиком.

    Пересборка, а не UPSERT: документ могли удалить или убрать из него позицию,
    и при UPSERT такая строка навсегда осталась бы в расчёте.
    """
    with storage._get_connection() as conn:
        conn.execute(
            "DELETE FROM warehouse_flows WHERE moment >= ? AND moment <= ?",
            (f"{date_from} 00:00:00", f"{date_to} 23:59:59.999"),
        )
        conn.executemany("""
            INSERT INTO warehouse_flows
            (doc_id, doc_type, doc_name, moment, store_id, store_name, product_id,
             product_name, folder_id, quantity, price, total, position_id, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """, [
            (r["doc_id"], r["doc_type"], r["doc_name"], r["moment"], r["store_id"],
             r["store_name"], r["product_id"], r["product_name"], r["folder_id"],
             r["quantity"], r["price"], r["total"], r["position_id"])
            for r in rows
        ])
        conn.commit()
    return len(rows)


def sync_window(storage, client, date_from: str, date_to: str) -> Dict[str, int]:
    """
    Загрузить документы оприходования и списания за окно и переписать его.

    Читает обе сущности целиком, потому что окно пересобирается атомарно: иначе
    отчёт мог бы увидеть период, где приход уже удалён, а списание ещё не
    записано, — и доля списания подскочила бы до бесконечности.
    """
    all_rows: List[Dict[str, Any]] = []
    counts = {}

    for entity, doc_type in ((DOC_ENTER, DOC_ENTER), (DOC_LOSS, DOC_LOSS)):
        offset = 0
        docs_count = 0
        while True:
            response = client.get(f"/entity/{entity}", params={
                "filter": f"moment>={date_from} 00:00:00;moment<={date_to} 23:59:59",
                "expand": "positions.assortment,store",
                "order": "moment,asc",
                "limit": PAGE_LIMIT,
                "offset": offset,
            })
            if response is None:
                raise RuntimeError(f"МойСклад не ответил на запрос {entity}")

            docs = response.get("rows", [])
            if not docs:
                break

            for doc in docs:
                all_rows.extend(parse_document(doc, doc_type))
            docs_count += len(docs)

            if len(docs) < PAGE_LIMIT:
                break
            offset += PAGE_LIMIT

        counts[entity] = docs_count

    replace_window(storage, date_from, date_to, all_rows)
    counts["positions"] = len(all_rows)
    logger.info(
        f"Склад {date_from}—{date_to}: оприходований {counts.get(DOC_ENTER, 0)}, "
        f"списаний {counts.get(DOC_LOSS, 0)}, позиций {len(all_rows)}"
    )
    return counts


# ============================================================================
# Чтение
# ============================================================================

def totals_by_store(storage, date_from: str, date_to: str) -> Dict[str, Dict[str, Any]]:
    """
    Движение за период по складам МойСклада.

    {store_id: {store_name, flower_in, flower_out, berry_in_sum, berry_in_qty,
                berry_out_qty, berry_qty_per_kg}}
    """
    with storage._get_connection() as conn:
        rows = conn.execute("""
            SELECT f.store_id, f.store_name, f.doc_type, g.kind,
                   MAX(g.qty_per_kg) AS qty_per_kg,
                   SUM(f.total) AS total, SUM(f.quantity) AS quantity
            FROM warehouse_flows f
            JOIN warehouse_groups g ON g.folder_id = f.folder_id
            WHERE f.moment >= ? AND f.moment <= ?
            GROUP BY f.store_id, f.store_name, f.doc_type, g.kind
        """, (f"{date_from} 00:00:00", f"{date_to} 23:59:59.999")).fetchall()

    result: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        cell = result.setdefault(row["store_id"], {
            "store_name": row["store_name"],
            "flower_in": 0.0, "flower_out": 0.0,
            "berry_in_sum": 0.0, "berry_in_qty": 0.0, "berry_out_qty": 0.0,
            "berry_qty_per_kg": None,
        })
        total = round(row["total"] or 0, 2)
        quantity = row["quantity"] or 0

        if row["kind"] == KIND_FLOWER:
            if row["doc_type"] == DOC_ENTER:
                cell["flower_in"] += total
            else:
                cell["flower_out"] += total
        elif row["kind"] == KIND_BERRY:
            if row["qty_per_kg"]:
                cell["berry_qty_per_kg"] = row["qty_per_kg"]
            if row["doc_type"] == DOC_ENTER:
                cell["berry_in_sum"] += total
                cell["berry_in_qty"] += quantity
            else:
                cell["berry_out_qty"] += quantity

    return result


def list_stores_with_flows(storage, date_from: str, date_to: str) -> List[Dict[str, Any]]:
    """Склады с движением за период — для поиска несопоставленных."""
    with storage._get_connection() as conn:
        rows = conn.execute("""
            SELECT store_id, MAX(store_name) AS store_name,
                   COUNT(DISTINCT doc_id) AS docs, SUM(total) AS total
            FROM warehouse_flows
            WHERE moment >= ? AND moment <= ?
            GROUP BY store_id
        """, (f"{date_from} 00:00:00", f"{date_to} 23:59:59.999")).fetchall()
    return [
        {"key": r["store_id"], "name": r["store_name"], "docs": r["docs"],
         "amount": round(r["total"] or 0, 2)}
        for r in rows if r["store_id"]
    ]


def health_snapshot(storage) -> Dict[str, Any]:
    """Техническое состояние витрины движения товара для /health."""
    with storage._get_connection() as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "warehouse_flows" not in tables:
            return {"table": False, "rows": 0}

        row = conn.execute("""
            SELECT COUNT(*) AS rows, COUNT(DISTINCT doc_id) AS docs,
                   COUNT(DISTINCT store_id) AS stores,
                   MIN(moment) AS since, MAX(moment) AS until
            FROM warehouse_flows
        """).fetchone()
        groups = conn.execute(
            "SELECT kind, COUNT(*) AS cnt FROM warehouse_groups GROUP BY kind"
        ).fetchall() if "warehouse_groups" in tables else []
        # Позиции, чья папка не отнесена ни к одной группе, в расчёт не идут —
        # если их много, показатели занижены, и это видно только отсюда
        unknown = conn.execute("""
            SELECT COUNT(*) AS cnt FROM warehouse_flows f
            LEFT JOIN warehouse_groups g ON g.folder_id = f.folder_id
            WHERE g.folder_id IS NULL
        """).fetchone() if "warehouse_groups" in tables else None

    return {
        "table": True,
        "rows": row["rows"],
        "documents": row["docs"],
        "stores": row["stores"],
        "since": row["since"],
        "until": row["until"],
        "groups": {r["kind"]: r["cnt"] for r in groups},
        "rows_outside_groups": unknown["cnt"] if unknown else None,
    }


def data_range(storage) -> Dict[str, Optional[str]]:
    with storage._get_connection() as conn:
        row = conn.execute(
            "SELECT MIN(moment) AS since, MAX(moment) AS until FROM warehouse_flows"
        ).fetchone()
    return {"since": row["since"], "until": row["until"]}


def default_window(days: int = SYNC_WINDOW_DAYS) -> tuple:
    today = datetime.now().date()
    return (today - timedelta(days=days)).isoformat(), today.isoformat()
