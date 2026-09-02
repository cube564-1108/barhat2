"""
Справочник банков по БИК: разбор ED807, запись, подстановка, API.

Зачем сторож. Подставленный корр. счёт уходит в платёжку, а ошибку в двадцати
цифрах человек глазами не ловит — она всплывает уже в банке. Поэтому здесь
закреплены именно ловушки формата ЦБ, а не «счастливый путь»:

  * корсчёт берётся ТОЛЬКО из счёта типа CRSA в статусе ACAC. У 455 записей из
    1416 (РКЦ, УФК, подразделения) его нет вовсе, и подставить вместо него
    счёт типа BANA/TRSA нельзя — это разные счета, платёжка уйдёт не туда;
  * живость банка определяется по PtType и RstrList, а НЕ по ParticipantStatus:
    у всех 1416 записей он равен PSAC и признаком ничего не является;
  * пустой разбор не должен затирать уже загруженный справочник.

Сеть выключена намеренно: подстановка обязана работать локальным SELECT'ом.
Если ручка когда-нибудь начнёт ходить наружу, тест упадёт — это и есть цель.
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

_TMP = tempfile.mkdtemp(prefix="banks_")
os.environ["BARHAT_DB_PATH"] = os.path.join(_TMP, "barhat.db")
os.environ["INVOICE_ATTACHMENTS_DIR"] = os.path.join(_TMP, "attachments")
os.environ["FLASK_SECRET_KEY"] = "test-secret"
os.environ["BANKS_SCHEDULER"] = "0"
os.environ["CARD_SYNC_SCHEDULER"] = "0"

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from invoices import storage                                    # noqa: E402
from invoices.banks import (                                    # noqa: E402
    init_banks_table,
    parse_ed807,
    save_banks,
    lookup_bank,
    is_valid_bic,
    get_banks_status,
)

failures = []


def check(name, condition, detail=""):
    print(("  OK   " if condition else "  FAIL ") + name + (f" - {detail}" if detail else ""))
    if not condition:
        failures.append(name)


# Выдержка из настоящей выгрузки ЦБ, обрезанная до нужных случаев.
# Структура и имена атрибутов сверены с 20260902_ED807_full.xml.
SAMPLE = """<?xml version="1.0" encoding="windows-1251"?>
<ED807 xmlns="urn:cbr-ru:ed:v2.0" EDNo="707231177" EDDate="2026-09-01"
       BusinessDay="2026-09-02" InfoTypeCode="FIRR">
  <BICDirectoryEntry BIC="044525225">
    <ParticipantInfo NameP="ПАО Сбербанк" Nnp="Москва" PtType="20" ParticipantStatus="PSAC"/>
    <Accounts Account="30101810400000000225" RegulationAccountType="CRSA" AccountStatus="ACAC"/>
  </BICDirectoryEntry>
  <BICDirectoryEntry BIC="046577674">
    <ParticipantInfo NameP="УРАЛЬСКИЙ БАНК ПАО СБЕРБАНК" Nnp="Екатеринбург" PtType="30" ParticipantStatus="PSAC"/>
    <Accounts Account="30101810500000000674" RegulationAccountType="CRSA" AccountStatus="ACAC"/>
  </BICDirectoryEntry>
  <BICDirectoryEntry BIC="044525378">
    <ParticipantInfo NameP="КУ ООО КБ ОПМ-БАНК - ГК АСВ" Nnp="Москва" PtType="90" ParticipantStatus="PSAC">
      <RstrList Rstr="URRS"/>
    </ParticipantInfo>
    <Accounts Account="30101810945250000378" RegulationAccountType="CRSA" AccountStatus="ACAC"/>
  </BICDirectoryEntry>
  <BICDirectoryEntry BIC="017003983">
    <ParticipantInfo NameP="ОТДЕЛЕНИЕ БАРНАУЛ БАНКА РОССИИ" Nnp="Барнаул" PtType="10" ParticipantStatus="PSAC"/>
    <Accounts Account="40102810045370000009" RegulationAccountType="BANA" AccountStatus="ACAC"/>
  </BICDirectoryEntry>
  <BICDirectoryEntry BIC="044525999">
    <ParticipantInfo NameP="БАНК С ОТОЗВАННОЙ ЛИЦЕНЗИЕЙ" Nnp="Москва" PtType="20" ParticipantStatus="PSAC">
      <RstrList Rstr="LWRS"/>
    </ParticipantInfo>
    <Accounts Account="30101810000000000999" RegulationAccountType="CRSA" AccountStatus="ACAC"/>
  </BICDirectoryEntry>
  <BICDirectoryEntry BIC="044525888">
    <ParticipantInfo NameP="БАНК С ЗАКРЫТЫМ КОРСЧЁТОМ" Nnp="Москва" PtType="20" ParticipantStatus="PSAC"/>
    <Accounts Account="30101810000000000888" RegulationAccountType="CRSA" AccountStatus="ACDL"/>
  </BICDirectoryEntry>
