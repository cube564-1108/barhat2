"""
Pyrus API Server
Простой HTTP сервер для отдачи данных Pyrus в JSON формате
"""

import os
import re
import sys
import glob
import time
import importlib
import shutil
import sqlite3
import logging
from datetime import datetime
from typing import Optional
from flask import (
    Flask, jsonify, request, send_from_directory, send_file, redirect, session,
    after_this_request,
)
from flask.sessions import SecureCookieSessionInterface
from flask_cors import CORS
from flask_compress import Compress
from dotenv import load_dotenv

# Импорт авторизации
import sys
import os
auth_path = os.path.join(os.path.dirname(__file__), '../')
sys.path.insert(0, auth_path)
from auth import (
    auth_bp, login_manager, init_auth_tables, section_required, role_required, log_action,
)

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

# Отчёт о качестве сборки. Раньше он жил в scripts/ и импортировался через
# подмешивание scripts/ в sys.path — рядом лежали две версии одного модуля, и
# по коду было не понять, какая работает в проде. Теперь это обычный модуль
# пакета, а scripts/quality_report_v2.py — тонкая обёртка над ним.
from . import quality as quality_report
from . import nos as nos_report

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


# Порог, после которого запрос попадает в лог как медленный. 1 секунда —
# это уже заметно человеку, но ещё не шум: обычная ручка укладывается в
# сотни миллисекунд даже на сетевом диске Amvera.
SLOW_REQUEST_SECONDS = float(os.environ.get("SLOW_REQUEST_SECONDS", "1.0"))


@app.before_request
def _start_request_timer():
    request._started_at = time.monotonic()


@app.after_request
def _log_slow_request(response):
    """Записать в лог запросы, которые шли дольше порога.

    Без этого «сайт виснет» невозможно разобрать: снаружи видно только общее
    время, а какая именно ручка встала — нет. Замеры прода 2026-08-26 дали
    диагноз про диск, но конкретного виновника пиков в 15-33 секунды не
    показали, потому что мерили /health, а не то, что нажимает человек.

    Логируется только превышение порога, поэтому статика и быстрые ручки шум
    не создают. Заголовки ответа хук не трогает — только читает статус.
    """
    started = getattr(request, "_started_at", None)
    if started is not None:
        elapsed = time.monotonic() - started
        if elapsed >= SLOW_REQUEST_SECONDS:
            # Путь БЕЗ query-строки: через ?token= приезжает JWT-пропуск Пульса,
            # а это учётные данные — в логах Amvera им не место. Для ответа на
            # вопрос «какая ручка встала» пути достаточно.
            logger.warning(
                "Медленный запрос: %s %s — %.1f c (статус %s)",
                request.method, request.path, elapsed, response.status_code,
            )
    return response


@app.after_request
def _apply_frame_ancestors(response):
    """Разрешаем встраивать наши ответы в <iframe> только со своего домена
    и с домена Пульса. X-Frame-Options намеренно НЕ выставляем —
    DENY/SAMEORIGIN сломали бы встраивание в Пульс, а frame-ancestors
    современным браузерам достаточно.

    Хук ДОПОЛНЯЕТ политику вьюхи, а не затирает её. Раньше здесь стояло
    присваивание, и оно молча выигрывало у вьюхи: просмотр вложений
    (src/invoices/server.py, ручка inline) ставит себе `sandbox`, но до
    браузера тот заголовок не доезжал. Последствий было два — песочницы на
    вложениях не существовало вовсе, а PDF вообще перестал открываться:
    ответу доставался только `frame-ancestors <домен Пульса>`, поэтому
    встраивание в <iframe> на самом дашборде браузер блокировал
    (ERR_BLOCKED_BY_RESPONSE). Отсюда же и `'self'` в списке источников.
    """
    frame_ancestors = f"frame-ancestors 'self' {PULSE_ORIGIN}"
    existing = (response.headers.get("Content-Security-Policy") or "").strip()
    if not existing:
        response.headers["Content-Security-Policy"] = frame_ancestors
    elif "frame-ancestors" not in existing:
        # Своя frame-ancestors у вьюхи — приоритет её: она знает про свой
        # ответ больше, чем общий хук.
        response.headers["Content-Security-Policy"] = f"{existing.rstrip('; ')}; {frame_ancestors}"
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
    from moysklad.server import moysklad_bp, start_sync_scheduler
    app.register_blueprint(moysklad_bp)
    logger.info("Blueprint МойСклад зарегистрирован")
    # Инкрементальная синхронизация заказов идёт сама, раз в 30 минут.
    # Планировщик стартует в каждом воркере, но прогон делает один — за это
    # отвечает лок в БД (moysklad/storage.py::try_acquire_sync_lock).
    start_sync_scheduler()
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

# Регистрируем blueprint оплаты курьерам (RetailCRM)
try:
    from couriers.server import couriers_bp, start_sync_scheduler as start_couriers_sync_scheduler
    app.register_blueprint(couriers_bp)
    logger.info("Blueprint оплаты курьерам зарегистрирован")
    # Заказы по дате доставки подтягиваются сами раз в 30 минут. Планировщик
    # стартует в каждом воркере, но прогон делает один — за это отвечает лок
    # в БД (couriers/storage.py::try_acquire_sync_lock).
    start_couriers_sync_scheduler()
except ImportError as e:
    logger.warning(f"Не удалось импортировать blueprint оплаты курьерам: {e}")
except Exception as e:
    logger.error(f"Ошибка регистрации blueprint оплаты курьерам: {e}")

# Регистрируем blueprint сторожа ссылок на товары
try:
    from linkwatch.server import linkwatch_bp, start_scheduler as start_linkwatch_scheduler
    from linkwatch.storage import init_tables as init_linkwatch_tables
    app.register_blueprint(linkwatch_bp)
    init_linkwatch_tables()
    logger.info("Blueprint сторожа ссылок зарегистрирован")
    # Ссылки проверяются раз в сутки ночью: обход сайта идёт ~30 минут, днём
    # это лишняя нагрузка на витрину. Планировщик стартует в каждом воркере,
    # прогон делает один — талон и лок в linkwatch/storage.py.
    start_linkwatch_scheduler()
