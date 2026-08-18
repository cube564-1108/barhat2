"""
Pyrus API Server
Простой HTTP сервер для отдачи данных Pyrus в JSON формате
"""

import os
import sys
import logging
from datetime import datetime
from typing import Optional
from flask import Flask, jsonify, request, send_from_directory, redirect, session
from flask.sessions import SecureCookieSessionInterface
from flask_cors import CORS
from flask_compress import Compress
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

# gzip-сжатие ответов. Amvera (envoy) ничего не сжимает сама, а дашборд —
# это ~450 КБ текста (index.html + 9 JS + CSS) на первую загрузку; после
# сжатия остаётся ~90 КБ. Брать br/zstd не даём: их жмёт сам Python на
# каждый запрос и это заметно дороже по CPU, чем gzip, при тех же 2 воркерах.
app.config['COMPRESS_ALGORITHM'] = ['gzip']
app.config['COMPRESS_LEVEL'] = 6
app.config['COMPRESS_MIN_SIZE'] = 1024
app.config['COMPRESS_MIMETYPES'] = [
    'text/html', 'text/css', 'text/javascript', 'application/javascript',
    'application/json', 'image/svg+xml',
]
Compress(app)

# Секретный ключ для сессий из env
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'dev-secret-key-change-in-production')

# Безопасные cookie для сессий.
# SESSION_COOKIE_SAMESITE остаётся "Lax" — это единственная CSRF-защита
# POST-эндпоинтов в проекте (CSRF-токенов нет). Ослаблять её глобально до
# "None" ради SSO-встраивания в Пульс нельзя: любой сторонний сайт с формой
# смог бы дёргать /api/auth/users/... от имени залогиненного пользователя.
# Вместо этого SameSite=None + Partitioned выставляются точечно — только
# сессиям, заведённым через /sso (см. _PartitionedSsoSessionInterface ниже
# и src/sso.py, где после login_user() ставится session["sso"] = True).
app.config.update(
    SESSION_COOKIE_SECURE=True,      # только HTTPS
    SESSION_COOKIE_HTTPONLY=True,    # недоступна из JS
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 8,  # 8 часов
)

# Домен портала БАРХАТ Пульс — единственный, кому разрешено встраивать
# дашборд в <iframe> (frame-ancestors CSP ниже). Переопределяем через env,
# если у портала когда-нибудь сменится домен.
PULSE_ORIGIN = os.environ.get(
    "BARKHAT_PULSE_ORIGIN", "https://proekt-barhat-doorhandle2.amvera.io"
)


@app.after_request
def _apply_frame_ancestors(response):
    # Разрешаем встраивание только с домена Пульса. X-Frame-Options
    # намеренно НЕ выставляем — DENY/SAMEORIGIN сломали бы встраивание,
    # а frame-ancestors современным браузерам достаточно.
    response.headers["Content-Security-Policy"] = f"frame-ancestors {PULSE_ORIGIN}"
    return response


class _PartitionedSsoSessionInterface(SecureCookieSessionInterface):
    """Для сессий из /sso (см. src/sso.py, session["sso"] = True) кука
    получает SameSite=None + Partitioned (CHIPS) — иначе Chrome/Safari режут
    её внутри чужого <iframe> при следующем же AJAX-запросе. Обычные сессии
    по паролю остаются на SameSite=Lax (SESSION_COOKIE_SAMESITE из конфига).

    Partitioned дописывается вручную строкой в Set-Cookie: Werkzeug 3.0 ещё
    не принимает этот атрибут как kwarg в response.set_cookie(). save_session()
    вызывается уже ПОСЛЕ всех after_request-хуков (Flask.process_response),
    поэтому патчить куку нужно именно тут, а не в after_request.
    """

    def save_session(self, app, session, response):
        is_sso = bool(session.get("sso"))
        if not is_sso:
            super().save_session(app, session, response)
            return

        original_samesite = app.config.get("SESSION_COOKIE_SAMESITE")
        app.config["SESSION_COOKIE_SAMESITE"] = "None"
        try:
            super().save_session(app, session, response)
        finally:
            app.config["SESSION_COOKIE_SAMESITE"] = original_samesite

        cookie_name = self.get_cookie_name(app)
        cookies = response.headers.getlist("Set-Cookie")
        if not cookies:
            return
        del response.headers["Set-Cookie"]
        for cookie in cookies:
            if cookie.startswith(f"{cookie_name}=") and "Partitioned" not in cookie:
                cookie += "; Partitioned"
            response.headers.add("Set-Cookie", cookie)


