"""
МойСклад JSON API
Blueprint для доступа к данным МойСклад (товары, остатки, заказы, ABC-анализ)

Регистрируется в src/pyrus/server.py (мастер Flask-приложение) — см. паттерн
cashshifts_bp / invoices_bp. Ниже также есть standalone-режим для локальной
разработки (python -m moysklad.server).
"""

import os
import re
import sys
import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Optional
from flask import Blueprint, Flask, jsonify, request
from flask_login import login_required
from dotenv import load_dotenv

# Импортируем модуль авторизации (как в cashshifts/server.py)
auth_path = os.path.join(os.path.dirname(__file__), '../')
sys.path.insert(0, auth_path)
from auth import section_required, role_required

from .storage import get_storage
from . import warehouse

load_dotenv()

logger = logging.getLogger(__name__)

# Конфигурация
DB_PATH = os.getenv('MOYSKLAD_DB_PATH', 'data/moysklad.db')

moysklad_bp = Blueprint("moysklad", __name__, url_prefix="/api/moysklad")


def get_db():
    """Получить подключение к БД"""
    return get_storage(DB_PATH)


def error_response(message: str, status: int = 400):
    """Сформировать ошибку"""
    return jsonify({
        'success': False,
        'error': message
    }), status


def success_response(data: Any, meta: dict = None):
    """Сформировать успешный ответ"""
    response = {
        'success': True,
        'data': data
    }
    if meta:
        response['meta'] = meta
    return jsonify(response)


# ========== Products ==========

@moysklad_bp.route('/products', methods=['GET'])
@login_required
def get_products():
    """
    Получить товары

    Query параметры:
        - folder_id: Фильтр по папке
        - archived: only_archived / active
        - limit: Максимум записей (по умолчанию 1000)
    """
    try:
        storage = get_db()

        folder_id = request.args.get('folder_id')
        archived_param = request.args.get('archived', 'active')
        limit = int(request.args.get('limit', 1000))

        archived = None
        if archived_param == 'only_archived':
            archived = True
        elif archived_param == 'active':
            archived = False

        products = storage.get_products(
            folder_id=folder_id,
            archived=archived,
            limit=limit
        )

        return success_response(
            products,
            meta={
                'count': len(products),
                'params': {
                    'folder_id': folder_id,
                    'archived': archived_param,
                    'limit': limit
                }
            }
        )

    except Exception as e:
        logger.error(f"Ошибка получения товаров: {e}")
        return error_response(str(e), 500)


# ========== Stock ==========

@moysklad_bp.route('/stock', methods=['GET'])
@login_required
def get_stock():
    """
    Получить остатки

    Query параметры:
        - store_id: Фильтр по складу
        - limit: Максимум записей (по умолчанию 1000)
    """
    try:
        storage = get_db()

        store_id = request.args.get('store_id')
        limit = int(request.args.get('limit', 1000))

        stock = storage.get_stock(store_id=store_id, limit=limit)

        return success_response(
            stock,
            meta={
                'count': len(stock),
                'params': {
                    'store_id': store_id,
                    'limit': limit
                }
            }
        )

    except Exception as e:
        logger.error(f"Ошибка получения остатков: {e}")
        return error_response(str(e), 500)


# ========== Stores ==========

@moysklad_bp.route('/stores', methods=['GET'])
@login_required
def get_stores():
    """Получить все склады"""
    try:
        storage = get_db()
        stores = storage.get_stores()

        return success_response(stores, meta={'count': len(stores)})

    except Exception as e:
        logger.error(f"Ошибка получения складов: {e}")
        return error_response(str(e), 500)


# ========== Сотрудники и отделы (для owner/group на документах) ==========

@moysklad_bp.route('/employees', methods=['GET'])
@role_required('admin')
def get_employees():
    """Сотрудники МойСклад (для связки с пользователями дашборда — owner на документах)."""
    try:
        from .client import get_client
        client = get_client()
        response = client.get_employees()
        rows = (response or {}).get('rows', [])
        items = [{'id': r.get('id'), 'name': r.get('name'), 'href': r.get('meta', {}).get('href')} for r in rows]
        return success_response(items, meta={'count': len(items)})
    except Exception as e:
        logger.error(f"Ошибка получения сотрудников МойСклад: {e}")
        return error_response(str(e), 500)


@moysklad_bp.route('/groups', methods=['GET'])
@role_required('admin')
def get_groups():
    """Отделы МойСклад (для связки с точками/городами — group на документах)."""
    try:
        from .client import get_client
        client = get_client()
        response = client.get_groups()
        rows = (response or {}).get('rows', [])
        items = [{'id': r.get('id'), 'name': r.get('name'), 'href': r.get('meta', {}).get('href')} for r in rows]
        return success_response(items, meta={'count': len(items)})
    except Exception as e:
        logger.error(f"Ошибка получения отделов МойСклад: {e}")
        return error_response(str(e), 500)


# ========== Sales Orders ==========

