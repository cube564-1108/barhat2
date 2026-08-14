# План: Модуль учёта кассы (Cash Shifts)

> **Цель:** Автоматический учёт наличных в кассах по точкам продаж БАРХАТ
> **Дата создания:** 2025-08-14
> **Статус:** 🚧 В разработке

---

## Контекст проекта

- Проект: БАРХАТ — сеть цветочных салонов
- Стек: Python 3.11, Flask, SQLite, RetailCRM API
- Роль в проекте: Единственный разработчик, владелец работает один
- Критическое требование: Минимум ручного труда, всё автоматизируется

---

## Done When (Критерий завершения)

✅ Флорист может открыть смену на СВОЕЙ точке продаж
✅ Флорист может добавить инкассацию (сумма + категория + комментарий)
✅ При закрытии смены автоматически запрашиваются наличные заказы из RetailCRM
✅ Рассчитывается расхождение между расчётным и фактическим остатком
✅ Админ может редактировать закрытые смены (остаток, инкассации)
✅ Управляющий видит несколько салонов, флорист — только один
✅ Админ видит все точки и может управлять доступом
✅ Видна история смен с фильтрами (точка, дата, статус)
✅ Добавление новых категорий расходов — только админом

---

## Фаза 1: Seed-данные и подготовка

**Goal:** Подготовить все справочники до создания миграций

**Задачи:**
- [ ] Создать файл `src/cashshifts/seed_data.py` с константами:
  - 9 точек продаж (НСК Восход, 3; НСК Блюхера, 61; и т.д.)
  - 16 категорий расходов (Бананы, Декор и упаковка, ЗП флористы, и т.д.)
  - + категория "Корректировка излишка"
- [ ] Добавить код оплаты наличных: `cash-in-shop`
- [ ] Подготовить SQL для вставки seed-данных

**Constraints:**
- Все данные как константы в Python (не хардкод в SQL)
- Готовность к быстрой замене/добавлению данных

**Done when:**
- Файл `seed_data.py` создан
- Все константы определены
- SQL-скрипт готов к запуску

---

## Фаза 2: Миграции БД

**Goal:** Создать все таблицы для модуля кассовых смен

**Задачи:**
- [ ] Создать модуль `src/cashshifts/storage.py`
- [ ] SQL-таблицы:
  - `stores` — точки продаж
  - `expense_categories` — статьи расхода
  - `cash_shifts` — кассовые смены
  - `cash_collections` — инкассации
  - `cash_orders_cache` — кэш заказов из CRM
  - `user_stores` — связь пользователей с точками (новое!)
- [ ] Вставить seed-данные при создании таблиц
- [ ] Добавить FK-constraints и CHECK-constraints
- [ ] Индексы для быстрого поиска (store_id, datetime_start, status)

**Constraints:**
- Использовать `BARHAT_DB_PATH` из .env
- Всё через sqlite3 с row_factory
- Проверять существование таблицы перед созданием

**Done when:**
- Миграция запускается без ошибок
- В БД есть 9 точек и 16+1 категорий
- FK-constraints работают (тест каскадного удаления)

---

## Фаза 3: RetailCRM-клиент

**Goal:** Модуль запроса наличных заказов из CRM

**Задачи:**
- [ ] Создать `src/cashshifts/retailcrm_client.py`
- [ ] Функция `get_cash_orders(store_id, datetime_start, datetime_end)`:
  - Фильтрация по магазину (store_code в CRM)
  - Фильтрация по периоду (datetime_start .. datetime_end)
  - Фильтрация платежей по type='cash-in-shop'
  - Парсинг массива payments[] в заказе
  - Суммирование подходящих платежей
- [ ] Кэширование результатов в `cash_orders_cache`
- [ ] Обработка ошибок API (таймауты, авторизация)

**Constraints:**
- Использовать `RETAILCRM_URL` и `RETAILCRM_API_KEY` из .env
- Всегда фильтровать по payments[].type, не весь заказ
- Временные метки в ISO8601

**Done when:**
- Функция возвращает корректную сумму наличных за период
- Платежи кэшируются в БД
- Проверено на реальных данных из CRM

---

## Фаза 4: Flask API — базовые операции

**Goal:** REST API для открытия/закрытия смен и инкассаций

**Структура:** `src/cashshifts/server.py`

**Эндпоинты:**

### Открытие смены
- [ ] `POST /api/cash-shifts/open` {store_id, shift_type}
  - Проверка: нет ли открытой смены на этой точке
  - opening_balance = actual_balance последней закрытой смены
  - Создание записи со status='open'

