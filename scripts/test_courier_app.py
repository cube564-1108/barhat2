"""
Сторож экрана курьера (Фаза 3): PWA-оболочка, фото позиций, чек-лист дизайна.

Что здесь ловится и почему именно тестом:

1. **Service worker без версии сборки.** Статика отдаётся с no-cache ровно
   затем, чтобы деплой доезжал до людей сразу. SW, закэшировавший оболочку под
   постоянным ключом, сводит это на нет — курьер после выкатки видит вчерашний
   экран и не понимает, почему кнопка «не работает» (находка К5 критики плана).
   Проверяется, что версию подставляет сервер и что она меняется вместе с
   файлами.
2. **`/app/<файл>` как дыра в каталог дашборда.** Отдавать `src/dashboard`
   целиком по маске нельзя: там разметка всех модулей.
3. **Кэш персональных данных.** `/api/*` в service worker не кэшируется
   никогда: отдать заказ с телефоном клиента другому вошедшему — утечка ПДн.
4. **Очередь фото.** Товар без фото обязан запоминаться записью, а не
   отсутствием записи, иначе он вечный кандидат и заставляет ходить в CRM
   каждый тик — так выжигали месячную квоту ПланФакта.
5. **Чек-лист §9 DESIGN-SPEC:** нет эмодзи, свои цвета не вводятся, нативные
   диалоги не используются (внутри Пульса они молча игнорируются).

Работает на ВРЕМЕННЫХ базах и с заблокированной сетью: приложение при импорте
читает боевой .env и умеет ходить в RetailCRM.

Запуск: python scripts/test_courier_app.py
"""

import json
import os
import re
import socket
import ssl  # noqa: F401  — импортировать до патча сокета
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))
DASHBOARD = os.path.join(REPO, "src", "dashboard")


class NetworkBlocked(Exception):
    pass


def _blocked(*args, **kwargs):
    raise NetworkBlocked("сторож не должен ходить в боевые внешние API")


socket.socket.connect = _blocked

WORK_DIR = tempfile.mkdtemp(prefix="courier_app_")
os.environ["BARHAT_DB_PATH"] = os.path.join(WORK_DIR, "barhat.db")
os.environ["COURIERS_DB_PATH"] = os.path.join(WORK_DIR, "couriers.db")
os.environ["PYRUS_DB_PATH"] = os.path.join(WORK_DIR, "pyrus.db")
os.environ["MOYSKLAD_DB_PATH"] = os.path.join(WORK_DIR, "moysklad.db")
os.environ["DISABLE_SCHEDULERS"] = "1"

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"  [ok] {name}")
    else:
        print(f"  [FAIL] {name} {detail}")
        failures.append(name)


print(f"\nвременные базы: {WORK_DIR}")

import auth  # noqa: E402
from pyrus import server as pyrus_server  # noqa: E402
from pyrus.server import app  # noqa: E402

from couriers import delivery_storage as ds  # noqa: E402
from couriers import storage as cs  # noqa: E402

app.config["TESTING"] = True

with app.app_context():
    auth.init_auth_tables()
cs.init_couriers_tables()
ds.init_delivery_tables()


def make_user(username, role, sections):
    conn = auth.get_db()
    try:
        from werkzeug.security import generate_password_hash
        conn.execute(
            "INSERT INTO users (username, full_name, password_hash, role, is_active, created_at) "
            "VALUES (?, ?, ?, ?, 1, datetime('now'))",
            (username, username, generate_password_hash("Parol12345"), role),
        )
        for section in sections:
            conn.execute(
                "INSERT INTO permissions (username, module_name, can_view) VALUES (?, ?, 1)",
                (username, section),
            )
        conn.commit()
    finally:
        conn.close()


make_user("kurier", "courier", ["courier_app"])


# ============================================================================
print("\n1. Страница приложения")
# ============================================================================

anon = app.test_client()
response = anon.get("/app/courier")
check("без входа /app/courier уводит на /login",
      response.status_code in (301, 302) and "/login" in response.headers.get("Location", ""),
      f"({response.status_code}, {response.headers.get('Location')})")

client = app.test_client()
login = client.post("/api/auth/login", json={"username": "kurier", "password": "Parol12345"})
check("курьер входит", login.status_code == 200, f"({login.status_code})")

response = client.get("/app/courier")
page = response.get_data(as_text=True)
check("/app/courier отдаётся курьеру", response.status_code == 200, f"({response.status_code})")
check("страница подключает манифест", 'rel="manifest"' in page)
check("страница не тянет дашборд", "script.js" not in page and "styles.css" not in page)
check("подключён Vollkorn", "Vollkorn" in page)