except ImportError as e:
    logger.warning(f"Не удалось импортировать blueprint сторожа ссылок: {e}")
except Exception as e:
    logger.error(f"Ошибка регистрации blueprint сторожа ссылок: {e}")

# Регистрируем blueprint задач дашборда
try:
    from tasks.server import tasks_bp
    app.register_blueprint(tasks_bp)
    logger.info("Blueprint задач дашборда зарегистрирован")
except ImportError as e:
    logger.warning(f"Не удалось импортировать blueprint задач дашборда: {e}")
except Exception as e:
    logger.error(f"Ошибка регистрации blueprint задач дашборда: {e}")

# Регистрируем blueprint показателей салонов
try:
    from salonkpi.server import salonkpi_bp
    app.register_blueprint(salonkpi_bp)
    logger.info("Blueprint показателей салонов зарегистрирован")
except ImportError as e:
    logger.warning(f"Не удалось импортировать blueprint показателей салонов: {e}")
except Exception as e:
    logger.error(f"Ошибка регистрации blueprint показателей салонов: {e}")

# Регистрируем blueprint обратной связи от сотрудников
try:
    from feedback.server import feedback_bp
    app.register_blueprint(feedback_bp)
    logger.info("Blueprint обратной связи зарегистрирован")
except ImportError as e:
    logger.warning(f"Не удалось импортировать blueprint обратной связи: {e}")
except Exception as e:
    logger.error(f"Ошибка регистрации blueprint обратной связи: {e}")

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

    # Фоновая разноска заявок по рабочим картам в ПланФакт. Запускается после
    # инициализации таблиц: планировщик первым делом читает базу.
    try:
        from invoices.cards_sync import start_card_sync_scheduler
        start_card_sync_scheduler()
    except Exception as e:
        logger.error(f"Не удалось запустить планировщик разноски карт: {e}")

    # Справочник банков ЦБ: обновление раз в сутки, своя стартовая задержка,
    # чтобы не совпасть волной с остальными синками на общем диске /data.
    try:
        from invoices.banks import start_banks_scheduler
        start_banks_scheduler()
    except Exception as e:
        logger.error(f"Не удалось запустить планировщик справочника банков: {e}")

    # Инициализация таблиц задач дашборда
    try:
        from tasks.storage import init_tasks_tables
        init_tasks_tables()
        logger.info("Таблицы задач дашборда инициализированы")
    except ImportError as e:
        logger.warning(f"Не удалось импортировать модуль задач дашборда: {e}")
    except Exception as e:
        logger.error(f"Ошибка инициализации таблиц задач дашборда: {e}")

    # Инициализация таблиц обратной связи
    try:
        from feedback.storage import init_feedback_tables
        init_feedback_tables()
        logger.info("Таблицы обратной связи инициализированы")
    except ImportError as e:
        logger.warning(f"Не удалось импортировать модуль обратной связи: {e}")
    except Exception as e:
        logger.error(f"Ошибка инициализации таблиц обратной связи: {e}")

    # Инициализация таблиц оплаты курьерам
    try:
        from couriers.storage import init_couriers_tables
        init_couriers_tables()
        logger.info("Таблицы оплаты курьерам инициализированы")
    except ImportError as e:
        logger.warning(f"Не удалось импортировать модуль оплаты курьерам: {e}")
    except Exception as e:
        logger.error(f"Ошибка инициализации таблиц оплаты курьерам: {e}")

    # Инициализация таблиц списаний товара
    try:
        from writeoffs.storage import init_writeoffs_tables
        init_writeoffs_tables()
        logger.info("Таблицы списаний товара инициализированы")
    except ImportError as e:
        logger.warning(f"Не удалось импортировать модуль списаний товара: {e}")
    except Exception as e:
        logger.error(f"Ошибка инициализации таблиц списаний товара: {e}")

    # Инициализация таблиц показателей салонов (справочник соответствий и планы).
    # Здесь же проставляется стартовое соответствие салонов: без него первый
    # запуск показал бы нули по всем показателям.
    try:
        from salonkpi.storage import init_salonkpi_tables
        init_salonkpi_tables()
        logger.info("Таблицы показателей салонов инициализированы")
    except ImportError as e:
        logger.warning(f"Не удалось импортировать модуль показателей салонов: {e}")
    except Exception as e:
        logger.error(f"Ошибка инициализации таблиц показателей салонов: {e}")


# Инициализация хранилища
db_path = os.getenv('PYRUS_DB_PATH', 'data/pyrus.db')
storage = get_storage(db_path)

# Витрина отчёта по качеству. Собирается один раз при старте процесса и только
# если пуста (первый деплой после появления таблицы) — делать это в обработчике
# запроса нельзя: инициализация на каждом запросе уже клала сайт.
quality_report.ensure_projection()

# Витрина негативной обратной связи — тот же принцип: собирается один раз при
# старте процесса и только если пуста.
nos_report.ensure_projection()


def _disk_free_info():
    """Свободное место на постоянном диске. Кончившееся место — одна из
    немногих причин, по которой SQLite читает нормально, но перестаёт писать."""
    target = '/data' if os.path.isdir('/data') else os.path.abspath(os.sep)
    try:
        usage = shutil.disk_usage(target)
        return {
            'path': target,
            'total_mb': round(usage.total / 1024 / 1024, 2),
            'used_mb': round(usage.used / 1024 / 1024, 2),
            'free_mb': round(usage.free / 1024 / 1024, 2),
        }
    except Exception as e:
        return {'path': target, 'error': str(e)}


