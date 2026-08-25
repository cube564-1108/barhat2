"""
Доверие корневому сертификату НУЦ Минцифры («Russian Trusted Root CA»).

Зачем. requests проверяет TLS по бандлу certifi, российского корня там нет и
не будет. Любой сервис, перешедший на сертификат Минцифры, начинает падать ещё
до отправки данных:

    SSLError: certificate verify failed: self-signed certificate in certificate
    chain (_ssl.c:1016)

Формулировка обманчива — «self-signed» относится не к сертификату сервиса, а к
самоподписанному корню в конце цепочки, который процессу просто неизвестен.
Так 2026-08-25 отвалилась отправка счетов в Модульбанк (api.modulbank.ru,
цепочка: *.modulbank.ru → Russian Trusted Sub CA → Russian Trusted Root CA).

Почему не verify=False. Это платёжный API: отключение проверки открывает MITM
на запросах, создающих платёжки. Вместо этого добавляем корень К штатному
бандлу — контекст доверяет certifi И Минцифры сразу, остальные сайты
продолжают проверяться как раньше. Поэтому адаптер безопасно вешать на любую
сессию наперёд, не дожидаясь, пока очередная интеграция сломается.

Почему адаптер, а не verify="путь". У ModulbankClient стоит trust_env=False
(отключает прокси из окружения), а это заодно выключает REQUESTS_CA_BUNDLE и
SSL_CERT_FILE — починить такую сессию через переменные окружения нельзя.
Адаптер работает независимо от trust_env.

Использование:

    from russian_ca import trust_russian_ca

    session = requests.Session()
    trust_russian_ca(session)

Если сломается ещё один российский сервис с другим корнем — класть его PEM
рядом в src/certs/ и добавлять в _CA_FILES.
"""

import os
import ssl
from typing import List

import certifi
import requests
from requests.adapters import HTTPAdapter

_CERTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "certs")

_CA_FILES: List[str] = [
    os.path.join(_CERTS_DIR, "russian_trusted_root_ca.pem"),
]


def build_ssl_context() -> ssl.SSLContext:
    """Штатный контекст проверки (certifi) плюс российские корни."""
    context = ssl.create_default_context(cafile=certifi.where())
    for ca_file in _CA_FILES:
        context.load_verify_locations(cafile=ca_file)
    return context


class RussianCAAdapter(HTTPAdapter):
    """HTTPS-адаптер requests с расширенным списком доверенных корней."""

    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = build_ssl_context()
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        kwargs["ssl_context"] = build_ssl_context()
        return super().proxy_manager_for(*args, **kwargs)


def trust_russian_ca(session: requests.Session) -> requests.Session:
    """Научить сессию доверять корню Минцифры. Возвращает ту же сессию."""
    session.mount("https://", RussianCAAdapter())
    return session
