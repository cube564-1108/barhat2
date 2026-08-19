"""
Flask API сервер для модуля кассовых смен БАРХАТ.

Предоставляет эндпоинты для:
- Открытия/закрытия смен
- Добавления инкассаций
- Получения деталей смены
- Списка смен с фильтрами
"""

import os
import sys
import time
import logging
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user

# Импортируем модуль авторизации
auth_path = os.path.join(os.path.dirname(__file__), '../')
sys.path.insert(0, auth_path)
from auth import role_required, log_action

from .storage import (
    get_all_stores,
    get_all_categories,
    get_store_by_id,
    get_category_by_id,
    create_store,
    update_store,
    delete_store,
    get_user_stores,
    check_store_access,
    get_users_full_names,
    # Смены
    create_cash_shift,
    get_open_shift,
    get_open_shifts,
    get_last_closed_shift,
    get_cash_shift_by_id,
    list_cash_shifts,
    update_cash_shift,
    # Инкассации
    create_collection,
    get_shift_collections,
    get_collections_total,
    update_collection,
    # Кэш заказов
    cache_cash_orders,
    get_shift_cash_orders,
    clear_shift_cache,
    # Категории расходов
    create_category,
    update_category,
    delete_category,
)

logger = logging.getLogger(__name__)

# Создаём Blueprint
cashshifts_bp = Blueprint("cashshifts", __name__, url_prefix="/api/cash-shifts")


# =============================================================================
# ОШИБКИ
# =============================================================================

class CashShiftError(Exception):
    """Базовая ошибка кассовых смен."""
    pass


class StoreAccessError(CashShiftError):
    """Нет доступа к точке."""
    pass


class ShiftAlreadyOpenError(CashShiftError):
    """Смена уже открыта."""
    pass


class ShiftNotFoundError(CashShiftError):
    """Смена не найдена."""
    pass


class ShiftClosedError(CashShiftError):
    """Смена уже закрыта."""
    pass


class ShiftEditForbiddenError(CashShiftError):
    """Нет прав на редактирование этой смены."""
    pass


# =============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =============================================================================

def get_current_user_role() -> str:
    """Получить роль текущего пользователя."""
    return getattr(current_user, "role", "guest")


def get_current_username() -> str:
    """Получить имя текущего пользователя."""
    return getattr(current_user, "username", "unknown")


def require_store_access(store_id: int):
    """
    Проверить доступ к точке. Если нет — выбросить исключение.

    Raises:
        StoreAccessError: Если нет доступа к точке
    """
    username = get_current_username()
    role = get_current_user_role()

    if not check_store_access(username, store_id, role):
        raise StoreAccessError(f"Нет доступа к точке {store_id}")


def require_shift_edit_access(shift: dict):
    """
    Проверить право на редактирование/пересчёт смены (PUT, /reclose).

    Админ — любая закрытая смена. Флорист — только последняя закрытая смена
    своей точки (не может трогать более старую историю). Остальные роли — запрет.

    Raises:
        ShiftEditForbiddenError: Если нет прав
    """
    role = get_current_user_role()
    username = get_current_username()

    if role == "admin":
        return

    if role == "florist":
        if not check_store_access(username, shift["store_id"], role):
            raise ShiftEditForbiddenError("Нет доступа к точке этой смены")
        last_closed = get_last_closed_shift(shift["store_id"])
        if not last_closed or last_closed["id"] != shift["id"]:
            raise ShiftEditForbiddenError("Можно редактировать только последнюю закрытую смену своей точки")
        return

    raise ShiftEditForbiddenError("Роль не имеет прав на редактирование смен")


def require_open_shift_edit_access(shift: dict):
    """
    Проверить право на правку ОТКРЫТОЙ смены (начальный остаток, суммы инкассаций).

    Право есть у тех, кто видит таблицу «Открытые смены»: админ — по всем точкам,
    менеджер — только по своим (user_stores). Флорист сюда не попадает намеренно:
    начальный остаток — это цифра, от которой считается недостача по его же
    смене, менять её себе он не должен.

    Raises:
        ShiftEditForbiddenError: Если нет прав
    """
    role = get_current_user_role()
    username = get_current_username()

    if role == "admin":
        return

    if role == "manager":
        if not check_store_access(username, shift["store_id"], role):
            raise ShiftEditForbiddenError("Нет доступа к точке этой смены")
        return

    raise ShiftEditForbiddenError("Роль не имеет прав на редактирование открытых смен")


def error_response(message: str, status: int = 400) -> tuple:
    """Создать ответ с ошибкой."""
    return jsonify({"success": False, "error": message}), status


def success_response(data: dict = None) -> dict:
    """Создать успешный ответ."""
    response = {"success": True}
    if data:
        response.update(data)
    return response


# =============================================================================
# ЭНДПОИНТЫ: СПРАВОЧНИКИ
# =============================================================================

@cashshifts_bp.route("/stores", methods=["GET"])
@login_required
def get_stores():
    """Получить список всех активных точек продаж."""
    try:
        username = get_current_username()
        role = get_current_user_role()

        all_stores = get_all_stores()

        # Фильтруем по доступам для не-админов
        if role != "admin":
            user_store_ids = get_user_stores(username)
            all_stores = [s for s in all_stores if s["id"] in user_store_ids]

        return jsonify(success_response({
            "count": len(all_stores),
            "stores": all_stores
        }))
    except Exception as e:
        logger.error(f"Ошибка /api/cash-shifts/stores: {e}")
        return error_response(str(e), 500)


