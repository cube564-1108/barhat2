/**
 * Списания товара БАРХАТ
 * Флорист подаёт заявку (товар из каталога МойСклад + кол-во + фото + причина),
 * управляющий/админ согласует — заявка одним документом уходит в МойСклад.
 */

(function() {
    'use strict';

    let currentUserData = null;
    let storeList = [];
    let currentCatalog = [];
    let currentCatalogStoreId = null;
    let loaded = false;
    let currentDetailsWriteoffId = null;

    // Совпадает с APPROVER_ROLES в src/writeoffs/server.py — кто согласует заявки
    const APPROVER_ROLES = ['admin', 'manager'];

    const STATUS_LABELS = {
        on_approval: 'На согласовании',
        processing: 'Отправляется…',
        sent: 'Списано',
        failed: 'Ошибка отправки',
        rejected: 'Отклонено',
        cancelled: 'Отменено',
    };

    const STATUS_COLORS = {
        on_approval: { bg: '#fff3cd', color: '#856404' },
        processing: { bg: '#d1ecf1', color: '#0c5460' },
        sent: { bg: '#d4edda', color: '#155724' },
        failed: { bg: '#f8d7da', color: '#721c24' },
        rejected: { bg: '#f8d7da', color: '#721c24' },
        cancelled: { bg: '#eee', color: '#555' },
    };

    const elements = {};

    function init() {
        elements.tbody = document.getElementById('writeoffs-tbody');
        if (!elements.tbody) return; // Страницы нет в DOM — модуль не нужен

        elements.createBtn = document.getElementById('create-writeoff-btn');
        elements.mappingBtn = document.getElementById('writeoff-mapping-btn');

        elements.filterStore = document.getElementById('writeoffs-filter-store');
        elements.filterStatus = document.getElementById('writeoffs-filter-status');
        elements.filterDateFrom = document.getElementById('writeoffs-filter-date-from');
        elements.filterDateTo = document.getElementById('writeoffs-filter-date-to');
        elements.applyFiltersBtn = document.getElementById('writeoffs-apply-filters-btn');
        elements.resetFiltersBtn = document.getElementById('writeoffs-reset-filters-btn');

        elements.modal = document.getElementById('create-writeoff-modal');
        elements.overlay = document.getElementById('create-writeoff-overlay');
        elements.closeBtn = document.getElementById('close-create-writeoff-btn');
        elements.cancelBtn = document.getElementById('cancel-create-writeoff-btn');
        elements.confirmBtn = document.getElementById('confirm-create-writeoff-btn');
        elements.storeSelect = document.getElementById('writeoff-store');
        elements.positionsRows = document.getElementById('writeoff-positions-rows');
        elements.addPositionBtn = document.getElementById('writeoff-add-position-btn');

        elements.detailsModal = document.getElementById('writeoff-details-modal');
        elements.detailsOverlay = document.getElementById('writeoff-details-overlay');
        elements.closeDetailsBtn = document.getElementById('close-writeoff-details-btn');
        elements.detailsTitle = document.getElementById('writeoff-details-title');
        elements.detailsInfo = document.getElementById('writeoff-details-info');
        elements.detailsPositions = document.getElementById('writeoff-details-positions');
        elements.detailsActions = document.getElementById('writeoff-details-actions');

        elements.mappingModal = document.getElementById('writeoff-mapping-modal');
        elements.mappingOverlay = document.getElementById('writeoff-mapping-overlay');
        elements.closeMappingBtn = document.getElementById('close-writeoff-mapping-btn');
        elements.cancelMappingBtn = document.getElementById('cancel-writeoff-mapping-btn');
        elements.saveMappingBtn = document.getElementById('save-writeoff-mapping-btn');
        elements.mappingRows = document.getElementById('writeoff-mapping-rows');

        elements.mappingBtn?.addEventListener('click', openMappingModal);
        elements.closeMappingBtn?.addEventListener('click', closeMappingModal);
        elements.cancelMappingBtn?.addEventListener('click', closeMappingModal);
        elements.mappingOverlay?.addEventListener('click', closeMappingModal);
        elements.saveMappingBtn?.addEventListener('click', saveMapping);

        elements.createBtn?.addEventListener('click', openCreateModal);
        elements.closeBtn?.addEventListener('click', closeCreateModal);
        elements.cancelBtn?.addEventListener('click', closeCreateModal);
        elements.overlay?.addEventListener('click', closeCreateModal);
        elements.confirmBtn?.addEventListener('click', submitWriteoff);
        elements.addPositionBtn?.addEventListener('click', () => {
            elements.positionsRows.appendChild(createPositionRow());
        });
        elements.storeSelect?.addEventListener('change', async () => {
            await loadCatalogForStore(parseInt(elements.storeSelect.value, 10));
            elements.positionsRows.querySelectorAll('.writeoff-position-product').forEach(refreshProductInput);
        });

        elements.applyFiltersBtn?.addEventListener('click', loadWriteoffs);
        elements.resetFiltersBtn?.addEventListener('click', resetFilters);

        elements.closeDetailsBtn?.addEventListener('click', closeDetailsModal);
        elements.detailsOverlay?.addEventListener('click', closeDetailsModal);

        elements.guideApprover = document.getElementById('writeoff-guide-approver');
    }

    async function onPageActivated(userData) {
        currentUserData = userData;

        if (elements.mappingBtn) {
            elements.mappingBtn.style.display = currentUserData?.role === 'admin' ? '' : 'none';
        }

        // Блок инструкции про согласование — только тем, кто может согласовывать
        if (elements.guideApprover) {
            elements.guideApprover.hidden = !APPROVER_ROLES.includes(currentUserData?.role);
        }

        if (!loaded) {
            await loadStores();
            loaded = true;
        }

        await loadWriteoffs();
    }

    async function loadStores() {
        try {
            const res = await fetch('/api/writeoffs/stores', { credentials: 'include' });
            const data = await res.json();
            storeList = data.stores || [];

            elements.filterStore.innerHTML = '<option value="">Все</option>' +
                storeList.map(s => `<option value="${s.id}">${escapeHtml(s.name)}</option>`).join('');
            elements.storeSelect.innerHTML = storeList.map(s => `<option value="${s.id}">${escapeHtml(s.name)}</option>`).join('');
        } catch (e) {
            console.error('Ошибка загрузки точек:', e);
        }
    }

    function resetFilters() {
        elements.filterStore.value = '';
        elements.filterStatus.value = '';
        elements.filterDateFrom.value = '';
        elements.filterDateTo.value = '';
        loadWriteoffs();
    }

    async function loadWriteoffs() {
        elements.tbody.innerHTML = `<tr><td colspan="7" style="text-align:center; color: var(--barkhat-gray); padding:20px;">Загрузка данных...</td></tr>`;

        try {
            const params = new URLSearchParams();
            if (elements.filterStore.value) params.set('store_id', elements.filterStore.value);
            if (elements.filterStatus.value) params.set('status', elements.filterStatus.value);
            // Границы периода считаем в UTC — created_at в базе хранится в UTC,
            // а сотрудник выбирает даты по своим часам. Заодно date_to
            // разворачивается до конца дня: раньше уходила голая дата и
            // сравнение created_at <= '2026-08-20' отсекало весь выбранный день
            if (elements.filterDateFrom.value) params.set('date_from', window.BarhatTime.dayStartUtc(elements.filterDateFrom.value));
            if (elements.filterDateTo.value) params.set('date_to', window.BarhatTime.dayEndUtc(elements.filterDateTo.value));

            const res = await fetch(`/api/writeoffs?${params.toString()}`, { credentials: 'include' });
            const data = await res.json();
            renderWriteoffs(data.writeoffs || []);
        } catch (e) {
            console.error('Ошибка загрузки списаний:', e);
            elements.tbody.innerHTML = `<tr><td colspan="7" style="text-align:center; color: var(--barkhat-gray); padding:20px;">Ошибка загрузки</td></tr>`;
        }
    }

    function storeName(id) {
        return storeList.find(s => s.id === id)?.name || `#${id}`;
    }

    function canApprove(storeId) {
        if (!APPROVER_ROLES.includes(currentUserData?.role)) return false;
        return storeList.some(s => s.id === storeId);
    }

    function renderWriteoffs(writeoffs) {
        if (writeoffs.length === 0) {
            elements.tbody.innerHTML = `<tr><td colspan="7" style="text-align:center; color: var(--barkhat-gray); padding:20px;">Списаний нет</td></tr>`;
            return;
        }

        elements.tbody.innerHTML = writeoffs.map(w => {
            const colors = STATUS_COLORS[w.status] || { bg: '#eee', color: '#333' };
            const badge = `<span class="status-badge" style="background:${colors.bg}; color:${colors.color};">${STATUS_LABELS[w.status] || w.status}</span>`;

            let actions = `<button class="btn btn-sm btn-secondary" data-action="details" data-id="${w.id}">Детали</button>`;
            if (w.status === 'on_approval' && canApprove(w.store_id)) {
                actions += `
                    <button class="btn btn-sm btn-success" data-action="approve" data-id="${w.id}">Согласовать</button>
                    <button class="btn btn-sm btn-danger" data-action="reject" data-id="${w.id}">Отклонить</button>
                `;
            }
            if (w.status === 'failed' && canApprove(w.store_id)) {
                actions += `<button class="btn btn-sm btn-secondary" data-action="retry" data-id="${w.id}">Повторить</button>`;
            }
            if (w.status === 'on_approval' && currentUserData?.username === w.created_by) {
                actions += `<button class="btn btn-sm btn-danger" data-action="cancel" data-id="${w.id}">Отменить</button>`;
            }

            const createdDate = window.BarhatTime.formatDateTime(w.created_at, '');

            return `
                <tr>
                    <td>#${w.id}</td>
                    <td>${escapeHtml(storeName(w.store_id))}</td>
                    <td>${(w.positions || []).length}</td>
                    <td>${badge}</td>
                    <td>${escapeHtml(w.created_by_full_name || w.created_by || '')}</td>
                    <td>${createdDate}</td>
                    <td style="white-space: nowrap;">${actions}</td>
                </tr>
            `;
        }).join('');

        elements.tbody.querySelectorAll('button[data-action]').forEach(btn => {
            btn.addEventListener('click', () => {
                const id = btn.getAttribute('data-id');
                const action = btn.getAttribute('data-action');
                if (action === 'details') openDetailsModal(id);
                if (action === 'approve') approveWriteoff(id);
                if (action === 'reject') rejectWriteoff(id);
                if (action === 'retry') retryWriteoff(id);
                if (action === 'cancel') cancelWriteoff(id);
            });
        });
    }

    async function approveWriteoff(id) {
        const ok = await window.BarhatUI.confirm(
            'Товар спишется в МойСклад. Отменить согласование будет нельзя.',
            { title: 'Согласовать списание?', confirmText: 'Согласовать' }
        );
        if (!ok) return;
        try {
            const res = await fetch(`/api/writeoffs/${id}/approve`, { method: 'POST', credentials: 'include' });
            const data = await res.json();
            if (!res.ok) { alert(data.error || 'Ошибка согласования'); return; }
            if (data.writeoff?.status === 'failed') {
                alert(`Согласовано, но отправка в МойСклад не удалась: ${data.writeoff.moysklad_error || 'см. логи сервера'}. Можно повторить кнопкой «Повторить».`);
            }
            await loadWriteoffs();
        } catch (e) {
            console.error('Ошибка согласования списания:', e);
            alert('Ошибка согласования списания');
        }
    }

    async function retryWriteoff(id) {
        try {
            const res = await fetch(`/api/writeoffs/${id}/retry`, { method: 'POST', credentials: 'include' });
            const data = await res.json();
            if (!res.ok) { alert(data.error || 'Ошибка повтора'); return; }
            if (data.writeoff?.status === 'failed') {
                alert(`Отправка снова не удалась: ${data.writeoff.moysklad_error || 'см. логи сервера'}`);
            }
            await loadWriteoffs();
        } catch (e) {
            console.error('Ошибка повтора отправки:', e);
            alert('Ошибка повтора отправки');
        }
    }

    async function rejectWriteoff(id) {
        // null = нажали «Отмена». Раньше здесь было `prompt(...) || ''`, из-за
        // чего отмена диалога всё равно отклоняла заявку с пустой причиной.
        const answer = await window.BarhatUI.prompt('Причина отклонения (необязательно):', '', {
            title: 'Отклонить списание',
            confirmText: 'Отклонить',
        });
        if (answer === null) return;
        const reason = answer.trim();
        try {
            const res = await fetch(`/api/writeoffs/${id}/reject`, {
                method: 'POST',
                credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ reason }),
            });
            const data = await res.json();
            if (!res.ok) { alert(data.error || 'Ошибка отклонения'); return; }
            await loadWriteoffs();
        } catch (e) {
            console.error('Ошибка отклонения списания:', e);
            alert('Ошибка отклонения списания');
        }
    }

    async function cancelWriteoff(id) {
        const ok = await window.BarhatUI.confirm('Заявка будет удалена.', {
            title: 'Отменить свою заявку?',
            confirmText: 'Отменить заявку',
            cancelText: 'Не отменять',
            danger: true,
        });
        if (!ok) return;
        try {
            const res = await fetch(`/api/writeoffs/${id}`, { method: 'DELETE', credentials: 'include' });
            const data = await res.json();
            if (!res.ok) { alert(data.error || 'Ошибка отмены'); return; }
            await loadWriteoffs();
        } catch (e) {
            console.error('Ошибка отмены списания:', e);
            alert('Ошибка отмены списания');
        }
    }

    // =========================================================================
    // СОЗДАНИЕ ЗАЯВКИ
    // =========================================================================

    async function loadCatalogForStore(storeId) {
        if (!storeId) { currentCatalog = []; currentCatalogStoreId = null; return; }
        if (currentCatalogStoreId === storeId) return;
        try {
            const res = await fetch(`/api/writeoffs/catalog?store_id=${storeId}`, { credentials: 'include' });
            const data = await res.json();
            if (!res.ok) { alert(data.error || 'Не удалось загрузить остатки точки'); currentCatalog = []; return; }
            currentCatalog = data.items || [];
            currentCatalogStoreId = storeId;
        } catch (e) {
            console.error('Ошибка загрузки каталога:', e);
            currentCatalog = [];
        }
    }

    // =========================================================================
    // ПОИСК ТОВАРА (комбобокс «начни вводить — подскажет», как в МойСклад)
    //
    // Обычный <select> не годится: на складе больше сотни позиций, найти нужную
    // прокруткой дольше, чем набрать три буквы. Ищем и по названию, и по
    // артикулу (флористу привычнее набрать «k1»).
    // =========================================================================

    const SUGGEST_LIMIT = 50;

    let suggestBox = null;      // единственный выпадающий список на весь модуль
    let suggestInput = null;    // поле, к которому он сейчас привязан
    let suggestItems = [];
    let suggestIndex = -1;

    function injectSuggestStyles() {
        if (document.getElementById('writeoff-suggest-styles')) return;
        const style = document.createElement('style');
        style.id = 'writeoff-suggest-styles';
        style.textContent = `
.writeoff-suggest {
    position: fixed;
    z-index: 2100; /* выше .modal (2000) — список рисуется поверх окна */
    display: none;
    max-height: 280px;
    overflow-y: auto;
    background: var(--bx-white, #fff);
    border: 1px solid var(--bx-border, #eee2ea);
    border-radius: var(--bx-r-lg, 8px);
    box-shadow: 0 8px 24px rgba(65, 19, 48, 0.16);
}
.writeoff-suggest.active { display: block; }
.writeoff-suggest-item {
    padding: 8px 12px;
    cursor: pointer;
    font-size: 13px;
    color: var(--bx-text, #3C3C3C);
    display: flex;
    gap: 10px;
    align-items: baseline;
    justify-content: space-between;
}
.writeoff-suggest-item + .writeoff-suggest-item { border-top: 1px solid var(--bx-border, #eee2ea); }
.writeoff-suggest-item.active,
.writeoff-suggest-item:hover { background: var(--bx-pink-wash, #F3E3EE); }
.writeoff-suggest-code { color: var(--bx-muted, #9b8f97); white-space: nowrap; }
.writeoff-suggest-stock { color: var(--bx-text-2, #6F6F6F); white-space: nowrap; }
.writeoff-suggest-stock.empty { color: var(--bx-down, #c0322f); }
.writeoff-suggest-hl { background: #fff3a3; color: inherit; padding: 0; border-radius: 2px; }
.writeoff-suggest-empty { padding: 10px 12px; font-size: 13px; color: var(--bx-muted, #9b8f97); }
.writeoff-position-product.invalid { border-color: var(--bx-down, #c0322f); }
`;
        document.head.appendChild(style);
    }

    function getSuggestBox() {
        if (suggestBox) return suggestBox;
        injectSuggestStyles();
        suggestBox = document.createElement('div');
        suggestBox.className = 'writeoff-suggest';
        // В <body>, а не в строку позиции: у .modal-body стоит overflow-y:auto,
        // внутри него выпадающий список обрезался бы по краю окна
        document.body.appendChild(suggestBox);
        suggestBox.addEventListener('mousedown', (e) => {
            // mousedown, а не click: до blur поля, иначе список успеет закрыться
            const el = e.target.closest('.writeoff-suggest-item');
            if (!el || !suggestInput) return;
            e.preventDefault();
            pickSuggestion(suggestInput, suggestItems[parseInt(el.dataset.index, 10)]);
        });
        window.addEventListener('resize', positionSuggestBox);
        window.addEventListener('scroll', positionSuggestBox, true);
        return suggestBox;
    }

    function positionSuggestBox() {
        if (!suggestBox || !suggestInput || !suggestBox.classList.contains('active')) return;
        const rect = suggestInput.getBoundingClientRect();
        const below = window.innerHeight - rect.bottom;
        suggestBox.style.left = `${rect.left}px`;
        suggestBox.style.width = `${rect.width}px`;
        // Не хватает места снизу — раскрываем вверх
        if (below < 160 && rect.top > below) {
            suggestBox.style.top = 'auto';
            suggestBox.style.bottom = `${window.innerHeight - rect.top + 4}px`;
            suggestBox.style.maxHeight = `${Math.min(280, rect.top - 12)}px`;
        } else {
            suggestBox.style.bottom = 'auto';
            suggestBox.style.top = `${rect.bottom + 4}px`;
            suggestBox.style.maxHeight = `${Math.min(280, below - 12)}px`;
        }
    }

    function closeSuggestions() {
        if (!suggestBox) return;
        suggestBox.classList.remove('active');
        suggestItems = [];
        suggestIndex = -1;
        suggestInput = null;
    }

    function matchCatalog(query) {
        const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
        if (!terms.length) return currentCatalog.slice(0, SUGGEST_LIMIT);
        return currentCatalog
            .filter(item => {
                const haystack = `${item.product_name} ${item.article || ''} ${item.code || ''}`.toLowerCase();
                return terms.every(t => haystack.includes(t));
            })
            .slice(0, SUGGEST_LIMIT);
    }

    /** Подсветка совпадений: режем исходный текст на куски и экранируем каждый. */
    function highlightMatches(text, terms) {
        const source = String(text ?? '');
        const lower = source.toLowerCase();
        const ranges = [];
        for (const term of terms) {
            if (!term) continue;
            let from = 0;
            let idx = lower.indexOf(term, from);
            while (idx !== -1) {
                ranges.push([idx, idx + term.length]);
                from = idx + term.length;
                idx = lower.indexOf(term, from);
            }
        }
        if (!ranges.length) return escapeHtml(source);

        ranges.sort((a, b) => a[0] - b[0]);
        const merged = [];
        for (const range of ranges) {
            const last = merged[merged.length - 1];
            if (last && range[0] <= last[1]) last[1] = Math.max(last[1], range[1]);
            else merged.push([range[0], range[1]]);
        }

        let html = '';
        let pos = 0;
        for (const [start, end] of merged) {
            html += escapeHtml(source.slice(pos, start));
            html += `<mark class="writeoff-suggest-hl">${escapeHtml(source.slice(start, end))}</mark>`;
            pos = end;
        }
        return html + escapeHtml(source.slice(pos));
    }

    function renderSuggestions(input) {
        const box = getSuggestBox();
        suggestInput = input;
        suggestItems = matchCatalog(input.value.trim());
        suggestIndex = suggestItems.length ? 0 : -1;

        if (!currentCatalog.length) {
            box.innerHTML = '<div class="writeoff-suggest-empty">Каталог точки не загружен</div>';
        } else if (!suggestItems.length) {
            box.innerHTML = '<div class="writeoff-suggest-empty">Ничего не найдено</div>';
        } else {
            const terms = input.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
            box.innerHTML = suggestItems.map((item, i) => {
                const stock = item.quantity_available;
                // Остаток в минусе — это не ошибка, а «продажи проведены, приход нет».
                // Списать такое можно, но флорист должен видеть, что учёт разошёлся.
                const stockClass = stock > 0 ? '' : ' empty';
                const title = stock > 0 ? '' : ' title="Остаток по МойСклад не больше нуля — списать можно, но учёт разошёлся"';
                const code = item.article || item.code || '';
                return `
                    <div class="writeoff-suggest-item${i === suggestIndex ? ' active' : ''}" data-index="${i}">
                        <span>
                            ${code ? `<span class="writeoff-suggest-code">${highlightMatches(code, terms)}</span> — ` : ''}
                            ${highlightMatches(item.product_name, terms)}
                        </span>
                        <span class="writeoff-suggest-stock${stockClass}"${title}>${stock}</span>
                    </div>`;
            }).join('');
        }

        box.classList.add('active');
        box.scrollTop = 0;
        positionSuggestBox();
    }

    function moveSuggestion(delta) {
        if (!suggestBox || !suggestItems.length) return;
        suggestIndex = (suggestIndex + delta + suggestItems.length) % suggestItems.length;
        const nodes = suggestBox.querySelectorAll('.writeoff-suggest-item');
        nodes.forEach((node, i) => node.classList.toggle('active', i === suggestIndex));
        nodes[suggestIndex]?.scrollIntoView({ block: 'nearest' });
    }

    function pickSuggestion(input, item) {
        if (!item) return;
        input.dataset.productId = item.moysklad_product_id;
        input.dataset.productName = item.product_name;
        input.value = item.article ? `${item.article} — ${item.product_name}` : item.product_name;
        input.classList.remove('invalid');
        closeSuggestions();
    }

    /** Текст в поле есть, а товар не выбран из списка — выбор недействителен. */
    function clearSelection(input) {
        delete input.dataset.productId;
        delete input.dataset.productName;
    }

    function createProductInput() {
        const input = document.createElement('input');
        input.type = 'text';
        input.className = 'form-input writeoff-position-product';
        input.placeholder = 'Товар: название или артикул';
        input.autocomplete = 'off';
        input.style.minWidth = '260px';

        input.addEventListener('input', () => {
            clearSelection(input);
            input.classList.remove('invalid');
            renderSuggestions(input);
        });
        input.addEventListener('focus', () => renderSuggestions(input));
        input.addEventListener('blur', () => {
            // Ушли из поля, не выбрав товар из списка — не оставляем текст,
            // который выглядит как выбор, но им не является
            if (!input.dataset.productId) input.value = '';
            closeSuggestions();
        });
        input.addEventListener('keydown', (e) => {
            const open = suggestBox && suggestBox.classList.contains('active') && suggestInput === input;
            if (e.key === 'ArrowDown') {
                e.preventDefault();
                if (open) moveSuggestion(1); else renderSuggestions(input);
            } else if (e.key === 'ArrowUp') {
                e.preventDefault();
                if (open) moveSuggestion(-1);
            } else if (e.key === 'Enter') {
                if (open && suggestIndex >= 0) {
                    e.preventDefault();
                    pickSuggestion(input, suggestItems[suggestIndex]);
                }
            } else if (e.key === 'Escape') {
                if (open) { e.stopPropagation(); closeSuggestions(); }
            }
        });

        return input;
    }

    /** Каталог сменился (другая точка) — прежний выбор может быть уже недоступен. */
    function refreshProductInput(input) {
        const id = input.dataset.productId;
        if (!id) return;
        const item = currentCatalog.find(i => i.moysklad_product_id === id);
        if (item) pickSuggestion(input, item);
        else { clearSelection(input); input.value = ''; }
    }

    function createPositionRow() {
        const row = document.createElement('div');
        row.className = 'writeoff-position-row';
        row.style.cssText = 'display:flex; gap:6px; margin-bottom:8px; align-items:center; flex-wrap:wrap; padding:8px; background:var(--barkhat-bg-secondary, #f7f7f7); border-radius:6px;';

        const productInput = createProductInput();

        const qtyInput = document.createElement('input');
        qtyInput.type = 'number';
        qtyInput.min = '0.01';
        qtyInput.step = '0.01';
        qtyInput.className = 'form-input writeoff-position-qty';
        qtyInput.style.width = '90px';
        qtyInput.placeholder = 'Кол-во';

        const reasonInput = document.createElement('input');
        reasonInput.type = 'text';
        reasonInput.className = 'form-input writeoff-position-reason';
        reasonInput.style.width = '180px';
        reasonInput.placeholder = 'Причина (брак, порча...)';

        const photoLabel = document.createElement('label');
        photoLabel.className = 'form-hint';
        photoLabel.style.margin = '0';
        photoLabel.textContent = 'Фото *:';

        const fileInput = document.createElement('input');
        fileInput.type = 'file';
        fileInput.accept = '.png,.jpg,.jpeg,.webp';
        fileInput.className = 'writeoff-position-photo';

        const removeBtn = document.createElement('button');
        removeBtn.type = 'button';
        removeBtn.className = 'btn btn-sm btn-danger';
        removeBtn.textContent = '×';
        removeBtn.addEventListener('click', () => {
            if (elements.positionsRows.children.length <= 1) { alert('Нужна хотя бы одна позиция'); return; }
            if (suggestInput && row.contains(suggestInput)) closeSuggestions();
            row.remove();
        });

        row.appendChild(productInput);
        row.appendChild(qtyInput);
        row.appendChild(reasonInput);
        row.appendChild(photoLabel);
        row.appendChild(fileInput);
        row.appendChild(removeBtn);
        return row;
    }

    function openCreateModal() {
        const defaultStoreId = storeList.length ? storeList[0].id : null;
        elements.storeSelect.value = defaultStoreId || '';
        elements.positionsRows.innerHTML = '';

        elements.modal.classList.add('active');
        elements.overlay.classList.add('active');

        loadCatalogForStore(defaultStoreId).then(() => {
            elements.positionsRows.appendChild(createPositionRow());
        });
    }

    function closeCreateModal() {
        closeSuggestions();
        elements.modal.classList.remove('active');
        elements.overlay.classList.remove('active');
    }

    function readPositions() {
        const rows = Array.from(elements.positionsRows.querySelectorAll('.writeoff-position-row'));
        return rows.map(row => {
            const input = row.querySelector('.writeoff-position-product');
            return {
                moysklad_product_id: input.dataset.productId || '',
                product_name: input.dataset.productName || '',
                inputEl: input,
                quantity: parseFloat(row.querySelector('.writeoff-position-qty').value),
                reason: row.querySelector('.writeoff-position-reason').value.trim() || null,
                file: row.querySelector('.writeoff-position-photo').files[0] || null,
            };
        });
    }

    async function submitWriteoff() {
        const storeId = parseInt(elements.storeSelect.value, 10);
        if (!storeId) { alert('Выберите точку'); return; }

        const positions = readPositions();
        if (positions.length === 0) { alert('Добавьте хотя бы одну позицию'); return; }

        for (const pos of positions) {
            if (!pos.moysklad_product_id) {
                // Товар выбирается подсказкой: набранный вручную текст выбором не считается
                pos.inputEl?.classList.add('invalid');
                pos.inputEl?.focus();
                alert('Выберите товар из подсказок во всех позициях');
                return;
            }
            if (!pos.quantity || pos.quantity <= 0) { alert('Укажите корректное количество во всех позициях'); return; }
            if (!pos.file) { alert('Приложите фото для каждой позиции — это подтверждение списания'); return; }
        }

        try {
            elements.confirmBtn.disabled = true;
            const res = await fetch('/api/writeoffs', {
                method: 'POST',
                credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    store_id: storeId,
                    positions: positions.map(p => ({
                        moysklad_product_id: p.moysklad_product_id,
                        product_name: p.product_name,
                        quantity: p.quantity,
                        reason: p.reason,
                    })),
                }),
            });
            const data = await res.json();
            if (!res.ok) { alert(data.error || 'Ошибка создания заявки'); return; }

            const createdPositions = data.writeoff.positions;
            for (let i = 0; i < positions.length && i < createdPositions.length; i++) {
                if (positions[i].file) {
                    await uploadPositionPhoto(createdPositions[i].id, positions[i].file);
                }
            }

            closeCreateModal();
            await loadWriteoffs();
        } catch (e) {
            console.error('Ошибка создания заявки на списание:', e);
            alert('Ошибка создания заявки');
        } finally {
            elements.confirmBtn.disabled = false;
        }
    }

    async function uploadPositionPhoto(positionId, file) {
        const formData = new FormData();
        formData.append('file', file);
        try {
            const res = await fetch(`/api/writeoffs/positions/${positionId}/attachments`, {
                method: 'POST',
                credentials: 'include',
                body: formData,
            });
            const data = await res.json();
            if (!res.ok) alert(`${file.name}: ${data.error || 'Ошибка загрузки фото'}`);
        } catch (e) {
            console.error('Ошибка загрузки фото:', e);
            alert(`${file.name}: ошибка загрузки фото`);
        }
    }

    // =========================================================================
    // ДЕТАЛИ ЗАЯВКИ
    // =========================================================================

    async function openDetailsModal(id) {
        currentDetailsWriteoffId = parseInt(id, 10);
        try {
            const res = await fetch(`/api/writeoffs/${id}`, { credentials: 'include' });
            const data = await res.json();
            if (!res.ok) { alert(data.error || 'Заявка не найдена'); return; }
            renderDetails(data.writeoff);
            elements.detailsModal.classList.add('active');
            elements.detailsOverlay.classList.add('active');
        } catch (e) {
            console.error('Ошибка загрузки заявки:', e);
            alert('Ошибка загрузки заявки');
        }
    }

    function closeDetailsModal() {
        elements.detailsModal.classList.remove('active');
        elements.detailsOverlay.classList.remove('active');
        currentDetailsWriteoffId = null;
    }

    function renderDetails(writeoff) {
        elements.detailsTitle.textContent = `Списание #${writeoff.id} — ${storeName(writeoff.store_id)}`;

        const rows = [
            ['Точка', storeName(writeoff.store_id)],
            ['Статус', STATUS_LABELS[writeoff.status] || writeoff.status],
            ['Создал', writeoff.created_by_full_name || writeoff.created_by],
            ['Заведено', window.BarhatTime.formatDateTimeLong(writeoff.created_at)],
        ];
        if (writeoff.approved_by) rows.push(['Согласовал', `${writeoff.approved_by_full_name || writeoff.approved_by}, ${window.BarhatTime.formatDateTimeLong(writeoff.approved_at)}`]);
        if (writeoff.rejected_by) rows.push(['Отклонил', `${writeoff.rejected_by_full_name || writeoff.rejected_by}${writeoff.rejected_reason ? ': ' + writeoff.rejected_reason : ''}`]);
        if (writeoff.moysklad_error) rows.push(['Ошибка МойСклад', writeoff.moysklad_error]);
        if (writeoff.moysklad_loss_id) rows.push(['Документ МойСклад', writeoff.moysklad_loss_id]);

        elements.detailsInfo.innerHTML = rows.map(([label, value]) =>
            `<div style="display:flex; gap:8px; padding:2px 0;"><strong style="min-width:180px;">${escapeHtml(label)}:</strong><span>${escapeHtml(String(value))}</span></div>`
        ).join('');

        elements.detailsPositions.innerHTML = (writeoff.positions || []).map(pos => `
            <div style="border-top:1px solid #eee; padding:8px 0;">
                <div><strong>${escapeHtml(pos.product_name)}</strong> — ${pos.quantity} шт.</div>
                ${pos.reason ? `<div class="form-hint">Причина: ${escapeHtml(pos.reason)}</div>` : ''}
                <div style="display:flex; gap:8px; margin-top:4px; flex-wrap:wrap;">
                    ${(pos.attachments || []).map(a => `
                        <a href="/api/writeoffs/attachments/${a.id}/download" target="_blank">${escapeHtml(a.original_filename)}</a>
                    `).join('') || '<span class="form-hint">Фото нет</span>'}
                </div>
            </div>
        `).join('');

        renderDetailsActions(writeoff);
    }

    function renderDetailsActions(writeoff) {
        let html = '';
        if (writeoff.status === 'on_approval' && canApprove(writeoff.store_id)) {
            html += `<button class="btn btn-success" data-action="approve">Согласовать</button>`;
            html += `<button class="btn btn-danger" data-action="reject">Отклонить</button>`;
        }
        if (writeoff.status === 'failed' && canApprove(writeoff.store_id)) {
            html += `<button class="btn btn-secondary" data-action="retry">Повторить отправку</button>`;
        }
        if (writeoff.status === 'on_approval' && currentUserData?.username === writeoff.created_by) {
            html += `<button class="btn btn-danger" data-action="cancel">Отменить</button>`;
        }
        html += `<button class="btn btn-secondary" data-action="close">Закрыть</button>`;

        elements.detailsActions.innerHTML = html;
        elements.detailsActions.querySelectorAll('button[data-action]').forEach(btn => {
            btn.addEventListener('click', () => handleDetailsAction(btn.getAttribute('data-action'), writeoff.id));
        });
    }

    async function handleDetailsAction(action, writeoffId) {
        if (action === 'close') { closeDetailsModal(); return; }
        if (action === 'approve') { await approveWriteoff(writeoffId); await openDetailsModal(writeoffId); return; }
        if (action === 'reject') { await rejectWriteoff(writeoffId); await openDetailsModal(writeoffId); return; }
        if (action === 'retry') { await retryWriteoff(writeoffId); await openDetailsModal(writeoffId); return; }
        if (action === 'cancel') {
            await cancelWriteoff(writeoffId);
            closeDetailsModal();
            return;
        }
    }

    // =========================================================================
    // СОПОСТАВЛЕНИЕ СОТРУДНИКОВ МОЙСКЛАД (только админ)
    // =========================================================================

    async function openMappingModal() {
        elements.mappingRows.innerHTML = '<p class="form-hint">Загрузка...</p>';
        elements.mappingModal.classList.add('active');
        elements.mappingOverlay.classList.add('active');

        try {
            const [usersRes, employeesRes, groupsRes, linksRes] = await Promise.all([
                fetch('/api/auth/users', { credentials: 'include' }),
                fetch('/api/moysklad/employees', { credentials: 'include' }),
                fetch('/api/moysklad/groups', { credentials: 'include' }),
                fetch('/api/writeoffs/employee-links', { credentials: 'include' }),
            ]);
            const usersData = await usersRes.json();
            const employeesData = await employeesRes.json();
            const groupsData = await groupsRes.json();
            const linksData = await linksRes.json();

            const users = (usersData.users || []).filter(u => (u.permissions || []).includes('writeoffs'));
            const employees = employeesData.data || [];
            const groups = groupsData.data || [];
            const links = linksData.links || [];
            const linkByUsername = Object.fromEntries(links.map(l => [l.username, l]));

            if (users.length === 0) {
                elements.mappingRows.innerHTML = '<p class="form-hint">Нет пользователей с доступом к списаниям</p>';
                return;
            }

            elements.mappingRows.innerHTML = users.map(u => {
                const current = linkByUsername[u.username];
                const employeeOptions = '<option value="">Не выбран</option>' + employees.map(e =>
                    `<option value="${e.id}" ${current?.moysklad_employee_id === e.id ? 'selected' : ''}>${escapeHtml(e.name)}</option>`
                ).join('');
                const groupOptions = '<option value="">Не выбран</option>' + groups.map(g =>
                    `<option value="${g.id}" ${current?.moysklad_group_id === g.id ? 'selected' : ''}>${escapeHtml(g.name)}</option>`
                ).join('');

                return `
                    <div class="writeoff-mapping-row" data-username="${escapeHtml(u.username)}"
                         style="display:flex; gap:8px; align-items:center; padding:6px 0; border-top:1px solid #eee; flex-wrap:wrap;">
                        <span style="min-width:200px;">${escapeHtml(u.full_name || u.username)} <span class="form-hint">(${escapeHtml(u.role)})</span></span>
                        <select class="form-select mapping-employee" style="min-width:180px;">${employeeOptions}</select>
                        <select class="form-select mapping-group" style="min-width:150px;">${groupOptions}</select>
                    </div>
                `;
            }).join('');
        } catch (e) {
            console.error('Ошибка загрузки данных для сопоставления:', e);
            elements.mappingRows.innerHTML = '<p class="form-hint">Ошибка загрузки</p>';
        }
    }

    function closeMappingModal() {
        elements.mappingModal.classList.remove('active');
        elements.mappingOverlay.classList.remove('active');
    }

    async function saveMapping() {
        const rows = Array.from(elements.mappingRows.querySelectorAll('.writeoff-mapping-row'));
        const links = rows.map(row => ({
            username: row.getAttribute('data-username'),
            moysklad_employee_id: row.querySelector('.mapping-employee').value,
            moysklad_group_id: row.querySelector('.mapping-group').value,
        })).filter(l => l.moysklad_employee_id && l.moysklad_group_id);

        if (links.length === 0) {
            alert('Выберите сотрудника и отдел хотя бы для одного пользователя');
            return;
        }

        try {
            elements.saveMappingBtn.disabled = true;
            const res = await fetch('/api/writeoffs/employee-links', {
                method: 'POST',
                credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ links }),
            });
            const data = await res.json();
            if (!res.ok) { alert(data.error || 'Ошибка сохранения'); return; }

            if (data.errors && data.errors.length > 0) {
                alert(`Сохранено: ${data.applied.length}. Ошибки:\n` + data.errors.map(e => `${e.username}: ${e.error}`).join('\n'));
            } else {
                alert(`Сопоставление сохранено (${data.applied.length})`);
            }
            closeMappingModal();
        } catch (e) {
            console.error('Ошибка сохранения сопоставления:', e);
            alert('Ошибка сохранения сопоставления');
        } finally {
            elements.saveMappingBtn.disabled = false;
        }
    }

    // Не div.textContent/innerHTML — не экранирует кавычки, значения с "
    // обрывались при подстановке в value="${...}" (см. историю сессий,
    // инцидент 2026-08-18, invoices.js).
    function escapeHtml(str) {
        return String(str ?? '').replace(/[&<>"']/g, (c) => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
        }[c]));
    }

    document.addEventListener('DOMContentLoaded', init);

    window.WriteoffsModule = {
        onPageActivated,
    };
})();
