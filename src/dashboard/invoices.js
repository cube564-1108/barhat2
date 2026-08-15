/**
 * Счета на оплату БАРХАТ
 * Создание и согласование счетов (замена формы в Pyrus)
 */

(function() {
    'use strict';

    let currentUserData = null;
    let storeList = [];
    let categoryList = [];
    let loaded = false;

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
        elements.statusFilter = document.getElementById('invoices-status-filter');
        elements.tbody = document.getElementById('invoices-tbody');
        elements.createBtn = document.getElementById('create-invoice-btn');

        elements.modal = document.getElementById('create-invoice-modal');
        elements.overlay = document.getElementById('create-invoice-overlay');
        elements.closeBtn = document.getElementById('close-create-invoice-btn');
        elements.cancelBtn = document.getElementById('cancel-create-invoice-btn');
        elements.confirmBtn = document.getElementById('confirm-create-invoice-btn');

        elements.storeSelect = document.getElementById('invoice-store');
        elements.categorySelect = document.getElementById('invoice-category');
        elements.counterpartyInput = document.getElementById('invoice-counterparty');
        elements.amountInput = document.getElementById('invoice-amount');
        elements.descriptionInput = document.getElementById('invoice-description');
        elements.innInput = document.getElementById('invoice-inn');
        elements.bankNameInput = document.getElementById('invoice-bank-name');
        elements.bankBikInput = document.getElementById('invoice-bank-bik');
        elements.bankAccountInput = document.getElementById('invoice-bank-account');
        elements.bankCorrInput = document.getElementById('invoice-bank-corr');
        elements.dueDateInput = document.getElementById('invoice-due-date');

        if (!elements.tbody) return; // Страницы нет в DOM — модуль не нужен

        elements.statusFilter?.addEventListener('change', loadInvoices);
        elements.createBtn?.addEventListener('click', openCreateModal);
        elements.closeBtn?.addEventListener('click', closeCreateModal);
        elements.cancelBtn?.addEventListener('click', closeCreateModal);
        elements.overlay?.addEventListener('click', closeCreateModal);
        elements.confirmBtn?.addEventListener('click', submitInvoice);
    }

    async function onPageActivated(userData) {
        currentUserData = userData;

        if (!loaded) {
            await Promise.all([loadStores(), loadCategories()]);
            loaded = true;
        }

        await loadInvoices();
    }

    async function loadStores() {
        try {
            const res = await fetch('/api/invoices/stores', { credentials: 'include' });
            const data = await res.json();
            storeList = data.stores || [];
            elements.storeSelect.innerHTML = storeList
                .map(s => `<option value="${s.id}">${escapeHtml(s.name)}</option>`)
                .join('');
        } catch (e) {
            console.error('Ошибка загрузки салонов:', e);
        }
    }

    async function loadCategories() {
        try {
            const res = await fetch('/api/invoices/categories', { credentials: 'include' });
            const data = await res.json();
            categoryList = data.categories || [];
            elements.categorySelect.innerHTML = categoryList
                .map(c => `<option value="${c.id}">${escapeHtml(c.name)}</option>`)
                .join('');
        } catch (e) {
            console.error('Ошибка загрузки статей расхода:', e);
        }
    }

    async function loadInvoices() {
        elements.tbody.innerHTML = `<tr><td colspan="9" style="text-align:center; color: var(--barkhat-gray); padding:20px;">Загрузка данных...</td></tr>`;

        try {
            const status = elements.statusFilter?.value || '';
            const params = new URLSearchParams();
            if (status) params.set('status', status);

            const res = await fetch(`/api/invoices?${params.toString()}`, { credentials: 'include' });
            const data = await res.json();
            renderInvoices(data.invoices || []);
        } catch (e) {
            console.error('Ошибка загрузки счетов:', e);
            elements.tbody.innerHTML = `<tr><td colspan="9" style="text-align:center; color: var(--barkhat-gray); padding:20px;">Ошибка загрузки</td></tr>`;
        }
    }

    function storeName(id) {
        return storeList.find(s => s.id === id)?.name || `#${id}`;
    }

    function categoryName(id) {
        return categoryList.find(c => c.id === id)?.name || `#${id}`;
    }

    function renderInvoices(invoices) {
        if (invoices.length === 0) {
            elements.tbody.innerHTML = `<tr><td colspan="9" style="text-align:center; color: var(--barkhat-gray); padding:20px;">Счетов нет</td></tr>`;
            return;
        }

        const isAdmin = currentUserData?.role === 'admin';

        elements.tbody.innerHTML = invoices.map(inv => {
            const colors = STATUS_COLORS[inv.status] || { bg: '#eee', color: '#333' };
            const badge = `<span class="status-badge" style="background:${colors.bg}; color:${colors.color};">${STATUS_LABELS[inv.status] || inv.status}</span>`;

            let actions = '';
            if (isAdmin && inv.status === 'on_approval') {
                actions = `
                    <button class="btn btn-sm btn-success" data-action="approve" data-id="${inv.id}">Согласовать</button>
                    <button class="btn btn-sm btn-danger" data-action="reject" data-id="${inv.id}">Отклонить</button>
                `;
            }

            const date = inv.created_at ? inv.created_at.slice(0, 16).replace('T', ' ') : '';

            return `
                <tr>
                    <td>${escapeHtml(inv.invoice_number || '')}</td>
                    <td>${escapeHtml(storeName(inv.store_id))}</td>
                    <td>${escapeHtml(inv.counterparty_name)}</td>
                    <td>${escapeHtml(categoryName(inv.expense_category_id))}</td>
                    <td>${formatMoney(inv.amount)}</td>
                    <td>${badge}</td>
                    <td>${escapeHtml(inv.created_by || '')}</td>
                    <td>${date}</td>
                    <td style="white-space: nowrap;">${actions}</td>
                </tr>
            `;
        }).join('');

        elements.tbody.querySelectorAll('button[data-action]').forEach(btn => {
            btn.addEventListener('click', () => {
                const id = btn.getAttribute('data-id');
                const action = btn.getAttribute('data-action');
                if (action === 'approve') approveInvoice(id);
                if (action === 'reject') rejectInvoice(id);
            });
        });
    }

    async function approveInvoice(id) {
        if (!confirm('Согласовать счёт?')) return;
        try {
            const res = await fetch(`/api/invoices/${id}/approve`, {
                method: 'POST',
                credentials: 'include',
            });
            const data = await res.json();
            if (!res.ok) {
                alert(data.error || 'Ошибка согласования');
                return;
            }
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
            if (!res.ok) {
                alert(data.error || 'Ошибка отклонения');
                return;
            }
            await loadInvoices();
        } catch (e) {
            console.error('Ошибка отклонения счёта:', e);
            alert('Ошибка отклонения счёта');
        }
    }

    function openCreateModal() {
        elements.counterpartyInput.value = '';
        elements.amountInput.value = '';
        elements.descriptionInput.value = '';
        elements.innInput.value = '';
        elements.bankNameInput.value = '';
        elements.bankBikInput.value = '';
        elements.bankAccountInput.value = '';
        elements.bankCorrInput.value = '';
        elements.dueDateInput.value = '';

        elements.modal.classList.add('active');
        elements.overlay.classList.add('active');
    }

    function closeCreateModal() {
        elements.modal.classList.remove('active');
        elements.overlay.classList.remove('active');
    }

    async function submitInvoice() {
        const storeId = parseInt(elements.storeSelect.value, 10);
        const categoryId = parseInt(elements.categorySelect.value, 10);
        const counterpartyName = elements.counterpartyInput.value.trim();
        const amount = parseFloat(elements.amountInput.value);

        if (!storeId || !categoryId) {
            alert('Выберите салон и статью расхода');
            return;
        }
        if (!counterpartyName) {
            alert('Укажите контрагента');
            return;
        }
        if (!amount || amount <= 0) {
            alert('Укажите корректную сумму');
            return;
        }

        const payload = {
            store_id: storeId,
            expense_category_id: categoryId,
            counterparty_name: counterpartyName,
            amount: amount,
            description: elements.descriptionInput.value.trim() || null,
            counterparty_inn: elements.innInput.value.trim() || null,
            counterparty_bank_name: elements.bankNameInput.value.trim() || null,
            counterparty_bank_bik: elements.bankBikInput.value.trim() || null,
            counterparty_bank_account: elements.bankAccountInput.value.trim() || null,
            counterparty_bank_corr_account: elements.bankCorrInput.value.trim() || null,
            due_date: elements.dueDateInput.value || null,
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
            if (!res.ok) {
                alert(data.error || 'Ошибка создания счёта');
                return;
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
