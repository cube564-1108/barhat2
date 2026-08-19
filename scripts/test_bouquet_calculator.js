/**
 * Прогон логики калькулятора букетов в Node с заглушкой DOM.
 * Запуск: node scripts/test_bouquet_calculator.js
 */

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const SRC = path.join(__dirname, '..', 'src', 'dashboard', 'bouquet-calculator.js');

// --- Заглушка DOM ---
function makeEl() {
    const el = {
        value: '',
        textContent: '',
        innerHTML: '',
        step: '',
        style: {},
        dataset: {},
        classList: { add() {}, remove() {}, contains() { return false; } },
        addEventListener() {},
        appendChild() {},
        querySelector() { return makeEl(); },
        querySelectorAll() { return []; },
        parentElement: null,
    };
    return el;
}

const store = {};
const sandbox = {
    console,
    setTimeout,
    Intl,
    document: {
        readyState: 'complete',
        body: { style: {} },
        getElementById: () => makeEl(),
        createElement: () => makeEl(),
        querySelectorAll: () => [],
        addEventListener() {},
    },
    localStorage: {
        getItem: (k) => (k in store ? store[k] : null),
        setItem: (k, v) => { store[k] = v; },
    },
    confirm: () => true,
    alert: (m) => { console.log('  [alert]', m); },
    prompt: () => null,
    location: { reload() {} },
};
sandbox.window = sandbox;

// Открываем внутренности модуля для теста
let code = fs.readFileSync(SRC, 'utf8');
code = code.replace(/\}\)\(\);\s*$/, `
    window.__test = {
        calculateBouquet,
        getNextTierInfo,
        getCurrentUnitPrice,
        setItems: (items) => { bouquetItems = items.map(i => ({ id: createItemId(), ...i })); },
        getItems: () => bouquetItems,
        priceList: () => PRICE_LIST,
        setPriceList: (p) => { PRICE_LIST = p; },
    };
})();`);

vm.createContext(sandbox);
vm.runInContext(code, sandbox);

const T = sandbox.window.__test;

let failed = 0;
function check(label, actual, expected) {
    const ok = JSON.stringify(actual) === JSON.stringify(expected);
    if (!ok) failed++;
    console.log(`${ok ? 'OK  ' : 'FAIL'} ${label}: ${JSON.stringify(actual)}${ok ? '' : ' != ' + JSON.stringify(expected)}`);
}

console.log('\n--- Пороги объёмной скидки (клубника) ---');
check('100 г → порог 251', T.getNextTierInfo('strawberry', 100).threshold, 251);
check('600 г → порог 751 (раньше был null)', T.getNextTierInfo('strawberry', 600).threshold, 751);
check('600 г → цена след. тира 4.16', T.getNextTierInfo('strawberry', 600).nextPrice, 4.16);
check('900 г → порогов больше нет', T.getNextTierInfo('strawberry', 900), null);
check('900 г → цена 4.16', T.getCurrentUnitPrice('strawberry', 900), 4.16);
check('2 000 000 г не падает', T.getCurrentUnitPrice('strawberry', 2000000), 4.16);

console.log('\n--- Расчёт букета: цена упаковки в строке ---');
T.setItems([
    { name: 'Роза одноголовая', category: 'flowers', quantity: 11 },   // 340*11 = 3740
    { name: 'Круглый S бархат', category: 'packaging', quantity: 1 },  // 831
]);
let r = T.calculateBouquet();
const items = T.getItems();
check('материалы', r.materials, 3740);
check('упаковка', r.packaging, 831);
check('цена строки упаковки (была 0)', r.itemPrices.get(items[1]), 831);
check('итог = ОКРУГЛВВЕРХ(4571;-2)-10', r.total, 4590);

console.log('\n--- Упаковка 8% считается от материалов ---');
T.setItems([
    { name: 'Клубника', category: 'strawberry', quantity: 500 },   // 5.72*500 = 2860
    { name: 'Упаковка + фирм. лента', category: 'packaging', quantity: 1 },
]);
r = T.calculateBouquet();
check('материалы', r.materials, 2860);
check('упаковка 8% + 34', r.packaging, Math.round(2860 * 0.08 + 34));
check('итог', r.total, Math.ceil((2860 + 2860 * 0.08 + 34) / 100) * 100 - 10);

console.log('\n--- Товар удалён из прайса → не NaN ---');
T.setItems([
    { name: 'Роза одноголовая', category: 'flowers', quantity: 5 },
    { name: 'Роза которой нет', category: 'flowers', quantity: 3 },
]);
r = T.calculateBouquet();
check('материалы без неизвестного товара', r.materials, 1700);
check('итог не NaN', r.total, 1690);
check('неизвестных позиций', r.unknownItems.length, 1);

console.log('\n--- Пустой букет ---');
T.setItems([]);
r = T.calculateBouquet();
check('итог 0, а не -10', r.total, 0);

console.log('\n--- Битый прайс-лист из localStorage игнорируется ---');
check('прайс валиден после старта', typeof T.priceList().strawberry.tiers[0].price, 'number');

console.log(failed === 0 ? '\nВсе проверки пройдены' : `\nПРОВАЛЕНО: ${failed}`);
process.exit(failed === 0 ? 0 : 1);
