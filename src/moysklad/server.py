"""
МойСклад JSON API
Blueprint для доступа к данным МойСклад (товары, остатки, заказы, ABC-анализ)

Регистрируется в src/pyrus/server.py (мастер Flask-приложение) — см. паттерн
cashshifts_bp / invoices_bp. Ниже также есть standalone-режим для локальной
разработки (python -m moysklad.server).
"""

import os
import sys
import logging
import threading
from datetime import datetime
from typing import Any
from flask import Blueprint, Flask, jsonify, request
from flask_login import login_required
from dotenv import load_dotenv

# Импортируем модуль авторизации (как в cashshifts/server.py)
auth_path = os.path.join(os.path.dirname(__file__), '../')
sys.path.insert(0, auth_path)
from auth import section_required, role_required

from .storage import get_storage

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


# ========== Синхронизация с МойСклад ==========

sync_status = {
    'running': False,
    'message': '',
    'last_sync': None,
    'error': None,
}


@moysklad_bp.route('/sync', methods=['POST'])
@role_required('admin')
def trigger_sync():
    """
    Запустить синхронизацию данных из МойСклад (склады, папки, каналы продаж,
    заказы покупателей с позициями) в фоновом потоке.

    Body params:
        - max_items (int): максимум заказов для загрузки (default 10000)
    """
    global sync_status

    if sync_status['running']:
        return jsonify({
            'success': False,
            'error': 'Синхронизация уже запущена',
            'status': sync_status
        })

    data = request.get_json(silent=True) or {}
    max_items = data.get('max_items', 10000)

    def sync_in_background():
        global sync_status
        try:
            sync_status['running'] = True
            sync_status['error'] = None
            sync_status['message'] = 'Запуск синхронизации...'

            from .fetcher import get_fetcher
            fetcher = get_fetcher()
            storage = get_db()

            for entity, label in [
                ('stores', 'складов'),
                ('folders', 'папок'),
                ('sales_channels', 'каналов продаж'),
            ]:
                sync_status['message'] = f'Загрузка {label}...'
                items = fetcher.get_full_entity_data(entity, max_items=1000)
                if items is None:
                    raise Exception(f'Не удалось загрузить {label}')
                save_method = {
                    'stores': storage.save_stores,
                    'folders': lambda rows: sum(1 for r in rows if storage.save_folder(r)),
                    'sales_channels': storage.save_sales_channels,
                }[entity]
                save_method(items)

            sync_status['message'] = 'Загрузка заказов покупателей...'
            orders = fetcher.get_full_entity_data(
                'sales_orders',
                max_items=max_items,
                expand='positions,positions.assortment,state'
            )
            if orders is None:
                raise Exception('Не удалось загрузить заказы')

            count = sum(1 for o in orders if storage.save_sales_order(o))
            sync_status['message'] = f'Синхронизировано {count}/{len(orders)} заказов'
            sync_status['last_sync'] = datetime.now().isoformat()

        except Exception as e:
            sync_status['error'] = str(e)
            sync_status['message'] = f'Ошибка: {str(e)}'
            logger.error(f"Ошибка синхронизации МойСклад: {e}")
        finally:
            sync_status['running'] = False

    thread = threading.Thread(target=sync_in_background)
    thread.daemon = True
    thread.start()

    return jsonify({
        'success': True,
        'message': 'Синхронизация запущена',
        'status': sync_status
    })


@moysklad_bp.route('/sync-status', methods=['GET'])
@login_required
def get_sync_status():
    """Получить статус синхронизации"""
    return jsonify({
        'success': True,
        'status': sync_status
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
