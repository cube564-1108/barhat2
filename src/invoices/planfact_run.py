"""
Один прогон синхронизации с ПланФактом: заявки по картам и оплаченные счета.

До 15.09.2026 это были два несвязанных механизма с кнопками на разных вкладках
и разной защитой от повторного запуска. Человек обязан был помнить оба и
понимать, какой что делает: «Синхронизировать» на вкладке «Синхронизация»
разносит счета и не трогает карты, а «Разнести сейчас» на «Рабочих картах» —
наоборот. Полдня рабочего времени ушло на то, чтобы это выяснить опытным путём.

Порядок этапов: сначала карты, потом счета. Карты создают собственные операции
и падают чаще (сопоставление, даты), счета лишь дозаполняют то, что банк уже
загрузил в ПланФакт, — их итог интереснее вторым.

**Оба этапа идут под ОДНИМ локом**, тем же, что берёт почасовой планировщик
(`cards_sync.SYNC_LOCK`). Свой отдельный лок здесь был бы хуже: кнопка и
планировщик перестали бы исключать друг друга и пошли бы разносить одно и то же
параллельно.
"""

import logging
from typing import Any, Dict, Optional

from planfact import quota as planfact_quota

from .cards import try_acquire_sync_lock, renew_sync_lock, release_sync_lock
from . import cards_sync
from .planfact_sync import _run_planfact_sync

logger = logging.getLogger(__name__)

# Тот же лок, что у почасового прогона карт: см. докстроку модуля.
FULL_SYNC_LOCK = cards_sync.SYNC_LOCK
LOCK_TTL_SECONDS = cards_sync.LOCK_TTL_SECONDS


def _empty_cards_result() -> Dict[str, Any]:
    return {"created": [], "exists": [], "failed": [], "candidates": 0}


def run_full_sync(dry_run: bool = False, force: bool = True) -> Dict[str, Any]:
    """
    Оба этапа подряд, под общим локом (берём его здесь, см. докстроку модуля).

    Возвращает:
        {"cards": {...}, "invoices": {...}, "skipped": str|None,
         "quota": {...}}

    force=True по умолчанию: эту функцию зовут с кнопки, а человек жмёт её
    сразу после того, как поправил настройку, и ждать шестичасовой отсрочки по
    падавшим заявкам ему незачем. Планировщик зовёт с force=False.
    """
    if not try_acquire_sync_lock(FULL_SYNC_LOCK, LOCK_TTL_SECONDS):
        return {"cards": _empty_cards_result(), "invoices": None,
                "skipped": "Синхронизация уже идёт", "quota": planfact_quota.snapshot()}

    try:
        return _run_both_stages(dry_run=dry_run, force=force)
    finally:
        release_sync_lock(FULL_SYNC_LOCK)


def _run_both_stages(dry_run: bool, force: bool) -> Dict[str, Any]:
    """Этапы под уже взятым локом. Отдельно от run_full_sync ради читаемости."""
    result: Dict[str, Any] = {"cards": _empty_cards_result(), "invoices": None,
                              "skipped": None, "quota": None}

    # Этап 1 — заявки по картам.
    #
    # Квоту проверяем перед КАЖДЫМ этапом, а не один раз на прогон: лимит
    # месячный, и между этапами он может кончиться — тогда второй этап просто
    # выжжет десяток гарантированных 403 и займёт воркера.
    quota_error = planfact_quota.error_text()
    if quota_error:
        result["skipped"] = quota_error
        result["quota"] = planfact_quota.snapshot()
        logger.warning("Прогон ПланФакта отложен до этапа карт: %s", quota_error)
        return result

    try:
        result["cards"] = cards_sync.run_card_sync(dry_run=dry_run, force=force)
    except Exception as error:
        # Отказ одного этапа не отменяет второй: счета разносятся, даже если
        # у карт не настроено сопоставление. Раньше это были разные кнопки, и
        # объединение не должно делать их судьбу общей.
        logger.exception("Этап разноски карт упал")
        result["cards"] = dict(_empty_cards_result(),
                               error=f"Этап карт не отработал: {type(error).__name__}")

    renew_sync_lock(FULL_SYNC_LOCK, LOCK_TTL_SECONDS)

    # Этап 2 — оплаченные счета.
    quota_error = planfact_quota.error_text()
    if quota_error:
        result["skipped"] = quota_error
        result["quota"] = planfact_quota.snapshot()
        logger.warning("Этап разноски счетов отложен: %s", quota_error)
        return result

    def renew() -> None:
        renew_sync_lock(FULL_SYNC_LOCK, LOCK_TTL_SECONDS)

    try:
        result["invoices"] = _run_planfact_sync(dry_run=dry_run, renew=renew)
    except Exception as error:
        logger.exception("Этап разноски счетов упал")
        result["invoices"] = {"matched": [], "unmatched": [],
                              "error": f"Этап счетов не отработал: {error}"}

    result["quota"] = planfact_quota.snapshot()
    return result


def summarize(result: Dict[str, Any]) -> Dict[str, Optional[int]]:
    """
    Итоги прогона числами — для лога и отчёта в интерфейсе.

    Разноска карт и разноска счетов считают разное, поэтому сводка не
    складывает их в одну цифру: «создано операций» и «разнесено счетов» — это
    разные действия, и человек, увидев сумму, не поймёт, что произошло.
    """
    cards = result.get("cards") or {}
    invoices = result.get("invoices") or {}
    return {
        "cards_created": len(cards.get("created") or []),
        "cards_exists": len(cards.get("exists") or []),
        "cards_failed": len(cards.get("failed") or []),
        "invoices_matched": len(invoices.get("matched") or []),
        "invoices_unmatched": len(invoices.get("unmatched") or []),
    }
