# Pyrus Integration

Интеграция с Pyrus API для проекта "Бархат" (сеть цветочных салонов).

## Возможности

- ✅ Получение форм и задач из Pyrus
- ✅ Хранение данных в SQLite с историчностью
- ✅ JSON API для дашбордов
- ✅ Ручное обновление данных

## Структура

```
src/pyrus/
├── __init__.py      # Модуль
├── client.py        # REST-клиент Pyrus API
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
cp .env.example .env
# Отредактируйте .env: PYRUS_LOGIN и PYRUS_SECURITY_KEY
```

## Использование

### Обновление данных

```bash
# Все формы
python scripts/update_pyrus.py

# Конкретные формы
python scripts/update_pyrus.py --forms 123 456

# С архивными задачами
python scripts/update_pyrus.py --full

# Только статистика
python scripts/update_pyrus.py --stats
```

### Запуск JSON API сервера

```bash
python -m pyrus.server
# или
python -m pyrus.server --host 0.0.0.0 --port 8080
```

### API Endpoints

| Endpoint | Описание | Параметры |
|---|---|---|
| `GET /health` | Проверка здоровья | - |
| `GET /api/pyrus/forms` | Список форм | `format=simple\|full` |
| `GET /api/pyrus/tasks` | Задачи | `form_id`, `limit`, `status`, `date_from`, `date_to`, `include_raw` |
| `GET /api/pyrus/stats` | Статистика БД | - |

### Примеры запросов

```bash
# Все формы
curl http://localhost:5000/api/pyrus/forms

# Задачи формы 123
curl http://localhost:5000/api/pyrus/tasks?form_id=123

# Задачи с фильтром по статусу
curl http://localhost:5000/api/pyrus/tasks?status=finished

# Исторические данные за период
curl "http://localhost:5000/api/pyrus/tasks?date_from=2025-01-01&date_to=2025-01-31"

# Статистика
curl http://localhost:5000/api/pyrus/stats
```

## База данных

Данные хранятся в SQLite (путь: `PYRUS_DB_PATH` из `.env`, по умолчанию `data/pyrus.db`).

### Таблицы

- `forms` — метаданные форм
- `tasks` — исторические записи задач (снапшоты)
- `latest_tasks` — актуальное состояние задач
- `sync_log` — лог синхронизаций

## Интеграция с дашбордом

Добавьте ссылку на JSON endpoint в ваш дашборд:

```
http://your-server:5000/api/pyrus/tasks?form_id=123&limit=1000
```

Ответ в формате JSON:
```json
{
  "success": true,
  "count": 150,
  "params": { "form_id": 123, "limit": 1000 },
  "data": [
    {
      "form_id": 123,
      "task_id": 456789,
      "title": "Задача",
      "status": "active",
      "last_modified": "2025-08-05T12:00:00"
    }
  ]
}
```

## Troubleshooting

**Ошибка авторизации:**
- Проверьте `PYRUS_LOGIN` и `PYRUS_SECURITY_KEY` в `.env`
- Убедитесь, что API ключ активен в Pyrus (Настройки → API ключи)

**Нет данных:**
- Запустите `python scripts/update_pyrus.py --stats` для проверки
- Проверьте есть ли задачи в формах Pyrus

**Медленная загрузка:**
- Используйте `--limit` для ограничения задач
- Загружайте только нужные формы через `--forms`
