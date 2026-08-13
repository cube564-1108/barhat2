"""
Pyrus API Server
Простой HTTP сервер для отдачи данных Pyrus в JSON формате
"""

import os
import sys
import logging
from datetime import datetime
from typing import Optional
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv

# Импорт авторизации
import sys
import os
auth_path = os.path.join(os.path.dirname(__file__), '../')
sys.path.insert(0, auth_path)
from auth import auth_bp, login_manager, init_auth_tables, section_required, role_required

# ОТЛАДКА: показываем откуда запущен
print("=" * 60)
print("SERVER STARTUP DEBUG")
print("=" * 60)
print(f"Current directory: {os.getcwd()}")
print(f"Script file: {__file__}")
print(f"sys.path[0]: {sys.path[0]}")
print("=" * 60)

from .storage import get_storage
from .client import get_client

# Импортируем модуль отчета о качестве
import sys
import os
scripts_path = os.path.join(os.path.dirname(__file__), '../../scripts')
sys.path.insert(0, scripts_path)

# ИСПОЛЬЗУЕМ НОВУЮ ВЕРСИЮ (без кэша)
import quality_report_v2 as quality_report
print(f"Using quality_report_v2 (cache-free version)")

load_dotenv()

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Для Amvera: создаём директорию /data если её нет
DATA_DIR = os.path.dirname(os.getenv('PYRUS_DB_PATH', 'data/pyrus.db'))
if DATA_DIR and not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR, exist_ok=True)
    logger.info(f"Создана директория: {DATA_DIR}")

# Создаём Flask приложение
app = Flask(__name__)

# Конфигурация
app.config['JSON_AS_ASCII'] = False  # Поддержка кириллицы
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = True  # Красивый JSON

# Секретный ключ для сессий из env
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'dev-secret-key-change-in-production')

# Безопасные cookie для сессий
app.config.update(
    SESSION_COOKIE_SECURE=True,      # только HTTPS
    SESSION_COOKIE_HTTPONLY=True,    # недоступна из JS
    SESSION_COOKIE_SAMESITE="Lax",   # защита от CSRF
    PERMANENT_SESSION_LIFETIME=60 * 60 * 8,  # 8 часов
)

# CORS — ограничиваем доменом
CORS(app, supports_credentials=True, origins=[
    "https://barhat2-cube564.amvera.io",
    "http://localhost:5000",  # для локальной разработки
])

# Инициализация авторизации
login_manager.init_app(app)
login_manager.login_view = None  # SPA сам решает, куда редиректить

# Регистрируем auth blueprint
app.register_blueprint(auth_bp)

# Инициализация таблиц авторизации
with app.app_context():
    init_auth_tables()


# Инициализация хранилища
db_path = os.getenv('PYRUS_DB_PATH', 'data/pyrus.db')
storage = get_storage(db_path)


@app.route('/health', methods=['GET'])
def health_check():
    """Проверка здоровья сервера"""
    return jsonify({
        'status': 'ok',
        'timestamp': datetime.now().isoformat(),
        'database': db_path
    })


# === Dashboard Routes ===

# Get directories relative to server.py
SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(SERVER_DIR))
DASHBOARD_DIR = os.path.abspath(os.path.join(SERVER_DIR, '..', 'dashboard'))
BRAND_DIR = os.path.abspath(os.path.join(PROJECT_ROOT, 'brand'))

# Debug: print paths
print(f"SERVER_DIR: {SERVER_DIR}")
print(f"PROJECT_ROOT: {PROJECT_ROOT}")
print(f"DASHBOARD_DIR: {DASHBOARD_DIR}")
print(f"DASHBOARD exists: {os.path.exists(DASHBOARD_DIR)}")
print(f"BRAND_DIR: {BRAND_DIR}")
print(f"BRAND exists: {os.path.exists(BRAND_DIR)}")


