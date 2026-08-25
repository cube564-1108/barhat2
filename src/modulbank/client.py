"""
Модульбанк API клиент.

Официальная документация api.modulbank.ru непригодна для прямого разбора —
та же ситуация, что с ПланФакт (см. src/planfact/client.py). Схема запроса
подтверждена дословно исходным кодом рабочей Python-обёртки
github.com/Otetz/modulbank (modulbank/client.py:create_payment_draft).

Авторизация: заголовок Authorization: Bearer <токен> (НЕ X-ApiKey, как у
ПланФакт). Тело запроса — не JSON-структура платежа, а классический
1С-обмен (1CClientBankExchange) целиком текстом внутри поля "document"
(см. document.py). Ответ: {"totalLoaded": int, "errors": [str, ...]}.

sandbox_mode=True добавляет заголовок "sandbox: on" — банк создаёт черновик
в тестовом контуре, он ни на что не влияет. Используем для проверки перед
первой боевой отправкой (см. план, Фаза 5).

Один токен обслуживает весь кабинет Модульбанка сразу со всеми компаниями
владельца — счёт списания (плательщик) определяется реквизитами внутри
самого документа, а не токеном. См. src/invoices/storage.py —
get_payer_bank_requisites() берёт реквизиты нужной компании из справочника
плательщиков ("На кого выставлен счёт"), а не из окружения.
"""

import logging
import os
from typing import Any, Dict, Optional

from dotenv import load_dotenv
import requests

from russian_ca import trust_russian_ca

from .document import build_1c_payment_document

load_dotenv()

logger = logging.getLogger(__name__)

MODULBANK_API_URL = "https://api.modulbank.ru/v1/"


class ModulbankClient:
    """Клиент для загрузки черновиков платёжек в Модульбанк."""

    def __init__(self, token: Optional[str] = None, sandbox_mode: bool = False):
        self.token = token or os.getenv("MODULBANK_API_TOKEN")
        if not self.token:
            raise ValueError(
                "Не указан MODULBANK_API_TOKEN. Установите переменную окружения "
                "в .env в корне проекта или передайте в конструктор."
            )
        self.sandbox_mode = sandbox_mode
        self.api_url = MODULBANK_API_URL
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.proxies = {"http": None, "https": None, "no_proxy": None}
        # api.modulbank.ru подписан корнем НУЦ Минцифры, которого нет в certifi:
        # без этого запрос падает на проверке TLS, не дойдя до банка. См.
        # src/russian_ca.py — там же объяснение, почему не verify=False.
        trust_russian_ca(self.session)

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.sandbox_mode:
            headers["sandbox"] = "on"
        return headers

    def create_payment_draft(self, document_text: str) -> Dict[str, Any]:
        """
        Загрузить платёжку в формате 1С (POST /v1/operation-upload/1c).
        Банк всегда создаёт статус "Черновик" — подписание доступно только
        вручную в личном кабинете, ничего не списывается автоматически.

        Возвращает {"ok": bool, "total_loaded": int, "errors": [str], "raw": dict|None}.
        """
        url = f"{self.api_url}operation-upload/1c"
        try:
            response = self.session.post(
                url, json={"document": document_text}, headers=self._headers(), timeout=30,
            )
            response.raise_for_status()
            body = response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Ошибка запроса к Модульбанк (operation-upload/1c): {e}")
            if hasattr(e, "response") and e.response is not None:
                logger.error(f"Response: {e.response.text[:500]}")
            return {"ok": False, "total_loaded": 0, "errors": [str(e)], "raw": None}

        errors = body.get("errors") or []
        total_loaded = body.get("totalLoaded", 0)
        return {"ok": total_loaded > 0 and not errors, "total_loaded": total_loaded, "errors": errors, "raw": body}

    def send_invoice_payment(
        self,
        doc_num: str,
        date: str,
        amount: float,
        purpose: str,
        payer: Dict[str, Optional[str]],
        recipient: Dict[str, Optional[str]],
    ) -> Dict[str, Any]:
        """Собрать документ 1С из реквизитов счёта и загрузить черновик в банк."""
        document_text = build_1c_payment_document(
            doc_num=doc_num, date=date, amount=amount, purpose=purpose,
            payer=payer, recipient=recipient,
        )
        result = self.create_payment_draft(document_text)
        result["document"] = document_text
        return result


def get_client(sandbox_mode: bool = False, token: Optional[str] = None) -> ModulbankClient:
    return ModulbankClient(token=token, sandbox_mode=sandbox_mode)
