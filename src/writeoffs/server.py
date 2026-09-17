"""
Flask API сервер для модуля списаний товара БАРХАТ.

Флорист подаёт заявку (несколько позиций: товар из каталога МойСклад + кол-во +
фото + причина) -> управляющий/админ согласует -> заявка одним документом
"Списание" уходит в МойСклад. Паттерн workflow — как в src/invoices/server.py,
паттерн доступа к точкам — как в src/cashshifts/server.py.

План: plans/2026-08-16-stock-writeoffs-module.md
"""

import logging
import os
import sys
import time
from datetime import datetime

from flask import Blueprint, jsonify, request, send_from_directory
from flask_login import current_user, login_required

# Импортируем модуль авторизации (как в cashshifts/server.py, invoices/server.py)
auth_path = os.path.join(os.path.dirname(__file__), '../')
sys.path.insert(0, auth_path)
from auth import role_required, section_required, log_action, require_ajax_header

from cashshifts.storage import get_all_stores, get_store_by_id, get_user_stores, check_store_access, get_users_full_names
from moysklad.client import get_client, build_entity_href
from moysklad.storage import get_storage as get_moysklad_storage

from .storage import (
    STATUSES,
    ATTACHMENTS_DIR,
    get_moysklad_store,
    list_moysklad_store_links,
    link_moysklad_store,
    get_moysklad_employee,
    list_moysklad_employee_links,
    link_moysklad_employee,
    create_writeoff,
    get_writeoff_by_id,
    list_writeoffs,
    cancel_writeoff,
    lock_writeoff_for_sending,
    lock_writeoff_for_retry,
    mark_writeoff_sent,
    mark_writeoff_failed,
    reject_writeoff,
    LastPhotoError,
    add_writeoff_photo,
    get_writeoff_head,
    get_writeoff_photos,
    get_writeoff_photo_by_id,
    delete_writeoff_photo,
)

logger = logging.getLogger(__name__)

writeoffs_bp = Blueprint("writeoffs", __name__, url_prefix="/api/writeoffs")

APPROVER_ROLES = ("admin", "manager")


def _require_store_access(store_id: int) -> bool:
    return check_store_access(current_user.username, store_id, current_user.role)


def _accessible_store_ids():
    """None означает "без ограничений" (admin) — иначе список ID точек пользователя."""
    if current_user.role == "admin":
        return None
    return get_user_stores(current_user.username)


# =============================================================================
# ЕДИНИЦЫ ИЗМЕРЕНИЯ
#
# Единица берётся из МойСклад по каждому товару, а не подписывается словом
# "шт." в интерфейсе. У клубники, бананов, винограда, фиников и чернослива в
# МойСклад стоят граммы (у остальных ~145 позиций — штуки), и зашитое "шт."
# заставляло флориста вводить количество в штуках там, где склад считает
# граммы. Список товаров-исключений в коде не держим: единицу меняют в
# МойСклад, а не в дашборде.
#
# Справочник единиц и связка товар -> единица меняются раз в год, а живой
# запрос к внешнему API на каждый вход в форму уже клал воркеров (см. память
# по внешним API) — держим в памяти воркера с TTL.
# =============================================================================

_UOM_CACHE_TTL_SEC = 6 * 3600
_UOM_REQUEST_TIMEOUT_SEC = 8
_UOM_MIN_SECONDS_LEFT = 10

_uom_names_cache = {"value": None, "expires": 0.0}
_product_uom_cache = {"value": None, "expires": 0.0}


def _cached(cache, loader):
    """Значение из кэша или свежее. Отказ МойСклад не затирает прошлое значение."""
    now = time.monotonic()
    if cache["value"] is not None and now < cache["expires"]:
        return cache["value"]
    value = loader()
    if value is None:
        return cache["value"]  # просроченный справочник полезнее пустого
    cache["value"] = value
    cache["expires"] = now + _UOM_CACHE_TTL_SEC
    return value


def _id_from_href(href: str) -> str:
    return href.split('?')[0].rstrip('/').split('/')[-1] if href else ''


def _load_uom_names(client):
    """{uom_id: 'г'} — справочник единиц измерения (несколько десятков строк)."""
    response = client.get('/entity/uom', params={'limit': 1000}, timeout=_UOM_REQUEST_TIMEOUT_SEC)
    if not response:
        return None
    return {
        row.get('id'): (row.get('name') or '').strip()
        for row in response.get('rows', [])
        if row.get('id')
    }


def _load_product_uoms(client):
    """{product_id: uom_id} по всему ассортименту — на случай, если отчёт остатков единицу не отдал."""
    result = {}
    offset = 0
    for _ in range(5):
        response = client.get(
            '/entity/assortment',
            params={'limit': 1000, 'offset': offset},
            timeout=_UOM_REQUEST_TIMEOUT_SEC,
        )
        if response is None:
            return None
        rows = response.get('rows', [])
        for row in rows:
            product_id = row.get('id')
            uom_id = _id_from_href(((row.get('uom') or {}).get('meta') or {}).get('href', ''))
            if product_id and uom_id:
                result[product_id] = uom_id
        if len(rows) < 1000:
            break
        offset += 1000
    return result


