"""
МойСклад Data Storage
Модуль для хранения данных МойСклад в SQLite с историчностью
"""

import os
import sqlite3
import json
import logging
import threading
from datetime import datetime
from typing import Optional, List, Dict, Any
from contextlib import contextmanager
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)


def _extract_id_from_meta(obj: Optional[Dict]) -> Optional[str]:
    """Достать id сущности из вложенного meta.href (МойСклад отдаёт ссылки, не id, для многих связей)"""
    if not obj:
        return None
    href = obj.get('meta', {}).get('href', '')
    return href.rsplit('/', 1)[-1] if href else None


def _add_column_if_missing(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    ddl: str,
    index_sql: Optional[str] = None,
) -> None:
    """
    Добавить колонку, если её ещё нет (идемпотентная миграция).

    Паттерн скопирован с cashshifts/storage.py::_add_column_if_missing, где он уже
    отработал по реальному инциденту: на проде 2 воркера gunicorn стартуют
    одновременно, оба видят «колонки нет» и оба выполняют один и тот же ALTER.
    Штатных исхода гонки два, и ни один не должен ронять воркер:

      duplicate column name — сосед успел закоммитить ALTER раньше;
      database is locked    — сосед держит write-лок прямо сейчас.

    В обоих случаях колонку создаёт сосед, цель достигнута. Здесь это особенно
    важно потому, что исключение из _init_db вылетает наружу через __init__ и
    get_storage(), то есть падал бы любой запрос к /api/moysklad/*.

    index_sql (если передан) выполняется всегда, а не только при успешном ALTER:
    колонку мог создать сосед, а индекс к ней — ещё нет.
    """
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}

    if column not in existing:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            conn.commit()
        except sqlite3.OperationalError as e:
            message = str(e).lower()
            if "duplicate column name" not in message and "locked" not in message:
                raise
            logger.info(f"Миграцию {table}.{column} выполняет параллельный воркер: {e}")

    if not index_sql:
        return

    try:
        conn.execute(index_sql)
        conn.commit()
    except sqlite3.OperationalError as e:
        message = str(e).lower()
        # "no such column" — сосед ещё не закоммитил свой ALTER; индекс создаст он же
        if "locked" not in message and "no such column" not in message:
            raise
        logger.info(f"Индекс для {table}.{column} создаёт параллельный воркер: {e}")


