/**
 * Заглушка DOM для прогона модулей дашборда в Node.
 *
 * Модули рисуют разметку строкой и сразу вешают на неё обработчики, поэтому
 * innerHTML здесь не «чёрный ящик»: он разбирается в дерево элементов. Именно
 * так ловятся потерянные кнопки — без разбора тест был бы зелёным (см. историю
 * writeoffs и правило про перезапись UI-файлов в CLAUDE.md).
 *
 * Общий модуль: одна заглушка на все сторожа интерфейса. Копии расходятся, и
 * тогда «в одном тесте ловится, в другом нет» — а причина не в коде модуля.
 *
 * Использование:
 *   const { makeSandbox, makeEl, flush } = require('./lib/dom_stub.js');
 *   const env = makeSandbox({ src: '<путь к модулю>', fetch: fn, xhr: Cls });
 */

const fs = require('fs');
const vm = require('vm');

// --- Заглушка DOM ------------------------------------------------------------

/**
 * Разбор селекторов вида `.class`, `#id`, `tag`, `tag[attr]`, `[attr="v"]`.
 * Атрибутные нужны по-настоящему: модуль вешает обработчики через
 * `querySelectorAll('button[data-action]')` по разметке из innerHTML, и без
 * этого потерянный обработчик тест бы не заметил.
 */
function matches(el, selector) {
    const attrMatch = selector.match(/^([^[]*)\[([^\]=]+)(?:="([^"]*)")?\]$/);
    if (attrMatch) {
        const [, head, attr, value] = attrMatch;
        if (head && !matches(el, head)) return false;
        const actual = el.getAttribute(attr);
        if (actual === null) return false;
        return value === undefined || actual === value;
    }
    if (selector.startsWith('.')) return el.classList.contains(selector.slice(1));
    if (selector.startsWith('#')) return el.id === selector.slice(1);
    return el.tagName === selector.toUpperCase();
}

/**
 * Мини-разбор HTML в дерево элементов. Нужен, чтобы innerHTML не был «чёрным
 * ящиком»: модули дашборда рисуют разметку строкой и сразу вешают на неё
 * обработчики — именно там теряются кнопки (см. CLAUDE.md и историю правок).
 * Текстовые узлы не заводим, атрибуты берём только в двойных кавычках — этого
 * хватает на нашу разметку.
 */
const VOID_TAGS = new Set(['img', 'input', 'br', 'hr', 'meta', 'link']);

function parseHtml(html, makeElement) {
    const root = { children: [] };
    const stack = [root];
    const tagRe = /<(\/?)([a-zA-Z][\w-]*)((?:\s+[^\s=/>]+(?:="[^"]*")?)*)\s*(\/?)>/g;
    let m;
    while ((m = tagRe.exec(html)) !== null) {
        const [, closing, tag, rawAttrs, selfClose] = m;
        if (closing) {
            if (stack.length > 1) stack.pop();
            continue;
        }
        const el = makeElement(tag);
        const attrRe = /([^\s=/>]+)(?:="([^"]*)")?/g;
        let a;
        while ((a = attrRe.exec(rawAttrs || '')) !== null) {
            const name = a[1];
            const value = a[2] === undefined ? '' : a[2];
            el.setAttribute(name, value);
            if (name === 'class') el.className = value;
            else if (name === 'id') el.id = value;
            else if (name === 'hidden') el.hidden = true;
            else if (name === 'disabled') el.disabled = true;
            else if (name.startsWith('data-')) {
                const key = name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
                el.dataset[key] = value;
            }
        }
        const parent = stack[stack.length - 1];
        (parent.children = parent.children || []).push(el);
        el.parentElement = parent === root ? null : parent;
        if (!selfClose && !VOID_TAGS.has(tag.toLowerCase())) stack.push(el);
    }
    return root.children;
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
        set innerHTML(v) {
            el._html = String(v);
            el.children = parseHtml(el._html, makeEl);
            el.children.forEach(c => { c.parentElement = el; });
        },
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

function makeSandbox(options) {
    const { src, fetch: fetchImpl, xhr: xhrImpl, extras } = options;

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
        BarhatTime: {
            formatDateTimeLong: () => '', formatDate: () => '', formatDateTime: () => '',
            dayStartUtc: () => '', dayEndUtc: () => '',
        },
    };
    sandbox.window = sandbox;
    sandbox.self = sandbox;
    // BarhatImage намеренно НЕ подставляем в части тестов: модуль обязан работать
    // и без сжатия (старый браузер, файл не загрузился).
    sandbox.BarhatImage = {
        compress: async (f) => f,
        formatBytes: (b) => `${Math.round(b / 1024)} КБ`,
    };

    // extras — то, чего модулю не хватает сверх общего набора (BarhatUI,
    // свои глобальные функции). Кладём ДО createContext: после него объект
    // уже привязан к контексту, и добавленное туда попадёт не всегда
    if (extras) Object.assign(sandbox, extras);

    vm.createContext(sandbox);
    vm.runInContext(fs.readFileSync(src, 'utf8'), sandbox, { filename: src });

    // init висит на DOMContentLoaded
    (domListeners['DOMContentLoaded'] || []).forEach(fn => fn());

    // domListeners — слушатели, повешенные модулем на документ (клик мимо, Esc,
    // отпускание кнопки мыши). Без доступа к ним сторож не может разыграть
    // «кликнул в стороне» и «нажал Esc», а это ровно те пути, на которых
    // компоненты закрываются и применяют выбор.
    return { sandbox, byId, alerts, doc, domListeners };
}


/** Дать очереди микрозадач доехать: модули грузят данные через await. */
const flush = () => new Promise(r => setTimeout(r, 5));

module.exports = { makeSandbox, makeEl, matches, parseHtml, flush };
