/**
 * Сторож блока резервных копий в интерфейсе (src/dashboard/backup.js).
 *
 * Проверяет не «нарисовалось», а связки, на которых этот дашборд уже горел:
 * кнопка есть, но обработчика нет; getElementById есть, а узла в разметке нет;
 * скрипт подключён в index.html, а маршрута на сервере нет — и он отдаёт 404.
 *
 * Проверяет:
 *   1. Таблица рисуется, строки по базам и вложениям
 *   2. У каждой кнопки есть обработчик и он ведёт на верный адрес
 *   3. Базу без файла скачивать нечем — кнопки нет
 *   4. База вне /data помечена: её стирает сборка, бэкапить нечего
 *   5. Кавычка в данных не рвёт разметку
 *   6. Сбой запроса показывает причину, а не пустоту
 *   7. Все id из модуля есть в index.html
 *   8. Скрипт подключён в index.html и у него есть маршрут на сервере
 *
 * Запуск: node scripts/test_backup_ui.js
 */

const fs = require('fs');
const path = require('path');
const { makeSandbox, flush } = require('./lib/dom_stub.js');

const ROOT = path.join(__dirname, '..');
const SRC = path.join(ROOT, 'src', 'dashboard', 'backup.js');
const INDEX = path.join(ROOT, 'src', 'dashboard', 'index.html');
const SERVER = path.join(ROOT, 'src', 'pyrus', 'server.py');

const failures = [];
function check(title, ok, detail) {
    console.log(`  [${ok ? 'OK  ' : 'ПРОВАЛ'}] ${title}` + (detail ? ` — ${detail}` : ''));
    if (!ok) failures.push(title);
}

const TARGETS = {
    databases: [
        { name: 'barhat', note: 'учётки и счета', path: '/data/barhat.db',
          exists: true, size_mb: 12.5, persistent: true },
        { name: 'moysklad', note: 'зеркало «МойСклада»', path: '/data/moysklad.db',
          exists: true, size_mb: 1160, persistent: true },
        { name: 'pyrus', note: 'задачи', path: '/app/pyrus.db',
          exists: true, size_mb: 3, persistent: false },
        { name: 'linkwatch', note: 'ссылки', path: '/data/linkwatch.db',
          exists: false, size_mb: 0, persistent: true },
    ],
    attachments: [
        { name: 'invoices', note: 'вложения к счетам', files: 214, size_mb: 88, exists: true },
        { name: 'writeoffs', note: 'фото к списаниям', files: 0, size_mb: 0, exists: true },
    ],
};

const clicked = [];
const toasts = [];

function makeEnv(payload, ok = true) {
    const env = makeSandbox({
        src: SRC,
        fetch: async () => ({
            ok,
            status: ok ? 200 : 500,
            json: async () => payload,
        }),
        extras: {
            BarhatUI: { toast: (text) => toasts.push(String(text)) },
        },
    });
    // Ссылку на скачивание модуль создаёт и «нажимает» — перехватываем.
    const realCreate = env.doc.createElement;
    env.doc.createElement = (tag) => {
        const el = realCreate(tag);
        if (tag === 'a') el.click = () => clicked.push(el.href);
        return el;
    };
    return env;
}

