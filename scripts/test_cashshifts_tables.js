/**
 * Прогон таблиц модуля кассовых смен в Node с заглушкой DOM.
 *
 * 19.09.2026 «История смен» и «Инкассации по салонам» стали постраничными.
 * Проверяется то, что ломается именно от пагинации и чего не видит бэкенд:
 * какая страница запрошена, куда возвращает фильтр, остаётся ли кнопка
 * «Исправить» только у самой свежей смены (index === 0 на второй странице —
 * это смена месячной давности), и не уходит ли экран в цикл перезапросов.
 *
 * Разделы:
 *   1. История смен: запрошена страница, номера, переход, подпись
 *   2. Инкассации: своя страница, независимая от истории смен
 *   3. Фильтры и смена салона возвращают на первую страницу
 *   4. «Исправить» у флориста — только на первой строке ПЕРВОЙ страницы
 *   5. Новая инкассация и закрытие смены показывают первую страницу
 *   6. Пустой ответ при живом счётчике не запускает долбёжку
 *   7. Итог по деньгам не путается с числом строк
 *
 * Запуск: node scripts/test_cashshifts_tables.js
 */

const path = require('path');

const { makeSandbox: createSandbox, flush } = require('./lib/dom_stub.js');

const SRC = path.join(__dirname, '..', 'src', 'dashboard', 'cash-shifts.js');

const failures = [];

function check(name, condition, detail) {
    const mark = condition ? 'OK  ' : 'FAIL';
    console.log(`  [${mark}] ${name}${detail ? ' — ' + detail : ''}`);
    if (!condition) failures.push(name);
}

/**
 * Поддельный бэкенд обеих таблиц. Отдаёт ровно то, что отдаёт настоящий:
 * страницу, total (у смен — строки, у инкассаций — ДЕНЬГИ) и total_count.
 */
function makeBackend(options = {}) {
    const shiftsTotal = options.shiftsTotal ?? 60;
    const collectionsTotal = options.collectionsTotal ?? 40;
    const collectionsAmount = options.collectionsAmount ?? 123456;
    const asked = { shifts: [], collections: [] };
    const state = { shiftsTotal, collectionsTotal };

    const fetchImpl = async (url, opts) => {
        const method = (opts && opts.method) || 'GET';

        if (url.startsWith('/api/cash-shifts/stores')) {
            return json({ stores: [{ id: 1, name: 'Салон А' }, { id: 2, name: 'Салон Б' }] });
        }
        if (url.startsWith('/api/cash-shifts/categories')) {
            return json({ categories: [{ id: 1, name: 'Инкассация' }] });
        }
        if (/\/api\/cash-shifts\/open\/\d+/.test(url)) {
            // Открытая смена выбранной точки — без неё нечем добавить инкассацию
            return json({
                shift: {
                    id: 777, store_id: 1, shift_type: 'day', status: 'open',
                    datetime_start: '2026-09-02 08:00:00', opening_balance: 1000,
                },
                collections: [], collections_total: 0,
            });
        }
        if (url.startsWith('/api/cash-shifts/open')) {
            return json({ shifts: [], duplicates: [] });
        }
        if (url.startsWith('/api/cash-shifts/collections')) {
            const params = new URLSearchParams(url.split('?')[1] || '');
            const limit = parseInt(params.get('limit') || '0', 10);
            const offset = parseInt(params.get('offset') || '0', 10);
            asked.collections.push({ limit, offset, store: params.get('store_id') });
            const rows = [];
            for (let i = offset; i < Math.min(offset + limit, state.collectionsTotal); i++) {
                rows.push({
                    id: i + 1, date: '2026-09-01 10:00:00', store_name: 'Салон А',
                    amount: 100, category_name: 'Инкассация', created_by: 'florist',
                });
            }
            return json({
                collections: rows,
                by_store: [{ store_id: 1, store_name: 'Салон А', count: state.collectionsTotal, total: collectionsAmount }],
                total: collectionsAmount,          // ДЕНЬГИ
                total_count: state.collectionsTotal,  // строки
                limit, offset,
            });
        }
        if (url.startsWith('/api/cash-shifts?')) {
            const params = new URLSearchParams(url.split('?')[1] || '');
            const limit = parseInt(params.get('limit') || '0', 10);
            const offset = parseInt(params.get('offset') || '0', 10);
            asked.shifts.push({ limit, offset, store: params.get('store_id') });
            const rows = [];
            for (let i = offset; i < Math.min(offset + limit, state.shiftsTotal); i++) {
                rows.push({
                    id: i + 1, store_id: 1, shift_type: 'day', status: 'closed',
                    datetime_start: '2026-09-01 08:00:00', closed_at: '2026-09-01 20:00:00',
                    opening_balance: 1000, actual_balance: 1200, cash_orders_total: 500,
                    collections_total: 300, discrepancy: 0,
                });
            }
            return json({ shifts: rows, count: rows.length, total: state.shiftsTotal });
        }
        if (method === 'POST' || method === 'PUT' || method === 'DELETE') {
            return json({ ok: true });
        }
        return json({});
    };

    function json(payload) {
        return { ok: true, status: 200, json: async () => Object.assign({ success: true }, payload) };
    }

    return { fetchImpl, asked, state };
}

