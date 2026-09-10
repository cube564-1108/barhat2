"""
JSON API модуля «Курьеры: доставка заказов» (Фаза 2).

Отдельный blueprint от `couriers_bp` (оплата курьерам) намеренно: у них разные
пользователи и разные права. Тот отвечает на вопрос «сколько заплатить», этот —
«что везти сейчас», и путать их права нельзя.

Три правила, которым подчиняется каждая ручка здесь:

1. **Читаем только свою базу.** Живой запрос в CRM из обработчика уже дважды
   укладывал прод: воркеров всего два, и один зависший запрос занимает половину
   мощности сайта. В CRM ходит лента (`delivery_feed.py`) и фоновый синк.
2. **Отбор на сервере.** Город курьера, тип доставки и видимые статусы
   применяются в запросе, а не в интерфейсе. Фронт получает то, что ему
   положено видеть, и не больше.
3. **Контакты — после брони.** До неё курьеру видны улица и время; телефон и
   комментарии открываются тому, кто взял заказ, и управляющему.
"""

import logging
import os
import re
import sys
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required

auth_path = os.path.join(os.path.dirname(__file__), '../')
sys.path.insert(0, auth_path)
from auth import (log_action, require_ajax_header, role_required,  # noqa: E402
                  section_required)

from . import delivery_storage as ds  # noqa: E402
from . import storage  # noqa: E402

logger = logging.getLogger(__name__)

delivery_bp = Blueprint("courier_delivery", __name__, url_prefix="/api/courier")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Секция управляющего: весь город, чужие брони, контакты по любому заказу.
DISPATCH_SECTION = "courier_dispatch"


def error_response(message: str, status: int = 400):
    return jsonify({"success": False, "error": message}), status


def success_response(data: Any, meta: Dict[str, Any] = None):
    payload = {"success": True, "data": data}
    if meta:
        payload["meta"] = meta
    return jsonify(payload)


def _has_dispatch() -> bool:
    """
    Есть ли у текущего пользователя права управляющего по доставке.

    Именно `has_module_access`, а не `current_user.sections`: у модели User
    права называются `permissions` и уже загружены вместе с учёткой одним
    соединением. `sections` — это поле ОТВЕТА ручки /api/auth/me для фронта,
    у объекта пользователя его нет, и обращение к нему молча давало False:
    управляющий получал пустую ленту вместо всех городов.
    """
    if getattr(current_user, "role", None) == "admin":
        return True
    return current_user.has_module_access(DISPATCH_SECTION)


def _courier_city() -> Optional[str]:
    """
    Город текущего курьера из его профиля.

    None означает «город не задан» — и это не «показать все города»: курьер без
    города не должен видеть чужие заказы с телефонами клиентов. Ручка в таком
    случае отдаёт пустой список и внятную причину.
    """
    profile = ds.get_courier_profile(int(current_user.id))
    return (profile or {}).get("city")


def _valid_date(value: Optional[str]) -> bool:
    return bool(value and _DATE_RE.match(value))


def _default_period() -> tuple:
    """Сегодня и завтра — горизонт, в котором курьер вообще что-то решает."""
    today = date.today()
    return today.isoformat(), (today + timedelta(days=1)).isoformat()


def _courier_delivery_codes() -> List[str]:
    """
    Коды типов доставки «своим курьером» из справочника.

    Не константа в коде: какой тип доставки считать курьерским, решает человек в
    интерфейсе (тот же справочник, что у выплат). У Яндекс.Доставки, например,
    вообще нет адреса — показывать её курьеру нечем.
    """
    return [row["code"] for row in storage.list_delivery_types()
            if row.get("counts_as_courier")]


# ---------------------------------------------------------------------------
# Экран курьера
# ---------------------------------------------------------------------------

@delivery_bp.route("/orders", methods=["GET"])
@section_required("courier_app", DISPATCH_SECTION)
def get_orders():
    """Лента заказов: свой город, видимые статусы, только курьерская доставка."""
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    if not (_valid_date(date_from) and _valid_date(date_to)):
        date_from, date_to = _default_period()
    if date_from > date_to:
        return error_response("Начало периода позже конца")

    dispatch = _has_dispatch()
    city = request.args.get("city") if dispatch else _courier_city()

    if not dispatch and not city:
        return success_response([], {
            "city": None,
            "warning": "Вам не назначен город. Обратитесь к управляющему — "
                       "без города заказы не показываются.",
        })

    orders = ds.list_orders_for_courier(
        city=city,
        date_from=date_from,
        date_to=date_to,
        courier_user_id=int(current_user.id),
        with_private=dispatch,
        courier_delivery_codes=_courier_delivery_codes(),
    )
    return success_response(orders, {
        "city": city,
        "date_from": date_from,
        "date_to": date_to,
        "free": sum(1 for order in orders if order["is_free"]),
        "mine": sum(1 for order in orders if order["is_mine"]),
        "ready": sum(1 for order in orders if order["is_ready"]),
    })


