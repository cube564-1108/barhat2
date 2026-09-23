"""Резервные копии: выгрузка и загрузка баз и вложений.

ЗАЧЕМ. До 23.09.2026 у сервиса не было НИ ОДНОЙ резервной копии: консоли
контейнера на нашем тарифе Amvera нет, эндпоинта выгрузки не было, а значит
любая миграция шла без страховки и любое повреждение файла означало потерю
кассовых смен, счетов и списаний без возможности восстановления.

Второе назначение — переезд. Данные между аккаунтами Amvera иначе не перевезти:
файловый доступ к постоянному диску `/data` есть только у самого приложения
(план `plans/2026-09-15-переезд-дашборда-в-общую-среду.md`, шаг 1).

ЧТО ЗДЕСЬ ОПАСНОГО. Ручка отдаёт боевую базу целиком — одним запросом уходят
все данные, включая хеши паролей из `users`. Поэтому:

- только `@role_required("admin")`;
- каждое скачивание и каждая загрузка пишутся в аудит (кто, что, когда);
- имя базы берётся ТОЛЬКО из белого списка `DATABASES`: параметр маршрута,
  подставленный в путь, — это обход каталога, а `..` фильтром не лечится
  (см. правило про чужой парсер в CLAUDE.md);
- загрузка отказывается писать поверх непустой базы: восстановление поверх
  живых данных — это их потеря, а не восстановление.

ПОЧЕМУ У СКАЧИВАНИЯ НЕТ `@require_ajax_header`. Декоратор защищает от
межсайтовой подделки ЗАПИСИ. Здесь запись только у `POST /restore`, он им и
закрыт. Чужой сайт может заставить браузер админа скачать файл, но прочитать
ответ он не сможет — межсайтовое чтение запрещено самим браузером. Зато без
заголовка работает обычная ссылка, и файл течёт сразу на диск, а не собирается
в памяти вкладки.

ПОЧЕМУ `Connection.backup()`, А НЕ КОПИЯ ФАЙЛА. Базы работают в режиме WAL:
часть уже принятых записей лежит в соседнем `-wal` и в сам файл ещё не попала.
Скопированный на ходу файл приезжает без них и выглядит целым — повреждение
обнаружится в момент восстановления, то есть в худший из возможных.
`backup()` снимает согласованный снимок штатным механизмом SQLite.
"""

import os
import time
import sqlite3
import zipfile
import tempfile
import logging
from datetime import datetime

from flask import Blueprint, Response, jsonify, request
from flask_login import current_user

from auth import role_required, log_action, require_ajax_header
from sqlite_conn import connect as sqlite_connect
from storage_paths import resolve as resolve_data_path

logger = logging.getLogger("barhat.backup")

backup_bp = Blueprint("backup", __name__, url_prefix="/api/backup")


# Белый список: ключ из URL → (переменная окружения, имя файла на /data).
# Пути резолвятся так же, как их резолвят сами модули, — выгружать надо ровно
# тот файл, в который приложение пишет, а не собранный заново из догадок.
DATABASES = {
    "barhat": ("BARHAT_DB_PATH", "barhat.db"),
    "couriers": ("COURIERS_DB_PATH", "couriers.db"),
    "pyrus": ("PYRUS_DB_PATH", "pyrus.db"),
    "moysklad": ("MOYSKLAD_DB_PATH", "moysklad.db"),
    "linkwatch": ("LINKWATCH_DB_PATH", "linkwatch.db"),
}

# Что в какой базе лежит — подпись для человека в интерфейсе. Без неё
# непонятно, что обязательно к бэкапу, а что восстановимо синхронизацией.
DATABASE_NOTES = {
    "barhat": "учётки и права, счета, кассовые смены, списания — терять нельзя",
    "couriers": "доставки, брони, выплаты курьерам — терять нельзя",
    "pyrus": "задачи и витрина качества сборки",
    "moysklad": "зеркало МойСклада — восстановимо синхронизацией",
    "linkwatch": "проверка ссылок на товары — восстановимо проверкой",
}

ATTACHMENT_DIRS = {
    "invoices": ("invoices.storage", "вложения к счетам"),
    "writeoffs": ("writeoffs.storage", "фото к списаниям"),
}


def _db_path(name: str) -> str:
    env_var, file_name = DATABASES[name]
    return resolve_data_path(env_var, file_name)


