"""
Pyrus Data Fetcher
Модуль для получения данных из Pyrus с retry-логикой и кэшированием
"""

import time
import logging
from typing import Optional, List, Dict, Callable, Any
from functools import wraps
from .client import PyrusClient, get_client

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


class PyrusFetcher:
    """
    Класс для загрузки данных из Pyrus с обработкой ошибок и логированием
    """

    def __init__(self, client: Optional[PyrusClient] = None):
        """
        Инициализация

        Args:
            client: Экземпляр PyrusClient (если None, создастся новый)
        """
        self.client = client or get_client()
        self._forms_cache: Optional[List[Dict]] = None

    @retry_on_error(max_retries=3, delay=1.0)
    def authenticate(self) -> bool:
        """Авторизация с retry"""
        return self.client.authenticate()

    @retry_on_error(max_retries=3, delay=1.0)
    def get_forms(self, use_cache: bool = True) -> Optional[List[Dict]]:
        """
        Получить список всех форм с кэшированием

        Args:
            use_cache: Использовать кэш если доступно

        Returns:
            Список форм или None
        """
        if use_cache and self._forms_cache is not None:
            logger.debug(f"Используем кэш форм: {len(self._forms_cache)} форм")
            return self._forms_cache

        logger.info("Загружаем формы из Pyrus...")
        forms = self.client.get_forms()

        if forms is not None:
            self._forms_cache = forms
            logger.info(f"✅ Загружено {len(forms)} форм")

        return forms

    @retry_on_error(max_retries=3, delay=1.0)
    def get_form_register(
        self,
        form_id: int,
        include_archived: bool = False,
        max_items: int = 10000,
        created_after: Optional[str] = None,
        created_before: Optional[str] = None
    ) -> Optional[List[Dict]]:
        """
        Получить все задачи из реестра формы с автоматической пагинацией

        Args:
            form_id: ID формы
            include_archived: Включать архивированные задачи
            max_items: Максимальное количество задач для загрузки
            created_after: Фильтр по дате создания с (ISO формат YYYY-MM-DDThh:mm:ssZ)
            created_before: Фильтр по дате создания по (ISO формат YYYY-MM-DDThh:mm:ssZ)

        Returns:
            Список задач или None
        """
        logger.info(f"Загружаем реестр формы {form_id}...")

        all_tasks = []
        offset = 0
        batch_size = 200  # Максимум для Pyrus API

        while len(all_tasks) < max_items:
            tasks = self.client.get_form_register(
                form_id=form_id,
                include_archived=include_archived,
                item_count=batch_size,
                item_offset=offset,
                created_after=created_after,
                created_before=created_before
            )

            if tasks is None:
                logger.error(f"Ошибка загрузки реестра формы {form_id}")
                return None

            if not tasks:
                # Больше нет задач
                break

            all_tasks.extend(tasks)
            logger.info(f"  Загружено {len(tasks)} задач (всего: {len(all_tasks)})")

            # Если получили меньше запрошенного — это последняя страница
            if len(tasks) < batch_size:
                break

            offset += batch_size

        logger.info(f"✅ Всего загружено {len(all_tasks)} задач формы {form_id}")
        return all_tasks

    @retry_on_error(max_retries=3, delay=1.0)
    def get_task(self, task_id: int) -> Optional[Dict]:
        """
        Получить задачу с комментариями

        Args:
            task_id: ID задачи

        Returns:
            Задача или None
        """
        logger.debug(f"Загружаем задачу {task_id}...")
        return self.client.get_task(task_id)

    def get_all_forms_data(
        self,
        form_ids: Optional[List[int]] = None,
        include_archived: bool = False,
        max_items_per_form: int = 10000
    ) -> Dict[int, List[Dict]]:
        """
        Получить данные по нескольким формам

        Args:
            form_ids: Список ID форм (если None, загрузит все формы)
            include_archived: Включать архивированные задачи
            max_items_per_form: Максимум задач на форму

        Returns:
            Словарь {form_id: [tasks]}
        """
        result = {}

        # Получаем формы
        forms = self.get_forms(use_cache=True)
        if forms is None:
            logger.error("Не удалось загрузить формы")
            return result

        # Фильтруем нужные формы
        if form_ids:
            forms = [f for f in forms if f.get('id') in form_ids]

        logger.info(f"Загружаем данные для {len(forms)} форм...")

        for form in forms:
            form_id = form.get('id')
            form_title = form.get('title', 'Unknown')

            logger.info(f"Форма {form_id}: {form_title}")

            tasks = self.get_form_register(
                form_id=form_id,
                include_archived=include_archived,
                max_items=max_items_per_form
            )

            if tasks is not None:
                result[form_id] = tasks

        logger.info(f"✅ Загружено {len(result)} форм с данными")
        return result

    def clear_cache(self) -> None:
        """Очистить кэш форм"""
        self._forms_cache = None
        logger.info("Кэш форм очищен")


def get_fetcher(client: Optional[PyrusClient] = None) -> PyrusFetcher:
    """
    Factory function для получения fetcher'а

    Args:
        client: Экземпляр PyrusClient

    Returns:
        Экземпляр PyrusFetcher
    """
    return PyrusFetcher(client)


if __name__ == "__main__":
    # Тест
    print("Тест Pyrus Fetcher...")

    fetcher = get_fetcher()

    if fetcher.authenticate():
        print("✅ Авторизация успешна!")

        forms = fetcher.get_forms()
        if forms:
            print(f"📋 Формы ({len(forms)}):")
            for form in forms[:5]:
                print(f"  - {form.get('id')}: {form.get('title')}")
    else:
        print("❌ Ошибка авторизации")
