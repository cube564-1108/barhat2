/**
 * Ссылки на товары — JavaScript
 *
 * Сторож: проверяет, что ссылка на товар из карточки в RetailCRM действительно
 * открывает эту карточку на сайте. Прогон идёт сам раз в сутки ночью и занимает
 * около получаса, поэтому страница показывает результат последней проверки из
 * локальной базы, а не ходит по сайту в момент открытия.
 *
 * Разбор проблемы и порядок починки: docs/инструкция-ссылки-товаров-битрикс-crm.md
 */

(function() {
    'use strict';

    // Пока идёт прогон, опрашиваем статус — иначе пришлось бы обновлять страницу
    // руками, чтобы увидеть результат.
    const POLL_INTERVAL_MS = 20000;
    let pollTimer = null;

    // ================= Загрузка =================

    async function loadStatus() {
        try {
            const response = await fetch('/api/linkwatch/status');
            if (!response.ok) throw new Error('Не удалось получить статус');
            const data = await response.json();

            renderSummary(data);
            setRunning(data.running);

            if (!data.running) {
                await loadBroken();
            }
        } catch (e) {
            document.getElementById('linkWatchSummary').innerHTML =
                `<p class="empty-state">${escapeHtml(e.message)}</p>`;
        }
    }

    async function loadBroken() {
        const wrap = document.getElementById('linkWatchTableWrap');
        try {
            const response = await fetch('/api/linkwatch/broken');
            if (!response.ok) throw new Error('Не удалось получить список');
            const data = await response.json();
            renderTable(data.items || [], data.titles || {});
        } catch (e) {
            wrap.innerHTML = `<p class="empty-state">${escapeHtml(e.message)}</p>`;
        }
    }

    // ================= Отрисовка =================

    function renderSummary(data) {
        const box = document.getElementById('linkWatchSummary');

        if (!data.configured) {
            box.innerHTML = '<p class="empty-state">RetailCRM не настроена — проверять нечего</p>';
            return;
        }

        const run = data.last_run;
        if (!run) {
            box.innerHTML = '<p class="empty-state">Проверок ещё не было. Первая пройдёт ночью либо запустите вручную</p>';
            return;
        }

        if (run.status === 'failed') {
            box.innerHTML =
                `<p class="empty-state">Последняя проверка сорвалась: ${escapeHtml(run.error || 'причина не записана')}</p>`;
            return;
        }

        // «Без ссылки» — это НЕ поломка: у товаров без публичной страницы
        // («Товары МС», снятые с публикации сезонные) пустая ссылка и есть
        // правильное состояние, его специально добивается пост-обработчик выгрузки.
        const tiles = [
            { label: 'Проверено товаров', value: run.checked },
            { label: 'Ссылка рабочая', value: run.ok },
            { label: 'Ссылка сломана', value: run.broken, bad: run.broken > 0 },
            { label: 'Без ссылки (норма)', value: run.no_url },
        ];

        box.innerHTML = tiles.map(function(tile) {
            const badge = tile.bad
                ? ' <span class="bx-badge bx-badge--bad">чинить</span>'
                : '';
            return `
                <div class="card">
                    <div class="label">${escapeHtml(tile.label)}</div>
                    <div class="value">${formatCount(tile.value)}${badge}</div>
                </div>`;
        }).join('');

        const when = run.finished_at
            ? (window.BarhatTime ? window.BarhatTime.formatDateTime(run.finished_at) : run.finished_at)
            : '';
        document.getElementById('linkWatchState').textContent =
            when ? `Последняя проверка: ${when}` : '';
    }

    function renderTable(items, titles) {
        const wrap = document.getElementById('linkWatchTableWrap');

        if (!items.length) {
            wrap.innerHTML = '<p class="empty-state">Сломанных ссылок нет</p>';
            return;
        }

        const rows = items.map(function(item) {
            const right = item.canonical_url
                ? `<a href="${escapeHtml(item.canonical_url)}" target="_blank" rel="noopener">${escapeHtml(item.canonical_url)}</a>`
                : '<span class="section-description">страницы нет</span>';
            return `
                <tr>
                    <td class="num">${escapeHtml(item.article || '')}</td>
                    <td>${escapeHtml(item.name || '')}</td>
                    <td>${escapeHtml(titles[item.status] || item.status)}</td>
                    <td style="word-break: break-all;">
                        <a href="${escapeHtml(item.url)}" target="_blank" rel="noopener">${escapeHtml(item.url)}</a>
                    </td>
                    <td style="word-break: break-all;">${right}</td>
                </tr>`;
        }).join('');

        wrap.innerHTML = `
            <table style="width: 100%;">
                <thead>
                    <tr>
                        <th class="num">Артикул</th>
                        <th>Наименование</th>
                        <th>Что не так</th>
                        <th>Ссылка сейчас (в CRM)</th>
                        <th>Правильная ссылка</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>`;
    }

    function setRunning(running) {
        const btn = document.getElementById('linkWatchRunBtn');
        const state = document.getElementById('linkWatchState');

        if (btn) {
            btn.disabled = !!running;
            btn.textContent = running ? 'Проверка идёт...' : 'Проверить сейчас';
        }
        if (running && state) {
            state.textContent = 'Идёт обход сайта, это занимает около получаса';
        }

        if (running && !pollTimer) {
            pollTimer = setInterval(loadStatus, POLL_INTERVAL_MS);
        } else if (!running && pollTimer) {
            clearInterval(pollTimer);
            pollTimer = null;
        }
    }

    // ================= Действия =================

    async function runNow() {
        // Нативные confirm/alert внутри iframe Пульса молча игнорируются —
        // только BarhatUI (см. src/dashboard/ui-dialog.js).
        const ok = await window.BarhatUI.confirm(
            'Проверка обойдёт все товары и займёт около получаса. Запустить?',
            { title: 'Проверить ссылки' }
        );
        if (!ok) return;

        try {
            const response = await fetch('/api/linkwatch/run', { method: 'POST' });
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || 'Не удалось запустить');
            setRunning(true);
        } catch (e) {
            window.BarhatUI.alert(e.message, { title: 'Ошибка' });
        }
    }

    function exportCsv() {
        window.location.href = '/api/linkwatch/export';
    }

    // ================= Утилиты =================

    function formatCount(value) {
        return new Intl.NumberFormat('ru-RU').format(value || 0);
    }

    function escapeHtml(str) {
        return String(str ?? '').replace(/[&<>"']/g, (c) => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
        }[c]));
    }

    // ================= Инициализация =================

    function init() {
        const runBtn = document.getElementById('linkWatchRunBtn');
        const exportBtn = document.getElementById('linkWatchExportBtn');
        if (runBtn) runBtn.addEventListener('click', runNow);
        if (exportBtn) exportBtn.addEventListener('click', exportCsv);

        // Данные грузим при активации страницы, а не при загрузке дашборда:
        // иначе каждый вход в любой раздел дёргал бы этот запрос.
        const observer = new MutationObserver(function(mutations) {
            mutations.forEach(function(mutation) {
                if (mutation.target.classList.contains('active') &&
                    mutation.target.getAttribute('data-page') === 'link_watch') {
                    loadStatus();
                }
            });
        });

        document.querySelectorAll('.page').forEach(function(page) {
            observer.observe(page, { attributes: true, attributeFilter: ['class'] });
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
