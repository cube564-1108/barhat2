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
import sqlite3
import logging
from functools import wraps
from datetime import datetime

from flask import Blueprint, request, jsonify, session, current_app
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    login_required, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash

logger = logging.getLogger("barhat.auth")

# Путь к той же SQLite базе, где уже лежат данные Pyrus.
# Берём из env, чтобы не хардкодить путь.
DB_PATH = os.environ.get("BARHAT_DB_PATH", "barhat.db")

auth_bp = Blueprint("auth", __name__)
login_manager = LoginManager()

# Роли и какие разделы им доступны.
# Меняйте под свои реальные разделы дашборда.
ROLE_SECTIONS = {
    "admin": {"dashboard", "quality", "calculator", "price_edit", "users_manage", "cash_shifts", "invoices"},
    "manager": {"dashboard", "quality", "calculator", "cash_shifts", "invoices"},
    "florist": {"cash_shifts"},  # Флорист работает только с кассой
    "florist_analyst": {"quality"},
}


def get_db():
    # timeout увеличен против дефолтных 5с — на проде gunicorn поднимает
    # 2 воркера (amvera.yml), пишущих в один файл SQLite; без запаса
    # конкурентная запись (например log_action во время чужой транзакции)
    # падает с "database is locked" вместо того, чтобы дождаться очереди
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    return conn


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
    'users_manage',   # Управление пользователями
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


class User(UserMixin):
    def __init__(self, row):
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
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return User(row) if row else None


def log_action(username, action, details=""):
    conn = get_db()
    conn.execute(
        "INSERT INTO audit_log (username, action, details, ip, created_at) VALUES (?,?,?,?,?)",
        (username, action, details, request.remote_addr, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


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


def section_required(section_name):
    """Декоратор по названию раздела дашборда.
    Проверяет permissions в БД, с фоллбэком на ROLE_SECTIONS для обратной совместимости."""
    def decorator(fn):
        @wraps(fn)
        @login_required
        def wrapper(*args, **kwargs):
            # Сначала проверяем по permissions (новая система)
            if current_user.has_module_access(section_name):
                return fn(*args, **kwargs)

            # Фоллбэк на ROLE_SECTIONS для обратной совместимости
            allowed = ROLE_SECTIONS.get(current_user.role, set())
            if section_name in allowed:
                return fn(*args, **kwargs)

            log_action(current_user.username, "access_denied", section_name)
            return jsonify({"error": "Недостаточно прав"}), 403
        return wrapper
    return decorator


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
        # Создаём пользователя
        conn.execute(
            "INSERT INTO users (username, full_name, password_hash, role, is_active, created_at) VALUES (?,?,?,?,1,?)",
            (username, full_name, generate_password_hash(password), role, datetime.utcnow().isoformat()),
        )

        # Добавляем permissions
        for module in permissions:
            conn.execute(
                "INSERT INTO permissions (username, module_name, can_view) VALUES (?, ?, 1)",
                (username, module)
            )

        conn.commit()
    except sqlite3.IntegrityError as e:
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
        "SELECT id, username, full_name, role, is_active, created_at FROM users ORDER BY created_at DESC"
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
