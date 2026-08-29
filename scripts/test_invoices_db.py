"""
Тестовый скрипт для проверки миграций и CRUD модуля счетов на оплату.

Пишет в throwaway SQLite-файл (не в прод БД). Проверяет:
1. Инициализацию таблиц и справочников на чистой базе.
2. Основной сценарий: создание счёта (с распределением) -> согласование /
   отклонение -> оплата -> авто-архивация.
3. Миграцию старой схемы invoices (один store_id/expense_category_id NOT
   NULL на счёт) на новую (invoice_line_items, много строк на счёт).
"""

import os
import sqlite3
import sys
import io

# UTF-8 для Windows
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# Добавляем src в путь
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), '_test_invoices.db')
TEST_ATTACHMENTS_DIR = os.path.join(os.path.dirname(__file__), '_test_invoice_attachments')
if os.path.exists(TEST_DB_PATH):
    os.remove(TEST_DB_PATH)
os.environ['BARHAT_DB_PATH'] = TEST_DB_PATH
os.environ['INVOICE_ATTACHMENTS_DIR'] = TEST_ATTACHMENTS_DIR

from cashshifts.storage import init_cashshifts_tables, get_all_stores, set_user_stores
from invoices.storage import (
    init_invoices_tables,
    get_all_expense_categories,
    create_city,
    get_all_cities,
    create_payer,
    get_all_payers,
    create_invoice,
    get_invoice_by_id,
    get_invoice_by_number,
    get_invoice_by_match_code,
    list_invoices,
    approve_invoice,
    reject_invoice,
    mark_invoice_paid,
    get_invoice_line_items,
    set_invoice_line_items,
    is_invoice_fully_allocated,
    user_can_access_invoice,
    set_invoice_archived,
    can_edit_invoice_fields,
    can_edit_invoice_status,
    update_invoice,
    update_invoice_status,
    get_invoice_history,
    add_invoice_comment,
    get_invoice_comments,
)
from invoices.seed_data import EXPENSE_CATEGORIES