def _fill_uom_names(client, items, seconds_left):
    """
    Проставить позициям каталога единицу измерения.

    Первый источник — сам отчёт остатков (бесплатно, если он отдаёт uom).
    Дальше — справочник единиц, и только если и этого мало — ассортимент.
    Каждый шаг делается лишь для позиций, у которых единицы ещё нет, и только
    пока остаётся запас времени: каталог без подписи единицы хуже, чем с ней,
    но занятый на минуту воркер хуже обоих.
    """
    if seconds_left() < _UOM_MIN_SECONDS_LEFT or all(i["uom_name"] for i in items):
        return

    names = _cached(_uom_names_cache, lambda: _load_uom_names(client)) or {}
    for item in items:
        if not item["uom_name"] and item["_uom_id"]:
            item["uom_name"] = names.get(item["_uom_id"], "")

    if not names or all(i["uom_name"] for i in items) or seconds_left() < _UOM_MIN_SECONDS_LEFT:
        return

    product_uoms = _cached(_product_uom_cache, lambda: _load_product_uoms(client)) or {}
    for item in items:
        if item["uom_name"]:
            continue
        uom_id = product_uoms.get(item["moysklad_product_id"])
        if uom_id:
            item["uom_name"] = names.get(uom_id, "")


# =============================================================================
# СПРАВОЧНИКИ
# =============================================================================

@writeoffs_bp.route("/stores", methods=["GET"])
@section_required("writeoffs")
def get_stores():
    """Точки продаж, доступные текущему пользователю (переиспользуем cashshifts.stores)."""
    store_ids = _accessible_store_ids()
    stores = get_all_stores()
    if store_ids is not None:
        stores = [s for s in stores if s["id"] in store_ids]
    return jsonify({"stores": stores})


@writeoffs_bp.route("/catalog", methods=["GET"])
@section_required("writeoffs")
def get_catalog():
    """
    Товары для выбора при списании — весь ассортимент склада точки.

    Запрашивается у МойСклад напрямую (report/stock/all?filter=store=<href>),
    не из локального синка: остатки локально ни разу не были синхронизированы
    корректно (см. Фазу 7 плана — save_stock падал на каждой строке из-за
    несовпадения числа колонок/значений, плюс /report/stock/all вообще не
    отдаёт store_id на верхнем уровне без фильтра по конкретному складу).
    Прямой запрос, отфильтрованный по одному складу, у МойСклад быстрый
    (проверено эмпирически) и не зависит от свежести локальной синхронизации —
    остаток и так должен быть максимально актуальным на момент списания.

    Позиции с остатком <= 0 НЕ отфильтровываются: у расходников и клубники
    учёт регулярно уходит в минус (продажи проведены, приход — нет), и раньше
    такие товары просто пропадали из списка — списать их было нельзя
    (проверено 2026-08-20: на точке «Восход, 3» 49 из 129 позиций были <= 0,
    в т.ч. клубника k1 с остатком -2695). Остаток отдаём как есть, решение
    принимает фронт: отрицательный помечает, но выбрать даёт.

    Query params: store_id (обязателен).
    """
    store_id = request.args.get("store_id", type=int)
    if not store_id or not get_store_by_id(store_id):
        return jsonify({"error": "Некорректная точка продаж"}), 400
    if not _require_store_access(store_id):
        return jsonify({"error": "Нет доступа к этой точке"}), 403

    link = get_moysklad_store(store_id)
    if not link:
        return jsonify({"error": "Точка не сопоставлена складу МойСклад — обратитесь к админу"}), 400

    try:
        client = get_client()
    except ValueError as e:
        return jsonify({"error": f"МойСклад не настроен: {e}"}), 500

    # Пагинация: сейчас на складе ~130 позиций и всё влезает в одну страницу,
    # но молча обрезать ассортимент на 1000-й позиции нельзя — товар просто
    # исчезнет из списка без единой ошибки. PAGE_CAP — страховка от бесконечного
    # цикла, если МойСклад начнёт игнорировать offset.
    # DEADLINE — жёсткий потолок на весь эндпоинт: у клиента таймаут 30 сек на
    # запрос, и без общего дедлайна цикл мог бы занять воркер на минуты. Воркеров
    # на бою всего два, живые запросы к внешнему API уже клали сайт целиком.
    PAGE_SIZE = 1000
    PAGE_CAP = 20
    DEADLINE_SEC = 25

    started = time.monotonic()
    seconds_left = lambda: DEADLINE_SEC - (time.monotonic() - started)  # noqa: E731

    items = []
    offset = 0
    for _ in range(PAGE_CAP):
        if seconds_left() < 0:
            logger.warning("Каталог списаний: превышен дедлайн %s сек (точка %s)", DEADLINE_SEC, store_id)
            return jsonify({"error": "МойСклад отвечает слишком долго — попробуйте ещё раз"}), 504

        response = client.get('/report/stock/all', params={
            'filter': f'store={link["moysklad_store_href"]}',
            'limit': PAGE_SIZE,
            'offset': offset,
        })
        if response is None:
            return jsonify({"error": "Не удалось получить остатки из МойСклад"}), 502

        rows = response.get('rows', [])
        for row in rows:
            product_href = row.get('meta', {}).get('href', '').split('?')[0]
            product_id = product_href.rstrip('/').split('/')[-1] if product_href else None
            if not product_id:
                continue
            uom = row.get('uom') or {}
            items.append({
                "moysklad_product_id": product_id,
                "moysklad_product_href": product_href,
                "product_name": row.get('name', ''),
                # Артикул и код нужны фронту для поиска: флористу привычнее
                # набрать «k1», чем полное название
                "article": row.get('article') or '',
                "code": row.get('code') or '',
                "quantity_available": row.get('stock', 0) or 0,
                # Единица измерения товара в МойСклад: у клубники и других
                # ягод/фруктов это граммы. Отчёт остатков её отдаёт не всегда,
                # поэтому ниже добираем через справочники (_fill_uom_names)
                "uom_name": (uom.get('name') or '').strip(),
                "_uom_id": _id_from_href((uom.get('meta') or {}).get('href', '')),
            })

        if len(rows) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    else:
        # Дошли до потолка страниц — список заведомо неполный. Отдавать его молча
        # нельзя: пропавший товар без единой ошибки — ровно тот баг, что чиним.
        logger.error("Каталог списаний: достигнут предел страниц (%s) для точки %s", PAGE_CAP, store_id)
        return jsonify({"error": "Не удалось получить весь каталог из МойСклад — обратитесь к админу"}), 502

    _fill_uom_names(client, items, seconds_left)
    for item in items:
        item.pop("_uom_id", None)

    items.sort(key=lambda i: i["product_name"].lower())
    return jsonify({"items": items})