### Добавление инкассации
- [ ] `POST /api/cash-shifts/{id}/collections` {amount, expense_category_id, custom_comment}
  - Проверка: смена должна быть открыта
  - Создание записи в cash_collections

### Закрытие смены
- [ ] `POST /api/cash-shifts/{id}/close` {actual_balance}
  - Запрос наличных заказов из CRM
  - Расчёт collections_total (сумма инкассаций)
  - Расчёт expected_balance
  - Сохранение actual_balance и discrepancy
  - status='closed', closed_at=now

**Constraints:**
- Все эндпоинты требуют авторизации (Flask-Login)
- Флорист работает только со своей точкой
- Админ видит все точки

**Done when:**
- Все эндпоинты работают через Postman/curl
- Проверен расчёт expected_balance
- Логика role-based работает

---

## Фаза 5: Flask API — история и корректировки

**Goal:** API для просмотра истории и редактирования смен админом

**Эндпоинты:**

### Детали смены
- [ ] `GET /api/cash-shifts/{id}`
  - Основные данные смены
  - Список инкассаций
  - Детализация наличных заказов (из cash_orders_cache)

### Список смен с фильтрами
- [ ] `GET /api/cash-shifts?store_id=&status=&date_from=&date_to=`
  - Пагинация
  - Сортировка по дате

### Редактирование смены (только админ)
- [ ] `PUT /api/cash-shifts/{id}` {actual_balance, collections?}
  - Перезапись actual_balance
  - Обновление сумм инкассаций
  - Пересчёт discrepancy

### Повторное закрытие смены
- [ ] `POST /api/cash-shifts/{id}/reclose`
  - Повторный запрос к CRM
  - Пересчёт expected_balance и discrepancy
  - Обновление cash_orders_cache

### CRUD категорий расходов
- [ ] `GET /api/expense-categories` — список активных
- [ ] `POST /api/expense-categories` — добавить (только админ)
- [ ] `PUT /api/expense-categories/{id}` — редактировать
- [ ] `DELETE /api/expense-categories/{id}` — деактивировать (не удалять)

**Constraints:**
- Редактирование смены — только @role_required('admin')
- Проверка FK при редактировании инкассаций
- История изменений через audit_log

**Done when:**
- Фильтры работают (точка, дата, статус)
- Админ может редактировать закрытую смену
- Повторное закрытие пересчитывает discrepancy

---

## Фаза 6: Интеграция с системой авторизации

**Goal:** Добавить роль florist и права доступа

**Задачи:**
- [ ] Добавить роли `florist` и `supervisor` в ROLE_SECTIONS (src/auth.py)
- [ ] Определить права доступа:
  - florist: открытие/закрытие смен СВОЕЙ точки, добавление инкассаций
  - supervisor: открытие/закрытие смен НА НЕСКОЛЬКИХ точках, просмотр истории
  - admin: всё + редактирование смен + CRUD категорий + управление доступом
- [ ] Создать модуль `src/cashshifts/access_control.py`:
  - `get_user_stores(username)` — возвращает список точек пользователя
  - `check_store_access(username, store_id)` — проверка доступа к точке
  - `is_florist(username)` — флорист привязан к 1 точке
  - `is_supervisor(username)` — управляющий привязан к >1 точке
- [ ] Добавить API для управления доступом (только админ):
  - `POST /api/auth/users/{username}/stores` — привязать к точкам
  - `GET /api/auth/users/{username}/stores` — список точек пользователя
  - `DELETE /api/auth/users/{username}/stores/{store_id}` — отвязать
- [ ] Обновить ALL_MODULES в auth.py (добавить `cash_management`)

**Constraints:**
- Не сломать существующие роли (admin, manager, florist_analyst)
- Permissions хранятся в таблице permissions
- Флорист не может работать с чужой точкой (жёсткая проверка)

**Done when:**
- Роли florist и supervisor созданы
- Функции access_control.py работают
- Флорист видит только свою точку, управляющий — несколько

---

## Фаза 7: Тестирование и отладка

**Goal:** Проверить все сценарии на реальных данных

**Сценарии тестирования:**
- [ ] Открытие смены → проверка opening_balance
- [ ] Добавление инкассации → проверка списков
- [ ] Закрытие смены → проверка расчёта discrepancy
- [ ] Расхождение (недостача) → редактирование админом → повторное закрытие
- [ ] Излишек → добавление корректирующей инкассации → повторное закрытие
- [ ] История смен → фильтры по точке и дате
- [ ] Добавление категории расходов (админом)

**Constraints:**
- Тестировать на реальном .env с RetailCRM
- Проверить ночную смену (пересечение полуночи)

**Done when:**
- Все сценарии отрабатывают корректно
- Ночная смена считается верно
- Расхождения рассчитываются точно

