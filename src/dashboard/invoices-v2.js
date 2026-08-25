/**
 * Раздел «Согласование счетов v2» — пилот поверх существующих таблиц и API.
 *
 * План:    plans/2026-08-24-счета-новый-раздел.md
 * Эталон:  prototypes/invoices/index.html
 * Стиль:   DESIGN-SPEC.md (токены --bx-*, Vollkorn, линейные иконки, без эмодзи)
 * Стили:   invoices-v2.css (всё скоуплено под .iv2)
 *
 * Работает на том же blueprint, что и старый раздел (src/invoices/server.py).
 * Старый src/dashboard/invoices.js заморожен — из этого файла его не трогаем.
 *
 * Фаза 1: каркас — шапка со счётчиком «N ждут вас» и заглушки видов.
 */

(function() {
    'use strict';

    // === Состояние ===

    let currentUser = null;
    let placeholdersRendered = false;

    // Счётчик перезапрашивается на каждой активации страницы. Токен нужен,
    // чтобы ответ на устаревший запрос не перезаписал более свежий: человек
    // может уйти из раздела и вернуться раньше, чем придёт первый ответ.
    let countRequestToken = 0;

    // === Утилиты ===

    function $(id) {
        return document.getElementById(id);
    }

    /**
     * Склонение «счёт / счёта / счетов» — для подписей вида «12 счетов».
     * Пригодится и дальше, поэтому лежит здесь, а не в месте вызова.
     */
    function plural(n, one, few, many) {
        const mod100 = Math.abs(n) % 100;
        const mod10 = mod100 % 10;
        if (mod100 >= 11 && mod100 <= 14) return many;
        if (mod10 === 1) return one;
        if (mod10 >= 2 && mod10 <= 4) return few;
        return many;
    }

    // === Счётчик в шапке ===

    /**
     * «N ждут вас» — счета в статусе «На согласовании».
     *
     * Временное решение Фазы 1: считаем длину списка из существующей ручки.
     * В Фазе 2 это заменяется на GET /api/invoices/summary, который отдаёт
     * все четыре KPI одним запросом и считает по всей базе, а не по странице.
     * Отсюда и «100+»: список ограничен limit=100 на сервере.
     */
    async function loadPendingCount() {
        const badge = $('iv2HeaderCount');
        if (!badge) return;

        const token = ++countRequestToken;

        try {
            const response = await fetch('/api/invoices?status=on_approval&limit=100', {
                credentials: 'include',
            });
            if (!response.ok) throw new Error('HTTP ' + response.status);

            const data = await response.json();
            if (token !== countRequestToken) return;  // пришёл устаревший ответ

            const count = Array.isArray(data.invoices) ? data.invoices.length : 0;
            if (count === 0) {
                badge.textContent = 'всё согласовано';
            } else if (count >= 100) {
                badge.textContent = '100+ ждут вас';
            } else {
                badge.textContent = count + ' ' + plural(count, 'ждёт', 'ждут', 'ждут') + ' вас';
            }
            badge.hidden = false;
        } catch (error) {
            if (token !== countRequestToken) return;
            // Счётчик — не главное на экране: молча прячем, а не показываем
            // ошибку в шапке. Разбор счетов от этого не ломается.
            console.error('invoices-v2: не удалось загрузить счётчик:', error);
            badge.hidden = true;
        }
    }

    // === Заглушки видов (снимаются по мере готовности фаз) ===

    function renderPlaceholders() {
        if (placeholdersRendered) return;

        const queue = $('iv2ViewQueue');
        if (queue) {
            queue.innerHTML = `
                <div class="iv2-placeholder">
                    <h3 class="iv2-placeholder__title">Раздел в работе</h3>
                    <p>Каркас готов. Дальше приезжают список с фильтрами, очередь согласования
                       со сканом рядом с полями и платёжный борд.</p>
                    <p style="margin-top:10px">Пока счета заводятся и согласуются в разделе «Счета на оплату».</p>
                </div>`;
        }

        placeholdersRendered = true;
    }

    // === Точка входа ===

    /**
     * Вызывается из script.js при переходе на страницу раздела.
     * Данные грузим здесь, а не при загрузке дашборда: иначе каждый вход
     * в любой раздел дёргал бы API счетов.
     */
    function onPageActivated(user) {
        currentUser = user || currentUser;
        renderPlaceholders();
        loadPendingCount();
    }

    window.InvoicesV2Module = {
        onPageActivated,
    };
})();
