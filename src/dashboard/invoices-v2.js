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
    // Колонка чекбоксов есть только у того, кому доступны массовые действия.
    const tableSpan = () => COLS.length + (canBulk() ? 1 : 0);

    // Из каких статусов счёт вообще можно выбрать для массового действия
    const BULK_SELECTABLE = ['on_approval', 'approved', 'sent_to_bank'];

    // Массовые действия: какие статусы принимает каждое и как называется
    const BULK_ACTIONS = [
        { key: 'approve', label: 'Согласовать', statuses: ['on_approval'], style: 'bx-btn--ok' },
        { key: 'bank', label: 'Отправить в банк', statuses: ['approved'], style: '' },
        { key: 'paid', label: 'Отметить оплаченными', statuses: ['approved', 'sent_to_bank'], style: 'bx-btn--ok' },
    ];

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

    // Очередь первой и по умолчанию (решение №4 плана): раздел делался ради
    // того, чтобы работа приходила к согласующему, а не искалась в таблице.
    const VIEWS = [
        { key: 'queue', label: 'Очередь' },
        { key: 'table', label: 'Таблица' },
        { key: 'board', label: 'Платёжный борд' },
    ];

    const PAGE_SIZE = 25;
    const TEXT_FILTER_DELAY_MS = 350;

    // Человеческие названия полей в истории изменений
    const HISTORY_FIELDS = {
        status: 'Статус',
        amount: 'Сумма',
        due_date: 'Срок оплаты',
        payment_purpose: 'Назначение платежа',
        comment: 'Комментарий',
        city_id: 'Город',
        payer_id: 'На кого выставлен',
        vat_id: 'НДС',
        counterparty_name: 'Контрагент',
        counterparty_inn: 'ИНН',
        counterparty_kpp: 'КПП',
        counterparty_bank_name: 'Банк',
        counterparty_bank_bik: 'БИК',
        counterparty_bank_account: 'Расчётный счёт',
        counterparty_bank_corr_account: 'Корр. счёт',
        line_items: 'Распределение',
        attachments: 'Вложения',
        is_archived: 'Архив',
    };

    // Показываем вложение прямо в интерфейсе только то, что браузер умеет
    // отрисовать. Список тот же, что в белом списке ручки inline на сервере.
    const IMAGE_EXTENSIONS = ['.png', '.jpg', '.jpeg', '.webp'];

    // Что вообще принимаем во вложения — тот же список, что
    // ALLOWED_ATTACHMENT_EXTENSIONS в storage.py. Расходиться им нельзя:
    // клиент отсеет лишнее до загрузки, сервер — окончательно.
    const ATTACHMENT_EXTENSIONS = IMAGE_EXTENSIONS.concat(['.pdf']);
    const MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024;

    // Фото счёта с телефона — это 3-8 МБ, интернет в салоне медленный, а
    // вложения копятся на платном /data. 1500px по длинной стороне читаемость
    // счёта сохраняют полностью.
    const IMAGE_MAX_SIDE = 1500;
    const COMPRESS_ABOVE_BYTES = 1024 * 1024;

    // =========================================================================
    // Состояние
    // =========================================================================

    const state = {
        user: null,
        view: 'queue',
        refs: { cities: [], payers: [], categories: [], stores: [], authors: [], statuses: [], vatOptions: [] },
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
        selected: new Set(),
        report: null,
        summary: null,
        loading: false,

        // Очередь: какой счёт открыт в правой колонке и какое из его вложений
        // показано. Карточка — та же панель, но модалкой поверх любого вида.
        queueActiveId: null,
        docIndex: 0,
        card: { id: null, data: null, loading: false, error: null },
        cardPushed: false,
        // Черновики сообщений переживают перерисовку: иначе набранный текст
        // пропадает от любого действия в соседней колонке.
        commentDrafts: {},
        details: {},
    };

    /**
     * Счёт из адреса страницы. Читаем СИНХРОННО при загрузке файла: script.js
     * на первой отрисовке зовёт history.replaceState('/invoices-v2') и стирает
     * параметр, а его обработчик асинхронный (ждёт /api/auth/me) и потому
     * выполняется позже этого модуля.
     */
    let pendingInvoiceId = readInvoiceIdFromUrl();

    function readInvoiceIdFromUrl() {
        try {
            const raw = new URLSearchParams(window.location.search).get('invoice');
            const id = Number(raw);
            return raw && Number.isFinite(id) && id > 0 ? id : null;
        } catch (e) {
            return null;
        }
    }

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
        const [cities, payers, categories, stores, authors, vat] = await Promise.all([
            apiGet('/api/invoices/cities').catch(() => ({ cities: [] })),
            apiGet('/api/invoices/payers').catch(() => ({ payers: [] })),
            apiGet('/api/invoices/categories').catch(() => ({ categories: [] })),
            apiGet('/api/invoices/stores').catch(() => ({ stores: [] })),
            apiGet('/api/invoices/authors').catch(() => ({ authors: [] })),
            apiGet('/api/invoices/vat-options').catch(() => ({ 'vat-options': [] })),
        ]);
        state.refs.cities = cities.cities || [];
        state.refs.payers = payers.payers || [];
        state.refs.categories = categories.categories || [];
        state.refs.stores = stores.stores || [];
        state.refs.vatOptions = vat['vat-options'] || [];
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

        // Показываем «Загрузка…» в том виде, который сейчас на экране, —
        // иначе очередь на первом входе успевает мигнуть «Очередь пуста».
        renderTable();
        if (state.view === 'queue') renderQueue();

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

    function renderHeaderActions() {
        const host = $('iv2HeaderActions');
        if (!host || host.dataset.ready === '1') return;
        host.innerHTML = `
            <button class="bx-btn bx-btn--light" type="button" id="iv2NewInvoice">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                     stroke-width="1.75" stroke-linecap="round"><path d="M12 5v14"/><path d="M5 12h14"/></svg>
                Новый счёт
            </button>`;
        const button = $('iv2NewInvoice');
        if (button) button.addEventListener('click', () => openForm('create'));
        host.dataset.ready = '1';
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
            hint.textContent = state.view === 'board'
                ? 'Этот вид ещё в работе, фильтры к нему уже применяются'
                : 'Фильтры общие для всех видов';
        }

        // Борд — Фаза 9. Пока показываем, что именно приедет, вместо пустого
        // экрана без объяснений.
        renderPlaceholder('iv2ViewBoard', 'Платёжный борд',
            'Колонки по срокам оплаты, перенос даты перетаскиванием — Фаза 9.');

        // Панель массовых действий имеет смысл только в таблице — там чекбоксы
        renderBulkBar();

        if (state.view === 'queue') {
            renderQueue();
            // Детали активного счёта могли не загрузиться, пока вид был скрыт
            if (state.queueActiveId && !state.details[state.queueActiveId]) {
                selectQueueInvoice(state.queueActiveId);
            }
        }
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
            case 'number': return `<button class="iv2-linkish iv2-linkish--num" type="button" data-open="${invoice.id}">${escapeHtml(invoice.invoice_number || ('#' + invoice.id))}</button>`;
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

    function canBulk() {
        return Boolean(state.user && state.user.role === 'admin');
    }

    function isSelectable(invoice) {
        return !invoice.is_archived && BULK_SELECTABLE.indexOf(invoice.status) !== -1;
    }

    function renderTable() {
        const host = $('iv2ViewTable');
        if (!host) return;

        const bulk = canBulk();
        const head = (bulk ? '<th class="iv2-cbx-col"></th>' : '') + COLS.map(col => `
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
                    ${bulk ? `<td class="iv2-cbx-col">${isSelectable(invoice)
                        ? `<input type="checkbox" class="iv2-cbx" data-select="${invoice.id}"
                                  ${state.selected.has(invoice.id) ? 'checked' : ''}
                                  aria-label="Выбрать счёт">`
                        : ''}</td>` : ''}
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

        host.querySelectorAll('[data-open]').forEach(button => {
            button.addEventListener('click', () => openCard(Number(button.getAttribute('data-open'))));
        });

        host.querySelectorAll('[data-select]').forEach(box => {
            box.addEventListener('change', () => {
                const id = Number(box.getAttribute('data-select'));
                if (box.checked) state.selected.add(id);
                else state.selected.delete(id);
                renderBulkBar();
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
        renderBulkBar();
        if (state.view === 'queue') {
            renderQueue();
            if (state.queueActiveId && !state.details[state.queueActiveId]) {
                selectQueueInvoice(state.queueActiveId);
            }
        }
    }

    // =========================================================================
    // Детали счёта: загрузка и вспомогательные разборы
    // =========================================================================

    /**
     * Открытие счёта — ОДИН запрос вместо четырёх.
     * В очереди счета листаются стрелками, и на каждом шаге уходила бы пачка
     * «счёт + история + сообщения + вложения».
     */
    async function loadDetails(invoiceId, force) {
        if (!force && state.details[invoiceId]) return state.details[invoiceId];
        const data = await apiGet('/api/invoices/' + invoiceId + '?include=history,comments');
        state.details[invoiceId] = data;
        return data;
    }

    function refName(refKey, id) {
        if (id === null || id === undefined || id === '') return '';
        const item = (state.refs[refKey] || []).find(x => String(x.id) === String(id));
        return item ? item.name : '';
    }

    function digits(value) {
        return String(value || '').replace(/\D/g, '');
    }

    /**
     * Проверка реквизитов на очевидные опечатки.
     *
     * ВНИМАНИЕ: правила намеренно повторяют серверную
     * counterparty_requisite_warnings (storage.py) — они нужны мгновенно, без
     * запроса. Источник истины при сохранении и при отправке в банк остаётся
     * серверным. Меняете правила — правьте оба места вместе.
     */
    function requisiteWarnings(invoice) {
        const warnings = [];
        const checks = [
            ['counterparty_inn', [10, 12], 'ИНН из %n цифр — должно быть 10 (юрлицо) или 12 (ИП/физлицо)'],
            ['counterparty_kpp', [9], 'КПП из %n цифр — должно быть 9'],
            ['counterparty_bank_bik', [9], 'БИК из %n цифр — должно быть 9'],
            ['counterparty_bank_account', [20], 'Расчётный счёт из %n цифр — должно быть 20'],
            ['counterparty_bank_corr_account', [20], 'Корр. счёт из %n цифр — должно быть 20'],
        ];
        checks.forEach(([field, lengths, template]) => {
            const value = digits(invoice[field]);
            if (value && lengths.indexOf(value.length) === -1) {
                warnings.push(template.replace('%n', value.length));
            }
        });
        return warnings;
    }

    const BANK_FIELDS = [
        'counterparty_name', 'counterparty_inn', 'counterparty_bank_name',
        'counterparty_bank_bik', 'counterparty_bank_account', 'counterparty_bank_corr_account',
    ];

    function bankReady(invoice) {
        return BANK_FIELDS.every(field => String(invoice[field] || '').trim())
            && requisiteWarnings(invoice).length === 0;
    }

    // =========================================================================
    // Отрисовка: превью документа
    // =========================================================================

    function isImage(filename) {
        const name = String(filename || '').toLowerCase();
        return IMAGE_EXTENSIONS.some(ext => name.endsWith(ext));
    }

    function inlineUrl(attachmentId) {
        return '/api/invoices/attachments/' + attachmentId + '/inline';
    }

    /**
     * Зона «приложите счёт»: вставка из буфера, перетаскивание и обычный выбор
     * файла. Кнопка съёмки и подсказка про камеру — только там, где ввод
     * пальцем (класс iv2-only-touch, критерий `@media (pointer: coarse)`):
     * на узком окне десктопа камеры всё равно нет, и кнопка вела бы в никуда.
     */
    function dropZoneHtml(invoiceId) {
        return `
            <div class="iv2-drop" data-drop="${invoiceId}">
                <div class="iv2-drop__title">Приложите счёт</div>
                <div class="iv2-drop__hint">Вставьте скрин из буфера, перетащите файл сюда или выберите на диске.
                    <span class="iv2-only-touch">Можно сфотографировать счёт камерой.</span></div>
                <div class="iv2-drop__btns">
                    <button class="bx-btn bx-btn--ghost bx-btn--sm" type="button" data-act="pick-file">Выбрать файл</button>
                    <button class="bx-btn bx-btn--ghost bx-btn--sm iv2-only-touch" type="button" data-act="take-photo">Сфотографировать</button>
                </div>
                <input type="file" class="iv2-hidden-input" data-file-input="${invoiceId}"
                       accept="image/*,.pdf" multiple>
                <input type="file" class="iv2-hidden-input" data-camera-input="${invoiceId}"
                       accept="image/*" capture="environment">
            </div>`;
    }

    /** Миниатюры вместо списка имён — по картинке сразу видно, что приложено. */
    function thumbsHtml(details, activeIndex) {
        const attachments = details.attachments || [];
        if (attachments.length < 2) return '';
        const canDelete = state.user && state.user.role === 'admin';
        return `<div class="iv2-thumbs">${attachments.map((a, i) => `
            <div class="iv2-thumb${i === activeIndex ? ' iv2-thumb--on' : ''}">
                <button class="iv2-thumb__pick" type="button" data-doctab="${i}"
                        title="${escapeHtml(a.original_filename)}">
                    ${isImage(a.original_filename)
                        ? `<img src="${escapeHtml(inlineUrl(a.id))}" alt="${escapeHtml(a.original_filename)}">`
                        : `<span class="iv2-thumb__pdf">PDF<br>${escapeHtml(shortName(a.original_filename))}</span>`}
                </button>
                ${canDelete
                    ? `<button class="iv2-thumb__del" type="button" data-del-att="${a.id}"
                               aria-label="Удалить вложение" title="Удалить вложение">
                           <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                                stroke-width="2.5" stroke-linecap="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>
                       </button>`
                    : ''}
            </div>`).join('')}</div>`;
    }

    function shortName(filename) {
        const name = String(filename || '');
        return name.length > 14 ? name.slice(0, 12) + '…' : name;
    }

    function docHtml(details) {
        const attachments = details.attachments || [];
        const invoiceId = details.invoice.id;

        if (!attachments.length) {
            return `
                <div class="iv2-doc">
                    <div class="iv2-doc__bar"><span class="bx-card__title">Документ</span></div>
                    <div class="iv2-doc__empty">Скана нет — решение придётся принимать вслепую.</div>
                    ${dropZoneHtml(invoiceId)}
                </div>`;
        }

        const index = Math.min(state.docIndex, attachments.length - 1);
        const active = attachments[index];
        const downloadUrl = '/api/invoices/attachments/' + active.id + '/download';
        const canDelete = state.user && state.user.role === 'admin';

        const frame = isImage(active.original_filename)
            ? `<img src="${escapeHtml(inlineUrl(active.id))}" alt="${escapeHtml(active.original_filename)}">`
            : `<iframe class="iv2-doc__pdf" src="${escapeHtml(inlineUrl(active.id))}" title="Скан счёта"></iframe>`;

        return `
            <div class="iv2-doc">
                <div class="iv2-doc__bar">
                    <span class="bx-card__title">Документ</span>
                    <span class="iv2-doc__actions">
                        ${canDelete && attachments.length === 1
                            ? `<button class="bx-btn bx-btn--ghost bx-btn--sm" type="button"
                                       data-del-att="${active.id}">Удалить</button>`
                            : ''}
                        <a class="bx-btn bx-btn--ghost bx-btn--sm" href="${escapeHtml(downloadUrl)}"
                           target="_blank" rel="noopener">Скачать</a>
                    </span>
                </div>
                ${thumbsHtml(details, index)}
                <div class="iv2-doc__frame">${frame}</div>
                <div class="iv2-doc__name">${escapeHtml(active.original_filename)}</div>
                ${dropZoneHtml(invoiceId)}
            </div>`;
    }

    // =========================================================================
    // Вложения: сжатие, вставка, перетаскивание, загрузка
    // =========================================================================

    function fileExtension(name) {
        const match = /\.[a-z0-9]+$/i.exec(String(name || ''));
        return match ? match[0].toLowerCase() : '';
    }

    function timestampName(extension) {
        const now = new Date();
        const pad = (n) => String(n).padStart(2, '0');
        return `screenshot-${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}`
            + `-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}${extension}`;
    }

    /**
     * Сжатие фото перед загрузкой.
     *
     * createImageBitmap с imageOrientation:'from-image' обязателен: canvas сам
     * по себе игнорирует EXIF, и снятый телефоном счёт приезжает боком.
     * Маленькие файлы не трогаем — перекодировать чёткий скрин в JPEG значит
     * без нужды размыть текст счёта.
     */
    async function compressImage(file) {
        if (!isImage(file.name) || file.size <= COMPRESS_ABOVE_BYTES) return file;
        if (typeof createImageBitmap !== 'function') return file;

        try {
            const bitmap = await createImageBitmap(file, { imageOrientation: 'from-image' });
            const scale = Math.min(1, IMAGE_MAX_SIDE / Math.max(bitmap.width, bitmap.height));
            const width = Math.round(bitmap.width * scale);
            const height = Math.round(bitmap.height * scale);

            const canvas = document.createElement('canvas');
            canvas.width = width;
            canvas.height = height;
            canvas.getContext('2d').drawImage(bitmap, 0, 0, width, height);
            if (bitmap.close) bitmap.close();

            const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg', 0.85));
            if (!blob || blob.size >= file.size) return file;

            const base = String(file.name).replace(/\.[^.]+$/, '');
            return new File([blob], base + '.jpg', { type: 'image/jpeg' });
        } catch (error) {
            // Сжатие — оптимизация, а не условие работы: не вышло — грузим как есть
            console.warn('invoices-v2: не удалось сжать изображение, грузим оригинал:', error);
            return file;
        }
    }

    async function uploadAttachments(invoiceId, files) {
        const list = Array.from(files || []).filter(Boolean);
        if (!list.length) return;

        let uploaded = 0;
        const skipped = [];

        for (const original of list) {
            const extension = fileExtension(original.name);
            if (ATTACHMENT_EXTENSIONS.indexOf(extension) === -1) {
                skipped.push(`${original.name || 'файл'} — тип не поддерживается`);
                continue;
            }

            let file = original;
            try {
                file = await compressImage(original);
            } catch (error) {
                file = original;
            }

            if (file.size > MAX_ATTACHMENT_BYTES) {
                skipped.push(`${file.name} — больше 15 МБ`);
                continue;
            }

            const form = new FormData();
            // Имя файла ОБЯЗАТЕЛЬНО третьим аргументом: без него blob из буфера
            // уедет под именем «blob» без расширения, и сервер ответит
            // «Недопустимый тип файла» — он проверяет именно расширение.
            form.append('file', file, file.name);

            try {
                const response = await fetch(`/api/invoices/${invoiceId}/attachments`, {
                    method: 'POST',
                    credentials: 'include',
                    body: form,
                });
                const data = await response.json().catch(() => ({}));
                if (!response.ok) throw new Error(data.error || ('HTTP ' + response.status));
                uploaded += 1;
            } catch (error) {
                skipped.push(`${file.name} — ${error.message}`);
            }
        }

        if (uploaded) {
            toast(uploaded === 1 ? 'Файл приложен' : `Приложено файлов: ${uploaded}`, 'success');
            // Показываем только что приложенное
            const details = state.details[invoiceId];
            const before = details && details.attachments ? details.attachments.length : 0;
            state.docIndex = Math.max(0, before + uploaded - 1);
            await refreshDetails(invoiceId);
        }
        if (skipped.length) {
            toast('Не приложено: ' + skipped.join('; '), 'error');
        }
    }

    /** Какому счёту сейчас адресована вставка: открытой карточке или очереди. */
    function attachTargetId() {
        // Пока открыта форма счёта, вложениям адресата нет: у создаваемого
        // счёта ещё нет id, а тихо приложить файл к соседнему счёту — худшее,
        // что можно сделать
        if (state.form.open) return null;
        if (state.card.id && state.card.data) return state.card.id;
        if (state.view === 'queue' && state.queueActiveId && state.details[state.queueActiveId]) {
            return state.queueActiveId;
        }
        return null;
    }

    function filesFromClipboard(event) {
        const data = event.clipboardData;
        if (!data) return [];
        if (data.files && data.files.length) return Array.from(data.files);
        // Скрин из мессенджера часто приходит только как item, без files
        return Array.from(data.items || [])
            .filter(item => item.kind === 'file')
            .map(item => item.getAsFile())
            .filter(Boolean);
    }

    /**
     * Вставка и перетаскивание вешаются на document, поэтому обязаны сначала
     * убедиться, что открыт именно наш раздел: дашборд — одностраничное
     * приложение, и обработчик перехватил бы вставку картинки в кассовых
     * сменах или каталоге.
     */
    document.addEventListener('paste', function(event) {
        if (!pageIsActive()) return;
        const invoiceId = attachTargetId();
        if (!invoiceId) return;

        const files = filesFromClipboard(event);
        if (!files.length) return;  // обычная вставка текста — не мешаем

        event.preventDefault();
        uploadAttachments(invoiceId, files.map(file => (
            // У blob из буфера имени нет вовсе — задаём сами, иначе сервер
            // отвергнет файл по расширению
            file.name && fileExtension(file.name)
                ? file
                : new File([file], timestampName(file.type === 'application/pdf' ? '.pdf' : '.png'),
                           { type: file.type || 'image/png' })
        )));
    });

    function draggingFiles(event) {
        return Boolean(event.dataTransfer)
            && Array.from(event.dataTransfer.types || []).indexOf('Files') !== -1;
    }

    // preventDefault обязателен и здесь: без него событие drop до нас вообще не
    // дойдёт, браузер откроет файл в текущей вкладке и человек потеряет
    // страницу вместе с набранным сообщением. Поэтому перехватываем всегда,
    // пока открыт наш раздел, — даже если приложить файл сейчас некуда.
    document.addEventListener('dragover', function(event) {
        if (!pageIsActive() || !draggingFiles(event)) return;
        event.preventDefault();
        highlightDropZone(event.target.closest ? event.target.closest('[data-drop]') : null);
    });

    document.addEventListener('dragleave', function(event) {
        if (!pageIsActive()) return;
        if (event.target.closest && event.target.closest('[data-drop]')) return;
        highlightDropZone(null);
    });

    document.addEventListener('drop', function(event) {
        if (!pageIsActive() || !draggingFiles(event)) return;
        event.preventDefault();
        highlightDropZone(null);

        const invoiceId = attachTargetId();
        if (!invoiceId) {
            toast('Откройте счёт, чтобы приложить к нему файл');
            return;
        }
        if (event.dataTransfer.files && event.dataTransfer.files.length) {
            uploadAttachments(invoiceId, event.dataTransfer.files);
        }
    });

    function highlightDropZone(zone) {
        document.querySelectorAll('.iv2-drop--hot').forEach(el => el.classList.remove('iv2-drop--hot'));
        if (zone) zone.classList.add('iv2-drop--hot');
    }

    // =========================================================================
    // Отрисовка: лента событий и обсуждение
    // =========================================================================

    /**
     * Запись истории человеческим языком: «Сумма: 42 800 ₽ → 45 000 ₽».
     * В базе лежат сырые значения — id справочников, числа, коды статусов;
     * читать такое в ленте невозможно.
     */
    function historyText(entry) {
        const field = HISTORY_FIELDS[entry.field_name] || entry.field_name;
        const asLabel = (value) => {
            if (value === null || value === undefined || value === '') return '—';
            switch (entry.field_name) {
                case 'status': return (STATUSES[value] || {}).label || String(value);
                case 'amount': return money(value);
                case 'due_date': return fmtDue(value);
                case 'city_id': return refName('cities', value) || String(value);
                case 'payer_id': return refName('payers', value) || String(value);
                case 'vat_id': return refName('vatOptions', value) || String(value);
                case 'is_archived': return String(value) === '1' ? 'да' : 'нет';
                // «салон 6: 900.0» — id вместо названия читается не лучше кода
                case 'распределение': return String(value).replace(/салон (\d+)/g,
                    (match, id) => refName('stores', id) || match);
                default: return String(value);
            }
        };
        if (entry.old_value === null || entry.old_value === undefined || entry.old_value === '') {
            return `${field}: ${asLabel(entry.new_value)}`;
        }
        return `${field}: ${asLabel(entry.old_value)} → ${asLabel(entry.new_value)}`;
    }

    /**
     * Лента — слияние истории изменений и переписки, по времени.
     * Сообщения людей визуально отличаются от системных событий: иначе
     * вопрос «а точно ли этот счёт наш?» теряется среди смен статуса.
     */
    function feedHtml(details) {
        const events = [];
        const byName = {};
        (details.attachments || []).forEach(a => { byName[a.original_filename] = a; });

        (details.history || []).forEach(h => {
            // Приложенная картинка видна прямо в ленте: «вложение: scan.png»
            // без превью не отвечает на вопрос «то ли приложили».
            const attachment = h.field_name === 'вложение' ? byName[h.new_value] : null;
            events.push({
                at: h.changed_at,
                who: h.changed_by_full_name || h.changed_by,
                text: historyText(h),
                message: false,
                attachment: attachment && isImage(attachment.original_filename) ? attachment : null,
            });
        });
        (details.comments || []).forEach(c => events.push({
            at: c.created_at,
            who: c.author_full_name || c.author,
            text: c.message,
            message: true,
        }));

        if (!events.length) return '<div class="iv2-hint">Событий пока нет</div>';

        events.sort((a, b) => String(a.at).localeCompare(String(b.at)));

        return events.map(event => event.message
            ? `<div class="iv2-feed__item">
                   <span class="iv2-feed__dot iv2-feed__dot--msg"></span>
                   <div class="iv2-feed__msg">
                       <b>${escapeHtml(event.who)}</b>
                       <span class="iv2-feed__when"> · ${escapeHtml(fmtCreated(event.at))}</span>
                       <div class="iv2-feed__text">${escapeHtml(event.text)}</div>
                   </div>
               </div>`
            : `<div class="iv2-feed__item">
                   <span class="iv2-feed__dot"></span>
                   <div><b>${escapeHtml(event.who)}</b> ${escapeHtml(event.text)}
                       ${event.attachment
                           ? `<div class="iv2-feed__pic"><img src="${escapeHtml(inlineUrl(event.attachment.id))}"
                                   alt="${escapeHtml(event.attachment.original_filename)}"></div>`
                           : ''}
                       <div class="iv2-feed__when">${escapeHtml(fmtCreated(event.at))}</div></div>
               </div>`).join('');
    }

    // =========================================================================
    // Отрисовка: панель счёта (общая для очереди и карточки)
    // =========================================================================

    function lockReason(invoice) {
        if (invoice.is_archived) return 'Счёт в архиве — правки закрыты. Верните его из архива, чтобы редактировать.';
        if (invoice.status === 'sent_to_bank') return 'Платёжка уже ушла в банк — правки полей закрыты.';
        if (invoice.status === 'paid') return 'Счёт оплачен — правки закрыты.';
        return '';
    }

    /**
     * Действия над счётом. Ручного селекта статуса нет (решение №16): статус
     * двигают только эти кнопки, за каждой стоит решение.
     *
     * Кнопок, которых ещё нет на клиенте, здесь не рисуем — вместо них подпись,
     * в какой фазе они появятся. Кнопка, которая «ничего не делает», хуже
     * отсутствующей.
     */
    function actionsHtml(details) {
        const invoice = details.invoice;
        const isAdmin = state.user && state.user.role === 'admin';
        const buttons = [];

        // Правку показываем по ответу сервера, а не по своей копии правил:
        // can_edit_fields считает тот же can_edit_invoice_fields, что потом
        // и разрешит запрос
        if (details.can_edit_fields) {
            buttons.push(`<button class="bx-btn bx-btn--ghost" type="button" data-act="edit">Редактировать</button>`);
        }
        buttons.push(`<button class="bx-btn bx-btn--ghost" type="button" data-act="copy">Копировать счёт</button>`);

        if (isAdmin) {
            if (invoice.is_archived) {
                buttons.push(`<button class="bx-btn bx-btn--ghost" type="button" data-act="unarchive">Вернуть из архива</button>`);
            } else if (invoice.status === 'on_approval') {
                buttons.unshift(`<button class="bx-btn bx-btn--danger" type="button" data-act="reject">Отклонить</button>`);
                buttons.unshift(`<button class="bx-btn bx-btn--ok" type="button" data-act="approve">Согласовать</button>`);
            } else if (invoice.status === 'approved' || invoice.status === 'sent_to_bank') {
                buttons.unshift(`<button class="bx-btn bx-btn--ok" type="button" data-act="paid">Отметить оплаченным</button>`);
            }
            if (!invoice.is_archived && (invoice.status === 'paid' || invoice.status === 'rejected')) {
                buttons.push(`<button class="bx-btn bx-btn--ghost" type="button" data-act="archive">В архив</button>`);
            }
        }

        const soon = [];
        if (isAdmin && !invoice.is_archived && invoice.status === 'on_approval') soon.push('«Вернуть на доработку» — Фаза 7');
        if (isAdmin && invoice.status === 'approved') soon.push('«Отправить в банк» — Фаза 6');

        return buttons.join('')
            + (soon.length ? `<div class="iv2-hint iv2-soon">Ещё приедет: ${escapeHtml(soon.join(' · '))}</div>` : '');
    }

    function paneHtml(details, compact) {
        const invoice = details.invoice;
        const warnings = requisiteWarnings(invoice);
        const items = details.line_items || [];

        const allocRows = items.length
            ? items.map(item => `
                <div class="iv2-alloc__row">
                    <span>${escapeHtml(item.store_name || '—')} · ${escapeHtml(item.category_name || '—')}</span>
                    <span>${escapeHtml(money(item.amount))}</span>
                </div>`).join('')
            : '<div class="iv2-alloc__row"><span class="iv2-hint">не распределён</span><span></span></div>';

        const requisiteRows = [
            ['ИНН', invoice.counterparty_inn],
            ['КПП', invoice.counterparty_kpp],
            ['Банк', invoice.counterparty_bank_name],
            ['БИК', invoice.counterparty_bank_bik],
            ['Расчётный счёт', invoice.counterparty_bank_account],
            ['Корр. счёт', invoice.counterparty_bank_corr_account],
        ].map(([label, value]) => `<dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value || '—')}</dd>`).join('');

        const requisiteBadge = warnings.length
            ? `<span class="bx-badge b-warn">${withCount(warnings.length, 'замечание', 'замечания', 'замечаний')}</span>`
            : (bankReady(invoice)
                ? '<span class="bx-badge b-appr">полные</span>'
                : '<span class="bx-badge b-rej">неполные</span>');

        const locked = lockReason(invoice);
        const draft = state.commentDrafts[invoice.id] || '';

        return `
            <div class="iv2-pane">
                <div class="iv2-pane__head">
                    <div>
                        <div class="iv2-pane__title">
                            <span class="bx-card__title">${escapeHtml(invoice.invoice_number || ('#' + invoice.id))}</span>
                            ${statusBadge(invoice)}
                        </div>
                        <div class="iv2-hint">${escapeHtml(invoice.counterparty_name || 'без контрагента')} ·
                            ИНН ${escapeHtml(invoice.counterparty_inn || '—')}</div>
                    </div>
                </div>

                <div class="iv2-pane__amount">${escapeHtml(money(invoice.amount))}</div>
                <div class="iv2-pane__purpose">${escapeHtml(invoice.payment_purpose || '')}</div>

                <dl class="iv2-rows">
                    <dt>Срок оплаты</dt><dd>${escapeHtml(fmtDue(invoice.due_date))} ${dueBadge(invoice)}</dd>
                    <dt>Город</dt><dd>${escapeHtml(invoice.city_name || refName('cities', invoice.city_id) || '—')}</dd>
                    <dt>На кого выставлен</dt><dd>${escapeHtml(invoice.payer_name || refName('payers', invoice.payer_id) || '—')}</dd>
                    <dt>НДС</dt><dd>${escapeHtml(refName('vatOptions', invoice.vat_id) || '—')}</dd>
                    <dt>Автор</dt><dd>${escapeHtml(invoice.created_by_full_name || invoice.created_by || '—')} ·
                        ${escapeHtml(fmtCreated(invoice.created_at))}</dd>
                    ${invoice.comment ? `<dt>Комментарий</dt><dd>${escapeHtml(invoice.comment)}</dd>` : ''}
                </dl>

                <div class="iv2-reqbox">
                    <button class="iv2-reqbox__head" type="button" data-act="toggle-req">
                        <span>Реквизиты контрагента ${requisiteBadge}</span>
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                             stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg>
                    </button>
                    <div class="iv2-reqbox__body">
                        <dl class="iv2-rows iv2-rows--req">${requisiteRows}</dl>
                        ${warnings.length
                            ? `<div class="iv2-warn">${warnings.map(w => escapeHtml(w)).join('<br>')}</div>`
                            : ''}
                    </div>
                </div>

                <div class="iv2-alloc">
                    <div class="iv2-alloc__row"><span>Салон · статья</span><span>Сумма</span></div>
                    ${allocRows}
                </div>

                <div class="iv2-feed">${feedHtml(details)}</div>

                <div class="iv2-talk">
                    <textarea class="iv2-textarea" rows="2" data-talk="${invoice.id}"
                        placeholder="Написать сообщение по счёту — автор и согласующий увидят его здесь">${escapeHtml(draft)}</textarea>
                    <button class="bx-btn bx-btn--ghost" type="button" data-act="send-comment">Отправить</button>
                </div>
                <div class="iv2-hint">Обсуждение доступно на любом этапе, включая архив. В банк не уходит.
                    <kbd>Ctrl</kbd>+<kbd>Enter</kbd> отправляет.</div>

                ${locked ? `<div class="iv2-locked">${escapeHtml(locked)}</div>` : ''}

                <div class="iv2-pane__actions">${actionsHtml(details)}</div>
                ${compact ? '' : `
                    <div class="iv2-hint iv2-hotkeys">
                        <kbd>A</kbd> согласовать · <kbd>R</kbd> отклонить ·
                        <kbd>↑</kbd><kbd>↓</kbd> следующий счёт
                    </div>`}
            </div>`;
    }

    /**
     * Обработчики панели. Вешаются заново после каждой перерисовки — панель
     * пересобирается целиком, старые узлы вместе с обработчиками исчезают.
     */
    function bindPane(root, details) {
        const invoiceId = details.invoice.id;

        root.querySelectorAll('[data-act]').forEach(button => {
            button.addEventListener('click', () => doAction(button.getAttribute('data-act'), invoiceId, button));
        });

        root.querySelectorAll('[data-doctab]').forEach(button => {
            button.addEventListener('click', () => {
                state.docIndex = Number(button.getAttribute('data-doctab'));
                if (state.card.id === invoiceId) renderCard(); else renderQueue();
            });
        });

        root.querySelectorAll('[data-talk]').forEach(textarea => {
            textarea.addEventListener('input', () => {
                state.commentDrafts[invoiceId] = textarea.value;
            });
            textarea.addEventListener('keydown', event => {
                if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
                    event.preventDefault();
                    doAction('send-comment', invoiceId, textarea);
                }
            });
        });

        root.querySelectorAll('[data-file-input], [data-camera-input]').forEach(input => {
            input.addEventListener('change', () => {
                const files = input.files;
                // Сбрасываем значение сразу: иначе повторный выбор того же
                // файла не даст события change
                input.value = '';
                uploadAttachments(invoiceId, files);
            });
        });

        root.querySelectorAll('[data-del-att]').forEach(button => {
            button.addEventListener('click', () => deleteAttachment(invoiceId, Number(button.getAttribute('data-del-att'))));
        });
    }

    async function deleteAttachment(invoiceId, attachmentId) {
        const details = state.details[invoiceId];
        const attachment = (details && details.attachments || []).find(a => a.id === attachmentId);
        const ok = await window.BarhatUI.confirm(
            `Файл «${attachment ? attachment.original_filename : ''}» будет удалён с диска безвозвратно.`,
            { title: 'Удалить вложение?', confirmText: 'Удалить', danger: true });
        if (!ok) return;

        try {
            const response = await fetch(`/api/invoices/attachments/${attachmentId}`, {
                method: 'DELETE',
                credentials: 'include',
            });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(data.error || ('HTTP ' + response.status));
            state.docIndex = 0;
            toast('Вложение удалено');
            await refreshDetails(invoiceId);
        } catch (error) {
            toast('Не удалось удалить: ' + error.message, 'error');
        }
    }

    // =========================================================================
    // Действия над счётом
    // =========================================================================

    async function apiPost(path, body) {
        const response = await fetch(path, {
            method: 'POST',
            credentials: 'include',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body || {}),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.error || ('HTTP ' + response.status));
        return data;
    }

    async function doAction(action, invoiceId, sourceElement) {
        const details = state.details[invoiceId];
        if (!details) return;

        if (action === 'toggle-req') {
            const box = sourceElement.closest('.iv2-reqbox');
            if (box) box.classList.toggle('iv2-reqbox--open');
            return;
        }

        if (action === 'edit' || action === 'copy') {
            openForm(action === 'edit' ? 'edit' : 'copy', invoiceId);
            return;
        }

        // Скрытый input открываем кликом: свой вид у него не настраивается,
        // а системный выглядит чужеродно рядом с кнопками раздела
        if (action === 'pick-file' || action === 'take-photo') {
            const zone = sourceElement.closest('[data-drop]');
            const input = zone && zone.querySelector(
                action === 'pick-file' ? '[data-file-input]' : '[data-camera-input]');
            if (input) input.click();
            return;
        }

        if (action === 'send-comment') {
            const text = (state.commentDrafts[invoiceId] || '').trim();
            if (!text) {
                toast('Сообщение пустое');
                return;
            }
            try {
                await apiPost(`/api/invoices/${invoiceId}/comments`, { message: text });
                state.commentDrafts[invoiceId] = '';
                await refreshDetails(invoiceId);
                toast('Сообщение отправлено');
            } catch (error) {
                toast('Не удалось отправить: ' + error.message, 'error');
            }
            return;
        }

        // Нативные confirm/prompt в iframe Пульса молча игнорируются —
        // кнопка выглядела бы сломанной. Только window.BarhatUI.
        const invoice = details.invoice;
        const label = invoice.invoice_number || ('#' + invoice.id);

        try {
            if (action === 'approve') {
                const ok = await window.BarhatUI.confirm(
                    `${label} · ${invoice.counterparty_name || 'без контрагента'} · ${money(invoice.amount)}`,
                    { title: 'Согласовать счёт?', confirmText: 'Согласовать' });
                if (!ok) return;
                await apiPost(`/api/invoices/${invoiceId}/approve`);
                toast('Счёт согласован', 'success');
            } else if (action === 'reject') {
                const reason = await window.BarhatUI.prompt(
                    `${label} · ${money(invoice.amount)}. Причину можно не указывать, но с ней автору понятнее.`,
                    '', { title: 'Отклонить счёт?', confirmText: 'Отклонить', placeholder: 'Причина отказа' });
                if (reason === null) return;
                await apiPost(`/api/invoices/${invoiceId}/reject`, { reason: reason || null });
                toast('Счёт отклонён и отправлен в архив');
            } else if (action === 'paid') {
                const ok = await window.BarhatUI.confirm(
                    `${label} · ${money(invoice.amount)}. Полностью распределённый счёт уйдёт в архив сам.`,
                    { title: 'Отметить оплаченным?', confirmText: 'Отметить' });
                if (!ok) return;
                await apiPost(`/api/invoices/${invoiceId}/mark-paid`);
                toast('Счёт отмечен оплаченным', 'success');
            } else if (action === 'archive') {
                const ok = await window.BarhatUI.confirm(`${label} уйдёт в архив.`,
                    { title: 'В архив?', confirmText: 'В архив' });
                if (!ok) return;
                await apiPost(`/api/invoices/${invoiceId}/archive`);
                toast('Счёт в архиве');
            } else if (action === 'unarchive') {
                await apiPost(`/api/invoices/${invoiceId}/unarchive`);
                toast('Счёт возвращён из архива');
            } else {
                return;
            }
        } catch (error) {
            toast('Не получилось: ' + error.message, 'error');
            return;
        }

        // Действие сдвинуло статус: перечитываем и счёт, и список, и плитки —
        // счёт мог уйти из очереди или вовсе в архив.
        await refreshDetails(invoiceId);
        loadSummary();
        loadList(false);
    }

    async function refreshDetails(invoiceId) {
        try {
            await loadDetails(invoiceId, true);
        } catch (error) {
            console.error('invoices-v2: не удалось перечитать счёт:', error);
        }
        if (state.card.id === invoiceId) {
            state.card.data = state.details[invoiceId];
            renderCard();
        }
        if (state.view === 'queue') renderQueue();
    }

    // =========================================================================
    // Отрисовка: вид «Очередь»
    // =========================================================================

    /**
     * Что показывать в очереди. По умолчанию — то, что ждёт согласующего;
     * но если человек сам выбрал статус или плитку, фильтр главнее режима,
     * иначе счёт «пропадает» и непонятно почему.
     */
    function queueRows() {
        if (state.filters.status || state.kpiFilter || state.showArchived) return state.rows;
        return state.rows.filter(invoice => invoice.status === 'on_approval');
    }

    function renderQueue() {
        const host = $('iv2ViewQueue');
        if (!host) return;

        const rows = queueRows();
        if (!rows.length) {
            host.innerHTML = `<div class="iv2-placeholder">
                <h3 class="iv2-placeholder__title">${state.loading ? 'Загрузка…' : 'Очередь пуста'}</h3>
                <p>${state.loading ? '' : 'Под текущие фильтры счетов на согласовании нет.'}</p>
            </div>`;
            return;
        }

        if (!state.queueActiveId || !rows.some(r => r.id === state.queueActiveId)) {
            state.queueActiveId = rows[0].id;
            state.docIndex = 0;
        }

        const items = rows.map(invoice => {
            const waiting = daysSince(invoice.created_at);
            return `
                <button class="iv2-qitem${invoice.id === state.queueActiveId ? ' iv2-qitem--active' : ''}"
                        type="button" data-queue="${invoice.id}">
                    <span class="iv2-qitem__top">
                        <span class="iv2-qitem__cp">${escapeHtml(invoice.counterparty_name || 'без контрагента')}</span>
                        <span class="iv2-qitem__amt">${escapeHtml(money(invoice.amount))}</span>
                    </span>
                    <span class="iv2-qitem__meta">
                        <span>${escapeHtml(invoice.invoice_number || ('#' + invoice.id))} ·
                            ${escapeHtml(invoice.city_name || '—')}${waiting === null ? '' : ' · ждёт ' + withCount(waiting, 'день', 'дня', 'дней')}</span>
                        ${dueBadge(invoice)}
                    </span>
                </button>`;
        }).join('');

        const rest = Math.max(0, state.total - state.rows.length);
        const more = rest > 0
            ? `<button class="bx-btn bx-btn--ghost bx-btn--sm iv2-queue__more" type="button" id="iv2QueueMore">
                   Показать ещё · осталось ${rest}</button>`
            : '';

        const details = state.details[state.queueActiveId];
        const pane = details
            ? `${docHtml(details)}${paneHtml(details, false)}`
            : `<div class="iv2-placeholder"><p>Загрузка счёта…</p></div>`;

        host.innerHTML = `
            <div class="iv2-queue">
                <div class="iv2-queue__list">${items}${more}</div>
                <div class="iv2-queue__pane">${pane}</div>
            </div>`;

        host.querySelectorAll('[data-queue]').forEach(button => {
            button.addEventListener('click', () => selectQueueInvoice(Number(button.getAttribute('data-queue'))));
        });
        const moreButton = $('iv2QueueMore');
        if (moreButton) moreButton.addEventListener('click', () => loadList(true));

        if (details) bindPane(host, details);
    }

    async function selectQueueInvoice(invoiceId) {
        state.queueActiveId = invoiceId;
        state.docIndex = 0;
        renderQueue();
        try {
            await loadDetails(invoiceId);
        } catch (error) {
            toast('Не удалось открыть счёт: ' + error.message, 'error');
        }
        if (state.queueActiveId === invoiceId) renderQueue();
    }

    function stepQueue(delta) {
        const rows = queueRows();
        const index = rows.findIndex(r => r.id === state.queueActiveId);
        const next = rows[index + delta];
        if (next) selectQueueInvoice(next.id);
    }

    // =========================================================================
    // Отрисовка: карточка счёта (модалка) и адрес страницы
    // =========================================================================

    function renderCard() {
        const host = modalHost('card');
        if (!host) return;

        if (!state.card.id) {
            host.innerHTML = '';
            return;
        }

        let body;
        if (state.card.error) {
            body = `
                <div class="iv2-card-message">
                    <h3 class="iv2-placeholder__title">${escapeHtml(state.card.error.title)}</h3>
                    <p>${escapeHtml(state.card.error.text)}</p>
                </div>`;
        } else if (!state.card.data) {
            body = '<div class="iv2-card-message"><p>Загрузка счёта…</p></div>';
        } else {
            body = `
                <div class="iv2-card-grid">
                    ${docHtml(state.card.data)}
                    ${paneHtml(state.card.data, true)}
                </div>`;
        }

        host.innerHTML = `
            <div class="iv2-ovl" id="iv2CardOverlay"></div>
            <div class="iv2-modal">
                <div class="iv2-modal__box">
                    <div class="iv2-modal__head">
                        <h3 class="bx-card__title">Счёт</h3>
                        <button class="iv2-x" type="button" id="iv2CardClose" aria-label="Закрыть">
                            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                                 stroke-width="1.75" stroke-linecap="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>
                        </button>
                    </div>
                    <div class="iv2-modal__body">${body}</div>
                </div>
            </div>`;

        const close = $('iv2CardClose');
        if (close) close.addEventListener('click', closeCard);
        const overlay = $('iv2CardOverlay');
        if (overlay) overlay.addEventListener('click', closeCard);

        if (state.card.data) bindPane(host, state.card.data);
    }

    /** Адрес карточки в браузере: «Назад» закрывает её, обновление — возвращает. */
    function cardUrl(invoiceId) {
        const embed = new URLSearchParams(window.location.search).get('embed');
        return '/invoices-v2?invoice=' + invoiceId + (embed ? '&embed=' + encodeURIComponent(embed) : '');
    }

    async function openCard(invoiceId, fromUrl) {
        state.card = { id: invoiceId, data: state.details[invoiceId] || null, loading: true, error: null };
        state.docIndex = 0;

        if (!fromUrl) {
            history.pushState({ page: 'invoices_v2', invoice: invoiceId }, '', cardUrl(invoiceId));
            state.cardPushed = true;
        }

        renderCard();

        try {
            const data = await loadDetails(invoiceId);
            if (state.card.id !== invoiceId) return;
            state.card.data = data;
        } catch (error) {
            if (state.card.id !== invoiceId) return;
            // Внятный экран вместо тоста «Счёт не найден»: человек пришёл
            // по ссылке и должен понять, что именно не так.
            const message = String(error.message || '');
            state.card.error = /нет доступа/i.test(message)
                ? { title: 'Нет доступа к этому счёту', text: 'Счёт заведён по другому салону. Попросите доступ у администратора.' }
                : { title: 'Счёт не найден', text: 'Возможно, его удалили или в ссылке опечатка.' };
        } finally {
            if (state.card.id === invoiceId) {
                state.card.loading = false;
                renderCard();
            }
        }
    }

    function closeCard() {
        if (!state.card.id) return;
        state.card = { id: null, data: null, loading: false, error: null };
        renderCard();

        if (state.cardPushed) {
            state.cardPushed = false;
            history.back();
        } else {
            // Карточку открыли прямой ссылкой — своей записи в истории нет,
            // и history.back() увёл бы человека вообще из раздела.
            history.replaceState({ page: 'invoices_v2' }, '', '/invoices-v2' + window.location.search.replace(/[?&]invoice=\d+/, '').replace(/^&/, '?'));
        }
    }

    // =========================================================================
    // Массовые действия
    // =========================================================================

    /** Выбранные счета из текущей страницы: со снятых фильтром отметку снимаем. */
    function selectedInvoices() {
        const byId = {};
        state.rows.forEach(invoice => { byId[invoice.id] = invoice; });
        const alive = [];
        state.selected.forEach(id => { if (byId[id]) alive.push(byId[id]); });
        if (alive.length !== state.selected.size) {
            state.selected = new Set(alive.map(invoice => invoice.id));
        }
        return alive;
    }

    function renderBulkBar() {
        const host = modalHost('bulk');
        if (!host) return;

        const chosen = selectedInvoices();
        if (!canBulk() || state.view !== 'table' || !chosen.length) {
            host.innerHTML = '';
            return;
        }

        const total = chosen.reduce((sum, invoice) => sum + (Number(invoice.amount) || 0), 0);

        // Кнопка появляется, если под неё подходит хотя бы один выбранный счёт,
        // и показывает, скольких именно касается: при смешанном выборе иначе
        // непонятно, что произойдёт.
        const buttons = BULK_ACTIONS.map(action => {
            const count = chosen.filter(invoice => action.statuses.indexOf(invoice.status) !== -1).length;
            if (!count) return '';
            return `<button class="bx-btn bx-btn--sm ${action.style}" type="button"
                            data-bulk="${action.key}">${escapeHtml(action.label)} · ${count}</button>`;
        }).join('');

        host.innerHTML = `
            <div class="iv2-bulkbar">
                <span class="iv2-bulkbar__txt">Выбрано <b>${chosen.length}</b> ·
                    <span class="iv2-bulkbar__sum">${escapeHtml(money(total))}</span></span>
                <span class="iv2-bulkbar__actions">${buttons || '<span class="iv2-bulkbar__txt">Для выбранных статусов массовых действий нет</span>'}</span>
                <button class="bx-btn bx-btn--sm iv2-bulkbar__clear" type="button" id="iv2BulkClear">Снять выбор</button>
            </div>`;

        host.querySelectorAll('[data-bulk]').forEach(button => {
            button.addEventListener('click', () => runBulkAction(button.getAttribute('data-bulk')));
        });
        const clear = $('iv2BulkClear');
        if (clear) {
            clear.addEventListener('click', () => {
                state.selected.clear();
                renderTable();
                renderBulkBar();
            });
        }
    }

    function invoiceListText(invoices, limit) {
        const shown = invoices.slice(0, limit || 12)
            .map(invoice => invoice.invoice_number || ('#' + invoice.id));
        const rest = invoices.length - shown.length;
        return shown.join(', ') + (rest > 0 ? ` и ещё ${rest}` : '');
    }

    async function runBulkAction(actionKey) {
        const action = BULK_ACTIONS.find(a => a.key === actionKey);
        if (!action) return;

        const chosen = selectedInvoices().filter(invoice => action.statuses.indexOf(invoice.status) !== -1);
        if (!chosen.length) return;

        const ids = chosen.map(invoice => invoice.id);
        const total = chosen.reduce((sum, invoice) => sum + (Number(invoice.amount) || 0), 0);

        // Подтверждение со списком номеров и итоговой суммой — обязательно для
        // всех трёх действий: пачка в 20 счетов уходит одним кликом, и ошибку
        // заметят поздно.
        const texts = {
            approve: {
                title: 'Согласовать счета?',
                confirm: 'Согласовать',
                body: `${withCount(chosen.length, 'счёт', 'счёта', 'счетов')} на ${money(total)}:\n${invoiceListText(chosen)}`,
            },
            paid: {
                title: 'Отметить оплаченными?',
                confirm: 'Отметить',
                body: `${withCount(chosen.length, 'счёт', 'счёта', 'счетов')} на ${money(total)}:\n${invoiceListText(chosen)}\n\n`
                    + 'Полностью распределённые счета уйдут в архив сами и пропадут из списка.',
            },
            bank: {
                title: 'Отправить платёжки в банк?',
                confirm: 'Отправить',
                body: `${withCount(chosen.length, 'счёт', 'счёта', 'счетов')} на ${money(total)}:\n${invoiceListText(chosen)}\n\n`
                    + 'Банк создаст черновики платёжек — подписывать их всё равно нужно вручную в личном кабинете. '
                    + 'Счета с неполными реквизитами будут пропущены и названы поимённо.',
            },
        };
        const text = texts[actionKey];
        const ok = await window.BarhatUI.confirm(text.body, {
            title: text.title,
            confirmText: text.confirm,
            danger: actionKey === 'bank',
        });
        if (!ok) return;

        const paths = {
            approve: '/api/invoices/bulk-approve',
            paid: '/api/invoices/bulk-mark-paid',
            bank: '/api/invoices/bulk-send-to-bank',
        };

        try {
            const data = await apiPost(paths[actionKey], { ids });
            state.selected.clear();
            showBulkReport(actionKey, data);
        } catch (error) {
            // Пакет мог быть отклонён целиком (например, в банк отправлять
            // нечего) — сервер в этом случае называет причины поимённо
            showBulkReport(actionKey, { error: error.message });
        }

        loadSummary();
        loadList(false);
    }

    function reportSection(title, items, withAmount) {
        if (!items || !items.length) return '';
        return `
            <div class="iv2-report__section">
                <div class="iv2-report__title">${escapeHtml(title)} — ${items.length}</div>
                <ul class="iv2-report__list">
                    ${items.map(item => `<li><b>${escapeHtml(item.label || ('#' + item.id))}</b>${
                        withAmount && item.amount !== undefined ? ' · ' + escapeHtml(money(item.amount)) : ''
                    }${item.reason ? ' — ' + escapeHtml(item.reason) : ''}</li>`).join('')}
                </ul>
            </div>`;
    }

    function showBulkReport(actionKey, data) {
        state.report = { actionKey, data };
        renderBulkReport();
    }

    function renderBulkReport() {
        const host = modalHost('report');
        if (!host) return;
        if (!state.report) {
            host.innerHTML = '';
            return;
        }

        const { actionKey, data } = state.report;
        let body;

        if (data.error) {
            body = `<div class="iv2-form-error">${escapeHtml(data.error)}</div>`
                + reportSection('Не отправлены', data.skipped);
        } else if (actionKey === 'approve') {
            body = `<div class="iv2-report__head">Согласовано ${withCount((data.approved || []).length, 'счёт', 'счёта', 'счетов')}
                        на ${escapeHtml(money(data.approved_amount || 0))}</div>`
                + reportSection('Согласованы', data.approved, true)
                + reportSection('Пропущены', data.skipped);
        } else if (actionKey === 'paid') {
            body = `<div class="iv2-report__head">Отмечено оплаченными ${withCount((data.paid || []).length, 'счёт', 'счёта', 'счетов')}
                        на ${escapeHtml(money(data.paid_amount || 0))}${
                        data.archived_count ? `, из них ${data.archived_count} ушли в архив автоматически` : ''}</div>`
                + reportSection('Оплачены', data.paid, true)
                + reportSection('Пропущены', data.skipped);
        } else {
            body = `<div class="iv2-report__head">${data.sandbox ? 'Тестовый контур. ' : ''}Отправлено
                        ${withCount((data.sent || []).length, 'платёжка', 'платёжки', 'платёжек')}
                        на ${escapeHtml(money(data.sent_amount || 0))}</div>`
                + reportSection('Ушли в банк', data.sent, true)
                + reportSection('Банк отклонил', data.failed)
                + reportSection('Пропущены до отправки', data.skipped)
                + reportSection('Не успели за отведённое время', data.postponed);
        }

        host.innerHTML = `
            <div class="iv2-ovl iv2-ovl--report" id="iv2ReportOverlay"></div>
            <div class="iv2-modal iv2-modal--report">
                <div class="iv2-modal__box iv2-modal__box--narrow">
                    <div class="iv2-modal__head">
                        <h3 class="bx-card__title">Результат</h3>
                        <button class="iv2-x" type="button" id="iv2ReportClose" aria-label="Закрыть">
                            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                                 stroke-width="1.75" stroke-linecap="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>
                        </button>
                    </div>
                    <div class="iv2-modal__body">${body}</div>
                    <div class="iv2-modal__foot">
                        <button class="bx-btn" type="button" id="iv2ReportOk">Понятно</button>
                    </div>
                </div>
            </div>`;

        const close = () => { state.report = null; renderBulkReport(); };
        [$('iv2ReportClose'), $('iv2ReportOk'), $('iv2ReportOverlay')].forEach(element => {
            if (element) element.addEventListener('click', close);
        });
    }

    // =========================================================================
    // Форма счёта: создание, копирование, правка
    // =========================================================================

    const COUNTERPARTY_FIELDS = [
        { key: 'counterparty_name', label: 'Контрагент', from: 'name' },
        { key: 'counterparty_inn', label: 'ИНН', from: 'inn', digits: [10, 12] },
        { key: 'counterparty_kpp', label: 'КПП', from: 'kpp', digits: [9] },
        { key: 'counterparty_bank_name', label: 'Банк', from: 'bank_name' },
        { key: 'counterparty_bank_bik', label: 'БИК', from: 'bank_bik', digits: [9] },
        { key: 'counterparty_bank_account', label: 'Расчётный счёт', from: 'bank_account', digits: [20] },
        { key: 'counterparty_bank_corr_account', label: 'Корр. счёт', from: 'bank_corr_account', digits: [20] },
    ];

    const FORM_TITLES = {
        create: 'Новый счёт',
        copy: 'Копия счёта',
        edit: 'Редактирование счёта',
    };

    function emptyForm() {
        const values = {
            city_id: '', payer_id: '', vat_id: '', due_date: '',
            amount: '', payment_purpose: '', comment: '',
        };
        COUNTERPARTY_FIELDS.forEach(f => { values[f.key] = ''; });
        return {
            open: false,
            mode: 'create',
            invoiceId: null,
            values,
            items: [],
            openSections: { main: true, requisites: false, allocation: true },
            suggestions: [],
            source: '',
            saving: false,
            error: '',
        };
    }

    state.form = emptyForm();
    let counterpartySearchTimer = null;

    /**
     * Модалки живут в двух отдельных хостах внутри #iv2Modals: карточка и форма
     * могут быть открыты одновременно (правка вызывается из карточки), а общий
     * innerHTML стирал бы одну при перерисовке другой.
     */
    const MODAL_HOSTS = {
        card: 'iv2CardHost',
        form: 'iv2FormHost',
        bulk: 'iv2BulkHost',
        report: 'iv2ReportHost',
    };

    function modalHost(name) {
        const root = $('iv2Modals');
        if (!root) return null;
        const id = MODAL_HOSTS[name];
        let host = $(id);
        if (!host) {
            host = document.createElement('div');
            host.id = id;
            root.appendChild(host);
        }
        return host;
    }

    function openForm(mode, invoiceId) {
        const form = emptyForm();
        form.open = true;
        form.mode = mode;

        if (mode === 'create') {
            form.values.due_date = today();
            form.items = [{ store_id: '', expense_category_id: '', amount: '' }];
        } else {
            const details = state.details[invoiceId];
            if (!details) {
                toast('Счёт ещё не загрузился, попробуйте ещё раз');
                return;
            }
            const invoice = details.invoice;
            form.invoiceId = mode === 'edit' ? invoiceId : null;
            form.values.city_id = invoice.city_id || '';
            form.values.payer_id = invoice.payer_id || '';
            form.values.vat_id = invoice.vat_id || '';
            form.values.payment_purpose = invoice.payment_purpose || '';
            form.values.comment = invoice.comment || '';
            COUNTERPARTY_FIELDS.forEach(f => { form.values[f.key] = invoice[f.key] || ''; });

            if (mode === 'edit') {
                form.values.due_date = invoice.due_date || '';
                form.values.amount = invoice.amount;
                form.items = (details.line_items || []).map(item => ({
                    store_id: item.store_id,
                    expense_category_id: item.expense_category_id,
                    amount: item.amount,
                }));
            } else {
                // Копия: сумма и суммы строк — ровно то, что меняется от счёта
                // к счёту, поэтому переносим только разрезы
                form.values.due_date = today();
                form.values.amount = '';
                form.items = (details.line_items || []).map(item => ({
                    store_id: item.store_id,
                    expense_category_id: item.expense_category_id,
                    amount: '',
                }));
            }
            if (!form.items.length) form.items = [{ store_id: '', expense_category_id: '', amount: '' }];
        }

        state.form = form;
        renderForm();
    }

    function closeForm() {
        state.form = emptyForm();
        renderForm();
    }

    function formAllocated() {
        return state.form.items.reduce((sum, item) => sum + (Number(item.amount) || 0), 0);
    }

    function formAmount() {
        return Number(state.form.values.amount) || 0;
    }

    /** Претензии к реквизитам прямо в форме — те же правила, что на сервере. */
    function formRequisiteIssues() {
        const issues = {};
        COUNTERPARTY_FIELDS.filter(f => f.digits).forEach(f => {
            const value = digits(state.form.values[f.key]);
            if (value && f.digits.indexOf(value.length) === -1) {
                issues[f.key] = `${f.label}: ${value.length} ${plural(value.length, 'цифра', 'цифры', 'цифр')}, `
                    + `нужно ${f.digits.join(' или ')}`;
            }
        });
        return issues;
    }

    function requisitesFilled() {
        return BANK_FIELDS.every(field => String(state.form.values[field] || '').trim());
    }

    function selectOptions(items, current, anyLabel) {
        return `<option value="">${escapeHtml(anyLabel)}</option>` + (items || []).map(item =>
            `<option value="${escapeHtml(item.id)}"${String(item.id) === String(current) ? ' selected' : ''}>${escapeHtml(item.name)}</option>`
        ).join('');
    }

    function allocationRowsHtml() {
        return state.form.items.map((item, index) => `
            <div class="iv2-arow">
                <select class="iv2-select" data-item="${index}" data-item-field="store_id">
                    ${selectOptions(state.refs.stores, item.store_id, 'Салон / проект')}
                </select>
                <select class="iv2-select" data-item="${index}" data-item-field="expense_category_id">
                    ${selectOptions(state.refs.categories, item.expense_category_id, 'Статья расхода')}
                </select>
                <input class="iv2-input" type="number" step="0.01" min="0" placeholder="Сумма"
                       data-item="${index}" data-item-field="amount"
                       value="${escapeHtml(item.amount === '' || item.amount === null ? '' : item.amount)}">
                <button class="iv2-arow__del" type="button" data-item-del="${index}"
                        aria-label="Удалить строку" title="Удалить строку">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                         stroke-width="1.75" stroke-linecap="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>
                </button>
            </div>`).join('');
    }

    function allocationSummaryHtml() {
        const amount = formAmount();
        const allocated = formAllocated();
        const rest = Math.round((amount - allocated) * 100) / 100;
        const ok = amount > 0 && Math.abs(rest) < 0.01;
        return `
            <div class="iv2-alloc-total ${ok ? 'iv2-alloc-total--ok' : 'iv2-alloc-total--bad'}">
                <span>Распределено <b>${escapeHtml(money(allocated))}</b> из <b>${escapeHtml(money(amount))}</b>${
                    ok ? '' : ` · осталось <b>${escapeHtml(money(rest))}</b>`}</span>
                <span class="iv2-alloc-total__btns">
                    <button class="bx-btn bx-btn--ghost bx-btn--sm" type="button" data-form-act="alloc-rest">Остаток в последнюю строку</button>
                    <button class="bx-btn bx-btn--ghost bx-btn--sm" type="button" data-form-act="alloc-even">Разделить поровну</button>
                </span>
            </div>`;
    }

    function sectionHtml(key, title, badge, body) {
        const open = state.form.openSections[key];
        return `
            <div class="iv2-sect${open ? ' iv2-sect--open' : ''}" data-section="${key}">
                <button class="iv2-sect__head" type="button" data-form-act="toggle-section" data-section-key="${key}">
                    <span class="iv2-sect__title">${escapeHtml(title)} ${badge}</span>
                    <svg class="iv2-sect__chev" width="16" height="16" viewBox="0 0 24 24" fill="none"
                         stroke="currentColor" stroke-width="1.75" stroke-linecap="round"
                         stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg>
                </button>
                <div class="iv2-sect__body">${body}</div>
            </div>`;
    }

    function renderForm() {
        const host = modalHost('form');
        if (!host) return;
        if (!state.form.open) {
            host.innerHTML = '';
            return;
        }

        const values = state.form.values;
        const issues = formRequisiteIssues();
        const issueCount = Object.keys(issues).length;

        const requisitesBadge = issueCount
            ? `<span class="bx-badge b-warn">${withCount(issueCount, 'замечание', 'замечания', 'замечаний')}</span>`
            : (requisitesFilled() ? '<span class="bx-badge b-appr">полные</span>'
                                  : '<span class="bx-badge b-rej">неполные</span>');

        const mainBody = `
            <div class="iv2-grid2">
                <div class="iv2-field">
                    <label class="iv2-field__label" for="iv2form-city_id">Город <span class="iv2-req">*</span></label>
                    <select class="iv2-select" id="iv2form-city_id" data-form-field="city_id">
                        ${selectOptions(state.refs.cities, values.city_id, 'Выберите город')}
                    </select>
                </div>
                <div class="iv2-field">
                    <label class="iv2-field__label" for="iv2form-payer_id">На кого выставлен <span class="iv2-req">*</span></label>
                    <select class="iv2-select" id="iv2form-payer_id" data-form-field="payer_id">
                        ${selectOptions(state.refs.payers, values.payer_id, 'Выберите плательщика')}
                    </select>
                </div>
                <div class="iv2-field">
                    <label class="iv2-field__label" for="iv2form-due_date">Дата оплаты (план) <span class="iv2-req">*</span></label>
                    <input class="iv2-input" type="date" id="iv2form-due_date" data-form-field="due_date"
                           value="${escapeHtml(values.due_date)}">
                </div>
                <div class="iv2-field">
                    <label class="iv2-field__label" for="iv2form-amount">Сумма счёта, ₽ <span class="iv2-req">*</span></label>
                    <input class="iv2-input" type="number" step="0.01" min="0" id="iv2form-amount"
                           data-form-field="amount" value="${escapeHtml(values.amount)}">
                </div>
                <div class="iv2-field">
                    <label class="iv2-field__label" for="iv2form-vat_id">НДС</label>
                    <select class="iv2-select" id="iv2form-vat_id" data-form-field="vat_id">
                        ${selectOptions(state.refs.vatOptions, values.vat_id, 'Не указан')}
                    </select>
                </div>
            </div>
            <div class="iv2-field">
                <label class="iv2-field__label" for="iv2form-payment_purpose">Назначение платежа <span class="iv2-req">*</span></label>
                <textarea class="iv2-textarea" id="iv2form-payment_purpose" rows="2"
                          data-form-field="payment_purpose">${escapeHtml(values.payment_purpose)}</textarea>
            </div>
            <div class="iv2-field">
                <label class="iv2-field__label" for="iv2form-comment">Комментарий (в банк не уходит)</label>
                <textarea class="iv2-textarea" id="iv2form-comment" rows="2"
                          data-form-field="comment">${escapeHtml(values.comment)}</textarea>
            </div>`;

        const suggestions = state.form.suggestions.length
            ? `<div class="iv2-suggest">${state.form.suggestions.map((cp, i) => `
                <button class="iv2-suggest__item" type="button" data-cp="${i}">
                    <span class="iv2-suggest__name">${escapeHtml(cp.name || 'без названия')}</span>
                    <span class="iv2-suggest__meta">ИНН ${escapeHtml(cp.inn || '—')}
                        · счёт ${escapeHtml(cp.bank_account || '—')}</span>
                </button>`).join('')}</div>`
            : '';

        const requisitesBody = `
            <div class="iv2-field">
                <label class="iv2-field__label" for="iv2form-cp-search">Найти в справочнике</label>
                <input class="iv2-input" id="iv2form-cp-search" placeholder="название, ИНН или расчётный счёт">
                ${suggestions}
                ${state.form.source ? `<div class="iv2-hint">${escapeHtml(state.form.source)}</div>` : ''}
            </div>
            <div class="iv2-grid2">
                ${COUNTERPARTY_FIELDS.map(f => `
                    <div class="iv2-field">
                        <label class="iv2-field__label" for="iv2form-${f.key}">${escapeHtml(f.label)}</label>
                        <input class="iv2-input${issues[f.key] ? ' iv2-input--bad' : ''}" id="iv2form-${f.key}"
                               data-form-field="${f.key}" value="${escapeHtml(values[f.key])}">
                        ${issues[f.key] ? `<div class="iv2-field__err">${escapeHtml(issues[f.key])}</div>` : ''}
                    </div>`).join('')}
            </div>`;

        const allocationBody = `
            ${allocationRowsHtml()}
            <button class="bx-btn bx-btn--ghost bx-btn--sm" type="button" data-form-act="alloc-add">Добавить строку</button>
            ${allocationSummaryHtml()}`;

        const allocationBadge = Math.abs(formAmount() - formAllocated()) < 0.01 && formAmount() > 0
            ? '<span class="bx-badge b-appr">сходится</span>'
            : '<span class="bx-badge b-warn">не сходится</span>';

        host.innerHTML = `
            <div class="iv2-ovl iv2-ovl--form" id="iv2FormOverlay"></div>
            <div class="iv2-modal iv2-modal--form">
                <div class="iv2-modal__box">
                    <div class="iv2-modal__head">
                        <h3 class="bx-card__title">${escapeHtml(FORM_TITLES[state.form.mode])}</h3>
                        <button class="iv2-x" type="button" id="iv2FormClose" aria-label="Закрыть">
                            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                                 stroke-width="1.75" stroke-linecap="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>
                        </button>
                    </div>
                    <div class="iv2-modal__body">
                        ${sectionHtml('main', 'Основное', '', mainBody)}
                        ${sectionHtml('requisites', 'Контрагент и реквизиты', requisitesBadge, requisitesBody)}
                        ${sectionHtml('allocation', 'Распределение по салонам и статьям', allocationBadge, allocationBody)}
                        ${state.form.error ? `<div class="iv2-form-error">${escapeHtml(state.form.error)}</div>` : ''}
                    </div>
                    <div class="iv2-modal__foot">
                        <button class="bx-btn bx-btn--ghost" type="button" id="iv2FormCancel">Отмена</button>
                        <button class="bx-btn" type="button" id="iv2FormSave"${state.form.saving ? ' disabled' : ''}>
                            ${state.form.mode === 'edit' ? 'Сохранить изменения' : 'Создать счёт'}
                        </button>
                    </div>
                </div>
            </div>`;

        bindForm(host);
    }

    function bindForm(host) {
        host.querySelectorAll('[data-form-field]').forEach(input => {
            const field = input.getAttribute('data-form-field');
            input.addEventListener('input', () => {
                state.form.values[field] = input.value;
                // Перерисовываем только то, что зависит от значения, — иначе
                // поле теряет фокус на каждом набранном символе
                if (field === 'amount') updateAllocationSummary(host);
                if (COUNTERPARTY_FIELDS.some(f => f.key === field)) updateRequisiteHints(host, input, field);
            });
            if (input.tagName === 'SELECT') {
                input.addEventListener('change', () => { state.form.values[field] = input.value; });
            }
        });

        host.querySelectorAll('[data-item]').forEach(input => {
            const index = Number(input.getAttribute('data-item'));
            const field = input.getAttribute('data-item-field');
            const handler = () => {
                state.form.items[index][field] = input.value;
                if (field === 'amount') updateAllocationSummary(host);
            };
            input.addEventListener('input', handler);
            input.addEventListener('change', handler);
        });

        host.querySelectorAll('[data-item-del]').forEach(button => {
            button.addEventListener('click', () => {
                state.form.items.splice(Number(button.getAttribute('data-item-del')), 1);
                if (!state.form.items.length) {
                    state.form.items = [{ store_id: '', expense_category_id: '', amount: '' }];
                }
                renderForm();
            });
        });

        host.querySelectorAll('[data-form-act]').forEach(button => {
            button.addEventListener('click', () => formAction(button.getAttribute('data-form-act'), button));
        });

        host.querySelectorAll('[data-cp]').forEach(button => {
            button.addEventListener('click', () => applyCounterparty(Number(button.getAttribute('data-cp'))));
        });

        const search = $('iv2form-cp-search');
        if (search) {
            search.addEventListener('input', () => {
                clearTimeout(counterpartySearchTimer);
                const query = search.value;
                counterpartySearchTimer = setTimeout(() => searchCounterparties(query), TEXT_FILTER_DELAY_MS);
            });
        }

        const close = $('iv2FormClose');
        if (close) close.addEventListener('click', confirmCloseForm);
        const cancel = $('iv2FormCancel');
        if (cancel) cancel.addEventListener('click', confirmCloseForm);
        const overlay = $('iv2FormOverlay');
        if (overlay) overlay.addEventListener('click', confirmCloseForm);
        const save = $('iv2FormSave');
        if (save) save.addEventListener('click', saveForm);
    }

    /** Точечное обновление итога — чтобы не перерисовывать форму на каждый символ. */
    function updateAllocationSummary(host) {
        const box = host.querySelector('.iv2-alloc-total');
        if (!box) return;
        const wrapper = document.createElement('div');
        wrapper.innerHTML = allocationSummaryHtml();
        const fresh = wrapper.firstElementChild;
        box.className = fresh.className;
        box.innerHTML = fresh.innerHTML;
        box.querySelectorAll('[data-form-act]').forEach(button => {
            button.addEventListener('click', () => formAction(button.getAttribute('data-form-act'), button));
        });
        const badge = host.querySelector('[data-section="allocation"] .iv2-sect__title .bx-badge');
        if (badge) {
            const ok = Math.abs(formAmount() - formAllocated()) < 0.01 && formAmount() > 0;
            badge.className = 'bx-badge ' + (ok ? 'b-appr' : 'b-warn');
            badge.textContent = ok ? 'сходится' : 'не сходится';
        }
    }

    /** Подсветка поля реквизита прямо на вводе, без перерисовки всей формы. */
    function updateRequisiteHints(host, input, field) {
        const issues = formRequisiteIssues();
        input.classList.toggle('iv2-input--bad', Boolean(issues[field]));

        let error = input.parentElement.querySelector('.iv2-field__err');
        if (issues[field]) {
            if (!error) {
                error = document.createElement('div');
                error.className = 'iv2-field__err';
                input.parentElement.appendChild(error);
            }
            error.textContent = issues[field];
        } else if (error) {
            error.remove();
        }

        const badge = host.querySelector('[data-section="requisites"] .iv2-sect__title .bx-badge');
        if (badge) {
            const count = Object.keys(issues).length;
            badge.className = 'bx-badge ' + (count ? 'b-warn' : (requisitesFilled() ? 'b-appr' : 'b-rej'));
            badge.textContent = count
                ? withCount(count, 'замечание', 'замечания', 'замечаний')
                : (requisitesFilled() ? 'полные' : 'неполные');
        }
    }

    function formAction(action, sourceElement) {
        if (action === 'toggle-section') {
            const key = sourceElement.getAttribute('data-section-key');
            state.form.openSections[key] = !state.form.openSections[key];
            const section = sourceElement.closest('[data-section]');
            if (section) section.classList.toggle('iv2-sect--open', state.form.openSections[key]);
            return;
        }
        if (action === 'alloc-add') {
            state.form.items.push({ store_id: '', expense_category_id: '', amount: '' });
            renderForm();
            return;
        }
        if (action === 'alloc-rest') {
            const rest = Math.round((formAmount() - formAllocated()) * 100) / 100;
            const last = state.form.items[state.form.items.length - 1];
            if (!last) return;
            const next = Math.round(((Number(last.amount) || 0) + rest) * 100) / 100;
            // Отрицательная сумма строки не пройдёт ни клиентскую проверку, ни
            // серверную — честнее сказать, в чём дело, чем подставить минус
            if (next <= 0) {
                toast('Распределено больше суммы счёта — уменьшите строки', 'error');
                return;
            }
            last.amount = next;
            renderForm();
            return;
        }
        if (action === 'alloc-even') {
            const count = state.form.items.length;
            const amount = formAmount();
            if (!count || amount <= 0) return;
            const share = Math.floor((amount / count) * 100) / 100;
            state.form.items.forEach(item => { item.amount = share; });
            // Копейки округления кладём в последнюю строку, иначе сумма не сойдётся
            const last = state.form.items[count - 1];
            last.amount = Math.round((amount - share * (count - 1)) * 100) / 100;
            renderForm();
        }
    }

    async function searchCounterparties(query) {
        if (!state.form.open) return;
        if (String(query || '').trim().length < 2) {
            state.form.suggestions = [];
            renderSuggestions();
            return;
        }
        try {
            const data = await apiGet('/api/invoices/counterparties?query=' + encodeURIComponent(query));
            if (!state.form.open) return;
            state.form.suggestions = data.counterparties || [];

            // Одно совпадение — подставляем сразу: гадать, какой из одного
            // варианта выбрать, человеку не нужно
            if (state.form.suggestions.length === 1) {
                applyCounterparty(0);
                return;
            }
            renderSuggestions();
        } catch (error) {
            console.error('invoices-v2: поиск контрагента не удался:', error);
        }
    }

    /**
     * Подсказки перерисовываем точечно, а не всей формой: полная перерисовка
     * отбирает фокус у поля поиска, и человек не может продолжить набор.
     */
    function renderSuggestions() {
        const search = $('iv2form-cp-search');
        if (!search) return;
        const field = search.parentElement;
        let box = field.querySelector('.iv2-suggest');

        if (!state.form.suggestions.length) {
            if (box) box.remove();
            return;
        }

        if (!box) {
            box = document.createElement('div');
            box.className = 'iv2-suggest';
            search.insertAdjacentElement('afterend', box);
        }
        box.innerHTML = state.form.suggestions.map((cp, i) => `
            <button class="iv2-suggest__item" type="button" data-cp="${i}">
                <span class="iv2-suggest__name">${escapeHtml(cp.name || 'без названия')}</span>
                <span class="iv2-suggest__meta">ИНН ${escapeHtml(cp.inn || '—')}
                    · счёт ${escapeHtml(cp.bank_account || '—')}</span>
            </button>`).join('');
        box.querySelectorAll('[data-cp]').forEach(button => {
            button.addEventListener('click', () => applyCounterparty(Number(button.getAttribute('data-cp'))));
        });
    }

    function applyCounterparty(index) {
        const counterparty = state.form.suggestions[index];
        if (!counterparty) return;
        COUNTERPARTY_FIELDS.forEach(f => {
            state.form.values[f.key] = counterparty[f.from] || '';
        });
        state.form.suggestions = [];
        state.form.openSections.requisites = true;
        state.form.source = 'Реквизиты подставлены из справочника контрагентов'
            + (counterparty.updated_at ? ` · обновлены ${fmtCreated(counterparty.updated_at)}` : '');
        renderForm();
    }

    function formValidationError() {
        const values = state.form.values;
        if (!values.city_id) return 'Выберите город';
        if (!values.payer_id) return 'Выберите, на кого выставлен счёт';
        if (!values.due_date) return 'Укажите планируемую дату оплаты';
        if (!(Number(values.amount) > 0)) return 'Сумма счёта должна быть больше нуля';
        if (!String(values.payment_purpose).trim()) return 'Заполните назначение платежа';

        // Распределение обязательно (решение №15): нераспределённый счёт не
        // попадает ни в один разрез по салонам — теряется ровно та аналитика,
        // ради которой модуль и делался
        const filled = state.form.items.filter(item => item.store_id || item.expense_category_id || item.amount);
        if (!filled.length) return 'Распределите счёт по салонам и статьям — без этого он не попадёт в аналитику';
        for (const item of filled) {
            if (!item.store_id) return 'В распределении не выбран салон';
            if (!item.expense_category_id) return 'В распределении не выбрана статья расхода';
            if (!(Number(item.amount) > 0)) return 'Сумма строки распределения должна быть больше нуля';
        }
        if (Math.abs(formAllocated() - Number(values.amount)) >= 0.01) {
            return 'Распределение не сходится с суммой счёта';
        }
        return '';
    }

    async function apiPut(path, body) {
        const response = await fetch(path, {
            method: 'PUT',
            credentials: 'include',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body || {}),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.error || ('HTTP ' + response.status));
        return data;
    }

    function formPayload() {
        const values = state.form.values;
        const payload = {
            city_id: Number(values.city_id),
            payer_id: Number(values.payer_id),
            vat_id: values.vat_id ? Number(values.vat_id) : null,
            due_date: values.due_date,
            amount: Number(values.amount),
            payment_purpose: String(values.payment_purpose).trim(),
            comment: String(values.comment || '').trim(),
            line_items: state.form.items
                .filter(item => item.store_id && item.expense_category_id && Number(item.amount) > 0)
                .map(item => ({
                    store_id: Number(item.store_id),
                    expense_category_id: Number(item.expense_category_id),
                    amount: Number(item.amount),
                })),
        };
        COUNTERPARTY_FIELDS.forEach(f => {
            payload[f.key] = String(values[f.key] || '').trim() || null;
        });
        return payload;
    }

    async function saveForm() {
        const error = formValidationError();
        if (error) {
            state.form.error = error;
            renderForm();
            return;
        }

        state.form.saving = true;
        state.form.error = '';
        renderForm();

        try {
            const payload = formPayload();
            if (state.form.mode === 'edit') {
                await apiPut('/api/invoices/' + state.form.invoiceId, payload);
                const invoiceId = state.form.invoiceId;
                state.form = emptyForm();
                renderForm();
                toast('Изменения сохранены', 'success');
                await refreshDetails(invoiceId);
            } else {
                const data = await apiPost('/api/invoices', payload);
                state.form = emptyForm();
                renderForm();
                toast('Счёт создан и отправлен на согласование', 'success');
                const newId = data.invoice && data.invoice.id;
                if (newId) {
                    delete state.details[newId];
                    openCard(newId);
                }
            }
            loadSummary();
            loadList(false);
        } catch (saveError) {
            state.form.saving = false;
            state.form.error = saveError.message;
            renderForm();
        }
    }

    async function confirmCloseForm() {
        // Форма длинная, случайный клик мимо стоит дорого
        const ok = await window.BarhatUI.confirm('Введённые данные не сохранятся.',
            { title: 'Закрыть форму?', confirmText: 'Закрыть', cancelText: 'Продолжить ввод' });
        if (ok) closeForm();
    }

    // =========================================================================
    // Хоткеи и история браузера
    // =========================================================================

    function pageIsActive() {
        const page = document.querySelector('.page[data-page="invoices_v2"]');
        return Boolean(page && page.classList.contains('active'));
    }

    // Дашборд — одностраничное приложение: все разделы живут в одном DOM,
    // поэтому обработчик на document обязан проверять, что открыт наш раздел.
    document.addEventListener('keydown', function(event) {
        if (!pageIsActive()) return;

        // Escape обрабатываем до проверки поля ввода: закрыть форму хочется и
        // из середины заполнения. Форма поверх карточки — закрываем верхнюю.
        if (event.key === 'Escape') {
            if (state.report) { state.report = null; renderBulkReport(); }
            else if (state.form.open) confirmCloseForm();
            else if (state.card.id) closeCard();
            return;
        }

        if (/^(INPUT|TEXTAREA|SELECT)$/.test(event.target.tagName || '')) return;
        if (event.ctrlKey || event.metaKey || event.altKey) return;
        if (state.form.open || state.card.id || state.view !== 'queue' || !state.queueActiveId) return;

        // Раскладку не переключают ради хоткея — принимаем и русские буквы
        const key = String(event.key).toLowerCase();
        if (key === 'a' || key === 'ф') {
            doAction('approve', state.queueActiveId);
            event.preventDefault();
        } else if (key === 'r' || key === 'к') {
            doAction('reject', state.queueActiveId);
            event.preventDefault();
        } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
            stepQueue(event.key === 'ArrowDown' ? 1 : -1);
            event.preventDefault();
        }
    });

    // Обработчик вешается синхронно при загрузке файла — раньше, чем свой
    // popstate заведёт script.js. Значит наш успевает прочитать адрес до того,
    // как роутер дашборда заменит его на «/invoices-v2».
    window.addEventListener('popstate', function() {
        if (!pageIsActive()) return;
        const invoiceId = readInvoiceIdFromUrl();
        if (invoiceId) {
            if (state.card.id !== invoiceId) {
                state.cardPushed = false;
                openCard(invoiceId, true);
            }
        } else if (state.card.id) {
            state.card = { id: null, data: null, loading: false, error: null };
            state.cardPushed = false;
            renderCard();
        }
    });

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

        renderHeaderActions();
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

        // Пришли по адресу с номером счёта — открываем карточку. Своей записи
        // в истории при этом не заводим: она уже там, человек пришёл по ссылке
        // или обновил страницу.
        if (pendingInvoiceId) {
            const invoiceId = pendingInvoiceId;
            pendingInvoiceId = null;
            state.cardPushed = false;
            openCard(invoiceId, true);
        }
    }

    window.InvoicesV2Module = {
        onPageActivated,
    };
})();
