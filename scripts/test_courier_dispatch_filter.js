/**
 * Сторож фильтра и выгрузки таблицы «Заказы» в разделе «Контроль доставки».
 *
 * Проверяется ПОВЕДЕНИЕ настоящих функций из courier-dispatch.js — их исходник
 * вырезается из файла и исполняется в песочнице (так же устроены
 * test_courier_mine_sections.js и test_courier_earnings_view.js). Проверка
 * «есть ли слово в файле» тут бессильна: она закрепила бы ошибку, а не поймала
 * её (правило CLAUDE.md про сторожа).
 *
 * Что здесь ловится:
 *
 * 1. **Фильтр и столбец считают одно и то же.** «Передан службе» живёт только
 *    на экране: в поле `state` у такого заказа `free`. Фильтр по сырому полю
 *    не нашёл бы его никогда, а фильтр «Свободен» показал бы заказ, который
 *    давно везёт агрегатор.
 * 2. **Заказ без окна доставки не проваливается молча** в ограничение по
 *    времени — он выбывает явно, и об этом сказано подписью.
 * 3. **Выбранное значение не исчезает вместе с данными.** Салон пропал из
 *    выборки — фильтр по нему остался применённым, и список обязан его
 *    показать, а не подставить «Все салоны» под невидимым условием.
 * 4. **Выгрузка отдаёт ровно то, что на экране** — с применённым фильтром.
 * 5. **CSV открывается в Excel без порчи:** BOM, `;`, CRLF, экранирование
 *    кавычек, апостроф перед формулой (имя курьера правят в CRM).
 * 6. **Дата в файле есть, хотя в таблице её нет:** период охватывает и
 *    завтра, и строка выгрузки без даты не значит ничего.
 * 7. **Фильтр не перерисовывает экран целиком** — иначе набор в строке
 *    «Заказ» обрывался бы на первом символе (правило CLAUDE.md про innerHTML).
 *
 * Запуск: node scripts/test_courier_dispatch_filter.js
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

/** Вырезать объявление функции по имени вместе с телом (по балансу скобок). */
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

/** Вырезать объявление переменной: объект, массив или простое значение. */
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

// Состояние модуля песочницы: функции отбора читают его напрямую — подменять
// их своими копиями значило бы проверять не тот код.
const state = {
    overview: null,
    loading: false,
    filters: { order: '', site: '', time_from: '', time_to: '',
               state: '', ready: '', courier: '' },
    period: { from: '', to: '' },
    shownPeriod: { from: '', to: '' },
    maxDays: null,
};

// Что показал BarhatUI.alert и что ушло в скачанный файл.
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
    },
};
sandbox.window = sandbox;
sandbox.window.BarhatUI = { alert(text) { captured.alerts.push(text); } };
vm.createContext(sandbox);

for (const name of ['STATE_TITLES', 'READY_TITLES', 'EMPTY_FILTERS', 'NO_COURIER',
                    'MAX_ROWS']) {
    vm.runInContext(cutVar(name), sandbox);
}
for (const name of ['esc', 'orderLabel', 'dateLabel', 'siteLabel', 'slotLabel',
                    'stateKey', 'stateLabel', 'readyKey', 'courierLabel', 'orderTable',
                    'hhmm', 'allOrders', 'hasFilters', 'filteredOrders',
                    'columnValues', 'filterSelect', 'filterField',
                    'periodQuery', 'periodHtml',
                    'ordersFilterHtml', 'ordersCountText', 'ordersBodyHtml',
                    'ordersSectionHtml', 'csvCell', 'exportOrders']) {
    vm.runInContext(cut(name), sandbox);
}

let nextId = 154500;

function ord(extra = {}) {
    const id = extra.retailcrm_order_id || nextId++;
    return Object.assign({
        retailcrm_order_id: id,
        order_number: 'A' + id,
        site_name: 'Свердловский',
        city: 'Екатеринбург',
        delivery_date: '2026-09-23',
        delivery_time_from: '14:00',
        delivery_time_to: '16:00',
        state: 'free',
        is_ready: false,
        outsourced: false,
        courier_name: '',
        crm_courier_name: '',
        stuck_claim: false,
    }, extra);
}

