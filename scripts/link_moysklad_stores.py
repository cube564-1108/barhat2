"""
Одноразовый интерактивный скрипт: сопоставляет точки продаж (cashshifts.stores,
barhat.db) складам МойСклад (moysklad.db, уже синхронизированным через
scripts/update_moysklad.py).

Результат сохраняется в таблицу moysklad_store_links (barhat.db) — её же
использует модуль writeoffs (Фаза 1 плана plans/2026-08-16-stock-writeoffs-module.md)
и сможет переиспользовать план мониторинга остатков.

Запуск: python scripts/link_moysklad_stores.py
"""

import os
import sys
import io

if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cashshifts.storage import get_all_stores
from moysklad.storage import get_storage as get_moysklad_storage
from moysklad.client import MOYSKLAD_API_URL
from writeoffs.storage import (
    init_writeoffs_tables,
    link_moysklad_store,
    list_moysklad_store_links,
)


def build_store_href(moysklad_store_id: str) -> str:
    return f"{MOYSKLAD_API_URL.rstrip('/')}/entity/store/{moysklad_store_id}"


def main():
    print("=== Связка точек продаж с складами МойСклад ===\n")

    init_writeoffs_tables()

    barhat_stores = get_all_stores()
    if not barhat_stores:
        print("В barhat.db нет активных точек продаж (таблица stores из cashshifts). Прервано.")
        return

    moysklad_stores = get_moysklad_storage().get_stores()
    if not moysklad_stores:
        print(
            "В moysklad.db нет складов. Сначала выполните синхронизацию:\n"
            "  python scripts/update_moysklad.py\n"
            "Прервано."
        )
        return

    existing_links = {link["store_id"]: link for link in list_moysklad_store_links()}

    print(f"Точек продаж (barhat.db): {len(barhat_stores)}")
    print(f"Складов МойСклад (moysklad.db): {len(moysklad_stores)}\n")

    for store in barhat_stores:
        store_id = store["id"]
        current = existing_links.get(store_id)
        print(f"--- Точка: {store['name']} (id={store_id}) ---")
        if current:
            print(f"    Уже сопоставлена со складом id={current['moysklad_store_id']}")
            answer = input("    Пересопоставить? (y/N): ").strip().lower()
            if answer != "y":
                print()
                continue

        for idx, ms in enumerate(moysklad_stores, start=1):
            print(f"    [{idx}] {ms.get('name')} (id={ms.get('id')})")
        print("    [0] Пропустить эту точку")

        choice = input("    Выберите номер склада: ").strip()
        if not choice or choice == "0":
            print("    Пропущено.\n")
            continue

        try:
            idx = int(choice)
            selected = moysklad_stores[idx - 1]
        except (ValueError, IndexError):
            print("    Некорректный ввод, точка пропущена.\n")
            continue

        href = build_store_href(selected["id"])
        link_moysklad_store(store_id, selected["id"], href)
        print(f"    Сопоставлено: {store['name']} -> {selected.get('name')}\n")

    print("=== Итоговые связки ===")
    for link in list_moysklad_store_links():
        print(f"  store_id={link['store_id']} -> moysklad_store_id={link['moysklad_store_id']}")


if __name__ == "__main__":
    main()
