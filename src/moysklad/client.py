"""
МойСклад API клиент
Базовый модуль для работы с МойСклад API remap 1.2

Документация: https://dev.moysklad.ru/doc/
Авторизация: https://dev.moysklad.ru/doc/#access-authorization
"""

import os
import base64
import logging
from typing import Optional, Dict, List, Any
from datetime import datetime
from dotenv import load_dotenv
import requests

# Загружаем переменные окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# МойСклад API endpoints
MOYSKLAD_API_URL = "https://api.moysklad.ru/api/remap/1.2/"


class MoySkladClient:
    """Клиент для работы с МойСклад API remap 1.2"""

    def __init__(
        self,
        login: Optional[str] = None,
        password: Optional[str] = None,
        token: Optional[str] = None
    ):
        """
        Инициализация клиента

        Args:
            login: Логин для Basic Auth (если None, берётся из MOYSKLAD_LOGIN)
            password: Пароль для Basic Auth (если None, берётся из MOYSKLAD_PASSWORD)
            token: API токен дляBearer Auth (если None, берётся из MOYSKLAD_TOKEN)
        """
        self.login = login or os.getenv('MOYSKLAD_LOGIN')
        self.password = password or os.getenv('MOYSKLAD_PASSWORD')
        self.token = token or os.getenv('MOYSKLAD_TOKEN')

        # Проверяем credentials
        if self.token:
            self.auth_type = 'bearer'
            logger.info("Используем Bearer token авторизацию")
        elif self.login and self.password:
            self.auth_type = 'basic'
            logger.info("Используем Basic Auth авторизацию")
        else:
            raise ValueError(
                "Не указаны credentials для МойСклад API. "
                "Установите MOYSKLAD_TOKEN или (MOYSKLAD_LOGIN + MOYSKLAD_PASSWORD) в .env "
                "или передайте в конструктор."
            )

        self.api_url = MOYSKLAD_API_URL
        self.session = requests.Session()
        # Отключаем прокси
        self.session.trust_env = False
        self.session.proxies = {'http': None, 'https': None, 'no_proxy': None}

    def _get_auth_headers(self) -> Dict[str, str]:
        """Получить заголовки авторизации"""
        headers = {
            'Content-Type': 'application/json;charset=utf-8',
            'Accept': 'application/json;charset=utf-8',
        }

        if self.auth_type == 'bearer':
            headers['Authorization'] = f'Bearer {self.token}'
        elif self.auth_type == 'basic':
            credentials = base64.b64encode(
                f"{self.login}:{self.password}".encode()
            ).decode()
            headers['Authorization'] = f'Basic {credentials}'

        return headers

    def request(
        self,
        method: str,
        path: str,
        params: Optional[Dict] = None,
        json_data: Optional[Dict] = None,
        **kwargs
    ) -> Optional[Dict]:
        """
        Выполнение запроса к МойСклад API

        Args:
            method: HTTP метод (GET, POST, PUT, DELETE)
            path: API path (например, /entity/product)
            params: Query параметры
            json_data: JSON тело запроса
            **kwargs: Дополнительные аргументы для requests

        Returns:
            JSON ответ или None в случае ошибки
        """
        url = f"{self.api_url.rstrip('/')}/{path.lstrip('/')}"

        headers = kwargs.pop('headers', {})
        headers.update(self._get_auth_headers())
        kwargs['headers'] = headers

        if params:
            kwargs['params'] = params

        if json_data:
            kwargs['json'] = json_data

        try:
            logger.debug(f"{method} {url}")
            response = self.session.request(method, url, **kwargs)
            response.raise_for_status()
            return response.json()

        except requests.exceptions.RequestException as e:
            logger.error(f"Ошибка запроса {method} {path}: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Response: {e.response.text}")
                # Обработка rate limiting (429)
                if e.response.status_code == 429:
                    logger.warning("Rate limited, retrying after 1s...")
                    import time
                    time.sleep(1)
                    return self.request(method, path, params, json_data, **kwargs)
            return None

    def get(self, path: str, params: Optional[Dict] = None, **kwargs) -> Optional[Dict]:
        """GET запрос"""
        return self.request('GET', path, params=params, **kwargs)

    def post(self, path: str, json_data: Optional[Dict] = None, **kwargs) -> Optional[Dict]:
        """POST запрос"""
        return self.request('POST', path, json_data=json_data, **kwargs)

    def put(self, path: str, json_data: Optional[Dict] = None, **kwargs) -> Optional[Dict]:
        """PUT запрос"""
        return self.request('PUT', path, json_data=json_data, **kwargs)

    def delete(self, path: str, **kwargs) -> Optional[Dict]:
        """DELETE запрос"""
        return self.request('DELETE', path, **kwargs)

    # ========== Entity endpoints ==========

    def get_products(
        self,
        limit: int = 1000,
        offset: int = 0,
        filter: Optional[Dict] = None,
        expand: Optional[str] = None
    ) -> Optional[Dict]:
        """
        Получить товары (products)

        Args:
            limit: Количество записей (макс 1000)
            offset: Смещение
            filter: Фильтры (например, {'archived': False})
            expand: Поля для раскрытия (например, 'images,group')

        Returns:
            Словарь с meta и rows
        """
        params = {'limit': limit, 'offset': offset}
        if filter:
            params.update(filter)
        if expand:
            params['expand'] = expand

        return self.get('/entity/product', params=params)

    def get_assortment(
        self,
        limit: int = 1000,
        offset: int = 0,
        filter: Optional[Dict] = None,
        expand: Optional[str] = None
    ) -> Optional[Dict]:
        """
        Получить ассортимент (товары, модификации, услуги, комплекты)

        Args:
            limit: Количество записей (макс 1000)
            offset: Смещение
            filter: Фильтры
            expand: Поля для раскрытия

        Returns:
            Словарь с meta и rows
        """
        params = {'limit': limit, 'offset': offset}
        if filter:
            params.update(filter)
        if expand:
            params['expand'] = expand

        return self.get('/entity/assortment', params=params)

    def get_stores(self, limit: int = 1000, offset: int = 0) -> Optional[Dict]:
        """
        Получить склады

        Args:
            limit: Количество записей
            offset: Смещение

        Returns:
            Словарь с meta и rows
        """
        return self.get('/entity/store', params={'limit': limit, 'offset': offset})

    def get_folders(self, limit: int = 1000, offset: int = 0) -> Optional[Dict]:
        """
        Получить папки/группы товаров

        Args:
            limit: Количество записей
            offset: Смещение

        Returns:
            Словарь с meta и rows
        """
        return self.get('/entity/productfolder', params={'limit': limit, 'offset': offset})

    def get_sales_channels(self, limit: int = 1000, offset: int = 0) -> Optional[Dict]:
        """
        Получить справочник каналов продаж

        Args:
            limit: Количество записей
            offset: Смещение

        Returns:
            Словарь с meta и rows
        """
        return self.get('/entity/saleschannel', params={'limit': limit, 'offset': offset})

    def get_stock(
        self,
        store_id: Optional[str] = None,
        limit: int = 1000,
        offset: int = 0
    ) -> Optional[Dict]:
        """
        Получить остатки на складе

        Args:
            store_id: ID склада (если None, остатки по всем складам)
            limit: Количество записей
            offset: Смещение

        Returns:
            Словарь с meta и rows
        """
        path = '/report/stock/all'
        params = {'limit': limit, 'offset': offset}

        if store_id:
            params['store.id'] = store_id

        return self.get(path, params=params)

    def get_sales_orders(
        self,
        limit: int = 1000,
        offset: int = 0,
        filter: Optional[Dict] = None,
        expand: Optional[str] = None
    ) -> Optional[Dict]:
        """
        Получить заказы покупателей

        Args:
            limit: Количество записей
            offset: Смещение
            filter: Фильтры (например, {'status': 'published'})
            expand: Поля для раскрытия

        Returns:
            Словарь с meta и rows
        """
        params = {'limit': limit, 'offset': offset}
        if filter:
            params.update(filter)
        if expand:
            params['expand'] = expand

        return self.get('/entity/customerorder', params=params)

    def get_demands(
        self,
        limit: int = 1000,
        offset: int = 0,
        filter: Optional[Dict] = None,
        expand: Optional[str] = None
    ) -> Optional[Dict]:
        """
        Получить расходные накладные (отгрузки)

        Args:
            limit: Количество записей
            offset: Смещение
            filter: Фильтры
            expand: Поля для раскрытия

        Returns:
            Словарь с meta и rows
        """
        params = {'limit': limit, 'offset': offset}
        if filter:
            params.update(filter)
        if expand:
            params['expand'] = expand

        return self.get('/entity/demand', params=params)

    def get_counterparties(
        self,
        limit: int = 1000,
        offset: int = 0,
        filter: Optional[Dict] = None
    ) -> Optional[Dict]:
        """
        Получить контрагентов

        Args:
            limit: Количество записей
            offset: Смещение
            filter: Фильтры

        Returns:
            Словарь с meta и rows
        """
        params = {'limit': limit, 'offset': offset}
        if filter:
            params.update(filter)

        return self.get('/entity/counterparty', params=params)

    def get_employees(self, limit: int = 1000, offset: int = 0) -> Optional[Dict]:
        """
        Получить сотрудников

        Args:
            limit: Количество записей
            offset: Смещение

        Returns:
            Словарь с meta и rows
        """
        return self.get('/entity/employee', params={'limit': limit, 'offset': offset})

    def get_projects(self, limit: int = 1000, offset: int = 0) -> Optional[Dict]:
        """
        Получить проекты

        Args:
            limit: Количество записей
            offset: Смещение

        Returns:
            Словарь с meta и rows
        """
        return self.get('/entity/project', params={'limit': limit, 'offset': offset})

    def get_by_id(self, entity: str, entity_id: str, expand: Optional[str] = None) -> Optional[Dict]:
        """
        Получить сущность по ID

        Args:
            entity: Тип сущности (product, salesorder, etc)
            entity_id: ID сущности
            expand: Поля для раскрытия

        Returns:
            Словарь с данными сущности
        """
        path = f'/entity/{entity}/{entity_id}'
        params = {}
        if expand:
            params['expand'] = expand

        return self.get(path, params=params)

    # ========== Report endpoints ==========

    def get_sales_report(
        self,
        moment: Optional[str] = None,
        limit: int = 1000,
        offset: int = 0
    ) -> Optional[Dict]:
        """
        Получить отчёт по продажам

        Args:
            moment: Момент времени (ISO формат)
            limit: Количество записей
            offset: Смещение

        Returns:
            Словарь с данными отчёта
        """
        params = {'limit': limit, 'offset': offset}
        if moment:
            params['moment'] = moment

        return self.get('/report/sales', params=params)

    def get_turnover_report(
        self,
        moment_from: str,
        moment_to: str,
        filter: Optional[Dict] = None
    ) -> Optional[Dict]:
        """
        Получить отчёт по оборотам

        Args:
            moment_from: Начало периода (ISO формат)
            moment_to: Конец периода (ISO формат)
            filter: Фильтры

        Returns:
            Словарь с данными отчёта
        """
        params = {
            'momentFrom': moment_from,
            'momentTo': moment_to
        }
        if filter:
            params.update(filter)

        return self.get('/report/turnover/all', params=params)


def get_client(
    login: Optional[str] = None,
    password: Optional[str] = None,
    token: Optional[str] = None
) -> MoySkladClient:
    """
    Factory function для получения клиента

    Args:
        login: Логин (если None, из MOYSKLAD_LOGIN)
        password: Пароль (если None, из MOYSKLAD_PASSWORD)
        token: Токен (если None, из MOYSKLAD_TOKEN)

    Returns:
        Экземпляр MoySkladClient
    """
    return MoySkladClient(login, password, token)


if __name__ == "__main__":
    # Тест подключения
    print("Тест подключения к МойСклад API remap 1.2...")

    client = get_client()

    # Пример: получаем склады
    stores = client.get_stores(limit=10)
    if stores:
        print("✅ Подключение успешно!")
        print(f"📦 Складов: {stores.get('meta', {}).get('size', 0)}")
        for store in stores.get('rows', [])[:3]:
            print(f"  - {store.get('name')} ({store.get('id')})")
    else:
        print("❌ Ошибка подключения. Проверьте credentials в .env")
