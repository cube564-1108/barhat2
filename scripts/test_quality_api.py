"""
Тестирование API качества сборки букетов
Запуск: python scripts/test_quality_api.py
"""

import requests
import json

API_BASE = 'http://127.0.0.1:5000'

def test_health():
    """Проверка здоровья сервера"""
    try:
        response = requests.get(f'{API_BASE}/health')
        data = response.json()
        print(f"✓ Health check: {data}")
        return True
    except Exception as e:
        print(f"✗ Health check failed: {e}")
        return False

def test_quality_report():
    """Проверка отчета по качеству"""
    try:
        response = requests.get(f'{API_BASE}/api/quality')
        data = response.json()
        if data.get('success'):
            report = data.get('data', {})
            print(f"✓ Quality report: {report.get('total_tasks')} задач")
            print(f"  Салонов: {len(report.get('salons', {}))}")
            print(f"  Средний балл: {report.get('overall_avg')}")
            return report
        else:
            print(f"✗ Quality report error: {data.get('error')}")
            return None
    except Exception as e:
        print(f"✗ Quality report failed: {e}")
        return None

def test_quality_report_with_dates():
    """Проверка отчета с фильтром по дате"""
    try:
        response = requests.get(f'{API_BASE}/api/quality?date_from=2026-08-03&date_to=2026-08-05')
        data = response.json()
        if data.get('success'):
            report = data.get('data', {})
            print(f"✓ Quality report (filtered): {report.get('total_tasks')} задач")
            return report
        else:
            print(f"✗ Filtered report error: {data.get('error')}")
            return None
    except Exception as e:
        print(f"✗ Filtered report failed: {e}")
        return None

def test_monthly_history():
    """Проверка истории по месяцам"""
    try:
        response = requests.get(f'{API_BASE}/api/quality/history?months=6')
        data = response.json()
        if data.get('success'):
            history = data.get('data', {})
            print(f"✓ Monthly history: {len(history)} месяцев")
            return history
        else:
            print(f"✗ Monthly history error: {data.get('error')}")
            return None
    except Exception as e:
        print(f"✗ Monthly history failed: {e}")
        return None

def test_salon_history():
    """Проверка истории по салону"""
    # Сначала получаем список салонов
    report = test_quality_report()
    if not report:
        return None

    salons = list(report.get('salons', {}).keys())
    if not salons:
        print("✗ Нет салонов для тестирования")
        return None

    salon_name = salons[0]
    print(f"\nТестирование истории салона: {salon_name}")

    try:
        response = requests.get(f'{API_BASE}/api/quality/salon-history?salon={salon_name}&months=6')
        data = response.json()
        if data.get('success'):
            history = data.get('data', {})
            print(f"✓ Salon history: {len(history)} месяцев")
            print(f"  Данные: {json.dumps(history, indent=2, ensure_ascii=False)}")
            return history
        else:
            print(f"✗ Salon history error: {data.get('error')}")
            return None
    except Exception as e:
        print(f"✗ Salon history failed: {e}")
        return None

def main():
    print("=" * 60)
    print("ТЕСТИРОВАНИЕ API КАЧЕСТВА СБОРКИ БУКЕТОВ")
    print("=" * 60)

    # Проверка здоровья
    print("\n1. Health check:")
    if not test_health():
        print("\n❌ Сервер не запущен! Запустите: python -m src.pyrus.server")
        return

    # Проверка отчета
    print("\n2. Quality report (все время):")
    report = test_quality_report()

    # Проверка с фильтром
    print("\n3. Quality report (03.08-05.08):")
    filtered = test_quality_report_with_dates()

    # Сравнение
    if report and filtered:
        print(f"\n📊 Сравнение:")
        print(f"  Всего: {report.get('total_tasks')} задач")
        print(f"  За период: {filtered.get('total_tasks')} задач")
        if filtered.get('total_tasks') > 1000:
            print(f"  ⚠️  ВНИМАНИЕ: Слишком много задач за период! Возможна ошибка фильтрации.")
        else:
            print(f"  ✓ Фильтрация работает корректно")

    # История по месяцам
    print("\n4. Monthly history:")
    test_monthly_history()

    # История по салону
    print("\n5. Salon history:")
    test_salon_history()

    print("\n" + "=" * 60)
    print("ТЕСТИРОВАНИЕ ЗАВЕРШЕНО")
    print("=" * 60)

if __name__ == '__main__':
    main()
