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


def _notify(order: Dict[str, Any], event_type: str, title: str, body: str,
            user_ids: List[int]) -> bool:
    """Одно событие: занять право на отправку и отправить."""
    if not is_configured() or not user_ids:
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