app.session_interface = _PartitionedSsoSessionInterface()

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

# Регистрируем SSO blueprint (единый вход из портала БАРХАТ Пульс)
from sso import sso_bp
app.register_blueprint(sso_bp)

# Регистрируем blueprint кассовых смен
try:
    from cashshifts.server import cashshifts_bp
    app.register_blueprint(cashshifts_bp)
    logger.info("Blueprint кассовых смен зарегистрирован")
except ImportError as e:
    logger.warning(f"Не удалось импортировать blueprint кассовых смен: {e}")
except Exception as e:
    logger.error(f"Ошибка регистрации blueprint кассовых смен: {e}")

# Регистрируем blueprint счетов на оплату
try:
    from invoices.server import invoices_bp
    app.register_blueprint(invoices_bp)
    logger.info("Blueprint счетов на оплату зарегистрирован")
except ImportError as e:
    logger.warning(f"Не удалось импортировать blueprint счетов на оплату: {e}")
except Exception as e:
    logger.error(f"Ошибка регистрации blueprint счетов на оплату: {e}")

# Регистрируем blueprint МойСклад (ABC-анализ товаров)
try:
    from moysklad.server import moysklad_bp
    app.register_blueprint(moysklad_bp)
    logger.info("Blueprint МойСклад зарегистрирован")
except ImportError as e:
    logger.warning(f"Не удалось импортировать blueprint МойСклад: {e}")
except Exception as e:
    logger.error(f"Ошибка регистрации blueprint МойСклад: {e}")

# Регистрируем blueprint списаний товара
try:
    from writeoffs.server import writeoffs_bp
    app.register_blueprint(writeoffs_bp)
    logger.info("Blueprint списаний товара зарегистрирован")
except ImportError as e:
    logger.warning(f"Не удалось импортировать blueprint списаний товара: {e}")
except Exception as e:
    logger.error(f"Ошибка регистрации blueprint списаний товара: {e}")

# Регистрируем blueprint задач дашборда
try:
    from tasks.server import tasks_bp
    app.register_blueprint(tasks_bp)
    logger.info("Blueprint задач дашборда зарегистрирован")
except ImportError as e:
    logger.warning(f"Не удалось импортировать blueprint задач дашборда: {e}")
except Exception as e:
    logger.error(f"Ошибка регистрации blueprint задач дашборда: {e}")

# Инициализация таблиц авторизации
with app.app_context():
    init_auth_tables()

    # Инициализация таблиц кассовых смен
    try:
        from cashshifts.storage import init_cashshifts_tables
        init_cashshifts_tables()
        logger.info("Таблицы кассовых смен инициализированы")
    except ImportError as e:
        logger.warning(f"Не удалось импортировать модуль кассовых смен: {e}")
    except Exception as e:
        logger.error(f"Ошибка инициализации таблиц кассовых смен: {e}")

    # Инициализация таблиц счетов на оплату
    try:
        from invoices.storage import init_invoices_tables
        init_invoices_tables()
        logger.info("Таблицы счетов на оплату инициализированы")
    except ImportError as e:
        logger.warning(f"Не удалось импортировать модуль счетов на оплату: {e}")
    except Exception as e:
        logger.error(f"Ошибка инициализации таблиц счетов на оплату: {e}")

    # Инициализация таблиц задач дашборда
    try:
        from tasks.storage import init_tasks_tables
        init_tasks_tables()
        logger.info("Таблицы задач дашборда инициализированы")
    except ImportError as e:
        logger.warning(f"Не удалось импортировать модуль задач дашборда: {e}")
    except Exception as e:
        logger.error(f"Ошибка инициализации таблиц задач дашборда: {e}")

    # Инициализация таблиц списаний товара
    try:
        from writeoffs.storage import init_writeoffs_tables
        init_writeoffs_tables()
        logger.info("Таблицы списаний товара инициализированы")
    except ImportError as e:
        logger.warning(f"Не удалось импортировать модуль списаний товара: {e}")
    except Exception as e:
        logger.error(f"Ошибка инициализации таблиц списаний товара: {e}")


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
VENDOR_DIR = os.path.abspath(os.path.join(DASHBOARD_DIR, 'vendor'))

