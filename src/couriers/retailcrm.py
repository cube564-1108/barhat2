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


# Кастомное поле «время готовности заказа». Разведка 2026-09-05 (4000 заказов):
# заполнено у 100% заказов и расходится с delivery.time.from у 70% — у доставки
# готовность раньше выезда на 10–60 минут, у самовывоза бывает и позже. Выводить
# готовность из времени доставки нельзя, это разные величины.
READY_TIME_FIELD = "order_availability_time"

# Поле текстовое, его заполняет человек. В выборке встречались «9:00» без
# ведущего нуля, «уточ», «ут», «уточнить», «Ждем уточнений» — около 1% заказов.
# Поэтому разбор терпимый к формату, но не «угадывающий»: что не разобралось,
# остаётся пустым и попадает в строку «требует уточнения», а не в 00:00.
_TIME_RE = re.compile(r"^\s*(\d{1,2})\s*[:.\-]\s*(\d{1,2})\s*$")
_HOUR_ONLY_RE = re.compile(r"^\s*(\d{1,2})\s*(?:ч|час|часов|:00)?\s*$", re.IGNORECASE)


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

    def get_statuses(self) -> List[Dict[str, Any]]:
        """
        Справочник статусов заказа с группой.

        Группа (`new`, `approval`, `assembling`, `delivery`, `complete`,
        `cancel`) — то, из чего сидируется признак «считать нагрузкой»: в CRM
        41 статус, и заполнять их руками — ровно тот ручной труд, который
        должен делать агент.
        """
        data = self._get("api/v5/reference/statuses")
        return [
            {
                "code": code,
                "name": item.get("name") or code,
                "group_code": item.get("group"),
                "active": bool(item.get("active", True)),
            }
            for code, item in (data.get("statuses") or {}).items()
        ]

    def iter_orders_by_delivery_date(
        self,
        date_from: str,
        date_to: str,
        status: Optional[str] = None,
        deadline: Optional[float] = None,
    ) -> Iterator[List[Dict[str, Any]]]:
        """
        Страницы заказов с датой доставки в [date_from, date_to].

        status=None — все статусы. Именно так ходит синк с 2026-09-05: витрина
        общая для выплат, показателей салонов и загрузки салонов, а будущий
        заказ по определению не «Выполнен». Отдельный проход только за
        будущим был бы вторым запросом к CRM ради тех же дат; отбор по статусу
        и так стоит в каждом чтении (см. COMPLETED_STATUS в storage.py), а
        отменённые заказы стоят всего ~5% объёма.

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

            params = {
                "filter[deliveryDateFrom]": date_from,
                "filter[deliveryDateTo]": date_to,
                "limit": PAGE_LIMIT,
                "page": page,
            }
            if status:
                params["filter[extendedStatus][]"] = [status]

            data = self._get("api/v5/orders", params)

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


def parse_time_value(value: Any) -> Optional[str]:
    """
    Человеческая запись времени → «HH:MM». None — это не время.

    Разбираем «10:20», «9:00», «9.00», «9-00», «18», «18ч». Не разбираем
    «уточ», «Ждем уточнений», «уточнить заказ был на вчера до 00» — такие
    значения обязаны остаться пустыми и попасть человеку на разбор.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    match = _TIME_RE.match(text)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
    else:
        match = _HOUR_ONLY_RE.match(text)
        if not match:
            return None
        hour, minute = int(match.group(1)), 0

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"


def ready_slot(order: Dict[str, Any]) -> Dict[str, Optional[Any]]:
    """
    Время готовности заказа → час слота.

    Отдельная функция с тестом, а не ветка внутри parse_order: у самовывоза,
    у интервальной доставки и у заказа «на сейчас» логика разная, и правило
    вывода слота должно быть одним местом, которое можно прогнать на реальной
    выгрузке (scripts/probe_crm_order_slots.py).

    Часовой пояс не трогаем. Время в CRM — стенные часы салона: менеджер
    вводит его так, как видит флорист. Разведка 2026-09-05 это подтверждает —
    у салонов из UTC+5 и UTC+7 рабочее окно одинаковое (9:00–22:00), сдвига
    между поясами в данных нет. Конвертировать здесь что-либо — значит сдвинуть
    всю сетку на 2 часа у половины салонов.

    Возвращает ready_time (HH:MM или None), ready_hour (0–23 или None) и
    ready_source: откуда взято значение. Источник хранится не для отладки —
    когда сетка поедет, первый вопрос будет «а из какого поля мы взяли час».
    """
    custom = order.get("customFields") or {}
    delivery = order.get("delivery") or {}
    time_block = delivery.get("time") or {}

    candidates = (
        ("availability", custom.get(READY_TIME_FIELD)),
        ("delivery_from", time_block.get("from")),
    )
    for source, raw in candidates:
        parsed = parse_time_value(raw)
        if parsed:
            return {
                "ready_time": parsed,
                "ready_hour": int(parsed[:2]),
                "ready_source": source,
            }

    # Значение есть, но это не время («уточ») — отличаем от «поля нет вовсе»:
    # первое разбирает человек, второе означает заказ без времени.
    # str() обязателен: кастомное поле правится в CRM и может прийти числом или
    # булевым, а .strip() на нестроке уронил бы весь прогон синка.
    raw_availability = str(custom.get(READY_TIME_FIELD) or "").strip()
    return {
        "ready_time": None,
        "ready_hour": None,
        "ready_source": "unparsed" if raw_availability else None,
    }


def parse_items(order: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Позиции заказа → строки order_items.

    Ключ — offer.id: внутренний идентификатор CRM, заполнен у 100% позиций и
    не меняется при переименовании товара (разведка 2026-09-05). Артикул и
    название кладём для человека, ключом они быть не могут.
    """
    rows = []
    for item in order.get("items") or []:
        offer = item.get("offer") or {}
        offer_id = offer.get("id")
        if offer_id is None:
            continue
        rows.append({
            "offer_id": int(offer_id),
            "product_name": offer.get("displayName") or offer.get("name"),
            "article": offer.get("article"),
            "quantity": float(item.get("quantity") or 0),
        })
    return rows


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
    slot = ready_slot(order)

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
        # Поля модуля «Загрузка салонов».
        # store_key — склад-исполнитель, заполнен у 100% заказов (разведка
        # 2026-09-05). Это именно он, а не site: сайт говорит, откуда пришёл
        # заказ, а собирает букет склад.
        "store_key": order.get("shipmentStore"),
        "ready_time": slot["ready_time"],
        "ready_hour": slot["ready_hour"],
        "ready_source": slot["ready_source"],
        "items": parse_items(order),
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
