"""
Диагностика БД Pyrus для проверки качества данных
"""

import sqlite3
import json
import os
from datetime import datetime
from collections import defaultdict

DB_PATH = os.getenv('PYRUS_DB_PATH', 'data/pyrus.db')
FORM_ID = 1327961

# ID полей
FIELD_FLORIST = 3
FIELD_SALON = 10
FIELD_CATEGORY = 6
FIELD_TOTAL_SCORE = 18
FIELD_DATE = 1

def diagnose():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    print(f"=== ДИАГНОСТИКА БД: {DB_PATH} ===\n")

    # 1. Всего задач в БД
    cursor.execute('SELECT COUNT(*) as count FROM latest_tasks WHERE form_id = ?', (FORM_ID,))
    total_latest = cursor.fetchone()['count']
    print(f"1. Актуальных задач в latest_tasks: {total_latest}")

    cursor.execute('SELECT COUNT(*) as count FROM tasks WHERE form_id = ?', (FORM_ID,))
    total_all = cursor.fetchone()['count']
    print(f"2. Всех задач в tasks (с историей): {total_all}\n")

    # 3. Проверим даты
    cursor.execute('''
        SELECT MIN(last_modified) as min_date, MAX(last_modified) as max_date
        FROM latest_tasks WHERE form_id = ?
    ''', (FORM_ID,))
    date_range = cursor.fetchone()
    print(f"3. Диапазон дат:")
    print(f"   Мин: {date_range['min_date']}")
    print(f"   Макс: {date_range['max_date']}\n")

    # 4. Парсим данные и смотрим что там
    cursor.execute('SELECT raw_data FROM latest_tasks WHERE form_id = ?', (FORM_ID,))
    tasks_raw = cursor.fetchall()

    salons = set()
    florists = set()
    categories = defaultdict(int)
    dates = []
    parsed_count = 0
    failed_count = 0

    for row in tasks_raw:
        data = json.loads(row['raw_data'])

        florist = None
        salon = None
        category = None
        score = None
        date = None

        for field in data.get('fields', []):
            field_id = field.get('id')
            value = field.get('value')

            if field_id == FIELD_FLORIST and isinstance(value, dict):
                florist = value.get('choice_names', [''])[0] if value.get('choice_names') else None
            elif field_id == FIELD_SALON and isinstance(value, dict):
                salon = value.get('choice_names', [''])[0] if value.get('choice_names') else None
            elif field_id == FIELD_CATEGORY and isinstance(value, dict):
                category = value.get('choice_names', [''])[0] if value.get('choice_names') else None
            elif field_id == FIELD_TOTAL_SCORE:
                score = value
            elif field_id == FIELD_DATE:
                date = value

        if not date and 'create_date' in data:
            date = data['create_date'][:10]

        if florist:
            florists.add(florist)
        if salon:
            salons.add(salon)
        if category:
            categories[category] += 1
        if date:
            dates.append(date)

        if florist and salon and category and score is not None:
            parsed_count += 1
        else:
            failed_count += 1

    print(f"4. Результаты парсинга:")
    print(f"   Успешно распаршено: {parsed_count}")
    print(f"   Неполные данные: {failed_count}\n")

    print(f"5. Найденные салоны ({len(salons)}):")
    for s in sorted(salons):
        print(f"   - {s}")

    print(f"\n6. Найденные флористы ({len(florists)}):")
    for f in sorted(florists):
        print(f"   - {f}")

    print(f"\n7. Распределение по категориям:")
    for cat, count in sorted(categories.items(), key=lambda x: -x[1]):
        print(f"   {cat}: {count}")

    # Группировка по салонам
    salon_counts = defaultdict(int)
    cursor.execute('SELECT raw_data FROM latest_tasks WHERE form_id = ?', (FORM_ID,))
    for row in cursor.fetchall():
        data = json.loads(row['raw_data'])
        salon = None
        for field in data.get('fields', []):
            if field.get('id') == FIELD_SALON and isinstance(field.get('value'), dict):
                salon = field.get('value', {}).get('choice_names', [''])[0]
                break
        if salon:
            salon_counts[salon] += 1

    print(f"\n8. Распределение по салонам:")
    for salon, count in sorted(salon_counts.items(), key=lambda x: -x[1]):
        print(f"   {salon}: {count}")

    conn.close()

if __name__ == '__main__':
    diagnose()
