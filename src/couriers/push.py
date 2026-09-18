"""
Push-уведомления курьерам (Фаза 6 плана «Курьеры: доставка заказов»).

Четыре правила, которым здесь всё подчинено:

1. **В уведомлении нет персональных данных.** Оно видно на экране блокировки,
   через плечо, кому угодно. «Новый заказ на ул. Ленина к 15:00» — можно,
   имя и телефон получателя — нельзя (§10.5 плана).
2. **Каждое событие уходит ровно один раз.** Планировщик крутится в каждом из
   двух воркеров, и без общего журнала «заказ + событие» курьер получает
   дубли (находка К6). Право на отправку занимается уникальным ключом в БД.
3. **Пуш — усиление, а не единственный канал.** Android гасит фон, iOS
   требует установленной PWA. Лента обновляется сама каждые 30 секунд, и
   молчание пушей не должно приводить к потерянному заказу.
4. **Тишиной управляет человек, а не расписание.** Тихие часы здесь были и
   убраны 2026-09-10 по решению владельца: курьер, включивший уведомления,
   уже согласился их получать, а не хочет ночью — выключает кнопкой. Цена
   расписания оказалась выше пользы: молчание по часам неотличимо от
   поломки, и первый же вопрос «почему не приходят» пришлось разбирать
   именно так. Колонки `quiet_hours_*` в `courier_city_settings` остались
   неиспользуемыми — сносить их отдельной миграцией ради этого не стоит.

Библиотека `pywebpush` импортируется ЛЕНИВО, внутри отправки: без ключей
VAPID пуши выключены целиком, и ни отсутствие библиотеки, ни отсутствие
ключей не должны ронять старт воркера или локальный прогон сторожей.
"""

import json
import logging
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from . import delivery_storage as ds
from . import salon_time

logger = logging.getLogger(__name__)

VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY", "").strip()
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "").strip()
VAPID_CONTACT = os.getenv("VAPID_CONTACT", "mailto:komdir.barhat@gmail.com").strip()

# Сколько ждём push-сервис. Он в тике ленты, а тик обязан оставаться дешёвым.
PUSH_TIMEOUT_SECONDS = 10


def is_configured() -> bool:
    """Настроены ли ключи. Без них модуль молчит, а не падает."""
    return bool(VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY)


def public_key() -> Optional[str]:
    return VAPID_PUBLIC_KEY or None


# Части адреса, которых в уведомлении быть не должно: по ним попадают в
# подъезд. Именно они стоят В КОНЦЕ строки, поэтому «взять две последние
# части» даёт не улицу с домом, а «45, кв. 12» — и адрес, и лишнее.
_PRIVATE_ADDRESS_PARTS = re.compile(
    r"^\s*(кв\b|квартира|подъезд|под\b|этаж|эт\b|код|домофон|офис|оф\b)", re.I)

# Административные хвосты в начале строки: курьеру они ничего не говорят
_ADMIN_ADDRESS_PARTS = re.compile(r"(область|обл\.|край|район|р-н|индекс)", re.I)


def _short_address(address: Optional[str]) -> str:
    """
    Улица и дом — без квартиры, подъезда и кода домофона.

    Уведомление видно на экране блокировки, через плечо, кому угодно. Курьеру
    нужен ориентир («успею или нет»), а попасть в подъезд по нему быть не
    должно: точный адрес открывается в приложении, после брони.
    """
    if not address:
        return "адрес уточняется"

    parts = [part.strip() for part in str(address).split(",") if part.strip()]
    useful = [p for p in parts
              if not _PRIVATE_ADDRESS_PARTS.search(p) and not _ADMIN_ADDRESS_PARTS.search(p)]
    if not useful:
        return "адрес уточняется"
    return ", ".join(useful[-2:])


