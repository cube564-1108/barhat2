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
from typing import Any, Dict, List, Optional
from dotenv import load_dotenv
import requests

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
    ) -> Optional[Dict[str, Any]]:
        """
        Выполнить запрос к ПланФакт API.

        Таймаут 30с и ретрай на 429 (макс 3 попытки, 2-5с пауза) — по аналогии
        с MoySkladClient (src/moysklad/client.py), там же объяснение, почему
        таймаут обязателен на прод gunicorn-воркерах.

        Возвращает содержимое поля "data" при isSuccess=true, иначе None
        (ошибка уже залогирована).
        """
        url = f"{self.api_url.rstrip('/')}/{path.lstrip('/')}"

        max_retries_429 = 3
        for attempt in range(max_retries_429 + 1):
            try:
                logger.debug(f"{method} {url}")
                response = self.session.request(
                    method,
                    url,
                    headers=self._headers(),
                    params=params,
                    json=json_data,
                    timeout=30,
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
        data = self.request("GET", "projects", params=params)
        if data is None:
            return None
        return data.get("items", []) if isinstance(data, dict) else data

    def get_operation_categories(self, operation_category_type: str = "Outcome") -> Optional[List[Dict[str, Any]]]:
        """Справочник статей операций указанного типа (по умолчанию — расходы). См. get_projects — та же обёртка {"items": [...]}."""
        params = {"filter.operationCategoryType": operation_category_type}
        data = self.request("GET", "operationcategories", params=params)
        if data is None:
            return None
        return data.get("items", []) if isinstance(data, dict) else data


def get_client(api_key: Optional[str] = None) -> PlanFactClient:
    return PlanFactClient(api_key)
