"""
Flask API сервер для раздела «Задачи» на дашборде БАРХАТ.

Личный бэклог идей и фич владельца, встроен в существующую страницу /dashboard
(не отдельный пункт меню). Доступ строго admin-only на каждом роуте — строже,
чем доступ к самой странице /dashboard (она открыта и для manager).

План: plans/2026-08-15-dashboard-tasks-tracker.md
"""

import logging
import os
import sys

from flask import Blueprint, jsonify, request
from flask_login import current_user

auth_path = os.path.join(os.path.dirname(__file__), '../')
sys.path.insert(0, auth_path)
from auth import role_required, log_action

from .storage import (
    STATUSES,
    create_task,
    get_task_by_id,
    list_tasks,
    update_task,
    set_task_status,
    delete_task,
)

logger = logging.getLogger(__name__)

tasks_bp = Blueprint("tasks", __name__, url_prefix="/api/tasks")


@tasks_bp.route("", methods=["GET"])
@role_required("admin")
def get_tasks():
    """Список задач. Query params: status (idea|in_progress|done)."""
    status = request.args.get("status")
    if status and status not in STATUSES:
        return jsonify({"error": f"Неизвестный статус. Доступны: {list(STATUSES)}"}), 400

    tasks = list_tasks(status=status)
    return jsonify({"tasks": tasks, "count": len(tasks)})


@tasks_bp.route("", methods=["POST"])
@role_required("admin")
def add_task():
    """Создать задачу. Обязательное поле: title."""
    data = request.get_json(silent=True) or {}

    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "Название задачи обязательно"}), 400

    status = data.get("status", "idea")
    if status not in STATUSES:
        return jsonify({"error": f"Неизвестный статус. Доступны: {list(STATUSES)}"}), 400

    task = create_task(
        title=title,
        created_by=current_user.username,
        description=(data.get("description") or "").strip() or None,
        status=status,
        progress_notes=(data.get("progress_notes") or "").strip() or None,
    )

    log_action(current_user.username, "create_task", title)
    return jsonify({"ok": True, "task": task}), 201


@tasks_bp.route("/<int:task_id>", methods=["PUT"])
@role_required("admin")
def edit_task(task_id):
    """Правка полей задачи. Body: title?, description?, progress_notes?"""
    if not get_task_by_id(task_id):
        return jsonify({"error": "Задача не найдена"}), 404

    data = request.get_json(silent=True) or {}

    title = data.get("title")
    if title is not None:
        title = title.strip()
        if not title:
            return jsonify({"error": "Название задачи не может быть пустым"}), 400

    description = data.get("description")
    if description is not None:
        description = description.strip()

    progress_notes = data.get("progress_notes")
    if progress_notes is not None:
        progress_notes = progress_notes.strip()

    update_task(task_id, title=title, description=description, progress_notes=progress_notes)

    log_action(current_user.username, "update_task", str(task_id))
    return jsonify({"ok": True, "task": get_task_by_id(task_id)})


@tasks_bp.route("/<int:task_id>/status", methods=["POST"])
@role_required("admin")
def change_task_status(task_id):
    """Сменить статус задачи. Body: {"status": "idea"|"in_progress"|"done"}"""
    if not get_task_by_id(task_id):
        return jsonify({"error": "Задача не найдена"}), 404

    data = request.get_json(silent=True) or {}
    status = data.get("status")
    if status not in STATUSES:
        return jsonify({"error": f"Неизвестный статус. Доступны: {list(STATUSES)}"}), 400

    set_task_status(task_id, status)

    log_action(current_user.username, "change_task_status", f"{task_id}: {status}")
    return jsonify({"ok": True, "task": get_task_by_id(task_id)})


@tasks_bp.route("/<int:task_id>", methods=["DELETE"])
@role_required("admin")
def remove_task(task_id):
    if not get_task_by_id(task_id):
        return jsonify({"error": "Задача не найдена"}), 404

    delete_task(task_id)

    log_action(current_user.username, "delete_task", str(task_id))
    return jsonify({"ok": True})
