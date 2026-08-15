/**
 * Счета на оплату БАРХАТ
 * Создание, согласование, распределение по проектам/статьям, вложения, архив.
 */

(function() {
    'use strict';

    let currentUserData = null;
    let storeList = [];
    let categoryList = [];
    let cityList = [];
    let payerList = [];
    let vatList = [];
    let loaded = false;
    let currentDetailsInvoiceId = null;
    let currentRefType = 'categories';

    const REF_LABELS = {
        categories: 'Статьи расхода',
        cities: 'Города',
        payers: 'На кого выставлен счёт',
        'vat-options': 'НДС',
    };

    const STATUS_LABELS = {
        on_approval: 'На согласовании',
        approved: 'Согласован',
        rejected: 'Отклонён',
        sent_to_bank: 'Отправлен в банк',
        paid: 'Оплачен',
    };

    const STATUS_COLORS = {
        on_approval: { bg: '#fff3cd', color: '#856404' },
        approved: { bg: '#d4edda', color: '#155724' },
        rejected: { bg: '#f8d7da', color: '#721c24' },
        sent_to_bank: { bg: '#d1ecf1', color: '#0c5460' },
        paid: { bg: '#d4edda', color: '#155724' },
    };

    const elements = {};

    function init() {
        elements.tbody = document.getElementById('invoices-tbody');
        if (!elements.tbody) return; // Страницы нет в DOM — модуль не нужен

        elements.createBtn = document.getElementById('create-invoice-btn');
        elements.manageReferencesBtn = document.getElementById('manage-references-btn');

        elements.filterCounterparty = document.getElementById('invoices-filter-counterparty');
        elements.filterPurpose = document.getElementById('invoices-filter-purpose');
        elements.filterCreatedFrom = document.getElementById('invoices-filter-created-from');
        elements.filterCreatedTo = document.getElementById('invoices-filter-created-to');
        elements.filterDueFrom = document.getElementById('invoices-filter-due-from');
        elements.filterDueTo = document.getElementById('invoices-filter-due-to');
        elements.filterStore = document.getElementById('invoices-filter-store');
        elements.filterStatus = document.getElementById('invoices-status-filter');
        elements.filterCreatedBy = document.getElementById('invoices-filter-created-by');
        elements.filterArchived = document.getElementById('invoices-filter-archived');
        elements.applyFiltersBtn = document.getElementById('invoices-apply-filters-btn');
        elements.resetFiltersBtn = document.getElementById('invoices-reset-filters-btn');

        elements.modal = document.getElementById('create-invoice-modal');
        elements.overlay = document.getElementById('create-invoice-overlay');
        elements.closeBtn = document.getElementById('close-create-invoice-btn');
        elements.cancelBtn = document.getElementById('cancel-create-invoice-btn');
        elements.confirmBtn = document.getElementById('confirm-create-invoice-btn');

        elements.citySelect = document.getElementById('invoice-city');
        elements.payerSelect = document.getElementById('invoice-payer');
        elements.vatSelect = document.getElementById('invoice-vat');
        elements.counterpartyInput = document.getElementById('invoice-counterparty');
        elements.amountInput = document.getElementById('invoice-amount');
        elements.purposeInput = document.getElementById('invoice-purpose');
        elements.innInput = document.getElementById('invoice-inn');
        elements.bankBikInput = document.getElementById('invoice-bank-bik');
        elements.bankAccountInput = document.getElementById('invoice-bank-account');
        elements.dueDateInput = document.getElementById('invoice-due-date');
        elements.lineItemsRows = document.getElementById('invoice-lineitems-rows');
        elements.addLineItemBtn = document.getElementById('invoice-add-lineitem-btn');
        elements.lineItemsTotal = document.getElementById('invoice-lineitems-total');
        elements.attachmentsInput = document.getElementById('invoice-attachments-input');

        elements.detailsModal = document.getElementById('invoice-details-modal');
        elements.detailsOverlay = document.getElementById('invoice-details-overlay');
        elements.closeDetailsBtn = document.getElementById('close-invoice-details-btn');
        elements.detailsTitle = document.getElementById('invoice-details-title');
        elements.detailsInfo = document.getElementById('invoice-details-info');
        elements.detailsLineItemsRows = document.getElementById('invoice-details-lineitems-rows');
        elements.detailsAddLineItemBtn = document.getElementById('invoice-details-add-lineitem-btn');
        elements.detailsLineItemsTotal = document.getElementById('invoice-details-lineitems-total');
        elements.detailsSaveLineItemsBtn = document.getElementById('invoice-details-save-lineitems-btn');
        elements.detailsAttachments = document.getElementById('invoice-details-attachments');
        elements.detailsAttachmentInput = document.getElementById('invoice-details-attachment-input');
        elements.detailsActions = document.getElementById('invoice-details-actions');

        elements.referencesModal = document.getElementById('invoice-references-modal');
        elements.referencesOverlay = document.getElementById('invoice-references-overlay');
        elements.closeReferencesBtn = document.getElementById('close-invoice-references-btn');
        elements.closeReferencesFooterBtn = document.getElementById('close-invoice-references-footer-btn');
        elements.referencesTabs = document.getElementById('invoice-references-tabs');
        elements.referencesList = document.getElementById('invoice-references-list');
        elements.referenceNewName = document.getElementById('invoice-reference-new-name');
        elements.referenceAddBtn = document.getElementById('invoice-reference-add-btn');

        elements.createBtn?.addEventListener('click', openCreateModal);
        elements.closeBtn?.addEventListener('click', closeCreateModal);
        elements.cancelBtn?.addEventListener('click', closeCreateModal);
        elements.overlay?.addEventListener('click', closeCreateModal);
        elements.confirmBtn?.addEventListener('click', submitInvoice);
        elements.addLineItemBtn?.addEventListener('click', () => {
            elements.lineItemsRows.appendChild(createLineItemRow(null, elements.lineItemsTotal));
            updateLineItemsTotal(elements.lineItemsRows, elements.lineItemsTotal);
        });

        elements.applyFiltersBtn?.addEventListener('click', loadInvoices);
        elements.resetFiltersBtn?.addEventListener('click', resetFilters);

        elements.closeDetailsBtn?.addEventListener('click', closeDetailsModal);
        elements.detailsOverlay?.addEventListener('click', closeDetailsModal);
        elements.detailsAddLineItemBtn?.addEventListener('click', () => {
            elements.detailsLineItemsRows.appendChild(createLineItemRow(null, elements.detailsLineItemsTotal));
            updateLineItemsTotal(elements.detailsLineItemsRows, elements.detailsLineItemsTotal);
        });
        elements.detailsSaveLineItemsBtn?.addEventListener('click', saveDetailsLineItems);
        elements.detailsAttachmentInput?.addEventListener('change', uploadDetailsAttachments);

        elements.manageReferencesBtn?.addEventListener('click', openReferencesModal);
        elements.closeReferencesBtn?.addEventListener('click', closeReferencesModal);
        elements.closeReferencesFooterBtn?.addEventListener('click', closeReferencesModal);
        elements.referencesOverlay?.addEventListener('click', closeReferencesModal);
        elements.referencesTabs?.addEventListener('click', (e) => {
            const btn = e.target.closest('button[data-ref]');
            if (!btn) return;
            elements.referencesTabs.querySelectorAll('.admin-tab').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            currentRefType = btn.getAttribute('data-ref');
            loadReferenceList();
        });
        elements.referenceAddBtn?.addEventListener('click', addReferenceItem);
    }

    async function onPageActivated(userData) {
        currentUserData = userData;

        if (!loaded) {
            await loadDictionaries();
            loaded = true;
        }

        if (currentUserData?.role === 'admin') {
            elements.manageReferencesBtn.style.display = '';
        } else {
            elements.manageReferencesBtn.style.display = 'none';
        }

        await loadInvoices();
    }

    async function loadDictionaries() {
        try {
            const [storesRes, categoriesRes, citiesRes, payersRes, vatRes] = await Promise.all([
                fetch('/api/invoices/stores', { credentials: 'include' }),
                fetch('/api/invoices/categories', { credentials: 'include' }),
                fetch('/api/invoices/cities', { credentials: 'include' }),
                fetch('/api/invoices/payers', { credentials: 'include' }),
                fetch('/api/invoices/vat-options', { credentials: 'include' }),
            ]);
            storeList = (await storesRes.json()).stores || [];
            categoryList = (await categoriesRes.json()).categories || [];
            cityList = (await citiesRes.json()).cities || [];
            payerList = (await payersRes.json()).payers || [];
            vatList = (await vatRes.json())['vat-options'] || [];

            elements.citySelect.innerHTML = cityList.map(c => `<option value="${c.id}">${escapeHtml(c.name)}</option>`).join('');
            elements.payerSelect.innerHTML = payerList.map(p => `<option value="${p.id}">${escapeHtml(p.name)}</option>`).join('');
            elements.vatSelect.innerHTML = '<option value="">Не указан</option>' +
                vatList.map(v => `<option value="${v.id}">${escapeHtml(v.name)}</option>`).join('');
            elements.filterStore.innerHTML = '<option value="">Все</option>' +
                storeList.map(s => `<option value="${s.id}">${escapeHtml(s.name)}</option>`).join('');
        } catch (e) {
            console.error('Ошибка загрузки справочников:', e);
        }
    }

    function resetFilters() {
        elements.filterCounterparty.value = '';
        elements.filterPurpose.value = '';
        elements.filterCreatedFrom.value = '';
        elements.filterCreatedTo.value = '';
        elements.filterDueFrom.value = '';
        elements.filterDueTo.value = '';
        elements.filterStore.value = '';
        elements.filterStatus.value = '';
        elements.filterCreatedBy.value = '';
        elements.filterArchived.checked = false;
        loadInvoices();
    }

    async function loadInvoices() {
        elements.tbody.innerHTML = `<tr><td colspan="10" style="text-align:center; color: var(--barkhat-gray); padding:20px;">Загрузка данных...</td></tr>`;

        try {
            const params = new URLSearchParams();
            if (elements.filterStatus.value) params.set('status', elements.filterStatus.value);
            if (elements.filterStore.value) params.set('store_id', elements.filterStore.value);
            if (elements.filterCounterparty.value.trim()) params.set('counterparty', elements.filterCounterparty.value.trim());
            if (elements.filterPurpose.value.trim()) params.set('payment_purpose', elements.filterPurpose.value.trim());
            if (elements.filterCreatedFrom.value) params.set('created_from', elements.filterCreatedFrom.value);
            if (elements.filterCreatedTo.value) params.set('created_to', elements.filterCreatedTo.value);
            if (elements.filterDueFrom.value) params.set('due_from', elements.filterDueFrom.value);
            if (elements.filterDueTo.value) params.set('due_to', elements.filterDueTo.value);
            if (elements.filterCreatedBy.value.trim()) params.set('created_by', elements.filterCreatedBy.value.trim());
            params.set('archived', elements.filterArchived.checked ? 'true' : 'false');

            const res = await fetch(`/api/invoices?${params.toString()}`, { credentials: 'include' });
            const data = await res.json();
            renderInvoices(data.invoices || []);
        } catch (e) {
            console.error('Ошибка загрузки счетов:', e);
            elements.tbody.innerHTML = `<tr><td colspan="10" style="text-align:center; color: var(--barkhat-gray); padding:20px;">Ошибка загрузки</td></tr>`;
        }
    }

    function cityName(id) {
        return cityList.find(c => c.id === id)?.name || (id ? `#${id}` : '—');
    }

    function payerName(id) {
        return payerList.find(p => p.id === id)?.name || (id ? `#${id}` : '—');
    }

    function vatName(id) {
        return vatList.find(v => v.id === id)?.name || '—';
    }

    function storeName(id) {
        return storeList.find(s => s.id === id)?.name || `#${id}`;
    }

    function categoryName(id) {
        return categoryList.find(c => c.id === id)?.name || `#${id}`;
    }

    function renderInvoices(invoices) {
        if (invoices.length === 0) {
            elements.tbody.innerHTML = `<tr><td colspan="10" style="text-align:center; color: var(--barkhat-gray); padding:20px;">Счетов нет</td></tr>`;
            return;
        }

        const isAdmin = currentUserData?.role === 'admin';

        elements.tbody.innerHTML = invoices.map(inv => {
            const colors = STATUS_COLORS[inv.status] || { bg: '#eee', color: '#333' };
            const badge = `<span class="status-badge" style="background:${colors.bg}; color:${colors.color};">${STATUS_LABELS[inv.status] || inv.status}</span>`;

            let actions = `<button class="btn btn-sm btn-secondary" data-action="details" data-id="${inv.id}">Детали</button>`;
            if (isAdmin && inv.status === 'on_approval') {
                actions += `
                    <button class="btn btn-sm btn-success" data-action="approve" data-id="${inv.id}">Согласовать</button>
                    <button class="btn btn-sm btn-danger" data-action="reject" data-id="${inv.id}">Отклонить</button>
                `;
            }

            const createdDate = inv.created_at ? inv.created_at.slice(0, 16).replace('T', ' ') : '';

            return `
                <tr>
                    <td>${escapeHtml(inv.invoice_number || '')}</td>
                    <td>${escapeHtml(cityName(inv.city_id))}</td>
                    <td>${escapeHtml(inv.counterparty_name || '—')}</td>
                    <td>${escapeHtml(payerName(inv.payer_id))}</td>
                    <td>${formatMoney(inv.amount)}</td>
                    <td>${inv.due_date || '—'}</td>
                    <td>${badge}</td>
                    <td>${escapeHtml(inv.created_by || '')}</td>
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
                if (action === 'approve') approveInvoice(id);
                if (action === 'reject') rejectInvoice(id);
            });
        });
    }

    async function approveInvoice(id) {
        if (!confirm('Согласовать счёт?')) return;
        try {
            const res = await fetch(`/api/invoices/${id}/approve`, { method: 'POST', credentials: 'include' });
            const data = await res.json();
            if (!res.ok) { alert(data.error || 'Ошибка согласования'); return; }
            await loadInvoices();
        } catch (e) {
            console.error('Ошибка согласования счёта:', e);
            alert('Ошибка согласования счёта');
        }
    }

    async function rejectInvoice(id) {
        const reason = prompt('Причина отклонения (необязательно):') || '';
        try {
            const res = await fetch(`/api/invoices/${id}/reject`, {
                method: 'POST',
                credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ reason }),
            });
            const data = await res.json();
            if (!res.ok) { alert(data.error || 'Ошибка отклонения'); return; }
            await loadInvoices();
        } catch (e) {
            console.error('Ошибка отклонения счёта:', e);
            alert('Ошибка отклонения счёта');
        }
    }

    // =========================================================================
    // СОЗДАНИЕ СЧЁТА
    // =========================================================================

    function createLineItemRow(item, totalEl) {
        const row = document.createElement('div');
        row.className = 'invoice-lineitem-row';
        row.style.cssText = 'display:flex; gap:6px; margin-bottom:6px; align-items:center;';

        const storeSelect = document.createElement('select');
        storeSelect.className = 'form-select lineitem-store';
        storeSelect.innerHTML = storeList.map(s => `<option value="${s.id}">${escapeHtml(s.name)}</option>`).join('');

        const categorySelect = document.createElement('select');
        categorySelect.className = 'form-select lineitem-category';
        categorySelect.innerHTML = categoryList.map(c => `<option value="${c.id}">${escapeHtml(c.name)}</option>`).join('');

        const amountInput = document.createElement('input');
        amountInput.type = 'number';
        amountInput.min = '0';
        amountInput.step = '0.01';
        amountInput.className = 'form-input lineitem-amount';
        amountInput.style.width = '120px';
        amountInput.placeholder = 'Сумма';
        amountInput.addEventListener('input', () => updateLineItemsTotal(row.parentElement, totalEl));

        const removeBtn = document.createElement('button');
        removeBtn.type = 'button';
        removeBtn.className = 'btn btn-sm btn-danger';
        removeBtn.textContent = '×';
        removeBtn.addEventListener('click', () => {
            const container = row.parentElement;
            row.remove();
            updateLineItemsTotal(container, totalEl);
        });

        if (item) {
            storeSelect.value = item.store_id;
            categorySelect.value = item.expense_category_id;
            amountInput.value = item.amount;
        }

        row.appendChild(storeSelect);
        row.appendChild(categorySelect);
        row.appendChild(amountInput);
        row.appendChild(removeBtn);
        return row;
    }

    function updateLineItemsTotal(container, totalEl) {
        if (!container || !totalEl) return;
        const items = readLineItems(container);
        const total = items.reduce((sum, i) => sum + (i.amount || 0), 0);
        totalEl.textContent = items.length ? `Сумма строк: ${formatMoney(total)}` : '';
    }

    function readLineItems(container) {
        return Array.from(container.querySelectorAll('.invoice-lineitem-row')).map(row => ({
            store_id: parseInt(row.querySelector('.lineitem-store').value, 10),
            expense_category_id: parseInt(row.querySelector('.lineitem-category').value, 10),
            amount: parseFloat(row.querySelector('.lineitem-amount').value),
        })).filter(i => i.amount > 0);
    }

    function openCreateModal() {
        elements.citySelect.value = '';
        elements.payerSelect.value = '';
        elements.vatSelect.value = '';
        elements.counterpartyInput.value = '';
        elements.amountInput.value = '';
        elements.purposeInput.value = '';
        elements.innInput.value = '';
        elements.bankBikInput.value = '';
        elements.bankAccountInput.value = '';
        elements.dueDateInput.value = '';
        elements.lineItemsRows.innerHTML = '';
        elements.lineItemsTotal.textContent = '';
        elements.attachmentsInput.value = '';

        elements.modal.classList.add('active');
        elements.overlay.classList.add('active');
    }

    function closeCreateModal() {
        elements.modal.classList.remove('active');
        elements.overlay.classList.remove('active');
    }

    async function submitInvoice() {
        const cityId = parseInt(elements.citySelect.value, 10);
        const payerId = parseInt(elements.payerSelect.value, 10);
        const dueDate = elements.dueDateInput.value;
        const amount = parseFloat(elements.amountInput.value);
        const purpose = elements.purposeInput.value.trim();

        if (!cityId) { alert('Выберите город'); return; }
        if (!payerId) { alert('Выберите, на кого выставлен счёт'); return; }
        if (!dueDate) { alert('Укажите планируемую дату оплаты'); return; }
        if (!amount || amount <= 0) { alert('Укажите корректную сумму'); return; }
        if (!purpose) { alert('Укажите назначение платежа'); return; }

        const lineItems = readLineItems(elements.lineItemsRows);
        if (lineItems.length > 0) {
            const total = lineItems.reduce((sum, i) => sum + i.amount, 0);
            if (Math.abs(total - amount) >= 0.01) {
                alert(`Сумма строк распределения (${total}) не равна сумме счёта (${amount})`);
                return;
            }
        }

        const payload = {
            city_id: cityId,
            payer_id: payerId,
            due_date: dueDate,
            amount: amount,
            payment_purpose: purpose,
            vat_id: elements.vatSelect.value ? parseInt(elements.vatSelect.value, 10) : null,
            counterparty_name: elements.counterpartyInput.value.trim() || null,
            counterparty_inn: elements.innInput.value.trim() || null,
            counterparty_bank_bik: elements.bankBikInput.value.trim() || null,
            counterparty_bank_account: elements.bankAccountInput.value.trim() || null,
            line_items: lineItems,
        };

        try {
            elements.confirmBtn.disabled = true;
            const res = await fetch('/api/invoices', {
                method: 'POST',
                credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            const data = await res.json();
            if (!res.ok) { alert(data.error || 'Ошибка создания счёта'); return; }

            const files = Array.from(elements.attachmentsInput.files || []);
            for (const file of files) {
                await uploadAttachment(data.invoice.id, file);
            }

            closeCreateModal();
            await loadInvoices();
        } catch (e) {
            console.error('Ошибка создания счёта:', e);
            alert('Ошибка создания счёта');
        } finally {
            elements.confirmBtn.disabled = false;
        }
    }

    async function uploadAttachment(invoiceId, file) {
        const formData = new FormData();
        formData.append('file', file);
        try {
            const res = await fetch(`/api/invoices/${invoiceId}/attachments`, {
                method: 'POST',
                credentials: 'include',
                body: formData,
            });
            const data = await res.json();
            if (!res.ok) alert(`${file.name}: ${data.error || 'Ошибка загрузки вложения'}`);
        } catch (e) {
            console.error('Ошибка загрузки вложения:', e);
            alert(`${file.name}: ошибка загрузки вложения`);
        }
    }

    // =========================================================================
    // ДЕТАЛИ СЧЁТА
    // =========================================================================

    async function openDetailsModal(id) {
        currentDetailsInvoiceId = parseInt(id, 10);
        try {
            const res = await fetch(`/api/invoices/${id}`, { credentials: 'include' });
            const data = await res.json();
            if (!res.ok) { alert(data.error || 'Счёт не найден'); return; }
            renderDetails(data.invoice, data.line_items || [], data.attachments || []);
            elements.detailsModal.classList.add('active');
            elements.detailsOverlay.classList.add('active');
        } catch (e) {
            console.error('Ошибка загрузки счёта:', e);
            alert('Ошибка загрузки счёта');
        }
    }

    function closeDetailsModal() {
        elements.detailsModal.classList.remove('active');
        elements.detailsOverlay.classList.remove('active');
        currentDetailsInvoiceId = null;
    }

    function renderDetails(invoice, lineItems, attachments) {
        elements.detailsTitle.textContent = `Счёт ${invoice.invoice_number}${invoice.is_archived ? ' (в архиве)' : ''}`;

        const rows = [
            ['Город', cityName(invoice.city_id)],
            ['На кого выставлен', payerName(invoice.payer_id)],
            ['Контрагент', invoice.counterparty_name || '—'],
            ['ИНН контрагента', invoice.counterparty_inn || '—'],
            ['Расчётный счёт контрагента', invoice.counterparty_bank_account || '—'],
            ['БИК контрагента', invoice.counterparty_bank_bik || '—'],
            ['НДС', vatName(invoice.vat_id)],
            ['Сумма', formatMoney(invoice.amount)],
            ['Назначение платежа', invoice.payment_purpose || '—'],
            ['Планируемая дата оплаты', invoice.due_date || '—'],
            ['Статус', STATUS_LABELS[invoice.status] || invoice.status],
            ['Создал', invoice.created_by],
            ['Заведён', invoice.created_at ? invoice.created_at.slice(0, 16).replace('T', ' ') : '—'],
        ];
        if (invoice.approved_by) rows.push(['Согласовал', `${invoice.approved_by}, ${(invoice.approved_at || '').slice(0, 16).replace('T', ' ')}`]);
        if (invoice.rejected_by) rows.push(['Отклонил', `${invoice.rejected_by}${invoice.rejected_reason ? ': ' + invoice.rejected_reason : ''}`]);
        if (invoice.paid_at) rows.push(['Оплачен', invoice.paid_at.slice(0, 16).replace('T', ' ')]);
        if (invoice.is_archived) rows.push(['В архиве с', (invoice.archived_at || '').slice(0, 16).replace('T', ' ')]);

        elements.detailsInfo.innerHTML = rows.map(([label, value]) =>
            `<div style="display:flex; gap:8px; padding:2px 0;"><strong style="min-width:220px;">${escapeHtml(label)}:</strong><span>${escapeHtml(String(value))}</span></div>`
        ).join('');

        elements.detailsLineItemsRows.innerHTML = '';
        lineItems.forEach(item => {
            elements.detailsLineItemsRows.appendChild(createLineItemRow(item, elements.detailsLineItemsTotal));
        });
        updateLineItemsTotal(elements.detailsLineItemsRows, elements.detailsLineItemsTotal);

        const canEditDistribution = !invoice.is_archived;
        elements.detailsAddLineItemBtn.style.display = canEditDistribution ? '' : 'none';
        elements.detailsSaveLineItemsBtn.style.display = canEditDistribution ? '' : 'none';

        elements.detailsAttachments.innerHTML = attachments.length ? attachments.map(a => `
            <div style="display:flex; gap:8px; align-items:center; padding:2px 0;">
                <a href="/api/invoices/attachments/${a.id}/download" target="_blank">${escapeHtml(a.original_filename)}</a>
                <span class="form-hint">${escapeHtml(a.uploaded_by)}</span>
                ${currentUserData?.role === 'admin' ? `<button class="btn btn-sm btn-danger" data-action="delete-attachment" data-id="${a.id}">Удалить</button>` : ''}
            </div>
        `).join('') : '<p class="form-hint">Вложений нет</p>';

        elements.detailsAttachments.querySelectorAll('button[data-action="delete-attachment"]').forEach(btn => {
            btn.addEventListener('click', () => deleteAttachment(btn.getAttribute('data-id')));
        });

        renderDetailsActions(invoice);
    }

    function renderDetailsActions(invoice) {
        const isAdmin = currentUserData?.role === 'admin';
        let html = '';

        if (isAdmin && invoice.status === 'on_approval') {
            html += `<button class="btn btn-success" data-action="approve">Согласовать</button>`;
            html += `<button class="btn btn-danger" data-action="reject">Отклонить</button>`;
        }
        if (isAdmin && invoice.status === 'approved') {
            html += `<button class="btn btn-success" data-action="mark-paid">Отметить оплаченным</button>`;
        }
        if (isAdmin) {
            html += invoice.is_archived
                ? `<button class="btn btn-secondary" data-action="unarchive">Вернуть из архива</button>`
                : `<button class="btn btn-secondary" data-action="archive">В архив</button>`;
        }
        html += `<button class="btn btn-secondary" data-action="close">Закрыть</button>`;

        elements.detailsActions.innerHTML = html;
        elements.detailsActions.querySelectorAll('button[data-action]').forEach(btn => {
            btn.addEventListener('click', () => handleDetailsAction(btn.getAttribute('data-action'), invoice.id));
        });
    }

    async function handleDetailsAction(action, invoiceId) {
        if (action === 'close') { closeDetailsModal(); return; }

        if (action === 'approve') { await approveInvoice(invoiceId); await openDetailsModal(invoiceId); return; }
        if (action === 'reject') { await rejectInvoice(invoiceId); await openDetailsModal(invoiceId); return; }

        const endpoints = { 'mark-paid': 'mark-paid', archive: 'archive', unarchive: 'unarchive' };
        const endpoint = endpoints[action];
        if (!endpoint) return;

        try {
            const res = await fetch(`/api/invoices/${invoiceId}/${endpoint}`, { method: 'POST', credentials: 'include' });
            const data = await res.json();
            if (!res.ok) { alert(data.error || 'Ошибка'); return; }
            await openDetailsModal(invoiceId);
            await loadInvoices();
        } catch (e) {
            console.error('Ошибка действия по счёту:', e);
            alert('Ошибка выполнения действия');
        }
    }

    async function saveDetailsLineItems() {
        if (!currentDetailsInvoiceId) return;
        const items = readLineItems(elements.detailsLineItemsRows);
        try {
            const res = await fetch(`/api/invoices/${currentDetailsInvoiceId}/line-items`, {
                method: 'PUT',
                credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ items }),
            });
            const data = await res.json();
            if (!res.ok) { alert(data.error || 'Ошибка сохранения распределения'); return; }
            await openDetailsModal(currentDetailsInvoiceId);
            await loadInvoices();
        } catch (e) {
            console.error('Ошибка сохранения распределения:', e);
            alert('Ошибка сохранения распределения');
        }
    }

    async function uploadDetailsAttachments() {
        if (!currentDetailsInvoiceId) return;
        const files = Array.from(elements.detailsAttachmentInput.files || []);
        for (const file of files) {
            await uploadAttachment(currentDetailsInvoiceId, file);
        }
        elements.detailsAttachmentInput.value = '';
        await openDetailsModal(currentDetailsInvoiceId);
    }

    async function deleteAttachment(attachmentId) {
        if (!confirm('Удалить вложение?')) return;
        try {
            const res = await fetch(`/api/invoices/attachments/${attachmentId}`, { method: 'DELETE', credentials: 'include' });
            const data = await res.json();
            if (!res.ok) { alert(data.error || 'Ошибка удаления'); return; }
            await openDetailsModal(currentDetailsInvoiceId);
        } catch (e) {
            console.error('Ошибка удаления вложения:', e);
            alert('Ошибка удаления вложения');
        }
    }

    // =========================================================================
    // СПРАВОЧНИКИ
    // =========================================================================

    function openReferencesModal() {
        currentRefType = 'categories';
        elements.referencesTabs.querySelectorAll('.admin-tab').forEach(b => b.classList.remove('active'));
        elements.referencesTabs.querySelector('[data-ref="categories"]')?.classList.add('active');
        elements.referenceNewName.value = '';
        loadReferenceList();
        elements.referencesModal.classList.add('active');
        elements.referencesOverlay.classList.add('active');
    }

    function closeReferencesModal() {
        elements.referencesModal.classList.remove('active');
        elements.referencesOverlay.classList.remove('active');
    }

    async function loadReferenceList() {
        elements.referencesList.innerHTML = '<p class="form-hint">Загрузка...</p>';
        try {
            const res = await fetch(`/api/invoices/${currentRefType}`, { credentials: 'include' });
            const data = await res.json();
            const list = data[currentRefType] || [];
            renderReferenceList(list);
        } catch (e) {
            console.error('Ошибка загрузки справочника:', e);
            elements.referencesList.innerHTML = '<p class="form-hint">Ошибка загрузки</p>';
        }
    }

    function renderReferenceList(list) {
        if (list.length === 0) {
            elements.referencesList.innerHTML = `<p class="form-hint">Список пуст</p>`;
            return;
        }
        elements.referencesList.innerHTML = list.map(item => `
            <div style="display:flex; gap:8px; align-items:center; padding:4px 0; justify-content: space-between;">
                <span>${escapeHtml(item.name)}</span>
                <span>
                    <button class="btn btn-sm btn-secondary" data-action="edit" data-id="${item.id}" data-name="${escapeHtml(item.name)}">Изменить</button>
                    <button class="btn btn-sm btn-danger" data-action="deactivate" data-id="${item.id}">Удалить</button>
                </span>
            </div>
        `).join('');

        elements.referencesList.querySelectorAll('button[data-action="edit"]').forEach(btn => {
            btn.addEventListener('click', () => editReferenceItem(btn.getAttribute('data-id'), btn.getAttribute('data-name')));
        });
        elements.referencesList.querySelectorAll('button[data-action="deactivate"]').forEach(btn => {
            btn.addEventListener('click', () => deactivateReferenceItem(btn.getAttribute('data-id')));
        });
    }

    async function addReferenceItem() {
        const name = elements.referenceNewName.value.trim();
        if (!name) { alert('Укажите название'); return; }
        try {
            const res = await fetch(`/api/invoices/${currentRefType}`, {
                method: 'POST',
                credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name }),
            });
            const data = await res.json();
            if (!res.ok) { alert(data.error || 'Ошибка добавления'); return; }
            elements.referenceNewName.value = '';
            await loadReferenceList();
            await loadDictionaries();
        } catch (e) {
            console.error('Ошибка добавления в справочник:', e);
            alert('Ошибка добавления');
        }
    }

    async function editReferenceItem(id, currentName) {
        const name = prompt('Новое название:', currentName);
        if (!name || !name.trim()) return;
        try {
            const res = await fetch(`/api/invoices/${currentRefType}/${id}`, {
                method: 'PUT',
                credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name: name.trim() }),
            });
            const data = await res.json();
            if (!res.ok) { alert(data.error || 'Ошибка изменения'); return; }
            await loadReferenceList();
            await loadDictionaries();
        } catch (e) {
            console.error('Ошибка изменения справочника:', e);
            alert('Ошибка изменения');
        }
    }

    async function deactivateReferenceItem(id) {
        if (!confirm('Удалить запись из справочника?')) return;
        try {
            const res = await fetch(`/api/invoices/${currentRefType}/${id}`, { method: 'DELETE', credentials: 'include' });
            const data = await res.json();
            if (!res.ok) { alert(data.error || 'Ошибка удаления'); return; }
            await loadReferenceList();
            await loadDictionaries();
        } catch (e) {
            console.error('Ошибка удаления из справочника:', e);
            alert('Ошибка удаления');
        }
    }

    function formatMoney(value) {
        const num = Number(value) || 0;
        return num.toLocaleString('ru-RU', { minimumFractionDigits: 0, maximumFractionDigits: 2 }) + ' ₽';
    }

    function escapeHtml(str) {
        const div = document.createElement('div');
        div.textContent = str ?? '';
        return div.innerHTML;
    }

    document.addEventListener('DOMContentLoaded', init);

    window.InvoicesModule = {
        onPageActivated,
    };
})();
