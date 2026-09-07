"""
Кэш справочников ПланФакта (счета, проекты, статьи расходов).

Эти три списка нужны только выпадающим спискам в настройках: счёт карты,
проект под салон, статья под категорию расхода. Меняются они раз в месяц, а
грузились при каждом открытии вкладки — «Карты» тянула счета, «Сопоставление»
разом проекты и статьи. В памяти воркера жил TTL на две минуты, то есть на
одно открытие; для соседнего воркера кэша не существовало вовсе.

Цена этому выяснилась 07.09.2026: лимит API ПланФакта не посекундный, а
МЕСЯЧНЫЙ — 2500 запросов, ≈83 в сутки на всё. Справочники в таком бюджете —
заметная доля, и тратить её на список, который не меняется, нельзя.

Поэтому кэш здесь — в базе, а не в памяти: он общий для воркеров, переживает
деплой и живёт 6 часов. Устаревший список всё равно отдаётся (с пометкой):
пустой выпадающий список хуже вчерашнего.
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from planfact import quota as planfact_quota

from .cards import try_acquire_sync_lock, release_sync_lock
from .storage import get_db

logger = logging.getLogger(__name__)

# Справочник меняется, когда человек заводит счёт или статью — это событие
# раз в месяц. Шесть часов означают максимум 4 запроса в сутки на справочник
# вместо одного на каждое открытие вкладки.
REFERENCE_TTL_SECONDS = 6 * 3600

# Насколько часто кнопка «Обновить справочник» вправе ходить наружу.
REFRESH_MIN_INTERVAL_SECONDS = 600

_LOCK_PREFIX = "planfact_ref_"


def _now_text() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def _threshold(seconds: int) -> str:
    return (datetime.utcnow() - timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")


def _read_cache(key: str) -> Optional[Dict[str, Any]]:
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT payload, fetched_at FROM planfact_reference_cache WHERE key = ?", (key,)
        ).fetchone()
    except Exception:
        logger.exception("Не удалось прочитать кэш справочника %s", key)
        return None
    finally:
        conn.close()

    if not row:
        return None
    try:
        items = json.loads(row["payload"])
    except (TypeError, ValueError):
        logger.warning("Кэш справочника %s нечитаем — перечитаем из ПланФакта", key)
        return None
    if not isinstance(items, list):
        return None
    return {"items": items, "fetched_at": row["fetched_at"]}


def _save_cache(key: str, items: List[Any]) -> None:
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO planfact_reference_cache (key, payload, fetched_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET payload = excluded.payload, fetched_at = excluded.fetched_at",
            (key, json.dumps(items, ensure_ascii=False), _now_text()),
        )
        conn.commit()
    except Exception:
        logger.exception("Не удалось сохранить кэш справочника %s", key)
    finally:
        conn.close()


def get_reference(key: str, fetch: Callable[[], Optional[List[Any]]],
                  refresh: bool = False) -> Dict[str, Any]:
    """
    Справочник из кэша, а при устаревании — из ПланФакта.

    Возвращает {"items", "fetched_at", "stale", "error", "quota"}. items = None
    только если кэша нет и сходить не удалось — тогда в error лежит причина
    (исчерпанная квота названа прямо, а не как «не ответил»).
    """
    cached = _read_cache(key)
    quota_state = planfact_quota.snapshot()

    fresh_enough = bool(cached and cached["fetched_at"] > _threshold(REFERENCE_TTL_SECONDS))
    if refresh and cached and cached["fetched_at"] > _threshold(REFRESH_MIN_INTERVAL_SECONDS):
        refresh = False       # антидребезг кнопки, как у остатков карт
    if fresh_enough and not refresh:
        return {"items": cached["items"], "fetched_at": cached["fetched_at"],
                "stale": False, "error": None, "quota": quota_state}

    quota_error = planfact_quota.error_text()
    if quota_error:
        return _stale_result(cached, quota_error)

    lock = _LOCK_PREFIX + key
    if not try_acquire_sync_lock(lock, ttl_seconds=60):
        # Сосед уже пошёл за тем же списком — второй раз квоту не тратим
        return _stale_result(cached, None)

    try:
        items = fetch()
    except Exception as error:
        logger.exception("Справочник %s из ПланФакта не получен", key)
        return _stale_result(cached, f"{type(error).__name__}: {error}")
    finally:
        release_sync_lock(lock)

    if items is None:
        return _stale_result(cached, planfact_quota.error_text() or "ПланФакт не ответил")

    _save_cache(key, items)
    return {"items": items, "fetched_at": _now_text(), "stale": False,
            "error": None, "quota": planfact_quota.snapshot()}


def _stale_result(cached: Optional[Dict[str, Any]], error: Optional[str]) -> Dict[str, Any]:
    return {
        "items": cached["items"] if cached else None,
        "fetched_at": cached["fetched_at"] if cached else None,
        "stale": bool(cached),
        "error": error,
        "quota": planfact_quota.snapshot(),
    }
