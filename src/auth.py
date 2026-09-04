"""
Модуль авторизации и ролевого доступа для БАРХАТ (Flask).

Что делает:
- Хранит пользователей в той же SQLite базе (отдельная таблица users)
- Пароли хранятся только в виде хэша (werkzeug.security)
- Сессии через Flask-Login (secure, httponly cookie)
- Декоратор @role_required('admin') для защиты конкретных эндпоинтов
- Простая таблица audit_log для логирования действий

Как подключить: см. integration_notes.md рядом с этим файлом.
"""

import os
import re
import queue
import sqlite3
import logging
import threading
import time
from functools import wraps
from datetime import datetime

from flask import Blueprint, request, jsonify, session, current_app, has_request_context
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    login_required, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash

from sqlite_conn import connect as sqlite_connect

logger = logging.getLogger("barhat.auth")

# Путь к той же SQLite базе, где уже лежат данные Pyrus.
# Берём из env, чтобы не хардкодить путь.
DB_PATH = os.environ.get("BARHAT_DB_PATH", "barhat.db")

auth_bp = Blueprint("auth", __name__)
login_manager = LoginManager()

# Роли и какие разделы им доступны.
# Меняйте под свои реальные разделы дашборда.
ROLE_SECTIONS = {
    # invoices_v2 — раздел «Согласование счетов» (plans/2026-08-24-счета-новый-раздел.md).
    # С 2026-08-26 это основной раздел счетов для всех, старый «Счета (архив)»
    # остаётся рабочим, но виден только админу.
    #
    # Секция invoices у сотрудников намеренно ОСТАЁТСЯ: пункт меню всё равно
    # скрыт (script.js прячет старый раздел у всех, кроме админа), а лишнее
    # снятие права закрыло бы доступ к данным тем, кого забыли перевести.
    # Ручки счетов проверяют @section_required("invoices", "invoices_v2") —
    # пускает любая из двух секций (INVOICE_SECTIONS в src/invoices/server.py).
    "admin": {"dashboard", "quality", "calculator", "price_edit", "users_manage", "cash_shifts", "invoices", "invoices_v2", "abc_analysis", "writeoffs", "courier_payouts", "link_watch", "salon_kpi"},
    "manager": {"dashboard", "quality", "calculator", "cash_shifts", "invoices", "invoices_v2", "writeoffs", "courier_payouts", "salon_kpi"},
    "florist": {"cash_shifts", "writeoffs"},
    "florist_analyst": {"quality"},
    # Пользователи, залогиненные через SSO из портала БАРХАТ Пульс (см. src/sso.py).
    # Роль в Пульсе (director/manager/...) на внутренние права намеренно не мапится:
    # все входящие через портал получают один и тот же набор — всё, КРОМЕ
    # управления пользователями. Админку через внешний JWT не открываем, иначе
    # пропуск Пульса позволял бы заводить и править учётки в нашем сервисе.
    "sso_viewer": {"dashboard", "quality", "calculator", "cash_shifts", "invoices", "invoices_v2", "abc_analysis", "writeoffs", "courier_payouts", "salon_kpi"},
}


# Рабочий email сотрудника — это ключ, по которому портал БАРХАТ Пульс
# опознаёт учётку при SSO-входе (см. _find_or_create_sso_user в src/sso.py:
# поиск идёт строго по users.email). Пока email не заполнен, вход из Пульса
# создаёт ОТДЕЛЬНУЮ учётку с урезанной ролью sso_viewer, и человек теряет
# свои права: именно так у управляющей перестало работать согласование
# списаний внутри Пульса, хотя на нашем домене всё работало.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_email(raw):
    """('  Ivan@Mail.RU ') -> 'ivan@mail.ru'; пусто -> None.

    Приводим к нижнему регистру, потому что sso.py ищет по email из JWT
    ровно так же (`.strip().lower()`) — иначе «Ivan@» и «ivan@» разъедутся
    в две учётки, а UNIQUE-индекс по email этого не поймает.

    Returns:
        (email|None, error|None)
    """
    email = (raw or "").strip().lower()
    if not email:
        return None, None
    if len(email) > 254 or not _EMAIL_RE.match(email):
        return None, "Некорректный email"
    return email, None


def _email_taken_message(email, row):
    """Понятный текст вместо «UNIQUE constraint failed».

    Отдельно подсказываем про автосозданную учётку из Пульса — это самый
    частый случай: человек уже заходил через портал, ему завели дубль, и
    теперь тот же email нельзя привязать к его настоящей учётке.
    """
    other = row["username"]
    if row["is_sso"]:
        return (
            f"Email {email} уже занят учётной записью «{other}», которая входит через Пульс. "
            f"Если это автоматически созданный дубль (роль «Из Пульса»), удалите его и "
            f"повторите — тогда вход из Пульса будет попадать сюда, с этой ролью и точками."
        )
    return f"Email {email} уже привязан к учётной записи «{other}»"


def get_db():
    # Все настройки соединения (busy_timeout, synchronous, однократный WAL)
    # живут в sqlite_conn — там же объяснено, почему на сетевом диске Amvera
    # они решают судьбу отзывчивости всего сайта.
    return sqlite_connect(DB_PATH, timeout=20)