def _attachments_dir(key: str) -> str:
    import importlib

    module_path, _ = ATTACHMENT_DIRS[key]
    module = importlib.import_module(module_path)
    return os.path.abspath(module.ATTACHMENTS_DIR)


def _dir_stats(directory: str):
    """Число файлов и объём папки. Пустой ответ, если папки ещё нет."""
    if not os.path.isdir(directory):
        return {"exists": False, "files": 0, "size_mb": 0}
    files = 0
    size = 0
    for dirpath, _dirnames, filenames in os.walk(directory):
        for name in filenames:
            try:
                size += os.path.getsize(os.path.join(dirpath, name))
                files += 1
            except OSError:
                continue
    return {"exists": True, "files": files, "size_mb": round(size / 1024 / 1024, 2)}


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning(f"Временный файл не удалился: {path}: {e}")


def _sweep_stale(directory: str, prefix: str, older_than_sec: int = 3600) -> None:
    """Убрать временные файлы, брошенные прошлыми вызовами.

    Удаление после отдачи покрывает штатный путь, но не покрывает падение
    воркера или обрыв закачки посреди гигабайтного файла. Без уборки такие
    хвосты копятся на `/data` и однажды не оставляют места самой базе —
    молча, потому что смотреть туда некому: консоли на тарифе нет.
    """
    try:
        now = time.time()
        for name in os.listdir(directory):
            if not name.startswith(prefix):
                continue
            full = os.path.join(directory, name)
            try:
                if now - os.path.getmtime(full) > older_than_sec:
                    os.remove(full)
                    logger.info(f"Убран брошенный временный файл: {full}")
            except OSError:
                continue
    except OSError as e:
        logger.warning(f"Не удалось осмотреть {directory} на предмет хвостов: {e}")


def _send_and_remove(path: str, download_name: str, mimetype: str) -> Response:
    """Отдать файл потоком и удалить его, когда поток закончился.

    Удаление живёт в `finally` генератора, а не на событии ответа. Так оно
    отрабатывает при любом исходе: отдали до конца, клиент оборвал закачку
    (GeneratorExit), упали в середине. Варианты через `after_this_request` и
    `call_on_close` зависят от того, кто и когда закроет ответ, и на Windows
    спотыкались о «файл занят» — а на Linux молча оставляли бы на `/data`
    копию базы после каждого скачивания.

    Отдаём кусками по мегабайту: базы измеряются гигабайтами, и читать их
    в память воркера целиком нельзя — воркеров всего два.
    """
    size = os.path.getsize(path)

    def stream():
        try:
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk:
                        break
                    yield chunk
        finally:
            _remove_quietly(path)

    response = Response(stream(), mimetype=mimetype)
    response.headers["Content-Length"] = str(size)
    # filename* по RFC 5987 — в именах наших файлов только латиница и цифры,
    # но заголовок обязан быть корректным и для будущих имён.
    response.headers["Content-Disposition"] = f'attachment; filename="{download_name}"'
    return response


def _free_bytes(path: str) -> int:
    """Свободное место на том же томе, где лежит путь."""
    target = path if os.path.exists(path) else os.path.dirname(os.path.abspath(path)) or "."
    try:
        usage = os.statvfs(target)  # type: ignore[attr-defined]
        return usage.f_bavail * usage.f_frsize
    except (AttributeError, OSError):
        # Windows (локальная разработка) — проверку места пропускаем, там
        # снимок делается по копии, а не по боевой базе.
        import shutil

        try:
            return shutil.disk_usage(target).free
        except OSError:
            return -1


@backup_bp.route("/targets", methods=["GET"])
@role_required("admin")
def list_targets():
    """Что можно выгрузить: базы и папки вложений с размерами.

    Объём здесь нужен не для красоты: `moysklad.db` больше гигабайта, и
    скачивать его тем же движением, что базу на полмегабайта, — разные по
    цене операции. Человек должен видеть это до нажатия.
    """
    databases = []
    for name in DATABASES:
        path = _db_path(name)
        exists = os.path.exists(path)
        databases.append({
            "name": name,
            "note": DATABASE_NOTES.get(name, ""),
            "path": path,
            "exists": exists,
            "size_mb": round(os.path.getsize(path) / 1024 / 1024, 2) if exists else 0,
            "persistent": os.path.abspath(path).replace(os.sep, "/").startswith("/data"),
        })

    attachments = []
    for key, (_module_path, note) in ATTACHMENT_DIRS.items():
        try:
            directory = _attachments_dir(key)
        except Exception as e:
            attachments.append({"name": key, "note": note, "error": f"{type(e).__name__}: {e}"})
            continue
        stats = _dir_stats(directory)
        stats.update({"name": key, "note": note, "path": directory})
        attachments.append(stats)

    return jsonify({"databases": databases, "attachments": attachments})


