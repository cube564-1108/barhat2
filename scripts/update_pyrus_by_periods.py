"""
Выгрузка задач из Pyrus по периодам
Решает проблему с пагинацией — выгружает по месяцам/кварталам
"""

import os
import sys
import time
import logging
from datetime import datetime, timedelta
from pathlib import Path

# Добавляем src в Python path
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from pyrus.client import get_client
from pyrus.storage import get_storage

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)


def generate_periods(start_date: str, end_date: str, months_per_period: int = 3):
    """
    Сгенерировать периоды для выгрузки

    Args:
        start_date: Начальная дата (YYYY-MM-DD)
        end_date: Конечная дата (YYYY-MM-DD)
        months_per_period: Месяцев на период

    Yields:
        (start, end, label) кортежи в формате ISO
    """
    current = datetime.strptime(start_date, '%Y-%m-%d')
    end = datetime.strptime(end_date, '%Y-%m-%d')

    while current < end:
        period_end = min(
            current + timedelta(days=months_per_period * 31),
            end
        )

        start_iso = current.strftime('%Y-%m-%dT00:00:00Z')
        end_iso = period_end.strftime('%Y-%m-%dT23:59:59Z')
        label = f"{current.strftime('%Y-%m')}-{period_end.strftime('%Y-%m')}"

        yield (start_iso, end_iso, label)

        current = period_end + timedelta(days=1)


def main():
    """Точка входа"""
    form_id = 1327961

    logger.info("=" * 60)
    logger.info("Выгрузка задач Pyrus по периодам")
    logger.info("=" * 60)

    # Инициализация
    client = get_client()
    storage = get_storage()

    if not client.authenticate():
        logger.error("Ошибка авторизации")
        return

    # Периоды: с 2023 по август 2026, по 3 месяца
    periods = list(generate_periods('2023-01-01', '2026-08-07', months_per_period=3))

    logger.info(f"Сгенерировано {len(periods)} периодов")

    total_tasks = 0
    for i, (start, end, label) in enumerate(periods, 1):
        logger.info(f"\n[{i}/{len(periods)}] Период {label}")

        # Проверяем сколько задач
        response = client.session.get(
            f'{client.api_url}forms/{form_id}/register',
            headers={'Authorization': f'Bearer {client.access_token}'},
            params={
                'include_archived': 'y',
                'item_count': 20000,
                'created_after': start,
                'created_before': end
            }
        )

        if response.status_code != 200:
            logger.error(f"Ошибка: {response.status_code}")
            continue

        data = response.json()
        tasks = data.get('tasks', [])

        if not tasks:
            logger.info(f"  Нет задач за этот период")
            continue

        # Сохраняем
        count = storage.save_tasks(form_id, tasks)
        total_tasks += count

        # Посчитаем даты для проверки
        dates = []
        for t in tasks[:20]:
            for field in t.get('fields', []):
                if field.get('id') == 1:
                    date_val = field.get('value')
                    if date_val:
                        dates.append(date_val)
                    break

        date_range = f"{min(dates)}-{max(dates)}" if dates else "нет дат"

        logger.info(f"  Загружено: {count} задач")
        logger.info(f"  Даты: {date_range}")

    logger.info("\n" + "=" * 60)
    logger.info(f"✅ Всего загружено: {total_tasks} задач")
    logger.info("=" * 60)

    # Статистика БД
    stats = storage.get_stats()
    logger.info(f"\nСтатистика БД:")
    logger.info(f"  Уникальных задач: {stats['tasks_count']}")
    logger.info(f"  Исторических записей: {stats['history_count']}")


if __name__ == '__main__':
    main()