@moysklad_bp.route('/sales_orders', methods=['GET'])
@login_required
def get_sales_orders():
    """
    Получить заказы покупателей

    Query параметры:
        - date_from: Начало периода (ISO формат)
        - date_to: Конец периода (ISO формат)
        - limit: Максимум записей (по умолчанию 1000)
    """
    try:
        storage = get_db()

        date_from = request.args.get('date_from')
        date_to = request.args.get('date_to')
        limit = int(request.args.get('limit', 1000))

        orders = storage.get_sales_orders(
            date_from=date_from,
            date_to=date_to,
            limit=limit
        )

        return success_response(
            orders,
            meta={
                'count': len(orders),
                'params': {
                    'date_from': date_from,
                    'date_to': date_to,
                    'limit': limit
                }
            }
        )

    except Exception as e:
        logger.error(f"Ошибка получения заказов: {e}")
        return error_response(str(e), 500)


# ========== Sales Channels ==========

@moysklad_bp.route('/sales_channels', methods=['GET'])
@section_required('abc_analysis')
def get_sales_channels():
    """Получить справочник каналов продаж"""
    try:
        storage = get_db()
        channels = storage.get_sales_channels()

        return success_response(channels, meta={'count': len(channels)})

    except Exception as e:
        logger.error(f"Ошибка получения каналов продаж: {e}")
        return error_response(str(e), 500)


# ========== ABC-анализ ==========

@moysklad_bp.route('/abc-analysis', methods=['GET'])
@section_required('abc_analysis')
def get_abc_analysis():
    """
    ABC-анализ товаров по выручке (заказы в статусе "Выполнен",
    раздел "Товары МС" исключён)

    Query параметры:
        - date_from: Начало периода по дате создания заказа (ISO формат)
        - date_to: Конец периода по дате создания заказа (ISO формат)
        - channel_id: Фильтр по каналу продаж (id из /api/moysklad/sales_channels)
    """
    try:
        storage = get_db()

        date_from = request.args.get('date_from')
        date_to = request.args.get('date_to')
        channel_id = request.args.get('channel_id')

        rows = storage.get_abc_analysis(
            date_from=date_from,
            date_to=date_to,
            channel_id=channel_id
        )

        return success_response(
            rows,
            meta={
                'count': len(rows),
                'params': {
                    'date_from': date_from,
                    'date_to': date_to,
                    'channel_id': channel_id
                }
            }
        )

    except Exception as e:
        logger.error(f"Ошибка расчёта ABC-анализа: {e}")
        return error_response(str(e), 500)


# ========== Stats ==========

@moysklad_bp.route('/stats', methods=['GET'])
@login_required
def get_stats():
    """Получить статистику БД"""
    try:
        storage = get_db()
        stats = storage.get_stats()

        return success_response(stats)

    except Exception as e:
        logger.error(f"Ошибка получения статистики: {e}")
        return error_response(str(e), 500)


# ========== Синхронизация с МойСклад (заказы для ABC-анализа) ==========
#
# Статус синхронизации читается/пишется через таблицу sync_log (файл на диске),
# а НЕ через in-memory словарь в процессе. Реальный инцидент: на Amvera 2 воркера
# gunicorn (amvera.yml, --workers 2) — фоновый поток синхронизации стартовал на
# одном воркере, а опрос /sync-status от фронтенда мог попасть на другой воркер
# с чистым дефолтным статусом (running=False, message=''), показывая ложный ✅
# сразу после запуска, пока реальная синхронизация ещё шла (или уже зависла —
# см. таймаут в client.py) на первом воркере.
#
# Также заказы сохраняются постранично по мере загрузки, а не одним блоком в
# конце: при 28К+ заказов и синхронизации на ~30+ минут любое прерывание процесса
# (зависший запрос, перезапуск воркера) при старой схеме "сначала весь список в
# память, потом сохранить" теряло вообще весь прогресс — сохранялось 0 записей
# без единой ошибки, что и произошло на первом прогоне на проде.

# Сколько минут без движения счётчика считаем признаком мёртвой синхронизации.
# Одна страница — 100 заказов и пауза 0.3 с, при живом синке прогресс обновляется
# раз в несколько секунд; 15 минут тишины означают, что поток не пережил
# перезапуск воркера или деплой.
SYNC_STALE_MINUTES = 15

# --- Инкрементальная синхронизация ---
#
# Раньше каждый прогон качал все заказы за последние 6 месяцев (filter=created>=...),
# то есть ~28К заказов и 20–40 минут — при том, что реально изменились из них единицы.
# Теперь основной режим — incremental: filter=updated>=<курсор>. У заказа, которому
# сменили статус на «Выполнен», обновляется updated, так что ABC-анализ видит его так же,
# как раньше. Полный прогон остался для первичного наполнения и еженедельной сверки.
#
# Курсор хранится ровно в том виде, в каком updated приходит из API, то есть в часовом
# поясе аккаунта МойСклад. Это сознательно: прод на Amvera живёт в UTC, и любая попытка
# сравнить локальное время сервера с временем МС промахнулась бы на 3 часа, молча теряя
# заказы. Максимум из загруженных строк такой ошибки не допускает по построению.
ORDERS_SYNC_LOCK = 'orders_sync'
SYNC_CURSOR_KEY = 'orders_updated_cursor'
FULL_SYNC_AT_KEY = 'orders_full_sync_at'
CURSOR_BOOTSTRAP_KEY = 'orders_cursor_bootstrap_done'

