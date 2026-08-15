/**
 * ABC-анализ товаров — JavaScript
 * Загрузка данных из /api/moysklad/* и отображение таблицы
 */

(function() {
    'use strict';

    let channelsLoaded = false;
    let isAdmin = false;

    document.addEventListener('userRoleChanged', function(e) {
        isAdmin = e.detail && e.detail.role === 'admin';
        const syncBtn = document.getElementById('syncMoyskladData');
        if (syncBtn) {
            syncBtn.style.display = isAdmin ? '' : 'none';
        }
    });

    /**
     * Загрузка справочника каналов продаж (один раз)
     */
    async function loadChannels() {
        if (channelsLoaded) return;

        try {
            const response = await fetch('/api/moysklad/sales_channels', { credentials: 'include' });
            const result = await response.json();

            if (!result.success) {
                console.error('Ошибка загрузки каналов продаж:', result.error);
                return;
            }

            const select = document.getElementById('abcChannel');
            if (!select) return;

            result.data.forEach(channel => {
                const option = document.createElement('option');
                option.value = channel.id;
                option.textContent = channel.name;
                select.appendChild(option);
            });

            channelsLoaded = true;
        } catch (error) {
            console.error('Ошибка загрузки каналов продаж:', error);
        }
    }

    /**
     * Загрузка данных ABC-анализа
     */
    async function loadAbcData() {
        const dateFrom = document.getElementById('abcDateFrom').value;
        const dateTo = document.getElementById('abcDateTo').value;
        const channelId = document.getElementById('abcChannel').value;

        const url = new URL('/api/moysklad/abc-analysis', window.location.origin);
        if (dateFrom) url.searchParams.set('date_from', dateFrom);
        if (dateTo) url.searchParams.set('date_to', dateTo);
        if (channelId) url.searchParams.set('channel_id', channelId);

        const tbody = document.getElementById('abcTableBody');

        try {
            const response = await fetch(url, { credentials: 'include' });
            const result = await response.json();

            if (!result.success) {
                console.error('Ошибка ABC-анализа:', result.error);
                if (tbody) {
                    tbody.innerHTML = `<tr><td colspan="6" style="text-align: center; color: var(--barkhat-wine);">Ошибка: ${result.error}</td></tr>`;
                }
                return;
            }

            renderAbcTable(result.data);
            renderAbcKpis(result.data);
        } catch (error) {
            console.error('Ошибка загрузки ABC-анализа:', error);
            if (tbody) {
                tbody.innerHTML = `<tr><td colspan="6" style="text-align: center; color: var(--barkhat-wine);">Ошибка сети: ${error.message}</td></tr>`;
            }
        }
    }

    /**
     * Отрисовка таблицы товаров
     */
    function renderAbcTable(rows) {
        const tbody = document.getElementById('abcTableBody');
        if (!tbody) return;

        if (rows.length === 0) {
            tbody.innerHTML = `<tr><td colspan="6" style="text-align: center; color: var(--barkhat-gray); padding: 20px;">Нет данных за выбранный период</td></tr>`;
            return;
        }

        const classColors = { A: '#2E7D32', B: '#B26FA1', C: '#6F6F6F' };

        tbody.innerHTML = rows.map((row, index) => `
            <tr>
                <td>${index + 1}</td>
                <td>${row.assortment_name || '—'}</td>
                <td>${formatMoney(row.revenue)}</td>
                <td>${row.share_pct}%</td>
                <td>${row.cumulative_pct}%</td>
                <td><span class="status-badge" style="background: ${classColors[row.abc_class]}20; color: ${classColors[row.abc_class]};">${row.abc_class}</span></td>
            </tr>
        `).join('');
    }

    /**
     * Обновление KPI плиток
     */
    function renderAbcKpis(rows) {
        const totalRevenue = rows.reduce((sum, r) => sum + r.revenue, 0);
        const classACount = rows.filter(r => r.abc_class === 'A').length;
        const classBCCount = rows.filter(r => r.abc_class === 'B' || r.abc_class === 'C').length;

        document.getElementById('abcTotalItems').textContent = rows.length;
        document.getElementById('abcTotalRevenue').textContent = formatMoney(totalRevenue);
        document.getElementById('abcClassACount').textContent = classACount;
        document.getElementById('abcClassBCCount').textContent = classBCCount;
    }

    function formatMoney(value) {
        return new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 0 }).format(value || 0) + ' ₽';
    }

    /**
     * Синхронизация данных из МойСклад
     */
    async function syncMoyskladData() {
        const btn = document.getElementById('syncMoyskladData');
        const btnText = btn.querySelector('.btn-text');
        const spinner = btn.querySelector('.btn-spinner');
        const statusEl = document.getElementById('abcSyncStatus');

        btn.disabled = true;
        btnText.textContent = 'Синхронизация...';
        spinner.classList.remove('hidden');
        statusEl.classList.remove('hidden');
        statusEl.className = 'update-status';
        statusEl.textContent = '⏳ Запуск синхронизации...';

        try {
            const response = await fetch('/api/moysklad/sync', {
                method: 'POST',
                credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({})
            });

            const result = await response.json();

            if (!result.success) {
                statusEl.textContent = '❌ Ошибка: ' + result.error;
                statusEl.classList.add('error');
                resetSyncButton(btn, btnText, spinner);
                return;
            }

            const checkInterval = setInterval(async () => {
                try {
                    const statusRes = await fetch('/api/moysklad/sync-status', { credentials: 'include' });
                    const statusData = await statusRes.json();

                    if (statusData.success) {
                        const status = statusData.status;

                        if (status.running) {
                            statusEl.textContent = `⏳ ${status.message || 'Синхронизация...'}`;
                        } else {
                            clearInterval(checkInterval);

                            if (status.error) {
                                statusEl.textContent = '❌ Ошибка: ' + status.error;
                                statusEl.classList.add('error');
                            } else {
                                statusEl.textContent = '✅ ' + status.message;
                                statusEl.classList.add('success');
                                setTimeout(loadAbcData, 500);
                            }

                            resetSyncButton(btn, btnText, spinner);
                            setTimeout(() => statusEl.classList.add('hidden'), 5000);
                        }
                    }
                } catch (e) {
                    console.error('Ошибка проверки статуса синхронизации:', e);
                }
            }, 2000);
        } catch (error) {
            statusEl.textContent = '❌ Ошибка: ' + error.message;
            statusEl.classList.add('error');
            resetSyncButton(btn, btnText, spinner);
        }
    }

    function resetSyncButton(btn, btnText, spinner) {
        btn.disabled = false;
        btnText.textContent = '🔄 Синхронизировать с МойСклад';
        spinner.classList.add('hidden');
    }

    /**
     * Инициализация
     */
    function init() {
        const applyBtn = document.getElementById('applyAbcFilters');
        const resetBtn = document.getElementById('resetAbcFilters');
        const syncBtn = document.getElementById('syncMoyskladData');

        if (applyBtn) applyBtn.addEventListener('click', loadAbcData);
        if (resetBtn) {
            resetBtn.addEventListener('click', function() {
                document.getElementById('abcDateFrom').value = '';
                document.getElementById('abcDateTo').value = '';
                document.getElementById('abcChannel').value = '';
                loadAbcData();
            });
        }
        if (syncBtn) syncBtn.addEventListener('click', syncMoyskladData);

        // Загружаем данные при активации страницы ABC-анализа
        const observer = new MutationObserver(function(mutations) {
            mutations.forEach(function(mutation) {
                if (mutation.target.classList.contains('active') &&
                    mutation.target.getAttribute('data-page') === 'abc_analysis') {
                    loadChannels();
                    loadAbcData();
                }
            });
        });

        const pages = document.querySelectorAll('.page');
        pages.forEach(function(page) {
            observer.observe(page, { attributes: true, attributeFilter: ['class'] });
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

})();
