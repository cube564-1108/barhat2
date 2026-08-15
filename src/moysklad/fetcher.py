"""
МойСклад Data Fetcher
Модуль для получения данных из МойСклад с retry-логикой и пагинацией
"""

import time
import logging
from typing import Optional, List, Dict, Callable, Any
from functools import wraps
from .client import MoySkladClient, get_client

logger = logging.getLogger(__name__)


def retry_on_error(max_retries: int = 3, delay: float = 1.0, backoff: float = 2.0):
    """
    Декоратор для retry при ошибках

    Args:
        max_retries: Максимальное количество попыток
        delay: Задержка между попытками (секунды)
        backoff: Множитель для увеличения задержки
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            retries = 0
            current_delay = delay

            while retries < max_retries:
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    retries += 1
                    if retries >= max_retries:
                        logger.error(f"{func.__name__} failed after {max_retries} retries: {e}")
                        raise

                    logger.warning(
                        f"{func.__name__} failed (attempt {retries}/{max_retries}): {e}. "
                        f"Retrying in {current_delay}s..."
                    )
                    time.sleep(current_delay)
                    current_delay *= backoff

        return wrapper
    return decorator


class MoySkladFetcher:
    """
    Класс для загрузки данных из МойСклад с обработкой ошибок и пагинацией
    """

    def __init__(self, client: Optional[MoySkladClient] = None):
        """
        Инициализация

        Args:
            client: Экземпляр MoySkladClient (если None, создастся новый)
        """
        self.client = client or get_client()

    @retry_on_error(max_retries=3, delay=1.0)
    def get_all_products(
        self,
        max_items: int = 10000,
        include_archived: bool = False,
        expand: Optional[str] = None
    ) -> Optional[List[Dict]]:
        """
        Получить все товары с автоматической пагинацией

        Args:
            max_items: Максимальное количество товаров
            include_archived: Включать архивированные
            expand: Поля для раскрытия

        Returns:
            Список товаров или None
        """
        logger.info(f"Загружаем товары (макс: {max_items})...")

        all_products = []
        offset = 0
        batch_size = 1000  # Макс для МойСклад API

        while len(all_products) < max_items:
            response = self.client.get_products(
                limit=batch_size,
                offset=offset,
                filter=None if include_archived else {'archived': False},
                expand=expand
            )

            if response is None:
                logger.error("Ошибка загрузки товаров")
                return None

            rows = response.get('rows', [])
            if not rows:
                break

            all_products.extend(rows)
            logger.info(f"  Загружено {len(rows)} товаров (всего: {len(all_products)})")

            if len(rows) < batch_size:
                break

            offset += batch_size

        logger.info(f"✅ Всего загружено {len(all_products)} товаров")
        return all_products

    @retry_on_error(max_retries=3, delay=1.0)
    def get_all_assortment(
        self,
        max_items: int = 10000,
        include_archived: bool = False,
        expand: Optional[str] = None
    ) -> Optional[List[Dict]]:
        """
        Получить весь ассортимент с пагинацией

        Args:
            max_items: Максимальное количество
            include_archived: Включать архивированные
            expand: Поля для раскрытия

        Returns:
            Список или None
        """
        logger.info(f"Загружаем ассортимент (макс: {max_items})...")

        all_items = []
        offset = 0
        batch_size = 1000

        while len(all_items) < max_items:
            response = self.client.get_assortment(
                limit=batch_size,
                offset=offset,
                expand=expand
            )

            if response is None:
                return None

            rows = response.get('rows', [])
            if not rows:
                break

            if not include_archived:
                rows = [r for r in rows if not r.get('archived')]

            all_items.extend(rows)
            logger.info(f"  Загружено {len(rows)} (всего: {len(all_items)})")

            if len(rows) < batch_size:
                break

            offset += batch_size

        logger.info(f"✅ Всего загружено {len(all_items)} позиций")
        return all_items

    @retry_on_error(max_retries=3, delay=1.0)
    def get_all_stock(
        self,
        store_id: Optional[str] = None,
        max_items: int = 10000
    ) -> Optional[List[Dict]]:
        """
        Получить все остатки с пагинацией

        Args:
            store_id: ID склада
            max_items: Максимальное количество

        Returns:
            Список остатков или None
        """
        logger.info(f"Загружаем остатки...")

        all_stock = []
        offset = 0
        batch_size = 1000

        while len(all_stock) < max_items:
            response = self.client.get_stock(
                store_id=store_id,
                limit=batch_size,
                offset=offset
            )

            if response is None:
                return None

            rows = response.get('rows', [])
            if not rows:
                break

            all_stock.extend(rows)
            logger.info(f"  Загружено {len(rows)} остатков (всего: {len(all_stock)})")

            if len(rows) < batch_size:
                break

            offset += batch_size

        logger.info(f"✅ Всего загружено {len(all_stock)} остатков")
        return all_stock

    @retry_on_error(max_retries=3, delay=1.0)
    def get_all_sales_orders(
        self,
        max_items: int = 1000,
        filter: Optional[Dict] = None,
        expand: Optional[str] = None
    ) -> Optional[List[Dict]]:
        """
        Получить все заказы покупателей с пагинацией

        Args:
            max_items: Максимальное количество
            filter: Фильтры
            expand: Поля для раскрытия

        Returns:
            Список заказов или None
        """
        logger.info(f"Загружаем заказы покупателей...")

        all_orders = []
        offset = 0
        # МойСклад не разворачивает вложенные коллекции (positions) в списочном
        # запросе при limit > 100 — молча возвращает только meta без rows.
        # Порог измерен эмпирически (limit=100 работает, limit=200 — нет).
        batch_size = 100 if expand and 'positions' in expand else 1000

        while len(all_orders) < max_items:
            response = self.client.get_sales_orders(
                limit=batch_size,
                offset=offset,
                filter=filter,
                expand=expand
            )

            if response is None:
                return None

            rows = response.get('rows', [])
            if not rows:
                break

            all_orders.extend(rows)
            logger.info(f"  Загружено {len(rows)} заказов (всего: {len(all_orders)})")

            if len(rows) < batch_size:
                break

            offset += batch_size

        logger.info(f"✅ Всего загружено {len(all_orders)} заказов")
        return all_orders

    @retry_on_error(max_retries=3, delay=1.0)
    def get_all_demands(
        self,
        max_items: int = 1000,
        filter: Optional[Dict] = None,
        expand: Optional[str] = None
    ) -> Optional[List[Dict]]:
        """
        Получить все расходные накладные с пагинацией

        Args:
            max_items: Максимальное количество
            filter: Фильтры
            expand: Поля для раскрытия

        Returns:
            Список накладных или None
        """
        logger.info(f"Загружаем расходные накладные...")

        all_demands = []
        offset = 0
        batch_size = 1000

        while len(all_demands) < max_items:
            response = self.client.get_demands(
                limit=batch_size,
                offset=offset,
                filter=filter,
                expand=expand
            )

            if response is None:
                return None

            rows = response.get('rows', [])
            if not rows:
                break

            all_demands.extend(rows)
            logger.info(f"  Загружено {len(rows)} накладных (всего: {len(all_demands)})")

            if len(rows) < batch_size:
                break

            offset += batch_size

        logger.info(f"✅ Всего загружено {len(all_demands)} накладных")
        return all_demands

    @retry_on_error(max_retries=3, delay=1.0)
    def get_all_counterparties(
        self,
        max_items: int = 1000,
        filter: Optional[Dict] = None
    ) -> Optional[List[Dict]]:
        """
        Получить всех контрагентов с пагинацией

        Args:
            max_items: Максимальное количество
            filter: Фильтры

        Returns:
            Список контрагентов или None
        """
        logger.info(f"Загружаем контрагентов...")

        all_counterparties = []
        offset = 0
        batch_size = 1000

        while len(all_counterparties) < max_items:
            response = self.client.get_counterparties(
                limit=batch_size,
                offset=offset,
                filter=filter
            )

            if response is None:
                return None

            rows = response.get('rows', [])
            if not rows:
                break

            all_counterparties.extend(rows)
            logger.info(f"  Загружено {len(rows)} контрагентов (всего: {len(all_counterparties)})")

            if len(rows) < batch_size:
                break

            offset += batch_size

        logger.info(f"✅ Всего загружено {len(all_counterparties)} контрагентов")
        return all_counterparties

    @retry_on_error(max_retries=3, delay=1.0)
    def get_all_stores(self) -> Optional[List[Dict]]:
        """
        Получить все склады

        Returns:
            Список складов или None
        """
        logger.info(f"Загружаем склады...")

        response = self.client.get_stores(limit=1000)

        if response is None:
            return None

        stores = response.get('rows', [])
        logger.info(f"✅ Всего загружено {len(stores)} складов")
        return stores

    @retry_on_error(max_retries=3, delay=1.0)
    def get_all_folders(self) -> Optional[List[Dict]]:
        """
        Получить все папки/группы товаров

        Returns:
            Список папок или None
        """
        logger.info(f"Загружаем папки...")

        all_folders = []
        offset = 0
        batch_size = 1000

        while True:
            response = self.client.get_folders(limit=batch_size, offset=offset)

            if response is None:
                return None

            rows = response.get('rows', [])
            if not rows:
                break

            all_folders.extend(rows)

            if len(rows) < batch_size:
                break

            offset += batch_size

        logger.info(f"✅ Всего загружено {len(all_folders)} папок")
        return all_folders

    @retry_on_error(max_retries=3, delay=1.0)
    def get_all_sales_channels(self) -> Optional[List[Dict]]:
        """
        Получить справочник каналов продаж

        Returns:
            Список каналов или None
        """
        logger.info(f"Загружаем каналы продаж...")

        response = self.client.get_sales_channels(limit=1000)

        if response is None:
            return None

        channels = response.get('rows', [])
        logger.info(f"✅ Всего загружено {len(channels)} каналов продаж")
        return channels

    def get_full_entity_data(
        self,
        entity_type: str,
        max_items: int = 10000,
        **kwargs
    ) -> Optional[List[Dict]]:
        """
        Универсальный метод для получения любой сущности с пагинацией

        Args:
            entity_type: Тип сущности (products, stock, sales_orders, etc)
            max_items: Максимальное количество
            **kwargs: Дополнительные параметры

        Returns:
            Список или None
        """
        dispatch = {
            'products': lambda: self.get_all_products(max_items, **kwargs),
            'assortment': lambda: self.get_all_assortment(max_items, **kwargs),
            'stock': lambda: self.get_all_stock(max_items=max_items, **kwargs),
            'stores': lambda: self.get_all_stores(),
            'folders': lambda: self.get_all_folders(),
            'sales_orders': lambda: self.get_all_sales_orders(max_items, **kwargs),
            'demands': lambda: self.get_all_demands(max_items, **kwargs),
            'counterparties': lambda: self.get_all_counterparties(max_items, **kwargs),
            'sales_channels': lambda: self.get_all_sales_channels(),
        }

        handler = dispatch.get(entity_type)
        if handler:
            return handler()

        logger.error(f"Неизвестный тип сущности: {entity_type}")
        return None


def get_fetcher(client: Optional[MoySkladClient] = None) -> MoySkladFetcher:
    """
    Factory function для получения fetcher'а

    Args:
        client: Экземпляр MoySkladClient

    Returns:
        Экземпляр MoySkladFetcher
    """
    return MoySkladFetcher(client)


if __name__ == "__main__":
    # Тест
    print("Тест MoySklad Fetcher...")

    fetcher = get_fetcher()

    print("✅ Fetcher инициализирован")
