"""
Сторож Фазы 1 модуля «Курьеры: доставка заказов».

Проверяет фундамент, на котором стоит всё остальное:
  - пояс салона заводится, сидируется по городу и не перетирается синком;
  - профиль курьера связывается с курьером CRM, дубль связки не проходит;
  - настройки города подставляют умолчания и отбивают бессмысленные значения;
  - роль «курьер» заведена во всех местах, где её забывают (auth.py, users.js,
    index.html) — по чек-листу, стоившему трёх багов на проде.

Работает на ВРЕМЕННОЙ базе, боевую не трогает и в сеть не ходит.

Запуск: python scripts/test_courier_delivery_setup.py
"""

import io
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))

# База — временная, до импорта модулей: путь читается при импорте.
TMP_DB = os.path.join(tempfile.gettempdir(), "_test_courier_delivery.db")
for suffix in ("", "-wal", "-shm"):
    if os.path.exists(TMP_DB + suffix):
        os.remove(TMP_DB + suffix)
os.environ["COURIERS_DB_PATH"] = TMP_DB

from couriers import delivery_storage as ds  # noqa: E402
from couriers import storage  # noqa: E402

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"  [ok] {name}")
    else:
        print(f"  [FAIL] {name} {detail}")
        failures.append(name)


print(f"\nбаза: {storage.DB_PATH}")
storage.init_couriers_tables()
ds.init_delivery_tables()

print("\n1. Пояс салона: сид по городу и ручная правка")

storage.upsert_sites([
    {"code": "nsk-voskhod-3", "name": "НСК Восход 3", "city": "Новосибирск"},
    {"code": "barkhat-ekb", "name": "ЕКБ Ленина", "city": "Екатеринбург"},
    {"code": "zakazy-new", "name": "Заказы сайтов", "city": None},
])
# Сид проставляется в init (для салонов, появившихся раньше) — прогоняем ещё раз
storage.init_couriers_tables()

offsets = storage.get_site_offsets()
check("Новосибирск получил UTC+7", offsets.get("nsk-voskhod-3") == 7,
      f"получено {offsets.get('nsk-voskhod-3')}")
check("Екатеринбург получил UTC+5", offsets.get("barkhat-ekb") == 5,
      f"получено {offsets.get('barkhat-ekb')}")
check("салон без города остался без пояса (а не «наверное, Москва»)",
      offsets.get("zakazy-new") is None)

check("список «пояс не задан» показывает только проблемные салоны",
      [s["code"] for s in storage.list_sites_timezones(only_missing=True)] == ["zakazy-new"])

storage.set_site_timezone("zakazy-new", 3)
check("пояс задан руками", storage.get_site_offsets().get("zakazy-new") == 3)

# Синк салонов не должен трогать выставленный руками пояс
storage.upsert_sites([{"code": "zakazy-new", "name": "Заказы сайтов", "city": None}])
check("синк справочника НЕ перетирает пояс, заданный человеком",
      storage.get_site_offsets().get("zakazy-new") == 3)

storage.set_site_timezone("zakazy-new", None)
check("пояс можно снять обратно в «неизвестно»",
      storage.get_site_offsets().get("zakazy-new") is None)

try:
    storage.set_site_timezone("nsk-voskhod-3", 77)
    check("опечатка «77» отбивается", False, "исключения не было")
except ValueError:
    check("опечатка «77» отбивается", True)

check("несуществующий салон — False, а не тихий успех",
      storage.set_site_timezone("нет-такого", 5) is False)


print("\n2. Профиль курьера и связка с CRM (от неё зависят выплаты)")

ds.save_courier_profile(1, "kurier-nsk-1", "Новосибирск", 101)
ds.save_courier_profile(2, "kurier-nsk-2", "Новосибирск", None)
ds.save_courier_profile(3, "kurier-ekb-1", "Екатеринбург", 102)

check("профиль сохранён", ds.get_courier_profile(1)["retailcrm_courier_id"] == 101)
check("фильтр по городу", len(ds.list_courier_profiles(city="Новосибирск")) == 2)
check("без связки с CRM виден отдельным списком",
      [p["username"] for p in ds.profiles_without_crm_link()] == ["kurier-nsk-2"])

try:
    ds.save_courier_profile(4, "kurier-nsk-3", "Новосибирск", 101)
    check("один курьер CRM — одна учётка", False, "дубль прошёл")
