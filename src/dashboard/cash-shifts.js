/**
 * Кассовые смены БАРХАТ
 * Открытие, закрытие смен, учёт инкассаций
 */

(function() {
    'use strict';

    // === Текущее состояние ===
    let currentShift = null;
    let storeList = [];
    let categoryList = [];
    let shiftsHistory = [];
    let currentUserData = null;
    let expectedBalance = 0;  // Ожидаемый остаток при закрытии

    // === DOM элементы ===
    const elements = {
        cashShiftsNav: null,
        cashShiftsPage: null,
        shiftActions: null,
        currentShiftInfo: null,
        noShiftInfo: null,
        currentShiftStatus: null,
        currentShiftTime: null,
        currentShiftSales: null,
        currentShiftCollections: null,
        currentShiftExpected: null,
        shiftStoreName: null,
        shiftStartTime: null,
        shiftStartBalance: null,
        collectionsTbody: null,
        shiftsTbody: null,
        shiftDateFilter: null,
        applyDateFilterBtn: null,
        // Модальные окна
        openShiftModal: null,
        openShiftOverlay: null,
        closeOpenShiftBtn: null,
        cancelOpenShiftBtn: null,
        confirmOpenShiftBtn: null,
        openShiftStore: null,
        openShiftBalance: null,
        closeShiftModal: null,
        closeShiftOverlay: null,
        closeCloseShiftBtn: null,
        cancelCloseShiftBtn: null,
        confirmCloseShiftBtn: null,
        closeShiftBalance: null,
        discrepancyCalc: null,
        expectedBalanceEl: null,
        actualBalanceEl: null,
        discrepancyResult: null,
        addCollectionModal: null,
        addCollectionOverlay: null,
        closeAddCollectionBtn: null,
        cancelAddCollectionBtn: null,
        confirmAddCollectionBtn: null,
        collectionCategory: null,
        collectionAmount: null,
        collectionComment: null
    };

    // === Инициализация ===
    function init() {
        // Получаем элементы DOM
        elements.cashShiftsNav = document.getElementById('cash-shifts-nav');
        elements.cashShiftsPage = document.querySelector('.page[data-page="cash_shifts"]');
        elements.shiftActions = document.getElementById('shift-actions');
        elements.currentShiftInfo = document.getElementById('current-shift-info');
        elements.noShiftInfo = document.getElementById('no-shift-info');
        elements.currentShiftStatus = document.getElementById('current-shift-status');
        elements.currentShiftTime = document.getElementById('current-shift-time');
        elements.currentShiftSales = document.getElementById('current-shift-sales');
        elements.currentShiftCollections = document.getElementById('current-shift-collections');
        elements.currentShiftExpected = document.getElementById('current-shift-expected');
        elements.shiftStoreName = document.getElementById('shift-store-name');
        elements.shiftStartTime = document.getElementById('shift-start-time');
        elements.shiftStartBalance = document.getElementById('shift-start-balance');
        elements.collectionsTbody = document.getElementById('collections-tbody');
        elements.shiftsTbody = document.getElementById('shifts-tbody');
        elements.shiftDateFilter = document.getElementById('shift-date-filter');
        elements.applyDateFilterBtn = document.getElementById('apply-date-filter');

        // Модальные окна
        elements.openShiftModal = document.getElementById('open-shift-modal');
        elements.openShiftOverlay = document.getElementById('open-shift-overlay');
        elements.closeOpenShiftBtn = document.getElementById('close-open-shift-btn');
        elements.cancelOpenShiftBtn = document.getElementById('cancel-open-shift-btn');
        elements.confirmOpenShiftBtn = document.getElementById('confirm-open-shift-btn');
        elements.openShiftStore = document.getElementById('open-shift-store');
        elements.openShiftBalance = document.getElementById('open-shift-balance');

        elements.closeShiftModal = document.getElementById('close-shift-modal');
        elements.closeShiftOverlay = document.getElementById('close-shift-overlay');
        elements.closeCloseShiftBtn = document.getElementById('close-close-shift-btn');
        elements.cancelCloseShiftBtn = document.getElementById('cancel-close-shift-btn');
        elements.confirmCloseShiftBtn = document.getElementById('confirm-close-shift-btn');
        elements.closeShiftBalance = document.getElementById('close-shift-balance');
        elements.discrepancyCalc = document.getElementById('discrepancy-calc');
        elements.expectedBalanceEl = document.getElementById('expected-balance');
        elements.actualBalanceEl = document.getElementById('actual-balance');
        elements.discrepancyResult = document.getElementById('discrepancy-result');

        elements.addCollectionModal = document.getElementById('add-collection-modal');
        elements.addCollectionOverlay = document.getElementById('add-collection-overlay');
        elements.closeAddCollectionBtn = document.getElementById('close-add-collection-btn');
        elements.cancelAddCollectionBtn = document.getElementById('cancel-add-collection-btn');
        elements.confirmAddCollectionBtn = document.getElementById('confirm-add-collection-btn');
        elements.collectionCategory = document.getElementById('collection-category');
        elements.collectionAmount = document.getElementById('collection-amount');
        elements.collectionComment = document.getElementById('collection-comment');

        // Привязка событий
        bindEvents();

        console.log('[CashShifts] Модуль инициализирован');
    }

    // === Привязка событий ===
    function bindEvents() {
        // Открытие смены
        if (elements.closeOpenShiftBtn) {
            elements.closeOpenShiftBtn.addEventListener('click', closeOpenShiftModal);
        }
        if (elements.cancelOpenShiftBtn) {
            elements.cancelOpenShiftBtn.addEventListener('click', closeOpenShiftModal);
        }
        if (elements.confirmOpenShiftBtn) {
            elements.confirmOpenShiftBtn.addEventListener('click', onOpenShift);
        }
        if (elements.openShiftOverlay) {
            elements.openShiftOverlay.addEventListener('click', closeOpenShiftModal);
        }

        // Закрытие смены
        if (elements.closeCloseShiftBtn) {
            elements.closeCloseShiftBtn.addEventListener('click', closeCloseShiftModal);
        }
        if (elements.cancelCloseShiftBtn) {
            elements.cancelCloseShiftBtn.addEventListener('click', closeCloseShiftModal);
        }
        if (elements.confirmCloseShiftBtn) {
            elements.confirmCloseShiftBtn.addEventListener('click', onCloseShift);
        }
        if (elements.closeShiftOverlay) {
            elements.closeShiftOverlay.addEventListener('click', closeCloseShiftModal);
        }
        if (elements.closeShiftBalance) {
            elements.closeShiftBalance.addEventListener('input', calculateDiscrepancy);
        }

        // Добавление инкассации
        if (elements.closeAddCollectionBtn) {
            elements.closeAddCollectionBtn.addEventListener('click', closeAddCollectionModal);
        }
        if (elements.cancelAddCollectionBtn) {
            elements.cancelAddCollectionBtn.addEventListener('click', closeAddCollectionModal);
        }
        if (elements.confirmAddCollectionBtn) {
            elements.confirmAddCollectionBtn.addEventListener('click', onAddCollection);
        }
        if (elements.addCollectionOverlay) {
            elements.addCollectionOverlay.addEventListener('click', closeAddCollectionModal);
        }

        // Фильтр по дате
        if (elements.applyDateFilterBtn) {
            elements.applyDateFilterBtn.addEventListener('click', loadShiftsHistory);
        }
    }

    // === Загрузка данных при активации страницы ===
    function onPageActivated(userData) {
        console.log('[CashShifts] Страница активирована', userData);
        currentUserData = userData;

        // Загружаем данные
        loadStores();
        loadCategories();
        loadCurrentShift();
        loadShiftsHistory();
    }

    // === API запросы ===
    async function apiRequest(url, options = {}) {
        try {
            const response = await fetch(url, {
                ...options,
                credentials: 'include',
                headers: {
                    'Content-Type': 'application/json',
                    ...options.headers
                }
            });

            if (!response.ok) {
                const error = await response.json();
                throw new Error(error.error || 'Ошибка запроса');
            }

            return await response.json();
        } catch (error) {
            console.error('[CashShifts] API error:', error);
            showNotification(error.message, 'error');
            throw error;
        }
    }

    // === Загрузка точек продаж ===
    async function loadStores() {
        try {
            const result = await apiRequest('/api/cash-shifts/stores');
            storeList = result.stores || [];

            // Заполняем селектор
            if (elements.openShiftStore) {
                elements.openShiftStore.innerHTML = '<option value="">-- Выберите салон --</option>';
                storeList.forEach(store => {
                    const option = document.createElement('option');
                    option.value = store.id;
                    option.textContent = store.name;
                    elements.openShiftStore.appendChild(option);
                });
            }

            // Для флориста оставляем только его точку
            if (currentUserData && currentUserData.role === 'florist' && currentUserData.store_id) {
                if (elements.openShiftStore) {
                    elements.openShiftStore.value = currentUserData.store_id;
                    elements.openShiftStore.disabled = true;
                }
            }

            console.log('[CashShifts] Загружены точки продаж:', storeList.length);
        } catch (error) {
            console.error('[CashShifts] Ошибка загрузки точек:', error);
        }
    }

    // === Загрузка категорий расходов ===
    async function loadCategories() {
        try {
            const result = await apiRequest('/api/cash-shifts/categories');
            categoryList = result.categories || [];

            // Заполняем селектор
            if (elements.collectionCategory) {
                elements.collectionCategory.innerHTML = '<option value="">-- Выберите категорию --</option>';
                categoryList.forEach(cat => {
                    const option = document.createElement('option');
                    option.value = cat.id;
                    option.textContent = cat.name;
                    elements.collectionCategory.appendChild(option);
                });
            }

            console.log('[CashShifts] Загружены категории:', categoryList.length);
        } catch (error) {
            console.error('[CashShifts] Ошибка загрузки категорий:', error);
        }
    }

    // === Загрузка текущей смены ===
    async function loadCurrentShift() {
        try {
            // Определяем ID точки
            let storeId = null;
            if (currentUserData && currentUserData.store_id) {
                storeId = currentUserData.store_id;
            }

            const params = new URLSearchParams({
                store_id: storeId || '',
                is_open: 'true',
                limit: '1'
            });

            const result = await apiRequest(`/api/cash-shifts?${params}`);
            const shifts = result.shifts || [];

            if (shifts.length > 0) {
                currentShift = shifts[0];
                renderCurrentShift();
            } else {
                currentShift = null;
                renderNoShift();
            }

            console.log('[CashShifts] Текущая смена загружена');
        } catch (error) {
            console.error('[CashShifts] Ошибка загрузки смены:', error);
            renderNoShift();
        }
    }

    // === Загрузка истории смен ===
    async function loadShiftsHistory() {
        try {
            const params = new URLSearchParams();

            if (currentUserData && currentUserData.store_id) {
                params.append('store_id', currentUserData.store_id);
            }

            // Фильтр по дате
            const dateFilter = elements.shiftDateFilter?.value;
            if (dateFilter) {
                params.append('date', dateFilter);
            }

            params.append('is_open', 'false');
            params.append('limit', '50');

            const result = await apiRequest(`/api/cash-shifts?${params}`);
            shiftsHistory = result.shifts || [];
            renderShiftsHistory();

            console.log('[CashShifts] История смен загружена:', shiftsHistory.length);
        } catch (error) {
            console.error('[CashShifts] Ошибка загрузки истории:', error);
        }
    }

    // === Рендер текущей смены ===
    function renderCurrentShift() {
        if (!currentShift) {
            renderNoShift();
            return;
        }

        // Показываем инфо
        if (elements.currentShiftInfo) {
            elements.currentShiftInfo.style.display = 'block';
        }
        if (elements.noShiftInfo) {
            elements.noShiftInfo.style.display = 'none';
        }

        // KPI
        if (elements.currentShiftStatus) {
            elements.currentShiftStatus.textContent = 'Открыта';
            elements.currentShiftStatus.style.color = 'var(--barkhat-success)';
        }
        if (elements.currentShiftTime) {
            const startTime = new Date(currentShift.opened_at);
            elements.currentShiftTime.textContent = formatTime(startTime);
        }

        const sales = currentShift.cash_sales || 0;
        const collections = currentShift.total_collections || 0;
        const expected = (currentShift.starting_balance || 0) + sales - collections;

        if (elements.currentShiftSales) {
            elements.currentShiftSales.textContent = formatMoney(sales);
        }
        if (elements.currentShiftCollections) {
            elements.currentShiftCollections.textContent = formatMoney(collections);
        }
        if (elements.currentShiftExpected) {
            elements.currentShiftExpected.textContent = formatMoney(expected);
        }

        // Детали смены
        if (elements.shiftStoreName) {
            const store = storeList.find(s => s.id === currentShift.store_id);
            elements.shiftStoreName.textContent = store ? store.name : '—';
        }
        if (elements.shiftStartTime) {
            elements.shiftStartTime.textContent = formatDateTime(currentShift.opened_at);
        }
        if (elements.shiftStartBalance) {
            elements.shiftStartBalance.textContent = formatMoney(currentShift.starting_balance);
        }

        // Таблица инкассаций
        renderCollections(currentShift.collections || []);

        // Кнопки действий
        renderShiftActions();
    }

    // === Рендер состояния без смены ===
    function renderNoShift() {
        if (elements.currentShiftInfo) {
            elements.currentShiftInfo.style.display = 'none';
        }
        if (elements.noShiftInfo) {
            elements.noShiftInfo.style.display = 'block';
        }

        // KPI
        if (elements.currentShiftStatus) {
            elements.currentShiftStatus.textContent = 'Нет смены';
            elements.currentShiftStatus.style.color = 'var(--barkhat-gray)';
        }
        if (elements.currentShiftTime) {
            elements.currentShiftTime.textContent = '';
        }
        if (elements.currentShiftSales) {
            elements.currentShiftSales.textContent = '— ₽';
        }
        if (elements.currentShiftCollections) {
            elements.currentShiftCollections.textContent = '— ₽';
        }
        if (elements.currentShiftExpected) {
            elements.currentShiftExpected.textContent = '— ₽';
        }

        // Кнопки действий
        renderShiftActions();
    }

    // === Рендер кнопок действий ===
    function renderShiftActions() {
        if (!elements.shiftActions) return;

        elements.shiftActions.innerHTML = '';

        if (currentShift) {
            // Есть открытая смена
            const closeBtn = document.createElement('button');
            closeBtn.className = 'btn btn-primary';
            closeBtn.innerHTML = `
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M9 14l-4-4 4-4"/>
                    <path d="M5 10h14"/>
                    <circle cx="12" cy="12" r="9"/>
                    <path d="M12 8v8"/>
                </svg>
                Закрыть смену
            `;
            closeBtn.addEventListener('click', openCloseShiftModal);
            elements.shiftActions.appendChild(closeBtn);

            const addCollectionBtn = document.createElement('button');
            addCollectionBtn.className = 'btn btn-secondary';
            addCollectionBtn.innerHTML = `
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <line x1="12" y1="5" x2="12" y2="19"/>
                    <line x1="5" y1="12" x2="19" y2="12"/>
                </svg>
                Инкассация
            `;
            addCollectionBtn.addEventListener('click', openAddCollectionModal);
            elements.shiftActions.appendChild(addCollectionBtn);
        } else {
            // Нет открытой смены
            const openBtn = document.createElement('button');
            openBtn.className = 'btn btn-success';
            openBtn.innerHTML = `
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M12 5v14M5 12h14"/>
                </svg>
                Открыть смену
            `;
            openBtn.addEventListener('click', openOpenShiftModal);
            elements.shiftActions.appendChild(openBtn);
        }
    }

    // === Рендер инкассаций ===
    function renderCollections(collections) {
        if (!elements.collectionsTbody) return;

        if (collections.length === 0) {
            elements.collectionsTbody.innerHTML = `
                <tr>
                    <td colspan="4" style="text-align: center; color: var(--barkhat-gray); padding: 20px;">
                        Нет инкассаций
                    </td>
                </tr>
            `;
            return;
        }

        elements.collectionsTbody.innerHTML = collections.map(c => {
            const category = categoryList.find(cat => cat.id === c.category_id);
            return `
                <tr>
                    <td>${formatDateTime(c.created_at)}</td>
                    <td>${category ? category.name : '—'}</td>
                    <td>${formatMoney(c.amount)}</td>
                    <td>${c.comment || '—'}</td>
                </tr>
            `;
        }).join('');
    }

    // === Рендер истории смен ===
    function renderShiftsHistory() {
        if (!elements.shiftsTbody) return;

        if (shiftsHistory.length === 0) {
            elements.shiftsTbody.innerHTML = `
                <tr>
                    <td colspan="7" style="text-align: center; color: var(--barkhat-gray); padding: 20px;">
                        Нет закрытых смен
                    </td>
                </tr>
            `;
            return;
        }

        elements.shiftsTbody.innerHTML = shiftsHistory.map(shift => {
            const store = storeList.find(s => s.id === shift.store_id);
            const discrepancy = shift.discrepancy || 0;
            const discrepancyClass = discrepancy > 10 ? 'color: var(--barkhat-error);' :
                                      discrepancy < -10 ? 'color: var(--barkhat-warning);' :
                                      'color: var(--barkhat-success);';

            return `
                <tr>
                    <td>${formatDate(shift.closed_at)}</td>
                    <td>${store ? store.name : '—'}</td>
                    <td>${formatTime(shift.opened_at)}</td>
                    <td>${formatTime(shift.closed_at)}</td>
                    <td>${formatMoney(shift.cash_sales)}</td>
                    <td>${formatMoney(shift.total_collections)}</td>
                    <td style="${discrepancyClass}">${formatMoney(discrepancy)}</td>
                </tr>
            `;
        }).join('');
    }

    // === Открытие модального окна открытия смены ===
    function openOpenShiftModal() {
        if (!elements.openShiftModal) return;

        // Сброс формы
        if (elements.openShiftBalance) {
            elements.openShiftBalance.value = '';
        }

        // Для флориста уже выбрана его точка
        if (currentUserData && currentUserData.role === 'florist' && elements.openShiftStore) {
            elements.openShiftStore.value = currentUserData.store_id;
        }

        elements.openShiftModal.classList.add('active');
    }

    // === Закрытие модального окна открытия смены ===
    function closeOpenShiftModal() {
        if (!elements.openShiftModal) return;
        elements.openShiftModal.classList.remove('active');
    }

    // === Обработка открытия смены ===
    async function onOpenShift() {
        const storeId = elements.openShiftStore?.value;
        const balance = parseFloat(elements.openShiftBalance?.value);

        if (!storeId || isNaN(balance)) {
            showNotification('Заполните все поля', 'error');
            return;
        }

        try {
            const result = await apiRequest('/api/cash-shifts/open', {
                method: 'POST',
                body: JSON.stringify({
                    store_id: parseInt(storeId),
                    starting_balance: balance
                })
            });

            showNotification('Смена открыта', 'success');
            closeOpenShiftModal();
            loadCurrentShift();

        } catch (error) {
            console.error('[CashShifts] Ошибка открытия смены:', error);
        }
    }

    // === Открытие модального окна закрытия смены ===
    function openCloseShiftModal() {
        if (!elements.closeShiftModal || !currentShift) return;

        // Сброс формы
        if (elements.closeShiftBalance) {
            elements.closeShiftBalance.value = '';
        }
        if (elements.discrepancyCalc) {
            elements.discrepancyCalc.style.display = 'none';
        }

        elements.closeShiftModal.classList.add('active');
    }

    // === Закрытие модального окна закрытия смены ===
    function closeCloseShiftModal() {
        if (!elements.closeShiftModal) return;
        elements.closeShiftModal.classList.remove('active');
    }

    // === Расчёт расхождения ===
    function calculateDiscrepancy() {
        if (!currentShift || !elements.closeShiftBalance) return;

        const actualBalance = parseFloat(elements.closeShiftBalance.value);
        if (isNaN(actualBalance)) return;

        // Ожидаемый остаток
        expectedBalance = (currentShift.starting_balance || 0) +
                        (currentShift.cash_sales || 0) -
                        (currentShift.total_collections || 0);

        const discrepancy = actualBalance - expectedBalance;

        // Показываем расчёт
        if (elements.discrepancyCalc) {
            elements.discrepancyCalc.style.display = 'block';
        }
        if (elements.expectedBalanceEl) {
            elements.expectedBalanceEl.textContent = formatMoney(expectedBalance);
        }
        if (elements.actualBalanceEl) {
            elements.actualBalanceEl.textContent = formatMoney(actualBalance);
        }
        if (elements.discrepancyResult) {
            const absDiscrepancy = Math.abs(discrepancy);
            const isSignificant = absDiscrepancy > 10;

            let style = '';
            let text = '';

            if (absDiscrepancy < 1) {
                style = 'background: var(--barkhat-success); color: white;';
                text = `✓ Сошлось (${formatMoney(discrepancy)})`;
            } else if (discrepancy > 0) {
                style = 'background: var(--barkhat-warning); color: white;';
                text = `Излишек ${formatMoney(discrepancy)}`;
            } else {
                style = 'background: var(--barkhat-error); color: white;';
                text = `Недостача ${formatMoney(discrepancy)}`;
            }

            elements.discrepancyResult.style.cssText = style;
            elements.discrepancyResult.textContent = text;
        }
    }

    // === Обработка закрытия смены ===
    async function onCloseShift() {
        const actualBalance = parseFloat(elements.closeShiftBalance?.value);

        if (isNaN(actualBalance)) {
            showNotification('Введите фактический остаток', 'error');
            return;
        }

        try {
            const result = await apiRequest(`/api/cash-shifts/${currentShift.id}/close`, {
                method: 'POST',
                body: JSON.stringify({
                    actual_balance: actualBalance
                })
            });

            showNotification('Смена закрыта', 'success');
            closeCloseShiftModal();
            currentShift = null;
            loadCurrentShift();
            loadShiftsHistory();

        } catch (error) {
            console.error('[CashShifts] Ошибка закрытия смены:', error);
        }
    }

    // === Открытие модального окна добавления инкассации ===
    function openAddCollectionModal() {
        if (!elements.addCollectionModal) return;

        // Сброс формы
        if (elements.collectionCategory) {
            elements.collectionCategory.value = '';
        }
        if (elements.collectionAmount) {
            elements.collectionAmount.value = '';
        }
        if (elements.collectionComment) {
            elements.collectionComment.value = '';
        }

        elements.addCollectionModal.classList.add('active');
    }

    // === Закрытие модального окна добавления инкассации ===
    function closeAddCollectionModal() {
        if (!elements.addCollectionModal) return;
        elements.addCollectionModal.classList.remove('active');
    }

    // === Обработка добавления инкассации ===
    async function onAddCollection() {
        const categoryId = elements.collectionCategory?.value;
        const amount = parseFloat(elements.collectionAmount?.value);
        const comment = elements.collectionComment?.value;

        if (!categoryId || isNaN(amount)) {
            showNotification('Заполните категорию и сумму', 'error');
            return;
        }

        try {
            const result = await apiRequest(`/api/cash-shifts/${currentShift.id}/collections`, {
                method: 'POST',
                body: JSON.stringify({
                    category_id: parseInt(categoryId),
                    amount: amount,
                    comment: comment
                })
            });

            showNotification('Инкассация добавлена', 'success');
            closeAddCollectionModal();
            loadCurrentShift();

        } catch (error) {
            console.error('[CashShifts] Ошибка добавления инкассации:', error);
        }
    }

    // === Утилиты ===
    function formatMoney(amount) {
        return new Intl.NumberFormat('ru-RU', {
            style: 'currency',
            currency: 'RUB',
            minimumFractionDigits: 0,
            maximumFractionDigits: 0
        }).format(amount || 0);
    }

    function formatDate(dateStr) {
        if (!dateStr) return '—';
        const date = new Date(dateStr);
        return date.toLocaleDateString('ru-RU');
    }

    function formatTime(dateStr) {
        if (!dateStr) return '—';
        const date = new Date(dateStr);
        return date.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
    }

    function formatDateTime(dateStr) {
        if (!dateStr) return '—';
        const date = new Date(dateStr);
        return date.toLocaleString('ru-RU', {
            day: '2-digit',
            month: '2-digit',
            year: '2-digit',
            hour: '2-digit',
            minute: '2-digit'
        });
    }

    function showNotification(message, type = 'info') {
        // TODO: реализовать уведомления
        console.log(`[CashShifts] Notification (${type}):`, message);
        alert(message);  // Временно
    }

    // === Экспорт публичных методов ===
    window.CashShiftsModule = {
        init,
        onPageActivated
    };

    // Инициализация при загрузке
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

})();