@delivery_bp.route("/orders/<int:order_id>", methods=["GET"])
@section_required("courier_app", DISPATCH_SECTION)
def get_order(order_id: int):
    """Карточка заказа с составом. Город проверяется и здесь — ссылку могут открыть прямо."""
    dispatch = _has_dispatch()
    city = None if dispatch else _courier_city()
    if not dispatch and not city:
        return error_response("Вам не назначен город", 403)

    card = ds.order_for_courier(order_id, city=city,
                                courier_user_id=int(current_user.id),
                                with_private=dispatch)
    if not card:
        return error_response("Заказ не найден", 404)

    # Открытие карточки с персональными данными — событие для аудита.
    # Пишется фоновой очередью, внутри запроса база не трогается.
    if card.get("recipient_phone") or card.get("customer_phone"):
        log_action(current_user.username, "courier_order_view",
                   f"Заказ {card.get('order_number') or order_id}")
    return success_response(card)


@delivery_bp.route("/profile", methods=["GET"])
@section_required("courier_app", DISPATCH_SECTION)
def get_profile():
    """Свой профиль: город и связка с курьером CRM (от неё зависит выплата)."""
    profile = ds.get_courier_profile(int(current_user.id)) or {}
    city = profile.get("city")
    return success_response({
        "city": city,
        "retailcrm_courier_id": profile.get("retailcrm_courier_id"),
        "settings": ds.city_settings(city),
        # Курьер должен видеть, что связки нет: это его деньги, и молчать об
        # этом до конца месяца нельзя.
        "warning": None if profile.get("retailcrm_courier_id") else
                   "Ваша учётная запись не связана с курьером в CRM — "
                   "доставки могут не попасть в расчёт оплаты",
    })


# ---------------------------------------------------------------------------
# Бронь
# ---------------------------------------------------------------------------

# Код причины отказа → HTTP-статус. «Занято» и «не ваш город» требуют разных
# действий человека, и одним кодом их подавать нельзя: фронту пришлось бы
# разбирать текст ошибки, а он меняется.
CLAIM_ERROR_STATUS = {
    "not_found": 404,
    "forbidden": 403,
    "taken": 409,
    "already_mine": 409,
    "gone": 409,
    "limit": 409,
    "picked_up": 409,
    "horizon": 400,
}


def _claim_failed(error: ds.ClaimError):
    return jsonify({"success": False, "error": str(error), "code": error.code}), \
        CLAIM_ERROR_STATUS.get(error.code, 409)


@delivery_bp.route("/orders/<int:order_id>/claim", methods=["POST"])
@section_required("courier_app", DISPATCH_SECTION)
@require_ajax_header
def claim_order(order_id: int):
    """
    Забронировать заказ за собой.

    Гонку держит хранилище (`BEGIN IMMEDIATE` + уникальный индекс): двое
    нажавших одновременно получают один — бронь, второй — 409 с именем того,
    кто успел. 500 здесь быть не должно ни при каком исходе гонки.
    """
    dispatch = _has_dispatch()
    city = _courier_city()
    if not dispatch and not city:
        return error_response("Вам не назначен город", 403)

    try:
        result = ds.claim_order(
            order_id=order_id,
            courier_user_id=int(current_user.id),
            courier_name=current_user.display_name or current_user.username,
            city=city,
            allow_any_city=dispatch,
        )
    except ds.ClaimError as e:
        return _claim_failed(e)

    log_action(current_user.username, "courier_claim",
               f"Заказ {result.get('order_number') or order_id}")
    return success_response(result)


@delivery_bp.route("/orders/<int:order_id>/release", methods=["POST"])
@section_required("courier_app", DISPATCH_SECTION)
@require_ajax_header
def release_order(order_id: int):
    """
    Отказаться от своей брони. Без объяснений, но с записью в журнал:
    автоснятия и отказы — вход для разговора, а не для санкции.
    """
    try:
        result = ds.release_order(
            order_id=order_id,
            courier_user_id=int(current_user.id),
            reason=ds.RELEASE_SELF,
        )
    except ds.ClaimError as e:
        return _claim_failed(e)

    log_action(current_user.username, "courier_release", f"Заказ {order_id}")
    return success_response(result)


