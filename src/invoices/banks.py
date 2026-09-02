"""
Справочник банков России по БИК (план plans/2026-09-02-справочник-банков-по-бик.md).

Название банка и корреспондентский счёт однозначно выводятся из БИК — это
справочные данные, а не решение человека, и вбивать их руками не нужно. Ошибка
в корсчёте глазами не ловится и всплывает уже в банке, после отправки платёжки.

ИСТОЧНИК. Справочник ЦБ РФ в формате ED807: https://www.cbr.ru/s/newbik —
zip ~110 КБ, без ключа и авторизации, обновляется каждый рабочий день. Это
первоисточник: банки берут корсчёт именно отсюда. У Модульбанка справочника
банков в API нет вовсе (там только account-info, operation-history и
operation-upload/1c), а DaData и агрегаторы перепродают тот же файл, добавляя
ключ, лимит и лишнюю точку отказа.

ПОЧЕМУ ЛОКАЛЬНАЯ ТАБЛИЦА, А НЕ ЗАПРОС НАРУЖУ. Подстановка нужна в момент ввода
БИК, то есть на каждое нажатие клавиши. Живой внешний вызов из обработчика
запроса в этом проекте дважды забирал оба gunicorn-воркера и клал сайт целиком.
Здесь наружу ходит только фоновый синк раз в сутки, а форма делает обычный
SELECT по первичному ключу — и работает, даже когда cbr.ru недоступен.
"""

import io
import logging
import os
import threading
import time
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

from russian_ca import trust_russian_ca

from .storage import get_db
from .cards import (
    try_acquire_sync_lock,
    try_claim_scheduled_run,
    release_sync_lock,
)

logger = logging.getLogger(__name__)

CBR_BIK_URL = "https://www.cbr.ru/s/newbik"
_NS = "{urn:cbr-ru:ed:v2.0}"

# Файл маленький (~110 КБ), но диск /data медленный, а сеть до ЦБ бывает вялой.
DOWNLOAD_TIMEOUT = 60
MAX_ARCHIVE_BYTES = 25 * 1024 * 1024

SYNC_LOCK = "banks_directory"
LOCK_TTL_SECONDS = 10 * 60

# Раз в сутки. Просыпаемся чаще, чем работаем: талон расписания всё равно не
# даст прогону случиться раньше срока, зато после перезапуска воркера (деплой)
# обновление не откладывается на целые сутки.
SCHEDULER_INTERVAL_SECONDS = 24 * 60 * 60
SCHEDULER_WAKEUP_SECONDS = 60 * 60
# Стартовая задержка своя, не совпадающая с чужими (курьеры — 120 с,
# карты — 300 с, МойСклад — 420 с): /data общий на все базы, и синки,
# стартующие одной волной, кладут сайт вместе.
SCHEDULER_START_DELAY_SECONDS = 900

# Насколько справочник считается свежим. ЦБ обновляет его по рабочим дням,
# так что три дня переживают любые выходные и праздники.
STALE_AFTER_DAYS = 3

_scheduler_started = False