class MoySkladStorage:
    """
    Хранилище данных МойСклад в SQLite
    Поддерживает историчность данных
    """

    def __init__(self, db_path: Optional[str] = None):
        """
        Инициализация хранилища

        Args:
            db_path: Путь к БД (если None, из MOYSKLAD_DB_PATH или data/moysklad.db)
        """
        self.db_path = db_path or os.getenv('MOYSKLAD_DB_PATH', 'data/moysklad.db')

        # Создаём директорию если нужно
        os.makedirs(os.path.dirname(self.db_path) if os.path.dirname(self.db_path) else '.', exist_ok=True)

        # Инициализируем БД
        self._init_db()

    @contextmanager
    def _get_connection(self):
        """Контекст менеджер для подключения к SQLite"""
        # busy_timeout — на проде несколько воркеров gunicorn пишут в один файл
        # (см. auth.py::get_db); без него конкурентная запись во время долгой
        # синхронизации заказов даёт "database is locked" читателям.
        #
        # journal_mode=WAL здесь НЕ выставляется: это персистентное свойство
        # самого файла БД, достаточно один раз в _init_db. Раньше PRAGMA шла на
        # каждом соединении — то есть на каждый INSERT одного заказа из 28 тысяч.
        #
        # synchronous=NORMAL при WAL — рекомендованная связка: fsync только на
        # чекпойнте, а не на каждой транзакции. На сетевом диске Amvera именно
        # fsync на каждый заказ и съедал дисковую очередь, из-за чего тормозили
        # запросы к соседним базам (/data/barhat.db — авторизация всего сайта).
        conn = sqlite3.connect(self.db_path, timeout=20)
        conn.row_factory = sqlite3.Row  # Доступ по имени колонки
        conn.execute("PRAGMA busy_timeout=20000")
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"Ошибка БД: {e}")
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        """Создание таблиц если не существуют"""
        with self._get_connection() as conn:
            # WAL — свойство файла БД, ставится один раз при инициализации.
            #
            # Переключение журнала требует эксклюзивной блокировки и, в отличие от
            # обычной записи, НЕ ждёт по busy_timeout: если в этот момент базу
            # держит второй воркер gunicorn (оба стартуют одновременно), PRAGMA
            # падает с "database is locked" — и роняла бы весь _init_db, а значит
            # любой запрос к /api/moysklad/*. Проиграть гонку здесь безобидно:
            # либо база уже в WAL, либо её переключает сосед.
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError as e:
                logger.info(f"journal_mode=WAL выставляет параллельный воркер: {e}")

            cursor = conn.cursor()

            # Таблица товаров
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS products (
                    id TEXT PRIMARY KEY,
                    name TEXT,
                    code TEXT,
                    external_code TEXT,
                    description TEXT,
                    article TEXT,
                    product_code TEXT,
                    uom_id TEXT,
                    uom_name TEXT,
                    folder_id TEXT,
                    folder_name TEXT,
                    image_url TEXT,
                    archived BOOLEAN DEFAULT 0,
                    raw_data TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    snapshot_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # Индексы для товаров
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_products_folder_id ON products(folder_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_products_archived ON products(archived)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_products_snapshot_at ON products(snapshot_at)')

            # Таблица остатков
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS stock (
                    id TEXT PRIMARY KEY,
                    store_id TEXT,
                    store_name TEXT,
                    product_id TEXT,
                    product_name TEXT,
                    quantity REAL DEFAULT 0,
                    reserve REAL DEFAULT 0,
                    in_transit REAL DEFAULT 0,
                    raw_data TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    snapshot_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # Индексы для остатков
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_stock_store_id ON stock(store_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_stock_product_id ON stock(product_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_stock_snapshot_at ON stock(snapshot_at)')

            # Таблица складов
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS stores (
                    id TEXT PRIMARY KEY,
                    name TEXT,
                    description TEXT,
                    address TEXT,
                    archived BOOLEAN DEFAULT 0,
                    raw_data TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # Таблица папок/групп
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS folders (
                    id TEXT PRIMARY KEY,
                    name TEXT,
                    parent_id TEXT,
                    product_count INTEGER DEFAULT 0,
                    archived BOOLEAN DEFAULT 0,
                    raw_data TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # Таблица заказов покупателей
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS sales_orders (
                    id TEXT PRIMARY KEY,
                    name TEXT,
                    description TEXT,
                    created TIMESTAMP,
                    moment TIMESTAMP,
                    delivered TIMESTAMP,
                    rate REAL DEFAULT 1.0,
                    sum REAL DEFAULT 0,
                    vat_sum REAL DEFAULT 0,
                    state_id TEXT,
                    state_name TEXT,
                    agent_id TEXT,
                    agent_name TEXT,
                    store_id TEXT,
                    store_name TEXT,
                    applicable BOOLEAN DEFAULT 1,
                    raw_data TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    snapshot_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # Индексы для заказов
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_sales_orders_moment ON sales_orders(moment)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_sales_orders_agent_id ON sales_orders(agent_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_sales_orders_snapshot_at ON sales_orders(snapshot_at)')

            # Миграция: колонки, добавленные позже создания таблицы
            _add_column_if_missing(
                conn, 'sales_orders', 'created', 'TIMESTAMP',
                # customerorder.created — дата создания заказа, используется как "дата
                # продажи" для ABC-анализа (см. get_abc_analysis); изначально не сохранялась
                index_sql='CREATE INDEX IF NOT EXISTS idx_sales_orders_created ON sales_orders(created)'
            )
            _add_column_if_missing(
                conn, 'sales_orders', 'sales_channel_id', 'TEXT',
                index_sql='CREATE INDEX IF NOT EXISTS idx_sales_orders_channel_id ON sales_orders(sales_channel_id)'
            )

            # Таблица каналов продаж (справочник)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS sales_channels (
                    id TEXT PRIMARY KEY,
                    name TEXT,
                    type TEXT,
                    archived BOOLEAN DEFAULT 0,
                    raw_data TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # Таблица позиций заказов (для ABC-анализа товаров)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS order_positions (
                    id TEXT PRIMARY KEY,
                    order_id TEXT,
                    assortment_id TEXT,
                    assortment_name TEXT,
                    product_folder_id TEXT,
                    quantity REAL DEFAULT 0,
                    price REAL DEFAULT 0,
                    discount REAL DEFAULT 0,
                    sum REAL DEFAULT 0,
                    raw_data TEXT,
                    snapshot_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_order_positions_order_id ON order_positions(order_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_order_positions_folder_id ON order_positions(product_folder_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_order_positions_assortment_id ON order_positions(assortment_id)')

            # Таблица контрагентов
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS counterparties (
                    id TEXT PRIMARY KEY,
                    name TEXT,
                    description TEXT,
                    external_code TEXT,
                    legal_title TEXT,
                    inn TEXT,
                    kpp TEXT,
                    address TEXT,
                    phone TEXT,
                    email TEXT,
                    counterparty_type TEXT,
                    archived BOOLEAN DEFAULT 0,
                    raw_data TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # Таблица расходных накладных
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS demands (
                    id TEXT PRIMARY KEY,
                    name TEXT,
                    description TEXT,
                    moment TIMESTAMP,
                    rate REAL DEFAULT 1.0,
                    sum REAL DEFAULT 0,
                    vat_sum REAL DEFAULT 0,
                    store_id TEXT,
                    store_name TEXT,
                    agent_id TEXT,
                    agent_name TEXT,
                    applicable BOOLEAN DEFAULT 1,
                    raw_data TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    snapshot_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # Лог синхронизаций
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS sync_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TIMESTAMP,
                    finished_at TIMESTAMP,
                    entity_type TEXT,
                    records_count INTEGER,
                    status TEXT,
                    error_message TEXT
                )
            ''')

            # Миграция: progress_at — время последнего обновления счётчика записей.
            # По нему видно, что синхронизация со статусом 'started' на самом деле
            # мертва (воркер перезапустился/деплой), а не идёт — иначе такая запись
            # блокировала бы запуск нового синка навсегда.
            _add_column_if_missing(conn, 'sync_log', 'progress_at', 'TIMESTAMP')

            logger.info(f"БД инициализирована: {self.db_path}")

    def save_product(self, product: Dict, snapshot_at: Optional[datetime] = None) -> bool:
        """
        Сохранить или обновить товар

        Args:
            product: Данные товара из МойСклад API
            snapshot_at: Время снапшота

        Returns:
            True если успешно
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()

                snapshot_ts = snapshot_at or datetime.now()
                product_id = product.get('id')

                # Извлекаем данные
                folder = product.get('productFolder', {})
                uom = product.get('uom', {})

                cursor.execute('''
                    INSERT OR REPLACE INTO products
                    (id, name, code, external_code, description, article, product_code,
                     uom_id, uom_name, folder_id, folder_name, image_url, archived,
                     raw_data, snapshot_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    product_id,
                    product.get('name'),
                    product.get('code'),
                    product.get('externalCode'),
                    product.get('description'),
                    product.get('article'),
                    product.get('productCode'),
                    uom.get('id') if uom else None,
                    uom.get('name') if uom else None,
                    folder.get('id') if folder else None,
                    folder.get('name') if folder else None,
                    self._extract_image_url(product),
                    1 if product.get('archived') else 0,
                    json.dumps(product, ensure_ascii=False),
                    snapshot_ts
                ))

                return True

        except Exception as e:
            logger.error(f"Ошибка сохранения товара {product.get('id')}: {e}")
            return False

    def save_products(self, products: List[Dict]) -> int:
        """Сохранить несколько товаров"""
        count = 0
        for product in products:
            if self.save_product(product):
                count += 1
        logger.info(f"Сохранено {count}/{len(products)} товаров")
        return count

    def save_stock(self, stock: Dict, snapshot_at: Optional[datetime] = None) -> bool:
        """
        Сохранить остаток

        Args:
            stock: Данные остатка из отчёта
            snapshot_at: Время снапшота

        Returns:
            True если успешно
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()

                snapshot_ts = snapshot_at or datetime.now()

                # Генерируем ID для записи остатка
                stock_id = f"{stock.get('storeId', 'all')}_{stock.get('productId', 'unknown')}"

                cursor.execute('''
                    INSERT OR REPLACE INTO stock
                    (id, store_id, store_name, product_id, product_name,
                     quantity, reserve, in_transit, raw_data, updated_at, snapshot_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    stock_id,
                    stock.get('storeId'),
                    stock.get('storeName'),
                    stock.get('productId'),
                    stock.get('productName'),
                    stock.get('quantity', 0),
                    stock.get('reserve', 0),
                    stock.get('inTransit', 0),
                    json.dumps(stock, ensure_ascii=False),
                    snapshot_ts
                ))

                return True

        except Exception as e:
            logger.error(f"Ошибка сохранения остатка: {e}")
            return False

    def save_stocks(self, stocks: List[Dict]) -> int:
        """Сохранить несколько остатков"""
        count = 0
        for stock in stocks:
            if self.save_stock(stock):
                count += 1
        logger.info(f"Сохранено {count}/{len(stocks)} остатков")
        return count

    def save_store(self, store: Dict) -> bool:
        """Сохранить или обновить склад"""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()

                cursor.execute('''
                    INSERT OR REPLACE INTO stores
                    (id, name, description, address, archived, raw_data, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ''', (
                    store.get('id'),
                    store.get('name'),
                    store.get('description'),
                    store.get('address'),
                    1 if store.get('archived') else 0,
                    json.dumps(store, ensure_ascii=False)
                ))

                return True

        except Exception as e:
            logger.error(f"Ошибка сохранения склада {store.get('id')}: {e}")
            return False

    def save_stores(self, stores: List[Dict]) -> int:
        """Сохранить несколько складов"""
        count = 0
        for store in stores:
            if self.save_store(store):
                count += 1
        logger.info(f"Сохранено {count}/{len(stores)} складов")
        return count

    def save_folder(self, folder: Dict) -> bool:
        """Сохранить или обновить папку"""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()

                # МойСклад отдаёт родителя папки под ключом productFolder (не parent),
                # и только как meta-ссылку, без прямого id
                parent_id = _extract_id_from_meta(folder.get('productFolder'))

                cursor.execute('''
                    INSERT OR REPLACE INTO folders
                    (id, name, parent_id, product_count, archived, raw_data, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ''', (
                    folder.get('id'),
                    folder.get('name'),
                    parent_id,
                    folder.get('productCount', 0),
                    1 if folder.get('archived') else 0,
                    json.dumps(folder, ensure_ascii=False)
                ))

                return True

        except Exception as e:
            logger.error(f"Ошибка сохранения папки {folder.get('id')}: {e}")
            return False

    def save_sales_order(self, order: Dict, snapshot_at: Optional[datetime] = None) -> bool:
        """
        Сохранить один заказ покупателя (+ канал продаж и позиции, если раскрыты в ответе API).

        Для массовой загрузки используйте save_sales_orders_batch — здесь на каждый
        заказ приходится отдельная транзакция.
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                self._save_sales_order_row(cursor, order, snapshot_at or datetime.now())
                return True

        except Exception as e:
            logger.error(f"Ошибка сохранения заказа {order.get('id')}: {e}")
            return False

    def save_sales_orders_batch(self, orders: List[Dict], snapshot_at: Optional[datetime] = None) -> int:
        """
        Сохранить пачку заказов одной транзакцией. Возвращает число сохранённых.

        Зачем отдельный метод: при загрузке 28К заказов вызов save_sales_order в
        цикле означал 28К отдельных соединений, транзакций и коммитов — то есть
        28К записей в WAL с последующими чекпойнтами на общий сетевой диск /data.
        Страница синхронизации — 100 заказов, так что батч сокращает число
        транзакций ровно в 100 раз.

        Ошибка на одном заказе не роняет всю страницу: он пропускается с логом,
        остальные сохраняются (SAVEPOINT на заказ).
        """
        if not orders:
            return 0

        saved = 0
        snapshot_ts = snapshot_at or datetime.now()
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                # Явный BEGIN обязателен: без него первый SAVEPOINT сам открыл бы
                # транзакцию, а RELEASE самого внешнего savepoint в SQLite её
                # коммитит — получилось бы снова по коммиту на заказ, ради чего
                # батч и затевался.
                cursor.execute('BEGIN')
                for order in orders:
                    try:
                        cursor.execute('SAVEPOINT order_save')
                        self._save_sales_order_row(cursor, order, snapshot_ts)
                        cursor.execute('RELEASE order_save')
                        saved += 1
                    except Exception as e:
                        cursor.execute('ROLLBACK TO order_save')
                        cursor.execute('RELEASE order_save')
                        logger.error(f"Ошибка сохранения заказа {order.get('id')}: {e}")
        except Exception as e:
            logger.error(f"Ошибка сохранения пачки заказов: {e}")
            return saved

        return saved

    def _save_sales_order_row(self, cursor, order: Dict, snapshot_ts: datetime) -> None:
        """Записать заказ и его позиции в переданный курсор (без управления транзакцией)"""
        state = order.get('state', {})
        agent = order.get('agent', {})
        store = order.get('store', {})
        sales_channel_id = _extract_id_from_meta(order.get('salesChannel'))
        # rate — объект {value?, currency}, не число (мультивалютность)
        rate = order.get('rate') or {}
        rate_value = rate.get('value', 1.0) if isinstance(rate, dict) else rate

        positions = order.get('positions', {})
        rows = positions.get('rows', []) if isinstance(positions, dict) else []

        # В raw_data заказа не кладём развёрнутые позиции: каждая из них и так
        # целиком лежит в order_positions.raw_data. Дублирование удваивало объём
        # записи на диск при синхронизации (десятки заказов в секунду × полный
        # JSON с позициями) — ровно та нагрузка, из-за которой тормозил сайт.
        raw_order = order
        if rows:
            raw_order = {k: v for k, v in order.items() if k != 'positions'}

        cursor.execute('''
            INSERT OR REPLACE INTO sales_orders
            (id, name, description, created, moment, delivered, rate, sum, vat_sum,
             state_id, state_name, agent_id, agent_name, store_id, store_name,
             applicable, sales_channel_id, raw_data, snapshot_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            order.get('id'),
            order.get('name'),
            order.get('description'),
            order.get('created'),
            order.get('moment'),
            order.get('delivered'),
            rate_value,
            order.get('sum', 0),
            order.get('vatSum', 0),
            state.get('id') if state else None,
            state.get('name') if state else None,
            agent.get('id') if agent else None,
            agent.get('name') if agent else None,
            store.get('id') if store else None,
            store.get('name') if store else None,
            1 if order.get('applicable') else 0,
            sales_channel_id,
            json.dumps(raw_order, ensure_ascii=False),
            snapshot_ts
        ))

        for position in rows:
            self._save_order_position(cursor, order.get('id'), position, snapshot_ts)

    def _save_order_position(self, cursor, order_id: str, position: Dict, snapshot_ts: datetime) -> None:
        """Сохранить позицию заказа (вызывается изнутри save_sales_order, использует его соединение)"""
        assortment = position.get('assortment', {})
        folder = assortment.get('productFolder', {}) if assortment else {}
        quantity = position.get('quantity', 0) or 0
        price = position.get('price', 0) or 0
        discount = position.get('discount', 0) or 0
        position_sum = price * quantity * (1 - discount / 100)

        cursor.execute('''
            INSERT OR REPLACE INTO order_positions
            (id, order_id, assortment_id, assortment_name, product_folder_id,
             quantity, price, discount, sum, raw_data, snapshot_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            position.get('id'),
            order_id,
            assortment.get('id') if assortment else None,
            assortment.get('name') if assortment else None,
            _extract_id_from_meta(folder),
            quantity,
            price,
            discount,
            position_sum,
            json.dumps(position, ensure_ascii=False),
            snapshot_ts
        ))

    def save_sales_channel(self, channel: Dict) -> bool:
        """Сохранить или обновить канал продаж"""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()

                cursor.execute('''
                    INSERT OR REPLACE INTO sales_channels
                    (id, name, type, archived, raw_data, updated_at)
                    VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ''', (
                    channel.get('id'),
                    channel.get('name'),
                    channel.get('type'),
                    1 if channel.get('archived') else 0,
                    json.dumps(channel, ensure_ascii=False)
                ))

                return True

        except Exception as e:
            logger.error(f"Ошибка сохранения канала продаж {channel.get('id')}: {e}")
            return False

    def save_sales_channels(self, channels: List[Dict]) -> int:
        """Сохранить несколько каналов продаж"""
        count = 0
        for channel in channels:
            if self.save_sales_channel(channel):
                count += 1
        logger.info(f"Сохранено {count}/{len(channels)} каналов продаж")
        return count

    def get_sales_channels(self) -> List[Dict]:
        """Получить справочник каналов продаж"""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT * FROM sales_channels WHERE archived = 0 ORDER BY name')
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Ошибка получения каналов продаж: {e}")
            return []

    def _get_excluded_folder_ids(self, cursor, root_folder_name: str = 'Товары МС') -> List[str]:
        """
        Найти id папки-раздела по имени и всех её прямых потомков.

        Резолвим по имени, а не по захардкоженному id — надёжнее на случай
        пересоздания раздела в МойСклад.
        """
        cursor.execute('SELECT id FROM folders WHERE name = ? AND parent_id IS NULL', (root_folder_name,))
        root = cursor.fetchone()
        if not root:
            logger.warning(f"Раздел '{root_folder_name}' не найден в таблице folders")
            return []

        root_id = root['id']
        cursor.execute('SELECT id FROM folders WHERE parent_id = ?', (root_id,))
        child_ids = [row['id'] for row in cursor.fetchall()]

        return [root_id] + child_ids

    def get_abc_analysis(
        self,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        channel_id: Optional[str] = None
    ) -> List[Dict]:
        """
        ABC-анализ товаров по выручке.

        Метрика — выручка (сумма позиций заказов в статусе "Выполнен"),
        дата — дата создания заказа (customerorder.created), товары
        из раздела "Товары МС" исключены.

        Args:
            date_from: Начало периода (ISO дата/datetime)
            date_to: Конец периода (ISO дата/datetime)
            channel_id: Фильтр по каналу продаж (id из sales_channels)

        Returns:
            Список товаров с полями: assortment_id, assortment_name, revenue,
            share_pct, cumulative_pct, abc_class — отсортирован по убыванию revenue.
            Суммы переведены из копеек (родной формат МойСклад) в рубли.
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()

                excluded_folder_ids = self._get_excluded_folder_ids(cursor)

                query = '''
                    SELECT
                        op.assortment_id,
                        op.assortment_name,
                        SUM(op.sum) / 100.0 AS revenue
                    FROM order_positions op
                    JOIN sales_orders so ON so.id = op.order_id
                    WHERE so.state_name = 'Выполнен'
                '''
                params: List[Any] = []

                if date_from:
                    query += ' AND so.created >= ?'
                    params.append(date_from)
                if date_to:
                    # created хранится как полный ISO-datetime ("2026-07-31 03:26:04.037"),
                    # а date_to обычно приходит голой датой ("2026-07-31") из <input type="date">.
                    # Лексикографически "2026-07-31" < "2026-07-31 03:26:04.037", поэтому без
                    # добавления времени последний день периода целиком выпадал из выборки.
                    if len(date_to) <= 10:
                        date_to = f'{date_to} 23:59:59.999999'
                    query += ' AND so.created <= ?'
                    params.append(date_to)
                if channel_id:
                    query += ' AND so.sales_channel_id = ?'
                    params.append(channel_id)
                if excluded_folder_ids:
                    placeholders = ','.join('?' for _ in excluded_folder_ids)
                    query += f' AND (op.product_folder_id IS NULL OR op.product_folder_id NOT IN ({placeholders}))'
                    params.extend(excluded_folder_ids)

                query += '''
                    GROUP BY op.assortment_id, op.assortment_name
                    HAVING revenue > 0
                    ORDER BY revenue DESC
                '''

                cursor.execute(query, params)
                rows = [dict(row) for row in cursor.fetchall()]

                total_revenue = sum(r['revenue'] for r in rows)
                cumulative = 0.0
                for r in rows:
                    share_pct = (r['revenue'] / total_revenue * 100) if total_revenue else 0
                    cumulative += share_pct
                    r['share_pct'] = round(share_pct, 2)
                    r['cumulative_pct'] = round(cumulative, 2)
                    if cumulative <= 80:
                        r['abc_class'] = 'A'
                    elif cumulative <= 95:
                        r['abc_class'] = 'B'
                    else:
                        r['abc_class'] = 'C'

                return rows

        except Exception as e:
            logger.error(f"Ошибка расчёта ABC-анализа: {e}")
            return []

    def save_counterparty(self, counterparty: Dict) -> bool:
        """Сохранить или обновить контрагента"""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()

                cursor.execute('''
                    INSERT OR REPLACE INTO counterparties
                    (id, name, description, external_code, legal_title, inn, kpp,
                     address, phone, email, counterparty_type, archived, raw_data, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ''', (
                    counterparty.get('id'),
                    counterparty.get('name'),
                    counterparty.get('description'),
                    counterparty.get('externalCode'),
                    counterparty.get('legalTitle'),
                    counterparty.get('inn'),
                    counterparty.get('kpp'),
                    counterparty.get('actualAddress'),
                    counterparty.get('phone'),
                    counterparty.get('email'),
                    counterparty.get('companyType', '').lower(),
                    1 if counterparty.get('archived') else 0,
                    json.dumps(counterparty, ensure_ascii=False)
                ))

                return True

        except Exception as e:
            logger.error(f"Ошибка сохранения контрагента {counterparty.get('id')}: {e}")
            return False

    def _extract_image_url(self, product: Dict) -> Optional[str]:
        """Извлечь URL главного изображения товара"""
        images = product.get('images', {})
        if isinstance(images, dict) and images.get('meta'):
            rows = images.get('rows', [])
            if rows and len(rows) > 0:
                meta = rows[0].get('meta', {})
                return meta.get('downloadHref')
        return None

    def get_products(
        self,
        folder_id: Optional[str] = None,
        archived: Optional[bool] = None,
        limit: int = 1000
    ) -> List[Dict]:
        """
        Получить товары

        Args:
            folder_id: Фильтр по папке
            archived: Только архивные (True) или активные (False)
            limit: Максимум записей

        Returns:
            Список товаров
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()

                query = 'SELECT * FROM products WHERE 1=1'
                params = []

                if folder_id:
                    query += ' AND folder_id = ?'
                    params.append(folder_id)

                if archived is not None:
                    query += ' AND archived = ?'
                    params.append(1 if archived else 0)

                query += ' ORDER BY name LIMIT ?'
                params.append(limit)

                cursor.execute(query, params)
                return [dict(row) for row in cursor.fetchall()]

        except Exception as e:
            logger.error(f"Ошибка получения товаров: {e}")
            return []

    def get_stock(
        self,
        store_id: Optional[str] = None,
        limit: int = 1000
    ) -> List[Dict]:
        """Получить остатки"""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()

                if store_id:
                    cursor.execute('''
                        SELECT * FROM stock
                        WHERE store_id = ?
                        ORDER BY quantity DESC
                        LIMIT ?
                    ''', (store_id, limit))
                else:
                    cursor.execute('''
                        SELECT * FROM stock
                        ORDER BY quantity DESC
                        LIMIT ?
                    ''', (limit,))

                return [dict(row) for row in cursor.fetchall()]

        except Exception as e:
            logger.error(f"Ошибка получения остатков: {e}")
            return []

    def get_stores(self) -> List[Dict]:
        """Получить все склады"""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT * FROM stores ORDER BY name')
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Ошибка получения складов: {e}")
            return []

    def get_sales_orders(
        self,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        limit: int = 1000
    ) -> List[Dict]:
        """
        Получить заказы покупателей

        Args:
            date_from: Начало периода
            date_to: Конец периода
            limit: Максимум записей

        Returns:
            Список заказов
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()

                query = 'SELECT * FROM sales_orders WHERE 1=1'
                params = []

                if date_from:
                    query += ' AND moment >= ?'
                    params.append(date_from)

                if date_to:
                    query += ' AND moment <= ?'
                    params.append(date_to)

                query += ' ORDER BY moment DESC LIMIT ?'
                params.append(limit)

                cursor.execute(query, params)
                return [dict(row) for row in cursor.fetchall()]

        except Exception as e:
            logger.error(f"Ошибка получения заказов: {e}")
            return []

    def start_sync_log(self, entity_type: str = 'unknown') -> int:
        """Начать запись лога синхронизации"""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO sync_log (started_at, entity_type, status)
                    VALUES (CURRENT_TIMESTAMP, ?, 'started')
                ''', (entity_type,))
                return cursor.lastrowid
        except Exception as e:
            logger.error(f"Ошибка создания log записи: {e}")
            return 0

    def finish_sync_log(
        self,
        log_id: int,
        records_count: int,
        status: str = 'completed',
        error_message: Optional[str] = None
    ) -> None:
        """Завершить запись лога синхронизации"""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    UPDATE sync_log
                    SET finished_at = CURRENT_TIMESTAMP,
                        records_count = ?,
                        status = ?,
                        error_message = ?
                    WHERE id = ?
                ''', (records_count, status, error_message, log_id))
        except Exception as e:
            logger.error(f"Ошибка обновления log записи: {e}")

    def update_sync_log_progress(self, log_id: int, records_count: int) -> None:
        """
        Обновить счётчик обработанных записей у идущей синхронизации.

        Нужен, чтобы /sync-status показывал прогресс, не вызывая get_stats():
        там шесть COUNT(*) подряд, в том числе по order_positions (46К+ строк),
        а фронтенд опрашивал статус каждые пару секунд все сорок минут синка.
        """
        try:
            with self._get_connection() as conn:
                conn.execute(
                    'UPDATE sync_log SET records_count = ?, progress_at = CURRENT_TIMESTAMP WHERE id = ?',
                    (records_count, log_id)
                )
        except Exception as e:
            logger.error(f"Ошибка обновления прогресса синхронизации: {e}")

    def get_latest_sync_log(self, entity_type: Optional[str] = None) -> Optional[Dict]:
        """
        Получить последнюю запись лога синхронизации (опционально — по типу сущности).

        Читается из общей таблицы sync_log (файл на диске), а не из памяти процесса —
        в отличие от in-memory статуса, одинаково видна из любого воркера gunicorn
        и переживает перезапуск воркера (запись 'started' без finished_at сигналит,
        что синхронизация была прервана, не дойдя до завершения).
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                if entity_type:
                    cursor.execute(
                        'SELECT * FROM sync_log WHERE entity_type = ? ORDER BY id DESC LIMIT 1',
                        (entity_type,)
                    )
                else:
                    cursor.execute('SELECT * FROM sync_log ORDER BY id DESC LIMIT 1')
                row = cursor.fetchone()
                return dict(row) if row else None
        except Exception as e:
            logger.error(f"Ошибка получения лога синхронизации: {e}")
            return None

    def get_stats(self) -> Dict:
        """Получить статистику БД"""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()

                stats = {}

                # Количество товаров
                cursor.execute('SELECT COUNT(*) as count FROM products WHERE archived = 0')
                stats['products_count'] = cursor.fetchone()['count']

                # Количество остатков
                cursor.execute('SELECT COUNT(*) as count FROM stock')
                stats['stock_count'] = cursor.fetchone()['count']

                # Количество заказов
                cursor.execute('SELECT COUNT(*) as count FROM sales_orders')
                stats['sales_orders_count'] = cursor.fetchone()['count']

                # Количество контрагентов
                cursor.execute('SELECT COUNT(*) as count FROM counterparties')
                stats['counterparties_count'] = cursor.fetchone()['count']

                # Количество позиций заказов
                cursor.execute('SELECT COUNT(*) as count FROM order_positions')
                stats['order_positions_count'] = cursor.fetchone()['count']

                # Количество каналов продаж
                cursor.execute('SELECT COUNT(*) as count FROM sales_channels')
                stats['sales_channels_count'] = cursor.fetchone()['count']

                # Последняя синхронизация
                cursor.execute('''
                    SELECT finished_at FROM sync_log
                    WHERE status = 'completed'
                    ORDER BY finished_at DESC LIMIT 1
                ''')
                row = cursor.fetchone()
                stats['last_sync'] = row['finished_at'] if row else None

                return stats

        except Exception as e:
            logger.error(f"Ошибка получения статистики: {e}")
            return {}


_storage_cache: Dict[str, MoySkladStorage] = {}
_storage_cache_lock = threading.Lock()


def get_storage(db_path: Optional[str] = None) -> MoySkladStorage:
    """
    Factory function для получения хранилища (один инстанс на путь к БД).

    Кэш здесь не микрооптимизация: `MoySkladStorage.__init__` вызывает
    `_init_db()`, а это ~10 CREATE TABLE IF NOT EXISTS, 13 CREATE INDEX
    IF NOT EXISTS и несколько PRAGMA table_info-миграций. Без кэша весь этот
    набор DDL выполнялся на КАЖДЫЙ вызов get_storage(): на каждый HTTP-запрос
    к любому /api/moysklad/*, в том числе на опрос /sync-status раз в пару
    секунд во время сорокаминутной синхронизации. Именно это (вместе с fsync
    на каждый заказ) и подтормаживало весь сайт — /data общий для barhat.db,
    pyrus.db и moysklad.db.

    Инстанс не хранит состояния кроме db_path, соединение к SQLite создаётся
    на каждый вызов — делиться им между потоками gthread безопасно.
    """
    resolved = db_path or os.getenv('MOYSKLAD_DB_PATH', 'data/moysklad.db')

    # In-memory БД у каждого вызова своя — кэшировать нельзя
    if resolved == ':memory:':
        return MoySkladStorage(resolved)

    cached = _storage_cache.get(resolved)
    if cached is not None:
        return cached

    with _storage_cache_lock:
        cached = _storage_cache.get(resolved)
        if cached is None:
            cached = MoySkladStorage(resolved)
            _storage_cache[resolved] = cached
        return cached


if __name__ == "__main__":
    # Тест
    print("Тест MoySklad Storage...")

    storage = get_storage(':memory:')

    print("Статистика:")
    stats = storage.get_stats()
    for key, value in stats.items():
        print(f"  {key}: {value}")
