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
                const result = await api(`/api/salon-load/day?date=${encodeURIComponent(state.date)}`);
                state.data = result.data;
                state.isAdmin = !!result.data.can_edit;
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
                    <button class="sload-btn sload-btn--onhead" data-open="weights">Веса товаров</button>
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

    function coverageNote(coverage) {
        if (!coverage || !coverage.products_missing) return '';
        return note('warn',
            `${coverage.products_missing} ${plural(coverage.products_missing, 'товар', 'товара', 'товаров')} без веса`,
            `${num(coverage.default_share)}% нагрузки посчитано весом по умолчанию ` +
            `(${num(coverage.default_weight)} ед.). Пока веса не проставлены, проценты приблизительные.`);
    }

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

        return header() + toolbar() +
            freshnessNote(data.freshness) + capacityNote(data) + coverageNote(data.coverage) + `
            <div class="sload-card">
                <div style="display:flex;align-items:baseline;justify-content:space-between;gap:12px;flex-wrap:wrap">
                    <div>
                        <h2 class="sload-card__title">${esc(dateLabel(data.date))}</h2>
                        <p class="sload-card__caption">Часы — местное время салона. В ячейке:
                            нагрузка из ёмкости и процент.</p>
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
            `${num(data.units)} ед. трудоёмкости`,
            data.capacity === null ? 'ёмкость не задана' : `ёмкость ${num(data.capacity)} ед.`,
            `${data.orders} ${plural(data.orders, 'заказ', 'заказа', 'заказов')}`,
            data.pickup_orders ? `самовывоз: ${data.pickup_orders}` : '',
            data.reason ? `причина: ${data.reason}` : ''
        ].filter(Boolean).join(' · ');

        return `<td><button class="sload-cell sload-cell--${esc(level)}" data-cell
            data-store="${store.store_id}" data-hour="${data.hour}" title="${esc(title)}">
            <span class="sload-cell__value">${data.percent === null
                ? num(data.units) : pct(data.percent)}${data.level === 'over'
                    ? ` <span class="sload-cell__icon">${icon(ICON_ALERT, 11)}</span>` : ''}</span>
            <span class="sload-cell__sub">${num(data.units)}${data.capacity === null
                ? ' ед.' : '/' + num(data.capacity)}</span>
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
                <thead><tr><th>Заказ</th><th>Готовность</th><th>Тип</th><th>Вес</th><th>Сумма</th></tr></thead>
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
                Сетка 7×24 на девять салонов — это 1512 полей, поэтому основной способ ввода здесь:
                часы работы и норма в час, остальное закрывается автоматически.</p>

            <div style="display:grid;gap:10px;grid-template-columns:1fr;margin-bottom:16px">
                <label class="sload-extra__label">Салон
                    <select class="sload-select" id="sloadCapStore" style="width:100%;margin-top:4px">${options}</select>
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
                <button class="sload-btn" data-apply-hours>Заполнить неделю</button>
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
            const applyHours = e.target.closest('[data-apply-hours]');
            const copyWeek = e.target.closest('[data-copy-week]');
            const setException = e.target.closest('[data-set-exception]');
            const button = applyHours || copyWeek || setException;
            if (!button) return;

            button.disabled = true;
            try {
                if (applyHours) {
                    const pickupRaw = overlay.querySelector('#sloadPickup').value;
                    await api('/api/salon-load/capacity/working-hours', postOptions({
                        store_id: Number(overlay.querySelector('#sloadCapStore').value),
                        open_hour: Number(overlay.querySelector('#sloadOpen').value),
                        close_hour: Number(overlay.querySelector('#sloadClose').value),
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
            <p class="sload-card__caption">Вес — сколько работы флориста стоит одна единица товара.
                Заказ считается суммой «вес × количество». Товар без веса считается по весу
                по умолчанию (${num(coverage.default_weight)} ед.) и виден здесь — нулём его
                считать нельзя, иначе нагрузка занижается незаметно.</p>

            <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:12px">
                <div class="sload-tabs">
                    <button class="sload-tab ${onlyMissing ? '' : 'sload-tab--active'}" data-weights-tab="all">Все товары</button>
                    <button class="sload-tab ${onlyMissing ? 'sload-tab--active' : ''}" data-weights-tab="missing">Требуют веса</button>
                </div>
                <input type="search" class="sload-input" id="sloadWeightSearch" placeholder="Поиск по названию или артикулу"
                    style="flex:1;min-width:180px">
                <label class="sload-extra__label" style="display:flex;align-items:center;gap:6px">
                    Вес по умолчанию
                    <input type="number" min="0.1" step="0.1" class="sload-weight-input"
                        id="sloadDefaultWeight" value="${esc(coverage.default_weight)}">
                    <button class="sload-btn sload-btn--ghost" data-save-default>Сохранить</button>
                </label>
            </div>

            ${coverage.default_share ? note('info', `${num(coverage.default_share)}% нагрузки — по весу по умолчанию`,
                `Товаров без веса: ${coverage.products_missing} из ${coverage.products_total}.`) : ''}

            <div id="sloadWeightRows">${weightRows(items)}</div>`;

        const overlay = modal('Веса товаров', body,
            '<button class="sload-btn" data-save-weights>Сохранить веса</button>' +
            '<button class="sload-btn sload-btn--ghost" data-close>Закрыть</button>');

        overlay.addEventListener('click', async e => {
            const tab = e.target.closest('[data-weights-tab]');
            if (tab) {
                overlay.remove();
                openWeights(tab.dataset.weightsTab === 'missing');
                return;
            }

            const saveDefault = e.target.closest('[data-save-default]');
            if (saveDefault) {
                saveDefault.disabled = true;
                try {
                    await api('/api/couriers/weights/default', postOptions({
                        weight: Number(overlay.querySelector('#sloadDefaultWeight').value)
                    }));
                    toast('Вес по умолчанию сохранён', 'success');
                    state.data = null;
                    load();
                } catch (error) {
                    toast('Не удалось сохранить: ' + error.message, 'error');
                }
                saveDefault.disabled = false;
                return;
            }

            const save = e.target.closest('[data-save-weights]');
            if (!save) return;

            const weights = {};
            overlay.querySelectorAll('[data-weight-input]').forEach(input => {
                const initial = input.dataset.initial;
                const value = input.value.trim();
                if (value === initial) return;
                weights[input.dataset.offer] = value === '' ? null : Number(value);
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

    function weightRows(items) {
        if (!items.length) {
            return '<div class="sload-empty"><p>Товаров нет — либо всё взвешено, либо за 60 дней их не заказывали.</p></div>';
        }
        return `<table class="sload-modal-table">
            <thead><tr><th>Товар</th><th>Артикул</th><th>Заказов</th><th>Штук</th><th>Вес</th></tr></thead>
            <tbody>${items.map(item => `
                <tr>
                    <td>${esc(item.product_name || ('Товар ' + item.offer_id))}</td>
                    <td>${esc(item.article || '—')}</td>
                    <td>${item.orders}</td>
                    <td>${num(item.quantity)}</td>
                    <td><input type="number" min="0.1" step="0.1" class="sload-weight-input"
                        data-weight-input data-offer="${item.offer_id}"
                        data-initial="${item.weight === null ? '' : esc(item.weight)}"
                        value="${item.weight === null ? '' : esc(item.weight)}"
                        placeholder="по умолч."></td>
                </tr>`).join('')}</tbody>
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