def migrate_users_from_old_db():
    """
    Миграция пользователей из старой локальной базы в новую persistent базу.
    Вызывается при первом запуске после смены BARHAT_DB_PATH.
    """
    # Пути к базам
    new_db = DB_PATH
    old_db_candidates = [
        "barhat.db",  # Локальная база в рабочей директории
        "/app/barhat.db",  # Старый путь в контейнере
        os.path.join(os.path.dirname(__file__), "../barhat.db"),  # Относительно src/
    ]

    # Проверяем, что новая база пустая
    conn_new = sqlite3.connect(new_db)
    conn_new.row_factory = sqlite3.Row
    try:
        result = conn_new.execute("SELECT COUNT(*) FROM users").fetchone()
        count_new = result[0] if result else 0
    except sqlite3.OperationalError:
        # Таблицы ещё нет
        count_new = 0
    conn_new.close()

    if count_new > 0:
        # В новой базе уже есть пользователи — не мигрируем
        logger.info(f"Migration: new DB already has {count_new} users, skipping")
        return

    # Ищем старую базу с пользователями
    for old_db in old_db_candidates:
        if not os.path.exists(old_db):
            continue

        try:
            conn_old = sqlite3.connect(old_db)
            conn_old.row_factory = sqlite3.Row
            result = conn_old.execute("SELECT COUNT(*) FROM users").fetchone()
            count_old = result[0] if result else 0

            if count_old == 0:
                conn_old.close()
                continue

            # Нашли базу с пользователями — мигрируем
            logger.info(f"Migration: found {count_old} users in {old_db}, migrating to {new_db}")

            # Проверяем наличие колонки full_name в старой базе
            cursor = conn_old.execute("PRAGMA table_info(users)")
            old_columns = [row[1] for row in cursor.fetchall()]
            has_full_name = 'full_name' in old_columns

            # Получаем всех пользователей
            if has_full_name:
                users = conn_old.execute(
                    "SELECT username, full_name, password_hash, role, is_active, created_at FROM users"
                ).fetchall()
            else:
                users = conn_old.execute(
                    "SELECT username, password_hash, role, is_active, created_at FROM users"
                ).fetchall()

            # Вставляем в новую базу
            conn_new = sqlite3.connect(new_db)
            conn_new.row_factory = sqlite3.Row
            for user in users:
                try:
                    if has_full_name:
                        conn_new.execute(
                            "INSERT INTO users (username, full_name, password_hash, role, is_active, created_at) VALUES (?,?,?,?,?,?)",
                            (user["username"], user["full_name"], user["password_hash"], user["role"], user["is_active"], user["created_at"])
                        )
                    else:
                        # Используем username как full_name
                        conn_new.execute(
                            "INSERT INTO users (username, full_name, password_hash, role, is_active, created_at) VALUES (?,?,?,?,?,?)",
                            (user["username"], user["username"], user["password_hash"], user["role"], user["is_active"], user["created_at"])
                        )
                except sqlite3.IntegrityError:
                    # Пользователь уже существует (дубликат)
                    pass

            conn_new.commit()
            conn_new.close()
            conn_old.close()

            logger.info(f"Migration: successfully migrated {len(users)} users")
            return

        except (sqlite3.OperationalError, sqlite3.Error) as e:
            # База существует, но нет таблиц users или другая ошибка
            logger.debug(f"Migration: {old_db} not suitable for migration: {e}")
            try:
                conn_old.close()
            except:
                pass
            continue

    logger.info("Migration: no suitable old DB found, starting fresh")


def migrate_permissions_for_existing_users():
    """
    Миграция permissions для существующих пользователей на основе ролей.
    Вызывается при старте, если у пользователя нет permissions.
    """
    conn = get_db()

    try:
        # Получаем всех пользователей без permissions
        users_without_perms = conn.execute("""
            SELECT username, role FROM users
            WHERE username NOT IN (SELECT DISTINCT username FROM permissions)
        """).fetchall()

        if not users_without_perms:
            conn.close()
            return

        logger.info(f"Migration: adding permissions for {len(users_without_perms)} existing users")

        for user in users_without_perms:
            username = user["username"]
            role = user["role"]

            # Получаем модули для роли из ROLE_SECTIONS
            modules = ROLE_SECTIONS.get(role, set())

            # Вставляем permissions
            for module in modules:
                try:
                    conn.execute(
                        "INSERT INTO permissions (username, module_name, can_view) VALUES (?, ?, 1)",
                        (username, module)
                    )
                except sqlite3.IntegrityError:
                    pass  # Уже существует

        conn.commit()
        logger.info("Migration: permissions added for existing users")

    except Exception as e:
        logger.error(f"Migration error for permissions: {e}")
    finally:
        conn.close()


def migrate_new_module_permissions(module_name: str, roles: list):
    """
    Догрузить права на новый модуль пользователям с указанными ролями,
    у которых он ещё не выдан.

    migrate_permissions_for_existing_users() backfill'ит роли только для
    пользователей БЕЗ единой записи в permissions — при добавлении нового
    модуля к уже существующему приложению (как invoices) у всех активных
    пользователей permissions уже есть, и они этот бэкафилл не проходят.
    Отсюда и баг: пункт меню невидим, пока не выдать право явно.
    """
    if not roles:
        return

    conn = get_db()
    try:
        placeholders = ",".join("?" * len(roles))
        users_missing = conn.execute(
            f"""
            SELECT username FROM users
            WHERE role IN ({placeholders})
              AND username NOT IN (
                  SELECT username FROM permissions WHERE module_name = ?
              )
            """,
            (*roles, module_name)
        ).fetchall()

        if not users_missing:
            conn.close()
            return

        for user in users_missing:
            try:
                conn.execute(
                    "INSERT INTO permissions (username, module_name, can_view) VALUES (?, ?, 1)",
                    (user["username"], module_name)
                )
            except sqlite3.IntegrityError:
                pass

        conn.commit()
        logger.info(f"Migration: добавлено право '{module_name}' для {len(users_missing)} пользователей")

    except Exception as e:
        logger.error(f"Migration error for module '{module_name}': {e}")
    finally:
        conn.close()


