"""
Разноска оплаченных счетов по данным ПланФакта.

Вынесено из `server.py` без изменения логики (фаза 0 плана
plans/2026-09-15-единая-синхронизация-планфакт.md): объединять этот прогон с
разноской заявок по картам, пока он лежит внутри модуля с обработчиками,
нельзя — `server.py` импортирует `cards_sync`, и общий модуль замкнул бы круг.

Функции сознательно не зависят от контекста запроса (`current_user` и прочего
флеск-окружения): их зовут и из обработчика, и из фонового потока.
"""

import logging
import re
from datetime import datetime, timedelta

from .storage import (
    CARD_KINDS,
    get_invoice_by_match_code,
    get_invoice_line_items,
    get_store_by_id,
    get_expense_category_by_id,
    get_all_store_planfact_mappings,
    get_all_expense_categories,
    mark_invoice_planfact_synced,
    mark_invoice_paid,
    record_planfact_unmatched,
)

logger = logging.getLogger(__name__)


# =============================================================================
# ПЛАНФАКТ — АВТОРАЗНОСКА ОПЛАЧЕННЫХ СЧЕТОВ (Фаза 6 плана)
# =============================================================================
#
# Матчинг ищет операции ПланФакт с "REF-" в назначении платежа (наш собственный
# match_code, см. storage.py) вместо попытки угадать системную категорию
# "нераспределённые расходы" — её точный API-идентификатор нигде не задоку-
# ментирован дословно (см. src/planfact/README.md, раздел "Известные пробелы").
#
# Запуск — только вручную, кнопкой в дашборде (см. src/planfact/README.md
# почему не сделали периодический автопоток на первом этапе). dry_run=true
# ничего не пишет в ПланФакт и не меняет статусы счетов — только показывает,
# что было бы сделано, для проверки перед первым боевым запуском.

_MATCH_CODE_RE = re.compile(r"REF-\d{6}")
_PLANFACT_POLL_WINDOW_DAYS = 60


def _match_planfact_operation(op, client, store_map, category_map, dry_run):
    """Обработать одну операцию ПланФакт. Возвращает dict с ключом 'status':
    'skip' (не наша операция или уже разнесена раньше), 'matched' (успех/превью)
    или 'unmatched' (нужна ручная разноска, см. 'reason')."""
    operation_id = str(op.get("operationId") or op.get("id") or "")
    comment = op.get("comment") or ""
    match = _MATCH_CODE_RE.search(comment)
    if not match:
        return {"status": "skip"}
    match_code = match.group(0)

    invoice = get_invoice_by_match_code(match_code)
    if not invoice:
        return {
            "status": "unmatched", "operation_id": operation_id, "match_code": match_code,
            "reason": f"Нет счёта с кодом {match_code}",
        }

    # Признак «уже разнесён» — отдельное поле, а НЕ статус. Статус paid
    # ставится и вручную (кнопка, массовое действие), и раньше синк принимал
    # его за «уже разнесено» и молча пропускал такие счета навсегда — операция
    # в ПланФакте оставалась нераспределённой, и в «Требует внимания» она тоже
    # не попадала, потому что этот выход стоит до записи туда.
    if invoice.get("planfact_synced_at"):
        return {"status": "skip"}

    # Заявки по рабочим картам этот синк не касается: он дообогащает операции,
    # которые ПланФакт получил автоимпортом из банка, а операции по картам
    # создаёт целиком другой синк (план 2026-08-29, Фаза 3). Разнести их здесь
    # значило бы записать пополнение карты расходом — тот же рубль лёг бы в
    # расход дважды: при пополнении и при трате.
    if invoice.get("kind") in CARD_KINDS:
        return {"status": "skip"}

    # Оплаченный, но не разнесённый счёт — нормальный кандидат на разноску
    if invoice["status"] not in ("approved", "sent_to_bank", "paid"):
        return {
            "status": "unmatched", "operation_id": operation_id, "match_code": match_code,
            "invoice_id": invoice["id"],
            "reason": f"Счёт {invoice['invoice_number']} в статусе «{invoice['status']}» — разноска невозможна",
        }

    line_items = get_invoice_line_items(invoice["id"])
    if not line_items:
        return {
            "status": "unmatched", "operation_id": operation_id, "match_code": match_code,
            "invoice_id": invoice["id"],
            "reason": f"Счёт {invoice['invoice_number']} ещё не распределён по проектам/статьям",
        }

    pf_items = []
    for li in line_items:
        project_id = store_map.get(li["store_id"])
        pf_category_id = category_map.get(li["expense_category_id"])
        if not project_id or not pf_category_id:
            store = get_store_by_id(li["store_id"])
            category = get_expense_category_by_id(li["expense_category_id"])
            return {
                "status": "unmatched", "operation_id": operation_id, "match_code": match_code,
                "invoice_id": invoice["id"],
                "reason": (
                    f"Не настроено сопоставление с ПланФакт: "
                    f"салон «{store['name'] if store else li['store_id']}» "
                    f"или статья «{category['name'] if category else li['expense_category_id']}»"
                ),
            }
        # id сопоставления вводят руками, когда ПланФакт не отвечает и
        # выпадающего списка нет. Нечисловое значение раньше роняло int()
        # прямо внутри цикла по операциям — обрывался весь прогон, и
        # остальные счета не разносились из-за одной опечатки в настройке.
        try:
            category_id_int = int(str(pf_category_id).strip())
            project_id_int = int(str(project_id).strip())
        except (TypeError, ValueError):
            return {
                "status": "unmatched", "operation_id": operation_id, "match_code": match_code,
                "invoice_id": invoice["id"],
                "reason": (
                    f"В сопоставлении с ПланФакт нечисловой id: проект «{project_id}», "
                    f"статья «{pf_category_id}» — поправьте на вкладке «Сопоставление»"
                ),
            }

        pf_items.append({
            "calculationDate": op.get("operationDate"),
            "isCalculationCommitted": bool(op.get("isCommitted", True)),
            "contrAgentId": (op.get("contrAgent") or {}).get("contrAgentId"),
            "operationCategoryId": category_id_int,
            "projectId": project_id_int,
            "value": li["amount"],
        })

    preview = {
        "status": "matched",
        "operation_id": operation_id,
        "match_code": match_code,
        "invoice_id": invoice["id"],
        "invoice_number": invoice["invoice_number"],
        "operation_amount": op.get("value"),
        "items": pf_items,
    }
    if dry_run:
        return preview

    # Реальный ответ ПланФакт вкладывает счёт списания в объект account
    # ({"account": {"accountId": ...}}), а не плоским полем на операции —
    # баг: раньше брали op.get("accountId") (всегда None), ПланФакт отвечал
    # "Не указан счёт" на каждую попытку разноски (см. историю сессий,
    # инцидент 2026-08-18). Аналогично amount у операции на самом деле в
    # поле "value", "accountId" плоско не существует нигде на операции.
    ok = client.update_outcome_operation(
        operation_id,
        operation_date=op.get("operationDate"),
        account_id=(op.get("account") or {}).get("accountId"),
        comment=comment,
        is_committed=bool(op.get("isCommitted", True)),
        items=pf_items,
    )
    if not ok:
        # Причину отказа называет сам ПланФакт, и она нужна человеку в списке
        # «Требует внимания»: отсылка к логам сервера бесполезна — консоли у
        # контейнера на нашем тарифе Amvera нет.
        detail = getattr(client, "last_error", None)
        return {
            "status": "unmatched", "operation_id": operation_id, "match_code": match_code,
            "invoice_id": invoice["id"],
            "reason": (f"ПланФакт не принял запись: {detail}" if detail
                       else "ПланФакт не принял запись и не объяснил причину"),
        }

    # Сначала признак разноски, потом статус: если процесс упадёт между этими
    # шагами, лучше «разнесён, но не отмечен оплаченным» (человек увидит и
    # поправит), чем «оплачен, но не отмечен разнесённым» — второе синк
    # попробует разнести ещё раз и создаст дубль в ПланФакте.
    mark_invoice_planfact_synced(invoice["id"], operation_id)
    mark_invoice_paid(invoice["id"], changed_by="planfact-sync")
    return preview


