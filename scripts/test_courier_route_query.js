/**
 * Сторож кнопки «Маршрут» в приложении курьера.
 *
 * Адрес приходит из RetailCRM одной строкой, которую заводят руками, и формат
 * у неё разный по городам: в Новосибирске — «ул. Ленина, 45», в Екатеринбурге
 * — «Свердловская область, Екатеринбург, ул. Бажова, 89». Кнопка отдаёт эту
 * строку Яндекс.Картам, подставляя город впереди — иначе улицу ищут по всей
 * стране. С 23.09.2026 адрес отдаётся курьеру целиком, то есть город в строке
 * УЖЕ есть у половины заказов, и безусловная подстановка давала
 * «Екатеринбург, Свердловская область, Екатеринбург, ...».
 *
 * Проверяется ПОВЕДЕНИЕ настоящей функции из courier-app.js: её исходник
 * вырезается из файла и исполняется в песочнице (так же устроен
 * test_courier_mine_sections.js).
 *
 * Запуск: node scripts/test_courier_route_query.js
 */

const fs = require('fs');
const path = require('path');
const vm = require('vm');

// Путь можно подменить — так проверяется сам сторож: прогон по версии до
// правки обязан падать.
const ROOT = path.join(__dirname, '..', 'src', 'dashboard');
const SRC = process.env.COURIER_APP_JS || path.join(ROOT, 'courier-app.js');

const source = fs.readFileSync(SRC, 'utf8');
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

const sandbox = { console: { log() {}, warn() {}, error() {} }, String, Object };
sandbox.window = sandbox;
vm.createContext(sandbox);

// Отсутствие функции — это тоже провал, а не падение сторожа с трейсом: на
// версии до 23.09.2026 строка для карт склеивалась прямо в разметке кнопки,
// и читающий должен увидеть причину, а не стек Node.
if (source.indexOf('function routeQuery(') === -1) {
    console.log('  [FAIL] в courier-app.js нет функции routeQuery '
                + '(строку для карт снова собирают в разметке)');
    console.log('\n=== ПРОВАЛЕНО: 1 ===');
    process.exit(1);
}
vm.runInContext(cut('routeQuery'), sandbox);

const routeQuery = sandbox.routeQuery;

console.log('\n1. Адрес без города: город подставляется впереди');

check('улица из CRM дополняется городом',
      routeQuery({ city: 'Новосибирск', address_text: 'ул. Ленина, 45, кв. 12' })
      === 'Новосибирск, ул. Ленина, 45, кв. 12',
      routeQuery({ city: 'Новосибирск', address_text: 'ул. Ленина, 45, кв. 12' }));

console.log('\n2. Адрес с городом внутри: второй раз город не подставляется');

const ekb = 'Свердловская область, Екатеринбург, ул. Бажова, 89, кв. 12';
check('город не задваивается',
      routeQuery({ city: 'Екатеринбург', address_text: ekb }) === ekb,
      routeQuery({ city: 'Екатеринбург', address_text: ekb }));

check('регистр не мешает узнать город',
      routeQuery({ city: 'Екатеринбург', address_text: 'свердловская область, ЕКАТЕРИНБУРГ, ул. Бажова, 89' })
      === 'свердловская область, ЕКАТЕРИНБУРГ, ул. Бажова, 89');

check('«г.» перед городом не мешает',
      routeQuery({ city: 'Екатеринбург', address_text: 'г. Екатеринбург, ул. Бажова, 89' })
      === 'г. Екатеринбург, ул. Бажова, 89');

check('город из двух слов узнаётся целиком',
      routeQuery({ city: 'Нижний Новгород', address_text: 'г. Нижний Новгород, ул. Минина, 3' })
      === 'г. Нижний Новгород, ул. Минина, 3');

console.log('\n2а. Область — это не город');
// «Новосибирск» входит подстрокой в «Новосибирская область»: поиск подстрокой
// решил бы, что город уже назван, и улицу искали бы по всей области — ровно
// тот случай, ради которого подстановка и нужна.

check('регион без города не считается городом',
      routeQuery({ city: 'Новосибирск', address_text: 'Новосибирская область, ул. Ленина, 45' })
      === 'Новосибирск, Новосибирская область, ул. Ленина, 45',
      routeQuery({ city: 'Новосибирск', address_text: 'Новосибирская область, ул. Ленина, 45' }));

check('регион И город — город не задваивается',
      routeQuery({ city: 'Новосибирск', address_text: 'Новосибирская область, Новосибирск, ул. Ленина, 45' })
      === 'Новосибирская область, Новосибирск, ул. Ленина, 45');

check('улица с названием города городом не считается',
      routeQuery({ city: 'Москва', address_text: 'ул. Москворецкая, 5' })
      === 'Москва, ул. Москворецкая, 5');

console.log('\n3. Пустые значения не превращаются в мусор для карт');

check('нет города — уходит один адрес',
      routeQuery({ city: null, address_text: 'ул. Ленина, 45' }) === 'ул. Ленина, 45');
check('нет адреса — пустая строка, а не «null»',
      routeQuery({ city: 'Новосибирск', address_text: null }) === 'Новосибирск, ');
check('нет ничего — пустая строка',
      routeQuery({}) === '');

console.log('\n4. Строку для карт собирает функция, а не разметка');
// Прежняя версия склеивала [order.city, order.address_text] прямо в шаблоне
// кнопки: правка формата адреса обходила бы любой сторож над функцией.
check('кнопка «Маршрут» зовёт routeQuery',
      /data-route="'\s*\+\s*esc\(routeQuery\(order\)\)/.test(source),
      '(в разметке снова склейка вручную)');

console.log('');
if (failures.length) {
    console.log(`=== ПРОВАЛЕНО: ${failures.length} ===`);
    failures.forEach((name) => console.log(`  - ${name}`));
    process.exit(1);
}
console.log('=== Строка маршрута собирается верно ===');
