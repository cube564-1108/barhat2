"""
Разноска заявок по рабочим картам в ПланФакт (план 2026-08-29, Фаза 3).

По рабочим картам ПланФакт не получает ничего сам: карты личные, банк о них
не рассказывает, и до сих пор владелец заводил все операции руками. Значит
этот модуль — единственный источник таких операций, и единственный возможный
источник дублей тоже он: наш собственный повторный прогон.

Отсюда две меры, обе обязательные:
  * признак разноски отдельным полем (`invoices.planfact_synced_at`), а не
    статусом — совмещённый признак на счетах уже приводил к тому, что синк
    молча пропускал заявки навсегда;
  * маркер `[cardexp:123]` / `[topup:123]` в комментарии операции. Искать по
    `externalId` нечем: единственный доступный поиск — `searchString` в
    `POST /operations/list`, и он ищет по тексту. Без маркера падение процесса
    между записью в ПланФакт и записью признака в нашу базу давало бы дубль на
    следующем прогоне.

Записи идут ТОЛЬКО отсюда, из фонового потока. Живой вызов внешнего API из
обработчика запроса в этом проекте дважды забирал оба gunicorn-воркера и клал
сайт целиком.
"""

import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

from .cards import (
    get_card_by_id,
    try_acquire_sync_lock,
    try_claim_scheduled_run,
    release_sync_lock,
    renew_sync_lock,
    is_valid_account_id,
)
from .storage import (
    get_db,
    get_invoice_line_items,
    get_all_expense_categories,
    get_all_store_planfact_mappings,
    get_store_by_id,
    get_expense_category_by_id,
    mark_invoice_planfact_synced,
)

logger = logging.getLogger(__name__)

SYNC_LOCK = "card_operations"
LOCK_TTL_SECONDS = 10 * 60

# Интервал и стартовая задержка. Задержка своя, не совпадающая с чужими
# (курьеры — 120 с, МойСклад — 420 с): /data общий на все базы, и синки,
# стартующие одной волной, кладут сайт вместе.
SCHEDULER_INTERVAL_SECONDS = 15 * 60
SCHEDULER_START_DELAY_SECONDS = 300

# Окно поиска своих операций в ПланФакте. Больше окно — дороже запрос, меньше —
# риск не увидеть свою операцию по старой заявке и завести её второй раз.
LOOKUP_WINDOW_DAYS = 180

_MARKER_RE = re.compile(r"\[(cardexp|topup):(\d+)\]")
_scheduler_started = False


def marker_for(invoice: Dict[str, Any]) -> str:
    prefix = "cardexp" if invoice["kind"] == "card_expense" else "topup"
    return f"[{prefix}:{invoice['id']}]"


def _operation_date(value: Optional[str]) -> str:
    """
    Дата операции для ПланФакта.

    Берётся из самой заявки (дата траты или перевода), а НЕ из времени сервера:
    прод на Amvera живёт в UTC, салоны в UTC+5 и UTC+7, и трата, занесённая
    вечером, уехала бы следующим днём. Время дописываем нулевое — операция
    привязана к дню, а не к моменту.
    """
    date = (value or "")[:10]
    return f"{date}T00:00:00" if date else ""


def set_invoice_planfact_error(invoice_id: int, error: Optional[str]) -> None:
    """Записать (или снять) причину, по которой заявка не уехала в ПланФакт."""
    conn = get_db()
    try:
        conn.execute("UPDATE invoices SET planfact_error = ? WHERE id = ?", (error, invoice_id))
        conn.commit()
    finally:
        conn.close()


def collect_candidates() -> List[Dict[str, Any]]:
    """
    Заявки, готовые уехать в ПланФакт.

    Трата — после подтверждения владельцем (`approved`): статуса «оплачен» у
    неё не бывает, деньги ушли до создания заявки. Пополнение — только когда
    перевод действительно сделан (`paid`): согласованное, но не переведённое
    на карту ещё не легло, и перемещение в ПФ было бы неправдой.
    """
    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT * FROM invoices
            WHERE planfact_synced_at IS NULL
              AND is_archived = 0
              AND (
                    (kind = 'card_expense' AND status = 'approved')
                 OR (kind = 'card_topup'   AND status = 'paid')
              )
            ORDER BY id
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _known_markers(client, date_start: str) -> Dict[str, str]:
    """
    Маркеры операций, которые в ПланФакте уже есть: {маркер: operationId}.

    Два запроса на весь прогон, а не по одному на заявку: список кандидатов
    обычно короткий, но ходить в внешний API в цикле — верный способ упереться
    в rate limit и растянуть прогон на минуты.
    """
    known: Dict[str, str] = {}
    for prefix, types in (("cardexp:", ["Outcome"]), ("topup:", ["Move"])):
        operations = client.list_operations(
            operation_type=types,
            search_string=prefix,
            operation_date_start=date_start,
            limit=1000,
        )
        if operations is None:
            raise RuntimeError("ПланФакт не ответил на поиск уже разнесённых операций")
        for operation in operations:
            match = _MARKER_RE.search(operation.get("comment") or "")
            if match:
                known.setdefault(match.group(0),
                                 str(operation.get("operationId") or operation.get("id") or ""))
    return known


