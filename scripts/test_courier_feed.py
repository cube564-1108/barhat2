"""
Сторож ленты изменений (Фаза 2 модуля «Курьеры: доставка заказов»).

Четыре вещи, которые ломаются молча:

1. **Эхо собственных записей.** Мы сами меняем статус заказа → изменение
   попадает в историю → лента видит его как новость и снова обновляет заказ.
   Цикл не падает, он просто жжёт запросы и воркер.
2. **Курсор с нуля.** Без стартового значения CRM отдаёт историю с 2021 года
   (41 708 записей за неделю) — первый запуск ленты уходит в никуда.
3. **Расхождение двух путей записи.** Витрину пишут глубокий синк (окно
   целиком) и лента (по заказу). Стоит одному пути забыть поле — карточка
   будет то полной, то пустой, в зависимости от того, кто обновил последним.
4. **Затирание чужих полей.** Вес слота и отметку смены часа готовности
   считает глубокий синк; точечное обновление не имеет права их обнулять.

Сеть не используется — клиент CRM подменяется заглушкой.

Запуск: python scripts/test_courier_feed.py
"""

import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))

TMP_DB = os.path.join(tempfile.mkdtemp(prefix="courier_feed_"), "couriers.db")
os.environ["COURIERS_DB_PATH"] = TMP_DB

from couriers import delivery_feed as feed  # noqa: E402
from couriers import delivery_storage as ds  # noqa: E402
from couriers import retailcrm, storage  # noqa: E402

assert storage.DB_PATH == TMP_DB, f"тест пишет не в свою базу: {storage.DB_PATH}"

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"  [ok] {name}")
    else:
        print(f"  [FAIL] {name} {detail}")
        failures.append(name)


def history(record_id, field, order_id, source="user", current_key=False):
    record = {"id": record_id, "field": field, "source": source,
              "order": {"id": order_id, "status": "send-to-florist"},
              "createdAt": "2026-09-08 10:00:00"}
    if source == "api":
        record["apiKey"] = {"current": current_key, "id": 82}
    return record


def crm_order(order_id, status="send-to-florist", code="dostavka-kurerom"):
    return {
        "id": order_id, "number": str(order_id), "status": status,
        "site": "nsk-voskhod-3", "shipmentStore": "sklad-nsk", "summ": 4200,
        "managerComment": "Позвонить за 20 минут",
        "customer": {"firstName": "Ирина", "phones": [{"number": "+79130000001"}]},
        "delivery": {
            "date": "2026-09-10", "code": code, "netCost": 300,
            "time": {"from": "14:00", "to": "15:00"},
            "address": {"city": "Новосибирск", "text": "ул. Ленина, 45"},
            "data": {"courierId": 101, "firstName": "Курьер 1"},
        },
        "customFields": {"recipient_name": "Евгения", "recipient_phone": "+79130000003",
                         "ne_sviazyvatsia_s_poluchatelem": True,
                         "order_availability_time": "13:00",
                         "data_i_vremia_gotovnosti": "2026-09-10 13:00:00"},
        "items": [{"quantity": 1, "offer": {"id": 9001, "displayName": "Букет", "article": "A-1"}}],
    }


class FakeClient:
    """Клиент CRM без сети: отдаёт заранее заданные страницы истории."""

    def __init__(self, pages, latest_id=5000):
        self.pages = pages
        self.latest_id = latest_id
        self.requested_ids = []
        self.history_calls = 0

    def latest_history_id(self, minutes_back=30):
        return self.latest_id

    def iter_history_since(self, since_id, max_pages=20):
        for page in self.pages:
            fresh = [r for r in page if (r.get("id") or 0) > since_id]
            if not fresh:
                continue
            self.history_calls += 1
            yield fresh
            since_id = max(r["id"] for r in fresh)

    def get_orders_by_ids(self, order_ids):
        self.requested_ids.append(list(order_ids))
        return [crm_order(order_id) for order_id in order_ids]


storage.init_couriers_tables()
ds.init_delivery_tables()
storage.upsert_sites([{"code": "nsk-voskhod-3", "name": "НСК Восход 3", "city": "Новосибирск"}])


print("\n1. Эхо собственных записей отбрасывается")

check("наша запись (apiKey.current) — эхо",
      feed.is_own_echo(history(1, "status", 100, source="api", current_key=True)))
check("чужая интеграция — не эхо",
      not feed.is_own_echo(history(2, "status", 100, source="api", current_key=False)))
check("правка человеком — не эхо",
      not feed.is_own_echo(history(3, "status", 100, source="user")))
check("правка триггером CRM — не эхо",
      not feed.is_own_echo(history(4, "status", 100, source="rule")))

