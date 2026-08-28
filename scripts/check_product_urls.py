"""
Сторож ссылок на товары: что из RetailCRM реально открывается на сайте.

Зачем. Ссылку на товар админ берёт из карточки в CRM и отправляет клиенту.
В CRM она приезжает из ICML-выгрузки Битрикса, и 2026-08-28 выяснилось, что
у 135 товаров из 456 она нерабочая: 101 отдаёт 404 (технический раздел
tovary-ms, которого на сайте нет), 34 уводят на раздел вместо карточки.

Проверять глазами бесполезно: ещё 202 ссылки открываются ТОЛЬКО через
301-редирект и на вид выглядят исправными. Отсюда четыре категории в отчёте,
а не «работает / не работает».

Разбор причин и порядок починки: docs/инструкция-ссылки-товаров-битрикс-crm.md

Запуск:
    python scripts/check_product_urls.py            # весь каталог
    python scripts/check_product_urls.py --limit 50 # быстрая проверка
"""

import argparse
import json
import os
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src'))

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '.env'))

import requests

from russian_ca import trust_russian_ca
from storage_paths import IS_PERSISTENT_MOUNT, resolve as resolve_data_path

RETAILCRM_URL = (os.environ.get('RETAILCRM_URL') or '').rstrip('/')
RETAILCRM_API_KEY = os.environ.get('RETAILCRM_API_KEY') or ''

# Внешний вызов без таймаута уже дважды укладывал прод (см. CLAUDE.md).
# Здесь скрипт разовый, но правило то же.
CRM_TIMEOUT = 30
SITE_TIMEOUT = 20

# RetailCRM принимает только 20/50/100
PAGE_LIMIT = 100

# Обход строго последовательный. Карточка товара генерится на сайте ~4 секунды,
# и любая параллельность его роняет: на 6 потоках 503 приходили на две трети
# ссылок, на 3 потоках — на 72%, и отчёт превращался в мусор (503 неотличим от
# «ссылка битая»). Полный проход занимает около получаса — для проверки,
# которую запускают после правок в Битриксе, это нормальная цена.
WORKERS = 1
PAUSE_BETWEEN_REQUESTS = 0.1

# 503 — это защита сайта от частых запросов, а не диагноз ссылке.
# Пара повторов с паузой отличает «сайт попросил помедленнее» от реальной поломки.
RETRY_CODES = (429, 502, 503)
RETRIES = 2
RETRY_PAUSE = 2

# На проде отчёт должен лечь на постоянный диск, локально — в data/ репозитория:
# resolve() вне posix отдаёт '/data/...', а на Windows это C:\data — то есть
# файл уехал бы вообще из проекта, и инструкция по проверке врала бы.
REPO_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
REPORT_PATH = (
    resolve_data_path('BAD_PRODUCT_URLS_PATH', 'bad_product_urls.json')
    if IS_PERSISTENT_MOUNT
    else os.path.join(REPO_ROOT, 'data', 'bad_product_urls.json')
)


def fetch_products(limit=None):
    """Все товары каталога из CRM: id, название и ссылка, которую увидит админ."""
    if not RETAILCRM_URL or not RETAILCRM_API_KEY:
        sys.exit('RETAILCRM_URL или RETAILCRM_API_KEY не заданы в .env')

    crm = requests.Session()
    crm.headers.update({'X-API-KEY': RETAILCRM_API_KEY})
    trust_russian_ca(crm)

    products, page = [], 1
    while True:
        response = crm.get(
            f'{RETAILCRM_URL}/api/v5/store/products',
            params={'limit': PAGE_LIMIT, 'page': page},
            timeout=CRM_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        products += data.get('products', [])

        if limit and len(products) >= limit:
            return products[:limit]
        if page >= data.get('pagination', {}).get('totalPageCount', 1):
            return products

        page += 1
        time.sleep(0.2)


def _same_page(requested, final):
    """Одна и та же карточка? Сравниваем последний сегмент пути.

    requests возвращает итоговый адрес с портом (`host:443`), поэтому сравнивать
    строки целиком нельзя — иначе каждая вторая ссылка ложно «уедет» в редиректы.
    """
    strip = lambda u: u.replace(':443', '').rstrip('/').split('/')[-1]
    return strip(requested) == strip(final)


def check(product, session):
    """Одна ссылка → категория и, если что-то не так, детали для отчёта."""
    url = product.get('url')
    name = product.get('name') or ''

    if not url:
        return 'ссылки нет вообще', {'id': product.get('id'), 'name': name, 'url': None}

    for attempt in range(RETRIES + 1):
        try:
            response = session.get(url, timeout=SITE_TIMEOUT, allow_redirects=True, stream=True)
            response.close()
        except requests.exceptions.RequestException as e:
            return 'ошибка запроса', {
                'id': product.get('id'), 'name': name, 'url': url,
                'problem': f'{type(e).__name__}',
            }

        if response.status_code not in RETRY_CODES or attempt == RETRIES:
            break
        time.sleep(RETRY_PAUSE)

    time.sleep(PAUSE_BETWEEN_REQUESTS)

    if response.status_code == 404:
        return 'чистый 404', {
            'id': product.get('id'), 'name': name, 'url': url, 'problem': '404',
        }
    if not response.ok:
        return f'ответ {response.status_code}', {
            'id': product.get('id'), 'name': name, 'url': url,
            'problem': str(response.status_code),
        }
    if _same_page(url, response.url):
        if response.url.replace(':443', '').rstrip('/') != url.rstrip('/'):
            return 'через редирект (неканоническая)', {
                'id': product.get('id'), 'name': name, 'url': url,
                'problem': f'301 -> {response.url}',
            }
        return 'ведёт точно на карточку', None

    return 'уводит на другую страницу', {
        'id': product.get('id'), 'name': name, 'url': url,
        'problem': f'уходит на {response.url}',
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--limit', type=int, help='проверить только первые N товаров')
    args = parser.parse_args()

    print('Забираю каталог из RetailCRM...')
    products = fetch_products(args.limit)
    print(f'товаров: {len(products)}\nПроверяю ссылки...')

    site = requests.Session()
    site.headers.update({'User-Agent': 'barhat-link-check/1.0'})
    trust_russian_ca(site)

    stats, bad = Counter(), []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for done, (category, detail) in enumerate(
            pool.map(lambda p: check(p, site), products), start=1
        ):
            stats[category] += 1
            if detail:
                bad.append(detail | {'категория': category})
            if done % 50 == 0:
                print(f'  проверено {done} из {len(products)}')

    print('\n=== Результат ===')
    for category, count in stats.most_common():
        share = count * 100 // len(products) if products else 0
        print(f'  {category:36} {count:4}  ({share}%)')

    os.makedirs(os.path.dirname(REPORT_PATH) or '.', exist_ok=True)
    with open(REPORT_PATH, 'w', encoding='utf-8') as f:
        json.dump(bad, f, ensure_ascii=False, indent=2)

    print(f'\nПроблемных товаров: {len(bad)} — подробности в {REPORT_PATH}')
    if bad:
        print('Что делать — docs/инструкция-ссылки-товаров-битрикс-crm.md')

    # Ненулевой код — чтобы скрипт годился как проверка после правок в Битриксе.
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
