"""
Запуск сервера Pyrus API
"""
import sys
sys.path.insert(0, 'src')

from pyrus.server import run_server

if __name__ == '__main__':
    print("🌸 Запуск сервера Бархат...")
    print("📊 Дашборд: http://127.0.0.1:5000/dashboard")
    print("=" * 50)
    run_server(host='127.0.0.1', port=5000, debug=True)
