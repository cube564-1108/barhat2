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

    // Прайс-лист (загружается из localStorage или дефолтный)
    let PRICE_LIST = { ...DEFAULT_PRICE_LIST };

    // === Функции для работы с прайс-листом в localStorage ===

    const PRICELIST_STORAGE_KEY = 'barhat_pricelist_v1';

    /**
     * Загрузить прайс-лист из localStorage
     */
    function loadPricelistFromStorage() {
        try {
            const saved = localStorage.getItem(PRICELIST_STORAGE_KEY);
            if (saved) {
                const parsed = JSON.parse(saved);
                // Проверяем валидность
                if (parsed.flowers && parsed.dried && parsed.packaging) {
                    PRICE_LIST = parsed;
                    console.log('Прайс-лист загружен из localStorage');
                }
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
    function resetPricelistToDefault() {
        if (confirm('Сбросить прайс-лист к исходным значениям? Все изменения будут потеряны.')) {
            PRICE_LIST = { ...DEFAULT_PRICE_LIST };
            savePricelistToStorage();
            location.reload();
        }
    }

    // === Состояние калькулятора ===

    let bouquetItems = [];
    let savedRecipes = [];

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
     * Рассчитать цену товара с учётом объёмной скидки
     */
    function calculateItemPrice(item) {
        const { name, category, quantity } = item;

        if (category === CATEGORIES.FLOWERS || category === CATEGORIES.DRIED) {
            return PRICE_LIST[category][name] * quantity;
        }

        if (category === CATEGORIES.BANANA) {
            return PRICE_LIST.banana.price * quantity;
        }

        // Товары с объёмной скидкой
        if ([CATEGORIES.STRAWBERRY, CATEGORIES.CHOCOLATE, CATEGORIES.SPRINKLES].includes(category)) {
            const tiers = PRICE_LIST[category].tiers;
            const tier = tiers.find(t => quantity <= t.max);
            return tier.price * quantity;
        }

        return 0;
    }

    /**
     * Получить текущую цену за единицу для товара с объёмной скидкой
     */
    function getCurrentUnitPrice(category, quantity) {
        if ([CATEGORIES.STRAWBERRY, CATEGORIES.CHOCOLATE, CATEGORIES.SPRINKLES].includes(category)) {
            const tiers = PRICE_LIST[category].tiers;
            const tier = tiers.find(t => quantity <= t.max);
            return tier.price;
        }
        return null;
    }

    /**
     * Получить информацию о следующем пороге скидки
     */
    function getNextTierInfo(category, quantity) {
        if ([CATEGORIES.STRAWBERRY, CATEGORIES.CHOCOLATE, CATEGORIES.SPRINKLES].includes(category)) {
            const tiers = PRICE_LIST[category].tiers;
            const currentIndex = tiers.findIndex(t => quantity <= t.max);

            if (currentIndex < tiers.length - 1) {
                const nextTier = tiers[currentIndex + 1];
                const currentTier = tiers[currentIndex];
                return {
                    threshold: nextTier.max === 999999 ? null : currentTier.max + 1,
                    nextPrice: nextTier.price,
                    currentPrice: currentTier.price,
                    max: nextTier.max === 999999 ? '∞' : nextTier.max
                };
            }
        }
        return null;
    }

    /**
     * Рассчитать итоговую стоимость букета
     */
    function calculateBouquet() {
        // 1. Считаем сумму материалов (БЕЗ упаковки)
        let materialsTotal = 0;
        bouquetItems.forEach(item => {
            if (item.category !== CATEGORIES.PACKAGING) {
                materialsTotal += calculateItemPrice(item);
            }
        });

        // 2. Добавляем упаковку
        let packagingTotal = 0;
        const packagingItems = bouquetItems.filter(i => i.category === CATEGORIES.PACKAGING);

        packagingItems.forEach(item => {
            if (item.name === 'Упаковка (8%)') {
                packagingTotal += materialsTotal * 0.08;
            } else if (item.name === 'Упаковка + фирм. лента') {
                packagingTotal += materialsTotal * 0.08 + 34;
            } else {
                packagingTotal += PRICE_LIST.packaging[item.name] * item.quantity;
            }
        });

        // 3. Итог и округление: ОКРУГЛВВЕРХ(цена;-2)-10
        // Округляем ВВЕРХ до 100, затем вычитаем 10
        const total = materialsTotal + packagingTotal;
        const roundedTotal = Math.ceil(total / 100) * 100 - 10;

        return {
            materials: Math.round(materialsTotal),
            packaging: Math.round(packagingTotal),
            total: roundedTotal,
            rawTotal: total
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
            case CATEGORIES.DRIED:
                hint = `Цена: ${formatCurrency(PRICE_LIST[category][productName])} за шт`;
                break;
            case CATEGORIES.PACKAGING:
                const price = PRICE_LIST.packaging[productName];
                if (price === null) {
                    if (productName === 'Упаковка (8%)') {
                        hint = '8% от суммы материалов';
                    } else if (productName === 'Упаковка + фирм. лента') {
                        hint = '8% от суммы материалов + 34 ₽';
                    }
                } else {
                    hint = `Цена: ${formatCurrency(price)}`;
                }
                break;
            case CATEGORIES.BANANA:
                hint = `Цена: ${PRICE_LIST.banana.price} ₽/г`;
                break;
        }

        elements.priceHint.textContent = hint;
    }

    /**
     * Обновить информацию о количестве для товаров с объёмной скидкой
     */
    function updateQuantityInfo() {
        const category = elements.categorySelect.value;
        const quantity = parseInt(elements.quantityInput.value) || 0;

        if ([CATEGORIES.STRAWBERRY, CATEGORIES.CHOCOLATE, CATEGORIES.SPRINKLES].includes(category)) {
            const unitPrice = getCurrentUnitPrice(category, quantity);
            const nextTier = getNextTierInfo(category, quantity);

            let info = `Текущая цена: ${unitPrice.toFixed(2)} ₽/г`;

            if (nextTier) {
                const gramsToNext = nextTier.threshold - quantity;
                if (gramsToNext > 0) {
                    info += `\nСледующий порог (${nextTier.max} г): через ${gramsToNext} г → ${nextTier.nextPrice.toFixed(2)} ₽/г`;
                } else {
                    info += `\n✓ Следующий порог достигнут!`;
                }
            } else {
                info += '\n✓ Минимальная цена!';
            }

            elements.quantityInfo.textContent = info;
        } else {
            elements.quantityInfo.textContent = '';
        }
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
            id: Date.now(),
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

        bouquetItems.forEach(item => {
            const itemPrice = calculateItemPrice(item);
            const itemEl = document.createElement('div');
            itemEl.className = 'bouquet-item';
            itemEl.innerHTML = `
                <div class="bouquet-item-info">
                    <div class="bouquet-item-name">${item.name}</div>
                    <div class="bouquet-item-category">${CATEGORY_LABELS[item.category]}</div>
                </div>
                <div class="bouquet-item-quantity">
                    <button class="quantity-btn" data-action="decrease" data-id="${item.id}">−</button>
                    <input type="number" class="quantity-input-small" value="${item.quantity}" min="1" data-id="${item.id}">
                    <button class="quantity-btn" data-action="increase" data-id="${item.id}">+</button>
                </div>
                <div class="bouquet-item-price">${formatCurrency(Math.round(itemPrice))}</div>
                <button class="bouquet-item-remove" data-id="${item.id}">
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <line x1="18" y1="6" x2="6" y2="18"/>
                        <line x1="6" y1="6" x2="18" y2="18"/>
                    </svg>
                </button>
            `;
            elements.bouquetItems.appendChild(itemEl);
        });

        // Обработчики для кнопок
        document.querySelectorAll('.bouquet-item-remove').forEach(btn => {
            btn.addEventListener('click', () => removeItem(parseInt(btn.dataset.id)));
        });

        document.querySelectorAll('.quantity-btn').forEach(btn => {
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

        document.querySelectorAll('.quantity-input-small').forEach(input => {
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
    function clearAll() {
        if (bouquetItems.length === 0) return;

        if (confirm('Очистить весь букет?')) {
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
        const saved = localStorage.getItem('barhat_bouquet_recipes');
        savedRecipes = saved ? JSON.parse(saved) : [];
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
                    <div class="recipe-name">${recipe.name}</div>
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

        // Обработчики
        document.querySelectorAll('.recipe-actions button').forEach(btn => {
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
            items: [...bouquetItems],
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

        bouquetItems = [...recipe.items];
        renderBouquetItems();
        updateSummary();
    }

    /**
     * Удалить рецепт
     */
    function deleteRecipe(index) {
        if (confirm('Удалить этот рецепт?')) {
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
        reader.onload = function(e) {
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

                if (confirm(`Импортировать ${imported.length} рецепт(ов)?`)) {
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
    let editablePriceList = JSON.parse(JSON.stringify(PRICE_LIST));

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
    function openAdminModal() {
        const password = prompt('Введите пароль для доступа к админке:');

        if (password === null) {
            return; // Отмена
        }

        if (password !== ADMIN_PASSWORD) {
            alert('Неверный пароль!');
            return;
        }

        // Загружаем текущий прайс-лист
        editablePriceList = JSON.parse(JSON.stringify(PRICE_LIST));
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

                const itemEl = document.createElement('div');
                itemEl.className = 'admin-item';
                itemEl.innerHTML = `
                    <div class="admin-item-name">${name}</div>
                    <input type="number" class="admin-item-price" value="${price}" step="0.01" data-name="${name}">
                    <button class="btn btn-primary btn-sm admin-item-save" data-name="${name}">✓</button>
                    <button class="admin-item-delete" data-name="${name}">
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
                <div class="admin-item-name">${getCategoryLabel(currentAdminCategory)} — объёмная скидка</div>
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

        // Обработчики для кнопок
        document.querySelectorAll('.admin-item-save').forEach(btn => {
            btn.addEventListener('click', () => saveItemPrice(btn));
        });

        document.querySelectorAll('.admin-item-delete:not([disabled])').forEach(btn => {
            btn.addEventListener('click', () => deleteItem(btn));
        });
    }

    /**
     * Сохранить цену товара
     */
    function saveItemPrice(btn) {
        const input = btn.parentElement.querySelector('.admin-item-price');
        const newValue = parseFloat(input.value);

        if (isNaN(newValue) || newValue < 0) {
            alert('Некорректная цена!');
            input.value = input.dataset.oldValue || input.defaultValue;
            return;
        }

        if (btn.dataset.name) {
            // Обычный товар
            const name = btn.dataset.name;
            editablePriceList[currentAdminCategory][name] = newValue;
        } else if (btn.dataset.tier !== undefined) {
            // Товар с объёмной скидкой
            const tierIndex = parseInt(btn.dataset.tier);
            editablePriceList[currentAdminCategory].tiers[tierIndex].price = newValue;
        } else if (btn.dataset.special === 'banana') {
            // Бананы
            editablePriceList[currentAdminCategory].price = newValue;
        }

        // Визуальная обратная связь
        btn.textContent = '✓';
        setTimeout(() => btn.textContent = '✓', 1000);
    }

    /**
     * Удалить товар
     */
    function deleteItem(btn) {
        if (!btn.dataset.name) return;

        const name = btn.dataset.name;

        if (!confirm(`Удалить "${name}" из прайс-листа?`)) {
            return;
        }

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
        reader.onload = function(e) {
            try {
                const imported = JSON.parse(e.target.result);

                // Проверяем структуру
                if (!imported.flowers || !imported.dried || !imported.packaging) {
                    throw new Error('Неверная структура прайс-листа');
                }

                if (confirm('Загрузить этот прайс-лист? Текущие изменения будут потеряны.')) {
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
    function applyPricelistChanges() {
        if (!confirm('Применить изменения прайс-листа? Калькулятор будет обновлён.')) {
            return;
        }

        // Обновляем глобальный PRICE_LIST
        PRICE_LIST = { ...editablePriceList };

        // Сохраняем в localStorage
        savePricelistToStorage();

        // Пересчитываем текущий букет
        renderBouquetItems();
        updateSummary();

        // Обновляем селекторы
        populateProductSelect(elements.categorySelect.value);

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