---

## Технические детали

### Модель данных (SQL)

```sql
CREATE TABLE stores (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    is_active BOOLEAN DEFAULT 1
);

CREATE TABLE expense_categories (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    is_active BOOLEAN DEFAULT 1
);

CREATE TABLE cash_shifts (
    id INTEGER PRIMARY KEY,
    store_id INTEGER NOT NULL REFERENCES stores(id),
    shift_type TEXT NOT NULL CHECK (shift_type IN ('day', 'night')),
    datetime_start TEXT NOT NULL,
    datetime_end TEXT,
    florist_id INTEGER,
    opening_balance REAL NOT NULL DEFAULT 0,
    cash_orders_total REAL,
    collections_total REAL,
    expected_balance REAL,
    actual_balance REAL,
    discrepancy REAL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    closed_at TEXT
);

CREATE TABLE cash_collections (
    id INTEGER PRIMARY KEY,
    shift_id INTEGER NOT NULL REFERENCES cash_shifts(id),
    date TEXT NOT NULL,
    amount REAL NOT NULL,
    expense_category_id INTEGER NOT NULL REFERENCES expense_categories(id),
    custom_comment TEXT,
    created_by INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE cash_orders_cache (
    id INTEGER PRIMARY KEY,
    shift_id INTEGER NOT NULL REFERENCES cash_shifts(id),
    retailcrm_order_id INTEGER NOT NULL,
    amount REAL NOT NULL,
    paid_at TEXT NOT NULL
);

CREATE TABLE user_stores (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL,
    store_id INTEGER NOT NULL REFERENCES stores(id),
    UNIQUE(username, store_id),
    FOREIGN KEY (username) REFERENCES users(username) ON DELETE CASCADE
);
```

### RetailCRM integration

**Код наличной оплаты:** `cash-in-shop`

**Фильтрация платежей:**
```python
# Не весь заказ, а каждый платеж в payments[]
for payment in order['payments']:
    if payment['type'] == 'cash-in-shop':
        if datetime_start <= payment['paidAt'] <= datetime_end:
            total += payment['amount']
```

### Механика корректировок

```
Смена закрыта → расхождение обнаружено
         ↓
    Администратор:
    ├─ Редактирует actual_balance
    ├─ Редактирует суммы инкассаций
    └─ Добавляет заказ в CRM
         ↓
    Повторное закрытие смены (POST /reclose)
         ↓
    Остаток излишек?
         ↓ да
    Флорист добавляет инкассацию "Корректировка излишка"
         ↓
    Повторное закрытие → discrepancy = 0
```

### Модель доступа к точкам продаж

```
Флорист (florist):
  └─ get_user_stores(username) → [store_id]
  └─ Если len(stores) != 1 → ошибка "Флорист должен быть привязан к одной точке"
  └─ Все операции только с этой точкой
  └─ При открытии смены: store_id берётся из привязки (не из запроса)

Управляющий (supervisor):
  └─ get_user_stores(username) → [store_id1, store_id2, ...]
  └─ Может работать с любыми из своих точек
  └─ Видит историю только по своим точкам

Админ (admin):
  └─ get_user_stores() → [] (пропускает проверку)
  └─ Может работать со всеми точками
  └─ Может управлять доступом (привязывать пользователей к точкам)

API управления доступом (только admin):
  POST /api/auth/users/{username}/stores
    Body: {store_ids: [1, 3, 5]}
    └─ Привязывает пользователя к точкам
    └─ Для флориста: только 1 точка (валидация)

  GET /api/auth/users/{username}/stores
    └─ Возвращает список точек пользователя

  DELETE /api/auth/users/{username}/stores/{store_id}
    └─ Отвязывает пользователя от точки
```

---

## Зависимости и конфликты

**Зависит от:**
- `src/auth.py` — система авторизации
- `src/pyrus/storage.py` — паттерн работы с SQLite

**Конфликты с другими фазами:**
- Нет — новая функциональность в отдельном модуле

---

## Безопасность

- ✅ Все секреты в .env (RETAILCRM_URL, RETAILCRM_API_KEY)
- ✅ .env в .gitignore
- ✅ Ролевая модель через Flask-Login
- ✅ FK-constraints в БД
- ✅ Проверка прав на каждом эндпоинте

---

## Следующие шаги

1. **Чат 2:** Реализация Фазы 1-2 (Seed-данные + Миграции)
2. **Чат 3:** Реализация Фазы 3-4 (RetailCRM + Базовый API)
3. **Чат 4:** Реализация Фазы 5-7 (История + Корректировки + Тестирование)

---

*План создан по методологии Артемия Миллера (multi-chat workflow)*