@cashshifts_bp.route("/stores", methods=["POST"])
@role_required("admin")
def add_store():
    """Создать точку продаж (салон/офис). Body: {"name": str}."""
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return error_response("Название обязательно", 400)
    try:
        store_id = create_store(name)
    except Exception as e:
        logger.error(f"Ошибка создания точки продаж «{name}»: {e}")
        return error_response(f"«{name}» уже есть в списке или произошла ошибка", 400)
    log_action(current_user.username, "create_store", name)
    return jsonify(success_response({"id": store_id, "name": name})), 201


@cashshifts_bp.route("/stores/<int:store_id>", methods=["PUT"])
@role_required("admin")
def edit_store(store_id):
    """Переименовать точку продаж. Body: {"name": str}."""
    if not get_store_by_id(store_id):
        return error_response("Точка не найдена", 404)
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return error_response("Название обязательно", 400)
    try:
        update_store(store_id, name)
    except Exception as e:
        logger.error(f"Ошибка переименования точки продаж {store_id} в «{name}»: {e}")
        return error_response(f"«{name}» уже есть в списке или произошла ошибка", 400)
    log_action(current_user.username, "update_store", f"{store_id}: {name}")
    return jsonify(success_response())


@cashshifts_bp.route("/stores/<int:store_id>", methods=["DELETE"])
@role_required("admin")
def remove_store(store_id):
    """Деактивировать точку продаж (мягкое удаление)."""
    if not get_store_by_id(store_id):
        return error_response("Точка не найдена", 404)
    delete_store(store_id)
    log_action(current_user.username, "delete_store", str(store_id))
    return jsonify(success_response())


@cashshifts_bp.route("/categories", methods=["GET"])
@login_required
def get_categories():
    """Получить список всех активных категорий расходов."""
    try:
        categories = get_all_categories()

        return jsonify(success_response({
            "count": len(categories),
            "categories": categories
        }))
    except Exception as e:
        logger.error(f"Ошибка /api/cash-shifts/categories: {e}")
        return error_response(str(e), 500)


@cashshifts_bp.route("/categories", methods=["POST"])
@role_required("admin")
def add_category():
    """Создать категорию расхода (только админ)."""
    try:
        data = request.get_json()
        if not data or not data.get("name"):
            return error_response("Не указано название категории")

        category_id = create_category(data["name"])
        category = get_category_by_id(category_id)

        logger.info(f"Категория расхода создана: ID={category_id}, name={data['name']}")

        return jsonify(success_response({"id": category_id, "category": category})), 201
    except Exception as e:
        logger.error(f"Ошибка POST /api/cash-shifts/categories: {e}")
        return error_response(str(e), 500)


@cashshifts_bp.route("/categories/<int:category_id>", methods=["PUT"])
@role_required("admin")
def edit_category(category_id: int):
    """Переименовать категорию расхода (только админ)."""
    try:
        category = get_category_by_id(category_id)
        if not category:
            return error_response(f"Категория {category_id} не найдена", 404)

        data = request.get_json()
        if not data or not data.get("name"):
            return error_response("Не указано название категории")

        update_category(category_id, data["name"])

        logger.info(f"Категория расхода обновлена: ID={category_id}, name={data['name']}")

        return jsonify(success_response({"category": get_category_by_id(category_id)}))
    except Exception as e:
        logger.error(f"Ошибка PUT /api/cash-shifts/categories/{category_id}: {e}")
        return error_response(str(e), 500)


@cashshifts_bp.route("/categories/<int:category_id>", methods=["DELETE"])
@role_required("admin")
def remove_category(category_id: int):
    """Деактивировать категорию расхода (не удаляет запись, только скрывает; только админ)."""
    try:
        category = get_category_by_id(category_id)
        if not category:
            return error_response(f"Категория {category_id} не найдена", 404)

        delete_category(category_id)

        logger.info(f"Категория расхода деактивирована: ID={category_id}")

        return jsonify(success_response({"id": category_id}))
    except Exception as e:
        logger.error(f"Ошибка DELETE /api/cash-shifts/categories/{category_id}: {e}")
        return error_response(str(e), 500)


# =============================================================================
# ЭНДПОИНТЫ: ОТКРЫТИЕ СМЕНЫ
# =============================================================================

