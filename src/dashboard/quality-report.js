/**
 * Качество сборки букетов — JavaScript
 * Загрузка данных из API и отображение графиков
 */

(function() {
    'use strict';

    const API_BASE = window.location.origin;
    let salonHistoryChart = null;

    /**
     * Экранирование для вставки в HTML.
     *
     * Кавычка обязательна: названия салонов и имена флористов подставлялись в
     * onclick="showSalonHistory('${name}')" как есть, и апостроф в названии
     * ломал кнопку целиком. Ту же дырку уже чинили в четырёх других модулях
     * дашборда.
     */
    function escapeHtml(str) {
        return String(str ?? '').replace(/[&<>"']/g, (c) => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
        }[c]));
    }

    function notifyError(message) {
        if (window.BarhatUI) {
            window.BarhatUI.toast(message, 'error');
        } else {
            console.error(message);
        }
    }

    // Цветовая палитра бренда
    const COLORS = [
        '#411330', // --barkhat-wine
        '#D19CC2', // --barkhat-pink
        '#E1A4C9', // --barkhat-pink-bright
        '#E4C2DD', // --barkhat-pink-light
        '#B26FA1', // --barkhat-pink-deep
        '#6F6F6F', // --barkhat-gray
    ];

    /**
     * Загрузка данных качества
     */
    async function loadQualityData() {
        const dateFrom = document.getElementById('qualityDateFrom').value;
        const dateTo = document.getElementById('qualityDateTo').value;

        const url = new URL(`${API_BASE}/api/quality`);
        if (dateFrom) url.searchParams.set('date_from', dateFrom);
        if (dateTo) url.searchParams.set('date_to', dateTo);

        console.log(`Loading quality data: ${url}`);

        // Разбор грузится своим запросом и независимо от таблицы: если отчёт
        // отвалится, блок покажет свою ошибку, а не останется в «Загрузка…»
        loadAssessment();

        try {
            const response = await fetch(url);
            const result = await response.json();

            if (!result.success) {
                console.error('API error:', result.error);
                showError(result.error);
                return;
            }

            console.log('Quality data received:', result.data);
            updateQualityDashboard(result.data);
        } catch (error) {
            console.error('Ошибка загрузки данных качества:', error);
            showError('Ошибка загрузки данных: ' + error.message);
        }
    }

    /**
     * Обновление дашборда качества
     */
    function updateQualityDashboard(data) {
        // Обновляем KPI плитки
        document.getElementById('qualityTotalTasks').textContent = data.total_tasks || '-';

        const overallAvg = data.overall_avg ? data.overall_avg.toFixed(1) : '-';
        document.getElementById('qualityOverallAvg').textContent = overallAvg;

        // Считаем средние по категориям
        const cat14Scores = [];
        const cat18Scores = [];

        Object.values(data.salons || {}).forEach(s => {
            if (s.cat14.count > 0) cat14Scores.push(s.cat14.avg_score);
            if (s.cat18.count > 0) cat18Scores.push(s.cat18.avg_score);
        });

        const cat14Avg = cat14Scores.length > 0
            ? (cat14Scores.reduce((a, b) => a + b, 0) / cat14Scores.length).toFixed(1)
            : '-';
        const cat18Avg = cat18Scores.length > 0
            ? (cat18Scores.reduce((a, b) => a + b, 0) / cat18Scores.length).toFixed(1)
            : '-';

        document.getElementById('qualityCat14Avg').textContent = cat14Avg;
        document.getElementById('qualityCat18Avg').textContent = cat18Avg;

        // Обновляем таблицу салонов
        updateSalonsTable(data.salons);
    }

    /**
     * Обновление таблицы салонов
     */
    function updateSalonsTable(salons) {
        const tbody = document.getElementById('qualitySalonsTable');
        if (!tbody) return;

        const sorted = Object.entries(salons)
            .sort((a, b) => b[1].total.count - a[1].total.count);

        // Название салона уходит в data-атрибут, а обработчик висит на tbody:
        // так имя не попадает в исполняемый код и кавычки в нём безопасны
        tbody.innerHTML = sorted.map(([name, stats]) => {
            const cat14Text = stats.cat14.count > 0
                ? `${stats.cat14.avg_score.toFixed(1)} (${stats.cat14.count})`
                : '-';
            const cat18Text = stats.cat18.count > 0
                ? `${stats.cat18.avg_score.toFixed(1)} (${stats.cat18.count})`
                : '-';
            const salonAttr = escapeHtml(name);

            return `
                <tr>
                    <td><strong>${escapeHtml(name)}</strong></td>
                    <td>${cat14Text}</td>
                    <td>${cat18Text}</td>
                    <td>${stats.total.count}</td>
                    <td>
                        <button class="btn btn-secondary btn-sm" data-salon-action="history" data-salon="${salonAttr}">
                            Динамика
                        </button>
                    </td>
                    <td>
                        <button class="btn btn-accent btn-sm" data-salon-action="order-types" data-salon="${salonAttr}">
                            По видам
                        </button>
                    </td>
                    <td>
                        <button class="btn btn-primary btn-sm" data-salon-action="florists" data-salon="${salonAttr}">
                            По флористам
                        </button>
                    </td>
                </tr>
            `;
        }).join('');
    }

    /**
     * Разбор по салонам: что проседает, что получается, куда движется.
     *
     * Отдельный запрос, а не часть /api/quality: таблица салонов должна
     * появиться сразу, а разбор считается по витрине критериев и может
     * подождать лишние миллисекунды.
     */
    async function loadAssessment() {
        const box = document.getElementById('qualityAssessment');
        if (!box) return;

        const dateFrom = document.getElementById('qualityDateFrom').value;
        const dateTo = document.getElementById('qualityDateTo').value;

        const url = new URL(`${API_BASE}/api/quality/salon-assessment`);
        if (dateFrom) url.searchParams.set('date_from', dateFrom);
        if (dateTo) url.searchParams.set('date_to', dateTo);

        try {
            const response = await fetch(url);
            const result = await response.json();

            if (!result.success) {
                box.innerHTML = `<p class="bx-assess__caption">Ошибка: ${escapeHtml(result.error)}</p>`;
                return;
            }

            renderAssessment(result.data);
        } catch (error) {
            box.innerHTML = `<p class="bx-assess__caption">Ошибка сети: ${escapeHtml(error.message)}</p>`;
        }
    }

    function renderDelta(delta) {
        if (delta === null || delta === undefined) return '';

        // Тренды по DESIGN-SPEC: стрелки ▲ ▼, зелёный рост, красное падение
        const abs = Math.abs(delta).toFixed(1).replace('.', ',');
        if (Math.abs(delta) < 1) {
            return `<span class="bx-assess__delta bx-assess__delta--flat">без изменений</span>`;
        }
        const dir = delta > 0 ? 'up' : 'down';
        const arrow = delta > 0 ? '▲' : '▼';
        return `<span class="bx-assess__delta bx-assess__delta--${dir}">${arrow} ${abs} п.п.</span>`;
    }

    function renderAssessment(data) {
        const box = document.getElementById('qualityAssessment');
        if (!box) return;

        const salons = data.salons || [];

        if (salons.length === 0) {
            box.innerHTML = '<p class="bx-assess__caption">За выбранный период оценок нет.</p>';
            return;
        }

        const items = salons.map(s => {
            let modifier = '';
            if (!s.reliable) modifier = 'bx-assess__item--weak';
            else if (s.problems.length === 0) modifier = 'bx-assess__item--good';
            else if (s.problems.some(p => p.kind === 'salon')) modifier = 'bx-assess__item--bad';

            const tags = []
                .concat(s.strengths.map(c =>
                    `<span class="bx-badge bx-badge--good">${escapeHtml(c.name)} · ${fmt(c.percent)}%</span>`))
                .concat(s.problems.map(c =>
                    `<span class="bx-badge bx-badge--bad">${escapeHtml(c.name)} · ${fmt(c.percent)}%</span>`))
                .join('');

            return `
                <div class="bx-assess__item ${modifier}">
                    <div class="bx-assess__head">
                        <span class="bx-assess__salon">${escapeHtml(s.salon)}</span>
                        <span class="bx-assess__value">${fmt(s.percent)}%</span>
                        ${renderDelta(s.delta)}
                        <span class="bx-assess__count">${s.count} ${pluralScores(s.count)}</span>
                    </div>
                    <p class="bx-assess__text">${escapeHtml(s.summary)}</p>
                    <div class="bx-assess__tags">${tags}</div>
                </div>
            `;
        }).join('');

        const network = data.network || {};
        const weakest = (network.criteria || []).slice(0, 3)
            .map(c => `${escapeHtml(c.name)} — ${fmt(c.percent)}%`)
            .join(', ');

        const networkBlock = weakest
            ? `<div class="bx-assess__network">
                   По сети в целом ${fmt(network.percent)}% от максимума.
                   Самые слабые критерии: ${weakest}.
               </div>`
            : '';

        box.innerHTML = items + networkBlock;
    }

    function fmt(value) {
        return Number(value).toFixed(1).replace('.', ',');
    }

    function pluralScores(n) {
        if (n % 10 === 1 && n % 100 !== 11) return 'оценка';
        if (n % 10 >= 2 && n % 10 <= 4 && !(n % 100 >= 12 && n % 100 <= 14)) return 'оценки';
        return 'оценок';
    }

    /**
     * Один обработчик на всю таблицу вместо инлайновых onclick
     */
    function onSalonsTableClick(event) {
        const button = event.target.closest('[data-salon-action]');
        if (!button) return;

        const salon = button.dataset.salon;
        const action = button.dataset.salonAction;

        if (action === 'history') showSalonHistory(salon);
        else if (action === 'order-types') showSalonOrderTypes(salon);
        else if (action === 'florists') showSalonFlorists(salon);
    }

    /**
     * Показать историю салона
     */
    async function showSalonHistory(salonName) {
        const modal = document.getElementById('salonHistoryModal');
        const title = document.getElementById('salonModalTitle');

        title.textContent = `Динамика: ${salonName}`;
        modal.classList.add('active');

        try {
            const url = `${API_BASE}/api/quality/salon-history?salon=${encodeURIComponent(salonName)}&months=6`;
            console.log(`Fetching salon history: ${url}`);
            const response = await fetch(url);
            const result = await response.json();

            if (!result.success) {
                console.error('History API error:', result.error);
                notifyError(`Ошибка загрузки данных: ${result.error}`);
                return;
            }

            console.log('History data:', result.data);
            updateSalonHistoryChart(result.data, salonName);
        } catch (error) {
            console.error('History fetch error:', error);
            notifyError(`Ошибка сети: ${error.message}`);
        }
    }

    /**
     * Показать виды заказа для салона
     */
    async function showSalonOrderTypes(salonName) {
        const modal = document.getElementById('salonOrderTypesModal');
        const title = document.getElementById('orderTypesModalTitle');
        const tbody = document.getElementById('orderTypesTableBody');

        title.textContent = `Качество по видам заказа: ${salonName}`;
        modal.classList.add('active');

        // Получаем текущие фильтры дат
        const dateFrom = document.getElementById('qualityDateFrom').value;
        const dateTo = document.getElementById('qualityDateTo').value;

        try {
            const url = new URL(`${API_BASE}/api/quality/salon-order-types`);
            url.searchParams.set('salon', salonName);
            if (dateFrom) url.searchParams.set('date_from', dateFrom);
            if (dateTo) url.searchParams.set('date_to', dateTo);

            console.log(`Fetching order types: ${url}`);
            const response = await fetch(url);
            const result = await response.json();

            if (!result.success) {
                console.error('Order types API error:', result.error);
                tbody.innerHTML = `<tr><td colspan="4" style="text-align: center; color: var(--barkhat-wine);">Ошибка: ${escapeHtml(result.error)}</td></tr>`;
                return;
            }

            console.log('Order types data:', result.data);
            updateOrderTypesTable(result.data.order_types);
        } catch (error) {
            console.error('Order types fetch error:', error);
            tbody.innerHTML = `<tr><td colspan="4" style="text-align: center; color: var(--barkhat-wine);">Ошибка сети: ${escapeHtml(error.message)}</td></tr>`;
        }
    }

    /**
     * Показать флористов салона
     */
    async function showSalonFlorists(salonName) {
        const modal = document.getElementById('salonFloristsModal');
        const title = document.getElementById('floristsModalTitle');
        const tbody = document.getElementById('floristsTableBody');

        title.textContent = `Качество по флористам: ${salonName}`;
        modal.classList.add('active');

        // Получаем текущие фильтры дат
        const dateFrom = document.getElementById('qualityDateFrom').value;
        const dateTo = document.getElementById('qualityDateTo').value;

        try {
            const url = new URL(`${API_BASE}/api/quality/salon-florists`);
            url.searchParams.set('salon', salonName);
            if (dateFrom) url.searchParams.set('date_from', dateFrom);
            if (dateTo) url.searchParams.set('date_to', dateTo);

            console.log(`Fetching florists: ${url}`);
            const response = await fetch(url);
            const result = await response.json();

            if (!result.success) {
                console.error('Florists API error:', result.error);
                tbody.innerHTML = `<tr><td colspan="3" style="text-align: center; color: var(--barkhat-wine);">Ошибка: ${escapeHtml(result.error)}</td></tr>`;
                return;
            }

            console.log('Florists data:', result.data);
            updateFloristsTable(result.data.florists);
        } catch (error) {
            console.error('Florists fetch error:', error);
            tbody.innerHTML = `<tr><td colspan="3" style="text-align: center; color: var(--barkhat-wine);">Ошибка сети: ${escapeHtml(error.message)}</td></tr>`;
        }
    }

    /**
     * Обновить таблицу видов заказа
     */
    function updateOrderTypesTable(orderTypes) {
        const tbody = document.getElementById('orderTypesTableBody');
        if (!tbody) return;

        tbody.innerHTML = orderTypes.map(item => {
            const avgScore = item.count > 0 ? item.avg_score.toFixed(1) : '—';
            const count = item.count > 0 ? item.count : '—';

            // Цвет для строк с данными
            const rowStyle = item.count > 0 ? '' : 'style="color: var(--barkhat-gray);"';

            return `
                <tr ${rowStyle}>
                    <td>${escapeHtml(item.order_type)}</td>
                    <td>${avgScore}</td>
                    <td>${item.max_score}</td>
                    <td>${count}</td>
                </tr>
            `;
        }).join('');
    }

    /**
     * Обновить таблицу флористов
     */
    function updateFloristsTable(florists) {
        const tbody = document.getElementById('floristsTableBody');
        if (!tbody) return;

        if (florists.length === 0) {
            tbody.innerHTML = `<tr><td colspan="3" style="text-align: center; color: var(--barkhat-gray);">Нет данных</td></tr>`;
            return;
        }

        tbody.innerHTML = florists.map(f => {
            // Формат: средний балл / кол-во оценок
            const cat14Text = f.cat14.count > 0
                ? `${f.cat14.avg_score.toFixed(1)} / ${f.cat14.count}`
                : '— / —';
            const cat18Text = f.cat18.count > 0
                ? `${f.cat18.avg_score.toFixed(1)} / ${f.cat18.count}`
                : '— / —';

            return `
                <tr>
                    <td><strong>${escapeHtml(f.florist)}</strong></td>
                    <td>${cat14Text}</td>
                    <td>${cat18Text}</td>
                </tr>
            `;
        }).join('');
    }

    /**
     * Закрыть модальное окно видов заказа
     */
    function closeOrderTypesModal() {
        const modal = document.getElementById('salonOrderTypesModal');
        modal.classList.remove('active');
    }

    /**
     * Закрыть модальное окно флористов
     */
    function closeFloristsModal() {
        const modal = document.getElementById('salonFloristsModal');
        modal.classList.remove('active');
    }

    /**
     * Закрыть модальное окно
     */
    function closeSalonModal() {
        const modal = document.getElementById('salonHistoryModal');
        modal.classList.remove('active');
    }

    /**
     * Обновление графика истории салона
     */
    function updateSalonHistoryChart(history, salonName) {
        const months = Object.keys(history).sort();

        if (months.length === 0) {
            // Показываем сообщение, если нет данных
            const tbody = document.getElementById('qualitySalonsTable');
            if (salonHistoryChart) salonHistoryChart.destroy();
            console.warn(`No history data for salon: ${salonName}`);
            return;
        }

        const cat14Data = months.map(m => {
            const data = history[m];
            return data && data.cat14_count > 0 ? data.cat14_avg : null;
        });
        const cat18Data = months.map(m => {
            const data = history[m];
            return data && data.cat18_count > 0 ? data.cat18_avg : null;
        });
        const totalData = months.map(m => {
            const data = history[m];
            return data && data.total_count > 0 ? data.total_avg : null;
        });

        if (salonHistoryChart) salonHistoryChart.destroy();

        const ctx = document.getElementById('salonHistoryChart');
        if (!ctx) return;

        salonHistoryChart = new Chart(ctx, {
            type: 'line',
            data: {
                labels: months,
                datasets: [
                    {
                        label: '14 баллов',
                        data: cat14Data,
                        borderColor: COLORS[0],
                        backgroundColor: COLORS[0] + '20',
                        tension: 0.3,
                        fill: false,
                        pointRadius: 6,
                        pointHoverRadius: 8
                    },
                    {
                        label: '18 баллов',
                        data: cat18Data,
                        borderColor: COLORS[1],
                        backgroundColor: COLORS[1] + '20',
                        tension: 0.3,
                        fill: false,
                        pointRadius: 6,
                        pointHoverRadius: 8
                    },
                    {
                        label: 'Средний',
                        data: totalData,
                        borderColor: COLORS[4],
                        backgroundColor: COLORS[4] + '20',
                        tension: 0.3,
                        fill: false,
                        pointRadius: 6,
                        pointHoverRadius: 8,
                        borderDash: [5, 5]
                    }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: {
                        display: true,
                        position: 'top',
                        labels: {
                            color: '#3C3C3C',
                            font: {
                                family: 'Inter'
                            },
                            usePointStyle: true,
                            padding: 16
                        }
                    },
                    tooltip: {
                        backgroundColor: '#411330',
                        titleColor: '#fff',
                        bodyColor: '#fff',
                        padding: 12,
                        displayColors: true
                    }
                },
                scales: {
                    x: {
                        grid: {
                            display: false
                        },
                        ticks: {
                            color: '#6F6F6F',
                            font: {
                                family: 'Inter'
                            }
                        }
                    },
                    y: {
                        beginAtZero: false,
                        min: 10,
                        max: 18,
                        grid: {
                            color: '#E4C2DD'
                        },
                        ticks: {
                            color: '#6F6F6F',
                            font: {
                                family: 'Inter'
                            }
                        }
                    }
                }
            }
        });
    }

    /**
     * Показать ошибку
     */
    function showError(message) {
        console.error('Quality report error:', message);
    }

    /**
     * Обновление данных из Pyrus.
     *
     * Период берётся из выпадающего списка: раньше кнопка всегда просила ровно
     * 7 дней, и догрузить историю можно было только запросом к
     * /api/pyrus/backfill руками — из интерфейса она не пополнялась.
     */
    async function updatePyrusData() {
        const btn = document.getElementById('updatePyrusData');
        const btnText = btn.querySelector('.btn-text');
        const spinner = btn.querySelector('.btn-spinner');
        const statusEl = document.getElementById('updateStatus');
        const periodEl = document.getElementById('qualitySyncPeriod');
        const days = parseInt(periodEl && periodEl.value, 10) || 7;

        const idleLabel = btnText.textContent;
        let checkInterval = null;

        function finish() {
            if (checkInterval) clearInterval(checkInterval);
            btn.disabled = false;
            btnText.textContent = idleLabel;
            spinner.classList.add('hidden');
        }

        btn.disabled = true;
        btnText.textContent = 'Обновление...';
        spinner.classList.remove('hidden');
        statusEl.classList.remove('hidden');
        statusEl.className = 'update-status';
        statusEl.textContent = 'Запуск обновления...';

        try {
            const response = await fetch(`${API_BASE}/api/pyrus/update`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ days })
            });

            const result = await response.json();

            if (!result.success) {
                statusEl.textContent = 'Ошибка: ' + result.error;
                statusEl.classList.add('error');
                finish();
                return;
            }

            // Проверяем статус каждые 2 секунды
            checkInterval = setInterval(async () => {
                try {
                    const statusRes = await fetch(`${API_BASE}/api/pyrus/update-status`);
                    const statusData = await statusRes.json();

                    if (!statusData.success) return;

                    const status = statusData.status;

                    if (status.running) {
                        statusEl.textContent = status.message || 'Обновление...';
                        return;
                    }

                    finish();

                    if (status.error) {
                        statusEl.textContent = 'Ошибка: ' + status.error;
                        statusEl.classList.add('error');
                    } else {
                        statusEl.textContent = status.message;
                        statusEl.classList.add('success');
                        setTimeout(loadQualityData, 500);
                    }

                    setTimeout(() => statusEl.classList.add('hidden'), 5000);
                } catch (e) {
                    console.error('Status check error:', e);
                }
            }, 2000);

        } catch (error) {
            console.error('Update error:', error);
            statusEl.textContent = 'Ошибка: ' + error.message;
            statusEl.classList.add('error');
            finish();
        }
    }

    /**
     * Инициализация при загрузке страницы
     */
    function init() {
        // Кнопки фильтров
        const applyBtn = document.getElementById('applyQualityFilters');
        const resetBtn = document.getElementById('resetQualityFilters');
        const updateBtn = document.getElementById('updatePyrusData');

        if (applyBtn) {
            applyBtn.addEventListener('click', loadQualityData);
        }

        if (resetBtn) {
            resetBtn.addEventListener('click', function() {
                document.getElementById('qualityDateFrom').value = '';
                document.getElementById('qualityDateTo').value = '';
                loadQualityData();
            });
        }

        if (updateBtn) {
            updateBtn.addEventListener('click', updatePyrusData);
        }

        // Кнопки в строках таблицы салонов — через делегирование, потому что
        // строки перерисовываются на каждый запрос
        const salonsTable = document.getElementById('qualitySalonsTable');
        if (salonsTable) {
            salonsTable.addEventListener('click', onSalonsTableClick);
        }

        // Кнопка закрытия модального окна
        const closeBtn = document.getElementById('closeSalonModalBtn');
        const overlay = document.getElementById('salonModalOverlay');

        if (closeBtn) {
            closeBtn.addEventListener('click', closeSalonModal);
        }
        if (overlay) {
            overlay.addEventListener('click', closeSalonModal);
        }

        // Кнопка закрытия модального окна видов заказа
        const closeOrderTypesBtn = document.getElementById('closeOrderTypesModalBtn');
        const orderTypesOverlay = document.getElementById('orderTypesModalOverlay');

        if (closeOrderTypesBtn) {
            closeOrderTypesBtn.addEventListener('click', closeOrderTypesModal);
        }
        if (orderTypesOverlay) {
            orderTypesOverlay.addEventListener('click', closeOrderTypesModal);
        }

        // Кнопка закрытия модального окна флористов
        const closeFloristsBtn = document.getElementById('closeFloristsModalBtn');
        const floristsOverlay = document.getElementById('floristsModalOverlay');

        if (closeFloristsBtn) {
            closeFloristsBtn.addEventListener('click', closeFloristsModal);
        }
        if (floristsOverlay) {
            floristsOverlay.addEventListener('click', closeFloristsModal);
        }

        // Загружаем данные при открытии страницы качества
        const observer = new MutationObserver(function(mutations) {
            mutations.forEach(function(mutation) {
                if (mutation.target.classList.contains('active') &&
                    mutation.target.getAttribute('data-page') === 'quality') {
                    loadQualityData();
                }
            });
        });

        // Начинаем наблюдение за страницами
        const pages = document.querySelectorAll('.page');
        pages.forEach(function(page) {
            observer.observe(page, { attributes: true, attributeFilter: ['class'] });
        });
    }

    // Запускаем инициализацию
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

})();