except ValueError as e:
    check("один курьер CRM — одна учётка", "уже привязан" in str(e), str(e))

check("нескольким профилям можно оставить связку пустой",
      ds.save_courier_profile(5, "kurier-tomsk", "Томск", None) is None)

ds.save_courier_profile(1, "kurier-nsk-1", "Новосибирск", 101, active=False)
check("курьера можно деактивировать", ds.get_courier_profile(1)["active"] == 0)
check("неактивный не попадает в список без связки",
      "kurier-nsk-1" not in [p["username"] for p in ds.profiles_without_crm_link()])


print("\n3. Настройки города: умолчания и защита от бессмысленных значений")

defaults = ds.city_settings("Новосибирск")
check("город без своей строки берёт умолчания",
      defaults["max_active_claims"] == ds.DEFAULT_MAX_ACTIVE_CLAIMS
      and defaults["unclaimed_alert_minutes"] == ds.DEFAULT_UNCLAIMED_ALERT_MINUTES)
check("видно, что настройки не заводили", defaults["has_own_settings"] is False)

ds.set_city_settings("Новосибирск", {"max_active_claims": 5,
                                     "unclaimed_alert_minutes": 120}, "admin")
nsk = ds.city_settings("Новосибирск")
check("заданные значения применились",
      nsk["max_active_claims"] == 5 and nsk["unclaimed_alert_minutes"] == 120)
check("незаданное поле осталось умолчанием",
      nsk["claim_horizon_days"] == ds.DEFAULT_CLAIM_HORIZON_DAYS)
check("порог «никто не взял» у другого города свой",
      ds.city_settings("Екатеринбург")["unclaimed_alert_minutes"]
      == ds.DEFAULT_UNCLAIMED_ALERT_MINUTES)

try:
    ds.set_city_settings("Томск", {"max_active_claims": 0})
    check("ноль броней не проходит (это выключенный модуль)", False, "прошло")
except ValueError:
    check("ноль броней не проходит (это выключенный модуль)", True)

try:
    ds.set_city_settings("Томск", {"quiet_hours_from": "22"})
    check("тихие часы проверяются на формат", False, "прошло")
except ValueError:
    check("тихие часы проверяются на формат", True)

ds.set_city_settings("Новосибирск", {"max_active_claims": None}, "admin")
check("сброс поля возвращает умолчание",
      ds.city_settings("Новосибирск")["max_active_claims"] == ds.DEFAULT_MAX_ACTIVE_CLAIMS)


print("\n4. Роль «курьер» заведена везде (чек-лист нового модуля)")

sys.path.insert(0, os.path.join(REPO, "src"))
os.environ.setdefault("BARHAT_DB_PATH", os.path.join(tempfile.gettempdir(),
                                                     "_test_courier_auth.db"))
from auth import ALL_MODULES, ROLE_SECTIONS  # noqa: E402

check("роль courier есть в ROLE_SECTIONS", "courier" in ROLE_SECTIONS)
check("курьеру доступно только своё приложение",
      ROLE_SECTIONS.get("courier") == {"courier_app"})
check("курьеру НЕ доступен раздел контроля",
      "courier_dispatch" not in ROLE_SECTIONS.get("courier", set()))
check("админ и управляющий видят раздел контроля",
      "courier_dispatch" in ROLE_SECTIONS["admin"]
      and "courier_dispatch" in ROLE_SECTIONS["manager"])
check("обе секции в ALL_MODULES (иначе права не выдать через UI)",
      "courier_app" in ALL_MODULES and "courier_dispatch" in ALL_MODULES)
check("заодно починены salon_kpi/salon_load, которых там не было",
      "salon_kpi" in ALL_MODULES and "salon_load" in ALL_MODULES)

users_js = io.open(os.path.join(REPO, "src/dashboard/users.js"), encoding="utf-8").read()
check("users.js: название модуля для чекбокса", "'courier_app':" in users_js)
check("users.js: пресет роли", "'courier': ['courier_app']" in users_js)
check("users.js: подпись роли в карточке", "'courier': 'Курьер'" in users_js)

index_html = io.open(os.path.join(REPO, "src/dashboard/index.html"), encoding="utf-8").read()
check("index.html: роль в выпадающем списке",
      '<option value="courier">' in index_html)


print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} — {failures}")
    sys.exit(1)
print("Все проверки пройдены")