# Все модули системы
ALL_MODULES = [
    'dashboard',      # Дашборд
    'calculator',     # Калькулятор букетов
    'quality',        # Качество сборки
    'cash_shifts',    # Кассовые смены
    'invoices',       # Счета на оплату
    'invoices_v2',    # Согласование счетов (основной раздел с 2026-08-26)
    'abc_analysis',   # ABC-анализ товаров
    'users_manage',   # Управление пользователями
    'writeoffs',      # Списания товара
    'courier_payouts',  # Оплата курьерам
    'link_watch',     # Ссылки на товары (сторож)
]

# Модули, доступные пользователям, вошедшим через SSO из портала БАРХАТ Пульс.
# Всё, кроме админки: управление учётками не должно открываться по внешнему
# пропуску. Список задан явно (а не вычитанием из ALL_MODULES), чтобы при
# добавлении нового модуля решение «пускать ли туда портал» принималось руками.
SSO_MODULES = [
    'dashboard',
    'calculator',
    'quality',
    'cash_shifts',
    'invoices',
    'invoices_v2',
    'abc_analysis',
    'writeoffs',
    'courier_payouts',
]


def init_auth_tables():
    """Вызвать один раз при старте приложения (создаёт таблицы, если их нет)."""
    conn = get_db()

    # Создаём users с full_name если не существует
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            full_name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )
    """)

    # Миграция: добавляем full_name если таблица старая
    try:
        cursor = conn.execute("PRAGMA table_info(users)")
        columns = [row[1] for row in cursor.fetchall()]

        if 'full_name' not in columns:
            logger.info("Migration: adding full_name column to users")
            # Добавляем колонку full_name (временно NULL для миграции)
            conn.execute("ALTER TABLE users ADD COLUMN full_name TEXT")
            conn.commit()

            # Заполняем username как full_name для существующих
            conn.execute("UPDATE users SET full_name = username WHERE full_name IS NULL")
            conn.commit()

            # Теперь делаем NOT NULL через пересоздание таблицы
            conn.execute("""
                CREATE TABLE users_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    full_name TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                INSERT INTO users_new (id, username, full_name, password_hash, role, is_active, created_at)
                SELECT id, username, full_name, password_hash, role, is_active, created_at FROM users
            """)
            conn.execute("DROP TABLE users")
            conn.execute("ALTER TABLE users_new RENAME TO users")
            conn.commit()
            logger.info("Migration: full_name column added and populated")
    except Exception as e:
        logger.error(f"Migration error for full_name: {e}")

    # Миграция: колонки для SSO-входа из БАРХАТ Пульс (см. src/sso.py)
    try:
        cursor = conn.execute("PRAGMA table_info(users)")
        columns = [row[1] for row in cursor.fetchall()]

        if 'email' not in columns:
            logger.info("Migration: adding email column to users")
            conn.execute("ALTER TABLE users ADD COLUMN email TEXT")
            conn.commit()

        if 'is_sso' not in columns:
            logger.info("Migration: adding is_sso column to users")
            conn.execute("ALTER TABLE users ADD COLUMN is_sso INTEGER NOT NULL DEFAULT 0")
            conn.commit()

        # Уникальный индекс на email — без него find_or_create по email в sso.py
        # мог бы создать дубликаты при гонке двух воркеров gunicorn
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email) WHERE email IS NOT NULL"
        )
        conn.commit()
    except Exception as e:
        logger.error(f"Migration error for email/is_sso: {e}")

    # Таблица permissions
    conn.execute("""
        CREATE TABLE IF NOT EXISTS permissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            module_name TEXT NOT NULL,
            can_view INTEGER NOT NULL DEFAULT 1,
            UNIQUE(username, module_name),
            FOREIGN KEY (username) REFERENCES users(username) ON DELETE CASCADE
        )
    """)

    # Таблица audit_log
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            action TEXT NOT NULL,
            details TEXT,
            ip TEXT,
            created_at TEXT NOT NULL
        )
    """)

    conn.commit()
    conn.close()

    # Попытка миграции из старой базы
    migrate_users_from_old_db()

    # Миграция permissions для существующих пользователей
    migrate_permissions_for_existing_users()

    # Догрузка права на модуль invoices тем, у кого permissions уже были
    # созданы до появления этого модуля (см. docstring migrate_new_module_permissions)
    migrate_new_module_permissions("invoices", ["admin", "manager"])

    # Догрузка права на модуль abc_analysis (по умолчанию только admin)
    migrate_new_module_permissions("abc_analysis", ["admin"])

    # Догрузка права на модуль writeoffs (списания товара)
    migrate_new_module_permissions("writeoffs", ["admin", "manager", "florist"])

    # Догрузка права на модуль courier_payouts (оплата курьерам)
    migrate_new_module_permissions("courier_payouts", ["admin", "manager"])

    # Догрузка права на раздел link_watch (сторож ссылок на товары).
    # Только админ: раздел технический — он про исправность выгрузки каталога
    # в CRM, а не про ежедневную работу с заказами.
    migrate_new_module_permissions("link_watch", ["admin"])

    # Догрузка права на раздел invoices_v2 («Согласование счетов»).
    # С приёмкой Фазы 10 (2026-08-26) он стал основным разделом счетов, поэтому
    # выдаётся всем, у кого раньше был старый раздел. Старую секцию `invoices`
    # при этом НЕ снимаем — см. комментарий у ROLE_SECTIONS.
    migrate_new_module_permissions("invoices_v2", ["admin", "manager", "sso_viewer"])

    # Догрузка права на раздел salon_kpi («Показатели салонов»). Управляющие
    # заходят и своими логинами (manager), и через Пульс (sso_viewer) — право
    # нужно обеим ролям, иначе половина людей раздела просто не увидит.
    migrate_new_module_permissions("salon_kpi", ["admin", "manager", "sso_viewer"])

    # Догрузка прав SSO-пользователям: первые из них были заведены, когда
    # sso_viewer имел доступ только к "quality" (см. SSO_MODULES выше).
    for module in SSO_MODULES:
        migrate_new_module_permissions(module, ["sso_viewer"])


