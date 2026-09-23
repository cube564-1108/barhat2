/**
 * Прогон компонента выбора периода (src/dashboard/date-range.js) в Node
 * с заглушкой DOM.
 *
 * Проверяется поведение из обращения #18 и то, ради чего компонент писался:
 *   - период выбирается протяжкой и кликом «начало → конец»;
 *   - один день выбирается как один день, а не как «с ... по пусто»;
 *   - onChange зовётся РОВНО ОДИН раз на выбранный период. Это не косметика:
 *     каждый вызов — запрос к списку, а запрос к базе на сетевом /data стоит
 *     90-700 мс при двух воркерах на проде. Раньше пара <input type="date">
 *     давала два запроса, первый из которых бессмыслен («с 16.09 по всегда»).
 *
 * Заглушка разбирает innerHTML в дерево: обработчики висят на контейнерах, а
 * дни рисуются строкой, и без разбора потерянный день выглядел бы как зелёный
 * тест (см. правило про перезапись UI-файлов в CLAUDE.md).
 *
 * Запуск:  node scripts/test_date_range.js
 * Проверка самого сторожа на сломанной версии:
 *          node scripts/test_date_range.js <путь к другой версии date-range.js>
 */

const path = require('path');
const { makeSandbox, makeEl } = require('./lib/dom_stub.js');

const SRC = process.argv[2]
    ? path.resolve(process.argv[2])
    : path.join(__dirname, '..', 'src', 'dashboard', 'date-range.js');

const failures = [];

function check(name, condition, detail) {
    const mark = condition ? 'OK  ' : 'FAIL';
    console.log(`  [${mark}] ${name}${detail ? ' — ' + detail : ''}`);
    if (!condition) failures.push(name);
}

// --- окружение ---------------------------------------------------------------

/** Песочница с компонентом и смонтированным полем периода. */
function makePicker(options) {
    const env = makeSandbox({ src: SRC });
    const api = env.sandbox.BarhatDateRange;
    const mount = makeEl('div');
    const calls = [];
    const picker = api.create(Object.assign({
        mount,
        onChange: (from, to) => calls.push({ from, to }),
    }, options || {}));

    const q = (selector) => mount.querySelector(selector);
    return {
        env, api, mount, picker, calls, q,
        btn: () => q('[data-dr-btn]'),
        pop: () => q('[data-dr-pop]'),
        label: () => q('[data-dr-label]').textContent,
        cals: () => q('[data-dr-cals]'),
        day: (key) => q(`[data-day="${key}"]`),
        inputFrom: () => q('[data-dr-in="from"]'),
        inputTo: () => q('[data-dr-in="to"]'),
    };
}

function makeEvent(target) {
    return { target, preventDefault() {}, stopPropagation() {} };
}

/**
 * Клик, как его видит браузер: одно и то же событие всплывает от цели к
 * документу. Важно именно так: компонент узнаёт свой клик по объекту события,
 * потому что перелистывание месяца заменяет разметку календаря и к моменту
 * проверки кликнутая кнопка из дерева уже выброшена.
 */
function bubbleClick(ctx, target, nodes) {
    const event = makeEvent(target);
    (nodes || []).forEach(node => { if (node) node.fire('click', event); });
    ctx.mount.fire('click', event);
    (ctx.env.domListeners.click || []).forEach(fn => fn(event));
    return event;
}

/** Клик по дню календаря: всплывает через контейнер календарей. */
function clickDay(ctx, key) {
    const cell = ctx.day(key);
    if (!cell) throw new Error('в календаре нет дня ' + key);
    return bubbleClick(ctx, cell, [ctx.cals()]);
}

/** Протяжка: нажали на одном дне, отпустили на другом. */
function dragDays(ctx, fromKey, toKey) {
    const cals = ctx.cals();
    cals.fire('pointerdown', makeEvent(ctx.day(fromKey)));
    cals.fire('pointermove', makeEvent(ctx.day(toKey)));
    cals.fire('pointerup', makeEvent(ctx.day(toKey)));
    // Браузер шлёт click по дню, на котором отпустили кнопку
    const cell = ctx.day(toKey);
    if (cell) bubbleClick(ctx, cell, [ctx.cals()]);
    (ctx.env.domListeners.pointerup || []).forEach(fn => fn({}));
}

