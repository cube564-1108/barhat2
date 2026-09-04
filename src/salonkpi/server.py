"""
JSON API раздела «Показатели салонов».

Blueprint регистрируется в src/pyrus/server.py (мастер-приложение) — тот же
паттерн, что couriers_bp / moysklad_bp.

Доступ режется по данным, а не только по меню: управляющий видит показатели
своих салонов и не должен получить выручку соседнего города, подставив store_id
в адрес. Справочник связей и список несопоставленного — только администратору.
"""

import logging
import os
import sys
from typing import List, Optional

from flask import Blueprint, jsonify, request
from flask_login import current_user

auth_path = os.path.join(os.path.dirname(__file__), "../")
sys.path.insert(0, auth_path)
from auth import section_required, role_required, require_ajax_header, log_action  # noqa: E402

from . import metrics, storage  # noqa: E402

logger = logging.getLogger(__name__)

salonkpi_bp = Blueprint("salonkpi", __name__, url_prefix="/api/salon-kpi")


def error_response(message: str, status: int = 400):
    return jsonify({"success": False, "error": message}), status


def success_response(payload: dict):
    data = {"success": True}
    data.update(payload)
    return jsonify(data)


def _allowed_store_ids() -> Optional[List[int]]:
    """
    Салоны, доступные текущему пользователю.

    None — доступны все (администратор). Пустой список — салоны не привязаны;
    это отдельное состояние, а не «нет данных»: у учётки, созданной входом из
    Пульса, привязок нет по умолчанию, и человек должен увидеть внятную
    подсказку вместо пустой таблицы.
    """
    if getattr(current_user, "role", None) == "admin":
        return None
    try:
        from cashshifts.storage import get_user_stores
        return get_user_stores(current_user.username)
    except Exception as e:
        logger.error(f"Не удалось получить салоны пользователя: {e}")
        return []


def _month_arg() -> str:
    month = request.args.get("month") or metrics.current_month()
    return month


# ============================================================================
# Показатели
# ============================================================================

@salonkpi_bp.route("/summary", methods=["GET"])
@section_required("salon_kpi")
def get_summary():
    """Показатели за месяц: KPI по доступным салонам + строки таблицы."""
    month = _month_arg()
    if not metrics.valid_month(month):
        return error_response("month должен быть в формате YYYY-MM")

    scope = request.args.get("scope", "salon")
    if scope not in ("salon", "city"):
        return error_response("scope должен быть salon или city")

    store_ids = _allowed_store_ids()
    data = metrics.build_summary(month, store_ids, scope)
    data["can_edit"] = getattr(current_user, "role", None) == "admin"
    return success_response({"data": data})


@salonkpi_bp.route("/salon/<int:store_id>", methods=["GET"])
@section_required("salon_kpi")
def get_salon(store_id: int):
    """Детализация салона: флористы, категории негатива, каналы, склад."""
    month = _month_arg()
    if not metrics.valid_month(month):
        return error_response("month должен быть в формате YYYY-MM")

    allowed = _allowed_store_ids()
    if allowed is not None and store_id not in allowed:
        return error_response("Нет доступа к этому салону", 403)

    data = metrics.salon_details(store_id, month)
    if not data:
        return error_response("Салон не найден", 404)
    return success_response({"data": data})


@salonkpi_bp.route("/stores", methods=["GET"])
@section_required("salon_kpi")
def get_stores():
    """Салоны, доступные пользователю (для фильтра)."""
    return success_response({"stores": storage.list_stores(_allowed_store_ids(), only_linked=True)})


# ============================================================================
# Планы
# ============================================================================

@salonkpi_bp.route("/plans", methods=["GET"])
@section_required("salon_kpi")
def get_plans():
    month = _month_arg()
    if not metrics.valid_month(month):
        return error_response("month должен быть в формате YYYY-MM")

    stores = storage.list_stores(_allowed_store_ids(), only_linked=True)
    plans = storage.get_plans(month)
    return success_response({
        "month": month,
        "plans": [
            {"store_id": s["id"], "name": s["name"], "amount": plans.get(s["id"])}
            for s in stores
        ],
    })


