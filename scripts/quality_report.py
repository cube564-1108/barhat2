"""
Отчет по оценке качества сборки букетов
Форма Pyrus 1327961
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


def get_all_tasks(form_id: int = 1327961) -> List[Dict]:
    """
    Получить все задачи из БД (актуальные версии)

    Читает из таблицы latest_tasks — актуальные данные без дубликатов
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Берём актуальные задачи из latest_tasks
    cursor.execute('''
        SELECT raw_data
        FROM latest_tasks
        WHERE form_id = ?
        ORDER BY task_id
    ''', (form_id,))

    tasks = []
    for row in cursor.fetchall():
        data = json.loads(row['raw_data'])
        tasks.append(data)

    conn.close()
    return tasks


def extract_task_data(task: Dict) -> Optional[Dict]:
    """
    Извлечь данные из задачи

    Returns:
        Dict с полями: florist, salon, category, order_id, total_score, date
    """
    florist = None
    salon = None
    category = None
    order_id = None
    total_score = None
    date = None

    for field in task.get('fields', []):
        field_id = field.get('id')
        value = field.get('value')

        if field_id == FIELD_FLORIST and isinstance(value, dict):  # Флорист
            florist = value.get('choice_names', [''])[0] if value.get('choice_names') else None
        elif field_id == FIELD_SALON and isinstance(value, dict):  # Салон
            salon = value.get('choice_names', [''])[0] if value.get('choice_names') else None
        elif field_id == FIELD_CATEGORY and isinstance(value, dict):  # Тип букета
            category = value.get('choice_names', [''])[0] if value.get('choice_names') else None
        elif field_id == FIELD_ORDER_ID:  # Номер заказа
            order_id = value
        elif field_id == FIELD_TOTAL_SCORE:  # Итоговая оценка
            total_score = value
        elif field_id == FIELD_DATE:  # Дата создания
            date = value

    # Альтернативно берем дату из create_date
    if not date and 'create_date' in task:
        date = task['create_date'][:10]  # YYYY-MM-DD

    if not florist or not salon or not category or total_score is None:
        return None

    # Пробуем преобразовать total_score в число
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
    """Определить группу категории (cat14 или cat18)"""
    if category in CATEGORIES_14:
        return 'cat14'
    elif category in CATEGORIES_18:
        return 'cat18'
    # По умолчанию считаем как 14
    return 'cat14'


def get_max_score(category: str) -> int:
    """Получить максимальный балл для категории"""
    if category in CATEGORIES_14:
        return 14
    elif category in CATEGORIES_18:
        return 18
    return 14  # По умолчанию


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


def generate_report(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None
) -> Dict:
    """
    Сгенерировать отчет

    Args:
        date_from: Начало периода (YYYY-MM-DD)
        date_to: Конец периода (YYYY-MM-DD)

    Returns:
        Dict с данными отчета
    """
    tasks = get_all_tasks()
    print(f"DEBUG generate_report: get_all_tasks() returned {len(tasks)} tasks")

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
    florist_by_salon = defaultdict(lambda: defaultdict(list))  # salon -> florist -> scores
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

        # Статистика по салонам
        if salon not in salon_stats:
            salon_stats[salon] = {
                'cat14': {'scores': [], 'count': 0, 'max_score': 14},
                'cat18': {'scores': [], 'count': 0, 'max_score': 18},
                'total': {'scores': [], 'count': 0}
            }

        salon_stats[salon][category_group]['scores'].append(score)
        salon_stats[salon][category_group]['count'] += 1
        salon_stats[salon]['total']['scores'].append(score)
        salon_stats[salon]['total']['count'] += 1

        # Статистика по флористам (общая)
        if florist not in florist_stats:
            florist_stats[florist] = {
                'cat14': {'scores': [], 'count': 0, 'max_score': 14},
                'cat18': {'scores': [], 'count': 0, 'max_score': 18},
                'total': {'scores': [], 'count': 0},
                'salon': salon  # Привязка к салону
            }

        florist_stats[florist][category_group]['scores'].append(score)
        florist_stats[florist][category_group]['count'] += 1
        florist_stats[florist]['total']['scores'].append(score)
        florist_stats[florist]['total']['count'] += 1

        # Флористы по салонам
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

    # Статистика по салонам
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
            'florists': {}  # Флористы салона
        }

        # Добавляем флористов салона
        for florist, scores in florist_by_salon[salon].items():
            f_avg = calculate_avg_score(scores)
            f_stat = florist_stats[florist]
            result['salons'][salon]['florists'][florist] = {
                'avg_score': f_avg,
                'count': len(scores),
                'cat14_count': f_stat['cat14']['count'],
                'cat18_count': f_stat['cat18']['count']
            }

    # Общая статистика по флористам
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
    """
    Получить историю по месяцам

    Args:
        months: Количество месяцев для истории

    Returns:
        Dict с данными по месяцам
    """
    tasks = get_all_tasks()

    # Группируем по месяцам и салонам
    monthly_data = defaultdict(lambda: defaultdict(lambda: {'cat14': [], 'cat18': []}))

    for task in tasks:
        task_data = extract_task_data(task)
        if not task_data or not task_data['date']:
            continue

        date = task_data['date'][:7]  # YYYY-MM
        salon = task_data['salon']
        category = task_data['category']
        score = task_data['total_score']

        category_group = get_category_group(category)
        monthly_data[date][salon][category_group].append(score)

    # Формируем результат за последние N месяцев
    result = {}
    current_month = datetime.now().strftime('%Y-%m')

    for i in range(months):
        # Вычисляем месяц в обратном порядке
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
    """
    Получить историю по конкретному салону

    Args:
        salon_name: Название салона
        months: Количество месяцев для истории

    Returns:
        Dict с данными по месяцам для салона
    """
    tasks = get_all_tasks()

    # Группируем по месяцам только для указанного салона
    monthly_data = defaultdict(lambda: {'cat14': [], 'cat18': [], 'total': []})

    for task in tasks:
        task_data = extract_task_data(task)
        if not task_data or not task_data['date']:
            continue

        if task_data['salon'] != salon_name:
            continue

        date = task_data['date'][:7]  # YYYY-MM
        category = task_data['category']
        score = task_data['total_score']

        category_group = get_category_group(category)
        monthly_data[date][category_group].append(score)
        monthly_data[date]['total'].append(score)

    # Формируем результат за последние N месяцев
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