@cashshifts_bp.route("/open", methods=["POST"])
@login_required
def open_shift():
    """
    Открыть новую кассовую смену.

    Body:
        - store_id (int): ID точки продажи
        - shift_type (str): 'day' или 'night'

    Returns:
        - id: ID созданной смены
        - shift: Данные смены
    """
    try:
        data = request.get_json()
        if not data:
            return error_response("Тело запроса пустое")

        store_id = data.get("store_id")
        shift_type = data.get("shift_type")

        # Валидация
        if not store_id:
            return error_response("Не указан store_id")

        if shift_type not in ("day", "night"):
            return error_response("shift_type должен быть 'day' или 'night'")

        # Проверка доступа к точке
        require_store_access(store_id)

        # Проверка: нет ли открытой смены
        existing = get_open_shift(store_id)
        if existing:
            return error_response(f"На этой точке уже есть открытая смена (ID={existing['id']})", 409)

        # Вычисляем opening_balance
        last_closed = get_last_closed_shift(store_id)
        if last_closed:
            # Берём actual_balance последней закрытой смены
            opening_balance = last_closed.get("actual_balance", 0.0)
        else:
            # Первая смена на точке
            opening_balance = 0.0

        # Создаём смену
        datetime_start = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        florist_username = get_current_username()

        shift_id = create_cash_shift(
            store_id=store_id,
            shift_type=shift_type,
            datetime_start=datetime_start,
            opening_balance=opening_balance,
            florist_username=florist_username
        )

        # Получаем созданную смену
        shift = get_cash_shift_by_id(shift_id)
        store = get_store_by_id(store_id)

        logger.info(
            f"Смена открыта: ID={shift_id}, точка={store['name'] if store else store_id}, "
            f"тип={shift_type}, баланс={opening_balance}"
        )

        return jsonify(success_response({
            "id": shift_id,
            "shift": shift,
            "store": store
        })), 201

    except StoreAccessError:
        return error_response("Нет доступа к указанной точке", 403)
    except Exception as e:
        logger.error(f"Ошибка /api/cash-shifts/open: {e}")
        import traceback
        traceback.print_exc()
        return error_response(str(e), 500)


# =============================================================================
# ЭНДПОИНТЫ: ДОБАВЛЕНИЕ ИНКАССАЦИИ
# =============================================================================

@cashshifts_bp.route("/<int:shift_id>/collections", methods=["POST"])
@login_required
def add_collection(shift_id: int):
    """
    Добавить инкассацию к смене.

    Body:
        - amount (float): Сумма инкассации
        - expense_category_id (int): ID категории расхода
        - custom_comment (str, опционально): Комментарий

    Returns:
        - id: ID созданной инкассации
        - collection: Данные инкассации
    """
    try:
        data = request.get_json()
        if not data:
            return error_response("Тело запроса пустое")

        amount = data.get("amount")
        expense_category_id = data.get("expense_category_id")
        custom_comment = data.get("custom_comment")

        # Валидация
        if amount is None:
            return error_response("Не указана сумма инкассации")

        if not expense_category_id:
            return error_response("Не указана категория расхода")

        # Проверка существования смены
        shift = get_cash_shift_by_id(shift_id)
        if not shift:
            return error_response(f"Смена {shift_id} не найдена", 404)

        # Проверка доступа к точке
        require_store_access(shift["store_id"])

        # Проверка: смена должна быть открыта, либо у пользователя есть право
        # корректировать эту закрытую смену (админ / флорист на последней своей смене)
        if shift["status"] != "open":
            try:
                require_shift_edit_access(shift)
            except ShiftEditForbiddenError:
                return error_response(f"Смена {shift_id} уже закрыта", 409)

        # Проверка существования категории
        category = get_category_by_id(expense_category_id)
        if not category:
            return error_response(f"Категория {expense_category_id} не найдена", 404)

        # Создаём инкассацию
        date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        created_by = get_current_username()

        collection_id = create_collection(
            shift_id=shift_id,
            amount=amount,
            expense_category_id=expense_category_id,
            date=date,
            custom_comment=custom_comment,
            created_by=created_by
        )

        logger.info(
            f"Инкассация добавена: ID={collection_id}, смена={shift_id}, "
            f"сумма={amount}, категория={category['name']}"
        )

        return jsonify(success_response({
            "id": collection_id,
            "amount": amount,
            "category": category["name"]
        })), 201

    except StoreAccessError:
        return error_response("Нет доступа к точке этой смены", 403)
    except Exception as e:
        logger.error(f"Ошибка /api/cash-shifts/{shift_id}/collections: {e}")
        import traceback
        traceback.print_exc()
        return error_response(str(e), 500)


# =============================================================================
# ЭНДПОИНТЫ: ЗАКРЫТИЕ СМЕНЫ
# =============================================================================

