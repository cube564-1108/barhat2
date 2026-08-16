# МойСклад Integration

Интеграция с МойСклад API для проекта "Бархат" (сеть цветочных салонов).

## Возможности

- ✅ Работа с МойСклад API (remap 1.2)
- ✅ Загрузка товаров, остатков, заказов
- ✅ Хранение данных в SQLite
- ✅ JSON API для дашбордов
- ✅ Retry-логика и автоматическая пагинация

## Структура

```
src/moysklad/
├── __init__.py      # Модуль
├── client.py        # REST-клиент МойСклад API
├── fetcher.py       # Загрузка данных с retry-логикой
├── storage.py       # Хранилище SQLite
└── server.py        # Flask JSON API
```

## Установка

1. Установите зависимости:
```bash
pip install -r requirements.txt
```

2. Настройте credentials в `.env`:
```bash
# Bearer token (рекомендуется)
MOYSKLAD_TOKEN=ваш_api_token

# Или Basic Auth
MOYSKLAD_LOGIN=ваш_login
MOYSKLAD_PASSWORD=ваш_password

# Путь к БД
MOYSKLAD_DB_PATH=data/moysklad.db

# Для создания документов (списание и т.д.) — href организации.
# Получить: python scripts/get_moysklad_organization.py
MOYSKLAD_ORGANIZATION_HREF=https://api.moysklad.ru/api/remap/1.2/entity/organization/...
```

### Получение API токена

1. Зайдите в МойСklad
2. Настройки → API ключи
3. Создайте новый ключ
4. Скопируйте токен в `.env`

## Использование

### Тест подключения

```bash
python -m moysklad.client
```

### Работа с клиентом

```python
from moysklad import get_client

client = get_client()

# Получить товары
products = client.get_products(limit=100)
print(f"Товаров: {products['meta']['size']}")

# Получить остатки
stock = client.get_stock(limit=100)

# Получить склады
stores = client.get_stores()
```

### Загрузка данных

```python
from moysklad import get_fetcher, get_storage

fetcher = get_fetcher()
storage = get_storage()

# Загрузить все товары
products = fetcher.get_all_products(max_items=10000)
if products:
    storage.save_products(products)

# Загрузить остатки
stock = fetcher.get_all_stock()
if stock:
    storage.save_stocks(stock)
```

### Запуск JSON API сервера

```bash
python -m moysklad.server
# или
python -m moysklad.server --host 0.0.0.0 --port 8080
```

## API Endpoints

| Endpoint | Описание | Параметры |
|---|---|---|
| `GET /health` | Проверка здоровья | - |
| `GET /api/moysklad/products` | Товары | `folder_id`, `archived`, `limit` |
| `GET /api/moysklad/stock` | Остатки | `store_id`, `limit` |
| `GET /api/moysklad/stores` | Склады | - |
| `GET /api/moysklad/sales_orders` | Заказы | `date_from`, `date_to`, `limit` |
| `GET /api/moysklad/stats` | Статистика БД | - |

### Примеры запросов

```bash
# Все товары
curl http://localhost:5001/api/moysklad/products

# Активные товары
curl http://localhost:5001/api/moysklad/products?archived=active

# Остатки на складе
curl http://localhost:5001/api/moysklad/stock

# Заказы за период
curl "http://localhost:5001/api/moysklad/sales_orders?date_from=2025-01-01&date_to=2025-01-31"

# Статистика
curl http://localhost:5001/api/moysklad/stats
```

## База данных

Данные хранятся в SQLite (путь: `MOYSKLAD_DB_PATH` из `.env`, по умолчанию `data/moysklad.db`).

### Таблицы

- `products` — товары
- `stock` — остатки
- `stores` — склады
- `folders` — папки/группы товаров
- `sales_orders` — заказы покупателей
- `counterparties` — контрагенты
- `demands` — расходные накладные
- `sync_log` — лог синхронизаций

## Создание документов

```python
client = get_client()

client.create_loss(
    organization_href=os.getenv("MOYSKLAD_ORGANIZATION_HREF"),
    store_href="https://api.moysklad.ru/api/remap/1.2/entity/store/...",
    positions=[
        {"assortment_href": "https://api.moysklad.ru/api/remap/1.2/entity/product/...", "quantity": 3},
        {"assortment_href": "https://api.moysklad.ru/api/remap/1.2/entity/product/...", "quantity": 1},
    ],
    applicable=True,       # сразу проводит документ, остаток уменьшается
    description="Списание #42 (дашборд)",
)
```

Все позиции уходят одним документом за один запрос — списание либо проводится
целиком, либо не проводится вовсе (используется модулем `src/writeoffs/`).

## Документация API МойСклад

Полная документация: https://dev.moysklad.ru/doc/api/remap/1.2/

Основные сущности:
- `/entity/product` — товары
- `/entity/assortment` — ассортимент
- `/entity/store` — склады
- `/entity/salesorder` — заказы покупателей
- `/entity/demand` — расходные накладные
- `/entity/counterparty` — контрагенты
- `/report/stock` — отчёт по остаткам

## Troubleshooting

**Ошибка авторизации:**
- Проверьте `MOYSKLAD_TOKEN` в `.env`
- Убедитесь, что API ключ активен в МойСклад

**Rate limiting (429):**
- Автоматический retry встроен
- Можно увеличить задержку в `fetcher.py`

**Нет данных:**
- Запустите загрузку данных скриптом
- Проверьте есть ли данные в МойСклад

**Медленная загрузка:**
- Используйте `max_items` для ограничения
- Загружайте только нужные сущности
