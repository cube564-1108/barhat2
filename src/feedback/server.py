"""
Flask API обратной связи от сотрудников.

Отправлять может любой авторизованный, читать чужое — только владелец (admin).
Ручки разбора закрыты через role_required("admin"), а не section_required:
отдельного раздела и права у модуля нет, экран владельца живёт на /dashboard
рядом с «Задачами».

План: plans/2026-09-02-обратная-связь-от-пользователей.md
"""

import json
import logging
import os
import sys

from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required

auth_path = os.path.join(os.path.dirname(__file__), '../')
sys.path.insert(0, auth_path)
from auth import role_required, require_ajax_header, log_action

from .storage import (
    RATE_LIMIT_PER_HOUR,
    STATUSES,
    STATUSES_REQUIRING_RESOLUTION,
    TYPES,
    add_supporter,
    count_recent_by_author,
    counters,
    create_item,
    find_similar,
    get_item,
    link_task,
    list_by_author,
    list_for_owner,
    list_merged_children,
    mark_author_seen,
    merge_into,
    set_status,
)

logger = logging.getLogger(__name__)

feedback_bp = Blueprint("feedback", __name__, url_prefix="/api/feedback")

# Владелец — единственная роль, которая видит чужие обращения
OWNER_ROLES = ("admin",)

MIN_TEXT_LENGTH = 10

TYPE_TITLES = {
    "bug": "не работает",
    "inconvenience": "неудобно",
    "wish": "хочу добавить",
}


def _is_owner() -> bool:
    return current_user.role in OWNER_ROLES


