"""
Состояние квоты API ПланФакта.

У тарифа ПланФакта лимит запросов не в секунду, а НА МЕСЯЦ: 2500 штук, сброс
первого числа. Это ≈83 запроса в сутки на всё — на остатки карт, справочники
и разноску. 07.09.2026 квота кончилась к седьмому числу месяца, и с этого
момента ПланФакт отвечал 403 на любой запрос: не работали ни остатки, ни
разноска счетов, ни фоновая разноска трат по картам. В интерфейсе при этом
было написано «ПланФакт не ответил» — то есть ровно то же, что при обрыве
сети, и понять, что кончилась квота, было неоткуда.

Между тем ПланФакт говорит остаток квоты в КАЖДОМ ответе, включая успешный:

    X-Quota-Limit: 2500   X-Quota-Used: 2500
    X-Quota-Remaining: 0  X-Quota-Reset: 1790802000   (unix, момент сброса)

Этот модуль их запоминает, чтобы:
  * показывать остаток квоты в интерфейсе и в /health — до того, как она
    кончится, а не после;
  * отличать «лимит исчерпан» от «сервис не ответил» — это разные новости
    для человека и разные действия;
  * не ходить в API до момента сброса, когда остаток нулевой: каждый такой
    поход всё равно вернёт 403, а стоит времени воркера.

Состояние живёт в памяти процесса, но переживает перезапуск и виден обоим
gunicorn-воркерам: хранилище подключается снаружи через set_persistence()
(в этом проекте — таблица invoice_sync_state, см. invoices/cards.py). Без
хранилища модуль тоже работает — просто в пределах процесса.
"""

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# Как часто перечитывать состояние из хранилища. Воркеров два, и об исчерпании
# квоты первым узнаёт тот, кто сходил в API; второй читает это из общего
# хранилища, а не выясняет своим 403.
RELOAD_SECONDS = 300

# Как часто состояние пишется в хранилище при обычной работе. Каждый ответ
# писать нельзя: /data на Amvera сетевой и медленный, а смена остатка квоты
# на единицу того не стоит. Переход в «исчерпано» и обратно пишется сразу.
SAVE_INTERVAL_SECONDS = 300

# Через сколько после исчерпания тратить ОДИН запрос на перепроверку.
#
# Остаток квоты приходит только в заголовке ответа, а запрос не отправляется,
# пока по сохранённому состоянию квота исчерпана. Получается замкнутый круг:
# расширение тарифа для нас невидимо. 15.09.2026 лимит подняли с 2500 до 50000,
# и модуль всё равно остался бы мёртвым до 1 октября — до момента сброса,
# записанного ещё старым тарифом.
#
# Поэтому исчерпание блокирует не навсегда, а до следующей пробы: раз в 6 часов
# один запрос уходит наружу и перечитывает заголовки. Если тариф расширили —
# модуль оживает сам, без человека. Если квота действительно кончилась — цена
# 4 холостых ответа 403 в сутки вместо сотен.
PROBE_AFTER_SECONDS = 6 * 3600

_lock = threading.Lock()
_state: Dict[str, Any] = {}
_state_saved_at: float = 0.0
_state_loaded_at: float = 0.0

_load_fn: Optional[Callable[[], Optional[str]]] = None
_save_fn: Optional[Callable[[str], None]] = None


def set_persistence(load: Callable[[], Optional[str]],
                    save: Callable[[str], None]) -> None:
    """Подключить хранилище состояния: load() -> json-строка или None, save(json)."""
    global _load_fn, _save_fn, _state_loaded_at
    _load_fn = load
    _save_fn = save
    _state_loaded_at = 0.0


