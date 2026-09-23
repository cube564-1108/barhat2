/**
 * Выбор периода одним полем — общий компонент дашборда (window.BarhatDateRange).
 *
 * Зачем свой, а не пара <input type="date"> (обращение #18, модуль счетов):
 *   - пара полей разъезжается по строкам сетки фильтров, и диапазон перестаёт
 *     читаться как диапазон;
 *   - один день приходится вводить дважды, двумя открытиями календаря;
 *   - каждое поле уходит запросом само по себе, то есть выбор периода — это
 *     два похода к списку, первый из которых бессмыслен («с 16.09 по всегда»);
 *   - нативный календарь не умеет показывать диапазон и не оформляется по
 *     DESIGN-SPEC.md.
 * По тем же причинам в счетах уже сделан свой мультивыбор вместо <select multiple>.
 *
 * Компонент ничего не знает о модуле, который его позвал: он отдаёт
 * onChange(from, to) строками «YYYY-MM-DD» (или пустыми строками, если период
 * сброшен). Перевод в UTC, параметры запроса и перезагрузка списка — дело
 * вызывающего модуля.
 *
 * Все даты собираются через `new Date(год, месяц - 1, день)`. `new Date('2026-09-16')`
 * — это UTC-полночь: при отрицательном смещении пояса она даёт вчерашний день,
 * и календарь молча съезжает на сутки.
 *
 * Стили — date-range.css (скоуплены под .bxdr, токены --bx-* из DESIGN-SPEC.md).
 * Сторож — node scripts/test_date_range.js.
 *
 * Использование:
 *   const picker = window.BarhatDateRange.create({
 *       mount: document.getElementById('host'),
 *       from: '2026-09-01', to: '2026-09-16',
 *       onChange: (from, to) => reload(from, to),
 *   });
 *   picker.setRange(from, to);   // снаружи, без onChange
 *   picker.getRange();           // { from, to }
 */

