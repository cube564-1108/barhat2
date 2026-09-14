/**
 * Прогон формы создания списания в Node с заглушкой DOM.
 *
 * Проверяется поведение, из-за которого и написано обращение #7. Раньше форма
 * закрывалась НЕЗАВИСИМО от того, доехало фото или нет: uploadPositionPhoto
 * гасил ошибку внутри себя, а submitWriteoff всё равно звал closeCreateModal().
 * Человек оставался с созданной заявкой без фото — согласовать её было нельзя,
 * а дозалить фото было нечем.
 *
 * Запуск: node scripts/test_writeoff_form.js
 */

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const SRC = path.join(__dirname, '..', 'src', 'dashboard', 'writeoffs.js');

const failures = [];

function check(name, condition, detail) {
    const mark = condition ? 'OK  ' : 'FAIL';
    console.log(`  [${mark}] ${name}${detail ? ' — ' + detail : ''}`);
    if (!condition) failures.push(name);
}

// --- Заглушка DOM ------------------------------------------------------------

function matches(el, selector) {
    if (selector.startsWith('.')) return el.classList.contains(selector.slice(1));
    if (selector.startsWith('#')) return el.id === selector.slice(1);
    return el.tagName === selector.toUpperCase();
}

function makeEl(tagName) {
    const classes = new Set();
    const el = {
        tagName: String(tagName || 'div').toUpperCase(),
        id: '',
        children: [],
        listeners: {},
        style: { cssText: '' },
        dataset: {},
        value: '',
        textContent: '',
        hidden: false,
        disabled: false,
        files: [],
        _html: '',
        attributes: {},
        classList: {
            add: (...c) => c.forEach(x => classes.add(x)),
            remove: (...c) => c.forEach(x => classes.delete(x)),
            contains: (c) => classes.has(c),
            toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)),
        },
        get className() { return Array.from(classes).join(' '); },
        set className(v) {
            classes.clear();
            String(v).split(/\s+/).filter(Boolean).forEach(c => classes.add(c));
        },
        get innerHTML() { return el._html; },
        set innerHTML(v) { el._html = String(v); el.children = []; },
        appendChild(child) { el.children.push(child); child.parentElement = el; return child; },
        removeChild(child) { el.children = el.children.filter(c => c !== child); },
        remove() { if (el.parentElement) el.parentElement.removeChild(el); },
        contains(node) {
            if (node === el) return true;
            return el.children.some(c => c.contains && c.contains(node));
        },
        setAttribute(k, v) { el.attributes[k] = String(v); },
        getAttribute(k) { return k in el.attributes ? el.attributes[k] : null; },
        addEventListener(type, fn) { (el.listeners[type] = el.listeners[type] || []).push(fn); },
        removeEventListener() {},
        focus() {}, blur() {}, click() { el.fire('click'); },
        setSelectionRange() {},
        getBoundingClientRect: () => ({ top: 0, left: 0, bottom: 0, right: 0, width: 0, height: 0 }),
        scrollIntoView() {},
        querySelector(sel) { return el.querySelectorAll(sel)[0] || null; },
        querySelectorAll(sel) {
            const out = [];
            (function walk(node) {
                for (const c of node.children) {
                    if (matches(c, sel)) out.push(c);
                    walk(c);
                }
            })(el);
            return out;
        },
        fire(type, event) {
            (el.listeners[type] || []).forEach(fn => fn(event || { preventDefault() {}, stopPropagation() {} }));
        },
    };
    el.parentElement = null;
    return el;
}