records = [
    history(10, "status", 100),
    history(11, "status", 101, source="api", current_key=True),   # наше эхо
    history(12, "custom_kolichestvo_tovarov", 102),               # не наше поле
    history(13, "delivery_type", 103),                            # аутсорс
    history(14, "status", 100),                                   # дубль заказа
]
ids = feed.collect_order_ids(records)
check("собраны только нужные заказы, без эха и шума", ids == [100, 103], ids)
check("дубликаты заказа схлопнуты", ids.count(100) == 1)


print("\n2. Курсор: первый запуск не тянет историю с 2021 года")

feed.set_cursor(0)
client = FakeClient(pages=[], latest_id=777)
check("курсор инициализируется текущим максимумом",
      feed.ensure_cursor(client) == 777)
check("значение сохранено", feed.get_cursor() == 777)
check("повторный вызов не ходит в CRM заново", feed.ensure_cursor(client) == 777)

feed.set_cursor(0)
storage.set_sync_state(feed.CURSOR_KEY, "мусор")
check("испорченный курсор не роняет ленту", feed.get_cursor() == 0)


print("\n3. Тик ленты обновляет витрину и двигает курсор")

feed.set_cursor(500)
client = FakeClient(pages=[[history(501, "status", 100), history(502, "delivery_type", 103)]])
retailcrm._client = client   # подменяем клиента: сети в тесте нет

stats = feed.run_once()
check("обновлены оба заказа", stats["orders"] == 2, stats)
check("курсор сдвинут на последнюю запись", feed.get_cursor() == 502, feed.get_cursor())
check("карточки дозапрошены одной пачкой", client.requested_ids == [[100, 103]],
      client.requested_ids)

with storage.get_db() as conn:
    row = dict(conn.execute(
        "SELECT * FROM courier_orders WHERE retailcrm_order_id = 100").fetchone())
    items_count = conn.execute(
        "SELECT COUNT(*) FROM order_items WHERE retailcrm_order_id = 100").fetchone()[0]
check("адрес доехал до витрины", row["address_text"] == "ул. Ленина, 45")
check("телефон получателя доехал", row["recipient_phone"] == "+79130000003")
check("флаг «не связываться» доехал", row["do_not_contact_recipient"] == 1)
check("позиции записаны", items_count == 1, items_count)


print("\n4. Заказ из ленты сразу получает трудоёмкость, а не ждёт синка")

# Портим значение так, будто заказ пришёл лентой и веса у него нет
with storage.get_db() as conn:
    conn.execute("UPDATE courier_orders SET weight_units = NULL, slot_changed_at = "
                 "'2026-09-08 09:00:00' WHERE retailcrm_order_id = 100")

feed.set_cursor(600)
client = FakeClient(pages=[[history(601, "status", 100)]])
retailcrm._client = client
stats = feed.run_once()

with storage.get_db() as conn:
    row = dict(conn.execute(
        "SELECT * FROM courier_orders WHERE retailcrm_order_id = 100").fetchone())
check("вес заказа пересчитан лентой, а не остался пустым",
      row["weight_units"] is not None and row["weight_units"] > 0, row["weight_units"])
check("пересчёт отмечен в сводке тика", stats.get("recalc_dates", 0) >= 1, stats)
check("отметка смены слота не затёрта точечным обновлением",
      row["slot_changed_at"] == "2026-09-08 09:00:00", row["slot_changed_at"])


print("\n5. Два пути записи витрины не разъехались")

parsed = retailcrm.parse_order(crm_order(200), storage.get_site_cities())
storage.replace_orders_window("2026-09-10", "2026-09-10", [parsed])
with storage.get_db() as conn:
    by_sync = dict(conn.execute(
        "SELECT * FROM courier_orders WHERE retailcrm_order_id = 200").fetchone())

with storage.get_db() as conn:
    conn.execute("DELETE FROM courier_orders WHERE retailcrm_order_id = 200")
# Именно apply_..., а не upsert_...: это единственная точка входа для точечной
# записи, и сравнивать надо полный путь, иначе тест «сойдётся» на неполном.
ds.apply_orders_from_crm([parsed])
with storage.get_db() as conn:
    by_feed = dict(conn.execute(
        "SELECT * FROM courier_orders WHERE retailcrm_order_id = 200").fetchone())

ignored = {"synced_at", "slot_changed_at"}
diff = {k: (by_sync.get(k), by_feed.get(k))
        for k in set(by_sync) | set(by_feed)
        if k not in ignored and by_sync.get(k) != by_feed.get(k)}
check("глубокий синк и лента пишут одно и то же", not diff, diff)


print("\n6. Пустая история — тик ничего не делает и не двигает курсор")

feed.set_cursor(900)
retailcrm._client = FakeClient(pages=[])
stats = feed.run_once()
check("нет записей — нет работы", stats["records"] == 0 and stats["orders"] == 0)
check("курсор на месте", feed.get_cursor() == 900)


print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} — {failures}")
    sys.exit(1)
print("Все проверки пройдены")