@writeoffs_bp.route("/store-links", methods=["GET"])
@role_required("admin")
def get_store_links():
    """
    Текущая связка точек продаж со складами МойСклад (для проверки без захода
    в БД напрямую — на бою нет консоли на контейнер, эту БД видно только отсюда).
    """
    links = list_moysklad_store_links()
    stores_by_id = {s["id"]: s["name"] for s in get_all_stores()}
    moysklad_stores_by_id = {s.get("id"): s.get("name") for s in get_moysklad_storage().get_stores()}

    result = [
        {
            "store_id": link["store_id"],
            "store_name": stores_by_id.get(link["store_id"], f"#{link['store_id']}"),
            "moysklad_store_id": link["moysklad_store_id"],
            "moysklad_store_name": moysklad_stores_by_id.get(link["moysklad_store_id"], "?"),
        }
        for link in links
    ]
    linked_store_ids = {r["store_id"] for r in result}
    unlinked = [{"store_id": s["id"], "store_name": s["name"]} for s in get_all_stores() if s["id"] not in linked_store_ids]

    return jsonify({"links": result, "unlinked_stores": unlinked})


@writeoffs_bp.route("/store-links", methods=["POST"])
@role_required("admin")
def set_store_links():
    """
    Применить связку точка -> склад МойСклад прямо к БД, с которой работает этот
    процесс. Единственный способ настроить это на бою без shell-доступа к
    контейнеру — см. plans/2026-08-16-stock-writeoffs-module.md, Фаза 7.

    Body: {"links": [{"store_id": int, "moysklad_store_id": str}, ...]}
    """
    data = request.get_json(silent=True) or {}
    raw_links = data.get("links")
    if not isinstance(raw_links, list) or not raw_links:
        return jsonify({"error": "Нужен непустой список links"}), 400

    known_moysklad_ids = {s.get("id") for s in get_moysklad_storage().get_stores()}

    applied = []
    errors = []
    for entry in raw_links:
        store_id = entry.get("store_id")
        moysklad_store_id = entry.get("moysklad_store_id")

        if not isinstance(store_id, int) or not get_store_by_id(store_id):
            errors.append({"store_id": store_id, "error": "Точка не найдена"})
            continue
        if not moysklad_store_id or moysklad_store_id not in known_moysklad_ids:
            errors.append({"store_id": store_id, "error": "Склад МойСклад с таким id не найден"})
            continue

        href = build_entity_href("store", moysklad_store_id)
        link_moysklad_store(store_id, moysklad_store_id, href)
        applied.append({"store_id": store_id, "moysklad_store_id": moysklad_store_id})

    log_action(current_user.username, "set_writeoff_store_links", f"{len(applied)} применено, {len(errors)} ошибок")
    return jsonify({"ok": True, "applied": applied, "errors": errors})


@writeoffs_bp.route("/employee-links", methods=["GET"])
@role_required("admin")
def get_employee_links():
    """Текущая связка пользователей дашборда с сотрудниками/отделами МойСклад."""
    links = list_moysklad_employee_links()
    usernames = [link["username"] for link in links]
    full_names = get_users_full_names(usernames)

    result = [
        {
            "username": link["username"],
            "full_name": full_names.get(link["username"], link["username"]),
            "moysklad_employee_id": link["moysklad_employee_id"],
            "moysklad_group_id": link["moysklad_group_id"],
        }
        for link in links
    ]
    return jsonify({"links": result})