def _parse_int(value: Any) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _utc_text(moment: datetime) -> str:
    """Время в том же виде, что и остальные метки в базе: UTC без таймзоны."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _now_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _seconds_since(text: Any) -> float:
    """
    Сколько секунд прошло с метки UTC. Пустая или нечитаемая метка — «очень
    давно»: состояние без даты означает, что мы не знаем, когда проверяли, и
    проба тут дешевле, чем вечная блокировка по неизвестно чему.
    """
    if not text:
        return float("inf")
    try:
        moment = datetime.strptime(str(text), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return float("inf")
    return (datetime.now(timezone.utc) - moment).total_seconds()


def _load_locked() -> None:
    """Подтянуть состояние из хранилища. Вызывать под _lock."""
    global _state, _state_loaded_at

    _state_loaded_at = time.monotonic()
    if _load_fn is None:
        return
    try:
        raw = _load_fn()
    except Exception:
        logger.exception("Не удалось прочитать состояние квоты ПланФакта")
        return
    if not raw:
        return
    try:
        stored = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("Состояние квоты ПланФакта нечитаемо: %r", raw[:200])
        return
    if not isinstance(stored, dict):
        return

    # Своё состояние новее сохранённого — оставляем своё: мы могли только что
    # сходить в API, а в хранилище лежит ответ соседа минутной давности.
    if (_state.get("checked_at") or "") >= (stored.get("checked_at") or ""):
        return
    _state = stored


def _save_locked(force: bool) -> None:
    """Записать состояние в хранилище. Вызывать под _lock."""
    global _state_saved_at

    if _save_fn is None or not _state:
        return
    if not force and (time.monotonic() - _state_saved_at) < SAVE_INTERVAL_SECONDS:
        return
    try:
        _save_fn(json.dumps(_state, ensure_ascii=False))
        _state_saved_at = time.monotonic()
    except Exception:
        logger.exception("Не удалось сохранить состояние квоты ПланФакта")


def _ensure_fresh_locked() -> None:
    if not _state or (time.monotonic() - _state_loaded_at) >= RELOAD_SECONDS:
        _load_locked()


def record_response(headers: Any, status_code: int = 200, body: str = "") -> None:
    """
    Запомнить остаток квоты из заголовков ответа.

    Зовётся на КАЖДЫЙ ответ, включая ошибочный: заголовки квоты ПланФакт
    отдаёт и с 403, и по ним видно, кончилась ли она.
    """
    global _state

    headers = headers or {}
    limit = _parse_int(headers.get("X-Quota-Limit"))
    used = _parse_int(headers.get("X-Quota-Used"))
    remaining = _parse_int(headers.get("X-Quota-Remaining"))
    reset_unix = _parse_int(headers.get("X-Quota-Reset"))

    # 403 с текстом про лимит — исчерпание даже там, где заголовков не будет.
    body_text = (body or "").lower()
    said_exhausted = status_code == 403 and (
        "лимит запросов" in body_text or "quota" in body_text
    )
    if limit is None and used is None and remaining is None and not said_exhausted:
        return

    exhausted = bool(said_exhausted or (remaining is not None and remaining <= 0))

    with _lock:
        was_exhausted = bool(_state.get("exhausted"))
        state: Dict[str, Any] = dict(_state)
        state["checked_at"] = _now_text()
        state["exhausted"] = exhausted
        if not exhausted:
            # Отметка о пробе живёт только внутри исчерпания. Иначе после
            # возврата к нормальной работе она осталась бы просроченной, и
            # следующее реальное исчерпание тут же потратило бы холостую пробу.
            state.pop("probed_at", None)
        if limit is not None:
            state["limit"] = limit
        if used is not None:
            state["used"] = used
        if remaining is not None:
            state["remaining"] = remaining
        if reset_unix is not None:
            state["reset_at"] = _utc_text(datetime.fromtimestamp(reset_unix, timezone.utc))
        _state = state
        _save_locked(force=(exhausted != was_exhausted))

    if exhausted and not was_exhausted:
        logger.error(
            "Квота API ПланФакта исчерпана: использовано %s из %s, сброс %s (UTC)",
            _state.get("used"), _state.get("limit"), _state.get("reset_at"),
        )


def snapshot(reload: bool = False) -> Dict[str, Any]:
    """
    Текущее состояние квоты для интерфейса и /health.

    reload=True — перечитать из хранилища принудительно (для /health: его
    воркер мог не делать ни одного запроса в ПланФакт).
    """
    with _lock:
        if reload:
            _load_locked()
        else:
            _ensure_fresh_locked()
        state = dict(_state)

    if not state:
        return {"known": False}

    state["known"] = True
    state["blocked"] = _is_blocked(state)
    if state["blocked"]:
        # Пробу диагностика не расходует — только показывает, когда сама
        # система перепроверит лимит. Без этого «исчерпано» выглядит как
        # состояние до конца месяца, хотя расширение тарифа подхватится раньше.
        state["next_probe_at"] = _next_probe_text(state)
    return state


def _is_blocked(state: Dict[str, Any]) -> bool:
    """Исчерпана ли квота ПРЯМО СЕЙЧАС (момент сброса мог уже наступить)."""
    if not state.get("exhausted"):
        return False
    reset_at = state.get("reset_at")
    if reset_at and reset_at <= _now_text():
        return False
    return True


def _next_probe_text(state: Dict[str, Any]) -> Optional[str]:
    """Когда состоится следующая проба — для /health и интерфейса, UTC."""
    last = state.get("probed_at") or state.get("checked_at")
    try:
        moment = datetime.strptime(str(last), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return _utc_text(moment + timedelta(seconds=PROBE_AFTER_SECONDS))


def _probe_due(state: Dict[str, Any]) -> bool:
    """Пора ли потратить один запрос на перепроверку исчерпанной квоты."""
    return _seconds_since(state.get("probed_at") or state.get("checked_at")) >= PROBE_AFTER_SECONDS


def _mark_probe_locked() -> None:
    """
    Отметить, что проба назначена. Вызывать под _lock.

    Отметка отдельным полем, а не через checked_at: checked_at отвечает на
    вопрос «когда мы в последний раз ЗНАЛИ остаток», и врать в нём нельзя —
    его показывает интерфейс. Пишется сразу (force), иначе проба повторится на
    каждом запросе, если ПланФакт не ответит вовсе и record_response не будет
    вызван.

    Воркеров два, и второй перечитает состояние не сразу (RELOAD_SECONDS),
    поэтому на одно окно может прийтись две пробы вместо одной. Это осознанно:
    лишний запрос раз в 6 часов дешевле, чем блокировка на общем локе.
    """
    global _state

    state = dict(_state)
    state["probed_at"] = _now_text()
    _state = state
    _save_locked(force=True)
    logger.info(
        "Квота ПланФакта числится исчерпанной с %s — тратим один запрос на перепроверку: тариф мог измениться",
        state.get("checked_at"),
    )


def _blocked_locked(consume_probe: bool) -> bool:
    """
    Нельзя ли ходить в API. Вызывать под _lock.

    consume_probe=True — вызов действительно решает, идти ли наружу, и имеет
    право израсходовать пробу. False — вызов только формулирует текст ошибки,
    пробу назначать не за что.
    """
    if not _is_blocked(_state):
        return False
    if not _probe_due(_state):
        return True
    if consume_probe:
        _mark_probe_locked()
    return False


def is_blocked() -> bool:
    """Нельзя ли ходить в API: квота исчерпана, сброс не наступил и проба не назначена."""
    with _lock:
        _ensure_fresh_locked()
        return _blocked_locked(consume_probe=True)


def error_text() -> Optional[str]:
    """
    Текст для человека, если ходить в ПланФакт нельзя. Дату сброса сюда не
    вписываем: время показывается по поясу устройства, это делает интерфейс
    из поля reset_at (см. DESIGN-SPEC и BarhatTime).

    Пробу этот вызов не расходует (consume_probe=False): он стоит ПЕРЕД походом
    наружу в нескольких модулях (cards, planfact_refs, cards_sync) и служит там
    ранним выходом, а сам в API не ходит. Расходует пробу только is_blocked() в
    client.request() — единственное место, за которым сразу идёт отправка.
    Иначе проба сгорала бы здесь, и запрос всё равно не уходил.
    """
    with _lock:
        _ensure_fresh_locked()
        if not _blocked_locked(consume_probe=False):
            return None
        used, limit = _state.get("used"), _state.get("limit")
    if used is not None and limit is not None:
        return f"Исчерпан месячный лимит запросов к API ПланФакта ({used} из {limit})"
    return "Исчерпан месячный лимит запросов к API ПланФакта"


def reset_for_tests() -> None:
    """Сбросить состояние (только для тестов)."""
    global _state, _state_saved_at, _state_loaded_at
    with _lock:
        _state = {}
        _state_saved_at = 0.0
        _state_loaded_at = 0.0
