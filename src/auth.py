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