@writeoffs_bp.route("/employee-links", methods=["POST"])
@role_required("admin")
def set_employee_links():
    """
    Применить связку пользователь дашборда -> сотрудник + отдел МойСклад.
    Тот же приём, что и store-links — прямо в БД работающего процесса,
    без shell-доступа к контейнеру на бою.

    Body: {"links": [{"username": str, "moysklad_employee_id": str, "moysklad_group_id": str}, ...]}
    """
    data = request.get_json(silent=True) or {}
    raw_links = data.get("links")
    if not isinstance(raw_links, list) or not raw_links:
        return jsonify({"error": "Нужен непустой список links"}), 400

    try:
        client = get_client()
        employees_response = client.get_employees()
        groups_response = client.get_groups()
    except ValueError as e:
        return jsonify({"error": f"МойСклад не настроен: {e}"}), 500

    known_employee_ids = {r.get("id") for r in (employees_response or {}).get("rows", [])}
    known_group_ids = {r.get("id") for r in (groups_response or {}).get("rows", [])}

    applied = []
    errors = []
    for entry in raw_links:
        username = entry.get("username")
        employee_id = entry.get("moysklad_employee_id")
        group_id = entry.get("moysklad_group_id")

        if not username:
            errors.append({"username": username, "error": "Не указан пользователь"})
            continue
        if not employee_id or employee_id not in known_employee_ids:
            errors.append({"username": username, "error": "Сотрудник МойСклад с таким id не найден"})
            continue
        if not group_id or group_id not in known_group_ids:
            errors.append({"username": username, "error": "Отдел МойСклад с таким id не найден"})
            continue

        link_moysklad_employee(
            username,
            employee_id,
            build_entity_href("employee", employee_id),
            group_id,
            build_entity_href("group", group_id),
        )
        applied.append({"username": username, "moysklad_employee_id": employee_id, "moysklad_group_id": group_id})

    log_action(current_user.username, "set_writeoff_employee_links", f"{len(applied)} применено, {len(errors)} ошибок")
    return jsonify({"ok": True, "applied": applied, "errors": errors})


# Разовая починка старых документов: пачка за вызов, а не «всё сразу».
# Каждый документ — это запрос себестоимости плюс PUT на позицию, то есть
# секунды на сетевых вызовах; воркеров на проде два, и занимать один надолго
# нельзя (см. CLAUDE.md про внешние вызовы из интерфейса).
BACKFILL_DOCS_PER_CALL = 10
BACKFILL_MAX_DOCS_PER_CALL = 25
BACKFILL_DEADLINE_SECONDS = 25
BACKFILL_DEFAULT_SINCE = "2026-08-01"


@writeoffs_bp.route("/admin/backfill-prices", methods=["POST"])
@role_required("admin")
@require_ajax_header
def backfill_prices():
    """
    Проставить себестоимость в старых документах списания с нулевой суммой.

    До 17.09.2026 дашборд отправлял позиции без цены, и МойСклад сохранял их
    нулевыми навсегда. Такие документы занижают списание в «Показателях
    салонов» и искажают себестоимость в самом МойСкладе, а починить их изнутри
    контейнера нечем — консоли на нашем тарифе Amvera нет.

    Цена берётся на МОМЕНТ документа, а не на сегодня: партии с тех пор
    сменились. Правятся только документы, созданные дашбордом, — чужие
    (заведённые руками в МойСкладе) не трогаем даже при нулевой сумме.

    Body: {"since": "YYYY-MM-DD", "limit": int, "dry_run": bool}
    dry_run по умолчанию True — сначала показать, что будет сделано.
    """
    data = request.get_json(silent=True) or {}

    since = (data.get("since") or BACKFILL_DEFAULT_SINCE).strip()
    try:
        datetime.strptime(since, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "since должен быть датой YYYY-MM-DD"}), 400

    dry_run = data.get("dry_run", True) is not False
    try:
        limit = int(data.get("limit") or BACKFILL_DOCS_PER_CALL)
    except (TypeError, ValueError):
        return jsonify({"error": "limit должен быть числом"}), 400
    limit = max(1, min(limit, BACKFILL_MAX_DOCS_PER_CALL))

    try:
        client = get_client()
    except ValueError as e:
        return jsonify({"error": f"МойСклад не настроен: {e}"}), 500

    # limit=50: при большем МойСклад не разворачивает positions.rows
    response = client.get("/entity/loss", params={
        "filter": f"moment>={since} 00:00:00;sum=0",
        "expand": "positions.assortment,store",
        "order": "moment,asc",
        "limit": 50,
    })
    if response is None:
        return jsonify({"error": "МойСклад не ответил на запрос списаний"}), 502

    ours = [doc for doc in response.get("rows", [])
            if (doc.get("description") or "").startswith("Списание #")]

    started = time.monotonic()
    processed = updated_positions = updated_docs = 0
    without_cost = estimated_positions = 0
    details = []
    errors = []

    for doc in ours:
        if processed >= limit or time.monotonic() - started > BACKFILL_DEADLINE_SECONDS:
            break
        processed += 1

        store_href = ((doc.get("store") or {}).get("meta") or {}).get("href")
        positions = (doc.get("positions") or {}).get("rows") or []
        targets = [p for p in positions if not (p.get("price") or 0) > 0]
        if not store_href or not targets:
            continue

        hrefs = [((p.get("assortment") or {}).get("meta") or {}).get("href", "").split("?")[0]
                 for p in targets]
        prices, sources = _resolve_prices(client, store_href, [h for h in hrefs if h],
                                          moment=doc["moment"][:19])

        doc_touched = False
        for position, href in zip(targets, hrefs):
            price = prices.get(href)
            name = ((position.get("assortment") or {}).get("name")) or "?"
            if not price:
                without_cost += 1
                details.append({"document": doc.get("name"), "position": name,
                                "source": "none",
                                "result": "цену взять неоткуда — нет ни партий, ни приходов"})
                continue

            source = sources.get(href, "stock")
            if source == "purchase":
                estimated_positions += 1
            entry = {
                "document": doc.get("name"), "position": name,
                "price": round(price / 100, 2),
                "sum": round(price * (position.get("quantity") or 0) / 100, 2),
                "source": source,
            }

            if dry_run:
                updated_positions += 1
                doc_touched = True
                entry["result"] = ("будет проставлена" if source == "stock"
                                   else "будет проставлена по цене прихода")
                details.append(entry)
                continue

            result = client.update_loss_position_price(doc["id"], position["id"], price)
            if result is None:
                errors.append({"document": doc.get("name"), "position": name,
                               "error": "МойСклад отклонил правку позиции"})
                continue
            updated_positions += 1
            doc_touched = True
            entry["result"] = ("проставлена" if source == "stock"
                               else "проставлена по цене прихода")
            details.append(entry)

        if doc_touched:
            updated_docs += 1

    log_action(
        current_user.username, "backfill_writeoff_prices",
        f"{'проверка' if dry_run else 'правка'}: документов {processed}, "
        f"позиций {updated_positions}, без цены {without_cost}, ошибок {len(errors)}"
    )

    return jsonify({
        "ok": True,
        "dry_run": dry_run,
        "since": since,
        "documents_found": len(ours),
        "documents_processed": processed,
        "documents_updated": updated_docs,
        "positions_updated": updated_positions,
        "positions_estimated": estimated_positions,
        "positions_without_cost": without_cost,
        "remaining": max(0, len(ours) - processed),
        "details": details,
        "errors": errors,
    })


