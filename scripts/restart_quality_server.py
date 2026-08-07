"""
Перезапуск API сервера качества

Инструкции:
1. Остановите текущий сервер (Ctrl+C в терминале где он запущен)
2. Запустите этот скрипт для проверки
"""

import subprocess
import sys
import time
import requests

def check_server():
    """Проверка работы сервера"""
    try:
        response = requests.get('http://127.0.0.1:5000/health', timeout=1)
        return response.status_code == 200
    except:
        return False

def test_api():
    """Тест API после перезапуска"""
    try:
        # Ждем запуска сервера
        for i in range(10):
            if check_server():
                break
            time.sleep(0.5)

        # Тестируем API
        response = requests.get('http://127.0.0.1:5000/api/quality')
        data = response.json()

        if data.get('success'):
            total = data.get('data', {}).get('total_tasks', 0)
            print(f"\n✅ API возвращает: {total} задач")
            if total > 1000:
                print(f"⚠️  Слишком много задач! Возможно, сервер не перезагрузился.")
                print(f"   Попробуйте остановить сервер и запустить заново.")
            else:
                print(f"✅ Фильтрация работает корректно!")
        return True
    except Exception as e:
        print(f"❌ Ошибка тестирования API: {e}")
        return False

if __name__ == '__main__':
    print("=" * 60)
    print("ПЕРЕЗАПУСК API СЕРВЕРА")
    print("=" * 60)

    if check_server():
        print("\n⚠️  Сервер работает!")
        print("\n📋 ИНСТРУКЦИЯ:")
        print("1. Найдите терминал с запущенным сервером")
        print("2. Нажмите Ctrl+C для остановки")
        print("3. Запустите заново: python -m src.pyrus.server")
        print("4. Откройте этот скрипт снова для проверки")
        test_api()
    else:
        print("\n❌ Сервер не работает!")
        print("\nЗапустите сервер:")
        print("  python -m src.pyrus.server")
