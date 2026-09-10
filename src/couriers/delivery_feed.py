"""
Лента изменений заказов из RetailCRM (Фаза 2 модуля «Курьеры: доставка заказов»).

Зачем отдельный контур, если синк уже есть. Глубокий синк ходит раз в полчаса и
переписывает окно дат целиком — для отчёта по выплатам это правильно, для
курьера бесполезно: заказ «на сейчас» протухнет раньше, чем появится на экране.
Лента решает другую задачу — узнать об изменении за минуту, потратив один
запрос вместо перебора всех заказов окна.

Механика (проверена на живом API 2026-09-08):

- `GET /api/v5/orders/history?filter[sinceId]=N` — официально рекомендованный
  способ трансляции изменений во внешнюю систему;
- **листать можно только курсором**: на глубине CRM отвечает «Use the shift of
  the `filter[sinceId]` instead of `page` parameter»;
- **курсор нельзя начинать с нуля**: без него история отдаётся с 2021 года
  (41 708 записей только за неделю). Первый запуск берёт текущий максимум id;
- **своё эхо отсекается по `apiKey.current`**: когда мы сами меняем статус
  заказа, изменение тоже попадает в историю, и без фильтра лента гоняла бы
  собственные записи по кругу. CRM сама помечает записи, сделанные тем ключом,
  которым мы читаем.

Что лента НЕ делает: не решает, кому показывать заказ (это отбор при чтении) и
не трогает брони (Фаза 4). Её работа — держать витрину свежей.
"""

import logging
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from . import retailcrm
from .storage import (get_site_cities, get_sync_state, release_sync_lock,
                      renew_sync_lock, set_sync_state, try_acquire_sync_lock,
                      try_claim_scheduled_run)

logger = logging.getLogger(__name__)

# Ключ курсора в sync_state. Хранится id ПОСЛЕДНЕЙ обработанной записи истории,
# а не время: время в CRM живёт в поясе аккаунта, а id монотонен и не зависит
# от часов (урок из feedback про курсоры синхронизации).
CURSOR_KEY = "orders_history_since_id"

# Как часто лента ходит в CRM. Минута — компромисс: заказ появляется у курьера
# практически сразу, а нагрузка это 1 запрос (~1 КБ) при лимите CRM в 10
# запросов в секунду.
FEED_INTERVAL_SECONDS = 60

# Стартовая задержка своя, не совпадающая с глубоким синком (у того 120 с):
# оба контура пишут на общий медленный диск /data, и одновременный старт
# означал бы волну записи в один момент.
FEED_START_DELAY_SECONDS = 45

FEED_LOCK = "couriers_feed"
FEED_LOCK_TTL_SECONDS = 5 * 60

# Потолок страниц истории на один тик: разгребать накопившееся лучше
# несколькими тиками, чем одним прогоном, который держит воркер.
MAX_PAGES_PER_TICK = 20

# Поля истории, изменение которых меняет картину для курьера. Остальные
# (себестоимость, цены позиций, служебные галочки) для ленты шум: их принесёт
# глубокий синк.
WATCHED_FIELDS = {
    "status",              # заказ стал видимым/готовым/уехал дальше
    "delivery_type",       # свой курьер ↔ аутсорс (26 смен за день)
    "delivery_date",
    "delivery_time",
    "delivery_address.text",
    "delivery_address.city",
    "integration_delivery_data.courier",
    "custom_data_i_vremia_gotovnosti",
    "custom_order_availability_time",
    "custom_recipient_name",
    "custom_recipient_phone",
    "custom_ne_sviazyvatsia_s_poluchatelem",
    "manager_comment",
    "customer_comment",
    "custom_note_text",
}


def is_own_echo(record: Dict[str, Any]) -> bool:
    """
    True — запись сделана нами же (тем API-ключом, которым мы читаем).

    Без этого фильтра лента разбирала бы собственные правки статусов: мы пишем
    «Курьер забрал» → изменение попадает в историю → лента видит его как
    новость и снова обновляет заказ. Работы это добавляет ровно столько же,
    сколько экономит.
    """
    if record.get("source") != "api":
        return False
    api_key = record.get("apiKey") or {}
    return bool(api_key.get("current"))


