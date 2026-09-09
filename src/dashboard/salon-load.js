/**
 * Загрузка салонов — сетка «часы × салоны» по времени готовности заказов.
 *
 * Данные из /api/salon-load/*; вся арифметика на сервере, здесь только показ.
 * Три состояния ячейки приходят с сервера и НЕ склеиваются: «ёмкость не
 * задана» (процента нет), «салон закрыт» (не ноль загрузки) и число.
 *
 * Цвет всегда дублируется числом внутри ячейки: сплошная зелёно-красная шкала
 * не читается дальтониками, а решение по ней принимают каждый день.
 *
 * Диалоги — только window.BarhatUI: нативные alert/confirm внутри iframe
 * Пульса молча игнорируются, и кнопка выглядит сломанной.
 */

(function () {
    'use strict';

    const NBSP = ' ';
    const WEEKDAYS = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'];

    let state = {
        view: 'day',
        date: todayIso(),
        weekDays: 7,
        data: null,
        week: null,
        alerts: null,
        isAdmin: false,
        loading: false
    };

    // ================= Утилиты =================

    function todayIso() {
        const now = new Date();
        return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
    }

    function shiftDate(iso, days) {
        const parts = iso.split('-').map(Number);
        const d = new Date(parts[0], parts[1] - 1, parts[2] + days);
        return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
    }

    function dateLabel(iso) {
        const months = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
            'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря'];
        const parts = iso.split('-').map(Number);
        const d = new Date(parts[0], parts[1] - 1, parts[2]);
        return `${parts[2]} ${months[parts[1] - 1]}, ${WEEKDAYS[(d.getDay() + 6) % 7].toLowerCase()}`;
    }

    // Экранирование через replace, а не через textContent: тот не экранирует
    // кавычку и молча резал бы значения внутри value="..."
    function esc(value) {
        return String(value === null || value === undefined ? '' : value)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    function num(value, digits) {
        if (value === null || value === undefined) return '—';
        const text = value.toFixed(digits === undefined ? 1 : digits).replace('.', ',');
        return text.replace(/,0$/, '');
    }

    function pct(value) {
        if (value === null || value === undefined) return '—';
        return Math.round(value) + '%';
    }

    function plural(n, one, few, many) {
        const m10 = n % 10, m100 = n % 100;
        if (m10 === 1 && m100 !== 11) return one;
        if (m10 >= 2 && m10 <= 4 && !(m100 >= 12 && m100 <= 14)) return few;
        return many;
    }

    function icon(paths, size) {
        return `<svg width="${size || 16}" height="${size || 16}" viewBox="0 0 24 24" fill="none"
            stroke="currentColor" stroke-width="1.75" stroke-linecap="round"
            stroke-linejoin="round">${paths}</svg>`;
    }

    const ICON_ALERT = '<path d="M12 9v4M12 17h.01"/><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>';
    const ICON_CLOCK = '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>';

    async function api(url, options) {
        const response = await fetch(url, options);
        const result = await response.json().catch(() => ({}));
        if (!response.ok || result.success === false) {
            throw new Error(result.error || `Ошибка ${response.status}`);
        }
        return result;
    }

    function postOptions(body) {
        return {
            method: 'POST',
            // Заголовок обязателен: ручки записи закрыты require_ajax_header.
            // Значение проверяется точно ('barhat-dashboard', см. AJAX_HEADER_VALUE
            // в src/auth.py) — привычное 'XMLHttpRequest' даст 403.
            headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'barhat-dashboard' },
            body: JSON.stringify(body)
        };
    }

    function toast(message, kind) {
        if (window.BarhatUI && window.BarhatUI.toast) window.BarhatUI.toast(message, kind);
    }

    // ================= Загрузка =================

    function host() {
        return document.getElementById('salonLoadContent');
    }

    async function load() {
        const container = host();
        if (!container) return;

        if (!state.data && !state.week) {
            container.innerHTML = '<div class="sload-empty"><p>Загружаем сетку…</p></div>';
        }
        state.loading = true;

        try {
            if (state.view === 'day') {
                const [result, alerts] = await Promise.all([
                    api(`/api/salon-load/day?date=${encodeURIComponent(state.date)}`),
                    // Предупреждения грузим вместе с сеткой: отдельной кнопки
                    // «проверить перегруз» быть не должно — её никто не нажмёт.
                    api('/api/salon-load/alerts').catch(() => null)
                ]);
                state.data = result.data;
                state.isAdmin = !!result.data.can_edit;
                state.alerts = alerts ? alerts.data : null;
            } else {
                const result = await api(
                    `/api/salon-load/week?from=${encodeURIComponent(state.date)}&days=${state.weekDays}`);
                state.week = result.data;
            }
        } catch (error) {
            container.innerHTML = `<div class="sload-empty"><h3>Не удалось загрузить сетку</h3>
                <p>${esc(error.message)}</p></div>`;
            state.loading = false;
            return;
        }

        state.loading = false;
        render();
    }

    // ================= Отрисовка =================

    function render() {
        const container = host();
        if (!container) return;

        // Перерисовываем экран целиком, поэтому прокрутку возвращаем руками:
        // иначе после клика по ячейке человек оказывается в шапке страницы.
        const pageScroll = window.scrollY;
        const gridScroll = (container.querySelector('.sload-grid-wrap') || {}).scrollLeft || 0;

        container.innerHTML = state.view === 'day' ? renderDay() : renderWeek();

        const wrap = container.querySelector('.sload-grid-wrap');
        if (wrap) wrap.scrollLeft = gridScroll;
        window.scrollTo(0, pageScroll);
    }

    function header() {
        return `
        <header class="sload-header">
            <div class="sload-header__left">
                <span class="sload-header__icon">${icon(ICON_CLOCK, 24)}</span>
                <div>
                    <h1 class="sload-header__title">Загрузка салонов</h1>
                    <p class="sload-header__subtitle">Сколько работы приходится на каждый час
                        по времени готовности заказов. Время — местное, по часам салона.</p>
                </div>
            </div>
            <div class="sload-header__actions">
                ${state.isAdmin ? `
                    <button class="sload-btn sload-btn--onhead" data-open="capacity">Ёмкость салонов</button>
                    <button class="sload-btn sload-btn--onhead" data-open="norms">Нормы времени</button>
                    <button class="sload-btn sload-btn--onhead" data-open="weights">Надбавки</button>
                    <button class="sload-btn sload-btn--onhead" data-open="statuses">Статусы заказов</button>` : ''}
            </div>
        </header>`;
    }

    function toolbar() {
        return `
        <div class="sload-toolbar">
            <div class="sload-tabs">
                <button class="sload-tab ${state.view === 'day' ? 'sload-tab--active' : ''}"
                    data-view="day">День</button>
                <button class="sload-tab ${state.view === 'week' ? 'sload-tab--active' : ''}"
                    data-view="week">Неделя</button>
            </div>
            <div class="sload-date">
                <button class="sload-btn sload-btn--ghost" data-shift="-1" title="Предыдущий день">←</button>
                <input type="date" class="sload-input" id="sloadDate" value="${esc(state.date)}">
                <button class="sload-btn sload-btn--ghost" data-shift="1" title="Следующий день">→</button>
                <button class="sload-btn sload-btn--ghost" data-today>Сегодня</button>
            </div>
        </div>`;
    }

    function freshnessNote(freshness) {
        if (!freshness) return '';
        if (freshness.error) {
            return note('bad', 'Состояние синхронизации неизвестно', esc(freshness.error));
        }
        if (!freshness.last_sync_at) {
            return note('warn', 'Синхронизация с CRM ещё не проходила',
                'Сетка пустая не потому, что заказов нет, а потому, что их ещё не загрузили.');
        }
        // Пустой справочник статусов даёт честный ноль во всех ячейках —
        // отличить его от «заказов нет» иначе невозможно.
        if (freshness.statuses_as_load === 0) {
            return note('bad', 'Ни один статус не считается нагрузкой',
                'Сетка показывает нули не потому, что заказов нет: в справочнике статусов ' +
                'нечего считать. Откройте «Статусы заказов» и отметьте рабочие.');
        }
        if (freshness.last_sync_status === 'failed') {
            return note('bad', 'Последняя синхронизация упала',
                `Данные на ${esc(freshness.last_sync_at)} UTC. Пока синк не пройдёт, ` +
                'сетка показывает устаревшую картину.');
        }
        return '';
    }

    function note(kind, title, body) {
        const cls = kind === 'bad' ? 'sload-note--bad' : (kind === 'info' ? 'sload-note--info' : '');
        return `<div class="sload-note ${cls}">
            <span style="line-height:0;flex:0 0 auto">${icon(ICON_ALERT, 18)}</span>
            <div class="sload-note__body">
                <div class="sload-note__title">${title}</div>
                <div>${body}</div>
            </div>
        </div>`;
    }

    // Плашки «N товаров без веса, проценты приблизительные» здесь больше нет.
    // Приблизительных процентов не осталось: заказ считается базой за сборку, а
    // надбавку получает только тот товар, которому её проставили руками.
    // Разбор нагрузки на базу и надбавки живёт в справочнике надбавок — там он
    // отвечает на вопрос «почему столько», а на главном экране был бы упрёком
    // за незаполненный справочник, который заполнять не обязательно.

    function capacityNote(data) {
        const withoutCapacity = (data.stores || []).filter(store =>
            store.cells.every(cell => cell.capacity === null && !cell.closed));
        if (!withoutCapacity.length) return '';
        const names = withoutCapacity.map(s => s.store_name).join(', ');
        return note('warn', 'Ёмкость не задана',
            `Проценты не считаются для: ${esc(names)}. ` +
            (state.isAdmin ? 'Задайте часы работы и норму в «Ёмкости салонов».'
                : 'Обратитесь к администратору.'));
    }

    function alertsCard() {
        const alerts = state.alerts;
        if (!alerts) return '';

        // Молчание модуля и «всё спокойно» — разные вещи. Если синк не
        // проходил больше двух часов, предупреждений просто нет физически.
        const stale = alerts.stale_sync
            ? note('bad', 'Синхронизация давно не проходила',
                'Предупреждения о перегрузе сейчас не считаются, а проценты в сетке ' +
                'описывают устаревшую картину.')
            : '';

        const items = alerts.items || [];
        if (!items.length) {
            return stale + (alerts.stats && alerts.stats.total ? `
                <div class="sload-note sload-note--info">
                    <span style="line-height:0;flex:0 0 auto">${icon(ICON_ALERT, 18)}</span>
                    <div class="sload-note__body">
                        <div class="sload-note__title">Перегруженных слотов нет</div>
                        <div>За 30 дней было ${alerts.stats.total}
                            ${plural(alerts.stats.total, 'предупреждение', 'предупреждения', 'предупреждений')},
                            из них ${alerts.stats.resolved} слотов разгрузили после сигнала.</div>
                    </div>
                </div>` : '');
        }

        return stale + `
        <div class="sload-card">
            <h2 class="sload-card__title">Перегруз: ${items.length}
                ${plural(items.length, 'слот', 'слота', 'слотов')}</h2>
            <p class="sload-card__caption">За сутки — успеть вывести ещё одного флориста,
                за 3 часа — успеть перенести заказ. Рядом — куда его перенести.</p>
            <div class="sload-extra">
                ${items.map(alert => `
                    <div class="sload-extra__item" style="cursor:default;flex-direction:column;align-items:stretch;gap:8px">
                        <div style="display:flex;align-items:center;justify-content:space-between;gap:10px">
                            <span>
                                <span class="sload-extra__label">${esc(alert.store_name)} ·
                                    ${esc(dateLabel(alert.date))} · ${String(alert.hour).padStart(2, '0')}:00</span><br>
                                <span class="sload-extra__value">${pct(alert.percent)}</span>
                                <span class="sload-extra__label">${num(alert.units)} из
                                    ${num(alert.capacity)} ед. ·
                                    ${alert.horizon === 'soon' ? 'ближайшие часы' : 'завтра'}</span>
                            </span>
                            <button class="sload-btn sload-btn--ghost" data-dismiss="${alert.id}">Разобрался</button>
                        </div>
                        <div class="sload-extra__label">
                            ${alert.free_slots && alert.free_slots.length
                                ? 'Свободно рядом: ' + alert.free_slots.map(slot =>
                                    `<button class="sload-badge sload-badge--pickup" style="border:none;cursor:pointer"
                                        data-free-slot data-store="${alert.store_id}" data-date="${esc(slot.date)}"
                                        data-hour="${slot.hour}">${esc(dateLabel(slot.date).split(',')[0])},
                                        ${String(slot.hour).padStart(2, '0')}:00 — запас ${num(slot.free_units)} ед.</button>`).join(' ')
                                : 'Свободных слотов рядом нет — здесь нужен ещё один флорист, а не перенос.'}
                        </div>
                    </div>`).join('')}
            </div>
            ${alerts.stats && alerts.stats.total ? `<p class="sload-card__caption" style="margin-top:10px">
                За 30 дней: ${alerts.stats.total}
                ${plural(alerts.stats.total, 'предупреждение', 'предупреждения', 'предупреждений')},
                разгружено после сигнала — ${alerts.stats.resolved}. Если разгруженных ноль,
                предупреждения не работают.</p>` : ''}
        </div>`;
    }

    function renderDay() {
        const data = state.data;
        if (!data) return '';

        if (data.no_stores) {
            return header() + toolbar() + `<div class="sload-card"><div class="sload-empty">
                <h3>Салоны не привязаны</h3>
                <p>За вашей учётной записью не закреплено ни одного салона со складом CRM.
                   Обратитесь к администратору — сетка появится сразу после привязки.</p>
            </div></div>`;
        }

        const hours = data.hours || [];
        const rows = (data.stores || []).map(store => `
            <tr>
                <th class="sload-grid__salon">
                    <span class="sload-grid__salon-name">${esc(store.store_name)}</span>
                    <span class="sload-grid__salon-meta">${esc(store.city || '')}${store.day_percent !== null && store.day_percent !== undefined
                        ? ` · за день ${pct(store.day_percent)}` : ''}</span>
                </th>
                ${hours.map(hour => cell(store, store.cells[hour])).join('')}
            </tr>`).join('');

        return header() + toolbar() + alertsCard() +
            freshnessNote(data.freshness) + capacityNote(data) + `
            <div class="sload-card">
                <div style="display:flex;align-items:baseline;justify-content:space-between;gap:12px;flex-wrap:wrap">
                    <div>
                        <h2 class="sload-card__title">${esc(dateLabel(data.date))}</h2>
                        <p class="sload-card__caption">Часы — местное время салона. В ячейке:
                            процент загрузки и число заказов. Трудоёмкость — в подсказке и по клику.</p>
                    </div>
                </div>
                <div class="sload-grid-wrap">
                    <table class="sload-grid">
                        <thead><tr>
                            <th class="sload-grid__corner"></th>
                            ${hours.map(h => `<th class="sload-grid__hour">${String(h).padStart(2, '0')}</th>`).join('')}
                        </tr></thead>
                        <tbody>${rows}</tbody>
                    </table>
                </div>
                ${legend()}
            </div>
            ${extraRows(data)}`;
    }

    function cell(store, data) {
        if (!data) return '<td></td>';

        if (data.closed) {
            return `<td><div class="sload-cell sload-cell--closed" title="Салон закрыт${
                data.reason ? ': ' + esc(data.reason) : ''}">
                <span class="sload-cell__value">—</span>
                <span class="sload-cell__sub">закрыт</span></div></td>`;
        }

        if (!data.orders && data.capacity === null) {
            return '<td><div class="sload-cell sload-cell--empty"><span class="sload-cell__value">·</span></div></td>';
        }

        const level = data.level === 'unknown' && !data.orders ? 'empty' : data.level;
        const title = [
            `${data.orders} ${plural(data.orders, 'заказ', 'заказа', 'заказов')}`,
            `${num(data.units)} ед. трудоёмкости`,
            data.capacity === null ? 'ёмкость не задана' : `ёмкость ${num(data.capacity)} ед.`,
            data.pickup_orders ? `самовывоз: ${data.pickup_orders}` : '',
            data.reason ? `причина: ${data.reason}` : ''
        ].filter(Boolean).join(' · ');

        // В ячейке — процент и число заказов. Единицы трудоёмкости остаются в
        // подсказке и в разборе слота: заказы человек считает глазами и может
        // проверить, а «7.4 ед.» без раскрытия проверить нечем.
        return `<td><button class="sload-cell sload-cell--${esc(level)}" data-cell
            data-store="${store.store_id}" data-hour="${data.hour}" title="${esc(title)}">
            <span class="sload-cell__value">${data.percent === null
                ? data.orders : pct(data.percent)}${data.level === 'over'
                    ? ` <span class="sload-cell__icon">${icon(ICON_ALERT, 11)}</span>` : ''}</span>
            <span class="sload-cell__sub">${data.percent === null
                ? 'зак.' : data.orders + ' зак.'}</span>
        </button></td>`;
    }

    function legend() {
        const item = (color, text, extra) => `<span class="sload-legend__item">
            <span class="sload-legend__swatch" style="background:${color};${extra || ''}"></span>${text}</span>`;
        return `<div class="sload-legend">
            ${item('#ecfdf5', 'в норме')}
            ${item('#fffbeb', `впритык (от ${state.data.thresholds.tight}%)`)}
            ${item('#fdeceb', `перегруз (от ${state.data.thresholds.over}%)`, 'box-shadow:inset 0 0 0 1.5px #e2a5a3')}
            ${item('#f5e8f3', 'ёмкость не задана')}
            ${item('#f6f1f5', 'салон закрыт')}
            <span class="sload-legend__item">Цвет продублирован числом — по проценту, а не по оттенку.</span>
        </div>`;
    }

    function extraRows(data) {
        const items = [];

        (data.stores || []).forEach(store => {
            if (store.no_time && store.no_time.orders) {
                items.push(`<button class="sload-extra__item" data-notime data-store="${store.store_id}">
                    <span>
                        <span class="sload-extra__label">Без времени готовности · ${esc(store.store_name)}</span><br>
                        <span class="sload-extra__value">${store.no_time.orders}</span>
                        <span class="sload-extra__label">${plural(store.no_time.orders, 'заказ', 'заказа', 'заказов')}${
                            store.no_time.unparsed ? `, из них ${store.no_time.unparsed} с непонятным временем` : ''}</span>
                    </span>
                    ${icon('<path d="M9 18l6-6-6-6"/>', 18)}
                </button>`);
            }
        });

        if (data.unassigned) {
            items.push(`<div class="sload-extra__item" disabled>
                <span>
                    <span class="sload-extra__label">Нераспределённые — склад не заполнен или не привязан</span><br>
                    <span class="sload-extra__value">${data.unassigned.orders}</span>
                    <span class="sload-extra__label">${plural(data.unassigned.orders, 'заказ', 'заказа', 'заказов')} ·
                        ${num(data.unassigned.units)} ед. Разбирать в CRM или в «Сопоставлении салонов».</span>
                </span>
            </div>`);
        }

        if (data.freshness && data.freshness.orders_without_date &&
                Number(data.freshness.orders_without_date) > 0) {
            items.push(`<div class="sload-extra__item" disabled>
                <span>
                    <span class="sload-extra__label">Без даты доставки — в сетку не попадают вовсе</span><br>
                    <span class="sload-extra__value">${esc(data.freshness.orders_without_date)}</span>
                    <span class="sload-extra__label">заказов на последнем синке</span>
                </span>
            </div>`);
        }

        if (!items.length) return '';
        return `<div class="sload-card">
            <h2 class="sload-card__title">Не попало в сетку</h2>
            <p class="sload-card__caption">Эти заказы не разложены по часам — их разбирает человек,
                автоматически им время не придумывается.</p>
            <div class="sload-extra">${items.join('')}</div>
        </div>`;
    }

    function renderWeek() {
        const data = state.week;
        if (!data) return '';

        if (data.no_stores) {
            return header() + toolbar() + `<div class="sload-card"><div class="sload-empty">
                <h3>Салоны не привязаны</h3><p>Обратитесь к администратору.</p></div></div>`;
        }

        const rows = (data.stores || []).map(store => `
            <tr>
                <th class="sload-grid__salon">
                    <span class="sload-grid__salon-name">${esc(store.store_name)}</span>
                    <span class="sload-grid__salon-meta">${esc(store.city || '')}</span>
                </th>
                ${store.days.map(day => `
                    <td><div class="sload-week__cell sload-cell--${esc(day.level)}"
                        title="${esc(day.orders + ' ' + plural(day.orders, 'заказ', 'заказа', 'заказов'))}">
                        <div class="sload-week__pct">${day.percent === null ? num(day.units) : pct(day.percent)}</div>
                        <div class="sload-week__sub">${day.orders} зак.</div>
                    </div></td>`).join('')}
            </tr>`).join('');

        return header() + toolbar() + freshnessNote(data.freshness) + `
            <div class="sload-card">
                <h2 class="sload-card__title">Неделя с ${esc(dateLabel(data.from))}</h2>
                <p class="sload-card__caption">Дневная загрузка. Предзаказов вне праздников мало,
                    поэтому будущие дни обычно пустые — это не ошибка.</p>
                <div class="sload-grid-wrap">
                    <table class="sload-week">
                        <thead><tr>
                            <th class="sload-grid__corner"></th>
                            ${data.dates.map(d => `<th class="sload-week__day">${esc(dateLabel(d).split(',')[0])}</th>`).join('')}
                        </tr></thead>
                        <tbody>${rows}</tbody>
                    </table>
                </div>
            </div>`;
    }

    // ================= Модалки =================

    function modal(title, bodyHtml, footerHtml) {
        const overlay = document.createElement('div');
        overlay.className = 'modal-overlay';
        overlay.style.cssText = 'position:fixed;inset:0;background:rgba(19,8,16,.45);z-index:1000;' +
            'display:flex;align-items:center;justify-content:center;padding:16px';
        overlay.innerHTML = `
            <div style="position:relative;background:#fff;border-radius:16px;width:100%;
                    max-width:860px;max-height:88vh;display:flex;flex-direction:column;
                    overflow:hidden;font-size:14px">
                <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;
                        padding:18px 20px;border-bottom:1px solid #eee2ea">
                    <h3 style="margin:0;font-family:'Vollkorn',Georgia,serif;color:#411330;font-size:18px">${esc(title)}</h3>
                    <button type="button" data-close style="background:none;border:none;cursor:pointer;
                        color:#9b8f97;padding:4px;line-height:0">${icon('<path d="M18 6L6 18M6 6l12 12"/>', 20)}</button>
                </div>
                <div class="sload-modal-body" style="padding:18px 20px;overflow-y:auto">${bodyHtml}</div>
                <div style="padding:14px 20px;border-top:1px solid #eee2ea;display:flex;gap:8px;
                        justify-content:flex-end">${footerHtml || ''}</div>
            </div>`;

        overlay.addEventListener('click', e => {
            if (e.target === overlay || e.target.closest('[data-close]')) overlay.remove();
        });
        document.body.appendChild(overlay);
        return overlay;
    }

    async function openSlot(storeId, hour) {
        let result;
        try {
            const hourParam = hour === null ? '' : `&hour=${hour}`;
            result = await api(`/api/salon-load/slot?date=${encodeURIComponent(state.date)}` +
                `&store_id=${storeId}${hourParam}`);
        } catch (error) {
            toast('Не удалось загрузить заказы слота: ' + error.message, 'error');
            return;
        }

        const orders = result.data.orders || [];
        const store = (state.data.stores || []).find(s => s.store_id === storeId);
        const title = hour === null
            ? `Заказы без времени готовности · ${store ? store.store_name : ''}`
            : `${String(hour).padStart(2, '0')}:00 · ${store ? store.store_name : ''}`;

        const body = orders.length ? `
            <p class="sload-card__caption">Всего ${num(result.data.units)} ед. трудоёмкости
                в ${orders.length} ${plural(orders.length, 'заказе', 'заказах', 'заказах')}.</p>
            <table class="sload-modal-table">
                <thead><tr><th>Заказ</th><th>Готовность</th><th>Тип</th><th>Трудоёмкость</th><th>Сумма</th></tr></thead>
                <tbody>${orders.map(order => `
                    <tr>
                        <td>№${esc(order.number || order.order_id)}</td>
                        <td>${order.ready_time ? esc(order.ready_time) :
                            '<span class="sload-badge sload-badge--warn">время не разобрано</span>'}</td>
                        <td>${order.is_pickup
                            ? '<span class="sload-badge sload-badge--pickup">самовывоз</span>'
                            : '<span class="sload-badge">доставка</span>'}</td>
                        <td>${num(order.units)}</td>
                        <td>${order.amount ? Math.round(order.amount) + NBSP + '₽' : '—'}</td>
                    </tr>`).join('')}</tbody>
            </table>`
            : '<div class="sload-empty"><p>В этом слоте заказов нет.</p></div>';

        modal(title, body,
            '<button class="sload-btn sload-btn--ghost" data-close>Закрыть</button>');
    }

    async function openCapacity() {
        let stores;
        try {
            stores = await api('/api/salon-load/stores');
        } catch (error) {
            toast('Не удалось загрузить салоны: ' + error.message, 'error');
            return;
        }

        const list = stores.stores || [];
        if (!list.length) {
            modal('Ёмкость салонов', '<div class="sload-empty"><p>Салоны не привязаны к складам CRM.</p></div>',
                '<button class="sload-btn sload-btn--ghost" data-close>Закрыть</button>');
            return;
        }

        const options = list.map(s =>
            `<option value="${s.id}">${esc(s.name)}${s.has_capacity ? '' : ' — не задана'}</option>`).join('');

        const body = `
            <p class="sload-card__caption">Ёмкость — сколько единиц трудоёмкости салон успевает за час.
                Одна единица — это примерно один обычный заказ. Сетка 7×24 на девять салонов — это
                1512 полей, поэтому основной способ ввода здесь: часы работы и норма в час,
                остальное закрывается автоматически.</p>

            <div style="display:grid;gap:10px;grid-template-columns:1fr;margin-bottom:16px">
                <label class="sload-extra__label">Салон
                    <select class="sload-select" id="sloadCapStore" style="width:100%;margin-top:4px">${options}</select>
                </label>
                <label class="sload-extra__label" style="display:flex;align-items:center;gap:8px">
                    <input type="checkbox" id="sloadAllDay"> Круглосуточно
                </label>
                <div style="display:grid;gap:8px;grid-template-columns:repeat(auto-fit,minmax(130px,1fr))">
                    <label class="sload-extra__label">Открытие
                        <input type="number" min="0" max="23" value="9" class="sload-input"
                            id="sloadOpen" style="width:100%;margin-top:4px"></label>
                    <label class="sload-extra__label">Закрытие
                        <input type="number" min="1" max="24" value="21" class="sload-input"
                            id="sloadClose" style="width:100%;margin-top:4px"></label>
                    <label class="sload-extra__label">Единиц в час
                        <input type="number" min="0.5" step="0.5" value="6" class="sload-input"
                            id="sloadUnits" style="width:100%;margin-top:4px"></label>
                    <label class="sload-extra__label">Выдача в час (необязательно)
                        <input type="number" min="0" step="0.5" class="sload-input"
                            id="sloadPickup" style="width:100%;margin-top:4px"></label>
                </div>
                <p class="sload-card__caption" style="margin:0">Ночная смена задаётся часами через
                    полночь: открытие 22, закрытие 6.</p>
                <div style="display:flex;gap:8px;flex-wrap:wrap">
                    <button class="sload-btn" data-apply-hours>Заполнить неделю</button>
                    <button class="sload-btn sload-btn--ghost" data-suggest>Сколько собирали на самом деле</button>
                </div>
                <div id="sloadSuggest"></div>
            </div>

            <h4 style="font-family:'Vollkorn',Georgia,serif;color:#411330;margin:0 0 4px">Копировать график</h4>
            <p class="sload-card__caption">Из салона с похожим режимом — быстрее, чем заполнять заново.</p>
            <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:16px">
                <select class="sload-select" id="sloadCopyFrom">${options}</select>
                ${icon('<path d="M5 12h14M13 6l6 6-6 6"/>', 18)}
                <select class="sload-select" id="sloadCopyTo">${options}</select>
                <button class="sload-btn sload-btn--ghost" data-copy-week>Скопировать</button>
            </div>

            <h4 style="font-family:'Vollkorn',Georgia,serif;color:#411330;margin:0 0 4px">Исключение на дату</h4>
            <p class="sload-card__caption">Праздник, отпуск, поломка. Пиковые даты (14 февраля, 8 марта)
                ставятся руками — в обычной статистике их нет.</p>
            <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
                <input type="date" class="sload-input" id="sloadExcDate" value="${esc(state.date)}">
                <input type="number" min="0" step="0.5" class="sload-input" id="sloadExcUnits"
                    placeholder="ед./час" style="width:110px">
                <input type="text" class="sload-input" id="sloadExcReason" placeholder="Причина" style="flex:1;min-width:160px">
                <label style="display:flex;align-items:center;gap:6px;font-size:13px">
                    <input type="checkbox" id="sloadExcClosed"> закрыт весь день</label>
                <button class="sload-btn sload-btn--ghost" data-set-exception>Сохранить</button>
            </div>`;

        const overlay = modal('Ёмкость салонов', body,
            '<button class="sload-btn sload-btn--ghost" data-close>Закрыть</button>');

        overlay.addEventListener('click', async e => {
            const suggest = e.target.closest('[data-suggest]');
            if (suggest) {
                const storeId = Number(overlay.querySelector('#sloadCapStore').value);
                const box = overlay.querySelector('#sloadSuggest');
                suggest.disabled = true;
                try {
                    const result = await api(`/api/salon-load/capacity/suggest?store_id=${storeId}`);
                    const data = result.data;
                    box.innerHTML = data.samples
                        ? note('info', `За месяц реально собирали ${num(data.median)} ед. в час (медиана)`,
                            `Загруженные часы доходили до ${num(data.p80)} ед. (80% случаев) и ` +
                            `${num(data.max)} ед. в пике. Сейчас стоит ${data.current === null
                                ? 'ничего' : num(data.current) + ' ед.'}. ` +
                            'Число только предложено: заниженная норма даёт постоянный ложный перегруз, ' +
                            'и на сетку перестают смотреть. Решает человек.')
                        : note('info', 'Данных пока нет',
                            'За месяц в этом салоне не набралось часов с заказами — норму задайте руками, ' +
                            'а через месяц работы вернитесь к этой кнопке.');
                } catch (error) {
                    toast('Не удалось посчитать: ' + error.message, 'error');
                }
                suggest.disabled = false;
                return;
            }

            // Круглосуточно — это те же 0 и 24, но догадаться об этом по двум
            // полям невозможно, а точки с таким режимом в сети есть.
            const allDay = e.target.closest('#sloadAllDay');
            if (allDay) {
                overlay.querySelector('#sloadOpen').disabled = allDay.checked;
                overlay.querySelector('#sloadClose').disabled = allDay.checked;
                return;
            }

            const applyHours = e.target.closest('[data-apply-hours]');
            const copyWeek = e.target.closest('[data-copy-week]');
            const setException = e.target.closest('[data-set-exception]');
            const button = applyHours || copyWeek || setException;
            if (!button) return;

            button.disabled = true;
            try {
                if (applyHours) {
                    const pickupRaw = overlay.querySelector('#sloadPickup').value;
                    const roundClock = overlay.querySelector('#sloadAllDay').checked;
                    await api('/api/salon-load/capacity/working-hours', postOptions({
                        store_id: Number(overlay.querySelector('#sloadCapStore').value),
                        open_hour: roundClock ? 0 : Number(overlay.querySelector('#sloadOpen').value),
                        close_hour: roundClock ? 24 : Number(overlay.querySelector('#sloadClose').value),
                        capacity: Number(overlay.querySelector('#sloadUnits').value),
                        pickup_capacity: pickupRaw === '' ? null : Number(pickupRaw)
                    }));
                    toast('График сохранён', 'success');
                } else if (copyWeek) {
                    const from = Number(overlay.querySelector('#sloadCopyFrom').value);
                    const to = Number(overlay.querySelector('#sloadCopyTo').value);
                    if (from === to) {
                        toast('Выберите разные салоны', 'error');
                        button.disabled = false;
                        return;
                    }
                    await api('/api/salon-load/capacity/copy', postOptions({
                        source_store_id: from, target_store_id: to
                    }));
                    toast('График скопирован', 'success');
                } else {
                    const unitsRaw = overlay.querySelector('#sloadExcUnits').value;
                    const closed = overlay.querySelector('#sloadExcClosed').checked;
                    if (unitsRaw === '' && !closed) {
                        toast('Укажите ёмкость или отметьте «закрыт»', 'error');
                        button.disabled = false;
                        return;
                    }
                    await api('/api/salon-load/exceptions', postOptions({
                        store_id: Number(overlay.querySelector('#sloadCapStore').value),
                        date: overlay.querySelector('#sloadExcDate').value,
                        hour: null,
                        capacity: unitsRaw === '' ? null : Number(unitsRaw),
                        closed: closed,
                        reason: overlay.querySelector('#sloadExcReason').value
                    }));
                    toast('Исключение сохранено', 'success');
                }
                state.data = null;
                state.week = null;
                load();
            } catch (error) {
                toast('Не удалось сохранить: ' + error.message, 'error');
            }
            button.disabled = false;
        });
    }

    async function openWeights(onlyMissing) {
        let result;
        try {
            // Справочник весов живёт в модуле заказов: веса лежат в одной базе
            // с позициями, и пересчёт нагрузки там — один SQL.
            result = await api(`/api/couriers/weights?only_missing=${onlyMissing ? 1 : 0}`);
        } catch (error) {
            toast('Не удалось загрузить справочник: ' + error.message, 'error');
            return;
        }

        const items = result.data || [];
        const coverage = (result.meta || {}).coverage || {};

        const body = `
            <p class="sload-card__caption">Любой заказ — это ${num(coverage.order_base || 1)} ед. нагрузки:
                базовая сборка. Надбавка нужна только тем товарам, которые заметно тяжелее обычного,
                — остальные её не получают. Количество в CRM меряется по-разному (букет в штуках,
                клубника в граммах, роза в стеблях), поэтому у надбавки указывается, за что она.</p>

            <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:12px">
                <div class="sload-tabs">
                    <button class="sload-tab ${onlyMissing ? '' : 'sload-tab--active'}" data-weights-tab="all">Все товары</button>
                    <button class="sload-tab ${onlyMissing ? 'sload-tab--active' : ''}" data-weights-tab="missing">Без надбавки</button>
                </div>
                <input type="search" class="sload-input" id="sloadWeightSearch" placeholder="Поиск по названию или артикулу"
                    style="flex:1;min-width:180px">
            </div>

            ${coverage.orders ? note('info',
                `За 60 дней: ${num(coverage.total_units)} ед. нагрузки`,
                `${coverage.orders} ${plural(coverage.orders, 'заказ', 'заказа', 'заказов')} ` +
                `= ${num(coverage.base_units)} ед. базы + ${num(coverage.extra_units)} ед. надбавок ` +
                `(${num(coverage.extra_share)}%). Надбавка задана у ${coverage.products_weighted} ` +
                `${plural(coverage.products_weighted, 'товара', 'товаров', 'товаров')} из ${coverage.products_total}.`) : ''}

            <div id="sloadWeightRows">${weightRows(items)}</div>`;

        const overlay = modal('Надбавки за трудоёмкость', body,
            '<button class="sload-btn" data-save-weights>Сохранить надбавки</button>' +
            '<button class="sload-btn sload-btn--ghost" data-close>Закрыть</button>');

        overlay.addEventListener('click', async e => {
            const tab = e.target.closest('[data-weights-tab]');
            if (tab) {
                overlay.remove();
                openWeights(tab.dataset.weightsTab === 'missing');
                return;
            }

            const save = e.target.closest('[data-save-weights]');
            if (!save) return;

            // Изменением считается и правка базы начисления: «0.2 за 100 г» и
            // «0.2 за штуку» у клубники различаются в пятьсот раз.
            const weights = {};
            overlay.querySelectorAll('[data-weight-input]').forEach(input => {
                const basisSelect = overlay.querySelector(
                    `[data-basis-input][data-offer="${input.dataset.offer}"]`);
                const basis = basisSelect ? basisSelect.value : 'unit';
                const value = input.value.trim();
                if (value === input.dataset.initial && basis === input.dataset.initialBasis) return;
                weights[input.dataset.offer] = value === ''
                    ? null : { weight: Number(value), basis: basis };
            });

            if (!Object.keys(weights).length) {
                toast('Ничего не изменилось', 'info');
                return;
            }

            save.disabled = true;
            try {
                await api('/api/couriers/weights', postOptions({ weights: weights }));
                toast(`Сохранено: ${Object.keys(weights).length}`, 'success');
                state.data = null;
                state.week = null;
                load();
                overlay.remove();
            } catch (error) {
                toast('Не удалось сохранить: ' + error.message, 'error');
                save.disabled = false;
            }
        });

        const search = overlay.querySelector('#sloadWeightSearch');
        let searchTimer = null;
        search.addEventListener('input', () => {
            clearTimeout(searchTimer);
            searchTimer = setTimeout(async () => {
                try {
                    const found = await api(`/api/couriers/weights?only_missing=${onlyMissing ? 1 : 0}` +
                        `&q=${encodeURIComponent(search.value.trim())}`);
                    overlay.querySelector('#sloadWeightRows').innerHTML = weightRows(found.data || []);
                } catch (error) {
                    toast('Поиск не удался: ' + error.message, 'error');
                }
            }, 350);
        });
    }

    // ------------------------------------------------------------------
    // Нормы времени сборки (Ф2 плана «нагрузка в минутах»)
    // ------------------------------------------------------------------

    // Роль позиции в расчёте. Подписи — не жаргон: человек размечает по ним
    // сотни товаров, и «catalog» ему ни о чём не говорит.
    const NORM_ROLES = [
        ['', 'не задана'],
        ['catalog', 'готовый товар'],
        ['flower', 'цветок (компонент)'],
        ['berry', 'клубника на вес'],
        ['packaging', 'упаковка'],
        ['none', 'не создаёт нагрузки']
    ];
    const NORM_BASES = [['unit', 'за штуку'], ['line', 'за позицию']];
    const BERRY_MODES = [['', '—'], ['bouquet', 'букет'], ['box', 'коробочка']];

    function roleLabel(role) {
        const found = NORM_ROLES.find(([code]) => code === (role || ''));
        // Значение из базы экранируется даже здесь: роль пишется только после
        // проверки по белому списку, но подпись уходит прямо в разметку, и
        // полагаться на дальнюю проверку в другом файле не стоит.
        return found ? found[1] : esc(role);
    }

    function normSource(norm) {
        if (!norm) return '<span class="sload-badge sload-badge--warn">нет нормы</span>';
        return roleLabel(norm.role);
    }

    // Фильтры по всем полям списка. Нормы задаются по каждому товару отдельно
    // (групповые отменены 2026-09-09: в группе CRM лежат товары с сильно
    // разным временем сборки), поэтому список длинный — без фильтров до нужной
    // строки не дойти.
    function normFiltersBar() {
        const option = (value, label, current) =>
            `<option value="${value}"${current === value ? ' selected' : ''}>${label}</option>`;
        return `
            <div style="display:grid;gap:8px;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
                    margin-bottom:12px">
                <input type="search" class="sload-input" data-filter="q" placeholder="Название или артикул"
                    value="${esc(normFilters.q)}">
                <select class="sload-select" data-filter="role">
                    ${option('', 'Роль: любая', normFilters.role)}
                    ${NORM_ROLES.filter(([code]) => code)
                        .map(([code, label]) => option(code, 'Роль: ' + label, normFilters.role)).join('')}
                </select>
                <select class="sload-select" data-filter="unit">
                    ${option('', 'Единица: любая', normFilters.unit)}
                    ${option('pc', 'Единица: штуки', normFilters.unit)}
                    ${option('g', 'Единица: граммы', normFilters.unit)}
                </select>
                <select class="sload-select" data-filter="in_catalog">
                    ${option('', 'Каталог: все', normFilters.in_catalog)}
                    ${option('1', 'Каталог: есть в CRM', normFilters.in_catalog)}
                    ${option('0', 'Каталог: нет в CRM', normFilters.in_catalog)}
                </select>
                <input type="number" min="0" class="sload-input" data-filter="min_orders"
                    placeholder="Заказов от" value="${esc(normFilters.min_orders)}">
                <input type="number" min="0" class="sload-input" data-filter="max_orders"
                    placeholder="Заказов до" value="${esc(normFilters.max_orders)}">
                <input type="number" min="0" step="0.1" class="sload-input" data-filter="min_median"
                    placeholder="Кол-во от" value="${esc(normFilters.min_median)}">
                <input type="number" min="0" step="0.1" class="sload-input" data-filter="max_median"
                    placeholder="Кол-во до" value="${esc(normFilters.max_median)}">
            </div>`;
    }

    // Фильтры списка товаров. Живут в состоянии модуля, а не в замыкании
    // модалки: экран перерисовывается на каждое действие, и фильтр,
    // сбрасывающийся после сохранения одной строки, бесполезен.
    const normFilters = { q: '', role: '', unit: '', in_catalog: '',
                          min_orders: '', max_orders: '', min_median: '', max_median: '' };

    function normQuery(extra) {
        const params = new URLSearchParams();
        Object.keys(normFilters).forEach(key => {
            if (normFilters[key] !== '') params.set(key, normFilters[key]);
        });
        Object.keys(extra || {}).forEach(key => params.set(key, extra[key]));
        const query = params.toString();
        return query ? '?' + query : '';
    }

    async function openTimeNorms(tab) {
        const active = tab || 'offers';
        let offers, tariffs;
        try {
            if (active === 'tariffs') {
                tariffs = await api('/api/couriers/time-norms/tariffs');
            } else {
                offers = await api('/api/couriers/time-norms/offers' +
                    normQuery(active === 'missing' ? { only_missing: 1 } : null));
            }
        } catch (error) {
            toast('Не удалось загрузить нормы: ' + error.message, 'error');
            return;
        }

        const meta = offers ? (offers.meta || {}) : {};
        const coverage = meta.coverage || null;
        const catalog = meta.catalog || null;

        const body = `
            <p class="sload-card__caption">Норма — сколько минут занимает сборка. Задаётся каждому
                товару отдельно: в одной группе номенклатуры лежат товары с сильно разным временем,
                и общая норма давала бы правдоподобное, но неверное число. Компонентам («цветок»,
                «клубника на вес») минуты не нужны — их время считается по тарифам от количества.
                Для массовой работы — выгрузка и загрузка файла.</p>

            <div class="sload-tabs" style="margin-bottom:12px">
                <button class="sload-tab ${active === 'offers' ? 'sload-tab--active' : ''}"
                    data-norm-tab="offers">Товары</button>
                <button class="sload-tab ${active === 'missing' ? 'sload-tab--active' : ''}"
                    data-norm-tab="missing">Без нормы</button>
                <button class="sload-tab ${active === 'tariffs' ? 'sload-tab--active' : ''}"
                    data-norm-tab="tariffs">Тарифы</button>
            </div>

            ${catalog && !catalog.offers ? note('bad', 'Каталог номенклатуры пуст',
                'Единицы измерения ещё не загружены из CRM. Штатно каталог обновляется ночью; ' +
                'кнопка ниже загрузит его сейчас.') : ''}

            ${active !== 'tariffs' ? `
                <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:12px">
                    <button class="sload-btn sload-btn--ghost" data-export-norms>Выгрузить в файл</button>
                    ${state.isAdmin ? `
                        <button class="sload-btn sload-btn--ghost" data-import-norms>Загрузить из файла</button>
                        <input type="file" accept=".csv,text/csv" id="sloadNormFile" style="display:none">
                        <button class="sload-btn sload-btn--ghost" data-sync-catalog>Обновить каталог</button>` : ''}
                    <span class="sload-extra__label">${catalog && catalog.offers
                        ? `${catalog.offers} ${plural(catalog.offers, 'товар', 'товара', 'товаров')} в каталоге` +
                          (catalog.synced_at ? ` · обновлён ${esc(catalog.synced_at)}` : '')
                        : 'каталог не загружен'}</span>
                </div>
                ${normFiltersBar()}` : ''}

            ${coverage && coverage.orders_incomplete ? note('warn',
                `${coverage.orders_incomplete} из ${coverage.orders} ` +
                `${plural(coverage.orders, 'заказа', 'заказов', 'заказов')} посчитаны не полностью`,
                `Это ${num(coverage.share)}% заказов за 60 дней: в них есть позиции без нормы, ` +
                `и загрузка по ним занижена. Товаров без нормы: ${coverage.offers_without_norm} ` +
                `из ${coverage.offers_total}.`) : ''}

            <div id="sloadNormRows">${
                active === 'tariffs' ? tariffRows(tariffs.data || {})
                : offerNormRows(offers.data || [])}</div>`;

        const overlay = modal('Нормы времени сборки', body,
            '<button class="sload-btn sload-btn--ghost" data-close>Закрыть</button>');

        overlay.addEventListener('click', async e => {
            const tabButton = e.target.closest('[data-norm-tab]');
            if (tabButton) {
                overlay.remove();
                openTimeNorms(tabButton.dataset.normTab);
                return;
            }

            const syncCatalog = e.target.closest('[data-sync-catalog]');
            if (syncCatalog) {
                syncCatalog.disabled = true;
                syncCatalog.textContent = 'Загружаю…';
                try {
                    const res = await api('/api/couriers/catalog/sync', postOptions({}));
                    toast(`Каталог обновлён: ${res.data.offers} товаров`, 'success');
                    overlay.remove();
                    openTimeNorms(active);
                } catch (error) {
                    toast('Не удалось обновить каталог: ' + error.message, 'error');
                    syncCatalog.disabled = false;
                    syncCatalog.textContent = 'Обновить каталог';
                }
                return;
            }

            // Выгрузка идёт обычной ссылкой, а не fetch: файл должен сохраниться
            // браузером, а не осесть в памяти вкладки.
            if (e.target.closest('[data-export-norms]')) {
                window.location.href = '/api/couriers/time-norms/export' +
                    normQuery(active === 'missing' ? { only_missing: 1 } : null);
                return;
            }

            if (e.target.closest('[data-import-norms]')) {
                overlay.querySelector('#sloadNormFile').click();
                return;
            }

            const saveTariff = e.target.closest('[data-save-tariff]');
            if (saveTariff) {
                const row = saveTariff.closest('[data-tariff-row]');
                const value = name => {
                    const field = row.querySelector(`[data-tariff="${name}"]`);
                    return field && field.value !== '' ? Number(field.value) : null;
                };
                const payload = row.dataset.kind === 'berries'
                    ? { kind: 'berries', mode: row.dataset.mode,
                        minutes_per_100g: value('per100'), package_minutes: value('package') }
                    : { kind: 'flowers',
                        range_from: Number(row.dataset.from), range_to: Number(row.dataset.to),
                        mono_minutes: value('mono'), mix_minutes: value('mix'),
                        ribbon_minutes: value('ribbon'), package_minutes: value('package') };

                saveTariff.disabled = true;
                try {
                    await api('/api/couriers/time-norms/tariffs', postOptions(payload));
                    toast('Тариф сохранён, нагрузка пересчитана', 'success');
                    overlay.remove();
                    openTimeNorms('tariffs');
                } catch (error) {
                    toast('Не удалось сохранить: ' + error.message, 'error');
                    saveTariff.disabled = false;
                }
                return;
            }

            const save = e.target.closest('[data-save-norm]');
            if (!save) return;

            const row = save.closest('[data-norm-row]');
            const role = row.querySelector('[data-norm-role]').value;
            const minutesRaw = row.querySelector('[data-norm-minutes]');
            const basisSelect = row.querySelector('[data-norm-basis]');
            const berrySelect = row.querySelector('[data-norm-berry]');

            const payload = {
                scope: row.dataset.scope,
                scope_id: Number(row.dataset.scopeId),
                role: role || null,
                minutes: minutesRaw && minutesRaw.value !== '' ? Number(minutesRaw.value) : null,
                basis: basisSelect ? basisSelect.value : null,
                berry_mode: berrySelect && berrySelect.value ? berrySelect.value : null
            };

            save.disabled = true;
            try {
                await api('/api/couriers/time-norms', postOptions(payload));
                toast('Норма сохранена', 'success');
                overlay.remove();
                openTimeNorms(active);
            } catch (error) {
                toast('Не удалось сохранить: ' + error.message, 'error');
                save.disabled = false;
            }
        });

        // Фильтры: текстовые с задержкой, списки — сразу. Перерисовываем весь
        // экран, поэтому фокус возвращается на то поле, где его оставили, —
        // иначе набор в поиске обрывается на первом символе.
        let filterTimer = null;
        const applyFilters = (focusName, delay) => {
            clearTimeout(filterTimer);
            filterTimer = setTimeout(async () => {
                overlay.remove();
                await openTimeNorms(active);
                const field = document.querySelector(`[data-filter="${focusName}"]`);
                if (field) {
                    field.focus();
                    if (field.setSelectionRange && field.type !== 'number') {
                        field.setSelectionRange(field.value.length, field.value.length);
                    }
                }
            }, delay);
        };

        overlay.querySelectorAll('[data-filter]').forEach(field => {
            const name = field.dataset.filter;
            const isSelect = field.tagName === 'SELECT';
            field.addEventListener(isSelect ? 'change' : 'input', () => {
                normFilters[name] = field.value.trim();
                applyFilters(name, isSelect ? 0 : 400);
            });
        });

        const fileInput = overlay.querySelector('#sloadNormFile');
        if (fileInput) {
            fileInput.addEventListener('change', async () => {
                const file = fileInput.files && fileInput.files[0];
                if (!file) return;
                try {
                    const rows = parseNormsCsv(await file.text());
                    if (!rows.length) {
                        toast('В файле нет строк с товарами', 'error');
                        return;
                    }
                    const res = await api('/api/couriers/time-norms/import',
                        postOptions({ rows: rows }));
                    const errors = res.data.errors || [];
                    toast(`Загружено: ${res.data.applied}, снято: ${res.data.cleared}` +
                        (errors.length ? `, с ошибками: ${errors.length}` : ''),
                        errors.length ? 'error' : 'success');
                    if (errors.length) {
                        BarhatUI.alert('Строки, которые не удалось применить',
                            errors.slice(0, 20).join('\n') +
                            (errors.length > 20 ? `\n…и ещё ${errors.length - 20}` : ''));
                    }
                    state.data = null;
                    state.week = null;
                    overlay.remove();
                    openTimeNorms(active);
                } catch (error) {
                    toast('Не удалось загрузить файл: ' + error.message, 'error');
                }
            });
        }
    }

    // Разбор выгруженного файла. Читаем по ЗАГОЛОВКАМ, а не по номерам
    // колонок: в Excel столбцы переставляют и вставляют свои, и разбор по
    // номеру начал бы писать время в поле роли.
    function parseNormsCsv(text) {
        const lines = text.replace(/^﻿/, '').split(/\r?\n/).filter(line => line.trim());
        if (!lines.length) return [];

        const delimiter = lines[0].includes(';') ? ';' : ',';
        const split = line => line.split(delimiter).map(cell => cell.trim().replace(/^"|"$/g, ''));
        const headers = split(lines[0]);
        const index = name => headers.indexOf(name);
        const columns = {
            offer_id: index('offer_id'), role: index('Роль'), minutes: index('Минут'),
            basis: index('За что'), berry_mode: index('Клубника')
        };
        if (columns.offer_id < 0) {
            throw new Error('в файле нет колонки offer_id — по ней находится товар');
        }

        const rows = [];
        for (const line of lines.slice(1)) {
            const cells = split(line);
            const id = Number(cells[columns.offer_id]);
            if (!id) continue;
            const cell = name => columns[name] >= 0 ? (cells[columns[name]] || '') : '';
            rows.push({
                offer_id: id, role: cell('role'), minutes: cell('minutes'),
                basis: cell('basis'), berry_mode: cell('berry_mode')
            });
        }
        return rows;
    }

    // Каждый контрол — своя колонка таблицы: иначе строки разъезжаются и
    // сравнить нормы соседних товаров глазами невозможно.
    function normCells(own) {
        const minutes = own && own.minutes !== null && own.minutes !== undefined
            ? esc(own.minutes) : '';
        return `
            <td><select class="sload-select" data-norm-role>
                ${NORM_ROLES.map(([code, label]) =>
                    `<option value="${code}"${(own && own.role) === code ? ' selected' : ''}>${label}</option>`).join('')}
            </select></td>
            <td><input type="number" min="0" step="0.5" class="sload-weight-input"
                data-norm-minutes value="${minutes}" placeholder="мин"></td>
            <td><select class="sload-select" data-norm-basis>
                ${NORM_BASES.map(([code, label]) =>
                    `<option value="${code}"${(own && own.basis) === code ? ' selected' : ''}>${label}</option>`).join('')}
            </select></td>
            <td><select class="sload-select" data-norm-berry>
                ${BERRY_MODES.map(([code, label]) =>
                    `<option value="${code}"${(own && own.berry_mode || '') === code ? ' selected' : ''}>${label}</option>`).join('')}
            </select></td>
            <td><button class="sload-btn sload-btn--ghost" data-save-norm>Сохранить</button></td>`;
    }

    function tariffField(name, value) {
        return `<td><input type="number" min="0" step="0.1" class="sload-weight-input"
            data-tariff="${name}" value="${value === null || value === undefined ? '' : esc(value)}"></td>`;
    }

    function tariffRows(data) {
        const flowers = data.flowers || [];
        const berries = data.berries || {};
        const berryRow = (mode, label) => {
            const row = berries[mode] || {};
            return `<tr data-tariff-row data-kind="berries" data-mode="${mode}">
                <td>${label}</td>
                ${tariffField('per100', row.minutes_per_100g)}
                ${tariffField('package', row.package_minutes)}
                <td><button class="sload-btn sload-btn--ghost" data-save-tariff>Сохранить</button></td>
            </tr>`;
        };

        return `
            <p class="sload-card__caption">Время сборки от количества. Диапазоны должны идти
                подряд, без дыр и пересечений — иначе заказ на «выпавшее» количество посчитался бы
                нулём молча, поэтому такая правка отклоняется. Любое изменение сразу
                пересчитывает нагрузку за 60 дней.</p>

            <h4 style="font-family:'Vollkorn',Georgia,serif;color:#411330;margin:12px 0 6px">Цветы</h4>
            <table class="sload-modal-table">
                <thead><tr><th>Количество</th><th>Монобукет,<br>мин/шт</th><th>Из разного<br>цветка</th>
                    <th>Только<br>лента</th><th>Упаковка</th><th></th></tr></thead>
                <tbody>${flowers.map(row => `
                    <tr data-tariff-row data-kind="flowers"
                        data-from="${row.range_from}" data-to="${row.range_to}">
                        <td>${row.range_from}–${row.range_to}</td>
                        ${tariffField('mono', row.mono_minutes)}
                        ${tariffField('mix', row.mix_minutes)}
                        ${tariffField('ribbon', row.ribbon_minutes)}
                        ${tariffField('package', row.package_minutes)}
                        <td><button class="sload-btn sload-btn--ghost" data-save-tariff>Сохранить</button></td>
                    </tr>`).join('')}</tbody>
            </table>
            <p class="sload-card__caption">Количество больше последнего диапазона считается по
                последней строке: 500 цветов — это ошибка ввода, а не букет, и выдуманный тариф
                был бы хуже крайнего известного.</p>

            <h4 style="font-family:'Vollkorn',Georgia,serif;color:#411330;margin:16px 0 6px">Клубника</h4>
            <table class="sload-modal-table">
                <thead><tr><th></th><th>Минут<br>на 100 г</th><th>Упаковка<br>на весь букет</th><th></th></tr></thead>
                <tbody>${berryRow('bouquet', 'Букет')}${berryRow('box', 'Коробочка')}</tbody>
            </table>`;
    }

    function offerNormRows(offers) {
        if (!offers.length) {
            return '<div class="sload-empty"><p>Ничего не найдено — либо всё размечено, ' +
                   'либо фильтры слишком узкие.</p></div>';
        }
        return `<table class="sload-modal-table">
            <thead><tr><th>Артикул</th><th>Товар</th><th>Заказов</th><th>Кол-во<br>в позиции</th>
                <th>Ед.</th><th>Норма</th><th>Роль</th><th>Минут</th><th>За что</th>
                <th>Клубника</th><th></th></tr></thead>
            <tbody>${offers.map(item => `
                <tr data-norm-row data-scope="offer" data-scope-id="${item.offer_id}">
                    <td>${esc(item.article || '—')}</td>
                    <td>${esc(item.product_name || ('Товар ' + item.offer_id))}
                        ${item.in_catalog ? '' :
                            '<br><span class="sload-badge sload-badge--warn">нет в каталоге</span>'}</td>
                    <td>${item.orders}</td>
                    <td>${item.median_quantity === null || item.median_quantity === undefined
                        ? '—' : num(item.median_quantity)}</td>
                    <td>${esc(item.unit_code || '—')}</td>
                    <td>${normSource(item.norm)}</td>
                    ${normCells(item.norm)}
                </tr>`).join('')}</tbody>
        </table>`;
    }

    // Базы начисления надбавки. Порядок — от самой частой к редкой.
    const WEIGHT_BASES = [
        ['unit', 'за штуку'],
        ['line', 'за позицию'],
        ['g100', 'за 100 г']
    ];

    function weightRows(items) {
        if (!items.length) {
            return '<div class="sload-empty"><p>Товаров нет — либо надбавки расставлены, либо за 60 дней их не заказывали.</p></div>';
        }
        return `<table class="sload-modal-table">
            <thead><tr><th>Товар</th><th>Артикул</th><th>Заказов</th><th>В среднем за заказ</th>
                <th>Надбавка</th><th>За что</th></tr></thead>
            <tbody>${items.map(item => {
                const basis = item.basis || 'unit';
                const value = item.weight === null || item.weight === undefined ? '' : esc(item.weight);
                return `
                <tr>
                    <td>${esc(item.product_name || ('Товар ' + item.offer_id))}</td>
                    <td>${esc(item.article || '—')}</td>
                    <td>${item.orders}</td>
                    <td>${num(item.per_order)}</td>
                    <td><input type="number" min="0.01" step="0.1" class="sload-weight-input"
                        data-weight-input data-offer="${item.offer_id}"
                        data-initial="${value}" data-initial-basis="${esc(basis)}"
                        value="${value}" placeholder="нет"></td>
                    <td><select class="sload-select" data-basis-input data-offer="${item.offer_id}">
                        ${WEIGHT_BASES.map(([code, label]) =>
                            `<option value="${code}"${basis === code ? ' selected' : ''}>${label}</option>`).join('')}
                    </select></td>
                </tr>`;
            }).join('')}</tbody>
        </table>`;
    }

    async function openStatuses() {
        let result;
        try {
            result = await api('/api/couriers/order-statuses');
        } catch (error) {
            toast('Не удалось загрузить статусы: ' + error.message, 'error');
            return;
        }

        const statuses = result.data || [];
        const body = `
            <p class="sload-card__caption">Какие статусы заказа считаются работой салона. Значение
                проставлено по группе RetailCRM (отменённые — не нагрузка) и правится руками:
                синхронизация ручную правку не перетирает.</p>
            <table class="sload-modal-table">
                <thead><tr><th>Статус</th><th>Группа</th><th>Считать нагрузкой</th></tr></thead>
                <tbody>${statuses.map(status => `
                    <tr>
                        <td>${esc(status.name)}</td>
                        <td><span class="sload-badge">${esc(status.group || '—')}</span></td>
                        <td><label style="display:flex;align-items:center;gap:6px">
                            <input type="checkbox" data-status="${esc(status.code)}"
                                ${status.counts_as_load ? 'checked' : ''}>
                            ${status.reviewed ? '' : '<span class="sload-badge sload-badge--warn">по группе</span>'}
                        </label></td>
                    </tr>`).join('')}</tbody>
            </table>`;

        const overlay = modal('Статусы заказов', body,
            '<button class="sload-btn sload-btn--ghost" data-close>Закрыть</button>');

        overlay.addEventListener('change', async e => {
            const box = e.target.closest('[data-status]');
            if (!box) return;
            box.disabled = true;
            try {
                await api(`/api/couriers/order-statuses/${encodeURIComponent(box.dataset.status)}/flag`,
                    postOptions({ counts_as_load: box.checked }));
                toast('Сохранено', 'success');
                state.data = null;
                state.week = null;
                load();
            } catch (error) {
                toast('Не удалось сохранить: ' + error.message, 'error');
                box.checked = !box.checked;
            }
            box.disabled = false;
        });
    }

    // ================= События =================

    document.addEventListener('click', function (e) {
        const container = host();
        if (!container || !container.contains(e.target)) return;

        const tab = e.target.closest('[data-view]');
        if (tab) {
            state.view = tab.dataset.view;
            load();
            return;
        }

        const shift = e.target.closest('[data-shift]');
        if (shift) {
            state.date = shiftDate(state.date, Number(shift.dataset.shift));
            state.data = null;
            state.week = null;
            load();
            return;
        }

        if (e.target.closest('[data-today]')) {
            state.date = todayIso();
            state.data = null;
            state.week = null;
            load();
            return;
        }

        const open = e.target.closest('[data-open]');
        if (open) {
            if (open.dataset.open === 'capacity') openCapacity();
            if (open.dataset.open === 'norms') openTimeNorms('groups');
            if (open.dataset.open === 'weights') openWeights(true);
            if (open.dataset.open === 'statuses') openStatuses();
            return;
        }

        const cellButton = e.target.closest('[data-cell]');
        if (cellButton) {
            openSlot(Number(cellButton.dataset.store), Number(cellButton.dataset.hour));
            return;
        }

        const noTime = e.target.closest('[data-notime]');
        if (noTime) {
            openSlot(Number(noTime.dataset.store), null);
            return;
        }

        const freeSlot = e.target.closest('[data-free-slot]');
        if (freeSlot) {
            // Переход на свободный слот: показываем тот день и тот час, чтобы
            // человек видел, куда переносит, а не верил подписи на бейдже.
            state.date = freeSlot.dataset.date;
            state.data = null;
            state.week = null;
            load().then(() => openSlot(Number(freeSlot.dataset.store), Number(freeSlot.dataset.hour)));
            return;
        }

        const dismiss = e.target.closest('[data-dismiss]');
        if (dismiss) {
            dismiss.disabled = true;
            api(`/api/salon-load/alerts/${dismiss.dataset.dismiss}/dismiss`, postOptions({}))
                .then(() => {
                    toast('Предупреждение снято', 'success');
                    state.data = null;
                    load();
                })
                .catch(error => {
                    toast('Не удалось снять: ' + error.message, 'error');
                    dismiss.disabled = false;
                });
        }
    });

    document.addEventListener('change', function (e) {
        if (e.target.id === 'sloadDate') {
            state.date = e.target.value;
            state.data = null;
            state.week = null;
            load();
        }
    });

    window.SalonLoadModule = {
        onPageActivated: function (user) {
            state.isAdmin = user && user.role === 'admin';
            if (!state.data && !state.week) load();
        }
    };
})();
