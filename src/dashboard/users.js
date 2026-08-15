/**
 * Управление пользователями в дашборде
 * Создание, редактирование, деактивация пользователей
 */

(function() {
    'use strict';

    // === Описание ролей (для подсказок) ===
    const ROLE_DESCRIPTIONS = {
        'admin': 'Полный доступ ко всем разделам, включая управление пользователями',
        'manager': 'Доступ к дашборду, качеству и калькулятору',
        'florist': 'Работает только со своей точкой продаж (кассовые смены)',
        'florist_analyst': 'Только раздел качества сборки'
    };

    // === Названия модулей для отображения ===
    const MODULE_NAMES = {
        'dashboard': 'Дашборд',
        'calculator': 'Калькулятор букетов',
        'quality': 'Качество сборки',
        'cash_shifts': 'Кассовые смены',
        'invoices': 'Счета на оплату',
        'users_manage': 'Управление пользователями'
    };

    // === Пресеты permissions для ролей ===
    const ROLE_PERMISSIONS = {
        'admin': ['dashboard', 'calculator', 'quality', 'cash_shifts', 'invoices', 'users_manage'],
        'manager': ['dashboard', 'calculator', 'quality', 'cash_shifts', 'invoices'],
        'florist': ['cash_shifts'],
        'florist_analyst': ['quality']
    };

    // === Текущее состояние ===
    let usersList = [];
    let storesList = [];
    let currentUserData = null;
    let editingUsername = null;

    // === DOM элементы ===
    const elements = {
        adminSection: null,
        usersPage: null,
        usersList: null,
        createUserBtn: null,
        userModal: null,
        userModalOverlay: null,
        userModalTitle: null,
        closeUserModalBtn: null,
        cancelUserBtn: null,
        saveUserBtn: null,
        userForm: null,
        userUsername: null,
        userFullname: null,
        userPassword: null,
        userRole: null,
        roleHint: null,
        modulesCheckboxes: null,
        userStoresGroup: null,
        userStoresCheckboxes: null,
        userStoresHint: null,
        passwordGroup: null,
        statusGroup: null,
        userStatusBadge: null,
        toggleStatusBtn: null
    };

    // === Инициализация ===
    function init() {
        // Получаем элементы DOM
        elements.adminSection = document.getElementById('admin-section');
        elements.usersPage = document.querySelector('.page[data-page="users"]');
        elements.usersList = document.getElementById('users-list');
        elements.createUserBtn = document.getElementById('create-user-btn');
        elements.userModal = document.getElementById('user-modal');
        elements.userModalOverlay = document.getElementById('user-modal-overlay');
        elements.userModalTitle = document.getElementById('user-modal-title');
        elements.closeUserModalBtn = document.getElementById('close-user-modal-btn');
        elements.cancelUserBtn = document.getElementById('cancel-user-btn');
        elements.saveUserBtn = document.getElementById('save-user-btn');
        elements.userForm = document.getElementById('user-form');
        elements.userUsername = document.getElementById('user-username');
        elements.userFullname = document.getElementById('user-fullname');
        elements.userPassword = document.getElementById('user-password');
        elements.userRole = document.getElementById('user-role');
        elements.roleHint = document.getElementById('role-hint');
        elements.modulesCheckboxes = document.getElementById('modules-checkboxes');
        elements.userStoresGroup = document.getElementById('user-stores-group');
        elements.userStoresCheckboxes = document.getElementById('user-stores-checkboxes');
        elements.userStoresHint = document.getElementById('user-stores-hint');
        elements.passwordGroup = document.getElementById('password-group');
        elements.statusGroup = document.getElementById('status-group');
        elements.userStatusBadge = document.getElementById('user-status-badge');
        elements.toggleStatusBtn = document.getElementById('toggle-status-btn');

        // Привязка событий
        bindEvents();

        // Обновление подсказки роли при изменении
        if (elements.userRole) {
            elements.userRole.addEventListener('change', onRoleChange);
        }

        // Инициализация чекбоксов модулей
        initModulesCheckboxes();

        // Загрузка точек продаж для привязки флориста/менеджера
        loadStores();

        console.log('[Users] Модуль инициализирован');
    }

    // === Привязка событий ===
    function bindEvents() {
        // Кнопка создания пользователя
        if (elements.createUserBtn) {
            elements.createUserBtn.addEventListener('click', () => openUserModal());
        }

        // Закрытие модального окна
        if (elements.closeUserModalBtn) {
            elements.closeUserModalBtn.addEventListener('click', closeUserModal);
        }
        if (elements.userModalOverlay) {
            elements.userModalOverlay.addEventListener('click', closeUserModal);
        }
        if (elements.cancelUserBtn) {
            elements.cancelUserBtn.addEventListener('click', closeUserModal);
        }

        // Сохранение пользователя
        if (elements.saveUserBtn) {
            elements.saveUserBtn.addEventListener('click', saveUser);
        }

        // Переключение статуса
        if (elements.toggleStatusBtn) {
            elements.toggleStatusBtn.addEventListener('click', toggleUserStatus);
        }

        // Отправка формы по Enter
        if (elements.userForm) {
            elements.userForm.addEventListener('submit', (e) => {
                e.preventDefault();
                saveUser();
            });
        }

        // Слушаем события изменения роли пользователя
        document.addEventListener('userRoleChanged', handleUserRoleChange);

        // Ограничение "1 точка" для флориста при выборе чекбоксов
        if (elements.userStoresCheckboxes) {
            elements.userStoresCheckboxes.addEventListener('change', onStoreCheckboxChange);
        }
    }

    // === Ограничение выбора точек для флориста (максимум одна) ===
    function onStoreCheckboxChange(e) {
        if (e.target.tagName !== 'INPUT') return;
        const role = elements.userRole.value;
        if (role === 'florist' && e.target.checked) {
            const checkboxes = elements.userStoresCheckboxes.querySelectorAll('input[type="checkbox"]');
            checkboxes.forEach(cb => {
                if (cb !== e.target) cb.checked = false;
            });
        }
    }

    // === Обработка изменения роли пользователя ===
    function handleUserRoleChange(e) {
        currentUserData = e.detail;

        // Показываем/скрываем админскую секцию
        if (elements.adminSection) {
            if (currentUserData.role === 'admin') {
                elements.adminSection.style.display = 'block';
            } else {
                elements.adminSection.style.display = 'none';
            }
        }
    }

    // === Показать админ-секцию (вызывается извне) ===
    function showAdminSection() {
        if (elements.adminSection && currentUserData && currentUserData.role === 'admin') {
            elements.adminSection.style.display = 'block';
        }
    }

    // === Загрузка списка пользователей ===
    async function loadUsers() {
        try {
            const response = await fetch('/api/auth/users', {
                credentials: 'include'
            });

            if (!response.ok) {
                throw new Error('Не удалось загрузить пользователей');
            }

            const data = await response.json();
            usersList = data.users || [];
            renderUsers();

        } catch (error) {
            console.error('[Users] Ошибка загрузки:', error);
            if (elements.usersList) {
                elements.usersList.innerHTML = `
                    <div class="error-state">
                        <p>Ошибка загрузки пользователей: ${error.message}</p>
                        <button class="btn btn-secondary btn-sm" onclick="BarhatUsers.loadUsers()">
                            Попробовать снова
                        </button>
                    </div>
                `;
            }
        }
    }

    // === Загрузка точек продаж (для привязки флориста/менеджера) ===
    async function loadStores() {
        try {
            const response = await fetch('/api/cash-shifts/stores', { credentials: 'include' });
            if (!response.ok) {
                throw new Error('Не удалось загрузить точки продаж');
            }
            const data = await response.json();
            storesList = data.stores || [];
            initUserStoresCheckboxes();
        } catch (error) {
            console.error('[Users] Ошибка загрузки точек:', error);
        }
    }

    // === Отрисовка списка пользователей ===
    function renderUsers() {
        if (!elements.usersList) return;

        if (usersList.length === 0) {
            elements.usersList.innerHTML = `
                <p class="empty-state">Пользователи не найдены. Создайте первого пользователя.</p>
            `;
            return;
        }

        const html = usersList.map(user => createUserCard(user)).join('');
        elements.usersList.innerHTML = html;
    }

    // === Создание карточки пользователя ===
    function createUserCard(user) {
        const isActive = Boolean(user.is_active);
        const statusClass = isActive ? 'active' : 'inactive';
        const statusText = isActive ? 'Активен' : 'Деактивирован';
        const createdDate = new Date(user.created_at).toLocaleDateString('ru-RU');

        // Отображаемое имя (ФИО или username)
        const displayName = user.full_name || user.username;

        // Формируем строку permissions
        const permissionsList = (user.permissions || [])
            .map(p => MODULE_NAMES[p] || p)
            .join(', ');

        const storesListStr = (user.stores || [])
            .map(s => s.name)
            .join(', ');

        // Нельзя удалить себя
        const isCurrentUser = currentUserData && currentUserData.username === user.username;

        return `
            <div class="user-card" data-username="${user.username}">
                <div class="user-card-header">
                    <div class="user-card-info">
                        <div class="user-card-avatar">
                            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/>
                                <circle cx="12" cy="7" r="4"/>
                            </svg>
                        </div>
                        <div>
                            <div class="user-card-name">${escapeHtml(displayName)}</div>
                            <div class="user-card-meta">
                                <span class="role-badge">${getRoleName(user.role)}</span>
                                <span class="status-badge status-${statusClass}">${statusText}</span>
                            </div>
                            ${permissionsList ? `
                                <div class="user-card-permissions" title="${escapeHtml(permissionsList)}">
                                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                        <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/>
                                        <circle cx="9" cy="7" r="4"/>
                                        <path d="M23 21v-2a4 4 0 0 0-3-3.87"/>
                                        <path d="M16 3.13a4 4 0 0 1 0 7.75"/>
                                    </svg>
                                    <span>${escapeHtml(permissionsList)}</span>
                                </div>
                            ` : ''}
                            ${storesListStr ? `
                                <div class="user-card-permissions" title="${escapeHtml(storesListStr)}">
                                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                        <path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>
                                        <polyline points="9 22 9 12 15 12 15 22"/>
                                    </svg>
                                    <span>${escapeHtml(storesListStr)}</span>
                                </div>
                            ` : ''}
                        </div>
                    </div>
                    <div class="user-card-actions">
                        <button class="btn btn-sm btn-secondary" onclick="BarhatUsers.editUser('${escapeHtml(user.username)}')" title="Редактировать">
                            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
                                <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
                            </svg>
                        </button>
                        ${!isCurrentUser ? `
                            <button class="btn btn-sm btn-${isActive ? 'warning' : 'success'}" onclick="BarhatUsers.toggleUserStatus('${escapeHtml(user.username)}')" title="${isActive ? 'Деактивировать' : 'Активировать'}">
                                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                    ${isActive
                                        ? '<rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><line x1="9" y1="9" x2="15" y2="15"/><line x1="15" y1="9" x2="9" y2="15"/>'
                                        : '<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/>'
                                    }
                                </svg>
                            </button>
                            <button class="btn btn-sm btn-danger" onclick="BarhatUsers.deleteUser('${escapeHtml(user.username)}')" title="Удалить">
                                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                    <polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/>
                                </svg>
                            </button>
                        ` : ''}
                    </div>
                </div>
                <div class="user-card-footer">
                    <span class="user-card-date">Создан: ${createdDate}</span>
                </div>
            </div>
        `;
    }

    // === Получить название роли ===
    function getRoleName(role) {
        const names = {
            'admin': 'Администратор',
            'manager': 'Менеджер',
            'florist': 'Флорист',
            'florist_analyst': 'Аналитик качества'
        };
        return names[role] || role;
    }

    // === Открытие модального окна ===
    function openUserModal(username = null) {
        editingUsername = username;

        if (username) {
            // Режим редактирования
            const user = usersList.find(u => u.username === username);
            if (!user) return;

            elements.userModalTitle.textContent = 'Редактировать пользователя';
            elements.userUsername.value = user.username;
            elements.userUsername.readOnly = true;
            elements.userUsername.disabled = true;
            elements.userFullname.value = user.full_name || '';
            elements.userRole.value = user.role;
            elements.userPassword.value = '';
            elements.userPassword.required = false;
            elements.passwordGroup.querySelector('label').textContent = 'Новый пароль (не меняйте, если не нужно)';
            elements.passwordGroup.querySelector('.form-hint').textContent = 'Оставьте пустым, чтобы не менять пароль';

            // Устанавливаем permissions
            setSelectedModules(user.permissions || []);

            // Устанавливаем привязанные точки продаж
            updateStoresVisibility(user.role);
            setSelectedStores((user.stores || []).map(s => s.id));

            // Показываем статус
            elements.statusGroup.style.display = 'block';
            updateUserStatusBadge(user.is_active);

        } else {
            // Режим создания
            elements.userModalTitle.textContent = 'Создать пользователя';
            elements.userUsername.value = '';
            elements.userUsername.readOnly = false;
            elements.userUsername.disabled = false;
            elements.userFullname.value = '';
            elements.userRole.value = '';
            elements.userPassword.value = '';
            elements.userPassword.required = true;
            elements.passwordGroup.querySelector('label').textContent = 'Пароль';
            elements.passwordGroup.querySelector('.form-hint').textContent = 'Минимум 8 символов';

            // Сбрасываем permissions
            setSelectedModules([]);

            // Сбрасываем привязанные точки продаж
            updateStoresVisibility('');
            setSelectedStores([]);

            // Скрываем статус
            elements.statusGroup.style.display = 'none';
        }

        // В режиме редактирования НЕ вызываем onRoleChange() чтобы не перезаписать
        // фактические permissions пользователя дефолтными значениями роли
        if (!editingUsername) {
            onRoleChange();
        }
        elements.userModal.classList.add('active');
        elements.userUsername.focus();
    }

    // === Закрытие модального окна ===
    function closeUserModal() {
        elements.userModal.classList.remove('active');
        elements.userForm.reset();
        editingUsername = null;
    }

    // === Инициализация чекбоксов модулей ===
    function initModulesCheckboxes() {
        if (!elements.modulesCheckboxes) return;

        const modules = Object.keys(MODULE_NAMES);
        elements.modulesCheckboxes.innerHTML = modules.map(module => `
            <label class="module-checkbox">
                <input type="checkbox" value="${module}" data-module="${module}">
                <span>${MODULE_NAMES[module]}</span>
            </label>
        `).join('');
    }

    // === Обработчик изменения роли ===
    function onRoleChange() {
        updateRoleHint();
        // При выборе роли можно автоматически проставить permissions
        const role = elements.userRole.value;
        if (ROLE_PERMISSIONS[role]) {
            const checkboxes = elements.modulesCheckboxes.querySelectorAll('input[type="checkbox"]');
            checkboxes.forEach(cb => {
                cb.checked = ROLE_PERMISSIONS[role].includes(cb.value);
            });
        }
        updateStoresVisibility(role);
    }

    // === Инициализация чекбоксов точек продаж ===
    function initUserStoresCheckboxes() {
        if (!elements.userStoresCheckboxes) return;
        elements.userStoresCheckboxes.innerHTML = storesList.map(store => `
            <label class="module-checkbox">
                <input type="checkbox" value="${store.id}">
                <span>${escapeHtml(store.name)}</span>
            </label>
        `).join('');
    }

    // === Показать/скрыть блок привязки к точкам в зависимости от роли ===
    function updateStoresVisibility(role) {
        if (!elements.userStoresGroup) return;
        const needsStores = role === 'florist' || role === 'manager';
        elements.userStoresGroup.style.display = needsStores ? 'block' : 'none';
        if (elements.userStoresHint) {
            elements.userStoresHint.textContent = role === 'florist'
                ? 'Флорист привязывается ровно к одной точке.'
                : 'Менеджер может видеть несколько точек.';
        }
    }

    // === Получить выбранные точки ===
    function getSelectedStores() {
        if (!elements.userStoresCheckboxes) return [];
        const checkboxes = elements.userStoresCheckboxes.querySelectorAll('input[type="checkbox"]:checked');
        return Array.from(checkboxes).map(cb => parseInt(cb.value, 10));
    }

    // === Установить выбранные точки ===
    function setSelectedStores(storeIds) {
        if (!elements.userStoresCheckboxes) return;
        const ids = (storeIds || []).map(id => String(id));
        const checkboxes = elements.userStoresCheckboxes.querySelectorAll('input[type="checkbox"]');
        checkboxes.forEach(cb => {
            cb.checked = ids.includes(cb.value);
        });
    }

    // === Получить выбранные модули ===
    function getSelectedModules() {
        if (!elements.modulesCheckboxes) return [];
        const checkboxes = elements.modulesCheckboxes.querySelectorAll('input[type="checkbox"]:checked');
        return Array.from(checkboxes).map(cb => cb.value);
    }

    // === Установить выбранные модули ===
    function setSelectedModules(modules) {
        if (!elements.modulesCheckboxes) return;
        const checkboxes = elements.modulesCheckboxes.querySelectorAll('input[type="checkbox"]');
        checkboxes.forEach(cb => {
            cb.checked = modules.includes(cb.value);
        });
    }

    // === Обновление подсказки роли ===
    function updateRoleHint() {
        const role = elements.userRole.value;
        if (elements.roleHint) {
            elements.roleHint.textContent = ROLE_DESCRIPTIONS[role] || '';
        }
    }

    // === Обновление бейджа статуса ===
    function updateUserStatusBadge(isActive) {
        if (!elements.userStatusBadge || !elements.toggleStatusBtn) return;

        const statusClass = Boolean(isActive) ? 'active' : 'inactive';
        const statusText = Boolean(isActive) ? 'Активен' : 'Деактивирован';

        elements.userStatusBadge.className = `status-badge status-${statusClass}`;
        elements.userStatusBadge.textContent = statusText;

        elements.toggleStatusBtn.textContent = Boolean(isActive) ? 'Деактивировать' : 'Активировать';
        elements.toggleStatusBtn.className = `btn btn-sm btn-${Boolean(isActive) ? 'warning' : 'success'}`;
    }

    // === Сохранение пользователя ===
    async function saveUser() {
        const username = elements.userUsername.value.trim();
        const fullName = elements.userFullname.value.trim();
        const password = elements.userPassword.value;
        const role = elements.userRole.value;
        const permissions = getSelectedModules(); // Берём из чекбоксов (пользователь может переопределить роль)

        // Валидация
        if (!editingUsername && !username) {
            alert('Введите имя пользователя');
            return;
        }

        if (!fullName) {
            alert('Введите ФИО');
            return;
        }

        if (!role) {
            alert('Выберите роль');
            return;
        }

        if (permissions.length === 0) {
            alert('Выберите хотя бы один модуль для доступа');
            return;
        }

        if (role === 'florist' && getSelectedStores().length !== 1) {
            alert('Флорист должен быть привязан к ровно одной точке продаж');
            return;
        }

        if (!editingUsername && password.length < 8) {
            alert('Пароль должен быть минимум 8 символов');
            return;
        }

        if (editingUsername && password && password.length < 8) {
            alert('Пароль должен быть минимум 8 символов');
            return;
        }

        try {
            elements.saveUserBtn.disabled = true;
            elements.saveUserBtn.textContent = 'Сохранение...';

            let response;

            if (editingUsername) {
                // Обновление существующего пользователя
                const data = {
                    full_name: fullName,
                    role,
                    permissions
                };
                if (password) {
                    data.password = password;
                }

                response = await fetch(`/api/auth/users/${editingUsername}`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(data),
                    credentials: 'include'
                });

            } else {
                // Создание нового пользователя
                response = await fetch('/api/auth/users', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ username, full_name: fullName, password, role, permissions }),
                    credentials: 'include'
                });
            }

            if (!response.ok) {
                const error = await response.json();
                throw new Error(error.error || 'Ошибка сохранения');
            }

            // Привязка к точкам продаж (только для florist/manager)
            const effectiveUsername = editingUsername || username;
            if (role === 'florist' || role === 'manager') {
                const storesResponse = await fetch(`/api/auth/users/${effectiveUsername}/stores`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ store_ids: getSelectedStores() }),
                    credentials: 'include'
                });

                if (!storesResponse.ok) {
                    const error = await storesResponse.json();
                    throw new Error(error.error || 'Ошибка привязки точек продаж');
                }
            }

            // Успешно сохранено
            closeUserModal();
            await loadUsers();

            // Показываем уведомление
            showNotification(editingUsername ? 'Пользователь обновлён' : 'Пользователь создан');

        } catch (error) {
            console.error('[Users] Ошибка сохранения:', error);
            alert(error.message);
        } finally {
            elements.saveUserBtn.disabled = false;
            elements.saveUserBtn.textContent = 'Сохранить';
        }
    }

    // === Переключение статуса пользователя ===
    async function toggleUserStatus(username) {
        const user = usersList.find(u => u.username === username);
        if (!user) return;

        const isActive = Boolean(user.is_active);
        const action = isActive ? 'deactivate' : 'activate';
        const confirmText = isActive
            ? 'Вы уверены, что хотите деактивировать этого пользователя? Он не сможет войти в систему.'
            : 'Вы уверены, что хотите активировать этого пользователя?';

        if (!confirm(confirmText)) {
            return;
        }

        try {
            const response = await fetch(`/api/auth/users/${username}/${action}`, {
                method: 'POST',
                credentials: 'include'
            });

            if (!response.ok) {
                const error = await response.json();
                throw new Error(error.error || 'Ошибка изменения статуса');
            }

            await loadUsers();
            showNotification(isActive ? 'Пользователь деактивирован' : 'Пользователь активирован');

        } catch (error) {
            console.error('[Users] Ошибка изменения статуса:', error);
            alert(error.message);
        }
    }

    // === Удаление пользователя ===
    async function deleteUser(username) {
        if (!confirm('Вы уверены, что хотите удалить этого пользователя? Это действие нельзя отменить.')) {
            return;
        }

        try {
            const response = await fetch(`/api/auth/users/${username}`, {
                method: 'DELETE',
                credentials: 'include'
            });

            if (!response.ok) {
                const error = await response.json();
                throw new Error(error.error || 'Ошибка удаления');
            }

            await loadUsers();
            showNotification('Пользователь удалён');

        } catch (error) {
            console.error('[Users] Ошибка удаления:', error);
            alert(error.message);
        }
    }

    // === Показать уведомление ===
    function showNotification(message) {
        // Создаём временное уведомление
        const notification = document.createElement('div');
        notification.className = 'notification notification-success';
        notification.textContent = message;
        notification.style.cssText = `
            position: fixed;
            bottom: 20px;
            right: 20px;
            background: var(--barkhat-success);
            color: white;
            padding: 12px 20px;
            border-radius: 8px;
            box-shadow: 0 4px 12px rgba(0,0,0,0.15);
            z-index: 10000;
            animation: slideIn 0.3s ease;
        `;

        document.body.appendChild(notification);

        setTimeout(() => {
            notification.style.opacity = '0';
            notification.style.transform = 'translateY(20px)';
            notification.style.transition = 'all 0.3s ease';
            setTimeout(() => notification.remove(), 300);
        }, 3000);
    }

    // === Экранирование HTML ===
    function escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    // === Экспорт публичного API ===
    window.BarhatUsers = {
        init,
        loadUsers,
        showAdminSection,
        editUser: openUserModal,
        toggleUserStatus,
        deleteUser
    };

    // Инициализация при готовности DOM
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
