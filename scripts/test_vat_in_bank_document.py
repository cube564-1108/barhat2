"""
НДС в платёжке Модульбанка: ставка берётся из справочника, а не из названия.

Регрессия 04.09.2026 (счёт REF-000168): фраза про НДС для назначения платежа
собиралась регуляркой по НАЗВАНИЮ варианта справочника, поэтому вариант,
названный без знака «%» («С НДС», «НДС 20/120»), давал в банк строку без
ставки — банк оставлял поле НДС пустым и молчал, платёжку при этом принимая.
Всплыло после того, как НДС сделали обязательным при оплате с расчётного
счёта: варианты стали выбирать все подряд, а не только привычные «20%».

Проверяет:
1. parse_vat_rate_from_name — что вообще выводится из названия (для бэкфилла).
2. Миграция _ensure_vat_rate_column: колонка + бэкфилл распознаваемых названий,
   идемпотентность повторного прогона, нераспознанные остаются без ставки.
3. Фраза для банка строится по ставке, а не по названию: вариант «С НДС» со
   ставкой 20 даёт корректное «В том числе НДС 20% — ... руб.».
4. Отправка в банк не проходит молча: счёт без НДС и счёт с вариантом без
   ставки -> 400 с внятным текстом, обращения к банку не было.
5. Переименование варианта из общего CRUD справочников не обнуляет ставку.
6. Перевод строки в назначении платежа не разрывает 1С-документ.
7. Текст ушедшей платёжки сохраняется и отдаётся ручкой /bank-document.

Внешний API не вызывается: modulbank.client.get_client подменяется фейком.
"""

import io
import os
import sys

if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), '_test_vat_bank.db')
TEST_ATTACHMENTS_DIR = os.path.join(os.path.dirname(__file__), '_test_vat_bank_attachments')
for path in (TEST_DB_PATH, TEST_DB_PATH + '-wal', TEST_DB_PATH + '-shm'):
    if os.path.exists(path):
        os.remove(path)
os.environ['BARHAT_DB_PATH'] = TEST_DB_PATH
os.environ['INVOICE_ATTACHMENTS_DIR'] = TEST_ATTACHMENTS_DIR
os.environ['MODULBANK_API_TOKEN'] = 'test-token'

from cashshifts.storage import init_cashshifts_tables
from invoices.storage import (  # noqa: E402
    init_invoices_tables,
    get_db,
    parse_vat_rate_from_name,
    _ensure_vat_rate_column,
    create_vat_option,
    update_vat_option,
    get_vat_option_by_id,
    create_payer,
    update_payer_bank_requisites,
    create_invoice,
    get_invoice_by_id,
    approve_invoice,
    get_invoice_bank_document,
)
import invoices.server as invoices_server  # noqa: E402
import modulbank.client as modulbank_client_module  # noqa: E402
from modulbank.document import build_1c_payment_document  # noqa: E402

PAYER = {
    "name": "ООО Кофферс", "inn": "7700000001", "kpp": "770001001",
    "account": "40702810900000000001", "bank_name": "АО «МОДУЛЬБАНК»",
    "bank_bik": "044525092", "bank_corr_account": "30101810845250000092",
}
RECIPIENT = {
    "name": "ИП Иванов", "inn": "500100000002", "kpp": None,
    "account": "40802810900000000002", "bank_name": "ПАО СБЕРБАНК",
    "bank_bik": "044525225", "bank_corr_account": "30101810400000000225",
}


class FakeModulbankClient:
    """Фейковый банк: запоминает, с чем звали, наружу не ходит."""

    def __init__(self):
        self.calls = []

    def send_invoice_payment(self, **kwargs):
        self.calls.append(kwargs)
        document = build_1c_payment_document(
            doc_num=kwargs["doc_num"], date=kwargs["date"], amount=kwargs["amount"],
            purpose=kwargs["purpose"], payer=kwargs["payer"], recipient=kwargs["recipient"],
        )
        return {"ok": True, "total_loaded": 1, "errors": [], "raw": {}, "document": document}


def setup():
    init_cashshifts_tables()
    init_invoices_tables()
    payer_id = create_payer("ООО Кофферс тест")
    update_payer_bank_requisites(
        payer_id, inn=PAYER["inn"], kpp=PAYER["kpp"], bank_account=PAYER["account"],
        bank_name=PAYER["bank_name"], bank_bik=PAYER["bank_bik"],
        bank_corr_account=PAYER["bank_corr_account"],
    )
    return payer_id


