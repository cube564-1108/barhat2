"""
Единственное место, где решается, где физически лежат данные.

На Amvera постоянный диск смонтирован в /data (persistenceMount в amvera.yml),
а /app — это код контейнера: он пересоздаётся при КАЖДОЙ сборке. Любой путь
мимо /data (относительный или просто чужой) означает потерю данных на
следующем деплое.

Так уже терялись:
- база Pyrus (2026-08-18) — в панели Amvera стояло PYRUS_DB_PATH=data/pyrus.db,
  относительный путь разворачивался в /app/data/pyrus.db;
- вложения к счетам и списаниям (2026-08-23) — дефолт в коде был относительный
  ("invoice_attachments"), запись в БД оставалась, а файл исчезал.

Поэтому логика живёт здесь, а не в точке входа: модули зовут resolve() при
импорте и получают верный путь независимо от того, как запущен процесс —
gunicorn через app.py, `python -m pyrus.server`, скрипт из scripts/ или тест.

ЛЮБАЯ новая фича, которая пишет файлы (загрузки, выгрузки, кэши), обязана
получать свой путь отсюда. Относительный дефолт — это не стиль, а потеря данных.
"""

import os

PERSISTENT_DIR = '/data'

# Признак боевого окружения — примонтированный /data на posix-системе.
# Только os.path.isdir('/data') мало: на Windows этот путь резолвится в
# C:\data, и случайная локальная папка с таким именем переписала бы пути
# разработчика.
IS_PERSISTENT_MOUNT = os.name == 'posix' and os.path.isdir(PERSISTENT_DIR)


def resolve(env_var: str, name: str) -> str:
    """Путь к данным, гарантированно лежащий на постоянном диске (на проде).

    env_var — переменная окружения с путём (может быть задана в панели Amvera),
    name — имя файла или папки внутри /data.

    На проде значение из env принимается, только если оно и так ведёт на /data;
    иначе принудительно заменяется. Локально путь разработчика не трогаем.
    """
    persistent = f'{PERSISTENT_DIR}/{name}'
    current = os.environ.get(env_var)

    if not IS_PERSISTENT_MOUNT:
        return current or persistent

    # Своя подпапка внутри /data — тоже постоянный диск, такой путь уважаем.
    # Сравниваем через '/', а не os.sep: /data существует только на posix
    # (см. IS_PERSISTENT_MOUNT), и тесты не должны зависеть от платформы.
    if current and (
        current == persistent
        or os.path.abspath(current).replace(os.sep, '/').startswith(PERSISTENT_DIR + '/')
    ):
        return current

    if current:
        print(
            f"WARNING: {env_var}={current} ведёт мимо постоянного диска "
            f"{PERSISTENT_DIR} — данные терялись бы при каждой сборке. "
            f"Использую {persistent}"
        )
    return persistent


def force_env(env_var: str, name: str) -> str:
    """То же, что resolve(), но ещё и переписывает переменную окружения.

    Нужно там, где путь читают напрямую из os.environ (пути к базам разбросаны
    по модулям), — чтобы все увидели уже исправленное значение.
    """
    path = resolve(env_var, name)
    os.environ[env_var] = path
    return path
