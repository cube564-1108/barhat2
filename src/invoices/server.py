"""
Flask API сервер для модуля счетов на оплату БАРХАТ.

Заменяет форму согласования счетов в Pyrus: создание, согласование/отклонение,
список с фильтрами, CRUD справочников (статьи расхода, города, плательщики, НДС),
распределение по проектам/статьям, вложения, архив. Отправка в банк (Модульбанк)
и авторазноска в ПланФакт — отдельные фазы плана (5-6), пока не подключены;
до их готовности оплата отмечается вручную.

План: plans/2026-08-14-invoice-approval-automation.md
"""

import logging
import os
import sqlite3
import sys

from flask import Blueprint, jsonify, request, send_from_directory
from flask_login import current_user, login_required

# Импортируем модуль авторизации (как в cashshifts/server.py)
auth_path = os.path.join(os.path.dirname(__file__), '../')
sys.path.insert(0, auth_path)
from auth import role_required, section_required, log_action

from .storage import (
    STATUSES,
    ATTACHMENTS_DIR,
    get_all_stores,
    get_store_by_id,
    get_all_expense_categories,
    get_expense_category_by_id,
    create_expense_category,
    update_expense_category,
    delete_expense_category,
    get_all_cities,
    get_city_by_id,
    create_city,
    update_city,
    delete_city,
    get_all_payers,
    get_payer_by_id,
    create_payer,
    update_payer,
    delete_payer,
    get_all_vat_options,
    get_vat_option_by_id,
    create_vat_option,
    update_vat_option,
    delete_vat_option,
    create_invoice,
    get_invoice_by_id,
    user_can_access_invoice,
    can_edit_invoice_fields,
    can_edit_invoice_status,
    update_invoice,
    update_invoice_status,
    get_invoice_history,
    add_invoice_comment,
    get_invoice_comments,
    get_user_stores,
    get_users_full_names,
    list_invoices,
    approve_invoice,
    reject_invoice,
    mark_invoice_paid,
    set_invoice_archived,
    get_invoice_line_items,
    set_invoice_line_items,
    add_invoice_attachment,
    get_invoice_attachments,
    get_attachment_by_id,
    delete_attachment,
)

logger = logging.getLogger(__name__)

invoices_bp = Blueprint("invoices", __name__, url_prefix="/api/invoices")


# =============================================================================
# СПРАВОЧНИКИ (для форм на фронте) — общий паттерн для 4 простых словарей
# =============================================================================

def _register_reference_crud(name, get_all, get_by_id, create, update, delete):
    """Регистрирует стандартный набор роутов GET/POST/PUT/DELETE для справочника."""

    def list_view():
        return jsonify({name: get_all()})
    list_view.__name__ = f"get_{name}"

    def create_view():
        data = request.get_json(silent=True) or {}
        item_name = (data.get("name") or "").strip()
        if not item_name:
            return jsonify({"error": "Название обязательно"}), 400
        try:
            item_id = create(item_name)
        except sqlite3.IntegrityError:
            return jsonify({"error": f"«{item_name}» уже есть в справочнике"}), 400
        except Exception:
            logger.exception(f"create_{name} упал")
            return jsonify({"error": "Не удалось сохранить запись"}), 500
        try:
            log_action(current_user.username, f"create_{name}", item_name)
        except Exception:
            # Запись уже создана (item_id есть) — не проваливаем весь запрос
            # из-за сбоя аудит-лога, только фиксируем в логах сервера
            logger.exception(f"log_action упал после успешного create_{name} (id={item_id})")
        return jsonify({"ok": True, "id": item_id, "name": item_name}), 201
    create_view.__name__ = f"add_{name}"

    def update_view(item_id):
        if not get_by_id(item_id):
            return jsonify({"error": "Запись не найдена"}), 404
        data = request.get_json(silent=True) or {}
        item_name = (data.get("name") or "").strip()
        if not item_name:
            return jsonify({"error": "Название обязательно"}), 400
        try:
            update(item_id, item_name)
        except sqlite3.IntegrityError:
            return jsonify({"error": f"«{item_name}» уже есть в справочнике"}), 400
        log_action(current_user.username, f"update_{name}", f"{item_id}: {item_name}")
        return jsonify({"ok": True})
    update_view.__name__ = f"edit_{name}"

    def delete_view(item_id):
        if not get_by_id(item_id):
            return jsonify({"error": "Запись не найдена"}), 404
        delete(item_id)
        log_action(current_user.username, f"delete_{name}", str(item_id))
        return jsonify({"ok": True})
    delete_view.__name__ = f"remove_{name}"

    invoices_bp.route(f"/{name}", methods=["GET"])(section_required("invoices")(list_view))
    invoices_bp.route(f"/{name}", methods=["POST"])(role_required("admin")(create_view))
    invoices_bp.route(f"/{name}/<int:item_id>", methods=["PUT"])(role_required("admin")(update_view))
    invoices_bp.route(f"/{name}/<int:item_id>", methods=["DELETE"])(role_required("admin")(delete_view))