def make_invoice(payer_id, vat_id=None, amount=10000.0, purpose="Оплата по счёту 55"):
    invoice = create_invoice(
        amount=amount, payment_purpose=purpose, created_by="tester",
        payer_id=payer_id, vat_id=vat_id,
        counterparty_name=RECIPIENT["name"], counterparty_inn=RECIPIENT["inn"],
        counterparty_kpp=RECIPIENT["kpp"], counterparty_bank_name=RECIPIENT["bank_name"],
        counterparty_bank_bik=RECIPIENT["bank_bik"], counterparty_bank_account=RECIPIENT["account"],
        counterparty_bank_corr_account=RECIPIENT["bank_corr_account"],
    )
    approve_invoice(invoice["id"], "admin")
    return get_invoice_by_id(invoice["id"])


def test_parse_rate_from_name():
    print("\n=== Тест 1: разбор ставки из названия (нужен только для бэкфилла) ===")
    assert parse_vat_rate_from_name("20%") == 20
    assert parse_vat_rate_from_name("НДС 20 %") == 20
    assert parse_vat_rate_from_name("10%") == 10
    assert parse_vat_rate_from_name("Без НДС") == 0
    assert parse_vat_rate_from_name("НДС не облагается") == 0
    # Ровно эти названия и ломали платёжку: ставки в них не видно
    for name in ("С НДС", "НДС 20/120", "НДС 20 процентов", "20", "УСН"):
        assert parse_vat_rate_from_name(name) is None, name
    print("   ✓ «20%»/«Без НДС» разбираются, «С НДС»/«20/120» — нет (и не должны)")


def test_migration_backfill():
    print("\n=== Тест 2: миграция проставляет ставку старым вариантам ===")
    conn = get_db()
    try:
        # Имитируем справочник, заполненный до появления поля rate
        for name in ("20% (старый)", "Без НДС (старый)", "С НДС (старый)"):
            conn.execute("INSERT INTO invoice_vat_options (name, is_active) VALUES (?, 1)", (name,))
        conn.execute("UPDATE invoice_vat_options SET rate = NULL")
        conn.commit()

        _ensure_vat_rate_column(conn)
        rates = {row["name"]: row["rate"] for row in
                 conn.execute("SELECT name, rate FROM invoice_vat_options").fetchall()}
        assert rates["20% (старый)"] == 20, rates
        assert rates["Без НДС (старый)"] == 0, rates
        assert rates["С НДС (старый)"] is None, rates
        print("   ✓ распознаваемые названия получили ставку, «С НДС» остался без неё")

        # Повторный прогон идемпотентен и не трогает выставленное руками
        conn.execute("UPDATE invoice_vat_options SET rate = 20 WHERE name = 'С НДС (старый)'")
        conn.commit()
        _ensure_vat_rate_column(conn)
        rate = conn.execute(
            "SELECT rate FROM invoice_vat_options WHERE name = 'С НДС (старый)'").fetchone()["rate"]
        assert rate == 20, "повторная миграция не должна затирать ставку, заданную человеком"
        print("   ✓ повторный старт не затирает ставку, выставленную руками")
    finally:
        conn.close()


def test_phrase_built_from_rate(payer_id):
    print("\n=== Тест 3: фраза для банка строится по ставке, а не по названию ===")
    # Ровно тот случай, который сломался на проде: название без «%»
    vat_id = create_vat_option("С НДС", rate=20)
    invoice = make_invoice(payer_id, vat_id=vat_id, amount=10000.0)

    fake = FakeModulbankClient()
    modulbank_client_module.get_client = lambda **kw: fake
    outcome = invoices_server._send_invoice_to_bank(invoice, sandbox=True, changed_by="admin")

    assert outcome["ok"] is True, outcome
    purpose = fake.calls[0]["purpose"]
    assert purpose.startswith(invoice["match_code"]), purpose
    assert "НДС 20% — 1666.67 руб." in purpose, purpose
    print(f"   ✓ вариант «С НДС» со ставкой 20 даёт: {purpose!r}")

    # Дробная ставка — через запятую, как в платёжке
    vat_half = create_vat_option("льготная", rate=7.5)
    invoice_half = make_invoice(payer_id, vat_id=vat_half, amount=1075.0)
    invoices_server._send_invoice_to_bank(invoice_half, sandbox=True, changed_by="admin")
    assert "НДС 7,5% — 75.00 руб." in fake.calls[-1]["purpose"], fake.calls[-1]["purpose"]
    print("   ✓ дробная ставка 7,5% считается и пишется корректно")

    # Без налога
    vat_zero = create_vat_option("Без НДС")
    invoice_zero = make_invoice(payer_id, vat_id=vat_zero)
    invoices_server._send_invoice_to_bank(invoice_zero, sandbox=True, changed_by="admin")
    assert fake.calls[-1]["purpose"].endswith("Без налога (НДС)."), fake.calls[-1]["purpose"]
    print("   ✓ «Без НДС» уходит как «Без налога (НДС).»")