# Лок берётся на прогон и продлевается после каждой страницы. TTL нужен на случай,
# когда держатель умер вместе с воркером (деплой) и release не позвал никто.
ORDERS_SYNC_LOCK_TTL = 20 * 60

# Заказ, изменённый в момент прогона, может не попасть в уже пройденные страницы —
# поэтому следующий прогон стартует чуть раньше курсора. Повторы безопасны:
# сохранение идёт через INSERT OR REPLACE.
CURSOR_OVERLAP_MINUTES = 15

FULL_SYNC_PERIOD_DAYS = 182
MS_DATETIME_FORMATS = ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S')


def _sync_log_is_stale(log: dict) -> bool:
    """Синхронизация числится запущенной, но прогресс давно не двигался"""
    last_tick = log.get('progress_at') or log.get('started_at')
    if not last_tick:
        return True
    try:
        # CURRENT_TIMESTAMP в SQLite пишется как UTC-строка 'YYYY-MM-DD HH:MM:SS'
        tick = datetime.fromisoformat(str(last_tick))
    except ValueError:
        return True
    return (datetime.utcnow() - tick) > timedelta(minutes=SYNC_STALE_MINUTES)


def _parse_ms_datetime(value: str):
    """Разобрать дату МойСклад ('2026-08-21 14:30:00.000'); None, если формат чужой"""
    if not value:
        return None
    for fmt in MS_DATETIME_FORMATS:
        try:
            return datetime.strptime(str(value), fmt)
        except ValueError:
            continue
    return None


def _shift_ms_datetime(value: str, minutes: int) -> str:
    """Сдвинуть дату МойСклад назад на minutes (для overlap курсора)"""
    parsed = _parse_ms_datetime(value)
    if not parsed:
        return str(value)
    return (parsed - timedelta(minutes=minutes)).strftime('%Y-%m-%d %H:%M:%S')


