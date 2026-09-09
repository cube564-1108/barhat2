/*
 * Service worker приложения курьера.
 *
 * Главное правило (находка К5 из критики плана): статика дашборда отдаётся с
 * no-cache именно потому, что деплой должен доезжать до людей сразу. Service
 * worker, закэшировавший HTML, сводит это на нет — курьер после выкатки видит
 * вчерашний экран и не понимает, почему кнопка «не работает». Поэтому:
 *
 *   - кэшируется ТОЛЬКО оболочка (css/js/иконка), под ключом с версией сборки;
 *   - версию подставляет сервер (см. serve_courier_sw в pyrus/server.py) из
 *     отпечатка самих файлов — бампать её руками никто не обязан помнить;
 *   - HTML — network-first, из кэша берётся только когда сети нет вовсе;
 *   - /api/* не кэшируется НИКОГДА: это чужие персональные данные, и отдать
 *     их из кэша другому вошедшему нельзя;
 *   - новая версия ставится немедленно (skipWaiting + clients.claim), а
 *     страница сама перезагружается по controllerchange.
 */

const CACHE_VERSION = '__CACHE_VERSION__';
const SHELL_CACHE = 'courier-shell-' + CACHE_VERSION;

// Оболочка: то, без чего экран не нарисуется. Данные сюда не попадают.
const SHELL_URLS = [
    '/app/courier-app.css',
    '/app/courier-app.js',
    '/app/courier-icon.svg',
    '/ui-dialog.js',
    '/datetime.js'
];

self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open(SHELL_CACHE)
            // addAll падает целиком, если хоть один файл не отдался, и тогда
            // SW не установится вовсе. Оболочка не настолько важна.
            .then((cache) => Promise.all(SHELL_URLS.map(
                (url) => cache.add(url).catch(() => null)
            )))
            .then(() => self.skipWaiting())
    );
});

self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys()
            .then((names) => Promise.all(names
                .filter((name) => name.startsWith('courier-shell-') && name !== SHELL_CACHE)
                .map((name) => caches.delete(name))))
            .then(() => self.clients.claim())
    );
});

self.addEventListener('message', (event) => {
    if (event.data === 'skip-waiting') self.skipWaiting();
});

self.addEventListener('fetch', (event) => {
    const request = event.request;
    if (request.method !== 'GET') return;

    const url = new URL(request.url);
    if (url.origin !== self.location.origin) return;

    // Персональные данные мимо кэша: заказы, телефоны, профиль.
    if (url.pathname.startsWith('/api/')) return;

    // Навигация: сначала сеть, кэш — только как аварийная подстилка, чтобы
    // офлайн курьер увидел экран с плашкой «данные на HH:MM», а не ошибку.
    if (request.mode === 'navigate') {
        event.respondWith(
            fetch(request)
                .then((response) => {
                    const copy = response.clone();
                    caches.open(SHELL_CACHE).then((cache) => cache.put(request, copy));
                    return response;
                })
                .catch(() => caches.match(request)
                    .then((cached) => cached || caches.match('/app/courier')))
        );
        return;
    }

    if (SHELL_URLS.indexOf(url.pathname) === -1) return;

    // Оболочка: из кэша сразу (ключ уже содержит версию сборки, протухнуть
    // содержимое не может), с дозагрузкой в фоне на случай пустого кэша.
    event.respondWith(
        caches.match(request).then((cached) => cached || fetch(request).then((response) => {
            const copy = response.clone();
            caches.open(SHELL_CACHE).then((cache) => cache.put(request, copy));
            return response;
        }))
    );
});
