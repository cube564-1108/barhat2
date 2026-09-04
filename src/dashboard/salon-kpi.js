/**
 * Показатели салонов — восемь цифр по салону за месяц из четырёх источников.
 *
 * Данные читаются из /api/salon-kpi/*; вся арифметика на сервере, здесь только
 * отображение. Показатель, который сервер не смог посчитать, приходит как null
 * с причиной — и рисуется прочерком с пояснением, а не нулём: «данных нет» и
 * «значение нулевое» человек читает по-разному.
 *
 * Диалоги — только window.BarhatUI: нативные alert/confirm внутри iframe Пульса
 * молча игнорируются, и кнопка выглядит сломанной.
 */

(function () {
    'use strict';

    const NBSP = ' ';
    const FLOWER_LOSS_ALERT = 20;

    let state = {
        month: currentMonth(),
        scope: 'salon',
        store: 'all',
        sort: 'plan',
        openRow: null,
        data: null,
        details: {},
        isAdmin: false,
        loading: false
    };

    // ================= Утилиты =================

    function currentMonth() {
        const now = new Date();
        return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`;
    }

    function monthLabel(month) {
        const names = ['Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь',
            'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь'];
        return `${names[parseInt(month.slice(5, 7), 10) - 1]} ${month.slice(0, 4)}`;
    }

    function monthOptions(count) {
        const out = [];
        const now = new Date();
        for (let i = 0; i < count; i++) {
            const d = new Date(now.getFullYear(), now.getMonth() - i, 1);
            out.push(`${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`);
        }
        return out;
    }

    // Экранирование через replace, а не через textContent: тот не экранирует
    // кавычку и молча резал бы значения внутри value="..."
    function esc(value) {
        return String(value === null || value === undefined ? '' : value)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    function money(value) {
        if (value === null || value === undefined) return '—';
        return Math.round(value).toString().replace(/\B(?=(\d{3})+(?!\d))/g, NBSP) + NBSP + '₽';
    }

    function short(value) {
        if (value === null || value === undefined) return '—';
        if (value >= 1000000) {
            const mln = value / 1000000;
            const text = mln >= 100 ? String(Math.round(mln)) : mln.toFixed(1).replace(/[.,]0$/, '');
            return text.replace('.', ',') + NBSP + 'млн';
        }
        if (value >= 1000) return Math.round(value / 1000) + NBSP + 'тыс.';
        return Math.round(value).toString();
    }

    function shortMoney(value) {
        return value === null || value === undefined ? '—' : short(value) + NBSP + '₽';
    }

    function pct(value, digits) {
        if (value === null || value === undefined) return '—';
        return value.toFixed(digits === undefined ? 1 : digits).replace('.', ',') + '%';
    }

    function num(value, digits) {
        if (value === null || value === undefined) return '—';
        return value.toFixed(digits === undefined ? 1 : digits).replace('.', ',');
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

    // ================= Загрузка =================

    async function load() {
        const host = document.getElementById('salonKpiContent');
        if (!host) return;

        if (!state.data) {
            host.innerHTML = '<div class="skpi-empty"><p>Загружаем показатели…</p></div>';
        }
        state.loading = true;

        try {
            const result = await api(
                `/api/salon-kpi/summary?month=${encodeURIComponent(state.month)}&scope=${state.scope}`);
            state.data = result.data;
            state.isAdmin = !!result.data.can_edit;
        } catch (error) {
            host.innerHTML = `<div class="skpi-empty"><h3>Не удалось загрузить показатели</h3>
                <p>${esc(error.message)}</p></div>`;
            state.loading = false;
            return;
        }

        state.loading = false;
        render();

        if (state.isAdmin) loadAlerts();
    }

    async function loadAlerts() {
        try {
            const [unmapped, couriers] = await Promise.all([
                api(`/api/salon-kpi/unmapped?month=${encodeURIComponent(state.month)}`),
                api(`/api/salon-kpi/unflagged-couriers?month=${encodeURIComponent(state.month)}`)
            ]);
            state.unmapped = unmapped.data.items || [];
            state.unflagged = couriers.couriers || [];
            render();
        } catch (error) {
            // Не критично: показатели уже на экране, а список несопоставленного
            // нужен только администратору
            console.warn('Не удалось получить список несопоставленного:', error);
        }
    }

    // ================= Плитки =================

    function delta(value, label) {
        if (value === null || value === undefined) {
            return '<div class="skpi-delta skpi-delta--flat">нет данных за прошлый период</div>';
        }
        const cls = Math.abs(value) < 1 ? 'flat' : (value > 0 ? 'up' : 'down');
        const arrow = Math.abs(value) < 1 ? '' : (value > 0 ? '▲ ' : '▼ ');
        return `<div class="skpi-delta skpi-delta--${cls}">${arrow}${pct(Math.abs(value))}
            <span class="skpi-delta__vs">${esc(label)}</span></div>`;
    }

    function kpi(o) {
        return `<div class="skpi-kpi${o.flag ? ' skpi-kpi--flag' : ''}">
            <button class="skpi-kpi__info" type="button" data-how="${o.id}"
                aria-label="Как считается: ${esc(o.label)}">i</button>
            <div class="skpi-kpi__label">${esc(o.label)}</div>
            <div class="skpi-kpi__value">${o.value}${o.unit
                ? `<span class="skpi-kpi__unit">${esc(o.unit)}</span>` : ''}</div>
            ${o.bar || ''}
            ${o.mean ? `<div class="skpi-kpi__mean">${o.mean}</div>` : ''}
            ${o.sub ? `<div class="skpi-kpi__sub">${esc(o.sub)}</div>` : ''}
            ${o.delta || ''}
            <div class="skpi-kpi__how" id="skpiHow-${o.id}">${esc(o.how)}</div>
        </div>`;
    }

    function kpisHtml(m, progress) {
        const out = [];
        const ship = m.shipments;

        let bar = '';
        if (ship.plan) {
            const done = Math.min(ship.plan_done || 0, 100);
            const pace = progress.days ? progress.passed / progress.days * 100 : 0;
            const low = (ship.plan_done || 0) < pace - 5;
            bar = `<div class="skpi-bar"><div class="skpi-bar__fill${low ? ' skpi-bar__fill--low' : ''}"
                    style="width:${done}%"></div></div>
                <div class="skpi-bar__pace"><span class="skpi-pace-mark" style="left:${pace}%"></span></div>`;
        }

        out.push(kpi({
            id: 'fact', label: 'Отгружено за месяц', value: short(ship.fact), unit: '₽',
            bar: bar,
            mean: ship.plan
                ? `<b>${pct(ship.plan_done, 0)}</b> плана${progress.running
                    ? ' · метка — где надо быть сегодня' : ''}`
                : 'План не задан',
            sub: ship.plan ? `План ${shortMoney(ship.plan)}`
                : (state.isAdmin ? 'Задайте план кнопкой «Планы на месяц»'
                    : 'Попросите администратора задать план'),
            delta: delta(ship.delta, progress.running
                ? 'к той же дате прошлого месяца' : 'к прошлому месяцу'),
            how: 'Сумма заказов в статусе «Выполнен», отнесённых к месяцу по дате доставки. ' +
                 'Берётся стоимость товаров, без стоимости доставки. Источник — RetailCRM.'
        }));

        out.push(kpi({
            id: 'street', label: 'Канал «Улица»', value: pct(m.street.share, 0),
            mean: `<b>${shortMoney(m.street.amount)}</b> из ${shortMoney(ship.fact)}`,
            sub: 'Заказы, оформленные в салоне',
            how: 'Доля отгрузок с каналом «Улица» (в CRM — способ оформления offline) ' +
                 'в общей сумме отгрузок салона.'
        }));

        out.push(kpi({
            id: 'nos', label: 'Негативная ОС', value: String(m.nos.confirmed),
            mean: `подтверждённых ${plural(m.nos.confirmed, 'обращение', 'обращения', 'обращений')}`,
            sub: m.nos.in_review
                ? `Ещё ${m.nos.in_review} в разборе — объективность не проставлена`
                : 'Всё разобрано',
            flag: m.nos.confirmed >= 5,
            how: 'Обращения из формы «Негативная ОС по заказу» в Pyrus за месяц, у которых ' +
                 'объективность — «Подтверждено». Обращения в разборе показаны отдельно: пока их ' +
                 'не разобрали, основная цифра занижена.'
        }));

        const q = m.quality;
        const scale = [];
        if (q.count14) scale.push(`${num(q.avg14, 1)} из 14`);
        if (q.count18) scale.push(`${num(q.avg18, 1)} из 18`);
        const counts = [];
        if (q.count14) counts.push(`${q.count14} по 14-балльным`);
        if (q.count18) counts.push(`${q.count18} по 18-балльным`);

        out.push(kpi({
            id: 'quality', label: 'Качество сборки',
            value: q.percent === null ? '—' : pct(q.percent, 0),
            unit: q.percent === null ? '' : 'от максимума',
            mean: scale.length ? `<b>${scale.join(' · ')}</b>` : 'Оценок за период нет',
            sub: q.count
                ? `${q.count} ${plural(q.count, 'оценка', 'оценки', 'оценок')}: ${counts.join(', ')}` +
                  (q.count < 15 ? ' — для выводов маловато' : '')
                : '',
            flag: q.percent !== null && q.percent < 90,
            how: 'Проверки сборки из формы качества в Pyrus. У видов заказа два максимума: ' +
                 '14 баллов (клубничный и цветочный букет, коробочка, боксы) и 18 ' +
                 '(клубнично-цветочный букет, коробочка с букетом, цветочно-клубничный бокс). ' +
                 'Средние баллы этих групп не смешиваются — показаны отдельно, а сравнимая ' +
                 'величина одна: набранные баллы к возможным.'
        }));

        out.push(kpi({
            id: 'taxi', label: 'Отдано такси-службам', value: pct(m.taxi.share, 0),
            mean: `<b>${m.taxi.taxi_orders}</b> из ${m.taxi.courier_orders} курьерских заказов`,
            sub: 'Яндекс Доставка, Максим Такси, Драйв такси',
            flag: (m.taxi.share || 0) > 60,
            how: 'Из заказов с типом доставки «Доставка курьером» — доля тех, где курьером ' +
                 'указана внешняя такси-служба. Считается по количеству заказов, не по сумме.'
        }));

        const cost = m.raw_cost;
        out.push(kpi({
            id: 'rawcost', label: 'Расходы на цветок и клубнику',
            value: cost.total === null ? '—' : pct(cost.total, 0),
            unit: cost.total === null ? '' : 'от отгрузок',
            mean: cost.total === null ? 'Нет отгрузок за период'
                : `Цветок <b>${pct(cost.flower, 0)}</b> · клубника <b>${pct(cost.berry, 0)}</b>`,
            sub: `Оприходовано ${shortMoney(cost.flower_amount)} и ${shortMoney(cost.berry_amount)}`,
            how: 'Сумма оприходованного за период товара, делённая на сумму отгруженных заказов ' +
                 'за тот же период. Цветок и клубника считаются отдельно, каждый — к общей сумме ' +
                 'отгрузок салона. Это закупка периода, а не себестоимость проданного: товар, ' +
                 'закупленный в конце месяца, поднимет долю, хотя продан будет в следующем.'
        }));

        const loss = m.flower_loss;
        out.push(kpi({
            id: 'flowerloss', label: 'Списание цветка',
            value: loss.share === null ? '—' : pct(loss.share, 0),
            unit: loss.share === null ? '' : 'от прихода',
            mean: loss.share === null ? 'Оприходования за период нет'
                : `<b>${shortMoney(loss.written_off)}</b> списано из ${shortMoney(loss.received)}`,
            sub: loss.alert ? 'Похоже на вопрос к учёту, а не на потери' : 'По документам МойСклада',
            flag: loss.alert,
            how: 'Сумма списанного цветка за период, делённая на сумму оприходованного цветка за ' +
                 'тот же период по складу салона. Товары из папки «Товары МС / Цветы», документы — ' +
                 '«Списание» и «Оприходование» в МойСкладе.'
        }));

        const berry = m.berry_price;
        out.push(kpi({
            id: 'berry', label: 'Средняя цена клубники',
            value: berry.price === null ? '—' : Math.round(berry.price).toString()
                .replace(/\B(?=(\d{3})+(?!\d))/g, NBSP),
            unit: berry.price === null ? '' : '₽/кг',
            mean: berry.price === null
                ? esc(berry.note || 'Нет данных')
                : `Закупочная <b>${Math.round(berry.buy_price)} ₽/кг</b>`,
            sub: berry.in_qty
                ? `Приход ${num(berry.in_qty / (berry.qty_per_kg || 1), 0)} кг, ` +
                  `списано ${num(berry.out_qty / (berry.qty_per_kg || 1), 0)} кг`
                : 'Оприходования за период нет',
            flag: !!berry.note,
            how: 'Сумма оприходованной клубники за период, делённая на разницу веса: вес ' +
                 'оприходованной минус вес списанной. То есть во сколько обошёлся килограмм ' +
                 'клубники, реально пошедшей в дело. Рядом закупочная цена — видно, сколько ' +
                 'добавило списание.'
        }));

        return out.join('');
    }

    // ================= Итог месяца словами =================

    function summaryHtml(data) {
        const m = data.total;
        const ship = m.shipments;
        const progress = data.progress;
        const parts = [`Отгружено <b>${money(ship.fact)}</b>`];
        if (progress.running) {
            parts.push(`за ${progress.passed} ${plural(progress.passed, 'день', 'дня', 'дней')} месяца`);
        }
        let text = parts.join(' ') + '. ';
        let kind = 'flat';

        if (!ship.plan) {
            text += 'План на месяц не задан — процент выполнения не считается. ';
        } else if (!progress.running) {
            kind = ship.fact >= ship.plan ? 'up' : 'down';
            text += ship.fact >= ship.plan
                ? `План выполнен: ${pct(ship.plan_done)} от плана. `
                : `План не выполнен: ${pct(ship.plan_done)}, не хватило ${shortMoney(ship.plan - ship.fact)}. `;
        } else {
            const expected = ship.expected_now || 0;
            const diff = ship.fact - expected;
            const tolerance = expected * 0.05;
            if (Math.abs(diff) <= tolerance) {
                text += `Идёте вровень с планом. Чтобы закрыть месяц — ${shortMoney(ship.need_per_day)} в день. `;
            } else if (diff > 0) {
                kind = 'up';
                text += `Опережаете план на ${shortMoney(Math.abs(diff))}. ` +
                    `Чтобы удержать — ${shortMoney(ship.need_per_day)} в день. `;
            } else {
                kind = 'down';
                text += `Отстаёте от плана на ${shortMoney(Math.abs(diff))}. Чтобы догнать — ` +
                    `${shortMoney(ship.need_per_day)} в день вместо ${shortMoney(ship.per_day_now)} сейчас. `;
            }
        }

        const worries = [];
        if (m.flower_loss.alert) {
            worries.push(`списано ${pct(m.flower_loss.share, 0)} оприходованного цветка`);
        }
        if (m.nos.confirmed >= 5) worries.push(`подтверждённого негатива ${m.nos.confirmed}`);
        if (m.quality.percent !== null && m.quality.percent < 90) {
            worries.push('качество ниже 90% от максимума');
        }
        if ((m.taxi.share || 0) > 60) {
            worries.push(`такси-службам отдано ${pct(m.taxi.share, 0)} курьерских заказов`);
        }
        if (worries.length) text += 'Стоит посмотреть: ' + worries.join(', ') + '.';

        const mark = kind === 'down'
            ? '<path d="M12 8v5"/><path d="M12 17h.01"/><circle cx="12" cy="12" r="9"/>'
            : '<path d="M20 6L9 17l-5-5"/>';

        const scopeName = state.store !== 'all'
            ? (data.rows.find(r => String(r.store_id) === state.store) || {}).name
            : (state.isAdmin ? 'Вся сеть' : 'Мои салоны');

        return `<div class="skpi-summary">
            <div class="skpi-summary__mark">${icon(mark, 20)}</div>
            <div>
                <div class="skpi-summary__text">${text}</div>
                <div class="skpi-summary__meta">${esc(scopeName || '')} · ${monthLabel(data.month)}
                    · период ${esc(data.period.from)} — ${esc(data.period.to)}</div>
            </div>
        </div>`;
    }

    // ================= Плашки =================

    function notice(kind, text, action) {
        const paths = kind === 'warn'
            ? '<path d="M12 9v4"/><path d="M12 17h.01"/><path d="M10.3 3.9L1.8 18a2 2 0 001.7 3h17a2 2 0 001.7-3L14.7 3.9a2 2 0 00-3.4 0z"/>'
            : '<circle cx="12" cy="12" r="9"/><path d="M12 16v-4"/><path d="M12 8h.01"/>';
        return `<div class="skpi-notice skpi-notice--${kind}">${icon(paths, 17)}
            <div>${text}</div>
            ${action ? `<div class="skpi-notice__act">${action}</div>` : ''}</div>`;
    }

    function noticesHtml(data) {
        const out = [];

        if (data.plans_missing && data.plans_missing.length) {
            out.push(notice('warn',
                `Не задан план на ${monthLabel(data.month).toLowerCase()}: ` +
                data.plans_missing.map(esc).join(', ') + '. Выполнение по ним не считается.',
                state.isAdmin
                    ? '<button class="skpi-btn-light" style="background:var(--bx-wine)" data-action="plans">Задать план</button>'
                    : ''));
        }

        if (state.isAdmin && state.unmapped && state.unmapped.length) {
            out.push(notice('warn',
                `Не отнесено ни к одному салону: ${state.unmapped.length} ` +
                plural(state.unmapped.length, 'источник', 'источника', 'источников') +
                ' — часть заказов и обращений не попадает в показатели.',
                '<button class="skpi-btn-light" style="background:var(--bx-wine)" data-action="mapping">Разобрать</button>'));
        }

        if (state.isAdmin && state.unflagged && state.unflagged.length) {
            const first = state.unflagged[0];
            out.push(notice('info',
                `Курьер «${esc(first.name || first.courier_id)}» (${first.orders} ` +
                plural(first.orders, 'заказ', 'заказа', 'заказов') + ` с ${esc(first.since)}) ` +
                'не отмечен такси-службой — доля такси его не учитывает.',
                '<button class="skpi-btn-light" style="background:var(--bx-wine)" data-action="couriers">Отметить</button>'));
        }

        const fresh = data.freshness || {};
        if (fresh.crm && fresh.crm.since && data.period.from < fresh.crm.since) {
            out.push(notice('info',
                `Данные по каналам и суммам заказов есть с ${esc(fresh.crm.since)} — за более ранние ` +
                'дни показатели неполные.'));
        }

        return out.join('');
    }

    // ================= Таблица =================

    function rowsHtml(data) {
        if (data.no_stores) {
            return `<div class="skpi-empty">
                <h3>Салоны не привязаны к вашей учётной записи</h3>
                <p>Показатели считаются по салонам, за которые вы отвечаете. Попросите
                администратора привязать салоны к вашей учётной записи — после этого экран
                заполнится.</p></div>`;
        }
        if (!data.rows.length) {
            return '<div class="skpi-empty"><h3>Нет данных за период</h3><p>За выбранный месяц ' +
                'показателей нет.</p></div>';
        }

        const sorters = {
            plan: (a, b) => (b.metrics.shipments.plan_done === null) - (a.metrics.shipments.plan_done === null)
                || (a.metrics.shipments.plan_done || 0) - (b.metrics.shipments.plan_done || 0),
            fact: (a, b) => b.metrics.shipments.fact - a.metrics.shipments.fact,
            nos: (a, b) => b.metrics.nos.confirmed - a.metrics.nos.confirmed,
            quality: (a, b) => (a.metrics.quality.percent || 0) - (b.metrics.quality.percent || 0),
            loss: (a, b) => (b.metrics.flower_loss.share || 0) - (a.metrics.flower_loss.share || 0),
            cost: (a, b) => (b.metrics.raw_cost.total || 0) - (a.metrics.raw_cost.total || 0)
        };
        const rows = data.rows.slice().sort(sorters[state.sort] || sorters.plan);

        const head = `<div class="skpi-row-head">
            <div>${state.scope === 'city' ? 'Город' : 'Салон'}</div>
            <div><button type="button" data-sort="fact">Отгружено / план</button></div>
            <div><button type="button" data-sort="plan">Выполн.</button></div>
            <div>Улица</div>
            <div><button type="button" data-sort="cost">Сырьё</button></div>
            <div><button type="button" data-sort="loss">Спис. цветка</button></div>
            <div>Клубника</div>
            <div><button type="button" data-sort="nos">Негатив</button></div>
            <div><button type="button" data-sort="quality">Качество</button></div>
            <div></div></div>`;

        const body = rows.map(row => {
            const m = row.metrics;
            const key = state.scope === 'city' ? `city:${row.city}` : `salon:${row.store_id}`;
            const open = state.openRow === key;
            const title = state.scope === 'city' ? row.city : row.name;
            const sub = state.scope === 'city'
                ? `${row.stores.length} ${plural(row.stores.length, 'салон', 'салона', 'салонов')}`
                : (row.city || '');

            const planCell = m.shipments.plan
                ? `<span class="skpi-badge skpi-badge--${(m.shipments.plan_done || 0) < 90 ? 'warn' : 'ok'}">${pct(m.shipments.plan_done, 0)}</span>`
                : '<span class="skpi-badge skpi-badge--neutral">план не задан</span>';

            const lossBad = m.flower_loss.share !== null && m.flower_loss.share >= FLOWER_LOSS_ALERT;
            const q = m.quality;
            const scale = [];
            if (q.count14) scale.push(`${num(q.avg14, 1)}/14`);
            if (q.count18) scale.push(`${num(q.avg18, 1)}/18`);

            return `<button class="skpi-row${open ? ' is-open' : ''}" type="button"
                    data-row="${esc(key)}" aria-expanded="${open}">
                <div class="skpi-row__name">${esc(title)}<div class="skpi-row__city">${esc(sub)}</div></div>
                <div><span class="skpi-cell-label">Отгружено</span><span class="skpi-row__num">${shortMoney(m.shipments.fact)}
                    <small>план ${m.shipments.plan ? shortMoney(m.shipments.plan) : '—'}</small></span></div>
                <div><span class="skpi-cell-label">Выполнение</span>${planCell}</div>
                <div><span class="skpi-cell-label">Улица</span><span class="skpi-row__num">${pct(m.street.share, 0)}
                    <small>${shortMoney(m.street.amount)}</small></span></div>
                <div><span class="skpi-cell-label">Расходы на сырьё</span><span class="skpi-row__num">${pct(m.raw_cost.total, 0)}
                    <small>${m.raw_cost.total === null ? 'нет отгрузок'
                        : `цв ${pct(m.raw_cost.flower, 0)} · кл ${pct(m.raw_cost.berry, 0)}`}</small></span></div>
                <div><span class="skpi-cell-label">Списание цветка</span><span class="skpi-row__num${lossBad ? ' skpi-row__num--bad' : ''}">${pct(m.flower_loss.share, 0)}
                    <small>${m.flower_loss.written_off ? shortMoney(m.flower_loss.written_off) : 'нет списаний'}</small></span></div>
                <div><span class="skpi-cell-label">Клубника</span><span class="skpi-row__num">${m.berry_price.price === null ? '—'
                    : Math.round(m.berry_price.price).toString().replace(/\B(?=(\d{3})+(?!\d))/g, NBSP) + ' ₽/кг'}
                    <small>${m.berry_price.buy_price ? 'закуп. ' + Math.round(m.berry_price.buy_price) + ' ₽/кг' : 'нет прихода'}</small></span></div>
                <div><span class="skpi-cell-label">Негатив</span><span class="skpi-row__num">${m.nos.confirmed}
                    <small>${m.nos.in_review ? '+' + m.nos.in_review + ' в разборе' : 'разобрано'}</small></span></div>
                <div><span class="skpi-cell-label">Качество</span><span class="skpi-row__num">${pct(q.percent, 0)}
                    <small>${scale.join(' · ') || 'нет оценок'}</small></span></div>
                <div class="skpi-row__chev">${icon('<path d="M6 9l6 6 6-6"/>', 18)}</div>
            </button>${open ? detailsHtml(row, key) : ''}`;
        }).join('');

        return head + body;
    }

    function barList(items, kind) {
        if (!items.length) return '<div class="skpi-dempty">Нет данных за период</div>';
        const max = Math.max.apply(null, items.map(i => i.value));
        return '<div class="skpi-dlist">' + items.map(i => `
            <div class="skpi-dline"><span class="skpi-dline__name">${esc(i.name)}</span>
            <span class="skpi-dbar"><span class="skpi-dbar__fill" style="width:${max ? i.value / max * 100 : 0}%"></span></span>
            <span class="skpi-dline__val">${kind === 'money' ? short(i.value) : i.value}</span></div>`).join('') + '</div>';
    }

    function detailsHtml(row, key) {
        const m = row.metrics;
        const details = state.details[key];

        const channels = (m.channels || []).map(c => ({ name: c.name, value: c.amount }));
        const cats = Object.keys(m.nos.categories || {})
            .map(k => ({ name: k, value: m.nos.categories[k] }))
            .sort((a, b) => b.value - a.value);

        let florists = '<div class="skpi-dempty">Загружаем…</div>';
        if (details) {
            florists = details.florists.length
                ? '<div class="skpi-dlist">' + details.florists.map(f => {
                    // Обе шкалы показываем раздельно: у флориста, собирающего больше
                    // 18-балльных заказов, сырой средний балл выше просто из-за состава
                    const counts = [
                        f.count14 ? `${f.count14} по 14-балльным` : '',
                        f.count18 ? `${f.count18} по 18-балльным` : ''
                    ].filter(Boolean).join(' · ');
                    return `<div class="skpi-dline skpi-dline--stack">
                        <span class="skpi-dline__name">${esc(f.name)}<small>${counts}</small></span>
                        <span class="skpi-dbar"><span class="skpi-dbar__fill" style="width:${f.percent || 0}%"></span></span>
                        <span class="skpi-dline__val">${pct(f.percent, 0)}</span>
                    </div>`;
                }).join('') + '</div>'
                : '<div class="skpi-dempty">Оценок за период нет</div>';
        } else if (state.scope === 'city') {
            florists = '<div class="skpi-dempty">Доступно в разрезе по салонам</div>';
        }

        const berry = m.berry_price;
        const perKg = berry.qty_per_kg || 1;
        const warehouse = [
            ['Оприходовано цветка', shortMoney(m.flower_loss.received)],
            ['— доля в отгрузках', pct(m.raw_cost.flower, 1)],
            ['Списано цветка', shortMoney(m.flower_loss.written_off)],
            ['Доля списания', pct(m.flower_loss.share, 1)],
            ['Оприходовано клубники', `${num(berry.in_qty / perKg, 1)} кг · ${shortMoney(m.raw_cost.berry_amount)}`],
            ['— доля в отгрузках', pct(m.raw_cost.berry, 1)],
            ['Списано клубники', `${num(berry.out_qty / perKg, 1)} кг`],
            ['Пошло в дело', berry.used_qty > 0 ? `${num(berry.used_qty / perKg, 1)} кг` : '—'],
            ['Закупочная цена', berry.buy_price ? `${Math.round(berry.buy_price)} ₽/кг` : '—'],
            ['С учётом списания', berry.price ? `${Math.round(berry.price)} ₽/кг` : '—']
        ];

        return `<div class="skpi-details"><div class="skpi-details__grid">
            <div class="skpi-dcard"><h4>Качество по флористам</h4>${florists}
                <div class="skpi-dnote">Процент — от максимума своего вида заказа: 14-балльные и
                18-балльные не смешиваются. Имена обезличены в форме Pyrus.</div></div>
            <div class="skpi-dcard"><h4>Негатив по категориям</h4>${barList(cats, 'count')}
                ${m.nos.in_review ? `<div class="skpi-dnote">Ещё ${m.nos.in_review} в разборе —
                попадут сюда, когда проставят объективность.</div>` : ''}</div>
            <div class="skpi-dcard"><h4>Отгрузки по каналам</h4>${barList(channels, 'money')}</div>
            <div class="skpi-dcard"><h4>Склад: цветок и клубника</h4>
                <div class="skpi-dlist">${warehouse.map(l => `
                    <div class="skpi-dline"><span class="skpi-dline__name">${esc(l[0])}</span>
                    <span class="skpi-dline__val">${l[1]}</span></div>`).join('')}</div>
                <div class="skpi-dnote">Документы «Оприходование» и «Списание» в МойСкладе по складу
                салона, товары из папок «Товары МС / Цветы» и «/ Клубника».</div></div>
        </div></div>`;
    }

    // ================= Рендер =================

    function render() {
        const host = document.getElementById('salonKpiContent');
        if (!host || !state.data) return;

        const data = state.data;
        const months = monthOptions(13);
        const stores = data.rows.filter(r => r.store_id);

        // Прокрутку возвращаем на место: экран перерисовывается целиком, и без
        // этого смена месяца или раскрытие строки выбрасывает в начало страницы
        const scrollTop = window.pageYOffset;

        host.innerHTML = `
            <div class="skpi-header">
                <div>
                    <h2 class="skpi-header__title">Показатели салонов</h2>
                    <p class="skpi-header__sub">${monthLabel(data.month)} · период
                        ${esc(data.period.from)} — ${esc(data.period.to)}</p>
                </div>
                ${state.isAdmin ? `<div class="skpi-header__actions">
                    <button class="skpi-btn-light" data-action="plans">Планы на месяц</button>
                    <button class="skpi-btn-light" data-action="mapping">Сопоставление</button>
                </div>` : ''}
            </div>

            <div class="skpi-filters">
                <select class="skpi-select" id="skpiMonth" aria-label="Месяц">
                    ${months.map(m => `<option value="${m}"${m === state.month ? ' selected' : ''}>${monthLabel(m)}</option>`).join('')}
                </select>
                ${stores.length > 1 || state.scope === 'city' ? `
                <div class="skpi-tabs">
                    <button class="skpi-tab${state.scope === 'salon' ? ' is-active' : ''}" data-scope="salon">По салонам</button>
                    <button class="skpi-tab${state.scope === 'city' ? ' is-active' : ''}" data-scope="city">По городам</button>
                </div>` : ''}
            </div>

            <div>${noticesHtml(data)}</div>
            ${data.no_stores ? '' : summaryHtml(data)}
            ${data.no_stores ? '' : `<div class="skpi-kpis">${kpisHtml(data.total, data.progress)}</div>`}

            <div class="skpi-section-head">
                <h3>${state.scope === 'city' ? 'Города' : (state.isAdmin ? 'Салоны сети' : 'Мои салоны')}</h3>
                <span>${data.no_stores ? '' : 'Нажмите на строку, чтобы раскрыть детали'}</span>
            </div>
            <div class="skpi-rows">${rowsHtml(data)}</div>
            <p style="font-size:12px;color:var(--bx-muted);margin:14px 2px 0">
                Отгрузка — заказ в статусе «Выполнен», месяц считается по дате доставки.
                Сумма — без стоимости доставки.</p>`;

        window.scrollTo(0, scrollTop);
    }

    // ================= Модалки =================

    function modal(title, bodyHtml, footerHtml) {
        const overlay = document.createElement('div');
        overlay.className = 'modal-overlay';
        overlay.style.cssText = 'position:fixed;inset:0;background:rgba(19,8,16,.45);z-index:1000;' +
            'display:flex;align-items:center;justify-content:center;padding:16px';
        overlay.innerHTML = `
            <div class="skpi-modal" style="position:relative;background:#fff;border-radius:16px;
                    width:100%;max-width:760px;max-height:88vh;display:flex;flex-direction:column;
                    overflow:hidden;font-size:14px">
                <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;
                        padding:18px 20px;border-bottom:1px solid #eee2ea">
                    <h3 style="margin:0;font-family:'Vollkorn',Georgia,serif;color:#411330;font-size:18px">${esc(title)}</h3>
                    <button type="button" data-close style="background:none;border:none;cursor:pointer;
                        color:#9b8f97;padding:4px;line-height:0">${icon('<path d="M18 6L6 18M6 6l12 12"/>', 20)}</button>
                </div>
                <div class="skpi-modal-body" style="padding:18px 20px;overflow-y:auto">${bodyHtml}</div>
                <div style="padding:14px 20px;border-top:1px solid #eee2ea;display:flex;gap:8px;
                        justify-content:flex-end">${footerHtml || ''}</div>
            </div>`;

        overlay.addEventListener('click', e => {
            if (e.target === overlay || e.target.closest('[data-close]')) overlay.remove();
        });
        document.body.appendChild(overlay);
        return overlay;
    }

    async function openPlans() {
        let plans;
        try {
            plans = await api(`/api/salon-kpi/plans?month=${encodeURIComponent(state.month)}`);
        } catch (error) {
            window.BarhatUI.toast('Не удалось загрузить планы: ' + error.message, 'error');
            return;
        }

        const factByStore = {};
        (state.data.rows || []).forEach(r => { factByStore[r.store_id] = r.metrics.shipments.fact; });

        const body = `
            <p class="skpi-hint">План вводит администратор. Пустое поле — плана нет: процент
            выполнения по такому салону не считается, а не показывается нулём. Изменения попадают
            в журнал действий.</p>
            <div id="skpiPlanRows">${plans.plans.map(p => `
                <div class="skpi-plan-row">
                    <div><div>${esc(p.name)}</div>
                        <div class="skpi-plan-row__fact">Факт ${shortMoney(factByStore[p.store_id] || 0)}</div></div>
                    <input class="skpi-input" inputmode="numeric" data-store="${p.store_id}"
                        value="${p.amount === null ? '' : Math.round(p.amount)}" placeholder="Не задан"
                        aria-label="План для ${esc(p.name)}">
                </div>`).join('')}</div>`;

        const overlay = modal(`Планы на ${monthLabel(state.month).toLowerCase()}`, body,
            `<button class="skpi-btn-light" style="background:#fff;color:#411330;border:1px solid #eee2ea" data-close>Отмена</button>
             <button class="skpi-btn-light" style="background:#411330" id="skpiPlanSave">Сохранить</button>`);

        overlay.querySelector('#skpiPlanSave').addEventListener('click', async function () {
            // Кнопка блокируется на время запроса: один медленный клик иначе
            // превращается в три запроса
            this.disabled = true;
            const inputs = overlay.querySelectorAll('#skpiPlanRows input');
            const payload = [];
            for (const input of inputs) {
                const raw = input.value.replace(/\s/g, '').replace(',', '.');
                if (raw && isNaN(Number(raw))) {
                    window.BarhatUI.toast('План должен быть числом', 'error');
                    this.disabled = false;
                    return;
                }
                payload.push({ store_id: Number(input.dataset.store), amount: raw ? Number(raw) : null });
            }

            try {
                await api('/api/salon-kpi/plans', postOptions({ month: state.month, plans: payload }));
                window.BarhatUI.toast('Планы сохранены', 'success');
                overlay.remove();
                state.data = null;
                load();
            } catch (error) {
                window.BarhatUI.toast('Не удалось сохранить: ' + error.message, 'error');
                this.disabled = false;
            }
        });
    }

    async function openMapping() {
        let unmapped, links;
        try {
            [unmapped, links] = await Promise.all([
                api(`/api/salon-kpi/unmapped?month=${encodeURIComponent(state.month)}`),
                api('/api/salon-kpi/links')
            ]);
        } catch (error) {
            window.BarhatUI.toast('Не удалось загрузить справочник: ' + error.message, 'error');
            return;
        }

        const stores = links.stores || [];
        const items = unmapped.data.items || [];
        const couriers = state.unflagged || [];

        const options = store => stores.map(s =>
            `<option value="${s.id}"${store && s.id === store ? ' selected' : ''}>${esc(s.name)}</option>`).join('');

        const body = `
            <p class="skpi-hint">Салон называется по-разному в CRM, в двух формах Pyrus и на складе
            МойСклада. Здесь всё, что система встретила в данных, но не смогла отнести ни к одному
            салону, — и сколько на этом теряется. Система предлагает вариант, решение принимает человек.</p>
            <div id="skpiMapRows">${items.length ? items.map((item, index) => `
                <div class="skpi-map-row" data-index="${index}">
                    <div><div class="skpi-map-row__key">${esc(item.label || item.key)}</div>
                        <div class="skpi-map-row__meta">${esc(item.source_name)} · ${esc(item.meta)}</div>
                        ${item.suggestion ? `<div class="skpi-map-row__meta">
                            <span class="skpi-suggest">похоже на: ${esc(item.suggestion.name)}</span></div>` : ''}</div>
                    <div class="skpi-map-row__act">
                        <select class="skpi-select" data-store>
                            <option value="">Не привязывать</option>
                            ${options(item.suggestion ? item.suggestion.store_id : null)}
                        </select>
                        <button class="skpi-btn-light" style="background:#411330" data-bind
                            data-source="${esc(item.source)}" data-key="${esc(item.key)}">Привязать</button>
                    </div>
                </div>`).join('') : '<p class="skpi-dempty">Всё сопоставлено — потерянных данных нет.</p>'}
            ${couriers.length ? couriers.map(c => `
                <div class="skpi-map-row">
                    <div><div class="skpi-map-row__key">${esc(c.name || ('Курьер ' + c.courier_id))}</div>
                        <div class="skpi-map-row__meta">RetailCRM, курьер · ${c.orders}
                            ${plural(c.orders, 'заказ', 'заказа', 'заказов')} с ${esc(c.since)}</div>
                        <div class="skpi-map-row__meta">Не отмечен такси-службой — доля такси его не учитывает</div></div>
                    <div class="skpi-map-row__act">
                        <button class="skpi-btn-light" style="background:#411330" data-taxi="${c.courier_id}">
                            Отметить такси-службой</button>
                    </div>
                </div>`).join('') : ''}
            </div>`;

        const overlay = modal('Сопоставление источников', body,
            '<button class="skpi-btn-light" style="background:#fff;color:#411330;border:1px solid #eee2ea" data-close>Закрыть</button>');

        overlay.addEventListener('click', async e => {
            const bind = e.target.closest('[data-bind]');
            if (bind) {
                const row = bind.closest('.skpi-map-row');
                const select = row.querySelector('[data-store]');
                if (!select.value) {
                    window.BarhatUI.toast('Выберите салон', 'error');
                    return;
                }
                bind.disabled = true;
                try {
                    await api('/api/salon-kpi/links', postOptions({
                        source: bind.dataset.source,
                        external_key: bind.dataset.key,
                        store_id: Number(select.value)
                    }));
                    row.remove();
                    window.BarhatUI.toast('Связь сохранена', 'success');
                    state.data = null;
                    load();
                } catch (error) {
                    window.BarhatUI.toast('Не удалось привязать: ' + error.message, 'error');
                    bind.disabled = false;
                }
                return;
            }

            const taxi = e.target.closest('[data-taxi]');
            if (taxi) {
                taxi.disabled = true;
                try {
                    await api(`/api/couriers/${taxi.dataset.taxi}/taxi-flag`,
                        postOptions({ is_external_taxi: true }));
                    taxi.closest('.skpi-map-row').remove();
                    window.BarhatUI.toast('Курьер отмечен такси-службой', 'success');
                    state.data = null;
                    load();
                } catch (error) {
                    window.BarhatUI.toast('Не удалось отметить: ' + error.message, 'error');
                    taxi.disabled = false;
                }
            }
        });
    }

    // ================= События =================

    async function openDetails(key, row) {
        if (state.scope === 'city' || state.details[key]) {
            render();
            return;
        }
        try {
            const result = await api(
                `/api/salon-kpi/salon/${row.store_id}?month=${encodeURIComponent(state.month)}`);
            state.details[key] = result.data;
        } catch (error) {
            state.details[key] = { florists: [] };
            console.warn('Детализация салона недоступна:', error);
        }
        render();
    }

    document.addEventListener('click', function (e) {
        const page = e.target.closest('.page[data-page="salon_kpi"]');
        if (!page) return;

        const action = e.target.closest('[data-action]');
        if (action) {
            if (action.dataset.action === 'plans') openPlans();
            if (action.dataset.action === 'mapping' || action.dataset.action === 'couriers') openMapping();
            return;
        }

        const how = e.target.closest('[data-how]');
        if (how) {
            const box = document.getElementById('skpiHow-' + how.dataset.how);
            if (box) box.classList.toggle('is-open');
            return;
        }

        const scope = e.target.closest('[data-scope]');
        if (scope) {
            state.scope = scope.dataset.scope;
            state.openRow = null;
            state.data = null;
            load();
            return;
        }

        const sort = e.target.closest('[data-sort]');
        if (sort) {
            state.sort = sort.dataset.sort;
            render();
            return;
        }

        const rowButton = e.target.closest('[data-row]');
        if (rowButton) {
            const key = rowButton.dataset.row;
            if (state.openRow === key) {
                state.openRow = null;
                render();
            } else {
                state.openRow = key;
                const row = (state.data.rows || []).find(r =>
                    (state.scope === 'city' ? `city:${r.city}` : `salon:${r.store_id}`) === key);
                if (row) openDetails(key, row); else render();
            }
        }
    });

    document.addEventListener('change', function (e) {
        if (e.target.id === 'skpiMonth') {
            state.month = e.target.value;
            state.openRow = null;
            state.details = {};
            state.data = null;
            load();
        }
    });

    window.SalonKpiModule = {
        onPageActivated: function (user) {
            state.isAdmin = user && user.role === 'admin';
            if (!state.data) load();
        }
    };
})();
