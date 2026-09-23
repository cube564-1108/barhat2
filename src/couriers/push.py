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

import base64
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

# СКОЛЬКО PUSH-СЕРВИС ХРАНИТ СООБЩЕНИЕ, ПОКА ТЕЛЕФОН НЕДОСТУПЕН.
#
# У pywebpush умолчание — `ttl=0`, и это не «без ограничения», а «доставить
# ТОЛЬКО ЕСЛИ устройство на связи прямо сейчас; иначе выбросить». Телефон
# курьера лежит в кармане с погашенным экраном, Android держит соединение с
# FCM усыплённым (Doze) — и уведомление о новом заказе выбрасывалось, не
# доходя. При этом FCM отвечает «принято», ошибки нет нигде: наша диагностика
# честно показывала `sent: 1`.
#
# Так это и выглядело 18.09.2026: пробное уведомление приходило всегда (его
# жмут, держа телефон в руке, с открытым приложением), а о заказах — никогда.
#
# Значения — по сроку жизни самого события, а не «побольше на всякий случай»:
# протухшее уведомление о заказе, который давно увезли, хуже молчания.
PUSH_TTL_SECONDS = {
    "new_order": 2 * 3600,      # заказ можно взять, пока он свободен
    "ready": 2 * 3600,          # «собрали, забирай» — столько же
    "claim_released": 3600,
    "test": 300,                # проверка «здесь и сейчас»
}
DEFAULT_PUSH_TTL = 3600

# Urgency по RFC 8030: `high` разрешает push-сервису будить устройство в
# энергосберегающем режиме. Для «новый заказ» это и есть смысл уведомления.
PUSH_URGENCY = "high"


def is_configured() -> bool:
    """Настроены ли ключи. Без них модуль молчит, а не падает."""
    return bool(VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY)


def _b64url_decode(value: str) -> bytes:
    """base64url без padding — формат, в котором ключи VAPID живут везде."""
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


# Готовый ключ считаем один раз на процесс: он не меняется, а сборка EC-ключа
# и кодирование в PEM — не бесплатны, и делать это на каждый пуш незачем.
_private_key_pem: Optional[str] = None
_private_key_error: Optional[str] = None


def private_key_pem() -> Optional[str]:
    """
    Приватный VAPID-ключ в PEM — единственном формате, который понимают ВСЕ
    версии py_vapid.

    ЗАЧЕМ ЭТО НУЖНО. `scripts/generate_vapid_keys.py` печатает приватный ключ
    как base64url от 32 сырых байт — это корректный «raw» формат VAPID, его
    ждут браузеры и выдают все генераторы ключей. Но py_vapid в установленной
    версии на такую строку зовёт разбор DER, и `cryptography` отвечает:

        ValueError: Could not deserialize key data ... ASN.1 parsing error:
        invalid length

    Наружу это выходило как «пробное уведомление не дошло, проверьте настройки
    телефона» — при полностью исправном телефоне (18.09.2026). Ни один пуш не
    отправлялся вообще.

    Поэтому формат приводим сами, а не полагаемся на разбор строки чужой
    библиотекой: PEM опознаётся по заголовку `-----BEGIN` однозначно и во всех
    версиях. Ключи в `.env` при этом менять НЕ надо — публичный остаётся тем
    же, и выданные подписки остаются действительными.
    """
    global _private_key_pem, _private_key_error
    if _private_key_pem or _private_key_error:
        return _private_key_pem
    if not VAPID_PRIVATE_KEY:
        _private_key_error = "ключ не задан"
        return None

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    def to_pem(key) -> str:
        return key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("ascii")

    # Ключ вводит человек, копируя из вывода скрипта или из чужого генератора,
    # и доезжает он в четырёх разных видах. Перебираем все, а не угадываем
    # один: цена ошибки — полностью мёртвые уведомления при внешне исправной
    # настройке (18.09.2026).
    #
    # Переносы строк в .env не выживают, поэтому PEM оттуда приходит либо
    # одной строкой с литеральными «\n», либо со срезанными переносами.
    text = VAPID_PRIVATE_KEY.strip().strip('"').strip("'")
    attempts = []

    if "-----BEGIN" in text or "BEGIN " in text:
        attempts.append(("pem", lambda: serialization.load_pem_private_key(
            text.replace("\\n", "\n").encode("ascii"), password=None)))

    def from_b64(data: bytes):
        if len(data) == 32:
            # Сырое скалярное значение приватного ключа P-256 — то, что
            # печатает наш scripts/generate_vapid_keys.py
            return ec.derive_private_key(int.from_bytes(data, "big"), ec.SECP256R1())
        return serialization.load_der_private_key(data, password=None)

    try:
        decoded = _b64url_decode(text)
    except Exception:
        decoded = None

    if decoded is not None:
        attempts.append(("base64", lambda: from_b64(decoded)))
        # Бывает и base64 от целого PEM-файла
        attempts.append(("base64-pem", lambda: serialization.load_pem_private_key(
            decoded, password=None)))

    errors = []
    for name, load in attempts:
        try:
            _private_key_pem = to_pem(load())
            logger.info("VAPID: приватный ключ принят как %s", name)
            return _private_key_pem
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__}")

    _private_key_error = ("ни один формат не подошёл ("
                          + ", ".join(errors) + ")") if errors else "пустой ключ"
    logger.error("VAPID: приватный ключ не читается — %s", _private_key_error)
    return None