def _sqlite_write_probe(path):
    """Проверить, что в базу вообще получается ПИСАТЬ, и за сколько.

    Логин отличается от неудачного логина ровно одним: успешный доходит до
    log_action() — INSERT в audit_log. Если запись заблокирована, чтение и
    /health остаются мгновенными, а вход висит до busy_timeout. Отсюда проба:
    пишем ровно в ту же таблицу audit_log и откатываем — база не меняется,
    но блокировка записи проявляется. Никакого DDL: служебных таблиц в боевой
    базе после диагностики не остаётся. Таймаут короткий (2с), чтобы сама
    проба не заняла воркер.
    """
    if not os.path.exists(path):
        return {'ok': False, 'error': 'база не найдена'}

    started = time.monotonic()
    conn = None
    try:
        conn = sqlite3.connect(path, timeout=2)
        conn.execute('PRAGMA busy_timeout=2000')
        conn.execute(
            "INSERT INTO audit_log (username, action, details, ip, created_at)"
            " VALUES ('_probe', '_write_probe', '', '', ?)",
            (datetime.utcnow().isoformat(),)
        )
        conn.rollback()
        return {'ok': True, 'seconds': round(time.monotonic() - started, 3)}
    except Exception as e:
        return {
            'ok': False,
            'error': f'{type(e).__name__}: {e}',
            'seconds': round(time.monotonic() - started, 3),
        }
    finally:
        if conn is not None:
            conn.close()


@app.route('/health', methods=['GET'])
def health_check():
    """Проверка здоровья сервера.

    Показывает фактические пути и размеры баз: на Amvera нет консоли, и
    единственный способ заметить, что база уехала с постоянного диска /data
    на эфемерный /app (см. комментарий про пути в app.py), — посмотреть их
    отсюда. Пустой размер у боевой базы = данные потерялись при сборке.
    """
    db_targets = [
        ('pyrus', db_path),
        ('barhat', os.environ.get('BARHAT_DB_PATH', 'barhat.db')),
        ('moysklad', os.environ.get('MOYSKLAD_DB_PATH', 'moysklad.db')),
    ]

    # Путь спрашиваем у самого модуля, а не собираем заново из env: couriers
    # резолвит его через storage_paths, и показывать надо ровно тот файл,
    # в который модуль пишет.
    try:
        from couriers.storage import DB_PATH as couriers_db_path
        db_targets.append(('couriers', couriers_db_path))
    except Exception as e:
        logger.warning(f"Не удалось получить путь базы курьеров для /health: {e}")

    databases = {}
    for name, path in db_targets:
        exists = os.path.exists(path)
        wal = path + '-wal'
        databases[name] = {
            'path': path,
            'exists': exists,
            'size_mb': round(os.path.getsize(path) / 1024 / 1024, 2) if exists else 0,
            'persistent': os.path.abspath(path).startswith('/data'),
            # Раздутый WAL = чекпоинт не проходит, обычно из-за зависшего
            # читателя или кончившегося места; тогда запись встаёт колом
            'wal_mb': round(os.path.getsize(wal) / 1024 / 1024, 2) if os.path.exists(wal) else 0,
        }

    # Папки вложений живут по тем же правилам, что и базы: если путь не на
    # /data, файлы стираются каждой сборкой, а записи о них остаются в БД —
    # и счёт открывается, а вложение к нему «не находится». Пути спрашиваем
    # у самих модулей, а не собираем заново: показывать надо ровно ту папку,
    # в которую они пишут.
    attachments = {}
    for name, module_path in (
        ('invoices', 'invoices.storage'),
        ('writeoffs', 'writeoffs.storage'),
    ):
        try:
            module = importlib.import_module(module_path)
            full = os.path.abspath(module.ATTACHMENTS_DIR)
        except Exception as e:
            attachments[name] = {'error': f'{type(e).__name__}: {e}'}
            continue
        attachments[name] = {
            'path': full,
            'exists': os.path.isdir(full),
            'persistent': full.startswith('/data'),
            'files': len(os.listdir(full)) if os.path.isdir(full) else 0,
        }

    # Инварианты, которые держит не код, а схема БД. Индекс мог не построиться
    # (например, поверх уже накопленных дублей), и снаружи это ничем не видно:
    # запись продолжает работать, просто последней преграды нет.
    guarantees = {}
    try:
        from cashshifts.storage import one_open_shift_guarantee_status
        guarantees['one_open_shift_per_store'] = one_open_shift_guarantee_status()
    except Exception as e:
        guarantees['one_open_shift_per_store'] = {'error': f'{type(e).__name__}: {e}'}

    return jsonify({
        'status': 'ok',
        'timestamp': datetime.now().isoformat(),
        'database': db_path,
        'databases': databases,
        'attachments': attachments,
        'disk': _disk_free_info(),
        'guarantees': guarantees,
        'write_test': _sqlite_write_probe(os.environ.get('BARHAT_DB_PATH', 'barhat.db')),
    })


# Больше этого одним файлом не отдаём. Смысл границы не в диске, а в том,
# что воркеров всего два (amvera.yml): отдача занимает один из них на всё
# время скачивания, и на медленном канале это заметно всем остальным.
MAX_DB_BACKUP_MB = 512


def _remove_stale_db_backups(directory):
    """Убрать временные копии, оставшиеся от прошлых выгрузок.

    На Linux временный файл удаляется сразу после открытия и сюда не попадает
    вовсе. На Windows (локальный запуск) открытый файл удалить нельзя, а
    уборка после отдачи ответа срабатывает не всегда — без этой подчистки
    копия базы лежала бы на диске до перезапуска. Копия базы на диске —
    ровно то, чего эта ручка не должна оставлять после себя.
    """
    for path in glob.glob(os.path.join(directory, '.db-backup-*.tmp')):
        try:
            os.remove(path)
        except OSError:
            logger.warning('Не удалось убрать прошлую временную копию базы: %s', path)