@salonkpi_bp.route("/plans", methods=["POST"])
@role_required("admin")
@require_ajax_header
def save_plans():
    """
    Сохранить планы на месяц.

    Body: {"month": "2026-09", "plans": [{"store_id": 1, "amount": 2100000}, ...]}
    amount = null удаляет план: «плана нет» и «план нулевой» — разные состояния.
    """
    data = request.get_json(silent=True) or {}
    month = data.get("month")
    if not metrics.valid_month(month or ""):
        return error_response("month должен быть в формате YYYY-MM")

    plans = data.get("plans")
    if not isinstance(plans, list):
        return error_response("plans должен быть списком")

    # План можно задать только тому, кто участвует в показателях
    known = {s["id"] for s in storage.list_stores(only_linked=True)}
    saved = 0
    for item in plans:
        try:
            store_id = int(item.get("store_id"))
        except (TypeError, ValueError):
            return error_response("store_id должен быть числом")
        if store_id not in known:
            return error_response(f"Салон {store_id} не найден")

        amount = item.get("amount")
        if amount is not None:
            try:
                amount = float(amount)
            except (TypeError, ValueError):
                return error_response(f"Некорректная сумма плана для салона {store_id}")
            if amount < 0:
                return error_response("План не может быть отрицательным")

        storage.set_plan(store_id, month, amount, current_user.username)
        saved += 1

    metrics.invalidate_cache()
    log_action(current_user.username, "salon_kpi_plans", f"{month}: обновлено {saved} планов")
    return success_response({"month": month, "saved": saved})


# ============================================================================
# Справочник соответствий (только админ)
# ============================================================================

@salonkpi_bp.route("/links", methods=["GET"])
@role_required("admin")
def get_links():
    # Список салонов здесь полный (без only_linked): именно отсюда привязывают
    # новый салон, а он по определению ещё ни с чем не связан
    return success_response({"links": storage.list_links(), "sources": storage.SOURCES,
                             "stores": storage.list_stores()})


@salonkpi_bp.route("/links", methods=["POST"])
@role_required("admin")
@require_ajax_header
def create_link():
    """Body: {"source": "crm_site", "external_key": "...", "store_id": 1}"""
    data = request.get_json(silent=True) or {}
    try:
        link = storage.set_link(
            data.get("source"), data.get("external_key"), int(data.get("store_id"))
        )
    except (TypeError, ValueError) as e:
        return error_response(str(e))
    except storage.LinkExistsError as e:
        return error_response(f"Ключ уже привязан: {e}", 409)

    metrics.invalidate_cache()
    log_action(current_user.username, "salon_kpi_link",
               f"{link['source']} «{link['external_key']}» → {link['store_name']}")
    return success_response({"link": link})


@salonkpi_bp.route("/links/<int:link_id>", methods=["DELETE"])
@role_required("admin")
@require_ajax_header
def remove_link(link_id: int):
    if not storage.delete_link(link_id):
        return error_response("Связь не найдена", 404)
    metrics.invalidate_cache()
    log_action(current_user.username, "salon_kpi_link_delete", str(link_id))
    return success_response({"deleted": link_id})


@salonkpi_bp.route("/unmapped", methods=["GET"])
@role_required("admin")
def get_unmapped():
    """Ключи источников, не отнесённые ни к одному салону, с подсказками."""
    month = _month_arg()
    if not metrics.valid_month(month):
        return error_response("month должен быть в формате YYYY-MM")
    return success_response({"data": metrics.unmapped(month)})


@salonkpi_bp.route("/unflagged-couriers", methods=["GET"])
@role_required("admin")
def get_unflagged_couriers():
    """
    Курьеры-службы без флага такси: новый агрегатор иначе молча занижает
    показатель «доля заказов, отданных такси-службам».
    """
    month = _month_arg()
    if not metrics.valid_month(month):
        return error_response("month должен быть в формате YYYY-MM")

    date_from, date_to = metrics.month_bounds(month)
    try:
        from couriers import storage as couriers_storage
        rows = couriers_storage.list_unflagged_couriers(date_from, date_to)
    except Exception as e:
        logger.warning(f"Список неотмеченных курьеров недоступен: {e}")
        rows = []
    return success_response({"couriers": rows})
