/*
 * Раздел «Контроль доставки» (Фаза 8).
 *
 * Четыре вкладки, и каждая отвечает на свой вопрос:
 *
 *   Доставка сегодня — где сейчас каждый заказ и какие никто не взял;
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
        { id: 'today', title: 'Доставка сегодня' },
        { id: 'couriers', title: 'Курьеры' },
        { id: 'statuses', title: 'Статусы CRM' },
        { id: 'outbox', title: 'Журнал отправок' }
    ];

    var STATE_TITLES = {
        free: 'Свободен',
        claimed: 'Забронирован',
        picked_up: 'В пути',
        delivered: 'Доставлен'
    };

    var state = {
        tab: 'today',
        isAdmin: false,
        overview: null,
        metrics: null,
        profiles: null,
        actions: [],
        statuses: [],
        outbox: [],
        users: [],
        loading: false
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

    function get(url) {
        return fetch(url, { credentials: 'same-origin' })
            .then(function (r) { return r.json(); })
            .then(function (payload) {
                if (!payload || payload.success !== true) {
                    throw new Error((payload && payload.error) || 'Сервер вернул ошибку');
                }
                return payload.data;
            });
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

    function loadTab() {
        var host = document.getElementById('cdispRoot');
        if (!host) return Promise.resolve();
        state.loading = true;
        render();

        var job;
        if (state.tab === 'today') {
            job = Promise.all([get('/api/courier/overview'), get('/api/courier/metrics')])
                .then(function (r) { state.overview = r[0]; state.metrics = r[1]; });
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
        } else {
            job = get('/api/courier/outbox?limit=100').then(function (data) {
                state.outbox = data || [];
            });
        }

        return job.catch(function (error) {
            toast('Не удалось загрузить: ' + error.message, 'error');
        }).then(function () {
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
                + orderTable(unclaimed) + '</div>';
        }

        return '<div class="cdisp-tiles">' + tiles + '</div>'
            + alarm
            + '<h3>Заказы</h3>'
            + orderTable(state.overview.orders || [])
            + metricsHtml();
    }

    function orderTable(orders) {
        if (!orders.length) return '<p class="section-description">Заказов нет</p>';
        var rows = orders.map(function (order) {
            var slot = (order.delivery_time_from || '') +
                (order.delivery_time_to ? '–' + order.delivery_time_to : '');
            var who = order.courier_name || '';
            var overdue = order.state === 'claimed' && order.expires_at
                && order.expires_at <= new Date().toISOString().slice(0, 19).replace('T', ' ');
            return '<tr>'
                + '<td>' + esc(order.order_number || order.retailcrm_order_id) + '</td>'
                + '<td>' + esc(order.site_name || order.city || '') + '</td>'
                + '<td>' + esc(slot || 'время уточняется') + '</td>'
                + '<td>' + esc(STATE_TITLES[order.state] || order.state)
                + (overdue ? ' <span class="cdisp-bad">просрочена</span>' : '') + '</td>'
                + '<td>' + esc(order.is_ready ? 'Готов' : 'Собирают') + '</td>'
                + '<td>' + esc(who) + '</td>'
                + '<td>' + (order.state === 'claimed' || order.state === 'picked_up'
                    ? '<button class="btn btn-secondary" data-release-order="'
                        + esc(order.retailcrm_order_id) + '">Снять бронь</button>'
                    : '') + '</td>'
                + '</tr>';
        }).join('');

        return '<table class="cdisp-table"><thead><tr>'
            + '<th>Заказ</th><th>Салон</th><th>Окно</th><th>Состояние</th>'
            + '<th>Сборка</th><th>Курьер</th><th></th>'
            + '</tr></thead><tbody>' + rows + '</tbody></table>';
    }

    function metricsHtml() {
        var m = state.metrics;
        if (!m) return '';
        var rows = [
            ['Броней за период', num(m.claims_total)],
            ['Доля просроченных броней', num(m.expired_share, ' %')],
            ['От брони до забора, медиана', num(m.minutes_to_pickup_median, ' мин')],
            ['Доставок вовремя', num(m.on_time_share, ' %')],
            ['Ушло аутсорсу после снятия брони', num(m.outsourced_after_release)],
            ['На сумму', num(m.outsourced_amount, ' ₽')]
        ].map(function (pair) {
            return '<tr><td>' + esc(pair[0]) + '</td><td>' + pair[1] + '</td></tr>';
        }).join('');

        return '<h3 style="margin-top:28px">Показатели за 30 дней</h3>'
            + '<table class="cdisp-table"><tbody>' + rows + '</tbody></table>'
            + '<p class="section-description">Не считается: '
            + esc((m.not_measured || []).join(', '))
            + ' — момент появления заказа в ленте нигде не записан, '
            + 'и показывать вместо него время синхронизации значило бы выдумать цифру.</p>';
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
            + (state.isAdmin ? addProfileHtml() : '');
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
        var blocked = state.actions.filter(function (a) { return !a.status_code; }).length;
        var rows = state.actions.map(function (item) {
            return '<tr>'
                + '<td>' + esc(item.title) + '</td>'
                + '<td>' + (state.isAdmin
                    ? '<select class="form-input" data-action-status="' + esc(item.action)
                        + '" style="min-width:280px">' + statusOptions(item.status_code) + '</select>'
                    : esc(item.status_code || '— не задан —')) + '</td>'
                + '<td>' + (item.status_code
                    ? '<span class="cdisp-ok">настроено</span>'
                    : '<span class="cdisp-bad">действие заблокировано</span>') + '</td>'
                + '<td>' + esc(item.updated_by || '') + '</td>'
                + '</tr>';
        }).join('');

        return (blocked
            ? '<p class="section-description" style="color:#c0322f">Не настроено действий: '
                + blocked + '. Пока статус не выбран, курьер не сможет отметить это действие.</p>'
            : '')
            + '<p class="section-description">Статус выбирается из справочника CRM. '
            + 'Выводить его из названия нельзя: названия меняют, и отправка сломается молча.</p>'
            + '<table class="cdisp-table"><thead><tr>'
            + '<th>Действие курьера</th><th>Статус в CRM</th><th>Состояние</th><th>Кто менял</th>'
            + '</tr></thead><tbody>' + rows + '</tbody></table>';
    }

    function outboxHtml() {
        if (!state.outbox.length) return '<p class="section-description">Отправок пока не было</p>';
        var rows = state.outbox.map(function (item) {
            var color = item.state === 'sent' ? '#0a7d3f'
                : (item.state === 'failed' ? '#c0322f' : '#6F6F6F');
            var label = item.state === 'sent' ? 'отправлено'
                : (item.state === 'failed' ? 'не ушло' : 'в очереди');
            return '<tr>'
                + '<td>' + esc(item.order_number || item.retailcrm_order_id) + '</td>'
                + '<td>' + esc(item.action_title) + '</td>'
                + '<td>' + esc(item.target_status) + '</td>'
                + '<td style="color:' + color + '">' + esc(label) + '</td>'
                + '<td>' + esc(item.attempts) + '</td>'
                + '<td>' + esc(item.error_message || '') + '</td>'
                + '<td>' + esc(item.sent_at || item.created_at) + '</td>'
                + '</tr>';
        }).join('');

        return '<p class="section-description">Что мы отправили и что ответила CRM. '
            + 'Отметка курьера сохраняется сразу, наружу уходит фоном.</p>'
            + '<table class="cdisp-table"><thead><tr>'
            + '<th>Заказ</th><th>Действие</th><th>Статус CRM</th><th>Итог</th>'
            + '<th>Попыток</th><th>Ответ</th><th>Когда</th>'
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
        if (state.loading) body = '<p class="section-description">Загружаем…</p>';
        else if (state.tab === 'today') body = todayHtml();
        else if (state.tab === 'couriers') body = couriersHtml();
        else if (state.tab === 'statuses') body = statusesHtml();
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
