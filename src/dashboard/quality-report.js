/**
 * Качество сборки букетов — JavaScript
 * Загрузка данных из API и отображение графиков
 */

(function() {
    'use strict';

    const API_BASE = 'http://127.0.0.1:5000';
    let salonHistoryChart = null;

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

        tbody.innerHTML = sorted.map(([name, stats]) => {
            const cat14Text = stats.cat14.count > 0
                ? `${stats.cat14.avg_score.toFixed(1)} (${stats.cat14.count})`
                : '-';
            const cat18Text = stats.cat18.count > 0
                ? `${stats.cat18.avg_score.toFixed(1)} (${stats.cat18.count})`
                : '-';

            return `
                <tr>
                    <td><strong>${name}</strong></td>
                    <td>${cat14Text}</td>
                    <td>${cat18Text}</td>
                    <td>${stats.total.count}</td>
                    <td>
                        <button class="btn btn-secondary btn-sm" onclick="showSalonHistory('${name}')">
                            Динамика
                        </button>
                    </td>
                    <td>
                        <button class="btn btn-accent btn-sm" onclick="showSalonOrderTypes('${name}')">
                            По видам
                        </button>
                    </td>
                    <td>
                        <button class="btn btn-primary btn-sm" onclick="showSalonFlorists('${name}')">
                            По флористам
                        </button>
                    </td>
                </tr>
            `;
        }).join('');
    }

    /**
     * Показать историю салона
     */
    window.showSalonHistory = async function(salonName) {
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
                alert(`Ошибка загрузки данных: ${result.error}`);
                return;
            }

            console.log('History data:', result.data);
            updateSalonHistoryChart(result.data, salonName);
        } catch (error) {
            console.error('History fetch error:', error);
            alert(`Ошибка сети: ${error.message}`);
        }
    };

    /**
     * Показать виды заказа для салона
     */
    window.showSalonOrderTypes = async function(salonName) {
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
                tbody.innerHTML = `<tr><td colspan="4" style="text-align: center; color: var(--barkhat-wine);">Ошибка: ${result.error}</td></tr>`;
                return;
            }

            console.log('Order types data:', result.data);
            updateOrderTypesTable(result.data.order_types);
        } catch (error) {
            console.error('Order types fetch error:', error);
            tbody.innerHTML = `<tr><td colspan="4" style="text-align: center; color: var(--barkhat-wine);">Ошибка сети: ${error.message}</td></tr>`;
        }
    };

    /**
     * Показать флористов салона
     */
    window.showSalonFlorists = async function(salonName) {
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
                tbody.innerHTML = `<tr><td colspan="3" style="text-align: center; color: var(--barkhat-wine);">Ошибка: ${result.error}</td></tr>`;
                return;
            }

            console.log('Florists data:', result.data);
            updateFloristsTable(result.data.florists);
        } catch (error) {
            console.error('Florists fetch error:', error);
            tbody.innerHTML = `<tr><td colspan="3" style="text-align: center; color: var(--barkhat-wine);">Ошибка сети: ${error.message}</td></tr>`;
        }
    };

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
                    <td>${item.order_type}</td>
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
                    <td><strong>${f.florist}</strong></td>
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
     * Обновление данных из Pyrus
     */
    async function updatePyrusData() {
        const btn = document.getElementById('updatePyrusData');
        const btnText = btn.querySelector('.btn-text');
        const spinner = btn.querySelector('.btn-spinner');
        const statusEl = document.getElementById('updateStatus');

        btn.disabled = true;
        btnText.textContent = 'Обновление...';
        spinner.classList.remove('hidden');
        statusEl.classList.remove('hidden');
        statusEl.className = 'update-status';
        statusEl.textContent = '⏳ Запуск обновления...';

        try {
            // Запускаем обновление
            const response = await fetch(`${API_BASE}/api/pyrus/update`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ days: 7 })
            });

            const result = await response.json();

            if (!result.success) {
                statusEl.textContent = '❌ Ошибка: ' + result.error;
                statusEl.classList.add('error');
                return;
            }

            // Проверяем статус каждые 2 секунды
            const checkInterval = setInterval(async () => {
                try {
                    const statusRes = await fetch(`${API_BASE}/api/pyrus/update-status`);
                    const statusData = await statusRes.json();

                    if (statusData.success) {
                        const status = statusData.status;

                        if (status.running) {
                            statusEl.textContent = `⏳ ${status.message || 'Обновление...'}`;
                        } else {
                            clearInterval(checkInterval);

                            if (status.error) {
                                statusEl.textContent = '❌ Ошибка: ' + status.error;
                                statusEl.classList.add('error');
                            } else {
                                statusEl.textContent = '✅ ' + status.message;
                                statusEl.classList.add('success');

                                // Перезагружаем данные
                                setTimeout(() => {
                                    loadQualityData();
                                }, 500);
                            }

                            btn.disabled = false;
                            btnText.textContent = '🔄 Обновить данные';
                            spinner.classList.add('hidden');

                            // Скрываем статус через 5 секунд
                            setTimeout(() => {
                                statusEl.classList.add('hidden');
                            }, 5000);
                        }
                    }
                } catch (e) {
                    console.error('Status check error:', e);
                }
            }, 2000);

        } catch (error) {
            console.error('Update error:', error);
            statusEl.textContent = '❌ Ошибка: ' + error.message;
            statusEl.classList.add('error');
            btn.disabled = false;
            btnText.textContent = '🔄 Обновить данные';
            spinner.classList.add('hidden');
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
