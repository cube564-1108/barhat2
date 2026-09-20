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
import time
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

# Потолок периода на экране «Мои доставки». Список по дням за год — это и
# лишнее чтение витрины на сетевом /data, и нечитаемый экран на телефоне.
# Превышение отбивается текстом, а не молчаливой обрезкой: обрезанный период
# показал бы сумму меньше настоящей, а это цифра про зарплату.
MAX_EARNINGS_DAYS = 92

# Порог, после которого разбор ленты уезжает в лог целиком, по шагам.
#
# Зачем отдельно от общего сторожа медленных запросов (`_log_slow_request` в
# pyrus/server.py): тот называет ручку и общее время, а вопрос «где именно эта
# минута» оставляет открытым. 16.09.2026 лента отвечала 53–113 секунд при
# форме запроса, которая обязана укладываться в десятки миллисекунд, и по коду
# причину найти не удалось — гадать на проде уже стоило сорока минут простоя
# (правило CLAUDE.md: меряй раньше, чем чинишь).
#
# Консоли у контейнера на нашем тарифе Amvera нет, логи читаются в панели —
# поэтому разбор обязан быть ОДНОЙ строкой, а не россыпью.
SLOW_FEED_SECONDS = float(os.environ.get("COURIER_SLOW_FEED_SECONDS", "1.0"))


def _request_started_at() -> Optional[float]:
    """Момент начала запроса, поставленный общим хуком `_start_request_timer`.

    Нужен, чтобы увидеть цену того, что происходит ДО тела ручки: загрузку
    учётки с правами (`load_user`) и проверку секции. Это отдельное соединение
    с barhat.db, и на сетевом диске оно не бесплатное — а в разборе «ручка
    тормозит» его обычно не видно вовсе, потому что меряют только тело.

    Хук живёт в pyrus/server.py и при отдельном запуске blueprint'а может не
    стоять — тогда просто не меряем эту часть.
    """
    return getattr(request, "_started_at", None)


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


def _real_date(value: Optional[str]) -> Optional[str]:
    """
    Дата, которая существует, — или None.

    Регулярки мало: «2026-13-45» ей соответствует, а календарю нет. Любой
    последующий `date.fromisoformat` на таком значении бросит исключение, и
    ручка ответит 500 вместо текста. Возвращаем саму строку, чтобы вызывающий
    код работал с тем же форматом, что хранится в витрине.
    """
    if not _valid_date(value):
        return None
    try:
        date.fromisoformat(value)
    except ValueError:
        return None
    return value


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
    """Лента заказов: свой город, видимые статусы, только курьерская доставка.

    Каждый шаг меряется и при превышении порога уезжает в лог одной строкой —
    см. SLOW_FEED_SECONDS. Замер ничего не решает и ни на что не влияет: он
    только отвечает на вопрос, за что заплачено время.
    """
    view_started = time.monotonic()
    timings: Dict[str, float] = {}
    request_started = _request_started_at()
    if request_started is not None:
        # Всё, что случилось до первой строки тела: разбор запроса, загрузка
        # учётки с правами, проверка секции.
        timings["auth"] = round((view_started - request_started) * 1000, 1)

    def mark(name: str, started: float) -> float:
        now = time.monotonic()
        timings[name] = round((now - started) * 1000, 1)
        return now

    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    if not (_valid_date(date_from) and _valid_date(date_to)):
        date_from, date_to = _default_period()
    if date_from > date_to:
        return error_response("Начало периода позже конца")

    step = time.monotonic()
    dispatch = _has_dispatch()
    step = mark("dispatch", step)
    city = request.args.get("city") if dispatch else _courier_city()
    step = mark("city", step)

    if not dispatch and not city:
        return success_response([], {
            "city": None,
            "warning": "Вам не назначен город. Обратитесь к управляющему — "
                       "без города заказы не показываются.",
        })

    delivery_codes = _courier_delivery_codes()
    step = mark("delivery_codes", step)

    orders = ds.list_orders_for_courier(
        city=city,
        date_from=date_from,
        date_to=date_to,
        courier_user_id=int(current_user.id),
        with_private=dispatch,
        courier_delivery_codes=delivery_codes,
        timings=timings,
    )

    # Общее время считается от НАЧАЛА ЗАПРОСА, а не от входа в тело: иначе
    # шаг `auth` (загрузка учётки с правами — отдельное соединение с barhat.db)
    # оказывается за границей целого, и сумма шагов превышает total_ms. Так
    # это и вылезло на стороже: 45,9 мс шагов против 21,5 мс «всего».
    #
    # Отдельного шага «вся выборка» здесь намеренно нет: он складывался бы из
    # visible_codes + connect + query + serialize и в логе выглядел бы вторым
    # слагаемым тех же миллисекунд.
    finished = time.monotonic()
    total_ms = round((finished - (request_started or view_started)) * 1000, 1)
    if (finished - (request_started or view_started)) >= SLOW_FEED_SECONDS:
        # Одной строкой и с датами: в панели Amvera строки не сгруппировать, а
        # без периода непонятно, сколько данных вообще просили.
        logger.warning(
            "Лента курьера медленно: %s — %s, город %s, шаги мс: %s",
            date_from, date_to, city or "-",
            ", ".join(f"{name}={value}" for name, value in timings.items()),
        )

    return success_response(orders, {
        "city": city,
        "date_from": date_from,
        "date_to": date_to,
        "free": sum(1 for order in orders if order["is_free"]),
        "mine": sum(1 for order in orders if order["is_mine"]),
        "ready": sum(1 for order in orders if order["is_ready"]),
        # Разбор виден и с телефона курьера: если лента встанет у него, а в
        # логах к тому моменту будет каша, спросить «что показывает meta» —
        # самый короткий путь к ответу.
        "timings_ms": timings,
        "total_ms": total_ms,
    })