# Debug: print paths
print(f"SERVER_DIR: {SERVER_DIR}")
print(f"PROJECT_ROOT: {PROJECT_ROOT}")
print(f"DASHBOARD_DIR: {DASHBOARD_DIR}")
print(f"DASHBOARD exists: {os.path.exists(DASHBOARD_DIR)}")
print(f"BRAND_DIR: {BRAND_DIR}")
print(f"BRAND exists: {os.path.exists(BRAND_DIR)}")


def _is_embed_request():
    """Нужно ли отдавать «голый» отчёт без сайдбара и шапки.

    Включается для сессий, заведённых через /sso (внутри iframe Пульса своя
    навигация уже есть — наша дублирует её и путает), либо явным ?embed=1 —
    им же можно посмотреть embed-вид, залогинившись обычным паролем.
    """
    if request.args.get("embed") == "1":
        return True
    return bool(session.get("sso"))


def _serve_dashboard_shell():
    """Отдаёт index.html, помечая <html> классом embed-mode в embed-режиме.

    Метку ставит сервер, а не JS: класс должен быть в разметке ДО первой
    отрисовки, иначе сайдбар и шапка успевают мигнуть внутри iframe, пока
    не ответит /api/auth/me.
    """
    index_path = os.path.join(DASHBOARD_DIR, 'index.html')
    if not _is_embed_request():
        return send_from_directory(DASHBOARD_DIR, "index.html")

    with open(index_path, encoding='utf-8') as f:
        html = f.read()
    html = html.replace('<html lang="ru">', '<html lang="ru" class="embed-mode">', 1)

    response = app.make_response(html)
    response.headers['Content-Type'] = 'text/html; charset=utf-8'
    # Оболочка в embed-виде отличается от обычной — кэшировать её нельзя,
    # иначе браузер подставит вариант с сайдбаром (или наоборот).
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.route('/')
def index():
    """Главная страница — дашборд (с проверкой авторизации)"""
    try:
        # Проверяем авторизацию
        from flask_login import current_user
        if not current_user.is_authenticated:
            return send_from_directory(DASHBOARD_DIR, 'login.html')
        return _serve_dashboard_shell()
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


# === SPA Routes (все отдают index.html для History API) ===

@app.route('/dashboard')
def dashboard_page():
    """Страница дашборда"""
    try:
        from flask_login import current_user
        if not current_user.is_authenticated:
            return redirect('/login')
        return _serve_dashboard_shell()
    except Exception as e:
        logger.error(f"Ошибка загрузки /dashboard: {e}")
        return f"Ошибка загрузки страницы: {e}", 500


@app.route('/calculator')
def calculator_page():
    """Страница калькулятора букетов"""
    try:
        from flask_login import current_user
        if not current_user.is_authenticated:
            return redirect('/login')
        return _serve_dashboard_shell()
    except Exception as e:
        logger.error(f"Ошибка загрузки /calculator: {e}")
        return f"Ошибка загрузки страницы: {e}", 500


@app.route('/quality')
def quality_page():
    """Страница качества сборки"""
    try:
        from flask_login import current_user
        if not current_user.is_authenticated:
            return redirect('/login')
        return _serve_dashboard_shell()
    except Exception as e:
        logger.error(f"Ошибка загрузки /quality: {e}")
        return f"Ошибка загрузки страницы: {e}", 500