@cashshifts_bp.route("/<int:shift_id>/close", methods=["POST"])
@login_required
def close_shift(shift_id: int):
    """
    Закрыть кассовую смену.

    Body:
        - actual_balance (float): Фактический остаток в кассе

    Действия:
        1. Проверяет, что смена открыта
        2. Запрашивает наличные заказы из CRM
        3. Кэширует их в БД
        4. Рассчитывает expected_balance
        5. Сохраняет actual_balance и discrepancy

    Returns:
        - shift: Обновлённые данные смены
    """
    try:
        data = request.get_json()
        if not data:
            return error_response("Тело запроса пустое")

        actual_balance = data.get("actual_balance")

        # Валидация
        if actual_balance is None:
            return error_response("Не указан actual_balance")

        # Проверка существования смены
        shift = get_cash_shift_by_id(shift_id)
        if not shift:
            return error_response(f"Смена {shift_id} не найдена", 404)

        # Проверка доступа к точке
        require_store_access(shift["store_id"])

        # Проверка: смена должна быть открыта
        if shift["status"] != "open":
            return error_response(f"Смена {shift_id} уже закрыта", 409)

        # Импортируем клиент RetailCRM
        try:
            from .retailcrm_client import get_client, get_store_code_from_name
            client = get_client()
        except Exception as e:
            logger.error(f"Не удалось инициализировать RetailCRM клиент: {e}")
            return error_response("Ошибка интеграции с CRM", 503)

        # Получаем точку для кода магазина
        store = get_store_by_id(shift["store_id"])
        if not store:
            return error_response(f"Точка {shift['store_id']} не найдена", 404)

        store_code = get_store_code_from_name(store["name"])

        # Период для запроса: datetime_start смены → сейчас
        datetime_start = datetime.strptime(shift["datetime_start"], "%Y-%m-%d %H:%M:%S")
        datetime_end = datetime.now()

        # Запрашиваем наличные заказы из CRM
        try:
            cash_orders = client.get_cash_orders(
                store_code=store_code,
                datetime_start=datetime_start,
                datetime_end=datetime_end
            )
        except Exception as e:
            logger.error(f"Ошибка запроса к RetailCRM: {e}")
            return error_response(f"Ошибка получения заказов из CRM: {e}", 503)

        # Кэшируем заказы (перезаписываем старые)
        clear_shift_cache(shift_id)
        cache_cash_orders(shift_id, cash_orders)

        # Рассчитываем суммы
        cash_orders_total = sum(o["amount"] for o in cash_orders)
        collections_total = get_collections_total(shift_id)
        expected_balance = shift["opening_balance"] + cash_orders_total - collections_total
        discrepancy = actual_balance - expected_balance

        # Обновляем смену
        datetime_end_str = datetime_end.strftime("%Y-%m-%d %H:%M:%S")
        closed_at = datetime_end_str

        update_cash_shift(
            shift_id=shift_id,
            cash_orders_total=cash_orders_total,
            collections_total=collections_total,
            expected_balance=expected_balance,
            actual_balance=actual_balance,
            discrepancy=discrepancy,
            status="closed",
            datetime_end=datetime_end_str,
            closed_at=closed_at,
            closed_by_username=get_current_username()
        )

        # Получаем обновлённую смену
        updated_shift = get_cash_shift_by_id(shift_id)

        logger.info(
            f"Смена закрыта: ID={shift_id}, "
            f"заказы={cash_orders_total:.2f}, инкассации={collections_total:.2f}, "
            f"ожидаемый={expected_balance:.2f}, фактический={actual_balance:.2f}, "
            f"расхождение={discrepancy:.2f}"
        )

        return jsonify(success_response({
            "shift": updated_shift,
            "cash_orders_count": len(cash_orders),
            "cash_orders_total": cash_orders_total,
            "collections_total": collections_total,
            "expected_balance": expected_balance,
            "discrepancy": discrepancy
        }))

    except StoreAccessError:
        return error_response("Нет доступа к точке этой смены", 403)
    except Exception as e:
        logger.error(f"Ошибка /api/cash-shifts/{shift_id}/close: {e}")
        import traceback
        traceback.print_exc()
        return error_response(str(e), 500)


# =============================================================================
# ЭНДПОИНТЫ: РЕДАКТИРОВАНИЕ И ПОВТОРНОЕ ЗАКРЫТИЕ (ТОЛЬКО АДМИН)
# =============================================================================

def _edit_open_shift(shift: dict):
    """
    Поправить открытую смену: начальный остаток и суммы уже внесённых инкассаций.

    Начальный остаток проставляется автоматически из остатка предыдущей закрытой
    смены точки, и если та закрылась с ошибкой — вся текущая смена считается от
    неверной цифры. Правка здесь чинит это, не дожидаясь закрытия.

    actual_balance для открытой смены не существует (кассу ещё не пересчитывали),
    поэтому discrepancy тут не трогаем — она появится при закрытии.

    Body:
        - opening_balance (float, опционально): новый начальный остаток
        - collections (list, опционально): [{id, amount}, ...] — правки сумм инкассаций
    """
    shift_id = shift["id"]
    require_open_shift_edit_access(shift)

    data = request.get_json() or {}
    opening_balance = data.get("opening_balance")
    collections = data.get("collections")

    if data.get("actual_balance") is not None:
        return error_response(
            "Фактический остаток задаётся только при закрытии смены", 400
        )

    if opening_balance is None and not collections:
        return error_response("Нечего обновлять: укажите opening_balance и/или collections")

    # Приводим суммы к числу до записи: SQLite не приводит типы насильно, и
    # строка из JSON легла бы в денежную колонку как текст, ломая все
    # последующие расчёты остатка
    if opening_balance is not None:
        try:
            opening_balance = float(opening_balance)
        except (TypeError, ValueError):
            return error_response("Начальный остаток должен быть числом")

    if collections:
        existing_ids = {c["id"] for c in get_shift_collections(shift_id)}
        for item in collections:
            collection_id = item.get("id")
            amount = item.get("amount")
            if collection_id not in existing_ids:
                return error_response(f"Инкассация {collection_id} не принадлежит смене {shift_id}", 404)
            if amount is None:
                return error_response(f"Не указана сумма для инкассации {collection_id}")
            try:
                amount = float(amount)
            except (TypeError, ValueError):
                return error_response(f"Сумма инкассации {collection_id} должна быть числом")
            update_collection(collection_id, amount)

    collections_total = get_collections_total(shift_id)
    final_opening_balance = (
        opening_balance if opening_balance is not None else shift["opening_balance"]
    )

    # cash_orders_total у открытой смены известен только если её уже
    # синхронизировали с CRM (таблица «Открытые смены»); иначе плановый
    # остаток посчитать не из чего — оставляем пустым до закрытия
    cash_orders_total = shift.get("cash_orders_total")
    expected_balance = (
        final_opening_balance + cash_orders_total - collections_total
        if cash_orders_total is not None else None
    )

    update_cash_shift(
        shift_id=shift_id,
        opening_balance=final_opening_balance,
        collections_total=collections_total,
        expected_balance=expected_balance
    )

    updated_shift = get_cash_shift_by_id(shift_id)

    logger.info(
        f"Открытая смена отредактирована: ID={shift_id}, "
        f"начальный остаток={final_opening_balance:.2f}, "
        f"инкассации={collections_total:.2f}"
    )

    return jsonify(success_response({"shift": updated_shift}))


