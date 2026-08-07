"""
Анализ продаж Томск 2025-2026 (v3 - корректное сравнение)
"""

import pandas as pd
import re
import sys

sys.stdout.reconfigure(encoding='utf-8')
output = []

# Загрузка
df = pd.read_excel('Заказы Томск 25-26.xlsx')
df['Дата и время'] = pd.to_datetime(df['Дата и время'], format='%d.%m.%Y %H:%M')
df['month'] = df['Дата и время'].dt.to_period('M')

# Исключаем неполный месяц (август 2026)
df_full = df[df['month'] < '2026-08']

output.append(f"Период анализа: {df_full['Дата и время'].min().date()} - {df_full['Дата и время'].max().date()}")
output.append(f"Всего заказов (полные месяцы): {len(df_full)}")

# Парсинг товаров
def parse_compose_v2(compose_str):
    if pd.isna(compose_str):
        return []
    items = compose_str.split(';')
    products = []
    for item in items:
        item = item.strip()
        if not item:
            continue
        qty_match = re.search(r'(\d+)\s*шт', item)
        quantity = int(qty_match.group(1)) if qty_match else 1
        without_price = re.sub(r'\s*-\s*[\d,.]+\s*₽.*$', '', item)
        without_qty = re.sub(r',?\s*\d+\s*шт\.?\s*$', '', without_price)
        name = re.sub(r'\s*\d+$', '', without_qty).strip()
        name = re.sub(r'\s*\(?\d+[а-яА-Яa-zA-Z]?\)?\s*$', '', name).strip()
        if name:
            products.append({'name': name, 'quantity': quantity})
    return products

# Товары
all_products = []
for idx, row in df_full.iterrows():
    products = parse_compose_v2(row['Состав'])
    for prod in products:
        all_products.append({
            'order_id': row['Номер заказа'],
            'date': row['Дата и время'],
            'month': row['month'],
            'channel': row['Способ оформления'],
            'product_name': prod['name'],
            'quantity': prod['quantity']
        })

products_df = pd.DataFrame(all_products)

# === СРАВНЕНИЕ 2025 vs 2026 (янв-июль) ===

# Данные за 2025 и 2026
df_2025 = df_full[df_full['Дата и время'].dt.year == 2025]
df_2026 = df_full[df_full['Дата и время'].dt.year == 2026]

products_2025 = products_df[products_df['date'].dt.year == 2025]
products_2026 = products_df[products_df['date'].dt.year == 2026]

output.append("\n=== СРАВНЕНИЕ 2025 vs 2026 (янв-июль) ===")

# Общая сводка
total_2025 = df_2025['Сумма заказа'].sum()
total_2026 = df_2026['Сумма заказа'].sum()
count_2025 = len(df_2025)
count_2026 = len(df_2026)

change_sum = ((total_2026 - total_2025) / total_2025 * 100) if total_2025 > 0 else 0
change_cnt = ((count_2026 - count_2025) / count_2025 * 100) if count_2025 > 0 else 0

output.append(f"\nОБЩАЯ ДИНАМИКА:")
output.append(f"  Выручка 2025: {total_2025:,.0f} руб.")
output.append(f"  Выручка 2026: {total_2026:,.0f} руб.")
output.append(f"  Изменение: {change_sum:+.1f}%")
output.append(f"\n  Заказы 2025: {count_2025}")
output.append(f"  Заказы 2026: {count_2026}")
output.append(f"  Изменение: {change_cnt:+.1f}%")

# По каналам
output.append("\n=== ДИНАМИКА ПО КАНАЛАМ ===")
channels_2025 = df_2025.groupby('Способ оформления').agg({
    'Сумма заказа': ['sum', 'count']
}).reset_index()
channels_2025.columns = ['channel', 'total_sum', 'order_count']

channels_2026 = df_2026.groupby('Способ оформления').agg({
    'Сумма заказа': ['sum', 'count']
}).reset_index()
channels_2026.columns = ['channel', 'total_sum', 'order_count']

