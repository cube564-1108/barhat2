"""
Отчет по оценке качества сборки букетов v2
ГАРАНТИРОВАННО НОВАЯ ВЕРСИЯ (обход кэша)
"""

import sqlite3
import json
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from collections import defaultdict
import os

# Путь к БД
DB_PATH = os.getenv('PYRUS_DB_PATH', 'data/pyrus.db')

# Категории с максимальным баллом (по данным пользователя)
CATEGORIES_14 = [
    'Клубничный букет',
    'Цветочный букет',
    'Коробочка с клубникой или бананами',
    'Цветочный бокс',
    'Клубничный бокс'
]

CATEGORIES_18 = [
    'Клубнично-цветочный букет',
    'Коробочка+цветочный букет',
    'Цветочно-клубничный бокс'
]

# ID полей в форме
FIELD_FLORIST = 3   # Флорист
FIELD_SALON = 10    # Салон
FIELD_CATEGORY = 6  # Тип букета / Вид заказа
FIELD_ORDER_ID = 4  # Номер заказа
FIELD_TOTAL_SCORE = 18  # Итоговая оценка
FIELD_DATE = 1      # Дата создания


def get_all_tasks(
    form_id: int = 1327961,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None
) -> List[Dict]:
    """
    Получить задачи из таблицы tasks (исторические данные)

    Использует последний snapshot для каждого task_id.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Получаем последние версии задач (по task_id, берем с максимальным snapshot_at)
    cursor.execute('''
        SELECT raw_data
        FROM tasks t1
        WHERE form_id = ?
          AND snapshot_at = (
            SELECT MAX(snapshot_at)
            FROM tasks t2
            WHERE t2.form_id = t1.form_id AND t2.task_id = t1.task_id
          )
        ORDER BY task_id
    ''', (form_id,))

    tasks = []
    for row in cursor.fetchall():
        data = json.loads(row['raw_data'])
        tasks.append(data)

    conn.close()
    return tasks


def extract_task_data(task: Dict) -> Optional[Dict]:
    """Извлечь данные из задачи"""
    florist = None
    salon = None
    category = None
    order_id = None
    total_score = None
    date = None

    for field in task.get('fields', []):
        field_id = field.get('id')
        value = field.get('value')

        if field_id == FIELD_FLORIST and isinstance(value, dict):
            florist = value.get('choice_names', [''])[0] if value.get('choice_names') else None
        elif field_id == FIELD_SALON and isinstance(value, dict):
            salon = value.get('choice_names', [''])[0] if value.get('choice_names') else None
        elif field_id == FIELD_CATEGORY and isinstance(value, dict):
            category = value.get('choice_names', [''])[0] if value.get('choice_names') else None
        elif field_id == FIELD_ORDER_ID:
            order_id = value
        elif field_id == FIELD_TOTAL_SCORE:
            total_score = value
        elif field_id == FIELD_DATE:
            date = value

    if not date and 'create_date' in task:
        date = task['create_date'][:10]

    if not florist or not salon or not category or total_score is None:
        return None

    try:
        total_score = int(total_score)
    except (ValueError, TypeError):
        return None

    return {
        'florist': florist,
        'salon': salon,
        'category': category,
        'order_id': order_id,
        'total_score': total_score,
        'date': date
    }


def get_category_group(category: str) -> str:
    """Определить группу категории"""
    if category in CATEGORIES_14:
        return 'cat14'
    elif category in CATEGORIES_18:
        return 'cat18'
    return 'cat14'


def calculate_avg_score(scores: List[int]) -> float:
    """Рассчитать средний балл"""
    if not scores:
        return 0.0
    return round(sum(scores) / len(scores), 2)


def calculate_percent(avg_score: float, max_score: int) -> float:
    """Рассчитать процент от максимального балла"""
    if max_score == 0:
        return 0.0
    return round(avg_score / max_score * 100, 1)


def generate_report(date_from: Optional[str] = None, date_to: Optional[str] = None) -> Dict:
    """Сгенерировать отчет"""
    tasks = get_all_tasks()

    # Фильтр по дате
    if date_from or date_to:
        filtered = []
        for task in tasks:
            task_data = extract_task_data(task)
            if not task_data or not task_data['date']:
                continue
            task_date = task_data['date']
            if date_from and task_date < date_from:
                continue
            if date_to and task_date > date_to:
                continue
            filtered.append(task)
        tasks = filtered

    # Агрегируем данные
    salon_stats = {}
    florist_stats = {}
    florist_by_salon = defaultdict(lambda: defaultdict(list))
    category_distribution = defaultdict(int)
    all_scores = []

    for task in tasks:
        task_data = extract_task_data(task)
        if not task_data:
            continue

        florist = task_data['florist']
        salon = task_data['salon']
        category = task_data['category']
        score = task_data['total_score']
        category_group = get_category_group(category)

        if salon not in salon_stats:
            salon_stats[salon] = {
                'cat14': {'scores': [], 'count': 0},
                'cat18': {'scores': [], 'count': 0},
                'total': {'scores': [], 'count': 0}
            }

        salon_stats[salon][category_group]['scores'].append(score)
        salon_stats[salon][category_group]['count'] += 1
        salon_stats[salon]['total']['scores'].append(score)
        salon_stats[salon]['total']['count'] += 1

        if florist not in florist_stats:
            florist_stats[florist] = {
                'cat14': {'scores': [], 'count': 0},
                'cat18': {'scores': [], 'count': 0},
                'total': {'scores': [], 'count': 0},
                'salon': salon
            }

        florist_stats[florist][category_group]['scores'].append(score)
        florist_stats[florist][category_group]['count'] += 1
        florist_stats[florist]['total']['scores'].append(score)
        florist_stats[florist]['total']['count'] += 1

        florist_by_salon[salon][florist].append(score)
        category_distribution[category] += 1
        all_scores.append(score)

    # Формируем результат
    result = {
        'period': {'from': date_from, 'to': date_to},
        'total_tasks': len(tasks),
        'category_distribution': dict(category_distribution),
        'overall_avg': calculate_avg_score(all_scores),
        'salons': {},
        'florists': {}
    }

    for salon, stats in salon_stats.items():
        cat14_avg = calculate_avg_score(stats['cat14']['scores'])
        cat18_avg = calculate_avg_score(stats['cat18']['scores'])
        total_avg = calculate_avg_score(stats['total']['scores'])

        result['salons'][salon] = {
            'cat14': {
                'avg_score': cat14_avg,
                'percent': calculate_percent(cat14_avg, 14),
                'count': stats['cat14']['count'],
                'max_score': 14
            },
            'cat18': {
                'avg_score': cat18_avg,
                'percent': calculate_percent(cat18_avg, 18),
                'count': stats['cat18']['count'],
                'max_score': 18
            },
            'total': {
                'avg_score': total_avg,
                'count': stats['total']['count']
            },
            'florists': {}
        }

        for florist, scores in florist_by_salon[salon].items():
            f_avg = calculate_avg_score(scores)
            f_stat = florist_stats[florist]
            result['salons'][salon]['florists'][florist] = {
                'avg_score': f_avg,
                'count': len(scores),
                'cat14_count': f_stat['cat14']['count'],
                'cat18_count': f_stat['cat18']['count']
            }

    for florist, stats in florist_stats.items():
        cat14_avg = calculate_avg_score(stats['cat14']['scores'])
        cat18_avg = calculate_avg_score(stats['cat18']['scores'])
        total_avg = calculate_avg_score(stats['total']['scores'])

        result['florists'][florist] = {
            'salon': stats['salon'],
            'cat14': {
                'avg_score': cat14_avg,
                'percent': calculate_percent(cat14_avg, 14),
                'count': stats['cat14']['count'],
                'max_score': 14
            },
            'cat18': {
                'avg_score': cat18_avg,
                'percent': calculate_percent(cat18_avg, 18),
                'count': stats['cat18']['count'],
                'max_score': 18
            },
            'total': {
                'avg_score': total_avg,
                'count': stats['total']['count']
            }
        }

    return result


def get_monthly_history(months: int = 6) -> Dict:
    """Получить историю по месяцам"""
    tasks = get_all_tasks()

    monthly_data = defaultdict(lambda: defaultdict(lambda: {'cat14': [], 'cat18': []}))

    for task in tasks:
        task_data = extract_task_data(task)
        if not task_data or not task_data['date']:
            continue

        date = task_data['date'][:7]
        salon = task_data['salon']
        category = task_data['category']
        score = task_data['total_score']

        category_group = get_category_group(category)
        monthly_data[date][salon][category_group].append(score)

    result = {}
    for i in range(months):
        year_month = datetime.now().replace(day=1) - timedelta(days=30*i)
        month_key = year_month.strftime('%Y-%m')

        if month_key in monthly_data:
            result[month_key] = {}
            for salon, scores in monthly_data[month_key].items():
                cat14_avg = calculate_avg_score(scores['cat14'])
                cat18_avg = calculate_avg_score(scores['cat18'])

                result[month_key][salon] = {
                    'cat14_avg': cat14_avg,
                    'cat14_percent': calculate_percent(cat14_avg, 14),
                    'cat14_count': len(scores['cat14']),
                    'cat18_avg': cat18_avg,
                    'cat18_percent': calculate_percent(cat18_avg, 18),
                    'cat18_count': len(scores['cat18'])
                }

    return result


def get_salon_history(salon_name: str, months: int = 6) -> Dict:
    """Получить историю по конкретному салону"""
    tasks = get_all_tasks()

    monthly_data = defaultdict(lambda: {'cat14': [], 'cat18': [], 'total': []})

    for task in tasks:
        task_data = extract_task_data(task)
        if not task_data or not task_data['date']:
            continue

        if task_data['salon'] != salon_name:
            continue

        date = task_data['date'][:7]
        category = task_data['category']
        score = task_data['total_score']

        category_group = get_category_group(category)
        monthly_data[date][category_group].append(score)
        monthly_data[date]['total'].append(score)

    result = {}
    for i in range(months):
        year_month = datetime.now().replace(day=1) - timedelta(days=30*i)
        month_key = year_month.strftime('%Y-%m')

        if month_key in monthly_data:
            data = monthly_data[month_key]
            cat14_avg = calculate_avg_score(data['cat14'])
            cat18_avg = calculate_avg_score(data['cat18'])
            total_avg = calculate_avg_score(data['total'])

            total_count = len(data['cat14']) + len(data['cat18'])

            result[month_key] = {
                'cat14_avg': cat14_avg,
                'cat14_percent': calculate_percent(cat14_avg, 14),
                'cat14_count': len(data['cat14']),
                'cat18_avg': cat18_avg,
                'cat18_percent': calculate_percent(cat18_avg, 18),
                'cat18_count': len(data['cat18']),
                'total_avg': total_avg,
                'total_count': total_count
            }

    return result


def get_salon_order_types(salon_name: str, date_from: Optional[str] = None, date_to: Optional[str] = None) -> Dict:
    """
    Получить разбивку по видам заказа для конкретного салона

    Args:
        salon_name: Название салона
        date_from: Начальная дата (YYYY-MM-DD)
        date_to: Конечная дата (YYYY-MM-DD)

    Returns:
        Словарь с данными по каждому виду заказа
    """
    tasks = get_all_tasks()

    # Все возможные виды заказа с их максимальным баллом
    all_order_types = {
        'Клубничный букет': 14,
        'Цветочный букет': 14,
        'Коробочка с клубникой или бананами': 14,
        'Цветочный бокс': 14,
        'Клубничный бокс': 14,
        'Клубнично-цветочный букет': 18,
        'Коробочка+цветочный букет': 18,
        'Цветочно-клубничный бокс': 18
    }

    # Собираем данные по видам заказа
    order_type_data = defaultdict(lambda: {'scores': [], 'count': 0})

    for task in tasks:
        task_data = extract_task_data(task)
        if not task_data or not task_data['date']:
            continue

        # Фильтр по салону
        if task_data['salon'] != salon_name:
            continue

        # Фильтр по датам
        task_date = task_data['date']
        if date_from and task_date < date_from:
            continue
        if date_to and task_date > date_to:
            continue

        category = task_data['category']
        score = task_data['total_score']

        order_type_data[category]['scores'].append(score)
        order_type_data[category]['count'] += 1

    # Формируем результат
    result = []

    for order_type, max_score in all_order_types.items():
        data = order_type_data.get(order_type, {'scores': [], 'count': 0})
        scores = data['scores']
        count = data['count']

        if count > 0:
            avg_score = calculate_avg_score(scores)
        else:
            avg_score = 0.0

        result.append({
            'order_type': order_type,
            'avg_score': avg_score,
            'max_score': max_score,
            'count': count
        })

    # Сортируем по среднему баллу (убывание), но с данными - выше
    result.sort(key=lambda x: (x['count'] == 0, -x['avg_score']))

    return {
        'salon': salon_name,
        'period': {'from': date_from, 'to': date_to},
        'order_types': result
    }


def get_salon_florists(salon_name: str, date_from: Optional[str] = None, date_to: Optional[str] = None) -> Dict:
    """
    Получить статистику по флористам конкретного салона

    Args:
        salon_name: Название салона
        date_from: Начальная дата (YYYY-MM-DD)
        date_to: Конечная дата (YYYY-MM-DD)

    Returns:
        Словарь с данными по каждому флористу салона
    """
    tasks = get_all_tasks()

    # Собираем данные по флористам: {florist: {cat14: [], cat18: []}}
    florist_data = defaultdict(lambda: {'cat14': [], 'cat18': []})

    for task in tasks:
        task_data = extract_task_data(task)
        if not task_data or not task_data['date']:
            continue

        # Фильтр по салону
        if task_data['salon'] != salon_name:
            continue

        # Фильтр по датам
        task_date = task_data['date']
        if date_from and task_date < date_from:
            continue
        if date_to and task_date > date_to:
            continue

        florist = task_data['florist']
        score = task_data['total_score']
        category_group = get_category_group(task_data['category'])

        florist_data[florist][category_group].append(score)

    # Формируем результат
    florists = []

    for florist, data in florist_data.items():
        cat14_scores = data['cat14']
        cat18_scores = data['cat18']

        florists.append({
            'florist': florist,
            'cat14': {
                'avg_score': calculate_avg_score(cat14_scores) if cat14_scores else 0.0,
                'count': len(cat14_scores)
            },
            'cat18': {
                'avg_score': calculate_avg_score(cat18_scores) if cat18_scores else 0.0,
                'count': len(cat18_scores)
            }
        })

    # Сортируем по количеству оценок (убывание)
    florists.sort(key=lambda x: -(x['cat14']['count'] + x['cat18']['count']))

    return {
        'salon': salon_name,
        'period': {'from': date_from, 'to': date_to},
        'florists': florists
    }