@app.route('/api/admin/db-backup', methods=['GET'])
@role_required('admin')
def download_db_backup():
    """
    Копия боевой базы одним файлом — страховка перед миграциями схемы.

    На этом тарифе Amvera нет консоли контейнера, поэтому снять копию можно
    только HTTP-ручкой. Копируем через sqlite3.Connection.backup(), а не
    `VACUUM INTO`: backup идёт порциями страниц с паузами и не держит базу
    заблокированной всё время, что важно при двух воркерах на одном WAL.
    Копия получается консистентной, без отдельного -wal рядом.

    Безопасность: это единственная ручка проекта, отдающая всю базу целиком —
    вместе с хешами паролей и данными всех модулей. Поэтому только admin,
    обязательная запись в audit_log, ответ без кэширования, а временный файл
    удаляется сразу после открытия — скачиваемой копии на диске не остаётся.

    Регулярные бэкапы этой ручкой не делаются: это была бы отдельная задача
    с ротацией и шифрованием, а не «скачать по ссылке».
    """
    from flask_login import current_user

    source = os.environ.get('BARHAT_DB_PATH', 'barhat.db')
    if not os.path.exists(source):
        return jsonify({'error': f'База не найдена: {source}'}), 404

    size = os.path.getsize(source)
    if size > MAX_DB_BACKUP_MB * 1024 * 1024:
        return jsonify({
            'error': f'База весит {round(size / 1024 / 1024, 1)} МБ — больше лимита '
                     f'{MAX_DB_BACKUP_MB} МБ. Выгружать её через эту ручку нельзя.'
        }), 413

    # Копия ложится рядом с базой: на проде это /data, то есть постоянный диск
    # с известным запасом места. В /tmp контейнера места может не быть вовсе.
    directory = os.path.dirname(os.path.abspath(source))
    _remove_stale_db_backups(directory)
    try:
        free = shutil.disk_usage(directory).free
    except OSError:
        free = None
    if free is not None and free < size * 1.5:
        return jsonify({
            'error': f'На диске свободно {round(free / 1024 / 1024, 1)} МБ — '
                     f'для копии базы ({round(size / 1024 / 1024, 1)} МБ) этого мало'
        }), 507

    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    tmp_path = os.path.join(directory, f'.db-backup-{stamp}-{os.getpid()}.tmp')

    source_conn = None
    target_conn = None
    try:
        source_conn = sqlite3.connect(source, timeout=30)
        source_conn.execute('PRAGMA busy_timeout=30000')
        target_conn = sqlite3.connect(tmp_path)
        # Порциями с паузой: за время копирования соседний воркер должен
        # успевать писать в базу, иначе логин и любое действие встают колом.
        source_conn.backup(target_conn, pages=2048, sleep=0.01)
    except Exception:
        logger.exception('Не удалось снять копию базы %s', source)
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                logger.warning('Временный файл копии остался на диске: %s', tmp_path)
        return jsonify({'error': 'Не удалось снять копию базы, подробности в логах сервера'}), 500
    finally:
        for conn in (target_conn, source_conn):
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass

    backup_size = os.path.getsize(tmp_path)

    # Файл открываем и тут же удаляем с диска: на Linux данные остаются
    # доступны через открытый дескриптор, а скачиваемой копии базы на диске
    # не существует ни секунды. На Windows (локальный запуск) удалить открытый
    # файл нельзя — там убираем его после ответа.
    handle = open(tmp_path, 'rb')
    try:
        os.remove(tmp_path)
        cleanup_after_send = False
    except OSError:
        cleanup_after_send = True

    log_action(
        current_user.username, 'download_db_backup',
        f'{os.path.basename(source)}, {round(backup_size / 1024 / 1024, 2)} МБ'
    )

    response = send_file(
        handle,
        mimetype='application/octet-stream',
        as_attachment=True,
        download_name=f'barhat-backup-{stamp}.db',
    )
    # Копия базы не должна осесть ни в одном кэше по дороге
    response.headers['Cache-Control'] = 'no-store'

    if cleanup_after_send:
        # Именно call_on_close, а не after_this_request: последний срабатывает
        # ДО того, как WSGI прочитает файл, и ответ падает с «read of closed
        # file». Здесь же уборка идёт после отдачи тела.
        @response.call_on_close
        def _remove_backup_file():
            try:
                handle.close()
                os.remove(tmp_path)
            except OSError:
                logger.warning('Временный файл копии остался на диске: %s', tmp_path)

    return response


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


_ACTIVE_CLASS_RE = re.compile(r'class="(nav-item|page) active"')


def _page_name_from_path(path):
    """Имя модуля (data-page) из пути запроса: /cash-shifts → cash_shifts."""
    slug = path.strip('/\\').lower()
    if not slug:
        return 'dashboard'
    return slug.replace('-', '_')


def _mark_active_page(html, page):
    """Переносит класс active на страницу из URL прямо в разметке.

    В index.html active жёстко проставлен «Дашборду». script.js исправляет
    это только после ответа /api/auth/me, поэтому при перезагрузке любого
    модуля пользователь успевал увидеть дашборд и лишь потом свою страницу.
    """
    if page == 'dashboard':
        return html

    target_re = re.compile(r'class="(nav-item|page)"(\s+data-page="%s")' % re.escape(page))
    if not target_re.search(html):
        # Страница ещё не свёрстана (заглушку создаёт JS) — оставляем как есть.
        return html

    html = _ACTIVE_CLASS_RE.sub(r'class="\1"', html)
    return target_re.sub(r'class="\1 active"\2', html)


def _serve_dashboard_shell():
    """Отдаёт index.html: помечает активный модуль и embed-режим.

    Метки ставит сервер, а не JS: они должны быть в разметке ДО первой
    отрисовки, иначе внутри iframe мигают сайдбар и шапка, а при F5 на
    любом модуле — страница «Дашборд», пока не ответит /api/auth/me.
    """
    index_path = os.path.join(DASHBOARD_DIR, 'index.html')
    is_embed = _is_embed_request()

    with open(index_path, encoding='utf-8') as f:
        html = f.read()

    html = _mark_active_page(html, _page_name_from_path(request.path))
    if is_embed:
        html = html.replace('<html lang="ru">', '<html lang="ru" class="embed-mode">', 1)

    response = app.make_response(html)
    response.headers['Content-Type'] = 'text/html; charset=utf-8'
    # Оболочка зависит от URL и режима, а не только от файла на диске:
    # embed-вид отличается от обычного, активный модуль — от пути. Кэш
    # браузера тут подставил бы чужой вариант.
    response.headers['Cache-Control'] = 'no-store' if is_embed else 'no-cache'
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


