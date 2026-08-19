"""
RetailCRM API-клиент для модуля кассовых смен БАРХАТ.

Запрашивает заказы, фильтрует наличные платежи, кэширует результаты.
"""

import os
import time
import logging
from datetime import datetime, timedelta
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

# RetailCRM не даёт фильтровать заказы по дате ОПЛАТЫ — только по дате СОЗДАНИЯ.
# Дата создания заказа для этого модуля значения не имеет (заказ мог быть
# оформлен на самовывоз/доставку сильно заранее, а оплачен наличными в салоне
# уже во время смены) — это окно лишь ограничивает, СКОЛЬКО заказов запросить
# у CRM для проверки, а не то, что засчитывается в кассу. Реальный критерий
# попадания в смену — исключительно paidAt конкретного платежа (см. ниже).
#
# Используется при окончательном расчёте (закрытие/пересчёт смены, get_client(),
# без дедлайна) — там точность важнее скорости, пользователь осознанно ждёт.
ORDER_LOOKBACK_DAYS = 30

# Укороченное окно для быстрых/фоновых запросов с бюджетом времени (таблица
# «Открытые смены», виджет текущей смены, get_fast_client()). 30-дневное окно
# на загруженной точке (сотни заказов) не укладывается в бюджет — пагинация
# обрывается по дедлайну, и число вообще не приходит. 7 дней с запасом
# покрывает подавляющее большинство предзаказов и укладывается по времени.
FAST_ORDER_LOOKBACK_DAYS = 7

# Таймаут одного HTTP-запроса к CRM. 30с — для закрытия/пересчёта смены, где
# пользователь осознанно ждёт результат и терять его нельзя. FAST_TIMEOUT — для
# фоновых/справочных запросов (таблица «Открытые смены»), где лучше показать
# устаревшее число, чем занять воркер gunicorn на полминуты: воркеров всего 2,
# и незакэшированные live-запросы к внешнему API уже один раз положили прод.
DEFAULT_TIMEOUT = 30
FAST_TIMEOUT = 6