function makeSandbox(fetchImpl, xhrImpl) {
    const byId = {};
    const domListeners = {};

    const doc = {
        readyState: 'loading',
        body: makeEl('body'),
        head: makeEl('head'),
        documentElement: makeEl('html'),
        createElement: (tag) => makeEl(tag),
        // Любой запрошенный id существует: разметку сверяет отдельная проверка
        // (все getElementById из модуля есть в index.html), здесь нас интересует
        // поведение, а не наличие узлов.
        getElementById: (id) => {
            if (!byId[id]) { byId[id] = makeEl('div'); byId[id].id = id; }
            return byId[id];
        },
        querySelector: () => null,
        querySelectorAll: () => [],
        addEventListener: (t, fn) => { (domListeners[t] = domListeners[t] || []).push(fn); },
        removeEventListener: () => {},
    };

    const alerts = [];
    const sandbox = {
        console: { log() {}, error() {}, warn() {} },
        setTimeout, clearTimeout, Math, Date, Object, Array, Promise, JSON, String, Number,
        Error, Set, Map, isNaN, parseInt, parseFloat, encodeURIComponent, Intl, RegExp, Boolean,
        URLSearchParams,
        URL: { createObjectURL: () => 'blob:fake', revokeObjectURL: () => {} },
        FormData: class { constructor() { this.entries = []; } append(k, v) { this.entries.push([k, v]); } },
        XMLHttpRequest: xhrImpl,
        fetch: fetchImpl,
        alert: (m) => alerts.push(String(m)),
        document: doc,
        BarhatTime: { formatDateTimeLong: () => '', formatDate: () => '' },
    };
    sandbox.window = sandbox;
    sandbox.self = sandbox;
    // BarhatImage намеренно НЕ подставляем в части тестов: модуль обязан работать
    // и без сжатия (старый браузер, файл не загрузился).
    sandbox.BarhatImage = {
        compress: async (f) => f,
        formatBytes: (b) => `${Math.round(b / 1024)} КБ`,
    };

    vm.createContext(sandbox);
    vm.runInContext(fs.readFileSync(SRC, 'utf8'), sandbox, { filename: 'writeoffs.js' });

    // init висит на DOMContentLoaded
    (domListeners['DOMContentLoaded'] || []).forEach(fn => fn());

    return { sandbox, byId, alerts, doc };
}

function fakeFile(name, size, lastModified) {
    return { name, size, lastModified, type: 'image/jpeg' };
}

/** XHR, который отдаёт заданный код ответа на загрузку фото. */
function makeXhr(statusFor) {
    const sent = [];
    class FakeXHR {
        constructor() { this.upload = {}; this.status = 0; this.responseText = ''; }
        open(method, url) { this.method = method; this.url = url; }
        send() {
            sent.push(this.url);
            const status = statusFor(this.url, sent.length);
            this.status = status;
            this.responseText = status >= 400
                ? JSON.stringify({ error: 'Файл пустой — загрузите фото заново' })
                : JSON.stringify({ ok: true, photo: { id: sent.length } });
            if (this.upload.onprogress) {
                this.upload.onprogress({ lengthComputable: true, loaded: 50, total: 100 });
            }
            setTimeout(() => this.onload(), 0);
        }
    }
    return { FakeXHR, sent };
}

/** Довести форму до состояния «можно отправлять»: точка, позиция, фото. */
async function prepareForm(env, files) {
    const { byId, sandbox } = env;

    byId['writeoff-store'].value = '1';

    const row = makeEl('div');
    row.className = 'writeoff-position-row';
    const product = makeEl('input');
    product.className = 'writeoff-position-product';
    product.dataset.productId = 'p-1';
    product.dataset.productName = 'Роза';
    product.dataset.uomName = 'шт';
    const qty = makeEl('input');
    qty.className = 'writeoff-position-qty';
    qty.value = '3';
    const reason = makeEl('input');
    reason.className = 'writeoff-position-reason';
    reason.value = 'увядание';
    row.appendChild(product); row.appendChild(qty); row.appendChild(reason);
    byId['writeoff-positions-rows'].appendChild(row);

    byId['writeoff-photo-input'].files = files;
    byId['writeoff-photo-input'].fire('change');

    // Модалку считаем открытой
    byId['create-writeoff-modal'].classList.add('active');
    return sandbox;
}

/**
 * Ждём, пока отправка реально завершится, а не фиксированные N мс: иначе
 * проверка «модалка ещё открыта» пройдёт просто потому, что поток не доехал
 * до конца, и тест будет зелёным по ошибке.
 */