# Объединяем для сравнения
channel_comparison = []
for ch in set(channels_2025['channel']) | set(channels_2026['channel']):
    row_2025 = channels_2025[channels_2025['channel'] == ch]
    row_2026 = channels_2026[channels_2026['channel'] == ch]

    sum_2025 = row_2025['total_sum'].values[0] if not row_2025.empty else 0
    sum_2026 = row_2026['total_sum'].values[0] if not row_2026.empty else 0
    cnt_2025 = row_2025['order_count'].values[0] if not row_2025.empty else 0
    cnt_2026 = row_2026['order_count'].values[0] if not row_2026.empty else 0

    change_sum_ch = ((sum_2026 - sum_2025) / sum_2025 * 100) if sum_2025 > 0 else (100 if sum_2026 > 0 else 0)
    change_cnt_ch = ((cnt_2026 - cnt_2025) / cnt_2025 * 100) if cnt_2025 > 0 else (100 if cnt_2026 > 0 else 0)

    channel_comparison.append({
        'channel': ch,
        'sum_2025': sum_2025,
        'sum_2026': sum_2026,
        'change_sum': change_sum_ch,
        'cnt_2025': cnt_2025,
        'cnt_2026': cnt_2026,
        'change_cnt': change_cnt_ch
    })

# Сортируем по изменению выручки
channel_comparison.sort(key=lambda x: x['change_sum'])

# Каналы с снижением
output.append("\nКАНАЛЫ СО СНИЖЕНИЕМ ПРОДАЖ:")
for ch in channel_comparison:
    if ch['change_sum'] < -10:  # Снижение более 10%
        output.append(f"\n  {ch['channel']}:")
        output.append(f"    Выручка: {ch['sum_2025']:,.0f} → {ch['sum_2026']:,.0f} руб. ({ch['change_sum']:.1f}%)")
        output.append(f"    Заказы: {ch['cnt_2025']:.0f} → {ch['cnt_2026']:.0f} ({ch['change_cnt']:.1f}%)")

# Каналы с ростом
output.append("\nКАНАЛЫ С РОСТОМ ПРОДАЖ:")
for ch in reversed(channel_comparison):
    if ch['change_sum'] > 10:  # Рост более 10%
        output.append(f"\n  {ch['channel']}:")
        output.append(f"    Выручка: {ch['sum_2025']:,.0f} → {ch['sum_2026']:,.0f} руб. ({ch['change_sum']:.1f}%)")

# По товарам
output.append("\n=== ДИНАМИКА ПО ТОВАРАМ ===")
prod_2025 = products_2025.groupby('product_name').agg({
    'quantity': 'sum'
}).reset_index()
prod_2026 = products_2026.groupby('product_name').agg({
    'quantity': 'sum'
}).reset_index()

prod_comparison = []
all_prod_names = set(prod_2025['product_name']) | set(prod_2026['product_name'])

for prod in all_prod_names:
    row_2025 = prod_2025[prod_2025['product_name'] == prod]
    row_2026 = prod_2026[prod_2026['product_name'] == prod]

    qty_2025 = row_2025['quantity'].values[0] if not row_2025.empty else 0
    qty_2026 = row_2026['quantity'].values[0] if not row_2026.empty else 0

    if qty_2025 >= 50:  # Только товары с минимум 50 продаж в 2025
        change = ((qty_2026 - qty_2025) / qty_2025 * 100) if qty_2025 > 0 else 0
        prod_comparison.append({
            'name': prod,
            'qty_2025': qty_2025,
            'qty_2026': qty_2026,
            'change': change
        })

prod_comparison.sort(key=lambda x: x['change'])

# Товары со снижением
output.append("\nТОП-20 ТОВАРОВ СО СНИЖЕНИЕМ:")
for p in prod_comparison[:20]:
    if p['change'] < -15:  # Снижение более 15%
        output.append(f"\n  {p['name']}:")
        output.append(f"    {p['qty_2025']:.0f} → {p['qty_2026']:.0f} шт. ({p['change']:.1f}%)")

# Сохранение
with open('analysis_results.txt', 'w', encoding='utf-8') as f:
    f.write('\n'.join(output))

print("Анализ завершён. Результаты в analysis_results.txt")