def _get_sync_cursor(storage) -> Optional[str]:
    """
    Курсор инкрементальной синхронизации; None — инкремент невозможен.

    Если курсора ещё нет (первый запуск после внедрения), но база уже наполнена
    прошлым полным синком — берём максимум updated прямо из неё. Иначе пришлось бы
    гонять полный 40-минутный прогон ради данных, которые уже лежат на диске.
    """
    cursor = storage.get_sync_state(SYNC_CURSOR_KEY)
    if cursor:
        return cursor

    # Попытка ровно одна: MAX(json_extract(...)) — это полный скан sales_orders
    # с разбором JSON каждой строки. Если json_extract недоступен или поля нет,
    # без маркера планировщик гонял бы этот скан каждые 30 минут вхолостую.
    if storage.get_sync_state(CURSOR_BOOTSTRAP_KEY):
        return None

    bootstrap = storage.get_max_order_updated()
    storage.set_sync_state(CURSOR_BOOTSTRAP_KEY, datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'))
    if bootstrap:
        storage.set_sync_state(SYNC_CURSOR_KEY, bootstrap)
        logger.info(f"Курсор синхронизации восстановлен из БД: {bootstrap}")
    return bootstrap


def _sync_orders(mode: str = 'incremental', date_from: str = None, max_items: int = 100000) -> dict:
    """
    Один прогон синхронизации заказов. Лок должен быть уже захвачен вызывающим.

    mode:
        incremental — заказы с updated >= курсора (обычный режим, секунды)
        full        — заказы с created >= date_from (первичное наполнение и сверка)

    Заказы сохраняются постранично, а не одним блоком в конце: при 28К заказов и
    прогоне на полчаса любое прерывание процесса (зависший запрос, перезапуск
    воркера) при схеме «сначала весь список в память» теряло вообще весь прогресс —
    сохранялось 0 записей без единой ошибки, что и произошло на первом прогоне на проде.
    """
    storage = get_db()

    cursor_value = _get_sync_cursor(storage) if mode == 'incremental' else None
    if mode == 'incremental' and not cursor_value:
        # Синхронизировать «с прошлого раза» не от чего — нужен полный прогон
        logger.info("Курсор синхронизации отсутствует — переключаемся на полный прогон")
        mode = 'full'

    if mode == 'full' and not date_from:
        date_from = (datetime.now() - timedelta(days=FULL_SYNC_PERIOD_DAYS)).strftime('%Y-%m-%d')

    log_id = storage.start_sync_log('sales_orders')
    saved_count = 0
    max_updated = None

    try:
        from .fetcher import get_fetcher
        from .client import get_client
        fetcher = get_fetcher()
        client = get_client()

        # Справочники — только при полном прогоне. Склады, папки и каналы продаж
        # меняются раз в месяц, тянуть их каждые полчаса ради ABC незачем.
        if mode == 'full':
            for entity, label in [
                ('stores', 'складов'),
                ('folders', 'папок'),
                ('sales_channels', 'каналов продаж'),
            ]:
                items = fetcher.get_full_entity_data(entity, max_items=1000)
                if items is None:
                    raise Exception(f'Не удалось загрузить {label}')
                save_method = {
                    'stores': storage.save_stores,
                    'folders': lambda rows: sum(1 for r in rows if storage.save_folder(r)),
                    'sales_channels': storage.save_sales_channels,
                }[entity]
                save_method(items)

        if mode == 'incremental':
            since = _shift_ms_datetime(cursor_value, CURSOR_OVERLAP_MINUTES)
            query_filter = {'filter': f'updated>={since}', 'order': 'updated,asc'}
        else:
            query_filter = {'filter': f'created>={date_from} 00:00:00', 'order': 'created,asc'}
        # order задан явно: при offset-пагинации по меняющимся данным без
        # фиксированного порядка заказ может «переехать» между страницами и
        # выпасть из выборки целиком.

        offset = 0
        batch_size = 100  # МойСклад не разворачивает positions.rows при limit > 100
        while saved_count < max_items:
            response = client.get_sales_orders(
                limit=batch_size,
                offset=offset,
                filter=query_filter,
                expand='positions,positions.assortment,state'
            )
            if response is None:
                raise Exception('Ошибка запроса заказов к МойСклад API')

            rows = response.get('rows', [])
            if not rows:
                break

            # Вся страница — одной транзакцией (см. save_sales_orders_batch)
            storage.save_sales_orders_batch(rows)
            saved_count += len(rows)
            offset += batch_size

            page_max_updated = max((r.get('updated') or '' for r in rows), default='')
            if page_max_updated > (max_updated or ''):
                max_updated = page_max_updated

            # В инкрементальном режиме страницы отсортированы по updated возрастанию,
            # поэтому курсор можно двигать сразу: всё до max_updated уже сохранено, и
            # обрыв на середине не заставит начинать сначала. В полном режиме порядок
            # по created, updated немонотонен — курсор ставим только по успеху.
            if mode == 'incremental' and max_updated:
                storage.set_sync_state(SYNC_CURSOR_KEY, max_updated)

            storage.update_sync_log_progress(log_id, saved_count)
            storage.renew_sync_lock(ORDERS_SYNC_LOCK, ORDERS_SYNC_LOCK_TTL)

            if len(rows) < batch_size:
                break

            # Пауза между страницами. Синхронизация идёт фоновым потоком
            # внутри воркера gunicorn, который параллельно обслуживает сайт:
            # без паузы поток непрерывно держит GIL на разборе JSON и очередь
            # к диску на записи, и всё остальное (авторизация, дашборд)
            # начинает заметно тормозить.
            time.sleep(0.3)

        if mode == 'full':
            if max_updated:
                storage.set_sync_state(SYNC_CURSOR_KEY, max_updated)
            storage.set_sync_state(FULL_SYNC_AT_KEY, datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'))

        storage.finish_sync_log(log_id, records_count=saved_count, status='completed')
        logger.info(f"Синхронизация заказов ({mode}) завершена: {saved_count} заказов")
        return {'mode': mode, 'saved': saved_count}

    except Exception as e:
        storage.finish_sync_log(log_id, records_count=saved_count, status='failed', error_message=str(e))
        logger.error(f"Ошибка синхронизации МойСклад ({mode}): {e}")
        raise


def _run_orders_sync(mode: str = 'incremental', date_from: str = None, max_items: int = 100000) -> bool:
    """
    Прогон под локом. False — синхронизация уже идёт (в этом или соседнем воркере).
    """
    storage = get_db()
    if not storage.try_acquire_sync_lock(ORDERS_SYNC_LOCK, ORDERS_SYNC_LOCK_TTL):
        logger.info("Синхронизация заказов уже идёт — пропускаем запуск")
        return False

    try:
        _sync_orders(mode=mode, date_from=date_from, max_items=max_items)
    except Exception:
        pass  # уже залогировано и записано в sync_log
    finally:
        storage.release_sync_lock(ORDERS_SYNC_LOCK)

    return True


@moysklad_bp.route('/sync', methods=['POST'])
@role_required('admin')
def trigger_sync():
    """
    Запустить синхронизацию заказов из МойСклад в фоновом потоке.

    Body params:
        - mode (str): incremental (по умолчанию, секунды) | full (полный ресинк, десятки минут)
        - date_from (str): нижняя граница created для full, YYYY-MM-DD (по умолчанию — 6 месяцев назад)
        - max_items (int): максимум заказов для загрузки (default 100000)

    Полная история на момент внедрения ABC-анализа — 75К+ заказов (~110 мин), 6 месяцев
    покрывают 28К заказов (~40 мин) и достаточно для большинства отчётов.
    """
    storage = get_db()
    last_log = storage.get_latest_sync_log('sales_orders')

    # Прерванный синк (перезапуск воркера, деплой) навсегда оставлял бы в логе
    # статус 'started' — статус на странице показывал бы вечный спиннер.
    # Закрываем такую запись как failed. Защита от параллельного запуска — не здесь,
    # а на локе внутри _run_orders_sync: проверка статуса гоночная по своей природе.
    if last_log and last_log['status'] == 'started' and _sync_log_is_stale(last_log):
        storage.finish_sync_log(
            last_log['id'],
            records_count=last_log['records_count'] or 0,
            status='failed',
            error_message='Синхронизация прервана (перезапуск сервера)'
        )

    data = request.get_json(silent=True) or {}
    mode = 'full' if data.get('mode') == 'full' else 'incremental'
    date_from = data.get('date_from')

    # date_from уходит в строку фильтра запроса к МойСклад — принимаем только
    # календарную дату, а не произвольный текст из тела запроса
    if date_from and not re.fullmatch(r'\d{4}-\d{2}-\d{2}', str(date_from)):
        return error_response('date_from должен быть в формате YYYY-MM-DD')

    try:
        max_items = int(data.get('max_items', 100000))
    except (TypeError, ValueError):
        return error_response('max_items должен быть числом')
    max_items = max(1, min(max_items, 1000000))

    def sync_in_background():
        _run_orders_sync(mode=mode, date_from=date_from, max_items=max_items)

    thread = threading.Thread(target=sync_in_background, daemon=True)
    thread.start()

    return jsonify({
        'success': True,
        'message': 'Полная пересинхронизация запущена' if mode == 'full' else 'Синхронизация запущена',
        'mode': mode,
        # id лога на момент запуска: инкрементальный прогон укладывается в пару
        # секунд, поэтому первый же опрос статуса мог застать ещё не начавшийся
        # синк, увидеть прошлый завершённый лог и отрапортовать ложное «готово».
        # Фронтенд ждёт лога с id больше этого.
        'prev_log_id': last_log['id'] if last_log else 0,
    })


@moysklad_bp.route('/sync-status', methods=['GET'])
@login_required
def get_sync_status():
    """
    Получить статус синхронизации заказов (из sync_log — общий для всех воркеров).

    Читается ровно одна строка sync_log по индексу. get_stats() здесь сознательно
    не вызывается: шесть COUNT(*) с полным сканом таблиц на каждый опрос статуса —
    это была ощутимая часть нагрузки, тормозившей сайт во время синхронизации.
    Прогресс пишет сам фоновый поток через update_sync_log_progress().
    """
    storage = get_db()
    last_log = storage.get_latest_sync_log('sales_orders')

    started = bool(last_log and last_log['status'] == 'started')
    stale = started and _sync_log_is_stale(last_log)
    running = started and not stale
    error = last_log['error_message'] if last_log and last_log['status'] == 'failed' else None
    if stale:
        # Поток не пережил перезапуск воркера — не крутим спиннер вечно
        error = 'Синхронизация прервана (перезапуск сервера), запустите заново'

    if running:
        message = f"Загружено {last_log['records_count'] or 0} заказов..."
    elif error:
        message = f"Ошибка: {error}"
    elif last_log:
        message = f"Синхронизировано {last_log['records_count']} заказов"
    else:
        message = ''

    # last_success — время последнего успешного прогона, а не последнего вообще:
    # именно оно отвечает на вопрос «насколько свежие данные в отчёте».
    last_success = storage.get_latest_sync_log('sales_orders', status='completed')

    return jsonify({
        'success': True,
        'status': {
            'running': running,
            'error': error,
            'message': message,
            'log_id': last_log['id'] if last_log else 0,
            'last_sync': last_log['finished_at'] if last_log else None,
            'last_success': last_success['finished_at'] if last_success else None,
        }
    })


# ========== Планировщик: синхронизация без участия человека ==========
#
# Интервал 30 минут: инкрементальный прогон за это время набирает считанные заказы,
# то есть одну страницу и несколько секунд работы — нагрузки на сайт практически нет.
SCHEDULER_INTERVAL_SECONDS = 30 * 60

# Задержка перед первым прогоном: на старте воркера и так идут миграции и прогрев,
# добавлять туда же сетевые запросы к МойСклад незачем.
#
# 420, а не 120 как у курьеров: интервал у обоих одинаковый (30 минут), и при
# равной задержке они стартовали одной волной и оставались синхронными до
# рестарта — оба писали в /data одновременно, а этот диск общий с barhat.db,
# на которой держится авторизация всего сайта. Разные задержки разводят
# прогоны на 5 минут навсегда.
SCHEDULER_START_DELAY_SECONDS = 420

# Инкремент по updated не видит заказы, удалённые в МойСклад: у удалённой записи
# ничего не обновляется, её просто больше нет в выдаче. Полный прогон раз в неделю
# приводит локальную базу в соответствие с источником.
FULL_RESYNC_INTERVAL_DAYS = 7

# Полный прогон — это десятки минут фоновой нагрузки, поэтому только ночью.
# Прод живёт в UTC, салоны — в UTC+5/+7, так что 21:00–23:59 UTC это глубокая ночь
# и в Москве, и на востоке.
FULL_RESYNC_NIGHT_HOURS_UTC = (21, 22, 23)

_scheduler_started = False


def _full_resync_due(storage) -> bool:
    """Пора ли гнать еженедельный полный прогон"""
    last_full = storage.get_sync_state(FULL_SYNC_AT_KEY)
    if not last_full:
        return True
    parsed = _parse_ms_datetime(last_full)
    if not parsed:
        return True
    return (datetime.utcnow() - parsed) > timedelta(days=FULL_RESYNC_INTERVAL_DAYS)


def _scheduled_mode(storage) -> Optional[str]:
    """
    Какой прогон запускать сейчас; None — не запускать ничего.

    Полный прогон допускается только ночью: если базу ещё ни разу не наполняли,
    сорокаминутный синк, стартовавший днём в разгар работы салонов, положил бы сайт.
    """
    night = datetime.utcnow().hour in FULL_RESYNC_NIGHT_HOURS_UTC

    if not _get_sync_cursor(storage):
        return 'full' if night else None

    if night and _full_resync_due(storage):
        return 'full'

    return 'incremental'


# ============================================================================
# Движение товара по складам (оприходование/списание) для показателей салонов
# ============================================================================

WAREHOUSE_SYNC_LOCK = 'warehouse-flows'
WAREHOUSE_SYNC_LOCK_TTL = 900
# Реже, чем заказы: документы склада заводят пачками раз в день, а не поминутно
WAREHOUSE_SYNC_INTERVAL_SECONDS = 3 * 60 * 60
# Окно пересобирается кусками: DELETE+INSERT одного куска атомарен, поэтому
# отчёт никогда не видит период, где приход уже удалён, а списание не записано
WAREHOUSE_CHUNK_DAYS = 15
MAX_MANUAL_WAREHOUSE_DAYS = 800


def run_warehouse_sync(days: int = None) -> dict:
    """
    Пересобрать движение товара за последние `days` дней.

    Возвращает счётчики документов; при занятом локе — {'skipped': True}.
    """
    from datetime import date, timedelta
    from .client import get_client

    storage = get_db()
    if not storage.try_acquire_sync_lock(WAREHOUSE_SYNC_LOCK, WAREHOUSE_SYNC_LOCK_TTL):
        logger.info("Синхронизация движения товара уже идёт — пропускаем")
        return {'skipped': True}

    days = days or warehouse.SYNC_WINDOW_DAYS
    log_id = storage.start_sync_log('warehouse_flows')
    totals = {'enter': 0, 'loss': 0, 'positions': 0}

    try:
        warehouse.init_warehouse_tables(storage)
        warehouse.seed_groups(storage)

        client = get_client()
        today = date.today()
        start = today - timedelta(days=days)

        cursor = start
        while cursor <= today:
            chunk_end = min(cursor + timedelta(days=WAREHOUSE_CHUNK_DAYS - 1), today)
            counts = warehouse.sync_window(
                storage, client, cursor.isoformat(), chunk_end.isoformat()
            )
            for key in totals:
                totals[key] += counts.get(key, 0)
            storage.update_sync_log_progress(log_id, totals['positions'])
            storage.renew_sync_lock(WAREHOUSE_SYNC_LOCK, WAREHOUSE_SYNC_LOCK_TTL)
            cursor = chunk_end + timedelta(days=1)

        storage.finish_sync_log(log_id, totals['positions'], 'completed')
        return totals
    except Exception as e:
        logger.error(f"Ошибка синхронизации движения товара: {e}")
        storage.finish_sync_log(log_id, totals['positions'], 'failed', str(e))
        raise
    finally:
        storage.release_sync_lock(WAREHOUSE_SYNC_LOCK)


@moysklad_bp.route('/warehouse/sync', methods=['POST'])
@role_required('admin')
def trigger_warehouse_sync():
    """
    Загрузить движение товара за последние N дней.

    Body: {"days": 365} — для первичного наполнения витрины.
    """
    data = request.get_json(silent=True) or {}
    try:
        days = max(1, min(int(data.get('days', warehouse.SYNC_WINDOW_DAYS)),
                          MAX_MANUAL_WAREHOUSE_DAYS))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'days должен быть числом'}), 400

    thread = threading.Thread(
        target=lambda: run_warehouse_sync(days), daemon=True, name='moysklad-warehouse-sync'
    )
    thread.start()
    return jsonify({'success': True, 'message': f'Загрузка движения товара за {days} дн. запущена'})


