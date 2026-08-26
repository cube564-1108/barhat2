"""
Отчёт по качеству сборки букетов (форма Pyrus 1327961).

Раньше отчёт жил в scripts/quality_report_v2.py и на КАЖДЫЙ запрос читал всю
таблицу tasks коррелированным подзапросом `snapshot_at = (SELECT MAX(...))` —
97 тысяч строк с полным JSON ради 16 тысяч актуальных задач, потом json.loads
на каждую. Локально это 2.2 с на запрос плюс секунда на парсинг; на сетевом
диске Amvera — кратно хуже, и открытие страницы с тремя модалками означало
четыре таких полных скана.

Здесь все отчёты — это GROUP BY по витрине quality_scores (одна строка на
задачу, поля формы разложены по колонкам, индексы по дате/салону/флористу).
Витрина заполняется при синхронизации, а не при чтении.
"""

import os
import json
import logging
import sqlite3
import threading
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

from sqlite_conn import connect as sqlite_connect

logger = logging.getLogger(__name__)

QUALITY_FORM_ID = 1327961

# Категории с максимальным баллом
CATEGORIES_14 = [
    'Клубничный букет',
    'Цветочный букет',
    'Коробочка с клубникой или бананами',
    'Цветочный бокс',
    'Клубничный бокс',
]

CATEGORIES_18 = [
    'Клубнично-цветочный букет',
    'Коробочка+цветочный букет',
    'Цветочно-клубничный бокс',
]

# ID полей в форме
FIELD_DATE = 1          # Дата
FIELD_FLORIST = 3       # Флорист
FIELD_ORDER_ID = 4      # Номер заказа
FIELD_CATEGORY = 6      # Вид заказа
FIELD_SALON = 10        # Салон
FIELD_TOTAL_SCORE = 18  # Итоговая оценка

_MAX_SCORE_DEFAULT = 14

# Критерии оценки: балл 0/1/2 по каждому, итоговая оценка — их сумма.
# Названия в родительном/именительном виде, пригодном для подстановки в текст.
CRITERIA = {
    7: 'соответствие каталогу',
    8: 'аккуратность упаковки',
    11: 'оформление клубники',
    13: 'техника сборки',
    14: 'клубника отделена плёнкой',
    15: 'вложение материалов',
    16: 'фотография',
    20: 'обработка цветка',
    23: 'свежесть компонентов',
}

CRITERION_MAX = 2

# Набор применимых критериев зависит от вида заказа, и в форме это выражено тем,
# заполнено поле или нет. У «Клубничного букета» «Обработка цветка» заполнена в
# 66 оценках из 8462 — критерий неприменим, и в этих редких случаях стоит 0.
# Считать такие нули провалом нельзя, поэтому критерий попадает в разбор, только
# если заполнен хотя бы у половины оценок выборки.
CRITERION_COVERAGE_MIN = 0.5

# Ниже этого числа оценок выводы по салону считаются ненадёжными
ASSESSMENT_MIN_SAMPLE = 15
# Порог «сильной стороны» и «проседания» в процентах от максимума
STRONG_PERCENT = 97.0
WEAK_PERCENT = 90.0
# Отставание/опережение сети, начиная с которого это стоит называть вслух
NETWORK_GAP_PP = 5.0

# Пересборка витрины делается один раз на процесс, а не на запрос: инициализация
# на каждом запросе — одна из причин, по которым фоновая работа уже клала сайт.
_projection_lock = threading.Lock()
_projection_checked = False


def _db_path() -> str:
    return os.getenv('PYRUS_DB_PATH', 'data/pyrus.db')


def _connect() -> sqlite3.Connection:
    """
    Соединение для чтения отчётов.

    Настройки соединения — в sqlite_conn: смена режима журнала пишет в
    заголовок базы, и делать это на каждом соединении читателя было бы лишней
    записью на общий диск. WAL там включается один раз на файл за процесс.
    """
    return sqlite_connect(_db_path(), timeout=20)


# ===== Разбор задачи формы =====

def get_category_group(category: Optional[str]) -> str:
    """Группа категории: cat18 — виды заказа с максимумом 18 баллов"""
    if category in CATEGORIES_18:
        return 'cat18'
    return 'cat14'


def get_max_score(category: Optional[str]) -> int:
    """Максимальный балл для вида заказа"""
    return 18 if category in CATEGORIES_18 else _MAX_SCORE_DEFAULT