@backup_bp.route("/db/<name>", methods=["GET"])
@role_required("admin")
def download_database(name):
    """Согласованный снимок базы через `Connection.backup()`."""
    if name not in DATABASES:
        return jsonify({"error": "Неизвестная база"}), 404

    path = _db_path(name)
    if not os.path.exists(path):
        return jsonify({"error": f"Файл базы не найден: {path}"}), 404

    size = os.path.getsize(path)
    free = _free_bytes(path)
    # Снимок кладём рядом с оригиналом — тот же том, и у SQLite не возникает
    # вопроса, куда писать. Значит нужно место ещё на одну такую базу.
    if 0 <= free < size * 1.2:
        return jsonify({
            "error": "Недостаточно места для снимка",
            "detail": f"нужно ≈{round(size * 1.2 / 1024 / 1024)} МБ, свободно {round(free / 1024 / 1024)} МБ",
        }), 507

    stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
    directory = os.path.dirname(os.path.abspath(path)) or "."
    _sweep_stale(directory, ".backup-")
    fd, tmp_path = tempfile.mkstemp(prefix=f".backup-{name}-", suffix=".db", dir=directory)
    os.close(fd)

    source = None
    target = None
    try:
        source = sqlite_connect(path, timeout=30)
        # Приёмник — обычный sqlite3.connect намеренно, в обход sqlite_conn:
        # это не рабочая база, а выходной файл. sqlite_conn включил бы WAL и
        # создал рядом -wal/-shm, которые пришлось бы отдавать следом, иначе
        # снимок оказался бы неполным.
        target = sqlite3.connect(tmp_path)
        source.backup(target)
        target.close()
        target = None
        source.close()
        source = None
    except Exception as e:
        logger.error(f"Снимок базы {name} не снялся: {type(e).__name__}: {e}")
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return jsonify({"error": "Не удалось снять снимок базы", "detail": f"{type(e).__name__}: {e}"}), 500
    finally:
        # Оба соединения закрываются при любом исходе: незакрытое соединение с
        # неоткатанной транзакцией держит write-лок общей базы до перезапуска
        # воркера, и после этого встаёт вход в дашборд (было 19.08.2026).
        for conn in (target, source):
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    log_action(current_user.username, "backup_download",
               f"база {name}, {round(size / 1024 / 1024, 2)} МБ")
    return _send_and_remove(tmp_path, f"{name}-{stamp}.db", "application/x-sqlite3")


@backup_bp.route("/attachments/<key>", methods=["GET"])
@role_required("admin")
def download_attachments(key):
    """Папка вложений одним zip.

    Вложения — это не «приложение к базе», а половина смысла записи: счёт без
    скана и списание без фото не проводятся. Бэкап только баз оставил бы
    записи, которые нельзя ни открыть, ни согласовать (так уже терялись
    вложения 23.08.2026 — из-за относительного пути).
    """
    if key not in ATTACHMENT_DIRS:
        return jsonify({"error": "Неизвестная папка"}), 404

    try:
        directory = _attachments_dir(key)
    except Exception as e:
        return jsonify({"error": "Не удалось определить путь", "detail": f"{type(e).__name__}: {e}"}), 500

    if not os.path.isdir(directory):
        return jsonify({"error": f"Папка не найдена: {directory}"}), 404

    stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
    _sweep_stale(directory, ".backup-")
    fd, tmp_path = tempfile.mkstemp(prefix=f".backup-{key}-", suffix=".zip", dir=directory)
    os.close(fd)

    files = 0
    try:
        # ZIP_STORED, а не DEFLATE: внутри jpeg и pdf, они уже сжаты. Сжимать
        # их повторно — это минуты работы воркера ради процента объёма.
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_STORED, allowZip64=True) as archive:
            for dirpath, _dirnames, filenames in os.walk(directory):
                for name in sorted(filenames):
                    full = os.path.join(dirpath, name)
                    if os.path.abspath(full) == os.path.abspath(tmp_path):
                        continue  # свой же временный архив внутрь не кладём
                    try:
                        archive.write(full, os.path.relpath(full, directory))
                        files += 1
                    except OSError as e:
                        logger.warning(f"Файл пропущен при архивации: {full}: {e}")
    except Exception as e:
        logger.error(f"Архив вложений {key} не собрался: {type(e).__name__}: {e}")
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return jsonify({"error": "Не удалось собрать архив", "detail": f"{type(e).__name__}: {e}"}), 500

    log_action(current_user.username, "backup_download", f"вложения {key}, файлов {files}")
    return _send_and_remove(tmp_path, f"{key}-attachments-{stamp}.zip", "application/zip")