function makeEnv(backend, user) {
    const env = createSandbox({
        src: SRC,
        fetch: backend.fetchImpl,
        extras: {
            BarhatUI: { confirm: async () => true, prompt: async () => '', toast() {} },
            BarhatTime: {
                formatDate: () => '01.09.2026', formatTime: () => '08:00',
                formatDateTime: () => '01.09.2026 08:00', formatDateTimeLong: () => '',
                dayStartUtc: (d) => `${d} 00:00:00`, dayEndUtc: (d) => `${d} 23:59:59`,
            },
        },
    });
    env.sandbox.CashShiftsModule.onPageActivated(user);
    return env;
}

const lastAsk = (list) => list[list.length - 1];

(async () => {
    console.log('\n=== 1. История смен: запрошена страница, а не вся история ===');
    {
        const backend = makeBackend();
        const env = makeEnv(backend, { username: 'admin_cs', role: 'admin' });
        await flush();

        check('Ушёл запрос на 25 строк с нулевым смещением',
            backend.asked.shifts[0] && backend.asked.shifts[0].limit === 25
            && backend.asked.shifts[0].offset === 0,
            JSON.stringify(backend.asked.shifts[0]));
        check('В таблице 25 строк',
            env.byId['shifts-tbody'].querySelectorAll('tr').length === 25,
            `строк: ${env.byId['shifts-tbody'].querySelectorAll('tr').length}`);
        check('Навигация показана', env.byId['shifts-pagination'].style.display === 'flex',
            env.byId['shifts-pagination'].style.display);
        check('Подписано, что видно и сколько всего',
            env.byId['shifts-pagination-info'].textContent === '1–25 из 60',
            env.byId['shifts-pagination-info'].textContent);
        check('Кнопка «назад» на первой странице выключена',
            env.byId['shifts-pagination-pages']
                .querySelectorAll('button[data-page="-1"]')[0]?.disabled === true);

        env.byId['shifts-pagination-pages'].querySelectorAll('button[data-page="2"]')[0].fire('click');
        await flush();
        check('Переход на третью страницу — смещение 50',
            lastAsk(backend.asked.shifts).offset === 50,
            JSON.stringify(lastAsk(backend.asked.shifts)));
        check('На последней странице остаток — 10 строк',
            env.byId['shifts-tbody'].querySelectorAll('tr').length === 10,
            `строк: ${env.byId['shifts-tbody'].querySelectorAll('tr').length}`);
        check('Подпись пересчитана',
            env.byId['shifts-pagination-info'].textContent === '51–60 из 60',
            env.byId['shifts-pagination-info'].textContent);
    }

    console.log('\n=== 2. Инкассации: своя страница, независимая от истории смен ===');
    {
        const backend = makeBackend();
        const env = makeEnv(backend, { username: 'admin_cs', role: 'admin' });
        await flush();

        check('Инкассации тоже просят страницу',
            backend.asked.collections[0].limit === 25 && backend.asked.collections[0].offset === 0,
            JSON.stringify(backend.asked.collections[0]));
        check('Подпись по числу СТРОК, а не по сумме денег',
            env.byId['collections-pagination-info'].textContent === '1–25 из 40',
            env.byId['collections-pagination-info'].textContent);

        // Листаем инкассации — история смен остаться должна на своей странице
        const shiftsAsksBefore = backend.asked.shifts.length;
        env.byId['collections-pagination-pages'].querySelectorAll('button[data-page="1"]')[0].fire('click');
        await flush();
        check('Инкассации ушли на вторую страницу',
            lastAsk(backend.asked.collections).offset === 25,
            JSON.stringify(lastAsk(backend.asked.collections)));
        check('История смен при этом не перезапрашивалась',
            backend.asked.shifts.length === shiftsAsksBefore,
            `запросов истории: ${backend.asked.shifts.length - shiftsAsksBefore}`);
        check('И осталась на своей странице',
            env.byId['shifts-pagination-info'].textContent === '1–25 из 60',
            env.byId['shifts-pagination-info'].textContent);
    }

    console.log('\n=== 3. Фильтры возвращают на первую страницу ===');
    {
        const backend = makeBackend();
        const env = makeEnv(backend, { username: 'admin_cs', role: 'admin' });
        await flush();

        env.byId['shifts-pagination-pages'].querySelectorAll('button[data-page="2"]')[0].fire('click');
        env.byId['collections-pagination-pages'].querySelectorAll('button[data-page="1"]')[0].fire('click');
        await flush();

        env.byId['shift-date-filter'].value = '2026-09-01';
        env.byId['apply-date-filter'].fire('click');
        await flush();
        check('Фильтр по дате вернул историю смен на первую страницу',
            lastAsk(backend.asked.shifts).offset === 0,
            JSON.stringify(lastAsk(backend.asked.shifts)));

        env.byId['collections-store-filter'].value = '2';
        env.byId['apply-collections-filter'].fire('click');
        await flush();
        check('Фильтр инкассаций вернул на первую страницу',
            lastAsk(backend.asked.collections).offset === 0
            && lastAsk(backend.asked.collections).store === '2',
            JSON.stringify(lastAsk(backend.asked.collections)));

        env.byId['collections-pagination-pages'].querySelectorAll('button[data-page="1"]')[0].fire('click');
        await flush();
        env.byId['reset-collections-filter'].fire('click');
        await flush();
        check('Сброс фильтров — тоже первая страница',
            lastAsk(backend.asked.collections).offset === 0,
            JSON.stringify(lastAsk(backend.asked.collections)));

        // Смена салона в шапке меняет набор смен целиком
        env.byId['shifts-pagination-pages'].querySelectorAll('button[data-page="2"]')[0].fire('click');
        await flush();
        env.byId['shift-store-selector'].value = '2';
        env.byId['shift-store-selector'].fire('change');
        await flush();
        check('Выбор другого салона вернул историю на первую страницу',
            lastAsk(backend.asked.shifts).offset === 0,
            JSON.stringify(lastAsk(backend.asked.shifts)));
    }

    console.log('\n=== 4. «Исправить» у флориста — только самая свежая смена ===');
    {
        const backend = makeBackend();
        const env = makeEnv(backend, { username: 'florist_cs', role: 'florist', store_id: 1 });
        await flush();

        const firstPageRows = env.byId['shifts-tbody'].querySelectorAll('button[data-edit-shift-id]');
        check('На первой странице кнопка ровно одна', firstPageRows.length === 1,
            `кнопок: ${firstPageRows.length}`);

        env.byId['shifts-pagination-pages'].querySelectorAll('button[data-page="1"]')[0].fire('click');
        await flush();
        const secondPageRows = env.byId['shifts-tbody'].querySelectorAll('button[data-edit-shift-id]');
        // Сервер такую правку всё равно отобьёт (require_shift_edit_access),
        // то есть кнопка вела бы человека в отказ
        check('На второй странице кнопки «Исправить» нет', secondPageRows.length === 0,
            `кнопок: ${secondPageRows.length}`);
    }

    console.log('\n=== 5. Новая строка показывается сразу — возврат на первую страницу ===');
    {
        const backend = makeBackend();
        const env = makeEnv(backend, { username: 'admin_cs', role: 'admin' });
        await flush();

        // Салон выбран — иначе открытой смены нет и добавлять инкассацию некуда
        env.byId['shift-store-selector'].value = '1';
        env.byId['shift-store-selector'].fire('change');
        await flush();

        env.byId['collections-pagination-pages'].querySelectorAll('button[data-page="1"]')[0].fire('click');
        await flush();
        check('Стоим на второй странице инкассаций',
            lastAsk(backend.asked.collections).offset === 25,
            JSON.stringify(lastAsk(backend.asked.collections)));

        // Добавляем инкассацию тем же путём, что и человек: категория ищется
        // по НАЗВАНИЮ (поле с подсказками), а не по id
        env.byId['collection-amount'].value = '500';
        env.byId['collection-category'].value = 'Инкассация';
        env.byId['confirm-add-collection-btn'].fire('click');
        await flush();
        await flush();

        check('После новой инкассации показана первая страница',
            lastAsk(backend.asked.collections).offset === 0,
            JSON.stringify(lastAsk(backend.asked.collections)));
    }

    console.log('\n=== 6. Пустой ответ при живом счётчике не запускает долбёжку ===');
    {
        // Рассинхрон: сервер отдаёт total, но строк на странице нет. Экран,
        // который перезапрашивает «пока не придут строки», уходит в цикл —
        // каждый виток это поход на медленный /data
        let asked = 0;
        let looped = false;
        const backend = makeBackend();
        const original = backend.fetchImpl;
        const env = createSandbox({
            src: SRC,
            fetch: async (url, opts) => {
                if (url.startsWith('/api/cash-shifts?')) {
                    asked += 1;
                    if (asked > 15) { looped = true; throw new Error('перезапросы зациклились'); }
                    const params = new URLSearchParams(url.split('?')[1] || '');
                    const offset = parseInt(params.get('offset') || '0', 10);
                    const rows = offset === 0 ? [{
                        id: 1, store_id: 1, shift_type: 'day', status: 'closed',
                        datetime_start: '2026-09-01 08:00:00', closed_at: '2026-09-01 20:00:00',
                        opening_balance: 0, actual_balance: 0, cash_orders_total: 0,
                        collections_total: 0, discrepancy: 0,
                    }] : [];
                    return { ok: true, status: 200, json: async () => ({
                        success: true, shifts: rows, count: rows.length, total: 60 }) };
                }
                return original(url, opts);
            },
            extras: {
                BarhatUI: { confirm: async () => true, prompt: async () => '', toast() {} },
                BarhatTime: {
                    formatDate: () => '', formatTime: () => '', formatDateTime: () => '',
                    formatDateTimeLong: () => '', dayStartUtc: (d) => d, dayEndUtc: (d) => d,
                },
            },
        });
        env.sandbox.CashShiftsModule.onPageActivated({ username: 'admin_cs', role: 'admin' });
        await flush();

        asked = 0;
        env.byId['shifts-pagination-pages'].querySelectorAll('button[data-page="1"]')[0].fire('click');
        await flush();
        await flush();
        check('Пустая страница не вызывает цепочку перезапросов',
            !looped && asked <= 2, `запросов: ${asked}`);
    }

    console.log('\n=== 7. Итог по деньгам не путается с числом строк ===');
    {
        const backend = makeBackend({ collectionsTotal: 40, collectionsAmount: 987654 });
        const env = makeEnv(backend, { username: 'admin_cs', role: 'admin' });
        await flush();

        const summary = env.byId['collections-summary'].innerHTML;
        check('В итогах показаны деньги за период', /987\s?654/.test(summary.replace(/ /g, ' ')),
            summary.slice(0, 120));
        check('А в навигации — число строк',
            env.byId['collections-pagination-info'].textContent === '1–25 из 40',
            env.byId['collections-pagination-info'].textContent);
    }

    console.log('\n' + '='.repeat(60));
    if (failures.length) {
        console.log(`ПРОВАЛЕНО проверок: ${failures.length}`);
        failures.forEach(n => console.log(`  - ${n}`));
        process.exit(1);
    }
    console.log('Все проверки прошли.');
})();