def extract_task_data(task: Dict) -> Optional[Dict]:
    """
    Достать из задачи Pyrus поля оценки.

    Returns:
        Словарь полей или None, если задача не годится для отчёта
        (нет флориста/салона/вида заказа или не проставлена итоговая оценка).
    """
    florist = salon = category = order_id = None
    total_score = None
    task_date = None

    for field in task.get('fields', []):
        field_id = field.get('id')
        value = field.get('value')

        if field_id == FIELD_FLORIST and isinstance(value, dict):
            names = value.get('choice_names')
            florist = names[0] if names else None
        elif field_id == FIELD_SALON and isinstance(value, dict):
            names = value.get('choice_names')
            salon = names[0] if names else None
        elif field_id == FIELD_CATEGORY and isinstance(value, dict):
            names = value.get('choice_names')
            category = names[0] if names else None
        elif field_id == FIELD_ORDER_ID:
            order_id = value
        elif field_id == FIELD_TOTAL_SCORE:
            total_score = value
        elif field_id == FIELD_DATE:
            task_date = value

    if not task_date and task.get('create_date'):
        task_date = task['create_date'][:10]

    if not florist or not salon or not category or total_score is None:
        return None

    try:
        total_score = int(total_score)
    except (ValueError, TypeError):
        return None

    return {
        'task_id': task.get('id'),
        'date': task_date,
        'florist': florist,
        'salon': salon,
        'category': category,
        'order_id': str(order_id) if order_id is not None else None,
        'total_score': total_score,
        'criteria': extract_criteria(task),
    }


def extract_criteria(task: Dict) -> Dict[int, int]:
    """
    Достать баллы по отдельным критериям.

    Отсутствие поля означает «критерий неприменим к этому виду заказа» — такие
    критерии в результат не попадают, чтобы не путать их с нулевым баллом.
    """
    scores = {}

    for field in task.get('fields', []):
        field_id = field.get('id')
        if field_id not in CRITERIA:
            continue

        value = field.get('value')
        if not isinstance(value, dict):
            continue

        names = value.get('choice_names')
        if not names:
            continue

        raw = str(names[0]).strip()
        if not raw.isdigit():
            continue

        scores[field_id] = int(raw)

    return scores


# ===== Витрина =====

def _upsert_rows(cursor, tasks: List[Dict]) -> Tuple[int, int]:
    """Записать разобранные задачи в quality_scores переданным курсором"""
    saved = 0
    skipped = 0

    for task in tasks:
        task_id = task.get('id')
        if task_id is None:
            continue

        parsed = extract_task_data(task)
        if not parsed:
            # Задача могла попасть в витрину раньше, а потом у неё убрали
            # оценку — тогда её надо убрать и из отчёта
            cursor.execute('DELETE FROM quality_scores WHERE task_id = ?', (task_id,))
            cursor.execute('DELETE FROM quality_criteria_scores WHERE task_id = ?', (task_id,))
            skipped += 1
            continue

        cursor.execute('''
            INSERT OR REPLACE INTO quality_scores
            (task_id, form_id, task_date, salon, florist, category,
             cat_group, max_score, total_score, order_id, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ''', (
            task_id,
            QUALITY_FORM_ID,
            parsed['date'],
            parsed['salon'],
            parsed['florist'],
            parsed['category'],
            get_category_group(parsed['category']),
            get_max_score(parsed['category']),
            parsed['total_score'],
            parsed['order_id'],
        ))

        # Критерии переписываем целиком: оценку могли исправить и снять балл,
        # а INSERT OR REPLACE обновил бы только пришедшие строки
        cursor.execute('DELETE FROM quality_criteria_scores WHERE task_id = ?', (task_id,))
        for criterion_id, score in parsed['criteria'].items():
            cursor.execute('''
                INSERT INTO quality_criteria_scores
                (task_id, criterion_id, salon, task_date, score)
                VALUES (?, ?, ?, ?, ?)
            ''', (task_id, criterion_id, parsed['salon'], parsed['date'], score))

        saved += 1

    return saved, skipped


def upsert_tasks(tasks: List[Dict]) -> int:
    """
    Обновить витрину по пачке задач (вызывается после сохранения окна загрузки).

    Одна транзакция на пачку — тот же принцип, что и в storage.save_tasks.
    """
    if not tasks:
        return 0

    conn = _connect()
    try:
        cursor = conn.cursor()
        saved, skipped = _upsert_rows(cursor, tasks)
        conn.commit()
        if skipped:
            logger.info(f"Витрина качества: записано {saved}, без оценки {skipped}")
        return saved
    except Exception as e:
        conn.rollback()
        logger.error(f"Ошибка обновления витрины качества: {e}")
        return 0
    finally:
        conn.close()