@backup_bp.route("/restore/<name>", methods=["POST"])
@role_required("admin")
@require_ajax_header
def restore_database(name):
    """Загрузка базы на ПУСТОЙ экземпляр — приёмная сторона переезда.

    Поверх непустой базы не пишем никогда. «Восстановить поверх работающего»
    звучит как спасение, а означает замену живых данных на снимок неизвестной
    свежести — и прежние данные после этого взять уже негде.
    """
    if name not in DATABASES:
        return jsonify({"error": "Неизвестная база"}), 404

    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return jsonify({"error": "Файл не передан"}), 400

    target_path = _db_path(name)
    if os.path.exists(target_path) and os.path.getsize(target_path) > 0:
        return jsonify({
            "error": "База уже существует и не пуста",
            "detail": f"{target_path} — {round(os.path.getsize(target_path) / 1024 / 1024, 2)} МБ. "
                      "Загрузка разрешена только на пустой экземпляр.",
        }), 409

    directory = os.path.dirname(os.path.abspath(target_path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=f".restore-{name}-", suffix=".db", dir=directory)
    os.close(fd)

    # try/finally на весь разбор: каждый отказ ниже — это ранний выход, и без
    # finally временный файл оставался бы на диске после КАЖДОЙ неудачной
    # попытки. На /data, где место считанное, это накопительная утечка.
    try:
        upload.save(tmp_path)

        # Пустой файл доезжает при обрыве связи и выглядит как успешная
        # загрузка — проверяем размер до всего остального.
        if os.path.getsize(tmp_path) == 0:
            return jsonify({"error": "Файл пустой — загрузка оборвалась"}), 400

        with open(tmp_path, "rb") as f:
            if f.read(16) != b"SQLite format 3\x00":
                return jsonify({"error": "Это не файл базы SQLite"}), 400

        # На повреждённом файле integrity_check не возвращает вердикт, а
        # бросает DatabaseError — это отказ приёма (400), а не наша ошибка
        # (500): человеку надо понять, что файл битый, и взять другой.
        probe = sqlite3.connect(tmp_path)
        try:
            verdict = probe.execute("PRAGMA integrity_check").fetchone()[0]
            tables = probe.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
        except sqlite3.DatabaseError as e:
            return jsonify({"error": "Файл повреждён", "detail": str(e)}), 400
        finally:
            probe.close()

        if verdict != "ok":
            return jsonify({"error": "Файл повреждён", "detail": verdict}), 400
        if tables == 0:
            return jsonify({"error": "В файле нет ни одной таблицы"}), 400

        os.replace(tmp_path, target_path)
    except Exception as e:
        logger.error(f"Загрузка базы {name} не удалась: {type(e).__name__}: {e}")
        return jsonify({"error": "Не удалось загрузить базу", "detail": f"{type(e).__name__}: {e}"}), 500
    finally:
        # После os.replace файла уже нет — _remove_quietly это переживает.
        _remove_quietly(tmp_path)

    size_mb = round(os.path.getsize(target_path) / 1024 / 1024, 2)
    log_action(current_user.username, "backup_restore", f"база {name}, {size_mb} МБ, таблиц {tables}")
    logger.info(f"База {name} загружена: {target_path}, {size_mb} МБ, таблиц {tables}")
    return jsonify({"ok": True, "name": name, "path": target_path,
                    "size_mb": size_mb, "tables": tables})
