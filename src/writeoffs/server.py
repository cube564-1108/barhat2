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

from flask import Blueprint, jsonify, request, send_from_directory
from flask_login import current_user, login_required

# Импортируем модуль авторизации (как в cashshifts/server.py, invoices/server.py)
auth_path = os.path.join(os.path.dirname(__file__), '../')
sys.path.insert(0, auth_path)
from auth import role_required, section_required, log_action

from cashshifts.storage import get_all_stores, get_store_by_id, get_user_stores, check_store_access
from moysklad.client import get_client, build_entity_href
from moysklad.storage import get_storage as get_moysklad_storage

from .storage import (
    STATUSES,
    ATTACHMENTS_DIR,
    get_moysklad_store,
    create_writeoff,
    get_writeoff_by_id,
    list_writeoffs,
    cancel_writeoff,
    lock_writeoff_for_sending,
    lock_writeoff_for_retry,
    mark_writeoff_sent,
    mark_writeoff_failed,
    reject_writeoff,
    add_writeoff_attachment,
    get_writeoff_attachments,
    get_writeoff_attachment_by_id,
    get_writeoff_position_by_id,
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
    Товары для выбора при списании — остатки склада точки (только > 0),
    из уже синхронизированного moysklad.db. Query params: store_id (обязателен).
    """
    store_id = request.args.get("store_id", type=int)
    if not store_id or not get_store_by_id(store_id):
        return jsonify({"error": "Некорректная точка продаж"}), 400
    if not _require_store_access(store_id):
        return jsonify({"error": "Нет доступа к этой точке"}), 403

    link = get_moysklad_store(store_id)
    if not link:
        return jsonify({"error": "Точка не сопоставлена складу МойСклад — обратитесь к админу"}), 400

    stock_rows = get_moysklad_storage().get_stock(store_id=link["moysklad_store_id"], limit=1000)
    items = [
        {
            "moysklad_product_id": row["product_id"],
            "product_name": row["product_name"],
            "quantity_available": row["quantity"],
        }
        for row in stock_rows
        if (row.get("quantity") or 0) > 0
    ]
    return jsonify({"items": items})


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
    return jsonify({"writeoffs": writeoffs, "count": len(writeoffs)})


@writeoffs_bp.route("/<int:writeoff_id>", methods=["GET"])
@section_required("writeoffs")
def get_writeoff(writeoff_id):
    writeoff = get_writeoff_by_id(writeoff_id)
    if not writeoff:
        return jsonify({"error": "Заявка не найдена"}), 404
    if not _require_store_access(writeoff["store_id"]):
        return jsonify({"error": "Нет доступа к этой точке"}), 403
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

        positions.append({
            "moysklad_product_id": product_id,
            "moysklad_product_href": build_entity_href("product", product_id),
            "product_name": product_name,
            "quantity": quantity,
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

def _send_to_moysklad(writeoff_id: int, store_id: int, positions: list) -> None:
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

    result = client.create_loss(
        organization_href=organization_href,
        store_href=link["moysklad_store_href"],
        positions=[
            {"assortment_href": pos["moysklad_product_href"], "quantity": pos["quantity"]}
            for pos in positions
        ],
        applicable=True,
        description=f"Списание #{writeoff_id} (дашборд БАРХАТ)",
    )

    if result and result.get("id"):
        mark_writeoff_sent(writeoff_id, result["id"])
    else:
        mark_writeoff_failed(writeoff_id, "МойСклад API вернул ошибку — подробности в логах сервера")


@writeoffs_bp.route("/<int:writeoff_id>/approve", methods=["POST"])
@role_required(*APPROVER_ROLES)
def approve(writeoff_id):
    """Согласовать заявку — сразу отправляет её в МойСклад одним документом."""
    writeoff = get_writeoff_by_id(writeoff_id)
    if not writeoff:
        return jsonify({"error": "Заявка не найдена"}), 404
    if not _require_store_access(writeoff["store_id"]):
        return jsonify({"error": "Нет доступа к этой точке"}), 403

    positions_without_photo = [p for p in writeoff["positions"] if not p["attachments"]]
    if positions_without_photo:
        names = ", ".join(p["product_name"] for p in positions_without_photo)
        return jsonify({"error": f"Нет фото у позиций: {names}. Согласовать нельзя."}), 400

    if not lock_writeoff_for_sending(writeoff_id, current_user.username):
        return jsonify({"error": "Заявку уже обрабатывает кто-то другой или она уже рассмотрена"}), 409

    _send_to_moysklad(writeoff_id, writeoff["store_id"], writeoff["positions"])

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
def retry(writeoff_id):
    """Повторить отправку упавшей заявки (status=failed)."""
    writeoff = get_writeoff_by_id(writeoff_id)
    if not writeoff:
        return jsonify({"error": "Заявка не найдена"}), 404
    if not _require_store_access(writeoff["store_id"]):
        return jsonify({"error": "Нет доступа к этой точке"}), 403

    if not lock_writeoff_for_retry(writeoff_id):
        return jsonify({"error": "Заявка не в статусе 'failed' или уже обрабатывается"}), 409

    _send_to_moysklad(writeoff_id, writeoff["store_id"], writeoff["positions"])

    log_action(current_user.username, "retry_writeoff", str(writeoff_id))
    return jsonify({"ok": True, "writeoff": get_writeoff_by_id(writeoff_id)})


# =============================================================================
# ВЛОЖЕНИЯ (фото списанного товара)
# =============================================================================

def _writeoff_for_position(position_id: int):
    position = get_writeoff_position_by_id(position_id)
    if not position:
        return None, None
    writeoff = get_writeoff_by_id(position["writeoff_id"])
    return position, writeoff


@writeoffs_bp.route("/positions/<int:position_id>/attachments", methods=["POST"])
@section_required("writeoffs")
def upload_attachment(position_id):
    """Загрузить фото списанного товара. multipart/form-data, поле 'file'."""
    position, writeoff = _writeoff_for_position(position_id)
    if not position:
        return jsonify({"error": "Позиция не найдена"}), 404
    if not _require_store_access(writeoff["store_id"]):
        return jsonify({"error": "Нет доступа к этой точке"}), 403

    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "Файл не передан"}), 400

    result = add_writeoff_attachment(position_id, file.filename, file.read(), current_user.username)
    if not result["ok"]:
        return jsonify({"error": result["error"]}), 400

    log_action(current_user.username, "upload_writeoff_attachment", f"{position_id}: {file.filename}")
    return jsonify({"ok": True, "attachment": result["attachment"]}), 201


@writeoffs_bp.route("/positions/<int:position_id>/attachments", methods=["GET"])
@section_required("writeoffs")
def list_attachments(position_id):
    position, writeoff = _writeoff_for_position(position_id)
    if not position:
        return jsonify({"error": "Позиция не найдена"}), 404
    if not _require_store_access(writeoff["store_id"]):
        return jsonify({"error": "Нет доступа к этой точке"}), 403
    return jsonify({"attachments": get_writeoff_attachments(position_id)})


@writeoffs_bp.route("/attachments/<int:attachment_id>/download", methods=["GET"])
@section_required("writeoffs")
def download_attachment(attachment_id):
    attachment = get_writeoff_attachment_by_id(attachment_id)
    if not attachment:
        return jsonify({"error": "Вложение не найдено"}), 404
    _, writeoff = _writeoff_for_position(attachment["position_id"])
    if not writeoff or not _require_store_access(writeoff["store_id"]):
        return jsonify({"error": "Нет доступа к этой точке"}), 403
    return send_from_directory(
        os.path.abspath(ATTACHMENTS_DIR),
        attachment["stored_filename"],
        download_name=attachment["original_filename"],
    )