def rebuild_projection() -> Dict:
    """
    Полностью пересобрать витрину из latest_tasks.

    Нужна при первом появлении таблицы (в базе уже лежит история) и после
    изменения списка категорий или разбора полей формы. Читает 16 тысяч строк
    одним проходом и пишет одной транзакцией.
    """
    started = datetime.utcnow()
    conn = _connect()
    try:
        cursor = conn.cursor()
        rows = cursor.execute(
            'SELECT raw_data FROM latest_tasks WHERE form_id = ?', (QUALITY_FORM_ID,)
        ).fetchall()

        tasks = []
        for row in rows:
            try:
                tasks.append(json.loads(row['raw_data']))
            except (TypeError, ValueError):
                continue

        cursor.execute('DELETE FROM quality_scores WHERE form_id = ?', (QUALITY_FORM_ID,))
        # Таблица критериев обслуживает только форму качества, поэтому чистится
        # целиком — отдельной колонки form_id в ней нет
        cursor.execute('DELETE FROM quality_criteria_scores')
        saved, skipped = _upsert_rows(cursor, tasks)
        conn.commit()

        seconds = round((datetime.utcnow() - started).total_seconds(), 2)
        logger.info(
            f"Витрина качества пересобрана: {saved} оценок из {len(tasks)} задач "
            f"(без оценки {skipped}) за {seconds} с"
        )
        return {'tasks': len(tasks), 'scores': saved, 'skipped': skipped, 'seconds': seconds}
    except Exception as e:
        conn.rollback()
        logger.error(f"Ошибка пересборки витрины качества: {e}")
        raise
    finally:
        conn.close()


def ensure_projection() -> None:
    """
    Один раз на процесс: если витрина пуста, а задачи в базе есть — собрать её.

    Вызывается на старте сервера, а не из обработчиков запросов. Лок нужен,
    чтобы два воркера gunicorn не пересобирали витрину одновременно.
    """
    global _projection_checked

    with _projection_lock:
        if _projection_checked:
            return
        _projection_checked = True

    try:
        conn = _connect()
        try:
            scores = conn.execute(
                'SELECT COUNT(*) FROM quality_scores WHERE form_id = ?', (QUALITY_FORM_ID,)
            ).fetchone()[0]
            # Пустая таблица критериев при заполненной quality_scores — это
            # деплой, добавивший разбор по критериям к уже собранной витрине
            criteria = conn.execute('SELECT COUNT(*) FROM quality_criteria_scores').fetchone()[0]
            tasks = conn.execute(
                'SELECT COUNT(*) FROM latest_tasks WHERE form_id = ?', (QUALITY_FORM_ID,)
            ).fetchone()[0]
        finally:
            conn.close()

        if (scores == 0 or criteria == 0) and tasks > 0:
            from .storage import get_storage
            store = get_storage()
            if store.try_acquire_sync_lock('quality-projection', 600):
                try:
                    rebuild_projection()
                finally:
                    store.release_sync_lock('quality-projection')
            else:
                logger.info("Витрину качества собирает другой воркер — пропускаем")
    except Exception as e:
        logger.error(f"Не удалось подготовить витрину качества: {e}")


# ===== Отчёты =====

def _date_filter(date_from: Optional[str], date_to: Optional[str]) -> Tuple[str, list]:
    """Кусок WHERE и параметры для фильтра по дате оценки"""
    where = ['form_id = ?']
    params: list = [QUALITY_FORM_ID]

    if date_from:
        where.append('task_date >= ?')
        params.append(date_from)
    if date_to:
        where.append('task_date <= ?')
        params.append(date_to)

    return ' AND '.join(where), params


def _avg(total: float, count: int) -> float:
    return round(total / count, 2) if count else 0.0


def _percent(avg_score: float, max_score: int) -> float:
    return round(avg_score / max_score * 100, 1) if max_score else 0.0


def _empty_group(max_score: int) -> Dict:
    return {'avg_score': 0.0, 'percent': 0.0, 'count': 0, 'max_score': max_score}


