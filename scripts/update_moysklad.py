"""
Скрипт для обновления данных из МойСклад

Использование:
    python scripts/update_moysklad.py --entities products stock stores
    python scripts/update_moysklad.py --all
"""

import os
import sys
import logging
import argparse
from datetime import datetime

# Добавляем src в path
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
src_path = os.path.join(project_root, 'src')
if src_path not in sys.path:
    sys.path.insert(0, src_path)

from moysklad import get_client, get_fetcher, get_storage

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Сущности для загрузки
ENTITIES = {
    'products': 'Товары',
    'assortment': 'Ассортимент',
    'stock': 'Остатки',
    'stores': 'Склады',
    'folders': 'Папки',
    'sales_orders': 'Заказы покупателей',
    'demands': 'Расходные накладные',
    'counterparties': 'Контрагенты',
    'sales_channels': 'Каналы продаж',
}

# Дополнительные параметры запроса для отдельных сущностей
ENTITY_FETCH_KWARGS = {
    'sales_orders': {'expand': 'positions,positions.assortment,state'},
}


def update_entity(entity: str, fetcher, storage, max_items: int = 10000):
    """
    Обновить данные сущности

    Args:
        entity: Тип сущности
        fetcher: MoySkladFetcher
        storage: MoySkladStorage
        max_items: Максимум записей
    """
    logger.info(f"Загрузка сущности: {ENTITIES.get(entity, entity)}")

    log_id = storage.start_sync_log(entity_type=entity)

    try:
        # Получаем данные
        extra_kwargs = ENTITY_FETCH_KWARGS.get(entity, {})
        data = fetcher.get_full_entity_data(entity, max_items=max_items, **extra_kwargs)

        if data is None:
            logger.error(f"Не удалось загрузить {entity}")
            storage.finish_sync_log(
                log_id,
                records_count=0,
                status='failed',
                error_message='Failed to fetch data'
            )
            return False

        # Сохраняем данные
        save_method = {
            'products': storage.save_products,
            'assortment': storage.save_products,  # ассортимент сохраняется как товары
            'stock': storage.save_stocks,
            'stores': storage.save_stores,
            'folders': lambda folders: sum(1 for f in folders if storage.save_folder(f)),  # список папок
            'sales_orders': lambda orders: sum(1 for o in orders if storage.save_sales_order(o)),
            'demands': lambda demands: sum(1 for d in demands if storage.save_sales_order(d)),  # временно через sales_orders
            'counterparties': lambda cps: sum(1 for c in cps if storage.save_counterparty(c)),
            'sales_channels': storage.save_sales_channels,
        }.get(entity)

        if not save_method:
            logger.error(f"Нет метода сохранения для {entity}")
            return False

        count = save_method(data)

        storage.finish_sync_log(
            log_id,
            records_count=count,
            status='completed'
        )

        logger.info(f"[OK] {entity}: загружено {count} записей")
        return True

    except Exception as e:
        logger.error(f"Ошибка при обработке {entity}: {e}")
        storage.finish_sync_log(
            log_id,
            records_count=0,
            status='failed',
            error_message=str(e)
        )
        return False


def main():
    parser = argparse.ArgumentParser(description='Обновление данных из МойСклад')
    parser.add_argument(
        '--entities',
        nargs='+',
        choices=list(ENTITIES.keys()),
        help='Сущности для загрузки'
    )
    parser.add_argument(
        '--all',
        action='store_true',
        help='Загрузить все сущности'
    )
    parser.add_argument(
        '--max-items',
        type=int,
        default=10000,
        help='Максимум записей на сущность (default: 10000)'
    )
    parser.add_argument(
        '--stats',
        action='store_true',
        help='Показать только статистику БД'
    )

    args = parser.parse_args()

    # Инициализация
    logger.info("Инициализация МойСklad...")

    client = get_client()
    fetcher = get_fetcher(client)
    storage = get_storage()

    # Только статистика
    if args.stats:
        print("\n[Stats] Database statistics:")
        stats = storage.get_stats()
        for key, value in stats.items():
            print(f"  {key}: {value}")
        return

    # Определяем сущности для загрузки
    if args.all:
        entities = list(ENTITIES.keys())
    elif args.entities:
        entities = args.entities
    else:
        # По умолчанию только товары и остатки
        entities = ['products', 'stock', 'stores']

    logger.info(f"Загрузка сущностей: {', '.join(ENTITIES.get(e, e) for e in entities)}")
    print(f"\n[Start] {datetime.now().strftime('%H:%M:%S')}")

    success_count = 0
    failed_entities = []

    for entity in entities:
        if update_entity(entity, fetcher, storage, args.max_items):
            success_count += 1
        else:
            failed_entities.append(entity)

    print(f"\n[Done] {datetime.now().strftime('%H:%M:%S')}")
    print(f"Успешно: {success_count}/{len(entities)}")

    if failed_entities:
        print(f"[Errors]: {', '.join(failed_entities)}")

    # Показать статистику
    print("\n[Stats] Database statistics:")
    stats = storage.get_stats()
    for key, value in stats.items():
        print(f"  {key}: {value}")


if __name__ == '__main__':
    main()