def print_report(report: Dict):
    """Вывести отчет в консоль"""
    print("=" * 70)
    print("ОТЧЕТ ПО КАЧЕСТВУ СБОРКИ БУКЕТОВ")
    print("=" * 70)

    period = report['period']
    print(f"\nПериод: {period['from'] or 'начала'} - {period['to'] or 'сейчас'}")
    print(f"Всего задач: {report['total_tasks']}")
    print(f"Общий средний балл: {report['overall_avg']}")

    print("\nРаспределение по категориям:")
    for cat, count in report['category_distribution'].items():
        max_score = get_max_score(cat)
        print(f"  {cat} (макс. {max_score}): {count}")

    print("\n--- По салонам ---")
    for salon, stats in sorted(report['salons'].items()):
        print(f"\n{salon}:")
        print(f"  Категории 14 баллов: средний {stats['cat14']['avg_score']:.1f} ({stats['cat14']['percent']}%) - {stats['cat14']['count']} оценок")
        print(f"  Категории 18 баллов: средний {stats['cat18']['avg_score']:.1f} ({stats['cat18']['percent']}%) - {stats['cat18']['count']} оценок")
        print(f"  Всего: средний {stats['total']['avg_score']:.1f} - {stats['total']['count']} оценок")

        # Флористы салона
        if 'florists' in stats and stats['florists']:
            print(f"  Флористы:")
            for florist, f_stats in sorted(stats['florists'].items()):
                print(f"    {florist}: средний {f_stats['avg_score']:.1f} - {f_stats['count']} оценок")

    print("\n--- По флористам (общий рейтинг) ---")
    # Сортируем флористов по среднему баллу
    sorted_florists = sorted(
        report['florists'].items(),
        key=lambda x: x[1]['total']['avg_score'],
        reverse=True
    )
    for florist, stats in sorted_florists:
        print(f"\n{florist} ({stats['salon']}):")
        print(f"  Категории 14 баллов: средний {stats['cat14']['avg_score']:.1f} ({stats['cat14']['percent']}%) - {stats['cat14']['count']} оценок")
        print(f"  Категории 18 баллов: средний {stats['cat18']['avg_score']:.1f} ({stats['cat18']['percent']}%) - {stats['cat18']['count']} оценок")
        print(f"  Всего: средний {stats['total']['avg_score']:.1f} - {stats['total']['count']} оценок")

    print("\n" + "=" * 70)


def print_monthly_history(history: Dict):
    """Вывести историю по месяцам"""
    print("\n" + "=" * 70)
    print("ДИНАМИКА ЗА ПОСЛЕДНИЕ МЕСЯЦЫ")
    print("=" * 70)

    for month in sorted(history.keys(), reverse=True):
        print(f"\n{month}:")
        for salon, stats in sorted(history[month].items()):
            cat14 = f"{stats['cat14_avg']:.1f}%" if stats['cat14_count'] > 0 else "-"
            cat18 = f"{stats['cat18_avg']:.1f}%" if stats['cat18_count'] > 0 else "-"
            print(f"  {salon}:")
            print(f"    14 баллов: {stats['cat14_avg']:.1f} ({stats['cat14_percent']}%) - {stats['cat14_count']} шт")
            print(f"    18 баллов: {stats['cat18_avg']:.1f} ({stats['cat18_percent']}%) - {stats['cat18_count']} шт")


if __name__ == '__main__':
    import sys

    # Параметры периода
    date_from = sys.argv[1] if len(sys.argv) > 1 else None
    date_to = sys.argv[2] if len(sys.argv) > 2 else None

    print(f"\nГенерация отчета...")
    print(f"Период: {date_from or 'все время'} - {date_to or 'сейчас'}\n")

    report = generate_report(date_from, date_to)
    print_report(report)

    print("\n" + "=" * 70)
    print("ДИНАМИКА ЗА 6 МЕСЯЦЕВ")
    print("=" * 70)

    history = get_monthly_history(6)
    print_monthly_history(history)

    print("\n" + "=" * 70)
    print("\nДля выбора периода:")
    print("  python scripts/quality_report.py YYYY-MM-DD YYYY-MM-DD")
    print("\nПример:")
    print("  python scripts/quality_report.py 2026-07-01 2026-07-31")
