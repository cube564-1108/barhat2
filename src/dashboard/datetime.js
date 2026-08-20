/**
 * Единое форматирование дат и времени в дашборде Бархат.
 *
 * ЗАЧЕМ: бэкенд хранит все метки времени в UTC (`datetime('now')` в SQLite и
 * `datetime.utcnow()` в Python — оба UTC), но отдаёт их строкой без указания
 * зоны: "2026-08-20 14:30:00" или "2026-08-20T14:30:00". Такую строку
 * `new Date()` трактует как местное время, а `.slice(0, 16)` вообще показывает
 * сырой UTC. В обоих случаях сотрудник видел время по Гринвичу — для салонов
 * в Новосибирске это минус 7 часов, для Екатеринбурга минус 5.
 *
 * Здесь строка без зоны явно достраивается до UTC и рендерится в часовом поясе
 * устройства смотрящего. Салонам с их поясами это даст верное время само собой:
 * флорист сидит в том же городе, где стоит касса.
 *
 * ВАЖНО: для дат без времени (due_date у счёта, дата отчёта) конвертация
 * запрещена — «10 августа» это не момент, а день; для них formatPlainDate().
 */

(function () {
    'use strict';

    // "2026-08-20 14:30:00", "2026-08-20T14:30", "2026-08-20T14:30:00.123456"
    const NAIVE_DATETIME_RE =
        /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?$/;

    // "2026-08-20" — календарная дата, момента времени в ней нет
    const PLAIN_DATE_RE = /^(\d{4})-(\d{2})-(\d{2})$/;

    /**
     * Разбирает метку времени с бэкенда в Date.
     * Строку без часового пояса считает UTC — так её и пишет сервер.
     * @param {string|number|Date} value
     * @returns {Date|null} null, если значение пустое или неразбираемое
     */
    function parse(value) {
        if (value === null || value === undefined || value === '') return null;
        if (value instanceof Date) return isNaN(value.getTime()) ? null : value;
        if (typeof value === 'number') {
            const fromNumber = new Date(value);
            return isNaN(fromNumber.getTime()) ? null : fromNumber;
        }

        const str = String(value).trim();
        if (!str) return null;

        const naive = NAIVE_DATETIME_RE.exec(str);
        if (naive) {
            const [, y, mo, d, h, mi, s] = naive;
            return new Date(Date.UTC(
                Number(y), Number(mo) - 1, Number(d),
                Number(h), Number(mi), Number(s || 0)
            ));
        }

        // Дата без времени: разворачиваем в полночь по местному времени, чтобы
        // formatDate() не увёл её на сутки назад в поясах западнее UTC.
        const plain = PLAIN_DATE_RE.exec(str);
        if (plain) {
            const [, y, mo, d] = plain;
            return new Date(Number(y), Number(mo) - 1, Number(d));
        }

        // Остальное (ISO с Z или явным смещением) отдаём штатному парсеру
        const parsed = new Date(str);
        return isNaN(parsed.getTime()) ? null : parsed;
    }

    function format(value, options, empty) {
        const date = parse(value);
        if (!date) return empty === undefined ? '—' : empty;
        return date.toLocaleString('ru-RU', options);
    }

    /** 20.08.2026 */
    function formatDate(value, empty) {
        return format(value, { day: '2-digit', month: '2-digit', year: 'numeric' }, empty);
    }

    /** 14:30 */
    function formatTime(value, empty) {
        return format(value, { hour: '2-digit', minute: '2-digit' }, empty);
    }

    /** 20.08.26, 14:30 — для плотных таблиц */
    function formatDateTime(value, empty) {
        return format(value, {
            day: '2-digit', month: '2-digit', year: '2-digit',
            hour: '2-digit', minute: '2-digit',
        }, empty);
    }

    /** 20.08.2026, 14:30 — для карточек и деталей */
    function formatDateTimeLong(value, empty) {
        return format(value, {
            day: '2-digit', month: '2-digit', year: 'numeric',
            hour: '2-digit', minute: '2-digit',
        }, empty);
    }

    /**
     * Календарная дата без времени (срок оплаты и т.п.) — печатается как есть,
     * без перевода в другой пояс.
     */
    function formatPlainDate(value, empty) {
        if (!value) return empty === undefined ? '—' : empty;
        const plain = PLAIN_DATE_RE.exec(String(value).trim());
        if (plain) {
            const [, y, mo, d] = plain;
            return `${d}.${mo}.${y}`;
        }
        return formatDate(value, empty);
    }

    /**
     * Подпись часового пояса устройства: "UTC+7".
     * Нужна в шапке, чтобы сотрудник понимал, в каком времени видит цифры,
     * и чтобы расхождение с другим городом сразу бросалось в глаза.
     */
    function zoneLabel() {
        // getTimezoneOffset даёт минуты, которые надо прибавить к местному
        // времени для получения UTC — знак обратный привычному
        const minutes = -new Date().getTimezoneOffset();
        const sign = minutes < 0 ? '-' : '+';
        const abs = Math.abs(minutes);
        const hours = Math.floor(abs / 60);
        const rest = abs % 60;
        return `UTC${sign}${hours}` + (rest ? `:${String(rest).padStart(2, '0')}` : '');
    }

    /** Date -> "YYYY-MM-DD HH:MM:SS" в UTC — формат, в котором время лежит в БД. */
    function toUtcSqlString(date) {
        return date.toISOString().slice(0, 19).replace('T', ' ');
    }

    /**
     * Границы выбранного дня в UTC — для фильтров по дате.
     *
     * Сотрудник в поле <input type="date"> имеет в виду свой день: «покажи
     * смены за 20 августа» это 20 августа по часам его салона. В базе время
     * лежит в UTC, поэтому сравнивать надо не с "2026-08-20 00:00:00", а с
     * моментом местной полуночи — для Новосибирска это 19 августа 17:00 UTC.
     * Иначе в выборку попадает хвост соседних суток, а утренние смены выпадают.
     *
     * @param {string} dateStr значение <input type="date">, "YYYY-MM-DD"
     * @returns {string|null} метка для параметра date_from / created_from
     */
    function dayStartUtc(dateStr) {
        const raw = String(dateStr || '').trim();
        const plain = PLAIN_DATE_RE.exec(raw);
        // Неожиданный формат отдаём как есть: бэкенд умеет и голую дату, это
        // безопаснее, чем послать в фильтр null
        if (!plain) return raw;
        const [, y, mo, d] = plain;
        return toUtcSqlString(new Date(Number(y), Number(mo) - 1, Number(d), 0, 0, 0));
    }

    /** Конец выбранного местного дня в UTC — пара к dayStartUtc(). */
    function dayEndUtc(dateStr) {
        const raw = String(dateStr || '').trim();
        const plain = PLAIN_DATE_RE.exec(raw);
        if (!plain) return raw;
        const [, y, mo, d] = plain;
        return toUtcSqlString(new Date(Number(y), Number(mo) - 1, Number(d), 23, 59, 59));
    }

    /** Текущая дата в формате YYYY-MM-DD по местному поясу (для <input type="date">). */
    function todayInputValue() {
        const now = new Date();
        const mo = String(now.getMonth() + 1).padStart(2, '0');
        const d = String(now.getDate()).padStart(2, '0');
        return `${now.getFullYear()}-${mo}-${d}`;
    }

    window.BarhatTime = {
        parse,
        formatDate,
        formatTime,
        formatDateTime,
        formatDateTimeLong,
        formatPlainDate,
        zoneLabel,
        dayStartUtc,
        dayEndUtc,
        todayInputValue,
    };
})();