def test_invoices():
    print("=== Тест миграций и CRUD Invoices ===\n")

    print("1. Инициализация таблиц...")
    init_cashshifts_tables()  # invoices зависит от stores из cashshifts
    init_invoices_tables()
    print("   ✓ Таблицы созданы\n")

    print("2. Проверка статей расхода:")
    categories = get_all_expense_categories()
    if len(categories) != len(EXPENSE_CATEGORIES):
        print(f"   ✗ Ошибка: ожидалось {len(EXPENSE_CATEGORIES)}, получено {len(categories)}")
        return False
    print(f"   ✓ Ожидаемое количество: {len(EXPENSE_CATEGORIES)}\n")

    stores = get_all_stores()
    if not stores:
        print("   ✗ Ошибка: нет точек продаж (модуль cashshifts должен быть проинициализирован первым)")
        return False

    store_id = stores[0]["id"]
    store_id_2 = stores[1]["id"] if len(stores) > 1 else store_id
    category_id = categories[0]["id"]
    category_id_2 = categories[1]["id"] if len(categories) > 1 else category_id

    print("3. Справочники города/плательщика (создаём как это сделал бы админ):")
    city_id = create_city("Томск")
    payer_id = create_payer("ИП Тестовый")
    if not get_all_cities() or not get_all_payers():
        print("   ✗ Ошибка: город/плательщик не создались")
        return False
    print("   ✓ Город и плательщик созданы\n")

    print("4. Создание счёта с распределением на 2 строки (проект+статья):")
    invoice = create_invoice(
        amount=15000.50,
        payment_purpose="Оплата поставщику за цветы",
        created_by="test_user",
        city_id=city_id,
        payer_id=payer_id,
        due_date="2026-08-20",
        counterparty_name="ООО Тестовый поставщик",
        line_items=[
            {"store_id": store_id, "expense_category_id": category_id, "amount": 10000.50},
            {"store_id": store_id_2, "expense_category_id": category_id_2, "amount": 5000},
        ],
    )
    print(f"   Создан счёт {invoice['invoice_number']} / {invoice['match_code']} на сумму {invoice['amount']}")

    if invoice["status"] != "on_approval":
        print(f"   ✗ Ошибка: ожидался статус on_approval, получен {invoice['status']}")
        return False
    if invoice["invoice_number"] != f"СЧ-{invoice['id']:06d}":
        print(f"   ✗ Ошибка: неверный номер счёта {invoice['invoice_number']}")
        return False
    if invoice["match_code"] != f"REF-{invoice['id']:06d}":
        print(f"   ✗ Ошибка: неверный match_code {invoice['match_code']}")
        return False

    line_items = get_invoice_line_items(invoice["id"])
    if len(line_items) != 2:
        print(f"   ✗ Ошибка: ожидалось 2 строки распределения, получено {len(line_items)}")
        return False
    print("   ✓ Счёт создан с корректными номером, match_code и распределением\n")

    print("5. Проверка поиска:")
    if not get_invoice_by_id(invoice["id"]) or not get_invoice_by_number(invoice["invoice_number"]) \
            or not get_invoice_by_match_code(invoice["match_code"]):
        print("   ✗ Ошибка поиска счёта")
        return False
    print("   ✓ Счёт находится по ID, номеру и match_code\n")

    print("6. Согласование счёта:")
    if not approve_invoice(invoice["id"], "admin_user"):
        print("   ✗ Ошибка: согласование не удалось")
        return False
    if approve_invoice(invoice["id"], "admin_user"):
        print("   ✗ Ошибка: повторное согласование не должно проходить")
        return False
    print("   ✓ Счёт согласован, повторное согласование корректно отклонено\n")

    print("7. Валидация суммы строк распределения при правке:")
    bad_result = set_invoice_line_items(invoice["id"], [{"store_id": store_id, "expense_category_id": category_id, "amount": 1}])
    if bad_result["ok"]:
        print("   ✗ Ошибка: распределение с неверной суммой не должно приниматься")
        return False
    print("   ✓ Несовпадение суммы строк с суммой счёта корректно отклонено\n")

    # Автоархивация отменена владельцем 2026-08-25: оплаченный счёт остаётся
    # в списке, в архив его кладёт человек (кнопкой или массовым действием).
    print("8. Оплата — счёт остаётся в списке, сам в архив не уходит:")
    if not mark_invoice_paid(invoice["id"]):
        print("   ✗ Ошибка: пометить оплаченным не удалось")
        return False
    paid = get_invoice_by_id(invoice["id"])
    if paid["status"] != "paid":
        print(f"   ✗ Ошибка: статус после оплаты {paid['status']}")
        return False
    if not is_invoice_fully_allocated(invoice["id"]):
        print("   ✗ Ошибка: счёт должен считаться полностью распределённым")
        return False
    if paid["is_archived"]:
        print("   ✗ Ошибка: оплаченный счёт не должен уезжать в архив сам (автоархивация отменена)")
        return False
    print("   ✓ Счёт оплачен и остался в списке\n")

    print("9. Ручной возврат из архива и обратно:")
    set_invoice_archived(invoice["id"], False)
    if get_invoice_by_id(invoice["id"])["is_archived"]:
        print("   ✗ Ошибка: ручной возврат из архива не сработал")
        return False
    set_invoice_archived(invoice["id"], True)
    print("   ✓ Ручное управление архивом работает\n")

    # Отклонённый счёт тоже остаётся на виду (решение владельца 2026-08-26):
    # иначе автор узнаёт об отказе, только если включит фильтр архива.
    print("10. Отклонение отдельного счёта (без распределения) -> остаётся в списке:")
    invoice2 = create_invoice(
        amount=5000,
        payment_purpose="Дублирующий счёт на отклонение",
        created_by="test_user",
        city_id=city_id,
        payer_id=payer_id,
        due_date="2026-08-21",
    )
    if not reject_invoice(invoice2["id"], "admin_user", "Дублирующий счёт"):
        print("   ✗ Ошибка: отклонение не удалось")
        return False
    rejected = get_invoice_by_id(invoice2["id"])
    if rejected["status"] != "rejected":
        print(f"   ✗ Ошибка: статус после отклонения {rejected['status']}")
        return False
    if rejected["is_archived"]:
        print("   ✗ Ошибка: отклонённый счёт не должен уезжать в архив сам — отказ должен остаться на виду")
        return False
    print("   ✓ Счёт отклонён и остался в списке\n")

    print("11. Фильтры списка счетов:")
    if not any(i["id"] == invoice2["id"] for i in list_invoices(status="rejected", is_archived=False)):
        print("   ✗ Ошибка: отклонённый счёт не найден в основном списке с фильтром по статусу")
        return False
    # Убираем в архив руками — так теперь и работает интерфейс — и проверяем,
    # что архивный счёт из основного списка пропадает
    set_invoice_archived(invoice2["id"], True)
    if any(i["id"] == invoice2["id"] for i in list_invoices(is_archived=False)):
        print("   ✗ Ошибка: архивный счёт не должен попадать в основной список")
        return False
    if not any(i["id"] == invoice2["id"] for i in list_invoices(status="rejected", is_archived=True)):
        print("   ✗ Ошибка: убранный в архив счёт не найден в архиве")
        return False
    if not any(i["id"] == invoice["id"] for i in list_invoices(store_id=store_id, is_archived=True)):
        print("   ✗ Ошибка: фильтр по салону (через invoice_line_items) не нашёл счёт")
        return False
    if not any(i["id"] == invoice["id"] for i in list_invoices(counterparty="Тестовый поставщик", is_archived=True)):
        print("   ✗ Ошибка: фильтр по контрагенту не сработал")
        return False
    print("   ✓ Фильтры по статусу/архиву/салону/контрагенту работают\n")

    print("12. Ограничение видимости по доступу к салонам (не-админ):")
    invoice_store1 = create_invoice(
        amount=2000, payment_purpose="Счёт салона 1", created_by="manager_a",
        city_id=city_id, payer_id=payer_id, due_date="2026-08-22",
        line_items=[{"store_id": store_id, "expense_category_id": category_id, "amount": 2000}],
    )
    invoice_store2 = create_invoice(
        amount=3000, payment_purpose="Счёт салона 2", created_by="manager_b",
        city_id=city_id, payer_id=payer_id, due_date="2026-08-22",
        line_items=[{"store_id": store_id_2, "expense_category_id": category_id, "amount": 3000}],
    )
    invoice_no_dist = create_invoice(
        amount=1000, payment_purpose="Свой счёт без распределения ещё", created_by="manager_a",
        city_id=city_id, payer_id=payer_id, due_date="2026-08-22",
    )
    set_user_stores("manager_a", [store_id])  # доступ только к своему салону

    if not user_can_access_invoice(invoice_store1, "manager_a", "manager"):
        print("   ✗ Ошибка: manager_a должен видеть счёт своего салона")
        return False
    if user_can_access_invoice(invoice_store2, "manager_a", "manager"):
        print("   ✗ Ошибка: manager_a НЕ должен видеть счёт чужого салона")
        return False
    if not user_can_access_invoice(invoice_no_dist, "manager_a", "manager"):
        print("   ✗ Ошибка: manager_a должен видеть свой же счёт, даже без распределения")
        return False
    if not user_can_access_invoice(invoice_store2, "manager_b", "manager"):
        print("   ✗ Ошибка: manager_b должен видеть свой счёт, даже без доступа к салону в user_stores")
        return False
    if not user_can_access_invoice(invoice_store2, "admin_user", "admin"):
        print("   ✗ Ошибка: admin должен видеть любой счёт")
        return False
    print("   ✓ user_can_access_invoice верно разграничивает доступ\n")

    visible_to_a = list_invoices(is_archived=False, restrict_username="manager_a", restrict_store_ids=[store_id])
    visible_ids = {i["id"] for i in visible_to_a}
    if invoice_store1["id"] not in visible_ids or invoice_no_dist["id"] not in visible_ids:
        print("   ✗ Ошибка: в списке manager_a не хватает его собственных/своего-салона счетов")
        return False
    if invoice_store2["id"] in visible_ids:
        print("   ✗ Ошибка: manager_a не должен видеть в списке счёт чужого салона")
        return False

    visible_no_stores = list_invoices(is_archived=False, restrict_username="manager_a", restrict_store_ids=[])
    visible_no_stores_ids = {i["id"] for i in visible_no_stores}
    if invoice_store2["id"] in visible_no_stores_ids:
        print("   ✗ Ошибка: без единого доступного салона (пустой user_stores) не должен быть виден чужой счёт")
        return False
    if invoice_store1["id"] not in visible_no_stores_ids or invoice_no_dist["id"] not in visible_no_stores_ids:
        print("   ✗ Ошибка: свои же счета (created_by) должны быть видны даже без единого доступного салона")
        return False
    print("   ✓ Фильтр списка по доступным салонам работает\n")

    print("=== Все тесты пройдены успешно! ===")
    return True


