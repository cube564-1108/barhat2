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
import re
import sqlite3
import sys
import threading
from datetime import datetime, timedelta
from typing import Optional

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
    update_payer_bank_requisites,
    get_payer_bank_requisites,
    payer_has_bank_requisites,
    set_invoice_bank_send_error,
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
    update_expense_category_planfact_id,
    get_all_store_planfact_mappings,
    set_store_planfact_project,
    delete_store_planfact_project,
    start_planfact_sync_log,
    finish_planfact_sync_log,
    get_latest_planfact_sync_log,
    record_planfact_unmatched,
    get_unresolved_planfact_unmatched,
    resolve_planfact_unmatched,
    get_invoice_by_match_code,
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


@invoices_bp.route("/payers/<int:payer_id>/bank-requisites", methods=["PUT"])
@role_required("admin")
def set_payer_bank_requisites(payer_id):
    """
    Реквизиты расчётного счёта плательщика (Фаза 5) — своя запись у каждой
    компании ("На кого выставлен счёт"), все компании в одном кабинете
    Модульбанка под одним токеном. Пустые реквизиты = этот плательщик не
    проводится через банк-автоматику, счёт закрывается вручную.
    Body: {"inn","kpp","bank_account","bank_name","bank_bik","bank_corr_account"}
    """
    if not get_payer_by_id(payer_id):
        return jsonify({"error": "Плательщик не найден"}), 404
    data = request.get_json(silent=True) or {}
    update_payer_bank_requisites(
        payer_id,
        inn=(data.get("inn") or "").strip() or None,
        kpp=(data.get("kpp") or "").strip() or None,
        bank_account=(data.get("bank_account") or "").strip() or None,
        bank_name=(data.get("bank_name") or "").strip() or None,
        bank_bik=(data.get("bank_bik") or "").strip() or None,
        bank_corr_account=(data.get("bank_corr_account") or "").strip() or None,
    )
    log_action(current_user.username, "set_payer_bank_requisites", str(payer_id))
    return jsonify({"ok": True, "payer": get_payer_by_id(payer_id)})


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

    for field in ("counterparty_name", "counterparty_inn", "counterparty_kpp", "counterparty_bank_name",
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
            counterparty_kpp=data.get("counterparty_kpp"),
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


_INVOICE_REQUIRED_BANK_FIELDS = (
    "counterparty_name", "counterparty_inn", "counterparty_bank_name",
    "counterparty_bank_bik", "counterparty_bank_account", "counterparty_bank_corr_account",
)

_VAT_NO_TAX_RE = re.compile(r"без\s*нал|без\s*нд[сc]|не облага", re.IGNORECASE)
_VAT_RATE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*%")


def _format_vat_suffix(vat_name: Optional[str], amount: float) -> str:
    """
    Фраза про НДС для назначения платежа. У 1С-обмена (document.py) нет
    отдельного структурного поля под НДС — по 383-П это указывается текстом
    внутри назначения платежа, банк вытаскивает ставку/сумму из него сам
    (баг: без этой фразы поле НДС в личном кабинете банка оставалось пустым).
    Если название варианта НДС не удаётся распознать как ставку/безНДС —
    подставляем его как есть, лучше показать что-то, чем ничего.
    """
    if not vat_name:
        return ""
    name = vat_name.strip()
    if _VAT_NO_TAX_RE.search(name):
        return "Без налога (НДС)."
    match = _VAT_RATE_RE.search(name)
    if match:
        rate = float(match.group(1).replace(",", "."))
        vat_amount = round(amount * rate / (100 + rate), 2)
        return f"В том числе НДС {match.group(1)}% — {vat_amount:.2f} руб."
    return f"НДС: {name}."


def _build_bank_payment_purpose(invoice: dict, vat_name: Optional[str]) -> str:
    """
    match_code — строго в начале строки (см. план, "Уточнения от владельца
    после исследования API ПланФакт") — Фаза 6 матчит операции ПланФакт
    регэкспом REF-\\d{6} по этому полю. Без него платёж, ушедший в банк,
    никогда не находится синхронизацией (баг: код не попадал в назначение
    платежа при автоотправке в Модульбанк).
    """
    parts = [invoice["match_code"], invoice["payment_purpose"]]
    vat_suffix = _format_vat_suffix(vat_name, invoice["amount"])
    if vat_suffix:
        parts.append(vat_suffix)
    return " ".join(parts)


def _send_invoice_to_bank(invoice: dict, sandbox: bool, changed_by: str) -> dict:
    """
    Собрать платёжку в формате 1С из реквизитов счёта (получатель) и
    реквизитов выбранного плательщика (см. invoice_payers.bank_*, Фаза 5) и
    загрузить черновик в Модульбанк. Не трогает Flask-глобалы (request/
    current_user/jsonify) — вынесена отдельно от вьюхи specifically, чтобы
    её можно было прогнать в тестах без Flask-контекста (см.
    scripts/test_modulbank.py), по аналогии с _run_planfact_sync выше.

    Возвращает {"ok": bool, "http_status": int, "body": dict} — body уже в
    форме, готовой под jsonify(), http_status — какой код вернуть вьюхе.
    """
    if not sandbox and invoice["status"] != "approved":
        return {"ok": False, "http_status": 409,
                "body": {"error": f"Счёт в статусе '{invoice['status']}', отправить в банк нельзя"}}

    missing_invoice_fields = [f for f in _INVOICE_REQUIRED_BANK_FIELDS if not invoice.get(f)]
    if missing_invoice_fields:
        return {"ok": False, "http_status": 400,
                "body": {"error": f"В счёте не заполнены реквизиты контрагента: {', '.join(missing_invoice_fields)}"}}

    if not payer_has_bank_requisites(invoice["payer_id"]):
        payer = get_payer_by_id(invoice["payer_id"])
        payer_name = payer["name"] if payer else invoice["payer_id"]
        return {"ok": False, "http_status": 400, "body": {
            "error": f"У плательщика «{payer_name}» не настроены реквизиты Модульбанка — "
                     f"добавьте их в справочнике плательщиков или оплатите счёт вручную и отметьте оплаченным"
        }}

    payer_requisites = get_payer_bank_requisites(invoice["payer_id"])
    recipient = {
        "name": invoice["counterparty_name"],
        "inn": invoice["counterparty_inn"],
        "kpp": invoice.get("counterparty_kpp"),
        "account": invoice["counterparty_bank_account"],
        "bank_name": invoice["counterparty_bank_name"],
        "bank_bik": invoice["counterparty_bank_bik"],
        "bank_corr_account": invoice["counterparty_bank_corr_account"],
    }

    try:
        from modulbank.client import get_client
        client = get_client(sandbox_mode=sandbox)
    except ValueError as e:
        return {"ok": False, "http_status": 400, "body": {"error": str(e)}}

    vat_option = get_vat_option_by_id(invoice["vat_id"]) if invoice.get("vat_id") else None
    purpose = _build_bank_payment_purpose(invoice, vat_option["name"] if vat_option else None)

    result = client.send_invoice_payment(
        doc_num=str(invoice["id"]),
        date=datetime.now().strftime("%Y-%m-%d"),
        amount=invoice["amount"],
        purpose=purpose,
        payer=payer_requisites,
        recipient=recipient,
    )

    if sandbox:
        return {"ok": result["ok"], "http_status": 200, "body": {"ok": result["ok"], "sandbox": True, "result": result}}

    if result["ok"]:
        set_invoice_bank_send_error(invoice["id"], None)
        updated = update_invoice_status(invoice["id"], "sent_to_bank", changed_by)
        return {"ok": True, "http_status": 200, "body": {"ok": True, "invoice": updated, "result": result}}

    error_message = "; ".join(result["errors"]) if result["errors"] else "Неизвестная ошибка банка"
    set_invoice_bank_send_error(invoice["id"], error_message)
    return {"ok": False, "http_status": 502, "body": {"ok": False, "error": error_message, "result": result}}


@invoices_bp.route("/<int:invoice_id>/send-to-bank", methods=["POST"])
@role_required("admin")
def send_to_bank(invoice_id):
    """
    Body: {"sandbox": bool}. sandbox=true — черновик уходит в тестовый контур
    банка, статус счёта не меняется, используется для проверки сборки
    документа перед первой боевой отправкой (см. план, Фаза 5). Банк всегда
    создаёт статус "Черновик" — подписание доступно только вручную в личном
    кабинете, поэтому даже боевая (не sandbox) отправка сама по себе не
    двигает деньги.
    """
    invoice = get_invoice_by_id(invoice_id)
    if not invoice:
        return jsonify({"error": "Счёт не найден"}), 404

    data = request.get_json(silent=True) or {}
    sandbox = bool(data.get("sandbox"))

    outcome = _send_invoice_to_bank(invoice, sandbox, current_user.username)

    # Логируем только реальные обращения к банку — не ранние отказы валидации
    # (не заполнены реквизиты и т.п.), иначе лог засоряется попытками из UI
    if "result" in outcome["body"]:
        action = "send_invoice_to_bank_sandbox" if sandbox else (
            "send_invoice_to_bank" if outcome["ok"] else "send_invoice_to_bank_failed"
        )
        log_action(current_user.username, action, invoice["invoice_number"])

    return jsonify(outcome["body"]), outcome["http_status"]


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


# =============================================================================
# ПЛАНФАКТ — АВТОРАЗНОСКА ОПЛАЧЕННЫХ СЧЕТОВ (Фаза 6 плана)
# =============================================================================
#
# Матчинг ищет операции ПланФакт с "REF-" в назначении платежа (наш собственный
# match_code, см. storage.py) вместо попытки угадать системную категорию
# "нераспределённые расходы" — её точный API-идентификатор нигде не задоку-
# ментирован дословно (см. src/planfact/README.md, раздел "Известные пробелы").
#
# Запуск — только вручную, кнопкой в дашборде (см. src/planfact/README.md
# почему не сделали периодический автопоток на первом этапе). dry_run=true
# ничего не пишет в ПланФакт и не меняет статусы счетов — только показывает,
# что было бы сделано, для проверки перед первым боевым запуском.

_MATCH_CODE_RE = re.compile(r"REF-\d{6}")
_PLANFACT_POLL_WINDOW_DAYS = 60


def _match_planfact_operation(op, client, store_map, category_map, dry_run):
    """Обработать одну операцию ПланФакт. Возвращает dict с ключом 'status':
    'skip' (не наша операция или уже разнесена раньше), 'matched' (успех/превью)
    или 'unmatched' (нужна ручная разноска, см. 'reason')."""
    operation_id = str(op.get("operationId") or op.get("id") or "")
    comment = op.get("comment") or ""
    match = _MATCH_CODE_RE.search(comment)
    if not match:
        return {"status": "skip"}
    match_code = match.group(0)

    invoice = get_invoice_by_match_code(match_code)
    if not invoice:
        return {
            "status": "unmatched", "operation_id": operation_id, "match_code": match_code,
            "reason": f"Нет счёта с кодом {match_code}",
        }

    if invoice["status"] == "paid":
        return {"status": "skip"}

    if invoice["status"] not in ("approved", "sent_to_bank"):
        return {
            "status": "unmatched", "operation_id": operation_id, "match_code": match_code,
            "invoice_id": invoice["id"],
            "reason": f"Счёт {invoice['invoice_number']} в статусе «{invoice['status']}» — разноска невозможна",
        }

    line_items = get_invoice_line_items(invoice["id"])
    if not line_items:
        return {
            "status": "unmatched", "operation_id": operation_id, "match_code": match_code,
            "invoice_id": invoice["id"],
            "reason": f"Счёт {invoice['invoice_number']} ещё не распределён по проектам/статьям",
        }

    pf_items = []
    for li in line_items:
        project_id = store_map.get(li["store_id"])
        pf_category_id = category_map.get(li["expense_category_id"])
        if not project_id or not pf_category_id:
            store = get_store_by_id(li["store_id"])
            category = get_expense_category_by_id(li["expense_category_id"])
            return {
                "status": "unmatched", "operation_id": operation_id, "match_code": match_code,
                "invoice_id": invoice["id"],
                "reason": (
                    f"Не настроено сопоставление с ПланФакт: "
                    f"салон «{store['name'] if store else li['store_id']}» "
                    f"или статья «{category['name'] if category else li['expense_category_id']}»"
                ),
            }
        pf_items.append({
            "calculationDate": op.get("operationDate"),
            "isCalculationCommitted": bool(op.get("isCommitted", True)),
            "contrAgentId": (op.get("contrAgent") or {}).get("contrAgentId"),
            "operationCategoryId": int(pf_category_id),
            "projectId": int(project_id),
            "value": li["amount"],
        })

    preview = {
        "status": "matched",
        "operation_id": operation_id,
        "match_code": match_code,
        "invoice_id": invoice["id"],
        "invoice_number": invoice["invoice_number"],
        "operation_amount": op.get("value"),
        "items": pf_items,
    }
    if dry_run:
        return preview

    # Реальный ответ ПланФакт вкладывает счёт списания в объект account
    # ({"account": {"accountId": ...}}), а не плоским полем на операции —
    # баг: раньше брали op.get("accountId") (всегда None), ПланФакт отвечал
    # "Не указан счёт" на каждую попытку разноски (см. историю сессий,
    # инцидент 2026-08-18). Аналогично amount у операции на самом деле в
    # поле "value", "accountId" плоско не существует нигде на операции.
    ok = client.update_outcome_operation(
        operation_id,
        operation_date=op.get("operationDate"),
        account_id=(op.get("account") or {}).get("accountId"),
        comment=comment,
        is_committed=bool(op.get("isCommitted", True)),
        items=pf_items,
    )
    if not ok:
        return {
            "status": "unmatched", "operation_id": operation_id, "match_code": match_code,
            "invoice_id": invoice["id"],
            "reason": "Ошибка записи в ПланФакт (подробности в логах сервера)",
        }

    mark_invoice_paid(invoice["id"], changed_by="planfact-sync")
    return preview


def _run_planfact_sync(dry_run: bool = False) -> dict:
    from planfact.client import get_client

    client = get_client()
    store_map = get_all_store_planfact_mappings()
    categories = get_all_expense_categories()
    category_map = {c["id"]: c["planfact_category_id"] for c in categories if c.get("planfact_category_id")}

    date_start = (datetime.now() - timedelta(days=_PLANFACT_POLL_WINDOW_DAYS)).strftime("%Y-%m-%d")

    matched = []
    unmatched = []
    offset = 0
    while True:
        ops = client.list_operations(
            operation_type=["Outcome"],
            search_string="REF-",
            operation_date_start=date_start,
            offset=offset,
            limit=1000,
        )
        if ops is None:
            raise RuntimeError("Не удалось получить список операций из ПланФакт")
        if not ops:
            break

        for op in ops:
            result = _match_planfact_operation(op, client, store_map, category_map, dry_run)
            if result["status"] == "matched":
                matched.append(result)
            elif result["status"] == "unmatched":
                unmatched.append(result)
                if not dry_run:
                    record_planfact_unmatched(
                        planfact_operation_id=result["operation_id"],
                        reason=result["reason"],
                        match_code=result.get("match_code"),
                        invoice_id=result.get("invoice_id"),
                        operation_amount=op.get("value"),
                        operation_comment=op.get("comment"),
                    )

        if len(ops) < 1000:
            break
        offset += 1000

    return {"matched": matched, "unmatched": unmatched}


@invoices_bp.route("/planfact/sync", methods=["POST"])
@role_required("admin")
def trigger_planfact_sync():
    """
    Body: {"dry_run": bool}. dry_run=true — синхронный превью-прогон, ничего
    не пишет в ПланФакт и не меняет счета, возвращает результат сразу.
    dry_run=false — реальный прогон в фоновом потоке (по аналогии с
    /api/moysklad/sync), статус — через /planfact/sync-status.
    """
    data = request.get_json(silent=True) or {}
    dry_run = bool(data.get("dry_run"))

    if dry_run:
        try:
            result = _run_planfact_sync(dry_run=True)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            logger.exception("Ошибка dry-run синхронизации с ПланФакт")
            return jsonify({"error": str(e)}), 502
        return jsonify({"ok": True, "dry_run": True, **result})

    last_log = get_latest_planfact_sync_log()
    if last_log and last_log["status"] == "started":
        return jsonify({"error": "Синхронизация уже запущена"}), 409

    try:
        log_id = start_planfact_sync_log(dry_run=False)
    except Exception:
        logger.exception("Не удалось создать запись лога синхронизации ПланФакт")
        return jsonify({"error": "Не удалось запустить синхронизацию"}), 500

    def run_in_background():
        try:
            result = _run_planfact_sync(dry_run=False)
            finish_planfact_sync_log(log_id, len(result["matched"]), len(result["unmatched"]), status="completed")
        except Exception as e:
            logger.exception("Ошибка синхронизации с ПланФакт")
            finish_planfact_sync_log(log_id, 0, 0, status="failed", error_message=str(e))

    thread = threading.Thread(target=run_in_background, daemon=True)
    thread.start()

    log_action(current_user.username, "trigger_planfact_sync", "")
    return jsonify({"ok": True, "message": "Синхронизация запущена"})


@invoices_bp.route("/planfact/sync-status", methods=["GET"])
@role_required("admin")
def planfact_sync_status():
    return jsonify({"status": get_latest_planfact_sync_log()})


@invoices_bp.route("/planfact/unmatched", methods=["GET"])
@role_required("admin")
def list_planfact_unmatched():
    return jsonify({"unmatched": get_unresolved_planfact_unmatched()})


@invoices_bp.route("/planfact/unmatched/<int:unmatched_id>/resolve", methods=["POST"])
@role_required("admin")
def resolve_planfact_unmatched_view(unmatched_id):
    """Отметить, что операция разнесена вручную — убрать из списка "Требует внимания"."""
    resolve_planfact_unmatched(unmatched_id)
    log_action(current_user.username, "resolve_planfact_unmatched", str(unmatched_id))
    return jsonify({"ok": True})


@invoices_bp.route("/planfact/mappings/stores", methods=["GET"])
@role_required("admin")
def get_store_planfact_mappings():
    """Салоны вместе с текущим сопоставлением на проект ПланФакт (если настроено)."""
    mapping = get_all_store_planfact_mappings()
    stores = get_all_stores()
    return jsonify({"stores": [
        {**store, "planfact_project_id": mapping.get(store["id"])}
        for store in stores
    ]})


@invoices_bp.route("/planfact/mappings/stores/<int:store_id>", methods=["PUT"])
@role_required("admin")
def set_store_planfact_mapping(store_id):
    """Body: {"planfact_project_id": str}. Пустое значение — снять сопоставление."""
    if not get_store_by_id(store_id):
        return jsonify({"error": "Салон не найден"}), 404
    data = request.get_json(silent=True) or {}
    planfact_project_id = (data.get("planfact_project_id") or "").strip()
    if not planfact_project_id:
        delete_store_planfact_project(store_id)
        log_action(current_user.username, "unset_store_planfact_mapping", str(store_id))
        return jsonify({"ok": True})
    set_store_planfact_project(store_id, planfact_project_id)
    log_action(current_user.username, "set_store_planfact_mapping", f"{store_id}: {planfact_project_id}")
    return jsonify({"ok": True})


@invoices_bp.route("/categories/<int:category_id>/planfact-mapping", methods=["PUT"])
@role_required("admin")
def set_category_planfact_mapping(category_id):
    """Body: {"planfact_category_id": str}. Пустое значение — снять сопоставление."""
    if not get_expense_category_by_id(category_id):
        return jsonify({"error": "Статья не найдена"}), 404
    data = request.get_json(silent=True) or {}
    planfact_category_id = (data.get("planfact_category_id") or "").strip() or None
    update_expense_category_planfact_id(category_id, planfact_category_id)
    log_action(current_user.username, "set_category_planfact_mapping", f"{category_id}: {planfact_category_id}")
    return jsonify({"ok": True})


# Инцидент 2026-08-17: вкладка "Сопоставление" дёргает эти два live-эндпоинта
# при каждом открытии без кэша; на нескольких быстрых перезагрузках медленные/
# рейтлимитящие ответы ПланФакт заняли собой обоих gunicorn-воркеров (их
# всего 2, amvera.yml) и подвесили весь сайт. Короткий TTL-кэш в процессе —
# самая простая защита от повторного залпа запросов при повторных открытиях
# вкладки одним и тем же админом; данные тут не критичны к свежести (это
# просто список для выпадающего списка настройки).
_planfact_dropdown_cache: dict = {}
_PLANFACT_DROPDOWN_CACHE_TTL_SECONDS = 120


def _get_planfact_dropdown_cached(cache_key: str, fetch_fn):
    cached = _planfact_dropdown_cache.get(cache_key)
    if cached and (datetime.now() - cached["at"]).total_seconds() < _PLANFACT_DROPDOWN_CACHE_TTL_SECONDS:
        return cached["value"]
    value = fetch_fn()
    if value is not None:
        _planfact_dropdown_cache[cache_key] = {"value": value, "at": datetime.now()}
    return value


@invoices_bp.route("/planfact/projects", methods=["GET"])
@role_required("admin")
def get_planfact_projects():
    """Живой список проектов ПланФакт — для выпадающего списка при настройке сопоставления."""
    from planfact.client import get_client
    try:
        client = get_client()
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    projects = _get_planfact_dropdown_cached("projects", client.get_projects)
    if projects is None:
        return jsonify({"error": "Не удалось получить проекты из ПланФакт"}), 502
    return jsonify({"projects": projects})


@invoices_bp.route("/planfact/categories", methods=["GET"])
@role_required("admin")
def get_planfact_categories():
    """Живой список статей расходов ПланФакт — для настройки сопоставления."""
    from planfact.client import get_client
    try:
        client = get_client()
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    categories = _get_planfact_dropdown_cached("categories", lambda: client.get_operation_categories("Outcome"))
    if categories is None:
        return jsonify({"error": "Не удалось получить статьи из ПланФакт"}), 502
    return jsonify({"categories": categories})
