/**
 * Сторож экрана «Мои доставки» в приложении курьера.
 *
 * Проверяется ПОВЕДЕНИЕ настоящих функций из courier-app.js: их исходник
 * вырезается из файла и исполняется в песочнице. Так же устроен сторож
 * таймаута (scripts/test_courier_timeout.js) — полная DOM-заглушка для этого
 * файла больше самой правки.
 *
 * Что здесь ловится:
 *
 * 1. **Границы пресетов.** «7 дней» — это сегодня и шесть назад, а не семь
 *    назад; «этот месяц» — с первого числа. Ошибка на день означает сумму,
 *    не совпавшую с выплатой, то есть спор о деньгах.
 * 2. **Числа берутся с сервера, а не считаются здесь.** Итог в разметке равен
 *    тому, что пришло в `totals`, даже если он не сходится с суммой дней:
 *    складывать дни на клиенте значит завести второй расчёт зарплаты.
 * 3. **Разрыв «отвезли — не закрыли».** Строка про ожидающие заказы обязана
 *    появляться, иначе заказ для курьера пропадает молча.
 * 4. **Нет связки с CRM** — объяснение вместо честного нуля.
 * 5. **Экранирование.** Текст предупреждения приходит с сервера и попадает в
 *    разметку.
 * 6. **Связка разметки и кода**: каждый id, который ищет модуль, есть в
 *    courier-app.html; у кнопок пресетов есть обработчик.
 *
 * Запуск: node scripts/test_courier_earnings_view.js
 */

const fs = require('fs');
const path = require('path');
const vm = require('vm');

// Путь можно подменить — так проверяется сам сторож: прогон по заведомо
// сломанной копии обязан падать.
const ROOT = path.join(__dirname, '..', 'src', 'dashboard');
const SRC = process.env.COURIER_APP_JS || path.join(ROOT, 'courier-app.js');
const HTML = process.env.COURIER_APP_HTML || path.join(ROOT, 'courier-app.html');

const source = fs.readFileSync(SRC, 'utf8');
const html = fs.readFileSync(HTML, 'utf8');

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

const sandbox = {
    console: { log() {}, warn() {}, error() {} },
    Date, Math, Number, String, Object, Array, JSON, RegExp, isNaN,
};
sandbox.window = sandbox;
vm.createContext(sandbox);

// shortDate опирается на список месяцев — берём и его из файла, иначе своя
// копия начнёт жить своей жизнью и проверка перестанет что-либо значить.
const months = source.match(/var MONTHS = \[[\s\S]*?\];/);
if (!months) throw new Error('в courier-app.js не найден список MONTHS');
vm.runInContext(months[0], sandbox);

// Порядок важен: earningsBodyHtml зовёт esc, shortDate, earningsMoney и
// earningsDayLabel. Берём их из того же файла — подменять их своими значило бы
// проверять не тот код.
for (const name of ['esc', 'shortDate', 'earningsPresetRange', 'earningsMoney',
                    'earningsDayLabel', 'earningsBodyHtml']) {
    vm.runInContext(cut(name), sandbox);
}


console.log('\n1. Границы пресетов');

const TODAY = '2026-09-19';

const today = sandbox.earningsPresetRange('today', TODAY);
check('«Сегодня» — один день', today.from === TODAY && today.to === TODAY,
      JSON.stringify(today));

const week = sandbox.earningsPresetRange('week', TODAY);
check('«7 дней» — это сегодня и шесть назад, а не семь',
      week.from === '2026-09-13' && week.to === TODAY, JSON.stringify(week));

const month = sandbox.earningsPresetRange('month', TODAY);
check('«Этот месяц» — с первого числа',
      month.from === '2026-09-01' && month.to === TODAY, JSON.stringify(month));

// Граница месяца: вычитание дней обязано переходить через неё
const overMonth = sandbox.earningsPresetRange('week', '2026-10-03');
check('неделя переходит через границу месяца',
      overMonth.from === '2026-09-27', JSON.stringify(overMonth));

check('неизвестный пресет не выдумывает период',
      sandbox.earningsPresetRange('quarter', TODAY) === null);
check('без «сегодня» с сервера период не строится',
      sandbox.earningsPresetRange('today', null) === null);


console.log('\n2. Числа показываются серверные, а не пересчитанные');

const payload = {
    days: [
        { date: '2026-09-19', orders_count: 3, total_net_cost: 900, zero_cost: 0 },
        { date: '2026-09-18', orders_count: 6, total_net_cost: 1800, zero_cost: 0 },
    ],
    // Итог намеренно НЕ равен сумме дней: если экран сложит дни сам, он
    // покажет 2700 — и это будет второй расчёт зарплаты
    totals: { orders_count: 9, total_net_cost: 2500, zero_cost: 0 },
};
const meta = { date_from: '2026-09-01', date_to: TODAY, awaiting_close: 0 };
const body = sandbox.earningsBodyHtml(payload, meta, TODAY);

