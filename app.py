"""
Entry point for Amvera deployment
Imports Flask app from pyrus.server
"""
import sys
import os

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

# Set production environment
os.environ.setdefault('FLASK_ENV', 'production')

# Пути к базам данных.
#
# На Amvera постоянный диск смонтирован в /data (persistenceMount в amvera.yml),
# а /app — это код контейнера: он пересоздаётся при каждой сборке. Раньше здесь
# стоял setdefault, и заданная в панели Amvera переменная PYRUS_DB_PATH со
# значением "data/pyrus.db" (скопированным из локального .env) молча
# разворачивалась в /app/data/pyrus.db — база Pyrus жила на эфемерном диске и
# обнулялась каждым деплоем. Поэтому, если /data существует, относительный или
# любой другой путь мимо постоянного диска принудительно исправляем.
PERSISTENT_DIR = '/data'
DB_FILES = {
    'PYRUS_DB_PATH': 'pyrus.db',
    'BARHAT_DB_PATH': 'barhat.db',
    'MOYSKLAD_DB_PATH': 'moysklad.db',
}

# Директории с вложениями — ровно та же ловушка, что и с базами. Дефолты
# ("invoice_attachments", "writeoff_attachments") относительные, поэтому файлы
# ложились в /app и стирались каждой сборкой: запись в БД оставалась, а файла
# больше не было. Скачивание при этом отвечало не «файл потерян», а общим
# 404-обработчиком ("Endpoint not found") — выглядело как сломанный маршрут.
ATTACHMENT_DIRS = {
    'INVOICE_ATTACHMENTS_DIR': 'invoice_attachments',
    'WRITEOFF_ATTACHMENTS_DIR': 'writeoff_attachments',
}

# Признак боевого окружения — примонтированный /data на posix-системе.
# Только os.path.isdir('/data') мало: на Windows этот путь резолвится в
# C:\data, и случайная локальная папка с таким именем переписала бы пути
# разработчика.
IS_PERSISTENT_MOUNT = os.name == 'posix' and os.path.isdir(PERSISTENT_DIR)

def force_persistent_path(env_var, name):
    """Прибить путь из env к постоянному диску /data (на проде)."""
    persistent_path = f'{PERSISTENT_DIR}/{name}'
    current = os.environ.get(env_var)

    # Локальный запуск: не трогаем относительные пути разработчика,
    # только подставляем дефолт
    if not IS_PERSISTENT_MOUNT:
        os.environ.setdefault(env_var, persistent_path)
        return

    if current != persistent_path:
        if current:
            print(
                f"WARNING: {env_var}={current} ведёт мимо постоянного диска "
                f"{PERSISTENT_DIR} — данные терялись бы при каждой сборке. "
                f"Использую {persistent_path}"
            )
        os.environ[env_var] = persistent_path


for env_var, file_name in DB_FILES.items():
    force_persistent_path(env_var, file_name)

for env_var, dir_name in ATTACHMENT_DIRS.items():
    force_persistent_path(env_var, dir_name)

from pyrus.server import app

if __name__ == '__main__':
    # Production settings for Amvera
    app.run(host='0.0.0.0', port=80, debug=False)