@cashshifts_bp.route("/<int:shift_id>", methods=["PUT"])
@login_required
def edit_shift(shift_id: int):
    """
    Отредактировать смену.

    Закрытая смена (админ — любую; флорист — только последнюю закрытую смену
    своей точки):
        - actual_balance (float, опционально): новый фактический остаток
        - collections (list, опционально): [{id, amount}, ...] — правки сумм инкассаций

    Discrepancy пересчитывается от уже сохранённого cash_orders_total. Если нужно
    учесть новые данные из CRM (например, заказ добавили в CRM задним числом) —
    после этого вызвать POST /<id>/reclose.

    Открытая смена (админ; менеджер — по своим точкам) — см. _edit_open_shift.

    Returns:
        - shift: Обновлённые данные смены
    """
    try:
        shift = get_cash_shift_by_id(shift_id)
        if not shift:
            return error_response(f"Смена {shift_id} не найдена", 404)

        if shift["status"] == "open":
            return _edit_open_shift(shift)

        require_shift_edit_access(shift)

        data = request.get_json() or {}
        actual_balance = data.get("actual_balance")
        collections = data.get("collections")

        if actual_balance is None and not collections:
            return error_response("Нечего обновлять: укажите actual_balance и/или collections")

        # Правим суммы инкассаций
        if collections:
            existing_ids = {c["id"] for c in get_shift_collections(shift_id)}
            for item in collections:
                collection_id = item.get("id")
                amount = item.get("amount")
                if collection_id not in existing_ids:
                    return error_response(f"Инкассация {collection_id} не принадлежит смене {shift_id}", 404)
                if amount is None:
                    return error_response(f"Не указана сумма для инкассации {collection_id}")
                update_collection(collection_id, amount)

        # Пересчитываем итоги
        collections_total = get_collections_total(shift_id)
        cash_orders_total = shift["cash_orders_total"] or 0.0
        expected_balance = shift["opening_balance"] + cash_orders_total - collections_total
        final_actual_balance = actual_balance if actual_balance is not None else shift["actual_balance"]
        discrepancy = final_actual_balance - expected_balance

        update_cash_shift(
            shift_id=shift_id,
            actual_balance=final_actual_balance,
            collections_total=collections_total,
            expected_balance=expected_balance,
            discrepancy=discrepancy
        )

        updated_shift = get_cash_shift_by_id(shift_id)

        logger.info(
            f"Смена отредактирована админом: ID={shift_id}, "
            f"фактический={final_actual_balance:.2f}, расхождение={discrepancy:.2f}"
        )

        return jsonify(success_response({"shift": updated_shift}))

    except ShiftEditForbiddenError as e:
        return error_response(str(e), 403)
    except Exception as e:
        logger.error(f"Ошибка PUT /api/cash-shifts/{shift_id}: {e}")
        import traceback
        traceback.print_exc()
        return error_response(str(e), 500)


