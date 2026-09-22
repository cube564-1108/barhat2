/**
 * Сторож вкладки «Мои» в приложении курьера: раскладка по секциям.
 *
 * Проверяется ПОВЕДЕНИЕ настоящих функций из courier-app.js — их исходник
 * вырезается из файла и исполняется в песочнице (так же устроены
 * test_courier_earnings_view.js и test_courier_timeout.js).
 *
 * Что здесь ловится:
 *
 * 1. **Состояние заказа определяет секцию, а не бейдж.** До 22.09.2026 «Мои»
 *    были плоским списком, и `claimed` от `picked_up` отличали два слова
 *    одного цвета.
 * 2. **Подъём готовых остался ТОЛЬКО в «Забрать в салоне».** Общая сортировка
 *    поднимала наверх то, что ещё лежит в салоне, и топила то, что уже в руках
 *    и горит по времени.
 * 3. **Счётчик таба считает работу, а не строки.** «Мои 7», где пять заказов
 *    доставлены, отвечает не на тот вопрос.
 * 4. **Незнакомое состояние не теряется** в свёрнутом блоке выполненных.
 * 5. **Раскрытие блока переживает перерисовку** — лента переписывается каждые
 *    30 секунд, и состояние в DOM стёрлось бы под рукой у читающего.
 * 6. **У доставленного заказа нет кнопки «Отказаться»** — живой брони там нет,
 *    и сервер ответил бы «Бронь уже снята».
 * 7. **Разметка и код не разъехались**: классы секций есть в CSS, у строки
 *    выполненных есть обработчик.
 *
 * Запуск: node scripts/test_courier_mine_sections.js
 */

const fs = require('fs');
const path = require('path');
const vm = require('vm');

// Путь можно подменить — так проверяется сам сторож: прогон по версии до
// правки обязан падать.
const ROOT = path.join(__dirname, '..', 'src', 'dashboard');
const SRC = process.env.COURIER_APP_JS || path.join(ROOT, 'courier-app.js');
const CSS = process.env.COURIER_APP_CSS || path.join(ROOT, 'courier-app.css');

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
    if (start === -1) throw new Error(`в courier-app.js нет функции ${name}`);
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

/** Вырезать объявление переменной-массива (MONTHS, MINE_GROUPS). */
function cutVar(name) {
    const found = source.match(new RegExp(`var ${name} = \\[[\\s\\S]*?\\];`));
    if (!found) throw new Error(`в courier-app.js не найден ${name}`);
    return found[0];
}

// Состояние модуля песочницы. Функции отбора читают его напрямую — подменять
// их своими копиями значило бы проверять не тот код.
const state = {
    orders: [], filter: 'mine', sites: [], city: 'Екатеринбург',
    loading: false, loadedAt: new Date(), mineDoneOpen: false,
};

const sandbox = {
    console: { log() {}, warn() {}, error() {} },
    Date, Math, Number, String, Object, Array, JSON, RegExp, isNaN,
    state,
};
sandbox.window = sandbox;
vm.createContext(sandbox);

vm.runInContext(cutVar('MONTHS'), sandbox);
vm.runInContext(cutVar('MINE_GROUPS'), sandbox);
for (const name of ['esc', 'salonNowMs', 'slotStartMs', 'countdown', 'shortDate',
                    'slotText', 'siteKey', 'availabilityBadge', 'readyBadge',
                    'changeNotice', 'mineGroupKey', 'isMineOpen', 'bySites',
                    'raiseReady', 'ordersByTab', 'visibleOrders', 'mineGroups',
                    'counts', 'siteOptions', 'cardHtml', 'mineDoneHtml',
                    'mineFeedHtml', 'emptyText']) {
    vm.runInContext(cut(name), sandbox);
}

let nextId = 154500;

/** Заказ курьера в заданном состоянии. */
function order(assignment_state, extra = {}) {
    const id = extra.retailcrm_order_id || nextId++;
    return Object.assign({
        retailcrm_order_id: id,
        order_number: String(id),
        assignment_state,
        is_mine: assignment_state !== null,
        is_free: assignment_state === null,
        is_ready: false,
        site_code: 'sverdlovsky',
        site_name: 'Свердловский',
        city: 'Екатеринбург',
        address_text: 'ул. Ленина, 12',
        delivery_date: '2026-09-22',
        delivery_time_from: '14:00',
        delivery_time_to: '16:00',
        changed_fields: [],
    }, extra);
}