@invoices_bp.route("/stores", methods=["GET"])
@section_required("invoices")
def get_stores():
    """Список салонов (=проектов ПланФакт) для распределения (переиспользуем cashshifts.stores)."""
    return jsonify({"stores": get_all_stores()})


_register_reference_crud("categories", get_all_expense_categories, get_expense_category_by_id,
                          create_expense_category, update_expense_category, delete_expense_category)
_register_reference_crud("cities", get_all_cities, get_city_by_id,
                          create_city, update_city, delete_city)
_register_reference_crud("payers", get_all_payers, get_payer_by_id,
                          create_payer, update_payer, delete_payer)
_register_reference_crud("vat-options", get_all_vat_options, get_vat_option_by_id,
                          create_vat_option, update_vat_option, delete_vat_option)


# =============================================================================
# СЧЕТА НА ОПЛАТУ
# =============================================================================

@invoices_bp.route("", methods=["GET"])
@section_required("invoices")
def get_invoices():
    """
    Список счетов с фильтрами.

    Query params: status, store_id, city_id, created_by, counterparty,
    payment_purpose, created_from, created_to, due_from, due_to, archived,
    limit, offset
    """
    status = request.args.get("status")
    if status and status not in STATUSES:
        return jsonify({"error": f"Неизвестный статус. Доступны: {list(STATUSES)}"}), 400

    # Не-админ видит только счета своих салонов (или созданные им самим) —
    # см. user_can_access_invoice в storage.py
    restrict_username = None
    restrict_store_ids = None
    if current_user.role != "admin":
        restrict_username = current_user.username
        restrict_store_ids = get_user_stores(current_user.username)

    invoices = list_invoices(
        status=status,
        store_id=request.args.get("store_id", type=int),
        city_id=request.args.get("city_id", type=int),
        created_by=request.args.get("created_by"),
        counterparty=request.args.get("counterparty"),
        payment_purpose=request.args.get("payment_purpose"),
        created_from=request.args.get("created_from"),
        created_to=request.args.get("created_to"),
        due_from=request.args.get("due_from"),
        due_to=request.args.get("due_to"),
        is_archived=request.args.get("archived", "false").lower() == "true",
        limit=request.args.get("limit", 100, type=int),
        offset=request.args.get("offset", 0, type=int),
        restrict_username=restrict_username,
        restrict_store_ids=restrict_store_ids,
    )

    usernames = {inv.get("created_by") for inv in invoices}
    usernames.update(inv.get("approved_by") for inv in invoices)
    usernames.discard(None)
    full_names = get_users_full_names(list(usernames))
    for inv in invoices:
        inv["created_by_full_name"] = full_names.get(inv.get("created_by"))
        inv["approved_by_full_name"] = full_names.get(inv.get("approved_by"))

    return jsonify({"invoices": invoices, "count": len(invoices)})


