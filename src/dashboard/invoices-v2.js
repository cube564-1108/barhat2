/**
 * Раздел «Согласование счетов» — основной раздел счетов, поверх существующих
 * таблиц и API. Имена файлов и секции прав остались с приставкой v2 намеренно:
 * переименовывать их — это ломать URL, права в базе и историю правок ради
 * косметики. Наружу приставка не показывается нигде.
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

    // Из каких статусов счёт вообще можно выбрать для массового действия.
    // Оплаченные и отклонённые здесь ради архивации: с отменой автоархивации
    // (решение владельца 2026-08-25) убирать отработанные счета надо пачкой.
    const BULK_SELECTABLE = ['on_approval', 'approved', 'sent_to_bank', 'paid', 'rejected'];

    /**
     * Массовые действия: какие статусы принимает каждое и как называется.
     *
     * `style` тут только из светлого набора: панель массовых действий залита
     * `--bx-wine`, и `bx-btn--ghost` красит текст ровно в этот же цвет — от
     * кнопки остаётся одна рамка. Для тёмного фона есть `bx-btn--ghost-light`.
     */
    const BULK_ACTIONS = [
        { key: 'approve', label: 'Согласовать', statuses: ['on_approval'], style: 'bx-btn--ok' },
        { key: 'bank', label: 'Отправить в банк', statuses: ['approved'], style: '' },
        { key: 'paid', label: 'Отметить оплаченными', statuses: ['approved', 'sent_to_bank'], style: 'bx-btn--ok' },
        { key: 'archive', label: 'Убрать в архив', statuses: ['paid', 'rejected'], style: 'bx-btn--ghost-light' },
        // Возврат из архива живёт по своим правилам: он касается только
        // архивных счетов, а остальные действия — только не архивных
        { key: 'unarchive', label: 'Вернуть из архива', statuses: ['paid', 'rejected', 'on_approval', 'approved', 'sent_to_bank'],
          style: 'bx-btn--ghost-light', archivedOnly: true },
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
        { key: 'clarification', label: 'На уточнении' },
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
        // Только оплаченные, не уехавшие в ПланФакт
        onlyUnsynced: false,
        kpiFilter: null,
        sort: { key: 'created_at', dir: 'desc' },
        rows: [],
        total: 0,
        totalAmount: 0,
        expandedAlloc: new Set(),
        selected: new Set(),
        report: null,
        summary: null,
        // Счета, уехавшие в архив при отменённой автоархивации (см. loadAutoArchived)
        autoArchived: null,
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

        // Платёжный борд: свой запрос и свой набор строк (см. loadBoard)
        board: { rows: [], total: 0, loading: false, loaded: false, error: '' },

        // Инструменты раздела (справочники, контрагенты, ПланФакт) — одна
        // модалка за раз, поэтому одно поле `open` на все три.
        tools: emptyTools(),
    };

    function emptyTools() {
        return {
            open: null,           // 'refs' | 'counterparties' | 'planfact'
            refs: { type: 'categories', list: [], loading: false, error: '', bankEditId: null, newName: '' },
            cp: { list: [], loaded: false, loading: false, error: '', search: '', form: null, saving: false },
            templates: { list: [], loading: false, error: '' },
            pf: {
                tab: 'sync',
                busy: false,
                status: '',
                result: null,
                mapping: null,
                mappingLoading: false,
                mappingError: '',
                unmatched: null,
                unmatchedLoading: false,
                unmatchedError: '',
            },
        };
    }

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

        // В очереди согласующего нет счетов, которые сейчас у автора: работа
        // по ним не его. В таблице и на борде они видны как обычно.
        if (state.view === 'queue' && !state.kpiFilter) params.set('clarification', 'exclude');

        // «Показывать оплаченные» имеет смысл, только пока статус не выбран
        // явно: иначе выбор «Оплачен» и снятая галочка дали бы пустой список
        // и вопрос «куда всё делось».
        if (!state.showPaid && !state.filters.status) params.set('hide_paid', 'true');

        // Оплачен, но в ПланФакт не уехал — сам по себе срез, статус в нём
        // всегда «Оплачен», поэтому hide_paid к нему не применяем
        if (state.onlyUnsynced) {
            params.set('planfact', 'unsynced');
            params.delete('hide_paid');
        }

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
        } else if (state.kpiFilter === 'clarification') {
            params.set('status', 'on_approval');
            params.set('clarification', 'only');
        }
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

        // Борд живёт на своём запросе, но фильтры у него те же, поэтому любая
        // пересборка списка обязана пересобрать и его. Открыт другой вид —
        // просто помечаем устаревшим, загрузится при переключении.
        if (!append) {
            state.board.loaded = false;
            if (state.view === 'board') loadBoard();
        }

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

    /**
     * Кнопки шапки. Ключ кэша — роль, а не «уже рисовали»: набор кнопок от неё
     * зависит, а роль становится известна только с первым onPageActivated.
     */
    function renderHeaderActions() {
        const host = $('iv2HeaderActions');
        if (!host) return;
        const role = (state.user && state.user.role) || 'unknown';
        if (host.dataset.ready === role) return;

        // Справочники, контрагенты и ПланФакт правит только админ — как и в
        // старом разделе. Подстановка реквизитов в форме счёта доступна всем.
        const toolButtons = isAdmin() ? TOOL_BUTTONS.map(tool => `
            <button class="bx-btn bx-btn--ghost-light" type="button" data-tool="${tool.key}">
                ${escapeHtml(tool.label)}
            </button>`).join('') : '';

        host.innerHTML = toolButtons + `
            <button class="bx-btn bx-btn--light" type="button" id="iv2NewInvoice">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                     stroke-width="1.75" stroke-linecap="round"><path d="M12 5v14"/><path d="M5 12h14"/></svg>
                Новый счёт
            </button>`;

        const button = $('iv2NewInvoice');
        if (button) button.addEventListener('click', () => openForm('create'));
        host.querySelectorAll('[data-tool]').forEach(element => {
            element.addEventListener('click', () => openTool(element.getAttribute('data-tool')));
        });
        host.dataset.ready = role;
    }

    /**
     * Счета, уехавшие в архив ещё при автоархивации.
     *
     * Её отмена подействовала только на будущее: те счета, что уже уехали,
     * так и лежат в архиве и в списке не видны. Показываем плашку с
     * предложением вернуть их — консоли на этом тарифе Amvera нет, разовые
     * операции с боевой базой делаются из интерфейса.
     */
    async function loadAutoArchived() {
        if (!isAdmin()) return;
        try {
            const data = await apiGet('/api/invoices/admin/auto-archived');
            state.autoArchived = { count: data.count || 0, amount: data.amount || 0 };
        } catch (error) {
            console.error('invoices-v2: не удалось проверить архив:', error);
            state.autoArchived = null;
        }
        renderAutoArchived();
    }

    function renderAutoArchived() {
        const host = $('iv2AutoArchived');
        if (!host) return;
        if (!state.autoArchived || !state.autoArchived.count) {
            host.innerHTML = '';
            return;
        }

        const { count, amount } = state.autoArchived;
        host.innerHTML = `
            <div class="iv2-notice">
                <div class="iv2-notice__text">
                    <b>${escapeHtml(withCount(count, 'оплаченный счёт', 'оплаченных счёта', 'оплаченных счетов'))}</b>
                    на ${escapeHtml(money(amount))} ${count === 1 ? 'лежит' : 'лежат'} в архиве —
                    их убрала туда автоархивация, пока она работала. Сейчас счета в архив кладёте только вы.
                </div>
                <button class="bx-btn bx-btn--sm" type="button" id="iv2RestoreArchived">Вернуть в список</button>
            </div>`;

        const button = $('iv2RestoreArchived');
        if (button) button.addEventListener('click', () => restoreAutoArchived(button));
    }

    async function restoreAutoArchived(button) {
        const { count, amount } = state.autoArchived;
        const ok = await window.BarhatUI.confirm(
            `${withCount(count, 'счёт', 'счёта', 'счетов')} на ${money(amount)} вернутся в обычный список. `
            + 'Те, что вы убирали в архив сами, останутся на месте.',
            { title: 'Вернуть счета из архива?', confirmText: 'Вернуть' });
        if (!ok) return;

        button.disabled = true;
        try {
            const data = await apiPost('/api/invoices/admin/auto-archived/restore', {});
            const restored = (data.restored || []).length;
            toast(restored
                ? `Возвращено в список: ${withCount(restored, 'счёт', 'счёта', 'счетов')}`
                : 'Возвращать нечего', 'success');
            state.autoArchived = { count: 0, amount: 0 };
            renderAutoArchived();
            loadSummary();
            loadList(false);
        } catch (error) {
            button.disabled = false;
            toast('Не удалось вернуть счета: ' + error.message, 'error');
        }
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
            // Плитка уточнения кликается только когда миграция прошла:
            // до неё фильтровать нечем, честнее показать ноль без действия
            const clickable = tile.key !== 'clarification'
                || Boolean(state.summary.clarification && state.summary.clarification.available);
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
        if (key === 'clarification') {
            return state.summary && state.summary.clarification && state.summary.clarification.available
                ? 'ждут ответа автора'
                : 'появится после обновления';
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
                        <label class="iv2-switch" title="Оплачены, но операция в ПланФакт не разнесена">
                            <input type="checkbox" class="iv2-cbx" id="iv2fUnsynced"> Не разнесены в ПланФакт
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

        const unsynced = $('iv2fUnsynced');
        if (unsynced) {
            unsynced.addEventListener('change', () => {
                state.onlyUnsynced = unsynced.checked;
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
        state.onlyUnsynced = false;
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
        const unsynced = $('iv2fUnsynced');
        if (unsynced) unsynced.checked = state.onlyUnsynced;
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
        if (state.onlyUnsynced) chips.push({ kind: 'unsynced', key: 'unsynced', text: 'Не разнесены в ПланФакт' });
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
                else if (kind === 'unsynced') state.onlyUnsynced = false;
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
                ? 'Перетащите карточку в другую колонку — изменится плановая дата оплаты'
                : 'Фильтры общие для всех видов';
        }

        // Панель массовых действий имеет смысл только в таблице — там чекбоксы
        renderBulkBar();

        if (state.view === 'board') {
            renderBoard();
            // Борд грузится своим запросом (см. loadBoard): ему нужны все счета
            // с открытым сроком, а не страница таблицы
            if (!state.board.loaded && !state.board.loading) loadBoard();
        }

        if (state.view === 'queue') {
            renderQueue();
            // Детали активного счёта могли не загрузиться, пока вид был скрыт
            if (state.queueActiveId && !state.details[state.queueActiveId]) {
                selectQueueInvoice(state.queueActiveId);
            }
        }
    }

    // =========================================================================
    // Отрисовка: платёжный борд
    // =========================================================================

    /**
     * Колонки борда — из принятого прототипа, плюс «Без срока».
     *
     * `days` — на сколько дней вперёд от сегодня встаёт срок, когда карточку
     * бросили в эту колонку. Правило одно на все колонки: дата, попадающая в
     * эту же колонку, — так человек предсказывает результат до того, как
     * отпустит карточку.
     */
    const BOARD_COLS = [
        { key: 'over',  label: 'Просрочено', days: null, test: days => days !== null && days < 0 },
        { key: 'today', label: 'Сегодня',    days: 0,    test: days => days === 0 },
        { key: 'tmrw',  label: 'Завтра',     days: 1,    test: days => days === 1 },
        { key: 'week',  label: 'Эта неделя', days: 4,    test: days => days !== null && days > 1 && days <= 7 },
        { key: 'later', label: 'Позже',      days: 14,   test: days => days !== null && days > 7 },
        // Счёта без срока в прототипе не было: там он есть у всех. В боевой
        // базе `due_date` не всегда заполнен, и без своей колонки такой счёт
        // молча исчезает с борда — а это ровно тот счёт, который потеряется.
        { key: 'none',  label: 'Без срока',  days: null, test: days => days === null },
    ];

    // Верхняя граница страницы на сервере. Борд показывает открытые платежи,
    // а не всю историю, поэтому одной страницы хватает; если счетов больше —
    // говорим об этом вслух, а не молча обрезаем.
    const BOARD_LIMIT = 200;

    let boardToken = 0;

    function shiftDays(plainDate, days) {
        const date = new Date(plainDate + 'T00:00:00');
        date.setDate(date.getDate() + days);
        const month = String(date.getMonth() + 1).padStart(2, '0');
        const day = String(date.getDate()).padStart(2, '0');
        return `${date.getFullYear()}-${month}-${day}`;
    }

    /**
     * Борд грузится своим запросом, а не берёт строки таблицы: таблица
     * листается страницами по 25 и сортирована как выбрал человек, а борду
     * нужны все открытые платежи разом и по возрастанию срока.
     */
    async function loadBoard() {
        const token = ++boardToken;
        state.board.loading = true;
        state.board.error = '';
        renderBoard();

        const params = buildListParams();
        params.set('limit', String(BOARD_LIMIT));
        params.set('offset', '0');
        params.set('with_total', 'true');
        params.set('sort', 'due_date');
        params.set('order', 'asc');
        // Архив и оплаченные на борде не нужны: он про то, что ещё предстоит
        // заплатить. Явный фильтр по статусу человек всё же вправе задать сам.
        params.delete('archived');
        if (!state.filters.status) params.set('hide_paid', 'true');

        try {
            const data = await apiGet('/api/invoices?' + params.toString());
            if (token !== boardToken) return;
            const rows = (data.invoices || []).filter(invoice =>
                !invoice.is_archived && invoice.status !== 'paid' && invoice.status !== 'rejected');
            state.board.rows = rows;
            state.board.total = data.total ?? rows.length;
            state.board.loaded = true;
        } catch (error) {
            if (token !== boardToken) return;
            console.error('invoices-v2: не удалось загрузить борд:', error);
            state.board.rows = [];
            state.board.total = 0;
            state.board.error = error.message;
        } finally {
            if (token === boardToken) {
                state.board.loading = false;
                renderBoard();
            }
        }
    }

    function boardCardHtml(invoice) {
        const locked = !invoice.can_edit_fields;
        const title = locked
            ? 'Срок меняется, только пока счёт открыт для правки'
            : 'Перетащите в другую колонку — изменится плановая дата оплаты';
        return `
            <div class="iv2-bcard${locked ? ' iv2-bcard--locked' : ''}" data-card="${escapeHtml(invoice.id)}"
                 ${locked ? '' : 'draggable="true"'} title="${escapeHtml(title)}">
                <div class="iv2-bcard__cp">${escapeHtml(invoice.counterparty_name || 'без контрагента')}</div>
                <div class="iv2-bcard__amt">${escapeHtml(money(invoice.amount))}</div>
                <div class="iv2-bcard__meta">
                    <span>${escapeHtml(invoice.invoice_number || ('#' + invoice.id))}${
                        invoice.city_name ? ' · ' + escapeHtml(invoice.city_name) : ''}</span>
                    ${statusBadge(invoice)}
                </div>
            </div>`;
    }

    function renderBoard() {
        const host = $('iv2ViewBoard');
        if (!host) return;

        if (state.board.error) {
            host.innerHTML = `<div class="iv2-board-msg">Не удалось загрузить борд: ${escapeHtml(state.board.error)}</div>`;
            return;
        }
        // «Загрузка…» и до старта запроса тоже: renderViews рисует борд раньше,
        // чем уходит loadBoard, и пустые колонки успевали мигнуть.
        if (!state.board.rows.length && (state.board.loading || !state.board.loaded)) {
            host.innerHTML = '<div class="iv2-board-msg">Загрузка…</div>';
            return;
        }

        const columns = BOARD_COLS.map(col => {
            const items = state.board.rows.filter(invoice => col.test(daysTo(invoice.due_date)));
            return { col, items, sum: items.reduce((total, i) => total + (Number(i.amount) || 0), 0) };
        });
        // Полоска в шапке — доля колонки от самой тяжёлой, а не от общей суммы:
        // так видно, куда смещён платёжный вес.
        const maxSum = Math.max(1, ...columns.map(c => c.sum));

        const cut = state.board.total > state.board.rows.length
            ? `<p class="iv2-hint iv2-board-note">Показаны первые ${state.board.rows.length} счетов
                   из ${state.board.total} — сузьте фильтры, если нужного нет на борде.</p>`
            : '';

        host.innerHTML = `
            <div class="iv2-board">
                ${columns.map(({ col, items, sum }) => `
                    <div class="iv2-col" data-col="${col.key}">
                        <div class="iv2-col__head">
                            <div class="iv2-col__name">${escapeHtml(col.label)}</div>
                            <div class="iv2-col__sum">${withCount(items.length, 'счёт', 'счёта', 'счетов')}
                                 · ${escapeHtml(money(sum))}</div>
                            <div class="iv2-col__bar">
                                <div class="iv2-col__fill" style="width:${Math.round(sum / maxSum * 100)}%"></div>
                            </div>
                        </div>
                        <div class="iv2-col__body">
                            ${items.map(boardCardHtml).join('') || '<div class="iv2-hint iv2-col__empty">пусто</div>'}
                        </div>
                    </div>`).join('')}
            </div>
            ${cut}
            <p class="iv2-hint iv2-board-note">
                Перенос ставит ближайшую дату из выбранной колонки: «Завтра» — завтра,
                «Эта неделя» — через четыре дня, «Позже» — через две недели. Точную дату
                можно поставить в карточке счёта.
            </p>`;

        bindBoard();
    }

    function bindBoard() {
        const host = $('iv2ViewBoard');
        if (!host) return;

        // Клик по карточке открывает счёт, но только если это был клик, а не
        // конец перетаскивания: иначе каждый перенос открывал бы карточку.
        let draggedId = null;
        let dragHappened = false;

        host.querySelectorAll('[data-card]').forEach(card => {
            const invoiceId = Number(card.getAttribute('data-card'));

            card.addEventListener('dragstart', event => {
                draggedId = invoiceId;
                dragHappened = true;
                card.classList.add('iv2-bcard--drag');
                if (event.dataTransfer) {
                    event.dataTransfer.effectAllowed = 'move';
                    try {
                        event.dataTransfer.setData('text/plain', String(invoiceId));
                    } catch (e) { /* старый Edge не даёт setData на dragstart */ }
                }
            });
            card.addEventListener('dragend', () => {
                card.classList.remove('iv2-bcard--drag');
                draggedId = null;
                setTimeout(() => { dragHappened = false; }, 0);
            });
            card.addEventListener('click', () => {
                if (dragHappened) return;
                openCard(invoiceId);
            });
        });

        host.querySelectorAll('[data-col]').forEach(column => {
            column.addEventListener('dragover', event => {
                event.preventDefault();
                column.classList.add('iv2-col--over');
            });
            column.addEventListener('dragleave', () => column.classList.remove('iv2-col--over'));
            column.addEventListener('drop', event => {
                event.preventDefault();
                column.classList.remove('iv2-col--over');
                let invoiceId = draggedId;
                if (!invoiceId && event.dataTransfer) {
                    invoiceId = Number(event.dataTransfer.getData('text/plain'));
                }
                if (invoiceId) moveInvoiceDue(invoiceId, column.getAttribute('data-col'));
            });
        });
    }

    async function moveInvoiceDue(invoiceId, columnKey) {
        const column = BOARD_COLS.find(col => col.key === columnKey);
        if (!column) return;

        if (column.key === 'over') {
            toast('В «Просрочено» перенести нельзя — это следствие даты, а не решение', 'error');
            return;
        }
        if (column.key === 'none') {
            toast('Срок оплаты обязателен — убрать его нельзя', 'error');
            return;
        }

        const invoice = state.board.rows.find(row => row.id === invoiceId);
        if (!invoice) return;

        const dueDate = shiftDays(today(), column.days);
        if (invoice.due_date === dueDate) return;

        const label = invoice.invoice_number || ('#' + invoiceId);
        try {
            await apiPut('/api/invoices/' + invoiceId, { due_date: dueDate });
        } catch (error) {
            toast(`${label}: не удалось перенести срок — ${error.message}`, 'error');
            // Карточка на экране осталась на старом месте, но состояние счёта
            // могло уйти вперёд у соседа — перечитываем борд целиком
            loadBoard();
            return;
        }

        invoice.due_date = dueDate;
        delete state.details[invoiceId];      // в карточке лежит копия со старым сроком
        renderBoard();
        toast(`${label}: срок оплаты — ${fmtDue(dueDate)}`, 'success');

        // Срок виден в таблице и в очереди, а «К оплате сегодня» и «Просрочено»
        // считаются по нему же
        loadSummary();
        loadList(false);
    }

    // =========================================================================
    // Отрисовка: таблица
    // =========================================================================

    function statusBadge(invoice) {
        const meta = STATUSES[invoice.status] || { label: invoice.status, cls: 'b-rej' };
        const archived = invoice.is_archived
            ? ' <span class="bx-badge b-arch">Архив</span>'
            : '';
        // Счёт на уточнении формально «на согласовании», но лежит у автора —
        // без отдельного бейджа он неотличим от тех, что ждут согласующего
        const clarification = invoice.clarification_at
            ? ' <span class="bx-badge b-warn">На уточнении</span>'
            : '';
        // Оплачен, но операция в ПланФакт не разнесена. Раньше это было
        // неотличимо от разнесённого счёта, и сбой не видел никто.
        const unsynced = invoice.status === 'paid' && !invoice.planfact_synced_at
            ? ' <span class="bx-badge b-warn" title="Операция в ПланФакт не разнесена">Не разнесён</span>'
            : '';
        return `<span class="bx-badge ${meta.cls}">${escapeHtml(meta.label)}</span>${clarification}${unsynced}${archived}`;
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

    function isAdmin() {
        return Boolean(state.user && state.user.role === 'admin');
    }

    function canBulk() {
        return isAdmin();
    }

    /**
     * Можно ли выбрать счёт чекбоксом. Архивные выбираются тоже — ради
     * массового возврата из архива; действия, которым архивный счёт не
     * подходит, до него просто не доберутся (см. bulkActionFits).
     */
    function isSelectable(invoice) {
        if (invoice.is_archived) return true;
        return BULK_SELECTABLE.indexOf(invoice.status) !== -1;
    }

    /** Подходит ли счёт под массовое действие: и по статусу, и по архиву. */
    function bulkActionFits(action, invoice) {
        if (action.archivedOnly) return Boolean(invoice.is_archived);
        if (invoice.is_archived) return false;
        return action.statuses.indexOf(invoice.status) !== -1;
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
        // Пока открыта форма счёта, готового адресата нет: у создаваемого счёта
        // ещё нет id, а тихо приложить файл к соседнему счёту — худшее, что
        // можно сделать. Файлы принимает сама форма (см. addFilesToForm).
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

        const files = filesFromClipboard(event);
        if (!files.length) return;  // обычная вставка текста — не мешаем

        // У blob из буфера имени нет вовсе — задаём сами, иначе сервер
        // отвергнет файл по расширению
        const named = files.map(file => (
            file.name && fileExtension(file.name)
                ? file
                : new File([file], timestampName(file.type === 'application/pdf' ? '.pdf' : '.png'),
                           { type: file.type || 'image/png' })
        ));

        // Открытая форма забирает вставку себе: счёт ещё не создан, файлы
        // ждут в ней и уедут сразу после создания
        if (state.form.open) {
            event.preventDefault();
            addFilesToForm(named);
            return;
        }

        const invoiceId = attachTargetId();
        if (!invoiceId) return;

        event.preventDefault();
        uploadAttachments(invoiceId, named);
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

        const files = event.dataTransfer.files;
        if (!files || !files.length) return;

        if (state.form.open) {
            addFilesToForm(files);
            return;
        }

        const invoiceId = attachTargetId();
        if (!invoiceId) {
            toast('Откройте счёт, чтобы приложить к нему файл');
            return;
        }
        uploadAttachments(invoiceId, files);
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
        const admin = isAdmin();
        const buttons = [];

        // Правку показываем по ответу сервера, а не по своей копии правил:
        // can_edit_fields считает тот же can_edit_invoice_fields, что потом
        // и разрешит запрос
        if (details.can_edit_fields) {
            buttons.push(`<button class="bx-btn bx-btn--ghost" type="button" data-act="edit">Редактировать</button>`);
        }
        buttons.push(`<button class="bx-btn bx-btn--ghost" type="button" data-act="copy">Копировать счёт</button>`);
        if (admin) {
            buttons.push(`<button class="bx-btn bx-btn--ghost" type="button" data-act="save-template">Сохранить как шаблон</button>`);
        }

        if (admin) {
            if (invoice.is_archived) {
                buttons.push(`<button class="bx-btn bx-btn--ghost" type="button" data-act="unarchive">Вернуть из архива</button>`);
            } else if (invoice.status === 'on_approval') {
                if (!invoice.clarification_at) {
                    buttons.unshift(`<button class="bx-btn bx-btn--danger" type="button" data-act="reject">Отклонить</button>`);
                    buttons.unshift(`<button class="bx-btn bx-btn--ghost" type="button" data-act="clarify">На уточнение</button>`);
                    buttons.unshift(`<button class="bx-btn bx-btn--ok" type="button" data-act="approve">Согласовать</button>`);
                }
            } else if (invoice.status === 'approved' || invoice.status === 'sent_to_bank') {
                buttons.unshift(`<button class="bx-btn bx-btn--ok" type="button" data-act="paid">Отметить оплаченным</button>`);
            }
            // Отправка в банк по одному счёту. Массовое действие живёт только
            // в виде «Таблица», а согласовывают в очереди — без этой кнопки
            // приходилось переключать вид ради одного счёта.
            if (invoice.status === 'approved' && !invoice.is_archived) {
                buttons.unshift(`<button class="bx-btn bx-btn--ghost" type="button" data-act="bank-test">Проверить сборку</button>`);
                buttons.unshift(`<button class="bx-btn" type="button" data-act="bank">Отправить в банк</button>`);
            }
            if (!invoice.is_archived && (invoice.status === 'paid' || invoice.status === 'rejected')) {
                buttons.push(`<button class="bx-btn bx-btn--ghost" type="button" data-act="archive">В архив</button>`);
            }
        }

        // Счёт у автора: согласовать его нельзя, пока автор не ответил, —
        // иначе уточнение теряет смысл
        if (invoice.status === 'on_approval' && invoice.clarification_at
            && (admin || invoice.created_by === (state.user && state.user.username))) {
            buttons.unshift(`<button class="bx-btn bx-btn--ok" type="button" data-act="resubmit">Отправить снова</button>`);
        }

        const soon = [];

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
                        <kbd>U</kbd> на уточнение · <kbd>↑</kbd><kbd>↓</kbd> следующий счёт
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

        if (action === 'save-template') {
            saveAsTemplate({
                invoiceId,
                suggestedName: details.invoice.counterparty_name || details.invoice.payment_purpose || '',
            });
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
                toast('Счёт отклонён. Остаётся в списке — уберите в архив, когда разберётесь');
            } else if (action === 'clarify') {
                const reason = await window.BarhatUI.prompt(
                    `${label}: что нужно уточнить? Текст уйдёт автору в обсуждение счёта.`,
                    '', { title: 'Отправить на уточнение', confirmText: 'Отправить',
                          placeholder: 'Например: нет скана счёта' });
                if (reason === null) return;
                if (!String(reason).trim()) {
                    toast('Без пояснения автору непонятно, что исправлять', 'error');
                    return;
                }
                await apiPost(`/api/invoices/${invoiceId}/clarify`, { reason: String(reason).trim() });
                toast('Счёт отправлен автору на уточнение', 'success');
            } else if (action === 'resubmit') {
                const comment = await window.BarhatUI.prompt(
                    `${label}: что исправлено? Можно не заполнять.`,
                    '', { title: 'Отправить снова на согласование', confirmText: 'Отправить',
                          placeholder: 'Например: приложил скан' });
                if (comment === null) return;
                await apiPost(`/api/invoices/${invoiceId}/resubmit`,
                              { comment: String(comment || '').trim() || null });
                toast('Счёт вернулся в очередь согласования', 'success');
            } else if (action === 'paid') {
                const ok = await window.BarhatUI.confirm(
                    `${label} · ${money(invoice.amount)}. Счёт останется в списке — в архив его убираете вы.`,
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
            } else if (action === 'bank' || action === 'bank-test') {
                // false = отменили в диалоге или это была проверка сборки:
                // статус не двигался, перечитывать нечего
                const moved = await sendOneToBank(invoiceId, invoice, action === 'bank-test');
                if (!moved) return;
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

    /**
     * Отправка платёжки по одному счёту (то же, что делает массовое действие,
     * но из карточки).
     *
     * `test = true` — сборка документа уходит в тестовый контур банка: статус
     * счёта не меняется, а в ответ приходит собранный документ. Это
     * единственный способ увидеть, что именно уедет в банк, до боевой отправки.
     *
     * Возвращает true, если статус счёта мог измениться (значит вызывающему
     * нужно перечитать счёт и список).
     */
    async function sendOneToBank(invoiceId, invoice, test) {
        const label = invoice.invoice_number || ('#' + invoice.id);

        if (!test) {
            const warnings = requisiteWarnings(invoice);
            const ok = await window.BarhatUI.confirm(
                `${label} · ${invoice.counterparty_name || 'без контрагента'} · ${money(invoice.amount)}.\n\n`
                + 'Банк создаст черновик платёжки — подписывать её всё равно нужно вручную '
                + 'в личном кабинете.'
                + (warnings.length ? '\n\nВ реквизитах есть замечания: ' + warnings.join('; ') : ''),
                { title: 'Отправить платёжку в банк?', confirmText: 'Отправить', danger: true });
            if (!ok) return false;
        }

        let data;
        try {
            data = await apiPost(`/api/invoices/${invoiceId}/send-to-bank`, { sandbox: Boolean(test) });
        } catch (error) {
            // Отказ банка приезжает с 502 и текстом ошибки — показываем его в
            // том же отчёте, что и у массовой отправки, а не одним тостом:
            // текст банка бывает длинным и его надо успеть прочитать
            showBulkReport(test ? 'bank-test' : 'bank-one', { error: error.message });
            return !test;
        }

        showBulkReport(test ? 'bank-test' : 'bank-one', data);
        return !test;
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
            const count = chosen.filter(invoice => bulkActionFits(action, invoice)).length;
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

        const chosen = selectedInvoices().filter(invoice => bulkActionFits(action, invoice));
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
                body: `${withCount(chosen.length, 'счёт', 'счёта', 'счетов')} на ${money(total)}:\n${invoiceListText(chosen)}`,
            },
            archive: {
                title: 'Убрать счета в архив?',
                confirm: 'Убрать в архив',
                body: `${withCount(chosen.length, 'счёт', 'счёта', 'счетов')} на ${money(total)}:\n${invoiceListText(chosen)}\n\n`
                    + 'Они пропадут из списка — найти их можно переключателем «Показать архив», '
                    + 'а вернуть оттуда тем же массовым действием.',
            },
            unarchive: {
                title: 'Вернуть счета из архива?',
                confirm: 'Вернуть',
                body: `${withCount(chosen.length, 'счёт', 'счёта', 'счетов')} на ${money(total)}:\n${invoiceListText(chosen)}\n\n`
                    + 'Они снова появятся в обычном списке.',
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
            archive: '/api/invoices/bulk-archive',
            unarchive: '/api/invoices/bulk-unarchive',
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

    /**
     * Собранный документ платёжки — под спойлером: нужен он редко, а занимает
     * весь экран. Формат 1С — обычный текст, поэтому <pre> с переносами.
     */
    function bankDocumentHtml(result) {
        const document_ = result && result.document;
        if (!document_) return '';
        return `
            <details class="iv2-report__doc">
                <summary>Показать документ, собранный для банка</summary>
                <pre class="iv2-report__pre">${escapeHtml(document_)}</pre>
            </details>`;
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
                + reportSection('Не отправлены', data.skipped)
                + bankDocumentHtml(data.result);
        } else if (actionKey === 'bank-test') {
            // Проверка сборки: главное здесь — сам документ, а не статус.
            // Банк на тестовом контуре может и отклонить — это тоже результат.
            body = `<div class="iv2-report__head">${data.ok
                        ? 'Тестовый контур принял черновик — сборка документа в порядке.'
                        : 'Тестовый контур черновик отклонил.'}</div>`
                + ((data.result && data.result.errors && data.result.errors.length)
                    ? reportSection('Что сказал банк',
                                    data.result.errors.map(text => ({ label: text })))
                    : '')
                + `<p class="iv2-hint">Статус счёта не менялся: в боевой банк ничего не уходило.</p>`
                + bankDocumentHtml(data.result);
        } else if (actionKey === 'bank-one') {
            body = `<div class="iv2-report__head">Платёжка загружена в банк черновиком
                        на ${escapeHtml(money((data.invoice && data.invoice.amount) || 0))}.</div>`
                + `<p class="iv2-hint">Подпишите её вручную в личном кабинете банка —
                        сама по себе загрузка деньги не двигает.</p>`
                + bankDocumentHtml(data.result);
        } else if (actionKey === 'approve') {
            body = `<div class="iv2-report__head">Согласовано ${withCount((data.approved || []).length, 'счёт', 'счёта', 'счетов')}
                        на ${escapeHtml(money(data.approved_amount || 0))}</div>`
                + reportSection('Согласованы', data.approved, true)
                + reportSection('Пропущены', data.skipped);
        } else if (actionKey === 'paid') {
            body = `<div class="iv2-report__head">Отмечено оплаченными ${withCount((data.paid || []).length, 'счёт', 'счёта', 'счетов')}
                        на ${escapeHtml(money(data.paid_amount || 0))}</div>
                    <p class="iv2-hint">Разноска в ПланФакт пойдёт следующим прогоном синхронизации.
                        Пока она не прошла, счета помечены «Не разнесён».</p>`
                + reportSection('Оплачены', data.paid, true)
                + reportSection('Пропущены', data.skipped);
        } else if (actionKey === 'archive') {
            body = `<div class="iv2-report__head">Убрано в архив ${withCount((data.archived || []).length, 'счёт', 'счёта', 'счетов')}
                        на ${escapeHtml(money(data.archived_amount || 0))}</div>`
                + reportSection('В архиве', data.archived, true)
                + reportSection('Пропущены', data.skipped);
        } else if (actionKey === 'unarchive') {
            body = `<div class="iv2-report__head">Возвращено из архива ${withCount((data.unarchived || []).length, 'счёт', 'счёта', 'счетов')}
                        на ${escapeHtml(money(data.unarchived_amount || 0))}</div>`
                + reportSection('Вернулись в список', data.unarchived, true)
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
            // Файлы, выбранные до сохранения: у нового счёта ещё нет id, поэтому
            // они ждут здесь и уходят на сервер сразу после создания счёта.
            // Каждый элемент — { file, url }, url нужен для миниатюры и его
            // обязательно освобождать (см. releaseFormFiles).
            files: [],
            openSections: { main: true, documents: true, requisites: false, allocation: true },
            suggestions: [],
            source: '',
            // Расхождение реквизитов шаблона со справочником (см. applyTemplate)
            templateMismatch: null,
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
        tools: 'iv2ToolsHost',
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
        releaseFormFiles();
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

    // ------------------------------------------------------------- Файлы в форме

    /**
     * Блок вложений прямо в форме счёта.
     *
     * У создаваемого счёта ещё нет id, поэтому загрузить файл сразу некуда:
     * копим выбранное здесь и отправляем сразу после того, как счёт создан.
     * Раньше приложить скан из формы было нельзя вовсе — приходилось создать
     * счёт, найти его и открыть карточку.
     */
    function formDocumentsHtml() {
        const already = state.form.mode === 'edit' && state.details[state.form.invoiceId]
            ? (state.details[state.form.invoiceId].attachments || []).length
            : 0;

        return `
            <div class="iv2-drop" data-drop="form">
                <div class="iv2-drop__title">Приложите счёт</div>
                <div class="iv2-drop__hint">Вставьте скрин из буфера (Ctrl+V), перетащите файл сюда
                    или выберите на диске.
                    <span class="iv2-only-touch">Можно сфотографировать счёт камерой.</span></div>
                <div class="iv2-drop__btns">
                    <button class="bx-btn bx-btn--ghost bx-btn--sm" type="button" data-form-act="pick-file">Выбрать файл</button>
                    <button class="bx-btn bx-btn--ghost bx-btn--sm iv2-only-touch" type="button" data-form-act="take-photo">Сфотографировать</button>
                </div>
                <input type="file" class="iv2-hidden-input" data-form-file-input accept="image/*,.pdf" multiple>
                <input type="file" class="iv2-hidden-input" data-form-camera-input accept="image/*" capture="environment">
            </div>
            ${already ? `<p class="iv2-hint iv2-form-files__note">К счёту уже приложено
                ${withCount(already, 'файл', 'файла', 'файлов')} — они видны в карточке,
                там же их можно удалить.</p>` : ''}
            <div id="iv2FormFiles">${formFilesHtml()}</div>`;
    }

    function formFilesHtml() {
        if (!state.form.files.length) return '';
        return `<div class="iv2-form-files">${state.form.files.map((item, index) => `
            <div class="iv2-form-file">
                ${item.url
                    ? `<img class="iv2-form-file__pic" src="${escapeHtml(item.url)}" alt="${escapeHtml(item.file.name)}">`
                    : '<div class="iv2-form-file__pdf">PDF</div>'}
                <div class="iv2-form-file__name" title="${escapeHtml(item.file.name)}">${escapeHtml(item.file.name)}</div>
                <button class="iv2-form-file__del" type="button" data-form-file-del="${index}"
                        aria-label="Убрать файл" title="Убрать файл">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                         stroke-width="1.75" stroke-linecap="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>
                </button>
            </div>`).join('')}</div>`;
    }

    /** Перерисовываем только список файлов: полная перерисовка отбирает фокус. */
    function renderFormFiles() {
        const host = $('iv2FormFiles');
        if (!host) return;
        host.innerHTML = formFilesHtml();
        host.querySelectorAll('[data-form-file-del]').forEach(button => {
            button.addEventListener('click', () => removeFormFile(Number(button.getAttribute('data-form-file-del'))));
        });

        // Бейдж секции считает файлы — иначе он врёт до следующей перерисовки
        const section = document.querySelector('[data-section="documents"] .iv2-sect__title .bx-badge');
        if (section) {
            section.className = 'bx-badge ' + (state.form.files.length ? 'b-appr' : 'b-warn');
            section.textContent = state.form.files.length
                ? withCount(state.form.files.length, 'файл', 'файла', 'файлов')
                : 'не приложен';
        }
    }

    /**
     * Принять файлы в форму. Проверки те же, что и при загрузке к счёту:
     * узнать о неподходящем файле лучше сразу, а не после создания счёта.
     */
    function addFilesToForm(files) {
        const list = Array.from(files || []).filter(Boolean);
        if (!list.length) return;

        const skipped = [];
        list.forEach(file => {
            const extension = fileExtension(file.name);
            if (ATTACHMENT_EXTENSIONS.indexOf(extension) === -1) {
                skipped.push(`${file.name || 'файл'} — тип не поддерживается`);
                return;
            }
            // Сжатие идёт при отправке (compressImage в uploadAttachments),
            // поэтому здесь сверяем исходный размер только грубо
            if (file.size > MAX_ATTACHMENT_BYTES && !isImage(file.name)) {
                skipped.push(`${file.name} — больше 15 МБ`);
                return;
            }
            state.form.files.push({
                file,
                url: isImage(file.name) ? URL.createObjectURL(file) : '',
            });
        });

        renderFormFiles();
        if (skipped.length) toast('Не приложено: ' + skipped.join('; '), 'error');
        else toast(list.length === 1 ? 'Файл добавлен к счёту' : `Добавлено файлов: ${list.length}`);
    }

    function removeFormFile(index) {
        const item = state.form.files[index];
        if (!item) return;
        if (item.url) URL.revokeObjectURL(item.url);
        state.form.files.splice(index, 1);
        renderFormFiles();
    }

    /** Ссылки на blob живут до перезагрузки страницы — освобождаем их сами. */
    function releaseFormFiles() {
        state.form.files.forEach(item => {
            if (item.url) URL.revokeObjectURL(item.url);
        });
        state.form.files = [];
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
            ${mismatchHtml(state.form.templateMismatch, 'iv2-form-mismatch')}
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

        const documentsBody = formDocumentsHtml();
        const documentsBadge = state.form.files.length
            ? `<span class="bx-badge b-appr">${withCount(state.form.files.length, 'файл', 'файла', 'файлов')}</span>`
            : '<span class="bx-badge b-warn">не приложен</span>';

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
                        ${sectionHtml('documents', 'Документ (скан счёта)', documentsBadge, documentsBody)}
                        ${sectionHtml('requisites', 'Контрагент и реквизиты', requisitesBadge, requisitesBody)}
                        ${sectionHtml('allocation', 'Распределение по салонам и статьям', allocationBadge, allocationBody)}
                        ${state.form.error ? `<div class="iv2-form-error">${escapeHtml(state.form.error)}</div>` : ''}
                    </div>
                    <div class="iv2-modal__foot">
                        ${state.form.mode === 'create'
                            ? `<button class="bx-btn bx-btn--ghost" type="button" id="iv2FormFromTemplate">Из шаблона</button>`
                            : ''}
                        ${isAdmin()
                            ? `<button class="bx-btn bx-btn--ghost" type="button" id="iv2FormSaveTemplate">Сохранить как шаблон</button>`
                            : ''}
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

        host.querySelectorAll('[data-form-file-input], [data-form-camera-input]').forEach(input => {
            input.addEventListener('change', () => {
                addFilesToForm(input.files);
                // Обнуляем, иначе повторный выбор того же файла не даст change
                input.value = '';
            });
        });

        host.querySelectorAll('[data-form-file-del]').forEach(button => {
            button.addEventListener('click', () => removeFormFile(Number(button.getAttribute('data-form-file-del'))));
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

        const fromTemplate = $('iv2FormFromTemplate');
        if (fromTemplate) fromTemplate.addEventListener('click', () => openTool('templates'));

        const saveTemplate = $('iv2FormSaveTemplate');
        if (saveTemplate) {
            saveTemplate.addEventListener('click', () => saveAsTemplate({
                payload: formTemplatePayload(),
                suggestedName: state.form.values.counterparty_name || '',
            }));
        }
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
        if (action === 'pick-file' || action === 'take-photo') {
            const zone = sourceElement.closest('[data-drop]');
            const input = zone && zone.querySelector(
                action === 'pick-file' ? '[data-form-file-input]' : '[data-form-camera-input]');
            if (input) input.click();
            return;
        }
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
                const files = state.form.files.map(item => item.file);
                releaseFormFiles();
                state.form = emptyForm();
                renderForm();
                toast('Изменения сохранены', 'success');
                // Файлы грузим после сохранения полей: uploadAttachments сам
                // перечитает счёт и покажет приложенное
                if (files.length) await uploadAttachments(invoiceId, files);
                else await refreshDetails(invoiceId);
            } else {
                const data = await apiPost('/api/invoices', payload);
                const newId = data.invoice && data.invoice.id;
                const files = state.form.files.map(item => item.file);
                releaseFormFiles();
                state.form = emptyForm();
                renderForm();
                toast('Счёт создан и отправлен на согласование', 'success');
                if (newId) {
                    // Счёт уже создан, поэтому неудача с файлом не должна
                    // выглядеть как неудача создания: uploadAttachments скажет,
                    // что именно не приложилось, а счёт всё равно откроется
                    if (files.length) await uploadAttachments(newId, files);
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
            else if (state.tools.open) closeTool();
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
        } else if (key === 'u' || key === 'г') {
            doAction('clarify', state.queueActiveId);
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
    // Инструменты раздела: справочники, контрагенты, ПланФакт
    //
    // Раздел заменяет старый целиком (решение №11 плана), поэтому всё, что
    // делалось из шапки старого раздела, должно делаться и здесь. Серверные
    // ручки те же — переписан только интерфейс, на токенах --bx-* и внутри
    // #iv2Modals: вынесенная в body модалка потеряла бы скоуп стилей .iv2.
    // =========================================================================

    const TOOL_BUTTONS = [
        { key: 'templates', label: 'Шаблоны' },
        { key: 'refs', label: 'Справочники' },
        { key: 'counterparties', label: 'Контрагенты' },
        { key: 'planfact', label: 'ПланФакт' },
    ];

    const TOOL_TITLES = {
        templates: 'Шаблоны счетов',
        refs: 'Справочники счетов',
        counterparties: 'Контрагенты',
        planfact: 'ПланФакт — авторазноска оплаченных счетов',
    };

    /**
     * Вкладки справочников. `api` — база роута: салоны (проекты) физически
     * живут в модуле кассовых смен, у них общая таблица stores, остальные
     * справочники принадлежат счетам. Обе группы построены по одному шаблону
     * (GET/POST списка, PUT/DELETE по id), поэтому одна модалка на всё.
     */
    const REF_TABS = [
        { key: 'categories',  label: 'Статьи расхода',     api: '/api/invoices' },
        { key: 'cities',      label: 'Города',             api: '/api/invoices' },
        { key: 'payers',      label: 'На кого выставлен',  api: '/api/invoices' },
        { key: 'vat-options', label: 'НДС',                api: '/api/invoices' },
        { key: 'stores',      label: 'Салоны (проекты)',   api: '/api/cash-shifts' },
    ];

    // Реквизиты расчётного счёта плательщика: своя запись у каждой компании,
    // все в одном кабинете Модульбанка под одним токеном. Пустые реквизиты =
    // этот плательщик через банк-автоматику не проводится.
    const PAYER_BANK_FIELDS = [
        { key: 'inn', label: 'ИНН' },
        { key: 'kpp', label: 'КПП' },
        { key: 'bank_account', label: 'Расчётный счёт' },
        { key: 'bank_name', label: 'Банк' },
        { key: 'bank_bik', label: 'БИК' },
        { key: 'bank_corr_account', label: 'Корр. счёт' },
    ];

    async function apiDelete(path) {
        const response = await fetch(path, { method: 'DELETE', credentials: 'include' });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.error || ('HTTP ' + response.status));
        return data;
    }

    /**
     * Перечитать справочники раздела после правки в модалке: селекты фильтров
     * и открытая форма счёта иначе останутся со старым списком до перезахода
     * в раздел.
     */
    async function reloadReferences() {
        state.refsLoaded = false;
        try {
            await loadReferences();
            fillFilterSelects();
            if (state.form.open) renderForm();
        } catch (error) {
            console.error('invoices-v2: не удалось перечитать справочники:', error);
        }
    }

    function openTool(key) {
        if (!isAdmin()) return;
        state.tools = emptyTools();
        state.tools.open = key;
        renderTools();
        if (key === 'refs') loadRefList();
        if (key === 'counterparties') loadCounterparties();
        if (key === 'templates') loadTemplates();
        if (key === 'planfact') state.tools.pf.tab = 'sync';
    }

    function closeTool() {
        state.tools = emptyTools();
        renderTools();
    }

    function toolsTabsHtml(tabs, current, attribute) {
        return `<div class="iv2-tabs iv2-tabs--tools">${tabs.map(tab => `
            <button class="iv2-tab${tab.key === current ? ' iv2-tab--active' : ''}" type="button"
                    ${attribute}="${escapeHtml(tab.key)}">${escapeHtml(tab.label)}</button>`).join('')}</div>`;
    }

    function renderTools() {
        const host = modalHost('tools');
        if (!host) return;

        const open = state.tools.open;
        if (!open) {
            host.innerHTML = '';
            return;
        }

        let body = '';
        if (open === 'templates') body = templatesBodyHtml();
        else if (open === 'refs') body = refsBodyHtml();
        else if (open === 'counterparties') body = counterpartiesBodyHtml();
        else if (open === 'planfact') body = planfactBodyHtml();

        host.innerHTML = `
            <div class="iv2-ovl iv2-ovl--tools" id="iv2ToolsOverlay"></div>
            <div class="iv2-modal iv2-modal--tools">
                <div class="iv2-modal__box iv2-modal__box--tools">
                    <div class="iv2-modal__head">
                        <h3 class="bx-card__title">${escapeHtml(TOOL_TITLES[open] || '')}</h3>
                        <button class="iv2-x" type="button" id="iv2ToolsClose" aria-label="Закрыть">
                            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                                 stroke-width="1.75" stroke-linecap="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>
                        </button>
                    </div>
                    <div class="iv2-modal__body">${body}</div>
                    <div class="iv2-modal__foot">
                        <button class="bx-btn bx-btn--ghost" type="button" id="iv2ToolsDone">Закрыть</button>
                    </div>
                </div>
            </div>`;

        [$('iv2ToolsClose'), $('iv2ToolsDone'), $('iv2ToolsOverlay')].forEach(element => {
            if (element) element.addEventListener('click', closeTool);
        });

        if (open === 'templates') bindTemplates();
        else if (open === 'refs') bindRefs();
        else if (open === 'counterparties') bindCounterparties();
        else if (open === 'planfact') bindPlanfact();
    }


    // ------------------------------------------------------------------ Шаблоны

    async function loadTemplates() {
        const templates = state.tools.templates;
        templates.loading = true;
        templates.error = '';
        renderTools();
        try {
            const data = await apiGet('/api/invoices/templates');
            templates.list = data.templates || [];
        } catch (error) {
            templates.list = [];
            templates.error = error.message;
        } finally {
            templates.loading = false;
            renderTools();
        }
    }

    function templatesBodyHtml() {
        const templates = state.tools.templates;
        if (templates.loading) return '<p class="iv2-tools-empty">Загрузка…</p>';
        if (templates.error) return `<p class="iv2-tools-empty">Не удалось загрузить: ${escapeHtml(templates.error)}</p>`;
        if (!templates.list.length) {
            return `<p class="iv2-tools-note">Шаблонов пока нет. Заведите счёт и нажмите в нём
                    «Сохранить как шаблон» — повторяющийся счёт (аренда, постоянный поставщик)
                    будет заводиться в два клика.</p>`;
        }

        return `<p class="iv2-tools-note">Шаблон подставляет всё, кроме суммы и даты оплаты —
                    их вы вводите каждый раз.</p>`
            + state.tools.templates.list.map(template => {
                const meta = [
                    template.counterparty_name,
                    template.city_name,
                    template.payer_name,
                    template.vat_name,
                    template.line_items && template.line_items.length
                        ? withCount(template.line_items.length, 'строка', 'строки', 'строк') + ' распределения'
                        : 'без распределения',
                ].filter(Boolean).join(' · ');

                return `
                    <div class="iv2-tools-row">
                        <div class="iv2-tools-row__main">
                            <div class="iv2-tools-row__name">${escapeHtml(template.name)}</div>
                            <div class="iv2-tools-row__req">${escapeHtml(meta)}</div>
                            ${template.payment_purpose
                                ? `<div class="iv2-hint">${escapeHtml(template.payment_purpose)}</div>` : ''}
                            ${mismatchHtml(template.requisite_mismatch)}
                        </div>
                        <div class="iv2-tools-row__btns">
                            <button class="bx-btn bx-btn--sm" type="button"
                                    data-tpl-act="use" data-tpl-id="${escapeHtml(template.id)}">Создать счёт</button>
                            <button class="bx-btn bx-btn--ghost bx-btn--sm" type="button"
                                    data-tpl-act="rename" data-tpl-id="${escapeHtml(template.id)}">Переименовать</button>
                            <button class="bx-btn bx-btn--ghost bx-btn--sm" type="button"
                                    data-tpl-act="delete" data-tpl-id="${escapeHtml(template.id)}">Удалить</button>
                        </div>
                    </div>`;
            }).join('');
    }

    /**
     * Расхождение реквизитов шаблона со справочником.
     * Показываем и в списке шаблонов, и в форме после подстановки: узнать о
     * смене банка поставщика надо до отправки платёжки, а не от банка.
     */
    function mismatchText(mismatch) {
        if (!mismatch) return '';
        const directoryBank = mismatch.directory_bank_name ? ` (${mismatch.directory_bank_name})` : '';
        return 'Реквизиты в шаблоне отличаются от справочника: расчётный счёт '
            + (mismatch.template_account || '—') + ', в справочнике '
            + (mismatch.directory_account || '—') + directoryBank
            + '. Проверьте перед отправкой в банк.';
    }

    function mismatchHtml(mismatch, extraClass) {
        if (!mismatch) return '';
        return `<div class="iv2-tools-row__warn${extraClass ? ' ' + extraClass : ''}">${
            escapeHtml(mismatchText(mismatch))}</div>`;
    }

    function bindTemplates() {
        const host = modalHost('tools');
        if (!host) return;
        host.querySelectorAll('[data-tpl-act]').forEach(button => {
            button.addEventListener('click', () => templateAction(
                button.getAttribute('data-tpl-act'), Number(button.getAttribute('data-tpl-id'))));
        });
    }

    async function templateAction(action, templateId) {
        const template = state.tools.templates.list.find(item => item.id === templateId);
        if (!template) return;

        if (action === 'use') {
            applyTemplate(template);
            return;
        }
        if (action === 'rename') {
            const name = await window.BarhatUI.prompt('Новое название шаблона:', template.name,
                { title: 'Переименовать шаблон', confirmText: 'Сохранить' });
            if (!name || !name.trim()) return;
            try {
                await apiPut('/api/invoices/templates/' + templateId, { name: name.trim() });
                await loadTemplates();
                toast('Название сохранено', 'success');
            } catch (error) {
                toast('Не удалось переименовать: ' + error.message, 'error');
            }
            return;
        }
        if (action === 'delete') {
            const ok = await window.BarhatUI.confirm(
                `Шаблон «${template.name}» пропадёт из списка. Счета, заведённые по нему, останутся.`,
                { title: 'Удалить шаблон?', confirmText: 'Удалить', danger: true });
            if (!ok) return;
            try {
                await apiDelete('/api/invoices/templates/' + templateId);
                await loadTemplates();
                toast('Шаблон удалён', 'success');
            } catch (error) {
                toast('Не удалось удалить: ' + error.message, 'error');
            }
        }
    }

    /**
     * Открыть форму нового счёта, заполненную из шаблона.
     *
     * Сумму счёта подставляем из сумм распределения: у аренды они те же из
     * месяца в месяц, и тогда счёт действительно заводится в два клика.
     * Если в шаблоне сумм нет — поле остаётся пустым.
     */
    function applyTemplate(template) {
        const form = emptyForm();
        form.open = true;
        form.mode = 'create';
        form.values.due_date = today();
        form.values.city_id = template.city_id || '';
        form.values.payer_id = template.payer_id || '';
        form.values.vat_id = template.vat_id || '';
        form.values.payment_purpose = template.payment_purpose || '';
        COUNTERPARTY_FIELDS.forEach(field => {
            form.values[field.key] = template[field.key] || '';
        });

        form.items = (template.line_items || []).map(item => ({
            store_id: item.store_id,
            expense_category_id: item.expense_category_id,
            amount: item.amount || '',
        }));
        if (!form.items.length) {
            form.items = [{ store_id: '', expense_category_id: '', amount: '' }];
        }

        const fromItems = form.items.reduce((sum, item) => sum + (Number(item.amount) || 0), 0);
        if (fromItems > 0) form.values.amount = fromItems;

        form.source = `Реквизиты из шаблона «${template.name}»`;
        form.templateMismatch = template.requisite_mismatch || null;
        // Реквизиты разошлись со справочником — раскрываем секцию, иначе
        // предупреждение окажется в свёрнутом блоке и его не увидят
        if (form.templateMismatch) form.openSections.requisites = true;

        state.form = form;
        closeTool();
        renderForm();
        toast(`Форма заполнена по шаблону «${template.name}»`);
    }

    /** «Сохранить как шаблон» — из формы счёта и из карточки. */
    async function saveAsTemplate(source) {
        const suggested = source.suggestedName || '';
        const name = await window.BarhatUI.prompt(
            'Название шаблона — по нему вы будете его находить:', suggested,
            { title: 'Сохранить как шаблон', confirmText: 'Сохранить', placeholder: 'Например: Аренда «Лазурная»' });
        if (name === null || !String(name).trim()) return;

        try {
            if (source.invoiceId) {
                await apiPost(`/api/invoices/${source.invoiceId}/save-as-template`, { name: name.trim() });
            } else {
                await apiPost('/api/invoices/templates', Object.assign({ name: name.trim() }, source.payload));
            }
            toast('Шаблон сохранён — он в кнопке «Шаблоны» наверху', 'success');
        } catch (error) {
            toast('Не удалось сохранить шаблон: ' + error.message, 'error');
        }
    }

    /** Что из открытой формы уходит в шаблон: всё, кроме суммы и даты оплаты. */
    function formTemplatePayload() {
        const values = state.form.values;
        const payload = {
            city_id: values.city_id ? Number(values.city_id) : null,
            payer_id: values.payer_id ? Number(values.payer_id) : null,
            vat_id: values.vat_id ? Number(values.vat_id) : null,
            payment_purpose: String(values.payment_purpose || '').trim(),
            line_items: state.form.items
                .filter(item => item.store_id && item.expense_category_id)
                .map(item => ({
                    store_id: Number(item.store_id),
                    expense_category_id: Number(item.expense_category_id),
                    amount: Number(item.amount) || 0,
                })),
        };
        COUNTERPARTY_FIELDS.forEach(field => {
            payload[field.key] = String(values[field.key] || '').trim() || null;
        });
        return payload;
    }

    // ------------------------------------------------------------------ Справочники

    function refApiBase() {
        const tab = REF_TABS.find(t => t.key === state.tools.refs.type);
        return tab ? tab.api : '/api/invoices';
    }

    async function loadRefList() {
        const refs = state.tools.refs;
        refs.loading = true;
        refs.error = '';
        renderTools();
        try {
            const data = await apiGet(`${refApiBase()}/${refs.type}`);
            // Ключ ответа совпадает с типом справочника, включая 'vat-options'
            refs.list = data[refs.type] || [];
        } catch (error) {
            refs.list = [];
            refs.error = error.message;
        } finally {
            refs.loading = false;
            renderTools();
        }
    }

    function refsBodyHtml() {
        const refs = state.tools.refs;
        return toolsTabsHtml(REF_TABS, refs.type, 'data-ref')
            + `<div class="iv2-tools-add">
                   <input class="iv2-input" type="text" id="iv2RefNewName" placeholder="Название"
                          autocomplete="off" value="${escapeHtml(refs.newName || '')}">
                   <button class="bx-btn bx-btn--sm" type="button" id="iv2RefAdd">Добавить</button>
               </div>
               <div id="iv2RefList">${refsListHtml()}</div>`;
    }

    function refsListHtml() {
        const refs = state.tools.refs;
        if (refs.loading) return '<p class="iv2-tools-empty">Загрузка…</p>';
        if (refs.error) return `<p class="iv2-tools-empty">Не удалось загрузить: ${escapeHtml(refs.error)}</p>`;
        if (!refs.list.length) return '<p class="iv2-tools-empty">Список пуст</p>';

        const isPayers = refs.type === 'payers';
        return refs.list.map(item => `
            <div class="iv2-tools-row">
                <div class="iv2-tools-row__main">
                    <div class="iv2-tools-row__name">${escapeHtml(item.name)}</div>
                    ${isPayers ? `<div class="iv2-hint">${item.bank_account
                        ? 'реквизиты банка заполнены' : 'реквизиты банка не заполнены'}</div>` : ''}
                </div>
                <div class="iv2-tools-row__btns">
                    <button class="bx-btn bx-btn--ghost bx-btn--sm" type="button"
                            data-ref-act="rename" data-ref-id="${escapeHtml(item.id)}">Изменить</button>
                    ${isPayers ? `<button class="bx-btn bx-btn--ghost bx-btn--sm" type="button"
                            data-ref-act="bank" data-ref-id="${escapeHtml(item.id)}">Реквизиты банка</button>` : ''}
                    <button class="bx-btn bx-btn--ghost bx-btn--sm" type="button"
                            data-ref-act="delete" data-ref-id="${escapeHtml(item.id)}">Удалить</button>
                </div>
                ${isPayers && String(refs.bankEditId) === String(item.id) ? payerBankFormHtml(item) : ''}
            </div>`).join('');
    }

    function payerBankFormHtml(item) {
        return `
            <div class="iv2-tools-bank" data-payer-form="${escapeHtml(item.id)}">
                <div class="iv2-tools-bank__grid">
                    ${PAYER_BANK_FIELDS.map(field => `
                        <div class="iv2-field">
                            <label class="iv2-field__label">${escapeHtml(field.label)}</label>
                            <input class="iv2-input" type="text" data-payer-field="${field.key}"
                                   value="${escapeHtml(item[field.key] || '')}">
                        </div>`).join('')}
                </div>
                <div class="iv2-tools-bank__btns">
                    <button class="bx-btn bx-btn--ok bx-btn--sm" type="button"
                            data-ref-act="bank-save" data-ref-id="${escapeHtml(item.id)}">Сохранить реквизиты</button>
                    <button class="bx-btn bx-btn--ghost bx-btn--sm" type="button" data-ref-act="bank-cancel">Отмена</button>
                </div>
            </div>`;
    }

    /** Перерисовать только список: иначе поле «Название» теряет набранный текст. */
    function renderRefList() {
        const host = $('iv2RefList');
        if (!host) return;
        host.innerHTML = refsListHtml();
        bindRefListActions();
    }

    function bindRefs() {
        const host = modalHost('tools');
        if (host) {
            host.querySelectorAll('[data-ref]').forEach(tab => {
                tab.addEventListener('click', () => {
                    state.tools.refs.type = tab.getAttribute('data-ref');
                    state.tools.refs.bankEditId = null;
                    state.tools.refs.list = [];
                    loadRefList();
                });
            });
        }

        const nameInput = $('iv2RefNewName');
        if (nameInput) {
            nameInput.addEventListener('input', () => { state.tools.refs.newName = nameInput.value; });
            nameInput.addEventListener('keydown', event => {
                if (event.key === 'Enter') { event.preventDefault(); addRefItem(); }
            });
        }
        const addButton = $('iv2RefAdd');
        if (addButton) addButton.addEventListener('click', addRefItem);

        bindRefListActions();
    }

    function bindRefListActions() {
        const host = $('iv2RefList');
        if (!host) return;
        host.querySelectorAll('[data-ref-act]').forEach(button => {
            button.addEventListener('click', () => {
                refAction(button.getAttribute('data-ref-act'), button.getAttribute('data-ref-id'));
            });
        });
    }

    async function refAction(action, id) {
        const refs = state.tools.refs;
        const item = refs.list.find(row => String(row.id) === String(id));

        if (action === 'bank') {
            refs.bankEditId = String(refs.bankEditId) === String(id) ? null : id;
            renderRefList();
            return;
        }
        if (action === 'bank-cancel') {
            refs.bankEditId = null;
            renderRefList();
            return;
        }
        if (action === 'bank-save') {
            await savePayerBank(id);
            return;
        }
        if (action === 'rename') {
            const name = await window.BarhatUI.prompt('Новое название:', item ? item.name : '', {
                title: 'Переименовать запись',
                confirmText: 'Сохранить',
            });
            if (!name || !name.trim()) return;
            try {
                await apiPut(`${refApiBase()}/${refs.type}/${id}`, { name: name.trim() });
                await loadRefList();
                await reloadReferences();
                toast('Название сохранено', 'success');
            } catch (error) {
                toast('Не удалось изменить: ' + error.message, 'error');
            }
            return;
        }
        if (action === 'delete') {
            const confirmed = await window.BarhatUI.confirm(
                `«${item ? item.name : id}» перестанет предлагаться в формах. Заведённые счета не изменятся.`,
                { title: 'Удалить запись из справочника?', confirmText: 'Удалить', danger: true }
            );
            if (!confirmed) return;
            try {
                await apiDelete(`${refApiBase()}/${refs.type}/${id}`);
                await loadRefList();
                await reloadReferences();
                toast('Запись удалена', 'success');
            } catch (error) {
                toast('Не удалось удалить: ' + error.message, 'error');
            }
        }
    }

    async function addRefItem() {
        const name = String(state.tools.refs.newName || '').trim();
        if (!name) {
            toast('Укажите название', 'error');
            return;
        }
        try {
            await apiPost(`${refApiBase()}/${state.tools.refs.type}`, { name });
            state.tools.refs.newName = '';
            await loadRefList();
            await reloadReferences();
            toast('Запись добавлена', 'success');
        } catch (error) {
            toast('Не удалось добавить: ' + error.message, 'error');
        }
    }

    async function savePayerBank(id) {
        const form = document.querySelector(`[data-payer-form="${id}"]`);
        if (!form) return;
        const payload = {};
        PAYER_BANK_FIELDS.forEach(field => {
            const input = form.querySelector(`[data-payer-field="${field.key}"]`);
            payload[field.key] = input ? input.value.trim() : '';
        });
        try {
            await apiPut(`/api/invoices/payers/${id}/bank-requisites`, payload);
            state.tools.refs.bankEditId = null;
            await loadRefList();
            toast('Реквизиты плательщика сохранены', 'success');
        } catch (error) {
            toast('Не удалось сохранить реквизиты: ' + error.message, 'error');
        }
    }

    // ------------------------------------------------------------------ Контрагенты

    /**
     * Поля справочника контрагентов — те же, что подставляются в счёт, и
     * ровно в том же порядке. Собраны из COUNTERPARTY_FIELDS намеренно:
     * разъехавшиеся списки означали бы, что в справочнике заполняют одно,
     * а в счёт уходит другое. `from` — имя поля в справочнике.
     */
    const CP_FIELDS = COUNTERPARTY_FIELDS.map((field, index) => ({
        key: field.from,
        label: index === 0 ? 'Наименование' : field.label,
        digits: field.digits,
        required: index === 0,
    }));

    async function loadCounterparties() {
        const cp = state.tools.cp;
        cp.loading = true;
        cp.error = '';
        renderTools();
        try {
            const data = await apiGet('/api/invoices/counterparties');
            cp.list = data.counterparties || [];
            cp.loaded = true;
        } catch (error) {
            cp.list = [];
            cp.error = error.message;
        } finally {
            cp.loading = false;
            renderTools();
        }
    }

    function counterpartiesBodyHtml() {
        const cp = state.tools.cp;
        return `
            <p class="iv2-tools-note">
                Справочник заполняется сам, когда счёт заводят с новыми реквизитами.
                Правка здесь не меняет уже выставленные счета — в них своя копия реквизитов.
            </p>
            <div class="iv2-tools-add">
                <input class="iv2-input" type="text" id="iv2CpSearch" autocomplete="off"
                       placeholder="Поиск по названию, ИНН или счёту" value="${escapeHtml(cp.search || '')}">
                <button class="bx-btn bx-btn--sm" type="button" id="iv2CpNew">Добавить контрагента</button>
            </div>
            <div id="iv2CpForm">${cpFormHtml()}</div>
            <div id="iv2CpList">${cpListHtml()}</div>`;
    }

    function visibleCounterparties() {
        const cp = state.tools.cp;
        const needle = String(cp.search || '').trim().toLowerCase();
        if (!needle) return cp.list;
        const onlyDigits = needle.replace(/\D/g, '');
        return cp.list.filter(item => {
            if (String(item.name || '').toLowerCase().includes(needle)) return true;
            if (!onlyDigits) return false;
            return String(item.inn || '').includes(onlyDigits)
                || String(item.bank_account || '').includes(onlyDigits);
        });
    }

    function cpListHtml() {
        const cp = state.tools.cp;
        if (cp.loading) return '<p class="iv2-tools-empty">Загрузка…</p>';
        if (cp.error) return `<p class="iv2-tools-empty">Не удалось загрузить: ${escapeHtml(cp.error)}</p>`;

        const visible = visibleCounterparties();
        if (!visible.length) {
            return `<p class="iv2-tools-empty">${cp.list.length ? 'Ничего не найдено' : 'Справочник пуст'}</p>`;
        }

        const problems = visible.filter(item => (item.requisite_warnings || []).length).length;
        const summary = `<p class="iv2-hint">Записей: ${visible.length}${
            problems ? `. С вопросами к реквизитам: ${problems}` : ''}.</p>`;

        return summary + visible.map(item => {
            const requisites = [
                item.inn ? 'ИНН ' + item.inn : null,
                item.kpp ? 'КПП ' + item.kpp : null,
                item.bank_account ? 'р/с ' + item.bank_account : null,
                item.bank_bik ? 'БИК ' + item.bank_bik : null,
            ].filter(Boolean).join(' · ');

            return `
                <div class="iv2-tools-row">
                    <div class="iv2-tools-row__main">
                        <div class="iv2-tools-row__name">${escapeHtml(item.name)}</div>
                        <div class="iv2-tools-row__req">${escapeHtml(requisites || 'реквизиты не заполнены')}</div>
                        ${item.bank_name ? `<div class="iv2-tools-row__req">${escapeHtml(item.bank_name)}</div>` : ''}
                        ${(item.requisite_warnings || []).map(warning =>
                            `<div class="iv2-tools-row__warn">${escapeHtml(warning)}</div>`).join('')}
                    </div>
                    <div class="iv2-tools-row__btns">
                        <button class="bx-btn bx-btn--ghost bx-btn--sm" type="button"
                                data-cp-act="edit" data-cp-id="${escapeHtml(item.id)}">Изменить</button>
                        <button class="bx-btn bx-btn--ghost bx-btn--sm" type="button"
                                data-cp-act="delete" data-cp-id="${escapeHtml(item.id)}">Удалить</button>
                    </div>
                </div>`;
        }).join('');
    }

    function cpFormHtml() {
        const form = state.tools.cp.form;
        if (!form) return '';
        return `
            <div class="iv2-tools-form">
                <div class="iv2-tools-form__title">${form.id ? 'Изменить контрагента' : 'Новый контрагент'}</div>
                <div class="iv2-tools-bank__grid">
                    ${CP_FIELDS.map(field => {
                        const value = form.values[field.key] || '';
                        const issue = cpFieldIssue(field, value);
                        return `
                            <div class="iv2-field">
                                <label class="iv2-field__label">${escapeHtml(field.label)}${field.required ? ' *' : ''}</label>
                                <input class="iv2-input${issue ? ' iv2-input--bad' : ''}" type="text"
                                       data-cp-field="${field.key}" value="${escapeHtml(value)}" autocomplete="off">
                                ${issue ? `<div class="iv2-field__err">${escapeHtml(issue)}</div>` : ''}
                            </div>`;
                    }).join('')}
                </div>
                <div class="iv2-tools-bank__btns">
                    <button class="bx-btn bx-btn--ok bx-btn--sm" type="button" id="iv2CpSave"
                            ${state.tools.cp.saving ? 'disabled' : ''}>Сохранить</button>
                    <button class="bx-btn bx-btn--ghost bx-btn--sm" type="button" id="iv2CpCancel">Отмена</button>
                </div>
            </div>`;
    }

    /** Претензия к длине реквизита — та же проверка, что в форме счёта. */
    function cpFieldIssue(field, value) {
        if (!field.digits) return '';
        const onlyDigits = String(value || '').replace(/\D/g, '');
        if (!onlyDigits || field.digits.indexOf(onlyDigits.length) !== -1) return '';
        return `${field.label}: ${onlyDigits.length} ${plural(onlyDigits.length, 'цифра', 'цифры', 'цифр')}, `
            + `нужно ${field.digits.join(' или ')}`;
    }

    /** Точечное обновление претензии к полю: подсветка и текст под ним. */
    function updateCpFieldIssue(input, key) {
        const field = CP_FIELDS.find(item => item.key === key);
        if (!field) return;
        const issue = cpFieldIssue(field, input.value);
        input.classList.toggle('iv2-input--bad', Boolean(issue));

        const wrap = input.parentNode;
        if (!wrap) return;
        let error = wrap.querySelector('.iv2-field__err');
        if (issue) {
            if (!error) {
                error = document.createElement('div');
                error.className = 'iv2-field__err';
                wrap.appendChild(error);
            }
            error.textContent = issue;
        } else if (error) {
            error.remove();
        }
    }

    function openCpForm(counterparty) {
        const values = {};
        CP_FIELDS.forEach(field => { values[field.key] = (counterparty && counterparty[field.key]) || ''; });
        state.tools.cp.form = { id: counterparty ? counterparty.id : null, values };
        renderTools();
    }

    function closeCpForm() {
        state.tools.cp.form = null;
        renderTools();
    }

    /** Перерисовать только список: поле поиска иначе теряет фокус на каждой букве. */
    function renderCpList() {
        const host = $('iv2CpList');
        if (!host) return;
        host.innerHTML = cpListHtml();
        bindCpListActions();
    }

    function bindCounterparties() {
        const search = $('iv2CpSearch');
        if (search) {
            search.addEventListener('input', () => {
                state.tools.cp.search = search.value;
                renderCpList();
            });
        }
        const newButton = $('iv2CpNew');
        if (newButton) newButton.addEventListener('click', () => openCpForm(null));

        const save = $('iv2CpSave');
        if (save) save.addEventListener('click', saveCounterparty);
        const cancel = $('iv2CpCancel');
        if (cancel) cancel.addEventListener('click', closeCpForm);

        const formHost = $('iv2CpForm');
        if (formHost) {
            formHost.querySelectorAll('[data-cp-field]').forEach(input => {
                const key = input.getAttribute('data-cp-field');
                // Подсказку о длине правим точечно, без перерисовки формы.
                // Перерисовка по `change` выглядела бы естественнее, но она
                // пересоздаёт кнопки между mousedown и click, и «Сохранить»
                // после правки поля перестала бы срабатывать.
                input.addEventListener('input', () => {
                    if (!state.tools.cp.form) return;
                    state.tools.cp.form.values[key] = input.value;
                    updateCpFieldIssue(input, key);
                });
            });
        }

        bindCpListActions();
    }

    function bindCpListActions() {
        const host = $('iv2CpList');
        if (!host) return;
        host.querySelectorAll('[data-cp-act]').forEach(button => {
            const id = Number(button.getAttribute('data-cp-id'));
            const action = button.getAttribute('data-cp-act');
            button.addEventListener('click', () => {
                if (action === 'edit') openCpForm(state.tools.cp.list.find(item => item.id === id));
                if (action === 'delete') removeCounterparty(id);
            });
        });
    }

    async function saveCounterparty() {
        const form = state.tools.cp.form;
        if (!form) return;

        const payload = {};
        CP_FIELDS.forEach(field => {
            payload[field.key] = String(form.values[field.key] || '').trim() || null;
        });
        if (!payload.name) {
            toast('Укажите наименование контрагента', 'error');
            return;
        }

        state.tools.cp.saving = true;
        renderTools();
        try {
            if (form.id) await apiPut('/api/invoices/counterparties/' + form.id, payload);
            else await apiPost('/api/invoices/counterparties', payload);
            state.tools.cp.form = null;
            toast('Контрагент сохранён', 'success');
            await loadCounterparties();
        } catch (error) {
            toast('Не удалось сохранить: ' + error.message, 'error');
        } finally {
            state.tools.cp.saving = false;
            renderTools();
        }
    }

    async function removeCounterparty(id) {
        const item = state.tools.cp.list.find(row => row.id === id);
        const confirmed = await window.BarhatUI.confirm(
            `Убрать «${item ? item.name : id}» из справочника? Счета и их реквизиты останутся на месте.`,
            { title: 'Удаление контрагента', confirmText: 'Убрать', danger: true }
        );
        if (!confirmed) return;

        try {
            await apiDelete('/api/invoices/counterparties/' + id);
            if (state.tools.cp.form && state.tools.cp.form.id === id) state.tools.cp.form = null;
            toast('Контрагент убран из справочника', 'success');
            await loadCounterparties();
        } catch (error) {
            toast('Не удалось удалить: ' + error.message, 'error');
        }
    }

    // ------------------------------------------------------------------ ПланФакт

    const PF_TABS = [
        { key: 'sync', label: 'Синхронизация' },
        { key: 'mapping', label: 'Сопоставление' },
        { key: 'unmatched', label: 'Требует внимания' },
    ];

    const PF_POLL_INTERVAL_MS = 2000;
    const PF_POLL_ATTEMPTS = 60;

    function switchPlanfactTab(tab) {
        state.tools.pf.tab = tab;
        renderTools();
        if (tab === 'mapping' && !state.tools.pf.mapping) loadPlanfactMapping();
        if (tab === 'unmatched' && !state.tools.pf.unmatched) loadPlanfactUnmatched();
    }

    function planfactBodyHtml() {
        const pf = state.tools.pf;
        let body = '';
        if (pf.tab === 'sync') body = planfactSyncHtml();
        else if (pf.tab === 'mapping') body = planfactMappingHtml();
        else body = planfactUnmatchedHtml();
        return toolsTabsHtml(PF_TABS, pf.tab, 'data-pf-tab') + body;
    }

    function planfactSyncHtml() {
        const pf = state.tools.pf;
        return `
            <p class="iv2-tools-note">
                Ищет в ПланФакт операции с кодом счёта (REF-000123) в назначении платежа и разносит их
                по проекту и статье из распределения счёта. «Проверить» ничего не меняет — только
                показывает, что будет сделано.
            </p>
            <div class="iv2-tools-bank__btns">
                <button class="bx-btn bx-btn--ghost bx-btn--sm" type="button" id="iv2PfDryRun"
                        ${pf.busy ? 'disabled' : ''}>Проверить (без записи)</button>
                <button class="bx-btn bx-btn--sm" type="button" id="iv2PfRun"
                        ${pf.busy ? 'disabled' : ''}>Синхронизировать</button>
            </div>
            ${pf.status ? `<p class="iv2-hint iv2-pf-status">${escapeHtml(pf.status)}</p>` : ''}
            ${pf.result ? planfactResultHtml(pf.result) : ''}`;
    }

    function planfactResultHtml(result) {
        const matched = result.matched || [];
        const unmatched = result.unmatched || [];
        if (!matched.length && !unmatched.length) {
            return '<p class="iv2-tools-empty">Подходящих операций не найдено</p>';
        }
        return reportSection('Будут разнесены', matched.map(item => ({
            id: item.invoice_id,
            label: (item.invoice_number || item.match_code || ''),
            amount: item.operation_amount,
        })), true)
            + reportSection('Требуют внимания', unmatched.map(item => ({
                id: item.operation_id,
                label: item.match_code || item.operation_id,
                reason: item.reason,
            })));
    }

    async function runPlanfactSync(dryRun) {
        const pf = state.tools.pf;
        pf.busy = true;
        pf.status = dryRun ? 'Проверяю…' : 'Запускаю синхронизацию…';
        pf.result = null;
        renderTools();

        try {
            const data = await apiPost('/api/invoices/planfact/sync', { dry_run: dryRun });
            if (dryRun) {
                pf.status = 'Проверка завершена, в ПланФакт ничего не записано';
                pf.result = data;
            } else {
                pf.status = 'Синхронизация идёт в фоне…';
                pollPlanfactSync();
            }
        } catch (error) {
            pf.status = 'Не удалось: ' + error.message;
        } finally {
            pf.busy = false;
            renderTools();
        }
    }

    /**
     * Опрос статуса фонового прогона. Прекращается, как только модалку закрыли
     * или ушли с раздела: висящий таймер продолжал бы дёргать сервер и писать
     * в состояние закрытого окна.
     */
    async function pollPlanfactSync() {
        for (let attempt = 0; attempt < PF_POLL_ATTEMPTS; attempt++) {
            await new Promise(resolve => setTimeout(resolve, PF_POLL_INTERVAL_MS));
            if (state.tools.open !== 'planfact' || !pageIsActive()) return;

            let status;
            try {
                status = (await apiGet('/api/invoices/planfact/sync-status')).status;
            } catch (error) {
                state.tools.pf.status = 'Статус синхронизации недоступен: ' + error.message;
                renderTools();
                return;
            }

            if (status && status.status === 'started') continue;

            if (!status) {
                state.tools.pf.status = 'Статус синхронизации недоступен';
            } else if (status.status === 'completed') {
                state.tools.pf.status = `Готово: разнесено ${status.matched_count}, `
                    + `требует внимания ${status.unmatched_count}`;
                // Список «требует внимания» мог измениться — перечитаем при
                // следующем открытии вкладки
                state.tools.pf.unmatched = null;
            } else {
                state.tools.pf.status = 'Ошибка: ' + (status.error_message || 'подробности в логах сервера');
            }
            renderTools();
            loadSummary();
            loadList(false);
            return;
        }
        state.tools.pf.status = 'Синхронизация идёт дольше обычного — загляните позже';
        renderTools();
    }

    async function loadPlanfactMapping() {
        const pf = state.tools.pf;
        pf.mappingLoading = true;
        pf.mappingError = '';
        renderTools();

        try {
            // Списки из ПланФакт живые и могут не приехать — тогда null и
            // ручной ввод id вместо выпадающего списка. Своя часть (салоны и
            // статьи) от этого не зависит и должна показаться в любом случае.
            const [stores, categories, projects, pfCategories] = await Promise.all([
                apiGet('/api/invoices/planfact/mappings/stores'),
                apiGet('/api/invoices/categories'),
                apiGet('/api/invoices/planfact/projects').catch(() => null),
                apiGet('/api/invoices/planfact/categories').catch(() => null),
            ]);
            pf.mapping = {
                stores: stores.stores || [],
                categories: categories.categories || [],
                pfProjects: projects ? (projects.projects || []) : null,
                pfCategories: pfCategories ? (pfCategories.categories || []) : null,
            };
        } catch (error) {
            pf.mapping = null;
            pf.mappingError = error.message;
        } finally {
            pf.mappingLoading = false;
            renderTools();
        }
    }

    function planfactMappingHtml() {
        const pf = state.tools.pf;
        if (pf.mappingLoading) return '<p class="iv2-tools-empty">Загрузка…</p>';
        if (pf.mappingError) return `<p class="iv2-tools-empty">Не удалось загрузить: ${escapeHtml(pf.mappingError)}</p>`;
        if (!pf.mapping) return '<p class="iv2-tools-empty">Нет данных</p>';

        const { stores, categories, pfProjects, pfCategories } = pf.mapping;
        return `
            <div class="iv2-pf-block">
                <div class="iv2-tools-form__title">Салоны → проекты ПланФакт</div>
                ${pfProjects === null
                    ? '<p class="iv2-hint">Список проектов из ПланФакт не пришёл — впишите id вручную.</p>' : ''}
                ${stores.length
                    ? stores.map(store => mappingRowHtml(store.id, store.name, store.planfact_project_id,
                        pfProjects, 'store')).join('')
                    : '<p class="iv2-tools-empty">Салонов нет</p>'}
            </div>
            <div class="iv2-pf-block">
                <div class="iv2-tools-form__title">Статьи расхода → статьи ПланФакт</div>
                ${pfCategories === null
                    ? '<p class="iv2-hint">Список статей из ПланФакт не пришёл — впишите id вручную.</p>' : ''}
                ${categories.length
                    ? categories.map(category => mappingRowHtml(category.id, category.name,
                        category.planfact_category_id, pfCategories, 'category')).join('')
                    : '<p class="iv2-tools-empty">Статей нет</p>'}
            </div>`;
    }

    /** Строка сопоставления: выпадающий список, а если ПланФакт молчит — поле ввода. */
    function mappingRowHtml(id, label, currentValue, options, kind) {
        const attribute = `data-pf-map="${kind}" data-pf-id="${escapeHtml(id)}"`;
        const control = options
            ? `<select class="iv2-select" ${attribute}>
                   <option value="">— не сопоставлено —</option>
                   ${options.map(option => {
                       const value = option.projectId ?? option.operationCategoryId;
                       const title = option.title ?? option.name ?? String(value);
                       const selected = String(value) === String(currentValue ?? '') ? ' selected' : '';
                       return `<option value="${escapeHtml(value)}"${selected}>${escapeHtml(title)}</option>`;
                   }).join('')}
               </select>`
            // Только цифры: id ПланФакта числовой, а нечисловое значение
            // отсюда роняло разноску на сервере (см. план от 2026-08-26)
            : `<input class="iv2-input" type="text" inputmode="numeric" pattern="[0-9]*"
                      placeholder="id в ПланФакт (только цифры)"
                      value="${escapeHtml(currentValue || '')}" ${attribute}>`;

        return `<div class="iv2-pf-row">
                    <span class="iv2-pf-row__label">${escapeHtml(label)}</span>
                    ${control}
                </div>`;
    }

    async function savePlanfactMapping(kind, id, value) {
        const path = kind === 'store'
            ? `/api/invoices/planfact/mappings/stores/${id}`
            : `/api/invoices/categories/${id}/planfact-mapping`;
        const payload = kind === 'store'
            ? { planfact_project_id: value }
            : { planfact_category_id: value };
        try {
            await apiPut(path, payload);
            // Держим состояние в согласии с сервером: вкладку можно закрыть и
            // открыть заново, а перезапроса при этом нет.
            const mapping = state.tools.pf.mapping;
            if (mapping) {
                const list = kind === 'store' ? mapping.stores : mapping.categories;
                const row = (list || []).find(item => String(item.id) === String(id));
                if (row) {
                    if (kind === 'store') row.planfact_project_id = value || null;
                    else row.planfact_category_id = value || null;
                }
            }
        } catch (error) {
            toast('Не удалось сохранить сопоставление: ' + error.message, 'error');
        }
    }

    async function loadPlanfactUnmatched() {
        const pf = state.tools.pf;
        pf.unmatchedLoading = true;
        pf.unmatchedError = '';
        renderTools();
        try {
            const data = await apiGet('/api/invoices/planfact/unmatched');
            pf.unmatched = data.unmatched || [];
        } catch (error) {
            pf.unmatched = [];
            pf.unmatchedError = error.message;
        } finally {
            pf.unmatchedLoading = false;
            renderTools();
        }
    }

    function planfactUnmatchedHtml() {
        const pf = state.tools.pf;
        if (pf.unmatchedLoading) return '<p class="iv2-tools-empty">Загрузка…</p>';
        if (pf.unmatchedError) return `<p class="iv2-tools-empty">Не удалось загрузить: ${escapeHtml(pf.unmatchedError)}</p>`;
        if (!pf.unmatched || !pf.unmatched.length) {
            return '<p class="iv2-tools-empty">Нерешённых операций нет</p>';
        }

        return pf.unmatched.map(item => `
            <div class="iv2-tools-row">
                <div class="iv2-tools-row__main">
                    <div class="iv2-tools-row__name">${escapeHtml(item.match_code || item.planfact_operation_id)}${
                        item.operation_amount ? ' · ' + escapeHtml(money(item.operation_amount)) : ''}</div>
                    <div class="iv2-tools-row__req">${escapeHtml(item.reason || '')}</div>
                    <div class="iv2-hint">${escapeHtml(fmtCreated(item.detected_at))}</div>
                </div>
                <div class="iv2-tools-row__btns">
                    <button class="bx-btn bx-btn--ghost bx-btn--sm" type="button"
                            data-pf-resolve="${escapeHtml(item.id)}">Разнесено вручную</button>
                </div>
            </div>`).join('');
    }

    async function resolvePlanfactUnmatched(id) {
        try {
            await apiPost(`/api/invoices/planfact/unmatched/${id}/resolve`, {});
            toast('Операция убрана из списка', 'success');
            await loadPlanfactUnmatched();
        } catch (error) {
            toast('Не удалось отметить: ' + error.message, 'error');
        }
    }

    function bindPlanfact() {
        const host = modalHost('tools');
        if (!host) return;

        host.querySelectorAll('[data-pf-tab]').forEach(tab => {
            tab.addEventListener('click', () => switchPlanfactTab(tab.getAttribute('data-pf-tab')));
        });

        const dryRun = $('iv2PfDryRun');
        if (dryRun) dryRun.addEventListener('click', () => runPlanfactSync(true));
        const run = $('iv2PfRun');
        if (run) run.addEventListener('click', () => runPlanfactSync(false));

        host.querySelectorAll('[data-pf-map]').forEach(control => {
            const kind = control.getAttribute('data-pf-map');
            const id = control.getAttribute('data-pf-id');
            // У выпадающего списка достаточно change, у поля ввода ждём ухода
            // с него: сохранять на каждую набранную цифру id незачем.
            const event = control.tagName === 'SELECT' ? 'change' : 'blur';
            if (control.tagName !== 'SELECT') {
                // Отсекаем нецифры на вводе, а не при сохранении: иначе человек
                // видит в поле одно, а уходит на сервер другое
                control.addEventListener('input', () => {
                    const digitsOnly = control.value.replace(/\D/g, '');
                    if (control.value !== digitsOnly) control.value = digitsOnly;
                });
            }
            control.addEventListener(event, () => savePlanfactMapping(kind, id, control.value.trim()));
        });

        host.querySelectorAll('[data-pf-resolve]').forEach(button => {
            button.addEventListener('click', () => resolvePlanfactUnmatched(button.getAttribute('data-pf-resolve')));
        });
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
        loadAutoArchived();

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