function setOrders(list) {
    state.overview = { orders: list, totals: {}, unclaimed: [], stuck: [] };
}

function resetFilters() {
    state.filters = Object.assign({}, sandbox.EMPTY_FILTERS);
}

function ids() {
    return sandbox.filteredOrders().map((o) => o.retailcrm_order_id).join(',');
}

/** Прогнать exportOrders и вернуть строки скачанного файла. */
function exported() {
    captured.text = null;
    captured.download = null;
    captured.alerts = [];
    sandbox.exportOrders();
    // BOM — часть файла, а не часть первой ячейки: для разбора его снимаем,
    // а на месте он проверяется отдельно (раздел 7).
    return captured.text === null
        ? null : captured.text.replace(/^﻿/, '').split('\r\n');
}


console.log('\n1. Фильтр по каждому полю таблицы');

setOrders([
    ord({ retailcrm_order_id: 1, order_number: '154553', site_name: 'Свердловский',
          delivery_time_from: '10:00', state: 'free' }),
    ord({ retailcrm_order_id: 2, order_number: '154554', site_name: 'Центральный',
          delivery_time_from: '14:00', state: 'claimed', courier_name: 'Иванов',
          is_ready: true }),
    ord({ retailcrm_order_id: 3, order_number: '160001', site_name: 'Центральный',
          delivery_time_from: '18:00', state: 'delivered', courier_name: 'Петров' }),
]);
resetFilters();

check('без фильтра видно всё', ids() === '1,2,3', ids());

state.filters.order = '1545';
check('поиск по номеру заказа ищет подстрокой', ids() === '1,2', ids());
resetFilters();

state.filters.site = 'Центральный';
check('фильтр по салону', ids() === '2,3', ids());
resetFilters();

state.filters.state = 'claimed';
check('фильтр по состоянию', ids() === '2', ids());
resetFilters();

state.filters.ready = 'ready';
check('фильтр по сборке «Готов»', ids() === '2', ids());
state.filters.ready = 'making';
check('фильтр по сборке «Собирают»', ids() === '1,3', ids());
resetFilters();

state.filters.courier = 'Иванов';
check('фильтр по курьеру', ids() === '2', ids());
state.filters.courier = sandbox.NO_COURIER;
check('«Без курьера» — тоже значение столбца, и выбрать его можно',
      ids() === '1', ids());
resetFilters();

state.filters.time_from = '12:00';
check('окно «с» отсекает раннее', ids() === '2,3', ids());
state.filters.time_to = '16:00';
check('окно «с» и «по» вместе', ids() === '2', ids());
resetFilters();

state.filters.time_from = '14:00';
state.filters.time_to = '14:00';
check('границы окна включаются', ids() === '2', ids());
resetFilters();

state.filters.site = 'Центральный';
state.filters.ready = 'making';
check('условия складываются, а не заменяют друг друга', ids() === '3', ids());
resetFilters();


console.log('\n2. Состояние: фильтр и столбец считают одно и то же');

// Заказ отдали службе доставки. В поле state у него по-прежнему free — и
// именно на этом расхождении фильтр по сырому полю врал бы дважды: терял
// заказ в «Передан службе» и подсовывал его в «Свободен».
setOrders([
    ord({ retailcrm_order_id: 10, state: 'free' }),
    ord({ retailcrm_order_id: 11, state: 'free', outsourced: true,
          crm_courier_name: 'Яндекс.Доставка' }),
]);
resetFilters();

state.filters.state = 'outsourced';
check('«Передан службе» фильтруется', ids() === '11', ids());
state.filters.state = 'free';
check('и в «Свободен» он не попадает', ids() === '10', ids());
resetFilters();