def collect_order_ids(records: List[Dict[str, Any]],
                      watched_fields: Optional[Set[str]] = None) -> List[int]:
    """
    Из пачки записей истории — заказы, которые надо перечитать.

    Отдельной функцией, потому что здесь легко ошибиться в обе стороны: взять
    всё подряд (и каждый тик дозапрашивать сотни заказов) или отфильтровать
    лишнего (и потерять смену типа доставки, из-за которой заказ должен
    исчезнуть у курьера).
    """
    fields = WATCHED_FIELDS if watched_fields is None else watched_fields
    order_ids: List[int] = []
    seen: Set[int] = set()
    for record in records:
        if is_own_echo(record):
            continue
        if record.get("field") not in fields:
            continue
        order_id = (record.get("order") or {}).get("id")
        if order_id and order_id not in seen:
            seen.add(order_id)
            order_ids.append(int(order_id))
    return order_ids


def get_cursor() -> int:
    raw = get_sync_state(CURSOR_KEY)
    try:
        return int(raw) if raw else 0
    except (TypeError, ValueError):
        logger.warning(f"Курсор ленты испорчен ({raw!r}) — начинаем заново")
        return 0


def set_cursor(value: int) -> None:
    set_sync_state(CURSOR_KEY, str(int(value)))


def ensure_cursor(client) -> int:
    """
    Курсор для первого запуска: текущий максимум id, а не ноль.

    Ноль означал бы «отдай мне всю историю с 2021 года» — это десятки тысяч
    записей и полчаса работы ради данных, которые давно неактуальны.
    """
    cursor = get_cursor()
    if cursor:
        return cursor
    cursor = client.latest_history_id()
    set_cursor(cursor)
    logger.info(f"Лента изменений: курсор инициализирован значением {cursor}")
    return cursor


def run_once(deadline: Optional[float] = None) -> Dict[str, Any]:
    """
    Один проход ленты. Возвращает сводку — её же пишем в лог.

    Курсор сдвигается ТОЛЬКО после успешной записи заказов в витрину: упасть
    посередине и потерять изменения хуже, чем перечитать пачку дважды
    (перечитывание идемпотентно, потеря — нет).
    """
    # apply_..., а не upsert_...: витрину читает ещё и «Загрузка салонов»,
    # и пришедший лентой заказ обязан сразу получить трудоёмкость.
    from .delivery_storage import apply_orders_from_crm

    client = retailcrm.get_client()
    cursor = ensure_cursor(client)
    site_cities = get_site_cities()

    stats = {"records": 0, "orders": 0, "pages": 0, "cursor_from": cursor}

    for records in client.iter_history_since(cursor, max_pages=MAX_PAGES_PER_TICK):
        stats["pages"] += 1
        stats["records"] += len(records)
        page_cursor = max(record.get("id") or 0 for record in records)

        order_ids = collect_order_ids(records)
        if order_ids:
            orders = client.get_orders_by_ids(order_ids)
            rows = [row for row in
                    (retailcrm.parse_order(order, site_cities) for order in orders)
                    if row]
            applied = apply_orders_from_crm(rows)
            stats["orders"] += applied["written"]
            stats["recalc_dates"] = stats.get("recalc_dates", 0) + applied["recalc_dates"]

        set_cursor(page_cursor)
        stats["cursor_to"] = page_cursor

        if deadline is not None and time.monotonic() >= deadline:
            logger.info("Лента изменений: истёк бюджет тика, продолжим со следующего")
            break

    stats["images"] = fetch_missing_images(client, deadline=deadline)
    stats["claims"] = sweep_assignments()
    return stats


