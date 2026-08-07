# Бренд-система Бархат

Единый источник истины для цветов, градиентов и шрифтов всех отчётов и дашбордов.

## Файлы

- `tokens.css` — CSS-переменные (НЕ ИЗМЕНЯТЬ без смены бренда)
- `brand.css` — базовые стили для типовых элементов отчётов

## Подключение в HTML

В `<head>` любого HTML-дашборда:

```html
<link rel="stylesheet" href="/brand/tokens.css">
<link rel="stylesheet" href="/brand/brand.css">
```

## Использование переменных

В собственных стилях отчёта используйте переменные `var(--barkhat-*)`:

```css
.my-element {
  background: var(--barkhat-wine);
  color: var(--barkhat-white);
  font-family: var(--font-heading);
}
```

## Доступные переменные

### Цвета
- `--barkhat-wine` — #411330 (тёмный винный)
- `--barkhat-pink` — #D19CC2 (основной розовый)
- `--barkhat-pink-bright` — #E1A4C9 (яркий розовый)
- `--barkhat-pink-light` — #E4C2DD (светлый розовый)
- `--barkhat-pink-deep` — #B26FA1 (тёмный розовый)
- `--barkhat-gray-dark` — #3C3C3C (текст)
- `--barkhat-gray` — #6F6F6F (вторичный серый)
- `--barkhat-white` — #FFFFFF

### Градиент
- `--barkhat-gradient` — линейный градиент 135deg (розовый)

### Шрифты
- `--font-heading` — Vollkorn (заголовки)
- `--font-body` — Inter (текст)

## CSP для Google Fonts

Если у сервиса строгий CSP, разрешите:
```
style-src fonts.googleapis.com
font-src fonts.gstatic.com
```

Или self-host шрифты в `/brand/fonts/` и уберите `@import` из `brand.css`.