const bothStates = sandbox.orderTable(state.overview.orders);
check('в столбце написано ровно то же', bothStates.includes('Передан службе'));
check('у переданного службе в столбце «Курьер» стоит агрегатор',
      bothStates.includes('Яндекс.Доставка'),
      '(иначе строка выглядит бесхозной)');

state.filters.courier = 'Яндекс.Доставка';
check('и по нему же он ищется в фильтре курьера', ids() === '11', ids());
resetFilters();

const stateOptions = sandbox.ordersFilterHtml();
check('в списке состояний только те, что есть на экране',
      stateOptions.includes('value="outsourced"') && stateOptions.includes('value="free"')
      && !stateOptions.includes('value="picked_up"'),
      '(пункт, который ничего не найдёт, — это обещание пустого экрана)');


console.log('\n3. Заказ без окна доставки');

setOrders([
    ord({ retailcrm_order_id: 20, delivery_time_from: '10:00' }),
    ord({ retailcrm_order_id: 21, delivery_time_from: null, delivery_time_to: null }),
]);
resetFilters();

check('без ограничения по времени он виден', ids() === '20,21', ids());
check('и в таблице объясняет себя',
      sandbox.orderTable(state.overview.orders).includes('время уточняется'));

state.filters.time_from = '09:00';
check('под ограничение по времени он не попадает — у него времени нет',
      ids() === '20', ids());
resetFilters();

check('и об этом сказано на экране, а не только здесь',
      sandbox.ordersSectionHtml().includes('без заведённого окна доставки'),
      '(иначе заказ пропадает молча)');


console.log('\n4. Выбранное значение не теряется вместе с данными');

setOrders([ord({ retailcrm_order_id: 30, site_name: 'Свердловский' })]);
resetFilters();
state.filters.site = 'Центральный';   // салона в выборке больше нет
const html = sandbox.ordersFilterHtml();
check('исчезнувший салон остаётся в списке выбранным',
      html.includes('value="Центральный"') && /Центральный[^<]*нет в выборке/.test(html),
      '(иначе браузер покажет «Все салоны», а фильтр останется применённым)');
check('и список пуст честно', ids() === '', ids());
check('пустой результат объясняется не как «заказов нет»',
      sandbox.ordersBodyHtml().includes('Под фильтр не попал'),
      '(«Заказов нет» тут читается как поломка синхронизации)');

setOrders([]);
resetFilters();
check('а вот когда заказов правда нет — так и написано',
      sandbox.ordersBodyHtml().includes('За выбранный период заказов нет'),
      '(«заказов нет» без упоминания периода читается как поломка синка)');


console.log('\n5. Счётчик показанного');

setOrders([ord({ retailcrm_order_id: 40, site_name: 'А' }),
           ord({ retailcrm_order_id: 41, site_name: 'Б' })]);
resetFilters();
check('без фильтра счётчик называет общее число',
      sandbox.ordersCountText() === 'Заказов: 2', sandbox.ordersCountText());
state.filters.site = 'А';
check('с фильтром — показано из скольких',
      sandbox.ordersCountText() === 'Показано 1 из 2', sandbox.ordersCountText());
resetFilters();
state.filters.time_from = '';
check('пустые поля фильтром не считаются',
      sandbox.ordersCountText() === 'Заказов: 2', sandbox.ordersCountText());


console.log('\n6. Выгрузка отдаёт то, что на экране');

setOrders([
    ord({ retailcrm_order_id: 50, order_number: '154553', site_name: 'Свердловский',
          state: 'claimed', courier_name: 'Иванов', is_ready: true,
          delivery_date: '2026-09-23', delivery_time_from: '10:00',
          delivery_time_to: '12:00', stuck_claim: true }),
    ord({ retailcrm_order_id: 51, order_number: '154554', site_name: 'Центральный',
          state: 'free', delivery_date: '2026-09-24', delivery_time_from: '18:00' }),
]);
resetFilters();

let lines = exported();
check('строк столько же, сколько заказов, плюс шапка', lines.length === 3,
      `(строк ${lines && lines.length})`);