@invoices_bp.route("/<int:invoice_id>", methods=["GET"])
@section_required("invoices")
def get_invoice(invoice_id):
    """Детали счёта — вместе со строками распределения и вложениями."""
    invoice = get_invoice_by_id(invoice_id)
    if not invoice:
        return jsonify({"error": "Счёт не найден"}), 404
    if not user_can_access_invoice(invoice, current_user.username, current_user.role):
        return jsonify({"error": "Нет доступа к этому счёту"}), 403

    invoice_full_names = get_users_full_names(
        [u for u in (invoice.get("created_by"), invoice.get("approved_by"), invoice.get("rejected_by")) if u]
    )
    invoice["created_by_full_name"] = invoice_full_names.get(invoice.get("created_by"))
    invoice["approved_by_full_name"] = invoice_full_names.get(invoice.get("approved_by"))
    invoice["rejected_by_full_name"] = invoice_full_names.get(invoice.get("rejected_by"))

    return jsonify({
        "invoice": invoice,
        "line_items": get_invoice_line_items(invoice_id),
        "attachments": get_invoice_attachments(invoice_id),
        "can_edit_fields": can_edit_invoice_fields(invoice, current_user.username, current_user.role),
        "can_edit_status": can_edit_invoice_status(invoice, current_user.role),
    })


@invoices_bp.route("/<int:invoice_id>", methods=["PUT"])
@section_required("invoices")
def edit_invoice(invoice_id):
    """
    Частично отредактировать поля счёта (не статус — см. /<id>/status).

    До согласования — автор счёта или админ. После согласования — только
    админ. После отправки в банк/оплаты или в архиве — нельзя никому.
    Body: любое подмножество редактируемых полей (см. can_edit_invoice_fields).
    """
    invoice = get_invoice_by_id(invoice_id)
    if not invoice:
        return jsonify({"error": "Счёт не найден"}), 404
    if not user_can_access_invoice(invoice, current_user.username, current_user.role):
        return jsonify({"error": "Нет доступа к этому счёту"}), 403
    if not can_edit_invoice_fields(invoice, current_user.username, current_user.role):
        return jsonify({"error": "Счёт закрыт для редактирования в текущем статусе"}), 409

    data = request.get_json(silent=True) or {}
    changes = {}

    if "city_id" in data:
        if not isinstance(data["city_id"], int) or not get_city_by_id(data["city_id"]):
            return jsonify({"error": "Некорректный город"}), 400
        changes["city_id"] = data["city_id"]

    if "payer_id" in data:
        if not isinstance(data["payer_id"], int) or not get_payer_by_id(data["payer_id"]):
            return jsonify({"error": "Некорректный плательщик"}), 400
        changes["payer_id"] = data["payer_id"]

    if "vat_id" in data:
        if data["vat_id"] is not None and not get_vat_option_by_id(data["vat_id"]):
            return jsonify({"error": "Некорректный вариант НДС"}), 400
        changes["vat_id"] = data["vat_id"]

    if "due_date" in data:
        if not (data["due_date"] or "").strip():
            return jsonify({"error": "Планируемая дата оплаты обязательна"}), 400
        changes["due_date"] = data["due_date"].strip()

    if "amount" in data:
        if not isinstance(data["amount"], (int, float)) or data["amount"] <= 0:
            return jsonify({"error": "Сумма должна быть положительным числом"}), 400
        changes["amount"] = data["amount"]

    if "payment_purpose" in data:
        if not (data["payment_purpose"] or "").strip():
            return jsonify({"error": "Назначение платежа обязательно"}), 400
        changes["payment_purpose"] = data["payment_purpose"].strip()

    for field in ("counterparty_name", "counterparty_inn", "counterparty_bank_name",
                  "counterparty_bank_bik", "counterparty_bank_account", "counterparty_bank_corr_account"):
        if field in data:
            changes[field] = data[field]

    if not changes:
        return jsonify({"error": "Нет полей для изменения"}), 400

    updated = update_invoice(invoice_id, changes, current_user.username)
    log_action(current_user.username, "edit_invoice", f"{invoice['invoice_number']}: {list(changes.keys())}")
    return jsonify({"ok": True, "invoice": updated})