_vapid_object = None
_vapid_object_error: Optional[str] = None
_vapid_object_how: Optional[str] = None


def vapid_key():
    """
    Готовый объект `Vapid` для pywebpush — вместо строки.

    ЗАЧЕМ. pywebpush разбирает переданную строку сам: `Vapid.from_string()`.
    Наш PKCS8 PEM он не принимает — прод 18.09.2026 ответил
    «Could not deserialize key data ... ASN.1 parsing error», хотя тот же PEM
    прекрасно читается `cryptography`. То есть спор двух разборов, в котором
    наши ключи ни при чём: они верные, `pair_matches: true`.

    Отдавая готовый объект, мы убираем чужой разбор строки из цепочки целиком:
    pywebpush проверяет `isinstance(..., Vapid01)` и берёт объект как есть.

    Способы перебираются, а не угадываются: у py_vapid разные версии ждут
    разного, и сломаться это должно один раз, а не при каждом обновлении.
    Сработавший способ пишется в лог и виден в диагностике.
    """
    global _vapid_object, _vapid_object_error, _vapid_object_how
    if _vapid_object is not None or _vapid_object_error:
        return _vapid_object

    pem = private_key_pem()
    if not pem:
        _vapid_object_error = "приватный ключ не читается"
        return None

    try:
        from py_vapid import Vapid01
    except ImportError as e:
        _vapid_object_error = f"py_vapid недоступен: {e}"
        return None

    from cryptography.hazmat.primitives import serialization

    def loaded():
        return serialization.load_pem_private_key(pem.encode("ascii"), password=None)

    attempts = [
        # Прямая передача ключа cryptography — то, чем объект и является
        ("constructor", lambda: Vapid01(loaded())),
        ("from_pem", lambda: Vapid01.from_pem(pem.encode("ascii"))),
        # Сырой base64url — ровно то, что лежит у нас в .env
        ("from_raw", lambda: Vapid01.from_raw(VAPID_PRIVATE_KEY.strip().encode("ascii"))),
    ]

    errors = []
    for name, make in attempts:
        try:
            obj = make()
            # Объект обязан уметь подписывать: конструктор может принять что
            # угодно и промолчать, а упадёт это уже на отправке.
            obj.sign({"aud": "https://example.com", "sub": VAPID_CONTACT})
            _vapid_object, _vapid_object_how = obj, name
            logger.info("VAPID: объект ключа собран через %s", name)
            return _vapid_object
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__}")

    _vapid_object_error = "; ".join(errors)
    logger.error("VAPID: объект ключа собрать не удалось — %s", _vapid_object_error)
    return None


