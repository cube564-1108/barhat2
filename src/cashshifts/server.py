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
import logging
from datetime import datetime
from typing import Optional

from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user

# Импортируем модуль авторизации
auth_path = os.path.join(os.path.dirname(__file__), '../')
sys.path.insert(0, auth_path)
from auth import role_required

from .storage import (
    get_all_stores,
    get_all_categories,
    get_store_by_id,
    get_category_by_id,
    get_user_stores,
    check_store_access,
    # Смены
    create_cash_shift,
    get_open_shift,
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

        # Проверка: смена должна быть открыта
        if shift["status"] != "open":
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
            logger.error(f"Ошибка запроса к RetailCRM с фильтром по магазину: {e}")
            # Fallback: пробуем без фильтра по магазину
            try:
                logger.warning("Пробуем запрос без фильтра по магазину...")
                cash_orders = client.get_cash_orders(
                    store_code=None,
                    datetime_start=datetime_start,
                    datetime_end=datetime_end
                )
                logger.info(f"Успешно получено {len(cash_orders)} заказов без фильтра по магазину")
            except Exception as e2:
                logger.error(f"Ошибка запроса к RetailCRM (даже без фильтра): {e2}")
                return error_response(f"Ошибка получения заказов из CRM: {e2}", 503)

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
            closed_at=closed_at
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

        return jsonify(success_response({
            "shift": shift,
            "store": store,
            "collections": collections,
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