@invoices_bp.route("/<int:invoice_id>/status", methods=["PUT"])
@role_required("admin")
def edit_invoice_status(invoice_id):
    """
    Прямая смена статуса счёта. Шире, чем обычное редактирование — не
    закрывается после отправки в банк/оплаты, единственная граница — архив,
    т.к. не все счета проходят через автозагрузку в банк и админу нужно
    иметь возможность проставить статус вручную в любой момент до архивации.
    Body: {"status": "on_approval"|"approved"|"rejected"|"sent_to_bank"|"paid"}
    """
    invoice = get_invoice_by_id(invoice_id)
    if not invoice:
        return jsonify({"error": "Счёт не найден"}), 404
    if not can_edit_invoice_status(invoice, current_user.role):
        return jsonify({"error": "Счёт в архиве — статус изменить нельзя"}), 409

    data = request.get_json(silent=True) or {}
    new_status = data.get("status")
    if new_status not in STATUSES:
        return jsonify({"error": f"Неизвестный статус. Доступны: {list(STATUSES)}"}), 400

    updated = update_invoice_status(invoice_id, new_status, current_user.username)
    log_action(current_user.username, "edit_invoice_status", f"{invoice['invoice_number']}: {invoice['status']} -> {new_status}")
    return jsonify({"ok": True, "invoice": updated})


@invoices_bp.route("", methods=["POST"])
@section_required("invoices")
def add_invoice():
    """
    Создать счёт на оплату. Сразу уходит на согласование (status=on_approval).

    Обязательные поля: city_id, payer_id, due_date, amount, payment_purpose.
    Остальное — опционально, включая line_items (распределение по проекту/статье).
    """
    data = request.get_json(silent=True) or {}

    city_id = data.get("city_id")
    payer_id = data.get("payer_id")
    due_date = (data.get("due_date") or "").strip()
    amount = data.get("amount")
    payment_purpose = (data.get("payment_purpose") or "").strip()

    if not isinstance(city_id, int) or not get_city_by_id(city_id):
        return jsonify({"error": "Некорректный город"}), 400

    if not isinstance(payer_id, int) or not get_payer_by_id(payer_id):
        return jsonify({"error": "Некорректный плательщик"}), 400

    if not due_date:
        return jsonify({"error": "Планируемая дата оплаты обязательна"}), 400

    if not isinstance(amount, (int, float)) or amount <= 0:
        return jsonify({"error": "Сумма должна быть положительным числом"}), 400

    if not payment_purpose:
        return jsonify({"error": "Назначение платежа обязательно"}), 400

    vat_id = data.get("vat_id")
    if vat_id is not None and not get_vat_option_by_id(vat_id):
        return jsonify({"error": "Некорректный вариант НДС"}), 400

    line_items = data.get("line_items") or []
    for item in line_items:
        if not get_store_by_id(item.get("store_id")):
            return jsonify({"error": "Некорректный проект (салон) в распределении"}), 400
        if not get_expense_category_by_id(item.get("expense_category_id")):
            return jsonify({"error": "Некорректная статья расхода в распределении"}), 400
        if not isinstance(item.get("amount"), (int, float)) or item["amount"] <= 0:
            return jsonify({"error": "Сумма строки распределения должна быть положительным числом"}), 400

    if line_items:
        total = sum(item["amount"] for item in line_items)
        if abs(total - amount) >= 0.01:
            return jsonify({"error": f"Сумма строк распределения ({total}) не равна сумме счёта ({amount})"}), 400

    try:
        invoice = create_invoice(
            amount=amount,
            payment_purpose=payment_purpose,
            created_by=current_user.username,
            city_id=city_id,
            payer_id=payer_id,
            vat_id=vat_id,
            counterparty_name=data.get("counterparty_name"),
            counterparty_inn=data.get("counterparty_inn"),
            counterparty_bank_name=data.get("counterparty_bank_name"),
            counterparty_bank_bik=data.get("counterparty_bank_bik"),
            counterparty_bank_account=data.get("counterparty_bank_account"),
            counterparty_bank_corr_account=data.get("counterparty_bank_corr_account"),
            due_date=due_date,
            line_items=line_items,
        )
    except sqlite3.IntegrityError:
        logger.exception("create_invoice упал с IntegrityError")
        return jsonify({"error": "Не удалось создать счёт, попробуйте ещё раз"}), 409

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