function setOrders(list, sites = []) {
    state.orders = list;
    state.sites = sites;
}

function groupKeys() {
    return sandbox.mineGroups().map((g) => g.key);
}

function groupByKey(key) {
    return sandbox.mineGroups().filter((g) => g.key === key)[0];
}


console.log('\n1. Каждое состояние попадает в свою секцию');

setOrders([
    order('delivered'),
    order('claimed'),
    order('problem'),
    order('picked_up'),
]);

check('секции идут в порядке действий: везу → забрать → проблема → выполнено',
      JSON.stringify(groupKeys())
      === JSON.stringify(['picked_up', 'claimed', 'problem', 'delivered']),
      JSON.stringify(groupKeys()));

check('в каждой секции ровно свой заказ',
      sandbox.mineGroups().every((g) => g.orders.length === 1));

check('у секций есть человеческие названия',
      sandbox.mineGroups().map((g) => g.title).join('|')
      === 'Везу|Забрать в салоне|Требует внимания|Выполнено',
      sandbox.mineGroups().map((g) => g.title).join('|'));

setOrders([order('claimed'), order('claimed')]);
check('пустые секции не возвращаются вовсе',
      JSON.stringify(groupKeys()) === JSON.stringify(['claimed']),
      JSON.stringify(groupKeys()));

setOrders([
    order('claimed'),
    // Состояния, которого фронт не знает: сервер мог завести новое
    order('handed_to_partner'),
]);
check('незнакомое состояние идёт к активным, а не в «Выполнено»',
      groupByKey('claimed').orders.length === 2 && !groupByKey('delivered'),
      '(потерять заказ в свёрнутом блоке хуже, чем лишняя строка в «Забрать»)');

setOrders([order('claimed'), order(null, { is_mine: false, is_free: true })]);
check('чужой и свободный заказ в секции не попадают',
      sandbox.mineGroups().reduce((n, g) => n + g.orders.length, 0) === 1);


console.log('\n2. Порядок внутри секций');

// Сервер отдаёт заказы по времени доставки; внутри секции этот порядок — это
// порядок объезда.
setOrders([
    order('picked_up', { retailcrm_order_id: 1, is_ready: false }),
    order('picked_up', { retailcrm_order_id: 2, is_ready: true }),
    order('claimed',   { retailcrm_order_id: 3, is_ready: false }),
    order('claimed',   { retailcrm_order_id: 4, is_ready: true }),
]);

check('в «Забрать в салоне» готовые подняты наверх',
      groupByKey('claimed').orders.map((o) => o.retailcrm_order_id).join(',') === '4,3',
      groupByKey('claimed').orders.map((o) => o.retailcrm_order_id).join(','));

check('в «Везу» порядок сервера не трогаем',
      groupByKey('picked_up').orders.map((o) => o.retailcrm_order_id).join(',') === '1,2',
      '(заказ уже в руках — готовность там ничего не решает, а время доставки решает)');

setOrders([
    order('problem',   { retailcrm_order_id: 5, is_ready: false }),
    order('problem',   { retailcrm_order_id: 6, is_ready: true }),
    order('delivered', { retailcrm_order_id: 7, is_ready: false }),
    order('delivered', { retailcrm_order_id: 8, is_ready: true }),
]);
check('в «Требует внимания» и «Выполнено» — тоже порядок сервера',
      groupByKey('problem').orders.map((o) => o.retailcrm_order_id).join(',') === '5,6'
      && groupByKey('delivered').orders.map((o) => o.retailcrm_order_id).join(',') === '7,8');

// Остальные табы не тронуты: там свои и чужие вперемешку, делить нечем
state.filter = 'free';
setOrders([
    order(null, { retailcrm_order_id: 10, is_mine: false, is_free: true, is_ready: false }),
    order(null, { retailcrm_order_id: 11, is_mine: false, is_free: true, is_ready: true }),
]);
check('на других табах общая сортировка с подъёмом готовых сохранилась',
      sandbox.visibleOrders().map((o) => o.retailcrm_order_id).join(',') === '11,10',
      sandbox.visibleOrders().map((o) => o.retailcrm_order_id).join(','));
state.filter = 'mine';


console.log('\n3. Счётчик таба считает работу, а не строки');

