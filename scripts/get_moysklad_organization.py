"""
Одноразовый скрипт: получает юрлицо (organization) из МойСклад и печатает его
meta.href — вставить в .env как MOYSKLAD_ORGANIZATION_HREF.

Требует настоящие credentials в .env (MOYSKLAD_TOKEN или MOYSKLAD_LOGIN+MOYSKLAD_PASSWORD) —
запускать на машине/сервере, где .env реально есть, не в песочнице разработки.

Запуск: python scripts/get_moysklad_organization.py
"""

import os
import sys
import io

if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from moysklad.client import get_client


def main():
    client = get_client()
    result = client.get_organizations()
    if not result or not result.get("rows"):
        print("Не удалось получить организации. Проверьте credentials в .env.")
        return

    rows = result["rows"]
    if len(rows) > 1:
        print(f"Найдено организаций: {len(rows)} — в .env берётся первая, проверьте, что это верная:")

    for org in rows:
        href = org.get("meta", {}).get("href")
        print(f"  {org.get('name')} -> {href}")

    first_href = rows[0].get("meta", {}).get("href")
    print(f"\nДобавьте в .env:\nMOYSKLAD_ORGANIZATION_HREF={first_href}")


if __name__ == "__main__":
    main()
