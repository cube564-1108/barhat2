/**
 * Прогон панели фильтров раздела «Согласование счетов» в Node с заглушкой DOM.
 *
 * Сторож на ВЕСЬ путь, а не на компонент: обращение #18 про поля дат, но цена
 * ошибки — в запросе. Проверяется, что выбранный период доезжает до
 * /api/invoices теми же параметрами, что и раньше (created_from/created_to —
 * границами суток в UTC, due_from/due_to — календарными датами), что на период
 * уходит ОДИН запрос, а не по одному на каждый край, и что чип снимает период
 * целиком.
 *
 * Отдельно проверяется запасной вид: если date-range.js не доехал (старый кэш,
 * оборванная загрузка), фильтр по датам обязан работать нативными полями.
 *
 * Компонент сам по себе проверяет scripts/test_date_range.js.
 *
 * Запуск: node scripts/test_invoices_filters.js
 */

const fs = require('fs');
const vm = require('vm');
const path = require('path');
const { makeSandbox } = require('./lib/dom_stub.js');

const DASHBOARD = path.join(__dirname, '..', 'src', 'dashboard');
const DATE_RANGE = path.join(DASHBOARD, 'date-range.js');
const INVOICES = path.join(DASHBOARD, 'invoices-v2.js');

const failures = [];

function check(name, condition, detail) {
    const mark = condition ? 'OK  ' : 'FAIL';
    console.log(`  [${mark}] ${name}${detail ? ' — ' + detail : ''}`);
    if (!condition) failures.push(name);
}

/**
 * Песочница с модулем счетов.
 * @param {boolean} withDateRange подключать ли компонент периода
 */
async function bootInvoices(withDateRange) {
    const requests = [];
    const fetchImpl = async (url) => {
        requests.push(String(url));
        return {
            ok: true,
            status: 200,
            headers: { get: () => null },
            json: async () => ({
                invoices: [], total: 0, items: [],
                cities: [], payers: [], categories: [], stores: [], authors: [],
                'vat-options': [], work_cards: [],
                // Сводка приходит верхним уровнем — модуль кладёт весь ответ в state.summary
                wait: { count: 0, amount: 0 }, due_today: { count: 0, amount: 0 },
                overdue: { count: 0, amount: 0 }, clarification: { count: 0, amount: 0 },
            }),
        };
    };

    // Компонент периода грузится первым — как и в index.html, где его тег стоит
    // до модулей: фильтры зовут window.BarhatDateRange прямо при отрисовке
    const env = makeSandbox({ src: withDateRange ? DATE_RANGE : path.join(DASHBOARD, 'datetime.js'), fetch: fetchImpl });

    // Всё, чего модулю не хватает в заглушке: диалоги (нативные в Пульсе не
    // работают), адрес страницы и часы сотрудника
    env.sandbox.BarhatUI = {
        toast: () => {}, confirm: async () => true, alert: async () => {},
        prompt: async () => null, recentErrors: () => [],
    };
    env.sandbox.location = {
        pathname: '/invoices-v2', search: '', origin: 'https://x', href: 'https://x/invoices-v2',
    };
    env.sandbox.history = { pushState: () => {}, replaceState: () => {} };
    env.sandbox.addEventListener = () => {};
    env.sandbox.removeEventListener = () => {};
    env.sandbox.requestAnimationFrame = (fn) => setTimeout(fn, 0);
    env.sandbox.navigator = { userAgent: 'node' };
    env.sandbox.innerWidth = 1600;
    env.sandbox.BarhatTime = {
        parse: (v) => new Date(v),
        formatDate: () => '', formatDateTime: () => '', formatDateTimeLong: () => '',
        formatPlainDate: (v) => String(v || ''),
        zoneLabel: () => 'UTC+7',
        dayStartUtc: (d) => d + ' 00:00:00',
        dayEndUtc: (d) => d + ' 23:59:59',
        todayInputValue: () => '2026-09-23',
    };

    vm.runInContext(fs.readFileSync(INVOICES, 'utf8'), env.sandbox, { filename: 'invoices-v2.js' });
    await env.sandbox.InvoicesV2Module.onPageActivated();
    await new Promise(r => setTimeout(r, 20));

    const fire = (el, type, target) => el.fire(type, {
        target: target || el, preventDefault() {}, stopPropagation() {},
    });

    return {
        env, requests, fire,
        filters: () => env.byId['iv2Filters'],
        chips: () => env.byId['iv2Chips'],
        listRequests: () => requests.filter(url => url.includes('/api/invoices?')),
        lastList: () => {
            const list = requests.filter(url => url.includes('/api/invoices?'));
            // URLSearchParams кодирует пробел плюсом — возвращаем его на место,
            // иначе «created_from=2026-09-12 00:00:00» в проверке не найдётся
            return decodeURIComponent(String(list[list.length - 1] || '').replace(/\+/g, ' '));
        },
        // Компонент периода монтируется в узел по id — заглушка отдаёт его
        // из своего реестра, в браузере это узел внутри панели
        range: (key) => env.doc.getElementById('iv2dr-' + key),
        wait: () => new Promise(r => setTimeout(r, 10)),
    };
}

