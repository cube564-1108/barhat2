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

        # По умолчанию приходов нет: базовые разделы проверяют поведение
        # «цены не существует вовсе». Запасной источник — в PurchaseClient.
        if method == 'GET' and path == '/entity/enter':
            return {'rows': [], 'meta': {'size': 0}}

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

print('\n8а. Запасной источник: цена ближайшего прихода')
# Клубника по учёту в минусе — себестоимости нет, но приходы с ценой есть.
# Без этой ветки её списание навсегда осталось бы нулевым.
ENTER_DOCS = [
    {'moment': '2026-08-14 09:57:00.000', 'positions': {'rows': [
        {'quantity': 2875, 'price': 7500.0,
         'assortment': {'name': 'Клубника', 'meta': {'href': href_of('berry')}}}]}},
    {'moment': '2026-08-11 09:52:00.000', 'positions': {'rows': [
        {'quantity': 3665, 'price': 8000.0,
         'assortment': {'name': 'Клубника', 'meta': {'href': href_of('berry')}}}]}},
]


class PurchaseClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.enter_calls = []

    def request(self, method, path, params=None, json_data=None, **kwargs):
        if method == 'GET' and path == '/entity/enter':
            self.enter_calls.append(params)
            return {'rows': ENTER_DOCS, 'meta': {'size': len(ENTER_DOCS)}}
        return super().request(method, path, params=params, json_data=json_data, **kwargs)


client = PurchaseClient()
prices = client.get_last_purchase_prices(STORE_HREF, [href_of('berry')],
                                         before_moment='2026-08-26 07:32:00')
check(prices.get(href_of('berry')) == 7500.0,
      'взята цена ближайшего прихода (75,00 ₽), а не более старого (80,00 ₽)',
      str(prices))
call = client.enter_calls[0]
check(f'store={STORE_HREF}' in call['filter'], 'приход ищется на складе заявки', call['filter'])
check('moment<=2026-08-26 07:32:00' in call['filter'],
      'приходы позже документа не берутся', call['filter'])
check(call.get('order') == 'moment,desc', 'сортировка от свежих к старым')

print('   -- себестоимость важнее цены прихода')
client = PurchaseClient()
prices, sources = writeoffs_server._resolve_prices(
    client, STORE_HREF, [href_of('rose'), href_of('berry')])
check(prices[href_of('rose')] == 11900.0 and sources[href_of('rose')] == 'stock',
      'где есть себестоимость — берётся она, источник stock', str(sources))
check(prices[href_of('berry')] == 7500.0 and sources[href_of('berry')] == 'purchase',
      'где её нет — цена прихода, источник purchase', str(sources))

print('   -- новое списание тоже получает цену клубники')
writeoff3 = create_writeoff(store_id, 'florist', [
    {'moysklad_product_id': 'berry', 'moysklad_product_href': href_of('berry'),
     'product_name': 'Клубника', 'quantity': 275, 'uom_name': 'г', 'reason': 'порча'},
])
purchase_client = PurchaseClient()
writeoffs_server.get_client = lambda: purchase_client
lock_writeoff_for_sending(writeoff3['id'], 'manager')
writeoffs_server._send_to_moysklad(
    writeoff3['id'], store_id, get_writeoff_by_id(writeoff3['id'])['positions'], 'florist')
sent_position = purchase_client.posted[0]['positions'][0]
check(sent_position.get('price') == 7500.0,
      'клубника уходит в МойСклад с ценой, а не нулём', str(sent_position))

print('\n9. Бэкфилл старых нулевых документов')
from datetime import datetime, timezone  # noqa: E402
from flask import Flask  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402
from auth import auth_bp, init_auth_tables, login_manager  # noqa: E402
from writeoffs.storage import get_db  # noqa: E402

# Документ месячной давности: цена должна браться на ЕГО момент, а не на сегодня
OLD_MOMENT = '2026-08-20 12:07:00.000'
COSTS_AT_MOMENT = {(STORE_HREF, href_of('rose')): 9900.0}

