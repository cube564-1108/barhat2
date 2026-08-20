/**
 * Диалоги и тосты дашборда — замена нативным alert/confirm/prompt.
 *
 * ЗАЧЕМ: дашборд встраивается в портал БАРХАТ Пульс через <iframe> с другого
 * домена (см. PULSE_ORIGIN в src/pyrus/server.py). Chrome с 92-й версии и
 * Safari МОЛЧА игнорируют alert/confirm/prompt, вызванные из кросс-доменного
 * iframe: confirm() мгновенно возвращает false, alert() не показывает ничего.
 * Из-за этого в Пульсе кнопки, начинающиеся с confirm(), не делали вообще
 * ничего, а ошибки от сервера были не видны (инцидент со списаниями,
 * согласование не работало у управляющей).
 *
 * Диалоги здесь — обычный DOM, он работает в iframe как везде.
 *
 * API (всё асинхронное — возвращает Promise):
 *   await BarhatUI.confirm('Точно?')            -> true | false
 *   await BarhatUI.prompt('Причина:', 'дефолт') -> строка | null (отмена)
 *   BarhatUI.alert('Готово')                    -> тост
 *   BarhatUI.toast('Сохранено', 'success')      -> тост
 *
 * window.alert подменяется на тост автоматически — все существующие вызовы
 * alert() по всему дашборду начинают работать в Пульсе без правок.
 * window.confirm/prompt НЕ подменяются (они синхронные, а честной замены
 * синхронному диалогу в браузере нет) — их вызовы переписаны на await
 * BarhatUI.confirm/prompt. Обёртка ниже только громко пишет в консоль, если
 * где-то остался пропущенный нативный вызов.
 *
 * Стили по DESIGN-SPEC.md: токены --bx-*, заголовки Vollkorn, без эмодзи.
 * Фоллбэк на --barkhat-* из brand/tokens.css — старый дашборд живёт на них.
 */