/** Выбрать период кликами по дням в поле периода. */
function pickRange(ctx, key, fromKey, toKey) {
    const mount = ctx.range(key);
    const button = mount.querySelector('[data-dr-btn]');
    if (!button) throw new Error(`поля периода «${key}» нет: фильтр собран не компонентом`);
    ctx.fire(button, 'click');
    const cals = mount.querySelector('[data-dr-cals]');
    const clickDay = (day) => {
        const cell = cals.querySelector(`[data-day="${day}"]`);
        if (!cell) throw new Error('в календаре нет дня ' + day);
        ctx.fire(cals, 'click', cell);
    };
    clickDay(fromKey);
    const afterFirst = ctx.listRequests().length;
    clickDay(toKey);
    return afterFirst;
}

(async () => {
    // --- 1. Панель фильтров --------------------------------------------------

    console.log('\n1. Поля периода вместо четырёх полей дат');
    const ctx = await bootInvoices(true);
    {
        const html = ctx.filters().innerHTML;
        check('поле «Заведён» создано',
            Boolean(ctx.range('created_range').querySelector('[data-dr-btn]')));
        check('поле «Оплата (план)» создано',
            Boolean(ctx.range('due_range').querySelector('[data-dr-btn]')));
        // Ровно то, на что жаловались: четыре нативные даты в сетке фильтров
        check('нативных полей даты в панели нет', !html.includes('type="date"'));
        check('подписи стали периодами', html.includes('>Заведён<') && html.includes('>Оплата (план)<'));
        check('старых подписей нет', !html.includes('Заведён с') && !html.includes('Оплата (план) по'));
    }

    // --- 2. Период = один запрос ---------------------------------------------

    console.log('\n2. Один период — один запрос к списку');
    {
        const before = ctx.listRequests().length;
        const afterFirst = pickRange(ctx, 'created_range', '2026-09-12', '2026-09-20');
        await ctx.wait();
        check('после первого клика запроса нет', afterFirst === before,
            `${afterFirst - before} лишний(х) запрос(а)`);
        check('замкнутый период — ровно один запрос', ctx.listRequests().length === before + 1,
            `${ctx.listRequests().length - before}`);
    }

    // --- 3. Параметры запроса -------------------------------------------------

    console.log('\n3. Параметры не изменились');
    {
        const url = ctx.lastList();
        // Дата заведения лежит в базе временем в UTC — границы суток по часам
        // сотрудника; иначе утренние счета выпадают, а хвост соседних суток лезет
        check('created_from — начало суток', url.includes('created_from=2026-09-12 00:00:00'), url);
        check('created_to — конец суток', url.includes('created_to=2026-09-20 23:59:59'));
    }

    console.log('\n4. Срок оплаты — календарная дата, без времени');
    {
        pickRange(ctx, 'due_range', '2026-09-15', '2026-09-15');
        await ctx.wait();
        const url = ctx.lastList();
        check('due_from без времени', url.includes('due_from=2026-09-15&') || url.includes('due_from=2026-09-15'), url);
        check('время к сроку оплаты не приписано', !url.includes('due_from=2026-09-15 '));
        check('один день — обе границы', url.includes('due_to=2026-09-15'));
    }

    // --- 5. Чип периода -------------------------------------------------------

    console.log('\n5. Чип снимает период целиком');
    {
        const chipButtons = ctx.chips().querySelectorAll('[data-chip-key="created_range"]');
        check('чип периода один', chipButtons.length === 1, String(chipButtons.length));
        check('в чипе читаемый период', ctx.chips().innerHTML.includes('12.09 – 20.09.2026'));

        ctx.fire(chipButtons[0], 'click');
        await ctx.wait();
        const url = ctx.lastList();
        check('дат в запросе больше нет', !url.includes('created_from'), url);
        check('срок оплаты не задело', url.includes('due_from=2026-09-15'));
        check('на кнопке снова «Все даты»',
            ctx.range('created_range').querySelector('[data-dr-label]').textContent === 'Все даты');
    }

    // --- 6. Сброс всех фильтров ----------------------------------------------

    console.log('\n6. «Сбросить всё» возвращает поля периода');
    {
        ctx.fire(ctx.env.byId['iv2FiltersReset'], 'click');
        await ctx.wait();
        check('срок оплаты сброшен',
            ctx.range('due_range').querySelector('[data-dr-label]').textContent === 'Все даты');
        check('в запросе дат нет', !ctx.lastList().includes('due_from'), ctx.lastList());
    }

    // --- 7. Запасной вид ------------------------------------------------------

    console.log('\n7. Без date-range.js фильтр всё равно работает');
    {
        const plain = await bootInvoices(false);
        check('компонента нет', !plain.env.sandbox.BarhatDateRange);
        const input = plain.env.byId['iv2f-created_from'];
        check('нативные поля дат на месте', Boolean(input));
        input.value = '2026-09-12';
        plain.fire(input, 'change');
        await plain.wait();
        check('дата доезжает до запроса',
            plain.lastList().includes('created_from=2026-09-12 00:00:00'), plain.lastList());
    }

    console.log('');
    if (failures.length) {
        console.log(`ПРОВАЛЕНО: ${failures.length}`);
        failures.forEach(name => console.log('  - ' + name));
        process.exit(1);
    }
    console.log('Все проверки пройдены');
})().catch(error => {
    console.log('ОШИБКА ПРОГОНА: ' + error.message);
    console.log(error.stack);
    process.exit(1);
});