def _expense_items(invoice: Dict[str, Any], store_map, category_map) -> List[Dict[str, Any]]:
    """Части расходной операции из распределения заявки. Кидает ValueError с текстом для человека."""
    line_items = get_invoice_line_items(invoice["id"])
    if not line_items:
        raise ValueError("Трата не распределена по салонам и статьям")

    calculation_date = _operation_date(invoice.get("spent_at"))
    items = []
    for item in line_items:
        project_id = store_map.get(item["store_id"])
        category_id = category_map.get(item["expense_category_id"])
        if not project_id or not category_id:
            store = get_store_by_id(item["store_id"])
            category = get_expense_category_by_id(item["expense_category_id"])
            raise ValueError(
                f"Не настроено сопоставление с ПланФакт: салон "
                f"«{store['name'] if store else item['store_id']}» или статья "
                f"«{category['name'] if category else item['expense_category_id']}»"
            )
        try:
            category_id_int = int(str(category_id).strip())
            project_id_int = int(str(project_id).strip())
        except (TypeError, ValueError):
            raise ValueError(
                f"В сопоставлении с ПланФакт нечисловой id: проект «{project_id}», "
                f"статья «{category_id}» — поправьте на вкладке «Сопоставление»"
            )
        items.append({
            "calculationDate": calculation_date,
            "isCalculationCommitted": True,
            "operationCategoryId": category_id_int,
            "projectId": project_id_int,
            "value": item["amount"],
        })
    return items


def _push_invoice(invoice: Dict[str, Any], client, store_map, category_map,
                  known: Dict[str, str], dry_run: bool) -> Dict[str, Any]:
    """
    Отправить одну заявку. Возвращает {'status': 'created'|'exists'|'failed', ...}.

    Ошибка здесь не поднимается выше цикла: одна незаполненная настройка не
    должна останавливать разноску всех остальных заявок.
    """
    marker = marker_for(invoice)
    card = get_card_by_id(invoice["card_id"]) if invoice.get("card_id") else None
    if not card:
        return {"status": "failed", "invoice_id": invoice["id"],
                "error": "У заявки не указана рабочая карта"}

    if marker in known:
        # Операция уже в ПланФакте — значит прошлый прогон успел её создать и
        # упал до записи признака. Второй раз не создаём, только помечаем.
        if not dry_run:
            mark_invoice_planfact_synced(invoice["id"], known[marker])
            set_invoice_planfact_error(invoice["id"], None)
        return {"status": "exists", "invoice_id": invoice["id"], "operation_id": known[marker]}

    comment = f"{invoice.get('payment_purpose') or ''} {marker}".strip()

    try:
        if invoice["kind"] == "card_expense":
            if not is_valid_account_id(card["planfact_account_id"]):
                raise ValueError(f"У карты «{card['title']}» неверный id счёта в ПланФакте")
            operation_date = _operation_date(invoice.get("spent_at"))
            if not operation_date:
                raise ValueError("У траты не заполнена дата")
            items = _expense_items(invoice, store_map, category_map)
            payload = {
                "kind": "outcome", "account_id": card["planfact_account_id"],
                "operation_date": operation_date, "items": items, "comment": comment,
            }
        else:
            for field, label in (("planfact_account_id", "счёт карты"),
                                 ("source_planfact_account_id", "счёт списания")):
                if not is_valid_account_id(card.get(field)):
                    raise ValueError(f"У карты «{card['title']}» неверный {label} в ПланФакте")
            operation_date = _operation_date(invoice.get("paid_at") or invoice.get("due_date"))
            if not operation_date:
                raise ValueError("У пополнения не заполнена дата перевода")
            payload = {
                "kind": "move",
                "debiting_account_id": card["source_planfact_account_id"],
                "admission_account_id": card["planfact_account_id"],
                "operation_date": operation_date,
                # У перемещения нет ни статьи, ни проекта: деньги не потрачены,
                # а переложены. Расход появится, когда их потратят.
                "items": [{"calculationDate": operation_date, "isCalculationCommitted": True,
                           "value": invoice["amount"]}],
                "comment": comment,
            }
    except ValueError as error:
        if not dry_run:
            set_invoice_planfact_error(invoice["id"], str(error))
        return {"status": "failed", "invoice_id": invoice["id"], "error": str(error)}

    if dry_run:
        return {"status": "created", "invoice_id": invoice["id"], "preview": payload, "dry_run": True}

    if payload["kind"] == "outcome":
        result = client.create_outcome_operation(
            account_id=payload["account_id"],
            operation_date=payload["operation_date"],
            items=payload["items"],
            comment=payload["comment"],
            external_id=marker.strip("[]"),
        )
    else:
        result = client.create_move_operation(
            debiting_account_id=payload["debiting_account_id"],
            admission_account_id=payload["admission_account_id"],
            operation_date=payload["operation_date"],
            debiting_items=payload["items"],
            comment=payload["comment"],
            external_id=marker.strip("[]"),
        )

    if not result:
        error = "ПланФакт не принял операцию (подробности в логах сервера)"
        set_invoice_planfact_error(invoice["id"], error)
        return {"status": "failed", "invoice_id": invoice["id"], "error": error}

    # У перемещения ПланФакт создаёт ДВЕ операции с общим boundMoveOperationId
    # (списание и зачисление) и возвращает их списком — сохраняем первую, по
    # ней пара находится через boundMoveOperationId.
    operation = result[0] if isinstance(result, list) and result else result
    operation_id = str((operation or {}).get("operationId") or (operation or {}).get("id") or "")

    mark_invoice_planfact_synced(invoice["id"], operation_id)
    set_invoice_planfact_error(invoice["id"], None)
    return {"status": "created", "invoice_id": invoice["id"], "operation_id": operation_id}