(async () => {
    console.log('\n=== 1. Таблица и строки ===');
    const env = makeEnv(TARGETS);
    env.sandbox.BackupModule.onPageActivated();
    await flush();

    const host = env.byId['backup-list'];
    const html = host.innerHTML;
    check('Таблица нарисована', html.includes('<table'), html.slice(0, 60));
    ['barhat', 'moysklad', 'pyrus', 'invoices', 'writeoffs'].forEach((name) => {
        check(`Строка «${name}» есть`, html.includes(`<strong>${name}</strong>`));
    });
    check('Размер в гигабайтах для большой базы', html.includes('1.13 ГБ'),
          (html.match(/[\d.]+ ГБ/) || ['нет'])[0]);

    console.log('\n=== 2. Кнопки связаны с обработчиками ===');
    const buttons = host.querySelectorAll('.bk-download');
    check('Кнопок скачивания столько же, сколько доступных целей',
          buttons.length === 4, `кнопок ${buttons.length}, ожидалось 4`);

    clicked.length = 0;
    buttons.forEach((b) => b.dispatchEvent
        ? b.dispatchEvent({ type: 'click' })
        : (b.listeners && (b.listeners.click || []).forEach((fn) => fn({}))));
    check('Каждая кнопка что-то скачивает', clicked.length === buttons.length,
          `нажатий ${buttons.length}, скачиваний ${clicked.length}`);
    check('Адрес базы верный', clicked.includes('/api/backup/db/barhat'));
    check('Адрес вложений верный', clicked.includes('/api/backup/attachments/invoices'));
    check('Про большой файл предупреждаем',
          toasts.some((t) => t.includes('Файл большой')), toasts.join(' | '));

    console.log('\n=== 3. Нет файла — нет кнопки ===');
    check('У отсутствующей базы кнопки нет', !html.includes('data-name="linkwatch"'));
    check('Отсутствие файла подписано', html.includes('нет файла'));
    check('У пустой папки вложений кнопки нет', !html.includes('data-name="writeoffs"'));

    console.log('\n=== 4. База вне постоянного диска помечена ===');
    // Файл вне /data стирается каждой сборкой: бэкапить там нечего, и это
    // надо видеть до того, как копия окажется пустой.
    check('Пометка «вне /data» стоит', html.includes('вне /data'));
    check('Пометка только у той базы, что вне диска',
          (html.match(/вне \/data/g) || []).length === 1);

    console.log('\n=== 5. Кавычка в данных не рвёт разметку ===');
    // Трюк с textContent кавычку не экранирует и молча режет value="..."
    // (инцидент 2026-08-18) — здесь свой escapeHtml, проверяем его делом.
    const env2 = makeEnv({
        databases: [{ name: 'ba"d', note: 'под "кавычками" <b>', path: '/data/x.db',
                      exists: true, size_mb: 1, persistent: true }],
        attachments: [],
    });
    env2.sandbox.BackupModule.onPageActivated();
    await flush();
    const html2 = env2.byId['backup-list'].innerHTML;
    check('Кавычка экранирована', html2.includes('ba&quot;d'), html2.slice(0, 120));
    check('Тег из данных не стал тегом', !html2.includes('<b>'));

    console.log('\n=== 6. Сбой запроса объясняет себя ===');
    const env3 = makeEnv(null, false);
    env3.sandbox.BackupModule.onPageActivated();
    await flush();
    const html3 = env3.byId['backup-list'].innerHTML;
    check('Показана причина, а не пустой блок',
          html3.includes('Не удалось получить список') && html3.includes('500'), html3);

    console.log('\n=== 7. Все id из модуля есть в разметке ===');
    const source = fs.readFileSync(SRC, 'utf8');
    const indexHtml = fs.readFileSync(INDEX, 'utf8');
    const ids = [...source.matchAll(/getElementById\('([^']+)'\)/g)].map((m) => m[1]);
    check('Идентификаторы найдены в модуле', ids.length > 0, ids.join(', '));
    ids.forEach((id) => {
        check(`id="${id}" есть в index.html`, indexHtml.includes(`id="${id}"`));
    });

    console.log('\n=== 8. Скрипт подключён и отдаётся сервером ===');
    // Разметка и обработчики бесполезны, если файл не доезжает до браузера:
    // общего маршрута на статику тут нет, у каждого скрипта свой.
    check('Подключён в index.html', indexHtml.includes('src="/backup.js"'));
    const serverSource = fs.readFileSync(SERVER, 'utf8');
    check('Есть маршрут /backup.js на сервере',
          serverSource.includes("@app.route('/backup.js')"));
    check('Маршрут отдаёт нужный файл', serverSource.includes("'backup.js'"));

    console.log('\n' + '='.repeat(60));
    if (failures.length) {
        console.log(`ПРОВАЛЕНО: ${failures.length}`);
        failures.forEach((t) => console.log(`  - ${t}`));
        process.exit(1);
    }
    console.log('Все проверки пройдены');
})();
