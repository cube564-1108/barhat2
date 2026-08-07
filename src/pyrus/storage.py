"""
Pyrus Data Storage
Модуль для хранения данных Pyrus в SQLite с историчностью
"""

import os
import sqlite3
import json
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any
from contextlib import contextmanager
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)


class PyrusStorage:
    """
    Хранилище данных Pyrus в SQLite
    Поддерживает историчность данных
    """

    def __init__(self, db_path: Optional[str] = None):
        """
        Инициализация хранилища

        Args:
            db_path: Путь к БД (если None, из PYRUS_DB_PATH или data/pyrus.db)
        """
        self.db_path = db_path or os.getenv('PYRUS_DB_PATH', 'data/pyrus.db')

        # Создаём директорию если нужно
        os.makedirs(os.path.dirname(self.db_path) if os.path.dirname(self.db_path) else '.', exist_ok=True)

        # Инициализируем БД
        self._init_db()

    @contextmanager
    def _get_connection(self):
        """Контекст менеджер для подключения к SQLite"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row  # Доступ по имени колонки
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
            cursor = conn.cursor()

            # Таблица форм (метаданные)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS forms (
                    id INTEGER PRIMARY KEY,
                    title TEXT,
                    name TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # Таблица задач (основные данные)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY,
                    form_id INTEGER NOT NULL,
                    task_id INTEGER NOT NULL,
                    title TEXT,
                    description TEXT,
                    author_id INTEGER,
                    author_name TEXT,
                    created_at TIMESTAMP,
                    finished_at TIMESTAMP,
                    last_modified TIMESTAMP,
                    current_step TEXT,
                    status TEXT,
                    raw_data TEXT,  -- Полный JSON задачи
                    snapshot_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(form_id, task_id, snapshot_at)
                )
            ''')

            # Индексы для быстрого поиска
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_tasks_form_id
                ON tasks(form_id)
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_tasks_snapshot_at
                ON tasks(snapshot_at)
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_tasks_last_modified
                ON tasks(last_modified)
            ''')

            # Таблица последних snapshot'ов (для быстрого доступа к актуальным данным)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS latest_tasks (
                    form_id INTEGER NOT NULL,
                    task_id INTEGER NOT NULL,
                    title TEXT,
                    status TEXT,
                    last_modified TIMESTAMP,
                    raw_data TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (form_id, task_id)
                )
            ''')

            # Лог загрузок
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS sync_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TIMESTAMP,
                    finished_at TIMESTAMP,
                    forms_count INTEGER,
                    tasks_count INTEGER,
                    status TEXT,
                    error_message TEXT
                )
            ''')

            logger.info(f"БД инициализирована: {self.db_path}")

    def save_form(self, form: Dict) -> bool:
        """
        Сохранить или обновить форму

        Args:
            form: Данные формы из Pyrus API

        Returns:
            True если успешно
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()

                # Пробуем title или name
                form_title = form.get('title') or form.get('name') or f'Form {form.get("id")}'

                cursor.execute('''
                    INSERT OR REPLACE INTO forms (id, title, name, updated_at)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ''', (form.get('id'), form_title, form.get('name')))

                return True

        except Exception as e:
            logger.error(f"Ошибка сохранения формы {form.get('id')}: {e}")
            return False

    def save_forms(self, forms: List[Dict]) -> int:
        """
        Сохранить несколько форм

        Args:
            forms: Список форм

        Returns:
            Количество сохранённых форм
        """
        count = 0
        for form in forms:
            if self.save_form(form):
                count += 1
        logger.info(f"Сохранено {count}/{len(forms)} форм")
        return count

    def save_task(
        self,
        form_id: int,
        task: Dict,
        snapshot_at: Optional[datetime] = None
    ) -> bool:
        """
        Сохранить задачу с историчностью

        Args:
            form_id: ID формы
            task: Данные задачи из Pyrus API
            snapshot_at: Время снапшота (если None, текущее время)

        Returns:
            True если успешно
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()

                snapshot_ts = snapshot_at or datetime.now()
                task_id = task.get('id')
                last_modified = task.get('last_modified_date') or task.get('created_date')

                # Сохраняем в историю (tasks)
                cursor.execute('''
                    INSERT OR IGNORE INTO tasks
                    (form_id, task_id, title, description, author_id, author_name,
                     created_at, finished_at, last_modified, current_step, status,
                     raw_data, snapshot_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    form_id,
                    task_id,
                    task.get('title'),
                    task.get('description'),
                    self._extract_author_id(task),
                    self._extract_author_name(task),
                    task.get('created_date'),
                    task.get('finished_date'),
                    last_modified,
                    self._extract_step_name(task),
                    self._determine_status(task),
                    json.dumps(task, ensure_ascii=False),
                    snapshot_ts
                ))

                # Обновляем актуальное состояние (latest_tasks)
                cursor.execute('''
                    INSERT OR REPLACE INTO latest_tasks
                    (form_id, task_id, title, status, last_modified, raw_data, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ''', (
                    form_id,
                    task_id,
                    task.get('title'),
                    self._determine_status(task),
                    last_modified,
                    json.dumps(task, ensure_ascii=False)
                ))

                return True

        except Exception as e:
            logger.error(f"Ошибка сохранения задачи {task.get('id')}: {e}")
            return False

    def save_tasks(self, form_id: int, tasks: List[Dict]) -> int:
        """
        Сохранить несколько задач

        Args:
            form_id: ID формы
            tasks: Список задач

        Returns:
            Количество сохранённых задач
        """
        count = 0
        for task in tasks:
            if self.save_task(form_id, task):
                count += 1
        logger.info(f"Сохранено {count}/{len(tasks)} задач формы {form_id}")
        return count

    def _extract_author_id(self, task: Dict) -> Optional[int]:
        """
        Извлечь ID автора из задачи

        Args:
            task: Данные задачи

        Returns:
            ID автора или None
        """
        author = task.get('author')
        if isinstance(author, dict):
            return author.get('id')
        elif isinstance(author, int):
            return author
        return None

    def _extract_author_name(self, task: Dict) -> Optional[str]:
        """
        Извлечь имя автора из задачи

        Args:
            task: Данные задачи

        Returns:
            Имя автора или None
        """
        author = task.get('author')
        if isinstance(author, dict):
            return author.get('name')
        return None

    def _extract_step_name(self, task: Dict) -> Optional[str]:
        """
        Извлечь название текущего шага

        Args:
            task: Данные задачи

        Returns:
            Название шага или None
        """
        current_step = task.get('current_step')
        if isinstance(current_step, dict):
            return current_step.get('name')
        elif isinstance(current_step, str):
            return current_step
        return None

    def _determine_status(self, task: Dict) -> str:
        """
        Определить статус задачи

        Args:
            task: Данные задачи

        Returns:
            Статус (active, finished, archived, etc)
        """
        if task.get('is_archived'):
            return 'archived'
        if task.get('finished_date'):
            return 'finished'
        if task.get('current_step'):
            return 'active'
        return 'draft'

    def get_forms(self) -> List[Dict]:
        """Получить все формы"""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT * FROM forms ORDER BY title')
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Ошибка получения форм: {e}")
            return []

    def get_latest_tasks(
        self,
        form_id: Optional[int] = None,
        limit: int = 1000
    ) -> List[Dict]:
        """
        Получить актуальные задачи

        Args:
            form_id: Фильтр по форме (если None, все формы)
            limit: Максимум задач

        Returns:
            Список задач
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()

                if form_id:
                    cursor.execute('''
                        SELECT * FROM latest_tasks
                        WHERE form_id = ?
                        ORDER BY last_modified DESC
                        LIMIT ?
                    ''', (form_id, limit))
                else:
                    cursor.execute('''
                        SELECT * FROM latest_tasks
                        ORDER BY last_modified DESC
                        LIMIT ?
                    ''', (limit,))

                return [dict(row) for row in cursor.fetchall()]

        except Exception as e:
            logger.error(f"Ошибка получения задач: {e}")
            return []

    def get_tasks_history(
        self,
        form_id: Optional[int] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None
    ) -> List[Dict]:
        """
        Получить историю задач (снапшоты)

        Args:
            form_id: Фильтр по форме
            date_from: Начало периода
            date_to: Конец периода

        Returns:
            Список снапшотов
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()

                query = 'SELECT * FROM tasks WHERE 1=1'
                params = []

                if form_id:
                    query += ' AND form_id = ?'
                    params.append(form_id)

                if date_from:
                    query += ' AND snapshot_at >= ?'
                    params.append(date_from)

                if date_to:
                    query += ' AND snapshot_at <= ?'
                    params.append(date_to)

                query += ' ORDER BY snapshot_at DESC'

                cursor.execute(query, params)
                return [dict(row) for row in cursor.fetchall()]

        except Exception as e:
            logger.error(f"Ошибка получения истории: {e}")
            return []

    def start_sync_log(self) -> int:
        """Начать запись лога синхронизации, возвращает ID записи"""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO sync_log (started_at, status)
                    VALUES (CURRENT_TIMESTAMP, 'started')
                ''')
                return cursor.lastrowid
        except Exception as e:
            logger.error(f"Ошибка создания log записи: {e}")
            return 0

    def finish_sync_log(
        self,
        log_id: int,
        forms_count: int,
        tasks_count: int,
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
                        forms_count = ?,
                        tasks_count = ?,
                        status = ?,
                        error_message = ?
                    WHERE id = ?
                ''', (forms_count, tasks_count, status, error_message, log_id))
        except Exception as e:
            logger.error(f"Ошибка обновления log записи: {e}")

    def get_last_sync_time(self) -> Optional[datetime]:
        """Получить время последней успешной синхронизации"""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT finished_at FROM sync_log
                    WHERE status = 'completed'
                    ORDER BY finished_at DESC LIMIT 1
                ''')
                row = cursor.fetchone()
                if row:
                    return datetime.fromisoformat(row['finished_at'])
                return None
        except Exception as e:
            logger.error(f"Ошибка получения последней синхронизации: {e}")
            return None

    def get_stats(self) -> Dict:
        """Получить статистику БД"""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()

                stats = {}

                # Количество форм
                cursor.execute('SELECT COUNT(*) as count FROM forms')
                stats['forms_count'] = cursor.fetchone()['count']

                # Количество актуальных задач
                cursor.execute('SELECT COUNT(*) as count FROM latest_tasks')
                stats['tasks_count'] = cursor.fetchone()['count']

                # Количество исторических записей
                cursor.execute('SELECT COUNT(*) as count FROM tasks')
                stats['history_count'] = cursor.fetchone()['count']

                # Последняя синхронизация
                last_sync = self.get_last_sync_time()
                stats['last_sync'] = last_sync.isoformat() if last_sync else None

                return stats

        except Exception as e:
            logger.error(f"Ошибка получения статистики: {e}")
            return {}


def get_storage(db_path: Optional[str] = None) -> PyrusStorage:
    """Factory function для получения хранилища"""
    return PyrusStorage(db_path)


if __name__ == "__main__":
    # Тест
    print("Тест Pyrus Storage...")

    storage = get_storage(':memory:')  # В памяти для теста

    print("Статистика:")
    stats = storage.get_stats()
    for key, value in stats.items():
        print(f"  {key}: {value}")