check('шапка перечисляет столбцы', lines[0].startsWith('Заказ;Дата доставки;Салон'),
      lines[0]);

state.filters.site = 'Центральный';
lines = exported();
check('под фильтром уезжает только отфильтрованное',
      lines.length === 2 && lines[1].includes('154554') && !lines[1].includes('154553'),
      JSON.stringify(lines));
resetFilters();

lines = exported();
check('дата доставки в файле есть, хотя в таблице её нет',
      lines[1].includes('2026-09-23') && lines[2].includes('2026-09-24'),
      '(период охватывает и завтра — без даты строка не значит ничего)');
check('город в файле есть', lines[1].includes('Екатеринбург'));
check('окно уходит целиком', lines[1].includes('10:00–12:00'), lines[1]);
check('состояние уходит словом, а не кодом', lines[1].includes('Забронирован'),
      lines[1]);
check('признак «не забран» вынесен отдельным столбцом',
      lines[0].includes('Не забран') && lines[1].split(';')[6] === 'да'
      && lines[2].split(';')[6] === '',
      JSON.stringify([lines[1].split(';')[6], lines[2].split(';')[6]]));

check('имя файла помнит период выгрузки',
      captured.download === 'контроль-доставки_заказы_2026-09-23_2026-09-24.csv',
      captured.download);

setOrders([ord({ retailcrm_order_id: 60, delivery_date: '2026-09-23' })]);
exported();
check('один день — одна дата в имени файла',
      captured.download === 'контроль-доставки_заказы_2026-09-23.csv',
      captured.download);


console.log('\n7. Файл открывается в Excel без порчи');

setOrders([
    ord({ retailcrm_order_id: 70, site_name: 'ООО "Ромашка"',
          courier_name: '=1+1', city: 'Тюмень; север' }),
]);
resetFilters();
lines = exported();

check('BOM на месте', captured.text.charCodeAt(0) === 0xFEFF,
      '(без него Excel открывает кириллицу кракозябрами)');
check('разделитель — точка с запятой', lines[0].includes(';') && !lines[0].includes(','),
      '(русская локаль Excel ждёт именно её)');
check('перевод строки — CRLF', captured.text.includes('\r\n'));
check('кавычки внутри значения удвоены',
      lines[1].includes('"ООО ""Ромашка"""'), lines[1]);
check('значение с разделителем взято в кавычки',
      lines[1].includes('"Тюмень; север"'), lines[1]);
check('формула обезврежена апострофом', lines[1].includes("'=1+1"),
      '(Excel исполнил бы ячейку, а имя курьера правят в CRM)');

check('csvCell не трогает обычное значение', sandbox.csvCell('Свердловский') === 'Свердловский');
check('csvCell переживает пустое значение', sandbox.csvCell(null) === ''
      && sandbox.csvCell(undefined) === '');


console.log('\n8. Выгружать нечего — так и сказано');

setOrders([]);
resetFilters();
check('пустой период: файл не скачивается', exported() === null);
check('и причина названа своя',
      captured.alerts.join('').includes('заказов за период нет'),
      captured.alerts.join('|'));

setOrders([ord({ retailcrm_order_id: 80, site_name: 'Свердловский' })]);
state.filters.site = 'Центральный';
check('пустой фильтр: файл не скачивается', exported() === null);
check('и причина другая — чинить надо разное',
      captured.alerts.join('').includes('под фильтр не попал'),
      captured.alerts.join('|'));
resetFilters();


console.log('\n9. Дата доставки в таблице');

setOrders([ord({ retailcrm_order_id: 90, delivery_date: '2026-01-05' })]);
resetFilters();
const withDate = sandbox.orderTable(state.overview.orders);
check('столбец «Дата» есть в шапке', withDate.includes('<th>Дата</th>'));
check('дата показана по-человечески', withDate.includes('05.01.2026'),
      '(ISO в сетке читают хуже)');
