"""
МойСklad JSON API Server
Flask сервер для доступа к данным МойСклад
"""

import os
import logging
from datetime import datetime
from typing import Any
from flask import Flask, jsonify, request
from dotenv import load_dotenv
from .storage import get_storage

load_dotenv()

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Создаём Flask приложение
app = Flask(__name__)

# Конфигурация
DB_PATH = os.getenv('MOYSKLAD_DB_PATH', 'data/moysklad.db')
HOST = os.getenv('MOYSKLAD_HOST', '0.0.0.0')
PORT = int(os.getenv('MOYSKLAD_PORT', 5001))


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


# ========== Health check ==========

@app.route('/health', methods=['GET'])
def health():
    """Проверка здоровья"""
    return jsonify({
        'status': 'ok',
        'service': 'moysklad-api',
        'timestamp': datetime.now().isoformat()
    })


# ========== Products ==========

@app.route('/api/moysklad/products', methods=['GET'])
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

@app.route('/api/moysklad/stock', methods=['GET'])
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

@app.route('/api/moysklad/stores', methods=['GET'])
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

@app.route('/api/moysklad/sales_orders', methods=['GET'])
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

@app.route('/api/moysklad/sales_channels', methods=['GET'])
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

@app.route('/api/moysklad/abc-analysis', methods=['GET'])
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

@app.route('/api/moysklad/stats', methods=['GET'])
def get_stats():
    """Получить статистику БД"""
    try:
        storage = get_db()
        stats = storage.get_stats()

        return success_response(stats)

    except Exception as e:
        logger.error(f"Ошибка получения статистики: {e}")
        return error_response(str(e), 500)


# ========== Main ==========

def main():
    """Запуск сервера"""
    logger.info(f"Запуск MoySklad API сервера на {HOST}:{PORT}")
    logger.info(f"База данных: {DB_PATH}")

    # Проверяем что БД доступна
    storage = get_db()
    stats = storage.get_stats()
    logger.info(f"Статистика БД: {stats}")

    app.run(host=HOST, port=PORT, debug=False)


if __name__ == '__main__':
    main()
