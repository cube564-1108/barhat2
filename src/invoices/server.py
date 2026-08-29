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
import time
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from flask import Blueprint, jsonify, request, send_from_directory
from flask_login import current_user, login_required

# Импортируем модуль авторизации (как в cashshifts/server.py)
auth_path = os.path.join(os.path.dirname(__file__), '../')
sys.path.insert(0, auth_path)
from auth import role_required, section_required, log_action

from .storage import (
    STATUSES,
    INVOICE_SORT_FIELDS,
    ATTACHMENTS_DIR,
    count_invoices,
    get_invoices_summary,
    list_invoice_authors,
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
    can_delete_invoice,
    delete_invoice,
    update_invoice,
    update_invoice_with_line_items,
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
    find_auto_archived_invoices,
    mark_invoice_planfact_synced,
    send_invoice_to_clarification,
    resubmit_invoice,
    list_invoice_templates,
    get_invoice_template,
    create_invoice_template,
    update_invoice_template,
    delete_invoice_template,
    template_requisite_mismatch,
    _validate_template_items,
    TEMPLATE_COUNTERPARTY_FIELDS,
    get_invoice_line_items,
    set_invoice_line_items,
    counterparty_data_report,
    list_counterparties,
    search_counterparties,
    get_counterparty_by_id,
    create_counterparty,
    update_counterparty,
    delete_counterparty,
    remember_counterparty_from_invoice,
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
from .cards import (
    list_cards,
    get_card_by_id,
    get_cards_for_user,
    create_card,
    update_card,
    deactivate_card,
    is_valid_account_id,
)

logger = logging.getLogger(__name__)

# Банк и ПланФакт живут по московскому времени; фиксированный сдвиг вместо
# pytz — в РФ нет перехода на летнее время с 2014 года (тот же приём, что в
# src/modulbank/document.py и src/cashshifts/retailcrm_client.py)
_MOSCOW_TZ = timezone(timedelta(hours=3))

# Календарная дата от клиента (KPI-плитки: «сегодня» по часам сотрудника).
# Проверяем формат до подстановки в SQL — сравнение с due_date строковое.
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

invoices_bp = Blueprint("invoices", __name__, url_prefix="/api/invoices")

# Ручки счетов обслуживают ДВА раздела дашборда сразу: старый «Счета на оплату»
# (секция invoices) и новый «Согласование счетов» (секция invoices_v2).
# Проверять только `invoices` нельзя: как только сотрудника переведут на новый
# раздел и снимут старую секцию, у него отвалятся все данные — раздел откроется
# пустым (Фаза 10 плана plans/2026-08-24-счета-новый-раздел.md).
INVOICE_SECTIONS = ("invoices", "invoices_v2")


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

    invoices_bp.route(f"/{name}", methods=["GET"])(section_required(*INVOICE_SECTIONS)(list_view))
    invoices_bp.route(f"/{name}", methods=["POST"])(role_required("admin")(create_view))
    invoices_bp.route(f"/{name}/<int:item_id>", methods=["PUT"])(role_required("admin")(update_view))
    invoices_bp.route(f"/{name}/<int:item_id>", methods=["DELETE"])(role_required("admin")(delete_view))


@invoices_bp.route("/stores", methods=["GET"])
@section_required(*INVOICE_SECTIONS)
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
# РАБОЧИЕ КАРТЫ (план 2026-08-29, Фаза 0)
# =============================================================================
#
# Карта — подотчётный счёт в ПланФакте, привязанный к городу (а не к
# сотруднику): управляющие меняются, карта остаётся. Справочник правит только
# админ; управляющий видит карты своих салонов, чтобы выбрать её в заявке.


@invoices_bp.route("/work-cards", methods=["GET"])
@section_required(*INVOICE_SECTIONS)
def get_work_cards():
    """
    Карты, доступные текущему пользователю. Админу — все активные, остальным —
    те, чьи салоны пересекаются с его салонами. `?all=true` (только админ) —
    вместе с деактивированными, для экрана справочника.
    """
    if request.args.get("all", "").lower() == "true" and current_user.role == "admin":
        return jsonify({"work_cards": list_cards(active_only=False)})
    return jsonify({"work_cards": get_cards_for_user(current_user.username, current_user.role)})


def _card_payload_error(data, *, require_all: bool):
    """
    Общая проверка тела для создания и правки: два набора правил на одни данные
    неизбежно разъезжаются.

    accountId проверяем на сервере, а не только фильтром на вводе: нечисловое
    значение уже роняло прогон разноски внутри цикла по операциям и обрывало
    обработку всех остальных записей.
    """
    if require_all or "title" in data:
        if not (data.get("title") or "").strip():
            return "Название карты обязательно"

    for field, label in (("planfact_account_id", "Счёт карты в ПланФакте"),
                         ("source_planfact_account_id", "Счёт, с которого пополняется карта")):
        if require_all or field in data:
            if not is_valid_account_id(data.get(field)):
                return f"{label}: нужен числовой идентификатор счёта"

    if "store_ids" in data:
        if not isinstance(data["store_ids"], list):
            return "Салоны должны приходить списком"
        for store_id in data["store_ids"]:
            if not get_store_by_id(store_id):
                return f"Некорректный салон в списке: {store_id}"
    return None


@invoices_bp.route("/work-cards", methods=["POST"])
@role_required("admin")
def add_work_card():
    data = request.get_json(silent=True) or {}
    error = _card_payload_error(data, require_all=True)
    if error:
        return jsonify({"error": error}), 400

    card_id = create_card(
        title=data["title"].strip(),
        planfact_account_id=str(data["planfact_account_id"]).strip(),
        source_planfact_account_id=str(data["source_planfact_account_id"]).strip(),
        store_ids=data.get("store_ids") or [],
        planfact_account_title=(data.get("planfact_account_title") or "").strip() or None,
        source_planfact_account_title=(data.get("source_planfact_account_title") or "").strip() or None,
    )
    log_action(current_user.username, "create_work_card", f"{card_id}: {data['title'].strip()}")
    return jsonify({"ok": True, "work_card": get_card_by_id(card_id)}), 201


@invoices_bp.route("/work-cards/<int:card_id>", methods=["PUT"])
@role_required("admin")
def edit_work_card(card_id):
    if not get_card_by_id(card_id):
        return jsonify({"error": "Карта не найдена"}), 404

    data = request.get_json(silent=True) or {}
    error = _card_payload_error(data, require_all=False)
    if error:
        return jsonify({"error": error}), 400

    values = {}
    for field in ("title", "planfact_account_id", "planfact_account_title",
                  "source_planfact_account_id", "source_planfact_account_title"):
        if field in data:
            values[field] = (str(data[field] or "").strip() or None)
    if "is_active" in data:
        values["is_active"] = 1 if data["is_active"] else 0

    update_card(card_id, values, store_ids=data.get("store_ids"))
    log_action(current_user.username, "update_work_card", str(card_id))
    return jsonify({"ok": True, "work_card": get_card_by_id(card_id)})


@invoices_bp.route("/work-cards/<int:card_id>", methods=["DELETE"])
@role_required("admin")
def remove_work_card(card_id):
    """Мягкое удаление: заявки прошлых месяцев ссылаются на карту по id."""
    if not get_card_by_id(card_id):
        return jsonify({"error": "Карта не найдена"}), 404
    deactivate_card(card_id)
    log_action(current_user.username, "delete_work_card", str(card_id))
    return jsonify({"ok": True})


@invoices_bp.route("/planfact/accounts", methods=["GET"])
@role_required("admin")
def get_planfact_accounts():
    """
    Счета ПланФакта для выпадающих списков в справочнике карт.

    Живой вызов без кэша, поэтому — короткий таймаут и без ретраев (см.
    request() в planfact/client.py, инцидент 2026-08-17: справочные вызовы
    из UI заняли обоих воркеров и подвесили сайт). Ошибка ПланФакта здесь не
    фатальна: id счёта можно вписать руками.
    """
    from planfact.client import get_client

    try:
        accounts = get_client().get_accounts(active_only=True)
    except Exception:
        logger.exception("get_planfact_accounts упал")
        accounts = None

    if accounts is None:
        return jsonify({"accounts": [], "error": "ПланФакт не ответил — впишите id счёта вручную"})

    return jsonify({"accounts": [
        {
            "account_id": account.get("accountId"),
            # Названия счетов в ПФ грязные (двойные и хвостовые пробелы),
            # схлопываем для показа; id при этом остаётся ключом
            "title": " ".join((account.get("title") or "").split()),
            "company": (account.get("company") or {}).get("title"),
        }
        for account in accounts
    ]})


# =============================================================================
# СЧЕТА НА ОПЛАТУ
# =============================================================================

# Максимум значений в одном мультифильтре. Ограничение не про интерфейс
# (в справочниках столько не наберётся), а про запрос руками: без него
# `?store_id=` можно повторить тысячи раз и получить SQL с тысячей плейсхолдеров.
MAX_FILTER_VALUES = 200


def _arg_list(name: str) -> List[str]:
    """
    Значения query-параметра, который может повторяться: `?status=a&status=b`.

    Мультивыбор в фильтрах нового раздела шлёт по одному параметру на значение —
    так его штатно кодирует URLSearchParams, и так же читает Flask. Старый
    раздел шлёт один параметр и получает список из одного элемента, то есть
    прежний фильтр.
    """
    values = []
    for raw in request.args.getlist(name):
        value = (raw or "").strip()
        if value and value not in values:
            values.append(value)
    return values[:MAX_FILTER_VALUES]


def _arg_int_list(name: str) -> List[int]:
    """То же для числовых id справочников. Нечисловое значение молча отбрасываем."""
    result = []
    for value in _arg_list(name):
        try:
            result.append(int(value))
        except ValueError:
            continue
    return result


@invoices_bp.route("", methods=["GET"])
@section_required(*INVOICE_SECTIONS)
def get_invoices():
    """
    Список счетов с фильтрами.

    Query params: status, store_id, city_id, payer_id, expense_category_id,
    created_by, counterparty, payment_purpose, created_from, created_to,
    due_from, due_to, archived, hide_paid, sort, order, with_total, limit, offset

    Параметры expense_category_id, hide_paid, sort, order и with_total добавлены
    для раздела «Согласование счетов v2» и все необязательны: старый раздел их
    не передаёт и получает ровно прежний ответ.

    Справочные фильтры (status, store_id, city_id, payer_id,
    expense_category_id, created_by) можно повторять — `?city_id=1&city_id=2`
    означает «Москва ИЛИ Казань». Одно значение работает как раньше.
    """
    statuses = _arg_list("status")
    unknown = [s for s in statuses if s not in STATUSES]
    if unknown:
        return jsonify({"error": f"Неизвестный статус. Доступны: {list(STATUSES)}"}), 400

    sort = request.args.get("sort")
    if sort and sort not in INVOICE_SORT_FIELDS:
        return jsonify({"error": f"Сортировка недоступна. Доступны: {sorted(INVOICE_SORT_FIELDS)}"}), 400

    order = (request.args.get("order") or "desc").lower()
    if order not in ("asc", "desc"):
        return jsonify({"error": "order должен быть asc или desc"}), 400

    # Верхняя граница страницы. Без неё limit=100000 разом вытянул бы всю базу
    # вместе со строками распределения — на двух воркерах это заметно.
    limit = max(1, min(request.args.get("limit", 100, type=int) or 100, 200))
    offset = max(0, request.args.get("offset", 0, type=int) or 0)

    # Не-админ видит только счета своих салонов (или созданные им самим) —
    # см. user_can_access_invoice в storage.py
    restrict_username = None
    restrict_store_ids = None
    if current_user.role != "admin":
        restrict_username = current_user.username
        restrict_store_ids = get_user_stores(current_user.username)

    filters = {
        "status": statuses,
        "store_id": _arg_int_list("store_id"),
        "city_id": _arg_int_list("city_id"),
        "payer_id": _arg_int_list("payer_id"),
        "expense_category_id": _arg_int_list("expense_category_id"),
        "created_by": _arg_list("created_by"),
        "counterparty": request.args.get("counterparty"),
        "payment_purpose": request.args.get("payment_purpose"),
        "created_from": request.args.get("created_from"),
        "created_to": request.args.get("created_to"),
        "due_from": request.args.get("due_from"),
        "due_to": request.args.get("due_to"),
        "is_archived": request.args.get("archived", "false").lower() == "true",
        "hide_paid": request.args.get("hide_paid", "false").lower() == "true",
        # Фаза 7: 'only' — счета, которые сейчас у автора на уточнении,
        # 'exclude' — очередь согласующего без них
        "clarification": request.args.get("clarification"),
        # 'unsynced' — оплачен, но в ПланФакт не уехал
        "planfact": request.args.get("planfact"),
        "restrict_username": restrict_username,
        "restrict_store_ids": restrict_store_ids,
    }

    invoices = list_invoices(sort=sort, order=order, limit=limit, offset=offset, **filters)

    usernames = {inv.get("created_by") for inv in invoices}
    usernames.update(inv.get("approved_by") for inv in invoices)
    usernames.discard(None)
    full_names = get_users_full_names(list(usernames))
    for inv in invoices:
        inv["created_by_full_name"] = full_names.get(inv.get("created_by"))
        inv["approved_by_full_name"] = full_names.get(inv.get("approved_by"))
        # Право на правку считаем той же функцией, что потом и разрешит PUT.
        # Нужно платёжному борду (Фаза 9): перетаскивать можно только то, что
        # сервер согласится изменить, а своя копия правил на клиенте рано или
        # поздно разойдётся с этой. Старый раздел лишнее поле игнорирует.
        inv["can_edit_fields"] = can_edit_invoice_fields(
            inv, current_user.username, current_user.role
        )

    result = {"invoices": invoices, "count": len(invoices)}

    # Итог по всей выборке считаем только когда о нём попросили. Клиент просит
    # его на первой странице и переиспользует при «Показать ещё»: набор фильтров
    # там не меняется, а лишний COUNT(*) на каждую подгрузку — лишняя работа.
    if request.args.get("with_total", "false").lower() == "true":
        totals = count_invoices(**filters)
        result["total"] = totals["count"]
        result["total_amount"] = totals["amount"]

    return jsonify(result)


@invoices_bp.route("/summary", methods=["GET"])
@section_required(*INVOICE_SECTIONS)
def get_invoices_summary_view():
    """
    Четыре KPI-плитки раздела v2 одним запросом.

    Query params: today=YYYY-MM-DD — календарная дата по часам сотрудника.
    Сервер живёт в UTC, салоны в UTC+5/+7, поэтому «сегодня» определяет клиент;
    без параметра берём дату сервера.
    """
    today = (request.args.get("today") or "").strip()
    if not _DATE_RE.match(today):
        today = datetime.utcnow().strftime("%Y-%m-%d")

    restrict_username = None
    restrict_store_ids = None
    if current_user.role != "admin":
        restrict_username = current_user.username
        restrict_store_ids = get_user_stores(current_user.username)

    return jsonify(get_invoices_summary(
        today=today,
        restrict_username=restrict_username,
        restrict_store_ids=restrict_store_ids,
    ))


@invoices_bp.route("/authors", methods=["GET"])
@section_required(*INVOICE_SECTIONS)
def get_invoice_authors():
    """Авторы счетов для фильтра «Автор» — только те, чьи счета видны этому пользователю."""
    restrict_username = None
    restrict_store_ids = None
    if current_user.role != "admin":
        restrict_username = current_user.username
        restrict_store_ids = get_user_stores(current_user.username)

    return jsonify({"authors": list_invoice_authors(
        restrict_username=restrict_username,
        restrict_store_ids=restrict_store_ids,
    )})


@invoices_bp.route("/<int:invoice_id>", methods=["GET"])
@section_required(*INVOICE_SECTIONS)
def get_invoice(invoice_id):
    """
    Детали счёта — вместе со строками распределения и вложениями.

    Query params: include=history,comments — добавить ленту событий тем же
    запросом. Параметр необязателен: старый раздел его не передаёт и получает
    прежний ответ. Нужен очереди согласования, где счета листаются стрелками:
    без него каждый шаг — четыре запроса вместо одного.
    """
    invoice = get_invoice_by_id(invoice_id)
    if not invoice:
        return jsonify({"error": "Счёт не найден"}), 404
    if not user_can_access_invoice(invoice, current_user.username, current_user.role):
        return jsonify({"error": "Нет доступа к этому счёту"}), 403

    include = {part.strip() for part in (request.args.get("include") or "").split(",") if part.strip()}

    history = get_invoice_history(invoice_id) if "history" in include else []
    comments = get_invoice_comments(invoice_id) if "comments" in include else []

    # Имена собираем одним запросом на всех участников сразу — иначе на счёт
    # с длинной историей уходит по обращению к базе на каждую запись.
    usernames = {invoice.get("created_by"), invoice.get("approved_by"), invoice.get("rejected_by")}
    usernames.update(h["changed_by"] for h in history)
    usernames.update(c["author"] for c in comments)
    usernames.discard(None)
    full_names = get_users_full_names(list(usernames))

    invoice["created_by_full_name"] = full_names.get(invoice.get("created_by"))
    invoice["approved_by_full_name"] = full_names.get(invoice.get("approved_by"))
    invoice["rejected_by_full_name"] = full_names.get(invoice.get("rejected_by"))

    result = {
        "invoice": invoice,
        "line_items": get_invoice_line_items(invoice_id),
        "attachments": get_invoice_attachments(invoice_id),
        "can_edit_fields": can_edit_invoice_fields(invoice, current_user.username, current_user.role),
        "can_edit_status": can_edit_invoice_status(invoice, current_user.role),
        "can_delete": can_delete_invoice(invoice, current_user.role),
    }

    if "history" in include:
        for h in history:
            h["changed_by_full_name"] = full_names.get(h["changed_by"])
        result["history"] = history
    if "comments" in include:
        for c in comments:
            c["author_full_name"] = full_names.get(c["author"])
        result["comments"] = comments

    return jsonify(result)


def _validate_line_items(line_items):
    """
    Проверить строки распределения. Возвращает текст ошибки или None.

    Общая для создания и правки: два набора правил на одни данные обязательно
    разъедутся. Сумму строк со суммой счёта здесь не сверяем — при правке
    сумма счёта может меняться тем же запросом, поэтому сверка идёт там, где
    известно итоговое значение.
    """
    for item in line_items:
        if not isinstance(item, dict):
            return "Строка распределения должна быть объектом"
        if not get_store_by_id(item.get("store_id")):
            return "Некорректный проект (салон) в распределении"
        if not get_expense_category_by_id(item.get("expense_category_id")):
            return "Некорректная статья расхода в распределении"
        if not isinstance(item.get("amount"), (int, float)) or item["amount"] <= 0:
            return "Сумма строки распределения должна быть положительным числом"
    return None


@invoices_bp.route("/<int:invoice_id>", methods=["PUT"])
@section_required(*INVOICE_SECTIONS)
def edit_invoice(invoice_id):
    """
    Частично отредактировать поля счёта (не статус — см. /<id>/status).

    До согласования — автор счёта, менеджер или админ. После согласования —
    только админ. После отправки в банк/оплаты или в архиве — нельзя никому.
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

    if "comment" in data:
        changes["comment"] = (data["comment"] or "").strip() or None

    # Распределение можно править тем же запросом, что и поля. Без этого правка
    # «сумма + распределение» рвётся на два запроса, и при обрыве второго счёт
    # остаётся с новой суммой и старым распределением — см.
    # update_invoice_with_line_items.
    line_items = None
    if "line_items" in data:
        line_items = data["line_items"] or []
        if not isinstance(line_items, list):
            return jsonify({"error": "line_items должен быть списком"}), 400
        error = _validate_line_items(line_items)
        if error:
            return jsonify({"error": error}), 400
        if invoice["is_archived"]:
            return jsonify({"error": "Счёт в архиве — распределение изменить нельзя"}), 409

    if not changes and line_items is None:
        return jsonify({"error": "Нет полей для изменения"}), 400

    # Распределение обязательно и при правке, но только когда правка трогает
    # деньги: меняется сумма или сами строки. Иначе перенос срока оплаты
    # (борд шлёт один due_date) упирался бы в старый счёт без распределения,
    # а таких в базе хватает — задним числом их никто не дозаполнял (§3.3).
    if "amount" in changes or line_items is not None:
        final_amount = changes.get("amount", invoice["amount"])
        final_items = line_items if line_items is not None else get_invoice_line_items(invoice_id)
        if not final_items:
            return jsonify({
                "error": "Распределите счёт по салонам и статьям расхода — "
                         "без этого не видно, куда ушли деньги"
            }), 400
        allocated = round(sum(item["amount"] for item in final_items), 2)
        if abs(allocated - final_amount) >= 0.01:
            return jsonify({
                "error": f"Распределено {allocated} из {final_amount} — суммы строк должны "
                         f"в точности складываться в сумму счёта"
            }), 400

    result = update_invoice_with_line_items(invoice_id, changes, line_items, current_user.username)
    if not result["ok"]:
        return jsonify({"error": result["error"]}), 400

    log_action(current_user.username, "edit_invoice",
               f"{invoice['invoice_number']}: {list(changes.keys())}"
               + (" + распределение" if line_items is not None else ""))
    return jsonify({"ok": True, "invoice": result["invoice"]})


@invoices_bp.route("/<int:invoice_id>", methods=["DELETE"])
@role_required("admin")
def remove_invoice(invoice_id):
    """
    Удалить счёт насовсем — админ, до оплаты.

    Нужно для ошибочно заведённых и дублирующих счетов: до 2026-08-28 их
    можно было только отклонить или убрать в архив, и мусор оставался в
    выборках и суммах навсегда. Оплаченный счёт не удаляется ни в каком
    случае — для него есть архив (см. can_delete_invoice).

    След остаётся в общем аудите (log_action): собственная история счёта
    удаляется вместе с ним.
    """
    invoice = get_invoice_by_id(invoice_id)
    if not invoice:
        return jsonify({"error": "Счёт не найден"}), 404
    if not can_delete_invoice(invoice, current_user.role):
        return jsonify({
            "error": "Оплаченный счёт удалить нельзя — уберите его в архив"
        }), 409

    deleted = delete_invoice(invoice_id, current_user.username)
    if not deleted:
        return jsonify({"error": "Счёт уже удалён или оплачен"}), 409

    log_action(current_user.username, "delete_invoice",
               f"{deleted['invoice_number']} · {deleted.get('counterparty_name') or 'без контрагента'} · "
               f"{deleted['amount']} · статус {deleted['status']}")
    return jsonify({"ok": True})


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
@section_required(*INVOICE_SECTIONS)
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
    error = _validate_line_items(line_items)
    if error:
        return jsonify({"error": error}), 400

    # Распределение обязательно (решение №15 плана, включено владельцем
    # 2026-08-25). Проверка стоит на сервере, а не только в форме нового
    # раздела: иначе требование обходится любым другим путём. Отсюда же
    # следствие — старый раздел тоже начал требовать распределение и
    # показывает этот текст в тосте.
    #
    # Массовых операций это не касается: bulk-approve и bulk-send-to-bank
    # намеренно не проверяют распределение, иначе разбор накопившихся
    # счетов встал бы (§3.3 плана).
    if not line_items:
        return jsonify({
            "error": "Распределите счёт по салонам и статьям расхода — "
                     "без этого не видно, куда ушли деньги"
        }), 400

    total = round(sum(item["amount"] for item in line_items), 2)
    if abs(total - amount) >= 0.01:
        return jsonify({
            "error": f"Распределено {total} из {amount} — суммы строк должны "
                     f"в точности складываться в сумму счёта"
        }), 400

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
            comment=(data.get("comment") or "").strip() or None,
            line_items=line_items,
        )
    except sqlite3.IntegrityError:
        logger.exception("create_invoice упал с IntegrityError")
        return jsonify({"error": "Не удалось создать счёт, попробуйте ещё раз"}), 409

    # Запоминаем контрагента уже после того, как счёт создан и его соединение
    # закрыто: справочник — приятное дополнение, ронять из-за него создание
    # счёта нельзя (внутри всё обёрнуто в try).
    remember_counterparty_from_invoice(invoice)

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


@invoices_bp.route("/<int:invoice_id>/clarify", methods=["POST"])
@role_required("admin")
def clarify(invoice_id):
    """
    Отправить счёт автору на уточнение. Body: {"reason": "..."} — обязательно.

    Отличается от отклонения: отклонение — окончательный отказ, а уточнение
    оставляет счёт в работе. Со счёта «На согласовании» статус не меняется,
    поэтому старый раздел показывает счёт корректно; согласованный счёт
    возвращается на согласование (см. send_invoice_to_clarification).

    Причина обязательна и уходит в обсуждение счёта: «верните, тут что-то не
    так» без объяснения означает второй круг тех же вопросов.
    """
    invoice = get_invoice_by_id(invoice_id)
    if not invoice:
        return jsonify({"error": "Счёт не найден"}), 404

    data = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "").strip()
    if not reason:
        return jsonify({"error": "Напишите, что нужно уточнить — автору иначе непонятно"}), 400

    if not send_invoice_to_clarification(invoice_id, current_user.username, reason):
        return jsonify({
            "error": "На уточнение можно вернуть счёт на согласовании или согласованный, "
                     "и только если он ещё не у автора"
        }), 409

    add_invoice_comment(invoice_id, current_user.username, f"На уточнение: {reason}")
    log_action(current_user.username, "clarify_invoice", f"{invoice['invoice_number']}: {reason}")
    return jsonify({"ok": True, "invoice": get_invoice_by_id(invoice_id)})


@invoices_bp.route("/<int:invoice_id>/resubmit", methods=["POST"])
@section_required(*INVOICE_SECTIONS)
def resubmit(invoice_id):
    """
    Автор поправил счёт и возвращает его в очередь. Body: {"comment": "..."} —
    необязательно, но полезно: согласующему видно, что именно исправлено.
    """
    invoice = get_invoice_by_id(invoice_id)
    if not invoice:
        return jsonify({"error": "Счёт не найден"}), 404
    if not user_can_access_invoice(invoice, current_user.username, current_user.role):
        return jsonify({"error": "Нет доступа к этому счёту"}), 403
    if current_user.role != "admin" and invoice["created_by"] != current_user.username:
        return jsonify({"error": "Вернуть счёт в очередь может автор или администратор"}), 403

    data = request.get_json(silent=True) or {}
    comment = (data.get("comment") or "").strip()

    if not resubmit_invoice(invoice_id, current_user.username, comment or None):
        return jsonify({"error": "Счёт не на уточнении"}), 409

    if comment:
        add_invoice_comment(invoice_id, current_user.username, f"Отправлен снова: {comment}")
    log_action(current_user.username, "resubmit_invoice", invoice["invoice_number"])
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
        # Дата платёжки — операционный день банка, то есть Москва, а не пояс
        # сервера: на Amvera процесс живёт в UTC и с 00:00 до 03:00 по Москве
        # документ уходил бы вчерашним числом.
        date=datetime.now(_MOSCOW_TZ).strftime("%Y-%m-%d"),
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


# =============================================================================
# МАССОВЫЕ ДЕЙСТВИЯ (план 2026-08-24, Фаза 6)
# =============================================================================

# Сколько счетов принимаем за раз. Для банка предел жёстче: каждый счёт —
# отдельное обращение к внешнему API, и пакет ограничен не числом, а временем.
BULK_MAX_IDS = 100
BULK_BANK_MAX_IDS = 25

# Бюджет времени на пакетную отправку платёжек. Клиент Модульбанка ходит с
# timeout=30 (modulbank/client.py), а gunicorn убивает запрос на 120 с
# (amvera.yml). Значит новую отправку нельзя начинать, если до конца бюджета
# осталось меньше одного полного таймаута: воркер умрёт после того, как часть
# платёжек уже ушла, и ответа пользователь не получит вовсе.
BANK_BATCH_BUDGET_SECONDS = 90
BANK_CALL_TIMEOUT_SECONDS = 30


def _read_bulk_ids(data, limit=BULK_MAX_IDS):
    """Разобрать и проверить {ids: [...]}. Возвращает (ids, error)."""
    ids = data.get("ids")
    if not isinstance(ids, list) or not ids:
        return None, "Не переданы счета"
    if not all(isinstance(i, int) for i in ids):
        return None, "Список счетов должен состоять из чисел"
    # Дубли схлопываем, порядок сохраняем: иначе один и тот же счёт
    # обрабатывается дважды и попадает в отчёт двумя строками
    unique = list(dict.fromkeys(ids))
    if len(unique) > limit:
        return None, f"За раз можно обработать не больше {limit} счетов"
    return unique, None


def _bulk_load(invoice_id):
    """Счёт для массовой операции: (invoice, reason_to_skip)."""
    invoice = get_invoice_by_id(invoice_id)
    if not invoice:
        return None, "счёт не найден"
    if not user_can_access_invoice(invoice, current_user.username, current_user.role):
        return None, "нет доступа"
    return invoice, None


def _bulk_label(invoice, invoice_id):
    return (invoice or {}).get("invoice_number") or f"#{invoice_id}"


@invoices_bp.route("/bulk-approve", methods=["POST"])
@role_required("admin")
def bulk_approve():
    """
    Согласовать пачку счетов. Body: {"ids": [int, ...]}.

    Каждый счёт обрабатывается своей транзакцией: ошибка на одном не должна
    откатывать остальные — иначе разбор накопившихся счетов встанет из-за
    одного проблемного.

    Идемпотентно: уже согласованный счёт попадает в «пропущено» с причиной,
    а не в ошибку. Распределение здесь НЕ проверяется намеренно (§3.3):
    старые нераспределённые счета должны разбираться пачкой.
    """
    data = request.get_json(silent=True) or {}
    ids, error = _read_bulk_ids(data)
    if error:
        return jsonify({"error": error}), 400

    done, skipped, total = [], [], 0.0
    for invoice_id in ids:
        invoice, reason = _bulk_load(invoice_id)
        if reason:
            skipped.append({"id": invoice_id, "label": f"#{invoice_id}", "reason": reason})
            continue
        if invoice["status"] != "on_approval":
            skipped.append({"id": invoice_id, "label": _bulk_label(invoice, invoice_id),
                            "reason": f"статус «{invoice['status']}», согласовать нельзя"})
            continue
        try:
            if approve_invoice(invoice_id, current_user.username):
                done.append({"id": invoice_id, "label": _bulk_label(invoice, invoice_id),
                             "amount": invoice["amount"]})
                total += invoice["amount"]
            else:
                skipped.append({"id": invoice_id, "label": _bulk_label(invoice, invoice_id),
                                "reason": "статус изменился, пока шла операция"})
        except Exception:
            logger.exception("bulk_approve: счёт %s не согласован", invoice_id)
            skipped.append({"id": invoice_id, "label": _bulk_label(invoice, invoice_id),
                            "reason": "внутренняя ошибка"})

    if done:
        log_action(current_user.username, "bulk_approve_invoices",
                   f"{len(done)} шт. на {total}")
    return jsonify({"ok": True, "approved": done, "skipped": skipped, "approved_amount": total})


@invoices_bp.route("/bulk-mark-paid", methods=["POST"])
@role_required("admin")
def bulk_mark_paid():
    """
    Отметить пачку счетов оплаченными. Body: {"ids": [int, ...]}.

    Доступно для «Согласован» и «Загружен в банк»: часть счетов оплачивается
    мимо автозагрузки платёжки.

    Оплаченный счёт остаётся в списке: автоархивация убрана 2026-08-25 —
    в архив счета кладёт человек (см. /bulk-archive).
    """
    data = request.get_json(silent=True) or {}
    ids, error = _read_bulk_ids(data)
    if error:
        return jsonify({"error": error}), 400

    done, skipped, total = [], [], 0.0
    for invoice_id in ids:
        invoice, reason = _bulk_load(invoice_id)
        if reason:
            skipped.append({"id": invoice_id, "label": f"#{invoice_id}", "reason": reason})
            continue
        if invoice["status"] not in ("approved", "sent_to_bank"):
            skipped.append({"id": invoice_id, "label": _bulk_label(invoice, invoice_id),
                            "reason": f"статус «{invoice['status']}», отметить оплаченным нельзя"})
            continue
        try:
            if mark_invoice_paid(invoice_id, current_user.username):
                done.append({"id": invoice_id, "label": _bulk_label(invoice, invoice_id),
                             "amount": invoice["amount"]})
                total += invoice["amount"]
            else:
                skipped.append({"id": invoice_id, "label": _bulk_label(invoice, invoice_id),
                                "reason": "статус изменился, пока шла операция"})
        except Exception:
            logger.exception("bulk_mark_paid: счёт %s не отмечен", invoice_id)
            skipped.append({"id": invoice_id, "label": _bulk_label(invoice, invoice_id),
                            "reason": "внутренняя ошибка"})

    if done:
        log_action(current_user.username, "bulk_mark_invoices_paid", f"{len(done)} шт. на {total}")
    return jsonify({"ok": True, "paid": done, "skipped": skipped, "paid_amount": total})


@invoices_bp.route("/admin/auto-archived", methods=["GET"])
@role_required("admin")
def list_auto_archived():
    """
    Сколько оплаченных счетов лежит в архиве не по решению человека.

    Отмена автоархивации подействовала только на будущее — уже уехавшие счета
    остались в архиве и в списке не видны. Сначала показываем, что найдено,
    и только отдельным запросом возвращаем: разовые операции с боевой базой на
    этом тарифе Amvera делаются HTTP-ручками, консоли контейнера нет.
    """
    found = find_auto_archived_invoices()
    return jsonify({
        "count": len(found),
        "amount": round(sum(float(i["amount"] or 0) for i in found), 2),
        "invoices": found,
    })


@invoices_bp.route("/admin/auto-archived/restore", methods=["POST"])
@role_required("admin")
def restore_auto_archived():
    """Вернуть из архива счета, которые туда положила автоархивация."""
    found = find_auto_archived_invoices()
    restored, failed = [], []
    for invoice in found:
        try:
            if set_invoice_archived(invoice["id"], False, current_user.username):
                restored.append({"id": invoice["id"],
                                 "label": invoice["invoice_number"] or f"#{invoice['id']}",
                                 "amount": invoice["amount"]})
            else:
                failed.append({"id": invoice["id"], "label": invoice["invoice_number"] or f"#{invoice['id']}",
                               "reason": "счёт не найден"})
        except Exception:
            logger.exception("restore_auto_archived: счёт %s не возвращён", invoice["id"])
            failed.append({"id": invoice["id"], "label": invoice["invoice_number"] or f"#{invoice['id']}",
                           "reason": "внутренняя ошибка"})

    if restored:
        log_action(current_user.username, "restore_auto_archived_invoices", f"{len(restored)} шт.")
    return jsonify({"ok": True, "restored": restored, "failed": failed,
                    "restored_amount": round(sum(float(i["amount"] or 0) for i in restored), 2)})


@invoices_bp.route("/bulk-unarchive", methods=["POST"])
@role_required("admin")
def bulk_unarchive():
    """Вернуть пачку счетов из архива. Body: {"ids": [int, ...]}."""
    data = request.get_json(silent=True) or {}
    ids, error = _read_bulk_ids(data)
    if error:
        return jsonify({"error": error}), 400

    done, skipped, total = [], [], 0.0
    for invoice_id in ids:
        invoice, reason = _bulk_load(invoice_id)
        if reason:
            skipped.append({"id": invoice_id, "label": f"#{invoice_id}", "reason": reason})
            continue
        if not invoice["is_archived"]:
            skipped.append({"id": invoice_id, "label": _bulk_label(invoice, invoice_id),
                            "reason": "счёт не в архиве"})
            continue
        try:
            if set_invoice_archived(invoice_id, False, current_user.username):
                done.append({"id": invoice_id, "label": _bulk_label(invoice, invoice_id),
                             "amount": invoice["amount"]})
                total += invoice["amount"]
            else:
                skipped.append({"id": invoice_id, "label": _bulk_label(invoice, invoice_id),
                                "reason": "счёт изменился, пока шла операция"})
        except Exception:
            logger.exception("bulk_unarchive: счёт %s не возвращён", invoice_id)
            skipped.append({"id": invoice_id, "label": _bulk_label(invoice, invoice_id),
                            "reason": "внутренняя ошибка"})

    if done:
        log_action(current_user.username, "bulk_unarchive_invoices", f"{len(done)} шт. на {total}")
    return jsonify({"ok": True, "unarchived": done, "skipped": skipped, "unarchived_amount": total})


@invoices_bp.route("/bulk-archive", methods=["POST"])
@role_required("admin")
def bulk_archive():
    """
    Убрать пачку счетов в архив. Body: {"ids": [int, ...]}.

    Появилась вместе с отменой автоархивации (2026-08-25): раз оплаченный счёт
    больше не уходит в архив сам, убирать отработанные счета надо уметь пачкой,
    иначе список растёт бесконечно.

    Статус значения не имеет: админ убирает в архив любой счёт (то же, что
    разрешает кнопка в карточке).
    """
    data = request.get_json(silent=True) or {}
    ids, error = _read_bulk_ids(data)
    if error:
        return jsonify({"error": error}), 400

    done, skipped, total = [], [], 0.0
    for invoice_id in ids:
        invoice, reason = _bulk_load(invoice_id)
        if reason:
            skipped.append({"id": invoice_id, "label": f"#{invoice_id}", "reason": reason})
            continue
        if invoice["is_archived"]:
            skipped.append({"id": invoice_id, "label": _bulk_label(invoice, invoice_id),
                            "reason": "уже в архиве"})
            continue
        try:
            if set_invoice_archived(invoice_id, True, current_user.username):
                done.append({"id": invoice_id, "label": _bulk_label(invoice, invoice_id),
                             "amount": invoice["amount"]})
                total += invoice["amount"]
            else:
                skipped.append({"id": invoice_id, "label": _bulk_label(invoice, invoice_id),
                                "reason": "счёт изменился, пока шла операция"})
        except Exception:
            logger.exception("bulk_archive: счёт %s не убран в архив", invoice_id)
            skipped.append({"id": invoice_id, "label": _bulk_label(invoice, invoice_id),
                            "reason": "внутренняя ошибка"})

    if done:
        log_action(current_user.username, "bulk_archive_invoices", f"{len(done)} шт. на {total}")
    return jsonify({"ok": True, "archived": done, "skipped": skipped, "archived_amount": total})


@invoices_bp.route("/bulk-send-to-bank", methods=["POST"])
@role_required("admin")
def bulk_send_to_bank():
    """
    Пакетная загрузка платёжек в Модульбанк. Body: {"ids": [...], "sandbox": bool}.

    Самая аккуратная ручка раздела: каждый счёт — отдельное обращение к
    внешнему API. Поэтому здесь:

    * счета с неполными реквизитами отсеиваются ДО обращения к банку и
      называются поимённо — банк развернул бы такую платёжку уже после отправки;
    * если отправлять нечего вовсе, пакет отклоняется целиком (400), а не
      возвращает пустой успех;
    * каждый счёт обрабатывается отдельно, результат фиксируется сразу —
      ошибка на одном не отменяет уже отправленные;
    * действует бюджет времени, см. BANK_BATCH_BUDGET_SECONDS.

    Сама по себе отправка деньги не двигает: банк создаёт черновик,
    подписание — вручную в личном кабинете (см. _send_invoice_to_bank).
    """
    data = request.get_json(silent=True) or {}
    ids, error = _read_bulk_ids(data, limit=BULK_BANK_MAX_IDS)
    if error:
        return jsonify({"error": error}), 400

    sandbox = bool(data.get("sandbox"))

    # Шаг 1: отбор. Обращаться к банку начинаем, только когда понятно, что
    # отправлять есть что.
    ready, skipped = [], []
    for invoice_id in ids:
        invoice, reason = _bulk_load(invoice_id)
        if reason:
            skipped.append({"id": invoice_id, "label": f"#{invoice_id}", "reason": reason})
            continue
        label = _bulk_label(invoice, invoice_id)
        if not sandbox and invoice["status"] != "approved":
            skipped.append({"id": invoice_id, "label": label,
                            "reason": f"статус «{invoice['status']}», в банк не отправляется"})
            continue
        missing = [f for f in _INVOICE_REQUIRED_BANK_FIELDS if not invoice.get(f)]
        if missing:
            skipped.append({"id": invoice_id, "label": label,
                            "reason": "не заполнены реквизиты контрагента"})
            continue
        if not payer_has_bank_requisites(invoice["payer_id"]):
            payer = get_payer_by_id(invoice["payer_id"])
            skipped.append({"id": invoice_id, "label": label,
                            "reason": f"у плательщика «{(payer or {}).get('name', '')}» нет реквизитов банка"})
            continue
        ready.append(invoice)

    if not ready:
        return jsonify({"error": "Ни один счёт не готов к отправке в банк", "skipped": skipped}), 400

    # Шаг 2: отправка по одному, с фиксацией результата сразу и с бюджетом
    # времени — недоделанный пакет лучше убитого воркера.
    started = time.monotonic()
    sent, failed, postponed = [], [], []
    for invoice in ready:
        elapsed = time.monotonic() - started
        if elapsed > BANK_BATCH_BUDGET_SECONDS - BANK_CALL_TIMEOUT_SECONDS:
            postponed.append({"id": invoice["id"], "label": _bulk_label(invoice, invoice["id"]),
                              "reason": "не успели за отведённое время, повторите для остальных"})
            continue
        try:
            outcome = _send_invoice_to_bank(invoice, sandbox, current_user.username)
        except Exception:
            logger.exception("bulk_send_to_bank: счёт %s упал", invoice["id"])
            failed.append({"id": invoice["id"], "label": _bulk_label(invoice, invoice["id"]),
                           "reason": "внутренняя ошибка"})
            continue

        if outcome["ok"]:
            sent.append({"id": invoice["id"], "label": _bulk_label(invoice, invoice["id"]),
                         "amount": invoice["amount"]})
        else:
            failed.append({"id": invoice["id"], "label": _bulk_label(invoice, invoice["id"]),
                           "reason": outcome["body"].get("error") or "банк отклонил платёжку"})

    log_action(current_user.username,
               "bulk_send_to_bank_sandbox" if sandbox else "bulk_send_to_bank",
               f"отправлено {len(sent)}, ошибок {len(failed)}, отложено {len(postponed)}")

    return jsonify({
        "ok": True,
        "sandbox": sandbox,
        "sent": sent,
        "sent_amount": sum(item["amount"] for item in sent),
        "failed": failed,
        "skipped": skipped,
        "postponed": postponed,
    })


@invoices_bp.route("/<int:invoice_id>/archive", methods=["POST"])
@role_required("admin")
def archive_invoice_view(invoice_id):
    """
    Вручную перенести счёт в архив — в любом статусе.

    Ограничение «только оплаченные и отклонённые» снято 2026-08-28 (решение
    владельца): в работе зависают и черновики, и счета, по которым
    договорились иначе, — убрать их было нечем, и список рос. Архив ничего
    не портит: счёт возвращается оттуда кнопкой.
    """
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
@section_required(*INVOICE_SECTIONS)
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

    # Та же обязательность, что в создании и правке счёта: через эту ручку
    # распределение иначе можно было бы просто обнулить, и требование
    # обходилось бы в один запрос.
    if not items:
        return jsonify({
            "error": "Распределите счёт по салонам и статьям расхода — "
                     "без этого не видно, куда ушли деньги"
        }), 400
    allocated = round(sum(item["amount"] for item in items), 2)
    if abs(allocated - invoice["amount"]) >= 0.01:
        return jsonify({
            "error": f"Распределено {allocated} из {invoice['amount']} — суммы строк должны "
                     f"в точности складываться в сумму счёта"
        }), 400

    result = set_invoice_line_items(invoice_id, items, current_user.username)
    if not result["ok"]:
        return jsonify({"error": result["error"]}), 400

    log_action(current_user.username, "update_invoice_line_items", str(invoice_id))
    return jsonify({"ok": True, "line_items": get_invoice_line_items(invoice_id)})


# =============================================================================
# ВЛОЖЕНИЯ
# =============================================================================

@invoices_bp.route("/<int:invoice_id>/attachments", methods=["GET"])
@section_required(*INVOICE_SECTIONS)
def list_attachments(invoice_id):
    invoice = get_invoice_by_id(invoice_id)
    if not invoice:
        return jsonify({"error": "Счёт не найден"}), 404
    if not user_can_access_invoice(invoice, current_user.username, current_user.role):
        return jsonify({"error": "Нет доступа к этому счёту"}), 403
    return jsonify({"attachments": get_invoice_attachments(invoice_id)})


@invoices_bp.route("/<int:invoice_id>/attachments", methods=["POST"])
@section_required(*INVOICE_SECTIONS)
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
@section_required(*INVOICE_SECTIONS)
def download_attachment(attachment_id):
    attachment = get_attachment_by_id(attachment_id)
    if not attachment:
        return jsonify({"error": "Вложение не найдено"}), 404
    invoice = get_invoice_by_id(attachment["invoice_id"])
    if not invoice or not user_can_access_invoice(invoice, current_user.username, current_user.role):
        return jsonify({"error": "Нет доступа к этому вложению"}), 403

    directory = os.path.abspath(ATTACHMENTS_DIR)
    # Файл может отсутствовать, если его залили, когда вложения складывались
    # мимо постоянного диска (см. ATTACHMENT_DIRS в app.py): запись в БД есть,
    # файла нет. Без явной проверки send_from_directory поднимает NotFound, и
    # общий обработчик 404 отвечает "Endpoint not found" — по такому ответу
    # кажется, что сломан маршрут, а не потерян файл.
    if not os.path.exists(os.path.join(directory, attachment["stored_filename"])):
        logger.error(
            "Вложение %s (%s) есть в БД, но файла нет в %s",
            attachment_id, attachment["original_filename"], directory
        )
        return jsonify({
            "error": "Файл вложения не найден на диске — загрузите его заново"
        }), 404

    return send_from_directory(
        directory,
        attachment["stored_filename"],
        download_name=attachment["original_filename"],
    )


# Что и с каким Content-Type отдаём на просмотр прямо в браузере.
# Тип берём по расширению из этого словаря, а не из запроса и не из заголовка
# загрузки: иначе загруженный файл превращается в XSS на домене дашборда,
# то есть в сессии админа. Всё, чего здесь нет, на просмотр не отдаём вовсе.
_INLINE_CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
}


@invoices_bp.route("/attachments/<int:attachment_id>/inline", methods=["GET"])
@section_required(*INVOICE_SECTIONS)
def preview_attachment(attachment_id):
    """
    Показать вложение прямо в интерфейсе, без скачивания на диск.

    Отдельная ручка, а не переключение `download`: та безопасна именно потому,
    что отдаёт файл вложением. Здесь же браузер файл исполняет, поэтому:
    белый список типов, `nosniff` (иначе браузер угадает тип сам и картинка
    с HTML внутри станет страницей) и CSP sandbox (изолирует содержимое от
    домена дашборда).
    """
    attachment = get_attachment_by_id(attachment_id)
    if not attachment:
        return jsonify({"error": "Вложение не найдено"}), 404
    invoice = get_invoice_by_id(attachment["invoice_id"])
    if not invoice or not user_can_access_invoice(invoice, current_user.username, current_user.role):
        return jsonify({"error": "Нет доступа к этому вложению"}), 403

    extension = os.path.splitext(attachment["original_filename"])[1].lower()
    content_type = _INLINE_CONTENT_TYPES.get(extension)
    if not content_type:
        return jsonify({"error": "Этот тип файла показывается только скачиванием"}), 415

    directory = os.path.abspath(ATTACHMENTS_DIR)
    if not os.path.exists(os.path.join(directory, attachment["stored_filename"])):
        logger.error(
            "Вложение %s (%s) есть в БД, но файла нет в %s",
            attachment_id, attachment["original_filename"], directory
        )
        return jsonify({"error": "Файл вложения не найден на диске — загрузите его заново"}), 404

    response = send_from_directory(directory, attachment["stored_filename"], mimetype=content_type)
    response.headers["Content-Type"] = content_type
    response.headers["X-Content-Type-Options"] = "nosniff"

    # Главную защиту даёт связка «белый список расширений + nosniff»: HTML и SVG
    # сюда не попадают вовсе, а браузеру запрещено угадывать тип самому.
    # CSP — второй рубеж: `sandbox` без `allow-same-origin` оставляет файл в
    # уникальном (opaque) источнике, откуда до сессии и DOM дашборда не
    # дотянуться.
    #
    # PDF — исключение, и это дорого далось. Встроенный просмотрщик PDF в
    # Chromium (а значит, и в Яндекс.Браузере) — это расширение браузера, и в
    # песочнице с opaque-источником загрузить его нельзя: запрос к ресурсу
    # расширения отбивается с ERR_BLOCKED_BY_CLIENT. Наружу это выглядит как
    # «страницу заблокировал браузер», из-за чего 26.08.2026 диагноз сначала
    # ушёл в блокировщик рекламы. С `allow-scripts` часть браузеров
    # просмотрщик всё же поднимала — отсюда и прошлая правка, вылечившая
    # белый лист, но не этот отказ. Поэтому PDF отдаём без `sandbox`:
    # исполнить его как HTML всё равно нельзя (белый список + nosniff),
    # а до DOM дашборда просмотрщик PDF не дотягивается по устройству.
    #
    # `default-src 'none'` картинке НЕ ставим, хотя подресурсов у неё и нет.
    # Открытую в отдельной вкладке картинку браузер показывает синтетическим
    # документом, который грузит её же как подресурс, — и такая политика гасит
    # показ. Пока это заметно не было только потому, что заголовок затирался
    # глобальным хуком в pyrus/server.py (см. _apply_frame_ancestors).
    if content_type != "application/pdf":
        response.headers["Content-Security-Policy"] = "sandbox"
    # Имя файла в заголовок не собираем: он тут только сообщает браузеру,
    # что это просмотр, а не загрузка.
    response.headers["Content-Disposition"] = "inline"
    return response


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
@section_required(*INVOICE_SECTIONS)
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
@section_required(*INVOICE_SECTIONS)
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
@section_required(*INVOICE_SECTIONS)
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

    # Признак «уже разнесён» — отдельное поле, а НЕ статус. Статус paid
    # ставится и вручную (кнопка, массовое действие), и раньше синк принимал
    # его за «уже разнесено» и молча пропускал такие счета навсегда — операция
    # в ПланФакте оставалась нераспределённой, и в «Требует внимания» она тоже
    # не попадала, потому что этот выход стоит до записи туда.
    if invoice.get("planfact_synced_at"):
        return {"status": "skip"}

    # Оплаченный, но не разнесённый счёт — нормальный кандидат на разноску
    if invoice["status"] not in ("approved", "sent_to_bank", "paid"):
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
        # id сопоставления вводят руками, когда ПланФакт не отвечает и
        # выпадающего списка нет. Нечисловое значение раньше роняло int()
        # прямо внутри цикла по операциям — обрывался весь прогон, и
        # остальные счета не разносились из-за одной опечатки в настройке.
        try:
            category_id_int = int(str(pf_category_id).strip())
            project_id_int = int(str(project_id).strip())
        except (TypeError, ValueError):
            return {
                "status": "unmatched", "operation_id": operation_id, "match_code": match_code,
                "invoice_id": invoice["id"],
                "reason": (
                    f"В сопоставлении с ПланФакт нечисловой id: проект «{project_id}», "
                    f"статья «{pf_category_id}» — поправьте на вкладке «Сопоставление»"
                ),
            }

        pf_items.append({
            "calculationDate": op.get("operationDate"),
            "isCalculationCommitted": bool(op.get("isCommitted", True)),
            "contrAgentId": (op.get("contrAgent") or {}).get("contrAgentId"),
            "operationCategoryId": category_id_int,
            "projectId": project_id_int,
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

    # Сначала признак разноски, потом статус: если процесс упадёт между этими
    # шагами, лучше «разнесён, но не отмечен оплаченным» (человек увидит и
    # поправит), чем «оплачен, но не отмечен разнесённым» — второе синк
    # попробует разнести ещё раз и создаст дубль в ПланФакте.
    mark_invoice_planfact_synced(invoice["id"], operation_id)
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
            # Одна операция не должна уносить весь прогон: раньше любое
            # неожиданное исключение обрывало цикл, и всё, что стояло в
            # очереди после неё, оставалось неразнесённым без объяснений.
            try:
                result = _match_planfact_operation(op, client, store_map, category_map, dry_run)
            except Exception as error:
                logger.exception("Разноска операции %s упала", op.get("operationId"))
                result = {
                    "status": "unmatched",
                    "operation_id": str(op.get("operationId") or op.get("id") or ""),
                    "match_code": None,
                    "reason": f"Внутренняя ошибка при разноске: {type(error).__name__}",
                }
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


@invoices_bp.route("/counterparties/data-report", methods=["GET"])
@role_required("admin")
def counterparties_data_report():
    """
    Отчёт по качеству реквизитов контрагентов в истории счетов —
    Фаза 2 плана 2026-08-24, смотрим ДО наполнения справочника.

    Только чтение. Эндпоинт, а не скрипт, потому что на этом тарифе Amvera
    нет доступа к консоли контейнера — разовые операции с боевой базой
    делаются HTTP-ручками.
    """
    return jsonify(counterparty_data_report())


# =============================================================================
# СПРАВОЧНИК КОНТРАГЕНТОВ (план 2026-08-24, Фаза 3)
# =============================================================================

@invoices_bp.route("/counterparties", methods=["GET"])
@section_required(*INVOICE_SECTIONS)
def get_counterparties():
    """
    Справочник контрагентов. С ?query= — подсказки для формы счёта
    (до 10 записей), без него — весь список для страницы справочника.
    """
    query = request.args.get("query")
    if query is not None:
        return jsonify({"counterparties": search_counterparties(query)})
    include_inactive = request.args.get("include_inactive", "false").lower() == "true"
    return jsonify({"counterparties": list_counterparties(include_inactive=include_inactive)})


@invoices_bp.route("/counterparties", methods=["POST"])
@role_required("admin")
def add_counterparty():
    """Завести контрагента вручную (обычно он заводится сам при создании счёта)."""
    data = request.get_json(silent=True) or {}
    try:
        counterparty = create_counterparty(data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except sqlite3.IntegrityError:
        return jsonify({"error": "Контрагент с таким ИНН и расчётным счётом уже есть"}), 409

    log_action(current_user.username, "create_counterparty", counterparty["name"])
    return jsonify({"ok": True, "counterparty": counterparty}), 201


@invoices_bp.route("/counterparties/<int:counterparty_id>", methods=["PUT"])
@role_required("admin")
def edit_counterparty(counterparty_id):
    """
    Поправить реквизиты в справочнике. Уже выставленные счета не меняются —
    там своя копия реквизитов на момент платежа.
    """
    if not get_counterparty_by_id(counterparty_id):
        return jsonify({"error": "Контрагент не найден"}), 404

    data = request.get_json(silent=True) or {}
    try:
        counterparty = update_counterparty(counterparty_id, data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except sqlite3.IntegrityError:
        return jsonify({"error": "Контрагент с таким ИНН и расчётным счётом уже есть"}), 409

    log_action(current_user.username, "update_counterparty", f"{counterparty_id}: {counterparty['name']}")
    return jsonify({"ok": True, "counterparty": counterparty})


@invoices_bp.route("/counterparties/<int:counterparty_id>", methods=["DELETE"])
@role_required("admin")
def remove_counterparty(counterparty_id):
    """Мягкое удаление: пропадает из подсказок, счета и история целы."""
    counterparty = get_counterparty_by_id(counterparty_id)
    if not counterparty:
        return jsonify({"error": "Контрагент не найден"}), 404

    delete_counterparty(counterparty_id)
    log_action(current_user.username, "delete_counterparty", f"{counterparty_id}: {counterparty['name']}")
    return jsonify({"ok": True})


# =============================================================================
# ШАБЛОНЫ СЧЕТОВ (план 2026-08-24, Фаза 8)
# =============================================================================

@invoices_bp.route("/templates", methods=["GET"])
@section_required(*INVOICE_SECTIONS)
def get_templates():
    """
    Список шаблонов. Читать может любой, у кого есть раздел, — счёт из
    шаблона заводит тот же человек, что и обычный. Править — только админ.

    К каждому шаблону сразу считаем расхождение реквизитов со справочником:
    предупредить надо в момент выбора шаблона, а не после отказа банка.
    """
    templates = list_invoice_templates()
    for template in templates:
        template["requisite_mismatch"] = template_requisite_mismatch(template)
    return jsonify({"templates": templates})


@invoices_bp.route("/templates", methods=["POST"])
@role_required("admin")
def add_template():
    """Завести шаблон. Body: поля шаблона + line_items."""
    data = request.get_json(silent=True) or {}
    items = data.get("line_items")
    error = _validate_template_items(items)
    if error:
        return jsonify({"error": error}), 400

    try:
        template = create_invoice_template(data, items, current_user.username)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except sqlite3.Error:
        logger.exception("Не удалось создать шаблон счёта")
        return jsonify({"error": "Не удалось сохранить шаблон"}), 500

    log_action(current_user.username, "create_invoice_template", template["name"])
    return jsonify({"ok": True, "template": template}), 201


@invoices_bp.route("/templates/<int:template_id>", methods=["PUT"])
@role_required("admin")
def edit_template(template_id):
    """Поправить шаблон. Уже заведённые по нему счета не меняются."""
    data = request.get_json(silent=True) or {}
    items = data.get("line_items")
    error = _validate_template_items(items)
    if error:
        return jsonify({"error": error}), 400

    try:
        template = update_invoice_template(template_id, data, items)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except sqlite3.Error:
        logger.exception("Не удалось обновить шаблон счёта %s", template_id)
        return jsonify({"error": "Не удалось сохранить шаблон"}), 500

    if not template:
        return jsonify({"error": "Шаблон не найден"}), 404

    log_action(current_user.username, "update_invoice_template", f"{template_id}: {template['name']}")
    return jsonify({"ok": True, "template": template})


@invoices_bp.route("/templates/<int:template_id>", methods=["DELETE"])
@role_required("admin")
def remove_template(template_id):
    """Мягкое удаление: шаблон пропадает из списка, счета по нему остаются."""
    template = get_invoice_template(template_id)
    if not template:
        return jsonify({"error": "Шаблон не найден"}), 404

    delete_invoice_template(template_id)
    log_action(current_user.username, "delete_invoice_template", f"{template_id}: {template['name']}")
    return jsonify({"ok": True})


@invoices_bp.route("/<int:invoice_id>/save-as-template", methods=["POST"])
@role_required("admin")
def save_invoice_as_template(invoice_id):
    """
    Сделать шаблон из существующего счёта. Body: {"name": "..."}.

    Сумма и дата оплаты не переносятся — это ровно то, что меняется от счёта
    к счёту. Разрезы распределения переносим вместе с суммами строк: у аренды
    они из месяца в месяц те же, а обнулить их в форме — одно действие.
    """
    invoice = get_invoice_by_id(invoice_id)
    if not invoice:
        return jsonify({"error": "Счёт не найден"}), 404
    if not user_can_access_invoice(invoice, current_user.username, current_user.role):
        return jsonify({"error": "Нет доступа к этому счёту"}), 403

    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Название шаблона обязательно"}), 400

    values = {"name": name, "city_id": invoice["city_id"], "payer_id": invoice["payer_id"],
              "vat_id": invoice["vat_id"], "payment_purpose": invoice["payment_purpose"]}
    for field in TEMPLATE_COUNTERPARTY_FIELDS:
        values[field] = invoice[field]

    items = [{"store_id": item["store_id"], "expense_category_id": item["expense_category_id"],
              "amount": item["amount"]}
             for item in get_invoice_line_items(invoice_id)]

    try:
        template = create_invoice_template(values, items, current_user.username)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    log_action(current_user.username, "create_invoice_template_from_invoice",
               f"{invoice['invoice_number']} -> {name}")
    return jsonify({"ok": True, "template": template}), 201
