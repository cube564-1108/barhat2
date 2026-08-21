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
        ['syncMoyskladData', 'fullResyncMoysklad'].forEach(function(id) {
            const btn = document.getElementById(id);
            if (btn) btn.style.display = isAdmin ? '' : 'none';
        });
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

    // Период по умолчанию — последний месяц. Пустой период означал бы выборку по
    // всей базе (46К+ позиций заказов, GROUP BY без границ по дате) на каждое
    // открытие страницы; месяц — и осмысленный отчётный горизонт, и лёгкий запрос.
    const DEFAULT_PERIOD_DAYS = 30;

    function applyDefaultPeriod() {
        const to = new Date();
        const from = new Date();
        from.setDate(from.getDate() - DEFAULT_PERIOD_DAYS);

        const fromEl = document.getElementById('abcDateFrom');
        const toEl = document.getElementById('abcDateTo');
        if (fromEl) fromEl.value = toIsoDate(from);
        if (toEl) toEl.value = toIsoDate(to);
    }

    function toIsoDate(date) {
        const month = String(date.getMonth() + 1).padStart(2, '0');
        const day = String(date.getDate()).padStart(2, '0');
        return `${date.getFullYear()}-${month}-${day}`;
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

        showTableMessage('Загрузка данных...', 'abc-empty');

        try {
            const response = await fetch(url, { credentials: 'include' });
            const result = await response.json();

            if (!result.success) {
                console.error('Ошибка ABC-анализа:', result.error);
                showTableMessage('Ошибка: ' + result.error, 'abc-error');
                return;
            }

            renderAbcTable(result.data);
            renderAbcKpis(result.data);
        } catch (error) {
            console.error('Ошибка загрузки ABC-анализа:', error);
            showTableMessage('Ошибка сети: ' + error.message, 'abc-error');
        }
    }

    /**
     * Сообщение вместо таблицы (загрузка / пусто / ошибка)
     */
    function showTableMessage(text, className) {
        const tbody = document.getElementById('abcTableBody');
        if (!tbody) return;
        tbody.innerHTML = `<tr><td colspan="8" class="${className}">${escapeHtml(text)}</td></tr>`;
    }

    /**
     * Отрисовка таблицы товаров
     */
    function renderAbcTable(rows) {
        const tbody = document.getElementById('abcTableBody');
        if (!tbody) return;

        if (rows.length === 0) {
            showTableMessage('Нет данных за выбранный период', 'abc-empty');
            return;
        }

        // Имя товара приходит из МойСклад и попадает в innerHTML — экранируем.
        // Класс A/B/C сводим к одному из трёх известных значений, чтобы в имя
        // CSS-класса не подставилось произвольное содержимое ответа.
        tbody.innerHTML = rows.map((row, index) => {
            const cls = ['A', 'B', 'C'].includes(row.abc_class) ? row.abc_class : 'C';
            return `
            <tr>
                <td class="num">${index + 1}</td>
                <td>${escapeHtml(row.assortment_name || '—')}</td>
                <td class="num">${formatCount(row.orders_count)}</td>
                <td class="num">${formatCount(row.quantity)}</td>
                <td class="num">${formatMoney(row.revenue)}</td>
                <td class="num">${formatPercent(row.share_pct)}</td>
                <td class="num">${formatPercent(row.cumulative_pct)}</td>
                <td><span class="abc-badge abc-badge--${cls.toLowerCase()}">${cls}</span></td>
            </tr>`;
        }).join('');
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

    // Штуки бывают дробными: цветы продаются поштучно, а лента и упаковка —
    // метрами и долями рулона
    function formatCount(value) {
        return new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 2 }).format(value || 0);
    }

    function formatPercent(value) {
        return new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 2 }).format(value || 0) + '%';
    }

    function escapeHtml(str) {
        return String(str ?? '').replace(/[&<>"']/g, (c) => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
        }[c]));
    }

    /**
     * Синхронизация данных из МойСклад
     */
    // Инкрементальный прогон укладывается в секунды (тянет только заказы,
    // изменившиеся с прошлого раза), поэтому опрашиваем часто. Полный прогон —
    // это ~40 минут, и там частый опрос сам становится заметной нагрузкой,
    // так что интервал у режимов разный.
    const SYNC_POLL_MS = { incremental: 2000, full: 10000 };

    // Сколько ждём появления нашего прогона, прежде чем решить, что запуститься
    // он не смог (лок держит планировщик или другой админ)
    const START_TIMEOUT_MS = 60000;

    const SYNC_BUTTONS = {
        incremental: { id: 'syncMoyskladData', idle: 'Обновить данные', busy: 'Обновление...' },
        full: { id: 'fullResyncMoysklad', idle: 'Полная пересинхронизация', busy: 'Синхронизация...' },
    };

    async function runSync(mode) {
        const config = SYNC_BUTTONS[mode];
        const btn = document.getElementById(config.id);
        const btnText = btn.querySelector('.btn-text');
        const statusEl = document.getElementById('abcSyncStatus');

        setButtonsDisabled(true);
        btnText.textContent = config.busy;
        statusEl.classList.remove('hidden');
        statusEl.className = 'update-status';
        statusEl.textContent = 'Запуск синхронизации...';

        try {
            const response = await fetch('/api/moysklad/sync', {
                method: 'POST',
                credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ mode: mode })
            });

            const result = await response.json();

            if (!result.success) {
                setSyncStatus(statusEl, 'Ошибка: ' + result.error, 'error');
                resetSyncButton(btn, btnText, config);
                return;
            }

            // Инкремент отрабатывает за пару секунд, и первый опрос статуса мог
            // застать ещё не стартовавший прогон: сервер отдал бы прошлый
            // завершённый лог, а страница показала бы ложное «готово» на старых
            // данных. Ждём лога, начавшегося после нашего запуска.
            const prevLogId = result.prev_log_id || 0;
            const startedAt = Date.now();

            const checkInterval = setInterval(async () => {
                try {
                    const status = await fetchSyncStatus();
                    if (!status) return;

                    if ((status.log_id || 0) <= prevLogId) {
                        // Прогон может вообще не начаться: лок держит планировщик
                        // или другой админ. Не ждём вечно — иначе кнопка залипает.
                        if (Date.now() - startedAt > START_TIMEOUT_MS) {
                            clearInterval(checkInterval);
                            setSyncStatus(statusEl, 'Синхронизация уже выполняется, данные скоро обновятся', 'success');
                            resetSyncButton(btn, btnText, config);
                            setTimeout(() => statusEl.classList.add('hidden'), 5000);
                            return;
                        }
                        statusEl.textContent = 'Запуск синхронизации...';
                        return;
                    }

                    if (status.running) {
                        statusEl.textContent = status.message || 'Синхронизация...';
                        return;
                    }

                    clearInterval(checkInterval);

                    if (status.error) {
                        setSyncStatus(statusEl, 'Ошибка: ' + status.error, 'error');
                    } else {
                        setSyncStatus(statusEl, status.message, 'success');
                        setTimeout(loadAbcData, 500);
                    }

                    renderFreshness(status.last_success);
                    resetSyncButton(btn, btnText, config);
                    setTimeout(() => statusEl.classList.add('hidden'), 5000);
                } catch (e) {
                    console.error('Ошибка проверки статуса синхронизации:', e);
                }
            }, SYNC_POLL_MS[mode]);
        } catch (error) {
            setSyncStatus(statusEl, 'Ошибка: ' + error.message, 'error');
            resetSyncButton(btn, btnText, config);
        }
    }

    async function fetchSyncStatus() {
        const response = await fetch('/api/moysklad/sync-status', { credentials: 'include' });
        const data = await response.json();
        return data.success ? data.status : null;
    }

    /**
     * Отметка свежести данных. Синхронизация идёт по расписанию сама, поэтому
     * важно не «когда нажимали кнопку», а насколько отчёт отстаёт от МойСклад.
     */
    function renderFreshness(lastSuccess) {
        const el = document.getElementById('abcSyncFreshness');
        if (!el) return;

        const date = window.BarhatTime ? window.BarhatTime.parse(lastSuccess) : null;
        if (!date) {
            el.textContent = '';
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
            ago = window.BarhatTime.formatDateTimeLong(lastSuccess);
        }

        el.textContent = `Данные обновлены: ${ago}. Синхронизация выполняется автоматически каждые 30 минут.`;
    }

    async function loadFreshness() {
        try {
            const status = await fetchSyncStatus();
            if (status) renderFreshness(status.last_success);
        } catch (e) {
            console.error('Ошибка загрузки статуса синхронизации:', e);
        }
    }

    function setSyncStatus(statusEl, text, kind) {
        statusEl.textContent = text;
        statusEl.classList.add(kind);
    }

    function setButtonsDisabled(disabled) {
        Object.values(SYNC_BUTTONS).forEach(function(config) {
            const btn = document.getElementById(config.id);
            if (btn) btn.disabled = disabled;
        });
    }

    function resetSyncButton(btn, btnText, config) {
        setButtonsDisabled(false);
        btnText.textContent = config.idle;
    }

    /**
     * Инициализация
     */
    function init() {
        const applyBtn = document.getElementById('applyAbcFilters');
        const resetBtn = document.getElementById('resetAbcFilters');
        const syncBtn = document.getElementById('syncMoyskladData');
        const fullResyncBtn = document.getElementById('fullResyncMoysklad');

        if (applyBtn) applyBtn.addEventListener('click', loadAbcData);
        if (resetBtn) {
            resetBtn.addEventListener('click', function() {
                applyDefaultPeriod();
                document.getElementById('abcChannel').value = '';
                loadAbcData();
            });
        }
        if (syncBtn) syncBtn.addEventListener('click', () => runSync('incremental'));
        if (fullResyncBtn) {
            fullResyncBtn.addEventListener('click', async function() {
                // Нативный confirm внутри iframe Пульса молча игнорируется —
                // кнопка выглядела бы сломанной. Диалоги только через BarhatUI.
                const message = 'Все заказы за 6 месяцев будут перечитаны заново. '
                    + 'Это занимает десятки минут и нагружает сайт. '
                    + 'Обычно достаточно кнопки «Обновить данные».';
                const ok = window.BarhatUI
                    ? await window.BarhatUI.confirm(message, {
                        title: 'Полная пересинхронизация',
                        confirmText: 'Запустить',
                    })
                    : true;
                if (ok) runSync('full');
            });
        }

        // Загружаем данные при активации страницы ABC-анализа
        const observer = new MutationObserver(function(mutations) {
            mutations.forEach(function(mutation) {
                if (mutation.target.classList.contains('active') &&
                    mutation.target.getAttribute('data-page') === 'abc_analysis') {
                    if (!document.getElementById('abcDateFrom').value) {
                        applyDefaultPeriod();
                    }
                    loadChannels();
                    loadAbcData();
                    loadFreshness();
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
