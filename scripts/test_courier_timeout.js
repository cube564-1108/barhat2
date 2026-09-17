/**
 * Сторож таймаута в приложении курьера.
 *
 * Зачем. Лента отвечала на проде 53–113 секунд (лог 16.09.2026). Без таймаута
 * зависший запрос держал `state.loading` всё это время: экран оставался на
 * «Загружаем заказы…», следующий тик обновления выходил по флагу и ничего не
 * делал, кнопка «Обновить» не помогала. Показать прежние заказы с пометкой о
 * свежести приложение при этом вполне могло — правило CLAUDE.md «экран обязан
 * мягко переносить медленный ответ».
 *
 * Проверяется ПОВЕДЕНИЕ настоящей функции из файла, а не наличие слов в коде:
 * исходник `apiGet` вырезается из courier-app.js и исполняется в песочнице с
 * заглушками fetch/AbortController. Заглушка fetch никогда не отвечает — ровно
 * как прод в тот день.
 *
 * Полной DOM-заглушки здесь нет намеренно: она больше самой правки (см.
 * scripts/test_writeoff_form.js). Поэтому связка «лента ходит через apiGet»
 * проверяется отдельно, по исходнику.
 *
 * Запуск: node scripts/test_courier_timeout.js
 */

const fs = require('fs');
const path = require('path');
const vm = require('vm');

// Путь можно подменить — так проверяется сам сторож: прогон по заведомо
// сломанной копии обязан падать, иначе зелёный результат ничего не значит.
const SRC = process.env.COURIER_APP_JS
    || path.join(__dirname, '..', 'src', 'dashboard', 'courier-app.js');
const source = fs.readFileSync(SRC, 'utf8');

const failures = [];

/**
 * Общий предохранитель прогона.
 *
 * Без него сторож бесполезен ровно там, где нужен больше всего. Если taймаут
 * в приложении сломан, заглушка fetch не отвечает НИКОГДА: промис не
 * разрешается, очередь событий пустеет, node выходит с кодом 0 — и сторож
 * выглядит зелёным на сломанном коде. Проверено: так и было при первой версии
 * этого файла.
 *
 * Живой таймер держит процесс и превращает «тихо не дошло» в честный провал.
 */
const WATCHDOG_MS = 10000;
const watchdog = setTimeout(function () {
    console.log(`\n  [FAIL] прогон не дошёл до конца за ${WATCHDOG_MS} мс —`
        + ' запрос не прервался, то есть таймаут не работает');
    console.log('ПРОВАЛЕНО: 1 — ["прогон завис"]');
    process.exit(1);
}, WATCHDOG_MS);

function check(name, condition, detail) {
    const mark = condition ? 'OK  ' : 'FAIL';
    console.log(`  [${mark}] ${name}${detail ? ' — ' + detail : ''}`);
    if (!condition) failures.push(name);
}

/**
 * Вырезать тело функции по имени, считая скобки.
 *
 * Именно исходник, а не переписанная в тесте копия: копия начинает жить своей
 * жизнью и зеленеет, когда боевой код уже сломан.
 */
function extractFunction(text, name) {
    const start = text.indexOf('function ' + name + '(');
    if (start === -1) return null;
    const open = text.indexOf('{', start);
    let depth = 0;
    for (let i = open; i < text.length; i++) {
        if (text[i] === '{') depth++;
        else if (text[i] === '}') {
            depth--;
            if (depth === 0) return text.slice(start, i + 1);
        }
    }
    return null;
}

const apiGetSource = extractFunction(source, 'apiGet');

console.log('1. Функция запроса вообще нашлась');
check('apiGet есть в courier-app.js', Boolean(apiGetSource));
if (!apiGetSource) {
    console.log('\nПРОВАЛЕНО: без apiGet проверять нечего');
    process.exit(1);
}

// --- Песочница ---------------------------------------------------------------

function makeSandbox(fetchImpl, timeoutMs) {
    const sandbox = {
        REQUEST_TIMEOUT_MS: timeoutMs,
        fetch: fetchImpl,
        setTimeout,
        clearTimeout,
        Math,
        console,
        AbortController: typeof AbortController === 'function' ? AbortController : undefined,
    };
    vm.createContext(sandbox);
    vm.runInContext(apiGetSource + '\nthis.apiGet = apiGet;', sandbox);
    return sandbox;
}

