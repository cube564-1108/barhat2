/**
 * Оплата курьерам — JavaScript
 *
 * Отчёт: сумма поля «Себестоимость доставки» по выполненным заказам RetailCRM,
 * период — по дате доставки, группировка — по курьеру.
 * Данные читаются из /api/couriers/* (локальная база, наполняется фоновым синком).
 */

(function() {
    'use strict';

    let citiesLoaded = false;
    let isAdmin = false;
    let lastReport = null;

    // Кнопки, меняющие данные и справочники, — только администратору
    const ADMIN_BUTTONS = ['syncCourierData', 'loadCourierPeriod', 'manageCouriers'];

    document.addEventListener('userRoleChanged', function(e) {
        isAdmin = e.detail && e.detail.role === 'admin';
        ADMIN_BUTTONS.forEach(function(id) {
            const btn = document.getElementById(id);
            if (btn) btn.style.display = isAdmin ? '' : 'none';
        });
    });

    // ================= Период =================

    // Выплаты курьерам считают за календарный месяц, поэтому по умолчанию —
    // с 1-го числа текущего месяца по сегодня, а не «последние N дней».
    function applyDefaultPeriod() {
        const today = new Date();
        const first = new Date(today.getFullYear(), today.getMonth(), 1);

        const fromEl = document.getElementById('courierDateFrom');
        const toEl = document.getElementById('courierDateTo');
        if (fromEl) fromEl.value = toIsoDate(first);
        if (toEl) toEl.value = toIsoDate(today);
    }

    function toIsoDate(date) {
        const month = String(date.getMonth() + 1).padStart(2, '0');
        const day = String(date.getDate()).padStart(2, '0');
        return `${date.getFullYear()}-${month}-${day}`;
    }

    function currentFilters() {
        return {
            dateFrom: document.getElementById('courierDateFrom').value,
            dateTo: document.getElementById('courierDateTo').value,
            city: document.getElementById('courierCity').value,
            onlyOwn: document.getElementById('courierOnlyOwn').checked,
        };
    }

    // ================= Загрузка данных =================

    async function loadCities() {
        if (citiesLoaded) return;

        try {
            const response = await fetch('/api/couriers/cities', { credentials: 'include' });
            const result = await response.json();
            if (!result.success) return;

            const select = document.getElementById('courierCity');
            if (!select) return;

            const previous = select.value;
            select.innerHTML = '<option value="">Все города</option>';
            result.data.forEach(function(city) {
                const option = document.createElement('option');
                option.value = city;
                option.textContent = city;
                select.appendChild(option);
            });
            select.value = previous;

            citiesLoaded = true;
        } catch (error) {
            console.error('Ошибка загрузки городов:', error);
        }
    }

    async function loadReport() {
        const filters = currentFilters();

        const url = new URL('/api/couriers/report', window.location.origin);
        if (filters.dateFrom) url.searchParams.set('date_from', filters.dateFrom);
        if (filters.dateTo) url.searchParams.set('date_to', filters.dateTo);
        if (filters.city) url.searchParams.set('city', filters.city);
        url.searchParams.set('only_own', filters.onlyOwn ? '1' : '0');

        showTableMessage('Загрузка данных...', 'abc-empty');

        try {
            const response = await fetch(url, { credentials: 'include' });
            const result = await response.json();

            if (!result.success) {
                showTableMessage('Ошибка: ' + result.error, 'abc-error');
                return;
            }

            lastReport = result.data;
            renderTable(result.data);
            renderKpis(result.data.totals);
        } catch (error) {
            console.error('Ошибка загрузки отчёта по курьерам:', error);
            showTableMessage('Ошибка сети: ' + error.message, 'abc-error');
        }
    }

    function showTableMessage(text, className) {
        const tbody = document.getElementById('courierTableBody');
        if (!tbody) return;
        tbody.innerHTML = `<tr><td colspan="6" class="${className}">${escapeHtml(text)}</td></tr>`;
    }

    function renderTable(report) {
        const tbody = document.getElementById('courierTableBody');
        if (!tbody) return;

        const rows = report.couriers || [];
        if (rows.length === 0) {
            showTableMessage('Нет выполненных заказов с курьером за выбранный период', 'abc-empty');
            return;
        }

        // Имена приходят из CRM и попадают в innerHTML — экранируем
        tbody.innerHTML = rows.map(function(row, index) {
            const serviceBadge = row.is_service
                ? ' <span class="bx-badge bx-badge--muted">Служба</span>'
                : '';
            return `
            <tr>
                <td class="num">${index + 1}</td>
                <td>${escapeHtml(row.courier_name)}${serviceBadge}</td>
                <td>${escapeHtml((row.cities || []).join(', ') || '—')}</td>
                <td class="num">${formatCount(row.orders_count)}</td>
                <td class="num">${formatMoney(row.total_net_cost)}</td>
                <td class="num">
                    <button class="btn btn-secondary btn-sm" data-courier-id="${row.courier_id}">Заказы</button>
                </td>
            </tr>`;
        }).join('');

        tbody.querySelectorAll('button[data-courier-id]').forEach(function(btn) {
            btn.addEventListener('click', function() {
                const courierId = Number(btn.getAttribute('data-courier-id'));
                const courier = rows.find(function(r) { return r.courier_id === courierId; });
                openOrdersModal(courierId, courier ? courier.courier_name : '');
            });
        });
    }

    function renderKpis(totals) {
        document.getElementById('courierTotalSum').textContent = formatMoney(totals.total_net_cost);
        document.getElementById('courierTotalCouriers').textContent = formatCount(totals.couriers_count);
        document.getElementById('courierTotalOrders').textContent = formatCount(totals.orders_count);

        // Заказы без курьера — это не ноль работы, а незаполненное поле в CRM:
        // деньги по ним потрачены, но кому платить — неизвестно. Показываем
        // и количество, и сумму, чтобы дырка была видна, а не терялась.
        const noCourierEl = document.getElementById('courierNoCourier');
        noCourierEl.textContent = totals.orders_without_courier
            ? `${formatCount(totals.orders_without_courier)} · ${formatMoney(totals.net_cost_without_courier)}`
            : '0';
    }

    // ================= Расшифровка по заказам =================

    async function openOrdersModal(courierId, courierName) {
        const modal = document.getElementById('courier-orders-modal');
        const tbody = document.getElementById('courierOrdersBody');
        document.getElementById('courierOrdersTitle').textContent = 'Заказы: ' + (courierName || '—');
        tbody.innerHTML = '<tr><td colspan="5" class="abc-empty">Загрузка...</td></tr>';
        modal.classList.add('active');

        const filters = currentFilters();
        const url = new URL('/api/couriers/report/orders', window.location.origin);
        if (filters.dateFrom) url.searchParams.set('date_from', filters.dateFrom);
        if (filters.dateTo) url.searchParams.set('date_to', filters.dateTo);
        if (filters.city) url.searchParams.set('city', filters.city);
        url.searchParams.set('courier_id', String(courierId));

        try {
            const response = await fetch(url, { credentials: 'include' });
            const result = await response.json();

            if (!result.success) {
                tbody.innerHTML = `<tr><td colspan="5" class="abc-error">Ошибка: ${escapeHtml(result.error)}</td></tr>`;
                return;
            }

            if (!result.data.length) {
                tbody.innerHTML = '<tr><td colspan="5" class="abc-empty">Заказов нет</td></tr>';
                return;
            }

            tbody.innerHTML = result.data.map(function(order) {
                return `
                <tr>
                    <td>${escapeHtml(order.order_number || order.retailcrm_order_id)}</td>
                    <td>${escapeHtml(order.delivery_date)}</td>
                    <td>${escapeHtml(order.city || '—')}</td>
                    <td>${escapeHtml(order.delivery_city || '—')}</td>
                    <td class="num">${formatMoney(order.net_cost)}</td>
                </tr>`;
            }).join('');
        } catch (error) {
            tbody.innerHTML = `<tr><td colspan="5" class="abc-error">Ошибка сети: ${escapeHtml(error.message)}</td></tr>`;
        }
    }

    function closeOrdersModal() {
        document.getElementById('courier-orders-modal').classList.remove('active');
    }

    // ================= Справочник курьеров =================

    async function openRefsModal() {
        const modal = document.getElementById('courier-refs-modal');
        const list = document.getElementById('courierRefsList');
        list.innerHTML = '<p class="empty-state">Загрузка...</p>';
        modal.classList.add('active');

        try {
            const response = await fetch('/api/couriers/list?only_active=1', { credentials: 'include' });
            const result = await response.json();

            if (!result.success) {
                list.innerHTML = `<p class="empty-state">Ошибка: ${escapeHtml(result.error)}</p>`;
                return;
            }

            if (!result.data.length) {
                list.innerHTML = '<p class="empty-state">Справочник пуст — сначала обновите данные</p>';
                return;
            }

            list.innerHTML = result.data.map(function(courier) {
                return `
                <label class="module-checkbox">
                    <input type="checkbox" data-flag-courier="${courier.id}" ${courier.is_service ? 'checked' : ''}>
                    <span>${escapeHtml(courier.name)}</span>
                </label>`;
            }).join('');

            list.querySelectorAll('input[data-flag-courier]').forEach(function(input) {
                input.addEventListener('change', function() {
                    setCourierFlag(Number(input.getAttribute('data-flag-courier')), input.checked, input);
                });
            });
        } catch (error) {
            list.innerHTML = `<p class="empty-state">Ошибка сети: ${escapeHtml(error.message)}</p>`;
        }
    }

    async function setCourierFlag(courierId, isService, input) {
        input.disabled = true;
        try {
            const response = await fetch(`/api/couriers/${courierId}/flag`, {
                method: 'POST',
                credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ is_service: isService }),
            });
            const result = await response.json();

            if (!result.success) {
                input.checked = !isService;
                window.BarhatUI.alert('Не удалось сохранить: ' + result.error);
                return;
            }
            loadReport();
        } catch (error) {
            input.checked = !isService;
            window.BarhatUI.alert('Ошибка сети: ' + error.message);
        } finally {
            input.disabled = false;
        }
    }

    function closeRefsModal() {
        document.getElementById('courier-refs-modal').classList.remove('active');
    }

    // ================= Синхронизация =================

    const SYNC_POLL_MS = 2000;
    // Сколько ждём появления нашего прогона, прежде чем решить, что запуститься
    // он не смог (лок держит планировщик или другой админ)
    const START_TIMEOUT_MS = 60000;

    async function runSync(body, busyText) {
        const btn = document.getElementById('syncCourierData');
        const btnText = btn.querySelector('.btn-text');
        const statusEl = document.getElementById('courierSyncStatus');

        setButtonsDisabled(true);
        btnText.textContent = busyText;
        statusEl.classList.remove('hidden');
        statusEl.className = 'update-status';
        statusEl.textContent = 'Запуск синхронизации...';

        try {
            const response = await fetch('/api/couriers/sync', {
                method: 'POST',
                credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body || {}),
            });
            const result = await response.json();

            if (!result.success) {
                finishSync(statusEl, 'Ошибка: ' + result.error, 'error', btnText);
                return;
            }

            // Прогон за неделю укладывается в секунды, и первый опрос статуса мог
            // застать ещё не стартовавший синк: сервер отдал бы прошлый лог, а
            // страница показала бы ложное «готово» на старых данных.
            const prevLogId = result.prev_log_id || 0;
            const startedAt = Date.now();

            const timer = setInterval(async function() {
                try {
                    const status = await fetchSyncStatus();
                    if (!status) return;

                    if ((status.log_id || 0) <= prevLogId) {
                        if (Date.now() - startedAt > START_TIMEOUT_MS) {
                            clearInterval(timer);
                            finishSync(statusEl, 'Синхронизация уже выполняется, данные скоро обновятся', 'success', btnText);
                        }
                        return;
                    }

                    if (status.running) {
                        statusEl.textContent = status.message || 'Синхронизация...';
                        return;
                    }

                    clearInterval(timer);

                    if (status.error) {
                        finishSync(statusEl, 'Ошибка: ' + status.error, 'error', btnText);
                    } else {
                        finishSync(statusEl, status.message, 'success', btnText);
                        citiesLoaded = false;
                        setTimeout(function() { loadCities(); loadReport(); }, 500);
                    }
                    renderFreshness(status);
                } catch (e) {
                    console.error('Ошибка проверки статуса синхронизации:', e);
                }
            }, SYNC_POLL_MS);
        } catch (error) {
            finishSync(statusEl, 'Ошибка: ' + error.message, 'error', btnText);
        }
    }

    function finishSync(statusEl, text, kind, btnText) {
        statusEl.textContent = text;
        statusEl.classList.add(kind);
        setButtonsDisabled(false);
        btnText.textContent = 'Обновить данные';
        setTimeout(function() { statusEl.classList.add('hidden'); }, 5000);
    }

    function setButtonsDisabled(disabled) {
        ADMIN_BUTTONS.forEach(function(id) {
            const btn = document.getElementById(id);
            if (btn) btn.disabled = disabled;
        });
    }

    async function fetchSyncStatus() {
        const response = await fetch('/api/couriers/sync-status', { credentials: 'include' });
        const data = await response.json();
        return data.success ? data.status : null;
    }

    /**
     * Свежесть данных: синхронизация идёт по расписанию сама, поэтому важно
     * не «когда нажимали кнопку», а насколько отчёт отстаёт от CRM.
     */
    function renderFreshness(status) {
        const el = document.getElementById('courierSyncFreshness');
        if (!el || !status) return;

        if (status.configured === false) {
            el.textContent = 'RetailCRM не настроен — данные не загружаются.';
            return;
        }

        const date = window.BarhatTime ? window.BarhatTime.parse(status.last_success) : null;
        const range = status.data_range || {};
        const rangeText = range.min_date && range.max_date
            ? ` Загружены доставки с ${range.min_date} по ${range.max_date}.`
            : '';

        if (!date) {
            el.textContent = 'Данные ещё не загружались. Синхронизация выполняется автоматически каждые 30 минут.' + rangeText;
            return;
        }

        const minutes = Math.max(0, Math.round((Date.now() - date.getTime()) / 60000));
        let ago;
        if (minutes < 1) {
            ago = 'только что';
        } else if (minutes < 60) {
            ago = `${minutes} мин назад`;
        } else if (minutes < 60 * 24) {
            ago = `${Math.floor(minutes / 60)} ч назад`;
        } else {
            ago = window.BarhatTime.formatDateTimeLong(status.last_success);
        }

        el.textContent = `Данные обновлены: ${ago}. Синхронизация выполняется автоматически каждые 30 минут.` + rangeText;
    }

    async function loadFreshness() {
        try {
            renderFreshness(await fetchSyncStatus());
        } catch (e) {
            console.error('Ошибка загрузки статуса синхронизации:', e);
        }
    }

    // ================= Экспорт =================

    function exportCsv() {
        if (!lastReport || !lastReport.couriers.length) {
            window.BarhatUI.alert('Нечего выгружать: отчёт пуст');
            return;
        }

        const filters = currentFilters();
        const lines = [['Курьер', 'Город', 'Доставок', 'Сумма к оплате'].join(';')];
        lastReport.couriers.forEach(function(row) {
            lines.push([
                csvCell(row.courier_name),
                csvCell((row.cities || []).join(', ')),
                row.orders_count,
                // Разделитель дробной части — запятая: Excel с русской локалью
                // иначе принимает 1234.50 за текст
                String(row.total_net_cost).replace('.', ','),
            ].join(';'));
        });
        lines.push(['ИТОГО', '', lastReport.totals.orders_count,
            String(lastReport.totals.total_net_cost).replace('.', ',')].join(';'));

        // BOM — иначе Excel открывает кириллицу кракозябрами
        const blob = new Blob(['﻿' + lines.join('\r\n')], { type: 'text/csv;charset=utf-8;' });
        const link = document.createElement('a');
        link.href = URL.createObjectURL(blob);
        link.download = `оплата-курьерам_${filters.dateFrom}_${filters.dateTo}.csv`;
        link.click();
        URL.revokeObjectURL(link.href);
    }

    function csvCell(value) {
        const text = String(value ?? '');
        return /[";\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
    }

    // ================= Утилиты =================

    function formatMoney(value) {
        return new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 0 }).format(value || 0) + ' ₽';
    }

    function formatCount(value) {
        return new Intl.NumberFormat('ru-RU').format(value || 0);
    }

    function escapeHtml(str) {
        return String(str ?? '').replace(/[&<>"']/g, (c) => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
        }[c]));
    }

    // ================= Инициализация =================

    function init() {
        const applyBtn = document.getElementById('applyCourierFilters');
        const resetBtn = document.getElementById('resetCourierFilters');
        const exportBtn = document.getElementById('exportCourierReport');
        const syncBtn = document.getElementById('syncCourierData');
        const loadPeriodBtn = document.getElementById('loadCourierPeriod');
        const refsBtn = document.getElementById('manageCouriers');

        if (applyBtn) applyBtn.addEventListener('click', loadReport);
        if (resetBtn) {
            resetBtn.addEventListener('click', function() {
                applyDefaultPeriod();
                document.getElementById('courierCity').value = '';
                document.getElementById('courierOnlyOwn').checked = true;
                loadReport();
            });
        }
        if (exportBtn) exportBtn.addEventListener('click', exportCsv);
        if (syncBtn) syncBtn.addEventListener('click', function() { runSync({}, 'Обновление...'); });

        if (loadPeriodBtn) {
            loadPeriodBtn.addEventListener('click', async function() {
                const filters = currentFilters();
                if (!filters.dateFrom || !filters.dateTo) {
                    window.BarhatUI.alert('Укажите период');
                    return;
                }
                // Нативный confirm внутри iframe Пульса молча игнорируется —
                // кнопка выглядела бы сломанной. Диалоги только через BarhatUI.
                const ok = await window.BarhatUI.confirm(
                    `Заказы с ${filters.dateFrom} по ${filters.dateTo} будут перечитаны из RetailCRM. `
                    + 'Долгий период занимает несколько минут.',
                    { title: 'Догрузить период', confirmText: 'Загрузить' }
                );
                if (ok) {
                    runSync({ date_from: filters.dateFrom, date_to: filters.dateTo }, 'Загрузка периода...');
                }
            });
        }

        if (refsBtn) refsBtn.addEventListener('click', openRefsModal);

        const closeOrders = document.getElementById('closeCourierOrdersModal');
        const ordersOverlay = document.getElementById('courier-orders-modal-overlay');
        if (closeOrders) closeOrders.addEventListener('click', closeOrdersModal);
        if (ordersOverlay) ordersOverlay.addEventListener('click', closeOrdersModal);

        const closeRefs = document.getElementById('closeCourierRefsModal');
        const refsOverlay = document.getElementById('courier-refs-modal-overlay');
        if (closeRefs) closeRefs.addEventListener('click', closeRefsModal);
        if (refsOverlay) refsOverlay.addEventListener('click', closeRefsModal);

        // Данные грузим при активации страницы, а не при загрузке дашборда:
        // иначе каждый вход в любой раздел дёргал бы отчёт
        const observer = new MutationObserver(function(mutations) {
            mutations.forEach(function(mutation) {
                if (mutation.target.classList.contains('active') &&
                    mutation.target.getAttribute('data-page') === 'courier_payouts') {
                    if (!document.getElementById('courierDateFrom').value) {
                        applyDefaultPeriod();
                    }
                    loadCities();
                    loadReport();
                    loadFreshness();
                }
            });
        });

        document.querySelectorAll('.page').forEach(function(page) {
            observer.observe(page, { attributes: true, attributeFilter: ['class'] });
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
