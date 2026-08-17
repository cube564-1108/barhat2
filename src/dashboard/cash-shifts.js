/**
 * Кассовые смены БАРХАТ
 * Открытие, закрытие смен, учёт инкассаций
 */

(function() {
    'use strict';

    // === Текущее состояние ===
    let currentShift = null;
    let currentShiftCollections = [];
    let currentShiftCollectionsTotal = 0;
    let storeList = [];
    let categoryList = [];
    let shiftsHistory = [];
    let currentUserData = null;
    let expectedBalance = 0;  // Ожидаемый остаток при закрытии
    let editingShiftId = null;  // Смена, открытая в модалке исправления

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
        shiftStoreSelectorWrap: null,
        shiftStoreSelector: null,
        shiftStoreName: null,
        shiftType: null,
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
        openShiftType: null,
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
        collectionComment: null,
        editShiftModal: null,
        editShiftOverlay: null,
        closeEditShiftBtn: null,
        cancelEditShiftBtn: null,
        confirmEditShiftBtn: null,
        editShiftBalance: null,
        editShiftCollections: null,
        editShiftNewCategory: null,
        editShiftNewAmount: null,
        editShiftNewComment: null,
        addEditShiftCollectionBtn: null,
        balancesCard: null,
        balancesTbody: null,
        shiftJournalModal: null,
        shiftJournalOverlay: null,
        closeShiftJournalBtn: null,
        closeShiftJournalFooterBtn: null,
        journalOpenedBy: null,
        journalClosedBy: null,
        journalOrdersTbody: null,
        journalCollectionsTbody: null
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
        elements.shiftStoreSelectorWrap = document.getElementById('shift-store-selector-wrap');
        elements.shiftStoreSelector = document.getElementById('shift-store-selector');
        elements.shiftStoreName = document.getElementById('shift-store-name');
        elements.shiftType = document.getElementById('shift-type');
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
        elements.openShiftType = document.getElementById('open-shift-type');
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

        elements.editShiftModal = document.getElementById('edit-shift-modal');
        elements.editShiftOverlay = document.getElementById('edit-shift-overlay');
        elements.closeEditShiftBtn = document.getElementById('close-edit-shift-btn');
        elements.cancelEditShiftBtn = document.getElementById('cancel-edit-shift-btn');
        elements.confirmEditShiftBtn = document.getElementById('confirm-edit-shift-btn');
        elements.editShiftBalance = document.getElementById('edit-shift-balance');
        elements.editShiftCollections = document.getElementById('edit-shift-collections');
        elements.editShiftNewCategory = document.getElementById('edit-shift-new-category');
        elements.editShiftNewAmount = document.getElementById('edit-shift-new-amount');
        elements.editShiftNewComment = document.getElementById('edit-shift-new-comment');
        elements.addEditShiftCollectionBtn = document.getElementById('add-edit-shift-collection-btn');

        elements.balancesCard = document.getElementById('balances-card');
        elements.balancesTbody = document.getElementById('balances-tbody');

        elements.shiftJournalModal = document.getElementById('shift-journal-modal');
        elements.shiftJournalOverlay = document.getElementById('shift-journal-overlay');
        elements.closeShiftJournalBtn = document.getElementById('close-shift-journal-btn');
        elements.closeShiftJournalFooterBtn = document.getElementById('close-shift-journal-footer-btn');
        elements.journalOpenedBy = document.getElementById('journal-opened-by');
        elements.journalClosedBy = document.getElementById('journal-closed-by');
        elements.journalOrdersTbody = document.getElementById('journal-orders-tbody');
        elements.journalCollectionsTbody = document.getElementById('journal-collections-tbody');

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

        // Выбор точки для просмотра текущей смены (админ/менеджер)
        if (elements.shiftStoreSelector) {
            elements.shiftStoreSelector.addEventListener('change', () => {
                loadCurrentShift();
                loadShiftsHistory();
            });
        }

        // Исправление закрытой смены
        if (elements.closeEditShiftBtn) {
            elements.closeEditShiftBtn.addEventListener('click', closeEditShiftModal);
        }
        if (elements.cancelEditShiftBtn) {
            elements.cancelEditShiftBtn.addEventListener('click', closeEditShiftModal);
        }
        if (elements.confirmEditShiftBtn) {
            elements.confirmEditShiftBtn.addEventListener('click', onSaveEditShift);
        }
        if (elements.editShiftOverlay) {
            elements.editShiftOverlay.addEventListener('click', closeEditShiftModal);
        }
        if (elements.addEditShiftCollectionBtn) {
            elements.addEditShiftCollectionBtn.addEventListener('click', onAddEditShiftCollection);
        }

        // Журнал смены
        if (elements.closeShiftJournalBtn) {
            elements.closeShiftJournalBtn.addEventListener('click', closeShiftJournalModal);
        }
        if (elements.closeShiftJournalFooterBtn) {
            elements.closeShiftJournalFooterBtn.addEventListener('click', closeShiftJournalModal);
        }
        if (elements.shiftJournalOverlay) {
            elements.shiftJournalOverlay.addEventListener('click', closeShiftJournalModal);
        }

        // Делегирование клика по кнопкам "Исправить"/"Журнал" в истории смен
        if (elements.shiftsTbody) {
            elements.shiftsTbody.addEventListener('click', (e) => {
                const editBtn = e.target.closest('[data-edit-shift-id]');
                if (editBtn) {
                    openEditShiftModal(parseInt(editBtn.dataset.editShiftId, 10));
                    return;
                }
                const journalBtn = e.target.closest('[data-journal-shift-id]');
                if (journalBtn) {
                    openShiftJournalModal(parseInt(journalBtn.dataset.journalShiftId, 10));
                }
            });
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

        // Остатки по кассам — только для админа
        if (elements.balancesCard) {
            elements.balancesCard.style.display = currentUserData && currentUserData.role === 'admin' ? 'block' : 'none';
        }
        if (currentUserData && currentUserData.role === 'admin') {
            loadBalances();
        }
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
                const method = options.method || 'GET';
                throw new Error(`${error.error || 'Ошибка запроса'} (${method} ${url})`);
            }

            return await response.json();
        } catch (error) {
            console.error('[CashShifts] API error:', error);
            if (!options.silent) {
                showNotification(error.message, 'error');
            }
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

            // Для не-флориста (админ/менеджер) — селектор точки для просмотра текущей смены,
            // т.к. у них нет единственной "своей" точки
            if (currentUserData && currentUserData.role !== 'florist' && elements.shiftStoreSelectorWrap) {
                elements.shiftStoreSelectorWrap.style.display = 'block';
                if (elements.shiftStoreSelector) {
                    elements.shiftStoreSelector.innerHTML = '<option value="">-- Выберите точку --</option>';
                    storeList.forEach(store => {
                        const option = document.createElement('option');
                        option.value = store.id;
                        option.textContent = store.name;
                        elements.shiftStoreSelector.appendChild(option);
                    });
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
            // Определяем ID точки: у флориста точка фиксирована, у остальных — выбирается вручную
            let storeId = null;
            if (currentUserData && currentUserData.role === 'florist' && currentUserData.store_id) {
                storeId = currentUserData.store_id;
            } else if (elements.shiftStoreSelector && elements.shiftStoreSelector.value) {
                storeId = parseInt(elements.shiftStoreSelector.value, 10);
            }

            if (!storeId) {
                currentShift = null;
                renderNoShift();
                return;
            }

            const result = await apiRequest(`/api/cash-shifts/open/${storeId}`);

            if (result.shift) {
                currentShift = result.shift;
                currentShiftCollections = result.collections || [];
                currentShiftCollectionsTotal = result.collections_total || 0;
                renderCurrentShift();
            } else {
                currentShift = null;
                currentShiftCollections = [];
                currentShiftCollectionsTotal = 0;
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

            // Определяем ID точки: у флориста точка фиксирована, у остальных — выбирается вручную
            let storeId = null;
            if (currentUserData && currentUserData.role === 'florist' && currentUserData.store_id) {
                storeId = currentUserData.store_id;
            } else if (elements.shiftStoreSelector && elements.shiftStoreSelector.value) {
                storeId = parseInt(elements.shiftStoreSelector.value, 10);
            }

            if (storeId) {
                params.append('store_id', storeId);
            }

            // Фильтр по дате
            const dateFilter = elements.shiftDateFilter?.value;
            if (dateFilter) {
                params.append('date', dateFilter);
            }

            params.append('status', 'closed');
            params.append('limit', '50');

            // Если точка не определена, а у пользователя их несколько — бэкенд попросит
            // явно выбрать точку в селекторе; это ожидаемо, не показываем ошибку
            const silent = !storeId && currentUserData && currentUserData.role !== 'admin';
            const result = await apiRequest(`/api/cash-shifts?${params}`, { silent });
            shiftsHistory = result.shifts || [];
            renderShiftsHistory();

            console.log('[CashShifts] История смен загружена:', shiftsHistory.length);
        } catch (error) {
            console.error('[CashShifts] Ошибка загрузки истории:', error);
            shiftsHistory = [];
            renderShiftsHistory();
        }
    }

    // === Загрузка остатков по точкам ===
    async function loadBalances() {
        try {
            const result = await apiRequest('/api/cash-shifts/balances');
            renderBalances(result.balances || []);
        } catch (error) {
            console.error('[CashShifts] Ошибка загрузки остатков:', error);
        }
    }

    // === Рендер остатков по точкам ===
    function renderBalances(balances) {
        if (!elements.balancesTbody) return;

        if (balances.length === 0) {
            elements.balancesTbody.innerHTML = `
                <tr>
                    <td colspan="4" style="text-align: center; color: var(--barkhat-gray); padding: 20px;">
                        Нет данных
                    </td>
                </tr>
            `;
            return;
        }

        elements.balancesTbody.innerHTML = balances.map(b => {
            const statusLabel = b.has_open_shift
                ? '<span style="color: var(--barkhat-warning);">Смена открыта</span>'
                : '<span style="color: var(--barkhat-success);">Смена закрыта</span>';

            return `
                <tr>
                    <td>${b.store_name}</td>
                    <td>${b.actual_balance != null ? formatMoney(b.actual_balance) : '—'}</td>
                    <td>${b.updated_at ? formatDateTime(b.updated_at) : '—'}</td>
                    <td>${statusLabel}</td>
                </tr>
            `;
        }).join('');
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
            elements.currentShiftTime.textContent = formatTime(currentShift.datetime_start);
        }

        const sales = currentShift.cash_orders_total || 0;
        const collections = currentShiftCollectionsTotal || 0;
        const expected = (currentShift.opening_balance || 0) + sales - collections;

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
        if (elements.shiftType) {
            const shiftTypeLabel = currentShift.shift_type === 'day' ? 'Дневная' : 'Ночная';
            elements.shiftType.textContent = shiftTypeLabel;
        }
        if (elements.shiftStartTime) {
            elements.shiftStartTime.textContent = formatDateTime(currentShift.datetime_start);
        }
        if (elements.shiftStartBalance) {
            elements.shiftStartBalance.textContent = formatMoney(currentShift.opening_balance);
        }

        // Таблица инкассаций
        renderCollections(currentShiftCollections);

        // Кнопки действий
        renderShiftActions();
    }

    // === Рендер состояния без смены ===
    function renderNoShift() {
        currentShiftCollections = [];
        currentShiftCollectionsTotal = 0;

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
            return `
                <tr>
                    <td>${formatDateTime(c.date)}</td>
                    <td>${c.category_name || '—'}</td>
                    <td>${formatMoney(c.amount)}</td>
                    <td>${c.custom_comment || '—'}</td>
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
                    <td colspan="11" style="text-align: center; color: var(--barkhat-gray); padding: 20px;">
                        Нет закрытых смен
                    </td>
                </tr>
            `;
            return;
        }

        const role = currentUserData && currentUserData.role;

        elements.shiftsTbody.innerHTML = shiftsHistory.map((shift, index) => {
            const store = storeList.find(s => s.id === shift.store_id);
            const discrepancy = shift.discrepancy || 0;
            const discrepancyClass = discrepancy > 10 ? 'color: var(--barkhat-error);' :
                                      discrepancy < -10 ? 'color: var(--barkhat-warning);' :
                                      'color: var(--barkhat-success);';

            const shiftTypeLabel = shift.shift_type === 'day' ? 'Дневная' : 'Ночная';

            // Флорист может исправить только самую свежую закрытую смену своей точки
            // (список уже отфильтрован по store_id и отсортирован DESC бэкендом)
            const canEdit = role === 'admin' || (role === 'florist' && index === 0);
            const editBtnHtml = canEdit
                ? `<button class="btn btn-sm btn-secondary" data-edit-shift-id="${shift.id}">Исправить</button>`
                : '';
            const actionsCell = `
                <div style="display: flex; gap: 6px;">
                    <button class="btn btn-sm btn-secondary" data-journal-shift-id="${shift.id}">Журнал</button>
                    ${editBtnHtml}
                </div>
            `;

            return `
                <tr>
                    <td>${formatDate(shift.closed_at)}</td>
                    <td>${store ? store.name : '—'}</td>
                    <td>${shiftTypeLabel}</td>
                    <td>${formatTime(shift.datetime_start)}</td>
                    <td>${formatTime(shift.closed_at)}</td>
                    <td>${formatMoney(shift.opening_balance)}</td>
                    <td>${formatMoney(shift.actual_balance)}</td>
                    <td>${formatMoney(shift.cash_orders_total)}</td>
                    <td>${formatMoney(shift.collections_total)}</td>
                    <td style="${discrepancyClass}">${formatMoney(discrepancy)}</td>
                    <td>${actionsCell}</td>
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
        if (elements.openShiftType) {
            elements.openShiftType.value = 'day';  // По умолчанию дневная
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
        const shiftType = elements.openShiftType?.value || 'day';
        const balance = parseFloat(elements.openShiftBalance?.value);

        if (!storeId || !shiftType || isNaN(balance)) {
            showNotification('Заполните все поля', 'error');
            return;
        }

        try {
            const result = await apiRequest('/api/cash-shifts/open', {
                method: 'POST',
                body: JSON.stringify({
                    store_id: parseInt(storeId),
                    shift_type: shiftType,
                    starting_balance: balance
                })
            });

            showNotification('Смена открыта', 'success');
            closeOpenShiftModal();

            // Синхронизируем селектор точки, чтобы сразу увидеть открытую смену в виджете
            if (elements.shiftStoreSelector && currentUserData && currentUserData.role !== 'florist') {
                elements.shiftStoreSelector.value = storeId;
            }
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
        expectedBalance = (currentShift.opening_balance || 0) +
                        (currentShift.cash_orders_total || 0) -
                        (currentShiftCollectionsTotal || 0);

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
            if (currentUserData && currentUserData.role === 'admin') {
                loadBalances();
            }

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
                    expense_category_id: parseInt(categoryId),
                    amount: amount,
                    custom_comment: comment
                })
            });

            showNotification('Инкассация добавлена', 'success');
            closeAddCollectionModal();
            loadCurrentShift();

        } catch (error) {
            console.error('[CashShifts] Ошибка добавления инкассации:', error);
        }
    }

    // === Открытие модального окна исправления смены ===
    async function openEditShiftModal(shiftId) {
        if (!elements.editShiftModal) return;

        try {
            const result = await apiRequest(`/api/cash-shifts/${shiftId}`);
            const shift = result.shift;
            const collections = result.collections || [];

            editingShiftId = shiftId;

            if (elements.editShiftBalance) {
                elements.editShiftBalance.value = shift.actual_balance ?? '';
            }

            if (elements.editShiftCollections) {
                if (collections.length === 0) {
                    elements.editShiftCollections.innerHTML = `<p class="form-hint">У смены нет инкассаций</p>`;
                } else {
                    elements.editShiftCollections.innerHTML = collections.map(c => `
                        <div class="form-group" style="display: flex; align-items: center; gap: 8px;">
                            <span style="flex: 1;">${c.category_name || '—'}</span>
                            <input type="number" class="form-input" style="width: 140px;"
                                   min="0" step="0.01"
                                   data-collection-id="${c.id}"
                                   value="${c.amount}">
                        </div>
                    `).join('');
                }
            }

            // Форма добавления новой инкассации
            if (elements.editShiftNewCategory) {
                elements.editShiftNewCategory.innerHTML = '<option value="">-- Категория --</option>' +
                    categoryList.map(cat => `<option value="${cat.id}">${cat.name}</option>`).join('');
            }
            if (elements.editShiftNewAmount) {
                elements.editShiftNewAmount.value = '';
            }
            if (elements.editShiftNewComment) {
                elements.editShiftNewComment.value = '';
            }

            elements.editShiftModal.classList.add('active');
        } catch (error) {
            console.error('[CashShifts] Ошибка загрузки смены для исправления:', error);
        }
    }

    // === Закрытие модального окна исправления смены ===
    function closeEditShiftModal() {
        if (!elements.editShiftModal) return;
        elements.editShiftModal.classList.remove('active');
        editingShiftId = null;
    }

    // === Добавление новой инкассации в редактируемую смену ===
    async function onAddEditShiftCollection() {
        if (!editingShiftId) return;

        const categoryId = elements.editShiftNewCategory?.value;
        const amount = parseFloat(elements.editShiftNewAmount?.value);
        const comment = elements.editShiftNewComment?.value;

        if (!categoryId || isNaN(amount)) {
            showNotification('Заполните категорию и сумму', 'error');
            return;
        }

        try {
            await apiRequest(`/api/cash-shifts/${editingShiftId}/collections`, {
                method: 'POST',
                body: JSON.stringify({
                    expense_category_id: parseInt(categoryId, 10),
                    amount: amount,
                    custom_comment: comment
                })
            });

            showNotification('Инкассация добавлена', 'success');
            await openEditShiftModal(editingShiftId);

        } catch (error) {
            console.error('[CashShifts] Ошибка добавления инкассации в смену:', error);
        }
    }

    // === Открытие журнала смены ===
    async function openShiftJournalModal(shiftId) {
        if (!elements.shiftJournalModal) return;

        elements.shiftJournalModal.classList.add('active');

        if (elements.journalOpenedBy) elements.journalOpenedBy.textContent = 'Загрузка...';
        if (elements.journalClosedBy) elements.journalClosedBy.textContent = 'Загрузка...';

        try {
            const result = await apiRequest(`/api/cash-shifts/${shiftId}`);
            const shift = result.shift;
            const collections = result.collections || [];
            const cashOrders = result.cash_orders || [];

            if (elements.journalOpenedBy) {
                elements.journalOpenedBy.textContent = shift.opened_by_full_name || shift.florist_username || '—';
            }
            if (elements.journalClosedBy) {
                elements.journalClosedBy.textContent = shift.closed_by_full_name || shift.closed_by_username || '—';
            }

            if (elements.journalOrdersTbody) {
                if (cashOrders.length === 0) {
                    elements.journalOrdersTbody.innerHTML = `
                        <tr><td colspan="3" style="text-align: center; color: var(--barkhat-gray); padding: 12px;">Нет наличных заказов</td></tr>
                    `;
                } else {
                    elements.journalOrdersTbody.innerHTML = cashOrders.map(o => `
                        <tr>
                            <td>${o.retailcrm_order_id}</td>
                            <td>${formatMoney(o.amount)}</td>
                            <td>${formatDateTime(o.paid_at)}</td>
                        </tr>
                    `).join('');
                }
            }

            if (elements.journalCollectionsTbody) {
                if (collections.length === 0) {
                    elements.journalCollectionsTbody.innerHTML = `
                        <tr><td colspan="5" style="text-align: center; color: var(--barkhat-gray); padding: 12px;">Нет инкассаций</td></tr>
                    `;
                } else {
                    elements.journalCollectionsTbody.innerHTML = collections.map(c => `
                        <tr>
                            <td>${formatDateTime(c.date)}</td>
                            <td>${c.category_name || '—'}</td>
                            <td>${formatMoney(c.amount)}</td>
                            <td>${c.created_by_full_name || c.created_by || '—'}</td>
                            <td>${c.custom_comment || '—'}</td>
                        </tr>
                    `).join('');
                }
            }
        } catch (error) {
            console.error('[CashShifts] Ошибка загрузки журнала смены:', error);
        }
    }

    // === Закрытие журнала смены ===
    function closeShiftJournalModal() {
        if (!elements.shiftJournalModal) return;
        elements.shiftJournalModal.classList.remove('active');
    }

    // === Сохранение исправлений смены ===
    async function onSaveEditShift() {
        if (!editingShiftId) return;

        const actualBalance = parseFloat(elements.editShiftBalance?.value);
        if (isNaN(actualBalance)) {
            showNotification('Введите фактический остаток', 'error');
            return;
        }

        const collections = [];
        if (elements.editShiftCollections) {
            elements.editShiftCollections.querySelectorAll('[data-collection-id]').forEach(input => {
                collections.push({
                    id: parseInt(input.dataset.collectionId, 10),
                    amount: parseFloat(input.value)
                });
            });
        }

        try {
            await apiRequest(`/api/cash-shifts/${editingShiftId}`, {
                method: 'PUT',
                body: JSON.stringify({
                    actual_balance: actualBalance,
                    collections: collections
                })
            });

            await apiRequest(`/api/cash-shifts/${editingShiftId}/reclose`, {
                method: 'POST'
            });

            showNotification('Смена исправлена и пересчитана', 'success');
            closeEditShiftModal();
            loadShiftsHistory();
            if (currentUserData && currentUserData.role === 'admin') {
                loadBalances();
            }

        } catch (error) {
            console.error('[CashShifts] Ошибка исправления смены:', error);
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
