"""
SSO-lite вход из корпоративного портала БАРХАТ Пульс (см. plans/... SSO).

Портал встраивает наш дашборд в <iframe> и передаёт подписанный JWT-пропуск
на GET /sso. Мы проверяем подпись/iss/aud/exp, заводим локальную сессию
(через ту же систему Flask-Login, что и обычный логин по паролю) и
редиректим внутрь SPA.

SSO-пользователи автосоздаются по email из пропуска с ролью sso_viewer —
доступ только к разделу "Качество" (см. ROLE_SECTIONS в auth.py).
Роль, присланная порталом (director/manager/...), на внутренние права
намеренно НЕ мапится: это отдельная сущность доверия, и раздувать ей
привилегии до admin/manager означало бы отдать управление пользователями
и кассами внешнему JWT-заявлению.
"""

import os
import json
import logging
import secrets
from datetime import datetime

import jwt
from flask import Blueprint, request, redirect, abort, session, jsonify
from flask_login import login_user
from werkzeug.security import generate_password_hash

from auth import get_db, User, log_action, role_required

logger = logging.getLogger("barhat.sso")

sso_bp = Blueprint("sso", __name__)

SSO_SECRET = os.environ.get("BARKHAT_SSO_SECRET")
SSO_ISSUER = "barkhat-pulse"
SSO_AUDIENCE = "stas-reports"
PULSE_ORIGIN = os.environ.get(
    "BARKHAT_PULSE_ORIGIN", "https://proekt-barhat-doorhandle2.amvera.io"
)

SSO_ROLE = "sso_viewer"


def _safe_next_path(raw: str) -> str:
    r"""Только локальный путь. Блокирует open-redirect через //evil.com и /\evil.com."""
    if not raw or not raw.startswith("/"):
        return "/"
    if raw.startswith("//") or raw.startswith("/\\"):
        return "/"
    return raw


def _unique_username(conn, base: str) -> str:
    base = base or "sso-user"
    candidate = base
    suffix = 1
    while conn.execute("SELECT 1 FROM users WHERE username = ?", (candidate,)).fetchone():
        suffix += 1
        candidate = f"{base}{suffix}"
    return candidate