def generate_report(date_from: Optional[str] = None, date_to: Optional[str] = None) -> Dict:
    """Сводный отчёт: средние баллы по салонам и флористам за период"""
    where, params = _date_filter(date_from, date_to)

    conn = _connect()
    try:
        grouped = conn.execute(f'''
            SELECT salon, florist, cat_group,
                   COUNT(*) AS cnt, SUM(total_score) AS score_sum
            FROM quality_scores
            WHERE {where}
            GROUP BY salon, florist, cat_group
        ''', params).fetchall()

        categories = conn.execute(f'''
            SELECT category, COUNT(*) AS cnt
            FROM quality_scores
            WHERE {where}
            GROUP BY category
        ''', params).fetchall()
    finally:
        conn.close()

    def new_bucket():
        return {'cat14': [0, 0], 'cat18': [0, 0]}  # [count, sum]

    salons: Dict[str, Dict] = {}
    florists: Dict[str, Dict] = {}
    # Салон флориста определяем по тому, где у него больше всего оценок:
    # раньше брался первый попавшийся, и у флориста, работавшего в двух салонах,
    # в карточке стоял случайный
    florist_salon_counts: Dict[str, Dict[str, int]] = {}
    salon_florists: Dict[str, Dict[str, Dict]] = {}

    for row in grouped:
        salon = row['salon']
        florist = row['florist']
        group = row['cat_group'] if row['cat_group'] in ('cat14', 'cat18') else 'cat14'
        cnt = row['cnt']
        score_sum = row['score_sum'] or 0

        bucket = salons.setdefault(salon, new_bucket())
        bucket[group][0] += cnt
        bucket[group][1] += score_sum

        fbucket = florists.setdefault(florist, new_bucket())
        fbucket[group][0] += cnt
        fbucket[group][1] += score_sum

        florist_salon_counts.setdefault(florist, {})
        florist_salon_counts[florist][salon] = florist_salon_counts[florist].get(salon, 0) + cnt

        # Счётчики флориста ВНУТРИ салона: раньше сюда подставлялась его
        # статистика по всем салонам сразу и не сходилась с общим count
        sf = salon_florists.setdefault(salon, {}).setdefault(florist, new_bucket())
        sf[group][0] += cnt
        sf[group][1] += score_sum

    def render(bucket: Dict) -> Dict:
        c14, s14 = bucket['cat14']
        c18, s18 = bucket['cat18']
        avg14 = _avg(s14, c14)
        avg18 = _avg(s18, c18)
        total_count = c14 + c18
        return {
            'cat14': {
                'avg_score': avg14,
                'percent': _percent(avg14, 14),
                'count': c14,
                'max_score': 14,
            },
            'cat18': {
                'avg_score': avg18,
                'percent': _percent(avg18, 18),
                'count': c18,
                'max_score': 18,
            },
            'total': {
                'avg_score': _avg(s14 + s18, total_count),
                'count': total_count,
            },
        }

    result_salons = {}
    for salon, bucket in salons.items():
        rendered = render(bucket)
        rendered['florists'] = {}
        for florist, fb in salon_florists.get(salon, {}).items():
            c14, s14 = fb['cat14']
            c18, s18 = fb['cat18']
            rendered['florists'][florist] = {
                'avg_score': _avg(s14 + s18, c14 + c18),
                'count': c14 + c18,
                'cat14_count': c14,
                'cat18_count': c18,
            }
        result_salons[salon] = rendered

    result_florists = {}
    for florist, bucket in florists.items():
        rendered = render(bucket)
        by_salon = florist_salon_counts.get(florist, {})
        rendered['salon'] = max(by_salon, key=by_salon.get) if by_salon else None
        result_florists[florist] = rendered

    total_tasks = sum(row['cnt'] for row in categories)
    score_total = sum(b['cat14'][1] + b['cat18'][1] for b in salons.values())
    score_count = sum(b['cat14'][0] + b['cat18'][0] for b in salons.values())

    return {
        'period': {'from': date_from, 'to': date_to},
        # Считаем именно оценки, попавшие в отчёт. Раньше здесь стояло общее
        # число задач периода вместе с теми, что отброшены как незаполненные,
        # и KPI не сходился с суммой по салонам
        'total_tasks': total_tasks,
        'category_distribution': {row['category']: row['cnt'] for row in categories},
        'overall_avg': _avg(score_total, score_count),
        'salons': result_salons,
        'florists': result_florists,
    }


def _month_key(base: date, months_back: int) -> str:
    """Ключ YYYY-MM на months_back месяцев назад от base"""
    year = base.year
    month = base.month - months_back
    while month <= 0:
        month += 12
        year -= 1
    return f'{year:04d}-{month:02d}'


def _history_window(months: int) -> Tuple[str, List[str]]:
    """
    Границы и список ключей месяцев для истории.

    Раньше месяц отсчитывался как `today - timedelta(days=30*i)`: на горизонте
    от полугода такой шаг уезжает и месяцы начинают пропускаться и повторяться.
    """
    months = max(1, min(int(months or 6), 60))
    today = date.today()
    keys = [_month_key(today, i) for i in range(months)]
    return min(keys) + '-01', keys


