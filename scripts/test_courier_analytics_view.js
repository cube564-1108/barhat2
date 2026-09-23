/**
 * Сторож вкладки «Аналитика» в разделе «Контроль доставки».
 *
 * Проверяется ПОВЕДЕНИЕ настоящих функций из courier-dispatch.js — их исходник
 * вырезается из файла и исполняется в песочнице (так же устроен
 * test_courier_dispatch_filter.js). «Есть ли слово в файле» тут бессильно:
 * такая проверка закрепила бы ошибку, а не поймала её.
 *
 * Что здесь ловится:
 *
 * 1. **Непосчитанное выглядит нулём.** «—» и «0» читаются человеком
 *    по-разному: доля без единой доставки — это «нечего считать», а не «ноль
 *    процентов вовремя».
 * 2. **Плитка без базы.** Процент без знаменателя не проверяется: при трёх
 *    доставках 66,7% не значит ничего, и база обязана стоять под числом.
 * 3. **Строка сходимости молчит про потери.** «Броней 247, доставок 198»
 *    рождает вопрос «где остальные 49» — отвечать на него надо на экране.
 * 4. **Салон без часового пояса пропадает молча** — вместе с ним пропадает и
 *    причина, по которой доля посчитана не по всем доставкам.
 * 5. **Фильтр применён, а числа старые.** Выбор салонов уходит на сервер по
 *    кнопке, и до нажатия человек обязан видеть, что смотрит на прежнее.
 * 6. **«Мало данных» не помечено.** «50,0 %» у курьера с двумя доставками
 *    читается так же уверенно, как «86,4 %» у курьера с двумя сотнями.
 * 7. **Выгрузка без периода и салонов.** Через неделю такой файл бессмыслен:
 *    числа есть, а за что они — неизвестно.
 * 8. **Разметка и CSS разъехались** — класс, которого нет в стилях, ломает
 *    экран молча.
 *
 * Запуск: node scripts/test_courier_analytics_view.js
 */

const fs = require('fs');
const path = require('path');
const vm = require('vm');

// Путь можно подменить — так проверяется сам сторож: прогон по версии до
// правки обязан падать.
const ROOT = path.join(__dirname, '..', 'src', 'dashboard');
const SRC = process.env.COURIER_DISPATCH_JS || path.join(ROOT, 'courier-dispatch.js');
const CSS = process.env.COURIER_DISPATCH_CSS || path.join(ROOT, 'courier-dispatch.css');

const source = fs.readFileSync(SRC, 'utf8');
const css = fs.readFileSync(CSS, 'utf8');

const failures = [];

function check(name, condition, detail = '') {
    if (condition) {
        console.log(`  [ok] ${name}`);
    } else {
        console.log(`  [FAIL] ${name} ${detail}`);
        failures.push(name);
    }
}

function cut(name) {
    const start = source.indexOf(`function ${name}(`);
    if (start === -1) throw new Error(`в courier-dispatch.js нет функции ${name}`);
    let depth = 0;
    let seen = false;
    for (let i = start; i < source.length; i++) {
        if (source[i] === '{') { depth++; seen = true; }
        else if (source[i] === '}') {
            depth--;
            if (seen && depth === 0) return source.slice(start, i + 1);
        }
    }
    throw new Error(`не удалось вырезать ${name}`);
}

function cutVar(name) {
    const marker = `var ${name} = `;
    const start = source.indexOf(marker);
    if (start === -1) throw new Error(`в courier-dispatch.js не найден ${name}`);
    const valueAt = start + marker.length;
    const open = source[valueAt];
    if (open !== '{' && open !== '[') {
        const end = source.indexOf(';', valueAt);
        return source.slice(start, end + 1);
    }
    const close = open === '{' ? '}' : ']';
    let depth = 0;
    for (let i = valueAt; i < source.length; i++) {
        if (source[i] === open) depth++;
        else if (source[i] === close) {
            depth--;
            if (depth === 0) return source.slice(start, i + 2);
        }
    }
    throw new Error(`не удалось вырезать ${name}`);
}

const state = {
    loading: false,
    analytics: null,
    anaSites: [
        { code: 'ekb', name: 'ЕКБ Бажова 89', city: 'Екатеринбург', utc_offset: 5 },
        { code: 'nsk', name: 'НСК Восход 3', city: 'Новосибирск', utc_offset: 7 },
    ],
    anaPicked: [],
    anaApplied: { period: { from: '2026-09-01', to: '2026-09-23' }, sites: [] },
    anaPeriod: { from: '2026-09-01', to: '2026-09-23' },
    anaSitesOpen: false,
    anaOpen: { late: false, outsourced: false, never: false },
    maxDays: 92,
};

