/**
 * Обратная связь от сотрудников владельцу.
 *
 * Две части в одном файле:
 *   1. Виджет для всех — плавающая кнопка на любом экране, форма «что мешает»,
 *      вкладка со своими обращениями и их статусами.
 *   2. Экран владельца — очередь разбора, встраивается в страницу /dashboard
 *      рядом с разделом «Задачи» (admin-only).
 *
 * Модуль монтирует свою разметку и стили сам: index.html правится одной
 * строкой подключения, а виджет обязан работать на всех страницах сразу.
 *
 * ЧТО ЗДЕСЬ ВАЖНО НЕ СЛОМАТЬ:
 *
 *  - Кнопка живёт в правом нижнем углу, где уже есть чужие фиксированные
 *    элементы: тосты BarhatUI, панель массовых действий счетов (.iv2-bulkbar),
 *    модалки (z-index 2000/2001). Поэтому z-index ниже модалок, а при открытом
 *    модальном окне кнопка прячется — иначе она накроет «Сохранить» на телефоне.
 *  - Нативные alert/confirm/prompt внутри Пульса молча игнорируются: только
 *    window.BarhatUI.
 *  - Список похожих обновляется точечно, без перерисовки формы: перезапись
 *    innerHTML съедает каретку, и набор текста обрывается на первом символе.
 *  - Ни одного запроса при загрузке страницы: /data на Amvera медленный.
 *    Счётчики бейджей грузятся отложенно, остальное — по клику.
 *
 * План: plans/2026-09-02-обратная-связь-от-пользователей.md
 */

