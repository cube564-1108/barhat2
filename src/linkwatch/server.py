"""
JSON API сторожа ссылок на товары.

Blueprint регистрируется в src/pyrus/server.py — тот же паттерн, что
couriers_bp / moysklad_bp / writeoffs_bp.

Проверка ходит по всему сайту и занимает около получаса, поэтому она НИКОГДА
не выполняется внутри HTTP-запроса: живые внешние вызовы из обработчика уже
дважды укладывали прод (воркеров всего 2). Обработчики читают только результат
последнего прогона из локальной базы, а сам прогон делает фоновый поток под локом.
"""

import csv
import io
import logging
import os
import sys
import threading
import time
from datetime import datetime

from flask import Blueprint, Response, jsonify

auth_path = os.path.join(os.path.dirname(__file__), '../')
sys.path.insert(0, auth_path)
from auth import section_required  # noqa: E402

from . import checker, storage  # noqa: E402

logger = logging.getLogger(__name__)

linkwatch_bp = Blueprint("linkwatch", __name__, url_prefix="/api/linkwatch")

SECTION = "link_watch"

# ============================================================================
# Параметры прогона
# ============================================================================

LOCK_NAME = "link_check"

# Прогон идёт ~30 минут. TTL с запасом, плюс продление по ходу: если воркер
# умрёт посреди обхода (деплой, OOM), лок освободится сам и не заблокирует
# проверку навсегда.
LOCK_TTL_SECONDS = 3600
LOCK_RENEW_INTERVAL_SECONDS = 60

# Своя стартовая задержка, не совпадающая с чужими (moysklad 420, couriers 120):
# иначе после каждого деплоя все фоновые задачи стартуют одной волной по общему
# медленному диску /data, и сайт встаёт у всех сразу.
SCHEDULER_START_DELAY_SECONDS = 300

# Тик раз в час, но прогон — только ночью и не чаще раза в сутки (талон ниже).
SCHEDULER_TICK_SECONDS = 3600
DAILY_CLAIM_SECONDS = 20 * 60 * 60

# Полчаса запросов к сайту — это заметная нагрузка на витрину, поэтому только
# ночью. Прод живёт в UTC, салоны в UTC+5/+7: 22–23 UTC это глубокая ночь в
# Москве и раннее утро на востоке, когда заказов нет.
NIGHT_HOURS_UTC = (22, 23)

_scheduler_started = False
_run_thread = None


# ============================================================================
# Прогон
# ============================================================================

def _run_check_locked(trigger: str) -> None:
    """Выполнить проверку под локом. Зовётся только из фонового потока."""
    if not storage.try_acquire_lock(LOCK_NAME, LOCK_TTL_SECONDS):
        logger.info("Проверка ссылок уже идёт — пропускаю запуск (%s)", trigger)
        return

    run_id = storage.start_run()
    last_renew = time.monotonic()

    def on_progress(done: int, total: int) -> None:
        # Продлеваем лок по ВРЕМЕНИ, а не после каждой ссылки: иначе это
        # отдельный UPDATE с commit на общий диск /data по нескольку раз в минуту.
        nonlocal last_renew
        if time.monotonic() - last_renew >= LOCK_RENEW_INTERVAL_SECONDS:
            storage.renew_lock(LOCK_NAME, LOCK_TTL_SECONDS)
            last_renew = time.monotonic()
            logger.info("Проверка ссылок: %d из %d", done, total)

    try:
        result = checker.run_check(on_progress=on_progress)
        storage.finish_run(run_id, result["counts"], result["broken"])
        logger.info(
            "Проверка ссылок завершена (%s): битых %d из %d",
            trigger, result["counts"]["broken"], result["counts"]["checked"],
        )
    except Exception as e:
        logger.error("Проверка ссылок сорвалась (%s): %s", trigger, e)
        storage.fail_run(run_id, str(e))
    finally:
        storage.release_lock(LOCK_NAME)


def _start_background_run(trigger: str) -> bool:
    """Запустить прогон в фоне. False — уже идёт."""
    global _run_thread

    if _run_thread and _run_thread.is_alive():
        return False

    _run_thread = threading.Thread(
        target=_run_check_locked, args=(trigger,), daemon=True, name="linkwatch-run"
    )
    _run_thread.start()
    return True