class User(UserMixin):
    def __init__(self, row, permissions=None):
        # permissions передаёт load_user — он забирает их тем же соединением,
        # что и саму учётку. Если не передали (вход, SSO), сработает ленивая
        # догрузка в свойстве ниже.
        if permissions is not None:
            self._permissions = set(permissions)
        self.id = row["id"]
        self.username = row["username"]
        # Фоллбэк на username если full_name отсутствует
        try:
            self.full_name = row["full_name"] if row["full_name"] else row["username"]
        except (KeyError, IndexError):
            self.full_name = row["username"]
        self.role = row["role"]
        self.is_active_flag = row["is_active"]

    @property
    def is_active(self):
        return bool(self.is_active_flag)

    @property
    def display_name(self):
        """Отображаемое имя - ФИО или username"""
        return self.full_name if self.full_name else self.username

    @property
    def permissions(self):
        """Получить список разрешённых модулей"""
        if not hasattr(self, '_permissions'):
            conn = get_db()
            rows = conn.execute(
                "SELECT module_name FROM permissions WHERE username = ? AND can_view = 1",
                (self.username,)
            ).fetchall()
            conn.close()
            self._permissions = {row["module_name"] for row in rows}
        return self._permissions

    def has_module_access(self, module_name):
        """Проверить доступ к модулю"""
        return module_name in self.permissions


@login_manager.user_loader
def load_user(user_id):
    """Пользователь и его права — за одно соединение.

    Права раньше догружались лениво, отдельным соединением: на защищённой
    ручке (а это почти все) один HTTP-запрос открывал базу дважды. На сетевом
    диске Amvera открытие соединения — это уже три файла (.db, -wal, -shm) и
    десятки миллисекунд, поэтому второй SELECT по тому же соединению дешевле
    второго соединения на порядок.
    """
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            return None
        permissions = {
            r["module_name"] for r in conn.execute(
                "SELECT module_name FROM permissions WHERE username = ? AND can_view = 1",
                (row["username"],)
            )
        }
    finally:
        conn.close()

    return User(row, permissions=permissions)


# ============================================================================
# Аудит действий — пишется асинхронно
#
# log_action() зовётся из горячего пути: каждое согласование счёта, закрытие
# смены, вход. Раньше он открывал СВОЁ соединение и делал commit прямо посреди
# обработки запроса, то есть добавлял к каждому действию открытие базы плюс
# fsync — на сетевом диске /data это десятки-сотни миллисекунд, а во время
# фонового синка, когда дисковая очередь занята, и секунды.
#
# Побочно это лечит давнюю ловушку с вложенными соединениями: log_action,
# вызванный, пока у вызывающего кода открыта своя незакоммиченная транзакция
# к тому же файлу, ждал сам себя до busy_timeout. Из очереди запись уходит
# уже после того, как обработчик отпустил своё соединение.
# ============================================================================

# maxsize — предохранитель от утечки памяти, если писатель умрёт: аудит важен,
# но не настолько, чтобы ради него съесть память воркера. Переполнение только
# логируется и никогда не роняет пользовательский запрос.
_audit_queue = queue.Queue(maxsize=10000)
_audit_writer_started = False
_audit_writer_lock = threading.Lock()

# Сколько записей писать одной транзакцией. Пачка из накопившегося — это один
# fsync на всю пачку вместо одного на каждое действие.
_AUDIT_BATCH_SIZE = 200


