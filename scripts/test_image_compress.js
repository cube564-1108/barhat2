/**
 * Прогон window.BarhatImage в Node с заглушками canvas/createImageBitmap.
 *
 * Проверяется логика, в которой и живут настоящие ошибки: пропорции при
 * уменьшении, запрет на растягивание маленьких картинок, смена расширения на
 * .jpg (иначе HEIC с айфона не примет сервер), EXIF-ориентация и — главное —
 * что при любом сбое возвращается ОРИГИНАЛ, а не пустота. Молчаливый возврат
 * пустоты превратился бы в успешно созданную заявку без фото, то есть ровно в
 * тот сбой, из-за которого всё и затевалось (обращение #7).
 *
 * Запуск: node scripts/test_image_compress.js
 */

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const SRC = path.join(__dirname, '..', 'src', 'dashboard', 'image-compress.js');

const failures = [];

function check(name, condition, detail) {
    const mark = condition ? 'OK  ' : 'FAIL';
    console.log(`  [${mark}] ${name}${detail ? ' — ' + detail : ''}`);
    if (!condition) failures.push(name);
}

// --- Заглушки браузера -------------------------------------------------------

class FakeBlob {
    constructor(parts, opts) {
        // .size тоже считаем: new File([blob], ...) заворачивает Blob в File,
        // и без этой ветки итоговый файл получался нулевого размера.
        this.size = (parts || []).reduce(
            (n, p) => n + (p.byteLength || p.length || p.size || 0), 0
        );
        this.type = (opts && opts.type) || '';
    }
}

class FakeFile extends FakeBlob {
    constructor(parts, name, opts) {
        super(parts, opts);
        this.name = name;
        this.lastModified = (opts && opts.lastModified) || 0;
    }
}

/** Сколько «весит» пожатый кадр: пропорционально площади, как у настоящего JPEG. */
function fakeEncodedSize(w, h) {
    return Math.max(1, Math.round(w * h * 0.08));
}

function makeSandbox(overrides) {
    const state = { drawn: null, canvases: [], bitmapOptions: null, closed: 0 };

    const sandbox = {
        console,
        setTimeout,
        Math,
        Date,
        Object,
        Array,
        Uint8Array,
        Error,
        String,
        Number,
        Promise,
        JSON,
        Blob: FakeBlob,
        File: FakeFile,
        URL: { createObjectURL: () => 'blob:fake', revokeObjectURL: () => {} },
        Image: function () {
            this.decode = () => Promise.resolve();
            this.naturalWidth = 800;
            this.naturalHeight = 600;
        },
        createImageBitmap: (file, opts) => {
            state.bitmapOptions = opts || null;
            return Promise.resolve({
                width: file.__w,
                height: file.__h,
                close() { state.closed += 1; },
            });
        },
        document: {
            createElement() {
                const canvas = {
                    width: 0,
                    height: 0,
                    getContext: () => ({
                        drawImage(src, x, y, w, h) { state.drawn = { w, h }; },
                    }),
                    toBlob(cb, type, quality) {
                        state.quality = quality;
                        state.type = type;
                        cb(new FakeBlob(
                            [new Uint8Array(fakeEncodedSize(canvas.width, canvas.height))],
                            { type }
                        ));
                    },
                };
                state.canvases.push(canvas);
                return canvas;
            },
        },
        window: {},
    };
    Object.assign(sandbox, overrides || {});
    sandbox.self = sandbox;

    vm.createContext(sandbox);
    vm.runInContext(fs.readFileSync(SRC, 'utf8'), sandbox, { filename: 'image-compress.js' });
    return { api: sandbox.window.BarhatImage, state, sandbox };
}

function photo(name, w, h, bytes, type) {
    const file = new FakeFile([new Uint8Array(bytes)], name, { type: type || 'image/jpeg' });
    file.__w = w;
    file.__h = h;
    return file;
}

// --- 1. Уменьшение большого снимка -------------------------------------------

