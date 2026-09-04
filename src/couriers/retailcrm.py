"""
Клиент RetailCRM для модуля «Оплата курьерам».

Отдельный от src/cashshifts/retailcrm_client.py намеренно: тот заточен под
кассовые смены (окно по дате СОЗДАНИЯ заказа, наличные платежи, дедлайны на
фоновые запросы), и подмешивать в него фильтры по дате доставки — значит
рисковать деньгами в кассе ради отчёта. Здесь нужен другой срез: заказы по
дате ДОСТАВКИ в статусе «Выполнен».

Что проверено на живом API 2026-08-24:
- курьер приходит в списочном ответе: delivery.data.courierId / firstName;
- себестоимость доставки — delivery.netCost;
- дата доставки — delivery.date, календарная (без времени и без московского
  сдвига, в отличие от paidAt/createdAt — см. _MOSCOW_TZ в cashshifts);
- limit принимает только 20/50/100, иначе 400 Errors in the pagination parameters.
"""

import logging
import os
import re
import time
from typing import Any, Dict, Iterator, List, Optional

import requests

from russian_ca import trust_russian_ca

logger = logging.getLogger(__name__)

RETAILCRM_URL = os.environ.get("RETAILCRM_URL")
RETAILCRM_API_KEY = os.environ.get("RETAILCRM_API_KEY")

# Таймаут одного HTTP-запроса. Внешний вызов без таймаута уже дважды укладывал
# прод: воркеров всего 2, и зависший запрос занимает целый воркер.
REQUEST_TIMEOUT = 30

# RetailCRM принимает только 20/50/100
PAGE_LIMIT = 100

# Пауза между страницами: синк фоновый, торопиться некуда, а сплошной поток
# запросов и нагружает CRM, и держит воркер занятым.
PAGE_PAUSE_SECONDS = 0.2

# Города салонов по первому слову названия сайта в RetailCRM
# («НСК Восход 3» → Новосибирск). Белый список, а не любое первое слово:
# среди сайтов есть служебные («Заказы сайтов», «Заявки с сайта», invisible),
# из которых иначе получились бы города-призраки в фильтре.
CITY_ALIASES = {
    "нск": "Новосибирск",
    "новосибирск": "Новосибирск",
    "академ": "Новосибирск",
    "екб": "Екатеринбург",
    "екатеринбург": "Екатеринбург",
    "барнаул": "Барнаул",
    "томск": "Томск",
    "челябинск": "Челябинск",
}

# Штатный курьер в CRM называется «Курьер <номер> <город>» — всё остальное
# (Яндекс Доставка, Купер Курьер, Максим Такси, «Общий») это служба доставки.
# Эвристика применяется только к новым записям справочника: выставленный
# руками флаг синхронизация не трогает (см. storage.upsert_couriers).
_OWN_COURIER_RE = re.compile(r"^\s*курьер\s+\d+", re.IGNORECASE)


class RetailCRMError(Exception):
    """Ошибка обращения к RetailCRM."""


def is_configured() -> bool:
    return bool(RETAILCRM_URL and RETAILCRM_API_KEY)


def city_from_site_name(site_name: str) -> Optional[str]:
    """«НСК Восход 3» → «Новосибирск»; служебные сайты → None."""
    if not site_name:
        return None
    first_word = site_name.strip().split()[0].lower().strip(",.")
    city = CITY_ALIASES.get(first_word)
    if not city:
        logger.debug(f"Город салона не определён по названию '{site_name}'")
    return city


def guess_is_service(courier_name: str) -> bool:
    """True — это служба доставки/агрегатор, а не штатный курьер."""
    return not bool(_OWN_COURIER_RE.match(courier_name or ""))