def _audit_writer_loop():
    while True:
        # Блокирующее ожидание первой записи: поток спит, пока никто ничего
        # не делает, и не крутит пустой цикл (такой цикл однажды уже
        # подъедал CPU в фоновом синке).
        batch = [_audit_queue.get()]
        while len(batch) < _AUDIT_BATCH_SIZE:
            try:
                batch.append(_audit_queue.get_nowait())
            except queue.Empty:
                break

        conn = None
        try:
            conn = get_db()
            conn.executemany(
                "INSERT INTO audit_log (username, action, details, ip, created_at)"
                " VALUES (?,?,?,?,?)",
                batch,
            )
            conn.commit()
        except Exception as e:
            # Потерянная строка аудита не повод убивать писателя: следующая
            # пачка должна записаться.
            logger.error(f"Не удалось записать аудит ({len(batch)} строк): {e}")
        finally:
            if conn is not None:
                conn.close()
            # task_done ровно столько раз, сколько было get() — иначе
            # flush_audit_log() зависнет на join().
            for _ in batch:
                _audit_queue.task_done()


def _ensure_audit_writer():
    """Поднять писателя при первом использовании.

    Именно лениво, а не при импорте: gunicorn форкает воркеры, и поток,
    созданный до форка, в них бы не выжил.
    """
    global _audit_writer_started

    if _audit_writer_started:
        return

    with _audit_writer_lock:
        if _audit_writer_started:
            return
        threading.Thread(
            target=_audit_writer_loop, daemon=True, name="audit-log-writer"
        ).start()
        _audit_writer_started = True


def flush_audit_log(timeout=5.0):
    """Дождаться записи всего, что стоит в очереди.

    Нужно там, где аудит читают сразу после действия: диагностика SSO-входа
    и тесты. В обычной работе звать не надо — смысл очереди в том, чтобы
    пользователь её не ждал.

    Returns: True — очередь разошлась, False — не успела за timeout.
    """
    if not _audit_writer_started:
        return _audit_queue.empty()

    deadline = time.monotonic() + timeout
    while _audit_queue.unfinished_tasks:
        if time.monotonic() > deadline:
            return False
        time.sleep(0.02)
    return True


def log_action(username, action, details=""):
    # remote_addr снимаем здесь: в фоновом потоке контекста запроса уже нет.
    ip = request.remote_addr if has_request_context() else None
    record = (username, action, details, ip, datetime.utcnow().isoformat())

    _ensure_audit_writer()
    try:
        _audit_queue.put_nowait(record)
    except queue.Full:
        logger.error(f"Очередь аудита переполнена, запись потеряна: {username} {action}")


def role_required(*allowed_roles):
    """Декоратор: @role_required('admin', 'manager') на конкретный роут."""
    def decorator(fn):
        @wraps(fn)
        @login_required
        def wrapper(*args, **kwargs):
            if current_user.role not in allowed_roles:
                log_action(current_user.username, "access_denied", request.path)
                return jsonify({"error": "Недостаточно прав"}), 403
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def section_required(*section_names):
    """Декоратор по названию раздела дашборда.
    Проверяет permissions в БД, с фоллбэком на ROLE_SECTIONS для обратной совместимости.

    Секций можно передать несколько — тогда пускает ЛЮБАЯ из них
    (`@section_required("invoices", "invoices_v2")`). Это нужно там, где один
    набор ручек обслуживает два раздела сразу: счета живут в старом разделе
    (`invoices`) и в новом (`invoices_v2`), и сотруднику, переведённому на
    новый раздел, старую секцию снимут — данные он терять при этом не должен.
    """
    if not section_names:
        raise ValueError("section_required требует хотя бы одну секцию")

    def decorator(fn):
        @wraps(fn)
        @login_required
        def wrapper(*args, **kwargs):
            # Сначала проверяем по permissions (новая система)
            if any(current_user.has_module_access(name) for name in section_names):
                return fn(*args, **kwargs)

            # Фоллбэк на ROLE_SECTIONS для обратной совместимости
            allowed = ROLE_SECTIONS.get(current_user.role, set())
            if any(name in allowed for name in section_names):
                return fn(*args, **kwargs)

            log_action(current_user.username, "access_denied", " / ".join(section_names))
            return jsonify({"error": "Недостаточно прав"}), 403
        return wrapper
    return decorator


# Заголовок, которым фронт дашборда помечает свои запросы. Значение неважно —
# важен сам факт кастомного заголовка.
AJAX_HEADER = "X-Requested-With"
AJAX_HEADER_VALUE = "barhat-dashboard"


