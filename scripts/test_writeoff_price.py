"""
Себестоимость в документе списания: цена берётся из отчёта остатков по складу.

Регрессия до 17.09.2026: в позицию документа "Списание" не уходило поле price,
и МойСклад сохранял её с нулевой стоимостью. Замер на проде показал, что сам он
цену не подставляет и потом не пересчитывает — документ без цены остаётся
нулевым навсегда (проверено перечитыванием через 20 с, 90 с и 3 минуты). За
60 дней так ушли 28 документов из 74, то есть 115 позиций из 223, и ровно на эту
сумму «Показатели салонов» занижали списание: витрина warehouse_flows считает
его как quantity × price из документа.

Проверяет:
1. get_cost_prices — фильтр строится по складу И товарам, цена возвращается
   в копейках, ноль и отрицательная себестоимость отбрасываются.
2. Длинный список товаров бьётся на пачки (предел длины URL).
3. Отказ отчёта не роняет отправку — возвращается пустой словарь.
4. create_loss кладёт price в тело, только когда цена положительная.
5. СВЯЗКА: согласование заявки реально подставляет цену из отчёта в документ —
   ломается и при потерянном price в клиенте, и при неподставленной цене
   в _send_to_moysklad.
6. Себестоимость запрашивается по складу ИМЕННО ЭТОЙ заявки: на разных складах
   она отличается в разы (10,98 ₽ против 20,83 ₽ по одному и тому же шару).
7. Товар без партий на складе не срывает списание: заявка уходит, позиция
   остаётся без цены, остальные позиции цену получают.

Внешний API не вызывается: подменяется MoySkladClient.request.
"""

import io
import os
import sys

if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), '_test_writeoff_price.db')
for path in (TEST_DB_PATH, TEST_DB_PATH + '-wal', TEST_DB_PATH + '-shm'):
    if os.path.exists(path):
        os.remove(path)
os.environ['BARHAT_DB_PATH'] = TEST_DB_PATH
os.environ['MOYSKLAD_TOKEN'] = 'test-token'
os.environ['MOYSKLAD_ORGANIZATION_HREF'] = 'https://api.moysklad.ru/api/remap/1.2/entity/organization/org-1'

from moysklad.client import MoySkladClient, build_entity_href  # noqa: E402

failures = []


def check(condition, title, detail=''):
    if condition:
        print(f'  OK   {title}')
    else:
        print(f'  FAIL {title}' + (f' — {detail}' if detail else ''))
        failures.append(title)


def href_of(product_id):
    return build_entity_href('product', product_id)


STORE_HREF = 'https://api.moysklad.ru/api/remap/1.2/entity/store/store-sverdlovsk'
OTHER_STORE_HREF = 'https://api.moysklad.ru/api/remap/1.2/entity/store/store-bazhova'

# Себестоимость: по складам она разная, у «клубники» партий нет вовсе
COSTS = {
    (STORE_HREF, href_of('rose')): 11900.0,
    (STORE_HREF, href_of('ball')): 1098.4057971014493,
    (OTHER_STORE_HREF, href_of('ball')): 2083.494117647059,
    (STORE_HREF, href_of('gypso')): 0.0,          # склад есть, себестоимости нет
    (STORE_HREF, href_of('berry')): -12.79,       # минусовой остаток
}


class FakeClient(MoySkladClient):
    """Настоящий клиент, подменён только транспорт — вся логика тестируется."""

    def __init__(self, stock_fails=False):
        super().__init__(token='test-token')
        self.stock_fails = stock_fails
        self.stock_calls = []
        self.posted = []

    def request(self, method, path, params=None, json_data=None, **kwargs):
        if method == 'GET' and path == '/report/stock/all':
            self.stock_calls.append(params)
            if self.stock_fails:
                return None
            conditions = (params or {}).get('filter', '').split(';')
            store = next((c.split('=', 1)[1] for c in conditions
                          if c.startswith('store=')), None)
            products = [c.split('=', 1)[1] for c in conditions
                        if c.startswith('product=')]
            rows = []
            for product in products:
                price = COSTS.get((store, product))
                if price is None:
                    continue            # товара нет на складе — строки нет
                rows.append({
                    'meta': {'href': product + '?expand=supplier'},
                    'name': product.rsplit('/', 1)[-1],
                    'price': price,
                })
            return {'rows': rows, 'meta': {'size': len(rows)}}

        if method == 'POST' and path == '/entity/loss':
            self.posted.append(json_data)
            return {'id': 'loss-created-1', 'name': '00210-00999'}

        raise AssertionError(f'Неожиданный запрос {method} {path}')


print('\n1. get_cost_prices: цены по складу')
client = FakeClient()
prices = client.get_cost_prices(STORE_HREF, [href_of('rose'), href_of('ball')])
check(prices.get(href_of('rose')) == 11900.0,
      'цена розы вернулась в копейках', str(prices))
check(abs(prices.get(href_of('ball'), 0) - 1098.4057971014493) < 1e-9,
      'дробная себестоимость не округляется', str(prices))
filter_used = client.stock_calls[0]['filter']
check(f'store={STORE_HREF}' in filter_used,
      'в фильтре есть склад', filter_used)
check(filter_used.count('product=') == 2,
      'в фильтре есть оба товара', filter_used)
check(client.stock_calls[0].get('stockMode') == 'all',
      'запрошен режим all (иначе товары с нулевым остатком выпадают)')

print('\n2. Непригодные цены отбрасываются')
client = FakeClient()
prices = client.get_cost_prices(
    STORE_HREF, [href_of('rose'), href_of('gypso'), href_of('berry')])
check(href_of('gypso') not in prices, 'нулевая себестоимость не попала в ответ')
check(href_of('berry') not in prices,
      'отрицательная себестоимость не попала в ответ', str(prices))
