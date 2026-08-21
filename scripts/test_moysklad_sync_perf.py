"""
Проверка изменений в синхронизации МойСклад (батч-запись, прогресс, кэш storage).

Запуск: python scripts/test_moysklad_sync_perf.py
Работает на временной БД, ничего рабочего не трогает.
"""

import os
import sys
import json
import time
import sqlite3
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from moysklad.storage import get_storage, MoySkladStorage


def make_order(idx: int, positions: int = 2) -> dict:
    """Заказ в формате ответа МойСклад (как приходит с expand=positions,...)"""
    return {
        'id': f'order-{idx}',
        'name': f'{idx}',
        'created': '2026-07-15 10:00:00.000',
        'moment': '2026-07-15 10:00:00.000',
        'sum': 100000,
        'rate': {'currency': {'meta': {'href': 'x/currency/rub'}}},
        'state': {'id': 'st-1', 'name': 'Выполнен',
                  'meta': {'href': 'https://api.moysklad.ru/x/state/st-1'}},
        'salesChannel': {'meta': {'href': 'https://api.moysklad.ru/x/saleschannel/ch-1'}},
        'positions': {'rows': [
            {
                'id': f'pos-{idx}-{p}',
                'quantity': 2,
                'price': 50000,
                'discount': 0,
                'assortment': {
                    'id': f'prod-{p}',
                    'name': f'Товар {p} <script>alert(1)</script>',
                    'productFolder': {'meta': {'href': 'https://api.moysklad.ru/x/productfolder/f-1'}},
                },
            }
            for p in range(positions)
        ]},
    }


