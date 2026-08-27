"""
ABC-анализ не должен читать гигабайт ради шестисот строк.

Откуда взялось. В `sales_orders` и `order_positions` лежит `raw_data` —
полный JSON от МойСклада: 21 КБ и 9 КБ на строку, около гигабайта суммарно.
ABC-анализу из строки нужны три числа, но без покрывающего индекса SQLite
читает страницы целиком, вместе с этим JSON. На сетевом диске Amvera запрос
занимал 24-57 секунд и всё это время держал весь сайт — логи прода
2026-08-27, 07:31:23:

    GET /api/moysklad/sync-status    — 32.6 c
    GET /api/tasks                   — 32.6 c
    GET /api/moysklad/sales_channels — 32.6 c

Три посторонние ручки встали ровно на то же время, пока считался ABC.

Проверяется на синтетической базе с той же схемой:
1. индексы строятся и повторный вызов ничего не делает;
2. план запроса становится ПОЛНОСТЬЮ покрывающим — ни одна из двух жирных
   таблиц не читается;
3. результат ABC-анализа от индексов не изменился.

Запуск: python scripts/test_abc_indexes.py
"""

import io
import os
import socket
import sys
import tempfile


if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')


class NoNetwork(socket.socket):
    def connect(self, *a, **k):
        raise OSError("сеть отключена намеренно")

    def connect_ex(self, *a, **k):
        raise OSError("сеть отключена намеренно")


socket.socket = NoNetwork

_TMP = tempfile.mkdtemp(prefix='barhat_abc_')
DB_PATH = os.path.join(_TMP, 'moysklad.db')
os.environ['MOYSKLAD_DB_PATH'] = DB_PATH

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from moysklad.storage import get_storage  # noqa: E402

failures = []


def check(condition, message):
    print(('   OK   ' if condition else '   ПРОВАЛ ') + message)
    if not condition:
        failures.append(message)


ABC_QUERY = '''
SELECT op.assortment_id, op.assortment_name,
       SUM(op.sum)/100.0 AS revenue,
       COUNT(DISTINCT op.order_id) AS orders_count,
       SUM(op.quantity) AS quantity
FROM order_positions op
JOIN sales_orders so ON so.id = op.order_id
WHERE so.state_name = 'Выполнен'
GROUP BY op.assortment_id, op.assortment_name
HAVING revenue > 0
ORDER BY revenue DESC
'''


def seed(storage, orders=400, positions_per_order=5):
    """Наполнить базу так же «жирно», как боевую: с raw_data в каждой строке."""
    fat = 'x' * 20000  # тот самый JSON от МойСклада, ~20 КБ на строку
    with storage._get_connection() as conn:
        for i in range(orders):
            state = 'Выполнен' if i % 10 else 'Отменён'
            conn.execute(
                "INSERT INTO sales_orders (id, name, state_name, created, "
                "sales_channel_id, raw_data) VALUES (?,?,?,?,?,?)",
                (f'order-{i}', f'N{i}', state, f'2026-08-{i % 28 + 1:02d} 10:00:00',
                 'channel-1', fat),
            )
            for p in range(positions_per_order):
                conn.execute(
                    "INSERT INTO order_positions (order_id, assortment_id, "
                    "assortment_name, quantity, sum, product_folder_id, raw_data) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (f'order-{i}', f'goods-{p}', f'Товар {p}', p + 1,
                     (p + 1) * 100000, None, fat),
                )


def main():
    print("=== ABC-анализ: покрывающие индексы ===\n")

    storage = get_storage(DB_PATH)
    seed(storage)

    with storage._get_connection() as conn:
        before_rows = [tuple(r) for r in conn.execute(ABC_QUERY).fetchall()]
    check(len(before_rows) > 0, f"на синтетике ABC что-то считает ({len(before_rows)} строк)")

    print()
    print("1. Индексы строятся один раз")
    check(storage.ensure_reporting_indexes(), "первый вызов создал индексы")
    check(not storage.ensure_reporting_indexes(), "повторный вызов ничего не делает")
    print()

    print("2. План стал полностью покрывающим")
    with storage._get_connection() as conn:
        plan = [r[3] for r in conn.execute('EXPLAIN QUERY PLAN ' + ABC_QUERY)]
    for line in plan:
        print('    ', line)

    table_reads = [
        line for line in plan
        # Обращение к самой таблице (а не к индексу) в этих строках — это и
        # есть чтение raw_data, ради которого всё вставало.
        if ('order_positions' in line or 'sales_orders' in line)
        and 'COVERING INDEX' not in line
    ]
    check(not table_reads, f"жирные таблицы не читаются напрямую ({table_reads})")
    covering = [line for line in plan if 'COVERING INDEX' in line]
    check(len(covering) == 2, f"покрывающих обращений два ({len(covering)})")
    print()

    print("3. Результат не изменился")
    with storage._get_connection() as conn:
        after_rows = [tuple(r) for r in conn.execute(ABC_QUERY).fetchall()]
    check(before_rows == after_rows,
          f"ABC до и после совпал ({len(before_rows)} / {len(after_rows)} строк)")
    print()

    if failures:
        print(f"=== ПРОВАЛОВ: {len(failures)} ===")
        for message in failures:
            print(f"  - {message}")
        return 1

    print("=== ABC-анализ больше не тащит с диска лишнее ===")
    return 0


if __name__ == '__main__':
    sys.exit(main())