@app.route('/users')
def users_page():
    """Страница управления пользователями"""
    try:
        from flask_login import current_user
        if not current_user.is_authenticated:
            return redirect('/login')
        return _serve_dashboard_shell()
    except Exception as e:
        logger.error(f"Ошибка загрузки /users: {e}")
        return f"Ошибка загрузки страницы: {e}", 500


@app.route('/cash-shifts')
@app.route('/cash_shifts')  # Алиас: старые вкладки/закладки с URL до фикса подчёркивания в роутере
def cash_shifts_page():
    """Страница кассовых смен"""
    try:
        from flask_login import current_user
        if not current_user.is_authenticated:
            return redirect('/login')
        return _serve_dashboard_shell()
    except Exception as e:
        logger.error(f"Ошибка загрузки /cash-shifts: {e}")
        return f"Ошибка загрузки страницы: {e}", 500


@app.route('/invoices')
def invoices_page():
    """Страница счетов на оплату"""
    try:
        from flask_login import current_user
        if not current_user.is_authenticated:
            return redirect('/login')
        return _serve_dashboard_shell()
    except Exception as e:
        logger.error(f"Ошибка загрузки /invoices: {e}")
        return f"Ошибка загрузки страницы: {e}", 500


@app.route('/writeoffs')
def writeoffs_page():
    """Страница списаний товара"""
    try:
        from flask_login import current_user
        if not current_user.is_authenticated:
            return redirect('/login')
        return _serve_dashboard_shell()
    except Exception as e:
        logger.error(f"Ошибка загрузки /writeoffs: {e}")
        return f"Ошибка загрузки страницы: {e}", 500


@app.route('/abc-analysis')
def abc_analysis_page():
    """Страница ABC-анализа товаров"""
    try:
        from flask_login import current_user
        if not current_user.is_authenticated:
            return redirect('/login')
        return _serve_dashboard_shell()
    except Exception as e:
        logger.error(f"Ошибка загрузки /abc-analysis: {e}")
        return f"Ошибка загрузки страницы: {e}", 500


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

@app.route('/users.js')
def serve_users():
    """Отдаёт скрипт управления пользователями"""
    return send_from_directory(DASHBOARD_DIR, 'users.js')


@app.route('/cash-shifts.js')
def serve_cash_shifts():
    """Отдаёт скрипт кассовых смен"""
    return send_from_directory(DASHBOARD_DIR, 'cash-shifts.js')

@app.route('/invoices.js')
def serve_invoices():
    """Отдаёт скрипт счетов на оплату"""
    return send_from_directory(DASHBOARD_DIR, 'invoices.js')

@app.route('/abc-analysis.js')
def serve_abc_analysis():
    """Отдаёт скрипт ABC-анализа товаров"""
    return send_from_directory(DASHBOARD_DIR, 'abc-analysis.js')

@app.route('/tasks.js')
def serve_tasks():
    """Отдаёт скрипт раздела задач дашборда"""
    return send_from_directory(DASHBOARD_DIR, 'tasks.js')

@app.route('/writeoffs.js')
def serve_writeoffs():
    """Отдаёт скрипт раздела списаний товара"""
    return send_from_directory(DASHBOARD_DIR, 'writeoffs.js')

@app.route('/brand/<path:filename>')
def serve_brand(filename):
    """Отдаёт файлы из директории brand"""
    return send_from_directory(BRAND_DIR, filename)


@app.route('/vendor/<path:filename>')
def serve_vendor(filename):
    """Отдаёт сторонние библиотеки (chart.js) со своего домена.

    Раньше chart.js тянулся с cdn.jsdelivr.net блокирующим тегом в <head>:
    это лишние DNS + TLS к чужому домену (~1 с) перед первой отрисовкой, да
    ещё и на CDN, доступность которого мы не контролируем.

    Версия зашита в имя файла (chart-4.4.0.umd.min.js), поэтому ответ можно
    кэшировать надолго — при обновлении библиотеки меняется имя, и браузер
    сам сходит за новым файлом.
    """
    response = send_from_directory(VENDOR_DIR, filename)
    response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
    return response


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

        conn = sqlite3.connect(DB_PATH, timeout=20)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=20000")
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