# =============================================================================
# ЗАЯВКИ НА СПИСАНИЕ
# =============================================================================

@writeoffs_bp.route("", methods=["GET"])
@section_required("writeoffs")
def get_writeoffs():
    """Список заявок с фильтрами. Query params: status, store_id, date_from, date_to, limit, offset."""
    status = request.args.get("status")
    if status and status not in STATUSES:
        return jsonify({"error": f"Неизвестный статус. Доступны: {list(STATUSES)}"}), 400

    accessible = _accessible_store_ids()
    requested_store_id = request.args.get("store_id", type=int)

    if requested_store_id is not None:
        if accessible is not None and requested_store_id not in accessible:
            return jsonify({"error": "Нет доступа к этой точке"}), 403
        store_ids = [requested_store_id]
    else:
        store_ids = accessible  # None = все точки (admin)

    writeoffs = list_writeoffs(
        store_ids=store_ids,
        status=status,
        date_from=request.args.get("date_from"),
        date_to=request.args.get("date_to"),
        limit=request.args.get("limit", 200, type=int),
        offset=request.args.get("offset", 0, type=int),
    )

    usernames = {w.get("created_by") for w in writeoffs}
    usernames.update(w.get("approved_by") for w in writeoffs)
    usernames.discard(None)
    full_names = get_users_full_names(list(usernames))
    for w in writeoffs:
        w["created_by_full_name"] = full_names.get(w.get("created_by"))
        w["approved_by_full_name"] = full_names.get(w.get("approved_by"))

    return jsonify({"writeoffs": writeoffs, "count": len(writeoffs)})


@writeoffs_bp.route("/<int:writeoff_id>", methods=["GET"])
@section_required("writeoffs")
def get_writeoff(writeoff_id):
    writeoff = get_writeoff_by_id(writeoff_id)
    if not writeoff:
        return jsonify({"error": "Заявка не найдена"}), 404
    if not _require_store_access(writeoff["store_id"]):
        return jsonify({"error": "Нет доступа к этой точке"}), 403

    full_names = get_users_full_names(
        [u for u in (writeoff.get("created_by"), writeoff.get("approved_by"), writeoff.get("rejected_by")) if u]
    )
    writeoff["created_by_full_name"] = full_names.get(writeoff.get("created_by"))
    writeoff["approved_by_full_name"] = full_names.get(writeoff.get("approved_by"))
    writeoff["rejected_by_full_name"] = full_names.get(writeoff.get("rejected_by"))

    return jsonify({"writeoff": writeoff})


