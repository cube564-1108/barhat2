"""
Стартовое сальдо рабочей карты: миграция, расчёт подотчёта, HTTP-правка.

Зачем сторож. Расхождение вкладки "Остатки на картах" с ПланФактом — это
индикатор незанесённой траты, и он работает, только пока НЕ горит постоянно.
02.09.26 по томской карте он показывал вечные "расхождение 4 527 ₽" при
полностью корректном учёте: подотчёт считался с нуля, а на карте до заведения
в модуле уже лежали деньги. Тест держит формулу
`остаток = сальдо + выдано − отчитано − ждёт подтверждения`.

Сеть отключается ДО импорта приложения: `pyrus.server` зовёт load_dotenv() и
подхватывает боевые токены, а подмена env-переменных от этого не спасает.
"""

import os
import socket
import sys
import tempfile


class _NoNetwork(socket.socket):
    def connect(self, *a, **k):
        raise OSError("сеть отключена намеренно")

    def connect_ex(self, *a, **k):
        raise OSError("сеть отключена намеренно")


socket.socket = _NoNetwork

_TMP = tempfile.mkdtemp(prefix="card_opening_")
os.environ["BARHAT_DB_PATH"] = os.path.join(_TMP, "barhat.db")
os.environ["INVOICE_ATTACHMENTS_DIR"] = os.path.join(_TMP, "attachments")
os.environ["FLASK_SECRET_KEY"] = "test-secret"
os.environ["CARD_SYNC_SCHEDULER"] = "0"

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from invoices import storage                                   # noqa: E402
from invoices.cards import (                                   # noqa: E402
    create_card,
    update_card,
    get_card_by_id,
    get_cards_balances,
    empty_accountable,
    init_cards_tables,
)

failures = []


def check(name, condition, detail=""):
    print(("  OK   " if condition else "  FAIL ") + name + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(name)


def make_invoice(card_id, kind, amount, status):
    """Заявка напрямую в базу: маршрут согласования тут не проверяется."""
    conn = storage.get_db()
    try:
        conn.execute(
            "INSERT INTO invoices (invoice_number, amount, payment_purpose, status, "
            "created_by, kind, card_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            (f"T-{kind}-{amount}-{status}", amount, "тест", status, "tester", kind, card_id),
        )
        conn.commit()
    finally:
        conn.close()


print("\n=== Схема и миграция ===")
storage.init_invoices_tables()
init_cards_tables()

conn = storage.get_db()
try:
    has_column = storage._column_exists(conn, "work_cards", "opening_balance")
finally:
    conn.close()
check("колонка opening_balance создана", has_column)

# Повторный вызов должен быть безвредным: init идёт при старте КАЖДОГО воркера
init_cards_tables()
check("повторная инициализация не падает", True)

print("\n=== Миграция поверх старой таблицы (без колонки) ===")
conn = storage.get_db()
try:
    conn.execute("DROP TABLE IF EXISTS work_cards")
    conn.execute("""
        CREATE TABLE work_cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            planfact_account_id TEXT NOT NULL,
            planfact_account_title TEXT,
            source_planfact_account_id TEXT NOT NULL,
            source_planfact_account_title TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute(
        "INSERT INTO work_cards (title, planfact_account_id, source_planfact_account_id) "
        "VALUES ('Старая карта', '111', '222')"
    )
    conn.commit()
finally:
    conn.close()

init_cards_tables()
old = get_card_by_id(1)
check("старая карта пережила миграцию", old is not None and old["title"] == "Старая карта")
check("сальдо старой карты = 0", old is not None and (old["opening_balance"] or 0) == 0,
      f"получено {old and old['opening_balance']!r}")

print("\n=== Расчёт подотчёта (томский сценарий) ===")
card_id = create_card(
    title="Рабочая карта Томск (ГПБ)",
    planfact_account_id="474531",
    source_planfact_account_id="749767",
    store_ids=[],
    opening_balance=4527,
)
make_invoice(card_id, "card_expense", 4000, "approved")
make_invoice(card_id, "card_topup", 20400, "paid")

own = get_cards_balances()[card_id]
check("было на начало учёта = 4527", own["opening"] == 4527.0, str(own["opening"]))
check("выдано = 20400", own["issued"] == 20400.0, str(own["issued"]))
check("отчитано = 4000", own["spent_confirmed"] == 4000.0, str(own["spent_confirmed"]))
check("остаток = 20927 (сходится с ПланФактом)", own["balance"] == 20927.0, str(own["balance"]))

print("\n=== Что НЕ должно попасть в подотчёт ===")
make_invoice(card_id, "card_topup", 9999, "approved")   # согласован, но не переведён
make_invoice(card_id, "card_expense", 500, "rejected")  # отклонён
make_invoice(card_id, "card_expense", 300, "on_approval")

own = get_cards_balances()[card_id]
check("согласованное пополнение не считается выданным", own["issued"] == 20400.0, str(own["issued"]))
check("отклонённая трата не считается", own["spent_confirmed"] == 4000.0, str(own["spent_confirmed"]))
check("трата на согласовании ждёт подтверждения", own["spent_pending"] == 300.0, str(own["spent_pending"]))
check("остаток учитывает ждущую трату", own["balance"] == 20627.0, str(own["balance"]))

print("\n=== Карта без заявок ===")
empty_id = create_card(title="Пустая", planfact_account_id="1", source_planfact_account_id="2",
                       opening_balance=1500)
own_empty = get_cards_balances().get(empty_id)
check("карта без заявок есть в подотчёте", own_empty is not None)
check("её остаток = стартовое сальдо", own_empty and own_empty["balance"] == 1500.0,
      str(own_empty and own_empty["balance"]))
check("форма ответа совпадает с empty_accountable",
      own_empty is not None and set(own_empty) == set(empty_accountable()))

print("\n=== Правка сальдо ===")
update_card(card_id, {"opening_balance": 0})
check("сальдо снимается в ноль", get_cards_balances()[card_id]["opening"] == 0.0)
check("остаток пересчитался", get_cards_balances()[card_id]["balance"] == 16100.0,
      str(get_cards_balances()[card_id]["balance"]))
update_card(card_id, {"opening_balance": 4527})
check("сальдо возвращается", get_cards_balances()[card_id]["opening"] == 4527.0)

print("\n=== HTTP: разбор значения из формы ===")
from invoices.server import _parse_opening_balance, _card_payload_error   # noqa: E402

check("строка 4527 -> 4527.0", _parse_opening_balance("4527") == 4527.0)
check("запятая принимается", _parse_opening_balance("4527,50") == 4527.5)
check("пробелы-разделители принимаются", _parse_opening_balance("4 527") == 4527.0)
check("отрицательное принимается", _parse_opening_balance("-100") == -100.0)
check("буквы отвергаются", _parse_opening_balance("4527 руб") is None)
check("True не считается числом", _parse_opening_balance(True) is None)
check("пустое значение проходит валидацию",
      _card_payload_error({"opening_balance": ""}, require_all=False) is None)
check("мусор отклоняется с текстом",
      _card_payload_error({"opening_balance": "много"}, require_all=False) is not None)

print()
if failures:
    print(f"ПРОВАЛЕНО {len(failures)}: " + "; ".join(failures))
    sys.exit(1)
print("Все проверки пройдены.")