@app.route('/')
def index():
    """Главная страница — дашборд (с проверкой авторизации)"""
    try:
        # Проверяем авторизацию
        from flask_login import current_user
        if not current_user.is_authenticated:
            return send_from_directory(DASHBOARD_DIR, 'login.html')
        return send_from_directory(DASHBOARD_DIR, 'index.html')
    except Exception as e:
        logger.error(f"Ошибка загрузки index.html: {e}")
        return f"Ошибка загрузки дашборда: {e}", 500


@app.route('/login')
def login_page():
    """Страница логина"""
    try:
        return send_from_directory(DASHBOARD_DIR, 'login.html')
    except Exception as e:
        logger.error(f"Ошибка загрузки login.html: {e}")
        return f"Ошибка загрузки страницы логина: {e}", 500


# === Static File Routes ===

@app.route('/styles.css')
def serve_styles():
    """Отдаёт стили дашборда"""
    return send_from_directory(DASHBOARD_DIR, 'styles.css')

@app.route('/script.js')
def serve_script():
    """Отдаёт основной скрипт дашборда"""
    return send_from_directory(DASHBOARD_DIR, 'script.js')

@app.route('/bouquet-calculator.js')
def serve_calculator():
    """Отдаёт скрипт калькулятора"""
    return send_from_directory(DASHBOARD_DIR, 'bouquet-calculator.js')

@app.route('/quality-report.js')
def serve_quality():
    """Отдаёт скрипт отчёта по качеству"""
    return send_from_directory(DASHBOARD_DIR, 'quality-report.js')

@app.route('/brand/<path:filename>')
def serve_brand(filename):
    """Отдаёт файлы из директории brand"""
    return send_from_directory(BRAND_DIR, filename)


@app.route('/debug/module', methods=['GET'])
def debug_module():
    """Отладка: какой модуль quality_report загружен"""
    return jsonify({
        'module_file': quality_report.__file__,
        'module_name': quality_report.__name__,
        'get_all_tasks_doc': quality_report.get_all_tasks.__doc__,
        'test_call': len(quality_report.get_all_tasks())
    })


@app.route('/debug/report', methods=['GET'])
def debug_report():
    """Отладка: прямой вызов generate_report"""
    report = quality_report.generate_report()
    return jsonify({
        'module_file': quality_report.__file__,
        'total_tasks': report['total_tasks'],
        'salons_count': len(report['salons'])
    })


@app.route('/debug/report-v2', methods=['GET'])
def debug_report_v2():
    """Отладка: использование НОВОЙ версии модуля"""
    import importlib
    import sys
    import os

    # Принудительно перезагружаем модуль
    scripts_path = os.path.join(os.path.dirname(__file__), '../../scripts')
    if scripts_path not in sys.path:
        sys.path.insert(0, scripts_path)

    # Импортируем новую версию
    import quality_report_v2

    report = quality_report_v2.generate_report()
    return jsonify({
        'module': 'quality_report_v2 (NEW)',
        'module_file': quality_report_v2.__file__,
        'total_tasks': report['total_tasks'],
        'salons_count': len(report['salons']),
        'expected': 232,
        'match': report['total_tasks'] == 232
    })