@writeoffs_bp.route("", methods=["POST"])
@section_required("writeoffs")
def add_writeoff():
    """
    Создать заявку на списание. Body: {store_id, positions: [{moysklad_product_id,
    product_name, quantity, reason?}, ...]} — минимум одна позиция.
    """
    data = request.get_json(silent=True) or {}

    store_id = data.get("store_id")
    if not isinstance(store_id, int) or not get_store_by_id(store_id):
        return jsonify({"error": "Некорректная точка продаж"}), 400
    if not _require_store_access(store_id):
        return jsonify({"error": "Нет доступа к этой точке"}), 403

    if not get_moysklad_store(store_id):
        return jsonify({"error": "Точка не сопоставлена складу МойСклад — обратитесь к админу"}), 400

    raw_positions = data.get("positions")
    if not isinstance(raw_positions, list) or not raw_positions:
        return jsonify({"error": "Нужна хотя бы одна позиция"}), 400

    positions = []
    for pos in raw_positions:
        product_id = pos.get("moysklad_product_id")
        product_name = (pos.get("product_name") or "").strip()
        quantity = pos.get("quantity")

        if not product_id or not isinstance(product_id, str):
            return jsonify({"error": "Некорректный товар в позиции"}), 400
        if not product_name:
            return jsonify({"error": "Не указано название товара в позиции"}), 400
        if not isinstance(quantity, (int, float)) or quantity <= 0:
            return jsonify({"error": "Количество должно быть положительным числом"}), 400

        # Единица измерения приходит из нашего же каталога и нужна только для
        # показа («250 г», а не «250 шт.»): в МойСклад количество и так уходит
        # в базовой единице товара. Поэтому не сверяем со справочником, но
        # режем длину — это подпись, а не идентификатор.
        uom_name = (pos.get("uom_name") or "").strip()[:20] or None

        positions.append({
            "moysklad_product_id": product_id,
            "moysklad_product_href": build_entity_href("product", product_id),
            "product_name": product_name,
            "quantity": quantity,
            "uom_name": uom_name,
            "reason": pos.get("reason"),
        })

    writeoff = create_writeoff(store_id, current_user.username, positions)
    log_action(current_user.username, "create_writeoff", f"{writeoff['id']}: {len(positions)} поз.")
    return jsonify({"ok": True, "writeoff": writeoff}), 201


@writeoffs_bp.route("/<int:writeoff_id>", methods=["DELETE"])
@section_required("writeoffs")
def cancel(writeoff_id):
    """Отменить свою же заявку, пока она не рассмотрена (status=on_approval)."""
    writeoff = get_writeoff_by_id(writeoff_id)
    if not writeoff:
        return jsonify({"error": "Заявка не найдена"}), 404

    if not cancel_writeoff(writeoff_id, current_user.username):
        return jsonify({"error": "Заявку нельзя отменить — не ваша или уже рассмотрена"}), 409

    log_action(current_user.username, "cancel_writeoff", str(writeoff_id))
    return jsonify({"ok": True})


# =============================================================================
# СОГЛАСОВАНИЕ
# =============================================================================

def _resolve_prices(client, store_href: str, product_hrefs: list,
                    moment: str = None) -> tuple:
    """
    Цены для позиций списания: ({href: копейки}, {href: 'stock' | 'purchase'}).

    Два источника, и они не равнозначны:
      stock    — себестоимость по партиям, то есть факт учёта;
      purchase — цена ближайшего оприходования, то есть оценка.

    Второй нужен потому, что по учёту товар регулярно уходит в минус: расход
    (продажи, списания) обгоняет оприходование, партий нет, и себестоимости не
    существует — при том что товар есть на полке, а цена закупки заведена
    руками. Без запасного источника списание клубники всегда было бы нулевым:
    по сети она числится в минусе на сотни килограммов.

    Источник возвращается вместе с ценой: человек должен видеть, где факт, а
    где оценка, — иначе оценка незаметно становится «данными».
    """
    sources = {}
    prices = client.get_cost_prices(store_href, product_hrefs, moment=moment)
    for href in prices:
        sources[href] = "stock"

    missing = [href for href in product_hrefs if href and href not in prices]
    if missing:
        fallback = client.get_last_purchase_prices(store_href, missing, before_moment=moment)
        for href, price in fallback.items():
            prices[href] = price
            sources[href] = "purchase"

    return prices, sources