def sweep_assignments() -> Dict[str, int]:
    """
    Прибрать брони: снять просроченные и те, чьих заказов больше нет.

    Здесь, а не отдельным планировщиком, по двум причинам. Во-первых, тик
    ленты уже захвачен талоном на обоих воркерах — своя фоновая задача
    означала бы второй лок и второй ритм обращений к общему медленному
    `/data`. Во-вторых, уборка осмысленна ровно после того, как витрина
    обновилась: заказ, который отменили минуту назад, виден именно сейчас.

    Ошибка не роняет тик: перенос курсора истории важнее уборки, а её
    повторит следующая минута.
    """
    from . import storage
    from .delivery_storage import expire_stale_claims, release_orphan_claims

    result = {"expired": 0}
    try:
        result["expired"] = expire_stale_claims()
        codes = [row["code"] for row in storage.list_delivery_types()
                 if row.get("counts_as_courier")]
        result.update(release_orphan_claims(codes))
    except Exception as e:
        logger.warning(f"Лента изменений: уборка броней не удалась — {e}")

    dropped = sum(value for value in result.values() if value)
    if dropped:
        logger.info(f"Брони: снято {dropped} ({result})")
    return result


def fetch_missing_images(client, deadline: Optional[float] = None) -> int:
    """
    Дотянуть ссылки на фото для позиций свежих заказов. Возвращает число
    записей (в том числе «фото нет» — это тоже ответ).

    Почему здесь, а не при открытии карточки: курьер открывает её на ходу, а
    внешний вызов из обработчика уже дважды укладывал прод — воркеров два, и
    один зависший запрос занимает половину мощности сайта.

    Почему одна пачка за тик: лента обязана оставаться дешёвым тиком в один
    запрос. После первого прогона очередь почти всегда пуста — новые офферы
    появляются десятками в день, а не тысячами.

    Ошибка здесь не должна ронять тик: фото — украшение карточки, а перенос
    курсора истории — нет.
    """
    if deadline is not None and time.monotonic() >= deadline:
        return 0

    try:
        # Импорт внутри try, а не над ним: обещание «не роняем тик» должно
        # держаться целиком, включая сам импорт.
        from .delivery_storage import pending_image_offer_ids, save_product_images
        offer_ids = pending_image_offer_ids()
        if not offer_ids:
            return 0
        return save_product_images(client.get_product_images(offer_ids))
    except Exception as e:
        logger.warning(f"Лента изменений: не удалось получить фото товаров — {e}")
        return 0


def _feed_loop() -> None:
    """Цикл планировщика ленты (демон-поток в каждом воркере)."""
    time.sleep(FEED_START_DELAY_SECONDS)
    while True:
        try:
            # Талон на тик — общий для обоих воркеров: иначе каждую минуту
            # в CRM уходило бы два одинаковых запроса.
            if try_claim_scheduled_run(FEED_LOCK, FEED_INTERVAL_SECONDS):
                if try_acquire_sync_lock(FEED_LOCK, FEED_LOCK_TTL_SECONDS):
                    try:
                        started = time.monotonic()
                        stats = run_once(deadline=started + FEED_LOCK_TTL_SECONDS / 2)
                        if stats["records"]:
                            logger.info(
                                f"Лента изменений: записей {stats['records']}, "
                                f"обновлено заказов {stats['orders']}, "
                                f"курсор {stats.get('cursor_from')} → "
                                f"{stats.get('cursor_to')}"
                            )
                        renew_sync_lock(FEED_LOCK, FEED_LOCK_TTL_SECONDS)
                    finally:
                        release_sync_lock(FEED_LOCK)
        except Exception as e:
            # Лента не имеет права уронить поток: следующий тик через минуту.
            logger.error(f"Лента изменений: ошибка тика — {e}", exc_info=True)
        time.sleep(FEED_INTERVAL_SECONDS)


def start_feed_scheduler() -> None:
    """Запустить ленту при старте приложения."""
    if not retailcrm.is_configured():
        logger.warning("Лента изменений не запущена: RetailCRM не настроен")
        return
    thread = threading.Thread(target=_feed_loop, daemon=True,
                              name="couriers-delivery-feed")
    thread.start()
    logger.info(
        f"Лента изменений заказов запущена (тик {FEED_INTERVAL_SECONDS} с, "
        f"старт через {FEED_START_DELAY_SECONDS} с)"
    )
