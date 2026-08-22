"""
Отчёт по качеству сборки букетов — обёртка над src/pyrus/quality.py.

Сама реализация переехала в модуль приложения: раньше сервер подмешивал
scripts/ в sys.path и импортировал отчёт отсюда, из-за чего рядом жили две
версии одного кода (quality_report.py и quality_report_v2.py) и было неясно,
какая работает в проде. Здесь остался только вход для ручного запуска и
совместимость со старыми скриптами.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src'))

from pyrus.quality import (  # noqa: E402
    CATEGORIES_14,
    CATEGORIES_18,
    QUALITY_FORM_ID,
    extract_task_data,
    generate_report,
    get_category_group,
    get_data_coverage,
    get_monthly_history,
    get_salon_florists,
    get_salon_history,
    get_salon_order_types,
    rebuild_projection,
)

__all__ = [
    'CATEGORIES_14',
    'CATEGORIES_18',
    'QUALITY_FORM_ID',
    'extract_task_data',
    'generate_report',
    'get_category_group',
    'get_data_coverage',
    'get_monthly_history',
    'get_salon_florists',
    'get_salon_history',
    'get_salon_order_types',
    'rebuild_projection',
]


if __name__ == '__main__':
    report = generate_report()
    print(f"Оценок: {report['total_tasks']}, средний балл: {report['overall_avg']}")
    for salon, stats in sorted(
        report['salons'].items(), key=lambda x: -x[1]['total']['count']
    ):
        print(
            f"  {salon}: 14 баллов — {stats['cat14']['avg_score']} "
            f"({stats['cat14']['count']}), 18 баллов — {stats['cat18']['avg_score']} "
            f"({stats['cat18']['count']})"
        )