const captured = { alerts: [], download: null, text: null };

function Blob(parts) { this.parts = parts; }

const sandbox = {
    console: { log() {}, warn() {}, error() {} },
    Date, Math, Number, String, Object, Array, JSON, RegExp, isNaN, Blob,
    state,
    URL: {
        createObjectURL(blob) { captured.text = blob.parts.join(''); return 'blob:test'; },
        revokeObjectURL() {},
    },
    document: {
        createElement() {
            return { href: '', download: '',
                     click() { captured.download = this.download; } };
        },
        getElementById() { return null; },
        querySelector() { return null; },
    },
};
sandbox.window = sandbox;
sandbox.window.BarhatUI = { alert(text) { captured.alerts.push(text); } };
vm.createContext(sandbox);

vm.runInContext(cutVar('MAX_ROWS'), sandbox);
vm.runInContext(cutVar('ANALYTICS_DEFAULT_DAYS'), sandbox);
for (const name of ['esc', 'num', 'capped', 'cutNotice', 'filterField', 'csvCell',
                    'defaultAnalyticsPeriod', 'analyticsQuery', 'analyticsDirty',
                    'siteName', 'anaSitesHtml', 'anaFiltersHtml', 'tile',
                    'anaTilesHtml', 'anaReconcileHtml', 'anaCouriersHtml',
                    'anaBlock', 'anaLateTable', 'anaOutsourcedTable',
                    'anaNeverTable', 'exportAnalytics', 'analyticsHtml']) {
    vm.runInContext(cut(name), sandbox);
}

function totals(extra = {}) {
    return Object.assign({
        claims: 247, delivered: 198,
        released_by_hand: 31, released_self: 24, released_admin: 7,
        released_expired: 9, order_gone: 0, active: 2, problem: 0,
        on_time: 171, late: 27, on_time_share: 86.4, late_minutes_avg: 14.0,
        outsourced_after_claim: 18, outsourced_amount: 5400,
        outsourced_never_claimed: 34,
        minutes_to_pickup_median: 12,
        not_counted: { no_interval: 12, no_timezone: 0, sites_without_timezone: [] },
    }, extra);
}

function courier(extra = {}) {
    return Object.assign({
        courier_user_id: 11, courier_name: 'Иван Петров',
        claims: 68, released_by_hand: 4, delivered: 61,
        on_time: 56, late: 5, on_time_share: 91.8, late_minutes_avg: 9,
        outsourced: 3, counted: 61, low_data: false,
    }, extra);
}

function setData(extra = {}) {
    state.analytics = Object.assign({
        period: { from: '2026-09-01', to: '2026-09-23' },
        site_codes: [],
        totals: totals(),
        couriers: [courier()],
        late_orders: [],
        outsourced_after_claim: [],
        outsourced_never_claimed: [],
        not_measured: ['время от появления заказа до брони',
                       'фактическое вручение (считаем по отметке курьера «Доставил»)'],
        timings_ms: { total: 12 },
    }, extra);
}

// ============================================================================
console.log('\n1. Непосчитанное показано прочерком, а не нулём');
// ============================================================================

setData({ totals: totals({ on_time: 0, late: 0, on_time_share: null,
                           late_minutes_avg: null, delivered: 0 }) });
let html = sandbox.anaTilesHtml(state.analytics.totals);
check('доля без посчитанных доставок — «—», а не «0 %»',
      html.includes('—') && !/>0 %</.test(html), '(в плитке появился ноль)');
check('под ней сказано, что считать нечего', html.includes('нечего считать'));
check('пустое опоздание названо «опозданий нет»', html.includes('опозданий нет'));

// ============================================================================
console.log('\n2. У процента есть база, у потерь — доля');
// ============================================================================

setData();
html = sandbox.anaTilesHtml(state.analytics.totals);
check('под долей вовремя стоит её знаменатель', html.includes('171 из 198'), html);
check('под доставками — из скольких броней', html.includes('из 247 броней'));
check('среднее опоздание названо числом опоздавших', html.includes('среднее по 27'));
check('«сняли руками» расшифровано по инициатору',
      html.includes('курьер 24') && html.includes('управляющий 7'));
check('аутсорс показан долей от броней', html.includes('7 % от броней'), html);

// ============================================================================
console.log('\n3. Строка сходимости отвечает, куда делись брони');
// ============================================================================

let line = sandbox.anaReconcileHtml(totals());
check('названо число броней', line.includes('Из 247 броней'));
for (const part of ['доставлено 198', 'снято руками 31', 'аутсорс 18',
                    'сгорело по таймеру 9', 'в работе 2']) {
    check(`в строке есть «${part}»`, line.includes(part), line);
}
check('непосчитанные по интервалу названы',
      line.includes('без интервала доставки — 12'), line);