def require_ajax_header(fn):
    """Простая защита POST-ручки от межсайтовой подделки запроса (CSRF).

    ЗАЧЕМ. CSRF-токенов в проекте нет, и единственной защитой служит
    SESSION_COOKIE_SAMESITE="Lax" (см. src/pyrus/server.py). Но для сессий,
    выданных через SSO из портала БАРХАТ Пульс, кука уходит с SameSite=None —
    иначе Chrome режет её внутри чужого <iframe> (_PartitionedSsoSessionInterface
    там же). То есть у тех, кто ходит через портал — а это большинство
    сотрудников, — защиты Lax нет вообще, и сторонняя страница может слать
    POST от их имени.

    КАК РАБОТАЕТ. Браузер не даёт добавить кастомный заголовок к межсайтовому
    запросу без CORS-предпроверки, а её мы не одобряем: ответ на OPTIONS не
    содержит Access-Control-Allow-Headers, поэтому сам POST не уходит.
    Обычные формы и <img>/<script> кастомных заголовков ставить не умеют вовсе.

    Свой домен это не задевает: запросы дашборда идут на собственный origin,
    предпроверка для них не нужна.

    Вешать на новые POST-ручки, доступные широкому кругу ролей. К старым
    ручкам не применять задним числом без правки их фронтенда — они начнут
    отвечать 403.
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if request.headers.get(AJAX_HEADER) != AJAX_HEADER_VALUE:
            logger.warning(
                "Запрос без %s на %s (origin=%s)",
                AJAX_HEADER, request.path, request.headers.get("Origin"),
            )
            return jsonify({"error": "Запрос отклонён"}), 403
        return fn(*args, **kwargs)
    return wrapper


# ---------- Роуты авторизации ----------

@auth_bp.route("/api/auth/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    username = data.get("username", "").strip()
    password = data.get("password", "")

    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()

    if not row or not check_password_hash(row["password_hash"], password):
        logger.warning("Неудачная попытка входа: %s", username)
        return jsonify({"error": "Неверный логин или пароль"}), 401

    if not row["is_active"]:
        return jsonify({"error": "Учётная запись деактивирована"}), 403

    user = User(row)
    # Без permanent кука сессии — сеансовая: она умирает вместе с закрытым
    # браузером, а объявленный в конфиге PERMANENT_SESSION_LIFETIME (8 часов)
    # не применяется вовсе. Наружу это выходило внезапными 401 в работающей
    # вкладке, которые читались как поломка того раздела, где нажали кнопку
    # (26.08.2026 — «сломалась» синхронизация с ПланФактом).
    # Flask при этом продлевает куку на каждом запросе, поэтому 8 часов
    # считаются от последней активности, а не от входа.
    session.permanent = True
    login_user(user, remember=False)
    log_action(username, "login")
    return jsonify({"username": user.username, "role": user.role})


@auth_bp.route("/api/auth/logout", methods=["POST"])
@login_required
def logout():
    log_action(current_user.username, "logout")
    logout_user()
    return jsonify({"ok": True})


@auth_bp.route("/api/auth/me", methods=["GET"])
@login_required
def me():
    # Для флориста добавляем store_id
    store_id = None
    if current_user.role == "florist":
        try:
            from cashshifts.storage import get_user_stores
            user_stores = get_user_stores(current_user.username)
            if user_stores:
                store_id = user_stores[0]
        except ImportError:
            pass

    return jsonify({
        "username": current_user.username,
        "full_name": current_user.full_name,
        "role": current_user.role,
        "display_name": current_user.display_name,
        "sections": sorted(current_user.permissions),
        "all_modules": ALL_MODULES,
        "store_id": store_id,
    })


# ---------- Управление пользователями (только admin) ----------

@auth_bp.route("/api/auth/modules", methods=["GET"])
@role_required("admin")
def get_modules():
    """Получить список всех модулей системы"""
    return jsonify({"modules": ALL_MODULES})

@auth_bp.route("/api/auth/users", methods=["POST"])
@role_required("admin")
def create_user():
    """Создать сотрудника. Пароль генерируется/задаётся один раз."""
    data = request.get_json(silent=True) or {}
    username = data.get("username", "").strip()
    full_name = data.get("full_name", "").strip()
    password = data.get("password", "")
    role = data.get("role", "")
    permissions = data.get("permissions", [])  # Список модулей

    email, email_error = normalize_email(data.get("email"))
    if email_error:
        return jsonify({"error": email_error}), 400

    if role not in ROLE_SECTIONS:
        return jsonify({"error": f"Неизвестная роль. Доступны: {list(ROLE_SECTIONS)}"}), 400
    if not username:
        return jsonify({"error": "Логин обязателен"}), 400
    if not full_name:
        return jsonify({"error": "ФИО обязательно"}), 400
    if len(password) < 8:
        return jsonify({"error": "Пароль минимум 8 символов"}), 400

    # Валидация permissions
    if not isinstance(permissions, list):
        return jsonify({"error": "permissions должен быть списком"}), 400
    invalid_modules = set(permissions) - set(ALL_MODULES)
    if invalid_modules:
        return jsonify({"error": f"Неизвестные модули: {list(invalid_modules)}"}), 400

    conn = get_db()
    try:
        # Email занят другой учёткой — чаще всего это автосозданный аккаунт
        # из Пульса (is_sso=1). Отдаём понятный текст, а не «UNIQUE constraint».
        if email:
            taken = conn.execute(
                "SELECT username, is_sso FROM users WHERE email = ?", (email,)
            ).fetchone()
            if taken:
                return jsonify({"error": _email_taken_message(email, taken)}), 409

        # Создаём пользователя
        conn.execute(
            "INSERT INTO users (username, full_name, email, password_hash, role, is_active, created_at) VALUES (?,?,?,?,?,1,?)",
            (username, full_name, email, generate_password_hash(password), role, datetime.utcnow().isoformat()),
        )

        # Добавляем permissions
        for module in permissions:
            conn.execute(
                "INSERT INTO permissions (username, module_name, can_view) VALUES (?, ?, 1)",
                (username, module)
            )

        conn.commit()
    except sqlite3.IntegrityError as e:
        # Гонка с параллельным воркером либо занятый логин. Различаем по тексту:
        # индекс на email называется idx_users_email (см. init_auth_tables).
        if "email" in str(e).lower():
            return jsonify({"error": f"Email {email} уже привязан к другой учётной записи"}), 409
        return jsonify({"error": "Такой логин уже существует"}), 409
    finally:
        conn.close()

    log_action(current_user.username, "create_user", f"{username} ({full_name})")
    return jsonify({"ok": True, "username": username, "full_name": full_name, "role": role}), 201


@auth_bp.route("/api/auth/users/<username>/deactivate", methods=["POST"])
@role_required("admin")
def deactivate_user(username):
    conn = get_db()
    conn.execute("UPDATE users SET is_active = 0 WHERE username = ?", (username,))
    conn.commit()
    conn.close()
    log_action(current_user.username, "deactivate_user", username)
    return jsonify({"ok": True})


@auth_bp.route("/api/auth/users/<username>/activate", methods=["POST"])
@role_required("admin")
def activate_user(username):
    """Активировать деактивированного пользователя"""
    conn = get_db()
    conn.execute("UPDATE users SET is_active = 1 WHERE username = ?", (username,))
    conn.commit()
    conn.close()
    log_action(current_user.username, "activate_user", username)
    return jsonify({"ok": True})


@auth_bp.route("/api/auth/users", methods=["GET"])
@role_required("admin")
def list_users():
    """Получить список всех пользователей (только для admin)"""
    conn = get_db()
    rows = conn.execute(
        "SELECT id, username, full_name, email, is_sso, role, is_active, created_at "
        "FROM users ORDER BY created_at DESC"
    ).fetchall()

    # Получаем permissions для каждого пользователя
    try:
        from cashshifts.storage import get_user_stores_with_details
    except ImportError:
        get_user_stores_with_details = None

    users = []
    for row in rows:
        user = dict(row)
        username = user["username"]
        perms = conn.execute(
            "SELECT module_name FROM permissions WHERE username = ? AND can_view = 1",
            (username,)
        ).fetchall()
        user["permissions"] = [p["module_name"] for p in perms]
        user["stores"] = get_user_stores_with_details(username) if get_user_stores_with_details else []
        users.append(user)

    conn.close()
    return jsonify({"users": users, "all_modules": ALL_MODULES})


@auth_bp.route("/api/auth/users/<username>", methods=["PUT", "PATCH"])
@role_required("admin")
def update_user(username):
    """Обновить пользователя (full_name, пароль, роль, permissions)"""
    data = request.get_json(silent=True) or {}

    # Проверяем, что пользователь существует
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()

    if not row:
        conn.close()
        return jsonify({"error": "Пользователь не найден"}), 404

    updates = []
    params = []
    log_details = []

    # Обновление ФИО
    if data.get("full_name") is not None:
        full_name = data.get("full_name", "").strip()
        if not full_name:
            conn.close()
            return jsonify({"error": "ФИО не может быть пустым"}), 400
        updates.append("full_name = ?")
        params.append(full_name)
        log_details.append(f"full_name={full_name}")

    # Обновление email — ключ связки с Пульсом (см. normalize_email выше).
    # Пустая строка = отвязать (email снимается), поэтому проверяем именно
    # наличие ключа, а не истинность значения.
    if "email" in data:
        email, email_error = normalize_email(data.get("email"))
        if email_error:
            conn.close()
            return jsonify({"error": email_error}), 400

        if email:
            taken = conn.execute(
                "SELECT username, is_sso FROM users WHERE email = ? AND username != ?",
                (email, username),
            ).fetchone()
            if taken:
                conn.close()
                return jsonify({"error": _email_taken_message(email, taken)}), 409

        updates.append("email = ?")
        params.append(email)
        log_details.append(f"email={email or '—'}")

    # Обновление пароля
    if data.get("password"):
        password = data.get("password", "")
        if len(password) < 8:
            conn.close()
            return jsonify({"error": "Пароль минимум 8 символов"}), 400
        updates.append("password_hash = ?")
        params.append(generate_password_hash(password))
        log_details.append("password")

    # Обновление роли
    if data.get("role"):
        role = data.get("role")
        if role not in ROLE_SECTIONS:
            conn.close()
            return jsonify({"error": f"Неизвестная роль. Доступны: {list(ROLE_SECTIONS)}"}), 400
        updates.append("role = ?")
        params.append(role)
        log_details.append(f"role={role}")

    # Обновление permissions
    if "permissions" in data:
        permissions = data.get("permissions", [])

        if not isinstance(permissions, list):
            conn.close()
            return jsonify({"error": "permissions должен быть списком"}), 400

        invalid_modules = set(permissions) - set(ALL_MODULES)
        if invalid_modules:
            conn.close()
            return jsonify({"error": f"Неизвестные модули: {list(invalid_modules)}"}), 400

        # Удаляем старые permissions
        conn.execute("DELETE FROM permissions WHERE username = ?", (username,))

        # Добавляем новые
        for module in permissions:
            conn.execute(
                "INSERT INTO permissions (username, module_name, can_view) VALUES (?, ?, 1)",
                (username, module)
            )

        log_details.append(f"permissions={permissions}")

    if not updates and "permissions" not in data:
        conn.close()
        return jsonify({"error": "Нечего обновлять"}), 400

    # Выполняем обновление полей users
    if updates:
        params.append(username)
        conn.execute(
            f"UPDATE users SET {', '.join(updates)} WHERE username = ?",
            params
        )

    conn.commit()
    conn.close()

    log_action(current_user.username, "update_user", f"{username}: {', '.join(log_details)}")
    return jsonify({"ok": True, "username": username})


@auth_bp.route("/api/auth/users/<username>", methods=["DELETE"])
@role_required("admin")
def delete_user(username):
    """Удалить пользователя (безопасное удаление - деактивация)"""
    # Нельзя удалить самого себя
    if current_user.username == username:
        return jsonify({"error": "Нельзя удалить самого себя"}), 400

    conn = get_db()
    conn.execute("DELETE FROM users WHERE username = ?", (username,))
    conn.commit()
    conn.close()
    log_action(current_user.username, "delete_user", username)
    return jsonify({"ok": True})


# ---------- Привязка пользователей к точкам продаж (кассовые смены) ----------

@auth_bp.route("/api/auth/users/<username>/stores", methods=["GET"])
@role_required("admin")
def get_user_stores_endpoint(username):
    """Получить точки продаж, привязанные к пользователю."""
    try:
        from cashshifts.storage import get_user_stores_with_details
    except ImportError:
        return jsonify({"error": "Модуль кассовых смен недоступен"}), 503

    return jsonify({"stores": get_user_stores_with_details(username)})


@auth_bp.route("/api/auth/users/<username>/stores", methods=["POST"])
@role_required("admin")
def set_user_stores_endpoint(username):
    """
    Привязать пользователя к точкам продаж (заменяет существующие связи).

    Body:
        - store_ids (list[int]): ID точек продаж

    Для роли florist допускается ровно одна точка.
    """
    try:
        from cashshifts.storage import set_user_stores, get_all_stores, get_user_stores_with_details
    except ImportError:
        return jsonify({"error": "Модуль кассовых смен недоступен"}), 503

    conn = get_db()
    row = conn.execute("SELECT role FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Пользователь не найден"}), 404

    data = request.get_json(silent=True) or {}
    store_ids = data.get("store_ids")

    if not isinstance(store_ids, list) or not all(isinstance(sid, int) for sid in store_ids):
        return jsonify({"error": "store_ids должен быть списком целых чисел"}), 400

    valid_ids = {s["id"] for s in get_all_stores()}
    invalid_ids = set(store_ids) - valid_ids
    if invalid_ids:
        return jsonify({"error": f"Неизвестные точки: {list(invalid_ids)}"}), 400

    if row["role"] == "florist" and len(store_ids) != 1:
        return jsonify({"error": "Флорист должен быть привязан к ровно одной точке"}), 400

    set_user_stores(username, store_ids)
    log_action(current_user.username, "set_user_stores", f"{username}: {store_ids}")

    return jsonify({"ok": True, "stores": get_user_stores_with_details(username)})


@auth_bp.route("/api/auth/users/<username>/stores/<int:store_id>", methods=["DELETE"])
@role_required("admin")
def delete_user_store_endpoint(username, store_id):
    """Отвязать пользователя от точки продаж."""
    try:
        from cashshifts.storage import delete_user_store
    except ImportError:
        return jsonify({"error": "Модуль кассовых смен недоступен"}), 503

    delete_user_store(username, store_id)
    log_action(current_user.username, "delete_user_store", f"{username}: store {store_id}")
    return jsonify({"ok": True})


# ---------- Одноразовый endpoint для создания первого админа ----------
# Использовать ТОЛЬКО если нет доступа к консоли сервера

@auth_bp.route("/api/auth/setup-first-admin", methods=["POST"])
def setup_first_admin():
    """
    Одноразовый endpoint для создания первого админа.
    Работает только если:
    1. В БД нет пользователей
    2. Предоставлен правильный SETUP_KEY из env

    После создания первого админа использовать нельзя.
    """
    # Проверка секретного ключа для setup
    setup_key = request.headers.get("X-Setup-Key")
    expected_key = os.environ.get("FIRST_ADMIN_SETUP_KEY")

    if not expected_key or setup_key != expected_key:
        logger.warning("Попытка создания админа без правильного ключа")
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    username = data.get("username", "").strip()
    full_name = data.get("full_name", "").strip()
    password = data.get("password", "")

    if not username:
        return jsonify({"error": "Логин обязателен"}), 400
    if not full_name:
        return jsonify({"error": "ФИО обязательно"}), 400
    if len(password) < 8:
        return jsonify({"error": "Пароль минимум 8 символов"}), 400

    conn = get_db()
    try:
        # Проверяем, есть ли уже пользователи
        existing_count = conn.execute("SELECT COUNT(*) as count FROM users").fetchone()["count"]

        if existing_count > 0:
            logger.warning("Попытка создать первого админа, когда пользователи уже есть")
            return jsonify({"error": "Setup уже был выполнен"}), 403

        # Создаём первого админа
        conn.execute(
            "INSERT INTO users (username, full_name, password_hash, role, is_active, created_at) VALUES (?,?,?,?,?,?)",
            (username, full_name, generate_password_hash(password), "admin", 1, datetime.utcnow().isoformat())
        )

        # Даём все permissions админу
        for module in ALL_MODULES:
            conn.execute(
                "INSERT INTO permissions (username, module_name, can_view) VALUES (?, ?, 1)",
                (username, module)
            )

        conn.commit()

        logger.info(f"Создан первый админ: {username} ({full_name})")
        return jsonify({
            "ok": True,
            "message": "Первый администратор создан",
            "username": username,
            "full_name": full_name
        }), 201

    except sqlite3.IntegrityError:
        return jsonify({"error": "Такой логин уже существует"}), 409
    finally:
        conn.close()