@delivery_bp.route("/earnings", methods=["GET"])
@section_required("courier_app")
def get_earnings():
    """
    Свои доставки и своя сумма по дням — экран курьера.

    **Секция только `courier_app`, без `DISPATCH_SECTION`.** Это не забывчивость:
    у управляющего профиля курьера нет, и допуск сюда означал бы параметр
    `courier_id` в запросе — то есть ровно ту дыру, из-за которой мы отказались
    открывать курьерам модуль «Оплата курьерам» (там `courier_id` принимается
    параметром, и любой курьер прочитал бы чужую зарплату). Управляющий смотрит
    тот модуль.

    Курьер берётся ИЗ ПРОФИЛЯ текущего пользователя и ниоткуда больше.

    Считает `storage.courier_earnings_by_day` — та же формула, что у выплаты.
    Расхождение между этим экраном и отчётом управляющего недопустимо: это одни
    и те же деньги (сторож `scripts/test_courier_earnings.py`).

    Query: date_from, date_to (YYYY-MM-DD) — имена как у ручки отчёта выплат,
    чтобы два экрана сверялись подстановкой одинаковых дат.
    """
    profile = ds.get_courier_profile(int(current_user.id)) or {}
    crm_courier_id = profile.get("retailcrm_courier_id")

    # Именно разбор датой, а не регулярка: `_valid_date` пропускает
    # «2026-13-45», и `date.fromisoformat` ниже уронил бы ручку в 500 вместо
    # внятного ответа. Логи контейнера на нашем тарифе не читаются, и такой
    # отказ разбирался бы вслепую.
    date_from = _real_date(request.args.get("date_from"))
    date_to = _real_date(request.args.get("date_to"))
    if not (date_from and date_to):
        # Период по умолчанию — как в «Оплате курьерам»: с 1-го числа по
        # сегодня. Одинаковый старт у двух экранов означает, что числа можно
        # сверить, ничего не настраивая.
        #
        # «Сегодня» — по стенным часам салона города курьера, а не по UTC и не
        # по часам его телефона: в 18:31 UTC в Новосибирске уже завтра.
        today = ds.city_today(profile.get("city"))
        date_from, date_to = today[:8] + "01", today
    if date_from > date_to:
        return error_response("Начало периода позже конца")

    days_in_period = (date.fromisoformat(date_to) - date.fromisoformat(date_from)).days
    if days_in_period > MAX_EARNINGS_DAYS:
        return error_response(
            f"Период больше {MAX_EARNINGS_DAYS} дней — выберите покороче")

    # Связки нет — считать нечего, и это НЕ пустой период: у такого человека и
    # выплата не считается вовсе (§7-тер плана модуля). Честный ноль здесь
    # соврал бы: он читается как «вы ничего не возили».
    if not crm_courier_id:
        return success_response({"days": [], "totals": None, "awaiting_close": 0}, {
            "date_from": date_from,
            "date_to": date_to,
            "warning": "Ваша учётная запись не связана с курьером в CRM — "
                       "доставки не считаются ни здесь, ни в оплате. "
                       "Обратитесь к управляющему.",
        })

    result = storage.courier_earnings_by_day(
        int(crm_courier_id), date_from, date_to)

    return success_response(result, {
        "date_from": date_from,
        "date_to": date_to,
        # Отвезли, а оператор ещё не закрыл — объяснение, почему сумма меньше
        # отвезённого. Отдельным числом и НЕ в итоге.
        "awaiting_close": ds.courier_awaiting_close(
            int(current_user.id), date_from, date_to),
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

    # Курьер открыл свой заказ — значит увидел, что в нём поменялось.
    # Гасим отметку только владельцу брони: для остальных правка адреса
    # остаётся новостью.
    if card.get("changed_fields") and card.get("is_mine"):
        ds.mark_changes_seen(order_id, int(current_user.id))
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
        # Салоны города из справочника, а не из сегодняшней ленты: выбор точек
        # — это намерение курьера на смену, и оно не должно зависеть от того,
        # что в ленте прямо сейчас (просьба владельца 18.09.2026).
        "sites": ds.sites_of_city(city),
        # «Сегодня» для выбора даты в приложении — по стенным часам салона.
        # Часы телефона курьера тут не годятся: он может ехать с устройством,
        # настроенным на другой пояс, а окно доставки живёт по салону.
        "today": ds.city_today(city),
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

    profile = ds.get_courier_profile(int(current_user.id)) or {}

    try:
        result = ds.claim_order(
            order_id=order_id,
            courier_user_id=int(current_user.id),
            courier_name=current_user.display_name or current_user.username,
            city=city,
            allow_any_city=dispatch,
            # Курьер проставляется в CRM уже при брони (решение владельца
            # 2026-09-11): оператор видит, кто повезёт заказ, не дожидаясь
            # отметки «Забрал». Статус при этом не меняется.
            courier_crm_id=profile.get("retailcrm_courier_id"),
            username=current_user.username,
        )
    except ds.ClaimError as e:
        return _claim_failed(e)

    log_action(current_user.username, "courier_claim",
               f"Заказ {result.get('order_number') or order_id}")

    # Связки нет — в CRM ничего не ушло, и знать об этом надо сразу, а не в
    # конце месяца по недостающей выплате (§7-тер плана).
    if not profile.get("retailcrm_courier_id"):
        result["warning"] = ("Ваша учётная запись не связана с курьером в CRM — "
                             "эта доставка может не попасть в расчёт оплаты")
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


@delivery_bp.route("/orders/<int:order_id>/extend", methods=["POST"])
@section_required("courier_app", DISPATCH_SECTION)
@require_ajax_header
def extend_claim(order_id: int):
    """
    «Я еду» — отодвинуть сгорание брони на 30 минут, один раз.

    Отдельной ручкой, а не действием в /action: там действия, которые меняют
    состояние заказа и уезжают статусом в CRM. Продление — про нашу бронь, в
    CRM о нём знать нечего.

    `@require_ajax_header` обязателен: POST без тела — простой запрос, его
    отправит форма с чужого сайта, а CSRF-токенов в проекте нет.
    """
    try:
        result = ds.extend_claim(order_id=order_id,
                                 courier_user_id=int(current_user.id))
    except ds.ClaimError as e:
        return _claim_failed(e)

    log_action(current_user.username, "courier_extend", f"Заказ {order_id}")
    return success_response(result)


@delivery_bp.route("/orders/<int:order_id>/action", methods=["POST"])
@section_required("courier_app", DISPATCH_SECTION)
@require_ajax_header
def order_action(order_id: int):
    """
    Отметка курьера: «Забрал», «Доставил», проблема.

    Состояние меняется мгновенно, статус в CRM уходит фоном — курьер не ждёт
    внешнюю систему и работает, даже когда CRM лежит.
    """
    payload = request.get_json(silent=True) or {}
    action = payload.get("action")
    # Бронь есть в справочнике статусов, но не в этой ручке: её ставит и
    # снимает /claim и /release, и попасть сюда она может только по ошибке
    if action not in ds.ALL_ACTIONS or action == ds.ACTION_CLAIM:
        return error_response(f"Неизвестное действие: {action}")

    profile = ds.get_courier_profile(int(current_user.id)) or {}

    try:
        result = ds.advance_assignment(
            order_id=order_id,
            courier_user_id=int(current_user.id),
            action=action,
            username=current_user.username,
            courier_crm_id=profile.get("retailcrm_courier_id"),
            problem_note=(payload.get("note") or None),
        )
    except ds.ClaimError as e:
        return _claim_failed(e)

    log_action(current_user.username, f"courier_{action}", f"Заказ {order_id}")

    # Курьер не сопоставлен с CRM — доставка не попадёт в расчёт оплаты.
    # Молчать об этом до конца месяца нельзя, но и мешать доставке из-за
    # незаполненного справочника тоже (§7-тер).
    if action == ds.ACTION_PICKUP and not profile.get("retailcrm_courier_id"):
        result["warning"] = ("Ваша учётная запись не связана с курьером в CRM — "
                             "эта доставка может не попасть в расчёт оплаты")
    return success_response(result)


# ---------------------------------------------------------------------------
# Push-уведомления
# ---------------------------------------------------------------------------

@delivery_bp.route("/push/key", methods=["GET"])
@section_required("courier_app", DISPATCH_SECTION)
def get_push_key():
    """
    Публичный ключ VAPID для подписки в браузере.

    `null` означает «пуши не настроены» — экран покажет это словами, а не
    молча не подпишется. Ключи кладёт человек в `.env`, сгенерировав их
    скриптом `scripts/generate_vapid_keys.py`.
    """
    from . import push
    return success_response({"public_key": push.public_key(),
                             "configured": push.is_configured()})


# Адреса push-сервисов, на которые нам вообще есть смысл ходить.
#
# ЭТО НЕ ФОРМАЛЬНОСТЬ. `endpoint` присылает браузер, но верит ему сервер: он
# сам пойдёт по этому адресу — а с появлением пробного уведомления пойдёт
# НЕМЕДЛЕННО и вернёт клиенту, получилось или нет. Без проверки любой
# вошедший курьер превращает ручку подписки в сканер нашей внутренней сети:
# подставил `http://10.0.0.5:8080/`, прочитал `test_sent` в ответе — узнал,
# живёт ли там что-нибудь. Наружу такие адреса не торчат, изнутри контейнера
# доступны (находка security-review 17.09.2026).
#
# Белый список, а не запрет приватных диапазонов: DNS-имя может указывать на
# внутренний адрес, и проверка по IP обходится перепривязкой между проверкой
# и самим запросом. Push-сервисов в мире всё равно четыре.
PUSH_HOST_SUFFIXES = (
    "googleapis.com",      # Chrome, Android — FCM
    "mozilla.com",         # Firefox
    "push.apple.com",      # Safari, iOS 16.4+
    "notify.windows.com",  # Edge — WNS
)


def _is_known_push_endpoint(endpoint: str) -> bool:
    """Ведёт ли адрес к настоящему push-сервису."""
    from urllib.parse import urlparse
    try:
        url = urlparse(endpoint)
    except ValueError:
        return False
    if url.scheme != "https":
        return False
    host = (url.hostname or "").lower()
    return any(host == suffix or host.endswith("." + suffix)
               for suffix in PUSH_HOST_SUFFIXES)


@delivery_bp.route("/push/subscribe", methods=["POST"])
@section_required("courier_app", DISPATCH_SECTION)
@require_ajax_header
def push_subscribe():
    """Запомнить подписку устройства."""
    payload = request.get_json(silent=True) or {}
    endpoint = payload.get("endpoint")
    keys = payload.get("keys") or {}
    if not endpoint or not keys.get("p256dh") or not keys.get("auth"):
        return error_response("Подписка неполная")

    if not _is_known_push_endpoint(endpoint):
        # Хост в лог: если однажды появится браузер с новым push-сервисом, это
        # будет выглядеть как «у одного курьера не включаются уведомления», и
        # ответ должен лежать в логе, а не выясняться сначала.
        from urllib.parse import urlparse
        logger.warning("Подписка с неизвестного push-сервиса отклонена: %s",
                       (urlparse(endpoint).hostname or endpoint)[:100])
        return error_response("Неизвестный адрес службы уведомлений")

    ds.save_push_subscription(
        user_id=int(current_user.id),
        endpoint=endpoint,
        p256dh=keys["p256dh"],
        auth=keys["auth"],
        user_agent=(request.headers.get("User-Agent") or "")[:300],
    )

    # Пробное уведомление сразу после подписки.
    #
    # Без него человек включает кнопку и ждёт неизвестно чего: настоящий пуш
    # уходит, только когда в городе ПОЯВИТСЯ новый свободный заказ, и в
    # пустой день молчание неотличимо от поломки. Проверить нечем — консоли
    # у контейнера нет.
    #
    # Отправка не влияет на успех подписки: она уже сохранена, и уронить её
    # из-за недоступного push-сервиса нельзя. Наружу отдаём счётчик и
    # причину, чтобы экран сказал человеку разные вещи для «не дошло» и
    # «дошло, ждите заказ».
    from . import push
    test = push.send_test([int(current_user.id)])

    # Кто вообще получит уведомление о новом заказе. Адресатов выбирают по
    # АКТИВНОМУ ПРОФИЛЮ КУРЬЕРА В ГОРОДЕ ЗАКАЗА, а не по тому, кто нажал
    # кнопку: у администратора и управляющего профиля обычно нет, и для них
    # включённые уведомления не значат ровно ничего. Молчать об этом нельзя —
    # человек считает, что всё настроено (разбор 17.09.2026).
    profile = ds.get_courier_profile(int(current_user.id)) or {}
    return success_response({
        "subscribed": True,
        "test_sent": test.get("sent", 0),
        "test_reason": test.get("reason"),
        # Что именно ответил push-сервис. Без этого «не дошло» одинаково
        # выглядит и когда библиотека не установлена, и когда FCM отверг ключи,
        # и когда телефон запретил уведомления — а чинят это в трёх разных
        # местах (разбор 18.09.2026).
        "test_detail": test.get("detail"),
        "test_status": test.get("status"),
        "city": profile.get("city"),
        "is_recipient": bool(profile.get("active") and profile.get("city")),
    })


@delivery_bp.route("/push/diagnostics", methods=["GET"])
@role_required("admin")
def push_diagnostics():
    """
    Почему уведомление о новом заказе не ушло — по шагам, на текущих данных.

    Сам разбор живёт в `push.why_silent()`: те же числа отдаёт публичный
    `/health?full=1`, а номера заказов и города — только здесь, под админом.
    Ничего не отправляет и не меняет.
    """
    from . import push
    # full=True — с номерами заказов и текстами отказов push-сервиса. В
    # публичный /health те же данные уходят без них: в тексте отказа лежит
    # адрес устройства курьера, а та ручка отдаётся без входа.
    return success_response(push.why_silent(full=True))


@delivery_bp.route("/push/reset-events", methods=["POST"])
@role_required("admin")
@require_ajax_header
def reset_push_events():
    """
    Забыть отправленные события — уведомления по этим заказам уйдут заново.

    Нужно после починки «право сгорало вхолостую»: заказы, по которым тик уже
    занял право, когда подписок ещё не было, иначе молчат навсегда. Консоли у
    контейнера нет, поэтому разовая операция живёт ручкой под админом.

    Период ограничен: полная очистка на живых курьерах — это лавина повторных
    уведомлений по всей истории.
    """
    days = request.get_json(silent=True) or {}
    try:
        days = max(1, min(7, int(days.get("days", 2))))
    except (TypeError, ValueError):
        days = 2

    removed = ds.reset_push_events(days)
    log_action(current_user.username, "push_events_reset", f"days={days}, removed={removed}")
    return success_response({"removed": removed, "days": days})


@delivery_bp.route("/push/unsubscribe", methods=["POST"])
@section_required("courier_app", DISPATCH_SECTION)
@require_ajax_header
def push_unsubscribe():
    payload = request.get_json(silent=True) or {}
    if payload.get("endpoint"):
        ds.delete_push_subscription(payload["endpoint"])
    return success_response({"subscribed": False})


# ---------------------------------------------------------------------------
# Разбор броней (управляющий)
# ---------------------------------------------------------------------------

@delivery_bp.route("/assignments", methods=["GET"])
@section_required(DISPATCH_SECTION)
def get_assignments():
    """Живые брони города: кто что везёт и что уже просрочено."""
    return success_response(ds.list_active_assignments(request.args.get("city")))


@delivery_bp.route("/overview", methods=["GET"])
@section_required(DISPATCH_SECTION)
def get_overview():
    """
    «Доставка сегодня»: где сейчас каждый заказ.

    Период по умолчанию — сегодня и завтра: горизонт, в котором вообще
    что-то решают. Дальше смотреть незачем, а лишние даты стоят чтения.
    """
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    if not (_valid_date(date_from) and _valid_date(date_to)):
        date_from, date_to = _default_period()

    city = request.args.get("city") or None
    overview = ds.dispatch_overview(
        city=city,
        date_from=date_from,
        date_to=date_to,
        courier_delivery_codes=_courier_delivery_codes(),
    )
    # Расхождения по курьеру — тем же вызовом, что и вся картина: это две
    # стороны одного вопроса «где сейчас заказ», и второй поход на экран за
    # ними означал бы второе обращение к общему медленному диску.
    overview["mismatches"] = ds.courier_mismatches(date_from, date_to, city)
    return success_response(overview,
                            {"date_from": date_from, "date_to": date_to})


@delivery_bp.route("/metrics", methods=["GET"])
@section_required(DISPATCH_SECTION)
def get_metrics():
    """Показатели работы курьеров за период."""
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    if not (_valid_date(date_from) and _valid_date(date_to)):
        today = date.today()
        date_from = (today - timedelta(days=30)).isoformat()
        date_to = today.isoformat()
    return success_response(ds.delivery_metrics(
        date_from, date_to, request.args.get("city") or None))


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


@delivery_bp.route("/action-statuses", methods=["GET"])
@section_required(DISPATCH_SECTION)
def get_action_statuses():
    """Справочник «действие курьера → статус CRM» плюс статусы для выбора."""
    return success_response({
        "actions": ds.list_action_statuses(),
        "statuses": storage.list_order_statuses(),
    })


@delivery_bp.route("/action-statuses/<action>", methods=["POST"])
@role_required("admin")
@require_ajax_header
def set_action_status(action: str):
    """Назначить действию код статуса CRM (или очистить, заблокировав действие)."""
    payload = request.get_json(silent=True) or {}
    try:
        ds.set_action_status(action, payload.get("status_code"), current_user.username)
    except ValueError as e:
        return error_response(str(e))
    log_action(current_user.username, "courier_action_status",
               f"{action} → {payload.get('status_code') or 'не задан'}")
    return success_response(ds.list_action_statuses())


@delivery_bp.route("/outbox", methods=["GET"])
@section_required(DISPATCH_SECTION)
def get_outbox():
    """Журнал отправок в CRM: что ушло, что ответили, что застряло."""
    return success_response(ds.list_outbox(
        limit=min(int(request.args.get("limit", 100)), 500),
        state=request.args.get("state") or None,
    ))


@delivery_bp.route("/outbox/<int:outbox_id>/retry", methods=["POST"])
@section_required(DISPATCH_SECTION)
@require_ajax_header
def retry_outbox(outbox_id: int):
    """
    Повторить отправку, застрявшую на 4xx.

    Сама она не повторится намеренно: заказ удалён, статус переименован, ключ
    отозван — такая задача ходила бы наружу вечно. Отсрочку снимает человек,
    починивший причину, и этот жест — кнопка в журнале.
    """
    try:
        result = ds.retry_outbox(outbox_id)
    except ValueError as e:
        return error_response(str(e))

    log_action(current_user.username, "courier_outbox_retry", f"Отправка {outbox_id}")
    return success_response(result)


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


@delivery_bp.route("/profiles/<int:user_id>", methods=["DELETE"])
@role_required("admin")
@require_ajax_header
def delete_profile(user_id: int):
    """
    Убрать профиль курьера. Учётную запись это не трогает.

    Профиль с живыми бронями не удаляется: заказы остались бы без владельца
    и висели бы забронированными, недоступные никому.
    """
    try:
        ds.delete_courier_profile(user_id)
    except ValueError as e:
        return error_response(str(e), 409)
    log_action(current_user.username, "courier_profile_delete", f"user_id={user_id}")
    return success_response({"deleted": user_id})


@delivery_bp.route("/profiles/<int:user_id>/active", methods=["POST"])
@role_required("admin")
@require_ajax_header
def toggle_profile_active(user_id: int):
    """Отключить курьера на время (отпуск, болезнь), не теряя настройки."""
    payload = request.get_json(silent=True) or {}
    ds.set_profile_active(user_id, bool(payload.get("active")), current_user.username)
    log_action(current_user.username, "courier_profile_active",
               f"user_id={user_id} -> {bool(payload.get('active'))}")
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