def test_migration_from_old_schema():
    print("\n=== Тест миграции старой схемы invoices ===\n")

    migration_db_path = os.path.join(os.path.dirname(__file__), '_test_invoices_migration.db')
    if os.path.exists(migration_db_path):
        os.remove(migration_db_path)
    os.environ['BARHAT_DB_PATH'] = migration_db_path

    import importlib
    import cashshifts.storage as cashshifts_storage
    import invoices.storage as invoices_storage
    importlib.reload(cashshifts_storage)
    importlib.reload(invoices_storage)

    cashshifts_storage.init_cashshifts_tables()
    stores = cashshifts_storage.get_all_stores()
    store_id = stores[0]["id"]

    conn = sqlite3.connect(migration_db_path)
    conn.execute("""
        CREATE TABLE invoice_expense_categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            is_active INTEGER NOT NULL DEFAULT 1,
            planfact_category_id TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("INSERT INTO invoice_expense_categories (name) VALUES ('Старая статья')")
    category_id = conn.execute("SELECT id FROM invoice_expense_categories").fetchone()[0]

    conn.execute("""
        CREATE TABLE invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_number TEXT UNIQUE,
            store_id INTEGER NOT NULL,
            expense_category_id INTEGER NOT NULL,
            counterparty_name TEXT NOT NULL,
            counterparty_inn TEXT,
            counterparty_bank_name TEXT,
            counterparty_bank_bik TEXT,
            counterparty_bank_account TEXT,
            counterparty_bank_corr_account TEXT,
            amount REAL NOT NULL CHECK (amount > 0),
            description TEXT,
            payment_purpose TEXT,
            due_date TEXT,
            status TEXT NOT NULL DEFAULT 'on_approval',
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            approved_by TEXT,
            approved_at TEXT,
            rejected_by TEXT,
            rejected_reason TEXT,
            paid_at TEXT
        )
    """)
    conn.execute(
        """
        INSERT INTO invoices (
            invoice_number, store_id, expense_category_id, counterparty_name,
            amount, payment_purpose, status, created_by
        ) VALUES ('СЧ-000001', ?, ?, 'Старый контрагент', 7000, 'Оплата по счёту СЧ-000001', 'approved', 'old_user')
        """,
        (store_id, category_id)
    )
    conn.commit()
    conn.close()

    print("1. Старая схема invoices создана вручную, запускаем init_invoices_tables()...")
    invoices_storage.init_invoices_tables()
    print("   ✓ Миграция выполнена без ошибок\n")

    print("2. Проверка перенесённых данных:")
    migrated = invoices_storage.get_invoice_by_id(1)
    if not migrated:
        print("   ✗ Ошибка: счёт не найден после миграции")
        return False
    if migrated["match_code"] != "REF-000001":
        print(f"   ✗ Ошибка: match_code не проставлен при миграции, получено {migrated['match_code']}")
        return False
    if migrated["counterparty_name"] != "Старый контрагент":
        print("   ✗ Ошибка: данные счёта потеряны при миграции")
        return False

    line_items = invoices_storage.get_invoice_line_items(1)
    if len(line_items) != 1 or line_items[0]["store_id"] != store_id or line_items[0]["amount"] != 7000:
        print(f"   ✗ Ошибка: распределение не перенесено корректно: {line_items}")
        return False
    print("   ✓ Счёт и распределение перенесены корректно, match_code проставлен\n")

    print("=== Миграция прошла успешно! ===")
    return True


def test_concurrent_migration():
    """
    На проде gunicorn поднимает 2 воркера (amvera.yml, --workers 2), которые
    независимо вызывают init_invoices_tables() при старте на один и тот же
    файл SQLite. Эмулируем это двумя потоками с отдельными соединениями,
    стартующими одновременно на одной старой схеме — миграция не должна
    падать и не должна терять/дублировать данные.
    """
    print("\n=== Тест гонки двух воркеров при миграции ===\n")

    import threading

    concurrent_db_path = os.path.join(os.path.dirname(__file__), '_test_invoices_concurrent.db')
    if os.path.exists(concurrent_db_path):
        os.remove(concurrent_db_path)
    os.environ['BARHAT_DB_PATH'] = concurrent_db_path

    import importlib
    import cashshifts.storage as cashshifts_storage
    import invoices.storage as invoices_storage
    importlib.reload(cashshifts_storage)
    importlib.reload(invoices_storage)

    cashshifts_storage.init_cashshifts_tables()
    stores = cashshifts_storage.get_all_stores()
    store_id = stores[0]["id"]

    conn = sqlite3.connect(concurrent_db_path)
    conn.execute("""
        CREATE TABLE invoice_expense_categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            is_active INTEGER NOT NULL DEFAULT 1,
            planfact_category_id TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("INSERT INTO invoice_expense_categories (name) VALUES ('Старая статья')")
    category_id = conn.execute("SELECT id FROM invoice_expense_categories").fetchone()[0]

    conn.execute("""
        CREATE TABLE invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_number TEXT UNIQUE,
            store_id INTEGER NOT NULL,
            expense_category_id INTEGER NOT NULL,
            counterparty_name TEXT NOT NULL,
            counterparty_inn TEXT,
            counterparty_bank_name TEXT,
            counterparty_bank_bik TEXT,
            counterparty_bank_account TEXT,
            counterparty_bank_corr_account TEXT,
            amount REAL NOT NULL CHECK (amount > 0),
            description TEXT,
            payment_purpose TEXT,
            due_date TEXT,
            status TEXT NOT NULL DEFAULT 'on_approval',
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            approved_by TEXT,
            approved_at TEXT,
            rejected_by TEXT,
            rejected_reason TEXT,
            paid_at TEXT
        )
    """)
    for i in range(5):
        conn.execute(
            """
            INSERT INTO invoices (
                invoice_number, store_id, expense_category_id, counterparty_name,
                amount, payment_purpose, status, created_by
            ) VALUES (?, ?, ?, ?, ?, ?, 'approved', 'old_user')
            """,
            (f"СЧ-{i+1:06d}", store_id, category_id, f"Контрагент {i+1}", 1000 + i, f"Оплата по счёту СЧ-{i+1:06d}")
        )
    conn.commit()
    conn.close()

    print("1. Старая схема с 5 счетами создана, запускаем init_invoices_tables() из 2 потоков одновременно...")

    errors = []
    barrier = threading.Barrier(2)

    def worker():
        try:
            barrier.wait()  # оба потока стартуют инициализацию максимально одновременно
            invoices_storage.init_invoices_tables()
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    if errors:
        print(f"   ✗ Ошибка: миграция упала при параллельном запуске: {errors}")
        return False
    print("   ✓ Оба воркера отработали без исключений\n")

    print("2. Проверка целостности данных после гонки:")
    conn = sqlite3.connect(concurrent_db_path)
    conn.row_factory = sqlite3.Row

    leftover = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='invoices_old_v1'"
    ).fetchone()
    if leftover:
        print("   ✗ Ошибка: осталась незавершённая invoices_old_v1")
        conn.close()
        return False

    all_invoices = conn.execute("SELECT * FROM invoices ORDER BY id").fetchall()
    if len(all_invoices) != 5:
        print(f"   ✗ Ошибка: ожидалось 5 счетов, получено {len(all_invoices)}")
        conn.close()
        return False

    for row in all_invoices:
        line_items = conn.execute(
            "SELECT * FROM invoice_line_items WHERE invoice_id = ?", (row["id"],)
        ).fetchall()
        if len(line_items) != 1:
            print(f"   ✗ Ошибка: у счёта {row['id']} {len(line_items)} строк распределения (ожидалась 1, дубликаты из-за гонки?)")
            conn.close()
            return False

    conn.close()
    print("   ✓ Все 5 счетов на месте, распределение без дублей, старая таблица убрана\n")

    print("=== Гонка двух воркеров обработана корректно! ===")
    return True