def key_health() -> Dict[str, Any]:
    """
    Читается ли приватный ключ и пара ли он публичному.

    Обе беды молчаливые: при нечитаемом ключе не уходит ни один пуш, при
    несовпадении пары push-сервис отвергает КАЖДУЮ отправку — а выясняется это
    только с телефона курьера. Здесь оно видно снаружи, без телефона.
    Сами ключи наружу не отдаются.
    """
    if not is_configured():
        return {"configured": False}

    pem = private_key_pem()
    info: Dict[str, Any] = {"configured": True,
                            "private_readable": bool(pem),
                            "error": _private_key_error}
    if not pem:
        # Форма значения, а не само значение. Восстановить ключ по длине
        # нельзя, а понять, что человек положил в .env, — можно: без этого
        # разбор упирается в «не читается» и дальше некуда, потому что читать
        # сам `.env` правила проекта запрещают.
        text = VAPID_PRIVATE_KEY.strip()
        try:
            decoded_len = len(_b64url_decode(text.strip('"').strip("'")))
        except Exception:
            decoded_len = None
        info["shape"] = {
            "chars": len(text),
            "decoded_bytes": decoded_len,
            "has_pem_header": "BEGIN" in text,
            "has_escaped_newline": "\\n" in text,
            "has_whitespace": any(c.isspace() for c in text),
            "has_quotes": text[:1] in ('"', "'") or text[-1:] in ('"', "'"),
            "expected": "32 байта после base64url — так печатает "
                        "scripts/generate_vapid_keys.py",
        }
        return info

    try:
        from cryptography.hazmat.primitives import serialization
        key = serialization.load_pem_private_key(pem.encode("ascii"), password=None)
        numbers = key.public_key().public_numbers()
        derived = (b"\x04" + numbers.x.to_bytes(32, "big")
                   + numbers.y.to_bytes(32, "big"))
        expected = base64.urlsafe_b64encode(derived).decode("ascii").rstrip("=")
        # Пара или нет — это ответ «да/нет», сам ключ показывать незачем
        info["pair_matches"] = (expected == VAPID_PUBLIC_KEY.strip())
    except Exception as e:
        info["pair_matches"] = None
        info["error"] = f"{type(e).__name__}: {e}"

    # Наш разбор и разбор библиотеки — разные вещи, и они разъезжаются.
    # 18.09.2026 ключ читался у нас и падал в py_vapid: снаружи это выглядело
    # как «ключ в порядке, а уведомления не идут». Спрашиваем саму библиотеку.
    try:
        from py_vapid import Vapid
        Vapid.from_string(pem)
        info["accepted_by_library"] = True
    except ImportError:
        info["accepted_by_library"] = None   # библиотеки нет — см. no_library
    except Exception as e:
        info["accepted_by_library"] = False
        info["library_error"] = f"{type(e).__name__}: {e}"[:200]

    # Разбор строки библиотекой мы обходим: отдаём готовый объект. Здесь видно,
    # собрался ли он и каким способом — именно он и работает на отправке.
    info["object_built"] = vapid_key() is not None
    info["object_how"] = _vapid_object_how
    if not info["object_built"]:
        info["object_error"] = _vapid_object_error
    return info


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
    должно: квартира и код домофона остаются в приложении.

    Сокращение здесь живёт только ради экрана блокировки — в самом приложении
    адрес с 23.09.2026 показывается целиком (см. `PUBLIC_ORDER_FIELDS`). И
    режется оно по СМЫСЛУ частей (регион и квартира — отдельными правилами
    выше), а не по их номеру: порядок частей в строке CRM не обещан никем.
    """
    if not address:
        return "адрес уточняется"

    parts = [part.strip() for part in str(address).split(",") if part.strip()]
    useful = [p for p in parts
              if not _PRIVATE_ADDRESS_PARTS.search(p) and not _ADMIN_ADDRESS_PARTS.search(p)]
    if not useful:
        return "адрес уточняется"
    return ", ".join(useful[-2:])


def send_to_users(user_ids: List[int], payload: Dict[str, Any],
                  event_type: Optional[str] = None) -> Dict[str, int]:
    """
    Отправить уведомление устройствам этих пользователей.

    Возвращает счётчики и — обязательно — ПРИЧИНУ, когда ничего не ушло.

    Раньше здесь были только счётчики, и три совершенно разных случая давали
    одинаковый ноль: библиотеки нет, подписок нет, push-сервис отказал. Наверх
    уходило безликое «не дошло», а объяснение — в лог, которого у нас нет
    (правило CLAUDE.md про «подробности в логах сервера»). 18.09.2026 на этом
    встал разбор: пробное не доходило, а сказать почему было нечем.

    `reason` — короткий код для ветвления, `detail` — то, что сказал сам
    push-сервис, вместе с HTTP-статусом: по нему ищут в документации.
    """
    result = {"sent": 0, "failed": 0, "dropped": 0,
              "reason": None, "detail": None, "status": None}
    if not is_configured():
        result["reason"] = "not_configured"
        return result

    subscriptions = ds.push_subscriptions_for(user_ids)
    if not subscriptions:
        result["reason"] = "no_subscriptions"
        return result

    # Ключ проверяем ПЕРВЫМ — раньше чужой библиотеки.
    #
    # Это наша собственная конфигурация, проверка дешёвая, и именно она чаще
    # всего и сломана. Нечитаемый ключ роняет каждую отправку одинаково, и
    # перебирать из-за него подписки бессмысленно: наружу уйдёт «не дошло»
    # вместо «ключ не читается», а это разные места починки.
    key_pem = private_key_pem()
    if not key_pem:
        result["reason"] = "bad_key"
        result["detail"] = _private_key_error
        return result

    # Готовый объект ключа вместо строки: разбор строки внутри pywebpush наш
    # PEM не принимает (см. vapid_key). Если объект собрать не вышло — шлём
    # строкой, хуже уже не будет.
    vapid_obj = vapid_key()

    try:
        from pywebpush import WebPushException, webpush
    except ImportError as e:
        # Отдельная причина, а не «не дошло»: чинит это администратор
        # пересборкой, и перебирать настройки телефона тут бесполезно.
        logger.warning("pywebpush не установлен — пуши не отправляются")
        result["reason"] = "no_library"
        result["detail"] = str(e)
        return result

    # Срок жизни — по типу события. Тег payload сюда не годится: он
    # уникален на заказ, а срок общий для всех уведомлений одного вида.
    ttl = PUSH_TTL_SECONDS.get(event_type or payload.get("tag"), DEFAULT_PUSH_TTL)

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
                vapid_private_key=vapid_obj or key_pem,
                vapid_claims={"sub": VAPID_CONTACT},
                timeout=PUSH_TIMEOUT_SECONDS,
                # Без ttl pywebpush ставит 0 — «доставить только если телефон
                # на связи сию секунду, иначе выбросить». См. PUSH_TTL_SECONDS.
                ttl=ttl,
                headers={"Urgency": PUSH_URGENCY},
            )
            ds.mark_push_ok(subscription["endpoint"])
            result["sent"] += 1
        except WebPushException as e:
            # 410 Gone / 404 — подписки больше нет. Держать её значит копить
            # очередь и тратить время тика на заведомо мёртвый адрес.
            response = getattr(e, "response", None)
            status = getattr(response, "status_code", None)
            drop = status in (404, 410)
            ds.mark_push_failed(subscription["endpoint"], drop=drop)
            result["dropped" if drop else "failed"] += 1
            result["reason"] = "expired" if drop else "rejected"
            result["status"] = status
            # Тело ответа — это и есть объяснение от FCM/Mozilla/Apple.
            # Режем: в него попадает эхо заголовков, а читать это человеку.
            body_text = getattr(response, "text", "") or str(e)
            result["detail"] = str(body_text)[:300]
        except Exception as e:
            ds.mark_push_failed(subscription["endpoint"])
            result["failed"] += 1
            result["reason"] = "error"
            # Текста исключения мало: «Could not deserialize key data» одинаково
            # звучит и про VAPID-ключ, и про ключи подписки браузера, а это
            # разные поломки. Последние кадры стека называют место — имя файла
            # и функцию, без наших данных и без секретов (18.09.2026).
            import traceback
            frames = traceback.extract_tb(e.__traceback__)[-3:]
            where = " < ".join(f"{f.filename.split('/')[-1]}:{f.name}"
                               for f in reversed(frames))
            result["detail"] = f"{type(e).__name__}: {e}"[:220] + f" [{where}]"
            logger.warning(f"Push не ушёл: {e}", exc_info=True)

    # Хоть одно устройство получило — это успех, а не отказ
    if result["sent"]:
        result["reason"] = None
        result["detail"] = None
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
    result = send_to_users(user_ids, {
        "title": "Уведомления включены",
        "body": "Так будет выглядеть сообщение о новом заказе.",
        # Свой тег: пробное не должно затирать настоящее уведомление о заказе
        "tag": "test",
        "url": "/app/courier",
    })

    # Итог пробной отправки живёт в базе: экран его покажет один раз, а вопрос
    # «почему не приходят» задают позже и уже без этого экрана.
    try:
        from .delivery_feed import PUSH_TEST_KEY
        from .storage import set_sync_state
        set_sync_state(PUSH_TEST_KEY, json.dumps(
            {"at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
             "sent": result.get("sent"), "reason": result.get("reason"),
             "status": result.get("status"), "detail": result.get("detail")},
            ensure_ascii=False))
    except Exception as e:
        logger.warning(f"Пробное уведомление: отметку записать не удалось — {e}")

    return result


PUSH_LOG_LIMIT = 20


def _log_send(order: Dict[str, Any], event_type: str,
              user_ids: List[int], result: Dict[str, Any]) -> None:
    """
    Журнал последних отправок: что, когда, скольким и чем кончилось.

    Отметка о ПРОГОНЕ отвечает «упал ли тик», но не отвечает на главный
    вопрос: «моё уведомление по заказу ушло или нет». Между тиками ничего не
    остаётся, и отправка, случившаяся двадцать минут назад, невидима — а
    именно о ней и спрашивают (18.09.2026, «по новым заказам пуши не
    приходят»).

    Номер заказа кладём: журнал уходит в админскую ручку. В публичный
    `/health` попадает та же запись без номера — см. `why_silent()`.
    """
    from .delivery_feed import PUSH_LOG_KEY
    from .storage import get_sync_state, set_sync_state

    entry = {
        "at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        "order": order.get("order_number") or order.get("retailcrm_order_id"),
        "event": event_type,
        "city": order.get("city"),
        "recipients": len(user_ids),
        "sent": result.get("sent", 0),
        "failed": result.get("failed", 0),
        "dropped": result.get("dropped", 0),
        "reason": result.get("reason"),
        "detail": (result.get("detail") or None),
    }
    try:
        raw = get_sync_state(PUSH_LOG_KEY)
        log = json.loads(raw) if raw else []
        if not isinstance(log, list):
            log = []
    except Exception:
        log = []

    log.append(entry)
    set_sync_state(PUSH_LOG_KEY, json.dumps(log[-PUSH_LOG_LIMIT:], ensure_ascii=False))


# Поля журнала, которым не место в публичном ответе.
#
# `order` и `city` — данные компании. `detail` опаснее: туда попадает текст,
# который вернул push-сервис, а он включает АДРЕС УСТРОЙСТВА (endpoint) —
# идентификатор конкретного телефона курьера. `/health` доступен без входа,
# и светить там endpoint'ы нельзя. Код отказа и причина остаются: понять
# «что за отказ» по ним можно, а найти по ним человека — нет.
PRIVATE_LOG_FIELDS = ("order", "city", "detail")


def _without(entry, *fields):
    """Копия записи без перечисленных полей. `None` остаётся `None`."""
    if not isinstance(entry, dict):
        return entry
    return {k: v for k, v in entry.items() if k not in fields}


def recent_sends(with_orders: bool = False) -> List[Dict[str, Any]]:
    """
    Последние отправки.

    `with_orders=True` — полная запись, только для админской ручки. По
    умолчанию — без номера заказа, города и текста отказа (см.
    `PRIVATE_LOG_FIELDS`): этот вид уходит в публичный `/health?full=1`.
    """
    from .delivery_feed import PUSH_LOG_KEY
    from .storage import get_sync_state

    try:
        log = json.loads(get_sync_state(PUSH_LOG_KEY) or "[]")
    except Exception:
        return []
    if not isinstance(log, list):
        return []
    if with_orders:
        return log
    return [{k: v for k, v in entry.items() if k not in PRIVATE_LOG_FIELDS}
            for entry in log]


def why_silent(full: bool = False) -> Dict[str, Any]:
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
        "feed": _feed_state(full=full),
        "recent_sends": recent_sends(with_orders=full),
        "by_city": by_city,
        "blocked": blocked,
    }


def _feed_state(full: bool = False) -> Dict[str, Any]:
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
    from .delivery_feed import CURSOR_KEY, FEED_LOCK, PUSH_RUN_KEY
    from .storage import get_db as couriers_db

    from .delivery_feed import PUSH_TEST_KEY

    keys = (f"schedule:{FEED_LOCK}", f"lock:{FEED_LOCK}", CURSOR_KEY,
            PUSH_RUN_KEY, PUSH_TEST_KEY)
    try:
        with couriers_db() as conn:
            rows = conn.execute(
                "SELECT key, value, updated_at FROM sync_state "
                f" WHERE key IN ({','.join('?' * len(keys))})", keys).fetchall()
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}

    state = {row["key"]: {"value": row["value"], "updated_at": row["updated_at"]}
             for row in rows}

    # Результат последнего прогона рассылки: сколько ушло и что упало. Именно
    # здесь и был слепой участок — исключение гасилось в лог, которого нет.
    def parsed(key):
        raw = state.get(key, {}).get("value")
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            return raw   # что записалось, то и показываем: строкой лучше, чем ничем

    last_run = parsed(PUSH_RUN_KEY)
    last_test = parsed(PUSH_TEST_KEY)

    # То же правило, что и для журнала отправок (см. PRIVATE_LOG_FIELDS): в
    # `detail` и `error` попадает ответ push-сервиса, а он включает АДРЕС
    # УСТРОЙСТВА курьера. `/health` отдаётся без входа, и держать там endpoint
    # нельзя. Код отказа и причина остаются — по ним понятно, что случилось.
    #
    # Найдено security-review 18.09.2026: журнал я закрыл, а эти две соседние
    # записи — нет. Один и тот же класс, два разных места.
    if not full:
        last_test = _without(last_test, "detail")
        last_run = _without(last_run, "error")

    return {
        # Время в этих полях — UTC, как всё, что пишет планировщик
        "next_tick_not_before": state.get(f"schedule:{FEED_LOCK}", {}).get("value"),
        "lock_until": state.get(f"lock:{FEED_LOCK}", {}).get("value"),
        "cursor": state.get(CURSOR_KEY, {}).get("value"),
        "cursor_updated_at": state.get(CURSOR_KEY, {}).get("updated_at"),
        "last_push_run": last_run,
        "last_push_test": last_test,
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

    result = send_to_users(user_ids, {
        "title": title,
        "body": body,
        "tag": f"{event_type}-{order['retailcrm_order_id']}",
        # Открываем приложение, а не карточку: до брони контактов всё равно
        # нет, а глубокая ссылка на чужой уже занятый заказ только раздражает
        "url": "/app/courier",
    }, event_type=event_type)

    _log_send(order, event_type, user_ids, result)

    # Не ушло никому — возвращаем право, пусть следующий тик попробует снова.
    #
    # Право занимается ДО отправки: иначе два воркера пошлют одно и то же
    # дважды. Обратная сторона — неудачная попытка хоронила уведомление
    # навсегда. 18.09.2026, пока разбирались со сломанным VAPID-ключом, так
    # молча сгорели девять заказов: каждая попытка не доходила И сжигала
    # единственный шанс.
    #
    # Бесконечного повтора не будет: мёртвую подписку push-сервис отзывает
    # (404/410), она удаляется, и следующий заход отсечёт `has_push_subscriptions`.
    if not result.get("sent"):
        ds.release_push_event(order["retailcrm_order_id"], event_type)
        return False
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