@app.route('/invoices-v2')
def invoices_v2_page():
    """Страница нового раздела «Согласование счетов v2» (пилот).

    Отдаёт ту же оболочку дашборда: раздел живёт внутри SPA, доступ к пункту
    меню решает секция invoices_v2 (см. ROLE_SECTIONS в src/auth.py), а доступ
    к данным — секция invoices на ручках счетов.
    """
    try:
        from flask_login import current_user
        if not current_user.is_authenticated:
            return redirect('/login')
        return _serve_dashboard_shell()
    except Exception as e:
        logger.error(f"Ошибка загрузки /invoices-v2: {e}")
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


@app.route('/courier-payouts')
def courier_payouts_page():
    """Страница оплаты курьерам"""
    try:
        from flask_login import current_user
        if not current_user.is_authenticated:
            return redirect('/login')
        return _serve_dashboard_shell()
    except Exception as e:
        logger.error(f"Ошибка загрузки /courier-payouts: {e}")
        return f"Ошибка загрузки страницы: {e}", 500


@app.route('/link-watch')
def link_watch_page():
    """Страница сторожа ссылок на товары"""
    try:
        from flask_login import current_user
        if not current_user.is_authenticated:
            return redirect('/login')
        return _serve_dashboard_shell()
    except Exception as e:
        logger.error(f"Ошибка загрузки /link-watch: {e}")
        return f"Ошибка загрузки страницы: {e}", 500


# === Static File Routes ===

@app.route('/styles.css')
def serve_styles():
    """Отдаёт стили дашборда"""
    return send_from_directory(DASHBOARD_DIR, 'styles.css')

@app.route('/ui-dialog.js')
def serve_ui_dialog():
    """Отдаёт диалоги/тосты (window.BarhatUI) — замена нативным alert/confirm,
    которые браузер игнорирует внутри iframe Пульса."""
    return send_from_directory(DASHBOARD_DIR, 'ui-dialog.js')

@app.route('/datetime.js')
def serve_datetime():
    """Отдаёт утилиты форматирования дат (window.BarhatTime)"""
    return send_from_directory(DASHBOARD_DIR, 'datetime.js')

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

@app.route('/invoices-v2.js')
def serve_invoices_v2():
    """Отдаёт скрипт нового раздела согласования счетов"""
    return send_from_directory(DASHBOARD_DIR, 'invoices-v2.js')

@app.route('/invoices-v2.css')
def serve_invoices_v2_css():
    """Отдаёт стили нового раздела согласования счетов (токены --bx-*)"""
    return send_from_directory(DASHBOARD_DIR, 'invoices-v2.css')

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

@app.route('/feedback.js')
def serve_feedback():
    """Отдаёт виджет обратной связи (грузится на всех страницах дашборда)"""
    return send_from_directory(DASHBOARD_DIR, 'feedback.js')

@app.route('/courier-payouts.js')
def serve_courier_payouts():
    """Отдаёт скрипт раздела оплаты курьерам"""
    return send_from_directory(DASHBOARD_DIR, 'courier-payouts.js')

@app.route('/link-watch.js')
def serve_link_watch():
    """Отдаёт скрипт раздела ссылок на товары"""
    return send_from_directory(DASHBOARD_DIR, 'link-watch.js')

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


# Отладочные эндпоинты /debug/module, /debug/report и /debug/report-v2 убраны:
# они были открыты без авторизации, и каждый вызов запускал полный отчёт по
# базе. Любой аноним мог этим занять оба воркера. Проверить, что модуль жив,
# теперь можно только авторизованным — через /api/quality.


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
@section_required('quality')
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
@section_required('quality')
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
@section_required('quality')
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