@invoices_bp.route("/<int:invoice_id>/mark-paid", methods=["POST"])
@role_required("admin")
def mark_paid(invoice_id):
    """
    Отметить счёт оплаченным вручную. Временная замена Фазы 5-6 плана
    (автоформирование платёжки в банк и авторазноска в ПланФакт) до появления
    токенов API этих сервисов.
    """
    invoice = get_invoice_by_id(invoice_id)
    if not invoice:
        return jsonify({"error": "Счёт не найден"}), 404

    if not mark_invoice_paid(invoice_id, current_user.username):
        return jsonify({"error": f"Счёт в статусе '{invoice['status']}', отметить оплаченным нельзя"}), 409

    log_action(current_user.username, "mark_invoice_paid", invoice["invoice_number"])
    return jsonify({"ok": True, "invoice": get_invoice_by_id(invoice_id)})


@invoices_bp.route("/<int:invoice_id>/archive", methods=["POST"])
@role_required("admin")
def archive_invoice_view(invoice_id):
    """Вручную перенести счёт в архив."""
    if not get_invoice_by_id(invoice_id):
        return jsonify({"error": "Счёт не найден"}), 404
    set_invoice_archived(invoice_id, True, current_user.username)
    log_action(current_user.username, "archive_invoice", str(invoice_id))
    return jsonify({"ok": True, "invoice": get_invoice_by_id(invoice_id)})


@invoices_bp.route("/<int:invoice_id>/unarchive", methods=["POST"])
@role_required("admin")
def unarchive_invoice_view(invoice_id):
    """Вручную вернуть счёт из архива."""
    if not get_invoice_by_id(invoice_id):
        return jsonify({"error": "Счёт не найден"}), 404
    set_invoice_archived(invoice_id, False, current_user.username)
    log_action(current_user.username, "unarchive_invoice", str(invoice_id))
    return jsonify({"ok": True, "invoice": get_invoice_by_id(invoice_id)})


# =============================================================================
# РАСПРЕДЕЛЕНИЕ ПО ПРОЕКТАМ/СТАТЬЯМ
# =============================================================================

@invoices_bp.route("/<int:invoice_id>/line-items", methods=["PUT"])
@section_required("invoices")
def update_line_items(invoice_id):
    """
    Полностью заменить распределение счёта по проектам/статьям.

    Body: {"items": [{"store_id", "expense_category_id", "amount"}, ...]}
    Доступно к правке до архивации счёта.
    """
    invoice = get_invoice_by_id(invoice_id)
    if not invoice:
        return jsonify({"error": "Счёт не найден"}), 404
    if not user_can_access_invoice(invoice, current_user.username, current_user.role):
        return jsonify({"error": "Нет доступа к этому счёту"}), 403
    if not can_edit_invoice_fields(invoice, current_user.username, current_user.role):
        return jsonify({"error": "Счёт закрыт для редактирования в текущем статусе"}), 409

    data = request.get_json(silent=True) or {}
    items = data.get("items") or []

    for item in items:
        if not get_store_by_id(item.get("store_id")):
            return jsonify({"error": "Некорректный проект (салон)"}), 400
        if not get_expense_category_by_id(item.get("expense_category_id")):
            return jsonify({"error": "Некорректная статья расхода"}), 400
        if not isinstance(item.get("amount"), (int, float)) or item["amount"] <= 0:
            return jsonify({"error": "Сумма строки должна быть положительным числом"}), 400

    result = set_invoice_line_items(invoice_id, items, current_user.username)
    if not result["ok"]:
        return jsonify({"error": result["error"]}), 400

    log_action(current_user.username, "update_invoice_line_items", str(invoice_id))
    return jsonify({"ok": True, "line_items": get_invoice_line_items(invoice_id)})


# =============================================================================
# ВЛОЖЕНИЯ
# =============================================================================

@invoices_bp.route("/<int:invoice_id>/attachments", methods=["GET"])
@section_required("invoices")
def list_attachments(invoice_id):
    invoice = get_invoice_by_id(invoice_id)
    if not invoice:
        return jsonify({"error": "Счёт не найден"}), 404
    if not user_can_access_invoice(invoice, current_user.username, current_user.role):
        return jsonify({"error": "Нет доступа к этому счёту"}), 403
    return jsonify({"attachments": get_invoice_attachments(invoice_id)})