def _find_or_create_sso_user(email: str, full_name: str):
    """Найти пользователя по email или создать нового с ролью sso_viewer.

    Повторяет попытку при UNIQUE-гонке между воркерами gunicorn (тот же
    паттерн, что и в остальных модулях: SQLite здесь единственный источник
    правды, конкурентная вставка не редкость).
    """
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if row:
            conn.execute(
                "UPDATE users SET full_name = ?, is_sso = 1 WHERE email = ?",
                (full_name, email),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
            return row

        username = _unique_username(conn, email.split("@")[0])
        # SSO-аккаунт не логинится паролем — хэш от случайного значения,
        # ни один пароль его не подберёт.
        unusable_hash = generate_password_hash(secrets.token_hex(32))

        for attempt in range(3):
            try:
                conn.execute(
                    """INSERT INTO users
                       (username, full_name, password_hash, role, is_active, created_at, email, is_sso)
                       VALUES (?, ?, ?, ?, 1, ?, ?, 1)""",
                    (username, full_name, unusable_hash, SSO_ROLE, datetime.utcnow().isoformat(), email),
                )
                conn.execute(
                    "INSERT INTO permissions (username, module_name, can_view) VALUES (?, 'quality', 1)",
                    (username,),
                )
                conn.commit()
                break
            except Exception:
                conn.rollback()
                # Либо email, либо username уже заняты параллельной вставкой —
                # перечитываем по email на случай, что нас опередили.
                row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
                if row:
                    return row
                username = _unique_username(conn, f"{email.split('@')[0]}-{secrets.token_hex(2)}")
        else:
            raise RuntimeError(f"Не удалось создать SSO-пользователя для {email}")

        return conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    finally:
        conn.close()


def _log_sso_attempt(outcome: str, detail: str = "", unverified: dict | None = None):
    """Пишет диагностику попытки SSO-входа в audit_log (временно, для разбора
    причины 403 — см. debug-эндпоинт /api/sso/debug-log ниже). SQLite —
    единственный способ увидеть это без консоли на тарифе Amvera."""
    unverified = unverified or {}
    payload = json.dumps(
        {
            "outcome": outcome,
            "detail": (detail or "")[:500],
            "iss": unverified.get("iss"),
            "aud": unverified.get("aud"),
            "email": unverified.get("email"),
            "sub": unverified.get("sub"),
            "exp": unverified.get("exp"),
        },
        ensure_ascii=False,
    )
    try:
        conn = get_db()
        conn.execute(
            "INSERT INTO audit_log (username, action, details, ip, created_at) VALUES (?,?,?,?,?)",
            ("sso", "sso_attempt", payload, request.remote_addr, datetime.utcnow().isoformat()),
        )
        conn.commit()
        conn.close()
    except Exception:
        logger.exception("Не удалось записать диагностику sso_attempt в audit_log")


@sso_bp.route("/sso", methods=["GET"])
def sso():
    token = request.args.get("token", "")
    next_path = _safe_next_path(request.args.get("next", "/"))

    logger.info(
        "SSO попытка: token_len=%d secret_len=%d expected_iss=%r expected_aud=%r",
        len(token), len(SSO_SECRET or ""), SSO_ISSUER, SSO_AUDIENCE,
    )

    if not SSO_SECRET:
        logger.error("BARKHAT_SSO_SECRET не задан в env — SSO-вход отключён")
        _log_sso_attempt("NO_SECRET", "BARKHAT_SSO_SECRET пуст/не задан")
        abort(403)

    # Разбор БЕЗ проверки подписи — только чтобы увидеть, что реально
    # прислал Пульс, даже если валидация ниже упадёт.
    unverified = {}
    try:
        unverified = jwt.decode(token, options={"verify_signature": False})
    except Exception as e:
        logger.warning("SSO: не удалось разобрать даже payload без проверки подписи: %r", e)

    logger.info(
        "SSO payload (unverified, подпись НЕ проверена): iss=%r aud=%r sub=%r email=%r exp=%r iat=%r",
        unverified.get("iss"), unverified.get("aud"), unverified.get("sub"),
        unverified.get("email"), unverified.get("exp"), unverified.get("iat"),
    )

    try:
        claims = jwt.decode(
            token,
            SSO_SECRET,
            algorithms=["HS256"],
            audience=SSO_AUDIENCE,
            issuer=SSO_ISSUER,
            options={"require": ["exp", "aud", "iss"]},
        )
    except jwt.ExpiredSignatureError as e:
        logger.warning("SSO отклонён: EXPIRED — истёк срок действия (exp). %r", e)
        _log_sso_attempt("EXPIRED", str(e), unverified)
        abort(403)
    except jwt.InvalidAudienceError as e:
        logger.warning(
            "SSO отклонён: BAD_AUD — ожидали aud=%r, пришло aud=%r",
            SSO_AUDIENCE, unverified.get("aud"),
        )
        _log_sso_attempt("BAD_AUD", str(e), unverified)
        abort(403)
    except jwt.InvalidIssuerError as e:
        logger.warning(
            "SSO отклонён: BAD_ISS — ожидали iss=%r, пришло iss=%r",
            SSO_ISSUER, unverified.get("iss"),
        )
        _log_sso_attempt("BAD_ISS", str(e), unverified)
        abort(403)
    except jwt.InvalidSignatureError as e:
        logger.warning(
            "SSO отклонён: BAD_SIGNATURE — секрет на нашей стороне не совпадает "
            "с тем, которым Пульс подписал токен. %r", e,
        )
        _log_sso_attempt("BAD_SIGNATURE", str(e), unverified)
        abort(403)
    except jwt.InvalidTokenError as e:
        logger.warning("SSO отклонён: %r", e)
        _log_sso_attempt("OTHER_" + type(e).__name__, repr(e), unverified)
        abort(403)

    email = (claims.get("email") or "").strip().lower()
    if not email:
        logger.warning("SSO-пропуск без email, sub=%s", claims.get("sub"))
        _log_sso_attempt("NO_EMAIL", "claims без email", unverified)
        abort(403)

    full_name = claims.get("name") or email

    row = _find_or_create_sso_user(email, full_name)
    if not row or not row["is_active"]:
        _log_sso_attempt("USER_INACTIVE_OR_MISSING", f"email={email}", unverified)
        abort(403)

    user = User(row)
    login_user(user, remember=False)
    # Помечает сессию как SSO-сессию — _PartitionedSsoSessionInterface в
    # server.py по этому флагу выставляет SameSite=None + Partitioned на
    # cookie именно для неё, не трогая SameSite обычных сессий по паролю.
    session["sso"] = True
    log_action(user.username, "sso_login", f"email={email} salon={claims.get('salon')}")
    _log_sso_attempt("OK", f"user={user.username}", unverified)

    return redirect(next_path)


@sso_bp.route("/api/sso/debug-log", methods=["GET"])
@role_required("admin")
def sso_debug_log():
    """Временный эндпоинт для разбора причин 403 на /sso — на тарифе Amvera
    нет консоли/логов из коробки, поэтому пишем диагностику в audit_log и
    читаем её отсюда. Удалить после того, как интеграция с Пульсом заработает."""
    conn = get_db()
    rows = conn.execute(
        """SELECT details, ip, created_at FROM audit_log
           WHERE action = 'sso_attempt' ORDER BY id DESC LIMIT 20"""
    ).fetchall()
    conn.close()
    return jsonify({"attempts": [dict(r) for r in rows]})