@app.route('/api/quality/salon-assessment', methods=['GET'])
@section_required('quality')
def get_salon_assessment():
    """
    Текстовый разбор качества по каждому салону за период.

    Query params:
        - date_from: Начальная дата (YYYY-MM-DD)
        - date_to: Конечная дата (YYYY-MM-DD)

    Разбор возвращается сразу по всем салонам: он нужен, чтобы одним взглядом
    увидеть, где в сети что проседает, а по одному салону за запрос это было бы
    N обращений вместо одного.
    """
    try:
        assessment = quality_report.get_salons_assessment(
            request.args.get('date_from'),
            request.args.get('date_to')
        )

        return jsonify({
            'success': True,
            'data': assessment
        })

    except Exception as e:
        logger.error(f"Ошибка /api/quality/salon-assessment: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/quality/data-coverage', methods=['GET'])
@section_required('quality')
def get_data_coverage():
    """
    Получить информацию о полноте данных по датам

    Query params:
        - date_from: Начальная дата (YYYY-MM-DD)
        - date_to: Конечная дата (YYYY-MM-DD)
        - granularity: 'day' (default) или 'month'

    Возвращает количество оценок за каждый период — по нему видно дыры в
    загруженной истории.
    """
    try:
        # Эндпоинт всегда отвечал 500: здесь использовалась переменная DB_PATH,
        # которой в модуле нет (есть db_path), — NameError на каждом вызове.
        # Заодно перестал читать всю таблицу tasks: считает по витрине.
        coverage = quality_report.get_data_coverage(
            request.args.get('date_from'),
            request.args.get('date_to'),
            request.args.get('granularity', 'day')
        )

        return jsonify({
            'success': True,
            'data': coverage
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
# Задачи грузятся окнами по датам и страницами внутри окна. Окно — точка, с
# которой прогон можно продолжить после обрыва; страница ограничивает объём
# ответа Pyrus (20 000 задач в одном ответе — это ~100 МБ JSON в памяти воркера)
# и сохраняется сразу, так что обрыв теряет страницу, а не весь прогресс.

QUALITY_FORM_ID = quality_report.QUALITY_FORM_ID

# Если фоновый поток не обновлял heartbeat дольше этого времени, считаем
# загрузку мёртвой (упал воркер) и разрешаем запустить новую — иначе запись
# в статусе 'started' блокировала бы кнопку до перезапуска сервиса.
SYNC_STALE_MINUTES = 15

# Лок прогона. Проверки "последний sync_log в статусе started" мало: между
# чтением статуса и стартом потока есть окно, в которое влезает второй воркер
# (их два, и в каждом крутится свой планировщик) — и оба тянут одно и то же.
# Срок жизни совпадает с SYNC_STALE_MINUTES, чтобы лок и статус в интерфейсе
# не расходились: иначе кнопка показывала бы "готово", а запуск отбивался локом.
QUALITY_SYNC_LOCK = 'pyrus-quality-sync'
QUALITY_SYNC_LOCK_TTL = SYNC_STALE_MINUTES * 60

# Размер страницы реестра. Раньше запрашивалось сразу item_count=20000 без
# item_offset: окно с бо́льшим числом задач молча обрезалось, а ответ на 20 тысяч
# задач — это ~100 МБ JSON в памяти воркера. Теперь страницами, и каждая
# сохраняется сразу — память ограничена, прогресс виден, обрыв теряет страницу.
REGISTER_PAGE_SIZE = 2000

# Автоматическая подкачка свежих оценок. Владелец работает один — руками
# нажимать «Обновить» никто не должен. Отключается PYRUS_SYNC_SCHEDULER=0
# (локальная разработка: не хочется, чтобы каждый запуск лез в боевой Pyrus).
SCHEDULER_INTERVAL_SECONDS = int(os.getenv('PYRUS_SYNC_INTERVAL_MINUTES', '60')) * 60
SCHEDULER_DAYS = int(os.getenv('PYRUS_SYNC_DAYS', '7'))
_scheduler_started = False


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


def _fetch_register_page(client, form_id, start_iso, end_iso, offset):
    """
    Загрузить одну страницу реестра формы за окно дат.

    401 обрабатывается отдельно: токен Pyrus живёт около часа, и длинная
    загрузка истории его переживала — запрос уходил мимо client.request() с его
    переавторизацией, так что весь прогон падал на середине.
    """
    def _request():
        return client.session.get(
            f'{client.api_url}forms/{form_id}/register',
            headers={'Authorization': f'Bearer {client.access_token}'},
            params={
                'include_archived': 'y',
                'item_count': REGISTER_PAGE_SIZE,
                'item_offset': offset,
                'created_after': start_iso,
                'created_before': end_iso
            },
            # без таймаута зависший Pyrus держал бы поток вечно, а запись в
            # sync_log — в статусе 'started'
            timeout=(10, 180)
        )

    response = _request()

    if response.status_code == 401:
        logger.info("Токен Pyrus истёк, переавторизуемся")
        if not client.authenticate():
            raise Exception('Ошибка авторизации: не удалось обновить токен')
        response = _request()

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
            offset = 0
            seen_ids = set()

            while True:
                tasks = _fetch_register_page(
                    client,
                    QUALITY_FORM_ID,
                    window_start.strftime('%Y-%m-%dT00:00:00Z'),
                    window_end.strftime('%Y-%m-%dT23:59:59Z'),
                    offset
                )

                if not tasks:
                    break

                # Страховка от бесконечного цикла: если Pyrus проигнорирует
                # item_offset и вернёт ту же страницу, условие «пришла полная
                # страница» будет выполняться вечно
                page_ids = {task.get('id') for task in tasks}
                if page_ids and page_ids <= seen_ids:
                    logger.warning(
                        f"Pyrus вернул повторную страницу реестра на offset={offset} "
                        f"— прекращаем пагинацию окна"
                    )
                    break
                seen_ids |= page_ids

                # Сначала сырые задачи, следом витрина отчёта — обе операции
                # пачкой, по одной транзакции на страницу
                total_saved += storage.save_tasks(QUALITY_FORM_ID, tasks)
                quality_report.upsert_tasks(tasks)

                storage.update_sync_log(
                    log_id,
                    f'Период {window_start:%d.%m.%Y}–{window_end:%d.%m.%Y} '
                    f'({index}/{len(windows)}): загружено {total_saved} задач',
                    total_saved
                )
                # Лок живёт SYNC_STALE_MINUTES — длинную загрузку продлеваем,
                # иначе второй воркер решит, что прогон умер, и начнёт свой
                storage.renew_sync_lock(QUALITY_SYNC_LOCK, QUALITY_SYNC_LOCK_TTL)

                if len(tasks) < REGISTER_PAGE_SIZE:
                    break

                offset += REGISTER_PAGE_SIZE

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
    finally:
        storage.release_sync_lock(QUALITY_SYNC_LOCK)


# ============================================================================
# Синхронизация формы «Негативная ОС по заказу» (1291124)
#
# Отдельного планировщика у неё намеренно НЕТ: в pyrus.db уже пишет цикл синка
# качества, а /data на Amvera — общий медленный диск, по которому бьют все базы
# сразу. Вторая волна записи туда же однажды уже клала сайт целиком, поэтому
# НОС грузится вторым шагом в том же тике, последовательно.
# ============================================================================

NOS_SYNC_LOCK = 'pyrus-nos-sync'
NOS_SYNC_LOCK_TTL = SYNC_STALE_MINUTES * 60

# Окно регулярной подкачки. Больше, чем у качества (7 дней): обращение правят и
# через месяц после создания — проставляют объективность, категорию, компенсацию.
NOS_SYNC_DAYS = int(os.getenv('PYRUS_NOS_SYNC_DAYS', '60'))


def sync_nos_window(days: int, step_days: int = 30) -> dict:
    """
    Загрузить обращения за последние `days` дней и обновить витрину.

    Работает синхронно в вызывающем потоке: объём смешной (сотни задач),
    заводить ради него ещё один фоновый поток незачем.
    """
    from datetime import timedelta

    if not storage.try_acquire_sync_lock(NOS_SYNC_LOCK, NOS_SYNC_LOCK_TTL):
        logger.info("Синхронизация НОС уже идёт — пропускаем")
        return {'skipped': True}

    log_id = storage.start_sync_log(job='nos', message='Загрузка негативной ОС...')
    total_saved = 0

    try:
        client = get_client()
        if not client.authenticate():
            raise Exception('Ошибка авторизации Pyrus')

        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)

        cursor_date = start_date
        while cursor_date <= end_date:
            window_end = min(cursor_date + timedelta(days=step_days - 1), end_date)
            offset = 0
            seen_ids = set()

            while True:
                tasks = _fetch_register_page(
                    client,
                    nos_report.NOS_FORM_ID,
                    cursor_date.strftime('%Y-%m-%dT00:00:00Z'),
                    window_end.strftime('%Y-%m-%dT23:59:59Z'),
                    offset
                )
                if not tasks:
                    break

                page_ids = {t.get('id') for t in tasks}
                if page_ids and page_ids <= seen_ids:
                    logger.warning("Pyrus вернул повторную страницу реестра НОС — прекращаем")
                    break
                seen_ids |= page_ids

                storage.save_tasks(nos_report.NOS_FORM_ID, tasks)
                nos_report.upsert_tasks(tasks)
                total_saved += len(tasks)

                storage.update_sync_log(log_id, f'Загружено {total_saved} обращений', total_saved)
                storage.renew_sync_lock(NOS_SYNC_LOCK, NOS_SYNC_LOCK_TTL)

                if len(tasks) < REGISTER_PAGE_SIZE:
                    break
                offset += REGISTER_PAGE_SIZE

            cursor_date = window_end + timedelta(days=1)

        storage.finish_sync_log(
            log_id, forms_count=1, tasks_count=total_saved, status='completed',
            message=f'Обновлено {total_saved} обращений'
        )
        logger.info(f"Синхронизация НОС завершена: {total_saved} обращений за {days} дн.")
        return {'saved': total_saved}

    except Exception as e:
        logger.error(f"Ошибка синхронизации НОС: {e}")
        storage.finish_sync_log(
            log_id, forms_count=1, tasks_count=total_saved, status='failed',
            error_message=str(e), message=f'Ошибка после {total_saved} обращений'
        )
        raise
    finally:
        storage.release_sync_lock(NOS_SYNC_LOCK)


@app.route('/api/pyrus/nos/sync', methods=['POST'])
@role_required('admin')
def trigger_nos_sync():
    """
    Загрузить обращения негативной ОС за последние N дней.

    Body: {"days": 365} — для первичного наполнения витрины. По умолчанию окно
    регулярной подкачки.
    """
    import threading

    data = request.get_json(silent=True) or {}
    try:
        days = max(1, min(int(data.get('days', NOS_SYNC_DAYS)), 1100))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'days должен быть числом'}), 400

    # Первичная загрузка за год идёт минуты — в фоне, чтобы не держать воркер
    thread = threading.Thread(
        target=lambda: sync_nos_window(days), daemon=True, name='pyrus-nos-import'
    )
    thread.start()

    return jsonify({'success': True, 'message': f'Загрузка негативной ОС за {days} дн. запущена'})