</ED807>
"""

print("\n=== Разбор ED807 ===")
# Кодировка именно cp1251, как в настоящей выгрузке ЦБ
# (`<?xml version="1.0" encoding="WINDOWS-1251"?>`). Разбор идёт от БАЙТОВ,
# чтобы объявление в заголовке отработало; декодируй мы файл сами в utf-8 —
# все названия банков приехали бы кракозябрами, и заметить это было бы
# некому: подстановка формально продолжила бы работать.
data = parse_ed807(SAMPLE.encode("cp1251"))
by_bic = {b["bic"]: b for b in data["banks"]}

check("дата выгрузки прочитана", data["business_day"] == "2026-09-02", data["business_day"])
check("разобраны все записи", len(data["banks"]) == 6, str(len(data["banks"])))

sber = by_bic.get("044525225")
check("название банка", sber and sber["name"] == "ПАО Сбербанк")
check("город банка", sber and sber["city"] == "Москва")
check("корсчёт из CRSA/ACAC", sber and sber["corr_account"] == "30101810400000000225")
check("действующий банк активен", sber and sber["is_active"] == 1)

check("региональное отделение Сбербанка разбирается",
      by_bic["046577674"]["corr_account"] == "30101810500000000674")

print("\n=== Ловушки формата ===")
check("PtType=90 (ликвидатор) помечен нерабочим", by_bic["044525378"]["is_active"] == 0)
check("RstrList LWRS (отзыв лицензии) помечен нерабочим", by_bic["044525999"]["is_active"] == 0)
check("счёт типа BANA НЕ подставляется как корсчёт",
      by_bic["017003983"]["corr_account"] is None,
      str(by_bic["017003983"]["corr_account"]))
check("название у записи без корсчёта всё равно есть",
      by_bic["017003983"]["name"].startswith("ОТДЕЛЕНИЕ БАРНАУЛ"))
check("закрытый корсчёт (ACDL) не подставляется",
      by_bic["044525888"]["corr_account"] is None,
      str(by_bic["044525888"]["corr_account"]))

print("\n=== Запись и чтение ===")
storage.init_invoices_tables()
init_banks_table()
init_banks_table()   # инициализация идёт при старте каждого воркера
check("повторная инициализация не падает", True)

saved = save_banks(data["banks"])
check("записаны все банки", saved == 6, str(saved))

found = lookup_bank("044525225")
check("поиск по БИК находит банк", found is not None and found["name"] == "ПАО Сбербанк")
check("поиск отдаёт корсчёт", found and found["corr_account"] == "30101810400000000225")
check("несуществующий БИК не находится", lookup_bank("099999999") is None)

print("\n=== Повторная загрузка ===")
changed = [dict(b) for b in data["banks"]]
changed[0]["name"] = "ПАО Сбербанк (переименован)"
save_banks(changed)
check("повторная запись обновляет, а не двоит", lookup_bank("044525225")["name"].endswith("(переименован)"))
status = get_banks_status()
check("количество не выросло при повторной записи", status["count"] == 6, str(status["count"]))

try:
    save_banks([])
    check("пустой разбор не затирает справочник", False, "исключения не было")
except ValueError:
    check("пустой разбор не затирает справочник", True)
check("после отказа данные на месте", get_banks_status()["count"] == 6)

print("\n=== Проверка БИК ===")
check("девять цифр — валидный БИК", is_valid_bic("044525225"))
check("восемь цифр отвергаются", not is_valid_bic("04452522"))
check("буквы отвергаются", not is_valid_bic("04452522x"))
check("пустое значение отвергается", not is_valid_bic(""))
check("None отвергается", not is_valid_bic(None))
check("короткий огрызок не идёт в базу", lookup_bank("044") is None)

print("\n=== Статус справочника ===")
status = get_banks_status()
check("справочник не считается пустым", status["empty"] is False)
check("свежая загрузка не помечена устаревшей", status["stale"] is False)
check("дата обновления записана", bool(status["updated_at"]))

print("\n=== Исход прогона виден снаружи ===")
# Прогон работает в фоновом потоке, его исключение уходит только в лог, а
# консоли контейнера на этом тарифе Amvera нет. Без записанного исхода
# интерфейс показывал «не загружен» и на «ещё качается», и на «упало».
from invoices import banks as banks_module                       # noqa: E402

banks_module._set_last_run("running")
check("прогон помечается идущим", banks_module.get_last_run()["status"] == "running")

banks_module._set_last_run("error", "SSLError: сертификат не проверен")
run = banks_module.get_last_run()
check("ошибка сохраняется", run["status"] == "error", run["status"])
check("текст ошибки доезжает до человека", "SSLError" in run["detail"], run["detail"])
check("исход попадает в статус", get_banks_status()["last_run"]["status"] == "error")

banks_module._set_last_run("ok", "1416 записей, выгрузка от 2026-09-02")
check("успех перекрывает прошлую ошибку", get_banks_status()["last_run"]["status"] == "ok")

# Сеть выключена, значит настоящий прогон обязан упасть — и обязан рассказать
# об этом, а не молча оставить справочник пустым
try:
    banks_module.refresh_banks()
    check("падение прогона записывается", False, "исключения не было")
except Exception:
    check("падение прогона записывается", banks_module.get_last_run()["status"] == "error")
    check("причина падения читаема", bool(banks_module.get_last_run()["detail"]))
check("данные при этом не потерялись", get_banks_status()["count"] == 6)

print("\n=== HTTP-ручка (без сети) ===")
from flask import Flask                                          # noqa: E402
from flask_login import LoginManager, AnonymousUserMixin         # noqa: E402
from invoices import server as invoices_server                   # noqa: E402


class _TestUser(AnonymousUserMixin):
    """Пользователь с доступом к разделу. LOGIN_DISABLED снимает только
    login_required, а section_required дополнительно спрашивает права у
    current_user — без подставного пользователя ручку не вызвать."""
    username = "tester"
    role = "admin"
    is_authenticated = True

    def has_module_access(self, name):
        return True


app = Flask(__name__)
app.config["LOGIN_DISABLED"] = True
login_manager = LoginManager()
login_manager.anonymous_user = _TestUser
login_manager.init_app(app)


@login_manager.user_loader
def _load_test_user(_user_id):
    return _TestUser()

with app.test_request_context("/api/invoices/banks/044525225"):
    response = invoices_server.get_bank_by_bic("044525225")
    body = response.get_json() if hasattr(response, "get_json") else response[0].get_json()
    check("ручка отдаёт банк", body.get("bank", {}).get("name") == "ПАО Сбербанк (переименован)")
    check("ручка отдаёт корсчёт",
          body.get("bank", {}).get("corr_account") == "30101810400000000225")

with app.test_request_context("/api/invoices/banks/099999999"):
    result = invoices_server.get_bank_by_bic("099999999")
    payload, code = result if isinstance(result, tuple) else (result, 200)
    check("неизвестный БИК даёт 404", code == 404, str(code))
    check("404 объясняет причину человеку",
          "не найден" in (payload.get_json().get("error") or ""))

with app.test_request_context("/api/invoices/banks/123"):
    result = invoices_server.get_bank_by_bic("123")
    payload, code = result if isinstance(result, tuple) else (result, 200)
    check("огрызок БИК даёт 400", code == 400, str(code))

print()
if failures:
    print(f"ПРОВАЛЕНО {len(failures)}: " + "; ".join(failures))
    sys.exit(1)
print("Все проверки пройдены.")