@cashshifts_bp.route("/<int:shift_id>/reclose", methods=["POST"])
@login_required
def reclose_shift(shift_id: int):
    """
    Повторно закрыть смену: перезапросить наличные заказы из CRM и пересчитать
    expected_balance/discrepancy от текущего actual_balance.

    Используется после того, как админ (или флорист — для последней закрытой
    смены своей точки) отредактировал смену через PUT и/или добавил
    недостающий заказ в CRM.

    Returns:
        - shift: Обновлённые данные смены
    """
    try:
        shift = get_cash_shift_by_id(shift_id)
        if not shift:
            return error_response(f"Смена {shift_id} не найдена", 404)

        if shift["status"] != "closed":
            return error_response(f"Смена {shift_id} ещё не закрыта, используйте /close", 409)

        require_shift_edit_access(shift)

        store = get_store_by_id(shift["store_id"])
        if not store:
            return error_response(f"Точка {shift['store_id']} не найдена", 404)

        # Импортируем клиент RetailCRM
        try:
            from .retailcrm_client import get_client, get_store_code_from_name
            client = get_client()
        except Exception as e:
            logger.error(f"Не удалось инициализировать RetailCRM клиент: {e}")
            return error_response("Ошибка интеграции с CRM", 503)

        store_code = get_store_code_from_name(store["name"])

        # Тот же период, что был у смены при закрытии
        datetime_start = datetime.strptime(shift["datetime_start"], "%Y-%m-%d %H:%M:%S")
        datetime_end = (
            datetime.strptime(shift["datetime_end"], "%Y-%m-%d %H:%M:%S")
            if shift.get("datetime_end") else datetime.now()
        )

        # Запрашиваем наличные заказы из CRM
        try:
            cash_orders = client.get_cash_orders(
                store_code=store_code,
                datetime_start=datetime_start,
                datetime_end=datetime_end
            )
        except Exception as e:
            logger.error(f"Ошибка запроса к RetailCRM: {e}")
            return error_response(f"Ошибка получения заказов из CRM: {e}", 503)

        # Перезаписываем кэш заказов
        clear_shift_cache(shift_id)
        cache_cash_orders(shift_id, cash_orders)

        # Пересчитываем суммы (actual_balance берём текущий — его правят через PUT)
        cash_orders_total = sum(o["amount"] for o in cash_orders)
        collections_total = get_collections_total(shift_id)
        expected_balance = shift["opening_balance"] + cash_orders_total - collections_total
        actual_balance = shift["actual_balance"]
        discrepancy = actual_balance - expected_balance

        update_cash_shift(
            shift_id=shift_id,
            cash_orders_total=cash_orders_total,
            collections_total=collections_total,
            expected_balance=expected_balance,
            discrepancy=discrepancy
        )

        updated_shift = get_cash_shift_by_id(shift_id)

        logger.info(
            f"Смена пересчитана (reclose): ID={shift_id}, "
            f"заказы={cash_orders_total:.2f}, инкассации={collections_total:.2f}, "
            f"ожидаемый={expected_balance:.2f}, фактический={actual_balance:.2f}, "
            f"расхождение={discrepancy:.2f}"
        )

        return jsonify(success_response({
            "shift": updated_shift,
            "cash_orders_count": len(cash_orders),
            "cash_orders_total": cash_orders_total,
            "collections_total": collections_total,
            "expected_balance": expected_balance,
            "discrepancy": discrepancy
        }))

    except ShiftEditForbiddenError as e:
        return error_response(str(e), 403)
    except Exception as e:
        logger.error(f"Ошибка /api/cash-shifts/{shift_id}/reclose: {e}")
        import traceback
        traceback.print_exc()
        return error_response(str(e), 500)


# =============================================================================
# ЭНДПОИНТЫ: ДЕТАЛИ СМЕНЫ
# =============================================================================

@cashshifts_bp.route("/<int:shift_id>", methods=["GET"])
@login_required
def get_shift_details(shift_id: int):
    """
    Получить детали смены.

    Возвращает:
        - Основные данные смены
        - Список инкассаций
        - Список наличных заказов (из кэша)
        - Детали по точке и пользователю
    """
    try:
        shift = get_cash_shift_by_id(shift_id)
        if not shift:
            return error_response(f"Смена {shift_id} не найдена", 404)

        # Проверка доступа к точке
        require_store_access(shift["store_id"])

        # Получаем связанные данные
        store = get_store_by_id(shift["store_id"])
        collections = get_shift_collections(shift_id)
        cash_orders = get_shift_cash_orders(shift_id)

        # ФИО сотрудников (для журнала смены) — открывший, закрывший, авторы инкассаций
        usernames = {shift.get("florist_username"), shift.get("closed_by_username")}
        usernames.update(c.get("created_by") for c in collections)
        usernames.discard(None)
        full_names = get_users_full_names(list(usernames))

        shift_with_names = dict(shift)
        shift_with_names["opened_by_full_name"] = full_names.get(shift.get("florist_username"))
        shift_with_names["closed_by_full_name"] = full_names.get(shift.get("closed_by_username"))

        collections_with_names = []
        for c in collections:
            c_copy = dict(c)
            c_copy["created_by_full_name"] = full_names.get(c.get("created_by"))
            collections_with_names.append(c_copy)

        return jsonify(success_response({
            "shift": shift_with_names,
            "store": store,
            "collections": collections_with_names,
            "cash_orders": cash_orders,
            "collections_total": get_collections_total(shift_id),
            "cash_orders_count": len(cash_orders)
        }))

    except StoreAccessError:
        return error_response("Нет доступа к точке этой смены", 403)
    except Exception as e:
        logger.error(f"Ошибка /api/cash-shifts/{shift_id}: {e}")
        return error_response(str(e), 500)


# =============================================================================
# ЭНДПОИНТЫ: СПИСОК СМЕН
# =============================================================================