def _launch_import(job, start_date, end_date, step_days):
    """
    Захватить лок и запустить фоновую загрузку.

    Returns:
        (запущено ли, payload для ответа)
    """
    import threading

    if not storage.try_acquire_sync_lock(QUALITY_SYNC_LOCK, QUALITY_SYNC_LOCK_TTL):
        return False, {
            'success': False,
            'error': 'Обновление уже запущено',
            'status': _sync_status_payload(storage.get_latest_sync_log())
        }

    log_id = storage.start_sync_log(job=job, message='Запуск обновления...')

    thread = threading.Thread(
        target=_import_tasks_background,
        args=(log_id, start_date, end_date, step_days),
        daemon=True,
        name='pyrus-quality-import'
    )
    thread.start()

    return True, {
        'success': True,
        'message': 'Обновление запущено',
        'status': _sync_status_payload(storage.get_latest_sync_log())
    }


def _start_import(job, start_date, end_date, step_days):
    """HTTP-обёртка над _launch_import"""
    _, payload = _launch_import(job, start_date, end_date, step_days)
    return jsonify(payload)


def _scheduler_loop():
    """Раз в SCHEDULER_INTERVAL_SECONDS подтягивать оценки за последние дни"""
    from datetime import timedelta

    while True:
        # Пауза в начале, а не в конце: при деплое воркеры перезапускаются, и
        # синхронизация не должна стартовать одновременно со сборкой
        time.sleep(SCHEDULER_INTERVAL_SECONDS)

        try:
            # Талон на тик: планировщик крутится в каждом воркере, и лок внутри
            # _launch_import ловит только одновременный запуск. Без талона
            # второй воркер повторял загрузку тех же дней целиком.
            if not storage.try_claim_scheduled_run('quality', SCHEDULER_INTERVAL_SECONDS):
                logger.info("Тик синхронизации Pyrus уже отработал соседний воркер")
                continue

            end_date = datetime.now()
            start_date = end_date - timedelta(days=SCHEDULER_DAYS)
            started, payload = _launch_import(
                'scheduled', start_date, end_date, step_days=SCHEDULER_DAYS
            )
            if not started:
                logger.info("Плановая синхронизация Pyrus пропущена: прогон уже идёт")
        except Exception as e:
            logger.error(f"Ошибка планировщика синхронизации Pyrus: {e}")

        # Второй шаг того же тика — негативная ОС. Отдельным планировщиком её
        # не заводим: см. комментарий у NOS_SYNC_LOCK. Свой талон нужен, чтобы
        # окна двух форм можно было развести по частоте, не трогая друг друга.
        try:
            if storage.try_claim_scheduled_run('nos', SCHEDULER_INTERVAL_SECONDS):
                sync_nos_window(NOS_SYNC_DAYS)
        except Exception as e:
            logger.error(f"Ошибка плановой синхронизации НОС: {e}")


