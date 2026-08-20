"""
Сборка платёжного поручения в текстовом формате 1CClientBankExchange для
Модульбанк API (POST /v1/operation-upload/1c, поле "document").

Официальная документация api.modulbank.ru недоступна для прямого разбора
(та же ситуация, что с ПланФакт — см. src/planfact/client.py). Набор и
порядок полей ниже подтверждены дословно исходным кодом рабочей Python-
библиотеки github.com/Otetz/modulbank (modulbank/client_bank_exchange.py —
классы GeneralSection/FilterSection/DocumentSection, и
modulbank/client.py:__fill_client_bank_exchange — какие поля реально
заполняются перед отправкой).
"""

import datetime
from decimal import Decimal, ROUND_HALF_DOWN
from typing import Dict, Optional

# Modulbank работает по московскому времени; фиксированный сдвиг вместо pytz
# — в РФ нет перехода на летнее время с 2014 года, лишняя зависимость не нужна
_MOSCOW_TZ = datetime.timezone(datetime.timedelta(hours=3))

# Порядок и состав — как в DocumentSection._mandatory_fields исходной
# библиотеки: банк ожидает видеть эти поля даже пустыми, не только
# заполненные.
_MANDATORY_DOCUMENT_FIELDS = (
    "Номер", "Дата", "Сумма", "ПлательщикСчет", "Плательщик", "ПлательщикИНН",
    "Плательщик1", "ПлательщикРасчСчет", "ПлательщикБанк1", "ПлательщикБИК",
    "ПлательщикКорсчет", "ПолучательСчет", "Получатель", "ПолучательИНН",
    "Получатель1", "ПолучательРасчСчет", "ПолучательБанк1", "ПолучательБИК",
    "ПолучательКорсчет", "ВидОплаты", "ВидПлатежа", "СтатусСоставителя",
    "ПлательщикКПП", "ПолучательКПП", "ПоказательКБК", "ОКАТО",
    "ПоказательОснования", "ПоказательПериода", "ПоказательНомера", "ПоказательДаты",
)


def _fmt_date(value) -> str:
    if isinstance(value, str):
        value = datetime.datetime.strptime(value, "%Y-%m-%d").date()
    return value.strftime("%d.%m.%Y")


def _fmt_amount(amount) -> str:
    return str(Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_DOWN))


def build_1c_payment_document(
    doc_num: str,
    date: str,
    amount: float,
    purpose: str,
    payer: Dict[str, Optional[str]],
    recipient: Dict[str, Optional[str]],
    payment_type: str = "01",
    priority: str = "5",
) -> str:
    """
    payer/recipient: {"name", "inn", "kpp", "account", "bank_name", "bank_bik",
    "bank_corr_account"}. date — "YYYY-MM-DD". Возвращает готовый текст
    документа для поля "document" в теле запроса Модульбанка.
    """
    # Дату и время берём из одного московского момента. Раньше дата бралась
    # через date.today() — то есть по поясу сервера (на Amvera это UTC), и с
    # 00:00 до 03:00 по Москве документ уходил с вчерашней ДатойСоздания при
    # сегодняшнем ВремениСоздания.
    moscow_now = datetime.datetime.now(_MOSCOW_TZ)
    today = moscow_now.date()
    now = moscow_now.time()
    date_str = _fmt_date(date)

    values = {
        "Номер": doc_num,
        "Дата": date_str,
        "Сумма": _fmt_amount(amount),
        "ПлательщикСчет": payer["account"],
        "ДатаСписано": date_str,
        "Плательщик": f"{payer['inn']} {payer['name']}",
        "ПлательщикИНН": payer["inn"],
        "Плательщик1": payer["name"],
        "ПлательщикРасчСчет": payer["account"],
        "ПлательщикБанк1": payer["bank_name"],
        "ПлательщикБИК": payer["bank_bik"],
        "ПлательщикКорсчет": payer["bank_corr_account"],
        "ПлательщикКПП": payer.get("kpp") or "",
        "ПолучательСчет": recipient["account"],
        "Получатель": recipient["name"],
        "ПолучательИНН": recipient["inn"],
        "Получатель1": recipient["name"],
        "ПолучательРасчСчет": recipient["account"],
        "ПолучательБанк1": recipient["bank_name"],
        "ПолучательБИК": recipient["bank_bik"],
        "ПолучательКорсчет": recipient["bank_corr_account"],
        "ПолучательКПП": recipient.get("kpp") or "",
        "ВидОплаты": payment_type,
        "Очередность": priority,
        "НазначениеПлатежа": purpose,
        "НазначениеПлатежа1": purpose,
    }

    lines = [
        "1CClientBankExchange",
        "ВерсияФормата=1.02",
        "Кодировка=Windows",
        "Отправитель=barhat-invoices",
        f"ДатаСоздания={today.strftime('%d.%m.%Y')}",
        f"ВремяСоздания={now.strftime('%H:%M:%S')}",
        f"ДатаНачала={today.strftime('%d.%m.%Y')}",
        f"ДатаКонца={today.strftime('%d.%m.%Y')}",
        f"РасчСчет={payer['account']}",
        "СекцияДокумент=Платежное поручение",
    ]
    for key in _MANDATORY_DOCUMENT_FIELDS:
        lines.append(f"{key}={values.get(key, '')}")
    for key, value in values.items():
        if key not in _MANDATORY_DOCUMENT_FIELDS and value:
            lines.append(f"{key}={value}")
    lines.append("КонецДокумента")
    lines.append("КонецФайла")

    return "\n".join(lines)