def test_send_blocked_without_rate(payer_id):
    print("\n=== Тест 4: без НДС и без ставки платёжка в банк не уходит ===")
    fake = FakeModulbankClient()
    modulbank_client_module.get_client = lambda **kw: fake

    invoice_no_vat = make_invoice(payer_id, vat_id=None)
    outcome = invoices_server._send_invoice_to_bank(invoice_no_vat, sandbox=False, changed_by="admin")
    assert outcome["http_status"] == 400, outcome
    assert "не указан НДС" in outcome["body"]["error"], outcome
    print("   ✓ счёт без НДС: 400 с объяснением, а не тихая отправка")

    vat_no_rate = create_vat_option("НДС 20/120")   # из названия ставку не вывести
    assert get_vat_option_by_id(vat_no_rate)["rate"] is None
    invoice_no_rate = make_invoice(payer_id, vat_id=vat_no_rate)
    outcome = invoices_server._send_invoice_to_bank(invoice_no_rate, sandbox=False, changed_by="admin")
    assert outcome["http_status"] == 400, outcome
    assert "не задана ставка" in outcome["body"]["error"], outcome
    assert "НДС 20/120" in outcome["body"]["error"], "в тексте должно быть видно, какой вариант чинить"
    print("   ✓ вариант без ставки: 400 с названием варианта в тексте")

    assert not fake.calls, "до банка ни один из этих счетов дойти не должен"
    print("   ✓ обращения к банку не было ни разу")


def test_rename_keeps_rate():
    print("\n=== Тест 5: переименование варианта не обнуляет ставку ===")
    vat_id = create_vat_option("20%", rate=20)
    update_vat_option(vat_id, "НДС двадцать")       # общий CRUD справочников, ставку не шлёт
    assert get_vat_option_by_id(vat_id)["rate"] == 20, "переименование не должно трогать ставку"
    update_vat_option(vat_id, "НДС двадцать", rate=None)   # а вот это — осознанная очистка
    assert get_vat_option_by_id(vat_id)["rate"] is None
    print("   ✓ ставка живёт отдельно от названия: переименование её сохраняет, очистка — снимает")


def test_newline_does_not_break_document():
    print("\n=== Тест 6: перенос строки в назначении не разрывает 1С-документ ===")
    document = build_1c_payment_document(
        doc_num="1", date="2026-09-04", amount=10000.0,
        purpose="REF-000168 Оплата по счёту 55\nза цветы В том числе НДС 20% — 1666.67 руб.",
        payer=dict(PAYER, bank_name="АО «МОДУЛЬБАНК»\n"), recipient=RECIPIENT,
    )
    lines = document.splitlines()
    structural = {"1CClientBankExchange", "КонецДокумента", "КонецФайла"}
    assert all("=" in line or line in structural for line in lines), \
        "каждая строка документа — либо «ключ=значение», либо служебная"
    purpose_line = next(line for line in lines if line.startswith("НазначениеПлатежа="))
    assert purpose_line.endswith("В том числе НДС 20% — 1666.67 руб."), purpose_line
    assert "за цветы" in purpose_line, "текст после переноса не должен теряться"
    print("   ✓ назначение склеено в одну строку, хвост с НДС на месте")


def test_document_saved(payer_id):
    print("\n=== Тест 7: текст ушедшей платёжки сохраняется ===")
    fake = FakeModulbankClient()
    modulbank_client_module.get_client = lambda **kw: fake

    vat_id = create_vat_option("20 процентов", rate=20)
    invoice = make_invoice(payer_id, vat_id=vat_id)
    outcome = invoices_server._send_invoice_to_bank(invoice, sandbox=False, changed_by="admin")
    assert outcome["ok"] is True, outcome

    saved = get_invoice_bank_document(invoice["id"])
    assert saved, "документ должен сохраниться рядом со счётом"
    assert "НазначениеПлатежа=" in saved["document"]
    assert "НДС 20%" in saved["document"], "в сохранённом документе видно, что ушло по НДС"
    assert saved["sent_by"] == "admin" and saved["accepted"] == 1 and saved["sandbox"] == 0
    print("   ✓ документ, автор и результат отправки сохранены — видно, что реально ушло в банк")


if __name__ == "__main__":
    payer_id = setup()
    test_parse_rate_from_name()
    test_migration_backfill()
    test_phrase_built_from_rate(payer_id)
    test_send_blocked_without_rate(payer_id)
    test_rename_keeps_rate()
    test_newline_does_not_break_document()
    test_document_saved(payer_id)
    print("\n=== Все проверки НДС в платёжке пройдены ===")