@cashshifts_bp.route("", methods=["GET"])
@login_required
def list_shifts():
    """
    Получить список смен с фильтрами.

    Query params:
        - store_id (int, опционально): Фильтр по точке
        - status (str, опционально): 'open' или 'closed'
        - date_from (str, опционально): Начало периода (YYYY-MM-DD HH:MM:SS)
        - date_to (str, опционально): Конец периода
        - limit (int, опционально): Максимум записей (default 100)
        - offset (int, опционально): Сдвиг (default 0)

    Returns:
        - count: Количество смен
        - shifts: Список смен
    """
    try:
        store_id = request.args.get("store_id", type=int)
        status = request.args.get("status")
        date_from = request.args.get("date_from")
        date_to = request.args.get("date_to")
        limit = request.args.get("limit", 100, type=int)
        offset = request.args.get("offset", 0, type=int)

        # Фильтруем доступы для не-админов
        username = get_current_username()
        role = get_current_user_role()

        if role != "admin" and not store_id:
            # Не-админ должен указывать store_id
            user_store_ids = get_user_stores(username)
            if len(user_store_ids) == 1:
                store_id = user_store_ids[0]
            else:
                return error_response("Не указан store_id", 400)

        # Проверка доступа к точке
        if store_id and role != "admin":
            require_store_access(store_id)

        # Получаем список смен
        shifts = list_cash_shifts(
            store_id=store_id,
            status=status,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
            offset=offset
        )

        # Добавляем детали по точкам
        result = []
        for shift in shifts:
            store = get_store_by_id(shift["store_id"])
            shift_copy = shift.copy()
            shift_copy["store_name"] = store["name"] if store else "Unknown"
            result.append(shift_copy)

        return jsonify(success_response({
            "count": len(result),
            "shifts": result,
            "params": {
                "store_id": store_id,
                "status": status,
                "date_from": date_from,
                "date_to": date_to,
                "limit": limit,
                "offset": offset
            }
        }))

    except StoreAccessError:
        return error_response("Нет доступа к указанной точке", 403)
    except Exception as e:
        logger.error(f"Ошибка /api/cash-shifts: {e}")
        return error_response(str(e), 500)


# =============================================================================
# ЭНДПОИНТЫ: АКТУАЛЬНАЯ СМЕНА
# =============================================================================

@cashshifts_bp.route("/open/<int:store_id>", methods=["GET"])
@login_required
def get_open_shift_by_store(store_id: int):
    """
    Получить открытую смену для точки (если есть).

    Args:
        store_id: ID точки продажи

    Returns:
        - shift: Данные открытой смены или null
    """
    try:
        # Проверка доступа
        require_store_access(store_id)

        shift = get_open_shift(store_id)
        if not shift:
            return jsonify(success_response({
                "shift": None,
                "message": "Нет открытой смены на этой точке"
            }))

        # Добавляем детали
        store = get_store_by_id(store_id)
        collections = get_shift_collections(shift["id"])
        collections_total = get_collections_total(shift["id"])

        return jsonify(success_response({
            "shift": shift,
            "store": store,
            "collections": collections,
            "collections_total": collections_total
        }))

    except StoreAccessError:
        return error_response("Нет доступа к этой точке", 403)
    except Exception as e:
        logger.error(f"Ошибка /api/cash-shifts/open/{store_id}: {e}")
        return error_response(str(e), 500)


# =============================================================================
# ЭНДПОИНТЫ: ОТКРЫТЫЕ СМЕНЫ ПО ВСЕМ ТОЧКАМ
# =============================================================================

# Как часто разрешено ходить в CRM за продажами одной открытой смены. Меньше
# этого интервала повторные заходы на страницу и F5 отдают закэшированное
# значение из cash_shifts.cash_orders_synced_at.
OPEN_SHIFTS_CRM_TTL_SECONDS = 300

# Общий потолок времени на все запросы к CRM внутри одного HTTP-запроса.
# На проде всего 2 воркера gunicorn: без потолка 9 точек × пагинация за 30 дней
# заняли бы оба воркера на минуты и положили сайт целиком (такое уже было).
# Точки, на которые бюджета не хватило, отдаются с прошлым значением и
# пометкой cash_orders_stale — пользователь видит, что число несвежее.
OPEN_SHIFTS_CRM_BUDGET_SECONDS = 10