# ---------------------------------------------------------------------------
# Разбор броней (управляющий)
# ---------------------------------------------------------------------------

@delivery_bp.route("/assignments", methods=["GET"])
@section_required(DISPATCH_SECTION)
def get_assignments():
    """Живые брони города: кто что везёт и что уже просрочено."""
    return success_response(ds.list_active_assignments(request.args.get("city")))


@delivery_bp.route("/assignments/<int:order_id>/release", methods=["POST"])
@section_required(DISPATCH_SECTION)
@require_ajax_header
def admin_release(order_id: int):
    """
    Снять чужую бронь.

    Отдельная ручка под секцией управляющего, а не флаг в курьерской:
    «снять бронь у любого» — другое право, и выдавать его курьеру нельзя.
    Консоли у контейнера на этом тарифе нет, поэтому разбор зависших броней
    возможен только так.
    """
    try:
        result = ds.release_order(
            order_id=order_id,
            courier_user_id=int(current_user.id),
            reason=ds.RELEASE_ADMIN,
            allow_any_courier=True,
        )
    except ds.ClaimError as e:
        return _claim_failed(e)

    log_action(current_user.username, "courier_release_admin", f"Заказ {order_id}")
    return success_response(result)


# ---------------------------------------------------------------------------
# Настройки (админ)
# ---------------------------------------------------------------------------

@delivery_bp.route("/statuses", methods=["GET"])
@role_required("admin")
def get_statuses():
    """Справочник «статус CRM → роль» плюс полный список статусов для выбора."""
    return success_response({
        "configured": ds.list_visible_statuses(),
        "all": storage.list_order_statuses(),
        "roles": list(ds.STATUS_ROLES),
    })


@delivery_bp.route("/statuses/<path:status_code>", methods=["POST"])
@role_required("admin")
@require_ajax_header
def set_status(status_code: str):
    """Назначить статусу роль (visible / ready) или убрать его из справочника."""
    payload = request.get_json(silent=True) or {}
    role = payload.get("role")
    try:
        ds.set_visible_status(status_code, role, current_user.username)
    except ValueError as e:
        return error_response(str(e))
    log_action(current_user.username, "courier_status_role",
               f"{status_code} → {role or 'убран'}")
    return success_response(ds.list_visible_statuses())


@delivery_bp.route("/profiles", methods=["GET"])
@role_required("admin")
def get_profiles():
    """Профили курьеров и те из них, у кого нет связки с CRM."""
    return success_response({
        "profiles": ds.list_courier_profiles(),
        "without_crm_link": ds.profiles_without_crm_link(),
        "couriers": storage.list_couriers(only_active=True),
        "cities": storage.list_cities(),
    })


@delivery_bp.route("/profiles/<int:user_id>", methods=["POST"])
@role_required("admin")
@require_ajax_header
def save_profile(user_id: int):
    """Завести или изменить профиль курьера: город и связка с курьером CRM."""
    payload = request.get_json(silent=True) or {}
    crm_id = payload.get("retailcrm_courier_id")
    try:
        ds.save_courier_profile(
            user_id=user_id,
            username=payload.get("username"),
            city=payload.get("city") or None,
            retailcrm_courier_id=int(crm_id) if crm_id not in (None, "") else None,
            active=bool(payload.get("active", True)),
            updated_by=current_user.username,
        )
    except ValueError as e:
        # Связка уже занята другой учёткой — штатная ошибка ввода, не 500.
        return error_response(str(e), 409)
    log_action(current_user.username, "courier_profile_save", f"user_id={user_id}")
    return success_response(ds.get_courier_profile(user_id))


@delivery_bp.route("/city-settings", methods=["GET"])
@section_required(DISPATCH_SECTION)
def get_city_settings():
    """Настройки по городам: лимит броней, горизонт, порог «никто не взял»."""
    return success_response(ds.list_city_settings(storage.list_cities()))


@delivery_bp.route("/city-settings/<path:city>", methods=["POST"])
@role_required("admin")
@require_ajax_header
def save_city_settings(city: str):
    payload = request.get_json(silent=True) or {}
    try:
        ds.set_city_settings(city, payload, current_user.username)
    except ValueError as e:
        return error_response(str(e))
    log_action(current_user.username, "courier_city_settings", city)
    return success_response(ds.city_settings(city))