def run_card_sync(dry_run: bool = False) -> Dict[str, Any]:
    """
    Один прогон разноски. Возвращает сводку {created, exists, failed}.

    Если кандидатов нет — во внешний API не ходим вовсе: прогон стоит один
    SELECT, и на медленном /data это важнее, чем кажется.
    """
    candidates = collect_candidates()
    if not candidates:
        return {"created": [], "exists": [], "failed": [], "candidates": 0}

    from planfact.client import get_client
    from datetime import datetime, timedelta

    client = get_client()
    store_map = get_all_store_planfact_mappings()
    category_map = {c["id"]: c["planfact_category_id"]
                    for c in get_all_expense_categories() if c.get("planfact_category_id")}
    date_start = (datetime.now() - timedelta(days=LOOKUP_WINDOW_DAYS)).strftime("%Y-%m-%d")

    known = _known_markers(client, date_start)

    created, exists, failed = [], [], []
    for index, invoice in enumerate(candidates):
        try:
            result = _push_invoice(invoice, client, store_map, category_map, known, dry_run)
        except Exception as error:
            logger.exception("Разноска заявки %s упала", invoice["id"])
            result = {"status": "failed", "invoice_id": invoice["id"],
                      "error": f"Внутренняя ошибка: {type(error).__name__}"}
            if not dry_run:
                set_invoice_planfact_error(invoice["id"], result["error"])

        {"created": created, "exists": exists, "failed": failed}[result["status"]].append(result)

        # Лок продлеваем по времени, а не после каждой заявки: очередь из
        # сотни трат иначе тратила бы на продление больше, чем на дело.
        if not dry_run and index and index % 20 == 0:
            renew_sync_lock(SYNC_LOCK, LOCK_TTL_SECONDS)

    return {"created": created, "exists": exists, "failed": failed, "candidates": len(candidates)}


def run_card_sync_locked(dry_run: bool = False) -> Dict[str, Any]:
    """Прогон под локом — чтобы два воркера не разносили одно и то же одновременно."""
    if not try_acquire_sync_lock(SYNC_LOCK, LOCK_TTL_SECONDS):
        return {"skipped": "Разноска уже идёт", "created": [], "exists": [], "failed": []}
    try:
        return run_card_sync(dry_run=dry_run)
    finally:
        release_sync_lock(SYNC_LOCK)


def _scheduler_loop() -> None:
    time.sleep(SCHEDULER_START_DELAY_SECONDS)
    while True:
        try:
            # Талон берётся до всякой работы: планировщик крутится в каждом
            # воркере, и без талона второй повторял бы прогон через полминуты
            # после первого.
            if try_claim_scheduled_run(SYNC_LOCK, SCHEDULER_INTERVAL_SECONDS):
                result = run_card_sync_locked()
                if result.get("candidates"):
                    logger.info(
                        "Разноска карт: создано %d, уже было %d, с ошибкой %d",
                        len(result.get("created", [])), len(result.get("exists", [])),
                        len(result.get("failed", [])),
                    )
        except Exception:
            logger.exception("Планировщик разноски карт упал на тике")
        time.sleep(SCHEDULER_INTERVAL_SECONDS)


def start_card_sync_scheduler() -> None:
    """
    Запустить фоновую разноску заявок по картам.

    Отключается переменной CARD_SYNC_SCHEDULER=0 — локальный запуск иначе
    полез бы в боевой ПланФакт и создал там операции.
    """
    global _scheduler_started

    if os.getenv("CARD_SYNC_SCHEDULER", "1") != "1":
        logger.info("Планировщик разноски карт отключён (CARD_SYNC_SCHEDULER=0)")
        return
    if _scheduler_started:
        return
    _scheduler_started = True

    threading.Thread(target=_scheduler_loop, daemon=True, name="card-sync-scheduler").start()
    logger.info("Планировщик разноски карт запущен (интервал %d мин)",
                SCHEDULER_INTERVAL_SECONDS // 60)