async function settle(env, timeoutMs = 2000) {
    const btn = env.byId['confirm-create-writeoff-btn'];
    const started = Date.now();
    while (Date.now() - started < timeoutMs) {
        await new Promise(r => setTimeout(r, 5));
        if (!btn.disabled) return;   // finally{} снимает блокировку последним
    }
}

const flush = () => new Promise(r => setTimeout(r, 5));

// --- Тесты -------------------------------------------------------------------

(async () => {
    console.log('\n=== 1. Сбой загрузки фото НЕ закрывает форму ===');
    {
        const { FakeXHR, sent } = makeXhr(() => 500);
        const env = makeSandbox(
            async (url) => ({
                ok: true, status: 201,
                json: async () => ({ writeoff: { id: 77, positions: [{ id: 1 }] } }),
            }),
            FakeXHR
        );
        await prepareForm(env, [fakeFile('kadr.jpg', 300000, 1)]);

        env.byId['confirm-create-writeoff-btn'].fire('click');
        await settle(env);

        const modal = env.byId['create-writeoff-modal'];
        const recovery = env.byId['writeoff-create-recovery'];
        check('Попытка загрузки была', sent.length === 1, `запросов: ${sent.length}`);
        check('Модалка осталась открытой', modal.classList.contains('active'));
        check('Показан блок восстановления', recovery.hidden === false);
        check('В нём есть номер заявки', /№77/.test(recovery.innerHTML),
            recovery.innerHTML.slice(0, 0) || 'см. разметку');
        check('И кнопка повтора', /writeoff-retry-photos/.test(recovery.innerHTML));
        check('Человеку сказано, что случилось', env.alerts.length > 0, env.alerts[0]);
    }

    console.log('\n=== 2. Успешная загрузка закрывает форму ===');
    {
        const { FakeXHR, sent } = makeXhr(() => 201);
        const env = makeSandbox(
            async (url) => ({
                ok: true, status: 201,
                json: async () => ({ writeoff: { id: 78, positions: [{ id: 1 }] } }),
            }),
            FakeXHR
        );
        await prepareForm(env, [fakeFile('kadr.jpg', 300000, 1)]);

        env.byId['confirm-create-writeoff-btn'].fire('click');
        await settle(env);

        check('Фото ушло', sent.length === 1);
        check('Модалка закрыта', !env.byId['create-writeoff-modal'].classList.contains('active'));
        check('Блок восстановления не показан', env.byId['writeoff-create-recovery'].hidden === true);
        check('Никаких ошибок человеку', env.alerts.length === 0, env.alerts.join('; '));
    }

    console.log('\n=== 3. Один кадр — одна загрузка ===');
    {
        const { FakeXHR, sent } = makeXhr(() => 201);
        const env = makeSandbox(
            async () => ({ ok: true, status: 201, json: async () => ({ writeoff: { id: 79, positions: [] } }) }),
            FakeXHR
        );
        // Тот же файл выбран дважды — раньше он уезжал по разу на каждую позицию
        const same = fakeFile('IMG_0042.jpg', 4000000, 111);
        await prepareForm(env, [same, fakeFile('vtoroy.jpg', 300000, 222)]);
        env.byId['writeoff-photo-input'].files = [same];
        env.byId['writeoff-photo-input'].fire('change');

        env.byId['confirm-create-writeoff-btn'].fire('click');
        await settle(env);

        check('Загрузок ровно две, а не три', sent.length === 2, `запросов: ${sent.length}`);
        check('Обе — в одну заявку', sent.every(u => u === '/api/writeoffs/79/photos'), sent.join(', '));
    }

    console.log('\n=== 4. Без фото заявка не создаётся вовсе ===');
    {
        const { FakeXHR, sent } = makeXhr(() => 201);
        let created = 0;
        const env = makeSandbox(
            async (url, opts) => {
                if (url === '/api/writeoffs' && opts && opts.method === 'POST') created += 1;
                return { ok: true, status: 201, json: async () => ({ writeoff: { id: 80, positions: [] } }) };
            },
            FakeXHR
        );
        await prepareForm(env, []);

        env.byId['confirm-create-writeoff-btn'].fire('click');
        await settle(env);

        check('Запроса на создание не было', created === 0, `создано: ${created}`);
        check('Фото не грузилось', sent.length === 0);
        check('Человеку сказано приложить фото',
            env.alerts.some(a => /фото/i.test(a)), env.alerts.join('; '));
        check('Модалка осталась открытой',
            env.byId['create-writeoff-modal'].classList.contains('active'));
    }

    console.log('\n=== 5. Частичный сбой: что доехало — то доехало ===');
    {
        // Первое фото проходит, второе падает
        const { FakeXHR, sent } = makeXhr((url, n) => (n === 1 ? 201 : 500));
        const env = makeSandbox(
            async () => ({ ok: true, status: 201, json: async () => ({ writeoff: { id: 81, positions: [] } }) }),
            FakeXHR
        );
        await prepareForm(env, [fakeFile('a.jpg', 100, 1), fakeFile('b.jpg', 100, 2)]);

        env.byId['confirm-create-writeoff-btn'].fire('click');
        await settle(env);

        check('Обе попытки сделаны', sent.length === 2, `запросов: ${sent.length}`);
        check('Форма не закрыта — есть что дозалить',
            env.byId['create-writeoff-modal'].classList.contains('active'));
        check('Показан блок восстановления', env.byId['writeoff-create-recovery'].hidden === false);
    }

    console.log('\n=== 6. Кнопка не даёт нажать себя дважды ===');
    {
        let resolveCreate;
        const { FakeXHR } = makeXhr(() => 201);
        // Подвешиваем ТОЛЬКО создание заявки. Обновление списка после него
        // должно отвечать сразу, иначе тест виснет на своей же заглушке и
        // выглядит как незакрытая блокировка кнопки.
        const env = makeSandbox(
            (url, opts) => {
                if (url === '/api/writeoffs' && opts && opts.method === 'POST') {
                    return new Promise(r => { resolveCreate = () => r({
                        ok: true, status: 201,
                        json: async () => ({ writeoff: { id: 82, positions: [] } }),
                    }); });
                }
                return Promise.resolve({
                    ok: true, status: 200,
                    json: async () => ({ writeoffs: [], stores: [], user: { role: 'florist' } }),
                });
            },
            FakeXHR
        );
        await prepareForm(env, [fakeFile('a.jpg', 100, 1)]);

        const btn = env.byId['confirm-create-writeoff-btn'];
        btn.fire('click');
        // Здесь ждём коротко и намеренно: запрос на создание ещё висит, и
        // settle() (он ждёт разблокировки) впустую крутился бы до таймаута.
        await flush();
        check('Пока идёт отправка, кнопка заблокирована', btn.disabled === true);
        resolveCreate();
        await settle(env);
        check('После завершения — разблокирована', btn.disabled === false);
    }

    console.log('\n=== 7. Модуль переживает отсутствие сжатия ===');
    {
        const { FakeXHR, sent } = makeXhr(() => 201);
        const env = makeSandbox(
            async () => ({ ok: true, status: 201, json: async () => ({ writeoff: { id: 83, positions: [] } }) }),
            FakeXHR
        );
        delete env.sandbox.BarhatImage;  // старый браузер / файл не загрузился
        await prepareForm(env, [fakeFile('a.jpg', 100, 1)]);

        env.byId['confirm-create-writeoff-btn'].fire('click');
        await settle(env);

        check('Фото всё равно ушло — оригиналом', sent.length === 1, `запросов: ${sent.length}`);
        check('Форма закрылась', !env.byId['create-writeoff-modal'].classList.contains('active'));
    }

    console.log('\n' + '='.repeat(60));
    if (failures.length) {
        console.log(`ПРОВАЛЕНО проверок: ${failures.length}`);
        failures.forEach(n => console.log(`  - ${n}`));
        process.exit(1);
    }
    console.log('Все проверки прошли.');
})();