def _parse_paid_at(paid_at: str) -> Optional[datetime]:
    """Распарсить дату оплаты платежа в naive datetime для сравнения с окном смены."""
    if not paid_at:
        return None
    try:
        if "T" in paid_at:
            return datetime.fromisoformat(paid_at.replace("Z", "+00:00")).replace(tzinfo=None)
        return datetime.strptime(paid_at, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


class CRMDeadlineExceeded(Exception):
    """Запрос к CRM не уложился в отведённый бюджет времени."""
    pass


# =============================================================================
# API КЛИЕНТ
# =============================================================================

class RetailCRMClient:
    """Клиент для работы с RetailCRM API."""

    def __init__(self, api_url: str = None, api_key: str = None, timeout: int = DEFAULT_TIMEOUT):
        self.api_url = (api_url or RETAILCRM_URL).rstrip("/")
        self.api_key = api_key or RETAILCRM_API_KEY
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "X-API-KEY": self.api_key
        })

    def _get(self, endpoint: str, params: Dict = None) -> Dict:
        """Выполнить GET-запрос к API."""
        url = f"{self.api_url}/{endpoint.lstrip('/')}"

        try:
            response = self.session.get(url, params=params, timeout=self.timeout)

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
        limit: int = 100,
        deadline: Optional[float] = None
    ) -> List[Dict[str, Any]]:
        """
        Получить заказы из CRM с фильтрами.

        Args:
            store_code: Код магазина для фильтрации (опционально)
            datetime_start: Начало периода
            datetime_end: Конец периода
            limit: Максимум заказов на страницу
            deadline: Значение time.monotonic(), после которого пагинация
                прерывается с CRMDeadlineExceeded. None — качать до конца
                (закрытие смены: недобрать заказы нельзя, пользователь ждёт).

        Returns:
            Список заказов

        Raises:
            CRMDeadlineExceeded: если дедлайн истёк, а страницы ещё не кончились.
                Намеренно исключение, а не частичный список: неполная сумма
                продаж выглядит как обычное число и молча врёт про кассу.
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
            if deadline is not None and time.monotonic() >= deadline:
                raise CRMDeadlineExceeded(
                    f"Истёк бюджет времени на запрос заказов (store={store_code}, "
                    f"страниц получено: {params['page'] - 1})"
                )

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

    def extract_cash_payments(
        self,
        order: Dict[str, Any],
        datetime_start: Optional[datetime] = None,
        datetime_end: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """
        Извлечь наличные платежи из заказа, попадающие в окно смены по paidAt.

        Args:
            order: Данные заказа из CRM
            datetime_start: Начало окна смены (по дате ОПЛАТЫ, не заказа)
            datetime_end: Конец окна смены

        Returns:
            Список наличных платежей [{amount, paid_at, payment_type}]
        """
        cash_payments = []
        payments = order.get("payments", [])

        # RetailCRM отдаёт payments как объект {payment_id: {...}}, а не массив
        if isinstance(payments, dict):
            payments = list(payments.values())

        for payment in payments:
            payment_type = payment.get("type", "")

            if payment_type != RETAILCRM_CASH_PAYMENT_CODE:
                continue

            # Наличный платеж в салоне
            paid_at = payment.get("paidAt") or order.get("createdAt", "")
            amount = float(payment.get("amount", 0))

            if amount <= 0:
                continue

            # Заказ мог попасть в выборку из-за широкого окна по createdAt
            # (см. ORDER_LOOKBACK_DAYS) — реальную принадлежность к смене
            # определяем по дате оплаты конкретного платежа
            if datetime_start or datetime_end:
                paid_at_dt = _parse_paid_at(paid_at)
                if paid_at_dt is None:
                    logger.warning(
                        f"Не удалось распарсить paidAt='{paid_at}' заказа {order.get('id')}, платёж пропущен"
                    )
                    continue
                if datetime_start and paid_at_dt < datetime_start:
                    continue
                if datetime_end and paid_at_dt > datetime_end:
                    continue

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
        limit: int = 100,
        deadline: Optional[float] = None,
        lookback_days: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Получить все наличные заказы за период.

        Это основная функция для кассовых смен:
        - Запрашивает заказы из CRM (с запасом по дате создания — lookback_days,
          чтобы не потерять предзаказы, оплаченные наличными уже во время смены)
        - Фильтрует по наличным платежам (payments[].type == 'cash-in-shop')
        - Отбирает только платежи, чей paidAt попадает в [datetime_start, datetime_end]
        - Возвращает плоский список наличных транзакций

        Args:
            store_code: Код магазина (например, 'nsk-voskhod-3')
            datetime_start: Начало периода включительно
            datetime_end: Конец периода включительно
            limit: Максимум заказов за один запрос (для пагинации)
            deadline: time.monotonic()-дедлайн на всю пагинацию (см. get_orders)
            lookback_days: на сколько дней раньше datetime_start запрашивать заказы
                (по умолчанию ORDER_LOOKBACK_DAYS=30 — полное окно для окончательного
                расчёта при закрытии смены). Для быстрых/фоновых запросов с дедлайном
                (таблица «Открытые смены») передавайте меньшее значение — иначе для
                загруженной точки пагинация 30 дней не укладывается в бюджет времени
                и результат вообще не приходит, хотя деньги в CRM есть.

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

        # Получаем заказы с запасом по дате создания (предзаказы)
        effective_lookback = lookback_days if lookback_days is not None else ORDER_LOOKBACK_DAYS
        fetch_from = (
            datetime_start - timedelta(days=effective_lookback)
            if datetime_start else None
        )
        orders = self.get_orders(
            store_code=store_code,
            datetime_start=fetch_from,
            datetime_end=datetime_end,
            limit=limit,
            deadline=deadline
        )

        # Извлекаем наличные платежи, попадающие в окно смены по paidAt
        cash_orders = []
        for order in orders:
            payments = self.extract_cash_payments(order, datetime_start, datetime_end)
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
_fast_client: Optional[RetailCRMClient] = None


def get_client() -> RetailCRMClient:
    """Получить глобальный экземпляр клиента (таймаут 30с, для закрытия смены)."""
    global _client
    if _client is None:
        if not RETAILCRM_URL or not RETAILCRM_API_KEY:
            raise RuntimeError("RetailCRM не настроен: задайте RETAILCRM_URL и RETAILCRM_API_KEY")
        _client = RetailCRMClient()
    return _client


def get_fast_client() -> RetailCRMClient:
    """
    Клиент с коротким таймаутом — для справочных запросов, которые нельзя
    давать блокировать воркер (таблица «Открытые смены»).
    """
    global _fast_client
    if _fast_client is None:
        if not RETAILCRM_URL or not RETAILCRM_API_KEY:
            raise RuntimeError("RetailCRM не настроен: задайте RETAILCRM_URL и RETAILCRM_API_KEY")
        _fast_client = RetailCRMClient(timeout=FAST_TIMEOUT)
    return _fast_client


def reset_client():
    """Сбросить глобальный экземпляр (для тестов)."""
    global _client, _fast_client
    _client = None
    _fast_client = None


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