def get_monthly_history(months: int = 6) -> Dict:
    """История средних баллов по месяцам в разрезе салонов"""
    start, keys = _history_window(months)
    allowed = set(keys)

    conn = _connect()
    try:
        rows = conn.execute('''
            SELECT substr(task_date, 1, 7) AS month, salon, cat_group,
                   COUNT(*) AS cnt, SUM(total_score) AS score_sum
            FROM quality_scores
            WHERE form_id = ? AND task_date >= ?
            GROUP BY month, salon, cat_group
        ''', (QUALITY_FORM_ID, start)).fetchall()
    finally:
        conn.close()

    result: Dict[str, Dict] = {}
    for row in rows:
        month = row['month']
        if month not in allowed:
            continue

        salon_data = result.setdefault(month, {}).setdefault(row['salon'], {
            'cat14_avg': 0.0, 'cat14_percent': 0.0, 'cat14_count': 0,
            'cat18_avg': 0.0, 'cat18_percent': 0.0, 'cat18_count': 0,
        })

        group = row['cat_group'] if row['cat_group'] in ('cat14', 'cat18') else 'cat14'
        avg = _avg(row['score_sum'] or 0, row['cnt'])
        salon_data[f'{group}_avg'] = avg
        salon_data[f'{group}_count'] = row['cnt']
        salon_data[f'{group}_percent'] = _percent(avg, 14 if group == 'cat14' else 18)

    return result


def get_salon_history(salon_name: str, months: int = 6) -> Dict:
    """История средних баллов одного салона по месяцам"""
    start, keys = _history_window(months)
    allowed = set(keys)

    conn = _connect()
    try:
        rows = conn.execute('''
            SELECT substr(task_date, 1, 7) AS month, cat_group,
                   COUNT(*) AS cnt, SUM(total_score) AS score_sum
            FROM quality_scores
            WHERE form_id = ? AND salon = ? AND task_date >= ?
            GROUP BY month, cat_group
        ''', (QUALITY_FORM_ID, salon_name, start)).fetchall()
    finally:
        conn.close()

    buckets: Dict[str, Dict[str, List[int]]] = {}
    for row in rows:
        month = row['month']
        if month not in allowed:
            continue
        group = row['cat_group'] if row['cat_group'] in ('cat14', 'cat18') else 'cat14'
        bucket = buckets.setdefault(month, {'cat14': [0, 0], 'cat18': [0, 0]})
        bucket[group][0] += row['cnt']
        bucket[group][1] += row['score_sum'] or 0

    result = {}
    for month, bucket in buckets.items():
        c14, s14 = bucket['cat14']
        c18, s18 = bucket['cat18']
        avg14 = _avg(s14, c14)
        avg18 = _avg(s18, c18)
        result[month] = {
            'cat14_avg': avg14,
            'cat14_percent': _percent(avg14, 14),
            'cat14_count': c14,
            'cat18_avg': avg18,
            'cat18_percent': _percent(avg18, 18),
            'cat18_count': c18,
            'total_avg': _avg(s14 + s18, c14 + c18),
            'total_count': c14 + c18,
        }

    return result


def get_salon_order_types(
    salon_name: str,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None
) -> Dict:
    """Разбивка качества по видам заказа для салона"""
    where, params = _date_filter(date_from, date_to)

    conn = _connect()
    try:
        rows = conn.execute(f'''
            SELECT category, max_score,
                   COUNT(*) AS cnt, SUM(total_score) AS score_sum
            FROM quality_scores
            WHERE {where} AND salon = ?
            GROUP BY category, max_score
        ''', params + [salon_name]).fetchall()
    finally:
        conn.close()

    # Известные виды заказа показываем всегда — даже с нулём, чтобы было видно,
    # что салон их не собирал
    known = {c: 14 for c in CATEGORIES_14}
    known.update({c: 18 for c in CATEGORIES_18})

    order_types = {
        name: {'order_type': name, 'avg_score': 0.0, 'max_score': max_score, 'count': 0}
        for name, max_score in known.items()
    }

    for row in rows:
        category = row['category']
        # Вид заказа, которого нет в списке (появился в форме позже), раньше
        # просто не показывался — теперь он виден со своим максимумом
        item = order_types.setdefault(category, {
            'order_type': category,
            'avg_score': 0.0,
            'max_score': row['max_score'] or _MAX_SCORE_DEFAULT,
            'count': 0,
        })
        item['count'] = row['cnt']
        item['avg_score'] = _avg(row['score_sum'] or 0, row['cnt'])

    result = sorted(
        order_types.values(),
        key=lambda x: (x['count'] == 0, -x['avg_score'])
    )

    return {
        'salon': salon_name,
        'period': {'from': date_from, 'to': date_to},
        'order_types': result,
    }


