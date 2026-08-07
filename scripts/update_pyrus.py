"""
Pyrus Update Script
Скрипт ручного обновления данных из Pyrus в БД

Использование:
    python scripts/update_pyrus.py                    # Все формы
    python scripts/update_pyrus.py --forms 123 456    # Конкретные формы
    python scripts/update_pyrus.py --full             # С архивными
    python scripts/update_pyrus.py --stats           # Только статистика
"""

import os
import sys
import time
import logging
import argparse
from datetime import datetime
from pathlib import Path

# Добавляем src в Python path
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from pyrus.client import get_client
from pyrus.fetcher import get_fetcher
from pyrus.storage import get_storage

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)


class PyrusUpdater:
    """Класс для обновления данных Pyrus"""

    def __init__(self):
        self.fetcher = None
        self.storage = None
        self.log_id = None

    def init(self):
        """Инициализация клиента и хранилища"""
        logger.info("Инициализация...")

        self.fetcher = get_fetcher()
        self.storage = get_storage()

        # Авторизация
        if not self.fetcher.authenticate():
            logger.error("❌ Ошибка авторизации. Проверьте credentials в .env")
            return False

        logger.info("✅ Авторизация успешна")
        return True

    def update_all(
        self,
        form_ids: list = None,
        include_archived: bool = True,
        max_items_per_form: int = 20000
    ) -> bool:
        """
        Полное обновление данных

        Args:
            form_ids: Список ID форм (если None, все формы)
            include_archived: Включать архивированные задачи
            max_items_per_form: Максимум задач на форму

        Returns:
            True если успешно
        """
        start_time = time.time()
        self.log_id = self.storage.start_sync_log()

        logger.info("=" * 50)
        logger.info("Начало обновления данных Pyrus")
        logger.info("=" * 50)

        try:
            # 1. Получаем и сохраняем формы
            logger.info("\n📋 Загрузка форм...")
            forms = self.fetcher.get_forms(use_cache=False)

            if not forms:
                logger.error("❌ Не удалось загрузить формы")
                self._finish_sync_log(status='failed', error_message='Failed to load forms')
                return False

            forms_count = self.storage.save_forms(forms)
            logger.info(f"✅ Сохранено {forms_count} форм")

            # 2. Фильтруем формы
            if form_ids:
                forms = [f for f in forms if f.get('id') in form_ids]
                logger.info(f"Фильтр по ID: {len(forms)} форм")

            # 3. Загружаем задачи по каждой форме
            total_tasks = 0
            for i, form in enumerate(forms, 1):
                form_id = form.get('id')
                form_title = form.get('title', 'Unknown')

                logger.info(f"\n[{i}/{len(forms)}] Форма {form_id}: {form_title}")

                tasks = self.fetcher.get_form_register(
                    form_id=form_id,
                    include_archived=include_archived,
                    max_items=max_items_per_form
                )

                if tasks is None:
                    logger.warning(f"⚠️  Пропуск формы {form_id} (ошибка загрузки)")
                    continue

                tasks_count = self.storage.save_tasks(form_id, tasks)
                total_tasks += tasks_count

                logger.info(f"   ✅ Сохранено {tasks_count} задач")

            # 4. Завершаем
            elapsed = time.time() - start_time
            logger.info("\n" + "=" * 50)
            logger.info(f"✅ Обновление завершено за {elapsed:.1f} сек")
            logger.info(f"   Форм: {forms_count}")
            logger.info(f"   Задач: {total_tasks}")
            logger.info("=" * 50)

            self._finish_sync_log(
                forms_count=forms_count,
                tasks_count=total_tasks,
                status='completed'
            )
            return True

        except Exception as e:
            logger.error(f"\n❌ Ошибка: {e}")
            self._finish_sync_log(status='failed', error_message=str(e))
            return False

    def _finish_sync_log(
        self,
        forms_count: int = 0,
        tasks_count: int = 0,
        status: str = 'failed',
        error_message: str = None
    ):
        """Завершить запись лога синхронизации"""
        if self.log_id:
            self.storage.finish_sync_log(
                log_id=self.log_id,
                forms_count=forms_count,
                tasks_count=tasks_count,
                status=status,
                error_message=error_message
            )

    def show_stats(self):
        """Показать статистику БД"""
        logger.info("\n📊 Статистика БД:")

        stats = self.storage.get_stats()

        for key, value in stats.items():
            if value is not None:
                logger.info(f"   {key}: {value}")


def main():
    """Точка входа"""
    parser = argparse.ArgumentParser(
        description='Обновление данных из Pyrus',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры:
  %(prog)s                              # Все формы
  %(prog)s --forms 123 456              # Конкретные формы
  %(prog)s --full --limit 5000          # С архивными, лимит 5000
  %(prog)s --stats                      # Только статистика
        """
    )

    parser.add_argument(
        '--forms',
        nargs='+',
        type=int,
        metavar='ID',
        help='ID форм для обновления (по умолчанию все)'
    )
    parser.add_argument(
        '--full',
        action='store_true',
        help='Включать архивированные задачи'
    )
    parser.add_argument(
        '--limit',
        type=int,
        default=20000,
        metavar='N',
        help='Максимум задач на форму (default: 20000)'
    )
    parser.add_argument(
        '--stats',
        action='store_true',
        help='Показать только статистику БД'
    )

    args = parser.parse_args()

    # Инициализация
    updater = PyrusUpdater()

    if not updater.init():
        sys.exit(1)

    # Только статистика
    if args.stats:
        updater.show_stats()
        sys.exit(0)

    # Полное обновление
    success = updater.update_all(
        form_ids=args.forms,
        include_archived=args.full,
        max_items_per_form=args.limit
    )

    # Показываем статистику после обновления
    if success:
        updater.show_stats()

    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
