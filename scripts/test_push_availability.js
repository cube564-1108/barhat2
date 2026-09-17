/*
 * Сторож ответа «почему нет уведомлений» в приложении курьера.
 *
 * ЧТО ЗДЕСЬ ЛОВИТСЯ.
 *
 * Apple отдаёт Push API ТОЛЬКО приложению, добавленному на экран «Домой», и
 * только с iOS 16.4. В обычной вкладке Safari `PushManager` и `Notification`
 * отсутствуют — проверка поддержки честно отвечает «нельзя», и раньше на этом
 * всё заканчивалось: кнопка оставалась спрятанной, приложение молчало.
 *
 * Молчание — худший из ответов. Курьер видит кнопку у коллеги на Android и
 * делает единственный доступный вывод: приложение сломано. Проверено на
 * iPhone владельца 17.09.2026 — кнопки не было и объяснения тоже.
 *
 * Тест ВЫПОЛНЯЕТ разбор на подставных окружениях, а не ищет слова в файле:
 * условие «есть ли API» пишется легко и так же легко переворачивается, а
 * увидеть это можно только на iPhone, которого под рукой обычно нет.
 *
 * Запуск: node scripts/test_push_availability.js
 */

'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO = path.dirname(__dirname);
const APP = path.join(REPO, 'src', 'dashboard', 'courier-app.js');

const failures = [];

function check(name, condition, detail) {
    if (condition) {
        console.log(`  [OK  ] ${name}`);
    } else {
        console.log(`  [FAIL] ${name}${detail ? ' — ' + detail : ''}`);
        failures.push(name);
    }
}

/** Вырезать объявление (функцию или var) целиком, считая скобки. */
function extract(source, header, open, close) {
    const start = source.indexOf(header);
    if (start === -1) return null;
    let depth = 0;
    for (let i = source.indexOf(open, start); i < source.length; i++) {
        if (source[i] === open) depth += 1;
        else if (source[i] === close) {
            depth -= 1;
            if (depth === 0) return source.slice(start, i + 1);
        }
    }
    return null;
}

const js = fs.readFileSync(APP, 'utf-8');

const pieces = {
    pushSupported: extract(js, 'function pushSupported(', '{', '}'),
    isIOS: extract(js, 'function isIOS(', '{', '}'),
    isStandalone: extract(js, 'function isStandalone(', '{', '}'),
    pushBlockReason: extract(js, 'function pushBlockReason(', '{', '}'),
    PUSH_BLOCK_TEXT: extract(js, 'var PUSH_BLOCK_TEXT = ', '{', '}'),
};

console.log('\n1. Разбор доступности на месте');
for (const [name, code] of Object.entries(pieces)) {
    check(`${name} найден`, !!code, '(без него приложение снова замолчит)');
}
if (Object.values(pieces).some((v) => !v)) {
    console.log('\nПРОВАЛЕНО: дальше проверять нечего');
    process.exit(1);
}

const SOURCE = Object.values(pieces).join('\n');

const UA_IPHONE = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) '
    + 'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1';
const UA_ANDROID = 'Mozilla/5.0 (Linux; Android 13; SM-A536B) AppleWebKit/537.36 '
    + '(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36';
const UA_IPAD = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 '
    + '(KHTML, like Gecko) Version/17.0 Safari/605.1.15';

/**
 * Прогнать pushBlockReason() в подставном окружении.
 *
 * `hasPush` / `hasNotification` — есть ли у window соответствующий
 * конструктор. Именно их Apple и не отдаёт вне «Домой»: не «метод вернул
 * false», а свойства вовсе нет.
 */
function reasonFor(env) {
    const navigator = {
        userAgent: env.ua,
        platform: env.platform || 'iPhone',
        maxTouchPoints: env.maxTouchPoints || 0,
    };
    if (env.hasServiceWorker !== false) navigator.serviceWorker = {};
    if (env.iosStandalone !== undefined) navigator.standalone = env.iosStandalone;

    const window = {
        navigator: navigator,
        matchMedia: function () { return { matches: !!env.displayModeStandalone }; },
    };
    if (env.hasPush) window.PushManager = function () {};
    if (env.hasNotification) window.Notification = function () {};

    const sandbox = { window: window, navigator: navigator };
    sandbox.global = sandbox;
    vm.createContext(sandbox);
    return vm.runInContext(`${SOURCE}; pushBlockReason();`, sandbox);
}

