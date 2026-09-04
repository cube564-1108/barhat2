"""
Тестовый скрипт для Фазы 5 плана (автоформирование платёжки в Модульбанк).

Пишет в throwaway SQLite-файл (не в прод БД). Реальный API Модульбанка НЕ
вызывается — modulbank.client.get_client() подменяется на фейковый клиент
(проверка server.py:_send_invoice_to_bank) либо requests.Session.post
монкипатчится точечно (проверка document.py/client.py), без похода в
интернет и без риска для боевого кабинета.

Проверяет:
1. build_1c_payment_document — все обязательные поля 1С-документа на месте,
   суммы и даты отформатированы верно.
2. ModulbankClient.create_payment_draft — верный URL/заголовки (Bearer,
   sandbox: on), тело запроса {"document": ...}, разбор ответа (ok/errors).
3. Реквизиты плательщика живут в справочнике invoice_payers (не в .env) —
   CRUD и payer_has_bank_requisites для случая "у плательщика нет банка".
4. _send_invoice_to_bank (src/invoices/server.py): не хватает реквизитов
   счёта/плательщика -> понятная ошибка, счёт не в статусе approved (не
   sandbox) -> 409, sandbox не меняет статус счёта, боевая отправка -> статус
   sent_to_bank и bank_send_error очищен, ошибка банка -> bank_send_error
   записан и статус не меняется.
"""

import os
import sys
import io

if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), '_test_modulbank.db')
TEST_ATTACHMENTS_DIR = os.path.join(os.path.dirname(__file__), '_test_modulbank_attachments')
if os.path.exists(TEST_DB_PATH):
    os.remove(TEST_DB_PATH)
os.environ['BARHAT_DB_PATH'] = TEST_DB_PATH
os.environ['INVOICE_ATTACHMENTS_DIR'] = TEST_ATTACHMENTS_DIR
os.environ['MODULBANK_API_TOKEN'] = 'test-token'

from cashshifts.storage import init_cashshifts_tables
from invoices.storage import (
    init_invoices_tables,
    create_payer,
    update_payer_bank_requisites,
    payer_has_bank_requisites,
    get_payer_bank_requisites,
    create_invoice,
    get_invoice_by_id,
    approve_invoice,
    create_vat_option,
)

from modulbank.document import build_1c_payment_document
import modulbank.client as modulbank_client_module


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


def test_build_document():
    print("\n=== Тест 1: build_1c_payment_document собирает все обязательные поля ===")
    doc = build_1c_payment_document(
        doc_num="42", date="2026-08-20", amount=1234.5, purpose="Аренда за август",
        payer=PAYER, recipient=RECIPIENT,
    )
    assert doc.startswith("1CClientBankExchange\n")
    assert doc.strip().endswith("КонецФайла")
    assert "Сумма=1234.50" in doc, doc
    assert "Дата=20.08.2026" in doc
    assert "ПлательщикИНН=7700000001" in doc
    assert "ПлательщикКПП=770001001" in doc
    assert "ПолучательИНН=500100000002" in doc
    assert "ПолучательКПП=" in doc  # получатель без КПП (ИП) — поле пустое, но присутствует (мандаторное)
    assert "НазначениеПлатежа=Аренда за август" in doc
    assert "РасчСчет=40702810900000000001" in doc
    print("   ✓ документ содержит корректно отформатированные обязательные поля")


class _FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.exceptions.HTTPError(response=self)

    def json(self):
        return self._body


