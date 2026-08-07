"""
Pyrus API клиент
Базовый модуль для работы с Pyrus API v4

Авторизация по документации: https://pyrus.com/ru/help/api/authorization
"""

import os
import json
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

# Pyrus API endpoints
PYRUS_AUTH_URL = "https://accounts.pyrus.com/api/v4/auth"


class PyrusClient:
    """Клиент для работы с Pyrus API v4 с Bearer token авторизацией"""

    def __init__(self, login: Optional[str] = None, security_key: Optional[str] = None):
        """
        Инициализация клиента

        Args:
            login: Email в Pyrus (если None, берётся из PYRUS_LOGIN)
            security_key: API ключ (если None, берётся из PYRUS_SECURITY_KEY)
        """
        self.login = login or os.getenv('PYRUS_LOGIN')
        self.security_key = security_key or os.getenv('PYRUS_SECURITY_KEY')

        if not self.login or not self.security_key:
            raise ValueError(
                "Не указаны credentials для Pyrus API. "
                "Установите PYRUS_LOGIN и PYRUS_SECURITY_KEY в .env или передайте в конструктор."
            )

        self.session = requests.Session()
        # Отключаем прокси
        self.session.trust_env = False
        self.session.proxies = {'http': None, 'https': None, 'no_proxy': None}

        # Токен и API URL
        self.access_token: Optional[str] = None
        self.api_url: str = "https://api.pyrus.com/v4/"
        self.files_url: Optional[str] = None

    def authenticate(self) -> bool:
        """
        Авторизация через POST /auth

        Returns:
            True если авторизация успешна
        """
        try:
            logger.info(f"Авторизация пользователя: {self.login}")

            response = self.session.post(
                PYRUS_AUTH_URL,
                json={
                    'login': self.login,
                    'security_key': self.security_key
                },
                headers={'Content-Type': 'application/json'}
            )

            response.raise_for_status()
            data = response.json()

            self.access_token = data.get('access_token')
            self.api_url = data.get('api_url', 'https://api.pyrus.com/v4/')
            self.files_url = data.get('files_url')

            if self.access_token:
                logger.info("✅ Авторизация успешна!")
                logger.debug(f"API URL: {self.api_url}")
                return True
            else:
                logger.error("❌ Не удалось получить access_token")
                return False

        except requests.exceptions.RequestException as e:
            logger.error(f"❌ Ошибка авторизации: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Response: {e.response.text}")
            return False

    def _get_auth_headers(self) -> Dict[str, str]:
        """Получить заголовки с Bearer token"""
        if not self.access_token:
            raise ValueError("Не авторизован. Вызовите authenticate() сначала.")

        return {
            'Authorization': f'Bearer {self.access_token}',
            'Content-Type': 'application/json'
        }

    def request(self, method: str, path: str, **kwargs) -> Optional[Dict]:
        """
        Выполнение запроса к Pyrus API с Bearer token

        Args:
            method: HTTP метод (GET, POST, etc)
            path: API path (например, /forms)
            **kwargs: дополнительные аргументы для requests

        Returns:
            JSON ответ или None в случае ошибки
        """
        # Авторизуемся если нужно
        if not self.access_token:
            if not self.authenticate():
                return None

        url = f"{self.api_url.rstrip('/')}{path}"

        # Устанавливаем headers
        headers = kwargs.pop('headers', {})
        headers.update(self._get_auth_headers())
        kwargs['headers'] = headers

        # Если есть json в kwargs — сериализуем
        if 'json' in kwargs:
            kwargs['data'] = json.dumps(kwargs.pop('json'), ensure_ascii=False)

        try:
            response = self.session.request(method, url, **kwargs)
            response.raise_for_status()
            return response.json()

        except requests.exceptions.RequestException as e:
            logger.error(f"Ошибка запроса {method} {path}: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Response: {e.response.text}")
                # Если токен истек, пробуем переавторизоваться
                if e.response is not None and e.response.status_code == 401:
                    logger.info("Токен истек, пробуем переавторизоваться...")
                    self.access_token = None
                    if self.authenticate():
                        # Повторяем запрос
                        headers = kwargs.pop('headers', {})
                        headers.update(self._get_auth_headers())
                        kwargs['headers'] = headers
                        if 'json' in kwargs:
                            kwargs['data'] = json.dumps(kwargs.pop('json'), ensure_ascii=False)
                        response = self.session.request(method, url, **kwargs)
                        response.raise_for_status()
                        return response.json()
            return None

    def get(self, path: str, **kwargs) -> Optional[Dict]:
        """GET запрос"""
        return self.request('GET', path, **kwargs)

    def post(self, path: str, **kwargs) -> Optional[Dict]:
        """POST запрос"""
        return self.request('POST', path, **kwargs)

    def get_forms(self) -> Optional[List[Dict]]:
        """
        Получить список всех форм

        Returns:
            Список форм или None в случае ошибки
        """
        try:
            response = self.get('/forms')
            if response and 'forms' in response:
                forms = response['forms']
                logger.info(f"Получено {len(forms)} форм")
                return forms
            return None
        except Exception as e:
            logger.error(f"Ошибка получения форм: {e}")
            return None

    def get_form_register(
        self,
        form_id: int,
        include_archived: bool = False,
        item_count: int = 100,
        item_offset: int = 0,
        created_after: Optional[str] = None,
        created_before: Optional[str] = None
    ) -> Optional[List[Dict]]:
        """
        Получить реестр задач по форме

        Args:
            form_id: ID формы
            include_archived: Включать архивированные задачи
            item_count: Количество задач (макс 20000)
            item_offset: Смещение для пагинации
            created_after: Фильтр по дате создания с (ISO формат YYYY-MM-DDThh:mm:ssZ)
            created_before: Фильтр по дате создания по (ISO формат YYYY-MM-DDThh:mm:ssZ)

        Returns:
            Список задач или None в случае ошибки
        """
        try:
            params = {
                'include_archived': 'y' if include_archived else None,
                'item_count': min(item_count, 20000),
                'item_offset': item_offset
            }

            # Добавляем фильтры по дате если указаны
            if created_after:
                params['created_after'] = created_after
            if created_before:
                params['created_before'] = created_before

            # Убираем None значения
            params = {k: v for k, v in params.items() if v is not None}

            response = self.get(f'/forms/{form_id}/register', params=params)

            if response and 'tasks' in response:
                tasks = response['tasks']
                logger.info(f"Получено {len(tasks)} задач для формы {form_id}")
                return tasks
            return None

        except Exception as e:
            logger.error(f"Ошибка получения реестра формы {form_id}: {e}")
            return None

    def get_task(self, task_id: int) -> Optional[Dict]:
        """
        Получить задачу с комментариями

        Args:
            task_id: ID задачи

        Returns:
            Задача или None в случае ошибки
        """
        try:
            response = self.get(f'/tasks/{task_id}')
            if response and 'task' in response:
                return response['task']
            return None
        except Exception as e:
            logger.error(f"Ошибка получения задачи {task_id}: {e}")
            return None


def get_client() -> PyrusClient:
    """
    Factory function для получения клиента

    Returns:
        Экземпляр PyrusClient
    """
    return PyrusClient()


if __name__ == "__main__":
    # Тест подключения
    print("Тест подключения к Pyrus API v4...")

    client = get_client()
    if client.authenticate():
        print("✅ Подключение успешно!")

        forms = client.get_forms()
        if forms:
            print(f"📋 Получено {len(forms)} форм:")
            for form in forms[:5]:  # Первые 5 форм
                print(f"  - {form.get('id')}: {form.get('title')}")
    else:
        print("❌ Ошибка подключения. Проверьте credentials в .env")
