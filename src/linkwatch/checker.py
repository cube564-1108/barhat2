"""
Проверка ссылок на товары: что из RetailCRM реально открывается на сайте.

Ссылку админ берёт из карточки товара в CRM и отправляет клиенту. В CRM она
приезжает из ICML-выгрузки Битрикса, и 2026-08-28 выяснилось, что у 147 товаров
из 456 она нерабочая. Причина и решение — docs/инструкция-ссылки-товаров-битрикс-crm.md.

Проверять «на глаз» бесполезно: 44% ссылок открываются ТОЛЬКО через 301-редирект
и выглядят исправными. Поэтому категорий четыре, а не «работает / не работает»,
и правильный адрес берётся из <link rel="canonical"> самой страницы, а не
угадывается.
"""

import logging
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional

import requests

from russian_ca import trust_russian_ca

from .storage import (
    STATUS_ERROR,
    STATUS_NO_URL,
    STATUS_NOT_FOUND,
    STATUS_OK,
    STATUS_REDIRECT,
    STATUS_WRONG_PAGE,
)

logger = logging.getLogger(__name__)

RETAILCRM_URL = (os.environ.get("RETAILCRM_URL") or "").rstrip("/")
RETAILCRM_API_KEY = os.environ.get("RETAILCRM_API_KEY") or ""

CRM_TIMEOUT = 30
SITE_TIMEOUT = 25
PAGE_LIMIT = 100

# Обход строго последовательный. Карточка товара генерится на сайте ~4 секунды,
# и любая параллельность его роняет: на 6 потоках сайт отдал 503 на две трети
# ссылок, на 3 — на 72%, а 503 неотличим от битой ссылки. Полный проход ~30 минут.
PAUSE_BETWEEN_REQUESTS = 0.1

# 503 — это просьба сайта помедленнее, а не диагноз ссылке.
RETRY_CODES = (429, 502, 503)
RETRIES = 2
RETRY_PAUSE = 3

CANONICAL_RE = re.compile(
    r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)["\']', re.I
)


class LinkCheckError(Exception):
    """Проверку не удалось выполнить целиком (CRM недоступна и т.п.)."""


def is_configured() -> bool:
    return bool(RETAILCRM_URL and RETAILCRM_API_KEY)


def fetch_products() -> List[Dict[str, Any]]:
    """Весь каталог из RetailCRM: то, что видит админ в карточке заказа."""
    if not is_configured():
        raise LinkCheckError("RETAILCRM_URL или RETAILCRM_API_KEY не заданы")

    session = requests.Session()
    session.headers.update({"X-API-KEY": RETAILCRM_API_KEY})
    trust_russian_ca(session)

    products: List[Dict[str, Any]] = []
    page = 1
    while True:
        try:
            response = session.get(
                f"{RETAILCRM_URL}/api/v5/store/products",
                params={"limit": PAGE_LIMIT, "page": page},
                timeout=CRM_TIMEOUT,
            )
        except requests.exceptions.RequestException as e:
            raise LinkCheckError(f"RetailCRM недоступна: {e}") from e

        if not response.ok:
            raise LinkCheckError(f"RetailCRM вернула {response.status_code}")

        data = response.json()
        products += data.get("products", [])
        if page >= data.get("pagination", {}).get("totalPageCount", 1):
            return products
        page += 1
        time.sleep(0.2)


def _leaf(url: str) -> str:
    """Последний сегмент пути. requests возвращает адрес с портом (host:443),
    поэтому сравнивать строки целиком нельзя — половина ссылок ложно уехала бы
    в «редиректы»."""
    return url.replace(":443", "").rstrip("/").split("/")[-1]


def check_one(product: Dict[str, Any], session: requests.Session) -> Dict[str, Any]:
    """Одна ссылка → её статус и канонический адрес, если он известен."""
    url = product.get("url") or ""
    result = {
        "product_id": product.get("id"),
        "article": product.get("article") or "",
        "name": product.get("name") or "",
        "url": url,
        "canonical_url": "",
    }

    # Пустая ссылка — это норма для товаров без публичной страницы («Товары МС»,
    # снятые с публикации сезонные). Их сознательно оставляют в CRM без ссылки.
    if not url:
        return {**result, "status": STATUS_NO_URL}

    response = None
    for attempt in range(RETRIES + 1):
        try:
            response = session.get(url, timeout=SITE_TIMEOUT, allow_redirects=True)
        except requests.exceptions.RequestException as e:
            return {**result, "status": STATUS_ERROR, "canonical_url": type(e).__name__}

        if response.status_code not in RETRY_CODES or attempt == RETRIES:
            break
        time.sleep(RETRY_PAUSE)

    if response.status_code == 404:
        return {**result, "status": STATUS_NOT_FOUND}
    if not response.ok:
        return {**result, "status": STATUS_ERROR, "canonical_url": str(response.status_code)}

    final = response.url.replace(":443", "")

    # Ушли на другую страницу — значит карточки нет, Битрикс показал раздел.
    # Канонический адрес не заполняем: его у такого товара не существует, и
    # подставить сюда адрес раздела значило бы предложить отправить клиенту его.
    if _leaf(url) != _leaf(final):
        return {**result, "status": STATUS_WRONG_PAGE}

    match = CANONICAL_RE.search(response.text)
    canonical = match.group(1) if match else final
    if canonical.rstrip("/") != url.rstrip("/"):
        return {**result, "status": STATUS_REDIRECT, "canonical_url": canonical}

    return {**result, "status": STATUS_OK, "canonical_url": canonical}


def run_check(
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> Dict[str, Any]:
    """
    Полный прогон. Возвращает счётчики и список проблемных товаров.

    on_progress зовётся по ходу — планировщик через него продлевает лок:
    прогон идёт полчаса, а TTL лока меньше.
    """
    products = fetch_products()

    site = requests.Session()
    site.headers.update({"User-Agent": "barhat-link-watch/1.0"})
    trust_russian_ca(site)

    counts = {"checked": 0, "ok": 0, "broken": 0, "no_url": 0}
    broken: List[Dict[str, Any]] = []

    from .storage import BROKEN_STATUSES

    for number, product in enumerate(products, start=1):
        outcome = check_one(product, site)
        counts["checked"] += 1

        if outcome["status"] == STATUS_OK:
            counts["ok"] += 1
        elif outcome["status"] == STATUS_NO_URL:
            counts["no_url"] += 1
        elif outcome["status"] in BROKEN_STATUSES:
            counts["broken"] += 1
            broken.append(outcome)

        if on_progress and number % 25 == 0:
            on_progress(number, len(products))

        time.sleep(PAUSE_BETWEEN_REQUESTS)

    logger.info(
        "Проверка ссылок: %d товаров, рабочих %d, битых %d, без ссылки %d",
        counts["checked"], counts["ok"], counts["broken"], counts["no_url"],
    )
    return {"counts": counts, "broken": broken}