def send_to_users(user_ids: List[int], payload: Dict[str, Any]) -> Dict[str, int]:
    """
    Отправить уведомление устройствам этих пользователей.

    Возвращает счётчики. Ошибка одной подписки не мешает остальным: у
    курьера может быть выброшенный телефон со старым endpoint.
    """
    result = {"sent": 0, "failed": 0, "dropped": 0}
    if not is_configured():
        return result

    subscriptions = ds.push_subscriptions_for(user_ids)
    if not subscriptions:
        return result

    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        logger.warning("pywebpush не установлен — пуши не отправляются")
        return result

    body = json.dumps(payload, ensure_ascii=False)
    for subscription in subscriptions:
        info = {
            "endpoint": subscription["endpoint"],
            "keys": {"p256dh": subscription["p256dh"], "auth": subscription["auth"]},
        }
        try:
            webpush(
                subscription_info=info,
                data=body,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": VAPID_CONTACT},
                timeout=PUSH_TIMEOUT_SECONDS,
            )
            ds.mark_push_ok(subscription["endpoint"])
            result["sent"] += 1
        except WebPushException as e:
            # 410 Gone / 404 — подписки больше нет. Держать её значит копить
            # очередь и тратить время тика на заведомо мёртвый адрес.
            status = getattr(getattr(e, "response", None), "status_code", None)
            drop = status in (404, 410)
            ds.mark_push_failed(subscription["endpoint"], drop=drop)
            result["dropped" if drop else "failed"] += 1
        except Exception as e:
            ds.mark_push_failed(subscription["endpoint"])
            result["failed"] += 1
            logger.warning(f"Push не ушёл: {e}")

    return result


def send_test(user_ids: List[int]) -> Dict[str, Any]:
    """
    Пробное уведомление — единственный честный ответ на «а они работают?».

    Пуш «новый заказ» уходит, только когда в городе ПОЯВИТСЯ новый свободный
    заказ, и уходит по каждому заказу ровно один раз. В пустой день молчание
    неотличимо от поломки, и разобрать его нечем: консоли у контейнера нет,
    а логи человеку недоступны.

    Пробное уведомление разделяет два случая, которые иначе выглядят
    одинаково: «цепочка браузер → сервер → push-сервис → телефон не работает»
    и «цепочка жива, просто повода не было». Это разные действия человека,
    поэтому и ответы должны быть разными.

    Дедупликации здесь нет намеренно: `claim_push_event` держит «одно событие
    на заказ», а проверку человек вправе повторять сколько угодно.

    17.09.2026 владелец включил уведомления и не смог понять, работают они
    или нет.
    """
    if not is_configured():
        return {"sent": 0, "failed": 0, "dropped": 0, "reason": "not_configured"}

    result = send_to_users(user_ids, {
        "title": "Уведомления включены",
        "body": "Так будет выглядеть сообщение о новом заказе.",
        # Свой тег: пробное не должно затирать настоящее уведомление о заказе
        "tag": "test",
        "url": "/app/courier",
    })
    if not any(result.values()):
        # Подписок нет вовсе — до push-сервиса дело не дошло
        result["reason"] = "no_subscriptions"
    return result


