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
            elements.positionsRows.querySelectorAll('.writeoff-position-product').forEach(populateProductSelect);
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
            if (elements.filterDateFrom.value) params.set('date_from', elements.filterDateFrom.value);
            if (elements.filterDateTo.value) params.set('date_to', elements.filterDateTo.value);

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

            const createdDate = w.created_at ? w.created_at.slice(0, 16).replace('T', ' ') : '';

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
        if (!confirm('Согласовать списание? Товар спишется в МойСклад.')) return;
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
        const reason = prompt('Причина отклонения (необязательно):') || '';
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
        if (!confirm('Отменить свою заявку?')) return;
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

    function populateProductSelect(select) {
        const previousValue = select.value;
        select.innerHTML = currentCatalog.map(item => `
            <option value="${item.moysklad_product_id}" data-name="${escapeHtml(item.product_name)}">
                ${escapeHtml(item.product_name)} (остаток: ${item.quantity_available})
            </option>
        `).join('');
        if (previousValue && currentCatalog.some(i => i.moysklad_product_id === previousValue)) {
            select.value = previousValue;
        }
    }

    function createPositionRow() {
        const row = document.createElement('div');
        row.className = 'writeoff-position-row';
        row.style.cssText = 'display:flex; gap:6px; margin-bottom:8px; align-items:center; flex-wrap:wrap; padding:8px; background:var(--barkhat-bg-secondary, #f7f7f7); border-radius:6px;';

        const productSelect = document.createElement('select');
        productSelect.className = 'form-select writeoff-position-product';
        productSelect.style.minWidth = '220px';
        populateProductSelect(productSelect);

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
            row.remove();
        });

        row.appendChild(productSelect);
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
        elements.modal.classList.remove('active');
        elements.overlay.classList.remove('active');
    }

    function readPositions() {
        const rows = Array.from(elements.positionsRows.querySelectorAll('.writeoff-position-row'));
        return rows.map(row => {
            const select = row.querySelector('.writeoff-position-product');
            const option = select.options[select.selectedIndex];
            return {
                moysklad_product_id: select.value,
                product_name: option ? option.getAttribute('data-name') : '',
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
            if (!pos.moysklad_product_id) { alert('Выберите товар во всех позициях'); return; }
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
            ['Заведено', writeoff.created_at ? writeoff.created_at.slice(0, 16).replace('T', ' ') : '—'],
        ];
        if (writeoff.approved_by) rows.push(['Согласовал', `${writeoff.approved_by_full_name || writeoff.approved_by}, ${(writeoff.approved_at || '').slice(0, 16).replace('T', ' ')}`]);
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