check('год в дате остался',
      sandbox.dateLabel({ delivery_date: '2026-01-05' }) === '05.01.2026',
      '(период до квартала может пересечь новый год)');
check('пустая дата не рисует мусор',
      sandbox.dateLabel({}) === '' && sandbox.dateLabel({ delivery_date: null }) === '');
check('неожиданный формат отдаётся как есть, а не портится',
      sandbox.dateLabel({ delivery_date: '05.01.2026' }) === '05.01.2026');
check('дату не гоняем через BarhatTime',
      !/BarhatTime/.test(cut('dateLabel')),
      '(это день по стенным часам салона, а не отметка времени в UTC: '
      + 'перевод в пояс устройства сдвинул бы утренний заказ на вчера)');

// Число столбцов в шапке и в строке обязано совпасть, иначе таблица едет
const head = (withDate.match(/<th[ >]/g) || []).length;
const cells = (withDate.split('<tbody>')[1].match(/<td[ >]/g) || []).length;
check('шапка и строка одной ширины', head === cells, `(${head} против ${cells})`);


console.log('\n10. Произвольный период');

state.period = { from: '', to: '' };
check('пустой период не уходит в запрос — умолчание знает сервер',
      sandbox.periodQuery() === '', sandbox.periodQuery());

state.period = { from: '2026-09-01', to: '2026-09-30' };
check('заданный период уходит обеими границами',
      sandbox.periodQuery() === '?date_from=2026-09-01&date_to=2026-09-30',
      sandbox.periodQuery());

state.period = { from: '2026-09-01', to: '' };
check('половина периода в запрос не уходит', sandbox.periodQuery() === '',
      '(сервер подставил бы умолчание по одной границе — это не то, что просили)');

state.period = { from: '2026-09-01', to: '2026-09-30' };
state.maxDays = 92;
const periodHtml = sandbox.periodHtml();
check('поля периода показывают выбранное',
      periodHtml.includes('value="2026-09-01"') && periodHtml.includes('value="2026-09-30"'));
check('предел назван до того, как в него упрутся',
      periodHtml.includes('не больше 92 дней'),
      '(отказ задним числом — худший способ узнать про ограничение)');
check('период применяется кнопкой, а не сам собой',
      periodHtml.includes('data-cdisp-period'),
      '(за ним идёт запрос к медленному /data)');
check('поля периода не помечены как мгновенный фильтр',
      !periodHtml.includes('data-cdisp-filter='),
      '(иначе обработчик мгновенных условий начнёт их применять без запроса)');

state.loading = true;
check('на время запроса кнопка гаснет',
      sandbox.periodHtml().includes('disabled'),
      '(медленный клик иначе превращается в три запроса)');
state.loading = false;
state.maxDays = null;
check('пока предел неизвестен, о нём молчим',
      !sandbox.periodHtml().includes('не больше'));
state.maxDays = 92;


console.log('\n11. Длинный период не вешает вкладку');

const many = [];
for (let i = 0; i < sandbox.MAX_ROWS + 25; i++) {
    many.push(ord({ retailcrm_order_id: 1000 + i }));
}
setOrders(many);
resetFilters();
const capped = sandbox.ordersBodyHtml();
const rowCount = (capped.split('<tbody>')[1].match(/<tr>/g) || []).length;
check('рисуется не больше предела строк', rowCount === sandbox.MAX_ROWS,
      `(нарисовано ${rowCount} из ${many.length})`);
check('отсечка называет оба числа',
      capped.includes(String(sandbox.MAX_ROWS)) && capped.includes(String(many.length)),
      '(показать часть молча — значит соврать про остальное)');
check('и зовёт за остальным в выгрузку, а не в сужение периода',
      /выгрузка в Excel заберёт все/.test(capped));

lines = exported();
check('выгрузка отсечкой не ограничена',
      lines.length === many.length + 1,
      `(в файле ${lines.length - 1} строк из ${many.length})`);

