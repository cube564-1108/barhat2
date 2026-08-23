"""
Pyrus Data Storage
Модуль для хранения данных Pyrus в SQLite с историчностью
"""

import os
import time
import shutil
import sqlite3
import json
import logging
from datetime import datetime, timedelta
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
        # timeout + busy_timeout, как в остальных модулях: воркеров теперь не 2,
        # а 2 × 8 потоков (см. amvera.yml), и без запаса на ожидание блокировки
        # параллельный запрос получил бы "database is locked" вместо очереди.
        conn = sqlite3.connect(self.db_path, timeout=20)
        conn.row_factory = sqlite3.Row  # Доступ по имени колонки
        # journal_mode здесь больше не выставляется: это персистентное свойство
        # файла БД, и переключать его на каждом соединении — лишняя запись на
        # общий диск /data при каждом запросе. Ставится один раз в _init_db.
        #
        # busy_timeout и synchronous, наоборот, живут в соединении и нужны
        # каждому: без первого параллельный запрос получает "database is locked"
        # вместо очереди, а synchronous=NORMAL в паре с WAL даёт fsync только на
        # чекпойнте, а не на каждой транзакции — на сетевом диске Amvera именно
        # fsync съедал дисковую очередь и тормозил соседние базы, включая
        # авторизацию всего сайта.
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
            # Переключение журнала требует эксклюзивной блокировки и, в отличие
            # от обычной записи, НЕ ждёт по busy_timeout: если базу в этот момент
            # держит второй воркер gunicorn (оба стартуют одновременно), PRAGMA
            # падает с "database is locked" и уронила бы весь _init_db. Проиграть
            # гонку здесь безобидно — либо база уже в WAL, либо её переключает
            # сосед.
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError as e:
                logger.info(f"journal_mode=WAL выставляет параллельный воркер: {e}")

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

            # Служебное key-value: курсоры синхронизации и локи прогонов.
            # Лок нужен потому, что на Amvera 2 воркера gunicorn и в каждом
            # крутится свой планировщик — без него оба качали бы одно и то же.
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS sync_state (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # Витрина оценок качества: поля формы, разложенные по колонкам.
            #
            # Раньше отчёт на каждый запрос читал всю таблицу tasks (97 тыс.
            # строк с полным JSON, ~500 МБ) и парсил её в Python — только чтобы
            # посчитать средние. Здесь те же данные лежат готовыми к GROUP BY,
            # одна строка на задачу. Заполняется при синхронизации, полностью
            # пересобирается из latest_tasks (см. quality.rebuild_projection).
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS quality_scores (
                    task_id INTEGER PRIMARY KEY,
                    form_id INTEGER NOT NULL,
                    task_date TEXT,
                    salon TEXT,
                    florist TEXT,
                    category TEXT,
                    cat_group TEXT,
                    max_score INTEGER,
                    total_score INTEGER,
                    order_id TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_quality_date
                ON quality_scores(task_date)
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_quality_salon_date
                ON quality_scores(salon, task_date)
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_quality_florist_date
                ON quality_scores(florist, task_date)
            ''')

            # Баллы по отдельным критериям формы (0/1/2): аккуратность упаковки,
            # техника сборки и т.д. Итоговая оценка — их сумма, но по ней не
            # понять, ЧТО именно проседает в салоне. Салон и дата продублированы
            # сюда намеренно: разбор группирует по ним, и без дубля каждый запрос
            # тянул бы JOIN с quality_scores.
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS quality_criteria_scores (
                    task_id INTEGER NOT NULL,
                    criterion_id INTEGER NOT NULL,
                    salon TEXT,
                    task_date TEXT,
                    score INTEGER,
                    PRIMARY KEY (task_id, criterion_id)
                )
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_quality_criteria_salon_date
                ON quality_criteria_scores(salon, task_date)
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_quality_criteria_date
                ON quality_criteria_scores(task_date)
            ''')

            # Миграция: колонки для отслеживания прогресса фоновых загрузок.
            # Статус обновления раньше жил в памяти процесса, но на Amvera 2
            # воркера gunicorn — опрос статуса мог попасть на воркер, который
            # ничего не запускал, и показать ложное "готово" (тот же инцидент
            # уже был в МойСкладе, см. комментарий в moysklad/server.py).
            existing = {row['name'] for row in cursor.execute("PRAGMA table_info(sync_log)")}
            for column, ddl in (
                ('job', "ALTER TABLE sync_log ADD COLUMN job TEXT DEFAULT 'update'"),
                ('message', "ALTER TABLE sync_log ADD COLUMN message TEXT"),
                ('updated_at', "ALTER TABLE sync_log ADD COLUMN updated_at TIMESTAMP"),
            ):
                if column not in existing:
                    cursor.execute(ddl)

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
        Сохранить одну задачу (отдельная транзакция).

        Для загрузки из Pyrus использовать save_tasks(): пачкой это на порядки
        быстрее, потому что там одно соединение и один commit на всю пачку.
        """
        return self.save_tasks(form_id, [task], snapshot_at=snapshot_at) == 1

    def save_tasks(
        self,
        form_id: int,
        tasks: List[Dict],
        snapshot_at: Optional[datetime] = None
    ) -> int:
        """
        Сохранить пачку задач за одну транзакцию.

        Раньше здесь был цикл по save_task(), а тот на КАЖДУЮ задачу открывал
        своё соединение, гонял PRAGMA journal_mode/busy_timeout, делал два INSERT
        и commit (то есть fsync) и закрывался. На 16 тысячах задач это 16 тысяч
        транзакций — синхронизация занимала минуты и на всё это время забивала
        общий диск /data, отчего тормозил весь сайт. Ровно ту же ошибку чинили
        в синхронизации МойСклада.

        Второе: снапшот в tasks пишется, только если у задачи изменился
        last_modified. UNIQUE(form_id, task_id, snapshot_at) от дублей не спасал
        (snapshot_at у каждой записи свой), поэтому каждый прогон складывал в
        базу полную копию всех задач — 97 тыс. строк на 16 тыс. задач и 543 МБ
        файла. Повторная синхронизация неизменившегося периода теперь не пишет
        вообще ничего.

        Returns:
            Количество обработанных задач
        """
        if not tasks:
            return 0

        snapshot_ts = snapshot_at or datetime.now()
        processed = 0
        changed = 0

        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()

                # Явный BEGIN обязателен: без него первый SAVEPOINT сам открыл бы
                # транзакцию, а RELEASE самого внешнего savepoint в SQLite её
                # коммитит — получился бы снова коммит на задачу, ради чего батч
                # и затевался. На этих граблях уже стояли в МойСкладе.
                cursor.execute('BEGIN')

                # Что уже лежит в базе — одним запросом, а не SELECT на задачу
                known = {
                    row['task_id']: row['last_modified']
                    for row in cursor.execute(
                        'SELECT task_id, last_modified FROM latest_tasks WHERE form_id = ?',
                        (form_id,)
                    )
                }

                for task in tasks:
                    # SAVEPOINT: битая задача откатывает только себя, а не всю
                    # пачку — иначе одна кривая запись стоила бы целого окна
                    try:
                        cursor.execute('SAVEPOINT task_save')
                        if self._save_task_row(cursor, form_id, task, snapshot_ts, known):
                            changed += 1
                        cursor.execute('RELEASE task_save')
                        processed += 1
                    except Exception as e:
                        cursor.execute('ROLLBACK TO task_save')
                        cursor.execute('RELEASE task_save')
                        logger.error(f"Ошибка сохранения задачи {task.get('id')}: {e}")

        except Exception as e:
            logger.error(f"Ошибка сохранения пачки задач формы {form_id}: {e}")
            return processed

        logger.info(
            f"Обработано {processed}/{len(tasks)} задач формы {form_id} "
            f"(изменилось: {changed})"
        )
        return processed

    def _save_task_row(
        self,
        cursor,
        form_id: int,
        task: Dict,
        snapshot_ts: datetime,
        known: Dict[int, Any]
    ) -> bool:
        """
        Записать задачу переданным курсором (транзакцией управляет вызывающий).

        Returns:
            True, если задача новая или изменилась (была запись в базу)
        """
        task_id = task.get('id')
        last_modified = task.get('last_modified_date') or task.get('created_date')

        # Не менялась с прошлой синхронизации — ни снапшота, ни перезаписи
        if task_id in known and known[task_id] == last_modified:
            return False

        raw_json = json.dumps(task, ensure_ascii=False)
        status = self._determine_status(task)

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
            status,
            raw_json,
            snapshot_ts
        ))

        cursor.execute('''
            INSERT OR REPLACE INTO latest_tasks
            (form_id, task_id, title, status, last_modified, raw_data, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ''', (
            form_id,
            task_id,
            task.get('title'),
            status,
            last_modified,
            raw_json
        ))

        known[task_id] = last_modified
        return True

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

    def start_sync_log(self, job: str = 'update', message: str = '') -> int:
        """Начать запись лога синхронизации, возвращает ID записи"""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO sync_log (started_at, updated_at, status, job, message, tasks_count)
                    VALUES (CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'started', ?, ?, 0)
                ''', (job, message))
                return cursor.lastrowid
        except Exception as e:
            logger.error(f"Ошибка создания log записи: {e}")
            return 0

    def update_sync_log(self, log_id: int, message: str, tasks_count: Optional[int] = None) -> None:
        """
        Обновить прогресс выполняющейся загрузки.

        updated_at служит heartbeat'ом: по нему видно, что фоновый поток жив
        (см. is_sync_running) — иначе упавший воркер оставил бы запись в статусе
        'started' навсегда и заблокировал повторный запуск.
        """
        if not log_id:
            return
        try:
            with self._get_connection() as conn:
                if tasks_count is None:
                    conn.execute(
                        "UPDATE sync_log SET message = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (message, log_id)
                    )
                else:
                    conn.execute(
                        "UPDATE sync_log SET message = ?, tasks_count = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (message, tasks_count, log_id)
                    )
        except Exception as e:
            logger.error(f"Ошибка обновления прогресса log записи: {e}")

    def get_latest_sync_log(self, job: Optional[str] = None) -> Optional[Dict]:
        """Последняя запись лога (при job=None — по всем типам загрузок)"""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                if job:
                    cursor.execute(
                        "SELECT * FROM sync_log WHERE job = ? ORDER BY id DESC LIMIT 1", (job,)
                    )
                else:
                    cursor.execute("SELECT * FROM sync_log ORDER BY id DESC LIMIT 1")
                row = cursor.fetchone()
                return dict(row) if row else None
        except Exception as e:
            logger.error(f"Ошибка получения последней log записи: {e}")
            return None

    def finish_sync_log(
        self,
        log_id: int,
        forms_count: int,
        tasks_count: int,
        status: str = 'completed',
        error_message: Optional[str] = None,
        message: Optional[str] = None
    ) -> None:
        """Завершить запись лога синхронизации"""
        if not log_id:
            return
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    UPDATE sync_log
                    SET finished_at = CURRENT_TIMESTAMP,
                        updated_at = CURRENT_TIMESTAMP,
                        forms_count = ?,
                        tasks_count = ?,
                        status = ?,
                        error_message = ?,
                        message = COALESCE(?, message)
                    WHERE id = ?
                ''', (forms_count, tasks_count, status, error_message, message, log_id))
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

    # ===== Обслуживание истории задач =====
    #
    # До перехода на пачечную запись каждая синхронизация складывала в tasks
    # полную копию всех задач: UNIQUE(form_id, task_id, snapshot_at) от дублей
    # не спасал, потому что snapshot_at был свой у каждой записи. В боевой базе
    # от этого осталось много лишних снапшотов, и это чистый расход диска —
    # отчёты читают витрину, а не историю.
    #
    # Чистим до одного, самого свежего снапшота на задачу: latest_tasks и
    # витрины от этого не зависят, а /api/pyrus/tasks с фильтром по датам
    # продолжает работать, просто история версий схлопывается.

    def count_task_history_duplicates(self) -> Dict:
        """Посчитать, сколько лишних снапшотов лежит в tasks (без изменений)"""
        try:
            with self._get_connection() as conn:
                total = conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
                unique = conn.execute(
                    'SELECT COUNT(*) FROM (SELECT 1 FROM tasks GROUP BY form_id, task_id)'
                ).fetchone()[0]

                redundant = total - unique
                return {
                    'total_rows': total,
                    'unique_tasks': unique,
                    'redundant_rows': redundant,
                    # Доля лишних строк, а не оценка освобождаемых мегабайт:
                    # в базе кроме tasks лежат latest_tasks, витрины и индексы,
                    # поэтому пересчёт доли строк в мегабайты завышал бы вдвое
                    # (на боевом слепке: оценка 432 МБ против реальных 220)
                    'redundant_percent': round(redundant / total * 100, 1) if total else 0.0,
                    # Реальный размер на диске (файл + WAL), а не page_count:
                    # иначе статистика и отчёт о выполненной чистке показывали
                    # бы разные числа при непустом журнале
                    'db_mb': self.database_size_mb(),
                }
        except Exception as e:
            logger.error(f"Ошибка подсчёта дублей истории: {e}")
            raise

    def compact_task_history(
        self,
        batch_size: int = 2000,
        pause: float = 0.15,
        max_rows: int = 200000,
        progress=None
    ) -> Dict:
        """
        Удалить все снапшоты задач, кроме самого свежего по каждой задаче.

        Удаление идёт пачками с паузой: разом снести десятки тысяч строк —
        значит надолго занять единственный диск /data, на котором лежит и база
        авторизации, то есть подвесить весь сайт. За один вызов чистится не
        больше max_rows строк, остаток возвращается в 'remaining'.
        """
        deleted = 0

        try:
            with self._get_connection() as conn:
                # Оконная функция вместо коррелированного подзапроса: тот пришлось
                # бы выполнять на каждую пачку заново по всей таблице
                doomed = [
                    row[0] for row in conn.execute('''
                        SELECT id FROM (
                            SELECT id, ROW_NUMBER() OVER (
                                PARTITION BY form_id, task_id
                                ORDER BY snapshot_at DESC, id DESC
                            ) AS rn
                            FROM tasks
                        )
                        WHERE rn > 1
                        LIMIT ?
                    ''', (max_rows,))
                ]

                total = len(doomed)
                if not total:
                    return {'deleted': 0, 'remaining': 0}

                for start in range(0, total, batch_size):
                    chunk = doomed[start:start + batch_size]
                    placeholders = ','.join('?' * len(chunk))
                    conn.execute(f'DELETE FROM tasks WHERE id IN ({placeholders})', chunk)
                    conn.commit()
                    deleted += len(chunk)

                    if progress:
                        progress(deleted, total)

                    # Пауза отдаёт диск и GIL остальному сайту
                    if pause:
                        time.sleep(pause)

                # Без чекпойнта удалённые страницы остаются в WAL, и файл журнала
                # разрастается вместо того, чтобы освободить место
                conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')

                remaining = conn.execute('''
                    SELECT COUNT(*) FROM (
                        SELECT id, ROW_NUMBER() OVER (
                            PARTITION BY form_id, task_id
                            ORDER BY snapshot_at DESC, id DESC
                        ) AS rn
                        FROM tasks
                    )
                    WHERE rn > 1
                ''').fetchone()[0]

                return {'deleted': deleted, 'remaining': remaining}

        except Exception as e:
            logger.error(f"Ошибка чистки истории задач: {e}")
            raise

    def vacuum(self) -> Dict:
        """
        Сжать файл базы после чистки.

        Удаление строк освобождает страницы внутри файла, но не отдаёт место
        файловой системе — это делает только VACUUM. Он переписывает базу
        целиком во временный файл, поэтому требует места примерно в размер
        базы и держит эксклюзивную блокировку всё время работы.
        """
        before = self.database_size_mb()

        free_mb = shutil.disk_usage(os.path.dirname(self.db_path) or '.').free / 1024 / 1024
        if free_mb < before * 2:
            raise RuntimeError(
                f'Недостаточно места для VACUUM: свободно {free_mb:.0f} МБ, '
                f'нужно от {before * 2:.0f} МБ'
            )

        # isolation_level=None — VACUUM нельзя выполнить внутри транзакции,
        # а обычное соединение модуля sqlite3 открывает её неявно
        conn = sqlite3.connect(self.db_path, timeout=60, isolation_level=None)
        try:
            conn.execute('PRAGMA busy_timeout=60000')
            started = time.monotonic()
            conn.execute('VACUUM')
            seconds = round(time.monotonic() - started, 2)
        finally:
            conn.close()

        after = self.database_size_mb()
        return {
            'before_mb': before,
            'after_mb': after,
            'freed_mb': round(before - after, 2),
            'seconds': seconds,
        }

    def database_size_mb(self) -> float:
        """Размер файла базы вместе с журналом WAL"""
        total = 0
        for suffix in ('', '-wal'):
            path = self.db_path + suffix
            if os.path.exists(path):
                total += os.path.getsize(path)
        return round(total / 1024 / 1024, 2)

    # ===== Локи прогонов =====
    #
    # Проверки «статус последнего sync_log == started» для этого не хватает:
    # между чтением статуса и стартом потока есть окно, в которое влезает второй
    # воркер (их на Amvera два, и в каждом свой планировщик) — и оба качают одно
    # и то же. Захват здесь — один UPDATE ... WHERE, атомарный на уровне SQLite.
    # В value лежит время истечения: держатель мог умереть вместе с воркером
    # (деплой, OOM) и не позвать release — по TTL лок освободится сам.

    def try_acquire_sync_lock(self, name: str, ttl_seconds: int) -> bool:
        """Захватить лок. True — лок наш, False — держит кто-то другой"""
        key = f'lock:{name}'
        now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        expires = (datetime.utcnow() + timedelta(seconds=ttl_seconds)).strftime('%Y-%m-%d %H:%M:%S')
        try:
            with self._get_connection() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO sync_state (key, value, updated_at)"
                    " VALUES (?, '', CURRENT_TIMESTAMP)",
                    (key,)
                )
                # Формат времени фиксированной ширины — лексикографическое
                # сравнение строк совпадает с хронологическим
                cursor = conn.execute('''
                    UPDATE sync_state
                    SET value = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE key = ? AND (value = '' OR value < ?)
                ''', (expires, key, now))
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Ошибка захвата лока {name}: {e}")
            return False

    def renew_sync_lock(self, name: str, ttl_seconds: int) -> None:
        """Продлить свой лок (вызывать по ходу долгого прогона)"""
        expires = (datetime.utcnow() + timedelta(seconds=ttl_seconds)).strftime('%Y-%m-%d %H:%M:%S')
        try:
            with self._get_connection() as conn:
                conn.execute(
                    'UPDATE sync_state SET value = ?, updated_at = CURRENT_TIMESTAMP WHERE key = ?',
                    (expires, f'lock:{name}')
                )
        except Exception as e:
            logger.error(f"Ошибка продления лока {name}: {e}")

    def release_sync_lock(self, name: str) -> None:
        """Освободить лок"""
        try:
            with self._get_connection() as conn:
                conn.execute(
                    "UPDATE sync_state SET value = '', updated_at = CURRENT_TIMESTAMP WHERE key = ?",
                    (f'lock:{name}',)
                )
        except Exception as e:
            logger.error(f"Ошибка освобождения лока {name}: {e}")

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