@moysklad_bp.route('/warehouse/groups', methods=['GET'])
@role_required('admin')
def get_warehouse_groups():
    """Папки каталога, отнесённые к «цветку» и «клубнике»."""
    storage = get_db()
    warehouse.init_warehouse_tables(storage)
    return jsonify({
        'success': True,
        'groups': warehouse.list_groups(storage),
        'folders': [
            {'id': fid, 'path': path}
            for fid, path in sorted(warehouse.folder_paths(storage).items(), key=lambda x: x[1])
        ],
    })


@moysklad_bp.route('/warehouse/groups', methods=['POST'])
@role_required('admin')
def set_warehouse_group():
    """
    Отнести папку каталога к группе.

    Body: {"folder_id": str, "kind": "flower"|"berry"|null, "qty_per_kg": float|null}
    """
    data = request.get_json(silent=True) or {}
    folder_id = (data.get('folder_id') or '').strip()
    if not folder_id:
        return jsonify({'success': False, 'error': 'Не передан folder_id'}), 400

    kind = data.get('kind')
    if kind not in (None, warehouse.KIND_FLOWER, warehouse.KIND_BERRY):
        return jsonify({'success': False, 'error': f'Неизвестная группа: {kind}'}), 400

    qty_per_kg = data.get('qty_per_kg')
    if qty_per_kg is not None:
        try:
            qty_per_kg = float(qty_per_kg)
            if qty_per_kg <= 0:
                raise ValueError
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': 'qty_per_kg должен быть положительным числом'}), 400

    storage = get_db()
    warehouse.init_warehouse_tables(storage)
    paths = warehouse.folder_paths(storage)
    warehouse.set_group(storage, folder_id, kind, qty_per_kg, paths.get(folder_id))
    return jsonify({'success': True, 'groups': warehouse.list_groups(storage)})


