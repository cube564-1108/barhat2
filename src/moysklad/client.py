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

from russian_ca import trust_russian_ca

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


def build_entity_href(entity: str, entity_id: str) -> str:
    """meta.href сущности по типу и id (склад, товар и т.д.) — без похода в API."""
    return f"{MOYSKLAD_API_URL.rstrip('/')}/entity/{entity}/{entity_id}"


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
        trust_russian_ca(self.session)

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
            **kwargs: Дополнительные аргументы для requests (кроме headers/timeout — заданы здесь)

        Returns:
            JSON ответ или None в случае ошибки

        Таймаут (30с) выставлен по умолчанию — без него зависший/медленный ответ
        МойСклад блокирует gunicorn-воркер навсегда (реальный инцидент: воркер
        завис на /report/stock/bystore, оба воркера заняты — сайт лёг). Ретрай
        на 429 — цикл максимум на 3 попытки, не рекурсия без предела.
        """
        url = f"{self.api_url.rstrip('/')}/{path.lstrip('/')}"

        headers = kwargs.pop('headers', {})
        headers.update(self._get_auth_headers())
        request_kwargs = dict(kwargs)
        request_kwargs['headers'] = headers
        request_kwargs.setdefault('timeout', 30)
        if params:
            request_kwargs['params'] = params
        if json_data:
            request_kwargs['json'] = json_data

        max_retries_429 = 3
        for attempt in range(max_retries_429 + 1):
            try:
                logger.debug(f"{method} {url}")
                response = self.session.request(method, url, **request_kwargs)
                response.raise_for_status()
                return response.json()

            except requests.exceptions.RequestException as e:
                logger.error(f"Ошибка запроса {method} {path}: {e}")
                if hasattr(e, 'response') and e.response is not None:
                    logger.error(f"Response: {e.response.text}")
                    if e.response.status_code == 429 and attempt < max_retries_429:
                        logger.warning(f"Rate limited, retrying after 1s... (попытка {attempt + 1}/{max_retries_429})")
                        import time
                        time.sleep(1)
                        continue
                return None
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

    def get_groups(self, limit: int = 1000, offset: int = 0) -> Optional[Dict]:
        """
        Получить отделы (group) — используются как owner/group на документах

        Args:
            limit: Количество записей
            offset: Смещение

        Returns:
            Словарь с meta и rows
        """
        return self.get('/entity/group', params={'limit': limit, 'offset': offset})

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

    def get_organizations(self, limit: int = 100, offset: int = 0) -> Optional[Dict]:
        """
        Получить юрлица/ИП (organization) — нужно для создания документов
        (списание, оприходование и т.д.), которые требуют ссылку на организацию.

        Returns:
            Словарь с meta и rows
        """
        return self.get('/entity/organization', params={'limit': limit, 'offset': offset})

    def create_loss(
        self,
        organization_href: str,
        store_href: str,
        positions: List[Dict[str, Any]],
        applicable: bool = True,
        description: Optional[str] = None,
        owner_href: Optional[str] = None,
        group_href: Optional[str] = None,
    ) -> Optional[Dict]:
        """
        Создать документ "Списание" (Loss) — списывает товар со склада.

        Все позиции заявки уходят ОДНИМ документом за один запрос: MoySklad
        нативно поддерживает несколько позиций в одном Loss, и это делает
        отправку атомарной на стороне API — не бывает состояния "половина
        позиций списалась, половина нет".

        Args:
            organization_href: meta.href организации (см. get_organizations())
            store_href: meta.href склада, с которого списываем
                (см. moysklad_store_links / get_stores())
            positions: [{"assortment_href": str, "quantity": float,
                "price": float | None}, ...] — ВСЕ позиции заявки одним списком.
                price — себестоимость единицы в копейках (см. get_cost_prices).
                Без неё МойСклад сохраняет позицию с нулевой стоимостью и
                НИКОГДА её не пересчитывает: замер на проде 17.09.2026 — документ
                без цены остался нулевым и через 3 минуты, документ с ценой её
                сохранил без изменений. Нулевые списания искажают и отчёты
                МойСклада, и показатель «доля списания цветка» в дашборде
            applicable: True — списание проводится сразу (остаток уменьшается).
                False — черновик, остаток не меняется
            description: Комментарий к документу (например, номер заявки в дашборде)
            owner_href: meta.href сотрудника (owner), от лица которого создаётся
                документ (см. get_employees()). Без этого МойСклад подставляет
                сотрудника, привязанного к API-токену — не того, кто реально
                списал товар
            group_href: meta.href отдела (group). Без этого — дефолтный отдел
                аккаунта ("Основной"), не отдел точки/сотрудника

        Returns:
            Созданный документ (с полем "id") или None при ошибке — причина
            ошибки уже залогирована в self.request()
        """
        body: Dict[str, Any] = {
            "organization": {"meta": {"href": organization_href, "type": "organization", "mediaType": "application/json"}},
            "store": {"meta": {"href": store_href, "type": "store", "mediaType": "application/json"}},
            "applicable": applicable,
            "positions": [
                self._loss_position(pos) for pos in positions
            ],
        }
        if description:
            body["description"] = description
        if owner_href:
            body["owner"] = {"meta": {"href": owner_href, "type": "employee", "mediaType": "application/json"}}
        if group_href:
            body["group"] = {"meta": {"href": group_href, "type": "group", "mediaType": "application/json"}}

        return self.post('/entity/loss', json_data=body)

    @staticmethod
    def _loss_position(pos: Dict[str, Any]) -> Dict[str, Any]:
        """
        Позиция документа списания. Цена уходит, только если она известна и
        положительна: ноль МойСклад принял бы как «товар бесплатный», а
        отрицательная себестоимость (так отчёт отвечает по товарам с минусовым
        остатком) — вообще не цена.
        """
        body: Dict[str, Any] = {
            "assortment": {
                "meta": {
                    "href": pos["assortment_href"],
                    "type": "product",
                    "mediaType": "application/json",
                }
            },
            "quantity": pos["quantity"],
        }
        price = pos.get("price")
        if price is not None and price > 0:
            body["price"] = price
        return body

    def get_last_purchase_prices(self, store_href: str, product_hrefs: List[str],
                                 before_moment: Optional[str] = None,
                                 max_pages: int = 3) -> Dict[str, float]:
        """
        Цена ближайшего ОПРИХОДОВАНИЯ товара на этом складе: {href: копейки}.

        Запасной источник для случая, когда себестоимости не существует: товар
        по учёту в минусе, потому что расход обгоняет оприходование. Физически
        товар есть, цена закупки известна (её завёл человек), но партии для
        расчёта FIFO нет — и get_cost_prices честно возвращает пусто.

        Это оценка, а не факт: настоящая себестоимость считалась бы по партиям.
        Для «доли списания» разница несущественна — порядок величины тот же, —
        но вызывающий код обязан различать источники и показывать это человеку.

        before_moment ограничивает поиск приходами ДО момента документа: цена,
        заведённая после списания, к нему отношения не имеет.
        """
        prices: Dict[str, float] = {}
        if not store_href or not product_hrefs:
            return prices

        wanted = {href for href in product_hrefs if href}
        conditions = [f"store={store_href}"]
        if before_moment:
            conditions.append(f"moment<={before_moment}")

        # От свежих к старым: первое совпадение и есть ближайший приход
        for page in range(max_pages):
            if not wanted:
                break
            response = self.get('/entity/enter', params={
                'filter': ';'.join(conditions),
                'expand': 'positions.assortment',
                'order': 'moment,desc',
                'limit': 50,
                'offset': page * 50,
            })
            if response is None:
                break

            rows = response.get('rows', [])
            for document in rows:
                for position in (document.get('positions') or {}).get('rows') or []:
                    assortment = position.get('assortment') or {}
                    href = (assortment.get('meta') or {}).get('href', '').split('?')[0]
                    price = position.get('price')
                    if href in wanted and price is not None and price > 0:
                        prices[href] = price
                        wanted.discard(href)
            if len(rows) < 50:
                break

        return prices

    def update_loss_position_price(self, loss_id: str, position_id: str,
                                   price: float) -> Optional[Dict]:
        """
        Проставить себестоимость в позицию уже созданного списания.

        Правится ОДНА позиция, а не документ целиком: PUT документа с массивом
        positions переписывает состав, и опечатка в нём стоила бы потерянных
        строк в проведённом документе учёта.
        """
        return self.put(
            f'/entity/loss/{loss_id}/positions/{position_id}',
            json_data={'price': price},
        )

    def get_cost_prices(self, store_href: str, product_hrefs: List[str],
                        moment: Optional[str] = None) -> Dict[str, float]:
        """
        Себестоимость товаров на КОНКРЕТНОМ складе: {href товара: цена в копейках}.

        moment ("YYYY-MM-DD HH:MM:SS") — себестоимость на прошедший момент, для
        починки старых документов: партии с тех пор сменились, и сегодняшняя цена
        к документу месячной давности отношения не имеет. Без него — текущая.

        Склад обязателен, а не «для точности»: себестоимость считается по партиям
        этого склада и отличается в разы. Замер 17.09.2026 по «Шар Белый»:
        10,98 ₽ на Свердловском против 20,83 ₽ в среднем по сети.

        В ответ попадают только товары с положительной себестоимостью. Товара
        нет в ответе, если на складе нет его партий (минусовой остаток —
        списывают неоприходованное): цены не существует, и подставлять вместо
        неё среднюю по сети нельзя — это выдуманные данные.

        Фильтр по нескольким товарам МойСклад объединяет по ИЛИ, поэтому вся
        заявка закрывается одним запросом; длинные списки бьются на пачки, чтобы
        не упереться в предел длины URL.
        """
        prices: Dict[str, float] = {}
        if not store_href or not product_hrefs:
            return prices

        chunk_size = 20
        for start in range(0, len(product_hrefs), chunk_size):
            chunk = product_hrefs[start:start + chunk_size]
            conditions = [f"product={href}" for href in chunk]
            conditions.append(f"store={store_href}")
            params = {
                'filter': ';'.join(conditions),
                'stockMode': 'all',
                'limit': 1000,
            }
            if moment:
                params['moment'] = moment
            response = self.get('/report/stock/all', params=params)
            if response is None:
                logger.warning(
                    f"Себестоимость не получена для {len(chunk)} товаров — "
                    f"списание уйдёт с нулевой стоимостью по ним"
                )
                continue

            for row in response.get('rows', []):
                href = (row.get('meta') or {}).get('href', '').split('?')[0]
                price = row.get('price')
                if href and price is not None and price > 0:
                    prices[href] = price

        return prices

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