setOrders([
    order('claimed'), order('claimed'),
    order('picked_up'),
    order('problem'),
    order('delivered'), order('delivered'), order('delivered'),
]);

check('выполненные в счётчик не входят', sandbox.counts().mine === 4,
      `(насчитал ${sandbox.counts().mine} из 7 строк)`);
check('проблемный заказ в счётчик входит',
      sandbox.counts().mine === 4,
      '(букет физически у курьера — дело не закрыто)');

setOrders([order('delivered'), order('delivered')]);
check('день, где всё доставлено, даёт ноль', sandbox.counts().mine === 0);

// Счётчик уважает фильтр по салонам — иначе «Мои 3» при пустом списке
setOrders([
    order('claimed', { site_code: 'sverdlovsky' }),
    order('claimed', { site_code: 'central' }),
], ['central']);
check('счётчик считается по выбранным салонам', sandbox.counts().mine === 1);
check('и секции тоже фильтруются по салонам',
      groupByKey('claimed').orders.length === 1
      && groupByKey('claimed').orders[0].site_code === 'central');

// Слой выбора салонов считает то же, что и таб: иначе «Мои 0» и
// «Свердловский 3» спорят друг с другом на одном экране.
state.sites = [];
state.sites_catalog = [
    { code: 'sverdlovsky', name: 'Свердловский' },
    { code: 'central', name: 'Центральный' },
];
setOrders([
    order('delivered', { site_code: 'sverdlovsky' }),
    order('delivered', { site_code: 'sverdlovsky' }),
    order('claimed',   { site_code: 'central' }),
]);
const sverdlovsky = sandbox.siteOptions().filter((s) => s.code === 'sverdlovsky')[0];
check('салон с одними выполненными показывает 0, как и таб',
      sverdlovsky && sverdlovsky.count === 0 && sandbox.counts().mine === 1,
      `(салон: ${sverdlovsky && sverdlovsky.count}, таб: ${sandbox.counts().mine})`);
check('но из списка салон не пропадает', !!sverdlovsky,
      '(иначе выполненные там уже не посмотреть)');
state.sites_catalog = [];


console.log('\n4. Разметка секций');

state.sites = [];
state.mineDoneOpen = false;
setOrders([
    order('picked_up', { retailcrm_order_id: 21 }),
    order('claimed',   { retailcrm_order_id: 22 }),
    order('delivered', { retailcrm_order_id: 23 }),
    order('delivered', { retailcrm_order_id: 24 }),
]);

const html = sandbox.mineFeedHtml();

check('заголовки секций на месте',
      html.includes('Везу') && html.includes('Забрать в салоне'));
check('у заголовка есть счётчик', html.includes('cd-group__count'));
check('строка выполненных показывает число',
      html.includes('Выполнено<span class="cd-done__count">2</span>'),
      '(должно быть «Выполнено 2»)');
check('свёрнутый блок не рисует карточки выполненных',
      !html.includes('data-order="23"') && !html.includes('data-order="24"'),
      '(иначе сворачивать нечего)');
check('активные карточки на месте',
      html.includes('data-order="21"') && html.includes('data-order="22"'));
check('свёрнутое состояние объявлено для доступности',
      html.includes('aria-expanded="false"'));

state.mineDoneOpen = true;
const opened = sandbox.mineFeedHtml();
check('раскрытый блок показывает карточки',
      opened.includes('data-order="23"') && opened.includes('data-order="24"'));
check('и объявляет себя раскрытым', opened.includes('aria-expanded="true"'));
check('раскрытие переживает перерисовку',
      sandbox.mineFeedHtml().includes('data-order="23"'),
      '(состояние живёт в state, а не в DOM)');
state.mineDoneOpen = false;


console.log('\n5. Пустые состояния объясняют себя');

setOrders([order('delivered'), order('delivered')]);
const allDone = sandbox.mineFeedHtml();
check('день без дел, но с доставками говорит «всё закрыто»',
      allDone.includes('Все заказы за этот день доставлены'),
      '(пустой экран здесь читается как сбой приложения)');
check('и не называет этот день сегодняшним', !allDone.includes('сегодня'),
      '(лента показывает выбранную дату, а она бывает любой)');
check('и блок выполненных всё равно показан', allDone.includes('cd-done'));