def test_editing_history_comments():
    """Редактирование полей/статуса с учётом окна прав, история изменений, сообщения."""
    print("\n=== Тест редактирования, истории и сообщений ===\n")

    os.environ['BARHAT_DB_PATH'] = TEST_DB_PATH
    import importlib
    import cashshifts.storage as cashshifts_storage
    import invoices.storage as s
    importlib.reload(cashshifts_storage)
    importlib.reload(s)

    cashshifts_storage.init_cashshifts_tables()
    s.init_invoices_tables()

    stores = cashshifts_storage.get_all_stores()
    city_id = s.create_city("Новосибирск")
    payer_id = s.create_payer("ООО Плательщик")

    print("1. Создание счёта автором creator_x:")
    invoice = s.create_invoice(
        amount=1000, payment_purpose="Черновое назначение", created_by="creator_x",
        city_id=city_id, payer_id=payer_id, due_date="2026-09-01",
    )
    inv_id = invoice["id"]

    if not s.can_edit_invoice_fields(invoice, "creator_x", "manager"):
        print("   ✗ Ошибка: автор должен редактировать свой счёт до согласования")
        return False
    # С 2026-08-28 (коммит 16f8988) менеджер правит и чужой счёт, пока тот на
    # согласовании: разбирать очередь вдвоём иначе не получалось. Проверяем
    # действующее правило — роль без права правки здесь флорист.
    if not s.can_edit_invoice_fields(invoice, "other_user", "manager"):
        print("   ✗ Ошибка: менеджер должен править чужой счёт до согласования")
        return False
    if s.can_edit_invoice_fields(invoice, "other_user", "florist"):
        print("   ✗ Ошибка: флорист не должен редактировать чужой счёт")
        return False
    if not s.can_edit_invoice_fields(invoice, "anyone", "admin"):
        print("   ✗ Ошибка: админ должен редактировать любой счёт до согласования")
        return False
    print("   ✓ Права на редактирование на этапе on_approval верны\n")

    print("2. Правка полей автором + проверка истории:")
    updated = s.update_invoice(inv_id, {"amount": 1500, "payment_purpose": "Итоговое назначение"}, "creator_x")
    if updated["amount"] != 1500 or updated["payment_purpose"] != "Итоговое назначение":
        print("   ✗ Ошибка: поля не обновились")
        return False
    history = s.get_invoice_history(inv_id)
    fields_changed = {h["field_name"] for h in history}
    if "amount" not in fields_changed or "payment_purpose" not in fields_changed or "счёт создан" not in fields_changed:
        print(f"   ✗ Ошибка: в истории не хватает записей, получено: {fields_changed}")
        return False
    print(f"   ✓ Поля обновлены, в истории {len(history)} записей\n")

    print("3. Согласование -> редактирование полей закрыто для не-админа:")
    s.approve_invoice(inv_id, "admin_user")
    invoice = s.get_invoice_by_id(inv_id)
    if s.can_edit_invoice_fields(invoice, "creator_x", "manager"):
        print("   ✗ Ошибка: после согласования автор-не-админ не должен редактировать поля")
        return False
    if not s.can_edit_invoice_fields(invoice, "admin_user", "admin"):
        print("   ✗ Ошибка: админ должен редактировать поля после согласования")
        return False
    print("   ✓ После согласования поля редактирует только админ\n")

    print("4. Ручная смена статуса на sent_to_bank (не через approve/reject/mark-paid):")
    updated = s.update_invoice_status(inv_id, "sent_to_bank", "admin_user")
    if updated["status"] != "sent_to_bank":
        print(f"   ✗ Ошибка: статус не изменился, получено {updated['status']}")
        return False
    if s.can_edit_invoice_fields(updated, "admin_user", "admin"):
        print("   ✗ Ошибка: после sent_to_bank поля должны быть закрыты даже для админа")
        return False
    if not s.can_edit_invoice_status(updated, "admin"):
        print("   ✗ Ошибка: статус должен оставаться редактируемым (не все счета идут через банк)")
        return False
    print("   ✓ Поля закрыты, статус всё ещё редактируем\n")

    print("5. Дальнейшая смена статуса sent_to_bank -> paid и авто-архив:")
    updated = s.update_invoice_status(inv_id, "paid", "admin_user")
    if updated["status"] != "paid" or not updated["paid_at"]:
        print("   ✗ Ошибка: paid_at не проставлен при ручной смене статуса")
        return False
    # без распределения счёт не считается полностью разнесённым -> не архивируется автоматически
    if updated["is_archived"]:
        print("   ✗ Ошибка: счёт без распределения не должен архивироваться автоматически")
        return False
    if not s.can_edit_invoice_status(updated, "admin"):
        print("   ✗ Ошибка: статус редактируем, пока счёт не в архиве")
        return False
    print("   ✓ Статус меняется вручную в обход approve/reject/mark-paid\n")

    print("6. Архивация -> статус закрыт даже для админа:")
    s.set_invoice_archived(inv_id, True, "admin_user")
    archived_invoice = s.get_invoice_by_id(inv_id)
    if s.can_edit_invoice_status(archived_invoice, "admin"):
        print("   ✗ Ошибка: статус архивного счёта не должен быть редактируемым")
        return False
    print("   ✓ Архив закрывает изменение статуса\n")

    print("7. Сообщения на карточке счёта:")
    s.add_invoice_comment(inv_id, "creator_x", "Отправил счёт на согласование")
    s.add_invoice_comment(inv_id, "admin_user", "Проверил, всё ок")
    comments = s.get_invoice_comments(inv_id)
    if len(comments) != 2 or comments[0]["author"] != "creator_x" or comments[1]["author"] != "admin_user":
        print(f"   ✗ Ошибка: сообщения не сохранились в правильном порядке: {comments}")
        return False
    print("   ✓ Сообщения сохраняются и отдаются по порядку\n")

    print("=== Редактирование, история и сообщения работают корректно! ===")
    return True


if __name__ == "__main__":
    success = test_invoices()
    success = test_migration_from_old_schema() and success
    success = test_concurrent_migration() and success
    success = test_editing_history_comments() and success

    for path in (TEST_DB_PATH,
                 os.path.join(os.path.dirname(__file__), '_test_invoices_migration.db'),
                 os.path.join(os.path.dirname(__file__), '_test_invoices_concurrent.db')):
        if os.path.exists(path):
            os.remove(path)
    if os.path.exists(TEST_ATTACHMENTS_DIR):
        import shutil
        shutil.rmtree(TEST_ATTACHMENTS_DIR, ignore_errors=True)

    sys.exit(0 if success else 1)
