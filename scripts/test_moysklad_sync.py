"""
Проверка инкрементальной синхронизации МойСклад без обращения к API.

Запуск: python scripts/test_moysklad_sync.py

Покрывает то, что нельзя проверить глазами в браузере:
лок между воркерами, движение курсора, перезапись позиций заказа.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from moysklad.storage import MoySkladStorage  # noqa: E402

failures = []


def check(name, condition, detail=''):
    status = 'OK  ' if condition else 'FAIL'
    print(f'[{status}] {name}' + (f' — {detail}' if detail and not condition else ''))
    if not condition:
        failures.append(name)


def order(order_id, updated, positions):
    """Заказ в формате ответа МойСклад (только используемые поля)"""
    return {
        'id': order_id,
        'name': order_id,
        'created': '2026-08-20 10:00:00.000',
        'updated': updated,
        'sum': sum(p['price'] * p['quantity'] for p in positions),
        'state': {'id': 'st-1', 'name': 'Выполнен'},
        'positions': {'rows': [
            {
                'id': p['id'],
                'quantity': p['quantity'],
                'price': p['price'],
                'discount': 0,
                'assortment': {'id': p['assortment_id'], 'name': p['name'], 'productFolder': {}},
            }
            for p in positions
        ]},
    }


def main():
    db_path = os.path.join(tempfile.mkdtemp(), 'moysklad_test.db')
    storage = MoySkladStorage(db_path)

    # --- Лок между воркерами ---
    check('лок захватывается', storage.try_acquire_sync_lock('orders_sync', 600))
    check('второй воркер лок не получает', not storage.try_acquire_sync_lock('orders_sync', 600))
    storage.release_sync_lock('orders_sync')
    check('после release лок снова свободен', storage.try_acquire_sync_lock('orders_sync', 600))

    # Истёкший лок (умерший воркер) не блокирует навсегда
    storage.release_sync_lock('orders_sync')
    storage.try_acquire_sync_lock('dead_worker', -1)
    check('истёкший лок перехватывается', storage.try_acquire_sync_lock('dead_worker', 600))

    # --- Курсор ---
    check('пустой курсор — None', storage.get_sync_state('orders_updated_cursor') is None)
    storage.set_sync_state('orders_updated_cursor', '2026-08-21 12:00:00.000')
    check('курсор читается',
          storage.get_sync_state('orders_updated_cursor') == '2026-08-21 12:00:00.000')
    storage.set_sync_state('orders_updated_cursor', '2026-08-21 13:00:00.000')
    check('курсор перезаписывается',
          storage.get_sync_state('orders_updated_cursor') == '2026-08-21 13:00:00.000')

    # --- Позиции заказа переписываются целиком ---
    storage.save_sales_orders_batch([order('ord-1', '2026-08-21 10:00:00.000', [
        {'id': 'pos-1', 'assortment_id': 'a-1', 'name': 'Розы', 'quantity': 3, 'price': 50000},
        {'id': 'pos-2', 'assortment_id': 'a-2', 'name': 'Лента', 'quantity': 1, 'price': 10000},
    ])])

    with storage._get_connection() as conn:
        count = conn.execute('SELECT COUNT(*) c FROM order_positions WHERE order_id = ?', ('ord-1',)).fetchone()['c']
    check('позиции сохранены', count == 2, f'ожидалось 2, получено {count}')

    # Из заказа убрали ленту — она должна исчезнуть и локально
    storage.save_sales_orders_batch([order('ord-1', '2026-08-21 11:00:00.000', [
        {'id': 'pos-1', 'assortment_id': 'a-1', 'name': 'Розы', 'quantity': 3, 'price': 50000},
    ])])

    with storage._get_connection() as conn:
        rows = conn.execute(
            'SELECT id FROM order_positions WHERE order_id = ? ORDER BY id', ('ord-1',)
        ).fetchall()
    check('удалённая позиция исчезла', [r['id'] for r in rows] == ['pos-1'],
          f"осталось: {[r['id'] for r in rows]}")

    # --- ABC не учитывает удалённые позиции ---
    abc = storage.get_abc_analysis()
    revenue = sum(r['revenue'] for r in abc)
    check('выручка ABC без удалённой позиции', revenue == 1500.0, f'получено {revenue}')

    # --- Заказы и штуки в разрезе товара ---
    # Те же розы во втором заказе: заказов должно стать 2, штук — 3 + 5
    storage.save_sales_orders_batch([order('ord-2', '2026-08-21 12:00:00.000', [
        {'id': 'pos-3', 'assortment_id': 'a-1', 'name': 'Розы', 'quantity': 5, 'price': 50000},
    ])])

    roses = next(r for r in storage.get_abc_analysis() if r['assortment_id'] == 'a-1')
    check('количество заказов по товару', roses['orders_count'] == 2, f"получено {roses['orders_count']}")
    check('количество штук по товару', roses['quantity'] == 8, f"получено {roses['quantity']}")
    check('выручка не изменилась от новых колонок', roses['revenue'] == 4000.0, f"получено {roses['revenue']}")

    # --- Bootstrap курсора из уже загруженных заказов ---
    max_updated = storage.get_max_order_updated()
    check('максимальный updated берётся из raw_data',
          max_updated == '2026-08-21 12:00:00.000', f'получено {max_updated}')

    print()
    if failures:
        print(f'ПРОВАЛЕНО: {len(failures)} — ' + ', '.join(failures))
        return 1
    print('Все проверки пройдены')
    return 0


if __name__ == '__main__':
    sys.exit(main())
