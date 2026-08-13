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
    "admin": {"dashboard", "quality", "calculator", "price_edit", "users_manage"},
    "manager": {"dashboard", "quality", "calculator"},
    "florist_analyst": {"quality"},
}


def get_db():
    conn = sqlite3.connect(DB_PATH)
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

            # Получаем всех пользователей
            users = conn_old.execute(
                "SELECT username, password_hash, role, is_active, created_at FROM users"
            ).fetchall()

            # Вставляем в новую базу
            conn_new = sqlite3.connect(new_db)
            conn_new.row_factory = sqlite3.Row
            for user in users:
                try:
                    conn_new.execute(
                        "INSERT INTO users (username, password_hash, role, is_active, created_at) VALUES (?,?,?,?,?)",
                        (user["username"], user["password_hash"], user["role"], user["is_active"], user["created_at"])
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


def init_auth_tables():
    """Вызвать один раз при старте приложения (создаёт таблицы, если их нет)."""
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )
    """)
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
    conn.commit()
    conn.close()


class User(UserMixin):
    def __init__(self, row):
        self.id = row["id"]
        self.username = row["username"]
        self.role = row["role"]
        self.is_active_flag = row["is_active"]

    @property
    def is_active(self):
        return bool(self.is_active_flag)


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
    """Декоратор по названию раздела дашборда, а не по конкретной роли —
    удобно, если ролей станет больше."""
    def decorator(fn):
        @wraps(fn)
        @login_required
        def wrapper(*args, **kwargs):
            allowed = ROLE_SECTIONS.get(current_user.role, set())
            if section_name not in allowed:
                log_action(current_user.username, "access_denied", section_name)
                return jsonify({"error": "Недостаточно прав"}), 403
            return fn(*args, **kwargs)
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
    return jsonify({
        "username": current_user.username,
        "role": current_user.role,
        "sections": sorted(ROLE_SECTIONS.get(current_user.role, [])),
    })


# ---------- Управление пользователями (только admin) ----------

@auth_bp.route("/api/auth/users", methods=["POST"])
@role_required("admin")
def create_user():
    """Создать сотрудника. Пароль генерируется/задаётся один раз,
    сотрудник может сменить его после первого входа (реализуйте отдельно при желании)."""
    data = request.get_json(silent=True) or {}
    username = data.get("username", "").strip()
    password = data.get("password", "")
    role = data.get("role", "")

    if role not in ROLE_SECTIONS:
        return jsonify({"error": f"Неизвестная роль. Доступны: {list(ROLE_SECTIONS)}"}), 400
    if not username or len(password) < 8:
        return jsonify({"error": "Логин обязателен, пароль минимум 8 символов"}), 400

    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO users (username, password_hash, role, is_active, created_at) VALUES (?,?,?,1,?)",
            (username, generate_password_hash(password), role, datetime.utcnow().isoformat()),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        return jsonify({"error": "Такой логин уже существует"}), 409
    finally:
        conn.close()

    log_action(current_user.username, "create_user", username)
    return jsonify({"ok": True, "username": username, "role": role}), 201


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
        "SELECT id, username, role, is_active, created_at FROM users ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    return jsonify({"users": [dict(row) for row in rows]})


@auth_bp.route("/api/auth/users/<username>", methods=["PUT", "PATCH"])
@role_required("admin")
def update_user(username):
    """Обновить пользователя (пароль или роль)"""
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

    if not updates:
        conn.close()
        return jsonify({"error": "Нечего обновлять"}), 400

    # Добавляем username в конец params для WHERE
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
    password = data.get("password", "")

    if not username or len(password) < 8:
        return jsonify({"error": "Логин обязателен, пароль минимум 8 символов"}), 400

    conn = get_db()
    try:
        # Проверяем, есть ли уже пользователи
        existing_count = conn.execute("SELECT COUNT(*) as count FROM users").fetchone()["count"]

        if existing_count > 0:
            logger.warning("Попытка создать первого админа, когда пользователи уже есть")
            return jsonify({"error": "Setup уже был выполнен"}), 403

        # Создаём первого админа
        conn.execute(
            "INSERT INTO users (username, password_hash, role, is_active, created_at) VALUES (?,?,?,?,?)",
            (username, generate_password_hash(password), "admin", 1, datetime.utcnow().isoformat())
        )
        conn.commit()

        logger.info(f"Создан первый админ: {username}")
        return jsonify({
            "ok": True,
            "message": "Первый администратор создан",
            "username": username
        }), 201

    except sqlite3.IntegrityError:
        return jsonify({"error": "Такой логин уже существует"}), 409
    finally:
        conn.close()