def main() -> int:
    tmp_dir = tempfile.mkdtemp(prefix='ms_test_')
    db_path = os.path.join(tmp_dir, 'moysklad.db')
    failures = []

    def check(name, condition, detail=''):
        status = 'OK  ' if condition else 'FAIL'
        print(f'  [{status}] {name}' + (f' — {detail}' if detail else ''))
        if not condition:
            failures.append(name)

    print('1. Кэш get_storage (один инстанс на путь)')
    s1 = get_storage(db_path)
    s2 = get_storage(db_path)
    check('get_storage возвращает тот же объект', s1 is s2)
    check(':memory: не кэшируется', get_storage(':memory:') is not get_storage(':memory:'))

    print('\n2. Миграция progress_at и WAL')
    with sqlite3.connect(db_path) as conn:
        cols = {r[1] for r in conn.execute('PRAGMA table_info(sync_log)')}
        journal = conn.execute('PRAGMA journal_mode').fetchone()[0]
    check('колонка sync_log.progress_at создана', 'progress_at' in cols)
    check('journal_mode = wal', journal == 'wal', journal)

    print('\n3. Батч-запись заказов')
    storage = s1
    orders = [make_order(i) for i in range(50)]
    saved = storage.save_sales_orders_batch(orders)
    check('сохранены все 50 заказов', saved == 50, f'saved={saved}')

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        orders_count = conn.execute('SELECT COUNT(*) c FROM sales_orders').fetchone()['c']
        pos_count = conn.execute('SELECT COUNT(*) c FROM order_positions').fetchone()['c']
        row = conn.execute('SELECT raw_data, created, state_name, sales_channel_id '
                           'FROM sales_orders WHERE id = ?', ('order-1',)).fetchone()
    check('50 строк в sales_orders', orders_count == 50, f'{orders_count}')
    check('100 строк в order_positions', pos_count == 100, f'{pos_count}')
    check('created сохранён', row['created'] == '2026-07-15 10:00:00.000', str(row['created']))
    check('state_name сохранён', row['state_name'] == 'Выполнен', str(row['state_name']))
    check('sales_channel_id разобран из meta', row['sales_channel_id'] == 'ch-1', str(row['sales_channel_id']))
    check('positions не дублируются в raw_data заказа',
          'positions' not in json.loads(row['raw_data']))

    print('\n4. Битый заказ не роняет всю пачку')
    broken = make_order(999)
    broken['rate'] = {'value': {'unexpected': 'dict'}}  # непривязываемый тип для sqlite
    batch = [make_order(100), broken, make_order(101)]
    saved = storage.save_sales_orders_batch(batch)
    with sqlite3.connect(db_path) as conn:
        ok_saved = conn.execute(
            "SELECT COUNT(*) FROM sales_orders WHERE id IN ('order-100','order-101')"
        ).fetchone()[0]
        broken_saved = conn.execute(
            "SELECT COUNT(*) FROM sales_orders WHERE id = 'order-999'"
        ).fetchone()[0]
        broken_pos = conn.execute(
            "SELECT COUNT(*) FROM order_positions WHERE order_id = 'order-999'"
        ).fetchone()[0]
    check('исправные заказы пачки сохранены', ok_saved == 2, f'{ok_saved}')
    check('битый заказ откачен', broken_saved == 0 and broken_pos == 0)
    check('возвращён счётчик без битого', saved == 2, f'saved={saved}')

    print('\n5. Прогресс синхронизации')
    log_id = storage.start_sync_log('sales_orders')
    storage.update_sync_log_progress(log_id, 1234)
    log = storage.get_latest_sync_log('sales_orders')
    check('records_count обновлён', log['records_count'] == 1234, str(log['records_count']))
    check('progress_at заполнен', bool(log['progress_at']), str(log['progress_at']))
    check('статус started', log['status'] == 'started', str(log['status']))

    storage.finish_sync_log(log_id, records_count=1234, status='completed')
    log = storage.get_latest_sync_log('sales_orders')
    check('finish_sync_log закрывает запись', log['status'] == 'completed', str(log['status']))

    print('\n6. Определение мёртвого синка (_sync_log_is_stale)')
    os.environ.setdefault('MOYSKLAD_DB_PATH', db_path)
    from moysklad.server import _sync_log_is_stale
    from datetime import datetime, timedelta
    fresh = {'progress_at': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}
    dead = {'progress_at': (datetime.utcnow() - timedelta(hours=2)).strftime('%Y-%m-%d %H:%M:%S')}
    check('свежий прогресс — живой синк', _sync_log_is_stale(fresh) is False)
    check('прогресс двухчасовой давности — мёртвый', _sync_log_is_stale(dead) is True)
    check('без отметок времени — мёртвый', _sync_log_is_stale({}) is True)
    check('битая отметка времени — мёртвый', _sync_log_is_stale({'progress_at': 'мусор'}) is True)

    print('\n7. ABC-анализ считается на записанных данных')
    storage.save_sales_channels([{'id': 'ch-1', 'name': 'WhatsApp'}])
    rows = storage.get_abc_analysis(date_from='2026-07-01', date_to='2026-07-31')
    check('ABC вернул товары', len(rows) == 2, f'{len(rows)} строк')
    if rows:
        check('накопительный процент доходит до 100',
              abs(rows[-1]['cumulative_pct'] - 100) < 0.01, str(rows[-1]['cumulative_pct']))
        check('классы проставлены', all(r['abc_class'] in ('A', 'B', 'C') for r in rows))
    rows_ch = storage.get_abc_analysis(date_from='2026-07-01', date_to='2026-07-31', channel_id='ch-1')
    check('фильтр по каналу работает', len(rows_ch) == 2, f'{len(rows_ch)} строк')
    rows_other = storage.get_abc_analysis(date_from='2026-07-01', date_to='2026-07-31', channel_id='ch-none')
    check('чужой канал даёт пусто', len(rows_other) == 0, f'{len(rows_other)} строк')

    print('\n8. Скорость: батч против позаказной записи')
    many = [make_order(1000 + i) for i in range(400)]
    t0 = time.perf_counter()
    storage.save_sales_orders_batch(many)
    batch_time = time.perf_counter() - t0

    single = [make_order(5000 + i) for i in range(400)]
    t0 = time.perf_counter()
    for order in single:
        storage.save_sales_order(order)
    single_time = time.perf_counter() - t0

    speedup = single_time / batch_time if batch_time else 0
    print(f'  батч: {batch_time:.2f} c | по одному: {single_time:.2f} c | выигрыш x{speedup:.1f}')
    check('батч быстрее позаказной записи', speedup > 1.5, f'x{speedup:.1f}')

    print('\n9. Гонка миграций между воркерами')
    # На проде 2 воркера gunicorn стартуют одновременно и оба гонят _init_db с
    # одними и теми же ALTER TABLE. Раньше проигравший падал с "duplicate column
    # name"/"database is locked", и исключение вылетало наружу через get_storage.
    import threading

    race_db = os.path.join(tmp_dir, 'race.db')
    # "Старая" БД, как на проде до деплоя: полная схема текущего кода минус те
    # колонки, что добавляются миграциями. Строим её из самой схемы, а не руками,
    # чтобы тест не разошёлся с реальным CREATE TABLE.
    MoySkladStorage(race_db)
    old = sqlite3.connect(race_db)
    old.execute('PRAGMA journal_mode=DELETE')  # прод-база до WAL — жёсткий вариант гонки
    old.execute('DROP INDEX IF EXISTS idx_sales_orders_created')
    old.execute('DROP INDEX IF EXISTS idx_sales_orders_channel_id')
    for table, column in [('sales_orders', 'created'),
                          ('sales_orders', 'sales_channel_id'),
                          ('sync_log', 'progress_at')]:
        old.execute(f'ALTER TABLE {table} DROP COLUMN {column}')
    old.commit()
    old.close()

    errors = []
    barrier = threading.Barrier(4)

    def init_worker():
        try:
            barrier.wait()
            MoySkladStorage(race_db)
        except Exception as e:
            errors.append(repr(e))

    threads = [threading.Thread(target=init_worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    check('4 параллельных воркера мигрировали без падений', not errors, '; '.join(errors[:2]))

    conn = sqlite3.connect(race_db)
    so_cols = {r[1] for r in conn.execute('PRAGMA table_info(sales_orders)')}
    sl_cols = {r[1] for r in conn.execute('PRAGMA table_info(sync_log)')}
    idx = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    conn.close()
    check('колонка created добавлена', 'created' in so_cols)
    check('колонка sales_channel_id добавлена', 'sales_channel_id' in so_cols)
    check('колонка progress_at добавлена', 'progress_at' in sl_cols)
    check('индекс по created создан', 'idx_sales_orders_created' in idx)
    check('индекс по sales_channel_id создан', 'idx_sales_orders_channel_id' in idx)

    print('\n' + ('ВСЁ ПРОШЛО' if not failures else f'ПРОВАЛЕНО: {failures}'))
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