def get_salon_florists(
    salon_name: str,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None
) -> Dict:
    """Статистика по флористам одного салона"""
    where, params = _date_filter(date_from, date_to)

    conn = _connect()
    try:
        rows = conn.execute(f'''
            SELECT florist, cat_group,
                   COUNT(*) AS cnt, SUM(total_score) AS score_sum
            FROM quality_scores
            WHERE {where} AND salon = ?
            GROUP BY florist, cat_group
        ''', params + [salon_name]).fetchall()
    finally:
        conn.close()

    buckets: Dict[str, Dict[str, List[int]]] = {}
    for row in rows:
        group = row['cat_group'] if row['cat_group'] in ('cat14', 'cat18') else 'cat14'
        bucket = buckets.setdefault(row['florist'], {'cat14': [0, 0], 'cat18': [0, 0]})
        bucket[group][0] += row['cnt']
        bucket[group][1] += row['score_sum'] or 0

    florists = []
    for florist, bucket in buckets.items():
        c14, s14 = bucket['cat14']
        c18, s18 = bucket['cat18']
        florists.append({
            'florist': florist,
            'cat14': {'avg_score': _avg(s14, c14), 'count': c14},
            'cat18': {'avg_score': _avg(s18, c18), 'count': c18},
        })

    florists.sort(key=lambda x: -(x['cat14']['count'] + x['cat18']['count']))

    return {
        'salon': salon_name,
        'period': {'from': date_from, 'to': date_to},
        'florists': florists,
    }


def _previous_period(date_from: Optional[str], date_to: Optional[str]):
    """
    Предыдущий период той же длины — с ним сравнивается динамика.

    Возвращает (from, to) или (None, None), если период не задан целиком:
    сравнивать «всю историю» не с чем.
    """
    if not date_from or not date_to:
        return None, None

    try:
        start = date.fromisoformat(date_from)
        end = date.fromisoformat(date_to)
    except ValueError:
        return None, None

    if end < start:
        return None, None

    length = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=length - 1)
    return prev_start.isoformat(), prev_end.isoformat()


def _salon_levels(conn, date_from: Optional[str], date_to: Optional[str]) -> Dict[str, Dict]:
    """Уровень качества салона: доля набранных баллов от максимума"""
    where, params = _date_filter(date_from, date_to)
    rows = conn.execute(f'''
        SELECT salon, COUNT(*) AS cnt,
               SUM(total_score) AS got, SUM(max_score) AS possible
        FROM quality_scores
        WHERE {where}
        GROUP BY salon
    ''', params).fetchall()

    return {
        row['salon']: {
            'count': row['cnt'],
            # Процент, а не средний балл: у видов заказа разный максимум (14 и
            # 18), и усреднять сырые баллы между ними некорректно
            'percent': round((row['got'] or 0) / row['possible'] * 100, 1) if row['possible'] else 0.0,
        }
        for row in rows
    }


def _criteria_stats(conn, date_from: Optional[str], date_to: Optional[str]):
    """
    Баллы по критериям в разрезе салонов и по сети целиком.

    Returns:
        (по салонам {salon: {criterion_id: {...}}}, по сети {criterion_id: {...}})
    """
    where = []
    params: list = []
    if date_from:
        where.append('task_date >= ?')
        params.append(date_from)
    if date_to:
        where.append('task_date <= ?')
        params.append(date_to)
    clause = ('WHERE ' + ' AND '.join(where)) if where else ''

    rows = conn.execute(f'''
        SELECT salon, criterion_id, COUNT(*) AS cnt, SUM(score) AS got
        FROM quality_criteria_scores
        {clause}
        GROUP BY salon, criterion_id
    ''', params).fetchall()

    by_salon: Dict[str, Dict[int, Dict]] = {}
    network: Dict[int, Dict] = {}

    for row in rows:
        criterion_id = row['criterion_id']
        if criterion_id not in CRITERIA:
            continue

        cnt = row['cnt']
        got = row['got'] or 0
        possible = cnt * CRITERION_MAX

        by_salon.setdefault(row['salon'], {})[criterion_id] = {
            'count': cnt,
            'got': got,
            'percent': round(got / possible * 100, 1) if possible else 0.0,
        }

        agg = network.setdefault(criterion_id, {'count': 0, 'got': 0})
        agg['count'] += cnt
        agg['got'] += got

    for criterion_id, agg in network.items():
        possible = agg['count'] * CRITERION_MAX
        agg['percent'] = round(agg['got'] / possible * 100, 1) if possible else 0.0

    return by_salon, network


