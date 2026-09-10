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

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Iterator, List, Optional

# Пояс аккаунта RetailCRM: в нём приходят createdAt и записи истории, и с ним
# же сравнивается фильтр startDate (замер 2026-09-08). Держим здесь ссылку на
# единственное определение, чтобы значение не разъехалось по файлам.
from .salon_time import CRM_ACCOUNT_UTC_OFFSET

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


# Кастомные поля карточки курьера. Имена найдены разведкой 2026-09-08
# (scripts/probe_crm_courier_fields.py), в скобках — доля заполненности среди
# доставочных заказов дня.
#
# Получателя заводят не всегда (36%), поэтому курьеру показывается либо он,
# либо заказчик — но подписью «клиент», чтобы курьер понимал, кому звонит:
# сюрприз-доставка у цветов обычное дело.
RECIPIENT_NAME_FIELD = "recipient_name"            # 36%
RECIPIENT_PHONE_FIELD = "recipient_phone"          # 33%
RECIPIENT_IS_CUSTOMER_FIELD = "recipient_customer"  # 8% = True
# «Не связываться с получателем» (5%) — сюрприз. Для курьера это важнее
# половины карточки: звонок ломает подарок.
DO_NOT_CONTACT_FIELD = "ne_sviazyvatsia_s_poluchatelem"
# Ещё один комментарий оператора, отдельный от managerComment (24%).
NOTE_TEXT_FIELD = "note_text"
# Плановые дата и время готовности одной отметкой (100%) — главный ориентир
# курьера: статус «Заказ готов» ставят в момент начала окна доставки, а у трети
# заказов уже после него.
READY_PLANNED_FIELD = "data_i_vremia_gotovnosti"

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

    # ------------------------------------------------------------------
    # Каталог номенклатуры (модель нагрузки в минутах, план 2026-09-07)
    # ------------------------------------------------------------------

    def get_product_groups(self) -> List[Dict[str, Any]]:
        """
        Дерево групп товаров: по нему задаётся норма времени сборки.

        Групп 98 (разведка 2026-09-07), помещаются в одну страницу. Дерево
        нужно целиком, включая витринные группы («Женщине», «Акции»): у товара
        их в среднем 20, и отличить товарную от витринной можно только тем, что
        человек разметил первую, а вторую — нет.
        """
        data = self._get("api/v5/store/product-groups", {"limit": PAGE_LIMIT})
        if not data.get("success", False):
            raise RetailCRMError(f"RetailCRM отклонил запрос групп: {data.get('errorMsg')}")
        return [
            {
                "id": item.get("id"),
                "parent_id": item.get("parentId"),
                "name": item.get("name") or f"Группа {item.get('id')}",
                "active": bool(item.get("active", True)),
            }
            for item in (data.get("productGroup") or [])
            if item.get("id") is not None
        ]

    def iter_products(self, deadline: Optional[float] = None) -> Iterator[List[Dict[str, Any]]]:
        """
        Страницы каталога: торговые предложения с единицей измерения и группами.

        `offer.unit` — то, ради чего это всё: количество в позиции заказа
        меряется штуками у букета и граммами у клубники, и это приходит
        данными, а не выводится из названия (CLAUDE.md, «количество из внешней
        системы — не безразмерное число»).

        Архивные товары НЕ отфильтровываются: позиция старого заказа ссылается
        на offer, которого уже нет в активных, и без него заказ ушёл бы в
        «без нормы» по причине, которую человек не может исправить (К1).

        `limit` принимает только 20, 50 или 100 — на других значениях CRM
        отвечает 400 (проверено разведкой 2026-09-07).
        """
        page = 1
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                raise RetailCRMError(
                    f"Истёк бюджет времени на выгрузку каталога "
                    f"(страниц получено: {page - 1})"
                )

            data = self._get("api/v5/store/products",
                             {"limit": PAGE_LIMIT, "page": page})
            if not data.get("success", False):
                raise RetailCRMError(f"RetailCRM отклонил запрос товаров: {data.get('errorMsg')}")

            products = data.get("products") or []
            if not products:
                return

            yield products

            if len(products) < PAGE_LIMIT:
                return

            page += 1
            time.sleep(PAGE_PAUSE_SECONDS)

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

    # ------------------------------------------------------------------
    # Лента изменений (модуль «Курьеры: доставка заказов», Фаза 2)
    # ------------------------------------------------------------------

    def latest_history_id(self, minutes_back: int = 30) -> int:
        """
        Максимальный id записи истории «сейчас» — стартовое значение курсора.

        Нужен потому, что первый вызов истории БЕЗ курсора отдаёт записи
        с 2021 года (проверено 2026-09-08: 41 708 записей только за неделю).
        Начав с нуля, лента вычитывала бы четыре года чужой истории вместо
        того, чтобы показать курьеру сегодняшний заказ.

        Окно берётся по времени в поясе аккаунта CRM: фильтр startDate
        сравнивается именно с ним, а не с UTC.
        """
        start = (datetime.utcnow()
                 + timedelta(hours=CRM_ACCOUNT_UTC_OFFSET)
                 - timedelta(minutes=minutes_back))
        data = self._get("api/v5/orders/history", {
            "filter[startDate]": start.strftime("%Y-%m-%d %H:%M:%S"),
            "limit": PAGE_LIMIT,
            "page": 1,
        })
        ids = [record.get("id") or 0 for record in data.get("history", [])]
        return max(ids) if ids else 0

    def iter_history_since(self, since_id: int, max_pages: int = 20
                           ) -> Iterator[List[Dict[str, Any]]]:
        """
        Страницы истории изменений заказов, начиная с записи since_id.

        Листаем ТОЛЬКО курсором: на глубине CRM отвечает «Use the shift of the
        `filter[sinceId]` instead of `page` parameter» — параметр page для
        истории неприменим. Следующая страница — это новый запрос с курсором,
        сдвинутым на максимальный полученный id.

        max_pages — потолок на один тик. Лента ходит раз в минуту, и разгребать
        накопившееся лучше несколькими тиками, чем одним долгим прогоном,
        который держит воркер (их всего два на весь сайт).
        """
        cursor = since_id
        for _ in range(max_pages):
            data = self._get("api/v5/orders/history", {
                "filter[sinceId]": cursor,
                "limit": PAGE_LIMIT,
            })
            records = data.get("history", [])
            if not records:
                return

            yield records

            cursor = max(record.get("id") or 0 for record in records)
            if len(records) < PAGE_LIMIT:
                return
            time.sleep(PAGE_PAUSE_SECONDS)

    def get_orders_by_ids(self, order_ids: List[int]) -> List[Dict[str, Any]]:
        """
        Карточки заказов по идентификаторам: в истории лежит только изменённое
        поле, а курьеру нужен весь заказ.
        """
        orders: List[Dict[str, Any]] = []
        for start in range(0, len(order_ids), PAGE_LIMIT):
            chunk = order_ids[start:start + PAGE_LIMIT]
            data = self._get("api/v5/orders", {
                "filter[ids][]": chunk,
                "limit": PAGE_LIMIT,
                "page": 1,
            })
            orders.extend(data.get("orders", []))
            if len(order_ids) > PAGE_LIMIT:
                time.sleep(PAGE_PAUSE_SECONDS)
        return orders


    def _post(self, endpoint: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        POST в RetailCRM: form-urlencoded, а не JSON.

        Это требование API: тело — обычная форма, а сложные структуры внутри
        неё передаются JSON-СТРОКОЙ в отдельном поле (`order`). Отправка
        привычного `json=` возвращает 400 с бесполезным текстом.
        """
        if not self.api_url or not self.api_key:
            raise RetailCRMError("RetailCRM не настроен: задайте RETAILCRM_URL и RETAILCRM_API_KEY")

        url = f"{self.api_url}/{endpoint.lstrip('/')}"
        try:
            response = self.session.post(url, data=data, timeout=self.timeout)
        except requests.exceptions.RequestException as e:
            raise RetailCRMError(f"Сеть/таймаут при запросе {endpoint}: {e}") from e

        try:
            payload = response.json()
        except ValueError:
            payload = {}

        if not response.ok or not payload.get("success", False):
            message = payload.get("errorMsg") or response.text[:300]
            # Код ответа отдаём наверх: 4xx повторять бессмысленно, 5xx стоит
            error = RetailCRMError(f"RetailCRM отклонил {endpoint}: {message}")
            error.status_code = response.status_code
            raise error
        return payload

    def edit_order(self, order_id: int, status: Optional[str] = None,
                   courier_id: Optional[int] = None,
                   site: Optional[str] = None) -> Dict[str, Any]:
        """
        Изменить заказ: статус и/или курьера.

        `courier_id` пишется вместе с «Забрал», и это про ДЕНЬГИ: модуль
        «Оплата курьерам» считает выплаты по `delivery.data.courierId`, и без
        него работа курьера просто не попадёт в оплату (§7-тер плана).

        `by=id` обязателен: без него CRM ищет заказ по externalId и отвечает
        «не найден» на совершенно живой заказ.
        """
        order: Dict[str, Any] = {}
        if status:
            order["status"] = status
        if courier_id is not None:
            order["delivery"] = {"data": {"courierId": int(courier_id)}}
        if not order:
            raise RetailCRMError("Нечего отправлять: не задан ни статус, ни курьер")

        data = {"by": "id", "order": json.dumps(order, ensure_ascii=False)}
        if site:
            data["site"] = site
        return self._post(f"api/v5/orders/{order_id}/edit", data)

    def get_product_images(self, offer_ids: List[int]) -> Dict[int, Optional[str]]:
        """
        Ссылки на фото товаров по идентификаторам торговых предложений.

        Отдаёт запись на КАЖДЫЙ запрошенный оффер, в том числе `None` для тех,
        у кого фото нет или кого CRM не вернула вовсе. Без этого товар без
        фото попадал бы в очередь снова и снова и заставлял ходить наружу
        каждый тик (CLAUDE.md, раздел про квоты внешних API).

        Разведка 2026-09-08: `imageUrl` заполнен у 98% товаров, `offers.images`
        содержит тот же URL. Это оригиналы с сайта (медиана 211 КБ), поэтому
        ссылка только сохраняется — грузит её браузер курьера и только по тапу.
        """
        result: Dict[int, Optional[str]] = {int(offer_id): None for offer_id in offer_ids}
        if not offer_ids:
            return result

        for start in range(0, len(offer_ids), PAGE_LIMIT):
            chunk = offer_ids[start:start + PAGE_LIMIT]
            data = self._get("api/v5/store/products", {
                "filter[offerIds][]": chunk,
                "limit": PAGE_LIMIT,
                "page": 1,
            })
            for product in data.get("products") or []:
                product_image = product.get("imageUrl")
                for offer in product.get("offers") or []:
                    offer_id = offer.get("id")
                    if offer_id is None or int(offer_id) not in result:
                        continue
                    images = offer.get("images") or []
                    result[int(offer_id)] = (images[0] if images else None) or product_image
            if len(offer_ids) > PAGE_LIMIT:
                time.sleep(PAGE_PAUSE_SECONDS)
        return result


def parse_catalog_page(products: List[Dict[str, Any]]) -> tuple:
    """
    Страница каталога → (офферы, связи с группами).

    Ключ — `offer.id`: именно им ссылается позиция заказа. У товара офферов
    может быть несколько, и группы у них общие — товарные, а не оферные.

    `unit` берётся с оффера: единица измерения — свойство предложения, и
    именно она отвечает, штуки в позиции или граммы.
    """
    offers = []
    links = []
    for product in products:
        product_id = product.get("id")
        group_ids = [g.get("id") for g in (product.get("groups") or [])
                     if isinstance(g, dict) and g.get("id") is not None]

        for offer in (product.get("offers") or []):
            offer_id = offer.get("id")
            if offer_id is None:
                continue
            unit = offer.get("unit") or {}
            offers.append({
                "offer_id": int(offer_id),
                "product_id": product_id,
                # Артикул оффера, а при его отсутствии — товара: у части
                # позиций он заполнен только на одном из уровней.
                "article": offer.get("article") or product.get("article"),
                "name": offer.get("name") or product.get("name"),
                "unit_code": unit.get("code"),
                "active": bool(offer.get("active", True)),
            })
            links.extend((int(offer_id), gid) for gid in group_ids)
    return offers, links


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
        # Поля карточки курьера (модуль «Курьеры: доставка заказов»).
        **delivery_card_fields(order),
    }


def _first_phone(node: Dict[str, Any]) -> Optional[str]:
    """Первый телефон клиента. Список бывает пустым — это не ошибка."""
    for phone in (node.get("phones") or []):
        number = (phone or {}).get("number")
        if number:
            return str(number).strip()
    return None


def _flag(value: Any) -> bool:
    """
    Кастомное поле-галочка → bool.

    Из CRM приходит и `True`, и `"true"`, и `"1"`: поле правят в интерфейсе, и
    его тип зависит от того, как заведено. Проверка `value is True` молча
    потеряла бы половину значений — а это флаг «не связываться с получателем»,
    цена ошибки здесь испорченный сюрприз.
    """
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "да", "yes")


def delivery_card_fields(order: Dict[str, Any]) -> Dict[str, Any]:
    """
    Поля, которые курьер видит в карточке заказа.

    Отдельной функцией, а не строчками внутри parse_order: набор проверяется
    тестом на реальной форме ответа CRM, и его читают, когда в карточке чего-то
    не хватает.
    """
    delivery = order.get("delivery") or {}
    address = delivery.get("address") or {}
    time_block = delivery.get("time") or {}
    custom = order.get("customFields") or {}
    customer = order.get("customer") or {}

    return {
        # Адрес одной строкой: отдельные street/building заполнены у единиц,
        # а text — у 99% заказов своей доставки (у Яндекс.Доставки его нет
        # вовсе, но такие заказы курьеру и не показываются).
        "address_text": address.get("text"),
        "delivery_time_from": parse_time_value(time_block.get("from")),
        "delivery_time_to": parse_time_value(time_block.get("to")),
        "recipient_name": (custom.get(RECIPIENT_NAME_FIELD) or None),
        "recipient_phone": (custom.get(RECIPIENT_PHONE_FIELD) or None),
        "recipient_is_customer": 1 if _flag(custom.get(RECIPIENT_IS_CUSTOMER_FIELD)) else 0,
        "do_not_contact_recipient": 1 if _flag(custom.get(DO_NOT_CONTACT_FIELD)) else 0,
        "customer_name": customer.get("firstName") or None,
        "customer_phone": _first_phone(customer),
        "manager_comment": order.get("managerComment") or None,
        "customer_comment": order.get("customerComment") or None,
        "note_text": custom.get(NOTE_TEXT_FIELD) or None,
        # Плановая готовность приходит как «2026-09-08 14:00:00» в стенных
        # часах салона. Не преобразуем: это то же время, что вводит менеджер,
        # и любая конвертация здесь сдвинет его для половины городов.
        "ready_planned_at": (custom.get(READY_PLANNED_FIELD) or None),
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
