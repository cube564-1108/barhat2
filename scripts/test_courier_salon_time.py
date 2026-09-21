"""
Сторож часового пояса салона (Фаза 1 модуля «Курьеры: доставка заказов»).

Проверяет то, что нельзя увидеть глазами в коде: одинаковое «14:00» в
Екатеринбурге и Новосибирске — это разные моменты, и бронь на них обязана
сгорать в разное время. Без пояса правило «за 60 минут до доставки» едет на
два часа у половины сети.

Запуск: python scripts/test_courier_salon_time.py
Ни база, ни сеть не нужны — здесь только арифметика.
"""

import os
import sys
from datetime import datetime, timedelta

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))

from couriers.salon_time import (TimezoneUnknownError, crm_time_to_utc,
                                 local_to_utc, minutes_until, parse_local,
                                 unclaimed_alert_at, utc_to_local)

# Момент брони задаём явно: срок не может оказаться раньше, чем через
# CLAIM_MIN_HOLD_MINUTES после неё, и без фиксации «сейчас» проверки формулы
# от окна доставки зависели бы от часа запуска.
CLAIMED_AT = datetime(2026, 9, 7, 0, 0)

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"  [ok] {name}")
    else:
        print(f"  [FAIL] {name} {detail}")
        failures.append(name)


print("\n1. Разбор времени доставки в стенных часах салона")

check("«14:00» разобрано",
      parse_local("2026-09-08", "14:00") == datetime(2026, 9, 8, 14, 0))
check("«9:30» без ведущего нуля",
      parse_local("2026-09-08", "9:30") == datetime(2026, 9, 8, 9, 30))
check("пустое время → конец дня, а не полночь",
      parse_local("2026-09-08", None) == datetime(2026, 9, 8, 23, 59))
check("«уточ» → конец дня (1% заказов приходит так)",
      parse_local("2026-09-08", "уточ") == datetime(2026, 9, 8, 23, 59))
check("«25:70» → конец дня, а не сдвиг суток",
      parse_local("2026-09-08", "25:70") == datetime(2026, 9, 8, 23, 59))


print("\n2. Два пояса: одно и то же «14:00» — разные моменты")
# Срока у брони больше нет (21.09.2026), поэтому пояса проверяются на пороге
# «никто не взял» и на прямом переводе — на том, что осталось живым кодом.

ekb = local_to_utc(parse_local("2026-09-08", "14:00"), 5)
nsk = local_to_utc(parse_local("2026-09-08", "14:00"), 7)
check("Екатеринбург: 14:00 местных — это 09:00 UTC",
      ekb == datetime(2026, 9, 8, 9, 0), f"получено {ekb}")
check("Новосибирск: те же 14:00 — это 07:00 UTC",
      nsk == datetime(2026, 9, 8, 7, 0), f"получено {nsk}")
check("между поясами ровно 2 часа разницы",
      (ekb - nsk).total_seconds() == 2 * 3600)

print("\n3. Пояс не задан — считаем честный отказ, а не «наверное, Москва»")

try:
    local_to_utc(parse_local("2026-09-08", "14:00"), None)
    check("отказ при неизвестном поясе", False, "исключения не было")
except TimezoneUnknownError:
    check("отказ при неизвестном поясе", True)

print("\n5. Порог «никто не взял» — свой в каждом городе")

alert_90 = unclaimed_alert_at("2026-09-08", "14:00", 7, alert_minutes=90)
alert_180 = unclaimed_alert_at("2026-09-08", "14:00", 7, alert_minutes=180)
check("порог 90 минут даёт 05:30 UTC",
      alert_90 == datetime(2026, 9, 8, 5, 30), f"получено {alert_90}")
check("больший порог зовёт человека раньше", alert_180 < alert_90)
check("тревога раньше, чем сгорает бронь (иначе звать уже поздно)",
      alert_90 < nsk)

print("\n6. Время CRM (пояс аккаунта UTC+7) — отдельная шкала")

check("createdAt из CRM переводится в UTC минусом 7 часов",
      crm_time_to_utc("2026-09-08 10:13:57") == datetime(2026, 9, 8, 3, 13, 57))
check("время доставки через эту функцию НЕ проходит: "
      "14:00 в Новосибирске это 07:00 UTC, а не 07:00 через crm_time_to_utc",
      local_to_utc(parse_local("2026-09-08", "14:00"), 7) == datetime(2026, 9, 8, 7, 0))

print("\n7. Обратный перевод для показа человеку")

check("UTC → стенные часы салона",
      utc_to_local(datetime(2026, 9, 8, 7, 0), 7) == datetime(2026, 9, 8, 14, 0))
check("туда-обратно без потерь",
      utc_to_local(local_to_utc(datetime(2026, 9, 8, 14, 0), 5), 5)
      == datetime(2026, 9, 8, 14, 0))

print("\n8. Заказ без времени: считаем от конца дня салона")

no_time = parse_local("2026-09-08", None)
check("время не разобрано — берём конец дня, а не полночь",
      no_time == datetime(2026, 9, 8, 23, 59), f"получено {no_time}")
check("это позже, чем у заказа на 14:00",
      no_time > parse_local("2026-09-08", "14:00"))

print("\n9. Расчёта срока брони в модуле больше нет")
# Он убран вместе с автоснятием: правило «сгорает за 60 минут до окна, но
# живёт хотя бы 30 минут» отбирало заказ у курьера раньше, чем тот вообще мог
# нажать «Забрал» (отметку «Заказ готов» ставят в момент начала окна).

import couriers.salon_time as st  # noqa: E402

for gone in ("claim_expires_at", "claim_warn_at", "CLAIM_LEAD_MINUTES",
             "CLAIM_WARN_MINUTES", "CLAIM_MIN_HOLD_MINUTES"):
    check(f"{gone} удалён", not hasattr(st, gone))


print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} — {failures}")
    sys.exit(1)
print("Все проверки пройдены")