def _applicable_criteria(salon_criteria: Dict[int, Dict], sample: int) -> List[Dict]:
    """
    Отобрать критерии, по которым вообще можно судить о салоне.

    Критерий, заполненный у меньшинства оценок, к этим видам заказа неприменим
    (например «обработка цветка» для клубничного букета) — и редкие нули в таком
    поле означают «не оценивалось», а не провал.
    """
    if sample <= 0:
        return []

    result = []
    for criterion_id, stats in salon_criteria.items():
        if stats['count'] < sample * CRITERION_COVERAGE_MIN:
            continue
        result.append({
            'criterion_id': criterion_id,
            'name': CRITERIA[criterion_id],
            'count': stats['count'],
            'percent': stats['percent'],
            # Сколько баллов салон потерял на этом критерии — по нему сортируем
            # проблемы: важнее не самый низкий процент, а самая большая потеря
            'lost_points': stats['count'] * CRITERION_MAX - stats['got'],
        })
    return result


def _plural_ru(number: int, one: str, few: str, many: str) -> str:
    """Согласовать существительное с числом (1 оценка, 2 оценки, 5 оценок)"""
    if number % 10 == 1 and number % 100 != 11:
        return one
    if 2 <= number % 10 <= 4 and not 12 <= number % 100 <= 14:
        return few
    return many


def _num(value: float, digits: int = 1) -> str:
    """Число с запятой в качестве разделителя — как принято в остальном дашборде"""
    return f'{value:.{digits}f}'.replace('.', ',')


def _build_summary(level: Dict, delta, strengths, problems, sample: int, has_criteria: bool) -> str:
    """Собрать связное описание салона из посчитанных чисел"""
    parts = [f"Качество {_num(level['percent'])}% от максимума"]

    if delta is not None:
        if abs(delta) < 1:
            parts.append('на уровне прошлого периода')
        elif delta > 0:
            parts.append(f'это на {_num(abs(delta))} п.п. лучше прошлого периода')
        else:
            parts.append(f'это на {_num(abs(delta))} п.п. хуже прошлого периода')

    text = ', '.join(parts) + '.'

    if sample < ASSESSMENT_MIN_SAMPLE:
        return text + (
            f' Всего {sample} {_plural_ru(sample, "оценка", "оценки", "оценок")} — '
            f'для выводов маловато.'
        )

    # Витрина критериев может быть ещё не собрана (первый запуск после деплоя).
    # Молчать об этом нельзя: иначе вывод «заметных провалов нет» читался бы
    # как результат разбора, хотя разбирать было нечего
    if not has_criteria:
        return text + ' Разбор по критериям пока недоступен — данные обновляются.'

    # Сильные стороны делим на «лучше, чем у остальных» и «просто хорошо».
    # Без этого у всех салонов в сильных оказывались одни и те же критерии,
    # которые у сети под 100%, и разбор переставал что-либо различать.
    ahead = [s for s in strengths if (s['vs_network'] or 0) >= 2]
    if ahead:
        text += ' Сильнее сети: ' + ', '.join(
            f"{s['name']} (+{_num(s['vs_network'])} п.п.)" for s in ahead
        ) + '.'
    elif strengths:
        text += ' Уверенно держат: ' + ', '.join(s['name'] for s in strengths) + '.'

    if not problems:
        return text + ' Заметных провалов нет.'

    worst = problems[0]
    tail = ''
    if worst['kind'] == 'salon' and worst['vs_network'] is not None:
        tail = f", на {_num(abs(worst['vs_network']))} п.п. ниже сети"
    text += f" Слабее всего — {worst['name']} ({_num(worst['percent'])}%{tail})."

    others = problems[1:]
    if others:
        text += ' Ещё проседает: ' + ', '.join(p['name'] for p in others) + '.'

    # Отдельно проговариваем, что проблема не салонная: чинить надо процесс или
    # обучение, а не разговаривать с конкретной точкой
    common = [p['name'] for p in problems if p['kind'] == 'network']
    if common and len(common) == len(problems):
        text += ' Это общие для сети слабые места, а не особенность салона.'
    elif common:
        text += f' По сети «{common[0]}» проседает у всех.'

    return text