LOSS_DOCS = [
    {   # починен наполовину: сумма уже НЕ нулевая, но позиция без цены осталась.
        # Отбор по sum=0 такой документ терял навсегда — ровно этот случай
        # вскрылся на проде 17.09.2026 после первого прогона бэкфилла.
        'id': 'doc-half', 'name': '00210-00061', 'moment': OLD_MOMENT,
        'description': 'Списание #61 (дашборд БАРХАТ)', 'sum': 15400.0,
        'store': {'meta': {'href': STORE_HREF}},
        'positions': {'rows': [
            {'id': 'pos-done', 'quantity': 1, 'price': 15400.0,
             'assortment': {'name': 'Гортензия белая', 'meta': {'href': href_of('rose')}}},
            {'id': 'pos-left', 'quantity': 315, 'price': 0.0,
             'assortment': {'name': 'Клубника', 'meta': {'href': href_of('rose')}}},
        ]},
    },
    {   # наш, нулевой — чинить
        'id': 'doc-ours', 'name': '00196-00046', 'moment': OLD_MOMENT,
        'description': 'Списание #6 (дашборд БАРХАТ)', 'sum': 0.0,
        'store': {'meta': {'href': STORE_HREF}},
        'positions': {'rows': [
            {'id': 'pos-1', 'quantity': 3, 'price': 0.0,
             'assortment': {'name': 'Роза', 'meta': {'href': href_of('rose')}}},
            {'id': 'pos-2', 'quantity': 275, 'price': 0.0,
             'assortment': {'name': 'Клубника', 'meta': {'href': href_of('berry')}}},
            {'id': 'pos-3', 'quantity': 1, 'price': 15400.0,
             'assortment': {'name': 'Роза кустовая', 'meta': {'href': href_of('rose')}}},
        ]},
    },
    {   # заведён руками в МойСкладе — не трогать
        'id': 'doc-alien', 'name': '00210-00001', 'moment': OLD_MOMENT,
        'description': 'Инвент шары', 'sum': 0.0,
        'store': {'meta': {'href': STORE_HREF}},
        'positions': {'rows': [
            {'id': 'pos-a', 'quantity': 5, 'price': 0.0,
             'assortment': {'name': 'Шар', 'meta': {'href': href_of('ball')}}},
        ]},
    },
    {   # тоже наш — для проверки лимита
        'id': 'doc-ours-2', 'name': '00196-00047', 'moment': OLD_MOMENT,
        'description': 'Списание #8 (дашборд БАРХАТ)', 'sum': 0.0,
        'store': {'meta': {'href': STORE_HREF}},
        'positions': {'rows': [
            {'id': 'pos-4', 'quantity': 1, 'price': 0.0,
             'assortment': {'name': 'Роза', 'meta': {'href': href_of('rose')}}},
        ]},
    },
]


class BackfillClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.puts = []
        self.loss_queries = []

    def request(self, method, path, params=None, json_data=None, **kwargs):
        if method == 'GET' and path == '/entity/loss':
            self.loss_queries.append(params)
            return {'rows': LOSS_DOCS, 'meta': {'size': len(LOSS_DOCS)}}
        if method == 'PUT' and '/positions/' in path:
            self.puts.append((path, json_data))
            return {'id': path.rsplit('/', 1)[-1], 'price': json_data.get('price')}
        if method == 'GET' and path == '/report/stock/all' and (params or {}).get('moment'):
            # исторические цены отличаются от сегодняшних
            self.stock_calls.append(params)
            conditions = params['filter'].split(';')
            store = next((c.split('=', 1)[1] for c in conditions if c.startswith('store=')), None)
            rows = []
            for c in conditions:
                if not c.startswith('product='):
                    continue
                product = c.split('=', 1)[1]
                price = COSTS_AT_MOMENT.get((store, product))
                if price:
                    rows.append({'meta': {'href': product}, 'name': product, 'price': price})
            return {'rows': rows, 'meta': {'size': len(rows)}}
        return super().request(method, path, params=params, json_data=json_data, **kwargs)


AJAX = {'X-Requested-With': 'barhat-dashboard'}
app = Flask(__name__)
app.secret_key = 'test-secret'
login_manager.init_app(app)
login_manager.login_view = None
app.register_blueprint(auth_bp)
app.register_blueprint(writeoffs_server.writeoffs_bp)
with app.app_context():
    init_auth_tables()

conn = get_db()
try:
    for username, role in (('admin_wo', 'admin'), ('manager_wo', 'manager')):
        conn.execute(
            """INSERT INTO users (username, full_name, password_hash, role, is_active, created_at)
               VALUES (?, ?, ?, ?, 1, ?)""",
            (username, username, generate_password_hash('secret'), role,
             datetime.now(timezone.utc).isoformat()))
    conn.commit()
finally:
    conn.close()

admin = app.test_client()
manager = app.test_client()
admin.post('/api/auth/login', json={'username': 'admin_wo', 'password': 'secret'})
manager.post('/api/auth/login', json={'username': 'manager_wo', 'password': 'secret'})

backfill = BackfillClient()
writeoffs_server.get_client = lambda: backfill

print('   -- проверка (dry_run)')
resp = admin.post('/api/writeoffs/admin/backfill-prices',
                  json={'since': '2026-08-01'}, headers=AJAX)
body = resp.get_json() or {}
check(resp.status_code == 200, 'проверка отработала', f'код {resp.status_code}')
check(backfill.puts == [], 'при проверке в МойСклад ничего не записано', str(backfill.puts))
check(body.get('positions_updated') == 3,
      'к правке намечены 3 позиции, включая оставшуюся в починенном наполовину', str(body))
check(body.get('positions_without_cost') == 1,
      'клубника без себестоимости посчитана отдельно', str(body))
check(body.get('documents_found') == 3,
      'чужой документ «Инвент шары» в работу не взят', str(body.get('documents_found')))