@app.route('/api/pyrus/forms', methods=['GET'])
@section_required('dashboard')
def get_forms():
    """
    Получить список всех форм

    Query params:
        - format: 'full' или 'simple' (default)
    """
    try:
        format_type = request.args.get('format', 'simple')
        forms = storage.get_forms()

        if format_type == 'simple':
            # Только базовые поля
            result = [{'id': f['id'], 'title': f['title']} for f in forms]
        else:
            result = forms

        return jsonify({
            'success': True,
            'count': len(result),
            'data': result
        })

    except Exception as e:
        logger.error(f"Ошибка /api/pyrus/forms: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/pyrus/tasks', methods=['GET'])
@section_required('dashboard')
def get_tasks():
    """
    Получить актуальные задачи

    Query params:
        - form_id: ID формы (опционально)
        - limit: Максимум задач (default 1000)
        - status: Фильтр по статусу (active, finished, archived)
        - date_from: Начало периода (ISO datetime)
        - date_to: Конец периода (ISO datetime)
    """
    try:
        form_id = request.args.get('form_id', type=int)
        limit = request.args.get('limit', 1000, type=int)
        status = request.args.get('status')
        date_from_str = request.args.get('date_from')
        date_to_str = request.args.get('date_to')

        # Парсим даты если указаны
        date_from = None
        date_to = None
        if date_from_str:
            try:
                date_from = datetime.fromisoformat(date_from_str)
            except ValueError:
                return jsonify({'success': False, 'error': 'Invalid date_from format'}), 400

        if date_to_str:
            try:
                date_to = datetime.fromisoformat(date_to_str)
            except ValueError:
                return jsonify({'success': False, 'error': 'Invalid date_to format'}), 400

        # Получаем задачи
        if date_from or date_to:
            # Исторические данные
            tasks = storage.get_tasks_history(
                form_id=form_id,
                date_from=date_from,
                date_to=date_to
            )
        else:
            # Актуальные данные
            tasks = storage.get_latest_tasks(form_id=form_id, limit=limit)

        # Фильтр по статусу
        if status:
            tasks = [t for t in tasks if t.get('status') == status]

        # Парсим raw_data если нужен полный JSON
        include_raw = request.args.get('include_raw', 'false').lower() == 'true'

        result = []
        for task in tasks:
            if include_raw and task.get('raw_data'):
                try:
                    import json
                    result.append(json.loads(task['raw_data']))
                except:
                    result.append(task)
            else:
                # Убираем raw_data для компактности
                task_copy = task.copy()
                task_copy.pop('raw_data', None)
                result.append(task_copy)

        return jsonify({
            'success': True,
            'count': len(result),
            'params': {
                'form_id': form_id,
                'limit': limit,
                'status': status,
                'date_from': date_from_str,
                'date_to': date_to_str,
                'include_raw': include_raw
            },
            'data': result
        })

    except Exception as e:
        logger.error(f"Ошибка /api/pyrus/tasks: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/pyrus/stats', methods=['GET'])
def get_stats():
    """Получить статистику БД"""
    try:
        stats = storage.get_stats()

        return jsonify({
            'success': True,
            'data': stats
        })

    except Exception as e:
        logger.error(f"Ошибка /api/pyrus/stats: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/quality', methods=['GET'])
@section_required('quality')
def get_quality_report():
    """
    Получить отчет о качестве сборки букетов

    Query params:
        - date_from: Начало периода (YYYY-MM-DD)
        - date_to: Конец периода (YYYY-MM-DD)
        - format: 'full' или 'simple' (default)
    """
    try:
        date_from = request.args.get('date_from')
        date_to = request.args.get('date_to')
        format_type = request.args.get('format', 'simple')

        report = quality_report.generate_report(date_from, date_to)

        if format_type == 'simple':
            # Только основные метрики
            result = {
                'period': report['period'],
                'total_tasks': report['total_tasks'],
                'overall_avg': report['overall_avg'],
                'salons': report['salons'],
                'florists': report['florists']
            }
        else:
            result = report

        return jsonify({
            'success': True,
            'data': result
        })

    except Exception as e:
        logger.error(f"Ошибка /api/quality: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/quality/history', methods=['GET'])
@section_required('quality')
def get_quality_history():
    """
    Получить историю качества по месяцам

    Query params:
        - months: Количество месяцев (default 6)
    """
    try:
        months = request.args.get('months', 6, type=int)
        history = quality_report.get_monthly_history(months)

        return jsonify({
            'success': True,
            'data': history
        })

    except Exception as e:
        logger.error(f"Ошибка /api/quality/history: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/quality/salon-history', methods=['GET'])
def get_salon_history():
    """
    Получить историю качества по салону

    Query params:
        - salon: Название салона (обязательно)
        - months: Количество месяцев (default 6)
    """
    try:
        salon = request.args.get('salon')
        if not salon:
            return jsonify({
                'success': False,
                'error': 'salon parameter is required'
            }), 400

        months = request.args.get('months', 6, type=int)
        history = quality_report.get_salon_history(salon, months)

        return jsonify({
            'success': True,
            'data': history
        })

    except Exception as e:
        logger.error(f"Ошибка /api/quality/salon-history: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/quality/salon-order-types', methods=['GET'])
def get_salon_order_types():
    """
    Получить разбивку по видам заказа для салона

    Query params:
        - salon: Название салона (обязательно)
        - date_from: Начальная дата (YYYY-MM-DD)
        - date_to: Конечная дата (YYYY-MM-DD)
    """
    try:
        salon = request.args.get('salon')
        if not salon:
            return jsonify({
                'success': False,
                'error': 'salon parameter is required'
            }), 400

        date_from = request.args.get('date_from')
        date_to = request.args.get('date_to')

        order_types = quality_report.get_salon_order_types(salon, date_from, date_to)

        return jsonify({
            'success': True,
            'data': order_types
        })

    except Exception as e:
        logger.error(f"Ошибка /api/quality/salon-order-types: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/quality/salon-florists', methods=['GET'])
def get_salon_florists():
    """
    Получить статистику по флористам салона

    Query params:
        - salon: Название салона (обязательно)
        - date_from: Начальная дата (YYYY-MM-DD)
        - date_to: Конечная дата (YYYY-MM-DD)
    """
    try:
        salon = request.args.get('salon')
        if not salon:
            return jsonify({
                'success': False,
                'error': 'salon parameter is required'
            }), 400

        date_from = request.args.get('date_from')
        date_to = request.args.get('date_to')

        florists = quality_report.get_salon_florists(salon, date_from, date_to)

        return jsonify({
            'success': True,
            'data': florists
        })

    except Exception as e:
        logger.error(f"Ошибка /api/quality/salon-florists: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/quality/data-coverage', methods=['GET'])
def get_data_coverage():
    """
    Получить информацию о полноте данных по датам

    Query params:
        - date_from: Начальная дата (YYYY-MM-DD)
        - date_to: Конечная дата (YYYY-MM-DD)
        - granularity: 'day' (default) или 'month'

    Возвращает количество задач за каждый период
    """
    try:
        import sqlite3
        import json
        from collections import defaultdict

        date_from = request.args.get('date_from')
        date_to = request.args.get('date_to')
        granularity = request.args.get('granularity', 'day')

        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Получаем все уникальные задачи
        cursor.execute('''
            SELECT raw_data
            FROM tasks t1
            WHERE form_id = 1327961
              AND snapshot_at = (
                SELECT MAX(snapshot_at)
                FROM tasks t2
                WHERE t2.form_id = t1.form_id AND t2.task_id = t1.task_id
              )
        ''')

        # Считаем задачи по датам
        date_counts = defaultdict(int)

        for row in cursor.fetchall():
            try:
                data = json.loads(row['raw_data'])
                for field in data.get('fields', []):
                    if field.get('id') == 1:
                        date_val = field.get('value')
                        if date_val:
                            key = date_val[:7] if granularity == 'month' else date_val
                            date_counts[key] += 1
                        break
            except:
                pass

        conn.close()

        # Фильтрация по датам если указаны
        filtered = {}
        for date, count in sorted(date_counts.items()):
            if date_from and date < date_from:
                continue
            if date_to and date > date_to:
                continue
            filtered[date] = count

        # Статистика
        total_tasks = sum(filtered.values())
        date_range = {
            'from': min(filtered.keys()) if filtered else None,
            'to': max(filtered.keys()) if filtered else None
        }

        return jsonify({
            'success': True,
            'data': {
                'total_tasks': total_tasks,
                'date_range': date_range,
                'days_with_data': len(filtered),
                'coverage': filtered
            }
        })

    except Exception as e:
        logger.error(f"Ошибка /api/quality/data-coverage: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


# Глобальная переменная для статуса обновления
update_status = {
    'running': False,
    'progress': 0,
    'total': 0,
    'message': '',
    'last_update': None,
    'error': None
}


@app.route('/api/pyrus/update', methods=['POST'])
@role_required('admin')
def trigger_update():
    """
    Запустить обновление данных из Pyrus

    Body params:
        - days: Количество последних дней для обновления (default: 7)
    """
    global update_status

    if update_status['running']:
        return jsonify({
            'success': False,
            'error': 'Обновление уже запущено',
            'status': update_status
        })

    try:
        import threading
        from datetime import datetime, timedelta

        data = request.get_json() or {}
        days = data.get('days', 7)

        # Функция обновления в фоновом потоке
        def update_in_background():
            global update_status
            try:
                update_status['running'] = True
                update_status['message'] = 'Запуск обновления...'
                update_status['error'] = None

                # Используем уже импортированные модули
                client = get_client()
                storage_obj = get_storage()

                if not client.authenticate():
                    raise Exception('Ошибка авторизации')

                # Вычисляем даты
                end_date = datetime.now()
                start_date = end_date - timedelta(days=days)

                start_iso = start_date.strftime('%Y-%m-%dT00:00:00Z')
                end_iso = end_date.strftime('%Y-%m-%dT23:59:59Z')

                # Получаем задачи
                response = client.session.get(
                    f'{client.api_url}forms/1327961/register',
                    headers={'Authorization': f'Bearer {client.access_token}'},
                    params={
                        'include_archived': 'y',
                        'item_count': 20000,
                        'created_after': start_iso,
                        'created_before': end_iso
                    }
                )

                if response.status_code != 200:
                    raise Exception(f'Ошибка API: {response.status_code}')

                tasks_data = response.json()
                tasks = tasks_data.get('tasks', [])

                update_status['total'] = len(tasks)
                update_status['message'] = f'Получено {len(tasks)} задач'

                # Сохраняем
                count = storage.save_tasks(1327961, tasks)
                update_status['progress'] = count

                # Обновляем статистику
                stats = storage.get_stats()

                update_status['message'] = f'Обновлено {count} задач'
                update_status['last_update'] = datetime.now().isoformat()

            except Exception as e:
                update_status['error'] = str(e)
                update_status['message'] = f'Ошибка: {str(e)}'
                logger.error(f"Ошибка фонового обновления: {e}")
            finally:
                update_status['running'] = False

        # Запускаем в фоновом потоке
        thread = threading.Thread(target=update_in_background)
        thread.daemon = True
        thread.start()

        return jsonify({
            'success': True,
            'message': 'Обновление запущено',
            'status': update_status
        })

    except Exception as e:
        logger.error(f"Ошибка запуска обновления: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/pyrus/update-status', methods=['GET'])
def get_update_status():
    """Получить статус обновления"""
    return jsonify({
        'success': True,
        'status': update_status
    })


@app.route('/dashboard', methods=['GET'])
def dashboard():
    """Редирект на новый дашборд"""
    from flask import redirect
    return redirect('/')


@app.errorhandler(404)
def not_found(error):
    """Обработка 404"""
    return jsonify({
        'success': False,
        'error': 'Endpoint not found'
    }), 404


@app.errorhandler(500)
def internal_error(error):
    """Обработка 500"""
    return jsonify({
        'success': False,
        'error': 'Internal server error'
    }), 500


def run_server(host: str = '0.0.0.0', port: int = 5000, debug: bool = False):
    """
    Запустить сервер

    Args:
        host: Хост для прослушивания (0.0.0.0 для prod, 127.0.0.1 для local)
        port: Порт
        debug: Режим отладки
    """
    logger.info(f"Запуск сервера на http://{host}:{port}")
    logger.info(f"БД: {db_path}")

    app.run(host=host, port=port, debug=debug)


if __name__ == '__main__':
    # Запуск из командной строки
    import argparse

    parser = argparse.ArgumentParser(description='Pyrus API Server')
    parser.add_argument('--host', default='127.0.0.1', help='Host')
    parser.add_argument('--port', type=int, default=5000, help='Port')
    parser.add_argument('--debug', action='store_true', help='Debug mode')

    args = parser.parse_args()

    run_server(host=args.host, port=args.port, debug=args.debug)
