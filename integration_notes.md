# Подключение авторизации к БАРХАТ (Flask)

## 1. Добавить зависимости

В `requirements.txt`:
```
flask-login==0.6.3
flask-cors==4.0.1
```
(werkzeug уже стоит вместе с Flask — хэширование паролей берём из него, доп. пакет не нужен)

## 2. Подключить в главном файле приложения (app.py / main.py)

```python
import os
from flask import Flask
from flask_cors import CORS
from auth import auth_bp, login_manager, init_auth_tables

app = Flask(__name__)

# Секретный ключ для подписи сессий — обязательно из env, сгенерировать один раз
# и никогда не менять "на лету" (иначе разлогинит всех разом)
app.secret_key = os.environ["FLASK_SECRET_KEY"]

# Cookie-сессии: безопасные настройки
app.config.update(
    SESSION_COOKIE_SECURE=True,      # передаётся только по HTTPS
    SESSION_COOKIE_HTTPONLY=True,    # недоступна из JS в браузере
    SESSION_COOKIE_SAMESITE="Lax",   # защита от части CSRF-сценариев
    PERMANENT_SESSION_LIFETIME=60 * 60 * 8,  # автологаут через 8 часов
)

login_manager.init_app(app)
login_manager.login_view = None  # у вас SPA/фронтенд сам решает, куда редиректить на 401

# CORS — ограничить своим доменом, а не "*"
CORS(app, supports_credentials=True, origins=[
    "https://barhat2-cube564.amvera.io",
])

app.register_blueprint(auth_bp)

with app.app_context():
    init_auth_tables()

# ... дальше регистрируете остальные blueprint'ы (quality, calculator и т.д.)
```

## 3. Защитить существующие эндпоинты

Там, где сейчас отдаются данные (например, `/api/quality`, `/api/calculator/...`,
эндпоинт редактирования прайс-листа), добавить декоратор:

```python
from auth import section_required, role_required

@app.route("/api/quality")
@section_required("quality")
def get_quality_data():
    ...

@app.route("/api/price-list", methods=["POST"])
@role_required("admin")   # редактирование цен — только админ
def update_price_list():
    ...
```

## 4. Создать первого администратора

Разово, локально или через одноразовый скрипт на сервере (НЕ через открытый API-роут):

```python
# create_admin.py — запустить один раз, потом удалить/не оставлять в проде
from auth import get_db, init_auth_tables
from werkzeug.security import generate_password_hash
from datetime import datetime

init_auth_tables()
conn = get_db()
conn.execute(
    "INSERT INTO users (username, password_hash, role, is_active, created_at) VALUES (?,?,?,1,?)",
    ("admin", generate_password_hash("ЗАМЕНИТЕ_НА_СВОЙ_НАДЁЖНЫЙ_ПАРОЛЬ"), "admin", datetime.utcnow().isoformat())
)
conn.commit()
print("Готово")
```

## 5. На стороне фронтенда

- Все `fetch()`-запросы к API должны идти с `credentials: 'include'`, чтобы cookie сессии отправлялась.
- Проверять `/api/auth/me` при загрузке страницы — если 401, показывать форму логина.
- Показывать/скрывать разделы дашборда по полю `sections` из ответа `/api/auth/me`
  (но это только UX — реальная защита всё равно на бэкенде через декораторы, фронтенд не источник истины).

## 6. Переменные окружения на Amvera

Добавить в настройках проекта (Secrets/Environment):
```
FLASK_SECRET_KEY=<сгенерировать: python -c "import secrets; print(secrets.token_hex(32))">
BARHAT_DB_PATH=/путь/к/вашей/barhat.db
```

## 7. Что дальше можно докрутить

- Rate limiting на `/api/auth/login` (например, `flask-limiter`) — защита от подбора пароля.
- 2FA для роли admin.
- Ротация паролей / принудительная смена при первом входе.
- Вынести audit_log в отдельную страницу дашборда для самих же сотрудников (прозрачность).