def _scheduler_loop():
    """Фоновый цикл синхронизации (по одному в каждом воркере, работает — один)"""
    time.sleep(SCHEDULER_START_DELAY_SECONDS)

    while True:
        try:
            # Талон на тик берётся ДО выбора режима и до всякой работы: лок
            # внутри _run_orders_sync ловит только одновременный прогон, а тики
            # двух воркеров разъезжаются во времени, и второй повторял работу
            # первого целиком (см. try_claim_scheduled_run).
            if not get_db().try_claim_scheduled_run('orders', SCHEDULER_INTERVAL_SECONDS):
                logger.info("Тик синхронизации МойСклад уже отработал соседний воркер")
            else:
                mode = _scheduled_mode(get_db())
                if mode:
                    # Лок внутри _run_orders_sync остаётся: он не даёт планировщику
                    # влезть в ручную синхронизацию, запущенную админом с дашборда.
                    _run_orders_sync(mode=mode)
        except Exception as e:
            logger.error(f"Ошибка планировщика синхронизации МойСклад: {e}")

        # Второй шаг того же тика — движение товара по складам (оприходование и
        # списание) для раздела «Показатели салонов». Своего планировщика не
        # заводим: в moysklad.db уже пишет этот цикл, а /data общий на все базы,
        # и вторая волна записи туда однажды уже клала сайт.
        try:
            if get_db().try_claim_scheduled_run('warehouse', WAREHOUSE_SYNC_INTERVAL_SECONDS):
                run_warehouse_sync()
        except Exception as e:
            logger.error(f"Ошибка синхронизации движения товара: {e}")

        time.sleep(SCHEDULER_INTERVAL_SECONDS)