// Сравниваем ТЕМ ЖЕ форматтером: ru-RU ставит неразрывный пробел, и своя
// строка с обычным пробелом не совпала бы, проверяя не то.
const money = sandbox.earningsMoney;
check('итог взят из totals, а не сложен из дней',
      body.includes(money(2500)) && !body.includes(money(2700)),
      '(экран обязан показывать серверное число)');
check('дни показаны', body.includes(money(900)) && body.includes(money(1800)));
check('сегодняшний день помечен', body.includes('cd-earn-day--today'));
check('итог подписан как деньги к оплате', body.includes('К оплате за период'));


console.log('\n3. Разрыв «отвезли — CRM не закрыла» назван словами');

const waiting = sandbox.earningsBodyHtml(
    payload, Object.assign({}, meta, { awaiting_close: 2 }), TODAY);
check('строка про ожидающие заказы есть', waiting.includes('Отвезли ещё 2'),
      '(без неё заказ для курьера просто пропадает)');
check('и она не попадает в сумму', waiting.includes(money(2500)));
check('без ожидающих заказов строки нет', !body.includes('Отвезли ещё'));


console.log('\n4. Пустой день и пустой период');

const emptyToday = sandbox.earningsBodyHtml(
    { days: [{ date: '2026-09-18', orders_count: 2, total_net_cost: 600, zero_cost: 0 }],
      totals: { orders_count: 2, total_net_cost: 600, zero_cost: 0 } },
    meta, TODAY);
check('сегодня без доставок всё равно показан прочерком',
      emptyToday.includes('cd-earn-day--today') && emptyToday.includes('—'),
      '(отсутствие строки читается как «экран не обновился»)');

const nothing = sandbox.earningsBodyHtml(
    { days: [], totals: { orders_count: 0, total_net_cost: 0, zero_cost: 0 } },
    { date_from: '2026-07-01', date_to: '2026-07-31', awaiting_close: 0 }, TODAY);
check('период без доставок говорит об этом словами',
      nothing.includes('выполненных доставок нет'));
check('и всё равно показывает итог нулём, а не пустотой',
      nothing.includes(money(0)));


console.log('\n5. Заказ без стоимости назван, а не растворён');

const zero = sandbox.earningsBodyHtml(
    { days: [{ date: '2026-09-17', orders_count: 2, total_net_cost: 400, zero_cost: 1 }],
      totals: { orders_count: 2, total_net_cost: 400, zero_cost: 1 } },
    meta, TODAY);
check('строка про заказы без стоимости есть',
      zero.includes('Заказов без стоимости доставки: 1'));


console.log('\n6. Нет связки с CRM — объяснение вместо нуля');

const warned = sandbox.earningsBodyHtml(
    { days: [], totals: null },
    { warning: 'Ваша учётная запись не связана с курьером в CRM', awaiting_close: 0 },
    TODAY);
check('показано предупреждение', warned.includes('не связана с курьером'));
check('и никакого итога рядом с ним нет', !warned.includes('К оплате за период'),
      '(ноль здесь читается как «вы ничего не возили»)');

const evil = sandbox.earningsBodyHtml(
    { days: [], totals: null },
    { warning: '<img src=x onerror="alert(1)">', awaiting_close: 0 }, TODAY);
check('текст с сервера экранируется', !evil.includes('<img'), `(${evil})`);


console.log('\n7. Разметка и код не разъехались');

const ids = ['cdEarningsBtn', 'cdEarnings', 'cdEarnPresets', 'cdEarnFrom',
             'cdEarnTo', 'cdEarnBody'];
ids.forEach((id) => {
    const inJs = source.includes(`getElementById('${id}')`);
    const inHtml = html.includes(`id="${id}"`);
    check(`${id}: есть и в коде, и в разметке`, inJs && inHtml,
          `(js:${inJs} html:${inHtml})`);
});

check('у кнопок пресетов есть обработчик',
      html.includes('data-earn-preset=') && source.includes("closest('[data-earn-preset]')"));
check('поля дат слушают change, а не отправляют каждый символ',
      source.includes("el.earnFrom.addEventListener('change'")
      && source.includes("el.earnTo.addEventListener('change'"));
check('кнопка «Обновить» знает про этот экран',
      source.includes("state.view === 'earnings'"),
      '(иначе она молча обновляет ленту и выглядит сломанной)');
check('переписывается только тело списка, не поля дат',
      source.includes('el.earnBody.innerHTML')
      && !source.includes('el.earnings.innerHTML'),
      '(перерисовка всего экрана сбрасывала бы фокус в поле даты)');


console.log('');
if (failures.length) {
    console.log(`=== ПРОВАЛЕНО: ${failures.length} ===`);
    failures.forEach((name) => console.log(`  - ${name}`));
    process.exit(1);
}
console.log('=== Экран «Мои доставки» в порядке ===');