def why_silent() -> Dict[str, Any]:
    """
    Почему уведомление о новом заказе не уходит — по шагам, на текущих данных.

    ЗАЧЕМ ЭТО СУЩЕСТВУЕТ. «Уведомления не приходят» — симптом, у которого
    полдесятка разных причин, и снаружи они выглядят одинаково. 17–18.09.2026
    на этом сгорели две правки подряд: сначала решили, что человек не адресат,
    потом — что право на событие сгорело вхолостую. Обе версии звучали
    убедительно, обе были мимо, а проверить их было нечем: консоли у
    контейнера нет, боевую базу не посмотреть.

    Функция повторяет ТУ ЖЕ выборку, что делает рассылка в
    `notify_courier_events`, и считает, сколько заказов отсеивается на каждом
    шаге. Ничего не отправляет и ничего не меняет — звать можно сколько угодно.

    `steps` — только числа, их отдаёт и публичный `/health?full=1`.
    `blocked` содержит номера заказов и города, поэтому наружу уходит лишь
    через админскую ручку.

    Правило CLAUDE.md: не нашёл причину со второй попытки — встраивай
    измерение, а не правку.
    """
    from datetime import date, timedelta

    from . import storage
    from .delivery_storage import list_orders_for_courier

    today = date.today().isoformat()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    # Коды и окно — ровно как в рассылке: диагностика по другой выборке врёт
    # убедительнее, чем молчание.
    codes = [r["code"] for r in storage.list_delivery_types()
             if r.get("counts_as_courier")]

    orders = list_orders_for_courier(city=None, date_from=today, date_to=tomorrow,
                                     courier_delivery_codes=codes)

    steps = {"in_window": len(orders), "free": 0, "has_courier_in_city": 0,
             "has_subscription": 0, "already_sent": 0, "would_send": 0}
    by_city: Dict[str, Dict[str, int]] = {}
    blocked: List[Dict[str, Any]] = []

    def note(order, city, reason):
        if len(blocked) < 10:
            blocked.append({"order": order.get("order_number"),
                            "city": city, "reason": reason})

    for order in orders:
        city = order.get("city")
        stat = by_city.setdefault(city or "(город не задан)",
                                  {"orders": 0, "free": 0, "couriers": 0,
                                   "subscribed_couriers": 0})
        stat["orders"] += 1

        if not order.get("is_free"):
            continue
        steps["free"] += 1
        stat["free"] += 1

        user_ids = ds.courier_user_ids(city)
        stat["couriers"] = len(user_ids)
        if not user_ids:
            note(order, city, "в городе нет активного профиля курьера")
            continue
        steps["has_courier_in_city"] += 1

        if not ds.has_push_subscriptions(user_ids):
            note(order, city, "у курьеров города нет подписанных устройств")
            continue
        stat["subscribed_couriers"] = 1
        steps["has_subscription"] += 1

        # Право НЕ занимаем: диагностика ничего не меняет
        with ds.get_db() as conn:
            seen = conn.execute(
                "SELECT 1 FROM push_events WHERE retailcrm_order_id = ? "
                "  AND event_type = ?",
                (order.get("retailcrm_order_id"), ds.EVENT_NEW_ORDER)).fetchone()
        if seen:
            steps["already_sent"] += 1
            note(order, city, "уведомление по этому заказу уже отправляли")
            continue

        steps["would_send"] += 1

    return {
        "window": {"date_from": today, "date_to": tomorrow,
                   "note": "даты по UTC, как в рассылке"},
        "delivery_codes": codes,
        "vapid_configured": is_configured(),
        "steps": steps,
        "feed": _feed_state(),
        "by_city": by_city,
        "blocked": blocked,
    }