(function () {
    'use strict';

    const MONTHS = [
        'Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь',
        'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь',
    ];
    const WEEKDAYS = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс'];

    const DEFAULT_PLACEHOLDER = 'Все даты';
    const HINT_START = 'Кликните день или протяните мышью';
    const HINT_END = 'Выберите второй день — или этот же ещё раз';
    const HINT_BAD = 'Дата не распознана: формат дд.мм.гггг';

    // Ниже этой ширины два месяца рядом не помещаются — показываем один.
    // Считается при каждом открытии: окно Пульса меняет размер вместе с сайдбаром.
    const NARROW_PX = 760;

    // Все живые компоненты: клик мимо и Esc ловятся одним слушателем на документ,
    // а не своим у каждого поля
    const instances = [];
    let documentBound = false;

    // =========================================================================
    // Даты
    // =========================================================================

    function pad2(value) {
        return String(value).padStart(2, '0');
    }

    /** Date -> «YYYY-MM-DD» по местному поясу. */
    function toKey(date) {
        return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`;
    }

    /**
     * «YYYY-MM-DD» -> Date местной полуночи, либо null, если это не дата.
     * 31 февраля браузер молча превращает в 3 марта — такое считаем не датой.
     */
    function parseKey(value) {
        const parts = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(value || '').trim());
        if (!parts) return null;
        const year = Number(parts[1]);
        const month = Number(parts[2]) - 1;
        const day = Number(parts[3]);
        const date = new Date(year, month, day);
        if (date.getFullYear() !== year || date.getMonth() !== month || date.getDate() !== day) return null;
        return date;
    }

    /** Значение для состояния: дата или пустая строка. */
    function normalizeKey(value) {
        return parseKey(value) ? String(value).trim() : '';
    }

    /**
     * Ручной ввод -> «YYYY-MM-DD».
     * Пустая строка — это «граница не задана» (''), непонятный текст — null:
     * разные случаи, и путать их нельзя, иначе опечатка молча сбросит фильтр.
     */
    function parseHuman(text) {
        const raw = String(text || '').trim();
        if (!raw) return '';
        if (/^\d{4}-\d{2}-\d{2}$/.test(raw)) return parseKey(raw) ? raw : null;
        const digits = raw.replace(/\D/g, '');
        let day;
        let month;
        let year;
        if (digits.length === 8) {
            day = digits.slice(0, 2); month = digits.slice(2, 4); year = digits.slice(4);
        } else if (digits.length === 6) {
            day = digits.slice(0, 2); month = digits.slice(2, 4); year = '20' + digits.slice(4);
        } else {
            return null;
        }
        const key = `${year}-${month}-${day}`;
        return parseKey(key) ? key : null;
    }

    /** «YYYY-MM-DD» -> «16.09.2026». */
    function formatHuman(key) {
        const date = parseKey(key);
        if (!date) return '';
        return `${pad2(date.getDate())}.${pad2(date.getMonth() + 1)}.${date.getFullYear()}`;
    }

    /** Сегодня по часам устройства — тем же способом, что и поля дат в дашборде. */
    function todayKey() {
        if (window.BarhatTime && window.BarhatTime.todayInputValue) {
            return window.BarhatTime.todayInputValue();
        }
        return toKey(new Date());
    }

    function shiftDays(key, days) {
        const date = parseKey(key);
        if (!date) return '';
        return toKey(new Date(date.getFullYear(), date.getMonth(), date.getDate() + days));
    }

    function addMonths(date, count) {
        return new Date(date.getFullYear(), date.getMonth() + count, 1);
    }

    /** Понедельник той недели, в которую попадает дата (неделя у нас с понедельника). */
    function weekStart(date) {
        const offset = (date.getDay() + 6) % 7;
        return new Date(date.getFullYear(), date.getMonth(), date.getDate() - offset);
    }

    /**
     * Подпись периода. Одна дата — один день, один год — короткое начало
     * («12.09 – 20.09.2026»): в чипе и на кнопке год дважды только мешает.
     */
    function rangeLabel(from, to, placeholder) {
        const start = normalizeKey(from);
        const end = normalizeKey(to);
        const empty = placeholder === undefined ? DEFAULT_PLACEHOLDER : placeholder;
        if (!start && !end) return empty;
        if (start && !end) return 'с ' + formatHuman(start);
        if (!start && end) return 'по ' + formatHuman(end);
        if (start === end) return formatHuman(start);
        const a = parseKey(start);
        const b = parseKey(end);
        if (a.getFullYear() === b.getFullYear()) {
            return `${pad2(a.getDate())}.${pad2(a.getMonth() + 1)} – ${formatHuman(end)}`;
        }
        return `${formatHuman(start)} – ${formatHuman(end)}`;
    }

    /** Готовые периоды: их и открывают в большинстве случаев. */
    function presetList(today) {
        const now = parseKey(today) || new Date();
        const monthStart = new Date(now.getFullYear(), now.getMonth(), 1);
        const monthEnd = new Date(now.getFullYear(), now.getMonth() + 1, 0);
        const prevStart = new Date(now.getFullYear(), now.getMonth() - 1, 1);
        const prevEnd = new Date(now.getFullYear(), now.getMonth(), 0);
        const yesterday = shiftDays(today, -1);
        return [
            { label: 'Сегодня', from: today, to: today },
            { label: 'Вчера', from: yesterday, to: yesterday },
            { label: '7 дней', from: shiftDays(today, -6), to: today },
            { label: '30 дней', from: shiftDays(today, -29), to: today },
            { label: 'Этот месяц', from: toKey(monthStart), to: toKey(monthEnd) },
            { label: 'Прошлый месяц', from: toKey(prevStart), to: toKey(prevEnd) },
        ];
    }

    // =========================================================================
    // Разметка
    // =========================================================================

    function escapeHtml(str) {
        return String(str === null || str === undefined ? '' : str)
            .replace(/[&<>"']/g, (c) => ({
                '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
            })[c]);
    }

    const ICON_CALENDAR = '<svg class="bxdr__ico" width="16" height="16" viewBox="0 0 24 24" fill="none"'
        + ' stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"'
        + ' aria-hidden="true"><rect x="3" y="5" width="18" height="16" rx="2"/><path d="M8 3v4"/>'
        + '<path d="M16 3v4"/><path d="M3 11h18"/></svg>';

    const ICON_CLOSE = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor"'
        + ' stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M18 6 6 18"/>'
        + '<path d="m6 6 12 12"/></svg>';

    function navIcon(direction) {
        const path = direction < 0 ? 'm15 18-6-6 6-6' : 'm9 18 6-6-6-6';
        return `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"`
            + ` stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">`
            + `<path d="${path}"/></svg>`;
    }

    function rootHtml(placeholder) {
        return `
            <div class="bxdr__control">
                <button type="button" class="bxdr__btn" data-dr-btn aria-haspopup="dialog" aria-expanded="false">
                    ${ICON_CALENDAR}
                    <span class="bxdr__value" data-dr-label>${escapeHtml(placeholder)}</span>
                </button>
                <button type="button" class="bxdr__x" data-dr-clear hidden aria-label="Сбросить период">
                    ${ICON_CLOSE}
                </button>
            </div>
            <div class="bxdr__pop" data-dr-pop hidden role="dialog" aria-label="Выбор периода">
                <div class="bxdr__presets" data-dr-presets></div>
                <div class="bxdr__main">
                    <div class="bxdr__inputs">
                        <label class="bxdr__inp" data-dr-wrap="from">
                            <span>с</span>
                            <input type="text" data-dr-in="from" inputmode="numeric"
                                   placeholder="дд.мм.гггг" aria-label="Начало периода">
                        </label>
                        <span class="bxdr__dash" aria-hidden="true">–</span>
                        <label class="bxdr__inp" data-dr-wrap="to">
                            <span>по</span>
                            <input type="text" data-dr-in="to" inputmode="numeric"
                                   placeholder="дд.мм.гггг" aria-label="Конец периода">
                        </label>
                    </div>
                    <div class="bxdr__cals" data-dr-cals></div>
                    <div class="bxdr__foot">
                        <span class="bxdr__hint" data-dr-hint>${escapeHtml(HINT_START)}</span>
                        <span class="bxdr__acts">
                            <button type="button" class="bxdr__link" data-dr-reset>Сбросить</button>
                            <button type="button" class="bxdr__done" data-dr-done>Готово</button>
                        </span>
                    </div>
                </div>
            </div>`;
    }

    // =========================================================================
    // Компонент
    // =========================================================================

    function create(options) {
        const opts = options || {};
        const mount = opts.mount;
        if (!mount) throw new Error('BarhatDateRange: не задан mount');

        const placeholder = opts.placeholder || DEFAULT_PLACEHOLDER;
        const monthsWanted = Number(opts.months) > 0 ? Number(opts.months) : 2;
        const onChange = typeof opts.onChange === 'function' ? opts.onChange : function () {};

        const state = {
            from: normalizeKey(opts.from),
            to: normalizeKey(opts.to),
            // Первый выбранный край, пока второй не назван. Живёт только пока
            // окно открыто: закрытие превращает его в «этот один день».
            anchor: null,
            preview: null,
            dragFrom: null,
            dragging: false,
            dragMoved: false,
            suppressClick: false,
            open: false,
            months: monthsWanted,
            view: null,
            lastEvent: null,
        };
        let lastEmitted = state.from + '|' + state.to;

        mount.classList.add('bxdr');
        mount.innerHTML = rootHtml(placeholder);

        const btn = mount.querySelector('[data-dr-btn]');
        const clearBtn = mount.querySelector('[data-dr-clear]');
        const pop = mount.querySelector('[data-dr-pop]');
        const labelHost = mount.querySelector('[data-dr-label]');
        const presetsHost = mount.querySelector('[data-dr-presets]');
        const calsHost = mount.querySelector('[data-dr-cals]');
        const hintHost = mount.querySelector('[data-dr-hint]');
        const inputFrom = mount.querySelector('[data-dr-in="from"]');
        const inputTo = mount.querySelector('[data-dr-in="to"]');

        if (opts.buttonId && btn) btn.id = opts.buttonId;
        if (opts.ariaLabel && btn) btn.setAttribute('aria-label', opts.ariaLabel);

        // --- отрисовка -------------------------------------------------------

        /** Какой отрезок сейчас подсвечен: выбираемый важнее выбранного. */
        function activeRange() {
            if (state.preview) return state.preview;
            if (state.from && state.to) return [state.from, state.to];
            if (state.anchor) return [state.anchor, state.anchor];
            if (state.from) return [state.from, state.from];
            if (state.to) return [state.to, state.to];
            return null;
        }

        function dayClasses(key, outside) {
            const classes = [];
            if (outside) classes.push('is-out');
            if (key === todayKey()) classes.push('is-today');
            const range = activeRange();
            if (range) {
                const [start, end] = range[0] <= range[1] ? range : [range[1], range[0]];
                if (key === start) classes.push('is-start');
                if (key === end) classes.push('is-end');
                if (key > start && key < end) classes.push('is-in');
            }
            return classes.length ? ' ' + classes.join(' ') : '';
        }

        function monthHtml(monthDate, first, last) {
            const year = monthDate.getFullYear();
            const month = monthDate.getMonth();
            const gridStart = weekStart(new Date(year, month, 1));
            const daysInMonth = new Date(year, month + 1, 0).getDate();
            const offset = (new Date(year, month, 1).getDay() + 6) % 7;
            const weeks = Math.ceil((offset + daysInMonth) / 7);

            const cells = [];
            for (let i = 0; i < weeks * 7; i++) {
                const day = new Date(gridStart.getFullYear(), gridStart.getMonth(), gridStart.getDate() + i);
                const key = toKey(day);
                const outside = day.getMonth() !== month;
                cells.push(`<button type="button" class="bxdr__day${dayClasses(key, outside)}"`
                    + ` data-day="${key}" tabindex="${outside ? '-1' : '0'}">${day.getDate()}</button>`);
            }

            const prev = first
                ? `<button type="button" class="bxdr__nav" data-dr-prev aria-label="Предыдущий месяц">${navIcon(-1)}</button>`
                : '<span class="bxdr__nav bxdr__nav--ghost" aria-hidden="true"></span>';
            const next = last
                ? `<button type="button" class="bxdr__nav" data-dr-next aria-label="Следующий месяц">${navIcon(1)}</button>`
                : '<span class="bxdr__nav bxdr__nav--ghost" aria-hidden="true"></span>';

            return `
                <div class="bxdr__cal">
                    <div class="bxdr__cal-head">
                        ${prev}
                        <span class="bxdr__cal-title" data-dr-month="${year}-${pad2(month + 1)}">${MONTHS[month]} ${year}</span>
                        ${next}
                    </div>
                    <div class="bxdr__wd">${WEEKDAYS.map(d => `<span>${d}</span>`).join('')}</div>
                    <div class="bxdr__days">${cells.join('')}</div>
                </div>`;
        }

        function renderCalendars() {
            if (!calsHost || !state.view) return;
            const months = [];
            for (let i = 0; i < state.months; i++) {
                months.push(monthHtml(addMonths(state.view, i), i === 0, i === state.months - 1));
            }
            // Перерисовка целиком — здесь она безопасна: внутри календаря нет
            // ни прокрутки, ни полей ввода (поля «с» и «по» лежат выше и не
            // трогаются), а обработчики висят на контейнере, а не на днях.
            calsHost.innerHTML = months.join('');
        }

        function renderPresets() {
            if (!presetsHost) return;
            presetsHost.innerHTML = presetList(todayKey()).map((preset, index) => `
                <button type="button" class="bxdr__preset" data-dr-preset="${index}">
                    ${escapeHtml(preset.label)}
                </button>`).join('');
        }

        function setHint(text) {
            if (hintHost) hintHost.textContent = text;
        }

        function markInput(input, bad) {
            if (!input || !input.parentElement) return;
            input.parentElement.classList.toggle('bxdr__inp--bad', Boolean(bad));
        }

        function syncInputs() {
            if (inputFrom) inputFrom.value = formatHuman(state.from);
            if (inputTo) inputTo.value = formatHuman(state.to);
            markInput(inputFrom, false);
            markInput(inputTo, false);
        }

        function syncControl() {
            if (labelHost) labelHost.textContent = rangeLabel(state.from, state.to, placeholder);
            const active = Boolean(state.from || state.to);
            mount.classList.toggle('bxdr--active', active);
            if (clearBtn) clearBtn.hidden = !active;
        }

        function syncAll() {
            syncControl();
            if (state.open) {
                syncInputs();
                renderCalendars();
            }
        }

        // --- изменение периода ------------------------------------------------

        function emit() {
            const key = state.from + '|' + state.to;
            if (key === lastEmitted) return;
            lastEmitted = key;
            onChange(state.from, state.to);
        }

        /** Записать период (края сортируются) и сообщить наружу. Окно не трогает. */
        function applyRange(a, b) {
            const first = normalizeKey(a);
            const second = normalizeKey(b);
            let from = '';
            let to = '';
            if (first && second) {
                from = first <= second ? first : second;
                to = first <= second ? second : first;
            } else {
                from = first || second || '';
                to = from;
            }
            state.from = from;
            state.to = to;
            state.anchor = null;
            state.preview = null;
            syncAll();
            emit();
        }

        /** Период выбран целиком: применяем и закрываем — ждать больше нечего. */
        function commit(a, b) {
            state.anchor = null;
            applyRange(a, b);
            closePopover(true);
        }

        function clearRange() {
            state.from = '';
            state.to = '';
            state.anchor = null;
            state.preview = null;
            syncAll();
            emit();
        }

        function setPreview(a, b) {
            const next = [a, b];
            if (state.preview && state.preview[0] === a && state.preview[1] === b) return;
            state.preview = next;
            renderCalendars();
        }

        function pickDay(key) {
            if (state.anchor) {
                const anchor = state.anchor;
                state.anchor = null;
                commit(anchor, key);
                return;
            }
            // Первый клик: край есть, второго ждём. Клик по тому же дню ещё раз
            // (или закрытие окна) даст период в один день — так и просили.
            state.anchor = key;
            state.preview = null;
            setPreview(key, key);
            setHint(HINT_END);
        }

        // --- окно -------------------------------------------------------------

        /** С какого месяца показывать: от начала периода, но так, чтобы конец был виден. */
        function startMonth() {
            const anchorKey = state.from || state.to || todayKey();
            const date = parseKey(anchorKey) || new Date();
            let view = new Date(date.getFullYear(), date.getMonth(), 1);
            const end = parseKey(state.to);
            if (end && state.months > 1) {
                const endMonth = new Date(end.getFullYear(), end.getMonth(), 1);
                const shown = addMonths(view, state.months - 1);
                if (endMonth > shown) view = addMonths(endMonth, -(state.months - 1));
            }
            return view;
        }

        function placePopover() {
            if (!pop || !pop.getBoundingClientRect || !window.innerWidth) return;
            mount.classList.remove('bxdr--right');
            const rect = pop.getBoundingClientRect();
            if (rect && rect.width && rect.right > window.innerWidth - 8) {
                mount.classList.add('bxdr--right');
            }
        }

        function openPopover() {
            closeOthers();
            // Два месяца рядом требуют места: на узком экране показываем один,
            // иначе окно уезжает за край и половина дней недоступна
            state.months = (window.innerWidth && window.innerWidth < NARROW_PX) ? 1 : monthsWanted;
            state.view = startMonth();
            state.anchor = null;
            state.preview = null;
            state.open = true;
            if (pop) pop.hidden = false;
            mount.classList.add('bxdr--open');
            if (btn) btn.setAttribute('aria-expanded', 'true');
            renderPresets();
            renderCalendars();
            syncInputs();
            setHint(HINT_START);
            placePopover();
        }

        /**
         * Закрыть окно. Незавершённый выбор (кликнули один день и ушли) считаем
         * выбором этого дня: человек его уже назвал. Отмена — это Esc, она
         * зовётся с keepPending и ничего не применяет.
         */
        function closePopover(keepPending) {
            if (!keepPending && state.anchor) {
                const anchor = state.anchor;
                state.anchor = null;
                applyRange(anchor, anchor);
            }
            state.anchor = null;
            state.preview = null;
            state.dragging = false;
            state.dragFrom = null;
            state.dragMoved = false;
            state.open = false;
            if (pop) pop.hidden = true;
            mount.classList.remove('bxdr--open', 'bxdr--right');
            if (btn) btn.setAttribute('aria-expanded', 'false');
            syncControl();
        }

        function closeOthers() {
            instances.forEach(other => {
                if (other !== instance && other.isOpen()) other.close();
            });
        }

        // --- ручной ввод -------------------------------------------------------

        /**
         * Прочитать поля «с» и «по».
         * @returns {{ok: boolean, from: string, to: string, partial: boolean}}
         */
        function readInputs() {
            const from = parseHuman(inputFrom ? inputFrom.value : '');
            const to = parseHuman(inputTo ? inputTo.value : '');
            markInput(inputFrom, from === null);
            markInput(inputTo, to === null);
            if (from === null || to === null) {
                setHint(HINT_BAD);
                return { ok: false, from: '', to: '', partial: false };
            }
            return { ok: true, from, to, partial: Boolean(from) !== Boolean(to) };
        }

        /** Применить то, что набрано руками. Одна граница — ждём вторую. */
        function applyInputs(force) {
            const read = readInputs();
            if (!read.ok) return false;
            if (!read.from && !read.to) {
                clearRange();
                closePopover(true);
                return true;
            }
            if (read.partial && !force) {
                const single = read.from || read.to;
                state.anchor = single;
                setPreview(single, single);
                setHint(HINT_END);
                return false;
            }
            commit(read.from || read.to, read.to || read.from);
            return true;
        }

        // --- события -----------------------------------------------------------

        /**
         * Ближайший предок с таким атрибутом — включая сам узел.
         * Своя, а не closest(): внутри дня лежит текст, и клик приходит по нему.
         */
        function closestWith(node, attr) {
            let el = node;
            while (el) {
                if (el.getAttribute && el.getAttribute(attr) !== null) return el;
                el = el.parentElement;
            }
            return null;
        }

        // Свой клик помечаем объектом события, а не проверкой «узел внутри
        // поля»: перелистывание месяца заменяет разметку календаря, и к моменту
        // проверки кликнутая кнопка уже выброшена из дерева — окно закрывалось бы
        // на каждом перелистывании.
        mount.addEventListener('click', event => { state.lastEvent = event; });

        if (btn) {
            btn.addEventListener('click', () => {
                if (state.open) closePopover(false);
                else openPopover();
            });
        }

        if (clearBtn) {
            clearBtn.addEventListener('click', event => {
                if (event && event.stopPropagation) event.stopPropagation();
                clearRange();
                closePopover(true);
            });
        }

        if (calsHost) {
            calsHost.addEventListener('click', event => {
                const target = event.target;
                if (closestWith(target, 'data-dr-prev')) {
                    state.view = addMonths(state.view, -1);
                    renderCalendars();
                    return;
                }
                if (closestWith(target, 'data-dr-next')) {
                    state.view = addMonths(state.view, 1);
                    renderCalendars();
                    return;
                }
                const cell = closestWith(target, 'data-day');
                if (!cell) return;
                // После протяжки браузер ещё раз шлёт click по дню, на котором
                // отпустили кнопку: без этого он начал бы новый выбор
                if (state.suppressClick) {
                    state.suppressClick = false;
                    return;
                }
                pickDay(cell.getAttribute('data-day'));
            });

            calsHost.addEventListener('pointerdown', event => {
                const cell = closestWith(event.target, 'data-day');
                if (!cell) return;
                // Иначе протяжка выделяет числа как текст
                if (event.preventDefault) event.preventDefault();
                state.dragging = true;
                state.dragFrom = cell.getAttribute('data-day');
                state.dragMoved = false;
            });

            const hover = event => {
                const cell = closestWith(event.target, 'data-day');
                if (!cell) return;
                const key = cell.getAttribute('data-day');
                if (state.dragging && state.dragFrom) {
                    if (key !== state.dragFrom) state.dragMoved = true;
                    setPreview(state.dragFrom, key);
                } else if (state.anchor) {
                    setPreview(state.anchor, key);
                }
            };
            calsHost.addEventListener('pointerover', hover);
            calsHost.addEventListener('pointermove', hover);

            calsHost.addEventListener('pointerup', event => {
                if (!state.dragging) return;
                const cell = closestWith(event.target, 'data-day');
                const from = state.dragFrom;
                const moved = state.dragMoved;
                state.dragging = false;
                state.dragFrom = null;
                state.dragMoved = false;
                // Отпустили там же, где нажали, — это обычный клик, его доведёт
                // обработчик click: два края в один клик не выбираются
                if (!cell || !moved) return;
                state.suppressClick = true;
                commit(from, cell.getAttribute('data-day'));
            });
        }

        if (presetsHost) {
            presetsHost.addEventListener('click', event => {
                const button = closestWith(event.target, 'data-dr-preset');
                if (!button) return;
                const preset = presetList(todayKey())[Number(button.getAttribute('data-dr-preset'))];
                if (preset) commit(preset.from, preset.to);
            });
        }

        [inputFrom, inputTo].forEach(input => {
            if (!input) return;
            input.addEventListener('change', () => { applyInputs(false); });
            input.addEventListener('keydown', event => {
                if (!event || event.key !== 'Enter') return;
                if (event.preventDefault) event.preventDefault();
                applyInputs(true);
            });
        });

        const resetBtn = mount.querySelector('[data-dr-reset]');
        if (resetBtn) {
            resetBtn.addEventListener('click', () => {
                clearRange();
                closePopover(true);
            });
        }

        const doneBtn = mount.querySelector('[data-dr-done]');
        if (doneBtn) {
            doneBtn.addEventListener('click', () => {
                if (applyInputs(true)) return;
                closePopover(false);
            });
        }

        // --- наружу -------------------------------------------------------------

        const instance = {
            element: mount,
            isOpen() { return state.open; },
            open() { if (!state.open) openPopover(); },
            close() { closePopover(true); },
            getRange() { return { from: state.from, to: state.to }; },
            /** Выставить период снаружи — без onChange: это не выбор человека. */
            setRange(from, to) {
                state.from = normalizeKey(from);
                state.to = normalizeKey(to);
                state.anchor = null;
                state.preview = null;
                lastEmitted = state.from + '|' + state.to;
                syncAll();
            },
            clear() { clearRange(); },
            destroy() {
                const index = instances.indexOf(instance);
                if (index >= 0) instances.splice(index, 1);
                mount.innerHTML = '';
                mount.classList.remove('bxdr', 'bxdr--open', 'bxdr--active', 'bxdr--right');
            },
            // Для общих слушателей документа
            _ownsEvent(event) { return state.lastEvent === event; },
            _outsideClick() { closePopover(false); },
            _cancelDrag() {
                if (!state.dragging) return;
                state.dragging = false;
                state.dragFrom = null;
                state.dragMoved = false;
            },
        };

        instances.push(instance);
        bindDocument();
        syncAll();
        return instance;
    }

    /**
     * Клик мимо, Esc и брошенная протяжка — одним слушателем на документ.
     * Клик мимо применяет незавершённый выбор («я выбрал этот день»), Esc —
     * отменяет: это разные намерения.
     */
    function bindDocument() {
        if (documentBound || typeof document === 'undefined' || !document.addEventListener) return;
        documentBound = true;

        document.addEventListener('click', event => {
            instances.slice().forEach(instance => {
                if (!instance.isOpen() || instance._ownsEvent(event)) return;
                instance._outsideClick();
            });
        });

        document.addEventListener('keydown', event => {
            if (!event || event.key !== 'Escape') return;
            instances.slice().forEach(instance => {
                if (instance.isOpen()) instance.close();
            });
        });

        // Отпустили кнопку мимо календаря — протяжка закончилась там же, иначе
        // следующий клик по дню будет считаться её продолжением
        document.addEventListener('pointerup', () => {
            instances.slice().forEach(instance => instance._cancelDrag());
        });
    }

    window.BarhatDateRange = {
        create,
        label: rangeLabel,
        formatHuman,
        parseHuman,
        todayKey,
    };
})();