(async function run() {
    console.log('\n2. Зависший запрос прерывается, а не висит вечно');

    let seenOptions = null;
    let aborted = false;

    const hangingFetch = function (url, options) {
        seenOptions = options;
        return new Promise(function (_resolve, reject) {
            // Настоящий fetch отклоняет промис с AbortError, когда сигнал
            // сработал. Воспроизводим это, иначе проверялся бы не тот путь.
            if (options && options.signal) {
                options.signal.addEventListener('abort', function () {
                    aborted = true;
                    const error = new Error('The operation was aborted.');
                    error.name = 'AbortError';
                    reject(error);
                });
            }
            // Ответа нет никогда — как на проде 16.09.2026.
        });
    };

    const sandbox = makeSandbox(hangingFetch, 50);

    const startedAt = Date.now();
    let message = null;
    try {
        await sandbox.apiGet('/api/courier/orders');
        message = null;
    } catch (error) {
        message = error && error.message;
    }
    const elapsed = Date.now() - startedAt;

    check('запрос уходит с signal — иначе прерывать нечем',
          Boolean(seenOptions && seenOptions.signal), JSON.stringify(Object.keys(seenOptions || {})));
    check('сигнал отмены действительно сработал', aborted);
    check('промис отклонился, а не завис', message !== null, String(message));
    check('уложился в разумное время', elapsed < 2000, elapsed + ' мс');

    // Текст читает курьер на улице. «Нет связи» и «сервер не успел» требуют
    // разных действий: искать сеть против подождать и обновить.
    check('причина названа таймаутом, а не «ошибкой»',
          typeof message === 'string' && /не ответил/i.test(message), String(message));

    console.log('\n3. Успешный ответ по-прежнему проходит насквозь');

    let cleared = false;
    const okFetch = function () {
        return Promise.resolve({
            ok: true,
            json: function () {
                return Promise.resolve({ success: true, data: [1, 2, 3] });
            },
        });
    };
    const okSandbox = makeSandbox(okFetch, 50);
    // Таймер обязан сниматься: не снятый держит процесс живым и в приложении
    // копится по одному на каждый тик обновления.
    okSandbox.clearTimeout = function (handle) {
        cleared = true;
        return clearTimeout(handle);
    };

    let payload = null;
    let failure = null;
    try {
        payload = await okSandbox.apiGet('/api/courier/orders');
    } catch (error) {
        failure = error && error.message;
    }

    check('успешный ответ возвращается целиком',
          Boolean(payload && payload.success === true && payload.data.length === 3),
          failure || JSON.stringify(payload));
    check('таймер снят после ответа', cleared);

    console.log('\n4. Ошибку сервера таймаут не проглатывает');

    const errorFetch = function () {
        return Promise.resolve({
            ok: true,
            json: function () {
                return Promise.resolve({ success: false, error: 'Вам не назначен город' });
            },
        });
    };
    const errorSandbox = makeSandbox(errorFetch, 50);
    let serverMessage = null;
    try {
        await errorSandbox.apiGet('/api/courier/orders');
    } catch (error) {
        serverMessage = error && error.message;
    }
    check('текст ошибки от сервера доходит до курьера',
          serverMessage === 'Вам не назначен город', String(serverMessage));

    console.log('\n5. Таймаут не добавляет серверу нагрузки');
    // Главная опасность самой этой правки. Прерывание на клиенте НЕ
    // останавливает обработчик — он доработает свои полторы минуты и займёт
    // поток воркера до конца. Без отсрочки клиент, освободившись на 45-й
    // секунде, слал бы следующий запрос через 45–75 секунд вместо ~100, и
    // держал бы на сервере полтора-два обработчика на курьера вместо одного.
    // Ровно так 16.09.2026 и положили прод: правка, разумная с одной стороны.
    const timeoutConst = source.match(/REQUEST_TIMEOUT_MS\s*=\s*(\d+)/);
    const backoffConst = source.match(/TIMEOUT_BACKOFF_MS\s*=\s*(\d+)/);
    check('отсрочка после таймаута задана', Boolean(backoffConst));

    // Худшее наблюдавшееся время ответа ленты на проде (лог 16.09.2026).
    // Пауза между попытками обязана быть не короче: иначе запросов в единицу
    // времени станет больше, чем было до правки.
    const WORST_OBSERVED_MS = 100000;
    const cycle = timeoutConst && backoffConst
        ? Number(timeoutConst[1]) + Number(backoffConst[1]) : 0;
    check('попытка не чаще, чем лента отвечала в худшем случае',
          cycle >= WORST_OBSERVED_MS, cycle + ' мс между попытками');

    // Признак таймаута — поле, а не разбор текста: текст меняют при первой же
    // правке формулировки, и отсрочка молча перестанет применяться.
    const apiGetMarksTimeout = /\.timedOut\s*=\s*true/.test(apiGetSource);
    check('таймаут помечен полем, а не текстом', apiGetMarksTimeout);

    const loadFeedSrc = extractFunction(source, 'loadFeed');
    check('лента ставит отсрочку именно по таймауту',
          Boolean(loadFeedSrc && /error\.timedOut/.test(loadFeedSrc)
                  && /retryNotBefore\s*=\s*Date\.now\(\)\s*\+\s*TIMEOUT_BACKOFF_MS/.test(loadFeedSrc)));
    check('лента уважает отсрочку перед запросом',
          Boolean(loadFeedSrc && /retryNotBefore/.test(loadFeedSrc)
                  && loadFeedSrc.indexOf('retryNotBefore') < loadFeedSrc.indexOf('state.loading = true')));
    check('удачный ответ снимает отсрочку',
          Boolean(loadFeedSrc && /retryNotBefore\s*=\s*0/.test(loadFeedSrc)));

    console.log('\n6. Лента ходит именно через apiGet');
    // Единственная проверка по исходнику: без неё таймаут мог бы оказаться в
    // функции, которой лента не пользуется, и сторож всё равно был бы зелёным.
    const loadFeedSource = extractFunction(source, 'loadFeed');
    check('loadFeed найдена', Boolean(loadFeedSource));
    check('loadFeed запрашивает заказы через apiGet',
          Boolean(loadFeedSource && /apiGet\(/.test(loadFeedSource)));
    check('loadFeed не ходит в fetch мимо таймаута',
          Boolean(loadFeedSource && !/\bfetch\(/.test(loadFeedSource)));

    clearTimeout(watchdog);

    console.log();
    if (failures.length) {
        console.log(`ПРОВАЛЕНО: ${failures.length} — ${JSON.stringify(failures)}`);
        process.exit(1);
    }
    console.log('Все проверки пройдены');
})();
