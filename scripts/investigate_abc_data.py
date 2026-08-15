"""
Разведка данных МойСклад для ABC-анализа (Фаза 1 плана plans/2026-08-15-abc-analysis.md)

Проверяет:
- раздел "Товары МС" в дереве папок
- кастомные атрибуты customerorder / demand (ищем канал продаж)
- реальную структуру заказа/отгрузки с позициями

Результат пишется в scripts/_abc_investigation_output.txt (не в stdout —
консоль Windows коверкает кириллицу).

Использование:
    python scripts/investigate_abc_data.py
"""

import os
import sys
import json
import io

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
src_path = os.path.join(project_root, 'src')
if src_path not in sys.path:
    sys.path.insert(0, src_path)

from moysklad import get_client

OUT_PATH = os.path.join(project_root, 'scripts', '_abc_investigation_output.txt')
out = io.StringIO()


def section(title):
    out.write(f"\n{'=' * 70}\n{title}\n{'=' * 70}\n")


def p(text):
    out.write(str(text) + "\n")


def main():
    client = get_client()

    # 1. Папки — ищем "Товары МС"
    section("1. ДЕРЕВО ПАПОК ТОВАРОВ")
    folders_resp = client.get_folders(limit=1000)
    folders = folders_resp.get('rows', []) if folders_resp else []
    p(f"Всего папок: {len(folders)}")
    for f in folders:
        parent = f.get('productFolder', {})
        parent_href = parent.get('meta', {}).get('href', '') if parent else ''
        parent_id = parent_href.rsplit('/', 1)[-1] if parent_href else None
        marker = "  <-- ПОХОЖЕ НА ЦЕЛЕВОЙ" if 'мс' in f.get('name', '').lower() else ""
        p(f"  id={f.get('id')} parent={parent_id} name={f.get('name')!r}{marker}")

    # 2. Кастомные атрибуты customerorder и demand
    for entity in ('customerorder', 'demand'):
        section(f"2. КАСТОМНЫЕ АТРИБУТЫ: {entity}")
        try:
            meta = client.get(f'/entity/{entity}/metadata/attributes')
            rows = meta.get('rows', []) if meta else []
            if not rows:
                p("  (нет кастомных атрибутов или запрос не удался)")
            for a in rows:
                p(f"  id={a.get('id')} name={a.get('name')!r} type={a.get('type')}")
        except Exception as e:
            p(f"  Ошибка: {e}")

    # 3. Метаданные статусов (states) customerorder и demand
    for entity in ('customerorder', 'demand'):
        section(f"3. СТАТУСЫ (states): {entity}")
        try:
            meta = client.get(f'/entity/{entity}/metadata')
            states = meta.get('states', []) if meta else []
            for s in states:
                p(f"  id={s.get('id')} name={s.get('name')!r}")
        except Exception as e:
            p(f"  Ошибка: {e}")

    # 4. Пример реального заказа покупателя с позициями и атрибутами
    section("4. ПРИМЕР ЗАКАЗА ПОКУПАТЕЛЯ (customerorder), raw JSON")
    try:
        orders_resp = client.get_sales_orders(limit=3, expand='positions,positions.assortment,state')
        orders = orders_resp.get('rows', []) if orders_resp else []
        p(f"Получено заказов: {len(orders)}")
        for sample in orders:
            p(json.dumps(sample, ensure_ascii=False, indent=2))
            p("-" * 40)
    except Exception as e:
        p(f"Ошибка: {e}")

    # 5. Пример отгрузки (demand)
    section("5. ПРИМЕР ОТГРУЗКИ (demand), raw JSON")
    try:
        demands_resp = client.get_demands(limit=3, expand='positions,positions.assortment,state')
        demands = demands_resp.get('rows', []) if demands_resp else []
        p(f"Получено отгрузок: {len(demands)}")
        for sample in demands:
            p(json.dumps(sample, ensure_ascii=False, indent=2))
            p("-" * 40)
    except Exception as e:
        p(f"Ошибка: {e}")

    # 6. Справочник каналов продаж
    section("6. СПРАВОЧНИК КАНАЛОВ ПРОДАЖ (saleschannel)")
    try:
        channels_resp = client.get('/entity/saleschannel', params={'limit': 1000})
        channels = channels_resp.get('rows', []) if channels_resp else []
        p(f"Всего каналов: {len(channels)}")
        for c in channels:
            p(f"  id={c.get('id')} name={c.get('name')!r} type={c.get('type')} archived={c.get('archived')}")
    except Exception as e:
        p(f"Ошибка: {e}")

    with open(OUT_PATH, 'w', encoding='utf-8') as f:
        f.write(out.getvalue())
    print(f"Результат записан в {OUT_PATH}")


if __name__ == '__main__':
    main()