// Салон без пояса — это не «вовремя», и человек обязан узнать, какой именно.
line = sandbox.anaReconcileHtml(totals({
    not_counted: { no_interval: 0, no_timezone: 3,
                   sites_without_timezone: ['Новый салон'] },
}));
check('салон без часового пояса назван по имени',
      line.includes('Новый салон') && line.includes('без часового пояса салона — 3'),
      line);
check('и сказано, где это чинить', line.includes('Настройках городов'));

// ============================================================================
console.log('\n4. Фильтр салонов: выбор, чипсы, «ещё не применено»');
// ============================================================================

state.anaPicked = [];
check('без выбора кнопка говорит «все»', sandbox.anaSitesHtml().includes('Салоны: все'));
check('пока ничего не меняли, напоминания нет',
      !sandbox.anaFiltersHtml().includes('нажмите «Показать»'));

state.anaPicked = ['nsk'];
const filters = sandbox.anaFiltersHtml();
check('выбранный салон показан чипсом с именем',
      filters.includes('НСК Восход 3'), filters);
check('счётчик выбранных на кнопке', filters.includes('Салоны: выбрано 1'));
check('видно, что фильтр ещё не применён',
      filters.includes('Фильтр изменён'), '(человек читает старые числа как новые)');
check('запрос уносит выбранные салоны',
      sandbox.analyticsQuery().includes('sites=nsk'), sandbox.analyticsQuery());

state.anaSitesOpen = true;
const list = sandbox.anaSitesHtml();
check('в списке есть все салоны справочника',
      list.includes('ЕКБ Бажова 89') && list.includes('НСК Восход 3'));
check('отмеченный салон отмечен галочкой',
      /data-cdisp-site="nsk"[^>]*checked/.test(list), list);
state.anaSitesOpen = false;

// Применённым считается то, что посчитал сервер: иначе подпись врёт.
state.anaApplied = { period: { from: '2026-09-01', to: '2026-09-23' }, sites: ['nsk'] };
check('после применения напоминание гаснет',
      !sandbox.anaFiltersHtml().includes('Фильтр изменён'));
state.anaPicked = [];
state.anaApplied = { period: { from: '2026-09-01', to: '2026-09-23' }, sites: [] };

// ============================================================================
console.log('\n5. «Мало данных» у курьера с парой доставок');
// ============================================================================

setData({ couriers: [
    courier(),
    courier({ courier_user_id: 12, courier_name: 'Анна', delivered: 2,
              on_time: 1, late: 1, on_time_share: 50.0, counted: 2, low_data: true }),
] });
const table = sandbox.anaCouriersHtml(state.analytics.couriers, state.analytics.totals);
check('процент при малом числе помечен', table.includes('мало данных'), table);
check('но сам процент показан, а не спрятан', table.includes('50'), table);
check('у курьера с сотней доставок пометки нет',
      table.indexOf('91,8') > -1 || table.indexOf('91.8') > -1);
check('в таблице есть итоговая строка', table.includes('Итого'));
check('итог берётся из totals, а не пересчитывается на клиенте',
      table.includes('247') && table.includes('198'));

// ============================================================================
console.log('\n6. Блоки детализации: закрыты, открываются, помнят состояние');
// ============================================================================

const late = [{ retailcrm_order_id: 154764, order_number: '154764',
                courier_name: 'Анна', site_name: 'ЕКБ Бажова 89',
                delivery_date: '2026-09-23', time_from: '12:00', time_to: '14:00',
                delivered_local: '15:12', late_minutes: 72 }];
setData({ late_orders: late, totals: totals() });

state.anaOpen.late = false;
let view = sandbox.analyticsHtml();
check('закрытый блок показывает число, но не строки',
      view.includes('Доставлены с опозданием — 1') && !view.includes('15:12'), '');

state.anaOpen.late = true;
view = sandbox.analyticsHtml();
check('открытый блок показывает номер заказа и курьера',
      view.includes('154764') && view.includes('Анна'));
check('план и факт стоят рядом в одной шкале',
      view.includes('12:00–14:00') && view.includes('15:12'), '');
check('опоздание подписано знаком', view.includes('+72 мин'));

check('сказано, что «доставлено» — это отметка курьера',
      view.includes('отметка курьера'), '');