@invoices_bp.route("/<int:invoice_id>/attachments", methods=["POST"])
@section_required("invoices")
def upload_attachment(invoice_id):
    """Загрузить вложение (скрин/скан счёта). multipart/form-data, поле 'file'."""
    invoice = get_invoice_by_id(invoice_id)
    if not invoice:
        return jsonify({"error": "Счёт не найден"}), 404
    if not user_can_access_invoice(invoice, current_user.username, current_user.role):
        return jsonify({"error": "Нет доступа к этому счёту"}), 403

    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "Файл не передан"}), 400

    result = add_invoice_attachment(invoice_id, file.filename, file.read(), current_user.username)
    if not result["ok"]:
        return jsonify({"error": result["error"]}), 400

    log_action(current_user.username, "upload_invoice_attachment", f"{invoice_id}: {file.filename}")
    return jsonify({"ok": True, "attachment": result["attachment"]}), 201


@invoices_bp.route("/attachments/<int:attachment_id>/download", methods=["GET"])
@section_required("invoices")
def download_attachment(attachment_id):
    attachment = get_attachment_by_id(attachment_id)
    if not attachment:
        return jsonify({"error": "Вложение не найдено"}), 404
    invoice = get_invoice_by_id(attachment["invoice_id"])
    if not invoice or not user_can_access_invoice(invoice, current_user.username, current_user.role):
        return jsonify({"error": "Нет доступа к этому вложению"}), 403
    return send_from_directory(
        os.path.abspath(ATTACHMENTS_DIR),
        attachment["stored_filename"],
        download_name=attachment["original_filename"],
    )


@invoices_bp.route("/attachments/<int:attachment_id>", methods=["DELETE"])
@role_required("admin")
def remove_attachment(attachment_id):
    if not get_attachment_by_id(attachment_id):
        return jsonify({"error": "Вложение не найдено"}), 404
    delete_attachment(attachment_id, current_user.username)
    log_action(current_user.username, "delete_invoice_attachment", str(attachment_id))
    return jsonify({"ok": True})


# =============================================================================
# ИСТОРИЯ ИЗМЕНЕНИЙ
# =============================================================================

@invoices_bp.route("/<int:invoice_id>/history", methods=["GET"])
@section_required("invoices")
def get_history(invoice_id):
    invoice = get_invoice_by_id(invoice_id)
    if not invoice:
        return jsonify({"error": "Счёт не найден"}), 404
    if not user_can_access_invoice(invoice, current_user.username, current_user.role):
        return jsonify({"error": "Нет доступа к этому счёту"}), 403

    history = get_invoice_history(invoice_id)
    full_names = get_users_full_names(list({h["changed_by"] for h in history}))
    for h in history:
        h["changed_by_full_name"] = full_names.get(h["changed_by"])

    return jsonify({"history": history})


# =============================================================================
# СООБЩЕНИЯ
# =============================================================================

@invoices_bp.route("/<int:invoice_id>/comments", methods=["GET"])
@section_required("invoices")
def list_comments(invoice_id):
    invoice = get_invoice_by_id(invoice_id)
    if not invoice:
        return jsonify({"error": "Счёт не найден"}), 404
    if not user_can_access_invoice(invoice, current_user.username, current_user.role):
        return jsonify({"error": "Нет доступа к этому счёту"}), 403

    comments = get_invoice_comments(invoice_id)
    full_names = get_users_full_names(list({c["author"] for c in comments}))
    for c in comments:
        c["author_full_name"] = full_names.get(c["author"])

    return jsonify({"comments": comments})


@invoices_bp.route("/<int:invoice_id>/comments", methods=["POST"])
@section_required("invoices")
def add_comment(invoice_id):
    """Body: {"message": str}. Доступно, пока есть доступ к счёту — даже после архивации (обсуждение не редактирование)."""
    invoice = get_invoice_by_id(invoice_id)
    if not invoice:
        return jsonify({"error": "Счёт не найден"}), 404
    if not user_can_access_invoice(invoice, current_user.username, current_user.role):
        return jsonify({"error": "Нет доступа к этому счёту"}), 403

    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "Сообщение не может быть пустым"}), 400

    comment = add_invoice_comment(invoice_id, current_user.username, message)
    return jsonify({"ok": True, "comment": comment}), 201
