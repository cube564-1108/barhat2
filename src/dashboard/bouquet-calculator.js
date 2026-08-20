/**
 * Калькулятор букетов для Бархат
 * Расчёт себестоимости и отпускной цены букета
 */

(function() {
    'use strict';

    // === Данные прайс-листа ===

    const CATEGORIES = {
        FLOWERS: 'flowers',
        DRIED: 'dried',
        STRAWBERRY: 'strawberry',
        CHOCOLATE: 'chocolate',
        SPRINKLES: 'sprinkles',
        BANANA: 'banana',
        PACKAGING: 'packaging'
    };

    const CATEGORY_LABELS = {
        'flowers': 'Цветы',
        'dried': 'Сушеные элементы',
        'strawberry': 'Клубника',
        'chocolate': 'Шоколад',
        'sprinkles': 'Посыпки',
        'banana': 'Бананы',
        'packaging': 'Упаковка'
    };

    // Категории, которые считаются по граммам с объёмной скидкой
    const TIERED_CATEGORIES = [CATEGORIES.STRAWBERRY, CATEGORIES.CHOCOLATE, CATEGORIES.SPRINKLES];

    // Дефолтный прайс-лист (для сброса и первого запуска)
    const DEFAULT_PRICE_LIST = {
        // Цветы (цена за 1 stem)
        flowers: {
            'Альстромерия': 374,
            'Гвоздика': 204,
            'Гипсофила': 408,
            'Роза кустовая': 493,
            'Роза одноголовая': 340,
            'Роза местная': 226,
            'Хлопок': 170,
            'Хризантема кустовая': 544,
            'Эвкалипт': 228,
            'Эустома': 680,
            'Пион': 935,
            'Гортензия': 1309,
            'Тюльпан': 204,
            'Нобилис': 555
        },

        // Сушеные элементы (цена за 1 шт)
        dried: {
            'Сушеный апельсин': 44,
            'Шарик елочный': 37,
            'Корица': 27,
            'Шишка': 34
        },

        // Клубника (цена за грамм с объёмной скидкой)
        strawberry: {
            tiers: [
                { max: 250, price: 7.28 },
                { max: 500, price: 5.72 },
                { max: 750, price: 5.33 },
                { max: 999999, price: 4.16 }
            ]
        },

        // Шоколад (цена за грамм с объёмной скидкой)
        chocolate: {
            tiers: [
                { max: 250, price: 3.6 },
                { max: 500, price: 3.5 },
                { max: 750, price: 3.3 },
                { max: 999999, price: 3.1 }
            ]
        },

        // Посыпки (цена за грамм с объёмной скидкой)
        sprinkles: {
            tiers: [
                { max: 250, price: 2.34 },
                { max: 500, price: 2.275 },
                { max: 750, price: 2.145 },
                { max: 999999, price: 2.015 }
            ]
        },

        // Бананы (цена за грамм)
        banana: { price: 2.05 },

        // Упаковка (фиксированная цена)
        packaging: {
            'Упаковка (8%)': null,  // Спец. случай — 8% от суммы
            'Упаковка + фирм. лента': null,  // 8% + 34
            'Лента': 12,
            'Фирменная лента': 34,
            'Круглый S touch': 453,
            'Круглый S бархат': 831,
            'Сердце M бархат': 3801,
            'Сердце с крышкой XS': 1350,
            'Стаканчик': 83,
            'Сумочка': 368,
            'Коробочка на 4 ягоды': 84,
            'Коробочка на 8 ягод': 216,
            'Коробочка на 16 ягод': 270
        }
    };

    // === Общие хелперы ===

    /**
     * Глубокая копия — чтобы правки не задевали DEFAULT_PRICE_LIST и рецепты
     */
    function deepClone(value) {
        return JSON.parse(JSON.stringify(value));
    }

    /**
     * Экранирование для вставки в innerHTML и в атрибуты
     */
    function escapeHtml(str) {
        return String(str ?? '').replace(/[&<>"']/g, (c) => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
        }[c]));
    }

    /**
     * Проверить, что прайс-лист пригоден для расчёта (все категории на месте)
     */
    function isValidPriceList(data) {
        if (!data || typeof data !== 'object') return false;

        const isPriceMap = (obj) => obj && typeof obj === 'object' && !Array.isArray(obj);
        if (!isPriceMap(data.flowers) || !isPriceMap(data.dried) || !isPriceMap(data.packaging)) {
            return false;
        }

        const hasValidTiers = TIERED_CATEGORIES.every(category => {
            const tiers = data[category] && data[category].tiers;
            return Array.isArray(tiers) && tiers.length > 0 &&
                tiers.every(t => t && typeof t.max === 'number' && typeof t.price === 'number');
        });
        if (!hasValidTiers) return false;

        return !!data.banana && typeof data.banana.price === 'number';
    }

    // Прайс-лист (загружается из localStorage или дефолтный)
    let PRICE_LIST = deepClone(DEFAULT_PRICE_LIST);

    // === Функции для работы с прайс-листом в localStorage ===

    const PRICELIST_STORAGE_KEY = 'barhat_pricelist_v1';

    /**
     * Загрузить прайс-лист из localStorage
     */
    function loadPricelistFromStorage() {
        try {
            const saved = localStorage.getItem(PRICELIST_STORAGE_KEY);
            if (!saved) return;

            const parsed = JSON.parse(saved);
            if (isValidPriceList(parsed)) {
                PRICE_LIST = parsed;
                console.log('Прайс-лист загружен из localStorage');
            } else {
                console.warn('Сохранённый прайс-лист повреждён — используем исходный');
            }
        } catch (err) {
            console.error('Ошибка загрузки прайс-листа:', err);
        }
    }

    /**
     * Сохранить прайс-лист в localStorage
     */
    function savePricelistToStorage() {
        try {
            localStorage.setItem(PRICELIST_STORAGE_KEY, JSON.stringify(PRICE_LIST));
            console.log('Прайс-лист сохранён в localStorage');
        } catch (err) {
            console.error('Ошибка сохранения прайс-листа:', err);
        }
    }

    /**
     * Сбросить прайс-лист к дефолтному
     */
    async function resetPricelistToDefault() {
        if (await window.BarhatUI.confirm('Все изменения прайс-листа будут потеряны.', {
            title: 'Сбросить прайс-лист к исходным значениям?',
            confirmText: 'Сбросить',
            danger: true,
        })) {
            PRICE_LIST = deepClone(DEFAULT_PRICE_LIST);
            savePricelistToStorage();
            location.reload();
        }
    }

    // === Состояние калькулятора ===

    let bouquetItems = [];
    let savedRecipes = [];

    // Счётчик id позиций: Date.now() давал дубли при быстром добавлении
    let nextItemId = 1;

    function createItemId() {
        return nextItemId++;
    }

    // === DOM элементы ===

    const elements = {
        categorySelect: document.getElementById('category-select'),
        productGroup: document.getElementById('product-group'),
        productSelect: document.getElementById('product-select'),
        productLabel: document.getElementById('product-label'),
        priceHint: document.getElementById('price-hint'),
        quantityGroup: document.getElementById('quantity-group'),
        quantityLabel: document.getElementById('quantity-label'),
        quantityInput: document.getElementById('quantity-input'),
        quantityUnit: document.getElementById('quantity-unit'),
        quantityInfo: document.getElementById('quantity-info'),
        addItemBtn: document.getElementById('add-item-btn'),
        bouquetItems: document.getElementById('bouquet-items'),
        bouquetSummary: document.getElementById('bouquet-summary'),
        summaryMaterials: document.getElementById('summary-materials'),
        summaryPackaging: document.getElementById('summary-packaging'),
        summaryTotal: document.getElementById('summary-total'),
        clearAllBtn: document.getElementById('clear-all-btn'),
        recipeNameInput: document.getElementById('recipe-name-input'),
        saveRecipeBtn: document.getElementById('save-recipe-btn'),
        savedRecipes: document.getElementById('saved-recipes'),
        exportRecipesBtn: document.getElementById('export-recipes-btn'),
        importRecipesInput: document.getElementById('import-recipes-input')
    };

    // === Функции расчёта ===

    /**
     * Найти тир объёмной скидки для количества
     */
    function findTier(category, quantity) {
        const tiers = PRICE_LIST[category] && PRICE_LIST[category].tiers;
        if (!Array.isArray(tiers) || tiers.length === 0) return null;
        // Если количество больше последнего порога — берём самый дешёвый тир
        return tiers.find(t => quantity <= t.max) || tiers[tiers.length - 1];
    }

    /**
     * Рассчитать цену товара с учётом объёмной скидки.
     * Возвращает null, если товара нет в прайс-листе (удалили / чужой рецепт).
     */
    function calculateItemPrice(item) {
        const { name, category, quantity } = item;

        if (category === CATEGORIES.FLOWERS || category === CATEGORIES.DRIED) {
            const price = PRICE_LIST[category] && PRICE_LIST[category][name];
            return typeof price === 'number' ? price * quantity : null;
        }

        if (category === CATEGORIES.BANANA) {
            const price = PRICE_LIST.banana && PRICE_LIST.banana.price;
            return typeof price === 'number' ? price * quantity : null;
        }

        // Товары с объёмной скидкой
        if (TIERED_CATEGORIES.includes(category)) {
            const tier = findTier(category, quantity);
            return tier ? tier.price * quantity : null;
        }

        return null;
    }

    /**
     * Рассчитать цену упаковочной позиции.
     * Проценты считаются от суммы материалов (без упаковки).
     */
    function calculatePackagingPrice(item, materialsTotal) {
        if (item.name === 'Упаковка (8%)') {
            return materialsTotal * 0.08;
        }
        if (item.name === 'Упаковка + фирм. лента') {
            return materialsTotal * 0.08 + 34;
        }
        const price = PRICE_LIST.packaging && PRICE_LIST.packaging[item.name];
        return typeof price === 'number' ? price * item.quantity : null;
    }

    /**
     * Получить текущую цену за единицу для товара с объёмной скидкой
     */
    function getCurrentUnitPrice(category, quantity) {
        if (TIERED_CATEGORIES.includes(category)) {
            const tier = findTier(category, quantity);
            return tier ? tier.price : null;
        }
        return null;
    }

    /**
     * Получить информацию о следующем пороге скидки.
     * threshold — количество, начиная с которого действует следующая цена.
     */
    function getNextTierInfo(category, quantity) {
        if (!TIERED_CATEGORIES.includes(category)) return null;

        const tiers = PRICE_LIST[category] && PRICE_LIST[category].tiers;
        if (!Array.isArray(tiers)) return null;

        const currentIndex = tiers.findIndex(t => quantity <= t.max);
        if (currentIndex < 0 || currentIndex >= tiers.length - 1) return null;

        const currentTier = tiers[currentIndex];
        const nextTier = tiers[currentIndex + 1];

        return {
            threshold: currentTier.max + 1,
            nextPrice: nextTier.price,
            currentPrice: currentTier.price
        };
    }

    /**
     * Рассчитать итоговую стоимость букета.
     * itemPrices — цена каждой позиции (ключ — сам объект позиции),
     * чтобы список и итог считались из одного источника.
     */
    function calculateBouquet() {
        const itemPrices = new Map();
        const unknownItems = [];

        // 1. Считаем сумму материалов (БЕЗ упаковки)
        let materialsTotal = 0;
        bouquetItems.forEach(item => {
            if (item.category === CATEGORIES.PACKAGING) return;

            const price = calculateItemPrice(item);
            itemPrices.set(item, price);
            if (price === null) {
                unknownItems.push(item);
            } else {
                materialsTotal += price;
            }
        });

        // 2. Добавляем упаковку
        let packagingTotal = 0;
        bouquetItems.forEach(item => {
            if (item.category !== CATEGORIES.PACKAGING) return;

            const price = calculatePackagingPrice(item, materialsTotal);
            itemPrices.set(item, price);
            if (price === null) {
                unknownItems.push(item);
            } else {
                packagingTotal += price;
            }
        });

        // 3. Итог и округление: ОКРУГЛВВЕРХ(цена;-2)-10
        // Округляем ВВЕРХ до 100, затем вычитаем 10
        const total = materialsTotal + packagingTotal;
        const roundedTotal = total > 0 ? Math.ceil(total / 100) * 100 - 10 : 0;

        return {
            materials: Math.round(materialsTotal),
            packaging: Math.round(packagingTotal),
            total: roundedTotal,
            rawTotal: total,
            itemPrices,
            unknownItems
        };
    }

    // === Форматирование ===

    function formatCurrency(value) {
        return new Intl.NumberFormat('ru-RU', {
            style: 'currency',
            currency: 'RUB',
            minimumFractionDigits: 0,
            maximumFractionDigits: 0
        }).format(value);
    }

    // === UI функции ===

    /**
     * Заполнить список товаров для выбранной категории
     */
    function populateProductSelect(category) {
        elements.productSelect.innerHTML = '';

        if (!category) return;

        let items = [];

        switch (category) {
            case CATEGORIES.FLOWERS:
            case CATEGORIES.DRIED:
            case CATEGORIES.PACKAGING:
                items = Object.keys(PRICE_LIST[category]);
                break;
            case CATEGORIES.STRAWBERRY:
                items = ['Клубника'];
                break;
            case CATEGORIES.CHOCOLATE:
                items = ['Шоколад'];
                break;
            case CATEGORIES.SPRINKLES:
                items = ['Посыпка'];
                break;
            case CATEGORIES.BANANA:
                items = ['Банан'];
                break;
        }

        items.forEach(item => {
            const option = document.createElement('option');
            option.value = item;
            option.textContent = item;
            elements.productSelect.appendChild(option);
        });
    }

    /**
     * Показать информацию о цене товара
     */
    function updatePriceHint(category, productName) {
        let hint = '';

        if (!category || !productName) {
            elements.priceHint.textContent = '';
            return;
        }

        switch (category) {
            case CATEGORIES.FLOWERS:
            case CATEGORIES.DRIED: {
                const stemPrice = PRICE_LIST[category] && PRICE_LIST[category][productName];
                hint = typeof stemPrice === 'number'
                    ? `Цена: ${formatCurrency(stemPrice)} за шт`
                    : 'Нет цены в прайс-листе';
                break;
            }
            case CATEGORIES.PACKAGING: {
                if (productName === 'Упаковка (8%)') {
                    hint = '8% от суммы материалов';
                } else if (productName === 'Упаковка + фирм. лента') {
                    hint = '8% от суммы материалов + 34 ₽';
                } else {
                    const price = PRICE_LIST.packaging && PRICE_LIST.packaging[productName];
                    hint = typeof price === 'number'
                        ? `Цена: ${formatCurrency(price)}`
                        : 'Нет цены в прайс-листе';
                }
                break;
            }
            case CATEGORIES.BANANA: {
                const bananaPrice = PRICE_LIST.banana && PRICE_LIST.banana.price;
                hint = typeof bananaPrice === 'number'
                    ? `Цена: ${bananaPrice} ₽/г`
                    : 'Нет цены в прайс-листе';
                break;
            }
        }

        elements.priceHint.textContent = hint;
    }

    /**
     * Обновить информацию о количестве для товаров с объёмной скидкой
     */
    function updateQuantityInfo() {
        const category = elements.categorySelect.value;
        const quantity = parseInt(elements.quantityInput.value) || 0;

        if (!TIERED_CATEGORIES.includes(category)) {
            elements.quantityInfo.textContent = '';
            return;
        }

        const unitPrice = getCurrentUnitPrice(category, quantity);
        if (unitPrice === null) {
            elements.quantityInfo.textContent = 'Нет цен для этой категории в прайс-листе';
            return;
        }

        const nextTier = getNextTierInfo(category, quantity);
        let info = `Текущая цена: ${unitPrice.toFixed(2)} ₽/г`;

        if (nextTier) {
            const gramsToNext = nextTier.threshold - quantity;
            info += `\nСледующий порог (от ${nextTier.threshold} г): через ${gramsToNext} г → ${nextTier.nextPrice.toFixed(2)} ₽/г`;
        } else {
            info += '\n✓ Минимальная цена!';
        }

        elements.quantityInfo.textContent = info;
    }

    /**
     * Добавить товар в букет
     */
    function addItemToBouquet() {
        const category = elements.categorySelect.value;
        const productName = elements.productSelect.value;
        const quantity = parseInt(elements.quantityInput.value) || 0;

        if (!category || !productName || quantity <= 0) {
            return;
        }

        const newItem = {
            id: createItemId(),
            name: productName,
            category: category,
            quantity: quantity
        };

        bouquetItems.push(newItem);
        renderBouquetItems();
        updateSummary();

        // Сбросить выбор товара
        elements.quantityInput.value = 1;
        updateQuantityInfo();
    }

    /**
     * Удалить товар из букета
     */
    function removeItem(id) {
        bouquetItems = bouquetItems.filter(item => item.id !== id);
        renderBouquetItems();
        updateSummary();
    }

    /**
     * Изменить количество товара
     */
    function updateItemQuantity(id, newQuantity) {
        const item = bouquetItems.find(i => i.id === id);
        if (item && newQuantity > 0) {
            item.quantity = newQuantity;
            renderBouquetItems();
            updateSummary();
        }
    }

    /**
     * Отрендерить список товаров в букете
     */
    function renderBouquetItems() {
        if (bouquetItems.length === 0) {
            elements.bouquetItems.innerHTML = '<p class="empty-state">Букет пуст. Добавьте товары из левой панели.</p>';
            elements.bouquetSummary.style.display = 'none';
            return;
        }

        elements.bouquetItems.innerHTML = '';
        elements.bouquetSummary.style.display = 'block';

        const { itemPrices } = calculateBouquet();

        bouquetItems.forEach(item => {
            const itemPrice = itemPrices.get(item);
            const priceText = itemPrice === null || itemPrice === undefined
                ? 'нет в прайсе'
                : formatCurrency(Math.round(itemPrice));
            const itemEl = document.createElement('div');
            itemEl.className = 'bouquet-item';
            itemEl.innerHTML = `
                <div class="bouquet-item-info">
                    <div class="bouquet-item-name">${escapeHtml(item.name)}</div>
                    <div class="bouquet-item-category">${escapeHtml(CATEGORY_LABELS[item.category] || item.category)}</div>
                </div>
                <div class="bouquet-item-quantity">
                    <button class="quantity-btn" data-action="decrease" data-id="${item.id}">−</button>
                    <input type="number" class="quantity-input-small" value="${item.quantity}" min="1" data-id="${item.id}">
                    <button class="quantity-btn" data-action="increase" data-id="${item.id}">+</button>
                </div>
                <div class="bouquet-item-price">${priceText}</div>
                <button class="bouquet-item-remove" data-id="${item.id}">
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <line x1="18" y1="6" x2="6" y2="18"/>
                        <line x1="6" y1="6" x2="18" y2="18"/>
                    </svg>
                </button>
            `;
            elements.bouquetItems.appendChild(itemEl);
        });

        // Обработчики для кнопок (только внутри списка букета)
        elements.bouquetItems.querySelectorAll('.bouquet-item-remove').forEach(btn => {
            btn.addEventListener('click', () => removeItem(parseInt(btn.dataset.id)));
        });

        elements.bouquetItems.querySelectorAll('.quantity-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                const id = parseInt(btn.dataset.id);
                const item = bouquetItems.find(i => i.id === id);
                if (item) {
                    const newQuantity = btn.dataset.action === 'increase'
                        ? item.quantity + 1
                        : Math.max(1, item.quantity - 1);
                    updateItemQuantity(id, newQuantity);
                }
            });
        });

        elements.bouquetItems.querySelectorAll('.quantity-input-small').forEach(input => {
            input.addEventListener('change', () => {
                const id = parseInt(input.dataset.id);
                const newQuantity = parseInt(input.value) || 1;
                updateItemQuantity(id, Math.max(1, newQuantity));
            });
        });
    }

    /**
     * Обновить сводку по букету
     */
    function updateSummary() {
        const summary = calculateBouquet();

        elements.summaryMaterials.textContent = formatCurrency(summary.materials);
        elements.summaryPackaging.textContent = formatCurrency(summary.packaging);
        elements.summaryTotal.textContent = formatCurrency(summary.total);
    }

    /**
     * Очистить весь букет
     */
    async function clearAll() {
        if (bouquetItems.length === 0) return;

        if (await window.BarhatUI.confirm('Все позиции будут убраны из состава.', {
            title: 'Очистить весь букет?',
            confirmText: 'Очистить',
            danger: true,
        })) {
            bouquetItems = [];
            renderBouquetItems();
            updateSummary();
        }
    }

    // === Сохранение рецептов ===

    /**
     * Загрузить рецепты из localStorage
     */
    function loadSavedRecipes() {
        try {
            const saved = localStorage.getItem('barhat_bouquet_recipes');
            const parsed = saved ? JSON.parse(saved) : [];
            savedRecipes = Array.isArray(parsed)
                ? parsed.filter(r => r && r.name && Array.isArray(r.items))
                : [];
        } catch (err) {
            console.error('Ошибка загрузки рецептов:', err);
            savedRecipes = [];
        }
        renderSavedRecipes();
    }

    /**
     * Сохранить рецепты в localStorage
     */
    function saveRecipesToStorage() {
        localStorage.setItem('barhat_bouquet_recipes', JSON.stringify(savedRecipes));
    }

    /**
     * Отрендерить список сохранённых рецептов
     */
    function renderSavedRecipes() {
        if (savedRecipes.length === 0) {
            elements.savedRecipes.innerHTML = '<p class="empty-state">Нет сохранённых рецептов</p>';
            return;
        }

        elements.savedRecipes.innerHTML = '';
        savedRecipes.forEach((recipe, index) => {
            const recipeEl = document.createElement('div');
            recipeEl.className = 'recipe-item';
            recipeEl.innerHTML = `
                <div class="recipe-info">
                    <div class="recipe-name">${escapeHtml(recipe.name)}</div>
                    <div class="recipe-meta">${recipe.items.length} позиций • ${formatCurrency(recipe.total)}</div>
                </div>
                <div class="recipe-actions">
                    <button class="btn btn-sm btn-secondary" data-action="load" data-index="${index}">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <polyline points="1 4 1 10 7 10"/>
                            <path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/>
                        </svg>
                    </button>
                    <button class="btn btn-sm btn-danger" data-action="delete" data-index="${index}">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <polyline points="3 6 5 6 21 6"/>
                            <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
                        </svg>
                    </button>
                </div>
            `;
            elements.savedRecipes.appendChild(recipeEl);
        });

        // Обработчики (только внутри списка рецептов)
        elements.savedRecipes.querySelectorAll('.recipe-actions button').forEach(btn => {
            btn.addEventListener('click', () => {
                const index = parseInt(btn.dataset.index);
                if (btn.dataset.action === 'load') {
                    loadRecipe(index);
                } else if (btn.dataset.action === 'delete') {
                    deleteRecipe(index);
                }
            });
        });
    }

    /**
     * Сохранить текущий букет как рецепт
     */
    function saveRecipe() {
        if (bouquetItems.length === 0) {
            alert('Букет пуст. Нечего сохранять.');
            return;
        }

        const name = elements.recipeNameInput.value.trim();
        if (!name) {
            alert('Введите название рецепта.');
            return;
        }

        const summary = calculateBouquet();

        const recipe = {
            name: name,
            // Глубокая копия: иначе правка количества в букете меняет сохранённый рецепт
            items: deepClone(bouquetItems),
            total: summary.total,
            createdAt: new Date().toISOString()
        };

        savedRecipes.push(recipe);
        saveRecipesToStorage();
        renderSavedRecipes();

        elements.recipeNameInput.value = '';
        alert(`Рецепт "${name}" сохранён!`);
    }

    /**
     * Загрузить рецепт
     */
    function loadRecipe(index) {
        const recipe = savedRecipes[index];
        if (!recipe) return;

        // Глубокая копия + свежие id: рецепт не должен меняться вслед за букетом
        bouquetItems = deepClone(recipe.items).map(item => ({
            ...item,
            id: createItemId()
        }));
        renderBouquetItems();
        updateSummary();
    }

    /**
     * Удалить рецепт
     */
    async function deleteRecipe(index) {
        if (await window.BarhatUI.confirm('Рецепт будет удалён из сохранённых.', {
            title: 'Удалить рецепт?',
            confirmText: 'Удалить',
            danger: true,
        })) {
            savedRecipes.splice(index, 1);
            saveRecipesToStorage();
            renderSavedRecipes();
        }
    }

    /**
     * Экспортировать рецепты в JSON
     */
    function exportRecipes() {
        if (savedRecipes.length === 0) {
            alert('Нет сохранённых рецептов для экспорта.');
            return;
        }

        const data = JSON.stringify(savedRecipes, null, 2);
        const blob = new Blob([data], { type: 'application/json' });
        const url = URL.createObjectURL(blob);

        const a = document.createElement('a');
        a.href = url;
        a.download = `barhat-recipes-${new Date().toISOString().slice(0, 10)}.json`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
    }

    /**
     * Импортировать рецепты из JSON
     */
    function importRecipes(file) {
        if (!file) return;

        const reader = new FileReader();
        // async — внутри ждём подтверждения через BarhatUI.confirm
        reader.onload = async function(e) {
            try {
                const imported = JSON.parse(e.target.result);

                if (!Array.isArray(imported)) {
                    throw new Error('Неверный формат файла');
                }

                // Проверяем структуру
                const valid = imported.every(r =>
                    r.name && Array.isArray(r.items) && typeof r.total === 'number'
                );

                if (!valid) {
                    throw new Error('Неверная структура рецептов');
                }

                if (await window.BarhatUI.confirm(`Будет добавлено рецептов: ${imported.length}.`, {
                    title: 'Импортировать рецепты?',
                    confirmText: 'Импортировать',
                })) {
                    savedRecipes = [...savedRecipes, ...imported];
                    saveRecipesToStorage();
                    renderSavedRecipes();
                    alert(`Успешно импортировано ${imported.length} рецепт(ов)!`);
                }
            } catch (err) {
                alert('Ошибка импорта: ' + err.message);
            }
        };

        reader.readAsText(file);

        // Сбросить input
        elements.importRecipesInput.value = '';
    }

    // === Обработчики событий ===

    function initEventListeners() {
        // Изменение категории
        elements.categorySelect.addEventListener('change', () => {
            const category = elements.categorySelect.value;

            if (!category) {
                elements.productGroup.style.display = 'none';
                elements.quantityGroup.style.display = 'none';
                elements.addItemBtn.style.display = 'none';
                return;
            }

            populateProductSelect(category);
            elements.productGroup.style.display = 'block';

            // Настраиваем интерфейс в зависимости от категории
            if (category === CATEGORIES.STRAWBERRY || category === CATEGORIES.CHOCOLATE ||
                category === CATEGORIES.SPRINKLES || category === CATEGORIES.BANANA) {
                // Товары по граммам
                elements.quantityLabel.textContent = 'Количество (грамм)';
                elements.quantityUnit.textContent = 'г';
                elements.quantityInput.step = '1';
                elements.quantityInput.value = '100';
            } else if (category === CATEGORIES.PACKAGING) {
                // Упаковка
                elements.quantityLabel.textContent = 'Количество';
                elements.quantityUnit.textContent = 'шт';
                elements.quantityInput.step = '1';
                elements.quantityInput.value = '1';
            } else {
                // Цветы и сушеные
                elements.quantityLabel.textContent = 'Количество (штук)';
                elements.quantityUnit.textContent = 'шт';
                elements.quantityInput.step = '1';
                elements.quantityInput.value = '1';
            }

            elements.quantityGroup.style.display = 'block';
            elements.addItemBtn.style.display = 'inline-flex';

            // Селект заполнен программно — событие change не сработает, обновляем сами
            updatePriceHint(category, elements.productSelect.value);
            updateQuantityInfo();
        });

        // Изменение товара
        elements.productSelect.addEventListener('change', () => {
            updatePriceHint(elements.categorySelect.value, elements.productSelect.value);
        });

        // Изменение количества
        elements.quantityInput.addEventListener('input', updateQuantityInfo);

        // Добавление товара
        elements.addItemBtn.addEventListener('click', addItemToBouquet);

        // Очистка всего
        elements.clearAllBtn.addEventListener('click', clearAll);

        // Сохранение рецепта
        elements.saveRecipeBtn.addEventListener('click', saveRecipe);

        // Экспорт рецептов
        elements.exportRecipesBtn.addEventListener('click', exportRecipes);

        // Импорт рецептов
        elements.importRecipesInput.addEventListener('change', (e) => {
            importRecipes(e.target.files[0]);
        });

        // Enter на поле количества
        elements.quantityInput.addEventListener('keypress', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                addItemToBouquet();
            }
        });
    }

    // === Инициализация ===

    function init() {
        loadPricelistFromStorage(); // Загружаем прайс-лист из localStorage
        initEventListeners();
        loadSavedRecipes();
        updateQuantityInfo();
    }

    // Запуск при готовности DOM
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    // === АДМИНКА ПРАЙС-ЛИСТА ===

    // Пароль для доступа к админке
    const ADMIN_PASSWORD = 'barhat2024';

    // Текущий прайс-лист (редактируемый)
    let editablePriceList = deepClone(PRICE_LIST);

    // DOM элементы админки
    const adminElements = {
        openAdminBtn: document.getElementById('open-admin-btn'),
        adminModal: document.getElementById('admin-modal'),
        adminModalOverlay: document.getElementById('admin-modal-overlay'),
        closeAdminModalBtn: document.getElementById('close-admin-modal-btn'),
        adminCategories: document.getElementById('admin-categories'),
        adminItemsList: document.getElementById('admin-items-list'),
        newItemName: document.getElementById('new-item-name'),
        newItemPrice: document.getElementById('new-item-price'),
        addNewItemBtn: document.getElementById('add-new-item-btn'),
        exportPricelistBtn: document.getElementById('export-pricelist-btn'),
        importPricelistInput: document.getElementById('import-pricelist-input'),
        resetPricelistBtn: document.getElementById('reset-pricelist-btn'),
        savePricelistBtn: document.getElementById('save-pricelist-btn')
    };

    // Текущая категория в админке
    let currentAdminCategory = 'flowers';

    /**
     * Открыть модальное окно админки
     */
    async function openAdminModal() {
        const password = await window.BarhatUI.prompt('Введите пароль для доступа к админке:', '', {
            title: 'Админка прайс-листа',
            confirmText: 'Войти',
        });

        if (password === null) {
            return; // Отмена
        }

        if (password !== ADMIN_PASSWORD) {
            alert('Неверный пароль!');
            return;
        }

        // Загружаем текущий прайс-лист
        editablePriceList = deepClone(PRICE_LIST);
        currentAdminCategory = 'flowers';

        renderAdminCategories();
        renderAdminItems();

        adminElements.adminModal.classList.add('active');
        document.body.style.overflow = 'hidden';
    }

    /**
     * Закрыть модальное окно админки
     */
    function closeAdminModal() {
        adminElements.adminModal.classList.remove('active');
        document.body.style.overflow = '';
    }

    /**
     * Отрендерить табы категорий
     */
    function renderAdminCategories() {
        const tabs = adminElements.adminCategories.querySelectorAll('.admin-tab');
        tabs.forEach(tab => {
            tab.classList.remove('active');
            if (tab.dataset.category === currentAdminCategory) {
                tab.classList.add('active');
            }
        });
    }

    /**
     * Отрендерить товары текущей категории
     */
    function renderAdminItems() {
        adminElements.adminItemsList.innerHTML = '';

        const categoryData = editablePriceList[currentAdminCategory];

        if (!categoryData) {
            adminElements.adminItemsList.innerHTML = '<p class="empty-state">Категория пуста</p>';
            return;
        }

        // Обычные товары (flowers, dried, packaging)
        if (typeof categoryData === 'object' && !categoryData.tiers && !categoryData.price) {
            Object.entries(categoryData).forEach(([name, price]) => {
                if (price === null) {
                    // Специальные элементы (упаковка 8%)
                    return;
                }

                const safeName = escapeHtml(name);
                const itemEl = document.createElement('div');
                itemEl.className = 'admin-item';
                itemEl.innerHTML = `
                    <div class="admin-item-name">${safeName}</div>
                    <input type="number" class="admin-item-price" value="${escapeHtml(price)}" step="0.01" data-name="${safeName}">
                    <button class="btn btn-primary btn-sm admin-item-save" data-name="${safeName}">✓</button>
                    <button class="admin-item-delete" data-name="${safeName}">
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <line x1="18" y1="6" x2="6" y2="18"/>
                            <line x1="6" y1="6" x2="18" y2="18"/>
                        </svg>
                    </button>
                `;
                adminElements.adminItemsList.appendChild(itemEl);
            });
        }
        // Товары с объёмной скидкой (strawberry, chocolate, sprinkles)
        else if (categoryData.tiers) {
            const tiers = categoryData.tiers;
            const itemEl = document.createElement('div');
            itemEl.className = 'admin-item';
            itemEl.style.gridColumn = '1 / -1';
            itemEl.innerHTML = `
                <div class="admin-item-name">${escapeHtml(getCategoryLabel(currentAdminCategory))} — объёмная скидка</div>
            `;
            adminElements.adminItemsList.appendChild(itemEl);

            tiers.forEach((tier, index) => {
                const tierEl = document.createElement('div');
                tierEl.className = 'admin-item';
                tierEl.innerHTML = `
                    <div class="admin-item-name">
                        Порог: ${tier.max === 999999 ? '∞' : tier.max + ' г'}
                    </div>
                    <input type="number" class="admin-item-price" value="${tier.price}" step="0.01" data-tier="${index}">
                    <button class="btn btn-primary btn-sm admin-item-save" data-tier="${index}">✓</button>
                    <button class="admin-item-delete" data-tier="${index}" style="opacity: 0.5; cursor: not-allowed;" disabled>
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <line x1="18" y1="6" x2="6" y2="18"/>
                            <line x1="6" y1="6" x2="18" y2="18"/>
                        </svg>
                    </button>
                `;
                adminElements.adminItemsList.appendChild(tierEl);
            });
        }
        // Бананы (простая цена за грамм)
        else if (categoryData.price) {
            const itemEl = document.createElement('div');
            itemEl.className = 'admin-item';
            itemEl.innerHTML = `
                <div class="admin-item-name">Бананы</div>
                <input type="number" class="admin-item-price" value="${categoryData.price}" step="0.01" data-special="banana">
                <button class="btn btn-primary btn-sm admin-item-save" data-special="banana">✓</button>
                <button class="admin-item-delete" style="opacity: 0.5; cursor: not-allowed;" disabled>
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <line x1="18" y1="6" x2="6" y2="18"/>
                        <line x1="6" y1="6" x2="18" y2="18"/>
                    </svg>
                </button>
            `;
            adminElements.adminItemsList.appendChild(itemEl);
        }

        // Обработчики (только внутри списка админки — класс .admin-item-delete
        // используется и в модуле задач)
        adminElements.adminItemsList.querySelectorAll('.admin-item-save').forEach(btn => {
            btn.addEventListener('click', () => saveItemPrice(btn));
        });

        adminElements.adminItemsList.querySelectorAll('.admin-item-delete:not([disabled])').forEach(btn => {
            btn.addEventListener('click', () => deleteItem(btn));
        });

        // Правка цены применяется и без нажатия «✓» — иначе значение молча терялось
        adminElements.adminItemsList.querySelectorAll('.admin-item-price').forEach(input => {
            input.addEventListener('change', () => commitPriceInput(input));
        });
    }

    /**
     * Записать значение поля цены в редактируемый прайс-лист.
     * Возвращает false, если значение некорректное.
     */
    function commitPriceInput(input) {
        const newValue = parseFloat(input.value);

        if (isNaN(newValue) || newValue < 0) {
            alert('Некорректная цена!');
            renderAdminItems();
            return false;
        }

        if (input.dataset.name) {
            editablePriceList[currentAdminCategory][input.dataset.name] = newValue;
        } else if (input.dataset.tier !== undefined) {
            editablePriceList[currentAdminCategory].tiers[parseInt(input.dataset.tier)].price = newValue;
        } else if (input.dataset.special === 'banana') {
            editablePriceList[currentAdminCategory].price = newValue;
        }

        return true;
    }

    /**
     * Записать все видимые поля цен (страховка перед сохранением)
     */
    function commitAllPriceInputs() {
        const inputs = adminElements.adminItemsList.querySelectorAll('.admin-item-price');
        return Array.from(inputs).every(input => commitPriceInput(input));
    }

    /**
     * Сохранить цену товара
     */
    function saveItemPrice(btn) {
        const input = btn.parentElement.querySelector('.admin-item-price');
        if (!input || !commitPriceInput(input)) return;

        // Визуальная обратная связь
        btn.classList.add('btn-success');
        setTimeout(() => btn.classList.remove('btn-success'), 800);
    }

    /**
     * Удалить товар
     */
    async function deleteItem(btn) {
        if (!btn.dataset.name) return;

        const name = btn.dataset.name;

        const ok = await window.BarhatUI.confirm(`Позиция «${name}» будет убрана из прайс-листа.`, {
            title: 'Удалить позицию?',
            confirmText: 'Удалить',
            danger: true,
        });
        if (!ok) return;

        delete editablePriceList[currentAdminCategory][name];
        renderAdminItems();
    }

    /**
     * Добавить новый товар
     */
    function addNewItem() {
        const name = adminElements.newItemName.value.trim();
        const price = parseFloat(adminElements.newItemPrice.value);

        if (!name) {
            alert('Введите название товара!');
            return;
        }

        if (isNaN(price) || price < 0) {
            alert('Некорректная цена!');
            return;
        }

        // Проверяем, можно ли добавлять в эту категорию
        if (['strawberry', 'chocolate', 'sprinkles', 'banana'].includes(currentAdminCategory)) {
            alert('В эту категорию нельзя добавлять товары вручную!');
            return;
        }

        if (editablePriceList[currentAdminCategory][name]) {
            alert('Товар с таким названием уже существует!');
            return;
        }

        editablePriceList[currentAdminCategory][name] = price;

        adminElements.newItemName.value = '';
        adminElements.newItemPrice.value = '';

        renderAdminItems();
    }

    /**
     * Экспортировать прайс-лист в JSON
     */
    function exportPricelist() {
        const data = JSON.stringify(editablePriceList, null, 2);
        const blob = new Blob([data], { type: 'application/json' });
        const url = URL.createObjectURL(blob);

        const a = document.createElement('a');
        a.href = url;
        a.download = `barhat-pricelist-${new Date().toISOString().slice(0, 10)}.json`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
    }

    /**
     * Импортировать прайс-лист из JSON
     */
    function importPricelist(file) {
        if (!file) return;

        const reader = new FileReader();
        // async — внутри ждём подтверждения через BarhatUI.confirm
        reader.onload = async function(e) {
            try {
                const imported = JSON.parse(e.target.result);

                // Проверяем структуру (все категории, а не только три первые)
                if (!isValidPriceList(imported)) {
                    throw new Error('Неверная структура прайс-листа');
                }

                if (await window.BarhatUI.confirm('Текущие изменения будут потеряны.', {
                    title: 'Загрузить этот прайс-лист?',
                    confirmText: 'Загрузить',
                    danger: true,
                })) {
                    editablePriceList = imported;
                    renderAdminItems();
                    alert('Прайс-лист загружен! Не забудьте нажать "Сохранить и закрыть" для применения изменений.');
                }
            } catch (err) {
                alert('Ошибка импорта: ' + err.message);
            }
        };

        reader.readAsText(file);
        adminElements.importPricelistInput.value = '';
    }

    /**
     * Применить изменения прайс-листа
     */
    async function applyPricelistChanges() {
        // Подхватываем то, что набрано в полях, но не подтверждено кнопкой «✓»
        if (!commitAllPriceInputs()) {
            return;
        }

        const ok = await window.BarhatUI.confirm('Калькулятор будет обновлён.', {
            title: 'Применить изменения прайс-листа?',
            confirmText: 'Применить',
        });
        if (!ok) return;

        if (!isValidPriceList(editablePriceList)) {
            alert('Прайс-лист неполный: проверьте цены по граммам (клубника, шоколад, посыпки, бананы).');
            return;
        }

        // Обновляем глобальный PRICE_LIST
        PRICE_LIST = deepClone(editablePriceList);

        // Сохраняем в localStorage
        savePricelistToStorage();

        // Пересчитываем текущий букет
        renderBouquetItems();
        updateSummary();

        // Обновляем селекторы и подсказки
        populateProductSelect(elements.categorySelect.value);
        updatePriceHint(elements.categorySelect.value, elements.productSelect.value);
        updateQuantityInfo();

        closeAdminModal();
        alert('Прайс-лист обновлён и сохранён!');
    }

    /**
     * Получить label категории
     */
    function getCategoryLabel(category) {
        const labels = {
            'flowers': 'Цветы',
            'dried': 'Сушеные',
            'strawberry': 'Клубника',
            'chocolate': 'Шоколад',
            'sprinkles': 'Посыпки',
            'banana': 'Бананы',
            'packaging': 'Упаковка'
        };
        return labels[category] || category;
    }

    /**
     * Инициализация админки
     */
    function initAdmin() {
        // Открытие админки
        adminElements.openAdminBtn.addEventListener('click', openAdminModal);

        // Закрытие админки
        adminElements.closeAdminModalBtn.addEventListener('click', closeAdminModal);
        adminElements.adminModalOverlay.addEventListener('click', closeAdminModal);

        // Переключение категорий
        adminElements.adminCategories.addEventListener('click', (e) => {
            if (e.target.classList.contains('admin-tab')) {
                currentAdminCategory = e.target.dataset.category;
                renderAdminCategories();
                renderAdminItems();
            }
        });

        // Добавление нового товара
        adminElements.addNewItemBtn.addEventListener('click', addNewItem);

        // Экспорт прайс-листа
        adminElements.exportPricelistBtn.addEventListener('click', exportPricelist);

        // Импорт прайс-листа
        adminElements.importPricelistInput.addEventListener('change', (e) => {
            importPricelist(e.target.files[0]);
        });

        // Применение изменений
        adminElements.savePricelistBtn.addEventListener('click', applyPricelistChanges);

        // Сброс к дефолтным ценам
        adminElements.resetPricelistBtn.addEventListener('click', resetPricelistToDefault);

        // Закрытие по Escape
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && adminElements.adminModal.classList.contains('active')) {
                closeAdminModal();
            }
        });
    }

    // Запуск админки
    initAdmin();
})();