def start_quality_scheduler():
    """Запустить фоновую подкачку оценок качества"""
    global _scheduler_started

    if os.getenv('PYRUS_SYNC_SCHEDULER', '1') != '1':
        logger.info("Планировщик синхронизации Pyrus отключён (PYRUS_SYNC_SCHEDULER=0)")
        return

    if _scheduler_started:
        return
    _scheduler_started = True

    import threading
    thread = threading.Thread(target=_scheduler_loop, daemon=True, name='pyrus-quality-scheduler')
    thread.start()
    logger.info(
        f"Планировщик синхронизации Pyrus запущен "
        f"(каждые {SCHEDULER_INTERVAL_SECONDS // 60} мин, период {SCHEDULER_DAYS} дн.)"
    )


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
        # Верхняя граница — три года: дальше уходить бессмысленно (форма
        # заведена в 2023-м), а случайная опечатка не запустит прогон на
        # десятилетия. Ниже единицы период не имеет смысла.
        days = max(1, min(int(data.get('days', 7)), 1100))

        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)

        # Окно загрузки не длиннее месяца: границы окон — это точки, с которых
        # прогон можно продолжить после обрыва
        return _start_import('update', start_date, end_date, step_days=min(days, 30))

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
        step_days = max(1, min(int(data.get('step_days', 30)), 90))

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
@section_required('quality')
def get_update_status():
    """Получить статус обновления"""
    return jsonify({
        'success': True,
        'status': _sync_status_payload(storage.get_latest_sync_log())
    })


def _compact_history_background(log_id, run_vacuum):
    """Фоновая чистка истории задач с прогрессом в sync_log"""
    try:
        def progress(done, total):
            storage.update_sync_log(
                log_id, f'Удалено {done} из {total} лишних снапшотов', done
            )
            storage.renew_sync_lock(QUALITY_SYNC_LOCK, QUALITY_SYNC_LOCK_TTL)

        before = storage.database_size_mb()
        result = storage.compact_task_history(progress=progress)

        message = f"Удалено {result['deleted']} снапшотов"
        if result['remaining']:
            # За один прогон чистим ограниченное число строк, чтобы не занимать
            # диск часами: остаток снимается повторным запуском
            message += f", осталось {result['remaining']} — запустите ещё раз"

        if run_vacuum and not result['remaining']:
            storage.update_sync_log(log_id, 'Сжатие файла базы (VACUUM)...', result['deleted'])
            storage.renew_sync_lock(QUALITY_SYNC_LOCK, QUALITY_SYNC_LOCK_TTL)
            vac = storage.vacuum()
            message += f", база {vac['before_mb']} → {vac['after_mb']} МБ"
        else:
            message += f", база {before} → {storage.database_size_mb()} МБ"

        storage.finish_sync_log(
            log_id, forms_count=0, tasks_count=result['deleted'],
            status='completed', message=message
        )

    except Exception as e:
        logger.error(f"Ошибка чистки истории задач: {e}")
        storage.finish_sync_log(
            log_id, forms_count=0, tasks_count=0,
            status='failed', error_message=str(e), message='Чистка прервана'
        )
    finally:
        storage.release_sync_lock(QUALITY_SYNC_LOCK)


@app.route('/api/pyrus/history-stats', methods=['GET'])
@role_required('admin')
def get_history_stats():
    """Сколько лишних снапшотов накопилось в истории задач (ничего не меняет)"""
    try:
        return jsonify({'success': True, 'data': storage.count_task_history_duplicates()})
    except Exception as e:
        logger.error(f"Ошибка /api/pyrus/history-stats: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/pyrus/compact-history', methods=['POST'])
@role_required('admin')
def trigger_compact_history():
    """
    Удалить из истории задач все снапшоты, кроме последнего по каждой задаче.

    Разовое обслуживание после перехода на пачечную запись: старые прогоны
    оставили в базе полные копии всех задач. Отчёты историю не читают, так что
    это чистое освобождение диска.

    Body params:
        - vacuum: сжать файл базы после чистки (default: true)

    Идёт в фоне под тем же локом, что и синхронизация: одновременно тянуть
    задачи из Pyrus и переписывать ту же таблицу — плохая идея.
    """
    import threading

    data = request.get_json(silent=True) or {}
    run_vacuum = bool(data.get('vacuum', True))

    if not storage.try_acquire_sync_lock(QUALITY_SYNC_LOCK, QUALITY_SYNC_LOCK_TTL):
        return jsonify({
            'success': False,
            'error': 'База сейчас занята синхронизацией — попробуйте позже',
            'status': _sync_status_payload(storage.get_latest_sync_log())
        })

    # Всё, что может упасть ПОСЛЕ захвата лока, — под своим try: снимать лок в
    # общем обработчике нельзя, иначе ошибка до захвата освободила бы чужой лок
    # и запустила бы вторую операцию поверх идущей
    try:
        log_id = storage.start_sync_log(job='compact', message='Чистка истории задач...')

        thread = threading.Thread(
            target=_compact_history_background,
            args=(log_id, run_vacuum),
            daemon=True,
            name='pyrus-compact-history'
        )
        thread.start()

        return jsonify({
            'success': True,
            'message': 'Чистка запущена',
            'status': _sync_status_payload(storage.get_latest_sync_log())
        })

    except Exception as e:
        logger.error(f"Ошибка запуска чистки истории: {e}")
        storage.release_sync_lock(QUALITY_SYNC_LOCK)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/quality/rebuild', methods=['POST'])
@role_required('admin')
def rebuild_quality_projection():
    """
    Пересобрать витрину оценок из уже загруженных задач.

    Нужен после изменения списка видов заказа или разбора полей формы: витрина
    хранит уже разложенные значения, и старые строки сами не обновятся.
    Отдельный эндпоинт, потому что на этом тарифе Amvera нет консоли — разовые
    операции над боевой базой делаются только по HTTP.
    """
    try:
        return jsonify({
            'success': True,
            'data': quality_report.rebuild_projection()
        })
    except Exception as e:
        logger.error(f"Ошибка пересборки витрины качества: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


# Планировщик стартует в каждом воркере, но прогон делает один — за это
# отвечает лок в БД (QUALITY_SYNC_LOCK). Вызов внизу файла, потому что
# start_quality_scheduler определён выше по коду, а не в начале модуля.
start_quality_scheduler()


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