// Самое дорогое враньё: активные заказы скрыты фильтром салонов, а экран
// отпускает курьера домой словами «всё доставлено».
setOrders([
    order('claimed',   { site_code: 'central' }),
    order('claimed',   { site_code: 'central' }),
    order('delivered', { site_code: 'sverdlovsky' }),
], ['sverdlovsky']);
const filtered = sandbox.mineFeedHtml();
check('скрытые фильтром дела не выдаются за выполненные',
      !filtered.includes('Все заказы за этот день доставлены'),
      '(курьер уедет, оставив два букета в другом салоне)');
check('и экран зовёт снять фильтр', filtered.includes('Все салоны'));
state.sites = [];

setOrders([]);
const nothing = sandbox.mineFeedHtml();
check('день без единого заказа объясняется своим текстом',
      nothing.includes('не брали заказов'), nothing);
check('текст помнит про выбранный день',
      nothing.includes('На этот день'),
      '(лента показывает выбранную дату, а не «вообще»)');

setOrders([order('claimed', { site_code: 'central' })], ['sverdlovsky']);
check('пустой фильтр по салонам объясняет себя отдельно',
      sandbox.mineFeedHtml().includes('Все салоны'),
      '(иначе это читается как «заказов нет»)');
state.sites = [];


console.log('\n6. Кнопки соответствуют состоянию');

const claimedCard = sandbox.cardHtml(order('claimed', { retailcrm_order_id: 31 }));
check('у забронированного есть «Отказаться»', claimedCard.includes('data-release="31"'));

const pickedCard = sandbox.cardHtml(order('picked_up', { retailcrm_order_id: 32 }));
check('у забранного «Отказаться» осталась', pickedCard.includes('data-release="32"'),
      '(сервер объяснит, что снимает бронь управляющий)');

const doneCard = sandbox.cardHtml(order('delivered', { retailcrm_order_id: 33 }));
check('у доставленного «Отказаться» нет', !doneCard.includes('data-release'),
      '(живой брони нет — сервер ответил бы «Бронь уже снята»)');
check('но открыть его можно', doneCard.includes('data-open="33"'));
check('и он приглушён', doneCard.includes('cd-card--done'));

const problemCard = sandbox.cardHtml(order('problem', { retailcrm_order_id: 34 }));
check('у проблемного «Отказаться» тоже нет', !problemCard.includes('data-release'));
check('и он открывается', problemCard.includes('data-open="34"'));


console.log('\n7. Разметка, стили и код не разъехались');

check('секции рисуются только на вкладке «Мои»',
      source.includes("state.filter === 'mine'") && source.includes('mineFeedHtml()'),
      '(на других табах свои и чужие заказы вперемешку)');
check('у строки выполненных есть обработчик',
      source.includes("closest('[data-mine-done]')") && source.includes('data-mine-done'));
check('обработчик переключает состояние в state',
      source.includes('state.mineDoneOpen = !state.mineDoneOpen'));
check('состояние заведено с безопасным значением',
      /mineDoneOpen:\s*false/.test(source));
check('прокрутка возвращается после перерисовки ленты',
      source.includes('window.scrollTo(0, scroll)'),
      '(правило CLAUDE.md про innerHTML)');

for (const cls of ['cd-group', 'cd-group__title', 'cd-group__count',
                   'cd-done', 'cd-done__count', 'cd-done__chevron',
                   'cd-card--done']) {
    check(`класс .${cls} описан в courier-app.css`, css.includes(`.${cls}`));
}

check('заголовки секций не липкие',
      !/\.cd-group__title\s*\{[^}]*position:\s*sticky/.test(css),
      '(над лентой уже три закреплённых слоя — четвёртый съест треть экрана)');
check('строка выполненных — полноценная цель нажатия',
      /\.cd-done\s*\{[^}]*min-height:\s*var\(--cd-touch\)/.test(css));
check('своих цветов не завели: только токены --bx-*',
      !/\.cd-group[^{]*\{[^}]*#[0-9a-fA-F]{3,6}/.test(css),
      '(DESIGN-SPEC: палитра только из токенов)');


console.log('');
if (failures.length) {
    console.log(`=== ПРОВАЛЕНО: ${failures.length} ===`);
    failures.forEach((name) => console.log(`  - ${name}`));
    process.exit(1);
}
console.log('=== Вкладка «Мои» разложена по секциям верно ===');