def _run_planfact_sync(dry_run: bool = False) -> dict:
    from planfact.client import get_client

    client = get_client()
    store_map = get_all_store_planfact_mappings()
    categories = get_all_expense_categories()
    category_map = {c["id"]: c["planfact_category_id"] for c in categories if c.get("planfact_category_id")}

    date_start = (datetime.now() - timedelta(days=_PLANFACT_POLL_WINDOW_DAYS)).strftime("%Y-%m-%d")

    matched = []
    unmatched = []
    offset = 0
    while True:
        ops = client.list_operations(
            operation_type=["Outcome"],
            search_string="REF-",
            operation_date_start=date_start,
            offset=offset,
            limit=1000,
        )
        if ops is None:
            raise RuntimeError("Не удалось получить список операций из ПланФакт")
        if not ops:
            break

        for op in ops:
            # Одна операция не должна уносить весь прогон: раньше любое
            # неожиданное исключение обрывало цикл, и всё, что стояло в
            # очереди после неё, оставалось неразнесённым без объяснений.
            try:
                result = _match_planfact_operation(op, client, store_map, category_map, dry_run)
            except Exception as error:
                logger.exception("Разноска операции %s упала", op.get("operationId"))
                result = {
                    "status": "unmatched",
                    "operation_id": str(op.get("operationId") or op.get("id") or ""),
                    "match_code": None,
                    "reason": f"Внутренняя ошибка при разноске: {type(error).__name__}",
                }
            if result["status"] == "matched":
                matched.append(result)
            elif result["status"] == "unmatched":
                unmatched.append(result)
                if not dry_run:
                    record_planfact_unmatched(
                        planfact_operation_id=result["operation_id"],
                        reason=result["reason"],
                        match_code=result.get("match_code"),
                        invoice_id=result.get("invoice_id"),
                        operation_amount=op.get("value"),
                        operation_comment=op.get("comment"),
                    )

        if len(ops) < 1000:
            break
        offset += 1000

    return {"matched": matched, "unmatched": unmatched}