def _crm_data_is_stale(shift: Dict[str, Any]) -> bool:
    """Пора ли обновлять продажи наличными этой смены из CRM."""
    synced_at = shift.get("cash_orders_synced_at")
    if not synced_at or shift.get("cash_orders_total") is None:
        return True
    try:
        synced_dt = datetime.strptime(synced_at, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return True
    return datetime.now() - synced_dt > timedelta(seconds=OPEN_SHIFTS_CRM_TTL_SECONDS)


def _sync_open_shifts_from_crm(shifts: List[Dict[str, Any]], force: bool = False) -> None:
    """
    Подтянуть из CRM продажи наличными для открытых смен (на месте, в shifts).

    Обновляет только те смены, чьи данные протухли (или все — при force).
    Любая ошибка по конкретной точке не роняет таблицу: смена остаётся с
    прошлым значением, а вызывающий помечает её как устаревшую.
    """
    targets = [s for s in shifts if force or _crm_data_is_stale(s)]
    if not targets:
        return

    try:
        from .retailcrm_client import (
            get_fast_client,
            get_store_code_from_name,
            CRMDeadlineExceeded,
        )
        client = get_fast_client()
    except Exception as e:
        logger.warning(f"RetailCRM недоступен для таблицы открытых смен: {e}")
        return

    deadline = time.monotonic() + OPEN_SHIFTS_CRM_BUDGET_SECONDS

    for shift in targets:
        if time.monotonic() >= deadline:
            logger.warning(
                f"Бюджет запросов к CRM исчерпан, смены после ID={shift['id']} "
                f"показаны по кэшу"
            )
            break

        try:
            datetime_start = datetime.strptime(shift["datetime_start"], "%Y-%m-%d %H:%M:%S")
            datetime_end = datetime.now()

            cash_orders = client.get_cash_orders(
                store_code=get_store_code_from_name(shift["store_name"]),
                datetime_start=datetime_start,
                datetime_end=datetime_end,
                deadline=deadline
            )
        except CRMDeadlineExceeded as e:
            # Бюджет кончился на середине пагинации — остальным точкам его
            # тем более не хватит, дальше не идём
            logger.warning(f"Смена ID={shift['id']}: {e}")
            break
        except Exception as e:
            logger.warning(f"Смена ID={shift['id']}: не удалось получить продажи из CRM: {e}")
            continue

        cash_orders_total = sum(o["amount"] for o in cash_orders)
        synced_at = datetime_end.strftime("%Y-%m-%d %H:%M:%S")

        # Кэшируем сами заказы — из них строится журнал открытой смены
        clear_shift_cache(shift["id"])
        cache_cash_orders(shift["id"], cash_orders)

        update_cash_shift(
            shift_id=shift["id"],
            cash_orders_total=cash_orders_total,
            cash_orders_synced_at=synced_at
        )

        shift["cash_orders_total"] = cash_orders_total
        shift["cash_orders_synced_at"] = synced_at


@cashshifts_bp.route("/open-shifts", methods=["GET"])
@login_required
def list_open_shifts():
    """
    Сводная таблица открытых смен по всем доступным точкам.

    Админ видит все точки, остальные роли — только свои (user_stores).

    Query params:
        - refresh (1): принудительно обновить продажи из CRM, игнорируя TTL

    Returns:
        - shifts: [{..., store_name, opened_by_full_name, collections_total,
                    cash_orders_total, expected_balance, cash_orders_stale}, ...]
    """
    try:
        role = get_current_user_role()
        username = get_current_username()

        store_ids = None if role == "admin" else get_user_stores(username)
        shifts = get_open_shifts(store_ids)

        if not shifts:
            return jsonify(success_response({"count": 0, "shifts": []}))

        force = request.args.get("refresh") == "1"
        _sync_open_shifts_from_crm(shifts, force=force)

        # ФИО открывших — тем же способом, что и в журнале смены
        usernames = {s.get("florist_username") for s in shifts}
        usernames.discard(None)
        full_names = get_users_full_names(list(usernames))

        result = []
        for shift in shifts:
            collections_total = get_collections_total(shift["id"])
            cash_orders_total = shift.get("cash_orders_total")

            item = dict(shift)
            item["opened_by_full_name"] = full_names.get(shift.get("florist_username"))
            item["collections_total"] = collections_total
            item["cash_orders_stale"] = _crm_data_is_stale(shift)
            item["expected_balance"] = (
                (shift["opening_balance"] or 0) + cash_orders_total - collections_total
                if cash_orders_total is not None else None
            )
            result.append(item)

        return jsonify(success_response({"count": len(result), "shifts": result}))

    except Exception as e:
        logger.error(f"Ошибка /api/cash-shifts/open-shifts: {e}")
        import traceback
        traceback.print_exc()
        return error_response(str(e), 500)


# =============================================================================
# ЭНДПОИНТЫ: ОСТАТКИ ПО ТОЧКАМ
# =============================================================================

@cashshifts_bp.route("/balances", methods=["GET"])
@role_required("admin")
def get_store_balances():
    """
    Получить фактический остаток денег в кассе по всем точкам (только админ).

    Остаток берётся из actual_balance последней ЗАКРЫТОЙ смены точки — это
    последняя реально пересчитанная сумма. Если на точке сейчас открыта смена,
    остаток дополнительно помечается как устаревший (has_open_shift=True),
    т.к. фактическая сумма в кассе могла измениться и станет известна только
    после закрытия.

    Returns:
        - balances: [{store_id, store_name, actual_balance, updated_at, has_open_shift}, ...]
    """
    try:
        stores = get_all_stores()

        balances = []
        for store in stores:
            last_closed = get_last_closed_shift(store["id"])
            balances.append({
                "store_id": store["id"],
                "store_name": store["name"],
                "actual_balance": last_closed["actual_balance"] if last_closed else None,
                "updated_at": last_closed["closed_at"] if last_closed else None,
                "has_open_shift": get_open_shift(store["id"]) is not None
            })

        return jsonify(success_response({"balances": balances}))
    except Exception as e:
        logger.error(f"Ошибка /api/cash-shifts/balances: {e}")
        return error_response(str(e), 500)


# =============================================================================
# РЕГИСТРАЦИЯ BLUEPRINT
# =============================================================================

def register_cashshifts(app):
    """
    Зарегистрировать Blueprint в приложении Flask.

    Использование в app.py или server.py:

        from cashshifts.server import register_cashshifts
        register_cashshifts(app)
    """
    app.register_blueprint(cashshifts_bp)
    logger.info("Blueprint кассовых смен зарегистрирован")


# =============================================================================
# ТЕСТЫ (запуск через python -m cashshifts.server)
# =============================================================================

if __name__ == "__main__":
    print("=" * 60)
    print("КАССОВЫЕ СМЕНЫ — SERVER")
    print("=" * 60)
    print("\nBlueprint создан. Эндпоинты:")

    for rule in cashshifts_bp.url_map.iter_rules():
        if rule.endpoint != "static":
            print(f"  {rule.methods} {rule.rule}")

    print("\nДля использования:")
    print("  from cashshifts.server import register_cashshifts")
    print("  register_cashshifts(app)")
