"""
Flask API сервер для модуля счетов на оплату БАРХАТ.

Заменяет форму согласования счетов в Pyrus: создание, согласование/отклонение,
список с фильтрами, CRUD статей расхода. Отправка в банк (Модульбанк) и
авторазноска в ПланФакт — отдельные фазы плана (5-6), пока не подключены.

План: plans/2026-08-14-invoice-approval-automation.md
"""

import logging
import os
import sys

from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required

# Импортируем модуль авторизации (как в cashshifts/server.py)
auth_path = os.path.join(os.path.dirname(__file__), '../')
sys.path.insert(0, auth_path)
from auth import role_required, section_required, log_action

from .storage import (
    STATUSES,
    get_all_stores,
    get_store_by_id,
    get_all_expense_categories,
    get_expense_category_by_id,
    create_expense_category,
    update_expense_category,
    delete_expense_category,
    create_invoice,
    get_invoice_by_id,
    list_invoices,
    approve_invoice,
    reject_invoice,
)

logger = logging.getLogger(__name__)

invoices_bp = Blueprint("invoices", __name__, url_prefix="/api/invoices")


# =============================================================================
# СПРАВОЧНИКИ (для форм на фронте)
# =============================================================================

@invoices_bp.route("/stores", methods=["GET"])
@section_required("invoices")
def get_stores():
    """Список салонов для выбора в форме счёта (переиспользуем cashshifts.stores)."""
    return jsonify({"stores": get_all_stores()})


@invoices_bp.route("/categories", methods=["GET"])
@section_required("invoices")
def get_categories():
    """Список активных статей расхода."""
    return jsonify({"categories": get_all_expense_categories()})


@invoices_bp.route("/categories", methods=["POST"])
@role_required("admin")
def add_category():
    """Создать статью расхода."""
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()

    if not name:
        return jsonify({"error": "Название обязательно"}), 400

    category_id = create_expense_category(name)
    log_action(current_user.username, "create_expense_category", name)
    return jsonify({"ok": True, "id": category_id, "name": name}), 201


@invoices_bp.route("/categories/<int:category_id>", methods=["PUT"])
@role_required("admin")
def edit_category(category_id):
    """Обновить название статьи расхода."""
    if not get_expense_category_by_id(category_id):
        return jsonify({"error": "Статья расхода не найдена"}), 404

    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()

    if not name:
        return jsonify({"error": "Название обязательно"}), 400

    update_expense_category(category_id, name)
    log_action(current_user.username, "update_expense_category", f"{category_id}: {name}")
    return jsonify({"ok": True})


@invoices_bp.route("/categories/<int:category_id>", methods=["DELETE"])
@role_required("admin")
def remove_category(category_id):
    """Деактивировать статью расхода."""
    if not get_expense_category_by_id(category_id):
        return jsonify({"error": "Статья расхода не найдена"}), 404

    delete_expense_category(category_id)
    log_action(current_user.username, "delete_expense_category", str(category_id))
    return jsonify({"ok": True})


# =============================================================================
# СЧЕТА НА ОПЛАТУ
# =============================================================================

@invoices_bp.route("", methods=["GET"])
@section_required("invoices")
def get_invoices():
    """
    Список счетов с фильтрами.

    Query params: status, store_id, date_from, date_to, limit, offset
    """
    status = request.args.get("status")
    if status and status not in STATUSES:
        return jsonify({"error": f"Неизвестный статус. Доступны: {list(STATUSES)}"}), 400

    store_id = request.args.get("store_id", type=int)
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    limit = request.args.get("limit", 100, type=int)
    offset = request.args.get("offset", 0, type=int)

    invoices = list_invoices(
        status=status, store_id=store_id,
        date_from=date_from, date_to=date_to,
        limit=limit, offset=offset,
    )
    return jsonify({"invoices": invoices, "count": len(invoices)})


@invoices_bp.route("/<int:invoice_id>", methods=["GET"])
@section_required("invoices")
def get_invoice(invoice_id):
    """Детали счёта."""
    invoice = get_invoice_by_id(invoice_id)
    if not invoice:
        return jsonify({"error": "Счёт не найден"}), 404
    return jsonify({"invoice": invoice})


@invoices_bp.route("", methods=["POST"])
@section_required("invoices")
def add_invoice():
    """
    Создать счёт на оплату. Сразу уходит на согласование (status=on_approval).

    Body: store_id, expense_category_id, counterparty_name, amount,
          description?, counterparty_inn?, counterparty_bank_name?,
          counterparty_bank_bik?, counterparty_bank_account?,
          counterparty_bank_corr_account?, due_date?
    """
    data = request.get_json(silent=True) or {}

    store_id = data.get("store_id")
    expense_category_id = data.get("expense_category_id")
    counterparty_name = (data.get("counterparty_name") or "").strip()
    amount = data.get("amount")

    if not isinstance(store_id, int) or not get_store_by_id(store_id):
        return jsonify({"error": "Некорректный салон"}), 400

    if not isinstance(expense_category_id, int) or not get_expense_category_by_id(expense_category_id):
        return jsonify({"error": "Некорректная статья расхода"}), 400

    if not counterparty_name:
        return jsonify({"error": "Название контрагента обязательно"}), 400

    if not isinstance(amount, (int, float)) or amount <= 0:
        return jsonify({"error": "Сумма должна быть положительным числом"}), 400

    invoice = create_invoice(
        store_id=store_id,
        expense_category_id=expense_category_id,
        counterparty_name=counterparty_name,
        amount=amount,
        created_by=current_user.username,
        description=data.get("description"),
        counterparty_inn=data.get("counterparty_inn"),
        counterparty_bank_name=data.get("counterparty_bank_name"),
        counterparty_bank_bik=data.get("counterparty_bank_bik"),
        counterparty_bank_account=data.get("counterparty_bank_account"),
        counterparty_bank_corr_account=data.get("counterparty_bank_corr_account"),
        due_date=data.get("due_date"),
    )

    log_action(current_user.username, "create_invoice", f"{invoice['invoice_number']}: {amount}")
    return jsonify({"ok": True, "invoice": invoice}), 201


@invoices_bp.route("/<int:invoice_id>/approve", methods=["POST"])
@role_required("admin")
def approve(invoice_id):
    """Согласовать счёт."""
    invoice = get_invoice_by_id(invoice_id)
    if not invoice:
        return jsonify({"error": "Счёт не найден"}), 404

    if not approve_invoice(invoice_id, current_user.username):
        return jsonify({"error": f"Счёт в статусе '{invoice['status']}', согласовать нельзя"}), 409

    log_action(current_user.username, "approve_invoice", invoice["invoice_number"])
    return jsonify({"ok": True, "invoice": get_invoice_by_id(invoice_id)})


@invoices_bp.route("/<int:invoice_id>/reject", methods=["POST"])
@role_required("admin")
def reject(invoice_id):
    """Отклонить счёт. Body: reason?"""
    invoice = get_invoice_by_id(invoice_id)
    if not invoice:
        return jsonify({"error": "Счёт не найден"}), 404

    data = request.get_json(silent=True) or {}
    reason = data.get("reason")

    if not reject_invoice(invoice_id, current_user.username, reason):
        return jsonify({"error": f"Счёт в статусе '{invoice['status']}', отклонить нельзя"}), 409

    log_action(current_user.username, "reject_invoice", f"{invoice['invoice_number']}: {reason or ''}")
    return jsonify({"ok": True, "invoice": get_invoice_by_id(invoice_id)})