console.log('\n2. Причина определяется верно');

check('Android Chrome — уведомления доступны',
      reasonFor({
          ua: UA_ANDROID, platform: 'Linux armv8l',
          hasPush: true, hasNotification: true,
      }) === null,
      '(на Android кнопка обязана работать как раньше)');

check('iPhone, вкладка Safari — «добавьте на Домой»',
      reasonFor({
          ua: UA_IPHONE, platform: 'iPhone',
          hasPush: false, hasNotification: false, iosStandalone: false,
      }) === 'ios-home-screen',
      '(ровно то, что видел владелец 17.09.2026)');

check('iPhone на «Домой», iOS 16.4+ — уведомления доступны',
      reasonFor({
          ua: UA_IPHONE, platform: 'iPhone',
          hasPush: true, hasNotification: true, iosStandalone: true,
      }) === null, '');

check('iPhone на «Домой», iOS старее 16.4 — «обновите систему»',
      reasonFor({
          ua: UA_IPHONE, platform: 'iPhone',
          hasPush: false, hasNotification: false, iosStandalone: true,
      }) === 'ios-version',
      '(добавлять на Домой второй раз бесполезно — надо сказать про версию)');

check('iPad на iPadOS выдаёт себя за мак — всё равно iPhone-путь',
      reasonFor({
          ua: UA_IPAD, platform: 'MacIntel', maxTouchPoints: 5,
          hasPush: false, hasNotification: false, iosStandalone: false,
      }) === 'ios-home-screen',
      '(проверка по userAgent одна такой iPad не ловит)');

check('прочий браузер без поддержки — свой текст',
      reasonFor({
          ua: UA_ANDROID, platform: 'Linux armv8l',
          hasPush: false, hasNotification: false,
      }) === 'browser', '');

console.log('\n3. У каждой причины есть, что сказать человеку');

const texts = vm.runInNewContext(`${pieces.PUSH_BLOCK_TEXT}; PUSH_BLOCK_TEXT;`, {});
for (const reason of ['ios-home-screen', 'ios-version', 'browser']) {
    const text = texts[reason];
    check(`${reason}: заголовок и объяснение`,
          !!text && !!text.title && !!text.message && text.message.length > 60,
          '(причина без текста — это снова молчание)');
}
// Инструкция обязана называть Safari: на iPhone добавить на «Домой» из Chrome
// нельзя, и курьер будет искать пункт меню, которого там нет.
check('инструкция для iPhone называет Safari',
      texts['ios-home-screen'].message.indexOf('Safari') !== -1,
      '(в Chrome на iPhone пункта «На экран Домой» нет)');
check('инструкция для iPhone называет версию iOS',
      texts['ios-home-screen'].message.indexOf('16.4') !== -1, '');

console.log('\n4. Кнопка остаётся живой и объясняет себя');

// Спрятанная кнопка не отвечает на вопрос «почему у меня её нет». Проверяем
// связку: заблокированное состояние рисуется, клик ведёт в объяснение.
check('заблокированная кнопка показывается',
      js.indexOf('if (state.pushBlock) {') !== -1
      && js.indexOf("classList.add('cd-header__btn--off')") !== -1,
      '(hidden = false до ветки с pushBlock)');
check('клик по ней объясняет причину',
      js.indexOf('if (state.pushBlock) { explainPushBlock(); return; }') !== -1, '');
check('объяснение показывается одной кнопкой',
      js.indexOf('cancelText: null') !== -1,
      '(это объяснение, а не выбор; см. showDialog в ui-dialog.js)');
check('setupPush больше не выходит молча',
      js.indexOf('if (!pushSupported()) return;') === -1,
      '(молчание — это и был баг)');

console.log();
if (failures.length) {
    console.log(`ПРОВАЛЕНО: ${failures.length} — ${failures.join('; ')}`);
    process.exit(1);
}
console.log('Все проверки пройдены');