# ========== Загрузка задач из Pyrus ==========
#
# Статус загрузки хранится в таблице sync_log (файл на диске), а НЕ в памяти
# процесса: на Amvera 2 воркера gunicorn (amvera.yml), фоновый поток стартует
# на одном из них, а опрос /update-status от фронтенда мог попасть на другой
# и вернуть чистый дефолт (running=False) — ложное "готово" посреди загрузки.
# Тот же инцидент уже был в синхронизации МойСклада.
#
# Задачи грузятся окнами по датам, а не одним запросом: Pyrus ограничивает
# item_count реестра 20 000 записями (client.py), а история формы качества —
# почти 100 000 задач. Каждое окно сохраняется сразу после загрузки, поэтому
# обрыв процесса теряет одно окно, а не весь прогресс.

QUALITY_FORM_ID = 1327961

# Если фоновый поток не обновлял heartbeat дольше этого времени, считаем
# загрузку мёртвой (упал воркер) и разрешаем запустить новую — иначе запись
# в статусе 'started' блокировала бы кнопку до перезапуска сервиса.
SYNC_STALE_MINUTES = 15


def _sync_status_payload(log_row):
    """Привести запись sync_log к формату, который ждёт фронтенд"""
    if not log_row:
        return {
            'running': False,
            'progress': 0,
            'total': 0,
            'message': '',
            'last_update': None,
            'error': None
        }

    from datetime import datetime, timedelta

    running = log_row.get('status') == 'started'
    if running:
        heartbeat = log_row.get('updated_at') or log_row.get('started_at')
        try:
            beat_time = datetime.fromisoformat(str(heartbeat))
            if datetime.utcnow() - beat_time > timedelta(minutes=SYNC_STALE_MINUTES):
                running = False
        except (TypeError, ValueError):
            pass

    count = log_row.get('tasks_count') or 0
    error = log_row.get('error_message')
    # message появился вместе с фоновой загрузкой окнами; у записей, созданных
    # до этого, его нет — показываем хотя бы количество задач
    message = log_row.get('message') or (f'Обновлено {count} задач' if count else '')
    if not running and log_row.get('status') == 'started' and not error:
        error = 'Загрузка прервана (процесс не отвечает)'

    return {
        'running': running,
        'progress': count,
        'total': count,
        'message': message,
        'last_update': log_row.get('finished_at'),
        'error': error
    }


def _fetch_register_window(client, form_id, start_iso, end_iso):
    """Загрузить реестр формы за одно окно дат"""
    response = client.session.get(
        f'{client.api_url}forms/{form_id}/register',
        headers={'Authorization': f'Bearer {client.access_token}'},
        params={
            'include_archived': 'y',
            'item_count': 20000,
            'created_after': start_iso,
            'created_before': end_iso
        },
        # без таймаута зависший Pyrus держал бы поток вечно, а запись в
        # sync_log — в статусе 'started'
        timeout=(10, 180)
    )

    if response.status_code != 200:
        raise Exception(f'Ошибка API: {response.status_code}')

    return response.json().get('tasks', [])