check(any(d.get('document') == '00210-00061' for d in body.get('details', [])),
      'документ с НЕнулевой суммой, но нулевой позицией, всё равно найден',
      '(отбор по sum=0 терял его — баг прода 17.09.2026)')

print('   -- цена берётся на момент документа')
check(any(call.get('moment', '').startswith('2026-08-20') for call in backfill.stock_calls),
      'себестоимость запрошена на момент документа, а не на сегодня',
      str([c.get('moment') for c in backfill.stock_calls]))

print('   -- реальная правка')
backfill = BackfillClient()
writeoffs_server.get_client = lambda: backfill
resp = admin.post('/api/writeoffs/admin/backfill-prices',
                  json={'since': '2026-08-01', 'dry_run': False}, headers=AJAX)
body = resp.get_json() or {}
check(len(backfill.puts) == 3, 'ушло 3 правки позиций', str(backfill.puts))
paths = [p for p, _ in backfill.puts]
check(any('pos-left' in p for p in paths),
      'дочинена позиция в документе с ненулевой суммой', str(paths))
check(not any('pos-done' in p for p in paths),
      'позиция, где цена уже стояла, второй раз не правится', str(paths))
check(all('/entity/loss/' in p and '/positions/' in p for p in paths),
      'правится позиция, а не документ целиком', str(paths))
check(all(j.get('price') == 9900.0 for _, j in backfill.puts),
      'проставлена историческая цена 99,00 ₽, а не сегодняшняя 119,00 ₽',
      str(backfill.puts))
check('pos-3' not in str(paths), 'позиция, где цена уже была, не тронута', str(paths))
check('pos-a' not in str(paths), 'позиция чужого документа не тронута', str(paths))
check(body.get('documents_updated') == 3, 'починены все три наших документа', str(body))

print('   -- отказ МойСклада объясняется, а не прячется')


class RejectingClient(BackfillClient):
    """МойСклад отвечает 412 «задвоен номер» — реальный отказ 17.09.2026."""

    def request(self, method, path, params=None, json_data=None, **kwargs):
        if method == 'PUT' and '/positions/' in path:
            self.last_error = {'text': "Ошибка сохранения объекта: нарушено "
                                       "ограничение уникальности параметра 'name'",
                               'code': 3006, 'status': 412}
            return None
        return super().request(method, path, params=params, json_data=json_data, **kwargs)


rejecting = RejectingClient()
writeoffs_server.get_client = lambda: rejecting
resp = admin.post('/api/writeoffs/admin/backfill-prices',
                  json={'since': '2026-08-01', 'dry_run': False}, headers=AJAX)
body = resp.get_json() or {}
first_error = (body.get('errors') or [{}])[0].get('error', '')
check('задвоен номер' in first_error,
      'причина отказа названа словами, а не «МойСклад отклонил правку»', first_error)
check('Переименуйте' in first_error,
      'сказано, что делать человеку', first_error)
check(len(body.get('errors') or []) == 3, 'ошибка записана по каждой позиции', str(body.get('errors')))

unknown = writeoffs_server._explain_moysklad_error(
    {'text': 'Что-то пошло не так', 'code': 1234, 'status': 400})
check('Что-то пошло не так' in unknown and '1234' in unknown,
      'незнакомая ошибка отдаётся как есть, с кодом', unknown)
check('не ответил' in writeoffs_server._explain_moysklad_error(None),
      'молчание МойСклада отличается от отказа')

print('   -- ограничения и доступ')
backfill = BackfillClient()
writeoffs_server.get_client = lambda: backfill
resp = admin.post('/api/writeoffs/admin/backfill-prices',
                  json={'since': '2026-08-01', 'dry_run': False, 'limit': 1}, headers=AJAX)
body = resp.get_json() or {}
check(body.get('documents_processed') == 1, 'за вызов обработан 1 документ', str(body))
check(body.get('remaining') == 2 and body.get('more_possible') is True,
      'остаток показан и видно, что работа не закончена', str(body))

bad_date = admin.post('/api/writeoffs/admin/backfill-prices',
                      json={'since': '20.08.2026'}, headers=AJAX)
check(bad_date.status_code == 400, 'кривая дата — 400', f'код {bad_date.status_code}')

no_header = admin.post('/api/writeoffs/admin/backfill-prices', json={'since': '2026-08-01'})
check(no_header.status_code == 403,
      'без заголовка X-Requested-With — 403 (защита от подделки запроса)',
      f'код {no_header.status_code}')

not_admin = manager.post('/api/writeoffs/admin/backfill-prices',
                         json={'since': '2026-08-01'}, headers=AJAX)
check(not_admin.status_code == 403, 'управляющему ручка недоступна',
      f'код {not_admin.status_code}')

print('\n' + '=' * 60)
if failures:
    print(f'ПРОВАЛЕНО проверок: {len(failures)}')
    for f in failures:
        print(f'  - {f}')
    sys.exit(1)
print('Все проверки пройдены')