# ============================================================================
print("\n2. Service worker и версия сборки")
# ============================================================================

response = anon.get("/app/courier-sw.js")
sw = response.get_data(as_text=True)
check("service worker отдаётся и без входа", response.status_code == 200,
      f"({response.status_code})")
check("версия сборки подставлена", "__CACHE_VERSION__" not in sw)
check("сам файл воркера не кэшируется",
      "no-store" in (response.headers.get("Cache-Control") or ""),
      f"({response.headers.get('Cache-Control')})")

version = re.search(r"CACHE_VERSION\s*=\s*'([^']+)'", sw)
check("версия непустая", bool(version and version.group(1)), f"({sw[:120]!r})")

# Версия обязана поменяться вместе с файлами оболочки — иначе после деплоя у
# курьера останется вчерашний экран.
before = pyrus_server._courier_build_version()
pyrus_server._courier_shell_version = None
os.utime(os.path.join(DASHBOARD, "courier-app.js"), (time.time() + 10, time.time() + 10))
after = pyrus_server._courier_build_version()
check("версия меняется при правке оболочки", before != after, f"({before} → {after})")

check("/api/* не кэшируется воркером",
      "startsWith('/api/')" in sw and "skipWaiting" in sw)
check("навигация идёт network-first", "request.mode === 'navigate'" in sw)


# ============================================================================
print("\n3. Статика приложения: только свой список")
# ============================================================================

for name, kind in (("courier-app.css", "text/css"),
                   ("courier-app.js", "javascript"),
                   ("courier-icon.svg", "svg"),
                   ("courier-manifest.json", "json")):
    response = anon.get("/app/" + name)
    check(f"/app/{name} отдаётся", response.status_code == 200, f"({response.status_code})")

for name in ("index.html", "users.js", "../auth.py"):
    response = anon.get("/app/" + name)
    check(f"/app/{name} не отдаётся", response.status_code == 404, f"({response.status_code})")

# Файл, который лежит на диске разработчика, но не попал в git, ведёт себя
# ровно как рабочий — до деплоя. `.gitignore` проекта глушит `*.json` целиком
# (правило заведено под выгрузки), и манифест PWA под него подпадает: локально
# установка приложения работает, на проде её не предлагают, и понять почему
# неоткуда. Поэтому проверяем не «файл существует», а «файл в репозитории».
import subprocess  # noqa: E402

for name in sorted(pyrus_server.COURIER_APP_FILES | {"courier-app.html", "courier-sw.js"}):
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "src/dashboard/" + name],
        cwd=REPO, capture_output=True,
    ).returncode == 0
    check(f"{name} лежит в git, а не только на диске", tracked,
          "(файл не доедет до прода)")

manifest = json.loads(anon.get("/app/courier-manifest.json").get_data(as_text=True))
check("start_url ведёт в приложение", manifest.get("start_url") == "/app/courier",
      f"({manifest.get('start_url')})")
check("scope покрывает service worker", manifest.get("scope") == "/app/",
      f"({manifest.get('scope')})")
check("display standalone", manifest.get("display") == "standalone")
check("иконка есть", bool(manifest.get("icons")))
check("есть maskable-иконка",
      any(i.get("purpose") == "maskable" for i in manifest.get("icons", [])))


# ============================================================================
print("\n4. Очередь фото товаров")
# ============================================================================

with cs.get_db() as conn:
    conn.execute(
        "INSERT INTO courier_orders (retailcrm_order_id, order_number, delivery_date, "
        "site_code, city, status) VALUES (?, ?, date('now'), ?, ?, ?)",
        (5001, "5001", "site-a", "Новосибирск", "send-to-florist"),
    )
    for offer_id, name in ((11, "Букет «Восход»"), (12, "Клубника в шоколаде")):
        conn.execute(
            "INSERT INTO order_items (retailcrm_order_id, offer_id, delivery_date, "
            "product_name, quantity) VALUES (?, ?, date('now'), ?, 1)",
            (5001, offer_id, name),
        )

pending = ds.pending_image_offer_ids()
check("новые офферы попадают в очередь", set(pending) == {11, 12}, f"({pending})")

ds.save_product_images({11: "https://example.test/buket.jpg", 12: None})
pending = ds.pending_image_offer_ids()
check("товар с фото из очереди уходит", 11 not in pending, f"({pending})")
check("товар БЕЗ фото тоже уходит из очереди — иначе вечный кандидат",
      12 not in pending, f"({pending})")