def _send_to_moysklad(writeoff_id: int, store_id: int, positions: list, created_by: str) -> None:
    """
    Отправить заявку в МойСклад одним документом "Списание". Заявка уже
    захвачена (status='processing') вызывающим кодом — здесь только сама
    отправка и фиксация результата (sent/failed).
    """
    link = get_moysklad_store(store_id)
    if not link:
        mark_writeoff_failed(writeoff_id, "Точка не сопоставлена складу МойСклад")
        return

    organization_href = os.environ.get("MOYSKLAD_ORGANIZATION_HREF")
    if not organization_href:
        mark_writeoff_failed(writeoff_id, "MOYSKLAD_ORGANIZATION_HREF не настроен в .env")
        return

    try:
        client = get_client()
    except ValueError as e:
        mark_writeoff_failed(writeoff_id, f"МойСклад не настроен: {e}")
        return

    # Если для флориста нет связки сотрудник/отдел — МойСклад подставит
    # дефолт (сотрудника API-токена, отдел "Основной"). Не блокируем
    # списание из-за незаполненного справочника, см. "Сопоставление" в UI.
    employee_link = get_moysklad_employee(created_by)

    # Себестоимость МойСклад в документ сам не подставляет и не пересчитывает
    # потом: без цены позиция навсегда остаётся нулевой (замер на проде
    # 17.09.2026), из-за чего «Показатели салонов» видят списание на 0 ₽.
    # Спрашиваем её у самого МойСклада по складу этой заявки.
    #
    # Отчёт не ответил или у товара нет партий на складе — отправляем как
    # раньше, без цены: непосчитанная стоимость не повод не дать флористу
    # списать товар. Потерянные цены видно по нулевой сумме документа.
    store_href = link["moysklad_store_href"]
    try:
        cost_prices, price_sources = _resolve_prices(
            client, store_href, [pos["moysklad_product_href"] for pos in positions]
        )
    except Exception as e:
        logger.warning(f"Списание #{writeoff_id}: себестоимость не получена ({e})")
        cost_prices, price_sources = {}, {}

    estimated = sum(1 for src in price_sources.values() if src == "purchase")
    if estimated:
        logger.info(
            f"Списание #{writeoff_id}: по {estimated} позициям взята цена последнего "
            f"прихода — себестоимости нет, товар по учёту в минусе"
        )

    missing = [pos["product_name"] for pos in positions
               if pos["moysklad_product_href"] not in cost_prices]
    if missing:
        logger.info(
            f"Списание #{writeoff_id}: нет себестоимости на складе для "
            f"{len(missing)} из {len(positions)} позиций ({', '.join(missing[:5])})"
        )

    result = client.create_loss(
        organization_href=organization_href,
        store_href=store_href,
        positions=[
            {
                "assortment_href": pos["moysklad_product_href"],
                "quantity": pos["quantity"],
                "price": cost_prices.get(pos["moysklad_product_href"]),
            }
            for pos in positions
        ],
        applicable=True,
        description=f"Списание #{writeoff_id} (дашборд БАРХАТ)",
        owner_href=employee_link["moysklad_employee_href"] if employee_link else None,
        group_href=employee_link["moysklad_group_href"] if employee_link else None,
    )

    if result and result.get("id"):
        mark_writeoff_sent(writeoff_id, result["id"])
    else:
        mark_writeoff_failed(writeoff_id, "МойСклад API вернул ошибку — подробности в логах сервера")


@writeoffs_bp.route("/<int:writeoff_id>/approve", methods=["POST"])
@role_required(*APPROVER_ROLES)
@require_ajax_header
def approve(writeoff_id):
    """Согласовать заявку — сразу отправляет её в МойСклад одним документом."""
    writeoff = get_writeoff_by_id(writeoff_id)
    if not writeoff:
        return jsonify({"error": "Заявка не найдена"}), 404
    if not _require_store_access(writeoff["store_id"]):
        return jsonify({"error": "Нет доступа к этой точке"}), 403

    # Фото — подтверждение списания, поэтому проверка остаётся. Но теперь из неё
    # есть выход: фото дозаливается в существующую заявку (POST /<id>/photos).
    if not writeoff["photos"]:
        return jsonify({
            "error": "К заявке не приложено фото. Добавьте фото и повторите согласование."
        }), 400

    if not lock_writeoff_for_sending(writeoff_id, current_user.username):
        return jsonify({"error": "Заявку уже обрабатывает кто-то другой или она уже рассмотрена"}), 409

    _send_to_moysklad(writeoff_id, writeoff["store_id"], writeoff["positions"], writeoff["created_by"])

    log_action(current_user.username, "approve_writeoff", str(writeoff_id))
    return jsonify({"ok": True, "writeoff": get_writeoff_by_id(writeoff_id)})


@writeoffs_bp.route("/<int:writeoff_id>/reject", methods=["POST"])
@role_required(*APPROVER_ROLES)
def reject(writeoff_id):
    """Отклонить заявку. Body: {reason?}"""
    writeoff = get_writeoff_by_id(writeoff_id)
    if not writeoff:
        return jsonify({"error": "Заявка не найдена"}), 404
    if not _require_store_access(writeoff["store_id"]):
        return jsonify({"error": "Нет доступа к этой точке"}), 403

    data = request.get_json(silent=True) or {}
    if not reject_writeoff(writeoff_id, current_user.username, data.get("reason")):
        return jsonify({"error": f"Заявка в статусе '{writeoff['status']}', отклонить нельзя"}), 409

    log_action(current_user.username, "reject_writeoff", str(writeoff_id))
    return jsonify({"ok": True, "writeoff": get_writeoff_by_id(writeoff_id)})


@writeoffs_bp.route("/<int:writeoff_id>/retry", methods=["POST"])
@role_required(*APPROVER_ROLES)
@require_ajax_header
def retry(writeoff_id):
    """Повторить отправку упавшей заявки (status=failed)."""
    writeoff = get_writeoff_by_id(writeoff_id)
    if not writeoff:
        return jsonify({"error": "Заявка не найдена"}), 404
    if not _require_store_access(writeoff["store_id"]):
        return jsonify({"error": "Нет доступа к этой точке"}), 403

    if not lock_writeoff_for_retry(writeoff_id):
        return jsonify({"error": "Заявка не в статусе 'failed' или уже обрабатывается"}), 409

    _send_to_moysklad(writeoff_id, writeoff["store_id"], writeoff["positions"], writeoff["created_by"])

    log_action(current_user.username, "retry_writeoff", str(writeoff_id))
    return jsonify({"ok": True, "writeoff": get_writeoff_by_id(writeoff_id)})