(async () => {
    console.log('\n=== 1. Снимок с телефона ===');
    {
        const { api, state } = makeSandbox();
        const original = photo('IMG_0042.jpg', 4000, 3000, 4 * 1024 * 1024);
        const out = await api.compress(original);

        check('Длинная сторона ужата до 1600', state.drawn.w === 1600, `${state.drawn.w}x${state.drawn.h}`);
        check('Пропорции сохранены', state.drawn.h === 1200, `${state.drawn.h}`);
        check('Результат заметно легче оригинала',
            out.size < original.size / 5, `${original.size} -> ${out.size} байт`);
        check('Тип — JPEG', out.type === 'image/jpeg', out.type);
        check('Качество 0.8', state.quality === 0.8, String(state.quality));
        check('ImageBitmap освобождён', state.closed === 1, `close(): ${state.closed}`);
    }

    console.log('\n=== 2. EXIF-ориентация ===');
    {
        const { api, state } = makeSandbox();
        await api.compress(photo('portrait.jpg', 3000, 4000, 3 * 1024 * 1024));
        check('Запрошена ориентация из EXIF (иначе кадр ляжет набок)',
            state.bitmapOptions && state.bitmapOptions.imageOrientation === 'from-image',
            JSON.stringify(state.bitmapOptions));
        check('Вертикальный кадр остался вертикальным',
            state.drawn.w === 1200 && state.drawn.h === 1600, `${state.drawn.w}x${state.drawn.h}`);
    }

    console.log('\n=== 3. Маленькие картинки не растягиваются ===');
    {
        const { api, state } = makeSandbox();
        const small = photo('mini.jpg', 800, 600, 40 * 1024);
        const out = await api.compress(small);
        check('Размер в пикселях не увеличен', state.drawn.w === 800 && state.drawn.h === 600,
            `${state.drawn.w}x${state.drawn.h}`);
        check('Раз пережатие всё же помогло — отдан облегчённый файл',
            out !== small && out.size < small.size, `${small.size} -> ${out.size} байт`);
    }
    {
        // Скриншот-иконка: пережатие в JPEG сделает файл ТЯЖЕЛЕЕ оригинала.
        // Отдавать такое наверх незачем.
        const { api } = makeSandbox();
        const icon = photo('icon.png', 200, 200, 2 * 1024, 'image/png');
        const out = await api.compress(icon);
        check('Пережатие сделало бы тяжелее — отдан оригинал', out === icon,
            `${icon.size} байт`);
    }

    console.log('\n=== 4. HEIC с айфона ===');
    {
        const { api } = makeSandbox();
        const heic = photo('IMG_0042.HEIC', 4032, 3024, 2 * 1024 * 1024, 'image/heic');
        const out = await api.compress(heic);
        check('Расширение стало .jpg (сервер HEIC мог не принять)',
            out.name === 'IMG_0042.jpg', out.name);
        check('Содержимое — JPEG', out.type === 'image/jpeg', out.type);
    }

    console.log('\n=== 5. Сбои возвращают оригинал, а не пустоту ===');
    {
        const { api } = makeSandbox({
            createImageBitmap: () => Promise.reject(new Error('нет декодера')),
            Image: function () {
                this.decode = () => Promise.reject(new Error('и тут тоже'));
            },
        });
        const broken = photo('broken.jpg', 4000, 3000, 1024);
        const out = await api.compress(broken);
        check('Отказ декодера — возвращён оригинал', out === broken);
    }
    {
        const { api } = makeSandbox({
            document: { createElement: () => ({ getContext: () => null }) },
        });
        const f = photo('nocanvas.jpg', 4000, 3000, 1024);
        check('Нет 2d-контекста — возвращён оригинал', (await api.compress(f)) === f);
    }
    {
        const { api } = makeSandbox({
            document: {
                createElement: () => ({
                    getContext: () => ({ drawImage() {} }),
                    toBlob: (cb) => cb(null),
                }),
            },
        });
        const f = photo('noblob.jpg', 4000, 3000, 1024);
        check('Кодировщик вернул пустоту — возвращён оригинал', (await api.compress(f)) === f);
    }
    {
        const { api } = makeSandbox();
        const pdf = new FakeFile([new Uint8Array(500)], 'smeta.pdf', { type: 'application/pdf' });
        check('Не картинку не трогаем', (await api.compress(pdf)) === pdf);
        check('null проходит насквозь', (await api.compress(null)) === null);
    }

    console.log('\n=== 6. Пачка файлов ===');
    {
        const { api } = makeSandbox();
        const files = [
            photo('a.jpg', 4000, 3000, 4 * 1024 * 1024),
            photo('b.jpg', 3000, 2000, 3 * 1024 * 1024),
        ];
        const seen = [];
        const out = await api.compressAll(files, undefined, (i, total) => seen.push(`${i + 1}/${total}`));
        check('Обработаны все', out.length === 2);
        check('Прогресс сообщён по каждому', seen.join(' ') === '1/2 2/2', seen.join(' '));
        check('Все результаты — JPEG', out.every(f => f.type === 'image/jpeg'));
    }

    console.log('\n=== 7. Подпись размера ===');
    {
        const { api } = makeSandbox();
        check('Килобайты', api.formatBytes(350 * 1024) === '350 КБ', api.formatBytes(350 * 1024));
        check('Мегабайты с запятой', api.formatBytes(3.8 * 1024 * 1024) === '3,8 МБ',
            api.formatBytes(3.8 * 1024 * 1024));
    }

    console.log('\n' + '='.repeat(60));
    if (failures.length) {
        console.log(`ПРОВАЛЕНО проверок: ${failures.length}`);
        failures.forEach(n => console.log(`  - ${n}`));
        process.exit(1);
    }
    console.log('Все проверки прошли.');
})();