function clickOutside(ctx) {
    const event = makeEvent(makeEl('div'));
    (ctx.env.domListeners.click || []).forEach(fn => fn(event));
}

function pressEscape(ctx) {
    (ctx.env.domListeners.keydown || []).forEach(fn => fn({ key: 'Escape' }));
}

function openPicker(ctx) {
    bubbleClick(ctx, ctx.btn(), [ctx.btn()]);
}

/** Дата, гарантированно попадающая в один из двух показанных месяцев. */
function shiftToday(api, days) {
    const today = api.todayKey();
    const [y, m, d] = today.split('-').map(Number);
    const date = new Date(y, m - 1, d + days);
    const pad = (n) => String(n).padStart(2, '0');
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

// --- 1. Разбор и подпись дат -------------------------------------------------

console.log('\n1. Разбор дат и подпись периода');
{
    const { api } = makePicker();
    check('дд.мм.гггг разбирается', api.parseHuman('16.09.2026') === '2026-09-16');
    check('дд.мм.гг разбирается', api.parseHuman('16.09.26') === '2026-09-16');
    check('разделители любые', api.parseHuman('16/09/2026') === '2026-09-16');
    check('YYYY-MM-DD разбирается', api.parseHuman('2026-09-16') === '2026-09-16');
    check('пусто — это пусто, а не ошибка', api.parseHuman('  ') === '');
    // Иначе 31 февраля молча превратится в 3 марта и уедет в фильтр
    check('несуществующая дата — null', api.parseHuman('31.02.2026') === null);
    check('мусор — null', api.parseHuman('позавчера') === null);

    check('один день — одна дата', api.label('2026-09-16', '2026-09-16', 'Все даты') === '16.09.2026');
    check('период в одном году — без лишнего года',
        api.label('2026-09-12', '2026-09-20', '') === '12.09 – 20.09.2026',
        api.label('2026-09-12', '2026-09-20', ''));
    check('период через год — обе даты целиком',
        api.label('2025-12-30', '2026-01-10', '') === '30.12.2025 – 10.01.2026');
    check('пусто — подпись по умолчанию', api.label('', '', 'Все даты') === 'Все даты');
}

// --- 2. Исходное состояние ---------------------------------------------------

console.log('\n2. Поле до выбора');
{
    const ctx = makePicker();
    check('на кнопке «Все даты»', ctx.label() === 'Все даты', ctx.label());
    check('окно закрыто', ctx.pop().hidden === true);
    check('крестик сброса спрятан', ctx.q('[data-dr-clear]').hidden === true);
    check('onChange не звался', ctx.calls.length === 0);

    openPicker(ctx);
    check('кнопка открывает окно', ctx.pop().hidden === false);
    check('календарь отрисован', ctx.cals().querySelectorAll('[data-day]').length >= 56,
        String(ctx.cals().querySelectorAll('[data-day]').length) + ' дней');
    check('месяцев показано два', ctx.cals().querySelectorAll('.bxdr__cal').length === 2);
}

// --- 3. Выбор периода кликами ------------------------------------------------

console.log('\n3. Клик «начало → конец»');
{
    const ctx = makePicker();
    const from = shiftToday(ctx.api, 2);
    const to = shiftToday(ctx.api, 9);
    openPicker(ctx);

    clickDay(ctx, from);
    check('после первого клика запроса нет', ctx.calls.length === 0);
    check('окно не закрылось', ctx.pop().hidden === false);
    check('первый край подсвечен', Boolean(ctx.day(from).className.match(/is-start/)));

    clickDay(ctx, to);
    check('период уехал одним вызовом', ctx.calls.length === 1,
        ctx.calls.length + ' вызов(а/ов) onChange');
    check('границы верные', ctx.calls[0].from === from && ctx.calls[0].to === to,
        JSON.stringify(ctx.calls[0]));
    check('окно закрылось само', ctx.pop().hidden === true);
    check('подпись — период', ctx.label() === ctx.api.label(from, to, ''), ctx.label());
    check('крестик сброса появился', ctx.q('[data-dr-clear]').hidden === false);
}

// --- 4. Обратный порядок -----------------------------------------------------

console.log('\n4. Сначала поздняя дата, потом ранняя');
{
    const ctx = makePicker();
    const early = shiftToday(ctx.api, 3);
    const late = shiftToday(ctx.api, 12);
    openPicker(ctx);
    clickDay(ctx, late);
    clickDay(ctx, early);
    check('края переставлены местами', ctx.calls.length === 1
        && ctx.calls[0].from === early && ctx.calls[0].to === late, JSON.stringify(ctx.calls));
}

// --- 5. Один день ------------------------------------------------------------

console.log('\n5. Один день — это один день');
{
    const ctx = makePicker();
    const day = shiftToday(ctx.api, 4);

    openPicker(ctx);
    clickDay(ctx, day);
    clickDay(ctx, day);
    check('повторный клик даёт ровно этот день',
        ctx.calls.length === 1 && ctx.calls[0].from === day && ctx.calls[0].to === day,
        JSON.stringify(ctx.calls));
    check('подпись — одна дата', ctx.label() === ctx.api.formatHuman(day), ctx.label());

    // Кликнул день и ушёл мимо окна: день назван, полпериода в фильтр не уедет
    const other = makePicker();
    const single = shiftToday(other.api, 6);
    openPicker(other);
    clickDay(other, single);
    clickOutside(other);
    check('клик мимо применяет выбранный день',
        other.calls.length === 1 && other.calls[0].from === single && other.calls[0].to === single,
        JSON.stringify(other.calls));
    check('окно закрылось', other.pop().hidden === true);
}

// --- 6. Esc — это отмена -----------------------------------------------------

console.log('\n6. Esc отменяет незавершённый выбор');
{
    const ctx = makePicker();
    openPicker(ctx);
    clickDay(ctx, shiftToday(ctx.api, 5));
    pressEscape(ctx);
    check('запроса не было', ctx.calls.length === 0);
    check('окно закрыто', ctx.pop().hidden === true);
    check('период остался пустым', ctx.picker.getRange().from === '');
}

// --- 7. Протяжка -------------------------------------------------------------

console.log('\n7. Выбор протяжкой');
{
    const ctx = makePicker();
    const from = shiftToday(ctx.api, 1);
    const to = shiftToday(ctx.api, 8);
    openPicker(ctx);
    dragDays(ctx, from, to);

    check('протяжка даёт период одним вызовом', ctx.calls.length === 1,
        ctx.calls.length + ' вызов(а/ов) onChange');
    check('границы верные', ctx.calls[0] && ctx.calls[0].from === from && ctx.calls[0].to === to,
        JSON.stringify(ctx.calls[0]));
    check('окно закрылось', ctx.pop().hidden === true);

    // click после отпускания не должен начинать новый выбор — иначе следующий
    // клик по календарю попадёт «вторым краем» неизвестно откуда
    check('лишнего выбора после протяжки нет', ctx.picker.getRange().to === to);
}

// --- 8. Перелистывание месяца ------------------------------------------------

console.log('\n8. Перелистывание месяца не закрывает окно');
{
    const ctx = makePicker();
    openPicker(ctx);
    const monthBefore = ctx.q('[data-dr-month]').getAttribute('data-dr-month');
    const next = ctx.q('[data-dr-next]');
    bubbleClick(ctx, next, [ctx.cals()]);

    check('окно осталось открытым', ctx.pop().hidden === false);
    const monthAfter = ctx.q('[data-dr-month]').getAttribute('data-dr-month');
    check('месяц сменился', monthAfter !== monthBefore, monthBefore + ' -> ' + monthAfter);
    check('запроса не было', ctx.calls.length === 0);
}

// --- 9. Ручной ввод ----------------------------------------------------------

console.log('\n9. Поля «с» и «по»');
{
    const ctx = makePicker();
    openPicker(ctx);
    ctx.inputFrom().value = '12.09.2026';
    ctx.inputFrom().fire('change');
    check('одна граница — запроса нет', ctx.calls.length === 0);
    check('окно ждёт вторую дату', ctx.pop().hidden === false);

    ctx.inputTo().value = '20.09.2026';
    ctx.inputTo().fire('change');
    check('обе даты — один запрос', ctx.calls.length === 1, JSON.stringify(ctx.calls));
    check('границы верные',
        ctx.calls[0].from === '2026-09-12' && ctx.calls[0].to === '2026-09-20');

    // Конец раньше начала — это перепутанные поля, а не пустой список
    const swap = makePicker();
    openPicker(swap);
    swap.inputFrom().value = '20.09.2026';
    swap.inputTo().value = '12.09.2026';
    swap.inputTo().fire('change');
    check('перепутанные границы меняются местами',
        swap.calls.length === 1 && swap.calls[0].from === '2026-09-12'
        && swap.calls[0].to === '2026-09-20', JSON.stringify(swap.calls));

    // Опечатка не должна молча сбрасывать фильтр
    const bad = makePicker({ from: '2026-09-01', to: '2026-09-30' });
    openPicker(bad);
    bad.inputFrom().value = '31.02.2026';
    bad.inputFrom().fire('change');
    check('непонятая дата не уезжает в фильтр', bad.calls.length === 0);
    check('поле помечено', bad.q('[data-dr-wrap="from"]').className.includes('bxdr__inp--bad'),
        bad.q('[data-dr-wrap="from"]').className);
    check('прежний период цел', bad.picker.getRange().from === '2026-09-01');
}

// --- 10. Пресеты -------------------------------------------------------------

console.log('\n10. Готовые периоды');
{
    const ctx = makePicker();
    openPicker(ctx);
    const today = ctx.api.todayKey();
    const preset = ctx.q('[data-dr-preset="0"]');
    check('первый пресет — «Сегодня»', preset.textContent.trim() === 'Сегодня'
        || ctx.q('[data-dr-presets]').innerHTML.includes('Сегодня'));
    bubbleClick(ctx, preset, [ctx.q('[data-dr-presets]')]);
    check('пресет даёт период одним вызовом',
        ctx.calls.length === 1 && ctx.calls[0].from === today && ctx.calls[0].to === today,
        JSON.stringify(ctx.calls));
    check('окно закрылось', ctx.pop().hidden === true);
}

// --- 11. Сброс ---------------------------------------------------------------

console.log('\n11. Сброс периода');
{
    const ctx = makePicker({ from: '2026-09-12', to: '2026-09-20' });
    check('заданный период виден сразу', ctx.label() === '12.09 – 20.09.2026', ctx.label());
    bubbleClick(ctx, ctx.q('[data-dr-clear]'), [ctx.q('[data-dr-clear]')]);
    check('сброс — один вызов', ctx.calls.length === 1, JSON.stringify(ctx.calls));
    check('границы пусты', ctx.calls[0].from === '' && ctx.calls[0].to === '');
    check('подпись вернулась', ctx.label() === 'Все даты', ctx.label());

    // Лишний запрос на пустом фильтре — это поход на /data ни за чем
    bubbleClick(ctx, ctx.q('[data-dr-clear]'), [ctx.q('[data-dr-clear]')]);
    check('повторный сброс не зовёт onChange', ctx.calls.length === 1, JSON.stringify(ctx.calls));
}

// --- 12. Установка снаружи ---------------------------------------------------

console.log('\n12. setRange — это не выбор человека');
{
    const ctx = makePicker();
    ctx.picker.setRange('2026-09-01', '2026-09-30');
    check('onChange не звался', ctx.calls.length === 0);
    check('подпись обновилась', ctx.label() === '01.09 – 30.09.2026', ctx.label());
    check('getRange отдаёт заданное',
        ctx.picker.getRange().from === '2026-09-01' && ctx.picker.getRange().to === '2026-09-30');

    // Сброс фильтров в модуле зовёт setRange('', '') — и тоже не должен
    // превращаться в лишний запрос
    ctx.picker.setRange('', '');
    check('пустой setRange тоже молчит', ctx.calls.length === 0);
    check('подпись вернулась', ctx.label() === 'Все даты', ctx.label());
}

// --- 13. Два поля рядом ------------------------------------------------------

console.log('\n13. Открытие соседнего поля закрывает первое');
{
    const env = makeSandbox({ src: SRC });
    const api = env.sandbox.BarhatDateRange;
    const first = api.create({ mount: makeEl('div'), onChange: () => {} });
    const second = api.create({ mount: makeEl('div'), onChange: () => {} });
    first.open();
    check('первое открыто', first.isOpen() === true);
    second.open();
    check('второе открыто', second.isOpen() === true);
    check('первое закрылось', first.isOpen() === false);
}

// --- Итог --------------------------------------------------------------------

console.log('');
if (failures.length) {
    console.log(`ПРОВАЛЕНО: ${failures.length}`);
    failures.forEach(name => console.log('  - ' + name));
    process.exit(1);
}
console.log('Все проверки пройдены');
