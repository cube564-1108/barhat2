/*
 * Экран курьера (PWA), Фаза 3 плана «Курьеры: доставка заказов».
 *
 * Что здесь важно понимать до правок:
 *
 * 1. **Фильтровать данные фронт не может и не должен.** Город, тип доставки и
 *    видимые статусы применяет сервер; контактов до брони он просто не
 *    присылает. Здесь нечего прятать — здесь показывают то, что пришло.
 * 2. **Состояние заказа берётся из наших полей** (`is_free`, `is_mine`,
 *    `assignment_state`, `is_ready`), а не из `status` CRM: между действием
 *    курьера и его отражением в CRM проходит до минуты (находка К1 критики).
 * 3. **Нативные диалоги внутри Пульса молча игнорируются** — только
 *    `window.BarhatUI`.
 * 4. **Перерисовка через innerHTML теряет прокрутку** — правило CLAUDE.md.
 *    Лента перерисовывается каждые 30 секунд, и без возврата прокрутки курьера
 *    выбрасывало бы наверх посреди чтения.
 */

(function () {
    'use strict';

    var REFRESH_MS = 30000;

    // Выбранные салоны переживают перезапуск: курьер собирает ходку из
    // двух-трёх точек и не должен выставлять их заново после каждого закрытия
    // приложения. Ключ с префиксом — на странице живут и другие модули.
    var SITES_KEY = 'courier-app:sites';

    var state = {
        orders: [],
        filter: 'free',
        sites: [],          // коды выбранных салонов; пусто = все
        city: null,
        profileWarning: null,
        loadedAt: null,     // когда лента последний раз пришла с сервера
        stale: false,       // последняя попытка не удалась
        loading: false,
        openOrderId: null,
        pushKey: null,      // публичный VAPID; null = пуши не настроены
        pushOn: false       // подписка этого устройства оформлена
    };

    var el = {};

    // === Утилиты ============================================================

    /**
     * Экранирование для вставки в HTML и в значения атрибутов.
     *
     * Именно так, а не через `div.textContent`: тот приём не экранирует кавычку,
     * и любое значение с ней рвало атрибут `value="..."`. Эта ошибка уже была
     * продублирована в четырёх файлах дашборда.
     */
    function esc(value) {
        if (value === null || value === undefined) return '';
        return String(value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function toast(message, kind) {
        if (window.BarhatUI) window.BarhatUI.toast(message, kind || 'info');
    }

    /**
     * Стенные часы салона в миллисекундах — в той же шкале, что и время
     * доставки из CRM.
     *
     * Время доставки менеджер вводит так, как его видит флорист: «14:00» в
     * Екатеринбурге и «14:00» в Новосибирске — разные моменты. Пояс устройства
     * курьера к этому отношения не имеет (он может ехать с телефоном,
     * настроенным на что угодно), поэтому сравниваем UTC-время с поправкой на
     * пояс салона, а не локальное время браузера. Те же три шкалы разведены на
     * сервере в src/couriers/salon_time.py.
     */
    function salonNowMs(utcOffset) {
        if (utcOffset === null || utcOffset === undefined) return null;
        return Date.now() + utcOffset * 3600000;
    }

    function slotStartMs(order) {
        if (!order.delivery_date || !order.delivery_time_from) return null;
        var d = order.delivery_date.split('-');
        var t = order.delivery_time_from.split(':');
        if (d.length !== 3 || t.length < 2) return null;
        return Date.UTC(+d[0], +d[1] - 1, +d[2], +t[0], +t[1]);
    }

    /** «через 1 ч 20 мин» / «через 15 мин» / «время вышло». */
    function countdown(order) {
        var now = salonNowMs(order.utc_offset);
        var start = slotStartMs(order);
        if (now === null || start === null) return null;

        var minutes = Math.round((start - now) / 60000);
        if (minutes < 0) return { text: 'время вышло', late: true };
        if (minutes < 60) return { text: 'через ' + minutes + ' мин', late: minutes <= 15 };

        var hours = Math.floor(minutes / 60);
        var rest = minutes % 60;
        return {
            text: 'через ' + hours + ' ч' + (rest ? ' ' + rest + ' мин' : ''),
            late: false
        };
    }

    function slotText(order) {
        if (order.delivery_time_from && order.delivery_time_to) {
            return order.delivery_time_from + '–' + order.delivery_time_to;
        }
        if (order.delivery_time_from) return 'с ' + order.delivery_time_from;
        return 'время уточняется';
    }

    /** Телефон для tel:. Всё лишнее (скобки, пробелы, дефисы) убираем. */
    function telHref(phone) {
        var digits = String(phone || '').replace(/[^\d+]/g, '');
        return digits ? 'tel:' + digits : null;
    }

    // === Загрузка данных ====================================================

    function apiGet(url) {
        return fetch(url, { credentials: 'same-origin' }).then(function (response) {
            if (!response.ok) throw new Error('HTTP ' + response.status);
            return response.json();
        }).then(function (payload) {
            if (!payload || payload.success !== true) {
                throw new Error((payload && payload.error) || 'Сервер вернул ошибку');
            }
            return payload;
        });
    }

    function loadProfile() {
        return apiGet('/api/courier/profile').then(function (payload) {
            state.city = payload.data.city;
            state.profileWarning = payload.data.warning;
        }).catch(function () {
            // Профиль — не повод не показать ленту: без него просто нет подписи
        });
    }

    function loadFeed() {
        if (state.loading) return Promise.resolve();
        state.loading = true;
        renderBusy();

        return apiGet('/api/courier/orders').then(function (payload) {
            state.orders = payload.data || [];
            state.loadedAt = new Date();
            state.stale = false;
            if (payload.meta && payload.meta.warning) {
                state.profileWarning = payload.meta.warning;
            }
            if (payload.meta && payload.meta.city) state.city = payload.meta.city;
        }).catch(function (error) {
            // Показываем последнее загруженное с честной пометкой: цифра
            // вчерашней свежести полезнее прочерка, но врать про актуальность
            // нельзя.
            state.stale = true;
            if (!state.loadedAt) toast('Не удалось загрузить заказы: ' + error.message, 'error');
        }).then(function () {
            state.loading = false;
            render();
            if (state.openOrderId) refreshOpenCard();
        });
    }

    // === Салоны забора ======================================================

    /**
     * Хранилище может быть недоступно (приватный режим, отключённые cookie).
     * Фильтр по салону — удобство, а не данные: не сохранилось так не
     * сохранилось, приложение из-за этого падать не должно.
     */
    function loadSites() {
        try {
            var raw = window.localStorage.getItem(SITES_KEY);
            var parsed = raw ? JSON.parse(raw) : [];
            return Array.isArray(parsed) ? parsed.map(String) : [];
        } catch (e) {
            return [];
        }
    }

    function saveSites() {
        try {
            window.localStorage.setItem(SITES_KEY, JSON.stringify(state.sites));
        } catch (e) { /* см. loadSites */ }
    }

    function siteKey(order) {
        return String(order.site_code || order.site_name || '');
    }

    /** Салоны, встречающиеся в ленте, со счётчиком по текущему табу. */
    function siteOptions() {
        var byTab = ordersByTab();
        var map = {};
        state.orders.forEach(function (order) {
            var key = siteKey(order);
            if (!key) return;
            if (!map[key]) {
                map[key] = { code: key, name: order.site_name || order.city || key, count: 0 };
            }
        });
        byTab.forEach(function (order) {
            var entry = map[siteKey(order)];
            if (entry) entry.count += 1;
        });
        // Выбранный салон, из которого заказы разобрали, обязан остаться в
        // ряду: иначе список пуст, а причины на экране нет.
        state.sites.forEach(function (code) {
            if (!map[code]) map[code] = { code: code, name: code, count: 0 };
        });
        return Object.keys(map)
            .map(function (key) { return map[key]; })
            .sort(function (a, b) { return a.name.localeCompare(b.name, 'ru'); });
    }

    function toggleSite(code) {
        var index = state.sites.indexOf(code);
        if (index === -1) state.sites.push(code);
        else state.sites.splice(index, 1);
        saveSites();
        render();              // лента и счётчики обновляются сразу за выбором
        if (!el.sites.hidden) renderSitesSheet();
    }

    /**
     * Подпись на кнопке. Названия перечисляем, пока их немного: «Восход,
     * Заря» отвечает на вопрос сразу, а «выбрано 2» заставляет открывать
     * список, чтобы вспомнить, что именно выбрано.
     */
    function sitesLabel(options) {
        if (!state.sites.length) return 'Все салоны';

        var names = state.sites.map(function (code) {
            var found = options.filter(function (o) { return o.code === code; })[0];
            return found ? found.name : code;
        });
        if (names.length <= 2) return names.join(', ');
        return 'Салоны: ' + names.length + ' из ' + options.length;
    }

    /** Строка постоянной высоты под фильтрами; сам выбор — отдельным слоем. */
    function renderSiteBar() {
        var options = siteOptions();

        // Один салон — строка не нужна: выбор из одного варианта только
        // занимает место на маленьком экране.
        if (options.length < 2) {
            el.siteBar.hidden = true;
            return;
        }
        el.siteBar.hidden = false;
        el.sitesLabel.textContent = sitesLabel(options);
        el.sitesOpen.classList.toggle('cd-sitebtn--on', state.sites.length > 0);
    }

    function openSites() {
        el.sites.hidden = false;
        renderSitesSheet();
        history.pushState({ courierLayer: 'sites' }, '');
    }

    function closeSites(fromHistory) {
        el.sites.hidden = true;
        el.sites.innerHTML = '';
        if (!fromHistory) historyBackIfOurs();
    }

    function renderSitesSheet() {
        var options = siteOptions();
        var rows = options.map(function (option) {
            var on = state.sites.indexOf(option.code) !== -1;
            return '<button type="button" class="cd-siterow' + (on ? ' cd-siterow--on' : '')
                + '" data-site="' + esc(option.code) + '"'
                + ' aria-pressed="' + (on ? 'true' : 'false') + '">'
                + '<span class="cd-siterow__box">'
                + '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"'
                + ' stroke-linecap="round" stroke-linejoin="round" width="16" height="16">'
                + '<path d="M20 6L9 17l-5-5"></path></svg></span>'
                + '<span class="cd-siterow__name">' + esc(option.name) + '</span>'
                + '<span class="cd-siterow__count">' + option.count + '</span>'
                + '</button>';
        }).join('');

        // Прокрутку слоя возвращаем: он перерисовывается на каждый выбор, а
        // салонов может быть больше, чем помещается на экран.
        var scroll = el.sites.scrollTop;
        el.sites.innerHTML = '<div class="cd-sheet__head">'
            + '<button type="button" class="cd-sheet__back" data-sites-close="1" aria-label="Назад">'
            + '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75"'
            + ' stroke-linecap="round" stroke-linejoin="round" width="22" height="22">'
            + '<path d="M19 12H5"></path><path d="M12 19l-7-7 7-7"></path></svg></button>'
            + '<span class="cd-sheet__title">Салоны забора</span></div>'
            + '<div class="cd-sheet__body">'
            + '<section class="cd-block" style="padding:0;overflow:hidden">' + rows + '</section>'
            + '<button type="button" class="cd-btn cd-btn--ghost" data-site-reset="1">'
            + 'Показать все салоны</button>'
            + '<button type="button" class="cd-btn" data-sites-close="1">Готово</button>'
            + '</div>';
        el.sites.scrollTop = scroll;
    }

    // === Отбор и порядок ====================================================

    /** Заказы под текущим табом, БЕЗ фильтра по салону (по ним считаем салоны). */
    function ordersByTab() {
        return state.orders.filter(function (order) {
            if (state.filter === 'free') return order.is_free;
            if (state.filter === 'ready') return order.is_ready;
            if (state.filter === 'mine') return order.is_mine;
            return true;
        });
    }

    function visibleOrders() {
        var list = ordersByTab().filter(function (order) {
            // Пустой выбор — это «все салоны», а не «ни одного»
            if (!state.sites.length) return true;
            return state.sites.indexOf(siteKey(order)) !== -1;
        });

        // Сервер уже отдал заказы по времени доставки. Поднимаем наверх
        // готовые: это то, что можно забирать прямо сейчас, — и ровно так это
        // описано в §8 плана. Сортировка устойчивая, внутри группы порядок
        // сервера сохраняется.
        return list
            .map(function (order, index) { return { order: order, index: index }; })
            .sort(function (a, b) {
                if (a.order.is_ready !== b.order.is_ready) return a.order.is_ready ? -1 : 1;
                return a.index - b.index;
            })
            .map(function (item) { return item.order; });
    }

    function counts() {
        // Счётчики на табах считаются по выбранным салонам: иначе «Свободные 7»
        // при пустом списке — не подсказка, а враньё.
        var scope = state.orders.filter(function (order) {
            if (!state.sites.length) return true;
            return state.sites.indexOf(siteKey(order)) !== -1;
        });
        return {
            free: scope.filter(function (o) { return o.is_free; }).length,
            ready: scope.filter(function (o) { return o.is_ready; }).length,
            mine: scope.filter(function (o) { return o.is_mine; }).length,
            all: scope.length
        };
    }

    // === Отрисовка ленты ====================================================

    function renderBusy() {
        if (el.refresh) el.refresh.setAttribute('data-busy', state.loading ? '1' : '0');
    }

    function availabilityBadge(order) {
        if (order.is_mine) {
            return order.assignment_state === 'picked_up'
                ? '<span class="cd-badge cd-badge--mine">У меня</span>'
                : '<span class="cd-badge cd-badge--mine">Мой</span>';
        }
        if (order.is_free) return '<span class="cd-badge cd-badge--free">Свободен</span>';
        // С именем, а не глухое «Занят»: курьер видит, что происходит со
        // всеми заказами города, и не звонит выяснять, свободен ли заказ
        var who = order.assignment_courier_name;
        var what = order.assignment_state === 'picked_up' ? 'Забрал' : 'Взял';
        return '<span class="cd-badge cd-badge--taken">'
            + esc(who ? what + ' ' + who : 'Занят') + '</span>';
    }

    function readyBadge(order) {
        return order.is_ready
            ? '<span class="cd-badge cd-badge--ready">Готов</span>'
            : '<span class="cd-badge cd-badge--cooking">Собирают</span>';
    }

    function cardHtml(order) {
        var tick = countdown(order);
        var classes = ['cd-card'];
        if (order.is_mine) classes.push('cd-card--mine');
        else if (order.is_ready) classes.push('cd-card--ready');

        var parts = [];
        parts.push('<article class="' + classes.join(' ') + '" data-order="'
            + esc(order.retailcrm_order_id) + '">');
        parts.push('<div class="cd-card__top">');
        parts.push('<span class="cd-card__number">№ ' + esc(order.order_number || order.retailcrm_order_id) + '</span>');
        parts.push(availabilityBadge(order));
        parts.push(readyBadge(order));
        parts.push('</div>');

        parts.push('<p class="cd-card__route"><span class="cd-card__site">'
            + esc(order.site_name || order.city || '') + '</span>'
            + '<span class="cd-card__arrow">→</span>'
            + esc(order.address_text || 'адрес не указан') + '</p>');

        parts.push('<p class="cd-card__time">' + esc(slotText(order))
            + (tick ? ' · <span class="cd-card__countdown'
                + (tick.late ? ' cd-card__countdown--late' : '') + '">'
                + esc(tick.text) + '</span>' : '')
            + '</p>');

        // Флаг «не связываться» виден уже в ленте, если контакты открыты:
        // курьер должен узнать об этом раньше, чем возьмётся за телефон.
        if (order.do_not_contact_recipient) {
            parts.push('<p class="cd-card__flag">Не связываться с получателем</p>');
        }

        parts.push('<div class="cd-card__actions">');
        if (order.is_mine) {
            parts.push('<div class="cd-btn-row">'
                + '<button type="button" class="cd-btn cd-btn--ghost" data-release="'
                + esc(order.retailcrm_order_id) + '">Отказаться</button>'
                + '<button type="button" class="cd-btn" data-open="'
                + esc(order.retailcrm_order_id) + '">Открыть заказ</button></div>');
        } else if (order.is_free) {
            parts.push('<button type="button" class="cd-btn" data-claim="'
                + esc(order.retailcrm_order_id) + '">Забронировать</button>');
        } else {
            parts.push('<button type="button" class="cd-btn cd-btn--ghost" data-open="'
                + esc(order.retailcrm_order_id) + '">Открыть заказ</button>');
        }
        parts.push('</div>');
        parts.push('</article>');
        return parts.join('');
    }

    function render() {
        renderBusy();

        el.subtitle.textContent = state.city
            ? state.city + ' · ' + state.orders.length + ' заказов'
            : 'Заказы вашего города';

        // «Включил уведомления, а они не приходят» — это почти всегда
        // отсутствие профиля курьера с городом: адресатов нового заказа
        // выбирают по нему. Молчать об этом нельзя: человек считает, что всё
        // настроено, и ждёт звука, которого не будет.
        var warnings = [];
        if (state.profileWarning) warnings.push(state.profileWarning);
        if (state.pushOn && !state.city) {
            warnings.push('Уведомления включены, но вам не назначен город — '
                + 'о новых заказах они приходить не будут. '
                + 'Попросите управляющего завести профиль курьера.');
        }

        if (warnings.length) {
            el.warning.textContent = warnings.join(' ');
            el.warning.hidden = false;
        } else {
            el.warning.hidden = true;
        }

        if (state.stale && state.loadedAt) {
            el.stale.textContent = 'Нет связи с сервером. Показаны данные на '
                + formatClock(state.loadedAt);
            el.stale.hidden = false;
        } else {
            el.stale.hidden = true;
        }

        var c = counts();
        Array.prototype.forEach.call(el.filters.querySelectorAll('.cd-tab'), function (tab) {
            var key = tab.getAttribute('data-filter');
            tab.classList.toggle('cd-tab--active', key === state.filter);
            tab.innerHTML = esc(tab.getAttribute('data-label'))
                + '<span class="cd-tab__count">' + c[key] + '</span>';
        });

        renderSiteBar();

        // Прокрутку возвращаем сами: innerHTML выбрасывает её в начало, а
        // лента перерисовывается каждые 30 секунд.
        var scroll = window.scrollY;
        var list = visibleOrders();
        el.feed.innerHTML = list.length
            ? list.map(cardHtml).join('')
            : '<p class="cd-empty">' + esc(emptyText()) + '</p>';
        window.scrollTo(0, scroll);
    }

    function emptyText() {
        if (state.loading && !state.loadedAt) return 'Загружаем заказы…';
        if (!state.city) return 'Вам не назначен город. Обратитесь к управляющему.';
        // Пустой экран из-за собственного фильтра обязан объяснять себя:
        // иначе это выглядит как «заказов нет» и как сбой приложения.
        if (state.sites.length) {
            return 'В выбранных салонах подходящих заказов нет. '
                + 'Нажмите «Все салоны», чтобы увидеть остальные.';
        }
        if (state.filter === 'free') return 'Свободных заказов сейчас нет.';
        if (state.filter === 'ready') return 'Готовых заказов сейчас нет.';
        if (state.filter === 'mine') return 'Вы пока не взяли ни одного заказа.';
        return 'Заказов на сегодня и завтра нет.';
    }

    function formatClock(date) {
        return String(date.getHours()).padStart(2, '0') + ':'
            + String(date.getMinutes()).padStart(2, '0');
    }

    // === Карточка заказа ====================================================

    /**
     * Номер заказа для показа человеку.
     *
     * `retailcrm_order_id` — внутренний идентификатор CRM, он НЕ равен номеру
     * заказа и человеку ничего не говорит. Пока шапка карточки подставляла
     * его, превью показывало «№ 154368», а карточка того же заказа —
     * «Заказ № 268835»: курьер назвал бы оператору несуществующий номер.
     */
    function displayNumber(order, fallbackId) {
        if (order && order.order_number) return order.order_number;
        var known = state.orders.filter(function (o) {
            return String(o.retailcrm_order_id) === String(fallbackId);
        })[0];
        return (known && known.order_number) || fallbackId;
    }

    function openCard(orderId) {
        state.openOrderId = orderId;
        el.card.hidden = false;
        el.card.innerHTML = sheetShell('<p class="cd-empty">Загружаем карточку…</p>',
                                       displayNumber(null, orderId));
        // Аппаратная «назад» на Android обязана закрывать карточку, а не
        // выкидывать из приложения: в standalone-режиме выход выглядит как сбой.
        history.pushState({ courierLayer: 'card', orderId: orderId }, '');
        refreshOpenCard();
    }

    function closeCard(fromHistory) {
        state.openOrderId = null;
        el.card.hidden = true;
        el.card.innerHTML = '';
        if (!fromHistory) historyBackIfOurs();
    }

    /**
     * Снять свою запись истории, если она наша.
     *
     * Каждый слой (карточка, фото) кладёт свою запись, чтобы аппаратная
     * «назад» закрывала слой, а не выходила из приложения. Закрытие кнопкой в
     * интерфейсе обязано эту запись убрать — иначе «назад» потом срабатывает
     * вхолостую, и курьеру приходится жать её дважды.
     */
    function historyBackIfOurs() {
        if (history.state && history.state.courierLayer) history.back();
    }

    function refreshOpenCard() {
        var orderId = state.openOrderId;
        if (!orderId) return;
        apiGet('/api/courier/orders/' + encodeURIComponent(orderId)).then(function (payload) {
            if (state.openOrderId !== orderId) return;   // успели закрыть
            el.card.innerHTML = sheetShell(cardBodyHtml(payload.data),
                                           displayNumber(payload.data, orderId));
            // Свайп живёт на своих pointer-событиях, делегированием его не
            // поймать: обработчик вешается заново после каждой перерисовки
            bindSlide(el.card);
        }).catch(function (error) {
            if (state.openOrderId !== orderId) return;
            el.card.innerHTML = sheetShell(
                '<p class="cd-empty">Не удалось открыть заказ: ' + esc(error.message) + '</p>',
                displayNumber(null, orderId));
        });
    }

    function sheetShell(body, orderNumber) {
        return '<div class="cd-sheet__head">'
            + '<button type="button" class="cd-sheet__back" data-close="1" aria-label="Назад">'
            + '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75"'
            + ' stroke-linecap="round" stroke-linejoin="round" width="22" height="22">'
            + '<path d="M19 12H5"></path><path d="M12 19l-7-7 7-7"></path></svg></button>'
            + '<span class="cd-sheet__title">Заказ № ' + esc(orderNumber) + '</span>'
            + '</div>'
            + '<div class="cd-sheet__body">' + body + '</div>';
    }

    function block(label, value, note) {
        return '<section class="cd-block">'
            + '<div class="cd-block__label">' + esc(label) + '</div>'
            + '<div class="cd-block__value">' + value + '</div>'
            + (note ? '<div class="cd-block__note">' + esc(note) + '</div>' : '')
            + '</section>';
    }

    /**
     * Дата салона «ДД.ММ.ГГГГ». Чистую дату отдаём в BarhatTime — он её не
     * конвертирует.
     */
    function fmtDate(value) {
        if (!value) return null;
        return window.BarhatTime
            ? window.BarhatTime.formatPlainDate(value, null)
            : String(value);
    }

    /**
     * Отметка «ГГГГ-ММ-ДД ЧЧ:ММ:СС» → «ДД.ММ.ГГГГ ЧЧ:ММ», БЕЗ перевода часовых
     * поясов.
     *
     * Своим кодом, а не `BarhatTime.formatDateTime`: тот считает время
     * пришедшим в UTC и переводит его в пояс устройства. Плановая готовность —
     * стенные часы салона, то самое «14:00», которое ввёл менеджер. Курьер
     * может ехать с телефоном, настроенным на другой город, и перевод сдвинул
     * бы ему готовность на два часа.
     */
    function fmtSalonStamp(value) {
        if (!value) return null;
        var m = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/.exec(String(value).trim());
        if (!m) return String(value);
        return m[3] + '.' + m[2] + '.' + m[1] + ' ' + m[4] + ':' + m[5];
    }

    /** «1 250 ₽» — с разделителем разрядов, как везде в дашборде. */
    function fmtMoney(value) {
        var number = Number(value);
        if (!isFinite(number) || number <= 0) return null;
        return number.toLocaleString('ru-RU', { maximumFractionDigits: 2 }) + ' ₽';
    }

    /**
     * Оба контакта: получатель и заказчик, каждый своей строкой.
     *
     * Раньше карточка показывала кого-то одного, выбирая за курьера. Это
     * ошибка: получатель заполнен у 36% заказов, и в остальных случаях звонить
     * надо заказчику — но бывает нужен и тот, и другой (не открыли дверь,
     * уточнить адрес). Кто есть кто — подписью, иначе курьер поздравит
     * заказчика с сюрпризом, который тот сам и оплатил.
     */
    function contactsHtml(order) {
        var rows = [];

        if (order.recipient_name || order.recipient_phone) {
            rows.push(contactRow(
                'Получатель', order.recipient_name, order.recipient_phone,
                order.do_not_contact_recipient
                    ? 'Не звонить — сюрприз-доставка'
                    : null));
        }
        if (order.customer_name || order.customer_phone) {
            rows.push(contactRow(
                'Заказчик', order.customer_name, order.customer_phone,
                order.do_not_contact_recipient
                    ? 'Все вопросы по доставке — сюда'
                    : null));
        }
        return rows.length ? rows.join('') : null;
    }

    function contactRow(role, name, phone, note) {
        var call = phone && !(role === 'Получатель' && note)
            ? '<div style="margin-top:10px"><a class="cd-btn" href="' + esc(telHref(phone))
                + '">Позвонить</a></div>'
            : '';
        return '<div class="cd-block__row">'
            + '<div class="cd-person__who">' + esc(role) + '</div>'
            + '<div class="cd-block__value--big">' + esc(name || 'без имени') + '</div>'
            + (phone ? '<div class="cd-block__value">' + esc(phone) + '</div>' : '')
            + (note ? '<div class="cd-block__note">' + esc(note) + '</div>' : '')
            + call
            + '</div>';
    }

    function itemsHtml(items) {
        if (!items || !items.length) return '<div class="cd-block__value">Состав не указан</div>';
        return items.map(function (item) {
            var qty = item.quantity;
            var photo = item.image_url
                ? '<button type="button" class="cd-item__photo" data-photo="' + esc(item.image_url)
                    + '" aria-label="Показать фото">'
                    + '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75"'
                    + ' stroke-linecap="round" stroke-linejoin="round" width="20" height="20">'
                    + '<rect x="3" y="3" width="18" height="18" rx="2"></rect>'
                    + '<circle cx="8.5" cy="8.5" r="1.5"></circle>'
                    + '<path d="M21 15l-5-5L5 21"></path></svg></button>'
                : '';
            return '<div class="cd-item">'
                + '<span class="cd-item__name">' + esc(item.product_name || 'без названия') + '</span>'
                + '<span class="cd-item__qty">' + esc(qty) + '</span>'
                + photo + '</div>';
        }).join('');
    }

    function cardBodyHtml(order) {
        var parts = [];

        // Флаг «не связываться с получателем» — ПЕРВЫМ и крупно. Это
        // сюрприз-доставка (5% заказов): звонок ломает подарок, и увидеть это
        // надо раньше, чем телефон.
        if (order.do_not_contact_recipient) {
            parts.push('<div class="cd-alert">'
                + '<span class="cd-alert__icon">'
                + '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75"'
                + ' stroke-linecap="round" stroke-linejoin="round" width="26" height="26">'
                + '<path d="M10.68 13.31a16 16 0 0 0 3.41 2.6l1.27-1.27a2 2 0 0 1 2.11-.45'
                + ' 12.84 12.84 0 0 0 2.29.62A2 2 0 0 1 21.72 16v3a2 2 0 0 1-2.18 2'
                + ' 19.79 19.79 0 0 1-8.63-3.07 19.42 19.42 0 0 1-3.33-2.67m-2.67-3.34'
                + 'A19.79 19.79 0 0 1 3.08 4.18 2 2 0 0 1 5.06 2h3a2 2 0 0 1 2 1.72'
                + ' 12.84 12.84 0 0 0 .7 2.81 2 2 0 0 1-.45 2.11L9.09 9.91"></path>'
                + '<path d="M2 2l20 20"></path></svg></span>'
                + '<div><div class="cd-alert__title">Не связываться с получателем</div>'
                + '<div class="cd-alert__text">Сюрприз-доставка. Не звоните и не пишите'
                + ' получателю — все вопросы через заказчика.</div></div></div>');
        }

        var tick = countdown(order);
        var date = fmtDate(order.delivery_date);
        parts.push(block('Доставка',
            '<div class="cd-block__value--big">' + esc(slotText(order)) + '</div>'
            + (tick ? '<div class="cd-block__note">' + esc(tick.text) + '</div>' : ''),
            date ? 'Дата: ' + date : null));

        parts.push(block('Адрес',
            '<div class="cd-block__value">' + esc(order.address_text || 'адрес не указан') + '</div>'
            + (order.address_text
                ? '<div class="cd-block__actions" style="margin-top:12px">'
                    + '<button type="button" class="cd-btn cd-btn--ghost" data-route="'
                    + esc([order.city, order.address_text].filter(Boolean).join(', '))
                    + '">Маршрут</button></div>'
                : '')));

        var ready = fmtSalonStamp(order.ready_planned_at);
        parts.push(block('Забрать в салоне',
            '<div class="cd-block__value">' + esc(order.site_name || order.city || 'салон не указан') + '</div>',
            ready ? 'Плановая готовность: ' + ready : null));

        var contacts = contactsHtml(order);
        if (contacts) parts.push(block('Контакты', contacts));

        parts.push(block('Состав', itemsHtml(order.items)));

        var pay = fmtMoney(order.net_cost);
        if (pay) {
            parts.push(block('Себестоимость доставки',
                '<div class="cd-block__value--big">' + esc(pay) + '</div>'));
        }

        // Комментарии оператора и клиента — разные по смыслу, поэтому разными
        // блоками, а не одной кучей.
        if (order.manager_comment) parts.push(block('Комментарий оператора',
            '<div class="cd-block__value">' + esc(order.manager_comment) + '</div>'));
        if (order.customer_comment) parts.push(block('Комментарий клиента',
            '<div class="cd-block__value">' + esc(order.customer_comment) + '</div>'));
        if (order.note_text) parts.push(block('Примечание',
            '<div class="cd-block__value">' + esc(order.note_text) + '</div>'));

        var id = esc(order.retailcrm_order_id);
        if (order.is_mine && order.assignment_state === 'picked_up') {
            // Доставка — необратимое действие, и оно не должно висеть на одном
            // тапе: телефон в кармане нажимает сам. Отсюда свайп (§3 плана).
            parts.push('<div class="cd-slide" data-slide-order="' + id + '">'
                + '<span class="cd-slide__hint">Проведите вправо — доставлено</span>'
                + '<span class="cd-slide__knob">'
                + '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"'
                + ' stroke-linecap="round" stroke-linejoin="round" width="22" height="22">'
                + '<path d="M5 12h14"></path><path d="M12 5l7 7-7 7"></path></svg></span></div>');
            parts.push('<button type="button" class="cd-btn cd-btn--ghost" data-problem-open="'
                + id + '">Проблема</button>');
        } else if (order.is_mine) {
            parts.push('<button type="button" class="cd-btn cd-btn--accent" data-pickup="'
                + id + '">Забрал заказ</button>');
            parts.push('<div class="cd-btn-row">'
                + '<button type="button" class="cd-btn cd-btn--ghost" data-release="'
                + id + '">Отказаться</button>'
                + '<button type="button" class="cd-btn cd-btn--ghost" data-problem-open="'
                + id + '">Проблема</button></div>');
        } else if (order.is_free) {
            parts.push('<button type="button" class="cd-btn cd-btn--accent" data-claim="'
                + id + '">Забронировать</button>');
        }

        return parts.join('');
    }

    // === Фото ===============================================================

    function openPhoto(url) {
        el.photo.hidden = false;
        history.pushState({ courierLayer: 'photo' }, '');
        el.photo.innerHTML = '<button type="button" class="cd-photo__close" data-photo-close="1"'
            + ' aria-label="Закрыть">'
            + '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75"'
            + ' stroke-linecap="round" stroke-linejoin="round" width="22" height="22">'
            + '<path d="M18 6L6 18"></path><path d="M6 6l12 12"></path></svg></button>'
            + '<span class="cd-photo__status">Загружаем фото…</span>';

        var img = new Image();
        img.className = 'cd-photo__img';
        img.alt = 'Фото товара';
        img.onload = function () {
            var status = el.photo.querySelector('.cd-photo__status');
            if (status) status.replaceWith(img);
        };
        img.onerror = function () {
            var status = el.photo.querySelector('.cd-photo__status');
            // Плейсхолдер вместо пустого экрана: ссылка ведёт на сайт, и он
            // может не ответить — курьер должен понимать, что это не он сломал.
            if (status) status.textContent = 'Фото не загрузилось';
        };
        img.src = url;
    }

    function closePhoto(fromHistory) {
        el.photo.hidden = true;
        el.photo.innerHTML = '';
        if (!fromHistory) historyBackIfOurs();
    }

    // === Обработчики ========================================================

    function apiPost(url, body) {
        return fetch(url, {
            method: 'POST',
            credentials: 'same-origin',
            body: body ? JSON.stringify(body) : undefined,
            // Ручки записи требуют именно это значение (см. AJAX_HEADER_VALUE
            // в auth.py): браузер не даёт поставить кастомный заголовок в
            // межсайтовом запросе, и это вся защита от CSRF — токенов в
            // проекте нет. Привычное 'XMLHttpRequest' здесь не подойдёт.
            headers: {
                'X-Requested-With': 'barhat-dashboard',
                'Content-Type': 'application/json'
            }
        }).then(function (response) {
            return response.json().catch(function () { return {}; })
                .then(function (payload) {
                    if (!response.ok || payload.success !== true) {
                        var error = new Error(payload.error || ('HTTP ' + response.status));
                        error.code = payload.code;
                        throw error;
                    }
                    return payload;
                });
        });
    }

    /**
     * Бронь заказа.
     *
     * Кнопка блокируется на время запроса — это третий уровень защиты от
     * двойной брони (первые два: `BEGIN IMMEDIATE` и уникальный индекс).
     * Без него один медленный клик превращается в три запроса: кнопка
     * выглядит живой, и её дожимают.
     */
    function claimOrder(orderId, button) {
        if (button.disabled) return;
        button.disabled = true;
        button.textContent = 'Бронируем…';

        apiPost('/api/courier/orders/' + encodeURIComponent(orderId) + '/claim')
            .then(function () {
                toast('Заказ ваш', 'success');
                return loadFeed();
            })
            .catch(function (error) {
                // «Занят» — это не сбой, а новость: список устарел, и его
                // надо перечитать, чтобы курьер увидел, кто успел
                toast(error.message, error.code === 'taken' ? 'info' : 'error');
                if (error.code === 'taken' || error.code === 'gone') loadFeed();
                else {
                    button.disabled = false;
                    button.textContent = 'Забронировать';
                }
            });
    }

    var PROBLEM_REASONS = [
        { action: 'no_answer', title: 'Не дозвонился' },
        { action: 'reschedule', title: 'Просят привезти позже' },
        { action: 'refused', title: 'Отказ от заказа' },
        { action: 'wrong_address', title: 'Адрес не тот' }
    ];

    /**
     * Отметка курьера, уходящая в CRM.
     *
     * Курьер не ждёт CRM: сервер меняет состояние сразу и кладёт отправку в
     * очередь. Поэтому «сохранено» здесь честно, даже когда CRM лежит.
     */
    function sendAction(orderId, action, extra, button) {
        if (button && button.disabled) return Promise.resolve();
        if (button) { button.disabled = true; button.textContent = 'Сохраняем…'; }

        var body = { action: action };
        if (extra) Object.keys(extra).forEach(function (k) { body[k] = extra[k]; });

        return apiPost('/api/courier/orders/' + encodeURIComponent(orderId) + '/action', body)
            .then(function (payload) {
                if (payload.data && payload.data.warning) {
                    toast(payload.data.warning, 'error');
                } else {
                    toast('Отметка сохранена', 'success');
                }
                closeCard();
                return loadFeed();
            })
            .catch(function (error) {
                if (error.code === 'not_ready') {
                    // Не запрет, а предупреждение: статус «Заказ готов» ставят
                    // в момент начала окна доставки, а у трети заказов позже.
                    // Запретить забирать — значит заставить курьера стоять в
                    // салоне и ждать, пока флорист щёлкнет статус.
                    return confirmNotReady(orderId, button);
                }
                toast(error.message, 'error');
                if (button) { button.disabled = false; }
                loadFeed();
            });
    }

    function confirmNotReady(orderId, button) {
        return window.BarhatUI.confirm(
            'Заказ ещё не отмечен готовым. Всё равно забираете?',
            { title: 'Заказ не готов', confirmText: 'Забираю', cancelText: 'Отмена' }
        ).then(function (ok) {
            if (!ok) {
                if (button) { button.disabled = false; button.textContent = 'Забрал заказ'; }
                return;
            }
            return sendAction(orderId, 'pickup', { force_not_ready: true }, button);
        });
    }

    function askProblem(orderId) {
        var buttons = PROBLEM_REASONS.map(function (reason) {
            return '<button type="button" class="cd-btn cd-btn--ghost" data-problem="'
                + esc(reason.action) + '" data-problem-order="' + esc(orderId) + '">'
                + esc(reason.title) + '</button>';
        }).join('');
        // Предустановленные причины, а не ввод текста: набирать за рулём никто
        // не будет, и данные разъедутся с жизнью
        el.card.querySelector('.cd-sheet__body').insertAdjacentHTML('afterbegin',
            '<section class="cd-block"><div class="cd-block__label">Что случилось</div>'
            + '<div style="display:flex;flex-direction:column;gap:8px;margin-top:10px">'
            + buttons + '</div></section>');
    }

    function releaseOrder(orderId, button) {
        if (button.disabled) return;
        window.BarhatUI.confirm('Вернуть заказ в общий список?', {
            title: 'Отказаться от заказа',
            confirmText: 'Отказаться',
            cancelText: 'Оставить'
        }).then(function (ok) {
            if (!ok) return;
            button.disabled = true;
            button.textContent = 'Отпускаем…';
            apiPost('/api/courier/orders/' + encodeURIComponent(orderId) + '/release')
                .then(function () {
                    toast('Заказ вернулся в общий список', 'info');
                    return loadFeed();
                })
                .catch(function (error) {
                    toast(error.message, 'error');
                    loadFeed();
                });
        });
    }

    function onCardClick(event) {
        var close = event.target.closest('[data-close]');
        if (close) { closeCard(); return; }

        var photo = event.target.closest('[data-photo]');
        if (photo) { openPhoto(photo.getAttribute('data-photo')); return; }

        var route = event.target.closest('[data-route]');
        if (route) { openRoute(route.getAttribute('data-route')); return; }

        var claim = event.target.closest('[data-claim]');
        if (claim) { claimOrder(claim.getAttribute('data-claim'), claim); return; }

        var release = event.target.closest('[data-release]');
        if (release) { releaseOrder(release.getAttribute('data-release'), release); return; }

        var pickup = event.target.closest('[data-pickup]');
        if (pickup) {
            sendAction(pickup.getAttribute('data-pickup'), 'pickup', null, pickup);
            return;
        }

        var problemOpen = event.target.closest('[data-problem-open]');
        if (problemOpen) { askProblem(problemOpen.getAttribute('data-problem-open')); return; }

        var problem = event.target.closest('[data-problem]');
        if (problem) {
            sendAction(problem.getAttribute('data-problem-order'),
                       problem.getAttribute('data-problem'), null, problem);
        }
    }

    /**
     * Свайп «Доставлено».
     *
     * Pointer events, а не touch: одним обработчиком закрываются и палец, и
     * мышь (карточку открывают и с компьютера). Порог — 65% ширины: меньше
     * срабатывает от случайного смаза, больше не дотягивается одной рукой.
     */
    function bindSlide(root) {
        var slide = root.querySelector('[data-slide-order]');
        if (!slide) return;
        var knob = slide.querySelector('.cd-slide__knob');
        var startX = null;
        var maxShift = 0;

        function move(event) {
            if (startX === null) return;
            var shift = Math.max(0, Math.min(event.clientX - startX, maxShift));
            knob.style.transform = 'translateX(' + shift + 'px)';
            slide.classList.toggle('cd-slide--armed', shift >= maxShift * 0.65);
        }

        function end(event) {
            if (startX === null) return;
            var shift = Math.max(0, Math.min(event.clientX - startX, maxShift));
            startX = null;
            knob.style.transform = '';
            slide.classList.remove('cd-slide--armed');
            window.removeEventListener('pointermove', move);
            window.removeEventListener('pointerup', end);
            window.removeEventListener('pointercancel', end);
            if (shift >= maxShift * 0.65) {
                slide.classList.add('cd-slide--busy');
                sendAction(slide.getAttribute('data-slide-order'), 'deliver', null, null);
            }
        }

        knob.addEventListener('pointerdown', function (event) {
            startX = event.clientX;
            maxShift = slide.clientWidth - knob.offsetWidth - 8;
            window.addEventListener('pointermove', move);
            window.addEventListener('pointerup', end);
            window.addEventListener('pointercancel', end);
        });
    }

    /**
     * Маршрут до адреса.
     *
     * Обычная https-ссылка, а не схема `yandexnavi://`: своей схемой браузер
     * ничего не делает, когда приложения нет, и кнопка выглядит сломанной.
     * По https-ссылке Android сам предложит открыть её в Яндекс.Картах или
     * Навигаторе, а без них она откроется в браузере.
     *
     * Точки маршрута появятся в Фазе 7 — там будут координаты из геокодера.
     */
    function openRoute(address) {
        if (!address) return;
        window.open('https://yandex.ru/maps/?rtext=~' + encodeURIComponent(address) + '&rtt=auto',
            '_blank', 'noopener');
    }

    function bind() {
        el.filters.addEventListener('click', function (event) {
            var tab = event.target.closest('.cd-tab');
            if (!tab) return;
            state.filter = tab.getAttribute('data-filter');
            render();
            window.scrollTo(0, 0);
        });

        el.sitesOpen.addEventListener('click', openSites);

        el.sites.addEventListener('click', function (event) {
            if (event.target.closest('[data-sites-close]')) { closeSites(); return; }
            if (event.target.closest('[data-site-reset]')) {
                state.sites = [];
                saveSites();
                render();
                renderSitesSheet();
                return;
            }
            var row = event.target.closest('[data-site]');
            if (row) toggleSite(row.getAttribute('data-site'));
        });

        el.refresh.addEventListener('click', function () {
            if (state.loading) return;
            loadFeed();
        });

        el.feed.addEventListener('click', function (event) {
            var claim = event.target.closest('[data-claim]');
            if (claim) { claimOrder(claim.getAttribute('data-claim'), claim); return; }

            var release = event.target.closest('[data-release]');
            if (release) { releaseOrder(release.getAttribute('data-release'), release); return; }

            var open = event.target.closest('[data-open]');
            if (open) { openCard(open.getAttribute('data-open')); return; }

            var card = event.target.closest('[data-order]');
            if (card) openCard(card.getAttribute('data-order'));
        });

        // Делегирование на самом слое, а не на его содержимом: карточка
        // перерисовывается каждым обновлением, и обработчик на внутренностях
        // копился бы по одному на перерисовку.
        el.card.addEventListener('click', onCardClick);

        el.photo.addEventListener('click', function (event) {
            if (event.target.closest('[data-photo-close]') || event.target === el.photo) closePhoto();
        });

        // Слои закрываются в обратном порядке открытия — верхний первым.
        window.addEventListener('popstate', function () {
            if (!el.photo.hidden) { closePhoto(true); return; }
            if (!el.sites.hidden) { closeSites(true); return; }
            if (state.openOrderId) closeCard(true);
        });

        // Обновление по возврату во вкладку вместо бесконечного поллинга в
        // фоне: телефон курьера не обязан греться, пока экран выключен.
        document.addEventListener('visibilitychange', function () {
            if (!document.hidden) loadFeed();
        });

        setInterval(function () {
            if (!document.hidden) loadFeed();
        }, REFRESH_MS);
    }

    // === Push-уведомления ===================================================

    /** base64url из манифеста → Uint8Array, как того требует PushManager. */
    function urlBase64ToUint8Array(base64) {
        var padding = '='.repeat((4 - base64.length % 4) % 4);
        var normalized = (base64 + padding).replace(/-/g, '+').replace(/_/g, '/');
        var raw = window.atob(normalized);
        var output = new Uint8Array(raw.length);
        for (var i = 0; i < raw.length; i++) output[i] = raw.charCodeAt(i);
        return output;
    }

    function pushSupported() {
        return 'serviceWorker' in navigator && 'PushManager' in window
            && 'Notification' in window;
    }

    /**
     * Показать кнопку включения уведомлений — или объяснить, почему нельзя.
     *
     * Молча не подписываемся: разрешение спрашивается по нажатию, иначе
     * Chrome его блокирует, а курьер не понимает, почему звука нет.
     */
    function setupPush() {
        if (!pushSupported()) return;

        apiGet('/api/courier/push/key').then(function (payload) {
            if (!payload.data.configured) return;   // ключи не заведены
            state.pushKey = payload.data.public_key;
            return navigator.serviceWorker.ready.then(function (registration) {
                return registration.pushManager.getSubscription();
            }).then(function (existing) {
                state.pushOn = !!existing;
                renderPushButton();
                render();
                if (existing) {
                    // Подписка могла быть выдана до перезапуска сервера —
                    // пересохраняем, чтобы она точно лежала в базе
                    return sendSubscription(existing);
                }
            });
        }).catch(function () { /* пуши — усиление, а не условие работы */ });
    }

    /**
     * Кнопка уведомлений: включить или выключить.
     *
     * Одна кнопка с двумя состояниями, а не расписание тихих часов. Тихие
     * часы здесь были и убраны: молчание по часам неотличимо от поломки, а
     * курьер и так знает лучше, когда его можно беспокоить.
     */
    function renderPushButton() {
        var button = document.getElementById('cdPushBtn');
        if (!button) {
            button = document.createElement('button');
            button.type = 'button';
            button.id = 'cdPushBtn';
            button.className = 'cd-btn cd-btn--ghost';
            button.style.margin = '12px 16px 0';
            button.style.width = 'calc(100% - 32px)';
            button.addEventListener('click', function () {
                if (state.pushOn) unsubscribePush();
                else subscribePush();
            });
            el.feed.parentNode.insertBefore(button, el.feed);
        }
        button.disabled = false;
        button.textContent = state.pushOn
            ? 'Выключить уведомления'
            : 'Включить уведомления о новых заказах';
    }

    function unsubscribePush() {
        var button = document.getElementById('cdPushBtn');
        if (button) { button.disabled = true; button.textContent = 'Выключаем…'; }

        navigator.serviceWorker.ready.then(function (registration) {
            return registration.pushManager.getSubscription();
        }).then(function (subscription) {
            if (!subscription) return null;
            var endpoint = subscription.endpoint;
            // Сначала снимаем подписку в браузере, потом убираем её у себя:
            // если оставить запись в базе, мы будем слать в мёртвый endpoint
            return subscription.unsubscribe().then(function () {
                return apiPost('/api/courier/push/unsubscribe', { endpoint: endpoint });
            });
        }).then(function () {
            state.pushOn = false;
            toast('Уведомления выключены', 'info');
            renderPushButton();
            render();
        }).catch(function (error) {
            toast('Не удалось выключить: ' + error.message, 'error');
            renderPushButton();
        });
    }

    function subscribePush() {
        var button = document.getElementById('cdPushBtn');
        if (button) { button.disabled = true; button.textContent = 'Подключаем…'; }

        Notification.requestPermission().then(function (permission) {
            if (permission !== 'granted') {
                toast('Уведомления запрещены в настройках браузера', 'error');
                renderPushButton();
                return;
            }
            return navigator.serviceWorker.ready.then(function (registration) {
                return registration.pushManager.subscribe({
                    userVisibleOnly: true,
                    applicationServerKey: urlBase64ToUint8Array(state.pushKey)
                });
            }).then(sendSubscription).then(function () {
                toast('Уведомления включены', 'success');
                state.pushOn = true;
                renderPushButton();
                render();
            });
        }).catch(function (error) {
            toast('Не удалось включить уведомления: ' + error.message, 'error');
            renderPushButton();
        });
    }

    function sendSubscription(subscription) {
        return apiPost('/api/courier/push/subscribe', subscription.toJSON());
    }

    // === Service worker =====================================================

    function registerServiceWorker() {
        if (!('serviceWorker' in navigator)) return;
        navigator.serviceWorker.register('/app/courier-sw.js', { scope: '/app/' })
            .catch(function (error) {
                console.warn('Service worker не зарегистрировался:', error);
            });

        // Новая версия оболочки — перезагружаем страницу один раз. Без этого
        // курьер после деплоя работал бы во вчерашнем экране (находка К5).
        var reloading = false;
        navigator.serviceWorker.addEventListener('controllerchange', function () {
            if (reloading) return;
            reloading = true;
            window.location.reload();
        });
    }

    // === Старт ==============================================================

    function start() {
        el.boot = document.getElementById('cdBoot');
        el.app = document.getElementById('cdApp');
        el.feed = document.getElementById('cdFeed');
        el.filters = document.getElementById('cdFilters');
        el.siteBar = document.getElementById('cdSiteBar');
        el.sitesOpen = document.getElementById('cdSitesOpen');
        el.sitesLabel = document.getElementById('cdSitesLabel');
        el.sites = document.getElementById('cdSites');
        el.subtitle = document.getElementById('cdSubtitle');
        el.warning = document.getElementById('cdWarning');
        el.stale = document.getElementById('cdStale');
        el.refresh = document.getElementById('cdRefresh');
        el.card = document.getElementById('cdCard');
        el.photo = document.getElementById('cdPhoto');

        // Подписи табов запоминаем до первой перерисовки: она переписывает их
        // вместе со счётчиком.
        Array.prototype.forEach.call(el.filters.querySelectorAll('.cd-tab'), function (tab) {
            tab.setAttribute('data-label', tab.textContent.trim());
        });

        state.sites = loadSites();

        fetch('/api/auth/me', { credentials: 'same-origin' }).then(function (response) {
            if (!response.ok) throw new Error('unauthorized');
            return response.json();
        }).then(function () {
            el.boot.hidden = true;
            el.app.hidden = false;
            bind();
            registerServiceWorker();
            setupPush();
            return loadProfile().then(loadFeed);
        }).catch(function () {
            window.location.href = '/login';
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', start);
    } else {
        start();
    }
})();
