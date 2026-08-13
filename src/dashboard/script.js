/**
 * JavaScript для главного дашборда Бархат
 * Навигация между страницами
 */

document.addEventListener('DOMContentLoaded', async function() {

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
    document.getElementById('userName').textContent = currentUser.username;
    const roleNames = {
        'admin': 'Администратор',
        'manager': 'Менеджер',
        'florist_analyst': 'Аналитик качества'
    };
    document.getElementById('userRole').textContent = roleNames[currentUser.role] || currentUser.role;

    // Отправляем событие о роли пользователя для других модулей
    document.dispatchEvent(new CustomEvent('userRoleChanged', { detail: currentUser }));

    // Обработчик кнопки выхода
    document.getElementById('logoutBtn').addEventListener('click', async () => {
        if (confirm('Вы уверены, что хотите выйти?')) {
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

    const navItems = document.querySelectorAll('.nav-item');
    const pages = document.querySelectorAll('.page');

    // Сохраняем permissions пользователя
    const userPermissions = currentUser.sections || [];

    // Скрываем пункты меню к которым нет доступа
    navItems.forEach(item => {
        const pageName = item.getAttribute('data-page');
        // users_manage проверяется отдельно через роль
        if (pageName === 'users') return;

        // Скрываем модуль если его нет в permissions
        if (pageName && !userPermissions.includes(pageName)) {
            item.style.display = 'none';
        }
    });

    // Функция для переключения страницы
    function navigateToPage(pageName) {
        // Проверяем permissions
        // users_manage проверяется отдельно через роль admin
        if (pageName !== 'users' && !userPermissions.includes(pageName)) {
            console.warn('Нет доступа к модулю:', pageName);
            // Редирект на первую доступную страницу
            const firstAllowed = userPermissions[0] || 'dashboard';
            navigateToPage(firstAllowed);
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

            // Обновляем URL без перезагрузки
            history.pushState({ page: pageName }, '', `#${pageName}`);

            // Загружаем данные для страницы пользователей
            if (pageName === 'users' && window.BarhatUsers) {
                window.BarhatUsers.loadUsers();
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

    // Обработка навигации при загрузке (по хешу URL)
    function handleHashChange() {
        const hash = window.location.hash.slice(1); // Убираем #
        if (hash) {
            // Проверяем есть ли доступ к запрошенной странице
            if (hash === 'users' || userPermissions.includes(hash)) {
                navigateToPage(hash);
            } else {
                // Нет доступа - редирект на первую доступную страницу
                navigateToPage(userPermissions[0] || 'dashboard');
            }
        } else {
            // Нет хеша - открываем первую доступную страницу
            navigateToPage(userPermissions[0] || 'dashboard');
        }
    }

    // Проверяем хеш при загрузке
    handleHashChange();

    // Слушаем изменения хеша
    window.addEventListener('hashchange', handleHashChange);

    // Обработка кнопок назад/вперёд
    window.addEventListener('popstate', function(e) {
        if (e.state && e.state.page) {
            navigateToPage(e.state.page);
        }
    });

    // === Мобильное меню ===

    // Создаём кнопку гамбургер (для мобильных)
    if (window.innerWidth <= 768) {
        const menuToggle = document.createElement('button');
        menuToggle.className = 'mobile-menu-toggle';
        menuToggle.innerHTML = '<span></span>';
        menuToggle.setAttribute('aria-label', 'Открыть меню');

        menuToggle.addEventListener('click', function() {
            document.querySelector('.sidebar').classList.toggle('open');
        });

        document.body.appendChild(menuToggle);
    }

    // Закрытие меню при клике вне его (на мобильных)
    document.addEventListener('click', function(e) {
        if (window.innerWidth <= 768) {
            const sidebar = document.querySelector('.sidebar');
            const menuToggle = document.querySelector('.mobile-menu-toggle');

            if (!sidebar.contains(e.target) && !menuToggle.contains(e.target)) {
                sidebar.classList.remove('open');
            }
        }
    });

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
