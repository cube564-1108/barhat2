"""
RetailCRM API-клиент для модуля кассовых смен БАРХАТ.

Запрашивает заказы, фильтрует наличные платежи, кэширует результаты.
"""

import os
import logging
from datetime import datetime
from typing import List, Dict, Any, Optional
import requests

from .seed_data import RETAILCRM_CASH_PAYMENT_CODE

logger = logging.getLogger(__name__)

# =============================================================================
# КОНФИГУРАЦИЯ
# =============================================================================

RETAILCRM_URL = os.environ.get("RETAILCRM_URL")
RETAILCRM_API_KEY = os.environ.get("RETAILCRM_API_KEY")

if not RETAILCRM_URL or not RETAILCRM_API_KEY:
    logger.warning("RETAILCRM_URL или RETAILCRM_API_KEY не заданы — интеграция отключена")


# =============================================================================
# API КЛИЕНТ
# =============================================================================

class RetailCRMClient:
    """Клиент для работы с RetailCRM API."""

    def __init__(self, api_url: str = None, api_key: str = None):
        self.api_url = (api_url or RETAILCRM_URL).rstrip("/")
        self.api_key = api_key or RETAILCRM_API_KEY
        self.session = requests.Session()
        self.session.headers.update({
            "X-API-KEY": self.api_key
        })

    def _get(self, endpoint: str, params: Dict = None) -> Dict:
        """Выполнить GET-запрос к API."""
        url = f"{self.api_url}/{endpoint.lstrip('/')}"

        try:
            response = self.session.get(url, params=params, timeout=30)

            # Логируем тело ответа при ошибке (ПЕРЕД raise_for_status)
            if not response.ok:
                logger.error(f"RetailCRM API error {response.status_code}: {response.text}")

            response.raise_for_status()
            data = response.json()

            # Защита от неожиданного формата ответа (например, двойной JSON-энкодинг)
            if not isinstance(data, dict):
                logger.error(f"RetailCRM API вернул не словарь (type={type(data).__name__}): {response.text[:1000]}")
                raise ValueError(f"Неожиданный формат ответа RetailCRM: {type(data).__name__}")

            return data
        except requests.exceptions.RequestException as e:
            # Логируем детали запроса для отладки
            logger.error(f"RetailCRM API error: {e}")
            logger.error(f"Request URL: {url}")
            logger.error(f"Request params: {params}")
            logger.error(f"API Key configured: {bool(self.api_key)}")
            raise

    def get_orders(
        self,
        store_code: Optional[str] = None,
        datetime_start: Optional[datetime] = None,
        datetime_end: Optional[datetime] = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        Получить заказы из CRM с фильтрами.

        Args:
            store_code: Код магазина для фильтрации (опционально)
            datetime_start: Начало периода
            datetime_end: Конец периода
            limit: Максимум заказов

        Returns:
            Список заказов
        """
        params = {"limit": limit}

        if store_code:
            params["filter[sites][]"] = [store_code]

        if datetime_start:
            params["filter[createdAtFrom]"] = datetime_start.strftime("%Y-%m-%d %H:%M:%S")

        if datetime_end:
            params["filter[createdAtTo]"] = datetime_end.strftime("%Y-%m-%d %H:%M:%S")

        # Добавляем пагинацию для получения всех заказов
        params["page"] = 1

        all_orders = []
        while True:
            data = self._get("api/v5/orders", params=params)

            # Проверяем success флаг
            if not data.get("success", False):
                break

            orders = data.get("orders", [])

            if not orders:
                break

            all_orders.extend(orders)

            # Проверяем, есть ли ещё страницы
            if len(orders) < limit:
                break

            params["page"] += 1

        return all_orders

    def extract_cash_payments(self, order: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Извлечь наличные платежи из заказа.

        Args:
            order: Данные заказа из CRM

        Returns:
            Список наличных платежей [{amount, paid_at, payment_type}]
        """
        cash_payments = []
        payments = order.get("payments", [])

        for payment in payments:
            payment_type = payment.get("type", "")

            if payment_type == RETAILCRM_CASH_PAYMENT_CODE:
                # Наличный платеж в салоне
                paid_at = payment.get("paidAt") or order.get("createdAt", "")
                amount = float(payment.get("amount", 0))

                if amount > 0:
                    cash_payments.append({
                        "amount": amount,
                        "paid_at": paid_at,
                        "payment_type": payment_type,
                        "order_id": order.get("id"),
                        "order_number": order.get("number"),
                    })

        return cash_payments

    def get_cash_orders(
        self,
        store_code: Optional[str] = None,
        datetime_start: Optional[datetime] = None,
        datetime_end: Optional[datetime] = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        Получить все наличные заказы за период.

        Это основная функция для кассовых смен:
        - Запрашивает заказы из CRM
        - Фильтрует по наличным платежам (payments[].type == 'cash-in-shop')
        - Возвращает плоский список наличных транзакций

        Args:
            store_code: Код магазина (например, 'nsk-voskhod-3')
            datetime_start: Начало периода включительно
            datetime_end: Конец периода включительно
            limit: Максимум заказов за один запрос (для пагинации)

        Returns:
            Список наличных платежей:
            [
                {
                    "retailcrm_order_id": 12345,
                    "order_number": "A-12345",
                    "amount": 2500.00,
                    "paid_at": "2026-08-14 12:34:56",
                    "store_code": "nsk-voskhod-3"
                },
                ...
            ]
        """
        if not self.api_url or not self.api_key:
            logger.error("RetailCRM не настроен — не могу получить заказы")
            return []

        logger.info(
            f"Запрос наличных заказов: store={store_code}, "
            f"period={datetime_start} — {datetime_end}"
        )

        # Получаем заказы
        orders = self.get_orders(
            store_code=store_code,
            datetime_start=datetime_start,
            datetime_end=datetime_end,
            limit=limit
        )

        # Извлекаем наличные платежи
        cash_orders = []
        for order in orders:
            payments = self.extract_cash_payments(order)
            for payment in payments:
                cash_orders.append({
                    "retailcrm_order_id": payment["order_id"],
                    "order_number": payment["order_number"],
                    "amount": payment["amount"],
                    "paid_at": payment["paid_at"],
                    "store_code": store_code or "unknown",
                    "order_data": None  # Можно добавить полный JSON заказа при необходимости
                })

        logger.info(f"Найдено {len(cash_orders)} наличных платежей")
        return cash_orders


# =============================================================================
# ГЛОБАЛЬНЫЙ ЭКЗЕМПЛЯР
# =============================================================================

_client: Optional[RetailCRMClient] = None


def get_client() -> RetailCRMClient:
    """Получить глобальный экземпляр клиента."""
    global _client
    if _client is None:
        if not RETAILCRM_URL or not RETAILCRM_API_KEY:
            raise RuntimeError("RetailCRM не настроен: задайте RETAILCRM_URL и RETAILCRM_API_KEY")
        _client = RetailCRMClient()
    return _client


def reset_client():
    """Сбросить глобальный экземпляр (для тестов)."""
    global _client
    _client = None


# =============================================================================
# ПОЛЕЗНЫЕ ФУНКЦИИ
# =============================================================================

def get_store_code_from_name(store_name: str) -> str:
    """
    Преобразовать название точки в код магазина RetailCRM.

    Маппинг названий точек БАРХАТ на коды в RetailCRM.

    Args:
        store_name: Название точки (например, "НСК Восход, 3")

    Returns:
        Код магазина (например, "nsk-voskhod-3")
    """
    # Маппинг названий на реальные коды в RetailCRM
    STORE_CODE_MAPPING = {
        "Барнаул Лазурная": "barkhat-barnaul2",
        "Барнаул Советская": "barkhat-barnaul",
        "ЕКБ Бажова": "barkhat-ekb",
        "НСК Блюхера, 61": "barkhat-nsk-levyi",
        "НСК Восход, 3": "nsk-voskhod-3",
        "НСК Железнодорожная, 15/1": "nsk-zheleznodorozhnaia-15-1",
        "Томск Дальне-Ключевская, 16а": "barkhat-tomsk",
        "Челябинск Цвиллинга, 59": "cheliabinsk-tsvillinga-59",
        "Челябинск пр-кт Свердловский, д 23": "cheliabinsk-sverdl-pr-23",
    }

    code = STORE_CODE_MAPPING.get(store_name)

    if not code:
        logger.warning(f"Не найден код CRM для точки '{store_name}', используется fallback")
        # Fallback: простая трансформация для неизвестных точек
        code = (
            store_name.lower()
            .replace(" ", "-")
            .replace(",", "")
            .replace("ё", "е")
        )

    return code


def format_paid_at(paid_at: str) -> str:
    """
    Привести дату оплаты к стандартному формату.

    Args:
        paid_at: Дата в любом формате из CRM

    Returns:
        Дата в ISO8601 (YYYY-MM-DD HH:MM:SS)
    """
    try:
        # Пытаемся распарсить дату
        if "T" in paid_at:
            # ISO8601 с timezone
            dt = datetime.fromisoformat(paid_at.replace("Z", "+00:00"))
            # Убираем timezone
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        else:
            # Уже без timezone
            return paid_at
    except Exception as e:
        logger.warning(f"Не удалось распарсить дату {paid_at}: {e}")
        return paid_at


# =============================================================================
# ТЕСТЫ (запуск через python -m cashshifts.retailcrm_client)
# =============================================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    print("=" * 60)
    print("RetailCRM КЛИЕНТ — ТЕСТ")
    print("=" * 60)

    # Проверяем конфигурацию
    print(f"\nAPI URL: {RETAILCRM_URL}")
    print(f"API Key: {'{' + RETAILCRM_API_KEY[:10] + '...' if RETAILCRM_API_KEY else 'None'}")
    print(f"Код оплаты наличными: {RETAILCRM_CASH_PAYMENT_CODE}")

    if not RETAILCRM_URL or not RETAILCRM_API_KEY:
        print("\n❌ RetailCRM не настроен — задайте переменные окружения")
        exit(1)

    # Пробуем получить заказы за сегодня
    from datetime import timedelta
    client = get_client()

    today = datetime.now()
    yesterday = today - timedelta(days=1)

    print(f"\n📅 Период: {yesterday.strftime('%Y-%m-%d %H:%M')} — {today.strftime('%Y-%m-%d %H:%M')}")

    try:
        # Для теста берём все заказы (без фильтра по магазину)
        orders = client.get_cash_orders(
            datetime_start=yesterday,
            datetime_end=today
        )

        print(f"\n✅ Успешно! Получено {len(orders)} наличных платежей")

        if orders:
            total = sum(o["amount"] for o in orders)
            print(f"💰 Общая сумма: {total:.2f} руб.")

            print("\nПервые 5 заказов:")
            for i, order in enumerate(orders[:5], 1):
                print(f"  {i}. Заказ #{order['order_number']} → {order['amount']:.2f} руб. ({order['paid_at']})")
    except Exception as e:
        print(f"\n❌ Ошибка: {e}")
        import traceback
        traceback.print_exc()
