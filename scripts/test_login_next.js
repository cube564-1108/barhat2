/*
 * Сторож возврата после входа (`?next=` на форме входа).
 *
 * ЗАЧЕМ ОТДЕЛЬНЫЙ ТЕСТ И ПОЧЕМУ ПО ПОВЕДЕНИЮ, А НЕ ПО ТЕКСТУ КОДА.
 *
 * Параметр `next` нужен приложению курьера: без него вход всегда высаживал
 * человека на дашборд. Но значение приходит из адресной строки, то есть его
 * подставляет кто угодно ссылкой в мессенджере, и цена ошибки здесь — пароль:
 * человек вводит его на НАШЕЙ форме и уезжает на чужой сайт, уверенный, что
 * он всё ещё у нас.
 *
 * Первая версия проверки смотрела на начало строки («начинается с одного /,
 * дальше не / и не \»). Она выглядит достаточной и обходится одним невидимым
 * символом: парсер адресов выбрасывает табы и переводы строк и приводит «\» к
 * «/», поэтому «/<таб>/чужой-домен» проходил её как свой путь, а браузер шёл
 * на «//чужой-домен». Поймал это security-review 17.09.2026.
 *
 * Отсюда форма сторожа: он не ищет в файле нужные слова — тогда он проверял
 * бы ровно ту бессильную проверку, — а ВЫПОЛНЯЕТ функцию на враждебных входах.
 *
 * Запуск: node scripts/test_login_next.js
 */

'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO = path.dirname(__dirname);
const LOGIN = path.join(REPO, 'src', 'dashboard', 'login.html');

const failures = [];

function check(name, condition, detail) {
    if (condition) {
        console.log(`  [OK  ] ${name}`);
    } else {
        console.log(`  [FAIL] ${name}${detail ? ' — ' + detail : ''}`);
        failures.push(name);
    }
}

/** Вырезать объявление функции целиком, считая скобки. */
function extractFunction(source, name) {
    const start = source.indexOf(`function ${name}(`);
    if (start === -1) return null;
    let depth = 0;
    for (let i = source.indexOf('{', start); i < source.length; i++) {
        if (source[i] === '{') depth += 1;
        else if (source[i] === '}') {
            depth -= 1;
            if (depth === 0) return source.slice(start, i + 1);
        }
    }
    return null;
}

const html = fs.readFileSync(LOGIN, 'utf-8');
const source = extractFunction(html, 'nextTarget');

console.log('\n1. Функция возврата найдена в форме входа');
check('nextTarget() на месте', !!source,
      '(вход перестал читать next — курьера снова высадит на дашборд)');
if (!source) {
    console.log('\nПРОВАЛЕНО: без функции остальные проверки бессмысленны');
    process.exit(1);
}

const ORIGIN = 'https://dashboard.example';

/** Выполнить nextTarget() так, как будто форма открыта по заданному адресу. */
function run(search) {
    const sandbox = {
        window: { location: { search: search, origin: ORIGIN } },
        URL: URL,
        URLSearchParams: URLSearchParams,
    };
    vm.createContext(sandbox);
    return vm.runInContext(`${source}; nextTarget();`, sandbox);
}

console.log('\n2. Свой путь пропускается');
check('приложение курьера', run('?next=%2Fapp%2Fcourier') === '/app/courier');
check('раздел дашборда', run('?next=%2Finvoices-v2') === '/invoices-v2');
check('путь с параметрами', run('?next=%2Freports%3Fid%3D7') === '/reports?id=7');
check('без next — дашборд', run('') === '/');
check('пустой next — дашборд', run('?next=') === '/');

console.log('\n3. Чужой адрес не пропускается');

// Невидимые символы собираем кодом, а не escape-последовательностью: в
// исходнике теста они иначе неотличимы от обычного отступа, и следующая
// правка файла молча выровняет их редактором.
const TAB = String.fromCharCode(9);
const LF = String.fromCharCode(10);
const CR = String.fromCharCode(13);

// Каждая строка — рабочий обход проверки «по началу строки». Цена промаха
// здесь — пароль сотрудника, поэтому перечисляем все известные написания.
const HOSTILE = [
    ['абсолютный адрес', 'https://evil.example/x'],
    ['без схемы', '//evil.example/x'],
    ['обратный слэш', '/\\evil.example'],
    ['два обратных слэша', '\\\\evil.example'],
    ['таб внутри', '/' + TAB + '/evil.example'],
    ['перевод строки внутри', '/' + LF + '/evil.example'],
    ['возврат каретки внутри', '/' + CR + '/evil.example'],
    ['таб перед обратным слэшем', '/' + TAB + '\\evil.example'],
    ['javascript:', 'javascript:alert(1)'],
    ['data:', 'data:text/html,<script>alert(1)</script>'],
    ['другой порт того же хоста', 'https://dashboard.example:8443/x'],
];

for (const [name, value] of HOSTILE) {
    const result = run('?next=' + encodeURIComponent(value));
    // Достаточное условие: то, что вернулось, остаётся НАШИМ адресом — оно
    // уходит прямо в window.location.href.
    let sameOrigin = false;
    try {
        sameOrigin = new URL(result, ORIGIN).origin === ORIGIN;
    } catch (e) {
        sameOrigin = false;
    }
    check(`${name} не уводит с сайта`, sameOrigin,
          `вернулось ${JSON.stringify(result)}`);
}

console.log();
if (failures.length) {
    console.log(`ПРОВАЛЕНО: ${failures.length} — ${failures.join('; ')}`);
    process.exit(1);
}
console.log('Все проверки пройдены');
