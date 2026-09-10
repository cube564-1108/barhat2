/*
 * Раздел «Контроль доставки»: справочник статусов и журнал отправок в CRM.
 *
 * Зачем это в интерфейсе, а не в конфиге. Действие курьера → код статуса CRM
 * заполняет ЧЕЛОВЕК: статусы в CRM переименовывают и заводят новые, и вывод
 * кода из названия ломается молча — ровно тот класс ошибки, из-за которого
 * счета уходили в банк без НДС. Пустой маппинг блокирует действие с внятным
 * текстом, и увидеть это надо здесь, а не по жалобе курьера.
 *
 * Журнал отправок — вторая половина того же правила: «что мы им отправили и
 * что они ответили» спрашивают всегда, и отвечать чтением кода — потерянный
 * час.
 *
 * Живые брони и метрики — Фаза 8, здесь их пока нет.
 */

(function () {
    'use strict';

    var AJAX = {
        'X-Requested-With': 'barhat-dashboard',
        'Content-Type': 'application/json'
    };

    var state = { isAdmin: false, actions: [], statuses: [], outbox: [], loaded: false };

    function esc(value) {
        if (value === null || value === undefined) return '';
        return String(value)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    function toast(message, kind) {
        if (window.BarhatUI) window.BarhatUI.toast(message, kind || 'info');
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

    function load() {
        return Promise.all([
            get('/api/courier/action-statuses'),
            get('/api/courier/outbox?limit=50')
        ]).then(function (results) {
            state.actions = results[0].actions || [];
            state.statuses = results[0].statuses || [];
            state.outbox = results[1] || [];
            state.loaded = true;
            render();
        }).catch(function (error) {
            var host = document.getElementById('cdispRoot');
            if (host) {
                host.innerHTML = '<p class="section-description">Не удалось загрузить: '
                    + esc(error.message) + '</p>';
            }
        });
    }

    function statusOptions(selected) {
        var options = ['<option value="">— не задан —</option>'];
        state.statuses.forEach(function (status) {
            options.push('<option value="' + esc(status.code) + '"'
                + (status.code === selected ? ' selected' : '') + '>'
                + esc(status.name) + ' (' + esc(status.code) + ')</option>');
        });
        return options.join('');
    }

    function actionsTable() {
        var rows = state.actions.map(function (item) {
            var missing = !item.status_code;
            return '<tr>'
                + '<td>' + esc(item.title) + '</td>'
                + '<td>'
                + (state.isAdmin
                    ? '<select class="form-input" data-action-status="' + esc(item.action) + '"'
                        + ' style="min-width:280px">' + statusOptions(item.status_code) + '</select>'
                    : esc(item.status_code || '— не задан —'))
                + '</td>'
                + '<td>' + (missing
                    ? '<span style="color:#c0322f">действие заблокировано</span>'
                    : '<span style="color:#0a7d3f">настроено</span>') + '</td>'
                + '<td>' + esc(item.updated_by || '') + '</td>'
                + '</tr>';
        }).join('');

        return '<table class="data-table"><thead><tr>'
            + '<th>Действие курьера</th><th>Статус в CRM</th>'
            + '<th>Состояние</th><th>Кто менял</th>'
            + '</tr></thead><tbody>' + rows + '</tbody></table>';
    }

    function outboxTable() {
        if (!state.outbox.length) {
            return '<p class="section-description">Отправок пока не было</p>';
        }
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

        return '<table class="data-table"><thead><tr>'
            + '<th>Заказ</th><th>Действие</th><th>Статус CRM</th><th>Итог</th>'
            + '<th>Попыток</th><th>Ответ</th><th>Когда</th>'
            + '</tr></thead><tbody>' + rows + '</tbody></table>';
    }

    function render() {
        var host = document.getElementById('cdispRoot');
        if (!host) return;

        var blocked = state.actions.filter(function (a) { return !a.status_code; }).length;

        host.innerHTML =
            (blocked
                ? '<p class="section-description" style="color:#c0322f">'
                    + 'Не настроено действий: ' + blocked
                    + '. Пока статус не выбран, курьер не сможет отметить это действие.</p>'
                : '')
            + '<h3 style="margin-top:8px">Действие курьера → статус в CRM</h3>'
            + '<p class="section-description">Статус выбирается из справочника CRM. '
            + 'Выводить его из названия нельзя: названия меняют, и отправка сломается молча.</p>'
            + actionsTable()
            + '<h3 style="margin-top:28px">Журнал отправок в CRM</h3>'
            + '<p class="section-description">Что мы отправили и что ответила CRM. '
            + 'Отметка курьера сохраняется сразу, наружу уходит фоном.</p>'
            + outboxTable();
    }

    document.addEventListener('change', function (event) {
        var select = event.target.closest ? event.target.closest('[data-action-status]') : null;
        if (!select) return;
        var action = select.getAttribute('data-action-status');
        select.disabled = true;
        post('/api/courier/action-statuses/' + encodeURIComponent(action),
             { status_code: select.value })
            .then(function (actions) {
                state.actions = actions;
                toast('Сохранено', 'success');
                render();
            })
            .catch(function (error) {
                toast('Не сохранилось: ' + error.message, 'error');
                select.disabled = false;
            });
    });

    window.CourierDispatchModule = {
        onPageActivated: function (user) {
            state.isAdmin = !!(user && user.role === 'admin');
            load();
        }
    };
})();