def _import_tasks_background(log_id, start_date, end_date, step_days):
    """Фоновая загрузка задач формы качества окнами по step_days дней"""
    from datetime import timedelta

    total_saved = 0

    try:
        client = get_client()
        if not client.authenticate():
            raise Exception('Ошибка авторизации')

        # Границы окон включительные с обеих сторон (created_after 00:00:00,
        # created_before 23:59:59), поэтому следующее окно начинается со
        # СЛЕДУЮЩЕГО дня — иначе стыковой день грузился бы дважды
        windows = []
        cursor_date = start_date
        while cursor_date <= end_date:
            window_end = min(cursor_date + timedelta(days=step_days - 1), end_date)
            windows.append((cursor_date, window_end))
            cursor_date = window_end + timedelta(days=1)

        for index, (window_start, window_end) in enumerate(windows, 1):
            tasks = _fetch_register_window(
                client,
                QUALITY_FORM_ID,
                window_start.strftime('%Y-%m-%dT00:00:00Z'),
                window_end.strftime('%Y-%m-%dT23:59:59Z')
            )
            total_saved += storage.save_tasks(QUALITY_FORM_ID, tasks)

            storage.update_sync_log(
                log_id,
                f'Период {window_start:%d.%m.%Y}–{window_end:%d.%m.%Y} '
                f'({index}/{len(windows)}): загружено {total_saved} задач',
                total_saved
            )

        storage.finish_sync_log(
            log_id,
            forms_count=1,
            tasks_count=total_saved,
            status='completed',
            message=f'Обновлено {total_saved} задач'
        )

    except Exception as e:
        logger.error(f"Ошибка фоновой загрузки Pyrus: {e}")
        storage.finish_sync_log(
            log_id,
            forms_count=1,
            tasks_count=total_saved,
            status='failed',
            error_message=str(e),
            message=f'Ошибка после {total_saved} задач'
        )


def _start_import(job, start_date, end_date, step_days):
    """Проверить, что загрузка не идёт, и запустить фоновый поток"""
    import threading

    last_log = storage.get_latest_sync_log()
    if _sync_status_payload(last_log)['running']:
        return jsonify({
            'success': False,
            'error': 'Обновление уже запущено',
            'status': _sync_status_payload(last_log)
        })

    log_id = storage.start_sync_log(job=job, message='Запуск обновления...')

    thread = threading.Thread(
        target=_import_tasks_background,
        args=(log_id, start_date, end_date, step_days)
    )
    thread.daemon = True
    thread.start()

    return jsonify({
        'success': True,
        'message': 'Обновление запущено',
        'status': _sync_status_payload(storage.get_latest_sync_log())
    })


@app.route('/api/pyrus/update', methods=['POST'])
@role_required('admin')
def trigger_update():
    """
    Запустить обновление данных из Pyrus

    Body params:
        - days: Количество последних дней для обновления (default: 7)
    """
    try:
        from datetime import datetime, timedelta

        data = request.get_json(silent=True) or {}
        days = int(data.get('days', 7))

        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)

        return _start_import('update', start_date, end_date, step_days=7)

    except Exception as e:
        logger.error(f"Ошибка запуска обновления: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/pyrus/backfill', methods=['POST'])
@role_required('admin')
def trigger_backfill():
    """
    Разовая загрузка истории задач формы качества за длительный период.

    Нужна потому, что прод-база наполняется только ежедневной подкачкой за
    7 дней: вся история формы (~98 тыс. задач) осталась в локальной базе,
    с которой отчёт работал, пока фронтенд ходил на 127.0.0.1.

    Body params:
        - date_from: начало периода, YYYY-MM-DD (default: 2026-01-01)
        - date_to: конец периода, YYYY-MM-DD (default: сегодня)
        - step_days: размер окна загрузки в днях (default: 7)
    """
    try:
        from datetime import datetime

        data = request.get_json(silent=True) or {}
        date_from = data.get('date_from', '2026-01-01')
        date_to = data.get('date_to')
        step_days = int(data.get('step_days', 7))

        start_date = datetime.strptime(date_from, '%Y-%m-%d')
        end_date = datetime.strptime(date_to, '%Y-%m-%d') if date_to else datetime.now()

        if start_date >= end_date:
            return jsonify({'success': False, 'error': 'date_from должен быть раньше date_to'}), 400

        return _start_import('backfill', start_date, end_date, step_days)

    except ValueError as e:
        return jsonify({'success': False, 'error': f'Неверный формат даты: {e}'}), 400
    except Exception as e:
        logger.error(f"Ошибка запуска backfill: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/pyrus/update-status', methods=['GET'])
def get_update_status():
    """Получить статус обновления"""
    return jsonify({
        'success': True,
        'status': _sync_status_payload(storage.get_latest_sync_log())
    })


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