# =============================================================================
# ФОТО СПИСАНИЯ — на заявку целиком
#
# Раньше фото крепилось к позиции, и один кадр на шесть строк означал шесть
# загрузок одного файла (обращение #7). Здесь же закрыт главный тупик: фото
# можно ДОЗАЛИТЬ в уже созданную заявку. Без этого любой обрыв сети делал
# заявку непроводимой навсегда — проверка при согласовании требовала фото,
# а добавить его было нечем.
# =============================================================================

# Статусы, в которых состав фото ещё можно менять. В 'sent' поздно (документ уже
# в МойСклад), в 'processing' идёт отправка, в 'rejected'/'cancelled' — незачем.
PHOTO_EDITABLE_STATUSES = ("on_approval", "failed")


def _may_edit_photos(writeoff) -> bool:
    """Фото заявки правит её автор или тот, кто эту заявку согласует."""
    return (
        writeoff["created_by"] == current_user.username
        or current_user.role in APPROVER_ROLES
    )


@writeoffs_bp.route("/<int:writeoff_id>/photos", methods=["POST"])
@section_required("writeoffs")
@require_ajax_header
def upload_photo(writeoff_id):
    """Загрузить фото списания. multipart/form-data, поле 'file'."""
    writeoff = get_writeoff_head(writeoff_id)
    if not writeoff:
        return jsonify({"error": "Заявка не найдена"}), 404
    if not _require_store_access(writeoff["store_id"]):
        return jsonify({"error": "Нет доступа к этой точке"}), 403
    if not _may_edit_photos(writeoff):
        return jsonify({"error": "Фото может добавить автор заявки или согласующий"}), 403
    if writeoff["status"] not in PHOTO_EDITABLE_STATUSES:
        return jsonify({
            "error": f"Заявка в статусе «{writeoff['status']}» — фото уже не изменить"
        }), 409

    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "Файл не передан"}), 400

    result = add_writeoff_photo(writeoff_id, file.filename, file.read(), current_user.username)
    if not result["ok"]:
        return jsonify({"error": result["error"]}), 400

    log_action(current_user.username, "upload_writeoff_photo", f"{writeoff_id}: {file.filename}")
    return jsonify({"ok": True, "photo": result["photo"]}), 201


@writeoffs_bp.route("/<int:writeoff_id>/photos", methods=["GET"])
@section_required("writeoffs")
def list_photos(writeoff_id):
    writeoff = get_writeoff_head(writeoff_id)
    if not writeoff:
        return jsonify({"error": "Заявка не найдена"}), 404
    if not _require_store_access(writeoff["store_id"]):
        return jsonify({"error": "Нет доступа к этой точке"}), 403
    return jsonify({"photos": get_writeoff_photos(writeoff_id)})


@writeoffs_bp.route("/photos/<int:photo_id>", methods=["DELETE"])
@section_required("writeoffs")
@require_ajax_header
def delete_photo(photo_id):
    """
    Удалить фото заявки. Последнее удалить нельзя: заявка без фото не проходит
    согласование, а это ровно то состояние, из которого раньше не было выхода.
    """
    photo = get_writeoff_photo_by_id(photo_id)
    if not photo:
        return jsonify({"error": "Фото не найдено"}), 404
    if not _require_store_access(photo["writeoff_store_id"]):
        return jsonify({"error": "Нет доступа к этой точке"}), 403
    if not _may_edit_photos({"created_by": photo["writeoff_created_by"]}):
        return jsonify({"error": "Фото может удалить автор заявки или согласующий"}), 403
    if photo["writeoff_status"] != "on_approval":
        return jsonify({
            "error": f"Заявка в статусе «{photo['writeoff_status']}» — фото уже не изменить"
        }), 409

    try:
        removed = delete_writeoff_photo(photo_id)
    except LastPhotoError as e:
        return jsonify({"error": str(e)}), 409
    if not removed:
        return jsonify({"error": "Фото уже удалено"}), 404

    log_action(current_user.username, "delete_writeoff_photo", f"{photo['writeoff_id']}: {photo_id}")
    return jsonify({"ok": True})


@writeoffs_bp.route("/photos/<int:photo_id>/download", methods=["GET"])
@section_required("writeoffs")
def download_photo(photo_id):
    photo = get_writeoff_photo_by_id(photo_id)
    if not photo:
        return jsonify({"error": "Фото не найдено"}), 404
    if not _require_store_access(photo["writeoff_store_id"]):
        return jsonify({"error": "Нет доступа к этой точке"}), 403

    directory = os.path.abspath(ATTACHMENTS_DIR)
    # См. такую же проверку в invoices/server.py: файла может не быть, если он
    # попал на эфемерный диск сборки. Без неё общий обработчик 404 отвечает
    # "Endpoint not found", и это читается как сломанный маршрут.
    if not os.path.exists(os.path.join(directory, photo["stored_filename"])):
        logger.error(
            "Фото %s (%s) есть в БД, но файла нет в %s",
            photo_id, photo["original_filename"], directory
        )
        return jsonify({
            "error": "Файл фото не найден на диске — загрузите его заново"
        }), 404

    return send_from_directory(
        directory,
        photo["stored_filename"],
        download_name=photo["original_filename"],
    )