(function () {
    'use strict';

    const STYLE_ID = 'bx-feedback-styles';
    const Z_BUTTON = 1500;    // ниже модалок (2000), выше контента
    const Z_MODAL = 99000;    // выше модалок, ниже диалогов BarhatUI (100000)

    const AJAX_HEADERS = {
        'Content-Type': 'application/json',
        'X-Requested-With': 'barhat-dashboard',
    };

    const TYPES = [
        { id: 'bug', label: 'Не работает' },
        { id: 'inconvenience', label: 'Неудобно' },
        { id: 'wish', label: 'Хочу добавить' },
    ];

    const TYPE_LABELS = {
        bug: 'Не работает',
        inconvenience: 'Неудобно',
        wish: 'Хочу добавить',
    };

    const STATUS_LABELS = {
        new: 'Новое',
        accepted: 'Взято в работу',
        done: 'Сделано',
        rejected: 'Отклонено',
        need_info: 'Нужен ответ',
    };

    // Названия модулей для человека. Дублируют getSectionTitle из script.js,
    // но в нижнем регистре: там заголовки страниц, здесь подпись в карточке.
    const MODULE_TITLES = {
        dashboard: 'Дашборд',
        calculator: 'Калькулятор букетов',
        quality: 'Качество сборки',
        cash_shifts: 'Кассовые смены',
        invoices: 'Счета (архив)',
        invoices_v2: 'Согласование счетов',
        writeoffs: 'Списания товара',
        abc_analysis: 'ABC-анализ',
        courier_payouts: 'Оплата курьерам',
        link_watch: 'Ссылки на товары',
        users: 'Пользователи',
    };

    const MIN_TEXT_LENGTH = 10;

    const state = {
        user: null,
        isOwner: false,
        open: false,
        tab: 'write',
        type: 'inconvenience',
        similar: [],
        supported: {},      // id -> true, чтобы не звать поддержку дважды
        submitting: false,
        prefill: '',
        ownerItems: [],
        ownerFilter: 'new',
        ownerLoaded: false,
    };

    let button = null;
    let modalRoot = null;
    let searchTimer = null;

    // === Стили (DESIGN-SPEC.md: токены --bx-*, Vollkorn, без эмодзи) ===

    function injectStyles() {
        if (document.getElementById(STYLE_ID)) return;
        const style = document.createElement('style');
        style.id = STYLE_ID;
        style.textContent = `
.bxfb-button {
    position: fixed; right: 20px; bottom: 20px; z-index: ${Z_BUTTON};
    display: inline-flex; align-items: center; gap: 8px;
    padding: 10px 16px; border: none; border-radius: 9999px;
    font-family: var(--bx-font-body, 'Inter', 'PT Sans', system-ui, sans-serif);
    font-size: 14px; font-weight: 600; color: #fff; cursor: pointer;
    background: var(--bx-wine, var(--barkhat-wine, #411330));
    box-shadow: 0 8px 24px rgba(19, 8, 16, 0.28);
    transition: opacity .15s ease, transform .15s ease;
}
.bxfb-button:hover { opacity: .92; }
.bxfb-button[hidden] { display: none; }
.bxfb-button__dot {
    position: absolute; top: 4px; right: 6px;
    width: 9px; height: 9px; border-radius: 50%;
    background: var(--bx-pink-bright, #E1A4C9); border: 2px solid var(--bx-wine, #411330);
}
@media (max-width: 640px) {
    .bxfb-button { right: 16px; bottom: 16px; padding: 10px 14px; font-size: 13px; }
    .bxfb-button__label { display: none; }
}

.bxfb-ovl {
    position: fixed; inset: 0; z-index: ${Z_MODAL};
    display: flex; align-items: center; justify-content: center; padding: 20px;
    background: rgba(19, 8, 16, 0.55);
    font-family: var(--bx-font-body, 'Inter', 'PT Sans', system-ui, sans-serif);
}
.bxfb-modal {
    width: 100%; max-width: 560px; max-height: 88vh; overflow-y: auto;
    background: var(--bx-white, #fff); border-radius: var(--bx-r-2xl, 16px);
    box-shadow: 0 18px 48px rgba(19, 8, 16, 0.28); padding: 24px;
}
.bxfb-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.bxfb-title {
    margin: 0; font-family: var(--bx-font-head, 'Vollkorn', Georgia, serif);
    font-size: 21px; font-weight: 700; color: var(--bx-wine, #411330);
}
.bxfb-sub { margin: 4px 0 0; font-size: 13px; color: var(--bx-muted, #9b8f97); }
.bxfb-close {
    background: none; border: none; cursor: pointer; padding: 4px;
    color: var(--bx-muted, #9b8f97); line-height: 0;
}
.bxfb-close:hover { color: var(--bx-wine, #411330); }

.bxfb-tabs {
    display: inline-flex; gap: 4px; padding: 4px; margin: 18px 0 16px;
    border-radius: var(--bx-r-xl, 12px); background: rgba(228, 194, 221, 0.15);
}
.bxfb-tab {
    padding: 7px 14px; font-size: 13.5px; border: none; background: none;
    border-radius: var(--bx-r-lg, 8px); color: var(--bx-wine, #411330); cursor: pointer;
}
.bxfb-tab--active { background: var(--bx-wine, #411330); color: #fff; font-weight: 500; }

.bxfb-label {
    display: block; margin: 0 0 6px; font-size: 12px; font-weight: 600;
    text-transform: uppercase; letter-spacing: .03em; color: var(--bx-muted, #9b8f97);
}
.bxfb-types { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 16px; }
.bxfb-type {
    padding: 8px 14px; font-size: 13.5px; cursor: pointer;
    border: 1px solid var(--bx-border, #eee2ea); border-radius: 9999px;
    background: var(--bx-white, #fff); color: var(--bx-text, #3C3C3C);
}
.bxfb-type--active {
    background: var(--bx-wine, #411330); border-color: var(--bx-wine, #411330); color: #fff;
}
.bxfb-input {
    width: 100%; box-sizing: border-box; padding: 12px 14px;
    font: inherit; font-size: 14px; color: var(--bx-text, #3C3C3C);
    border: 1px solid var(--bx-border, #eee2ea); border-radius: var(--bx-r-xl, 12px);
    resize: vertical; outline: none;
}
.bxfb-input:focus {
    border-color: var(--bx-pink-deep, #B26FA1);
    box-shadow: 0 0 0 3px rgba(178, 111, 161, 0.15);
}
.bxfb-hint { margin: 6px 0 16px; font-size: 12px; color: var(--bx-muted, #9b8f97); }

.bxfb-similar { margin: 0 0 16px; }
.bxfb-similar__title {
    margin: 0 0 8px; font-size: 13px; font-weight: 600; color: var(--bx-wine, #411330);
}
.bxfb-similar__item {
    display: flex; align-items: flex-start; justify-content: space-between; gap: 12px;
    padding: 10px 12px; margin-bottom: 8px;
    border: 1px solid var(--bx-border, #eee2ea); border-radius: var(--bx-r-lg, 8px);
    background: var(--bx-bg, #faf4f9);
}
.bxfb-similar__text { font-size: 13px; line-height: 1.45; color: var(--bx-text, #3C3C3C); }
.bxfb-similar__meta { font-size: 11.5px; color: var(--bx-muted, #9b8f97); margin-top: 3px; }

.bxfb-actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 4px; }
.bxfb-btn {
    padding: 10px 18px; font: inherit; font-size: 14px; font-weight: 600;
    border: 1px solid transparent; border-radius: var(--bx-r-xl, 12px); cursor: pointer;
    background: var(--bx-wine, #411330); color: #fff;
}
.bxfb-btn:disabled { opacity: .5; cursor: default; }
.bxfb-btn--ghost {
    background: transparent; color: var(--bx-wine, #411330);
    border-color: var(--bx-border, #eee2ea);
}
.bxfb-btn--sm { padding: 6px 12px; font-size: 12.5px; font-weight: 500; }

.bxfb-badge {
    display: inline-block; border-radius: 9999px; padding: 2px 10px;
    font-size: 11px; font-weight: 500; white-space: nowrap;
}
.bxfb-badge--new { background: rgba(0,0,0,0.05); color: #6F6F6F; }
.bxfb-badge--accepted { background: #fffbeb; color: #b45309; border: 1px solid #fde68a; }
.bxfb-badge--done { background: #ecfdf5; color: #047857; border: 1px solid #a7f3d0; }
.bxfb-badge--rejected { background: rgba(0,0,0,0.05); color: #6F6F6F; }
.bxfb-badge--need_info { background: #fffbeb; color: #b45309; border: 1px solid #fde68a; }
.bxfb-badge--bug { background: var(--bx-wine, #411330); color: #fff; }
.bxfb-badge--inconvenience { background: rgba(228,194,221,0.4); color: var(--bx-wine, #411330); }
.bxfb-badge--wish { background: rgba(0,0,0,0.05); color: #6F6F6F; }

.bxfb-item {
    padding: 14px; margin-bottom: 10px;
    border: 1px solid var(--bx-border, #eee2ea); border-radius: var(--bx-r-xl, 12px);
    background: var(--bx-white, #fff);
}
.bxfb-item__head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; }
.bxfb-item__meta { font-size: 11.5px; color: var(--bx-muted, #9b8f97); }
.bxfb-item__text { font-size: 14px; line-height: 1.5; color: var(--bx-text, #3C3C3C); white-space: pre-line; }
.bxfb-item__answer {
    margin-top: 10px; padding: 10px 12px; font-size: 13px; line-height: 1.5;
    border-left: 3px solid var(--bx-pink, #D19CC2); background: var(--bx-bg, #faf4f9);
    color: var(--bx-text-2, #6F6F6F);
}
.bxfb-item__tech { margin-top: 10px; font-size: 12px; color: var(--bx-text-2, #6F6F6F); }
.bxfb-item__tech pre {
    margin: 6px 0 0; padding: 8px 10px; overflow-x: auto;
    background: #f5f0f4; border-radius: var(--bx-r-md, 6px); font-size: 11.5px;
}
.bxfb-item__actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
.bxfb-empty { padding: 18px; text-align: center; font-size: 13.5px; color: var(--bx-muted, #9b8f97); }

.bxfb-owner__filters { display: flex; gap: 8px; margin-bottom: 14px; }
.bxfb-copy { width: 100%; box-sizing: border-box; height: 220px; font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 12px; }
`;
        document.head.appendChild(style);
    }

    // === Общее ===

    // Не div.textContent — тот не экранирует кавычки, и значение с " обрывает
    // атрибут (инцидент 2026-08-18, повторён в четырёх файлах дашборда).
    function escapeHtml(text) {
        return String(text ?? '').replace(/[&<>"']/g, (c) => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
        }[c]));
    }

    function formatDate(value) {
        return window.BarhatTime ? window.BarhatTime.formatDateTimeLong(value) : String(value || '');
    }

    function moduleTitle(id) {
        return MODULE_TITLES[id] || id || 'не указан';
    }

    function currentModule() {
        const page = document.querySelector('.page.active[data-page]');
        return page ? page.getAttribute('data-page') : null;
    }

    async function api(url, options) {
        const response = await fetch(url, Object.assign({ credentials: 'include' }, options || {}));
        let data = {};
        try { data = await response.json(); } catch (e) { /* пустой ответ */ }
        if (!response.ok) throw new Error(data.error || `Не удалось выполнить запрос (${response.status})`);
        return data;
    }

    // === Плавающая кнопка ===

    function createButton() {
        button = document.createElement('button');
        button.type = 'button';
        button.className = 'bxfb-button';
        button.title = 'Написать владельцу о проблеме или идее';
        button.innerHTML = `
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                 stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
            </svg>
            <span class="bxfb-button__label">Есть идея</span>
        `;
        button.addEventListener('click', () => openWidget());
        document.body.appendChild(button);
    }

    // Чужие фиксированные элементы не должны оказаться под нашей кнопкой.
    // Наблюдаем за DOM, а не вешаем ручные вызовы: модалок в дашборде больше
    // десятка, и каждая открывается по-своему.
    const BLOCKING_SELECTOR = '.modal.active, .iv2-modal, .bx-dialog-backdrop, .iv2-bulkbar, .bxfb-ovl';

    function refreshButtonVisibility() {
        if (!button) return;
        button.hidden = Boolean(document.querySelector(BLOCKING_SELECTOR));
    }

    function watchOverlays() {
        let scheduled = false;
        const observer = new MutationObserver(() => {
            if (scheduled) return;
            scheduled = true;
            requestAnimationFrame(() => {
                scheduled = false;
                refreshButtonVisibility();
            });
        });
        observer.observe(document.body, {
            childList: true,
            subtree: true,
            attributes: true,
            attributeFilter: ['class'],
        });
    }

    // === Виджет ===

    function openWidget(prefillText, type) {
        state.open = true;
        state.tab = 'write';
        state.prefill = prefillText || '';
        state.type = type || 'inconvenience';
        state.similar = [];
        renderModal();
        refreshButtonVisibility();
    }

    function closeWidget() {
        state.open = false;
        state.prefill = '';
        if (modalRoot) {
            modalRoot.remove();
            modalRoot = null;
        }
        refreshButtonVisibility();
    }

    function renderModal() {
        if (modalRoot) modalRoot.remove();

        modalRoot = document.createElement('div');
        modalRoot.className = 'bxfb-ovl';
        modalRoot.innerHTML = `
            <div class="bxfb-modal" role="dialog" aria-modal="true">
                <div class="bxfb-head">
                    <div>
                        <h2 class="bxfb-title">Обратная связь</h2>
                        <p class="bxfb-sub">Пишите как есть — разберусь и отвечу</p>
                    </div>
                    <button type="button" class="bxfb-close" data-action="close" aria-label="Закрыть">
                        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                             stroke-width="1.75" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>
                    </button>
                </div>
                <div class="bxfb-tabs">
                    <button type="button" class="bxfb-tab ${state.tab === 'write' ? 'bxfb-tab--active' : ''}"
                            data-tab="write">Написать</button>
                    <button type="button" class="bxfb-tab ${state.tab === 'mine' ? 'bxfb-tab--active' : ''}"
                            data-tab="mine">Мои обращения</button>
                </div>
                <div class="bxfb-body"></div>
            </div>
        `;

        modalRoot.addEventListener('mousedown', (e) => {
            if (e.target === modalRoot) closeWidget();
        });
        modalRoot.querySelector('[data-action="close"]').addEventListener('click', closeWidget);
        modalRoot.querySelectorAll('.bxfb-tab').forEach((tab) => {
            tab.addEventListener('click', () => {
                state.tab = tab.getAttribute('data-tab');
                renderModal();
            });
        });

        document.body.appendChild(modalRoot);

        if (state.tab === 'write') renderWriteTab();
        else renderMineTab();
    }

    function body() {
        return modalRoot ? modalRoot.querySelector('.bxfb-body') : null;
    }

    function renderWriteTab() {
        const host = body();
        if (!host) return;

        host.innerHTML = `
            <span class="bxfb-label">Что это</span>
            <div class="bxfb-types">
                ${TYPES.map((type) => `
                    <button type="button" class="bxfb-type ${state.type === type.id ? 'bxfb-type--active' : ''}"
                            data-type="${type.id}">${escapeHtml(type.label)}</button>
                `).join('')}
            </div>

            <span class="bxfb-label">Что мешает</span>
            <textarea class="bxfb-input" id="bxfbText" rows="4"
                      placeholder="Например: при закрытии смены сумма наличных сбрасывается, и её приходится вводить заново">${escapeHtml(state.prefill)}</textarea>
            <p class="bxfb-hint">Опишите проблему, а не решение — так быстрее найдётся лучший вариант</p>

            <div class="bxfb-similar" id="bxfbSimilar"></div>

            <span class="bxfb-label">Как хотелось бы (необязательно)</span>
            <textarea class="bxfb-input" id="bxfbWish" rows="2"
                      placeholder="Если есть мысль, как удобнее"></textarea>
            <p class="bxfb-hint">Экран, роль и последние ошибки приложатся сами</p>

            <div class="bxfb-actions">
                <button type="button" class="bxfb-btn bxfb-btn--ghost" data-action="cancel">Отмена</button>
                <button type="button" class="bxfb-btn" data-action="submit">Отправить</button>
            </div>
        `;

        host.querySelectorAll('.bxfb-type').forEach((chip) => {
            chip.addEventListener('click', () => {
                state.type = chip.getAttribute('data-type');
                // Перерисовываем только пилюли: перезапись всей формы стёрла бы
                // набранный текст вместе с кареткой.
                host.querySelectorAll('.bxfb-type').forEach((other) => {
                    other.classList.toggle('bxfb-type--active',
                        other.getAttribute('data-type') === state.type);
                });
            });
        });

        const textarea = host.querySelector('#bxfbText');
        textarea.addEventListener('input', scheduleSimilarSearch);
        host.querySelector('[data-action="cancel"]').addEventListener('click', closeWidget);
        host.querySelector('[data-action="submit"]').addEventListener('click', submit);

        textarea.focus();
        const length = textarea.value.length;
        if (length) {
            textarea.setSelectionRange(length, length);
            scheduleSimilarSearch();
        }
    }

    function scheduleSimilarSearch() {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(searchSimilar, 500);
    }

    async function searchSimilar() {
        const host = modalRoot && modalRoot.querySelector('#bxfbSimilar');
        const textarea = modalRoot && modalRoot.querySelector('#bxfbText');
        if (!host || !textarea) return;

        const text = textarea.value.trim();
        if (text.length < MIN_TEXT_LENGTH) {
            host.innerHTML = '';
            return;
        }

        try {
            const params = new URLSearchParams({ q: text });
            const module = currentModule();
            if (module) params.set('module', module);
            const data = await api(`/api/feedback/similar?${params.toString()}`);
            state.similar = data.items || [];
        } catch (e) {
            state.similar = [];   // подсказки — необязательная роскошь, молчим
        }
        renderSimilar();
    }

    function renderSimilar() {
        const host = modalRoot && modalRoot.querySelector('#bxfbSimilar');
        if (!host) return;

        if (!state.similar.length) {
            host.innerHTML = '';
            return;
        }

        host.innerHTML = `
            <p class="bxfb-similar__title">Похоже, об этом уже писали</p>
            ${state.similar.map((item) => `
                <div class="bxfb-similar__item">
                    <div>
                        <div class="bxfb-similar__text">${escapeHtml(item.text)}</div>
                        <div class="bxfb-similar__meta">
                            ${escapeHtml(STATUS_LABELS[item.status] || item.status)} ·
                            просили ${item.people_count} чел.
                        </div>
                    </div>
                    <button type="button" class="bxfb-btn bxfb-btn--ghost bxfb-btn--sm"
                            data-support="${item.id}" ${item.mine || state.supported[item.id] ? 'disabled' : ''}>
                        ${item.mine ? 'Ваше' : (state.supported[item.id] ? 'Учтено' : 'У меня тоже')}
                    </button>
                </div>
            `).join('')}
        `;

        host.querySelectorAll('[data-support]').forEach((btn) => {
            btn.addEventListener('click', () => supportItem(parseInt(btn.getAttribute('data-support'), 10), btn));
        });
    }

    async function supportItem(itemId, btn) {
        btn.disabled = true;   // один медленный клик не должен превратиться в три запроса
        try {
            const data = await api(`/api/feedback/${itemId}/support`, {
                method: 'POST',
                headers: AJAX_HEADERS,
            });
            state.supported[itemId] = true;
            btn.textContent = 'Учтено';
            window.BarhatUI.toast(
                `Засчитано — уже ${data.people_count} чел. Отдельное обращение писать не нужно`,
                'success'
            );
        } catch (e) {
            btn.disabled = false;
            window.BarhatUI.toast(e.message, 'error');
        }
    }

    async function submit() {
        if (state.submitting) return;

        const textarea = modalRoot.querySelector('#bxfbText');
        const wish = modalRoot.querySelector('#bxfbWish');
        const text = textarea.value.trim();

        if (text.length < MIN_TEXT_LENGTH) {
            window.BarhatUI.toast('Опишите подробнее, что мешает — хотя бы одним предложением', 'error');
            textarea.focus();
            return;
        }

        const submitBtn = modalRoot.querySelector('[data-action="submit"]');
        state.submitting = true;
        submitBtn.disabled = true;
        submitBtn.textContent = 'Отправляем...';

        try {
            await api('/api/feedback', {
                method: 'POST',
                headers: AJAX_HEADERS,
                body: JSON.stringify({
                    text,
                    wish: wish.value.trim() || null,
                    type: state.type,
                    module: currentModule(),
                    page_url: window.location.pathname,
                    client_context: collectContext(),
                }),
            });
            closeWidget();
            window.BarhatUI.toast('Отправлено. Ответ появится во вкладке «Мои обращения»', 'success');
        } catch (e) {
            window.BarhatUI.toast(e.message, 'error');
            submitBtn.disabled = false;
            submitBtn.textContent = 'Отправить';
        } finally {
            state.submitting = false;
        }
    }

    function collectContext() {
        const context = {
            screen: `${window.innerWidth}x${window.innerHeight}`,
            user_agent: navigator.userAgent,
        };
        if (window.BarhatTime) context.timezone = window.BarhatTime.zoneLabel();
        // Последние неудачные запросы копит обёртка fetch в ui-dialog.js —
        // ради этого «не сохраняется счёт» приезжает с кодом и путём.
        if (window.BarhatUI && window.BarhatUI.recentErrors) {
            const errors = window.BarhatUI.recentErrors();
            if (errors.length) context.errors = errors;
        }
        return context;
    }

    async function renderMineTab() {
        const host = body();
        if (!host) return;

        host.innerHTML = '<div class="bxfb-empty">Загружаем...</div>';

        let items = [];
        try {
            const data = await api('/api/feedback/mine');
            items = data.items || [];
        } catch (e) {
            host.innerHTML = `<div class="bxfb-empty">${escapeHtml(e.message)}</div>`;
            return;
        }

        if (!items.length) {
            host.innerHTML = '<div class="bxfb-empty">Вы пока ничего не отправляли</div>';
            return;
        }

        host.innerHTML = items.map((item) => `
            <div class="bxfb-item">
                <div class="bxfb-item__head">
                    <span class="bxfb-badge bxfb-badge--${item.status}">
                        ${escapeHtml(STATUS_LABELS[item.status] || item.status)}
                    </span>
                    <span class="bxfb-item__meta">
                        ${escapeHtml(moduleTitle(item.module))} · ${escapeHtml(formatDate(item.created_at))}
                        ${item.people_count > 1 ? ` · просили ${item.people_count} чел.` : ''}
                    </span>
                </div>
                <div class="bxfb-item__text">${escapeHtml(item.text)}</div>
                ${item.resolution
                    ? `<div class="bxfb-item__answer">${escapeHtml(item.resolution)}</div>`
                    : ''}
            </div>
        `).join('');

        // Точку об обновлениях гасит сам запрос /mine — обновим бейдж кнопки
        loadCounters();
    }

    // === Бейджи ===

    async function loadCounters() {
        try {
            const data = await api('/api/feedback/counters');
            applyCounters(data);
        } catch (e) {
            /* счётчик — украшение, молча пропускаем */
        }
    }

    function applyCounters(data) {
        if (button) {
            const existing = button.querySelector('.bxfb-button__dot');
            if (data.my_updates > 0 && !existing) {
                const dot = document.createElement('span');
                dot.className = 'bxfb-button__dot';
                button.appendChild(dot);
            } else if (!data.my_updates && existing) {
                existing.remove();
            }
        }

        const counter = document.getElementById('bxfbOwnerCount');
        if (counter) {
            counter.textContent = data.new_for_owner ? `${data.new_for_owner} новых` : '';
        }
    }

    // === Экран владельца ===

    function mountOwnerSection() {
        if (document.getElementById('bxfbOwnerSection')) return;

        const page = document.querySelector('.page[data-page="dashboard"]');
        if (!page) return;

        const section = document.createElement('section');
        section.className = 'section';
        section.id = 'bxfbOwnerSection';
        section.innerHTML = `
            <h2 class="section-title">ОБРАТНАЯ СВЯЗЬ <span id="bxfbOwnerCount" class="bxfb-item__meta"></span></h2>
            <div class="bxfb-owner__filters">
                <button type="button" class="bxfb-btn bxfb-btn--sm" data-filter="new">Новые</button>
                <button type="button" class="bxfb-btn bxfb-btn--ghost bxfb-btn--sm" data-filter="">Все</button>
            </div>
            <div id="bxfbOwnerList"></div>
        `;

        // Рядом с «Задачами»: оба раздела admin-only и оба про то, что делать дальше
        const tasksSection = document.getElementById('tasksSection');
        if (tasksSection) page.insertBefore(section, tasksSection);
        else page.appendChild(section);

        section.querySelectorAll('[data-filter]').forEach((btn) => {
            btn.addEventListener('click', () => {
                state.ownerFilter = btn.getAttribute('data-filter');
                section.querySelectorAll('[data-filter]').forEach((other) => {
                    const active = other.getAttribute('data-filter') === state.ownerFilter;
                    other.classList.toggle('bxfb-btn--ghost', !active);
                });
                loadOwnerQueue();
            });
        });

        loadOwnerQueue();
    }

    async function loadOwnerQueue() {
        const host = document.getElementById('bxfbOwnerList');
        if (!host) return;

        host.innerHTML = '<div class="bxfb-empty">Загружаем...</div>';
        try {
            const params = state.ownerFilter ? `?status=${state.ownerFilter}` : '';
            const data = await api(`/api/feedback${params}`);
            state.ownerItems = data.items || [];
        } catch (e) {
            host.innerHTML = `<div class="bxfb-empty">${escapeHtml(e.message)}</div>`;
            return;
        }
        renderOwnerQueue();
        loadCounters();
    }

    function renderOwnerQueue() {
        const host = document.getElementById('bxfbOwnerList');
        if (!host) return;

        if (!state.ownerItems.length) {
            host.innerHTML = '<div class="bxfb-empty">Обращений нет</div>';
            return;
        }

        host.innerHTML = state.ownerItems.map(ownerCardHtml).join('');

        host.querySelectorAll('button[data-action]').forEach((btn) => {
            btn.addEventListener('click', () => ownerAction(
                btn.getAttribute('data-action'),
                parseInt(btn.getAttribute('data-id'), 10),
                btn
            ));
        });
    }

    function ownerCardHtml(item) {
        let context = {};
        try { context = item.client_context ? JSON.parse(item.client_context) : {}; } catch (e) { /* нет */ }
        const errors = context.errors || [];

        const tech = errors.length ? `
            <details class="bxfb-item__tech">
                <summary>Ошибки на экране (${errors.length})</summary>
                <pre>${escapeHtml(errors.map((err) =>
                    `${err.status} ${err.path} — ${err.error} (${err.at})`).join('\n'))}</pre>
            </details>
        ` : '';

        return `
            <div class="bxfb-item" data-id="${item.id}">
                <div class="bxfb-item__head">
                    <span class="bxfb-badge bxfb-badge--${item.type}">
                        ${escapeHtml(TYPE_LABELS[item.type] || item.type)}
                    </span>
                    <span class="bxfb-badge bxfb-badge--${item.status}">
                        ${escapeHtml(STATUS_LABELS[item.status] || item.status)}
                    </span>
                    <span class="bxfb-item__meta">
                        #${item.id} · ${escapeHtml(moduleTitle(item.module))} ·
                        ${escapeHtml(item.author_username)} (${escapeHtml(item.author_role || '—')}) ·
                        ${escapeHtml(formatDate(item.created_at))}
                        ${item.people_count > 1 ? ` · просили ${item.people_count} чел.` : ''}
                        ${item.merged_count ? ` · подшито ${item.merged_count}` : ''}
                    </span>
                </div>
                <div class="bxfb-item__text">${escapeHtml(item.text)}</div>
                ${item.wish ? `<div class="bxfb-item__answer">Как хотелось бы: ${escapeHtml(item.wish)}</div>` : ''}
                ${item.resolution ? `<div class="bxfb-item__answer">Ответ: ${escapeHtml(item.resolution)}</div>` : ''}
                ${tech}
                <div class="bxfb-item__actions">
                    ${item.task_id
                        ? `<span class="bxfb-item__meta">В бэклоге, задача #${item.task_id}</span>`
                        : `<button type="button" class="bxfb-btn bxfb-btn--sm" data-action="task" data-id="${item.id}">В бэклог</button>`}
                    <button type="button" class="bxfb-btn bxfb-btn--ghost bxfb-btn--sm" data-action="prompt" data-id="${item.id}">Промпт</button>
                    <button type="button" class="bxfb-btn bxfb-btn--ghost bxfb-btn--sm" data-action="merge" data-id="${item.id}">Объединить</button>
                    <button type="button" class="bxfb-btn bxfb-btn--ghost bxfb-btn--sm" data-action="ask" data-id="${item.id}">Уточнить</button>
                    <button type="button" class="bxfb-btn bxfb-btn--ghost bxfb-btn--sm" data-action="done" data-id="${item.id}">Сделано</button>
                    <button type="button" class="bxfb-btn bxfb-btn--ghost bxfb-btn--sm" data-action="reject" data-id="${item.id}">Отклонить</button>
                </div>
            </div>
        `;
    }

    async function ownerAction(action, itemId, btn) {
        btn.disabled = true;
        try {
            if (action === 'task') await actionToTask(itemId);
            else if (action === 'prompt') await actionPrompt(itemId);
            else if (action === 'merge') await actionMerge(itemId);
            else if (action === 'ask') await actionStatusWithText(itemId, 'need_info',
                'Что уточнить у автора?', 'Вопрос автору');
            else if (action === 'reject') await actionStatusWithText(itemId, 'rejected',
                'Почему не будем делать? Текст увидит автор', 'Причина отказа');
            else if (action === 'done') await actionDone(itemId);
        } catch (e) {
            window.BarhatUI.toast(e.message, 'error');
        } finally {
            btn.disabled = false;
        }
    }

    async function actionToTask(itemId) {
        const item = state.ownerItems.find((one) => one.id === itemId);
        const title = await window.BarhatUI.prompt(
            'Название задачи в бэклоге:',
            (item ? item.text : '').slice(0, 80),
            { title: 'В бэклог', confirmText: 'Создать' }
        );
        if (title === null) return;

        const data = await api(`/api/feedback/${itemId}/to-task`, {
            method: 'POST',
            headers: AJAX_HEADERS,
            body: JSON.stringify({ title: title.trim() }),
        });
        window.BarhatUI.toast(`Задача #${data.task.id} создана — автор увидит «Взято в работу»`, 'success');
        await loadOwnerQueue();
    }

    async function actionMerge(itemId) {
        const target = await window.BarhatUI.prompt(
            'Номер обращения, к которому подшить (виден в карточке после #):',
            '',
            { title: 'Объединить дубли', confirmText: 'Подшить' }
        );
        if (target === null) return;

        const targetId = parseInt(String(target).replace('#', '').trim(), 10);
        if (!targetId) {
            window.BarhatUI.toast('Нужен номер обращения', 'error');
            return;
        }

        await api(`/api/feedback/${itemId}/merge`, {
            method: 'POST',
            headers: AJAX_HEADERS,
            body: JSON.stringify({ target_id: targetId }),
        });
        window.BarhatUI.toast('Дубль подшит, автор учтён как просивший', 'success');
        await loadOwnerQueue();
    }

    async function actionStatusWithText(itemId, status, question, title) {
        const text = await window.BarhatUI.prompt(question, '', { title, confirmText: 'Отправить' });
        if (text === null) return;
        if (!text.trim()) {
            window.BarhatUI.toast('Без текста автор решит, что его не услышали', 'error');
            return;
        }

        await api(`/api/feedback/${itemId}/status`, {
            method: 'POST',
            headers: AJAX_HEADERS,
            body: JSON.stringify({ status, resolution: text.trim() }),
        });
        await loadOwnerQueue();
    }

    async function actionDone(itemId) {
        const text = await window.BarhatUI.prompt(
            'Что сделано? Текст увидит автор (можно оставить пустым)', '',
            { title: 'Отметить сделанным', confirmText: 'Готово' }
        );
        if (text === null) return;

        await api(`/api/feedback/${itemId}/status`, {
            method: 'POST',
            headers: AJAX_HEADERS,
            body: JSON.stringify({ status: 'done', resolution: text.trim() || null }),
        });
        await loadOwnerQueue();
    }

    async function actionPrompt(itemId) {
        const data = await api(`/api/feedback/${itemId}/export`);
        const markdown = data.markdown || '';

        // navigator.clipboard в кросс-доменном iframe блокируется без
        // allow="clipboard-write" — молчаливо «ничего не произошло» здесь
        // недопустимо, поэтому всегда есть окно с текстом.
        let copied = false;
        try {
            if (navigator.clipboard && navigator.clipboard.writeText) {
                await navigator.clipboard.writeText(markdown);
                copied = true;
            }
        } catch (e) {
            copied = false;
        }

        if (copied) {
            window.BarhatUI.toast('Промпт скопирован в буфер обмена', 'success');
            return;
        }
        showCopyDialog(markdown);
    }

    function showCopyDialog(markdown) {
        const overlay = document.createElement('div');
        overlay.className = 'bxfb-ovl';
        overlay.innerHTML = `
            <div class="bxfb-modal" role="dialog" aria-modal="true">
                <div class="bxfb-head">
                    <div>
                        <h2 class="bxfb-title">Промпт для Claude Code</h2>
                        <p class="bxfb-sub">Скопируйте текст — буфер обмена браузер закрыл</p>
                    </div>
                </div>
                <textarea class="bxfb-input bxfb-copy" readonly></textarea>
                <div class="bxfb-actions">
                    <button type="button" class="bxfb-btn" data-action="close">Закрыть</button>
                </div>
            </div>
        `;
        const textarea = overlay.querySelector('textarea');
        textarea.value = markdown;

        overlay.addEventListener('mousedown', (e) => {
            if (e.target === overlay) { overlay.remove(); refreshButtonVisibility(); }
        });
        overlay.querySelector('[data-action="close"]').addEventListener('click', () => {
            overlay.remove();
            refreshButtonVisibility();
        });

        document.body.appendChild(overlay);
        textarea.focus();
        textarea.select();
        refreshButtonVisibility();
    }

    // === Старт ===

    function init() {
        injectStyles();
        createButton();
        watchOverlays();
    }

    document.addEventListener('userRoleChanged', function (e) {
        state.user = e.detail || null;
        state.isOwner = Boolean(state.user && state.user.role === 'admin');

        if (state.isOwner) mountOwnerSection();

        // Счётчики — единственный автоматический запрос модуля, и он уходит
        // после того, как страница отрисовалась: /data медленный, а бейдж
        // подождёт.
        setTimeout(loadCounters, 3000);
    });

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    window.BarhatFeedback = {
        open: openWidget,
        openFromError: function (message) {
            // Тип задаём сразу, а не после отрисовки: пилюли рисуются внутри
            // openWidget и правку постфактум уже не увидят.
            openWidget(message ? `Ошибка на экране: ${message}\n\nЧто делал(а): ` : '', 'bug');
        },
    };
})();