class CourierOrdersClient:
    """Минимальный клиент: заказы по дате доставки + справочники курьеров и салонов."""

    def __init__(self, api_url: str = None, api_key: str = None, timeout: int = REQUEST_TIMEOUT):
        self.api_url = (api_url or RETAILCRM_URL or "").rstrip("/")
        self.api_key = api_key or RETAILCRM_API_KEY
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"X-API-KEY": self.api_key or ""})
        trust_russian_ca(self.session)

    def _get(self, endpoint: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        if not self.api_url or not self.api_key:
            raise RetailCRMError("RetailCRM не настроен: задайте RETAILCRM_URL и RETAILCRM_API_KEY")

        url = f"{self.api_url}/{endpoint.lstrip('/')}"
        try:
            response = self.session.get(url, params=params, timeout=self.timeout)
        except requests.exceptions.RequestException as e:
            raise RetailCRMError(f"Сеть/таймаут при запросе {endpoint}: {e}") from e

        if not response.ok:
            logger.error(f"RetailCRM {endpoint} -> {response.status_code}: {response.text[:500]}")
            raise RetailCRMError(f"RetailCRM вернул {response.status_code} на {endpoint}")

        data = response.json()
        if not isinstance(data, dict):
            raise RetailCRMError(f"Неожиданный формат ответа {endpoint}: {type(data).__name__}")
        return data

    # ------------------------------------------------------------------
    # Справочники
    # ------------------------------------------------------------------

    def get_couriers(self) -> List[Dict[str, Any]]:
        """Справочник курьеров с эвристикой «свой / служба доставки»."""
        data = self._get("api/v5/reference/couriers")
        couriers = []
        for item in data.get("couriers", []):
            name = " ".join(
                part for part in [item.get("firstName"), item.get("lastName")] if part
            ).strip()
            couriers.append({
                "id": item.get("id"),
                "name": name or f"Курьер {item.get('id')}",
                "active": bool(item.get("active", True)),
                "is_service": guess_is_service(name),
            })
        return [c for c in couriers if c["id"] is not None]

    def get_delivery_types(self) -> List[Dict[str, Any]]:
        """Справочник типов доставки: от него считается доля такси-служб."""
        data = self._get("api/v5/reference/delivery-types")
        return [
            {"code": code, "name": item.get("name") or code, "active": bool(item.get("active", True))}
            for code, item in (data.get("deliveryTypes") or {}).items()
        ]

    def get_sites(self) -> List[Dict[str, Any]]:
        """Справочник салонов с определённым городом."""
        data = self._get("api/v5/reference/sites")
        sites = []
        for code, item in (data.get("sites") or {}).items():
            name = item.get("name") or code
            sites.append({"code": code, "name": name, "city": city_from_site_name(name)})
        return sites

    # ------------------------------------------------------------------
    # Заказы
    # ------------------------------------------------------------------

    def iter_completed_orders(
        self,
        date_from: str,
        date_to: str,
        status: str,
        deadline: Optional[float] = None,
    ) -> Iterator[List[Dict[str, Any]]]:
        """
        Страницы заказов в статусе `status` с датой доставки в [date_from, date_to].

        Отдаём страницами, а не одним списком: вызывающий пишет прогресс и
        продлевает лок между страницами, а память не держит десятки тысяч
        заказов разом.

        deadline — значение time.monotonic(), после которого прогон
        прерывается ошибкой. Молча возвращать неполную выборку нельзя:
        недосчитанная сумма выглядит как обычное число и тихо врёт про выплату.
        """
        page = 1
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                raise RetailCRMError(
                    f"Истёк бюджет времени на выгрузку заказов "
                    f"({date_from}—{date_to}, страниц получено: {page - 1})"
                )

            data = self._get("api/v5/orders", {
                "filter[deliveryDateFrom]": date_from,
                "filter[deliveryDateTo]": date_to,
                "filter[extendedStatus][]": [status],
                "limit": PAGE_LIMIT,
                "page": page,
            })

            if not data.get("success", False):
                raise RetailCRMError(f"RetailCRM отклонил запрос заказов: {data.get('errorMsg')}")

            orders = data.get("orders", [])
            if not orders:
                return

            yield orders

            if len(orders) < PAGE_LIMIT:
                return

            page += 1
            time.sleep(PAGE_PAUSE_SECONDS)


def parse_order(order: Dict[str, Any], site_cities: Dict[str, Optional[str]]) -> Optional[Dict[str, Any]]:
    """
    Заказ RetailCRM → строка для courier_orders. None — заказ без даты доставки
    (в отчёт по периоду он всё равно попасть не может).
    """
    delivery = order.get("delivery") or {}
    delivery_date = delivery.get("date")
    if not delivery_date:
        return None

    courier = delivery.get("data") or {}
    courier_id = courier.get("courierId") or courier.get("id")
    courier_name = " ".join(
        part for part in [courier.get("firstName"), courier.get("lastName")] if part
    ).strip()

    site_code = order.get("site")
    address = delivery.get("address") or {}

    return {
        "retailcrm_order_id": order.get("id"),
        "order_number": order.get("number"),
        "delivery_date": delivery_date,
        "courier_id": int(courier_id) if courier_id else None,
        "courier_name": courier_name or None,
        # netCost бывает пустым (самовывоз, доставка без себестоимости) — это
        # не ошибка, просто ноль в выплате
        "net_cost": float(delivery.get("netCost") or 0),
        "site_code": site_code,
        "city": site_cities.get(site_code),
        "delivery_city": address.get("city"),
        "status": order.get("status"),
        # Поля для показателей салонов. summ — стоимость товаров БЕЗ доставки
        # (решение владельца); totalSumm включал бы доставку и скидки
        "total_summ": float(order.get("summ") or 0),
        "order_method": order.get("orderMethod"),
        "delivery_code": delivery.get("code"),
    }


_client: Optional[CourierOrdersClient] = None


def get_client() -> CourierOrdersClient:
    global _client
    if _client is None:
        _client = CourierOrdersClient()
    return _client


def reset_client() -> None:
    """Сбросить глобальный экземпляр (тесты)."""
    global _client
    _client = None