# ============================================================================
# Планировщик
# ============================================================================

def _scheduler_loop() -> None:
    time.sleep(SCHEDULER_START_DELAY_SECONDS)

    while True:
        try:
            if datetime.utcnow().hour in NIGHT_HOURS_UTC:
                # Талон берётся ДО работы: лок ловит только одновременный прогон,
                # а тики двух воркеров разъезжаются во времени, и второй повторил
                # бы получасовой обход сайта следом за первым.
                if storage.try_claim_scheduled_run("daily", DAILY_CLAIM_SECONDS):
                    _run_check_locked("расписание")
        except Exception as e:
            logger.error("Ошибка планировщика сторожа ссылок: %s", e)

        time.sleep(SCHEDULER_TICK_SECONDS)


def start_scheduler() -> None:
    """
    Запустить ежедневную проверку.

    Отключается LINKWATCH_SCHEDULER=0 — на локальной машине лезть в боевую CRM
    и полчаса ходить по живому сайту при каждом запуске сервера незачем.
    """
    global _scheduler_started

    if os.getenv("LINKWATCH_SCHEDULER", "1") != "1":
        logger.info("Планировщик сторожа ссылок отключён (LINKWATCH_SCHEDULER=0)")
        return
    if _scheduler_started:
        return
    _scheduler_started = True

    threading.Thread(target=_scheduler_loop, daemon=True, name="linkwatch-scheduler").start()
    logger.info("Планировщик сторожа ссылок запущен (ночью, раз в сутки)")


# ============================================================================
# API
# ============================================================================

@linkwatch_bp.route("/status", methods=["GET"])
@section_required(SECTION)
def get_status():
    """Сводка последнего прогона — то, что видно на экране сразу."""
    last = storage.get_last_run()
    running = storage.get_running()

    return jsonify({
        "configured": checker.is_configured(),
        "running": bool(running) or bool(_run_thread and _run_thread.is_alive()),
        "last_run": last,
        "history": storage.get_history(),
    })


@linkwatch_bp.route("/broken", methods=["GET"])
@section_required(SECTION)
def get_broken():
    """Проблемные товары последнего прогона."""
    last = storage.get_last_run()
    if not last:
        return jsonify({"items": [], "titles": storage.STATUS_TITLES})

    return jsonify({
        "run_id": last["id"],
        "items": storage.get_broken(last["id"]),
        "titles": storage.STATUS_TITLES,
    })


@linkwatch_bp.route("/run", methods=["POST"])
@section_required(SECTION)
def run_now():
    """Запустить проверку вручную. Ответ приходит сразу, прогон идёт в фоне."""
    if not checker.is_configured():
        return jsonify({"error": "RetailCRM не настроена"}), 400

    if not _start_background_run("вручную"):
        return jsonify({"error": "Проверка уже идёт"}), 409

    return jsonify({"started": True})


@linkwatch_bp.route("/export", methods=["GET"])
@section_required(SECTION)
def export_csv():
    """
    Выгрузка проблемных товаров.

    CSV, а не xlsx: ради одной кнопки не тащим на прод openpyxl. Excel открывает
    файл сам — для этого нужны BOM (иначе кириллица превращается в кракозябры)
    и точка с запятой как разделитель (русская локаль Excel так и ждёт).
    Полноценная xlsx-выгрузка есть в scripts/export_broken_urls.py.
    """
    last = storage.get_last_run()
    items = storage.get_broken(last["id"]) if last else []

    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", quoting=csv.QUOTE_MINIMAL)
    writer.writerow([
        "Артикул", "Наименование", "Что не так",
        "Ссылка сейчас (в CRM)", "Правильная ссылка", "ID в CRM",
    ])
    for item in items:
        writer.writerow([
            item.get("article") or "",
            item.get("name") or "",
            storage.STATUS_TITLES.get(item.get("status"), item.get("status")),
            item.get("url") or "",
            item.get("canonical_url") or "",
            item.get("product_id") or "",
        ])

    stamp = datetime.utcnow().strftime("%Y-%m-%d")
    return Response(
        buffer.getvalue().encode("utf-8-sig"),
        mimetype="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="ssylki-tovarov-{stamp}.csv"'
        },
    )