def _feed_state() -> Dict[str, Any]:
    """
    Живёт ли лента — тот, кто рассылает.

    Уведомления отправляются НЕ сами по себе: рассылка — предпоследний шаг
    тика ленты (`run_once` → `sweep_assignments` → `notify_courier_events`).
    Если тик падает раньше или вовсе не идёт, «ушло бы 5» останется «ушло бы»
    навсегда, и по одним счётчикам отсева этого не видно.

    Смотрим то, что тик о себе оставляет в `sync_state`: талон на следующий
    запуск, лок и курсор истории. Залипший лок — отдельная беда: держатель мог
    умереть вместе с воркером, и до истечения TTL лента стоит целиком.
    """
    from .delivery_feed import CURSOR_KEY, FEED_LOCK
    from .storage import get_db as couriers_db

    keys = (f"schedule:{FEED_LOCK}", f"lock:{FEED_LOCK}", CURSOR_KEY)
    try:
        with couriers_db() as conn:
            rows = conn.execute(
                "SELECT key, value, updated_at FROM sync_state "
                f" WHERE key IN ({','.join('?' * len(keys))})", keys).fetchall()
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}

    state = {row["key"]: {"value": row["value"], "updated_at": row["updated_at"]}
             for row in rows}
    return {
        # Время в этих полях — UTC, как всё, что пишет планировщик
        "next_tick_not_before": state.get(f"schedule:{FEED_LOCK}", {}).get("value"),
        "lock_until": state.get(f"lock:{FEED_LOCK}", {}).get("value"),
        "cursor": state.get(CURSOR_KEY, {}).get("value"),
        "cursor_updated_at": state.get(CURSOR_KEY, {}).get("updated_at"),
        "now_utc": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _notify(order: Dict[str, Any], event_type: str, title: str, body: str,
            user_ids: List[int]) -> bool:
    """Одно событие: занять право на отправку и отправить."""
    if not is_configured() or not user_ids:
        return False

    # ПРАВО НА СОБЫТИЕ ЗАНИМАЕТСЯ ТОЛЬКО ТОГДА, КОГДА ЕСТЬ КУДА ОТПРАВЛЯТЬ.
    #
    # Журнал «заказ + событие» одноразовый: занял — второго шанса нет. Пока
    # проверки не было, тик ленты занимал право по каждому свободному заказу,
    # даже если ни одно устройство курьеров города ещё не подписано, и слал в
    # пустоту. Курьер, включивший уведомления после этого, не получал ничего
    # по УЖЕ существующим заказам — а в спокойный день новых и не появляется.
    #
    # Ровно так 17.09.2026 выглядело «включил уведомления, ни одного пуша»:
    # профиль курьера был, подписки на момент прохода тика — ещё нет.
    #
    # Проверка стоит здесь, а не внутри send_to_users: та уже после claim, и
    # знание «отправлять некому» приходит слишком поздно.
    if not ds.has_push_subscriptions(user_ids):
        return False

    if not ds.claim_push_event(order["retailcrm_order_id"], event_type):
        return False

    send_to_users(user_ids, {
        "title": title,
        "body": body,
        "tag": f"{event_type}-{order['retailcrm_order_id']}",
        # Открываем приложение, а не карточку: до брони контактов всё равно
        # нет, а глубокая ссылка на чужой уже занятый заказ только раздражает
        "url": "/app/courier",
    })
    return True


def notify_new_order(order: Dict[str, Any]) -> bool:
    """Новый свободный заказ в городе — всем активным курьерам города."""
    when = order.get("delivery_time_from") or "времени нет"
    return _notify(
        order, ds.EVENT_NEW_ORDER,
        "Новый заказ",
        f"{_short_address(order.get('address_text'))} к {when}",
        ds.courier_user_ids(order.get("city")),
    )


def notify_ready(order: Dict[str, Any], courier_user_id: int) -> bool:
    """Забронированный заказ собрали — тому, кто его взял."""
    return _notify(
        order, ds.EVENT_READY,
        "Заказ готов",
        f"Можно забирать: {order.get('site_name') or 'салон'}",
        [courier_user_id],
    )


def notify_claim_expiring(order: Dict[str, Any], courier_user_id: int) -> bool:
    """Бронь скоро сгорит — подтвердите, что едете."""
    return _notify(
        order, ds.EVENT_CLAIM_EXPIRING,
        "Бронь скоро снимется",
        f"Подтвердите, что едете за заказом на {_short_address(order.get('address_text'))}",
        [courier_user_id],
    )


def notify_claim_released(order: Dict[str, Any], courier_user_id: int,
                          reason: str) -> bool:
    """
    Бронь сняли. Причина в тексте: «сгорела» и «заказ отдали Яндексу» —
    разные новости и разные действия курьера.
    """
    texts = {
        ds.RELEASE_EXPIRED: "Бронь снята: вы не отметили, что забрали заказ",
        ds.RELEASE_OUTSOURCED: "Заказ передали службе доставки",
        ds.RELEASE_ORDER_GONE: "Заказ отозван",
        ds.RELEASE_ADMIN: "Бронь снял управляющий",
    }
    event = (ds.EVENT_ORDER_GONE if reason in (ds.RELEASE_OUTSOURCED, ds.RELEASE_ORDER_GONE)
             else ds.EVENT_CLAIM_RELEASED)
    return _notify(
        order, event,
        "Заказ больше не ваш",
        texts.get(reason, "Бронь снята"),
        [courier_user_id],
    )