def init_banks_table():
    """Создать таблицу справочника. Пустая таблица — это норма: до первого
    прогона синка подстановка просто молчит, форма от этого не ломается."""
    conn = get_db()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS banks (
                bic TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                corr_account TEXT,
                city TEXT,
                is_active INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.commit()
    finally:
        conn.close()


# =============================================================================
# РАЗБОР ED807
# =============================================================================

def parse_ed807(xml_bytes: bytes) -> Dict[str, Any]:
    """
    Разобрать выгрузку ЦБ. Возвращает {"banks": [...], "business_day": "YYYY-MM-DD"}.

    Корсчёт берём ТОЛЬКО из счёта типа CRSA в статусе ACAC. У 455 записей из
    1416 (РКЦ, УФК, подразделения) такого счёта нет вовсе, и подставить вместо
    него счёт другого типа (BANA/TRSA) нельзя — это разные счета, платёжка
    уйдёт не туда. Для таких отдаём название и пустой корсчёт: пусть человек
    впишет сам, зная об этом, чем молча получит чужой номер.

    Живость определяем по PtType и ограничениям, а НЕ по ParticipantStatus:
    у всех 1416 записей он равен PSAC и признаком ничего не является.
    """
    root = ET.fromstring(xml_bytes)

    banks: List[Dict[str, Any]] = []
    for entry in root.findall(f"{_NS}BICDirectoryEntry"):
        bic = (entry.get("BIC") or "").strip()
        info = entry.find(f"{_NS}ParticipantInfo")
        if not bic or info is None:
            continue

        corr_account = None
        for account in entry.findall(f"{_NS}Accounts"):
            if (account.get("RegulationAccountType") == "CRSA"
                    and account.get("AccountStatus") == "ACAC"):
                corr_account = (account.get("Account") or "").strip() or None
                break

        # PtType 90 — конкурсный управляющий или ликвидатор; LWRS — отзыв
        # лицензии. И то и другое означает «платить сюда нельзя», о чём форма
        # обязана предупредить до отправки платёжки, а не после.
        restrictions = {r.get("Rstr") for r in info.findall(f"{_NS}RstrList")}
        is_active = info.get("PtType") != "90" and "LWRS" not in restrictions

        banks.append({
            "bic": bic,
            "name": (info.get("NameP") or "").strip(),
            "corr_account": corr_account,
            "city": (info.get("Nnp") or "").strip() or None,
            "is_active": 1 if is_active else 0,
        })

    return {"banks": banks, "business_day": root.get("BusinessDay") or ""}


def fetch_cbr_directory() -> Dict[str, Any]:
    """
    Скачать и разобрать справочник ЦБ. Исключения не глушим — вызывающая
    сторона решает, оставить ли старые данные.
    """
    session = requests.Session()
    session.trust_env = False
    session.proxies = {"http": None, "https": None, "no_proxy": None}
    # cbr.ru переходит на сертификаты НУЦ Минцифры, корня которых нет в
    # certifi. Без этого запрос падает на проверке TLS с формулировкой про
    # self-signed, не дойдя до сервера (см. src/russian_ca.py).
    trust_russian_ca(session)

    try:
        response = session.get(CBR_BIK_URL, timeout=DOWNLOAD_TIMEOUT)
        response.raise_for_status()
        payload = response.content
    finally:
        session.close()

    # Архив маленький и предсказуемый; неожиданный размер — повод не разжимать
    # его в память, а не повод падать по OOM на проде.
    if len(payload) > MAX_ARCHIVE_BYTES:
        raise RuntimeError(f"ЦБ вернул архив неожиданного размера: {len(payload)} байт")

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = [name for name in archive.namelist() if name.lower().endswith(".xml")]
        if not names:
            raise RuntimeError(f"В архиве ЦБ нет XML: {archive.namelist()}")
        xml_bytes = archive.read(names[0])

    result = parse_ed807(xml_bytes)
    result["file_name"] = names[0]
    return result


def save_banks(banks: List[Dict[str, Any]]) -> int:
    """
    Записать справочник одной транзакцией.

    Именно одной: полторы тысячи отдельных коммитов на сетевом диске /data —
    это полторы тысячи fsync, и на время такой записи встаёт весь сайт, включая
    вход в дашборд (база общая с авторизацией).

    Записи не удаляем, а перезаписываем: банк, исчезнувший из выгрузки, всё ещё
    может стоять в реквизитах старого счёта, и потерять его название значит
    испортить историю.
    """
    if not banks:
        raise ValueError("Пустой справочник — не записываем, старые данные полезнее")

    conn = get_db()
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.executemany(
                "INSERT INTO banks (bic, name, corr_account, city, is_active, updated_at) "
                "VALUES (?, ?, ?, ?, ?, datetime('now')) "
                "ON CONFLICT(bic) DO UPDATE SET name = excluded.name, "
                "corr_account = excluded.corr_account, city = excluded.city, "
                "is_active = excluded.is_active, updated_at = excluded.updated_at",
                [(b["bic"], b["name"], b["corr_account"], b["city"], b["is_active"])
                 for b in banks],
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    return len(banks)


def refresh_banks() -> Dict[str, Any]:
    """Один прогон обновления. Возвращает сводку для интерфейса и логов."""
    data = fetch_cbr_directory()
    saved = save_banks(data["banks"])
    logger.info("[Banks] Справочник ЦБ обновлён: %d записей, выгрузка от %s",
                saved, data["business_day"])
    return {"saved": saved, "business_day": data["business_day"],
            "file_name": data.get("file_name")}


def refresh_banks_locked() -> Dict[str, Any]:
    """Прогон под локом — чтобы два воркера не тянули файл одновременно."""
    if not try_acquire_sync_lock(SYNC_LOCK, LOCK_TTL_SECONDS):
        return {"skipped": "Обновление уже идёт"}
    try:
        return refresh_banks()
    finally:
        release_sync_lock(SYNC_LOCK)


# =============================================================================
# ЧТЕНИЕ
# =============================================================================

def is_valid_bic(value: Any) -> bool:
    """БИК — ровно девять цифр. Проверяем до запроса в базу: по огрызку из
    трёх цифр искать нечего, а форма шлёт значение на каждое нажатие."""
    text = str(value or "").strip()
    return len(text) == 9 and text.isdigit()


def lookup_bank(bic: str) -> Optional[Dict[str, Any]]:
    """Банк по БИК. Только локальный SELECT, наружу не ходим."""
    if not is_valid_bic(bic):
        return None
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT bic, name, corr_account, city, is_active, updated_at "
            "FROM banks WHERE bic = ?", (str(bic).strip(),)
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def get_banks_status() -> Dict[str, Any]:
    """
    Состояние справочника для «Инструментов»: сколько записей и насколько они
    свежие. Без этого поломка синка (ЦБ сменил адрес или формат) никак не
    проявляется — подстановка просто продолжает отдавать данные годичной
    давности, и заметить это нечем.
    """
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS count, MAX(updated_at) AS updated_at FROM banks"
        ).fetchone()
    finally:
        conn.close()

    count = row["count"] if row else 0
    updated_at = row["updated_at"] if row else None

    stale = True
    if updated_at:
        try:
            age = datetime.utcnow() - datetime.strptime(updated_at, "%Y-%m-%d %H:%M:%S")
            stale = age.days >= STALE_AFTER_DAYS
        except ValueError:
            logger.warning("Непонятная дата обновления справочника банков: %r", updated_at)

    return {"count": count, "updated_at": updated_at, "stale": stale,
            "empty": count == 0, "source": CBR_BIK_URL}


# =============================================================================
# ФОНОВОЕ ОБНОВЛЕНИЕ
# =============================================================================

def _scheduler_loop() -> None:
    time.sleep(SCHEDULER_START_DELAY_SECONDS)
    while True:
        try:
            # Талон берём до всякой работы: планировщик крутится в каждом
            # воркере, и без него оба потянули бы файл с разницей в полминуты.
            if try_claim_scheduled_run(SYNC_LOCK, SCHEDULER_INTERVAL_SECONDS):
                refresh_banks_locked()
        except Exception:
            logger.exception("Обновление справочника банков упало на тике")
        time.sleep(SCHEDULER_WAKEUP_SECONDS)


def start_banks_scheduler() -> None:
    """
    Запустить фоновое обновление справочника банков.

    Отключается переменной BANKS_SCHEDULER=0 — локальный прогон иначе полез бы
    в сеть при каждом импорте приложения.
    """
    global _scheduler_started

    if os.getenv("BANKS_SCHEDULER", "1") != "1":
        logger.info("Планировщик справочника банков отключён (BANKS_SCHEDULER=0)")
        return
    if _scheduler_started:
        return
    _scheduler_started = True

    threading.Thread(target=_scheduler_loop, daemon=True, name="banks-scheduler").start()
    logger.info("Планировщик справочника банков запущен (раз в сутки)")
