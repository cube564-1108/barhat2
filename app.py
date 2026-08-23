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

# Пути к базам данных. Сама логика «данные только на постоянном диске» живёт
# в src/storage_paths.py — там же история двух реальных потерь данных. Здесь
# только выставляем переменные окружения до импорта модулей: пути к базам
# читаются из os.environ в каждом модуле отдельно, и все должны увидеть уже
# исправленное значение.
from storage_paths import force_env

DB_FILES = {
    'PYRUS_DB_PATH': 'pyrus.db',
    'BARHAT_DB_PATH': 'barhat.db',
    'MOYSKLAD_DB_PATH': 'moysklad.db',
}

for env_var, file_name in DB_FILES.items():
    force_env(env_var, file_name)

from pyrus.server import app

if __name__ == '__main__':
    # Production settings for Amvera
    app.run(host='0.0.0.0', port=80, debug=False)