setOrders(many.slice(0, sandbox.MAX_ROWS));
check('ровно предел — отсечки нет',
      !sandbox.ordersBodyHtml().includes('Показаны первые'));


console.log('\n12. Разметка, стили и код не разъехались');

// Смотрим ТЕЛО обработчика, а не файл целиком: `render()` в файле есть и
// должен быть, и поиск по всему исходнику зеленел бы на любой версии.
const filterHandler = cut('onFilterEvent');
check('фильтр не перерисовывает экран целиком',
      filterHandler.includes('refreshOrders()') && !/\brender\(\)/.test(filterHandler)
      && cut('refreshOrders').includes("getElementById('cdispOrders')"),
      '(полный render() переписал бы и сами поля: набор оборвётся на первом символе)');
check('обработчик фильтра слушает и input, и change',
      source.includes("addEventListener('input', onFilterEvent)")
      && source.includes("addEventListener('change', onFilterEvent)"),
      '(input — для строки поиска, change — для select в старых браузерах)');
check('обработчик пишет только известные поля фильтра',
      source.includes('if (!(field in EMPTY_FILTERS)) return;'),
      '(иначе чужой data-атрибут заведёт в state поле, которого фильтр не знает)');
check('«Сбросить» перерисовывает экран целиком',
      /data-cdisp-filter-reset[\s\S]{0,400}?render\(\);/.test(source),
      '(поля обязаны опустеть, а точечное обновление таблицы их не трогает)');
check('кнопка выгрузки заведена и привязана',
      source.includes('data-cdisp-export') && source.includes('exportOrders();'));

// Период: ответы на длинный и короткий отрезок приходят не в том порядке,
// в каком их просили, и отказ по слишком длинному периоду не должен оставлять
// в полях то, чего на экране нет.
check('у загрузки есть номер запроса',
      source.includes('var token = ++loadToken;')
      && (source.match(/token !== loadToken/g) || []).length >= 3,
      '(иначе в таблице осядет ответ, которого уже никто не ждёт)');
check('отказ возвращает поля периода к показанному',
      source.includes('state.period = state.shownPeriod;'),
      '(иначе подпись врёт про то, что на экране)');
check('успех запоминает показанный период',
      source.includes('state.shownPeriod = state.period;'));
check('предел периода фронт у себя не хранит',
      !/MAX_OVERVIEW_DAYS|92/.test(cut('periodHtml')) && source.includes('meta.max_days'),
      '(одно и то же число в двух местах однажды разъедется)');
check('обновление не стирает уже показанное',
      /state\.loading && state\.tab === 'today' && state\.overview/.test(source)
      && source.includes('Обновляем…'),
      '(«медленно» превратилось бы в «пусто», а поля периода — не поправить)');
check('вкладка больше не называет период сегодняшним',
      !/title:\s*'Доставка сегодня'/.test(cutVar('TABS')),
      '(имя врало бы на каждом выборе прошлой недели)');

for (const cls of ['cdisp-filters', 'cdisp-filters--period', 'cdisp-filter',
                   'cdisp-filter__label', 'cdisp-filter__control',
                   'cdisp-orders-head']) {
    check(`класс .${cls} описан в courier-dispatch.css`, css.includes(`.${cls}`));
}
check('своих цветов не завели: только токены --bx-*',
      !/\.cdisp-filter[^{]*\{[^}]*#[0-9a-fA-F]{3,6}/.test(css),
      '(DESIGN-SPEC: палитра только из токенов)');
check('эмодзи в разделе нет',
      !/[\u{1F300}-\u{1FAFF}\u{2700}-\u{27BF}]/u.test(source),
      '(DESIGN-SPEC §9)');


console.log('');
if (failures.length) {
    console.log(`=== ПРОВАЛЕНО: ${failures.length} ===`);
    failures.forEach((name) => console.log(`  - ${name}`));
    process.exit(1);
}
console.log('=== Фильтр и выгрузка таблицы «Заказы» работают верно ===');
