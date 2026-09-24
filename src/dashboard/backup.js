/**
 * Резервные копии: список баз и вложений с кнопками скачивания.
 *
 * Живёт блоком на странице «Управление пользователями», а не отдельным
 * разделом: страница уже только для админа, и это избавляет от заведения
 * новой секции прав, пункта меню и всего, что к ним прилагается.
 *
 * Серверная часть и объяснение, почему снимок снимается через backup(), —
 * src/backup/server.py. Загрузки копии обратно здесь намеренно нет: поверх
 * работающей базы сервер её и не примет, а на пустой сервер она заливается
 * один раз при переезде разовым ключом из панели Amvera.
 */

(function () {
    'use strict';

    let loaded = false;

    function escapeHtml(text) {
        // Свой escape, а не трюк с textContent: тот не экранирует кавычку и
        // молча рвёт значения внутри value="..." (инцидент 2026-08-18).
        return String(text ?? '').replace(/[&<>"']/g, (c) => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
        }[c]));
    }

    function formatSize(mb) {
        if (!mb) return '—';
        if (mb >= 1024) return (mb / 1024).toFixed(2) + ' ГБ';
        return mb.toFixed(mb < 10 ? 2 : 0) + ' МБ';
    }

    function rowsHtml(targets) {
        const rows = [];

        targets.databases.forEach((db) => {
            const size = formatSize(db.size_mb);
            const state = db.exists
                ? escapeHtml(size)
                : '<span style="color: var(--barkhat-text-muted, #999)">нет файла</span>';
            // Предупреждение про эфемерный диск: если база не на /data, она
            // стирается каждой сборкой, и бэкапить там уже нечего.
            const warn = db.exists && !db.persistent
                ? ' <span title="Файл не на постоянном диске — стирается при сборке" ' +
                  'style="color:#c0392b">вне /data</span>'
                : '';
            rows.push(
                '<tr>' +
                '<td><strong>' + escapeHtml(db.name) + '</strong>' + warn + '</td>' +
                '<td>' + escapeHtml(db.note || '') + '</td>' +
                '<td style="text-align:right; white-space:nowrap">' + state + '</td>' +
                '<td style="text-align:right">' +
                (db.exists
                    ? '<button class="btn btn-secondary bk-download" data-kind="db" ' +
                      'data-name="' + escapeHtml(db.name) + '" ' +
                      'data-size="' + escapeHtml(String(db.size_mb)) + '">Скачать</button>'
                    : '') +
                '</td></tr>'
            );
        });

        targets.attachments.forEach((dir) => {
            if (dir.error) {
                rows.push(
                    '<tr><td><strong>' + escapeHtml(dir.name) + '</strong></td>' +
                    '<td colspan="3" style="color:#c0392b">' + escapeHtml(dir.error) + '</td></tr>'
                );
                return;
            }
            rows.push(
                '<tr>' +
                '<td><strong>' + escapeHtml(dir.name) + '</strong></td>' +
                '<td>' + escapeHtml(dir.note || '') + ', файлов ' + escapeHtml(String(dir.files)) + '</td>' +
                '<td style="text-align:right; white-space:nowrap">' + escapeHtml(formatSize(dir.size_mb)) + '</td>' +
                '<td style="text-align:right">' +
                (dir.files
                    ? '<button class="btn btn-secondary bk-download" data-kind="attachments" ' +
                      'data-name="' + escapeHtml(dir.name) + '" ' +
                      'data-size="' + escapeHtml(String(dir.size_mb)) + '">Скачать архив</button>'
                    : '') +
                '</td></tr>'
            );
        });

        return rows.join('');
    }

    function download(kind, name, sizeMb) {
        // Обычная ссылка, а не fetch: файл течёт браузером сразу на диск.
        // Через fetch он собирался бы в памяти вкладки целиком, а базы
        // измеряются гигабайтами.
        const url = kind === 'db'
            ? '/api/backup/db/' + encodeURIComponent(name)
            : '/api/backup/attachments/' + encodeURIComponent(name);
        const link = document.createElement('a');
        link.href = url;
        link.rel = 'noopener';
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);

        if (window.BarhatUI) {
            window.BarhatUI.toast(
                sizeMb >= 100
                    ? 'Скачивание началось. Файл большой — это займёт время, вкладку не закрывайте'
                    : 'Скачивание началось',
                'info'
            );
        }
    }

    async function render() {
        const host = document.getElementById('backup-list');
        if (!host) return;
        host.innerHTML = '<p class="empty-state">Загрузка...</p>';

        let targets;
        try {
            const response = await fetch('/api/backup/targets');
            if (!response.ok) throw new Error('HTTP ' + response.status);
            targets = await response.json();
        } catch (e) {
            host.innerHTML = '<p class="empty-state">Не удалось получить список: ' +
                escapeHtml(e.message) + '</p>';
            return;
        }

        host.innerHTML =
            '<table class="data-table"><thead><tr>' +
            '<th>Что</th><th>Содержимое</th>' +
            '<th style="text-align:right">Размер</th>' +
            '<th style="text-align:right">Действие</th>' +
            '</tr></thead><tbody>' + rowsHtml(targets) + '</tbody></table>';

        // Обработчики вешаем заново после каждой перерисовки: innerHTML
        // выбрасывает прежние вместе с разметкой.
        host.querySelectorAll('.bk-download').forEach((button) => {
            button.addEventListener('click', () => {
                download(button.dataset.kind, button.dataset.name,
                         parseFloat(button.dataset.size) || 0);
            });
        });
    }

    function onPageActivated() {
        // Список считает объём папок вложений обходом каталога — на сетевом
        // /data это не бесплатно. Пересчитываем по кнопке, а не при каждом
        // заходе на страницу пользователей.
        if (loaded) return;
        loaded = true;
        render();
    }

    function bindRefresh() {
        const button = document.getElementById('backup-refresh-btn');
        if (button) button.addEventListener('click', render);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', bindRefresh);
    } else {
        bindRefresh();
    }

    window.BackupModule = { onPageActivated, render };
})();
