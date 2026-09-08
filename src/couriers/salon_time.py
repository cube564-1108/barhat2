"""
Время салона против времени сервера (Фаза 1 плана «Курьеры: доставка заказов»).

Здесь только арифметика над временем: ни обращений к базе, ни запросов наружу.
Отдельным модулем — потому что этот код отвечает на вопрос «когда сгорит бронь»,
и ошибка в нём выглядит не как исключение, а как заказ, отданный обратно в общий
список на два часа раньше, чем нужно.

Три шкалы, которые нельзя путать:

1. **Стенные часы салона.** Время доставки и готовности в RetailCRM менеджер
   вводит так, как их видит флорист: «14:00» в Екатеринбурге и «14:00» в
   Новосибирске — это разные моменты. Разведка 2026-09-05 подтвердила, что
   сдвига между поясами в этих полях нет.
2. **Пояс аккаунта CRM — UTC+7.** В нём приходят `createdAt` заказа и записи
   истории изменений (замер 2026-09-08: `createdAt` опережает UTC на ~6:50).
   К времени доставки это отношения не имеет.
3. **UTC.** В нём живут все наши базы и все отметки времени модуля.

Правило брони («держится, пока до окна доставки больше 60 минут») считается по
первой шкале, а хранится во второй — отсюда весь этот модуль.
"""

from datetime import datetime, timedelta
from typing import Optional

# Пояс аккаунта RetailCRM: в нём приходят createdAt заказов и записи истории.
# Не путать с поясом салона — он свой у каждого города.
CRM_ACCOUNT_UTC_OFFSET = 7

# За сколько минут до начала окна доставки сгорает бронь, если курьер так и не
# отметил «Забрал» (решение владельца 2026-09-08).
CLAIM_LEAD_MINUTES = 60

# За сколько минут до сгорания курьеру уходит пуш «подтвердите, что едете».
CLAIM_WARN_MINUTES = 15


class TimezoneUnknownError(Exception):
    """У салона не задан часовой пояс — считать сроки по нему нельзя."""


def parse_local(delivery_date: str, time_text: Optional[str],
                fallback_time: str = "23:59") -> datetime:
    """
    «2026-09-08» + «14:00» → datetime в стенных часах салона.

    fallback_time применяется, когда времени нет или оно не разобрано («уточ»,
    «Ждем уточнений» — около 1% заказов). Конец дня, а не полночь: заказ без
    времени не должен сгореть с утра только потому, что время не заполнили.
    """
    text = (time_text or "").strip()
    hour = minute = None
    if len(text) >= 4 and ":" in text:
        head = text.split(":", 1)
        if head[0].strip().isdigit() and head[1][:2].isdigit():
            hour, minute = int(head[0]), int(head[1][:2])
    if hour is None or not (0 <= hour <= 23 and 0 <= minute <= 59):
        hour, minute = int(fallback_time[:2]), int(fallback_time[3:5])

    day = datetime.strptime(delivery_date, "%Y-%m-%d")
    return day.replace(hour=hour, minute=minute)


def local_to_utc(local: datetime, utc_offset: Optional[int]) -> datetime:
    """Стенные часы салона → UTC. Пояс не задан — считать нечего."""
    if utc_offset is None:
        raise TimezoneUnknownError("У салона не задан часовой пояс")
    return local - timedelta(hours=utc_offset)


def utc_to_local(moment: datetime, utc_offset: Optional[int]) -> datetime:
    """UTC → стенные часы салона."""
    if utc_offset is None:
        raise TimezoneUnknownError("У салона не задан часовой пояс")
    return moment + timedelta(hours=utc_offset)


def crm_time_to_utc(crm_stamp: str) -> datetime:
    """
    Отметка времени из CRM («2026-09-08 10:13:57») → UTC.

    Касается только createdAt заказа и записей истории: они приходят в поясе
    аккаунта. Время доставки через эту функцию пропускать НЕЛЬЗЯ — оно в
    стенных часах салона, и сдвиг на 7 часов превратит утренний заказ во
    вчерашний.
    """
    return datetime.strptime(crm_stamp[:19], "%Y-%m-%d %H:%M:%S") - timedelta(
        hours=CRM_ACCOUNT_UTC_OFFSET)


def claim_expires_at(delivery_date: str, time_from: Optional[str],
                     utc_offset: Optional[int],
                     lead_minutes: int = CLAIM_LEAD_MINUTES) -> datetime:
    """
    Когда сгорит бронь на этот заказ — в UTC.

    Считается от НАЧАЛА окна доставки в стенных часах салона: «пока до окна
    больше 60 минут — держим». Заказ без разобранного времени получает конец
    дня салона (см. parse_local): такой заказ помечается в списке отдельно,
    но не сгорает раньше времени.
    """
    local_start = parse_local(delivery_date, time_from)
    return local_to_utc(local_start, utc_offset) - timedelta(minutes=lead_minutes)


def claim_warn_at(expires_at: datetime,
                  warn_minutes: int = CLAIM_WARN_MINUTES) -> datetime:
    """Когда напомнить курьеру, что бронь скоро сгорит."""
    return expires_at - timedelta(minutes=warn_minutes)


def unclaimed_alert_at(delivery_date: str, time_from: Optional[str],
                       utc_offset: Optional[int],
                       alert_minutes: int) -> datetime:
    """
    Когда звать человека, если заказ так и не взяли, — в UTC.

    Порог свой в каждом городе (решение владельца 2026-09-08): города
    различаются размером и числом курьеров, одно число на сеть будет либо
    шуметь, либо опаздывать.
    """
    local_start = parse_local(delivery_date, time_from)
    return local_to_utc(local_start, utc_offset) - timedelta(minutes=alert_minutes)


def minutes_until(moment: datetime, now: Optional[datetime] = None) -> float:
    """Сколько минут осталось до момента (отрицательное — уже прошло)."""
    return ((moment - (now or datetime.utcnow())).total_seconds()) / 60.0