def _clean_context(raw) -> dict:
    """Оставить из присланного контекста только известные поля.

    Контекст приходит с фронта, то есть управляется пользователем: складывать
    его в базу как есть нельзя ни по объёму, ни по содержанию. Тела ответов
    сюда не попадают вовсе — в буфер ошибок (ui-dialog.js) кладётся только
    путь, код и короткий текст ошибки, потому что тела наших ручек содержат
    данные клиентов и суммы.
    """
    if not isinstance(raw, dict):
        return {}

    context = {}
    for key in ("screen", "user_agent", "timezone"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            context[key] = value.strip()[:200]

    errors = raw.get("errors")
    if isinstance(errors, list):
        cleaned = []
        for item in errors[:5]:
            if not isinstance(item, dict):
                continue
            cleaned.append({
                "path": str(item.get("path", ""))[:200],
                "status": str(item.get("status", ""))[:10],
                "error": str(item.get("error", ""))[:200],
                "at": str(item.get("at", ""))[:40],
            })
        if cleaned:
            context["errors"] = cleaned

    return context


# ---------------------------------------------------------------------------
# Пользователь
# ---------------------------------------------------------------------------

@feedback_bp.route("", methods=["POST"])
@login_required
@require_ajax_header
def create():
    """Отправить обращение. Обязательное поле одно — text."""
    data = request.get_json(silent=True) or {}

    text = (data.get("text") or "").strip()
    if len(text) < MIN_TEXT_LENGTH:
        return jsonify({"error": "Опишите подробнее, что мешает — хотя бы одним предложением"}), 400

    type_ = data.get("type") or "inconvenience"
    if type_ not in TYPES:
        return jsonify({"error": f"Неизвестный тип. Доступны: {list(TYPES)}"}), 400

    if count_recent_by_author(current_user.username) >= RATE_LIMIT_PER_HOUR:
        return jsonify({
            "error": "Слишком много обращений за час. Продолжите чуть позже — "
                     "всё отправленное уже сохранено"
        }), 429

    item = create_item(
        author_username=current_user.username,
        author_role=current_user.role,
        type_=type_,
        text=text,
        wish=(data.get("wish") or "").strip() or None,
        module=(data.get("module") or "").strip()[:64] or None,
        page_url=(data.get("page_url") or "").strip()[:300] or None,
        client_context=_clean_context(data.get("client_context")),
    )

    log_action(current_user.username, "create_feedback", f"{item['id']}: {type_}")
    return jsonify({"ok": True, "item": item}), 201


@feedback_bp.route("/mine", methods=["GET"])
@login_required
def my_items():
    """Свои обращения со статусами. Заодно гасит точку об обновлениях —
    открытие вкладки и есть «прочитал ответ»."""
    items = list_by_author(current_user.username)
    mark_author_seen(current_user.username)
    return jsonify({"items": items, "count": len(items)})


@feedback_bp.route("/similar", methods=["GET"])
@login_required
def similar():
    """Похожие обращения для подсказки при вводе.

    Отдаёт только разобранные владельцем и собственные — см. комментарий
    в storage.find_similar. Имён авторов в ответе нет.
    """
    text = (request.args.get("q") or "").strip()
    if len(text) < MIN_TEXT_LENGTH:
        return jsonify({"items": []})

    items = find_similar(
        text=text,
        module=(request.args.get("module") or "").strip() or None,
        username=current_user.username,
    )
    return jsonify({"items": items})


@feedback_bp.route("/<int:item_id>/support", methods=["POST"])
@login_required
@require_ajax_header
def support(item_id):
    """«У меня тоже» — вместо создания дубля."""
    item = get_item(item_id)
    if not item:
        return jsonify({"error": "Обращение не найдено"}), 404

    # Отдавать чужое обращение целиком тому, кто нажал кнопку, не нужно:
    # он его уже видел в подсказке, а лишние поля (автор, техконтекст) —
    # это ровно то, что решено не показывать.
    if item["author_username"] == current_user.username:
        return jsonify({"ok": True, "added": False, "people_count": item["people_count"]})

    added = add_supporter(item_id, current_user.username)
    if added:
        log_action(current_user.username, "support_feedback", str(item_id))

    updated = get_item(item_id)
    return jsonify({
        "ok": True,
        "added": added,
        "people_count": updated["people_count"] if updated else item["people_count"],
    })


@feedback_bp.route("/counters", methods=["GET"])
@login_required
def get_counters():
    """Числа для бейджей. Ручка лёгкая, но фронт зовёт её отложенно —
    в критическом пути загрузки страницы запросов к базе быть не должно."""
    return jsonify(counters(current_user.username, _is_owner()))


# ---------------------------------------------------------------------------
# Владелец
# ---------------------------------------------------------------------------

@feedback_bp.route("", methods=["GET"])
@role_required("admin")
def owner_list():
    """Очередь разбора. Query: status, module."""
    status = request.args.get("status")
    if status and status not in STATUSES:
        return jsonify({"error": f"Неизвестный статус. Доступны: {list(STATUSES)}"}), 400

    items = list_for_owner(status=status, module=request.args.get("module") or None)
    return jsonify({"items": items, "count": len(items)})


@feedback_bp.route("/<int:item_id>/status", methods=["POST"])
@role_required("admin")
@require_ajax_header
def change_status(item_id):
    """Сменить статус. Для отклонения и вопроса ответ автору обязателен."""
    if not get_item(item_id):
        return jsonify({"error": "Обращение не найдено"}), 404

    data = request.get_json(silent=True) or {}
    status = data.get("status")
    if status not in STATUSES:
        return jsonify({"error": f"Неизвестный статус. Доступны: {list(STATUSES)}"}), 400

    resolution = (data.get("resolution") or "").strip() or None
    if status in STATUSES_REQUIRING_RESOLUTION and not resolution:
        return jsonify({
            "error": "Нужен текст для автора: без ответа человек считает, что его не услышали"
        }), 400

    set_status(item_id, status, resolution)
    log_action(current_user.username, "feedback_status", f"{item_id}: {status}")
    return jsonify({"ok": True, "item": get_item(item_id)})


@feedback_bp.route("/<int:item_id>/merge", methods=["POST"])
@role_required("admin")
@require_ajax_header
def merge(item_id):
    """Подшить обращение к другому как дубль. Body: {"target_id": N}"""
    data = request.get_json(silent=True) or {}
    target_id = data.get("target_id")
    if not isinstance(target_id, int):
        return jsonify({"error": "Не указано, к какому обращению подшивать"}), 400

    if not merge_into(item_id, target_id):
        return jsonify({"error": "Обращение или цель не найдены"}), 404

    log_action(current_user.username, "feedback_merge", f"{item_id} -> {target_id}")
    return jsonify({"ok": True, "item": get_item(item_id)})


@feedback_bp.route("/<int:item_id>/to-task", methods=["POST"])
@role_required("admin")
@require_ajax_header
def to_task(item_id):
    """Принять обращение в бэклог: создаёт задачу в разделе «Задачи».

    Порядок важен: задача создаётся и коммитится своим соединением
    (tasks.storage открывает собственное), и только потом отдельным
    соединением проставляется task_id. Вложенных соединений здесь быть не
    должно — они уже дважды вешали запись в общую базу.
    """
    item = get_item(item_id)
    if not item:
        return jsonify({"error": "Обращение не найдено"}), 404
    if item["task_id"]:
        return jsonify({"error": "Обращение уже в бэклоге"}), 409

    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip() or item["text"][:80]

    from tasks.storage import create_task

    task = create_task(
        title=title,
        created_by=current_user.username,
        description=_task_description(item),
        status="idea",
        progress_notes=None,
    )

    link_task(item_id, task["id"])
    log_action(current_user.username, "feedback_to_task", f"{item_id} -> task {task['id']}")
    return jsonify({"ok": True, "item": get_item(item_id), "task": task})


def _task_description(item: dict) -> str:
    parts = [item["text"]]
    if item.get("wish"):
        parts.append(f"Как хотелось бы: {item['wish']}")
    who = f"Просил(и): {item['people_count']} чел., роль автора — {item.get('author_role') or '—'}"
    parts.append(who)
    if item.get("module"):
        parts.append(f"Модуль: {item['module']}")
    return "\n\n".join(parts)


@feedback_bp.route("/<int:item_id>/export", methods=["GET"])
@role_required("admin")
def export_prompt(item_id):
    """Собрать обращение в markdown — готовый вход для Claude Code.

    Отдаём текстом, копирование делает фронт: navigator.clipboard в
    кросс-доменном iframe блокируется, и у кнопки обязан быть запасной путь.
    """
    item = get_item(item_id)
    if not item:
        return jsonify({"error": "Обращение не найдено"}), 404

    lines = [
        f"# Доработка по обращению #{item['id']}",
        "",
        f"- **Модуль:** {item.get('module') or 'не указан'}",
        f"- **Тип:** {TYPE_TITLES.get(item['type'], item['type'])}",
        f"- **Просили:** {item['people_count']} чел. (роль автора — {item.get('author_role') or '—'})",
        f"- **Дата:** {item['created_at']}",
    ]
    if item.get("page_url"):
        lines.append(f"- **Экран:** {item['page_url']}")

    lines += ["", "## Что мешает", "", item["text"]]

    if item.get("wish"):
        lines += ["", "## Как хотелось бы (со слов автора)", "", item["wish"]]

    context = {}
    if item.get("client_context"):
        try:
            context = json.loads(item["client_context"])
        except (ValueError, TypeError):
            context = {}

    errors = context.get("errors") or []
    if errors:
        lines += ["", "## Ошибки на экране в момент обращения", ""]
        for err in errors:
            lines.append(
                f"- `{err.get('status', '')}` {err.get('path', '')} — "
                f"{err.get('error', '')} ({err.get('at', '')})"
            )

    tech = [f"{key}: {value}" for key, value in context.items() if key != "errors"]
    if tech:
        lines += ["", "## Окружение", ""] + [f"- {line}" for line in tech]

    children = list_merged_children(item_id)
    if children:
        lines += ["", "## Та же боль другими словами", ""]
        for child in children:
            lines.append(f"- ({child.get('author_role') or '—'}) {child['text']}")

    lines += [
        "",
        "## Задача",
        "",
        "Разобраться в причине и предложить решение. Перед правками — прочитать "
        "CLAUDE.md и DESIGN-SPEC.md.",
    ]

    return jsonify({"markdown": "\n".join(lines)})