(function () {
    'use strict';

    const Z_DIALOG = 100000;
    const Z_TOAST = 100010;

    const STYLE_ID = 'bx-dialog-styles';

    function injectStyles() {
        if (document.getElementById(STYLE_ID)) return;
        const style = document.createElement('style');
        style.id = STYLE_ID;
        style.textContent = `
.bx-dialog-backdrop {
    position: fixed; inset: 0; z-index: ${Z_DIALOG};
    display: flex; align-items: center; justify-content: center;
    padding: 20px;
    background: rgba(19, 8, 16, 0.55);
    animation: bx-dialog-fade 0.15s ease;
}
.bx-dialog {
    width: 100%; max-width: 440px;
    background: var(--bx-white, var(--barkhat-white, #fff));
    border-radius: var(--bx-r-2xl, 16px);
    box-shadow: 0 18px 48px rgba(19, 8, 16, 0.28);
    padding: 24px;
    font-family: var(--bx-font-body, 'Inter', 'PT Sans', system-ui, sans-serif);
    animation: bx-dialog-rise 0.18s ease;
}
@keyframes bx-dialog-fade { from { opacity: 0; } to { opacity: 1; } }
@keyframes bx-dialog-rise {
    from { opacity: 0; transform: translateY(10px); }
    to   { opacity: 1; transform: translateY(0); }
}
.bx-dialog-title {
    margin: 0 0 10px;
    font-family: var(--bx-font-head, 'Vollkorn', Georgia, serif);
    font-size: 20px; font-weight: 600; line-height: 1.25;
    color: var(--bx-wine, var(--barkhat-wine, #411330));
}
.bx-dialog-message {
    margin: 0; font-size: 14px; line-height: 1.5;
    color: var(--bx-text, var(--barkhat-gray-dark, #3C3C3C));
    white-space: pre-line; word-break: break-word;
    max-height: 45vh; overflow-y: auto;
}
.bx-dialog-input {
    width: 100%; margin-top: 14px; padding: 10px 12px;
    font: inherit; font-size: 14px;
    color: var(--bx-text, var(--barkhat-gray-dark, #3C3C3C));
    background: var(--bx-white, #fff);
    border: 1px solid var(--bx-border, #eee2ea);
    border-radius: var(--bx-r-lg, 8px);
    box-sizing: border-box;
}
.bx-dialog-input:focus {
    outline: none;
    border-color: var(--bx-pink-deep, var(--barkhat-pink-deep, #B26FA1));
    box-shadow: 0 0 0 3px var(--bx-pink-wash, #F3E3EE);
}
.bx-dialog-actions {
    display: flex; justify-content: flex-end; gap: 10px; margin-top: 22px;
}
.bx-dialog-btn {
    padding: 9px 18px; font: inherit; font-size: 14px; font-weight: 500;
    border: 1px solid transparent; border-radius: var(--bx-r-lg, 8px);
    cursor: pointer; transition: filter 0.15s ease, background 0.15s ease;
}
.bx-dialog-btn:focus-visible {
    outline: 2px solid var(--bx-pink-deep, #B26FA1); outline-offset: 2px;
}
.bx-dialog-btn-cancel {
    background: var(--bx-bg-2, var(--barkhat-gray-light, #f5e8f3));
    color: var(--bx-text, #3C3C3C);
    border-color: var(--bx-border, #eee2ea);
}
.bx-dialog-btn-cancel:hover { filter: brightness(0.97); }
.bx-dialog-btn-confirm {
    background: var(--bx-wine, var(--barkhat-wine, #411330));
    color: #fff;
}
.bx-dialog-btn-confirm:hover { filter: brightness(1.15); }
.bx-dialog-btn-danger { background: var(--bx-down, #c0322f); color: #fff; }
.bx-dialog-btn-danger:hover { filter: brightness(1.1); }

.bx-toast-stack {
    position: fixed; right: 20px; bottom: 20px; z-index: ${Z_TOAST};
    display: flex; flex-direction: column; align-items: flex-end; gap: 10px;
    pointer-events: none;
}
.bx-toast {
    max-width: min(420px, calc(100vw - 40px));
    padding: 12px 16px;
    font-family: var(--bx-font-body, 'Inter', 'PT Sans', system-ui, sans-serif);
    font-size: 14px; line-height: 1.45; color: #fff;
    background: var(--bx-wine, var(--barkhat-wine, #411330));
    border-radius: var(--bx-r-xl, 12px);
    box-shadow: 0 10px 28px rgba(19, 8, 16, 0.28);
    white-space: pre-line; word-break: break-word;
    pointer-events: auto; cursor: pointer;
    animation: bx-toast-in 0.2s ease;
    transition: opacity 0.25s ease, transform 0.25s ease;
}
.bx-toast-success { background: var(--bx-up, var(--barkhat-success, #0a7d3f)); }
.bx-toast-error   { background: var(--bx-down, #c0322f); }
.bx-toast-hiding  { opacity: 0; transform: translateY(12px); }
@keyframes bx-toast-in {
    from { opacity: 0; transform: translateY(12px); }
    to   { opacity: 1; transform: translateY(0); }
}
@media (max-width: 640px) {
    .bx-toast-stack { left: 16px; right: 16px; align-items: stretch; }
    .bx-toast { max-width: none; }
}
`;
        document.head.appendChild(style);
    }

    // === Тосты ===

    let toastStack = null;

    function getToastStack() {
        if (toastStack && toastStack.isConnected) return toastStack;
        toastStack = document.createElement('div');
        toastStack.className = 'bx-toast-stack';
        document.body.appendChild(toastStack);
        return toastStack;
    }

    // Тексты ошибок по всему дашборду не размечены типом — определяем по
    // содержанию, чтобы подменённый alert() красил ошибки красным, а не
    // выдавал всё нейтральным винным.
    const ERROR_RE = /ошибк|не удал|неверн|некорректн|нельзя|запрещ|недостаточно|failed|error/i;

    function toast(message, type, timeoutMs) {
        injectStyles();
        const text = String(message ?? '');
        if (!text.trim()) return;

        const resolvedType = type || (ERROR_RE.test(text) ? 'error' : 'info');
        // Длинный текст (например traceback от МойСклад) нужно успеть прочитать.
        const lifetime = timeoutMs || Math.min(12000, 3500 + text.length * 35);

        const el = document.createElement('div');
        el.className = 'bx-toast' + (resolvedType !== 'info' ? ` bx-toast-${resolvedType}` : '');
        el.textContent = text;
        el.setAttribute('role', resolvedType === 'error' ? 'alert' : 'status');
        el.title = 'Нажмите, чтобы скрыть';

        let closed = false;
        function close() {
            if (closed) return;
            closed = true;
            el.classList.add('bx-toast-hiding');
            setTimeout(() => el.remove(), 260);
        }
        el.addEventListener('click', close);
        setTimeout(close, lifetime);

        getToastStack().appendChild(el);
    }

    // === Модальный диалог ===

    let openDialog = null;

    /**
     * @param {Object} cfg
     * @param {string} cfg.message   текст (подставляется через textContent)
     * @param {string} [cfg.title]
     * @param {string} [cfg.confirmText]
     * @param {string} [cfg.cancelText]  если null — кнопки «Отмена» нет
     * @param {boolean} [cfg.danger]     красная кнопка подтверждения
     * @param {boolean} [cfg.withInput]  режим prompt
     * @param {string} [cfg.defaultValue]
     * @param {string} [cfg.placeholder]
     * @returns {Promise<*>} confirm: true/false; prompt: строка/null
     */
    function showDialog(cfg) {
        injectStyles();

        // Второй диалог поверх первого не открываем — закрываем предыдущий
        // отменой, иначе Esc/фокус начинают работать непредсказуемо.
        if (openDialog) openDialog.cancel();

        return new Promise((resolve) => {
            const backdrop = document.createElement('div');
            backdrop.className = 'bx-dialog-backdrop';

            const dialog = document.createElement('div');
            dialog.className = 'bx-dialog';
            dialog.setAttribute('role', 'dialog');
            dialog.setAttribute('aria-modal', 'true');

            if (cfg.title) {
                const h = document.createElement('h2');
                h.className = 'bx-dialog-title';
                h.textContent = cfg.title;
                dialog.appendChild(h);
            }

            const p = document.createElement('p');
            p.className = 'bx-dialog-message';
            p.textContent = String(cfg.message ?? '');
            dialog.appendChild(p);

            let input = null;
            if (cfg.withInput) {
                input = document.createElement('input');
                input.type = 'text';
                input.className = 'bx-dialog-input';
                input.value = cfg.defaultValue != null ? String(cfg.defaultValue) : '';
                if (cfg.placeholder) input.placeholder = cfg.placeholder;
                dialog.appendChild(input);
            }

            const actions = document.createElement('div');
            actions.className = 'bx-dialog-actions';

            let cancelBtn = null;
            if (cfg.cancelText !== null) {
                cancelBtn = document.createElement('button');
                cancelBtn.type = 'button';
                cancelBtn.className = 'bx-dialog-btn bx-dialog-btn-cancel';
                cancelBtn.textContent = cfg.cancelText || 'Отмена';
                actions.appendChild(cancelBtn);
            }

            const confirmBtn = document.createElement('button');
            confirmBtn.type = 'button';
            confirmBtn.className = 'bx-dialog-btn ' +
                (cfg.danger ? 'bx-dialog-btn-danger' : 'bx-dialog-btn-confirm');
            confirmBtn.textContent = cfg.confirmText || 'Подтвердить';
            actions.appendChild(confirmBtn);

            dialog.appendChild(actions);
            backdrop.appendChild(dialog);

            const previouslyFocused = document.activeElement;
            let settled = false;

            function finish(value) {
                if (settled) return;
                settled = true;
                openDialog = null;
                document.removeEventListener('keydown', onKeydown, true);
                backdrop.remove();
                if (previouslyFocused && previouslyFocused.focus) {
                    try { previouslyFocused.focus(); } catch (e) { /* элемент мог исчезнуть */ }
                }
                resolve(value);
            }

            const cancelValue = cfg.withInput ? null : false;
            function cancel() { finish(cancelValue); }
            function accept() { finish(cfg.withInput ? input.value : true); }

            function onKeydown(e) {
                if (e.key === 'Escape') { e.preventDefault(); cancel(); return; }
                if (e.key === 'Enter' && (cfg.withInput || document.activeElement !== cancelBtn)) {
                    e.preventDefault();
                    accept();
                }
            }

            if (cancelBtn) cancelBtn.addEventListener('click', cancel);
            confirmBtn.addEventListener('click', accept);
            backdrop.addEventListener('mousedown', (e) => {
                if (e.target === backdrop) cancel();
            });
            document.addEventListener('keydown', onKeydown, true);

            openDialog = { cancel };
            document.body.appendChild(backdrop);
            (input || confirmBtn).focus();
            if (input) input.select();
        });
    }

    function confirmDialog(message, opts) {
        opts = opts || {};
        return showDialog({
            message,
            title: opts.title || 'Подтвердите действие',
            confirmText: opts.confirmText || 'Подтвердить',
            cancelText: opts.cancelText || 'Отмена',
            danger: Boolean(opts.danger),
        });
    }

    function promptDialog(message, defaultValue, opts) {
        opts = opts || {};
        return showDialog({
            message,
            title: opts.title || 'Введите значение',
            confirmText: opts.confirmText || 'ОК',
            cancelText: opts.cancelText || 'Отмена',
            withInput: true,
            defaultValue,
            placeholder: opts.placeholder,
        });
    }

    // === Подмена нативных диалогов ===

    const nativeAlert = window.alert.bind(window);
    const nativeConfirm = window.confirm.bind(window);
    const nativePrompt = window.prompt.bind(window);

    const inIframe = (function () {
        try { return window.self !== window.top; } catch (e) { return true; }
    })();

    // alert -> тост. Сигнатура совпадает (возвращает undefined), поэтому
    // подмена безопасна для всех существующих вызовов по дашборду.
    window.alert = function (message) { toast(message); };

    // confirm/prompt синхронные — честной DOM-замены им нет. Оставляем
    // нативные, но громко сигналим о пропущенном вызове: внутри Пульса он
    // вернёт false/null и кнопка «ничего не сделает», как было со списаниями.
    window.confirm = function (message) {
        if (inIframe) {
            console.error(
                '[BarhatUI] Нативный confirm() внутри iframe — браузер его проигнорирует. ' +
                'Замените на «await BarhatUI.confirm(...)». Текст:', message
            );
        }
        return nativeConfirm(message);
    };
    window.prompt = function (message, defaultValue) {
        if (inIframe) {
            console.error(
                '[BarhatUI] Нативный prompt() внутри iframe — браузер его проигнорирует. ' +
                'Замените на «await BarhatUI.prompt(...)». Текст:', message
            );
        }
        return nativePrompt(message, defaultValue);
    };

    window.BarhatUI = {
        alert: toast,
        toast,
        confirm: confirmDialog,
        prompt: promptDialog,
        inIframe,
        nativeAlert,
    };
})();