def start_sync_scheduler() -> None:
    """
    Запустить фоновую синхронизацию заказов.

    Вызывается из pyrus/server.py после регистрации blueprint. Отключается
    переменной MOYSKLAD_SYNC_SCHEDULER=0 (локальная разработка: не хочется, чтобы
    каждый запуск сервера лез в боевой МойСклад).
    """
    global _scheduler_started

    if os.getenv('MOYSKLAD_SYNC_SCHEDULER', '1') != '1':
        logger.info("Планировщик синхронизации МойСклад отключён (MOYSKLAD_SYNC_SCHEDULER=0)")
        return

    if _scheduler_started:
        return
    _scheduler_started = True

    thread = threading.Thread(target=_scheduler_loop, daemon=True, name='moysklad-sync-scheduler')
    thread.start()
    logger.info(
        f"Планировщик синхронизации МойСклад запущен "
        f"(интервал {SCHEDULER_INTERVAL_SECONDS // 60} мин)"
    )

    start_index_maintenance()


# Задержка перед постройкой индексов. Операция разовая, но тяжёлая: читает
# гигабайтные таблицы целиком. Пусть воркер сначала спокойно поднимется и
# отдаст первые страницы людям.
INDEX_BUILD_DELAY_SECONDS = 90

# Талон, чтобы индексы строил ОДИН воркер, а не оба разом. Сутки — с запасом:
# после успешной постройки CREATE INDEX IF NOT EXISTS отрабатывает мгновенно,
# так что повторные заходы ничего не стоят.
INDEX_BUILD_CLAIM_SECONDS = 24 * 60 * 60

_index_maintenance_started = False


def _index_maintenance_loop() -> None:
    time.sleep(INDEX_BUILD_DELAY_SECONDS)
    try:
        storage = get_db()
        if not storage.try_claim_scheduled_run('reporting_indexes', INDEX_BUILD_CLAIM_SECONDS):
            logger.info("Индексы отчётов проверяет соседний воркер")
            return
        if storage.ensure_reporting_indexes():
            logger.info("Индексы отчётов МойСклад достроены")
    except Exception as e:
        # Отчёты без индексов работают, просто медленно. Ронять воркер нельзя.
        logger.error(f"Не удалось построить индексы отчётов МойСклад: {e}")


