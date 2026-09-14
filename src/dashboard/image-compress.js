/**
 * window.BarhatImage — сжатие фото перед отправкой на сервер.
 *
 * Зачем. Снимок с телефона — это 3-5 МБ и 4000x3000 пикселей. Для подтверждения
 * списания или дефекта столько не нужно: 1600 px по длинной стороне и JPEG q0.8
 * дают 200-400 КБ, то есть в 10-20 раз меньше. Обращение #7 (модуль списаний,
 * 2026-09-06): «платформа тупит, грузит очень медленно, не всегда фото
 * подгружается с первого раза» — при том, что один и тот же кадр уезжал наверх
 * по числу позиций в заявке.
 *
 * Побочно решается вопрос форматов: iPhone по умолчанию снимает в HEIC, который
 * наш сервер не принимал вовсе. Что бы ни выбрал пользователь, canvas отдаёт
 * JPEG, и до сервера доезжает .jpg.
 *
 * Модуль общий и ничего не знает о конкретном разделе — его же ждут «Обратная
 * связь» (фото к обращению) и «Курьеры» (фото доставки).
 *
 * Использование:
 *     const small = await window.BarhatImage.compress(file);
 *     formData.append('file', small);
 */
(function () {
    'use strict';

    const DEFAULTS = {
        maxSide: 1600,   // px по длинной стороне
        quality: 0.8,    // качество JPEG
    };

    /** Читаемый размер для подписей в интерфейсе: «3,8 МБ». */
    function formatBytes(bytes) {
        if (!bytes) return '0 КБ';
        if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} КБ`;
        return `${(bytes / (1024 * 1024)).toFixed(1).replace('.', ',')} МБ`;
    }

    /**
     * Загрузить файл в объект, который умеет рисоваться на canvas.
     *
     * imageOrientation: 'from-image' обязателен. Без него снимок, сделанный
     * вертикально, ложится набок: телефон пишет кадр как есть и добавляет в EXIF
     * пометку «повернуть», а canvas про EXIF не знает. На экране в <img> браузер
     * поворот применяет, поэтому баг не виден до тех пор, пока фото не проходит
     * через canvas — и вылезает уже на сервере.
     */
    async function loadBitmap(file) {
        if (typeof createImageBitmap === 'function') {
            try {
                return await createImageBitmap(file, { imageOrientation: 'from-image' });
            } catch (e) {
                // Опция imageOrientation поддерживается не везде — пробуем без неё,
                // и только потом уходим на <img>.
                try {
                    return await createImageBitmap(file);
                } catch (e2) {
                    /* пусто: остаётся фолбэк ниже */
                }
            }
        }

        // Фолбэк через <img>: браузер сам применит EXIF-поворот при декодировании.
        const url = URL.createObjectURL(file);
        try {
            const img = new Image();
            img.src = url;
            if (img.decode) {
                await img.decode();
            } else {
                await new Promise((resolve, reject) => {
                    img.onload = resolve;
                    img.onerror = () => reject(new Error('Не удалось прочитать изображение'));
                });
            }
            return img;
        } finally {
            // Отзываем не сразу: Safari теряет уже декодированную картинку, если
            // отозвать URL в том же такте.
            setTimeout(() => URL.revokeObjectURL(url), 0);
        }
    }

    function targetSize(width, height, maxSide) {
        const longest = Math.max(width, height);
        // Маленькие картинки не растягиваем: увеличение не добавит информации,
        // а вес вырастет.
        if (!longest || longest <= maxSide) return { width, height };
        const ratio = maxSide / longest;
        return {
            width: Math.max(1, Math.round(width * ratio)),
            height: Math.max(1, Math.round(height * ratio)),
        };
    }

    function canvasToBlob(canvas, quality) {
        return new Promise((resolve) => {
            if (canvas.toBlob) {
                canvas.toBlob((blob) => resolve(blob), 'image/jpeg', quality);
                return;
            }
            try {
                const dataUrl = canvas.toDataURL('image/jpeg', quality);
                const binary = atob(dataUrl.split(',')[1]);
                const bytes = new Uint8Array(binary.length);
                for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
                resolve(new Blob([bytes], { type: 'image/jpeg' }));
            } catch (e) {
                resolve(null);
            }
        });
    }

    /** «IMG_0042.HEIC» -> «IMG_0042.jpg» */
    function jpegName(filename) {
        const base = String(filename || 'photo').replace(/\.[^.]+$/, '');
        return `${base || 'photo'}.jpg`;
    }

    /**
     * Сжать изображение. Возвращает File — всегда, даже когда сжать не вышло.
     *
     * Молча вернуть пустоту нельзя: вызывающий код грузит результат на сервер, и
     * «ничего» превратилось бы в успешно созданную заявку без фото — ровно тот
     * сбой, из-за которого всё и затевалось. Не смогли пожать — отдаём оригинал,
     * пусть решает сервер.
     *
     * @param {File} file
     * @param {{maxSide?: number, quality?: number}} [options]
     * @returns {Promise<File>}
     */
    async function compress(file, options) {
        const opts = Object.assign({}, DEFAULTS, options || {});
        if (!file) return file;
        if (!/^image\//i.test(file.type || '') && !/\.(jpe?g|png|webp|heic|heif)$/i.test(file.name || '')) {
            return file;  // не картинка — не наше дело
        }

        try {
            const source = await loadBitmap(file);
            const width = source.width || source.naturalWidth;
            const height = source.height || source.naturalHeight;
            if (!width || !height) return file;

            const size = targetSize(width, height, opts.maxSide);
            const canvas = document.createElement('canvas');
            canvas.width = size.width;
            canvas.height = size.height;

            const ctx = canvas.getContext('2d');
            if (!ctx) return file;
            ctx.drawImage(source, 0, 0, size.width, size.height);
            if (source.close) source.close();  // ImageBitmap держит память до сборки

            const blob = await canvasToBlob(canvas, opts.quality);
            if (!blob || !blob.size) return file;

            // Пережатый PNG-скриншот иногда весит больше оригинала — тогда
            // сжимать было незачем.
            if (blob.size >= file.size && size.width === width && size.height === height) {
                return file;
            }

            return new File([blob], jpegName(file.name), {
                type: 'image/jpeg',
                lastModified: Date.now(),
            });
        } catch (e) {
            console.error('[BarhatImage] Не удалось сжать фото, отправляем оригинал:', e);
            return file;
        }
    }

    /**
     * Сжать несколько файлов подряд, с колбэком прогресса.
     * Последовательно, а не Promise.all: параллельное декодирование нескольких
     * снимков по 12 Мпикс укладывает слабый телефон.
     */
    async function compressAll(files, options, onProgress) {
        const list = Array.from(files || []);
        const out = [];
        for (let i = 0; i < list.length; i++) {
            if (typeof onProgress === 'function') onProgress(i, list.length, list[i]);
            out.push(await compress(list[i], options));
        }
        return out;
    }

    window.BarhatImage = {
        compress,
        compressAll,
        formatBytes,
        DEFAULTS,
    };
})();
