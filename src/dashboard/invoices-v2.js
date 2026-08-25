/**
 * Раздел «Согласование счетов v2» — пилот поверх существующих таблиц и API.
 *
 * План:    plans/2026-08-24-счета-новый-раздел.md
 * Эталон:  prototypes/invoices/index.html
 * Стиль:   DESIGN-SPEC.md (токены --bx-*, Vollkorn, линейные иконки, без эмодзи)
 * Стили:   invoices-v2.css (всё скоуплено под .iv2)
 *
 * Работает на том же blueprint, что и старый раздел (src/invoices/server.py).
 * Старый src/dashboard/invoices.js заморожен — из этого файла его не трогаем.
 *
 * Фаза 1: каркас — шапка со счётчиком «N ждут вас».
 * Фаза 2: KPI-плитки, единая панель фильтров, таблица с сортировкой и «Показать ещё».
 */

(function() {
    'use strict';

    // =========================================================================
    // Константы
    // =========================================================================

    const STATUSES = {
        on_approval:  { label: 'На согласовании', cls: 'b-onappr' },
        approved:     { label: 'Согласован',      cls: 'b-appr' },
        sent_to_bank: { label: 'Загружен в банк', cls: 'b-bank' },
        paid:         { label: 'Оплачен',         cls: 'b-paid' },
        rejected:     { label: 'Отклонён',        cls: 'b-rej' },
    };

    /**
     * Колонки таблицы — декларативным массивом (соглашение модуля, см. план
     * доработок счетов). Добавить колонку = добавить запись, а не править
     * разметку в четырёх местах.
     *
     * `sort` — ключ из белого списка сервера (INVOICE_SORT_FIELDS в storage.py).
     * Колонок «Действия» и «Ждёт» нет намеренно (решение №17 плана).
     */
    const COLS = [
        { key: 'number',       label: 'Номер',             sort: 'id' },
        { key: 'city',         label: 'Город',             sort: 'city' },
        { key: 'counterparty', label: 'Контрагент',        sort: 'counterparty_name' },
        { key: 'payer',        label: 'На кого выставлен', sort: 'payer' },
        { key: 'amount',       label: 'Сумма',             sort: 'amount', right: true },
        { key: 'alloc',        label: 'Распределение' },
        { key: 'due',          label: 'Оплата (план)',     sort: 'due_date' },
        { key: 'status',       label: 'Статус',            sort: 'status' },
        { key: 'author',       label: 'Автор',             sort: 'created_by' },
        { key: 'created',      label: 'Заведён',           sort: 'created_at' },
    ];

    // Считаем, а не хардкодим: раскрывающаяся строка распределения разъедется
    // при любом изменении набора колонок, если colspan прописать числом.
    const tableSpan = () => COLS.length;

    /**
     * Поля панели фильтров — тоже декларативно. `key` совпадает с параметром
     * запроса везде, кроме дат: их отдаём серверу пересчитанными в UTC.
     */
    const FILTERS = [
        { key: 'counterparty',        label: 'Контрагент',          type: 'text',   placeholder: 'название или ИНН' },
        { key: 'payment_purpose',     label: 'Назначение платежа',  type: 'text',   placeholder: 'текст в назначении' },
        { key: 'city_id',             label: 'Город',               type: 'select', ref: 'cities',     any: 'Все города' },
        { key: 'store_id',            label: 'Салон / проект',      type: 'select', ref: 'stores',     any: 'Все салоны' },
        { key: 'payer_id',            label: 'На кого выставлен',   type: 'select', ref: 'payers',     any: 'Все' },
        { key: 'expense_category_id', label: 'Статья расхода',      type: 'select', ref: 'categories', any: 'Все статьи' },
        { key: 'status',              label: 'Статус',              type: 'select', ref: 'statuses',   any: 'Любой' },
        { key: 'created_by',          label: 'Автор',               type: 'select', ref: 'authors',    any: 'Все' },
        { key: 'created_from',        label: 'Заведён с',           type: 'date' },
        { key: 'created_to',          label: 'Заведён по',          type: 'date' },
        { key: 'due_from',            label: 'Оплата (план) с',     type: 'date' },
        { key: 'due_to',              label: 'Оплата (план) по',    type: 'date' },
    ];

    // KPI-плитки. Клик по плитке = фильтр списка, повторный клик снимает.
    const KPI_TILES = [
        { key: 'wait',      label: 'Ждут согласования' },
        { key: 'due_today', label: 'К оплате сегодня' },
        { key: 'overdue',   label: 'Просрочено' },
        { key: 'rework',    label: 'На доработке у авторов' },
    ];

    const VIEWS = [
        { key: 'table', label: 'Таблица' },
        { key: 'queue', label: 'Очередь' },
        { key: 'board', label: 'Платёжный борд' },
    ];

    const PAGE_SIZE = 25;
    const TEXT_FILTER_DELAY_MS = 350;

    // =========================================================================
    // Состояние
    // =========================================================================

    const state = {
        user: null,
        view: 'table',
        refs: { cities: [], payers: [], categories: [], stores: [], authors: [], statuses: [] },
        refsLoaded: false,
        filters: emptyFilters(),
        showArchived: false,
        showPaid: true,
        kpiFilter: null,
        sort: { key: 'created_at', dir: 'desc' },
        rows: [],
        total: 0,
        totalAmount: 0,
        expandedAlloc: new Set(),
        summary: null,
        loading: false,
    };

    // Токен запроса: ответы приходят не в том порядке, в каком уходили. Без
    // него быстрый набор в поле фильтра оставляет на экране результат
    // предпоследнего запроса.
    let listToken = 0;
    let summaryToken = 0;
    let filtersRendered = false;
    let textFilterTimer = null;

    function emptyFilters() {
        const empty = {};
        FILTERS.forEach(f => { empty[f.key] = ''; });
        return empty;
    }

    // =========================================================================
    // Утилиты
    // =========================================================================

    function $(id) {
        return document.getElementById(id);
    }

    /**
     * Экранирование для вставки в HTML.
     * Кавычка обязательна: трюк через div.textContent её не экранирует и молча
     * обрезает любое value="..." — на этом уже ловились четыре модуля дашборда.
     */
    function escapeHtml(str) {
        return String(str ?? '').replace(/[&<>"']/g, (c) => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
        }[c]));
    }

    const RUB = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 0 });

    function money(value) {
        return RUB.format(Math.round(Number(value) || 0)) + ' ₽';
    }

    function plural(n, one, few, many) {
        const mod100 = Math.abs(n) % 100;
        const mod10 = mod100 % 10;
        if (mod100 >= 11 && mod100 <= 14) return many;
        if (mod10 === 1) return one;
        if (mod10 >= 2 && mod10 <= 4) return few;
        return many;
    }

    function withCount(n, one, few, many) {
        return n + ' ' + plural(n, one, few, many);
    }

    /** Сегодняшняя дата по часам устройства — ею же считаются KPI на сервере. */
    function today() {
        return window.BarhatTime
            ? window.BarhatTime.todayInputValue()
            : new Date().toISOString().slice(0, 10);
    }

    /** Сколько дней от сегодня до календарной даты: <0 — просрочен. */
    function daysTo(plainDate) {
        if (!plainDate) return null;
        const target = new Date(String(plainDate).slice(0, 10) + 'T00:00:00');
        if (isNaN(target.getTime())) return null;
        const now = new Date();
        now.setHours(0, 0, 0, 0);
        return Math.round((target - now) / 86400000);
    }

    /** Срок оплаты — календарная дата, в другой пояс не переводится. */
    function fmtDue(value) {
        return window.BarhatTime ? window.BarhatTime.formatPlainDate(value, '—') : (value || '—');
    }

    /** Дата заведения — в базе UTC, показываем по поясу устройства. */
    function fmtCreated(value) {
        return window.BarhatTime ? window.BarhatTime.formatDateTime(value, '—') : (value || '—');
    }

    function daysSince(utcValue) {
        const parsed = window.BarhatTime ? window.BarhatTime.parse(utcValue) : new Date(utcValue);
        if (!parsed || isNaN(parsed.getTime())) return null;
        return Math.max(0, Math.round((Date.now() - parsed.getTime()) / 86400000));
    }

    function toast(message, kind) {
        if (window.BarhatUI) window.BarhatUI.toast(message, kind);
    }

    // =========================================================================
    // Запросы
    // =========================================================================

    async function apiGet(path) {
        const response = await fetch(path, { credentials: 'include' });
        if (!response.ok) {
            let message = 'HTTP ' + response.status;
            try {
                const body = await response.json();
                if (body && body.error) message = body.error;
            } catch (e) { /* тело не JSON — оставляем код статуса */ }
            throw new Error(message);
        }
        return response.json();
    }

    /**
     * Параметры запроса списка из текущих фильтров.
     *
     * Даты заведения уходят границами суток по часам сотрудника, пересчитанными
     * в UTC (в базе время в UTC). Срок оплаты — календарная дата, её не трогаем.
     */
    function buildListParams() {
        const params = new URLSearchParams();
        const time = window.BarhatTime;

        FILTERS.forEach(f => {
            const value = (state.filters[f.key] || '').trim();
            if (!value) return;
            if (f.key === 'created_from') {
                params.set('created_from', time ? time.dayStartUtc(value) : value);
            } else if (f.key === 'created_to') {
                params.set('created_to', time ? time.dayEndUtc(value) : value);
            } else {
                params.set(f.key, value);
            }
        });

        if (state.showArchived) params.set('archived', 'true');

        // «Показывать оплаченные» имеет смысл, только пока статус не выбран
        // явно: иначе выбор «Оплачен» и снятая галочка дали бы пустой список
        // и вопрос «куда всё делось».
        if (!state.showPaid && !state.filters.status) params.set('hide_paid', 'true');

        applyKpiFilter(params);

        params.set('sort', state.sort.key);
        params.set('order', state.sort.dir);
        return params;
    }

    /**
     * Плитка KPI поверх фильтров: она задаёт свой срез и в спорных полях
     * побеждает — за плитку человек только что кликнул, а фильтр мог остаться
     * с прошлого раза. Активная плитка показана чипом и снимается им же.
     *
     * Скрытых поправок здесь нет: всё, что плитка меняет сверх дат и статуса
     * (архив, оплаченные), выставляется переключателями в kpiTileClicked —
     * иначе на экране одни фильтры, а в запросе другие.
     */
    function applyKpiFilter(params) {
        const now = today();
        if (state.kpiFilter === 'wait') {
            params.set('status', 'on_approval');
        } else if (state.kpiFilter === 'due_today') {
            params.set('due_from', now);
            params.set('due_to', now);
        } else if (state.kpiFilter === 'overdue') {
            params.delete('due_from');
            params.set('due_to', yesterday(now));
        }
        // 'rework' появится в Фазе 7 вместе с колонкой rework_at
    }

    /**
     * Клик по плитке. Плитки считаются по неархивным счетам, а «сегодня» и
     * «просрочено» — ещё и без оплаченных. Переключатели приводим в то же
     * положение, иначе число на плитке и число в списке разойдутся, и это
     * первое, что заметит пользователь.
     */
    function kpiTileClicked(key) {
        state.kpiFilter = state.kpiFilter === key ? null : key;
        if (state.kpiFilter) {
            state.showArchived = false;
            if (state.kpiFilter === 'due_today' || state.kpiFilter === 'overdue') {
                state.showPaid = false;
            }
            syncFilterInputs();
        }
        renderKpis();
        renderChips();
        loadList(false);
    }

    function yesterday(plainDate) {
        const d = new Date(plainDate + 'T00:00:00');
        d.setDate(d.getDate() - 1);
        const mo = String(d.getMonth() + 1).padStart(2, '0');
        const day = String(d.getDate()).padStart(2, '0');
        return `${d.getFullYear()}-${mo}-${day}`;
    }

    async function loadReferences() {
        if (state.refsLoaded) return;
        const [cities, payers, categories, stores, authors] = await Promise.all([
            apiGet('/api/invoices/cities').catch(() => ({ cities: [] })),
            apiGet('/api/invoices/payers').catch(() => ({ payers: [] })),
            apiGet('/api/invoices/categories').catch(() => ({ categories: [] })),
            apiGet('/api/invoices/stores').catch(() => ({ stores: [] })),
            apiGet('/api/invoices/authors').catch(() => ({ authors: [] })),
        ]);
        state.refs.cities = cities.cities || [];
        state.refs.payers = payers.payers || [];
        state.refs.categories = categories.categories || [];
        state.refs.stores = stores.stores || [];
        state.refs.authors = (authors.authors || []).map(a => ({
            id: a.username,
            name: a.full_name || a.username,
        }));
        state.refs.statuses = Object.keys(STATUSES).map(key => ({ id: key, name: STATUSES[key].label }));
        state.refsLoaded = true;
    }

    async function loadSummary() {
        const token = ++summaryToken;
        try {
            const data = await apiGet('/api/invoices/summary?today=' + encodeURIComponent(today()));
            if (token !== summaryToken) return;
            state.summary = data;
        } catch (error) {
            if (token !== summaryToken) return;
            console.error('invoices-v2: не удалось загрузить KPI:', error);
            state.summary = null;
        }
        renderKpis();
        renderHeaderCount();
    }

    /**
     * Перечитать список.
     * append=true — «Показать ещё»: дозагружаем следующую страницу и НЕ просим
     * пересчитать итог, он уже посчитан на первой странице тем же фильтром.
     */
    async function loadList(append) {
        const token = ++listToken;
        state.loading = true;

        const params = buildListParams();
        params.set('limit', String(PAGE_SIZE));
        params.set('offset', String(append ? state.rows.length : 0));
        if (!append) params.set('with_total', 'true');

        renderTable();

        try {
            const data = await apiGet('/api/invoices?' + params.toString());
            if (token !== listToken) return;

            state.rows = append ? state.rows.concat(data.invoices || []) : (data.invoices || []);
            if (!append) {
                state.total = data.total ?? state.rows.length;
                state.totalAmount = data.total_amount ?? 0;
                state.expandedAlloc.clear();
            }
        } catch (error) {
            if (token !== listToken) return;
            console.error('invoices-v2: не удалось загрузить список:', error);
            toast('Не удалось загрузить счета: ' + error.message, 'error');
            if (!append) {
                state.rows = [];
                state.total = 0;
                state.totalAmount = 0;
            }
        } finally {
            if (token === listToken) {
                state.loading = false;
                render();
            }
        }
    }

    // =========================================================================
    // Отрисовка: шапка и KPI
    // =========================================================================

    function renderHeaderCount() {
        const badge = $('iv2HeaderCount');
        if (!badge) return;
        if (!state.summary) {
            badge.hidden = true;
            return;
        }
        const waiting = state.summary.wait.count;
        badge.textContent = waiting
            ? waiting + ' ' + plural(waiting, 'ждёт', 'ждут', 'ждут') + ' вас'
            : 'всё согласовано';
        badge.hidden = false;
    }

    function renderKpis() {
        const host = $('iv2Kpis');
        if (!host) return;

        if (!state.summary) {
            host.innerHTML = '<div class="iv2-kpis-empty">Показатели пока не загрузились</div>';
            return;
        }

        host.className = 'iv2-kpi-grid';
        host.innerHTML = KPI_TILES.map(tile => {
            const data = state.summary[tile.key] || { count: 0, amount: 0 };
            const active = state.kpiFilter === tile.key ? ' iv2-kpi--active' : '';
            // Плитка «на доработке» до Фазы 7 не кликается: колонки rework_at
            // ещё нет, фильтровать нечем — честнее показать ноль без действия.
            const clickable = tile.key !== 'rework';
            return `
                <button class="iv2-kpi${active}" data-kpi="${tile.key}"${clickable ? '' : ' disabled'}>
                    <span class="iv2-kpi__label">${escapeHtml(tile.label)}</span>
                    <span class="iv2-kpi__value">${data.count}<span class="iv2-kpi__unit">${escapeHtml(money(data.amount))}</span></span>
                    <span class="iv2-kpi__sub ${kpiSubClass(tile.key, data)}">${kpiSubText(tile.key, data)}</span>
                </button>`;
        }).join('');

        host.querySelectorAll('[data-kpi]').forEach(button => {
            button.addEventListener('click', () => kpiTileClicked(button.getAttribute('data-kpi')));
        });
    }

    function kpiSubClass(key, data) {
        if (key === 'overdue') return data.count ? 'iv2-kpi__sub--down' : 'iv2-kpi__sub--up';
        return 'iv2-kpi__sub--flat';
    }

    function kpiSubText(key, data) {
        if (key === 'wait') {
            const days = data.oldest_at ? daysSince(data.oldest_at) : null;
            return days === null ? 'очередь пуста' : `самый старый ждёт ${withCount(days, 'день', 'дня', 'дней')}`;
        }
        if (key === 'due_today') return 'по плановой дате';
        if (key === 'overdue') return data.count ? '▼ требует решения сегодня' : '▲ просрочек нет';
        if (key === 'rework') {
            return state.summary && state.summary.rework && state.summary.rework.available
                ? 'вернулись к сотрудникам'
                : 'появится вместе с возвратом на доработку';
        }
        return '';
    }

    // =========================================================================
    // Отрисовка: панель фильтров
    // =========================================================================

    function renderFiltersPanel() {
        const host = $('iv2Filters');
        if (!host || filtersRendered) return;

        const fields = FILTERS.map(f => `
            <div class="iv2-field">
                <label class="iv2-field__label" for="iv2f-${f.key}">${escapeHtml(f.label)}</label>
                ${f.type === 'select'
                    ? `<select class="iv2-select" id="iv2f-${f.key}"></select>`
                    : `<input class="iv2-input" id="iv2f-${f.key}"
                              type="${f.type === 'date' ? 'date' : 'text'}"
                              ${f.placeholder ? `placeholder="${escapeHtml(f.placeholder)}"` : ''}>`}
            </div>`).join('');

        host.className = 'iv2-filters';
        host.innerHTML = `
            <div class="iv2-filters__bar">
                <div class="iv2-filters__left">
                    <button class="iv2-filters__toggle" id="iv2FiltersToggle" type="button">
                        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                             stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
                            <path d="M3 6h18"/><path d="M7 12h10"/><path d="M10 18h4"/>
                        </svg>
                        Фильтры
                    </button>
                    <div class="iv2-chips" id="iv2Chips"></div>
                </div>
                <div class="iv2-hint" id="iv2FiltersCount"></div>
            </div>
            <div class="iv2-filters__body">
                <div class="iv2-filters__grid">${fields}</div>
                <div class="iv2-filters__foot">
                    <div class="iv2-switches">
                        <label class="iv2-switch">
                            <input type="checkbox" class="iv2-cbx" id="iv2fArchived"> Показать архив
                        </label>
                        <label class="iv2-switch">
                            <input type="checkbox" class="iv2-cbx" id="iv2fPaid" checked> Показывать оплаченные
                        </label>
                    </div>
                    <button class="bx-btn bx-btn--ghost bx-btn--sm" id="iv2FiltersReset" type="button">Сбросить всё</button>
                </div>
            </div>`;

        filtersRendered = true;
        bindFilters();
    }

    function fillFilterSelects() {
        FILTERS.filter(f => f.type === 'select').forEach(f => {
            const select = $('iv2f-' + f.key);
            if (!select) return;
            const items = state.refs[f.ref] || [];
            const current = state.filters[f.key];
            select.innerHTML = `<option value="">${escapeHtml(f.any)}</option>` + items.map(item =>
                `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)}</option>`).join('');
            select.value = current || '';
        });
    }

    function bindFilters() {
        const toggle = $('iv2FiltersToggle');
        if (toggle) {
            toggle.addEventListener('click', () => {
                const host = $('iv2Filters');
                if (host) host.classList.toggle('iv2-filters--open');
            });
        }

        // Кнопки «Применить» нет (решение владельца): список пересобирается сам.
        // Текстовые поля ждут паузы в наборе — иначе каждый символ уходит
        // запросом, а воркеров на проде всего два. Селекты, даты и
        // переключатели срабатывают сразу: там одно осознанное действие.
        FILTERS.forEach(f => {
            const el = $('iv2f-' + f.key);
            if (!el) return;
            if (f.type === 'text') {
                el.addEventListener('input', () => {
                    state.filters[f.key] = el.value;
                    clearTimeout(textFilterTimer);
                    textFilterTimer = setTimeout(() => {
                        renderChips();
                        loadList(false);
                    }, TEXT_FILTER_DELAY_MS);
                });
            } else {
                el.addEventListener('change', () => {
                    state.filters[f.key] = el.value;
                    renderChips();
                    loadList(false);
                });
            }
        });

        const archived = $('iv2fArchived');
        if (archived) {
            archived.addEventListener('change', () => {
                state.showArchived = archived.checked;
                renderChips();
                loadList(false);
            });
        }

        const paid = $('iv2fPaid');
        if (paid) {
            paid.addEventListener('change', () => {
                state.showPaid = paid.checked;
                renderChips();
                loadList(false);
            });
        }

        const reset = $('iv2FiltersReset');
        if (reset) reset.addEventListener('click', resetFilters);
    }

    function resetFilters() {
        state.filters = emptyFilters();
        state.showArchived = false;
        state.showPaid = true;
        state.kpiFilter = null;
        syncFilterInputs();
        renderChips();
        loadList(false);
        toast('Фильтры сброшены');
    }

    function syncFilterInputs() {
        FILTERS.forEach(f => {
            const el = $('iv2f-' + f.key);
            if (el) el.value = state.filters[f.key] || '';
        });
        const archived = $('iv2fArchived');
        if (archived) archived.checked = state.showArchived;
        const paid = $('iv2fPaid');
        if (paid) paid.checked = state.showPaid;
    }

    /** Человеческая подпись значения фильтра для чипа. */
    function filterValueLabel(f) {
        const value = state.filters[f.key];
        if (!value) return '';
        if (f.type === 'select') {
            const item = (state.refs[f.ref] || []).find(x => String(x.id) === String(value));
            return item ? item.name : value;
        }
        if (f.type === 'date') return fmtDue(value);
        return value;
    }

    function renderChips() {
        const host = $('iv2Chips');
        if (!host) return;

        const chips = [];
        FILTERS.forEach(f => {
            if (!state.filters[f.key]) return;
            chips.push({ kind: 'filter', key: f.key, text: `${f.label}: ${filterValueLabel(f)}` });
        });
        if (state.showArchived) chips.push({ kind: 'archived', key: 'archived', text: 'Только архив' });
        if (!state.showPaid) chips.push({ kind: 'paid', key: 'paid', text: 'Без оплаченных' });
        if (state.kpiFilter) {
            const tile = KPI_TILES.find(t => t.key === state.kpiFilter);
            if (tile) chips.push({ kind: 'kpi', key: state.kpiFilter, text: tile.label });
        }

        host.innerHTML = chips.map(chip => `
            <span class="iv2-chip">${escapeHtml(chip.text)}
                <button type="button" data-chip="${escapeHtml(chip.kind)}" data-chip-key="${escapeHtml(chip.key)}"
                        aria-label="Снять фильтр">
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                         stroke-width="2" stroke-linecap="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>
                </button>
            </span>`).join('');

        host.querySelectorAll('[data-chip]').forEach(button => {
            button.addEventListener('click', () => {
                const kind = button.getAttribute('data-chip');
                const key = button.getAttribute('data-chip-key');
                if (kind === 'archived') state.showArchived = false;
                else if (kind === 'paid') state.showPaid = true;
                else if (kind === 'kpi') state.kpiFilter = null;
                else state.filters[key] = '';
                syncFilterInputs();
                renderChips();
                loadList(false);
            });
        });
    }

    // =========================================================================
    // Отрисовка: табы видов
    // =========================================================================

    function renderTabs() {
        const host = $('iv2Toolbar');
        if (!host || host.dataset.ready === '1') return;

        host.className = 'iv2-toolbar';
        host.innerHTML = `
            <div class="iv2-tabs">
                ${VIEWS.map(v => `
                    <button class="iv2-tab${v.key === state.view ? ' iv2-tab--active' : ''}"
                            data-view="${v.key}" type="button">${escapeHtml(v.label)}</button>`).join('')}
            </div>
            <div class="iv2-hint" id="iv2ViewHint"></div>`;

        host.querySelectorAll('[data-view]').forEach(button => {
            button.addEventListener('click', () => {
                state.view = button.getAttribute('data-view');
                host.querySelectorAll('[data-view]').forEach(b =>
                    b.classList.toggle('iv2-tab--active', b.getAttribute('data-view') === state.view));
                renderViews();
            });
        });

        host.dataset.ready = '1';
    }

    function renderViews() {
        const map = { table: 'iv2ViewTable', queue: 'iv2ViewQueue', board: 'iv2ViewBoard' };
        Object.keys(map).forEach(view => {
            const el = $(map[view]);
            if (el) el.hidden = view !== state.view;
        });

        const hint = $('iv2ViewHint');
        if (hint) {
            hint.textContent = state.view === 'table'
                ? 'Фильтры общие для всех видов'
                : 'Этот вид ещё в работе, фильтры к нему уже применяются';
        }

        // Очередь и борд — Фазы 3 и 9. Пока показываем, что именно приедет,
        // вместо пустого экрана без объяснений.
        renderPlaceholder('iv2ViewQueue', 'Очередь согласования',
            'Список слева, скан по центру, поля и кнопки справа — Фаза 3.');
        renderPlaceholder('iv2ViewBoard', 'Платёжный борд',
            'Колонки по срокам оплаты, перенос даты перетаскиванием — Фаза 9.');
    }

    function renderPlaceholder(hostId, title, text) {
        const host = $(hostId);
        if (!host || host.dataset.ready === '1') return;
        host.innerHTML = `
            <div class="iv2-placeholder">
                <h3 class="iv2-placeholder__title">${escapeHtml(title)}</h3>
                <p>${escapeHtml(text)}</p>
            </div>`;
        host.dataset.ready = '1';
    }

    // =========================================================================
    // Отрисовка: таблица
    // =========================================================================

    function statusBadge(invoice) {
        const meta = STATUSES[invoice.status] || { label: invoice.status, cls: 'b-rej' };
        const archived = invoice.is_archived
            ? ' <span class="bx-badge b-arch">Архив</span>'
            : '';
        return `<span class="bx-badge ${meta.cls}">${escapeHtml(meta.label)}</span>${archived}`;
    }

    function dueBadge(invoice) {
        if (invoice.status === 'paid') return '<span class="iv2-hint">оплачен</span>';
        const days = daysTo(invoice.due_date);
        if (days === null) return '';
        if (days < 0) return `<span class="bx-badge b-over">просрочен на ${withCount(Math.abs(days), 'день', 'дня', 'дней')}</span>`;
        if (days === 0) return '<span class="bx-badge b-today">сегодня</span>';
        if (days === 1) return '<span class="iv2-hint">завтра</span>';
        return `<span class="iv2-hint">через ${withCount(days, 'день', 'дня', 'дней')}</span>`;
    }

    /**
     * Ячейка распределения. При одной строке показываем и салон, и статью:
     * без статьи колонка отвечает лишь на половину вопроса «куда ушли деньги».
     */
    function allocCell(invoice) {
        const items = invoice.line_items || [];
        if (!items.length) return '<span class="iv2-hint">не распределён</span>';
        if (items.length === 1) {
            return `${escapeHtml(items[0].store_name || '—')}
                    <div class="iv2-hint">${escapeHtml(items[0].category_name || '—')}</div>`;
        }
        const stores = new Set(items.map(i => i.store_name));
        const cats = new Set(items.map(i => i.category_name));
        return `<button class="iv2-linkish" type="button" data-alloc="${invoice.id}">
                    ${withCount(items.length, 'строка', 'строки', 'строк')}</button>
                <div class="iv2-hint">${escapeHtml(withCount(stores.size, 'салон', 'салона', 'салонов'))} ·
                    ${escapeHtml(withCount(cats.size, 'статья', 'статьи', 'статей'))}</div>`;
    }

    function allocDetailRow(invoice) {
        const items = invoice.line_items || [];
        if (items.length < 2 || !state.expandedAlloc.has(invoice.id)) return '';
        const rows = items.map(item => `
            <tr>
                <td>${escapeHtml(item.store_name || '—')}</td>
                <td>${escapeHtml(item.category_name || '—')}</td>
                <td class="iv2-right">${escapeHtml(money(item.amount))}</td>
            </tr>`).join('');
        return `
            <tr class="iv2-alloc-row">
                <td colspan="${tableSpan()}">
                    <table class="iv2-alloc-table">
                        <thead><tr><th>Салон</th><th>Статья</th><th class="iv2-right">Сумма</th></tr></thead>
                        <tbody>${rows}</tbody>
                    </table>
                </td>
            </tr>`;
    }

    function cellContent(col, invoice) {
        switch (col.key) {
            case 'number': return escapeHtml(invoice.invoice_number || ('#' + invoice.id));
            case 'city': return escapeHtml(invoice.city_name || '—');
            case 'counterparty': return escapeHtml(invoice.counterparty_name || '—');
            case 'payer': return escapeHtml(invoice.payer_name || '—');
            case 'amount': return escapeHtml(money(invoice.amount));
            case 'alloc': return allocCell(invoice);
            case 'due': return `${escapeHtml(fmtDue(invoice.due_date))}<div class="iv2-due-badge">${dueBadge(invoice)}</div>`;
            case 'status': return statusBadge(invoice);
            case 'author': return escapeHtml(invoice.created_by_full_name || invoice.created_by || '—');
            case 'created': return escapeHtml(fmtCreated(invoice.created_at));
            default: return '';
        }
    }

    function sortArrow(col) {
        if (!col.sort || state.sort.key !== col.sort) return '';
        return state.sort.dir === 'asc' ? ' ▲' : ' ▼';
    }

    function currentSortLabel() {
        const col = COLS.find(c => c.sort === state.sort.key);
        const name = col ? col.label : 'дата заведения';
        return `${name} — ${state.sort.dir === 'asc' ? 'по возрастанию' : 'по убыванию'}`;
    }

    function renderTable() {
        const host = $('iv2ViewTable');
        if (!host) return;

        const head = COLS.map(col => `
            <th class="${col.sort ? 'iv2-sortable' : ''}${col.sort === state.sort.key ? ' iv2-sorted' : ''}${col.right ? ' iv2-right' : ''}"
                ${col.sort ? `data-sort="${col.sort}"` : ''}>${escapeHtml(col.label)}${sortArrow(col)}</th>`).join('');

        let body;
        if (state.loading && !state.rows.length) {
            body = `<tr><td colspan="${tableSpan()}" class="iv2-tbl-empty">Загрузка…</td></tr>`;
        } else if (!state.rows.length) {
            body = `<tr><td colspan="${tableSpan()}" class="iv2-tbl-empty">Под текущие фильтры счетов нет</td></tr>`;
        } else {
            body = state.rows.map(invoice => `
                <tr class="${invoice.is_archived ? 'iv2-arch' : ''}">
                    ${COLS.map(col => `<td class="${col.right ? 'iv2-right iv2-num' : ''}">${cellContent(col, invoice)}</td>`).join('')}
                </tr>${allocDetailRow(invoice)}`).join('');
        }

        const shownAmount = state.rows.reduce((sum, i) => sum + (Number(i.amount) || 0), 0);
        const rest = Math.max(0, state.total - state.rows.length);

        host.innerHTML = `
            <div class="iv2-tbl-wrap">
                <div class="iv2-tbl-sum">
                    <span>Найдено: <b>${state.total}</b> на <b>${escapeHtml(money(state.totalAmount))}</b></span>
                    <span>Показано: <b>${state.rows.length}</b> на <b>${escapeHtml(money(shownAmount))}</b></span>
                    <span class="iv2-hint">Сортировка: ${escapeHtml(currentSortLabel())}</span>
                </div>
                <table class="iv2-tbl">
                    <thead><tr>${head}</tr></thead>
                    <tbody>${body}</tbody>
                </table>
                <div class="iv2-more">
                    ${rest > 0
                        ? `<button class="bx-btn bx-btn--ghost" id="iv2MoreBtn" type="button"${state.loading ? ' disabled' : ''}>
                               Показать ещё · осталось ${rest}</button>`
                        : `<span class="iv2-hint">${state.rows.length ? 'Показаны все ' + state.rows.length : ''}</span>`}
                </div>
            </div>`;

        host.querySelectorAll('[data-sort]').forEach(th => {
            th.addEventListener('click', () => {
                const key = th.getAttribute('data-sort');
                if (state.sort.key === key) {
                    state.sort.dir = state.sort.dir === 'asc' ? 'desc' : 'asc';
                } else {
                    state.sort.key = key;
                    // Деньги и даты человек почти всегда смотрит с большего:
                    // сначала крупные суммы и ближний срок.
                    state.sort.dir = (key === 'amount' || key === 'created_at') ? 'desc' : 'asc';
                }
                loadList(false);
            });
        });

        host.querySelectorAll('[data-alloc]').forEach(button => {
            button.addEventListener('click', () => {
                const id = Number(button.getAttribute('data-alloc'));
                if (state.expandedAlloc.has(id)) state.expandedAlloc.delete(id);
                else state.expandedAlloc.add(id);
                renderTable();
            });
        });

        const more = $('iv2MoreBtn');
        if (more) more.addEventListener('click', () => loadList(true));

        const count = $('iv2FiltersCount');
        if (count) {
            count.textContent = state.loading && !state.rows.length
                ? 'Считаем…'
                : `Найдено ${withCount(state.total, 'счёт', 'счёта', 'счетов')} на ${money(state.totalAmount)}`;
        }
    }

    function render() {
        renderKpis();
        renderChips();
        renderTable();
    }

    // =========================================================================
    // Точка входа
    // =========================================================================

    /**
     * Вызывается из script.js при переходе на страницу раздела.
     * Данные грузим здесь, а не при загрузке дашборда: иначе каждый вход
     * в любой раздел дёргал бы API счетов.
     */
    async function onPageActivated(user) {
        state.user = user || state.user;

        renderFiltersPanel();
        renderTabs();
        renderViews();

        try {
            await loadReferences();
            fillFilterSelects();
        } catch (error) {
            console.error('invoices-v2: не удалось загрузить справочники:', error);
        }

        loadSummary();
        loadList(false);
    }

    window.InvoicesV2Module = {
        onPageActivated,
    };
})();
