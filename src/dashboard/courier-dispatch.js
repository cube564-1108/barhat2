/*
 * Раздел «Контроль доставки» (Фаза 8).
 *
 * Четыре вкладки, и каждая отвечает на свой вопрос:
 *
 *   Доставка        — где сейчас каждый заказ и какие никто не взял. Период
 *                     по умолчанию «сегодня и завтра», но задаётся любой:
 *                     таблица заказов фильтруется и выгружается в Excel;
 *   Курьеры         — профили: город и связка с курьером CRM (от неё
 *                     зависит, попадёт ли работа человека в выплаты);
 *   Статусы CRM     — действие курьера → код статуса, заполняет человек;
 *   Журнал отправок — что мы отправили в CRM и что она ответила.
 *
 * Состояние заказа берётся из НАШЕЙ таблицы броней, а не из статуса CRM:
 * между действием курьера и его отражением в CRM проходит до минуты, и
 * экран не должен врать эту минуту (находка К1 критики плана).
 */

(function () {
    'use strict';

    var AJAX = {
        'X-Requested-With': 'barhat-dashboard',
        'Content-Type': 'application/json'
    };

    var TABS = [
        // Не «Доставка сегодня»: период стал произвольным, и старое имя
        // врало бы на каждом выборе прошлой недели.
        { id: 'today', title: 'Доставка' },
        { id: 'couriers', title: 'Курьеры' },
        { id: 'statuses', title: 'Статусы CRM' },
        { id: 'cities', title: 'Настройки городов' },
        { id: 'outbox', title: 'Журнал отправок' },
        // Сюда переехали «Показатели за 30 дней», жившие внизу «Доставки»:
        // два места с одними числами разъезжаются на первой правке формулы,
        // и потом не понять, какое верное (решение владельца 23.09.2026).
        { id: 'analytics', title: 'Аналитика' }
    ];

    // Сколько дней показываем при первом открытии «Аналитики». Месяц — это то,
    // на что смотрят: неделя шумит, квартал прячет свежее. Предел периода у
    // сервера свой и приезжает в meta.
    var ANALYTICS_DEFAULT_DAYS = 30;

    var STATE_TITLES = {
        free: 'Свободен',
        claimed: 'Забронирован',
        picked_up: 'В пути',
        delivered: 'Доставлен',
        // Заказ отдали службе доставки. В поле `state` такого значения нет —
        // оно живёт только на экране, и потому лежит здесь же, чтобы столбец
        // таблицы и выпадающий список фильтра не разъехались.
        outsourced: 'Передан службе'
    };

    var READY_TITLES = { ready: 'Готов', making: 'Собирают' };

    var EMPTY_FILTERS = {
        order: '', site: '', time_from: '', time_to: '',
        state: '', ready: '', courier: ''
    };

    // «Курьер не назначен» — тоже значение столбца, и выбрать его надо уметь.
    // Пустая строка занята под «любой», поэтому у него свой признак.
    var NO_COURIER = '__none__';

    /*
     * Сколько строк рисуем за раз.
     *
     * Период стал произвольным, и «прошлый месяц» — это тысячи заказов.
     * innerHTML на таком объёме вешает вкладку на секунды, а читать сетку из
     * десяти тысяч строк всё равно нельзя. Отсечка громкая, с числом и
     * подсказкой: тихо показать часть — значит соврать про остальное.
     *
     * Выгрузки это не касается: файл открывают ровно затем, чтобы работать со
     * всем объёмом.
     */
    var MAX_ROWS = 300;

    var state = {
        tab: 'today',
        isAdmin: false,
        overview: null,
        profiles: null,
        actions: [],
        statuses: [],
        cities: [],
        outbox: [],
        users: [],
        loading: false,
        filters: Object.assign({}, EMPTY_FILTERS),
        // Что просим у сервера и что он реально отдал. Две разные вещи:
        // умолчание («сегодня и завтра») знает только он, а отказ по слишком
        // длинному периоду обязан вернуть поля к тому, что на экране.
        period: { from: '', to: '' },
        shownPeriod: { from: '', to: '' },
        maxDays: null,

        // --- Вкладка «Аналитика» --------------------------------------------
        //
        // Свой период, а не общий с «Доставкой»: там смотрят сегодня и завтра,
        // здесь — прошлый месяц. Один period на две вкладки означал бы, что
        // переключение молча меняет то, на что человек только что смотрел.
        analytics: null,
        anaSites: [],            // справочник салонов, приезжает с данными
        anaPicked: [],           // что отмечено СЕЙЧАС (ещё не применено)
        anaApplied: { period: { from: '', to: '' }, sites: [] },
        anaPeriod: { from: '', to: '' },
        anaSitesOpen: false,
        // Раскрытые блоки детализации. Переживают перерисовку: лента блоков
        // переписывается целиком, и состояние в DOM стёрлось бы под рукой.
        anaOpen: { late: false, outsourced: false, never: false }
    };

    function esc(value) {
        if (value === null || value === undefined) return '';
        return String(value)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    function toast(message, kind) {
        if (window.BarhatUI) window.BarhatUI.toast(message, kind || 'info');
    }

    /** «—» вместо нуля, когда показатель не посчитан: это разные вещи. */
    function num(value, suffix) {
        if (value === null || value === undefined) return '—';
        return esc(value) + (suffix || '');
    }

    /**
     * Ответ целиком, вместе с meta.
     *
     * Умолчание периода и его предел знает сервер, и оба приезжают в meta.
     * Повторять их во фронте значило бы однажды их развести: поля показывали
     * бы один период, а запрос уходил за другой.
     */
    function getFull(url) {
        return fetch(url, { credentials: 'same-origin' })
            .then(function (r) { return r.json(); })
            .then(function (payload) {
                if (!payload || payload.success !== true) {
                    throw new Error((payload && payload.error) || 'Сервер вернул ошибку');
                }
                return { data: payload.data, meta: payload.meta || {} };
            });
    }

    function get(url) {
        return getFull(url).then(function (r) { return r.data; });
    }

    function post(url, body) {
        return fetch(url, {
            method: 'POST', credentials: 'same-origin', headers: AJAX,
            body: JSON.stringify(body || {})
        }).then(function (r) {
            return r.json().catch(function () { return {}; }).then(function (payload) {
                if (!r.ok || payload.success !== true) {
                    throw new Error(payload.error || ('HTTP ' + r.status));
                }
                return payload.data;
            });
        });
    }

    function del(url) {
        return fetch(url, { method: 'DELETE', credentials: 'same-origin', headers: AJAX })
            .then(function (r) {
                return r.json().catch(function () { return {}; }).then(function (payload) {
                    if (!r.ok || payload.success !== true) {
                        throw new Error(payload.error || ('HTTP ' + r.status));
                    }
                    return payload.data;
                });
            });
    }

    // === Загрузка ===========================================================

    /*
     * Номер загрузки. Период произвольный, и длинный отрезок считается дольше
     * короткого: ответы приходят не в том порядке, в каком их просили, и без
     * номера на экране осядет тот, которого уже никто не ждёт (CLAUDE.md).
     */
    var loadToken = 0;

    function loadTab() {
        var host = document.getElementById('cdispRoot');
        if (!host) return Promise.resolve();
        var token = ++loadToken;
        state.loading = true;
        render();

        var job;
        if (state.tab === 'today') {
            // Показателей здесь больше нет — они переехали во вкладку
            // «Аналитика» (23.09.2026). Вместе с ними ушёл и второй запрос:
            // каждый поход на медленный /data стоит денег, а числа, которых
            // на экране нет, грузить незачем.
            job = getFull('/api/courier/overview' + periodQuery())
                .then(function (r) {
                    if (token !== loadToken) return;
                    state.overview = r.data;
                    state.period = {
                        from: r.meta.date_from || '',
                        to: r.meta.date_to || ''
                    };
                    state.shownPeriod = state.period;
                    state.maxDays = r.meta.max_days || null;
                })
                .catch(function (error) {
                    // Данные на экране остались прежние — значит и поля
                    // периода обязаны остаться прежними. Иначе подпись врёт
                    // про то, что показано (CLAUDE.md: «медленно» и «отказ»
                    // не должны превращаться в «пусто» и в чужие цифры).
                    if (token === loadToken) state.period = state.shownPeriod;
                    throw error;
                });
        } else if (state.tab === 'couriers') {
            // Список учёток нужен, чтобы завести профиль новому курьеру:
            // профилей у него ещё нет, и выбирать не из чего
            job = Promise.all([get('/api/courier/profiles'), loadUsers()])
                .then(function (r) { state.profiles = r[0]; });
        } else if (state.tab === 'statuses') {
            job = get('/api/courier/action-statuses').then(function (data) {
                state.actions = data.actions || [];
                state.statuses = data.statuses || [];
            });
        } else if (state.tab === 'cities') {
            job = get('/api/courier/city-settings').then(function (data) {
                state.cities = data || [];
            });
        } else if (state.tab === 'analytics') {
            if (!state.anaPeriod.from || !state.anaPeriod.to) {
                state.anaPeriod = defaultAnalyticsPeriod();
            }
            job = getFull('/api/courier/analytics' + analyticsQuery())
                .then(function (r) {
                    if (token !== loadToken) return;
                    state.analytics = r.data;
                    state.anaSites = (r.meta && r.meta.sites) || state.anaSites;
                    state.maxDays = (r.meta && r.meta.max_days) || state.maxDays;
                    // Применённым считается то, что сервер реально посчитал, а
                    // не то, что человек отметил: иначе подпись под таблицей
                    // врёт про показанные числа при любом отказе.
                    state.anaApplied = {
                        period: {
                            from: (r.meta && r.meta.date_from) || state.anaPeriod.from,
                            to: (r.meta && r.meta.date_to) || state.anaPeriod.to
                        },
                        sites: (r.data.site_codes || []).slice()
                    };
                    state.anaPeriod = {
                        from: state.anaApplied.period.from,
                        to: state.anaApplied.period.to
                    };
                })
                .catch(function (error) {
                    // Отказ возвращает поля к показанному: данные на экране
                    // прежние, значит и период обязан остаться прежним.
                    if (token === loadToken) {
                        state.anaPeriod = {
                            from: state.anaApplied.period.from || state.anaPeriod.from,
                            to: state.anaApplied.period.to || state.anaPeriod.to
                        };
                        state.anaPicked = state.anaApplied.sites.slice();
                    }
                    throw error;
                });
        } else {
            job = get('/api/courier/outbox?limit=100').then(function (data) {
                state.outbox = data || [];
            });
        }

        return job.catch(function (error) {
            if (token !== loadToken) return;
            toast('Не удалось загрузить: ' + error.message, 'error');
        }).then(function () {
            if (token !== loadToken) return;
            state.loading = false;
            render();
        });
    }

    function loadUsers() {
        if (state.users.length) return Promise.resolve(state.users);
        return fetch('/api/auth/users', { credentials: 'same-origin' })
            .then(function (r) { return r.ok ? r.json() : []; })
            .then(function (data) {
                state.users = (Array.isArray(data) ? data : (data.users || []))
                    .filter(function (u) { return u.is_active !== false; });
                return state.users;
            })
            .catch(function () { return []; });
    }

    // === Вкладка «Доставка сегодня» =========================================

    function todayHtml() {
        if (!state.overview) return '<p class="section-description">Загружаем…</p>';

        var totals = state.overview.totals || {};
        var unclaimed = state.overview.unclaimed || [];

        var tiles = [
            ['Свободны', totals.free || 0],
            ['Забронированы', totals.claimed || 0],
            ['В пути', totals.picked_up || 0],
            ['Доставлены', totals.delivered || 0]
        ].map(function (pair) {
            return '<div class="cdisp-tile"><div class="cdisp-tile__label">' + esc(pair[0])
                + '</div><div class="cdisp-tile__value">' + esc(pair[1]) + '</div></div>';
        }).join('');

        var alarm = '';
        if (unclaimed.length) {
            // Решение «отдать аутсорсу» стоит денег и зависит от контекста:
            // модуль показывает ситуацию, кнопку жмёт человек — в CRM
            alarm = '<div class="cdisp-alarm">'
                + '<h3>Никто не взял: ' + unclaimed.length + '</h3>'
                + '<p class="section-description">До доставки осталось меньше порога города. '
                + 'Если свой курьер не успевает — заказ передают службе доставки в CRM.</p>'
                + orderTable(capped(unclaimed)) + cutNotice(unclaimed) + '</div>';
        }

        // Забронированы, но не забраны, а окно близко. Это замена автоснятию
        // брони по таймеру: снимать её теперь некому, кроме человека, — значит
        // человек обязан о ней узнать. В «Никто не взял» такой заказ не
        // попадает: он не свободен.
        var stuck = state.overview.stuck || [];
        var stuckBlock = '';
        if (stuck.length) {
            stuckBlock = '<div class="cdisp-alarm">'
                + '<h3>Взяли, но не забрали: ' + stuck.length + '</h3>'
                + '<p class="section-description">До доставки осталось меньше порога '
                + 'города, а заказ всё ещё в салоне. Бронь сама не снимается — '
                + 'свяжитесь с курьером или снимите бронь здесь.</p>'
                + orderTable(capped(stuck)) + cutNotice(stuck) + '</div>';
        }

        return '<div class="cdisp-tiles">' + tiles + '</div>'
            + alarm
            + stuckBlock
            + mismatchHtml()
            + ordersSectionHtml();
    }

    /**
     * Курьер в CRM разошёлся с нашей бронью.
     *
     * По полю «курьер» в CRM считают выплаты, и правит его не только модуль —
     * оператор назначает курьера руками. Расхождение это чьи-то деньги, и
     * увидеть его надо раньше, чем закроется месяц.
     *
     * Ни бронь, ни поле в CRM модуль сам не трогает: снять бронь из-за
     * возможной опечатки оператора значит отдать букет второму курьеру,
     * а стереть курьера в CRM — затереть решение человека.
     */
    function mismatchHtml() {
        var list = (state.overview && state.overview.mismatches) || [];
        if (!list.length) return '';

        // Отсечка та же, что у таблицы заказов: расхождения считаются по
        // всему периоду, а он теперь до квартала — блок растёт вместе с ним.
        var rows = capped(list).map(function (item) {
            var what = item.kind === 'stale_courier'
                ? 'Бронь снята, курьер в CRM остался'
                : 'В CRM другой курьер';
            return '<tr>'
                + '<td>' + esc(item.order_number || item.retailcrm_order_id) + '</td>'
                + '<td>' + esc(item.delivery_date || '') + '</td>'
                + '<td>' + esc(item.our_courier_name || '—') + '</td>'
                + '<td>' + esc(item.crm_courier_name || item.crm_courier_id || '—') + '</td>'
                + '<td>' + esc(what) + '</td>'
                + '</tr>';
        }).join('');

        return '<div class="cdisp-alarm">'
            + '<h3>Курьер в CRM не совпадает: ' + list.length + '</h3>'
            + '<p class="section-description">Выплаты считают по полю «курьер» в CRM. '
            + 'Модуль сюда не вмешивается — поправьте в CRM или снимите бронь.</p>'
            + '<table class="cdisp-table"><thead><tr>'
            + '<th>Заказ</th><th>Дата</th><th>У нас</th><th>В CRM</th><th>Что не так</th>'
            + '</tr></thead><tbody>' + rows + '</tbody></table>'
            + cutNotice(list) + '</div>';
    }

    /*
     * Отсечка отрисовки, одна на все таблицы раздела.
     *
     * Период стал произвольным, и «прошлый месяц» — это тысячи строк:
     * innerHTML на таком объёме вешает вкладку на секунды, а читать сетку из
     * десяти тысяч строк всё равно нельзя. Молчаливая отсечка врёт про
     * остальное, поэтому `capped` всегда ходит в паре с `cutNotice`.
     */
    function capped(list) {
        return list.length > MAX_ROWS ? list.slice(0, MAX_ROWS) : list;
    }

    function cutNotice(list, hint) {
        if (list.length <= MAX_ROWS) return '';
        return '<p class="section-description">Показаны первые ' + MAX_ROWS
            + ' строк из ' + list.length + '. ' + (hint || 'Сузьте период.') + '</p>';
    }

    // --- Значения ячеек -----------------------------------------------------
    /*
     * Что показано в столбце, по чему фильтруем и что уходит в выгрузку —
     * считает ОДНА функция на столбец. Развести их значит развести и смысл:
     * в столбце «Состояние» есть «Передан службе», которого в поле `state`
     * нет вовсе, и фильтр по сырому полю такой заказ никогда бы не нашёл.
     */

    function orderLabel(order) {
        return order.order_number || order.retailcrm_order_id || '';
    }

    function siteLabel(order) {
        return order.site_name || order.city || '';
    }

    /**
     * Дата доставки в человеческом виде.
     *
     * Через BarhatTime не пропускаем: это НЕ отметка времени в UTC, а день по
     * стенным часам салона — ровно тот, что менеджер ввёл в CRM. Перевод в
     * пояс устройства сдвинул бы утренний заказ на вчера.
     */
    function dateLabel(order) {
        var raw = String(order.delivery_date || '');
        var parts = raw.split('-');
        return parts.length === 3 ? parts[2] + '.' + parts[1] + '.' + parts[0] : raw;
    }

    function slotLabel(order) {
        return (order.delivery_time_from || '')
            + (order.delivery_time_to ? '–' + order.delivery_time_to : '');
    }

    /**
     * Заказ отдали службе доставки: в CRM в поле «курьер» стоит агрегатор.
     * Свободным он выглядит только в наших глазах — показывать его так
     * значило бы звать человека решать решённое.
     */
    function stateKey(order) {
        if (order.outsourced && order.state === 'free') return 'outsourced';
        return order.state || 'free';
    }

    function stateLabel(order) {
        var key = stateKey(order);
        return STATE_TITLES[key] || key;
    }

    function readyKey(order) {
        return order.is_ready ? 'ready' : 'making';
    }

    function courierLabel(order) {
        return order.courier_name
            || (order.outsourced ? (order.crm_courier_name || '') : '');
    }

    function orderTable(orders) {
        if (!orders.length) return '<p class="section-description">Заказов нет</p>';
        var rows = orders.map(function (order) {
            // «Зависла» считает сервер: бронь по времени больше не сгорает
            // (21.09.2026), и признак теперь не про срок брони, а про заказ —
            // взят, но не забран, а окно доставки уже близко. Порог свой у
            // каждого города, и знать его фронту незачем.
            var overdue = order.stuck_claim === true;
            return '<tr>'
                + '<td>' + esc(orderLabel(order)) + '</td>'
                + '<td>' + esc(dateLabel(order)) + '</td>'
                + '<td>' + esc(siteLabel(order)) + '</td>'
                + '<td>' + esc(slotLabel(order) || 'время уточняется') + '</td>'
                + '<td>' + esc(stateLabel(order))
                + (overdue ? ' <span class="cdisp-bad">не забран</span>' : '') + '</td>'
                + '<td>' + esc(READY_TITLES[readyKey(order)]) + '</td>'
                + '<td>' + esc(courierLabel(order)) + '</td>'
                + '<td>' + (order.state === 'claimed' || order.state === 'picked_up'
                    ? '<button class="btn btn-secondary" data-release-order="'
                        + esc(order.retailcrm_order_id) + '">Снять бронь</button>'
                    : '') + '</td>'
                + '</tr>';
        }).join('');

        return '<table class="cdisp-table"><thead><tr>'
            + '<th>Заказ</th><th>Дата</th><th>Салон</th><th>Окно</th><th>Состояние</th>'
            + '<th>Сборка</th><th>Курьер</th><th></th>'
            + '</tr></thead><tbody>' + rows + '</tbody></table>';
    }

    // --- Фильтр таблицы «Заказы» --------------------------------------------
    /*
     * Фильтр клиентский: заказы за период уже пришли одним запросом, и ходить
     * за каждым переключением на общий медленный /data незачем (правило
     * CLAUDE.md про цену обращения к базе). Тревожные блоки выше фильтр не
     * трогает: это не таблица, а список того, что требует действия сейчас.
     */

    function hhmm(value) {
        return String(value || '').slice(0, 5);
    }

    function allOrders() {
        return (state.overview && state.overview.orders) || [];
    }

    function hasFilters() {
        return Object.keys(EMPTY_FILTERS).some(function (key) {
            return String(state.filters[key] || '').trim() !== '';
        });
    }

    function filteredOrders() {
        var f = state.filters;
        var query = String(f.order || '').trim().toLowerCase();
        var from = hhmm(f.time_from);
        var to = hhmm(f.time_to);

        return allOrders().filter(function (order) {
            if (query && String(orderLabel(order)).toLowerCase().indexOf(query) === -1) {
                return false;
            }
            if (f.site && siteLabel(order) !== f.site) return false;
            if (f.state && stateKey(order) !== f.state) return false;
            if (f.ready && readyKey(order) !== f.ready) return false;
            if (f.courier) {
                var who = courierLabel(order);
                if (f.courier === NO_COURIER ? who !== '' : who !== f.courier) return false;
            }
            if (from || to) {
                // Окно у заказа может быть не заведено вовсе («время
                // уточняется»). Такой заказ не «раньше» и не «позже» — он вне
                // шкалы, и под ограничение по времени не попадает. Об этом
                // сказано подписью под фильтром, чтобы он не пропадал молча.
                var start = hhmm(order.delivery_time_from);
                if (!start) return false;
                if (from && start < from) return false;
                if (to && start > to) return false;
            }
            return true;
        });
    }

    /** Уникальные непустые значения столбца — в том же виде, в каком они в нём. */
    function columnValues(fn) {
        var seen = {};
        allOrders().forEach(function (order) {
            var value = fn(order);
            if (value) seen[value] = true;
        });
        return Object.keys(seen).sort(function (a, b) {
            return a.localeCompare(b, 'ru');
        });
    }

    /**
     * Выбранное значение обязано остаться в списке, даже если данные
     * обновились и такого салона/курьера в выборке больше нет: иначе браузер
     * молча покажет первый пункт («Все»), а фильтр останется применённым —
     * экран и состояние разойдутся.
     */
    function filterSelect(field, options, titles) {
        var current = state.filters[field] || '';
        var known = options.some(function (pair) { return pair[0] === current; });
        var all = options.slice();
        if (current && !known) {
            // Имя берём из словаря, а не из ключа: у салона и курьера значение
            // и есть название, а у состояния значение — это код (`delivered`),
            // и человеку он ничего не говорит.
            all.push([current, ((titles || {})[current] || current)
                      + ' — нет в выборке']);
        }
        return '<select class="form-select cdisp-filter__control" data-cdisp-filter="'
            + field + '">'
            + all.map(function (pair) {
                return '<option value="' + esc(pair[0]) + '"'
                    + (pair[0] === current ? ' selected' : '') + '>'
                    + esc(pair[1]) + '</option>';
            }).join('')
            + '</select>';
    }

    function filterField(label, control) {
        return '<div class="cdisp-filter"><label class="cdisp-filter__label">'
            + esc(label) + '</label>' + control + '</div>';
    }

    /**
     * Период — единственное условие, за которым мы идём на сервер.
     *
     * Поэтому он и живёт за кнопкой «Показать», а не применяется на каждое
     * изменение поля: остальные условия мгновенные и бесплатные, а это —
     * запрос к медленному /data, и нажать его должен человек.
     */
    function periodQuery() {
        if (!state.period.from || !state.period.to) return '';
        return '?date_from=' + encodeURIComponent(state.period.from)
            + '&date_to=' + encodeURIComponent(state.period.to);
    }

    function periodHtml() {
        return '<div class="cdisp-filters cdisp-filters--period">'
            + filterField('Дата доставки с',
                '<input type="date" class="form-input cdisp-filter__control"'
                + ' id="cdispPeriodFrom" value="' + esc(state.period.from || '') + '">')
            + filterField('по',
                '<input type="date" class="form-input cdisp-filter__control"'
                + ' id="cdispPeriodTo" value="' + esc(state.period.to || '') + '">')
            + '<div class="cdisp-filter">'
            + '<button type="button" class="btn btn-primary" data-cdisp-period="1"'
            + (state.loading ? ' disabled' : '') + '>Показать</button></div>'
            + (state.maxDays
                ? '<div class="cdisp-filter"><span class="cdisp-note">За раз можно '
                    + 'запросить не больше ' + esc(state.maxDays) + ' дней</span></div>'
                : '')
            + '</div>';
    }

    function ordersFilterHtml() {
        var sites = [['', 'Все салоны']].concat(
            columnValues(siteLabel).map(function (v) { return [v, v]; }));

        // Состояния перечисляем в порядке жизни заказа, а не по алфавиту, и
        // только те, что реально есть на экране.
        var present = {};
        allOrders().forEach(function (order) { present[stateKey(order)] = true; });
        var states = [['', 'Любое состояние']].concat(
            ['free', 'claimed', 'picked_up', 'delivered', 'outsourced']
                .filter(function (key) { return present[key]; })
                .map(function (key) { return [key, STATE_TITLES[key]]; }));

        var couriers = [['', 'Любой курьер'], [NO_COURIER, 'Без курьера']].concat(
            columnValues(courierLabel).map(function (v) { return [v, v]; }));

        return '<div class="cdisp-filters">'
            + filterField('Заказ', '<input type="search" class="form-input cdisp-filter__control"'
                + ' data-cdisp-filter="order" placeholder="номер заказа"'
                + ' value="' + esc(state.filters.order || '') + '">')
            + filterField('Салон', filterSelect('site', sites))
            + filterField('Окно с', '<input type="time" class="form-input cdisp-filter__control"'
                + ' data-cdisp-filter="time_from" value="' + esc(state.filters.time_from || '') + '">')
            + filterField('по', '<input type="time" class="form-input cdisp-filter__control"'
                + ' data-cdisp-filter="time_to" value="' + esc(state.filters.time_to || '') + '">')
            + filterField('Состояние', filterSelect('state', states, STATE_TITLES))
            + filterField('Сборка', filterSelect('ready', [
                ['', 'Любая'], ['ready', 'Готов'], ['making', 'Собирают']],
                READY_TITLES))
            + filterField('Курьер', filterSelect('courier', couriers))
            + '<div class="cdisp-filter">'
            + '<button type="button" class="btn btn-secondary" data-cdisp-filter-reset="1">'
            + 'Сбросить</button></div>'
            + '</div>';
    }

    function ordersCountText() {
        var total = allOrders().length;
        var shown = filteredOrders().length;
        if (!hasFilters()) return 'Заказов: ' + total;
        return 'Показано ' + shown + ' из ' + total;
    }

    function ordersBodyHtml() {
        if (!allOrders().length) {
            return '<p class="section-description">За выбранный период заказов нет</p>';
        }
        var rows = filteredOrders();
        if (!rows.length) {
            return '<p class="section-description">Под фильтр не попал ни один заказ. '
                + 'Снимите часть условий или нажмите «Сбросить».</p>';
        }
        // Отсечка громкая и с числом: показать часть молча — значит соврать
        // про остальное. Выгрузка при этом заберёт всё, и здесь об этом
        // сказано, чтобы за недостающими строками не шли сужать период.
        return cutNotice(rows, 'Сузьте период или фильтр — а выгрузка в Excel '
                               + 'заберёт все ' + rows.length + '.')
            + orderTable(capped(rows));
    }

    function ordersSectionHtml() {
        return '<h3 style="margin-top:28px">Заказы</h3>'
            + periodHtml()
            + ordersFilterHtml()
            + '<div class="cdisp-orders-head">'
            + '<span class="cdisp-note" id="cdispOrdersCount">' + esc(ordersCountText())
            + '</span>'
            + '<button type="button" class="btn btn-secondary" data-cdisp-export="1">'
            + 'Выгрузить в Excel</button>'
            + '</div>'
            + '<p class="cdisp-note">Заказы без заведённого окна доставки под ограничение '
            + '«Окно с / по» не попадают: у них времени нет вовсе.</p>'
            + '<div id="cdispOrders">' + ordersBodyHtml() + '</div>';
    }

    /**
     * Перерисовываем ТОЛЬКО таблицу, а не весь экран.
     *
     * Полный render() переписывает host.innerHTML целиком и вместе с ним —
     * поля самого фильтра: набор в строке «Заказ» обрывался бы на первом
     * символе, а прокрутка прыгала бы вверх (правило CLAUDE.md про
     * перерисовку через innerHTML).
     */
    function refreshOrders() {
        var host = document.getElementById('cdispOrders');
        if (!host) { render(); return; }
        host.innerHTML = ordersBodyHtml();
        var counter = document.getElementById('cdispOrdersCount');
        if (counter) counter.textContent = ordersCountText();
    }

    // --- Выгрузка в Excel ---------------------------------------------------
    /*
     * CSV, а не xlsx: ради одной кнопки не тащим на прод openpyxl — то же
     * решение, что в «Ссылках товаров» и «Оплате курьерам». Excel с русской
     * локалью открывает такой файл двойным кликом.
     *
     * Выгружается ровно то, что видно на экране: применённый фильтр — часть
     * ответа на вопрос «что это за список». Даты и город в файле есть, хотя
     * в таблице их нет: строка из выгрузки читается отдельно от экрана, и без
     * даты «10:00–12:00» не значит ничего — период охватывает и завтра.
     */

    function csvCell(value) {
        var text = String(value === null || value === undefined ? '' : value);
        // Название салона и имя курьера правят в CRM, а Excel исполняет
        // ячейку, начинающуюся с = + - @, как формулу. Апостроф делает её
        // текстом.
        if (/^[=+\-@\t\r]/.test(text)) text = "'" + text;
        return /[";\r\n]/.test(text) ? '"' + text.replace(/"/g, '""') + '"' : text;
    }

    function exportOrders() {
        var rows = filteredOrders();
        if (!rows.length) {
            window.BarhatUI.alert(allOrders().length
                ? 'Нечего выгружать: под фильтр не попал ни один заказ'
                : 'Нечего выгружать: заказов за период нет');
            return;
        }

        var lines = [[
            'Заказ', 'Дата доставки', 'Салон', 'Город', 'Окно', 'Состояние',
            'Не забран', 'Сборка', 'Курьер', 'Курьер в CRM'
        ].join(';')];

        rows.forEach(function (order) {
            lines.push([
                csvCell(orderLabel(order)),
                csvCell(order.delivery_date || ''),
                csvCell(order.site_name || ''),
                csvCell(order.city || ''),
                csvCell(slotLabel(order)),
                csvCell(stateLabel(order)),
                csvCell(order.stuck_claim === true ? 'да' : ''),
                csvCell(READY_TITLES[readyKey(order)]),
                csvCell(order.courier_name || ''),
                csvCell(order.crm_courier_name || '')
            ].join(';'));
        });

        // Период берём из самих строк: ручка отдаёт его в meta, но до экрана
        // meta не доходит, а выдумывать «сегодня» в имени файла нельзя —
        // выгрузка захватывает и завтрашние доставки.
        var dates = rows.map(function (o) { return o.delivery_date || ''; })
            .filter(Boolean).sort();
        var stamp = dates.length
            ? (dates[0] === dates[dates.length - 1]
                ? dates[0] : dates[0] + '_' + dates[dates.length - 1])
            : new Date().toISOString().slice(0, 10);

        // BOM — иначе Excel открывает кириллицу кракозябрами
        var blob = new Blob(['﻿' + lines.join('\r\n')],
                            { type: 'text/csv;charset=utf-8;' });
        var link = document.createElement('a');
        link.href = URL.createObjectURL(blob);
        link.download = 'контроль-доставки_заказы_' + stamp + '.csv';
        link.click();
        URL.revokeObjectURL(link.href);
    }

    /*
     * Блок «Показатели за 30 дней» жил здесь до 23.09.2026 и переехал во
     * вкладку «Аналитика» — целиком, вместе с медианой «от брони до забора» и
     * суммой аутсорса. Держать одни и те же числа в двух местах значит
     * однажды их развести: формулу правят в одном, а читают из другого.
     *
     * Ручка `/api/courier/metrics` осталась на месте — её ответ показывает
     * теперь вкладка «Аналитика» через свой расчёт, а формула «вовремя» у них
     * общая (`salon_time.deadline_utc`).
     */

    // === Вкладка «Аналитика» ================================================
    /*
     * Сюда переехали «Показатели за 30 дней» из «Доставки» и добавился разрез
     * по курьерам. Решения по виду (согласованы 23.09.2026):
     *
     *   - у плитки под числом стоит его БАЗА («171 из 198»): процент без
     *     знаменателя не проверяется — при трёх доставках 66,7% не значит
     *     ничего;
     *   - строка сходимости обязательна: «броней 247, доставок 198» рождает
     *     вопрос «где остальные 49», и отвечать на него надо на экране;
     *   - полоски доли нет, только число — сравнение даёт сортировка;
     *   - непосчитанное («без интервала», «салон без пояса») названо вслух,
     *     а не спрятано: иначе доля считается по куску выборки молча.
     */

    function defaultAnalyticsPeriod() {
        var to = new Date();
        var from = new Date();
        from.setDate(from.getDate() - (ANALYTICS_DEFAULT_DAYS - 1));
        function iso(d) {
            return d.getFullYear() + '-'
                + String(d.getMonth() + 1).padStart(2, '0') + '-'
                + String(d.getDate()).padStart(2, '0');
        }
        return { from: iso(from), to: iso(to) };
    }

    function analyticsQuery() {
        var query = '?date_from=' + encodeURIComponent(state.anaPeriod.from)
            + '&date_to=' + encodeURIComponent(state.anaPeriod.to);
        if (state.anaPicked.length) {
            query += '&sites=' + encodeURIComponent(state.anaPicked.join(','));
        }
        return query;
    }

    /** Выбранное отличается от показанного — значит «Показать» ещё не нажимали. */
    function analyticsDirty() {
        var applied = state.anaApplied;
        if (state.anaPeriod.from !== applied.period.from
            || state.anaPeriod.to !== applied.period.to) return true;
        if (state.anaPicked.length !== applied.sites.length) return true;
        return state.anaPicked.some(function (code) {
            return applied.sites.indexOf(code) === -1;
        });
    }

    function siteName(code) {
        var found = state.anaSites.filter(function (s) { return s.code === code; })[0];
        return (found && (found.name || found.code)) || code;
    }

    /**
     * Салоны: выпадающий список с галочками и чипсы выбранного.
     *
     * Выбор НЕ уходит на сервер сразу: каждый щелчок стоил бы запроса к
     * медленному /data. Применяется той же кнопкой «Показать», что и период,
     * а до нажатия рядом висит напоминание — иначе человек смотрит на числа
     * одного набора салонов и читает подписи другого.
     */
    function anaSitesHtml() {
        var picked = state.anaPicked;
        var label = picked.length
            ? 'Салоны: выбрано ' + picked.length
            : 'Салоны: все';

        var list = '';
        if (state.anaSitesOpen) {
            var options = state.anaSites.map(function (site) {
                var on = picked.indexOf(site.code) !== -1;
                return '<label class="cdisp-ms__row">'
                    + '<input type="checkbox" data-cdisp-site="' + esc(site.code) + '"'
                    + (on ? ' checked' : '') + '> '
                    + '<span>' + esc(site.name || site.code)
                    + (site.city ? ' <span class="cdisp-note">· ' + esc(site.city) + '</span>' : '')
                    + '</span></label>';
            }).join('');
            list = '<div class="cdisp-ms__list" data-cdisp-ms-list>'
                + (options || '<p class="section-description">Салонов пока нет</p>')
                + (picked.length
                    ? '<button type="button" class="btn btn-secondary cdisp-ms__clear"'
                        + ' data-cdisp-sites-clear="1">Снять все</button>'
                    : '')
                + '</div>';
        }

        var chips = picked.map(function (code) {
            return '<button type="button" class="cdisp-chip" data-cdisp-site-off="'
                + esc(code) + '" title="Убрать из выборки">'
                + esc(siteName(code)) + ' ×</button>';
        }).join('');

        return '<div class="cdisp-filter cdisp-ms">'
            + '<label class="cdisp-filter__label">Салоны</label>'
            + '<button type="button" class="form-input cdisp-filter__control cdisp-ms__button"'
            + ' data-cdisp-sites-toggle="1">' + esc(label) + ' ▾</button>'
            + list + '</div>'
            + (chips ? '<div class="cdisp-chips">' + chips + '</div>' : '');
    }

    function anaFiltersHtml() {
        return '<div class="cdisp-filters cdisp-filters--period">'
            + filterField('Дата доставки с',
                '<input type="date" class="form-input cdisp-filter__control"'
                + ' id="cdispAnaFrom" value="' + esc(state.anaPeriod.from || '') + '">')
            + filterField('по',
                '<input type="date" class="form-input cdisp-filter__control"'
                + ' id="cdispAnaTo" value="' + esc(state.anaPeriod.to || '') + '">')
            + anaSitesHtml()
            + '<div class="cdisp-filter">'
            + '<button type="button" class="btn btn-primary" data-cdisp-ana-apply="1"'
            + (state.loading ? ' disabled' : '') + '>Показать</button></div>'
            + '<div class="cdisp-filter">'
            + '<button type="button" class="btn btn-secondary" data-cdisp-ana-excel="1"'
            + (state.analytics ? '' : ' disabled') + '>Excel</button></div>'
            + (analyticsDirty()
                ? '<div class="cdisp-filter"><span class="cdisp-note cdisp-note--warn">'
                    + 'Фильтр изменён — нажмите «Показать»</span></div>'
                : '')
            + (state.maxDays
                ? '<div class="cdisp-filter"><span class="cdisp-note">За раз можно '
                    + 'запросить не больше ' + esc(state.maxDays) + ' дней</span></div>'
                : '')
            + '</div>';
    }

    function tile(label, value, caption) {
        return '<div class="cdisp-tile">'
            + '<div class="cdisp-tile__label">' + esc(label) + '</div>'
            + '<div class="cdisp-tile__value">' + value + '</div>'
            + (caption ? '<div class="cdisp-tile__caption">' + caption + '</div>' : '')
            + '</div>';
    }

    function anaTilesHtml(t) {
        var deliveredCaption = t.claims
            ? 'из ' + esc(t.claims) + ' броней' : '';
        var counted = (t.on_time || 0) + (t.late || 0);
        var byHand = 'курьер ' + esc(t.released_self || 0)
            + ' · управляющий ' + esc(t.released_admin || 0);
        var outsourcedCaption = t.claims
            ? esc(Math.round((t.outsourced_after_claim || 0) / t.claims * 100)) + ' % от броней'
            : '';

        return '<div class="cdisp-tiles cdisp-tiles--ana">'
            + tile('Броней', num(t.claims), '')
            + tile('Доставок', num(t.delivered), deliveredCaption)
            + tile('Вовремя', num(t.on_time_share, ' %'),
                counted ? esc(t.on_time) + ' из ' + esc(counted) : 'нечего считать')
            + tile('Опоздание', num(t.late_minutes_avg, ' мин'),
                t.late ? 'среднее по ' + esc(t.late) : 'опозданий нет')
            + tile('Сняли руками', num(t.released_by_hand), byHand)
            + tile('Ушло аутсорсу', num(t.outsourced_after_claim), outsourcedCaption)
            + '</div>';
    }

    /**
     * Куда делись брони, не ставшие доставками.
     *
     * Без этой строки «броней 247, доставок 198» выглядит потерей сорока девяти
     * заказов. Здесь же названо непосчитанное: доля «вовремя» считается не по
     * всем доставкам, и знать об этом надо на экране, а не из кода.
     */
    function anaReconcileHtml(t) {
        var parts = [
            'доставлено ' + esc(t.delivered || 0),
            'снято руками ' + esc(t.released_by_hand || 0),
            'аутсорс ' + esc(t.outsourced_after_claim || 0)
        ];
        if (t.released_expired) parts.push('сгорело по таймеру ' + esc(t.released_expired));
        if (t.order_gone) parts.push('заказ ушёл из работы ' + esc(t.order_gone));
        if (t.active) parts.push('в работе ' + esc(t.active));
        if (t.problem) parts.push('с проблемой ' + esc(t.problem));

        var notCounted = t.not_counted || {};
        var tail = [];
        if (notCounted.no_interval) {
            tail.push('без интервала доставки — ' + esc(notCounted.no_interval)
                + ' (в долю не вошли)');
        }
        if (notCounted.no_timezone) {
            tail.push('без часового пояса салона — ' + esc(notCounted.no_timezone)
                + ' ('
                + esc((notCounted.sites_without_timezone || []).join(', '))
                + ' — задайте пояс в «Настройках городов»)');
        }

        return '<p class="cdisp-reconcile">Из ' + esc(t.claims || 0) + ' броней: '
            + parts.join(' · ') + '.'
            + (tail.length ? ' Не посчитано: ' + tail.join('; ') + '.' : '')
            + '</p>';
    }

    function anaCouriersHtml(rows, t) {
        if (!rows.length) {
            return '<p class="section-description">За выбранный период броней нет</p>';
        }
        var body = rows.map(function (row) {
            // «Мало данных» — про число посчитанных доставок, а не всех: у
            // курьера с двумя «50,0 %» читается так же уверенно, как «86,4 %»
            // у курьера с двумя сотнями, и по нему примут решение о человеке.
            var share = num(row.on_time_share, ' %');
            if (row.low_data && row.on_time_share !== null) {
                share = '<span class="cdisp-note" title="Посчитано доставок: '
                    + esc(row.counted) + ' — слишком мало, чтобы судить">'
                    + share + ' · мало данных</span>';
            }
            return '<tr>'
                + '<td>' + esc(row.courier_name || ('#' + row.courier_user_id)) + '</td>'
                + '<td>' + esc(row.claims) + '</td>'
                + '<td>' + esc(row.released_by_hand) + '</td>'
                + '<td>' + esc(row.delivered) + '</td>'
                + '<td>' + share + '</td>'
                + '<td>' + num(row.late_minutes_avg, ' мин') + '</td>'
                + '<td>' + esc(row.outsourced) + '</td>'
                + '</tr>';
        }).join('');

        return '<table class="cdisp-table cdisp-table--ana"><thead><tr>'
            + '<th>Курьер</th><th>Брони</th><th>Снял</th><th>Доставок</th>'
            + '<th>Вовремя</th><th>Опоздание</th><th>Аутсорс</th>'
            + '</tr></thead><tbody>' + body + '</tbody>'
            + '<tfoot><tr>'
            + '<td>Итого</td>'
            + '<td>' + esc(t.claims || 0) + '</td>'
            + '<td>' + esc(t.released_by_hand || 0) + '</td>'
            + '<td>' + esc(t.delivered || 0) + '</td>'
            + '<td>' + num(t.on_time_share, ' %') + '</td>'
            + '<td>' + num(t.late_minutes_avg, ' мин') + '</td>'
            + '<td>' + esc(t.outsourced_after_claim || 0) + '</td>'
            + '</tr></tfoot></table>';
    }

    /**
     * Аккордеон детализации: заголовок с числом, тело — таблица.
     *
     * Число в заголовке берётся из СЧЁТЧИКА сервера, а не из длины массива:
     * длинные списки сервер обрезает, и «Ушли службе — 500» при тысяче таких
     * заказов было бы враньём ровно в той цифре, ради которой блок открывают.
     */
    function anaBlock(key, title, rows, total, tableHtml, hint) {
        var open = state.anaOpen[key];
        var shown = Math.min(rows.length, MAX_ROWS);
        var cut = total > shown
            ? '<p class="section-description">Показаны первые ' + esc(shown)
                + ' строк из ' + esc(total) + '. Сузьте период или выберите '
                + 'меньше салонов — либо выгрузите в Excel.</p>'
            : '';

        return '<div class="cdisp-block">'
            + '<button type="button" class="cdisp-block__head" data-cdisp-ana-block="'
            + esc(key) + '" aria-expanded="' + (open ? 'true' : 'false') + '">'
            + '<span class="cdisp-block__arrow">' + (open ? '▾' : '▸') + '</span> '
            + esc(title) + ' — ' + esc(total)
            + '</button>'
            + (open
                ? '<div class="cdisp-block__body">'
                    + (hint ? '<p class="section-description">' + hint + '</p>' : '')
                    + (rows.length ? tableHtml() : '<p class="section-description">Пусто</p>')
                    + cut
                    + '</div>'
                : '')
            + '</div>';
    }

    function anaLateTable(rows) {
        var body = capped(rows).map(function (row) {
            var plan = [row.time_from, row.time_to].filter(Boolean).join('–');
            return '<tr>'
                + '<td>' + esc(row.order_number || row.retailcrm_order_id) + '</td>'
                + '<td>' + esc(row.courier_name || '') + '</td>'
                + '<td>' + esc(row.site_name || '') + '</td>'
                + '<td>' + esc(row.delivery_date || '') + '</td>'
                + '<td>' + esc(plan || 'не задан') + '</td>'
                + '<td>' + esc(row.delivered_local || '') + '</td>'
                + '<td>+' + esc(row.late_minutes) + ' мин</td>'
                + '</tr>';
        }).join('');
        return '<table class="cdisp-table"><thead><tr>'
            + '<th>№ заказа</th><th>Курьер</th><th>Салон</th><th>Дата</th>'
            + '<th>План</th><th>Факт</th><th>Опоздание</th>'
            + '</tr></thead><tbody>' + body + '</tbody></table>';
    }

    function anaOutsourcedTable(rows) {
        var body = capped(rows).map(function (row) {
            return '<tr>'
                + '<td>' + esc(row.order_number || row.retailcrm_order_id) + '</td>'
                + '<td>' + esc(row.courier_name || '') + '</td>'
                + '<td>' + esc(row.site_name || '') + '</td>'
                + '<td>' + esc(row.delivery_date || '') + '</td>'
                + '<td>' + esc((row.released_at || '').slice(0, 16)) + '</td>'
                + '</tr>';
        }).join('');
        return '<table class="cdisp-table"><thead><tr>'
            + '<th>№ заказа</th><th>Курьер</th><th>Салон</th><th>Дата доставки</th>'
            + '<th>Когда сняли бронь</th>'
            + '</tr></thead><tbody>' + body + '</tbody></table>';
    }

    function anaNeverTable(rows) {
        var body = capped(rows).map(function (row) {
            var plan = [row.delivery_time_from, row.delivery_time_to]
                .filter(Boolean).join('–');
            return '<tr>'
                + '<td>' + esc(row.order_number || row.retailcrm_order_id) + '</td>'
                + '<td>' + esc(row.site_name || '') + '</td>'
                + '<td>' + esc(row.delivery_date || '') + '</td>'
                + '<td>' + esc(plan || 'не задан') + '</td>'
                + '</tr>';
        }).join('');
        return '<table class="cdisp-table"><thead><tr>'
            + '<th>№ заказа</th><th>Салон</th><th>Дата доставки</th><th>Интервал</th>'
            + '</tr></thead><tbody>' + body + '</tbody></table>';
    }

    /**
     * Выгрузка аналитики: свод по курьерам и три списка одним файлом.
     *
     * Период и салоны идут ПЕРВОЙ строкой файла. Без них выгрузка через неделю
     * бессмысленна: числа есть, а за что они — неизвестно.
     */
    function exportAnalytics() {
        var data = state.analytics;
        if (!data) {
            window.BarhatUI.alert('Нечего выгружать: данные ещё не загружены');
            return;
        }
        var t = data.totals || {};
        var applied = state.anaApplied;
        var sitesLabel = applied.sites.length
            ? applied.sites.map(siteName).join(', ') : 'все салоны';

        var lines = [
            ['Аналитика курьеров'].join(';'),
            ['Период', applied.period.from + ' — ' + applied.period.to].map(csvCell).join(';'),
            ['Салоны', sitesLabel].map(csvCell).join(';'),
            '',
            ['Курьер', 'Брони', 'Снял руками', 'Доставок', 'Вовремя, %',
             'Среднее опоздание, мин', 'Аутсорс', 'Посчитано доставок'].join(';')
        ];
        (data.couriers || []).forEach(function (row) {
            lines.push([
                csvCell(row.courier_name || ('#' + row.courier_user_id)),
                csvCell(row.claims), csvCell(row.released_by_hand),
                csvCell(row.delivered),
                csvCell(row.on_time_share === null ? '' : row.on_time_share),
                csvCell(row.late_minutes_avg === null ? '' : row.late_minutes_avg),
                csvCell(row.outsourced), csvCell(row.counted)
            ].join(';'));
        });
        lines.push(['Итого', t.claims || 0, t.released_by_hand || 0, t.delivered || 0,
                    t.on_time_share === null ? '' : t.on_time_share,
                    t.late_minutes_avg === null ? '' : t.late_minutes_avg,
                    t.outsourced_after_claim || 0, (t.on_time || 0) + (t.late || 0)]
            .map(csvCell).join(';'));

        lines.push('', ['Доставлены с опозданием'].join(';'));
        lines.push(['№ заказа', 'Курьер', 'Салон', 'Дата', 'План', 'Факт',
                    'Опоздание, мин'].join(';'));
        (data.late_orders || []).forEach(function (row) {
            lines.push([
                csvCell(row.order_number || row.retailcrm_order_id),
                csvCell(row.courier_name || ''), csvCell(row.site_name || ''),
                csvCell(row.delivery_date || ''),
                csvCell([row.time_from, row.time_to].filter(Boolean).join('–')),
                csvCell(row.delivered_local || ''), csvCell(row.late_minutes)
            ].join(';'));
        });

        lines.push('', ['Переданы службе после снятия брони'].join(';'));
        lines.push(['№ заказа', 'Курьер', 'Салон', 'Дата доставки',
                    'Когда сняли бронь'].join(';'));
        (data.outsourced_after_claim || []).forEach(function (row) {
            lines.push([
                csvCell(row.order_number || row.retailcrm_order_id),
                csvCell(row.courier_name || ''), csvCell(row.site_name || ''),
                csvCell(row.delivery_date || ''), csvCell(row.released_at || '')
            ].join(';'));
        });

        lines.push('', ['Ушли службе, не взяты никем'].join(';'));
        lines.push(['№ заказа', 'Салон', 'Дата доставки', 'Интервал'].join(';'));
        (data.outsourced_never_claimed || []).forEach(function (row) {
            lines.push([
                csvCell(row.order_number || row.retailcrm_order_id),
                csvCell(row.site_name || ''), csvCell(row.delivery_date || ''),
                csvCell([row.delivery_time_from, row.delivery_time_to]
                    .filter(Boolean).join('–'))
            ].join(';'));
        });

        // BOM — иначе Excel открывает кириллицу кракозябрами
        var blob = new Blob(['﻿' + lines.join('\r\n')],
                            { type: 'text/csv;charset=utf-8;' });
        var link = document.createElement('a');
        link.href = URL.createObjectURL(blob);
        link.download = 'аналитика-курьеров_' + applied.period.from
            + '_' + applied.period.to + '.csv';
        link.click();
        URL.revokeObjectURL(link.href);
    }

    function analyticsHtml() {
        if (!state.analytics) return anaFiltersHtml();

        var data = state.analytics;
        var t = data.totals || {};
        var late = data.late_orders || [];
        var outsourced = data.outsourced_after_claim || [];
        var never = data.outsourced_never_claimed || [];

        var counts = data.detail_totals || {};
        function total(key, rows) {
            return counts[key] === undefined ? rows.length : counts[key];
        }

        return anaFiltersHtml()
            + anaTilesHtml(t)
            + anaReconcileHtml(t)
            + anaCouriersHtml(data.couriers || [], t)
            + anaBlock('late', 'Доставлены с опозданием', late,
                total('late_orders', late),
                function () { return anaLateTable(late); })
            + anaBlock('outsourced', 'Переданы службе после снятия брони', outsourced,
                total('outsourced_after_claim', outsourced),
                function () { return anaOutsourcedTable(outsourced); })
            + anaBlock('never', 'Ушли службе, не взяты никем', never,
                total('outsourced_never_claimed', never),
                function () { return anaNeverTable(never); },
                'Курьер на эти заказы не нашёлся вовсе — это не про отказы, '
                + 'а про число людей и условия на непопулярные слоты.')
            + '<p class="section-description">Не считается: '
            + esc((data.not_measured || []).join('; '))
            + '. «Доставлено» — это отметка курьера в приложении, а не момент '
            + 'вручения: другого источника факта у нас нет.</p>';
    }

    // === Вкладка «Курьеры» ==================================================

    function couriersHtml() {
        if (!state.profiles) return '<p class="section-description">Загружаем…</p>';

        var withoutCrm = state.profiles.without_crm_link || [];
        var warning = withoutCrm.length
            ? '<p class="section-description" style="color:#c0322f">'
                + 'Не сопоставлены с CRM: ' + withoutCrm.length
                + '. Доставки этих курьеров не попадут в расчёт оплаты — '
                + 'её считают по полю «курьер» в CRM.</p>'
            : '';

        var rows = (state.profiles.profiles || []).map(function (profile) {
            return '<tr>'
                + '<td>' + esc(profile.username || profile.user_id) + '</td>'
                + '<td>' + citySelect(profile) + '</td>'
                + '<td>' + crmSelect(profile) + '</td>'
                + '<td>' + (profile.active
                    ? '<span class="cdisp-ok">работает</span>'
                    : '<span class="cdisp-note">отключён</span>') + '</td>'
                + '<td>' + (state.isAdmin ? profileActions(profile) : '') + '</td>'
                + '</tr>';
        }).join('');

        return warning
            + '<h3>Профили курьеров</h3>'
            + '<p class="section-description">Город решает, какие заказы курьер видит и '
            + 'кому уходят уведомления о новых. Связка с CRM решает, попадёт ли '
            + 'его работа в выплаты.</p>'
            + '<table class="cdisp-table"><thead><tr>'
            + '<th>Учётная запись</th><th>Город</th><th>Курьер в CRM</th>'
            + '<th>Состояние</th><th></th>'
            + '</tr></thead><tbody>' + (rows || '<tr><td colspan="5">Профилей пока нет</td></tr>')
            + '</tbody></table>'
            + (state.isAdmin ? addProfileHtml() : '')
            + (state.isAdmin ? resetPushHtml() : '');
    }

    /**
     * Переотправка уведомлений по уже известным заказам.
     *
     * Событие «новый заказ» уходит по каждому заказу ровно один раз — так
     * курьер не получает одно и то же дважды от двух воркеров. Обратная
     * сторона: если в момент прохода отправлять было некому, заказ молчит
     * навсегда. Так и вышло 17.09.2026 — курьер завёлся раньше, чем подписал
     * устройство.
     *
     * Кнопка, а не команда в консоли: консоли у контейнера на этом тарифе
     * Amvera нет, а разовые операции с боевой базой нужны регулярно.
     */
    function resetPushHtml() {
        return '<h3 style="margin-top:28px">Уведомления</h3>'
            + '<p class="section-description">Сообщение о новом заказе уходит по каждому '
            + 'заказу один раз. Если в тот момент ни одно устройство не было подписано, '
            + 'заказ больше не напомнит о себе. Кнопка ниже забывает отправленное за '
            + 'последние двое суток — уведомления по этим заказам придут заново '
            + 'ближайшим обновлением ленты.</p>'
            + '<button type="button" class="btn btn-secondary" data-reset-push="1">'
            + 'Переотправить уведомления за 2 дня</button>';
    }

    /**
     * Отключить и удалить — разные действия, и обе кнопки нужны.
     *
     * Отпуск и болезнь не повод терять город и связку с CRM, которые заводили
     * руками: для этого «отключить». «Удалить» — когда человек перестал быть
     * курьером совсем.
     */
    function profileActions(profile) {
        return '<div class="cdisp-form" style="margin:0">'
            + '<button class="btn btn-secondary" data-profile-toggle="'
            + esc(profile.user_id) + '" data-active="' + (profile.active ? '1' : '0') + '">'
            + (profile.active ? 'Отключить' : 'Включить') + '</button>'
            + '<button class="btn btn-danger" data-profile-delete="'
            + esc(profile.user_id) + '" data-name="'
            + esc(profile.username || profile.user_id) + '">Удалить</button>'
            + '</div>';
    }

    function citySelect(profile) {
        if (!state.isAdmin) return esc(profile.city || '— не задан —');
        var options = ['<option value="">— не задан —</option>'].concat(
            (state.profiles.cities || []).map(function (city) {
                return '<option value="' + esc(city) + '"'
                    + (city === profile.city ? ' selected' : '') + '>' + esc(city) + '</option>';
            }));
        return '<select class="form-input" data-profile-city="' + esc(profile.user_id) + '">'
            + options.join('') + '</select>';
    }

    function crmSelect(profile) {
        var current = profile.retailcrm_courier_id;
        if (!state.isAdmin) return esc(current || '— не связан —');
        var options = ['<option value="">— не связан —</option>'].concat(
            (state.profiles.couriers || []).map(function (courier) {
                return '<option value="' + esc(courier.id) + '"'
                    + (String(courier.id) === String(current) ? ' selected' : '') + '>'
                    + esc(courier.name) + '</option>';
            }));
        return '<select class="form-input" data-profile-crm="' + esc(profile.user_id) + '">'
            + options.join('') + '</select>';
    }

    function addProfileHtml() {
        var known = {};
        (state.profiles.profiles || []).forEach(function (p) { known[p.user_id] = true; });
        var candidates = state.users.filter(function (u) { return !known[u.id]; });
        if (!candidates.length) {
            return '<p class="section-description">Все учётные записи уже заведены</p>';
        }

        return '<h3 style="margin-top:24px">Добавить курьера</h3>'
            + '<div class="cdisp-form">'
            + '<select class="form-input" id="cdispNewUser" style="min-width:220px">'
            + candidates.map(function (u) {
                return '<option value="' + esc(u.id) + '" data-username="' + esc(u.username) + '">'
                    + esc(u.full_name || u.username) + ' (' + esc(u.role) + ')</option>';
            }).join('')
            + '</select>'
            + '<select class="form-input" id="cdispNewCity" style="min-width:180px">'
            + (state.profiles.cities || []).map(function (c) {
                return '<option value="' + esc(c) + '">' + esc(c) + '</option>';
            }).join('')
            + '</select>'
            + '<button class="btn btn-primary" id="cdispAddProfile">Завести профиль</button>'
            + '</div>';
    }

    // === Вкладки справочника и журнала ======================================

    function statusOptions(selected) {
        return ['<option value="">— не задан —</option>'].concat(
            state.statuses.map(function (status) {
                return '<option value="' + esc(status.code) + '"'
                    + (status.code === selected ? ' selected' : '') + '>'
                    + esc(status.name) + ' (' + esc(status.code) + ')</option>';
            })).join('');
    }

    function statusesHtml() {
        // Пустой статус блокирует не всякое действие: бронь без него просто
        // не меняет статус в CRM, а курьера проставляет. Считать её
        // «незаконченной настройкой» значило бы звать чинить исправное
        var blocked = state.actions.filter(function (a) {
            return !a.status_code && a.blocks_when_empty !== false;
        }).length;
        var rows = state.actions.map(function (item) {
            var state_cell;
            if (item.status_code) state_cell = '<span class="cdisp-ok">настроено</span>';
            else if (item.blocks_when_empty === false) {
                state_cell = '<span style="color:#6F6F6F">статус не меняем</span>';
            } else state_cell = '<span class="cdisp-bad">действие заблокировано</span>';
            return '<tr>'
                + '<td>' + esc(item.title) + '</td>'
                + '<td>' + (state.isAdmin
                    ? '<select class="form-input" data-action-status="' + esc(item.action)
                        + '" style="min-width:280px">' + statusOptions(item.status_code) + '</select>'
                    : esc(item.status_code || '— не задан —')) + '</td>'
                + '<td>' + state_cell + '</td>'
                + '<td>' + esc(item.updated_by || '') + '</td>'
                + '</tr>';
        }).join('');

        return (blocked
            ? '<p class="section-description" style="color:#c0322f">Не настроено действий: '
                + blocked + '. Пока статус не выбран, курьер не сможет отметить это действие.</p>'
            : '')
            + '<p class="section-description">Статус выбирается из справочника CRM. '
            + 'Выводить его из названия нельзя: названия меняют, и отправка сломается молча. '
            + 'У брони статус необязателен: пока он не выбран, бронь проставляет в CRM '
            + 'только курьера.</p>'
            + '<table class="cdisp-table"><thead><tr>'
            + '<th>Действие курьера</th><th>Статус в CRM</th><th>Состояние</th><th>Кто менял</th>'
            + '</tr></thead><tbody>' + rows + '</tbody></table>';
    }

    /**
     * Настройки по городам: лимит броней, горизонт бронирования, порог тревоги.
     *
     * Ручки существовали с самого начала модуля, а экрана к ним не было — и
     * лимит «3 заказа на курьера» жил дефолтом кода во всех девяти городах,
     * хотя в праздники курьер увозит шесть-восемь. Настройка без интерфейса
     * равна отсутствию настройки: менять её мог только тот, кто умеет слать
     * запросы руками.
     *
     * Пустое поле означает умолчание, и у лимита умолчание — «без
     * ограничения». Поэтому очистка поля и есть способ ограничение снять,
     * отдельной галочки для этого не нужно.
     */
    function citiesHtml() {
        if (!state.cities.length) {
            return '<p class="section-description">Городов пока нет: они приходят '
                + 'из справочника салонов.</p>';
        }

        // Строки помечаются НОМЕРОМ, а не названием города: название уходит в
        // разметку экранированным, а getAttribute отдаёт его обратно уже
        // расшифрованным. Для города с «&» или кавычкой сравнение строк не
        // совпало бы ни с одним полем — ушёл бы пустой payload, и настройки
        // сбросились бы к умолчаниям под тостом «сохранено».
        var rows = state.cities.map(function (item, index) {
            function cell(field, value, placeholder, min, max) {
                if (!state.isAdmin) {
                    return '<td>' + esc(value === null || value === undefined
                        ? placeholder : value) + '</td>';
                }
                return '<td><input type="number" class="form-input"'
                    + ' data-city-field="' + field + '" data-city-row="' + index + '"'
                    + ' min="' + min + '" max="' + max + '" style="width:120px"'
                    + ' placeholder="' + esc(placeholder) + '"'
                    + ' value="' + (value === null || value === undefined ? '' : esc(value))
                    + '"></td>';
            }
            return '<tr>'
                + '<td>' + esc(item.city) + '</td>'
                + cell('max_active_claims', item.max_active_claims, 'без ограничения', 1, 50)
                + cell('claim_horizon_days', item.claim_horizon_days, '1', 0, 14)
                + cell('unclaimed_alert_minutes', item.unclaimed_alert_minutes, '90', 5, 1440)
                + '<td>' + (state.isAdmin
                    ? '<button class="btn btn-sm btn-primary" data-city-save="' + index
                        + '">Сохранить</button>'
                    : '') + '</td>'
                + '</tr>';
        }).join('');

        return '<p class="section-description">'
            + '<b>Лимит броней</b> — сколько заказов курьер может держать на руках '
            + 'одновременно (взятые и забранные; доставленные слот освобождают). '
            + 'Пусто — ограничения нет. '
            + '<b>Горизонт</b> — на сколько дней вперёд можно бронировать (0 — только '
            + 'сегодня). <b>«Никто не взял»</b> — за сколько минут до окна доставки '
            + 'свободный заказ попадает в тревожный список.</p>'
            + (state.isAdmin ? '' : '<p class="section-description">Менять настройки '
                + 'может администратор.</p>')
            + '<table class="cdisp-table"><thead><tr>'
            + '<th>Город</th><th>Лимит броней</th><th>Горизонт, дней</th>'
            + '<th>«Никто не взял», мин</th><th></th>'
            + '</tr></thead><tbody>' + rows + '</tbody></table>';
    }

    function outboxHtml() {
        if (!state.outbox.length) return '<p class="section-description">Отправок пока не было</p>';
        var rows = state.outbox.map(function (item) {
            var color = item.state === 'sent' ? '#0a7d3f'
                : (item.state === 'failed' ? '#c0322f' : '#6F6F6F');
            var label = item.state === 'sent' ? 'отправлено'
                : (item.state === 'failed' ? 'не ушло' : 'в очереди');
            // Пустой статус — это бронь: она отправляет только курьера и в
            // поле статуса не пишет вовсе. Прочерк честнее пустой ячейки,
            // которая читается как «потеряли значение».
            var target = item.target_status ? esc(item.target_status)
                : '<span style="color:#6F6F6F">статус не меняем</span>';
            // Повтор — только у застрявших: отправленное повторять нечего, а
            // ждущее очереди уедет само ближайшим тиком.
            var retry = item.state === 'sent' ? ''
                : '<button class="btn btn-secondary btn-sm" data-outbox-retry="'
                  + esc(item.id) + '">Повторить</button>';
            return '<tr>'
                + '<td>' + esc(item.order_number || item.retailcrm_order_id) + '</td>'
                + '<td>' + esc(item.action_title) + '</td>'
                + '<td>' + target + '</td>'
                + '<td style="color:' + color + '">' + esc(label) + '</td>'
                + '<td>' + esc(item.attempts) + '</td>'
                + '<td>' + esc(item.error_message || '') + '</td>'
                + '<td>' + esc(item.sent_at || item.created_at) + '</td>'
                + '<td>' + retry + '</td>'
                + '</tr>';
        }).join('');

        return '<p class="section-description">Что мы отправили и что ответила CRM. '
            + 'Отметка курьера сохраняется сразу, наружу уходит фоном. '
            + 'Отказ CRM сам не повторяется — нажмите «Повторить», когда причина устранена.</p>'
            + '<table class="cdisp-table"><thead><tr>'
            + '<th>Заказ</th><th>Действие</th><th>Статус CRM</th><th>Итог</th>'
            + '<th>Попыток</th><th>Ответ</th><th>Когда</th><th></th>'
            + '</tr></thead><tbody>' + rows + '</tbody></table>';
    }

    // === Отрисовка ==========================================================

    function render() {
        var host = document.getElementById('cdispRoot');
        if (!host) return;

        var tabs = TABS.map(function (tab) {
            return '<button class="btn ' + (tab.id === state.tab ? 'btn-primary' : 'btn-secondary')
                + '" data-cdisp-tab="' + tab.id + '">' + esc(tab.title) + '</button>';
        }).join(' ');

        var body;
        if (state.loading && state.tab === 'analytics' && state.analytics) {
            // То же правило, что и в «Доставке»: пока есть что показать, экран
            // не очищаем. Иначе «медленно» превращается в «пусто», а поля
            // фильтра не поправить, не дождавшись длинного запроса.
            body = '<p class="section-description">Обновляем…</p>' + analyticsHtml();
        } else if (state.loading && state.tab === 'today' && state.overview) {
            // Экран не очищаем, пока есть что показать: «медленно»
            // превращается в «пусто», и человек видит сломанный модуль вместо
            // задержки (CLAUDE.md). Заодно поля периода остаются на месте —
            // иначе их не поправить, не дождавшись длинного запроса.
            body = '<p class="section-description">Обновляем…</p>' + todayHtml();
        } else if (state.loading) {
            body = '<p class="section-description">Загружаем…</p>';
        } else if (state.tab === 'today') body = todayHtml();
        else if (state.tab === 'couriers') body = couriersHtml();
        else if (state.tab === 'statuses') body = statusesHtml();
        else if (state.tab === 'cities') body = citiesHtml();
        else if (state.tab === 'analytics') body = analyticsHtml();
        else body = outboxHtml();

        host.innerHTML = '<div class="cdisp-tabs">'
            + tabs + '</div>' + body;
    }

    // === Обработчики ========================================================

    document.addEventListener('click', function (event) {
        if (!event.target.closest) return;

        var tab = event.target.closest('[data-cdisp-tab]');
        if (tab) {
            state.tab = tab.getAttribute('data-cdisp-tab');
            loadTab();
            return;
        }

        var period = event.target.closest('[data-cdisp-period]');
        if (period) {
            var fromEl = document.getElementById('cdispPeriodFrom');
            var toEl = document.getElementById('cdispPeriodTo');
            if (!fromEl || !toEl) {
                toast('Поля периода не найдены, обновите экран', 'error');
                return;
            }
            if (!fromEl.value || !toEl.value) {
                toast('Задайте обе даты периода', 'error');
                return;
            }
            // Понятную половину проверок делаем здесь, чтобы не гонять заведомо
            // отвергаемый запрос. Предел длины остаётся за сервером: держать
            // одно и то же число в двух местах значит однажды их развести.
            if (fromEl.value > toEl.value) {
                toast('Начало периода позже конца', 'error');
                return;
            }
            state.period = { from: fromEl.value, to: toEl.value };
            // Кнопка гасится на время запроса; loadTab перерисует панель
            // целиком и вернёт её живой в любом исходе
            period.disabled = true;
            loadTab();
            return;
        }

        // --- Вкладка «Аналитика» --------------------------------------------

        if (event.target.closest('[data-cdisp-sites-toggle]')) {
            state.anaSitesOpen = !state.anaSitesOpen;
            render();
            return;
        }

        var siteOff = event.target.closest('[data-cdisp-site-off]');
        if (siteOff) {
            var offCode = siteOff.getAttribute('data-cdisp-site-off');
            state.anaPicked = state.anaPicked.filter(function (c) { return c !== offCode; });
            render();
            return;
        }

        if (event.target.closest('[data-cdisp-sites-clear]')) {
            state.anaPicked = [];
            render();
            return;
        }

        var siteBox = event.target.closest('[data-cdisp-site]');
        if (siteBox) {
            var code = siteBox.getAttribute('data-cdisp-site');
            var chosen = state.anaPicked.indexOf(code) === -1;
            state.anaPicked = chosen
                ? state.anaPicked.concat([code])
                : state.anaPicked.filter(function (c) { return c !== code; });
            // Список салонов перерисовывается целиком — вернём ему прокрутку,
            // иначе на длинном справочнике каждый щелчок выбрасывает человека
            // в начало списка (CLAUDE.md про innerHTML).
            var list = document.querySelector('[data-cdisp-ms-list]');
            var scroll = list ? list.scrollTop : 0;
            render();
            var again = document.querySelector('[data-cdisp-ms-list]');
            if (again) again.scrollTop = scroll;
            return;
        }

        var anaBlockBtn = event.target.closest('[data-cdisp-ana-block]');
        if (anaBlockBtn) {
            var blockKey = anaBlockBtn.getAttribute('data-cdisp-ana-block');
            state.anaOpen[blockKey] = !state.anaOpen[blockKey];
            render();
            return;
        }

        var anaApply = event.target.closest('[data-cdisp-ana-apply]');
        if (anaApply) {
            var anaFrom = document.getElementById('cdispAnaFrom');
            var anaTo = document.getElementById('cdispAnaTo');
            if (!anaFrom || !anaTo) {
                toast('Поля периода не найдены, обновите экран', 'error');
                return;
            }
            if (!anaFrom.value || !anaTo.value) {
                toast('Задайте обе даты периода', 'error');
                return;
            }
            if (anaFrom.value > anaTo.value) {
                toast('Начало периода позже конца', 'error');
                return;
            }
            state.anaPeriod = { from: anaFrom.value, to: anaTo.value };
            state.anaSitesOpen = false;
            anaApply.disabled = true;
            loadTab();
            return;
        }

        if (event.target.closest('[data-cdisp-ana-excel]')) {
            exportAnalytics();
            return;
        }

        if (event.target.closest('[data-cdisp-filter-reset]')) {
            state.filters = Object.assign({}, EMPTY_FILTERS);
            // Здесь перерисовываем экран целиком: поля фильтра сами обязаны
            // опустеть, а точечное обновление таблицы их не трогает
            render();
            return;
        }

        var exportBtn = event.target.closest('[data-cdisp-export]');
        if (exportBtn) {
            exportOrders();
            return;
        }

        var citySave = event.target.closest('[data-city-save]');
        if (citySave) {
            var rowIndex = citySave.getAttribute('data-city-save');
            var item = (state.cities || [])[Number(rowIndex)];
            if (!item) { toast('Строка города не найдена, обновите экран', 'error'); return; }
            var city = item.city;
            var payload = {};
            var fields = document.querySelectorAll('[data-city-row][data-city-field]');
            Array.prototype.forEach.call(fields, function (input) {
                if (input.getAttribute('data-city-row') !== rowIndex) return;
                // Пустое поле отправляем как есть: на сервере это «вернуть к
                // умолчанию», а у лимита умолчание — «без ограничения»
                var raw = input.value.trim();
                payload[input.getAttribute('data-city-field')] = raw === '' ? null : raw;
            });
            // Ни одного поля не нашлось — значит разметка и обработчик
            // разъехались. Пустой payload сервер понял бы как «ничего не
            // менять», но молчать об этом нельзя: человек нажал «Сохранить»
            if (!Object.keys(payload).length) {
                toast('Поля настроек не найдены, обновите экран', 'error');
                return;
            }
            // Кнопка гасится на время запроса: медленный ответ иначе
            // превращает один клик в три (правило CLAUDE.md)
            citySave.disabled = true;
            post('/api/courier/city-settings/' + encodeURIComponent(city), payload)
                .then(function () {
                    toast('Настройки города сохранены', 'success');
                    return loadTab();
                })
                .catch(function (error) {
                    citySave.disabled = false;
                    toast(error.message, 'error');
                });
            return;
        }

        var resetPush = event.target.closest('[data-reset-push]');
        if (resetPush) {
            resetPush.disabled = true;
            window.BarhatUI.confirm(
                'Курьеры получат уведомления по заказам за последние двое суток заново. '
                + 'Если устройства уже подписаны, это будет пачка сообщений.',
                { title: 'Переотправить уведомления', confirmText: 'Переотправить',
                  cancelText: 'Отмена' }
            ).then(function (ok) {
                if (!ok) { resetPush.disabled = false; return; }
                return post('/api/courier/push/reset-events', { days: 2 })
                    .then(function (data) {
                        toast('Забыто событий: ' + (data && data.removed)
                            + '. Уведомления уйдут ближайшим обновлением ленты', 'success');
                    })
                    .catch(function (e) { toast(e.message, 'error'); })
                    .then(function () { resetPush.disabled = false; });
            });
            return;
        }

        var release = event.target.closest('[data-release-order]');
        if (release) {
            release.disabled = true;
            window.BarhatUI.confirm('Снять бронь с этого заказа?', {
                title: 'Снять чужую бронь', confirmText: 'Снять', cancelText: 'Отмена'
            }).then(function (ok) {
                if (!ok) { release.disabled = false; return; }
                return post('/api/courier/assignments/'
                    + encodeURIComponent(release.getAttribute('data-release-order')) + '/release')
                    .then(function () { toast('Бронь снята', 'success'); return loadTab(); })
                    .catch(function (error) {
                        toast(error.message, 'error');
                        release.disabled = false;
                    });
            });
            return;
        }

        var retry = event.target.closest('[data-outbox-retry]');
        if (retry) {
            // Блокируем на время запроса: медленный клик иначе превращается в
            // три отправки, а кнопка всё это время выглядит живой
            retry.disabled = true;
            post('/api/courier/outbox/'
                 + encodeURIComponent(retry.getAttribute('data-outbox-retry')) + '/retry')
                .then(function () {
                    toast('Отправка вернулась в очередь — уйдёт в течение минуты', 'success');
                    return loadTab();
                })
                .catch(function (error) {
                    toast(error.message, 'error');
                    retry.disabled = false;
                });
            return;
        }

        var toggle = event.target.closest('[data-profile-toggle]');
        if (toggle) {
            var turnOn = toggle.getAttribute('data-active') !== '1';
            toggle.disabled = true;
            post('/api/courier/profiles/'
                 + encodeURIComponent(toggle.getAttribute('data-profile-toggle')) + '/active',
                 { active: turnOn })
                .then(function () {
                    toast(turnOn ? 'Курьер включён' : 'Курьер отключён', 'success');
                    state.profiles = null;
                    return loadTab();
                })
                .catch(function (error) {
                    toast(error.message, 'error');
                    toggle.disabled = false;
                });
            return;
        }

        var remove = event.target.closest('[data-profile-delete]');
        if (remove) {
            var userId = remove.getAttribute('data-profile-delete');
            remove.disabled = true;
            // Удаление профиля необратимо, и подтверждение тут не формальность:
            // город и связку с CRM заводили руками
            window.BarhatUI.confirm(
                'Убрать ' + remove.getAttribute('data-name') + ' из курьеров? '
                + 'Учётная запись останется, город и связка с CRM пропадут.',
                { title: 'Удалить профиль курьера', confirmText: 'Удалить',
                  cancelText: 'Отмена' }
            ).then(function (ok) {
                if (!ok) { remove.disabled = false; return; }
                return del('/api/courier/profiles/' + encodeURIComponent(userId))
                    .then(function () {
                        toast('Профиль удалён', 'success');
                        state.profiles = null;
                        return loadTab();
                    })
                    .catch(function (error) {
                        // Живые брони — штатный отказ с внятным текстом,
                        // а не сбой: заказы остались бы без владельца
                        toast(error.message, 'error');
                        remove.disabled = false;
                    });
            });
            return;
        }

        if (event.target.id === 'cdispAddProfile') {
            var userSelect = document.getElementById('cdispNewUser');
            var citySelectEl = document.getElementById('cdispNewCity');
            if (!userSelect || !citySelectEl) return;
            var option = userSelect.options[userSelect.selectedIndex];
            event.target.disabled = true;
            post('/api/courier/profiles/' + encodeURIComponent(userSelect.value), {
                username: option.getAttribute('data-username'),
                city: citySelectEl.value,
                active: true
            }).then(function () {
                toast('Профиль заведён', 'success');
                state.profiles = null;
                return loadTab();
            }).catch(function (error) {
                toast('Не сохранилось: ' + error.message, 'error');
                event.target.disabled = false;
            });
        }
    });

    /*
     * Фильтр слушаем и по `input`, и по `change`: у текстового поля нужен
     * первый (иначе список обновится только на уходе фокуса), у select в
     * старых браузерах приходит только второй. Обработчик идемпотентен, и
     * двойной вызов ничего не стоит — данные уже в памяти.
     */
    function onFilterEvent(event) {
        if (!event.target.closest) return;
        var control = event.target.closest('[data-cdisp-filter]');
        if (!control) return;
        var field = control.getAttribute('data-cdisp-filter');
        if (!(field in EMPTY_FILTERS)) return;
        state.filters[field] = control.value;
        refreshOrders();
    }

    document.addEventListener('input', onFilterEvent);
    document.addEventListener('change', onFilterEvent);

    document.addEventListener('change', function (event) {
        if (!event.target.closest) return;

        var statusSelect = event.target.closest('[data-action-status]');
        if (statusSelect) {
            var action = statusSelect.getAttribute('data-action-status');
            statusSelect.disabled = true;
            post('/api/courier/action-statuses/' + encodeURIComponent(action),
                 { status_code: statusSelect.value })
                .then(function (actions) {
                    state.actions = actions;
                    toast('Сохранено', 'success');
                    render();
                })
                .catch(function (error) {
                    toast('Не сохранилось: ' + error.message, 'error');
                    statusSelect.disabled = false;
                });
            return;
        }

        var citySel = event.target.closest('[data-profile-city]');
        var crmSel = event.target.closest('[data-profile-crm]');
        var target = citySel || crmSel;
        if (!target) return;

        var userId = target.getAttribute(citySel ? 'data-profile-city' : 'data-profile-crm');
        var profile = (state.profiles.profiles || []).filter(function (p) {
            return String(p.user_id) === String(userId);
        })[0] || {};

        target.disabled = true;
        post('/api/courier/profiles/' + encodeURIComponent(userId), {
            username: profile.username,
            city: citySel ? citySel.value : profile.city,
            retailcrm_courier_id: crmSel ? crmSel.value : profile.retailcrm_courier_id,
            active: profile.active !== 0
        }).then(function () {
            toast('Сохранено', 'success');
            state.profiles = null;
            return loadTab();
        }).catch(function (error) {
            // Связка с CRM уникальна: занята другой учёткой — это ошибка
            // ввода, а не сбой, и текст у неё внятный
            toast('Не сохранилось: ' + error.message, 'error');
            target.disabled = false;
        });
    });

    window.CourierDispatchModule = {
        onPageActivated: function (user) {
            state.isAdmin = !!(user && user.role === 'admin');
            loadTab();
        }
    };
})();
