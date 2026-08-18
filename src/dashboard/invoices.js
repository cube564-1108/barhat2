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
    let currentDetailsInvoice = null;
    let currentRefType = 'categories';
    let currentRefList = [];
    let editingPayerBankId = null;

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
        sent_to_bank: 'Загружен в банк',
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
        elements.kppInput = document.getElementById('invoice-kpp');
        elements.bankNameInput = document.getElementById('invoice-bank-name');
        elements.bankCorrAccountInput = document.getElementById('invoice-bank-corr-account');
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
        elements.detailsEditFields = document.getElementById('invoice-details-edit-fields');
        elements.detailsSaveFieldsBtn = document.getElementById('invoice-details-save-fields-btn');
        elements.detailsStatusSelect = document.getElementById('invoice-details-status-select');
        elements.detailsSaveStatusBtn = document.getElementById('invoice-details-save-status-btn');
        elements.detailsHistory = document.getElementById('invoice-details-history');
        elements.detailsComments = document.getElementById('invoice-details-comments');
        elements.detailsNewComment = document.getElementById('invoice-details-new-comment');
        elements.detailsSendCommentBtn = document.getElementById('invoice-details-send-comment-btn');

        elements.referencesModal = document.getElementById('invoice-references-modal');
        elements.referencesOverlay = document.getElementById('invoice-references-overlay');
        elements.closeReferencesBtn = document.getElementById('close-invoice-references-btn');
        elements.closeReferencesFooterBtn = document.getElementById('close-invoice-references-footer-btn');
        elements.referencesTabs = document.getElementById('invoice-references-tabs');
        elements.referencesList = document.getElementById('invoice-references-list');
        elements.referenceNewName = document.getElementById('invoice-reference-new-name');
        elements.referenceAddBtn = document.getElementById('invoice-reference-add-btn');

        elements.openPlanfactBtn = document.getElementById('open-planfact-btn');
        elements.planfactModal = document.getElementById('invoice-planfact-modal');
        elements.planfactOverlay = document.getElementById('invoice-planfact-overlay');
        elements.closePlanfactBtn = document.getElementById('close-invoice-planfact-btn');
        elements.closePlanfactFooterBtn = document.getElementById('close-invoice-planfact-footer-btn');
        elements.planfactTabs = document.getElementById('invoice-planfact-tabs');
        elements.planfactTabSync = document.getElementById('invoice-planfact-tab-sync');
        elements.planfactTabMapping = document.getElementById('invoice-planfact-tab-mapping');
        elements.planfactTabUnmatched = document.getElementById('invoice-planfact-tab-unmatched');
        elements.planfactDryRunBtn = document.getElementById('invoice-planfact-dry-run-btn');
        elements.planfactRunBtn = document.getElementById('invoice-planfact-run-btn');
        elements.planfactSyncStatus = document.getElementById('invoice-planfact-sync-status');
        elements.planfactSyncResult = document.getElementById('invoice-planfact-sync-result');
        elements.planfactStoreMapping = document.getElementById('invoice-planfact-store-mapping');
        elements.planfactCategoryMapping = document.getElementById('invoice-planfact-category-mapping');
        elements.planfactUnmatchedList = document.getElementById('invoice-planfact-unmatched-list');

        elements.createBtn?.addEventListener('click', () => openCreateModal());
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
        elements.detailsSaveFieldsBtn?.addEventListener('click', saveDetailsFields);
        elements.detailsSaveStatusBtn?.addEventListener('click', saveDetailsStatus);
        elements.detailsSendCommentBtn?.addEventListener('click', sendDetailsComment);

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
            editingPayerBankId = null;
            loadReferenceList();
        });
        elements.referenceAddBtn?.addEventListener('click', addReferenceItem);

        elements.openPlanfactBtn?.addEventListener('click', openPlanfactModal);
        elements.closePlanfactBtn?.addEventListener('click', closePlanfactModal);
        elements.closePlanfactFooterBtn?.addEventListener('click', closePlanfactModal);
        elements.planfactOverlay?.addEventListener('click', closePlanfactModal);
        elements.planfactTabs?.addEventListener('click', (e) => {
            const btn = e.target.closest('button[data-pf-tab]');
            if (!btn) return;
            switchPlanfactTab(btn.getAttribute('data-pf-tab'));
        });
        elements.planfactDryRunBtn?.addEventListener('click', () => runPlanfactSync(true));
        elements.planfactRunBtn?.addEventListener('click', () => runPlanfactSync(false));
    }

    async function onPageActivated(userData) {
        currentUserData = userData;

        if (!loaded) {
            await loadDictionaries();
            loaded = true;
        }

        if (currentUserData?.role === 'admin') {
            elements.manageReferencesBtn.style.display = '';
            elements.openPlanfactBtn.style.display = '';
        } else {
            elements.manageReferencesBtn.style.display = 'none';
            elements.openPlanfactBtn.style.display = 'none';
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
                    <td>${escapeHtml(inv.created_by_full_name || inv.created_by || '')}</td>
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

    async function sendInvoiceToBank(invoiceId, sandbox) {
        if (!sandbox && !confirm('Отправить платёжку в Модульбанк? Банк создаст черновик — подписывать нужно будет вручную в личном кабинете.')) return;
        try {
            const res = await fetch(`/api/invoices/${invoiceId}/send-to-bank`, {
                method: 'POST',
                credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ sandbox }),
            });
            const data = await res.json();
            if (!res.ok) { alert(data.error || 'Ошибка отправки в банк'); return; }
            if (sandbox) {
                const errors = data.result?.errors?.length ? `\nОшибки банка: ${data.result.errors.join('; ')}` : '';
                alert(`Sandbox-проверка: ${data.ok ? 'банк принял черновик' : 'банк отклонил'}${errors}\n\nСобранный документ:\n${data.result?.document || ''}`);
                return;
            }
            alert('Платёжка загружена в Модульбанк черновиком. Подпишите её в личном кабинете банка.');
            await openDetailsModal(invoiceId);
            await loadInvoices();
        } catch (e) {
            console.error('Ошибка отправки счёта в банк:', e);
            alert('Ошибка отправки в банк');
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

    function openCreateModal(sourceInvoice = null) {
        // Копирование счёта (см. handleDetailsAction 'copy') — переносит
        // всё, кроме файла, суммы и разнесения по проектам/статьям: это
        // именно те поля, которые у одного и того же поставщика меняются
        // от счёта к счёту, остальное (реквизиты, город, плательщик, НДС,
        // назначение платежа) обычно одинаково.
        elements.citySelect.value = sourceInvoice?.city_id || '';
        elements.payerSelect.value = sourceInvoice?.payer_id || '';
        elements.vatSelect.value = sourceInvoice?.vat_id || '';
        elements.counterpartyInput.value = sourceInvoice?.counterparty_name || '';
        elements.amountInput.value = '';
        elements.purposeInput.value = sourceInvoice?.payment_purpose || '';
        elements.innInput.value = sourceInvoice?.counterparty_inn || '';
        elements.bankBikInput.value = sourceInvoice?.counterparty_bank_bik || '';
        elements.bankAccountInput.value = sourceInvoice?.counterparty_bank_account || '';
        elements.kppInput.value = sourceInvoice?.counterparty_kpp || '';
        elements.bankNameInput.value = sourceInvoice?.counterparty_bank_name || '';
        elements.bankCorrAccountInput.value = sourceInvoice?.counterparty_bank_corr_account || '';
        elements.dueDateInput.value = sourceInvoice?.due_date || '';
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
            counterparty_kpp: elements.kppInput.value.trim() || null,
            counterparty_bank_bik: elements.bankBikInput.value.trim() || null,
            counterparty_bank_account: elements.bankAccountInput.value.trim() || null,
            counterparty_bank_name: elements.bankNameInput.value.trim() || null,
            counterparty_bank_corr_account: elements.bankCorrAccountInput.value.trim() || null,
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
            const [detailsRes, historyRes, commentsRes] = await Promise.all([
                fetch(`/api/invoices/${id}`, { credentials: 'include' }),
                fetch(`/api/invoices/${id}/history`, { credentials: 'include' }),
                fetch(`/api/invoices/${id}/comments`, { credentials: 'include' }),
            ]);
            const data = await detailsRes.json();
            if (!detailsRes.ok) { alert(data.error || 'Счёт не найден'); return; }
            const historyData = await historyRes.json();
            const commentsData = await commentsRes.json();

            renderDetails(data.invoice, data.line_items || [], data.attachments || [],
                Boolean(data.can_edit_fields), Boolean(data.can_edit_status));
            renderHistory(historyData.history || []);
            renderComments(commentsData.comments || []);

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

    function renderDetails(invoice, lineItems, attachments, canEditFields, canEditStatus) {
        currentDetailsInvoice = invoice;
        elements.detailsTitle.textContent = `Счёт ${invoice.invoice_number}${invoice.is_archived ? ' (в архиве)' : ''}`;

        const rows = [
            ['Создал', invoice.created_by_full_name || invoice.created_by],
            ['Заведён', invoice.created_at ? invoice.created_at.slice(0, 16).replace('T', ' ') : '—'],
        ];
        if (invoice.approved_by) rows.push(['Согласовал', `${invoice.approved_by_full_name || invoice.approved_by}, ${(invoice.approved_at || '').slice(0, 16).replace('T', ' ')}`]);
        if (invoice.rejected_by) rows.push(['Отклонил', `${invoice.rejected_by_full_name || invoice.rejected_by}${invoice.rejected_reason ? ': ' + invoice.rejected_reason : ''}`]);
        if (invoice.paid_at) rows.push(['Оплачен', invoice.paid_at.slice(0, 16).replace('T', ' ')]);
        if (invoice.is_archived) rows.push(['В архиве с', (invoice.archived_at || '').slice(0, 16).replace('T', ' ')]);

        elements.detailsInfo.innerHTML = rows.map(([label, value]) =>
            `<div style="display:flex; gap:8px; padding:2px 0;"><strong style="min-width:220px;">${escapeHtml(label)}:</strong><span>${escapeHtml(String(value))}</span></div>`
        ).join('') + (invoice.bank_send_error ? `
            <div style="display:flex; gap:8px; padding:2px 0; color:#721c24;"><strong style="min-width:220px;">Ошибка отправки в банк:</strong><span>${escapeHtml(invoice.bank_send_error)}</span></div>
        ` : '');

        elements.detailsStatusSelect.value = invoice.status;
        elements.detailsStatusSelect.disabled = !canEditStatus;
        elements.detailsSaveStatusBtn.style.display = canEditStatus ? '' : 'none';

        renderEditFields(invoice, canEditFields);

        elements.detailsLineItemsRows.innerHTML = '';
        lineItems.forEach(item => {
            elements.detailsLineItemsRows.appendChild(createLineItemRow(item, elements.detailsLineItemsTotal));
        });
        updateLineItemsTotal(elements.detailsLineItemsRows, elements.detailsLineItemsTotal);

        elements.detailsAddLineItemBtn.style.display = canEditFields ? '' : 'none';
        elements.detailsSaveLineItemsBtn.style.display = canEditFields ? '' : 'none';
        elements.detailsLineItemsRows.querySelectorAll('select, input').forEach(el => { el.disabled = !canEditFields; });

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

    function renderEditFields(invoice, canEditFields) {
        const disabled = canEditFields ? '' : 'disabled';
        elements.detailsEditFields.innerHTML = `
            <div class="form-group">
                <label class="form-label">Город</label>
                <select id="edit-field-city" class="form-select" ${disabled}>
                    ${cityList.map(c => `<option value="${c.id}" ${c.id === invoice.city_id ? 'selected' : ''}>${escapeHtml(c.name)}</option>`).join('')}
                </select>
            </div>
            <div class="form-group">
                <label class="form-label">На кого выставлен счёт</label>
                <select id="edit-field-payer" class="form-select" ${disabled}>
                    ${payerList.map(p => `<option value="${p.id}" ${p.id === invoice.payer_id ? 'selected' : ''}>${escapeHtml(p.name)}</option>`).join('')}
                </select>
            </div>
            <div class="form-group">
                <label class="form-label">НДС</label>
                <select id="edit-field-vat" class="form-select" ${disabled}>
                    <option value="">Не указан</option>
                    ${vatList.map(v => `<option value="${v.id}" ${v.id === invoice.vat_id ? 'selected' : ''}>${escapeHtml(v.name)}</option>`).join('')}
                </select>
            </div>
            <div class="form-group">
                <label class="form-label">Наименование контрагента</label>
                <input type="text" id="edit-field-counterparty" class="form-input" value="${escapeHtml(invoice.counterparty_name || '')}" ${disabled}>
            </div>
            <div class="form-group">
                <label class="form-label">ИНН контрагента</label>
                <input type="text" id="edit-field-inn" class="form-input" value="${escapeHtml(invoice.counterparty_inn || '')}" ${disabled}>
            </div>
            <div class="form-group">
                <label class="form-label">Расчётный счёт контрагента</label>
                <input type="text" id="edit-field-account" class="form-input" value="${escapeHtml(invoice.counterparty_bank_account || '')}" ${disabled}>
            </div>
            <div class="form-group">
                <label class="form-label">БИК контрагента</label>
                <input type="text" id="edit-field-bik" class="form-input" value="${escapeHtml(invoice.counterparty_bank_bik || '')}" ${disabled}>
            </div>
            <div class="form-group">
                <label class="form-label">КПП контрагента</label>
                <input type="text" id="edit-field-kpp" class="form-input" value="${escapeHtml(invoice.counterparty_kpp || '')}" ${disabled}>
            </div>
            <div class="form-group">
                <label class="form-label">Банк контрагента</label>
                <input type="text" id="edit-field-bank-name" class="form-input" value="${escapeHtml(invoice.counterparty_bank_name || '')}" ${disabled}>
            </div>
            <div class="form-group">
                <label class="form-label">Корр. счёт банка контрагента</label>
                <input type="text" id="edit-field-corr-account" class="form-input" value="${escapeHtml(invoice.counterparty_bank_corr_account || '')}" ${disabled}>
            </div>
            <div class="form-group">
                <label class="form-label">Сумма, ₽</label>
                <input type="number" id="edit-field-amount" class="form-input" min="0" step="0.01" value="${invoice.amount}" ${disabled}>
            </div>
            <div class="form-group">
                <label class="form-label">Назначение платежа</label>
                <textarea id="edit-field-purpose" class="form-input" rows="2" ${disabled}>${escapeHtml(invoice.payment_purpose || '')}</textarea>
            </div>
            <div class="form-group">
                <label class="form-label">Планируемая дата оплаты</label>
                <input type="date" id="edit-field-due-date" class="form-input" value="${invoice.due_date || ''}" ${disabled}>
            </div>
        `;
        elements.detailsSaveFieldsBtn.style.display = canEditFields ? '' : 'none';
    }

    async function saveDetailsFields() {
        if (!currentDetailsInvoiceId) return;
        const payload = {
            city_id: parseInt(document.getElementById('edit-field-city').value, 10),
            payer_id: parseInt(document.getElementById('edit-field-payer').value, 10),
            vat_id: document.getElementById('edit-field-vat').value ? parseInt(document.getElementById('edit-field-vat').value, 10) : null,
            counterparty_name: document.getElementById('edit-field-counterparty').value.trim() || null,
            counterparty_inn: document.getElementById('edit-field-inn').value.trim() || null,
            counterparty_bank_account: document.getElementById('edit-field-account').value.trim() || null,
            counterparty_bank_bik: document.getElementById('edit-field-bik').value.trim() || null,
            counterparty_kpp: document.getElementById('edit-field-kpp').value.trim() || null,
            counterparty_bank_name: document.getElementById('edit-field-bank-name').value.trim() || null,
            counterparty_bank_corr_account: document.getElementById('edit-field-corr-account').value.trim() || null,
            amount: parseFloat(document.getElementById('edit-field-amount').value),
            payment_purpose: document.getElementById('edit-field-purpose').value.trim(),
            due_date: document.getElementById('edit-field-due-date').value,
        };
        try {
            const res = await fetch(`/api/invoices/${currentDetailsInvoiceId}`, {
                method: 'PUT',
                credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            const data = await res.json();
            if (!res.ok) { alert(data.error || 'Ошибка сохранения'); return; }
            await openDetailsModal(currentDetailsInvoiceId);
            await loadInvoices();
        } catch (e) {
            console.error('Ошибка сохранения счёта:', e);
            alert('Ошибка сохранения счёта');
        }
    }

    async function saveDetailsStatus() {
        if (!currentDetailsInvoiceId) return;
        const status = elements.detailsStatusSelect.value;
        if (!confirm(`Сменить статус на «${STATUS_LABELS[status] || status}»?`)) return;
        try {
            const res = await fetch(`/api/invoices/${currentDetailsInvoiceId}/status`, {
                method: 'PUT',
                credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ status }),
            });
            const data = await res.json();
            if (!res.ok) { alert(data.error || 'Ошибка смены статуса'); return; }
            await openDetailsModal(currentDetailsInvoiceId);
            await loadInvoices();
        } catch (e) {
            console.error('Ошибка смены статуса:', e);
            alert('Ошибка смены статуса');
        }
    }

    function renderHistory(history) {
        if (!history.length) {
            elements.detailsHistory.innerHTML = '<p class="form-hint">Изменений нет</p>';
            return;
        }
        elements.detailsHistory.innerHTML = history.map(h => {
            const when = (h.changed_at || '').slice(0, 16).replace('T', ' ');
            const from = h.old_value !== null && h.old_value !== undefined ? escapeHtml(String(h.old_value)) : '—';
            const to = h.new_value !== null && h.new_value !== undefined ? escapeHtml(String(h.new_value)) : '—';
            return `<div style="padding:3px 0; font-size:13px;">
                <span class="form-hint">${when}</span> — <strong>${escapeHtml(h.changed_by_full_name || h.changed_by)}</strong>:
                ${escapeHtml(h.field_name)}: ${from} → ${to}
            </div>`;
        }).join('');
    }

    function renderComments(comments) {
        elements.detailsComments.innerHTML = comments.length ? comments.map(c => `
            <div style="padding:4px 0; border-bottom:1px solid #eee;">
                <div><strong>${escapeHtml(c.author_full_name || c.author)}</strong> <span class="form-hint">${(c.created_at || '').slice(0, 16).replace('T', ' ')}</span></div>
                <div>${escapeHtml(c.message)}</div>
            </div>
        `).join('') : '<p class="form-hint">Сообщений нет</p>';
    }

    async function sendDetailsComment() {
        if (!currentDetailsInvoiceId) return;
        const message = elements.detailsNewComment.value.trim();
        if (!message) return;
        try {
            const res = await fetch(`/api/invoices/${currentDetailsInvoiceId}/comments`, {
                method: 'POST',
                credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ message }),
            });
            const data = await res.json();
            if (!res.ok) { alert(data.error || 'Ошибка отправки сообщения'); return; }
            elements.detailsNewComment.value = '';
            const commentsRes = await fetch(`/api/invoices/${currentDetailsInvoiceId}/comments`, { credentials: 'include' });
            const commentsData = await commentsRes.json();
            renderComments(commentsData.comments || []);
        } catch (e) {
            console.error('Ошибка отправки сообщения:', e);
            alert('Ошибка отправки сообщения');
        }
    }

    function renderDetailsActions(invoice) {
        const isAdmin = currentUserData?.role === 'admin';
        let html = '';

        if (isAdmin && invoice.status === 'on_approval') {
            html += `<button class="btn btn-success" data-action="approve">Согласовать</button>`;
            html += `<button class="btn btn-danger" data-action="reject">Отклонить</button>`;
        }
        if (isAdmin && invoice.status === 'approved') {
            html += `<button class="btn btn-secondary" data-action="send-to-bank-sandbox">Проверить отправку в банк (sandbox)</button>`;
            html += `<button class="btn btn-primary" data-action="send-to-bank">Отправить в банк</button>`;
            html += `<button class="btn btn-success" data-action="mark-paid">Отметить оплаченным</button>`;
        }
        if (isAdmin && invoice.status === 'sent_to_bank') {
            html += `<button class="btn btn-success" data-action="mark-paid">Отметить оплаченным</button>`;
        }
        if (isAdmin) {
            html += invoice.is_archived
                ? `<button class="btn btn-secondary" data-action="unarchive">Вернуть из архива</button>`
                : `<button class="btn btn-secondary" data-action="archive">В архив</button>`;
        }
        html += `<button class="btn btn-secondary" data-action="copy">Копировать счёт</button>`;
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
        if (action === 'send-to-bank-sandbox') { await sendInvoiceToBank(invoiceId, true); return; }
        if (action === 'send-to-bank') { await sendInvoiceToBank(invoiceId, false); return; }
        if (action === 'copy') { closeDetailsModal(); openCreateModal(currentDetailsInvoice); return; }

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
        editingPayerBankId = null;
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

    // Салоны (проекты) физически живут в модуле cashshifts (своя таблица
    // stores, общая с кассовыми сменами), а не в invoices — остальные
    // справочники этой модалки принадлежат invoices. Обе группы роутов
    // построены по одному и тому же шаблону (GET/POST list, PUT/DELETE
    // /<id>), поэтому вкладку "Салоны" проще переиспользовать здесь, чем
    // заводить отдельную модалку только ради одной вкладки.
    function refApiBase() {
        return currentRefType === 'stores' ? '/api/cash-shifts' : '/api/invoices';
    }

    async function loadReferenceList() {
        elements.referencesList.innerHTML = '<p class="form-hint">Загрузка...</p>';
        try {
            const res = await fetch(`${refApiBase()}/${currentRefType}`, { credentials: 'include' });
            const data = await res.json();
            currentRefList = data[currentRefType] || [];
            renderReferenceList(currentRefList);
        } catch (e) {
            console.error('Ошибка загрузки справочника:', e);
            elements.referencesList.innerHTML = '<p class="form-hint">Ошибка загрузки</p>';
        }
    }

    // Реквизиты расчётного счёта плательщика (Фаза 5) — своя запись у каждой
    // компании, все в одном кабинете Модульбанка под одним токеном. Пустые
    // реквизиты = этот плательщик не проводится через банк-автоматику.
    const PAYER_BANK_FIELDS = [
        ['inn', 'ИНН'], ['kpp', 'КПП'], ['bank_account', 'Расчётный счёт'],
        ['bank_name', 'Банк'], ['bank_bik', 'БИК'], ['bank_corr_account', 'Корр. счёт'],
    ];

    function renderPayerBankForm(item) {
        return `
            <div style="padding:8px 0 12px; border-top: 1px dashed #ccc; margin-top:4px;">
                ${PAYER_BANK_FIELDS.map(([field, label]) => `
                    <div class="form-group">
                        <label class="form-label">${label}</label>
                        <input type="text" class="form-input" data-payer-bank-field="${field}" value="${escapeHtml(item[field] || '')}">
                    </div>
                `).join('')}
                <button class="btn btn-sm btn-success" data-action="save-bank" data-id="${item.id}">Сохранить реквизиты</button>
                <button class="btn btn-sm btn-secondary" data-action="cancel-bank">Отмена</button>
            </div>
        `;
    }

    function renderReferenceList(list) {
        if (list.length === 0) {
            elements.referencesList.innerHTML = `<p class="form-hint">Список пуст</p>`;
            return;
        }
        const isPayers = currentRefType === 'payers';
        elements.referencesList.innerHTML = list.map(item => `
            <div data-payer-row="${item.id}" style="padding:4px 0;">
                <div style="display:flex; gap:8px; align-items:center; justify-content: space-between;">
                    <span>${escapeHtml(item.name)}</span>
                    <span>
                        <button class="btn btn-sm btn-secondary" data-action="edit" data-id="${item.id}" data-name="${escapeHtml(item.name)}">Изменить</button>
                        ${isPayers ? `<button class="btn btn-sm btn-secondary" data-action="toggle-bank" data-id="${item.id}">${item.bank_account ? 'Реквизиты банка ✓' : 'Реквизиты банка'}</button>` : ''}
                        <button class="btn btn-sm btn-danger" data-action="deactivate" data-id="${item.id}">Удалить</button>
                    </span>
                </div>
                ${isPayers && editingPayerBankId === item.id ? renderPayerBankForm(item) : ''}
            </div>
        `).join('');

        elements.referencesList.querySelectorAll('button[data-action="edit"]').forEach(btn => {
            btn.addEventListener('click', () => editReferenceItem(btn.getAttribute('data-id'), btn.getAttribute('data-name')));
        });
        elements.referencesList.querySelectorAll('button[data-action="deactivate"]').forEach(btn => {
            btn.addEventListener('click', () => deactivateReferenceItem(btn.getAttribute('data-id')));
        });
        elements.referencesList.querySelectorAll('button[data-action="toggle-bank"]').forEach(btn => {
            btn.addEventListener('click', () => {
                const id = parseInt(btn.getAttribute('data-id'), 10);
                editingPayerBankId = editingPayerBankId === id ? null : id;
                renderReferenceList(currentRefList);
            });
        });
        elements.referencesList.querySelectorAll('button[data-action="cancel-bank"]').forEach(btn => {
            btn.addEventListener('click', () => { editingPayerBankId = null; renderReferenceList(currentRefList); });
        });
        elements.referencesList.querySelectorAll('button[data-action="save-bank"]').forEach(btn => {
            btn.addEventListener('click', () => savePayerBankRequisites(parseInt(btn.getAttribute('data-id'), 10)));
        });
    }

    async function savePayerBankRequisites(id) {
        const row = elements.referencesList.querySelector(`[data-payer-row="${id}"]`);
        const payload = {};
        PAYER_BANK_FIELDS.forEach(([field]) => {
            payload[field] = row.querySelector(`[data-payer-bank-field="${field}"]`).value.trim();
        });
        try {
            const res = await fetch(`/api/invoices/payers/${id}/bank-requisites`, {
                method: 'PUT',
                credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            const data = await res.json();
            if (!res.ok) { alert(data.error || 'Ошибка сохранения реквизитов'); return; }
            editingPayerBankId = null;
            await loadReferenceList();
        } catch (e) {
            console.error('Ошибка сохранения реквизитов плательщика:', e);
            alert('Ошибка сохранения реквизитов');
        }
    }

    async function addReferenceItem() {
        const name = elements.referenceNewName.value.trim();
        if (!name) { alert('Укажите название'); return; }
        try {
            const res = await fetch(`${refApiBase()}/${currentRefType}`, {
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
            const res = await fetch(`${refApiBase()}/${currentRefType}/${id}`, {
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
            const res = await fetch(`${refApiBase()}/${currentRefType}/${id}`, { method: 'DELETE', credentials: 'include' });
            const data = await res.json();
            if (!res.ok) { alert(data.error || 'Ошибка удаления'); return; }
            await loadReferenceList();
            await loadDictionaries();
        } catch (e) {
            console.error('Ошибка удаления из справочника:', e);
            alert('Ошибка удаления');
        }
    }

    // =========================================================================
    // ПЛАНФАКТ — синхронизация оплаченных счетов, сопоставление, требует внимания
    // =========================================================================

    function openPlanfactModal() {
        switchPlanfactTab('sync');
        elements.planfactSyncStatus.textContent = '';
        elements.planfactSyncResult.innerHTML = '';
        elements.planfactModal.classList.add('active');
        elements.planfactOverlay.classList.add('active');
    }

    function closePlanfactModal() {
        elements.planfactModal.classList.remove('active');
        elements.planfactOverlay.classList.remove('active');
    }

    function switchPlanfactTab(tab) {
        elements.planfactTabs.querySelectorAll('.admin-tab').forEach(b => b.classList.remove('active'));
        elements.planfactTabs.querySelector(`[data-pf-tab="${tab}"]`)?.classList.add('active');

        elements.planfactTabSync.style.display = tab === 'sync' ? '' : 'none';
        elements.planfactTabMapping.style.display = tab === 'mapping' ? '' : 'none';
        elements.planfactTabUnmatched.style.display = tab === 'unmatched' ? '' : 'none';

        if (tab === 'mapping') loadPlanfactMappingTab();
        if (tab === 'unmatched') loadPlanfactUnmatchedTab();
    }

    async function runPlanfactSync(dryRun) {
        elements.planfactDryRunBtn.disabled = true;
        elements.planfactRunBtn.disabled = true;
        elements.planfactSyncStatus.textContent = dryRun ? 'Проверяю...' : 'Синхронизирую...';
        elements.planfactSyncResult.innerHTML = '';
        try {
            const res = await fetch('/api/invoices/planfact/sync', {
                method: 'POST',
                credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ dry_run: dryRun }),
            });
            const data = await res.json();
            if (!res.ok) {
                elements.planfactSyncStatus.textContent = data.error || 'Ошибка синхронизации';
                return;
            }

            if (dryRun) {
                elements.planfactSyncStatus.textContent = 'Проверка завершена, ничего не записано в ПланФакт:';
                renderPlanfactSyncResult(data.matched || [], data.unmatched || []);
            } else {
                elements.planfactSyncStatus.textContent = 'Синхронизация запущена в фоне...';
                pollPlanfactSyncStatus();
            }
        } catch (e) {
            console.error('Ошибка синхронизации с ПланФакт:', e);
            elements.planfactSyncStatus.textContent = 'Ошибка синхронизации';
        } finally {
            elements.planfactDryRunBtn.disabled = false;
            elements.planfactRunBtn.disabled = false;
        }
    }

    async function pollPlanfactSyncStatus() {
        for (let attempt = 0; attempt < 60; attempt++) {
            await new Promise(resolve => setTimeout(resolve, 2000));
            try {
                const res = await fetch('/api/invoices/planfact/sync-status', { credentials: 'include' });
                const data = await res.json();
                const status = data.status;
                if (!status || status.status !== 'started') {
                    if (!status) {
                        elements.planfactSyncStatus.textContent = 'Статус синхронизации недоступен';
                    } else if (status.status === 'completed') {
                        elements.planfactSyncStatus.textContent =
                            `Готово: разнесено ${status.matched_count}, требует внимания ${status.unmatched_count}`;
                    } else {
                        elements.planfactSyncStatus.textContent = `Ошибка: ${status.error_message || 'см. логи сервера'}`;
                    }
                    await loadInvoices();
                    return;
                }
            } catch (e) {
                console.error('Ошибка опроса статуса синхронизации:', e);
                return;
            }
        }
        elements.planfactSyncStatus.textContent = 'Синхронизация выполняется дольше обычного, проверьте статус позже';
    }

    function renderPlanfactSyncResult(matched, unmatched) {
        let html = '';
        if (matched.length) {
            html += `<p><strong>Разнесено (${matched.length}):</strong></p>` + matched.map(m => `
                <div style="padding:3px 0; font-size:13px;">Счёт ${escapeHtml(m.invoice_number || '')} (${escapeHtml(m.match_code)}) — ${formatMoney(m.operation_amount)}</div>
            `).join('');
        }
        if (unmatched.length) {
            html += `<p><strong>Требует внимания (${unmatched.length}):</strong></p>` + unmatched.map(u => `
                <div style="padding:3px 0; font-size:13px;">${escapeHtml(u.match_code || u.operation_id)} — ${escapeHtml(u.reason)}</div>
            `).join('');
        }
        if (!matched.length && !unmatched.length) {
            html = '<p class="form-hint">Подходящих операций не найдено</p>';
        }
        elements.planfactSyncResult.innerHTML = html;
    }

    async function loadPlanfactMappingTab() {
        elements.planfactStoreMapping.innerHTML = '<p class="form-hint">Загрузка...</p>';
        elements.planfactCategoryMapping.innerHTML = '<p class="form-hint">Загрузка...</p>';
        try {
            const [storesRes, categoriesRes, pfProjectsRes, pfCategoriesRes] = await Promise.all([
                fetch('/api/invoices/planfact/mappings/stores', { credentials: 'include' }),
                fetch('/api/invoices/categories', { credentials: 'include' }),
                fetch('/api/invoices/planfact/projects', { credentials: 'include' }),
                fetch('/api/invoices/planfact/categories', { credentials: 'include' }),
            ]);
            const storesData = await storesRes.json();
            const categoriesData = await categoriesRes.json();
            const pfProjects = pfProjectsRes.ok ? (await pfProjectsRes.json()).projects || [] : null;
            const pfCategories = pfCategoriesRes.ok ? (await pfCategoriesRes.json()).categories || [] : null;

            renderStoreMapping(storesData.stores || [], pfProjects);
            renderCategoryMapping(categoriesData.categories || [], pfCategories);
        } catch (e) {
            console.error('Ошибка загрузки сопоставления с ПланФакт:', e);
            elements.planfactStoreMapping.innerHTML = '<p class="form-hint">Ошибка загрузки</p>';
            elements.planfactCategoryMapping.innerHTML = '<p class="form-hint">Ошибка загрузки</p>';
        }
    }

    function mappingRowHtml(id, label, currentValue, options, dataAttr) {
        if (options) {
            const optionsHtml = ['<option value="">— не сопоставлено —</option>']
                .concat(options.map(o => {
                    const value = o.projectId ?? o.operationCategoryId;
                    const title = o.title ?? o.name ?? String(value);
                    const selected = String(value) === String(currentValue) ? 'selected' : '';
                    return `<option value="${escapeHtml(String(value))}" ${selected}>${escapeHtml(title)}</option>`;
                }))
                .join('');
            return `
                <div style="display:flex; gap:8px; align-items:center; padding:4px 0; justify-content: space-between;">
                    <span style="min-width:220px;">${escapeHtml(label)}</span>
                    <select class="form-select" style="width:260px;" data-${dataAttr}="${id}">${optionsHtml}</select>
                </div>
            `;
        }
        return `
            <div style="display:flex; gap:8px; align-items:center; padding:4px 0; justify-content: space-between;">
                <span style="min-width:220px;">${escapeHtml(label)}</span>
                <input type="text" class="form-input" style="width:260px;" placeholder="id в ПланФакт" value="${escapeHtml(currentValue || '')}" data-${dataAttr}="${id}">
            </div>
        `;
    }

    function renderStoreMapping(stores, pfProjects) {
        if (!stores.length) {
            elements.planfactStoreMapping.innerHTML = '<p class="form-hint">Салонов нет</p>';
            return;
        }
        if (pfProjects === null) {
            elements.planfactStoreMapping.innerHTML = '<p class="form-hint">Не удалось получить список проектов из ПланФакт — впишите id вручную.</p>' +
                stores.map(s => mappingRowHtml(s.id, s.name, s.planfact_project_id, null, 'store-id')).join('');
        } else {
            elements.planfactStoreMapping.innerHTML = stores.map(s => mappingRowHtml(s.id, s.name, s.planfact_project_id, pfProjects, 'store-id')).join('');
        }
        elements.planfactStoreMapping.querySelectorAll('[data-store-id]').forEach(el => {
            const handler = () => savePlanfactStoreMapping(el.getAttribute('data-store-id'), el.value.trim());
            el.addEventListener(el.tagName === 'SELECT' ? 'change' : 'blur', handler);
        });
    }

    function renderCategoryMapping(categories, pfCategories) {
        if (!categories.length) {
            elements.planfactCategoryMapping.innerHTML = '<p class="form-hint">Статей нет</p>';
            return;
        }
        if (pfCategories === null) {
            elements.planfactCategoryMapping.innerHTML = '<p class="form-hint">Не удалось получить список статей из ПланФакт — впишите id вручную.</p>' +
                categories.map(c => mappingRowHtml(c.id, c.name, c.planfact_category_id, null, 'category-id')).join('');
        } else {
            elements.planfactCategoryMapping.innerHTML = categories.map(c => mappingRowHtml(c.id, c.name, c.planfact_category_id, pfCategories, 'category-id')).join('');
        }
        elements.planfactCategoryMapping.querySelectorAll('[data-category-id]').forEach(el => {
            const handler = () => savePlanfactCategoryMapping(el.getAttribute('data-category-id'), el.value.trim());
            el.addEventListener(el.tagName === 'SELECT' ? 'change' : 'blur', handler);
        });
    }

    async function savePlanfactStoreMapping(storeId, planfactProjectId) {
        try {
            const res = await fetch(`/api/invoices/planfact/mappings/stores/${storeId}`, {
                method: 'PUT',
                credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ planfact_project_id: planfactProjectId }),
            });
            const data = await res.json();
            if (!res.ok) alert(data.error || 'Ошибка сохранения сопоставления');
        } catch (e) {
            console.error('Ошибка сохранения сопоставления салона:', e);
            alert('Ошибка сохранения сопоставления');
        }
    }

    async function savePlanfactCategoryMapping(categoryId, planfactCategoryId) {
        try {
            const res = await fetch(`/api/invoices/categories/${categoryId}/planfact-mapping`, {
                method: 'PUT',
                credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ planfact_category_id: planfactCategoryId }),
            });
            const data = await res.json();
            if (!res.ok) alert(data.error || 'Ошибка сохранения сопоставления');
        } catch (e) {
            console.error('Ошибка сохранения сопоставления статьи:', e);
            alert('Ошибка сохранения сопоставления');
        }
    }

    async function loadPlanfactUnmatchedTab() {
        elements.planfactUnmatchedList.innerHTML = '<p class="form-hint">Загрузка...</p>';
        try {
            const res = await fetch('/api/invoices/planfact/unmatched', { credentials: 'include' });
            const data = await res.json();
            renderPlanfactUnmatched(data.unmatched || []);
        } catch (e) {
            console.error('Ошибка загрузки списка "Требует внимания":', e);
            elements.planfactUnmatchedList.innerHTML = '<p class="form-hint">Ошибка загрузки</p>';
        }
    }

    function renderPlanfactUnmatched(list) {
        if (!list.length) {
            elements.planfactUnmatchedList.innerHTML = '<p class="form-hint">Нерешённых операций нет</p>';
            return;
        }
        elements.planfactUnmatchedList.innerHTML = list.map(u => `
            <div style="display:flex; gap:8px; align-items:center; justify-content: space-between; padding:6px 0; border-bottom:1px solid #eee;">
                <div>
                    <div><strong>${escapeHtml(u.match_code || u.planfact_operation_id)}</strong> ${u.operation_amount ? formatMoney(u.operation_amount) : ''}</div>
                    <div class="form-hint">${escapeHtml(u.reason)}</div>
                    <div class="form-hint">${escapeHtml((u.detected_at || '').slice(0, 16).replace('T', ' '))}</div>
                </div>
                <button class="btn btn-sm btn-secondary" data-action="resolve" data-id="${u.id}">Разнесено вручную</button>
            </div>
        `).join('');
        elements.planfactUnmatchedList.querySelectorAll('button[data-action="resolve"]').forEach(btn => {
            btn.addEventListener('click', () => resolvePlanfactUnmatched(btn.getAttribute('data-id')));
        });
    }

    async function resolvePlanfactUnmatched(id) {
        try {
            const res = await fetch(`/api/invoices/planfact/unmatched/${id}/resolve`, { method: 'POST', credentials: 'include' });
            const data = await res.json();
            if (!res.ok) { alert(data.error || 'Ошибка'); return; }
            await loadPlanfactUnmatchedTab();
        } catch (e) {
            console.error('Ошибка отметки операции как решённой:', e);
            alert('Ошибка');
        }
    }

    function formatMoney(value) {
        const num = Number(value) || 0;
        return num.toLocaleString('ru-RU', { minimumFractionDigits: 0, maximumFractionDigits: 2 }) + ' ₽';
    }

    // ВАЖНО: не заменять на div.textContent/innerHTML — тот способ не
    // экранирует кавычки, из-за чего значение с " (например название
    // контрагента ООО Фирма "Арома-Люкс") обрывалось прямо на кавычке при
    // подстановке в value="${...}" — браузер закрывал атрибут раньше
    // времени, и обрезанное значение потом реально пересохранялось в БД
    // (см. историю сессий, счёт СЧ-000003, инцидент 2026-08-18).
    function escapeHtml(str) {
        return String(str ?? '').replace(/[&<>"']/g, (c) => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
        }[c]));
    }

    document.addEventListener('DOMContentLoaded', init);

    window.InvoicesModule = {
        onPageActivated,
    };
})();
