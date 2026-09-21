/**
 * Прогон общих диалогов дашборда (src/dashboard/ui-dialog.js) в Node с
 * заглушкой DOM.
 *
 * ЗАЧЕМ отдельный сторож: этот файл грузят ВСЕ модули дашборда, и любая
 * правка в нём ломает их разом. 21.09.2026 в него добавился диалог выбора
 * (BarhatUI.choice) для ручной смены статуса счёта, и при этом переписалась
 * общая ветка «диалог с полем»: подтверждение и ввод текста обязаны остаться
 * ровно такими же.
 *
 * Главная ловушка выбора — `input.select()`. У <input> этот метод есть, у
 * <select> его НЕТ: общий код, зовущий его вслепую, роняет весь диалог, и
 * кнопка «Сменить статус» не делает ничего. Заглушка это воспроизводит —
 * select() определён только у INPUT, как в браузере.
 *
 * Разделы:
 *   1. confirm отвечает true/false, а не значением
 *   2. prompt отдаёт введённое и null при отказе
 *   3. choice: список, значение по умолчанию, ответ и отказ
 *   4. choice не зовёт select() у <select> и не падает
 *   5. Пустой список не открывает диалог
 *   6. Закрытый диалог убирает себя из DOM
 *
 * Запуск: node scripts/test_ui_dialog.js
 */

const path = require('path');

const { makeSandbox: createSandbox } = require('./lib/dom_stub.js');

const SRC = path.join(__dirname, '..', 'src', 'dashboard', 'ui-dialog.js');

const failures = [];

function check(name, condition, detail) {
    const mark = condition ? 'OK  ' : 'FAIL';
    console.log(`  [${mark}] ${name}${detail ? ' — ' + detail : ''}`);
    if (!condition) failures.push(name);
}

function boot() {
    const { sandbox, doc } = createSandbox({
        src: SRC,
        fetch: async () => ({ ok: true, status: 200, json: async () => ({}) }),
        extras: {
            // ui-dialog.js на загрузке делает window.confirm.bind(window)
            confirm: () => false,
            prompt: () => null,
            top: {},
        },
    });

    // Браузерная правда: select() есть у <input> и нет у <select>. Без этого
    // заглушка пропустила бы ровно ту ошибку, ради которой сторож написан.
    const createElement = doc.createElement;
    doc.createElement = (tag) => {
        const el = createElement(tag);
        if (String(tag).toLowerCase() === 'input') el.select = () => { el._selectCalled = true; };
        return el;
    };

    return { sandbox, doc };
}

/** Узлы открытого сейчас диалога: последний слой в body. */
function opened(doc) {
    const backdrop = doc.body.children[doc.body.children.length - 1];
    if (!backdrop) return null;
    const dialog = backdrop.children[0];
    const actions = dialog.children[dialog.children.length - 1];
    const field = dialog.children.find(c => c.tagName === 'INPUT' || c.tagName === 'SELECT') || null;
    return {
        backdrop, dialog, field,
        cancelBtn: actions.children[0],
        confirmBtn: actions.children[actions.children.length - 1],
    };
}

async function main() {
    console.log('=== Общие диалоги дашборда ===\n');
    const { sandbox, doc } = boot();
    const UI = sandbox.BarhatUI;

    console.log('1. confirm отвечает true/false');
    let promise = UI.confirm('Точно?', { title: 'Проверка' });
    let nodes = opened(doc);
    check('поля в подтверждении нет', nodes.field === null,
          nodes.field ? nodes.field.tagName : 'нет');
    nodes.confirmBtn.click();
    check('подтверждение даёт true', (await promise) === true);

    promise = UI.confirm('Точно?');
    opened(doc).cancelBtn.click();
    check('отказ даёт false, а не null', (await promise) === false);

    console.log('\n2. prompt отдаёт введённое');
    promise = UI.prompt('Причина:', 'по умолчанию');
    nodes = opened(doc);
    check('поле ввода создано', nodes.field && nodes.field.tagName === 'INPUT',
          nodes.field ? nodes.field.tagName : 'нет');
    check('значение по умолчанию подставлено', nodes.field.value === 'по умолчанию', nodes.field.value);
    check('текст в поле выделен', nodes.field._selectCalled === true);
    nodes.field.value = 'нет скана';
    nodes.confirmBtn.click();
    check('вернулось введённое', (await promise) === 'нет скана');

    promise = UI.prompt('Причина:', '');
    opened(doc).cancelBtn.click();
    check('отказ от ввода даёт null', (await promise) === null);

    console.log('\n3. choice: список и ответ');
    const options = [
        { value: 'on_approval', label: 'На согласовании' },
        { value: 'approved', label: 'Согласован' },
        { value: 'paid', label: 'Оплачен' },
    ];
    promise = UI.choice('Куда двигаем?', options, { title: 'Сменить статус', defaultValue: 'approved' });
    nodes = opened(doc);
    check('создан <select>', nodes.field && nodes.field.tagName === 'SELECT',
          nodes.field ? nodes.field.tagName : 'нет');
    check('пунктов столько же, сколько вариантов', nodes.field.children.length === 3,
          String(nodes.field.children.length));
    check('подписи взяты из label',
          nodes.field.children.map(c => c.textContent).join('|') === 'На согласовании|Согласован|Оплачен',
          nodes.field.children.map(c => c.textContent).join('|'));
    check('значения взяты из value',
          nodes.field.children.map(c => c.value).join('|') === 'on_approval|approved|paid');
    check('выбран текущий статус', nodes.field.value === 'approved', nodes.field.value);
    nodes.field.value = 'paid';
    nodes.confirmBtn.click();
    check('вернулось выбранное значение', (await promise) === 'paid');

    promise = UI.choice('Куда двигаем?', options, { defaultValue: 'approved' });
    opened(doc).cancelBtn.click();
    check('отказ от выбора даёт null, а не false', (await promise) === null);

    console.log('\n4. choice не зовёт select() у <select>');
    // Если бы звал — диалог не открылся бы вовсе, и проверки выше упали бы
    // с TypeError. Отдельной строкой ради внятного сообщения.
    promise = UI.choice('Куда двигаем?', options);
    nodes = opened(doc);
    check('диалог выбора открылся', Boolean(nodes.field));
    check('select() у <select> не вызывался', nodes.field._selectCalled === undefined);
    nodes.cancelBtn.click();
    await promise;

    console.log('\n5. Пустой список не открывает диалог');
    const before = doc.body.children.length;
    const empty = await UI.choice('Выберите', []);
    check('ответ null', empty === null, String(empty));
    check('в DOM ничего не добавилось', doc.body.children.length === before,
          `${before} -> ${doc.body.children.length}`);

    console.log('\n6. Закрытый диалог убирает себя');
    promise = UI.confirm('Точно?');
    const layers = doc.body.children.length;
    opened(doc).confirmBtn.click();
    await promise;
    check('слой снят из body', doc.body.children.length === layers - 1,
          `${layers} -> ${doc.body.children.length}`);

    console.log('\n' + '='.repeat(60));
    if (failures.length) {
        console.log(`ПРОВАЛОВ: ${failures.length}`);
        failures.forEach(f => console.log('  - ' + f));
        process.exit(1);
    }
    console.log('Все проверки пройдены');
}

main().catch((error) => {
    console.error('Прогон упал:', error);
    process.exit(1);
});