// Длинные списки сервер обрезает, а счётчик отдаёт полный: заголовок обязан
// показывать, сколько таких заказов ВСЕГО. Иначе «Ушли службе — 500» при
// тысяче — враньё ровно в той цифре, ради которой блок открывают.
setData({
    totals: totals(),
    late_orders: late,
    detail_totals: { late_orders: 900, outsourced_after_claim: 0,
                     outsourced_never_claimed: 0 },
    detail_truncated: { late_orders: true, outsourced_after_claim: false,
                        outsourced_never_claimed: false },
});
state.anaOpen.late = true;
view = sandbox.analyticsHtml();
check('заголовок показывает полное число, а не длину среза',
      view.includes('Доставлены с опозданием — 900'), '');
check('сказано, сколько строк показано из скольких',
      view.includes('из 900') && view.includes('Excel'), '');
state.anaOpen.late = false;

// Раскрытие блока «не взяты никем» объясняет, что это другая беда: курьер на
// эти заказы не нашёлся вовсе, и чинится это не разговором с курьерами.
setData({
    totals: totals(),
    outsourced_never_claimed: [{ retailcrm_order_id: 154690, order_number: '154690',
                                 site_name: 'ЕКБ Ленина', delivery_date: '2026-09-22',
                                 delivery_time_from: '10:00', delivery_time_to: '12:00' }],
});
state.anaOpen.never = true;
view = sandbox.analyticsHtml();
check('у блока «не взяты никем» объяснено, что это другая беда',
      view.includes('число людей и условия'), '');
check('в нём есть номер заказа и салон, но не курьер — его не было',
      view.includes('154690') && view.includes('ЕКБ Ленина'));
state.anaOpen.never = false;

// ============================================================================
console.log('\n7. Выгрузка помнит, за что эти числа');
// ============================================================================

captured.text = null;
captured.download = null;
state.anaApplied = { period: { from: '2026-09-01', to: '2026-09-23' }, sites: ['nsk'] };
setData({ totals: totals(), couriers: [courier()], late_orders: late });
sandbox.exportAnalytics();
check('файл назван периодом',
      (captured.download || '').includes('2026-09-01_2026-09-23'), captured.download);
check('внутри записан период', (captured.text || '').includes('2026-09-01 — 2026-09-23'));
check('и выбранные салоны', (captured.text || '').includes('НСК Восход 3'), '');
check('свод по курьерам попал в файл', (captured.text || '').includes('Иван Петров'));
check('список опозданий попал туда же',
      (captured.text || '').includes('Доставлены с опозданием')
      && (captured.text || '').includes('154764'));
check('BOM на месте — Excel не покажет кракозябры',
      (captured.text || '').charCodeAt(0) === 0xFEFF);
check('разделитель — точка с запятой, перевод строки CRLF',
      (captured.text || '').includes(';') && (captured.text || '').includes('\r\n'));

state.analytics = null;
captured.alerts.length = 0;
sandbox.exportAnalytics();
check('без данных выгрузка объясняет отказ, а не молчит',
      captured.alerts.length === 1, `(${captured.alerts.length})`);

// ============================================================================
console.log('\n8. Разметка и стили не разъехались');
// ============================================================================

const used = new Set();
for (const chunk of source.matchAll(/class="([^"]+)"/g)) {
    for (const name of chunk[1].split(/\s+/)) {
        if (name.startsWith('cdisp-')) used.add(name);
    }
}
const missing = [...used].filter((name) => !css.includes('.' + name));
check('все классы раздела описаны в CSS', missing.length === 0, `(${missing})`);

check('строка блока — полноценная цель нажатия на телефоне',
      /\.cdisp-block__head\s*\{[^}]*min-height:\s*48px/.test(css));
check('строка салона в списке — тоже',
      /\.cdisp-ms__row\s*\{[^}]*min-height:\s*38px/.test(css));
check('своих цветов не завели: только токены --bx-*',
      !/\.cdisp-(tile__caption|reconcile|chip|block)[^{]*\{[^}]*#[0-9a-fA-F]{3,6}/.test(css));

// ============================================================================
console.log('\n9. Старая сводка убрана целиком, а не спрятана');
// ============================================================================
//
// Два места с одними числами разъезжаются на первой правке формулы. Если
// функция вернётся — сторож обязан сказать об этом раньше, чем разойдутся
// цифры.

check('функции metricsHtml больше нет', !source.includes('function metricsHtml'));
check('вкладка «Доставка» её не зовёт', !source.includes('+ metricsHtml()'));
check('второй запрос за показателями со вкладки «Доставка» убран',
      !source.includes("get('/api/courier/metrics')"), '');
check('вкладка «Аналитика» заведена в списке разделов',
      /id:\s*'analytics'/.test(source));

console.log('');
if (failures.length) {
    console.log(`=== ПРОВАЛЕНО: ${failures.length} ===`);
    failures.forEach((name) => console.log(`  - ${name}`));
    process.exit(1);
}
console.log('=== Вкладка «Аналитика» собрана верно ===');
