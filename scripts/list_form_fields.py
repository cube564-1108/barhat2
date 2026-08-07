"""
Вывод всех полей формы Pyrus с примерами значений
Полезно для поиска ID поля по названию
"""

import sqlite3
import json
import os
import sys

DB_PATH = os.getenv('PYRUS_DB_PATH', 'data/pyrus.db')


def list_form_fields(form_id: int, limit: int = 5):
    """Вывести все поля формы с примерами значений"""

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Получаем задачи
    cursor.execute(
        'SELECT raw_data FROM tasks WHERE form_id = ? LIMIT ?',
        (form_id, limit)
    )
    rows = cursor.fetchall()

    if not rows:
        print(f'Задачи формы {form_id} не найдены')
        conn.close()
        return

    # Собираем все уникальные поля
    fields_info = {}

    for row in rows:
        task = json.loads(row[0])

        for field in task.get('fields', []):
            field_id = field.get('id')
            field_type = field.get('type')
            field_name = field.get('name')
            value = field.get('value')

            if field_id not in fields_info:
                fields_info[field_id] = {
                    'id': field_id,
                    'type': field_type,
                    'name': field_name,
                    'examples': []
                }

            # Добавляем пример значения, если его еще нет
            value_str = str(value)[:100] if value is not None else 'None'
            if value_str not in fields_info[field_id]['examples']:
                fields_info[field_id]['examples'].append(value_str)

    conn.close()

    # Вывод
    print(f'Поля формы {form_id}:')
    print('=' * 80)

    for field_id in sorted(fields_info.keys()):
        info = fields_info[field_id]
        print(f'\nID: {info["id"]:3d} | Тип: {info["type"]:20s} | {info["name"]}')
        print('  Примеры значений:')
        for ex in info['examples'][:3]:  # До 3 примеров
            print(f'    - {ex}')


def search_field(form_id: int, search_term: str):
    """Поиск поля по названию"""

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        'SELECT raw_data FROM tasks WHERE form_id = ? LIMIT 1',
        (form_id,)
    )
    row = cursor.fetchone()

    if not row:
        print(f'Форма {form_id} не найдена')
        conn.close()
        return

    task = json.loads(row[0])

    print(f'Поиск поля "{search_term}" в форме {form_id}:')
    print('=' * 80)

    found = False
    for field in task.get('fields', []):
        field_name = field.get('name', '').lower()
        if search_term.lower() in field_name:
            found = True
            print(f'\n[+] ID: {field.get("id"):3d} | Тип: {field.get("type"):20s}')
            print(f'  Название: {field.get("name")}')
            print(f'  Значение: {str(field.get("value"))[:100]}')

    if not found:
        print(f'\nПоле с названием "{search_term}" не найдено')
        print('\nВсе поля формы:')
        for field in task.get('fields', []):
            print(f'  ID {field.get("id"):3d}: {field.get("name")}')

    conn.close()


if __name__ == '__main__':
    form_id = int(sys.argv[1]) if len(sys.argv) > 1 else 1327961

    if len(sys.argv) > 2:
        # Поиск по названию
        search_term = sys.argv[2]
        search_field(form_id, search_term)
    else:
        # Вывод всех полей
        list_form_fields(form_id)
