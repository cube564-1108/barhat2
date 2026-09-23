"""
Время салона против времени сервера (Фаза 1 плана «Курьеры: доставка заказов»).

Здесь только арифметика над временем: ни обращений к базе, ни запросов наружу.
Отдельным модулем — потому что этот код отвечает на вопрос «пора ли уже», и
ошибка в нём выглядит не как исключение, а как тревога у управляющего на два
часа раньше времени или её отсутствие там, где заказ реально стоит.

Три шкалы, которые нельзя путать:

1. **Стенные часы салона.** Время доставки и готовности в RetailCRM менеджер
   вводит так, как их видит флорист: «14:00» в Екатеринбурге и «14:00» в
   Новосибирске — это разные моменты. Разведка 2026-09-05 подтвердила, что
   сдвига между поясами в этих полях нет.
2. **Пояс аккаунта CRM — UTC+7.** В нём приходят `createdAt` заказа и записи
   истории изменений (замер 2026-09-08: `createdAt` опережает UTC на ~6:50).
   К времени доставки это отношения не имеет.
3. **UTC.** В нём живут все наши базы и все отметки времени модуля.

Пороги считаются по первой шкале, а хранятся в третьей — отсюда весь этот
модуль.
"""

from datetime import datetime, timedelta
from typing import Optional

# Пояс аккаунта RetailCRM: в нём приходят createdAt заказов и записи истории.
# Не путать с поясом салона — он свой у каждого города.
CRM_ACCOUNT_UTC_OFFSET = 7

# СРОКА У БРОНИ БОЛЬШЕ НЕТ (решение владельца 21.09.2026).
#
# Здесь жили CLAIM_LEAD_MINUTES / CLAIM_WARN_MINUTES / CLAIM_MIN_HOLD_MINUTES и
# функции claim_expires_at / claim_warn_at. Правило «сгорает за 60 минут до
# окна, но держится хотя бы 30 минут после брони» на практике означало: заказ,
# взятый за 45 минут до доставки, сгорал за 15 минут до неё — а забрать его
# курьер всё это время не мог, потому что отметку «Заказ готов» флорист ставит
# в момент начала окна. Модуль отбирал заказ у человека за то, что тому нечего
# было нажать.
#
# Теперь бронь снимает только человек (сам курьер или управляющий), а зависшую
# видно по сигналу «взяли, но не забрали» в «Контроле доставки». Порог того
# сигнала — общий с «никто не взял», см. unclaimed_alert_at ниже.


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


# ---------------------------------------------------------------------------
# «Вовремя или опоздал» — ОДНА формула на весь модуль
# ---------------------------------------------------------------------------
#
# Живёт здесь, а не в том месте, которое считает показатели, потому что мест
# этих два: сводка управляющего (`delivery_metrics`) и вкладка «Аналитика»
# (`analytics.py`). Две копии формулы — это две правды, которые разъедутся на
# первой же правке, и потом не понять, какая верная.
#
# Здесь же — причина, по которой фактическое время нельзя сравнивать с
# интервалом напрямую: `delivered_at` мы пишем в UTC, а `delivery_time_to`
# менеджер вводит в стенных часах салона. Салоны в UTC+5 и UTC+7: сравнение
# «в лоб» показало бы пятичасовое опоздание у каждого заказа Екатеринбурга,
# и выглядело бы это как работающая метрика.


def deadline_utc(delivery_date: str, time_to: Optional[str],
                 utc_offset: Optional[int]) -> Optional[datetime]:
    """
    Конец интервала доставки в UTC. `None` — посчитать нечем.

    Две причины вернуть `None`, и обе означают «неизвестно», а не «успел»:

    - **интервала нет** (`time_to` пуст или записан словами: «уточ», «Ждем
      уточнений» — около 1% заказов);
    - **у салона не задан пояс** (`utc_offset IS NULL` — салон новый).

    Поэтому здесь НЕ используется `parse_local` с его `fallback_time`:
    подстановка «23:59» превратила бы заказ без времени в заказ, доставленный
    точно в срок. Ноль наоборот — заказ, про который мы ничего не знаем,
    получает право считаться успешным.
    """
    if utc_offset is None:
        return None

    text = (time_to or "").strip()
    if len(text) < 4 or ":" not in text:
        return None
    head, tail = text.split(":", 1)
    if not head.strip().isdigit() or not tail[:2].isdigit():
        return None
    hour, minute = int(head), int(tail[:2])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None

    try:
        day = datetime.strptime(delivery_date, "%Y-%m-%d")
    except (ValueError, TypeError):
        return None
    return local_to_utc(day.replace(hour=hour, minute=minute), utc_offset)


def lateness_minutes(delivered_at: Optional[str],
                     deadline: Optional[datetime]) -> Optional[float]:
    """
    На сколько минут опоздали. `0` — вовремя, `None` — посчитать нечем.

    Доставленное РАНЬШЕ интервала — вовремя, а не «минус сорок минут»
    (решение владельца 23.09.2026): опоздание считается только от конца окна,
    и отрицательные значения не должны попадать в среднее, иначе одна ранняя
    доставка компенсирует чужое опоздание.
    """
    if not delivered_at or deadline is None:
        return None
    try:
        delivered = datetime.fromisoformat(str(delivered_at))
    except (ValueError, TypeError):
        return None
    late = (delivered - deadline).total_seconds() / 60.0
    return late if late > 0 else 0.0
