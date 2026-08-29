"""
ПланФакт API клиент.

Документация: https://apidoc.planfact.io/ (полная спецификация — SPA, слишком
тяжёлая для прямого разбора; ниже — то, что подтверждено рабочими примерами
из официальных публичных репозиториев ПланФакт: github.com/planfact/planfact-
agent-kit (OPERATIONS.md, LOOKUPS.md, QUICKSTART.md) и github.com/planfact/
planfact-api-php-example (curl/put.php — единственный найденный ДОСЛОВНЫЙ
пример тела запроса на запись разбивки по items[], остальные поля запросов
собраны из этого же примера и текстового описания в agent-kit).

Авторизация: заголовок X-ApiKey (не Bearer).
Формат ответа: {"data": ..., "isSuccess": bool, "errorMessage": str|None, "errorCode": str|None}.
"""

import logging
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional
from dotenv import load_dotenv
import requests

from russian_ca import trust_russian_ca

load_dotenv()

logger = logging.getLogger(__name__)

PLANFACT_API_URL = "https://api.planfact.io/api/v1/"


class PlanFactClient:
    """Клиент для работы с ПланФакт API v1."""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("PLANFACT_API_KEY")
        if not self.api_key:
            raise ValueError(
                "Не указан PLANFACT_API_KEY. Установите переменную окружения "
                "в .env в корне проекта или передайте в конструктор."
            )

        self.api_url = PLANFACT_API_URL
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.proxies = {"http": None, "https": None, "no_proxy": None}
        trust_russian_ca(self.session)

    def _headers(self) -> Dict[str, str]:
        return {
            "X-ApiKey": self.api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def request(
        self,
        method: str,
        path: str,
        params: Optional[Dict] = None,
        json_data: Optional[Dict] = None,
        timeout: int = 30,
        max_retries_429: int = 3,
    ) -> Optional[Dict[str, Any]]:
        """
        Выполнить запрос к ПланФакт API.

        Таймаут по умолчанию 30с и ретрай на 429 (макс 3 попытки, 2-5с пауза)
        — по аналогии с MoySkladClient (src/moysklad/client.py), там же
        объяснение, почему таймаут обязателен на прод gunicorn-воркерах.

        timeout/max_retries_429 переопределяемы — на проде всего 2 воркера
        (amvera.yml), а get_projects()/get_operation_categories() дёргаются
        при каждом открытии вкладки "Сопоставление" в UI без кэша; при
        нескольких быстрых перезагрузках вкладки медленные/рейтлимитящие
        ответы ПланФакт с дефолтным таймаутом+ретраями заняли собой обоих
        воркеров и подвесили весь сайт (см. историю сессий, инцидент
        2026-08-17) — для некритичных "справочных" вызовов таймаут короче и
        без ретраев, лучше быстро отказать, чем держать воркер занятым.

        Возвращает содержимое поля "data" при isSuccess=true, иначе None
        (ошибка уже залогирована).
        """
        url = f"{self.api_url.rstrip('/')}/{path.lstrip('/')}"

        for attempt in range(max_retries_429 + 1):
            try:
                logger.debug(f"{method} {url}")
                response = self.session.request(
                    method,
                    url,
                    headers=self._headers(),
                    params=params,
                    json=json_data,
                    timeout=timeout,
                )

                if response.status_code == 429 and attempt < max_retries_429:
                    wait = 2 + attempt
                    logger.warning(f"ПланФакт rate limit, повтор через {wait}с (попытка {attempt + 1}/{max_retries_429})")
                    time.sleep(wait)
                    continue

                response.raise_for_status()
                body = response.json()

                if not body.get("isSuccess", True):
                    logger.error(f"ПланФакт вернул ошибку на {method} {path}: {body.get('errorCode')} {body.get('errorMessage')}")
                    return None

                return body.get("data")

            except requests.exceptions.RequestException as e:
                logger.error(f"Ошибка запроса {method} {path} к ПланФакт: {e}")
                if hasattr(e, "response") and e.response is not None:
                    logger.error(f"Response: {e.response.text[:500]}")
                return None

        return None

    def list_operations(
        self,
        operation_type: Optional[List[str]] = None,
        search_string: Optional[str] = None,
        operation_date_start: Optional[str] = None,
        operation_date_end: Optional[str] = None,
        offset: int = 0,
        limit: int = 1000,
    ) -> Optional[List[Dict[str, Any]]]:
        """
        Список операций (POST /operations/list) — предпочтительный способ
        поиска, в отличие от GET /operations (полный дамп, не используем).

        Для матчинга со счетами ищем по searchString="REF-" — назначение
        платежа обязано содержать match_code счёта (REF-000123), это наш
        собственный формат, substring-поиск ловит его надёжнее, чем попытка
        угадать системную категорию "нераспределённых" операций (её точный
        API-идентификатор нигде не задокументирован дословно).
        """
        body: Dict[str, Any] = {"offset": offset, "limit": limit}
        if operation_type:
            body["operationType"] = operation_type
        if search_string:
            body["searchString"] = search_string
        if operation_date_start:
            body["operationDateStart"] = operation_date_start
        if operation_date_end:
            body["operationDateEnd"] = operation_date_end

        data = self.request("POST", "operations/list", json_data=body)
        if data is None:
            return None
        return data.get("items", []) if isinstance(data, dict) else data

    def update_outcome_operation(
        self,
        operation_id: str,
        operation_date: str,
        account_id: Any,
        comment: str,
        is_committed: bool,
        items: List[Dict[str, Any]],
    ) -> bool:
        """
        Полностью заменить разбивку расходной операции по частям (PUT
        /operations/outcome/{id}). Это PUT, не PATCH — тело обязано включать
        все верхнеуровневые поля операции (не только items), иначе рискуем
        затереть их пустыми значениями. Значения operation_date/account_id/
        comment/is_committed нужно брать из уже загруженной операции
        (list_operations), а не изобретать заново.

        items: [{"calculationDate", "isCalculationCommitted", "contrAgentId",
                 "operationCategoryId", "projectId", "value"}, ...]
        Схема подтверждена дословным примером кода в
        github.com/planfact/planfact-api-php-example (curl/put.php).
        """
        body = {
            "operationDate": operation_date,
            "accountId": account_id,
            "comment": comment or "",
            "isCommitted": is_committed,
            "items": items,
        }
        data = self.request("PUT", f"operations/outcome/{operation_id}", json_data=body)
        return data is not None

    def get_projects(self, active_only: bool = True) -> Optional[List[Dict[str, Any]]]:
        """
        Справочник проектов ПланФакт (соответствуют салонам Бархата).

        Реальный ответ — не плоский список, а {"items": [...], "deletedItems":
        [...], "total", "totalDeleted"} (баг: фронт ждал массив и падал на
        .map() прямо на этом объекте — см. историю сессий, "Ошибка загрузки"
        во вкладке "Сопоставление"). deletedItems сознательно отбрасываем —
        удалённые проекты не нужны в списке для сопоставления.
        """
        params = {"filter.active": "true"} if active_only else None
        # timeout короткий, ретраев нет — это справочный вызов для выпадающего
        # списка в UI, дёргается при каждом открытии вкладки без кэша (см.
        # request(), докстринг про инцидент 2026-08-17)
        data = self.request("GET", "projects", params=params, timeout=8, max_retries_429=0)
        if data is None:
            return None
        return data.get("items", []) if isinstance(data, dict) else data

    def get_accounts(self, active_only: bool = True) -> Optional[List[Dict[str, Any]]]:
        """
        Справочник счетов (GET /accounts) — отсюда accountId рабочих карт и
        счетов, с которых они пополняются.

        Ответ в той же обёртке, что projects: {"items": [...], "total",
        "deletedItems", "totalDeleted"}. Поле "remainder" у счёта — НЕ остаток,
        оно всегда 0.0 (проверено на боевом кабинете 2026-08-29); остаток даёт
        get_account_balances(). Признак живого счёта — "active".
        """
        data = self.request("GET", "accounts", timeout=8, max_retries_429=0)
        if data is None:
            return None
        items = data.get("items", []) if isinstance(data, dict) else data
        if active_only:
            items = [item for item in items if item.get("active")]
        return items

    def get_account_balances(
        self,
        account_ids: List[int],
        on_date: Optional[str] = None,
        period_start: Optional[str] = None,
    ) -> Optional[List[Dict[str, Any]]]:
        """
        Остатки на счетах (POST /dashboards/accountbalance).

        Единственный найденный способ узнать остаток: GET /accounts его не
        отдаёт (см. get_accounts). Тело обязано содержать currentDate,
        periodStartDate и periodEndDate, иначе запрос отвергается; accountIds
        сужает выборку до нужных счетов.

        Возвращает items[]: {"accountId", "total" (остаток), "account" {...},
        "totalValuesByDays": [...]} — по дням готовая история, своё накопление
        остатков заводить не нужно.
        """
        today = on_date or datetime.now().strftime("%Y-%m-%d")
        body = {
            "currentDate": today,
            "periodStartDate": period_start or today,
            "periodEndDate": today,
            "accountIds": [int(account_id) for account_id in account_ids],
        }
        data = self.request("POST", "dashboards/accountbalance", json_data=body,
                            timeout=15, max_retries_429=0)
        if data is None:
            return None
        return data.get("items", []) if isinstance(data, dict) else data

    def create_outcome_operation(
        self,
        account_id: Any,
        operation_date: str,
        items: List[Dict[str, Any]],
        comment: str = "",
        is_committed: bool = True,
        external_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Создать расходную операцию (POST /operations/outcome) — трата с рабочей
        карты. Обязательны accountId и operationDate; сумма идёт ТОЛЬКО через
        items[] (верхнеуровневые value/valueByProjects помечены deprecated).

        items: [{"value", "calculationDate", "isCalculationCommitted",
                 "operationCategoryId", "projectId", "contrAgentId"}, ...]

        comment обязан содержать маркер вида [cardexp:123]: искать свою операцию
        по externalId нечем — searchString в operations/list ищет по тексту.
        Без маркера падение процесса между записью в ПланФакт и записью признака
        в нашу базу приведёт к дублю на следующем прогоне.

        Возвращает созданную операцию (в ней operationId) или None.
        """
        body: Dict[str, Any] = {
            "accountId": int(account_id),
            "operationDate": operation_date,
            "comment": comment or "",
            "isCommitted": is_committed,
            "items": items,
        }
        if external_id:
            body["externalId"] = external_id
        return self.request("POST", "operations/outcome", json_data=body)

    def create_move_operation(
        self,
        debiting_account_id: Any,
        admission_account_id: Any,
        operation_date: str,
        debiting_items: List[Dict[str, Any]],
        admission_items: Optional[List[Dict[str, Any]]] = None,
        comment: str = "",
        is_committed: bool = True,
        external_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Создать перемещение (POST /operations/move) — пополнение рабочей карты
        с расчётного счёта.

        Даты списания и зачисления раздельные, но у нас перевод мгновенный,
        поэтому обе равны operation_date. Суммы — через debitingItems/
        admissionItems (верхнеуровневые debitingValue/admissionValue deprecated).

        ПланФакт создаёт на одно перемещение ДВЕ операции с общим
        boundMoveOperationId (списание и зачисление), и в списке пара даёт 0 —
        это нормально, а не потерянная сумма.
        """
        body: Dict[str, Any] = {
            "debitingAccountId": int(debiting_account_id),
            "admissionAccountId": int(admission_account_id),
            "debitingDate": operation_date,
            "admissionDate": operation_date,
            "comment": comment or "",
            "isCommitted": is_committed,
            "debitingItems": debiting_items,
            "admissionItems": admission_items if admission_items is not None else debiting_items,
        }
        if external_id:
            body["debitingExternalId"] = external_id
            body["admissionExternalId"] = external_id
        return self.request("POST", "operations/move", json_data=body)

    def get_operation_categories(self, operation_category_type: str = "Outcome") -> Optional[List[Dict[str, Any]]]:
        """Справочник статей операций указанного типа (по умолчанию — расходы). См. get_projects — та же обёртка {"items": [...]}."""
        params = {"filter.operationCategoryType": operation_category_type}
        data = self.request("GET", "operationcategories", params=params, timeout=8, max_retries_429=0)
        if data is None:
            return None
        return data.get("items", []) if isinstance(data, dict) else data


def get_client(api_key: Optional[str] = None) -> PlanFactClient:
    return PlanFactClient(api_key)