def test_client_request_format():
    print("\n=== Тест 2: ModulbankClient шлёт верные заголовки/тело, разбирает ответ ===")
    captured = {}

    def fake_post(self, url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return _FakeResponse(200, {"totalLoaded": 1, "errors": []})

    import requests
    original_post = requests.Session.post
    requests.Session.post = fake_post
    try:
        client = modulbank_client_module.get_client(sandbox_mode=True)
        result = client.create_payment_draft("1CClientBankExchange\n...\nКонецФайла")
    finally:
        requests.Session.post = original_post

    assert captured["url"] == "https://api.modulbank.ru/v1/operation-upload/1c"
    assert captured["headers"]["Authorization"] == "Bearer test-token"
    assert captured["headers"]["sandbox"] == "on"
    assert "document" in captured["json"]
    assert result == {"ok": True, "total_loaded": 1, "errors": [], "raw": {"totalLoaded": 1, "errors": []}}
    print("   ✓ Authorization: Bearer, sandbox: on, тело {'document': ...} — всё верно")

    def fake_post_error(self, url, json=None, headers=None, timeout=None):
        return _FakeResponse(200, {"totalLoaded": 0, "errors": ["Неверный БИК плательщика"]})

    requests.Session.post = fake_post_error
    try:
        client = modulbank_client_module.get_client(sandbox_mode=False)
        result = client.create_payment_draft("doc")
    finally:
        requests.Session.post = original_post

    assert result["ok"] is False
    assert result["errors"] == ["Неверный БИК плательщика"]
    print("   ✓ ошибка банка (totalLoaded=0, errors=[...]) распознаётся как неуспех")


def setup_db():
    init_cashshifts_tables()
    init_invoices_tables()


def test_payer_bank_requisites():
    print("\n=== Тест 3: реквизиты банка живут в справочнике плательщиков, не в .env ===")
    setup_db()

    payer_id = create_payer("ООО Кофферс тест")
    assert payer_has_bank_requisites(payer_id) is False, "у нового плательщика реквизитов ещё нет"

    update_payer_bank_requisites(
        payer_id, inn=PAYER["inn"], kpp=PAYER["kpp"], bank_account=PAYER["account"],
        bank_name=PAYER["bank_name"], bank_bik=PAYER["bank_bik"], bank_corr_account=PAYER["bank_corr_account"],
    )
    assert payer_has_bank_requisites(payer_id) is True
    requisites = get_payer_bank_requisites(payer_id)
    assert requisites["account"] == PAYER["account"]
    print("   ✓ после заполнения реквизитов payer_has_bank_requisites становится True")

    payer_no_bank_id = create_payer("Плательщик без банка (наличка)")
    assert payer_has_bank_requisites(payer_no_bank_id) is False, \
        "плательщик без реквизитов не должен считаться готовым к банк-автоматике"
    print("   ✓ плательщик без реквизитов — не проводится через банк-автоматику (ожидаемое поведение)")

    return payer_id, payer_no_bank_id


def _make_invoice(payer_id, with_counterparty_bank=True, amount=1000.0, vat_id=None):
    invoice = create_invoice(
        amount=amount, payment_purpose="Аренда за август", created_by="tester",
        payer_id=payer_id, vat_id=vat_id,
        counterparty_name=RECIPIENT["name"] if with_counterparty_bank else None,
        counterparty_inn=RECIPIENT["inn"] if with_counterparty_bank else None,
        counterparty_kpp=RECIPIENT["kpp"],
        counterparty_bank_name=RECIPIENT["bank_name"] if with_counterparty_bank else None,
        counterparty_bank_bik=RECIPIENT["bank_bik"] if with_counterparty_bank else None,
        counterparty_bank_account=RECIPIENT["account"] if with_counterparty_bank else None,
        counterparty_bank_corr_account=RECIPIENT["bank_corr_account"] if with_counterparty_bank else None,
    )
    return invoice


class FakeModulbankClient:
    def __init__(self, ok=True, errors=None):
        self.ok = ok
        self.errors = errors or []
        self.calls = []

    def send_invoice_payment(self, **kwargs):
        self.calls.append(kwargs)
        return {"ok": self.ok, "total_loaded": 1 if self.ok else 0, "errors": self.errors, "raw": {}, "document": "..."}


def test_send_invoice_to_bank():
    print("\n=== Тест 4: _send_invoice_to_bank — валидация, sandbox, успех, ошибка банка ===")
    payer_id, payer_no_bank_id = test_payer_bank_requisites()

    from invoices.server import _send_invoice_to_bank

    # a) нет реквизитов контрагента в счёте (sandbox=True — чтобы проверить
    # именно эту валидацию отдельно от проверки статуса согласования)
    invoice_no_recipient = _make_invoice(payer_id, with_counterparty_bank=False)
    outcome = _send_invoice_to_bank(invoice_no_recipient, sandbox=True, changed_by="admin")
    assert outcome["http_status"] == 400 and "реквизиты контрагента" in outcome["body"]["error"], outcome
    print("   ✓ без реквизитов контрагента — понятная ошибка 400 (проверяется даже в sandbox)")

    # b) плательщик без банковских реквизитов
    invoice_no_payer_bank = _make_invoice(payer_no_bank_id)
    outcome = _send_invoice_to_bank(invoice_no_payer_bank, sandbox=True, changed_by="admin")
    assert outcome["http_status"] == 400 and "не настроены реквизиты" in outcome["body"]["error"], outcome
    print("   ✓ у плательщика без реквизитов Модульбанка — понятная ошибка, счёт не теряется")

    # c) не согласован — боевая отправка запрещена
    invoice_draft = _make_invoice(payer_id)
    outcome = _send_invoice_to_bank(invoice_draft, sandbox=False, changed_by="admin")
    assert outcome["http_status"] == 409, outcome
    print("   ✓ боевая отправка неcогласованного счёта запрещена (409)")

    # d) sandbox — не меняет статус счёта даже на approved; назначение
    # платежа, реально ушедшее в банк, должно содержать match_code (баг:
    # раньше уходил только payment_purpose, код для матчинга с ПланФакт
    # никогда не попадал в реальный платёж) и фразу про НДС (баг: банк не
    # заполнял поле НДС, т.к. в назначении платежа не было ставки текстом)
    vat_id = create_vat_option("20%")
    invoice_ok = _make_invoice(payer_id, vat_id=vat_id)
    approve_invoice(invoice_ok["id"], "admin")
    invoice_ok = get_invoice_by_id(invoice_ok["id"])

    fake = FakeModulbankClient(ok=True)
    modulbank_client_module.get_client = lambda **kw: fake
    outcome = _send_invoice_to_bank(invoice_ok, sandbox=True, changed_by="admin")
    assert outcome["ok"] is True and outcome["body"]["sandbox"] is True
    invoice_after_sandbox = get_invoice_by_id(invoice_ok["id"])
    assert invoice_after_sandbox["status"] == "approved", "sandbox не должен менять статус счёта"
    assert len(fake.calls) == 1
    sent_purpose = fake.calls[0]["purpose"]
    assert sent_purpose.startswith(invoice_ok["match_code"]), \
        f"match_code должен быть в начале назначения платежа: {sent_purpose!r}"
    assert "НДС 20%" in sent_purpose and "166.67" in sent_purpose, \
        f"назначение платежа должно содержать сумму НДС: {sent_purpose!r}"
    print("   ✓ sandbox=true отправляет в тестовый контур, статус счёта не меняется")
    print("   ✓ назначение платежа содержит match_code и корректную сумму НДС")

    # e) боевая отправка — успех, статус sent_to_bank, ошибка очищена
    outcome = _send_invoice_to_bank(invoice_ok, sandbox=False, changed_by="admin")
    assert outcome["ok"] is True
    invoice_after_real = get_invoice_by_id(invoice_ok["id"])
    assert invoice_after_real["status"] == "sent_to_bank"
    assert invoice_after_real["bank_send_error"] is None
    print("   ✓ боевая отправка переводит счёт в sent_to_bank")

    # f) ошибка банка — bank_send_error записан, статус не двигается дальше.
    # НДС обязателен и здесь: без него отправка не доходит до банка вовсе
    # (400 вместо 502) — см. scripts/test_vat_in_bank_document.py
    invoice_fail = _make_invoice(payer_id, vat_id=vat_id)
    approve_invoice(invoice_fail["id"], "admin")
    invoice_fail = get_invoice_by_id(invoice_fail["id"])

    fake_fail = FakeModulbankClient(ok=False, errors=["Неверный БИК плательщика"])
    modulbank_client_module.get_client = lambda **kw: fake_fail
    outcome = _send_invoice_to_bank(invoice_fail, sandbox=False, changed_by="admin")
    assert outcome["http_status"] == 502
    invoice_after_fail = get_invoice_by_id(invoice_fail["id"])
    assert invoice_after_fail["status"] == "approved", "статус не должен меняться при ошибке банка"
    assert invoice_after_fail["bank_send_error"] == "Неверный БИК плательщика"
    print("   ✓ ошибка банка не теряется молча — записана в bank_send_error, счёт остаётся approved")


if __name__ == "__main__":
    test_build_document()
    test_client_request_format()
    test_send_invoice_to_bank()
    print("\n=== Все тесты Модульбанк-интеграции (Фаза 5) пройдены успешно! ===")