def get_salons_assessment(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None
) -> Dict:
    """
    Текстовый разбор качества по каждому салону за период.

    Считается правилами из уже посчитанных чисел — без обращения к внешним
    сервисам, поэтому отвечает мгновенно и одинаково на одних и тех же данных.
    Разбор по всем салонам делается одним проходом: открывать его салон за
    салоном означало бы N запросов вместо двух.
    """
    prev_from, prev_to = _previous_period(date_from, date_to)

    conn = _connect()
    try:
        levels = _salon_levels(conn, date_from, date_to)
        prev_levels = _salon_levels(conn, prev_from, prev_to) if prev_from else {}
        by_salon, network = _criteria_stats(conn, date_from, date_to)
    finally:
        conn.close()

    salons = []

    for salon, level in levels.items():
        sample = level['count']
        criteria = _applicable_criteria(by_salon.get(salon, {}), sample)

        for item in criteria:
            net = network.get(item['criterion_id'], {}).get('percent')
            item['network_percent'] = net
            item['vs_network'] = round(item['percent'] - net, 1) if net is not None else None

        # Сильные стороны: либо почти идеально, либо заметно лучше сети
        strengths = [
            c for c in criteria
            if c['percent'] >= STRONG_PERCENT
            or (c['vs_network'] is not None and c['vs_network'] >= NETWORK_GAP_PP)
        ]
        # Сначала то, чем салон выделяется на фоне сети, и только потом просто
        # высокий процент — иначе в «сильных» у всех подряд одно и то же
        strengths.sort(key=lambda c: (-(c['vs_network'] or 0), -c['percent']))

        # Проблемы: либо низкий процент сам по себе, либо отставание от сети.
        # Критерий, попавший в сильные стороны, проблемой быть не может: если
        # салон заметно лучше сети, ругать его не за что даже при невысоком
        # проценте — слаба сеть целиком, и это видно в общем блоке
        strong_ids = {c['criterion_id'] for c in strengths}
        problems = [
            c for c in criteria
            if c['criterion_id'] not in strong_ids
            and (
                c['percent'] < WEAK_PERCENT
                or (c['vs_network'] is not None and c['vs_network'] <= -NETWORK_GAP_PP)
            )
        ]
        problems.sort(key=lambda c: (-c['lost_points'], c['percent']))

        for item in problems:
            # Отделяем личную проблему салона от общей беды сети: во втором
            # случае разбираться надо не с салоном, а с процессом или обучением
            if item['vs_network'] is not None and item['vs_network'] <= -NETWORK_GAP_PP:
                item['kind'] = 'salon'
            elif item['percent'] < WEAK_PERCENT:
                item['kind'] = 'network'
            else:
                item['kind'] = 'salon'

        prev = prev_levels.get(salon)
        delta = None
        if prev and prev['count'] > 0:
            delta = round(level['percent'] - prev['percent'], 1)

        top_strengths = strengths[:3]
        top_problems = problems[:3]

        salons.append({
            'salon': salon,
            'count': sample,
            'percent': level['percent'],
            'delta': delta,
            'previous_percent': prev['percent'] if prev else None,
            'reliable': sample >= ASSESSMENT_MIN_SAMPLE,
            'strengths': top_strengths,
            'problems': top_problems,
            'summary': _build_summary(
                level, delta, top_strengths, top_problems, sample, bool(criteria)
            ),
        })

    # Худшие сверху: разбор нужен, чтобы браться за проблемы, а не любоваться
    # лидерами. Салоны без надёжной выборки уходят в конец
    salons.sort(key=lambda s: (not s['reliable'], s['percent']))

    return {
        'period': {'from': date_from, 'to': date_to},
        'previous_period': {'from': prev_from, 'to': prev_to},
        'network': {
            'percent': round(
                sum(l['percent'] * l['count'] for l in levels.values())
                / sum(l['count'] for l in levels.values()), 1
            ) if levels else 0.0,
            'criteria': [
                {
                    'criterion_id': cid,
                    'name': CRITERIA[cid],
                    'percent': stats['percent'],
                    'count': stats['count'],
                }
                for cid, stats in sorted(network.items(), key=lambda x: x[1]['percent'])
                if cid in CRITERIA
            ],
        },
        'salons': salons,
    }


def get_data_coverage(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    granularity: str = 'day'
) -> Dict:
    """Сколько оценок загружено по датам — видно дыры в истории"""
    where, params = _date_filter(date_from, date_to)
    period = "substr(task_date, 1, 7)" if granularity == 'month' else 'task_date'

    conn = _connect()
    try:
        rows = conn.execute(f'''
            SELECT {period} AS period, COUNT(*) AS cnt
            FROM quality_scores
            WHERE {where} AND task_date IS NOT NULL
            GROUP BY period
            ORDER BY period
        ''', params).fetchall()
    finally:
        conn.close()

    coverage = {row['period']: row['cnt'] for row in rows}

    return {
        'total_tasks': sum(coverage.values()),
        'date_range': {
            'from': min(coverage) if coverage else None,
            'to': max(coverage) if coverage else None,
        },
        'days_with_data': len(coverage),
        'coverage': coverage,
    }
