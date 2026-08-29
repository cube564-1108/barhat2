/**
 * JavaScript для главного дашборда Бархат
 * Навигация между страницами
 */

document.addEventListener('DOMContentLoaded', async function() {

    // Embed-режим: страница открыта внутри iframe портала БАРХАТ Пульс.
    // Класс на <html> проставляет сервер (_serve_dashboard_shell в
    // src/pyrus/server.py) — до первой отрисовки, чтобы сайдбар не мигал.
    const isEmbed = document.documentElement.classList.contains('embed-mode');

    // === Проверка авторизации ===

    async function checkAuth() {
        try {
            const response = await fetch('/api/auth/me', {
                credentials: 'include'  // Важно для cookie сессии
            });

            if (!response.ok) {
                // Не авторизован — редирект на логин
                window.location.href = '/login';
                return false;
            }

            const userData = await response.json();
            console.log('Авторизован как:', userData.username, 'Роль:', userData.role);
            return userData;
        } catch (error) {
            console.error('Ошибка проверки авторизации:', error);
            // При ошибке тоже редиректим на логин
            window.location.href = '/login';
            return false;
        }
    }

    // Проверяем авторизацию при загрузке
    const currentUser = await checkAuth();
    if (!currentUser) return;  // Редирект уже произошёл

    // Отображаем информацию о пользователе
    document.getElementById('userName').textContent = currentUser.full_name || currentUser.username;
    const roleNames = {
        'admin': 'Администратор',
        'manager': 'Менеджер',
        'florist_analyst': 'Аналитик качества'
    };
    // Пояс подписываем явно: время в модулях показывается по часам устройства,
    // и сотруднику из другого города должно быть видно, в каком времени цифры.
    // Подпись косметическая, а этот файл держит навигацию всего дашборда —
    // если datetime.js почему-то не доехал, показываем роль без пояса.
    const roleLabel = roleNames[currentUser.role] || currentUser.role;
    const zoneSuffix = window.BarhatTime ? ` · ${window.BarhatTime.zoneLabel()}` : '';
    document.getElementById('userRole').textContent = roleLabel + zoneSuffix;

    // Отправляем событие о роли пользователя для других модулей
    document.dispatchEvent(new CustomEvent('userRoleChanged', { detail: currentUser }));

    // Обработчик кнопки выхода
    document.getElementById('logoutBtn').addEventListener('click', async () => {
        const ok = await window.BarhatUI.confirm('Потребуется войти заново.', {
            title: 'Выйти из дашборда?',
            confirmText: 'Выйти',
        });
        if (ok) {
            try {
                const response = await fetch('/api/auth/logout', {
                    method: 'POST',
                    credentials: 'include'
                });

                if (response.ok) {
                    window.location.href = '/login';
                }
            } catch (error) {
                console.error('Logout error:', error);
                // При ошибке всё равно редиректим
                window.location.href = '/login';
            }
        }
    });

    // === Навигация ===

    // URL использует дефисы (/cash-shifts), внутренние id страниц — подчёркивания (cash_shifts,
    // как в data-page и правах доступа). Конвертируем между ними при работе с History API.
    function pageNameToUrlSlug(pageName) {
        return pageName.replace(/_/g, '-');
    }

    function urlSlugToPageName(slug) {
        return slug.replace(/-/g, '_');
    }

    const navItems = document.querySelectorAll('.nav-item');
    const pages = document.querySelectorAll('.page');

    // Сохраняем permissions пользователя
    const userPermissions = currentUser.sections || [];

    // Старый раздел счетов заморожен и оставлен только админу как архив
    // (Фаза 10 плана plans/2026-08-24-счета-новый-раздел.md). Право `invoices`
    // сотрудникам оставили намеренно — на нём держится доступ к данным, —
    // поэтому прячем пункт по роли, а не по отсутствию права.
    const ADMIN_ONLY_PAGES = ['invoices'];

    // Скрываем пункты меню к которым нет доступа
    navItems.forEach(item => {
        const pageName = item.getAttribute('data-page');
        // users_manage проверяется отдельно через роль
        if (pageName === 'users') return;

        if (ADMIN_ONLY_PAGES.includes(pageName) && currentUser.role !== 'admin') {
            item.style.display = 'none';
            return;
        }

        // Скрываем модуль если его нет в permissions
        if (pageName && !userPermissions.includes(pageName)) {
            item.style.display = 'none';
        }
    });

    // Функция для переключения страницы.
    // replaceUrl=true — когда страница открывается не по клику, а по текущему URL
    // (первая загрузка, кнопки назад/вперёд): новую запись в истории заводить
    // нельзя, иначе «Назад» упирается в дубли того же адреса.
    function navigateToPage(pageName, replaceUrl = false) {
        // Проверяем permissions
        // users_manage проверяется отдельно через роль admin
        // Право `invoices` у сотрудника осталось, поэтому проверка по
        // permissions ниже его пропустит и раздел откроется по прямой ссылке
        // /invoices — прячем ещё и здесь, по роли
        const hiddenByRole = ADMIN_ONLY_PAGES.includes(pageName) && currentUser.role !== 'admin';

        if (hiddenByRole || (pageName !== 'users' && !userPermissions.includes(pageName))) {
            console.warn('Нет доступа к модулю:', pageName);
            // Редирект на первую доступную страницу. Скрытые по роли из
            // кандидатов убираем, иначе редирект зациклится на самом себе.
            const allowed = userPermissions.filter(
                name => !(ADMIN_ONLY_PAGES.includes(name) && currentUser.role !== 'admin'));
            const firstAllowed = allowed[0] || 'dashboard';
            if (firstAllowed === pageName) return;  // деваться некуда — не зацикливаемся
            navigateToPage(firstAllowed, true);
            return;
        }

        // Убираем активный класс у всех пунктов меню
        navItems.forEach(item => {
            item.classList.remove('active');
        });

        // Скрываем все страницы
        pages.forEach(page => {
            page.classList.remove('active');
        });

        // Находим нужный пункт меню и страницу
        const targetNav = document.querySelector(`.nav-item[data-page="${pageName}"]`);
        const targetPage = document.querySelector(`.page[data-page="${pageName}"]`);

        if (targetNav && targetPage) {
            targetNav.classList.add('active');
            targetPage.classList.add('active');

            // Обновляем URL без перезагрузки. В embed-режиме тащим за собой
            // ?embed=1: иначе перезагрузка страницы внутри рамки (или переход
            // по F5) вернёт вид с сайдбаром для сессий, где embed включён
            // флагом, а не SSO.
            const newUrl = `/${pageNameToUrlSlug(pageName)}${isEmbed ? '?embed=1' : ''}`;
            if (replaceUrl) {
                history.replaceState({ page: pageName }, '', newUrl);
            } else {
                history.pushState({ page: pageName }, '', newUrl);
            }

            // Загружаем данные для страницы пользователей
            if (pageName === 'users' && window.BarhatUsers) {
                window.BarhatUsers.loadUsers();
            }

            // Активируем модуль кассовых смен
            if (pageName === 'cash_shifts' && window.CashShiftsModule) {
                window.CashShiftsModule.onPageActivated(currentUser);
            }

            // Активируем модуль счетов на оплату
            if (pageName === 'invoices' && window.InvoicesModule) {
                window.InvoicesModule.onPageActivated(currentUser);
            }

            // Активируем новый раздел согласования счетов (пилот, см.
            // plans/2026-08-24-счета-новый-раздел.md). Старый модуль выше не трогаем.
            if (pageName === 'invoices_v2' && window.InvoicesV2Module) {
                window.InvoicesV2Module.onPageActivated(currentUser);
            }

            // Активируем модуль списаний товара
            if (pageName === 'writeoffs' && window.WriteoffsModule) {
                window.WriteoffsModule.onPageActivated(currentUser);
            }
        } else {
            // Если страницы нет, показываем заглушку
            const targetNav = document.querySelector(`.nav-item[data-page="${pageName}"]`);
            if (targetNav) {
                targetNav.classList.add('active');
            }

            // Проверяем, есть ли хоть одна страница
            let existingPage = document.querySelector(`.page[data-page="${pageName}"]`);

            if (!existingPage) {
                // Создаём заглушку для несуществующей страницы
                existingPage = document.createElement('div');
                existingPage.className = 'page active';
                existingPage.setAttribute('data-page', pageName);
                existingPage.innerHTML = `
                    <section class="section">
                        <h2 class="section-title">${getSectionTitle(pageName)}</h2>
                        <div class="summary-placeholder">
                            <p>Раздел «${getSectionTitle(pageName)}» — скоро будет</p>
                        </div>
                    </section>
                `;
                document.getElementById('page-content').appendChild(existingPage);
            }
        }

        // На мобильных закрываем меню после выбора
        if (window.innerWidth <= 768) {
            document.querySelector('.sidebar').classList.remove('open');
        }
    }

    // Получить заголовок секции по имени страницы
    function getSectionTitle(pageName) {
        const titles = {
            'dashboard': 'ДАШБОРД',
            'crm': 'CRM АНАЛИТИКА',
            'systems': 'СТАТУС СИСТЕМ',
            'inventory': 'СВЕРКА ОСТАТКОВ',
            'seo': 'SEO',
            'yandex': 'ЯНДЕКС ЕДА',
            'flowwow': 'FLOWWOW',
            'reviews': 'ОТЗЫВЫ',
            'mailing': 'РАССЫЛКИ',
            'cart': 'БРОШЕННЫЕ КОРЗИНЫ',
            'budget': 'БЮДЖЕТ ПЛАН/ФАКТ',
            'salary': 'ЗАРПЛАТА',
            'tasks': 'ЗАДАЧНИК',
            'calculator': 'КАЛЬКУЛЯТОР БУКЕТОВ',
            'quality': 'КАЧЕСТВО СБОРКИ БУКЕТОВ',
            'cash_shifts': 'КАССОВЫЕ СМЕНЫ',
            'invoices': 'СЧЕТА (АРХИВ)',
            'invoices_v2': 'СОГЛАСОВАНИЕ СЧЕТОВ',
            'writeoffs': 'СПИСАНИЯ ТОВАРА',
            'abc_analysis': 'ABC-АНАЛИЗ ТОВАРОВ',
            'courier_payouts': 'ОПЛАТА КУРЬЕРАМ',
            'link_watch': 'ССЫЛКИ НА ТОВАРЫ',
            'regulations': 'РЕГЛАМЕНТЫ',
            'roles': 'УПРАВЛЕНИЕ РОЛЯМИ'
        };
        return titles[pageName] || pageName.toUpperCase();
    }

    // Обработчик клика по пунктам меню
    navItems.forEach(item => {
        item.addEventListener('click', function(e) {
            e.preventDefault();
            const pageName = this.getAttribute('data-page');
            navigateToPage(pageName);
        });
    });

    // Обработка навигации при загрузке (по пути URL)
    function handlePathChange() {
        const path = urlSlugToPageName(window.location.pathname.slice(1)); // Убираем первый /
        if (path) {
            // Проверяем есть ли доступ к запрошенной странице
            if (path === 'users' || userPermissions.includes(path)) {
                navigateToPage(path, true);
            } else {
                // Нет доступа - редирект на первую доступную страницу
                navigateToPage(userPermissions[0] || 'dashboard', true);
            }
        } else {
            // Нет пути - открываем первую доступную страницу
            navigateToPage(userPermissions[0] || 'dashboard', true);
        }
    }

    // Проверяем путь при загрузке
    handlePathChange();

    // Обработка кнопок назад/вперёд
    window.addEventListener('popstate', function(e) {
        const path = urlSlugToPageName(window.location.pathname.slice(1)); // Убираем первый /
        if (path) {
            navigateToPage(path, true);
        } else {
            navigateToPage(userPermissions[0] || 'dashboard', true);
        }
    });

    // === Мобильное меню ===
    // В embed-режиме сайдбара нет — гамбургер и обработчики к нему не нужны.

    if (!isEmbed) {
        // Кнопка гамбургер создаётся всегда — видимость (display) регулируется медиа-запросом
        // в CSS, чтобы она появлялась и при resize/повороте экрана, а не только при загрузке страницы.
        const menuToggle = document.createElement('button');
        menuToggle.className = 'mobile-menu-toggle';
        menuToggle.innerHTML = '<span></span>';
        menuToggle.setAttribute('aria-label', 'Открыть меню');

        menuToggle.addEventListener('click', function() {
            document.querySelector('.sidebar').classList.toggle('open');
        });

        document.body.appendChild(menuToggle);

        // Закрытие меню при клике вне его (на мобильных)
        document.addEventListener('click', function(e) {
            if (window.innerWidth <= 768) {
                const sidebar = document.querySelector('.sidebar');

                if (!sidebar.contains(e.target) && !menuToggle.contains(e.target)) {
                    sidebar.classList.remove('open');
                }
            }
        });
    }

    // === Загрузка метрик (заглушка для будущего API) ===

    /**
     * Загружает метрики с сервера
     * @param {Object} options - Опции загрузки
     * @returns {Promise<Object>} Данные метрик
     */
    async function fetchMetrics(options = {}) {
        // TODO: Подключить к реальному API
        return {
            sales: null,
            growth: null,
            target: null,
            aov: null,
            conversion: null,
            abandoned: null,
            upt: null
        };
    }

    /**
     * Обновляет отображение метрик на дашборде
     * @param {Object} metrics - Данные метрик
     */
    function updateMetrics(metrics) {
        // TODO: Обновить значения метрик
        console.log('Метрики:', metrics);
    }

    // Загрузка метрик при открытии дашборда
    fetchMetrics()
        .then(metrics => updateMetrics(metrics))
        .catch(err => console.log('Метрики пока недоступны:', err));
});

// === Экспорт функций для использования извне ===

window.BarhatDashboard = {
    /**
     * Программная навигация к странице
     * @param {string} pageName - Имя страницы
     */
    navigateTo: function(pageName) {
        const navItem = document.querySelector(`.nav-item[data-page="${pageName}"]`);
        if (navItem) {
            navItem.click();
        }
    },

    /**
     * Форматирование валюты (рубли)
     * @param {number} value - Значение
     * @returns {string} Отформатированная строка
     */
    formatCurrency: function(value) {
        return new Intl.NumberFormat('ru-RU', {
            style: 'currency',
            currency: 'RUB',
            minimumFractionDigits: 0,
            maximumFractionDigits: 0
        }).format(value);
    },

    /**
     * Форматирование чисел
     * @param {number} value - Значение
     * @returns {string} Отформатированная строка
     */
    formatNumber: function(value) {
        return new Intl.NumberFormat('ru-RU').format(value);
    },

    /**
     * Форматирование процентов
     * @param {number} value - Значение
     * @returns {string} Отформатированная строка
     */
    formatPercent: function(value) {
        const sign = value >= 0 ? '+' : '';
        return `${sign}${value.toFixed(1)}%`;
    }
};