def start_index_maintenance() -> None:
    """Достроить покрывающие индексы под ABC-анализ — фоном, один раз.

    Без них ABC читал с диска ~1 ГБ вместо ~11 МБ (в таблицах лежит raw_data
    от МойСклада, 21 и 9 КБ на строку) и занимал 24-57 секунд, подвешивая на
    это время весь сайт: диск /data общий для всех баз. Подробности — в
    MoySkladStorage.ensure_reporting_indexes.

    Отдельным потоком, а не в _init_db: построение читает таблицы целиком, и
    в горячем пути это повесило бы первый запрос к модулю.
    """
    global _index_maintenance_started

    if _index_maintenance_started:
        return
    _index_maintenance_started = True

    thread = threading.Thread(
        target=_index_maintenance_loop, daemon=True, name='moysklad-index-maintenance'
    )
    thread.start()


# ========== Синхронизация каталога товаров (для справочников) ==========
# /sync выше тянет заказы для ABC-анализа и никогда не трогал products.
#
# Остатки (stock) сюда сознательно не включены: writeoffs/server.py
# get_catalog() запрашивает остатки у МойСклад напрямую, по одному складу
# за раз (report/stock/all?filter=store=<href>) — это быстро и всегда
# актуально. Локальная синхронизация stock в этот момент оказалась ещё и
# сломана (storage.save_stock падал на каждой строке — расхождение числа
# колонок/значений в INSERT, плюс /report/stock/all без фильтра по складу
# вообще не отдаёт store_id на верхнем уровне), чинить её ради устаревающего
# кэша смысла не было — см. plans/2026-08-16-stock-writeoffs-module.md, Фаза 7.

catalog_sync_status = {
    'running': False,
    'message': '',
    'last_sync': None,
    'error': None,
}


@moysklad_bp.route('/sync-catalog', methods=['POST'])
@role_required('admin')
def trigger_catalog_sync():
    """Запустить синхронизацию товаров в фоновом потоке."""
    global catalog_sync_status

    if catalog_sync_status['running']:
        return jsonify({
            'success': False,
            'error': 'Синхронизация каталога уже запущена',
            'status': catalog_sync_status
        })

    data = request.get_json(silent=True) or {}
    max_items = data.get('max_items', 10000)

    def sync_catalog_in_background():
        global catalog_sync_status
        try:
            catalog_sync_status['running'] = True
            catalog_sync_status['error'] = None
            catalog_sync_status['message'] = 'Загрузка товаров...'

            scripts_path = os.path.join(os.path.dirname(__file__), '../../scripts')
            if scripts_path not in sys.path:
                sys.path.insert(0, scripts_path)
            from update_moysklad import update_entity

            from .fetcher import get_fetcher
            fetcher = get_fetcher()
            storage = get_db()

            ok = update_entity('products', fetcher, storage, max_items=max_items)
            if not ok:
                catalog_sync_status['error'] = 'Не удалось загрузить товары'
                catalog_sync_status['message'] = catalog_sync_status['error']
            else:
                catalog_sync_status['message'] = 'Товары синхронизированы'
                catalog_sync_status['last_sync'] = datetime.now().isoformat()

        except Exception as e:
            catalog_sync_status['error'] = str(e)
            catalog_sync_status['message'] = f'Ошибка: {e}'
            logger.error(f"Ошибка синхронизации каталога МойСклад: {e}")
        finally:
            catalog_sync_status['running'] = False

    thread = threading.Thread(target=sync_catalog_in_background)
    thread.daemon = True
    thread.start()

    return jsonify({
        'success': True,
        'message': 'Синхронизация каталога запущена',
        'status': catalog_sync_status
    })


@moysklad_bp.route('/sync-catalog-status', methods=['GET'])
@login_required
def get_catalog_sync_status():
    """Получить статус синхронизации товаров"""
    return jsonify({
        'success': True,
        'status': catalog_sync_status
    })


# ========== Standalone-режим (локальная разработка) ==========

def _build_standalone_app() -> Flask:
    """Собрать отдельное Flask-приложение с этим blueprint'ом (без auth-обвязки
    мастер-приложения) — только для локального запуска `python -m moysklad.server`."""
    app = Flask(__name__)

    @app.route('/health', methods=['GET'])
    def health():
        return jsonify({
            'status': 'ok',
            'service': 'moysklad-api',
            'timestamp': datetime.now().isoformat()
        })

    app.register_blueprint(moysklad_bp)
    return app


def main():
    """Запуск standalone-сервера для локальной разработки"""
    host = os.getenv('MOYSKLAD_HOST', '0.0.0.0')
    port = int(os.getenv('MOYSKLAD_PORT', 5001))

    logger.info(f"Запуск MoySklad API сервера на {host}:{port}")
    logger.info(f"База данных: {DB_PATH}")

    storage = get_db()
    stats = storage.get_stats()
    logger.info(f"Статистика БД: {stats}")

    app = _build_standalone_app()
    app.run(host=host, port=port, debug=False)


if __name__ == '__main__':
    main()