card = ds.order_for_courier(5001, city="Новосибирск", courier_user_id=1, with_private=True)
images = {item["offer_id"]: item.get("image_url") for item in card["items"]}
check("карточка отдаёт ссылку на фото",
      images.get(11) == "https://example.test/buket.jpg", f"({images})")
check("товар без фото отдаётся без ссылки, а не пропадает",
      12 in images and images[12] is None, f"({images})")


# ============================================================================
print("\n5. Чек-лист DESIGN-SPEC §9 и правила интерфейса")
# ============================================================================

sources = {}
for name in ("courier-app.html", "courier-app.css", "courier-app.js", "courier-sw.js"):
    with open(os.path.join(DASHBOARD, name), encoding="utf-8") as f:
        sources[name] = f.read()

EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️]")
for name, text in sources.items():
    found = EMOJI.findall(text)
    check(f"{name}: нет эмодзи", not found, f"({found[:5]})")

css = sources["courier-app.css"]
# Палитра спеки плюс белый/чёрный. Свой цвет здесь — это расхождение с Пульсом,
# которое никто не заметит на код-ревью, но увидит человек рядом с дашбордом.
ALLOWED = {
    "#130810", "#1e0d1b", "#411330", "#4a1942", "#6b2660", "#8b3a7d",
    "#b26fa1", "#d19cc2", "#e1a4c9", "#e4c2dd", "#f3e3ee",
    "#faf4f9", "#f5e8f3", "#ffffff", "#fff",
    "#3c3c3c", "#6f6f6f", "#9b8f97", "#eee2ea", "#d1b8ce",
    "#0a7d3f", "#c0322f",
    # Бейджи «успех» и «внимание» — прямо из таблицы §6 спеки
    "#ecfdf5", "#047857", "#a7f3d0", "#fffbeb", "#b45309", "#fde68a",
}
used = {c.lower() for c in re.findall(r"#[0-9a-fA-F]{3,8}", css)}
check("своих цветов в CSS нет", used <= ALLOWED, f"(лишние: {sorted(used - ALLOWED)})")

# Атрибут `hidden` даёт display:none таблицей стилей браузера, с самым низким
# приоритетом: любое своё правило display его перебивает. Так экран загрузки
# (`display: flex`) висел поверх работающего приложения — страница загрузилась,
# лента пришла, а курьер видел «Загрузка…». Правило проверяем, а не глазами:
# слои прячутся атрибутом в четырёх местах, и следующий `display` в CSS
# сломает их молча.
check("hidden перебивает свой display",
      re.search(r"\[hidden\][^{]*\{[^}]*display:\s*none\s*!important", css),
      "(нужно правило [hidden] { display: none !important })")
html = sources["courier-app.html"]
check("светлая тема объявлена — Chrome не перекрасит",
      "color-scheme" in css and 'name="color-scheme"' in html,
      "(без этого Android инвертирует палитру в тёмную)")
check("фон страницы — фирменный", "--bx-bg:           #faf4f9" in css or "#faf4f9" in css)
check("градиентная шапка на месте", "--bx-grad-header" in css and ".cd-header" in css)
check("карточки: радиус 16px и граница спеки",
      "--bx-r-2xl: 16px" in css and "--bx-border" in css)

js = sources["courier-app.js"]
# Внутри iframe Пульса нативные диалоги молча игнорируются: кнопка выглядит
# сломанной, и это уже ловили в других модулях.
for bad in ("alert(", "confirm(", "prompt("):
    check(f"нет нативного {bad[:-1]}()",
          not re.search(r"(?<![.\w])" + re.escape(bad), js), "")
check("диалоги через BarhatUI", "BarhatUI" in js)
check("флаг «не связываться» выводится первым",
      js.index("do_not_contact_recipient") < js.index("Доставка"),
      "(блок с флагом должен идти раньше остальных)")
check("прокрутка возвращается после перерисовки",
      "window.scrollY" in js and "window.scrollTo" in js)
check("экран не выводит действие из статуса CRM",
      "order.status" not in js)
check("обновление по возврату во вкладку", "visibilitychange" in js)


# ============================================================================
print()
if failures:
    print(f"=== ПРОВАЛОВ: {len(failures)} ===")
    for name in failures:
        print(f"  - {name}")
    sys.exit(1)

print("=== Экран курьера в порядке ===")
sys.exit(0)