check(href_of('rose') in prices, 'нормальная цена осталась')

print('\n3. Себестоимость берётся по складу заявки, а не общая')
client = FakeClient()
here = client.get_cost_prices(STORE_HREF, [href_of('ball')])
there = client.get_cost_prices(OTHER_STORE_HREF, [href_of('ball')])
check(here[href_of('ball')] != there[href_of('ball')],
      'цена одного товара на разных складах различается',
      f'{here} vs {there}')

print('\n4. Длинный список бьётся на пачки')
client = FakeClient()
many = [href_of(f'p{i}') for i in range(45)]
client.get_cost_prices(STORE_HREF, many)
check(len(client.stock_calls) == 3,
      'на 45 товаров ушло 3 запроса', f'запросов: {len(client.stock_calls)}')
check(all(call['filter'].count('product=') <= 20 for call in client.stock_calls),
      'в каждой пачке не больше 20 товаров')

print('\n5. Отчёт не ответил — не исключение, а пустой словарь')
client = FakeClient(stock_fails=True)
check(client.get_cost_prices(STORE_HREF, [href_of('rose')]) == {},
      'вернулся пустой словарь')

print('\n6. create_loss: price в теле только когда цена положительная')
client = FakeClient()
client.create_loss(
    organization_href=os.environ['MOYSKLAD_ORGANIZATION_HREF'],
    store_href=STORE_HREF,
    positions=[
        {'assortment_href': href_of('rose'), 'quantity': 3, 'price': 11900.0},
        {'assortment_href': href_of('gypso'), 'quantity': 1, 'price': None},
        {'assortment_href': href_of('berry'), 'quantity': 200, 'price': -12.79},
        {'assortment_href': href_of('ball'), 'quantity': 1, 'price': 0.0},
    ],
)
sent = client.posted[0]['positions']
check(sent[0].get('price') == 11900.0, 'цена ушла в позицию', str(sent[0]))
check('price' not in sent[1], 'price=None не попал в тело')
check('price' not in sent[2], 'отрицательная цена не попала в тело')
check('price' not in sent[3], 'нулевая цена не попала в тело')
check(all(p['quantity'] for p in sent), 'количество на месте')

print('\n7. СВЯЗКА: согласование заявки подставляет цену в документ')
from cashshifts.storage import init_cashshifts_tables, create_store  # noqa: E402
from writeoffs.storage import (  # noqa: E402
    init_writeoffs_tables,
    link_moysklad_store,
    create_writeoff,
    get_writeoff_by_id,
    lock_writeoff_for_sending,
)
import writeoffs.server as writeoffs_server  # noqa: E402

init_cashshifts_tables()
init_writeoffs_tables()

store_id = create_store('Свердловский проспект, 23')
link_moysklad_store(store_id, 'store-sverdlovsk', STORE_HREF)

writeoff = create_writeoff(store_id, 'florist', [
    {'moysklad_product_id': 'rose', 'moysklad_product_href': href_of('rose'),
     'product_name': 'Роза одноголовая белая', 'quantity': 3,
     'uom_name': 'шт', 'reason': 'увядание'},
    {'moysklad_product_id': 'berry', 'moysklad_product_href': href_of('berry'),
     'product_name': 'Клубника', 'quantity': 275,
     'uom_name': 'г', 'reason': 'порча'},
])

client = FakeClient()
writeoffs_server.get_client = lambda: client

lock_writeoff_for_sending(writeoff['id'], 'manager')
writeoffs_server._send_to_moysklad(
    writeoff['id'], store_id, get_writeoff_by_id(writeoff['id'])['positions'], 'florist')

check(len(client.posted) == 1, 'документ ушёл в МойСклад')
body = client.posted[0]
positions = {p['assortment']['meta']['href']: p for p in body['positions']}
check(positions[href_of('rose')].get('price') == 11900.0,
      'цена розы подставлена из отчёта остатков',
      str(positions[href_of('rose')]))
check('price' not in positions[href_of('berry')],
      'у клубники без партий цены нет, и выдуманной тоже нет',
      str(positions[href_of('berry')]))
check(body['store']['meta']['href'] == STORE_HREF, 'склад заявки')
check(client.stock_calls and f'store={STORE_HREF}' in client.stock_calls[0]['filter'],
      'себестоимость спрошена по складу заявки')
check(get_writeoff_by_id(writeoff['id'])['status'] == 'sent',
      'заявка помечена отправленной')

print('\n8. Отчёт недоступен — списание всё равно уходит')
writeoff2 = create_writeoff(store_id, 'florist', [
    {'moysklad_product_id': 'rose', 'moysklad_product_href': href_of('rose'),
     'product_name': 'Роза одноголовая белая', 'quantity': 1,
     'uom_name': 'шт', 'reason': 'увядание'},
])
broken = FakeClient(stock_fails=True)
writeoffs_server.get_client = lambda: broken
lock_writeoff_for_sending(writeoff2['id'], 'manager')
writeoffs_server._send_to_moysklad(
    writeoff2['id'], store_id, get_writeoff_by_id(writeoff2['id'])['positions'], 'florist')
check(len(broken.posted) == 1, 'документ ушёл, несмотря на недоступный отчёт')
check('price' not in broken.posted[0]['positions'][0],
      'позиция ушла без цены, а не с нулевой')
check(get_writeoff_by_id(writeoff2['id'])['status'] == 'sent',
      'заявка не подвисла в processing')

print('\n' + '=' * 60)
if failures:
    print(f'ПРОВАЛЕНО проверок: {len(failures)}')
    for f in failures:
        print(f'  - {f}')
    sys.exit(1)
print('Все проверки пройдены')
