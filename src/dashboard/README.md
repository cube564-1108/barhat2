# Главный дашборд Бархат

Центральная точка для всех отчётов и аналитики сети цветочных салонов «Бархат».

## Структура

```
src/dashboard/
├── index.html      # Главная страница дашборда
├── styles.css      # Стили дашборда (использует бренд-токены)
├── script.js       # JavaScript для интерактивности
└── README.md       # Этот файл
```

## Бренд-система

Дашборд использует официальные бренд-токены из `../../brand/`:

- **Цвета:** винный `#411330`, розовые оттенки `#D19CC2`, `#E1A4C9`, `#E4C2DD`
- **Шрифты:** Vollkorn (заголовки), Inter (текст)
- **Градиент:** линейный 135deg от яркого к тёмному розовому

Подробнее: [brand/README.md](../../brand/README.md)

## Как запустить локально

### Вариант 1: Простой HTTP-сервер (Python)

```bash
# Из корня проекта
python -m http.server 8080
```

Дашборд будет доступен по адресу: http://localhost:8080/src/dashboard/

### Вариант 2: Live Server (VS Code)

1. Установите расширение Live Server
2. Нажмите правой кнопкой на `index.html` → "Open with Live Server"

## Как добавить новый отчёт

### 1. Создайте файл отчёта

Создайте HTML-файл в `src/dashboard/reports/` (например, `sales.html`):

```html
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Продажи — Бархат</title>
    <!-- Бренд-токены -->
    <link rel="stylesheet" href="../../brand/tokens.css">
    <link rel="stylesheet" href="../../brand/brand.css">
    <!-- Ваш стиль -->
    <link rel="stylesheet" href="sales.css">
</head>
<body>
    <header class="bx-header">
        <h1>Продажи</h1>
        <span class="bx-descr">Выручка, средний чек, динамика</span>
    </header>

    <main>
        <!-- Ваш контент -->
    </main>

    <script src="sales.js"></script>
</body>
</html>
```

### 2. Обновите карточку на главном дашборде

В `index.html` найдите соответствующую карточку и замените:

```html
<!-- Было -->
<a href="#" class="report-card coming-soon">
    ...
    <span class="report-status">Скоро</span>
</a>

<!-- Стало -->
<a href="reports/sales.html" class="report-card" data-report-id="sales">
    ...
    <span class="report-status">Открыть</span>
</a>
```

### 3. Используйте бренд-токены в стилях

```css
.my-element {
    background: var(--barkhat-wine);
    color: var(--barkhat-white);
    font-family: var(--font-heading);
}
```

## Доступные переменные

### Цвета

```css
--barkhat-wine        /* #411330 — тёмный винный */
--barkhat-pink        /* #D19CC2 — основной розовый */
--barkhat-pink-bright /* #E1A4C9 — яркий розовый */
--barkhat-pink-light  /* #E4C2DD — светлый розовый */
--barkhat-pink-deep   /* #B26FA1 — тёмный розовый */
--barkhat-gray-dark   /* #3C3C3C — текст */
--barkhat-gray        /* #6F6F6F — вторичный серый */
--barkhat-white       /* #FFFFFF */
```

### Шрифты

```css
--font-heading /* 'Vollkorn' — заголовки */
--font-body    /* 'Inter' — текст */
```

### Градиент

```css
--barkhat-gradient /* linear-gradient(135deg, #E1A4C9 0%, #B26FA1 100%) */
```

## Планируемые отчёты

| Отчёт | Статус | Описание |
|-------|--------|----------|
| Продажи | 📋 Запланирован | Выручка, средний чек, динамика по периодам |
| Маркетинг | 📋 Запланирован | Каналы, конверсии, ROI |
| Склад | 📋 Запланирован | Остатки, движения, отчётность |
| Операции | 📋 Запланирован | Заказы, доставка, выполнение |
| Ассортимент | 📋 Запланирован | Популярность, маржинальность |
| Клиенты | 📋 Запланирован | База, повторные покупки, LTV |

## JavaScript API

Объект `window.BarhatDashboard` доступен глобально:

```javascript
// Форматирование валюты
BarhatDashboard.formatCurrency(15000); // "15 000 ₽"

// Форматирование чисел
BarhatDashboard.formatNumber(1500000); // "1 500 000"

// Показать уведомление
BarhatDashboard.showNotification('Данные обновлены');
```

## Адаптивность

Дашборд адаптирован для:
- Десктопа (1400px+)
- Планшета (768px - 1399px)
- Мобильного (< 768px)
