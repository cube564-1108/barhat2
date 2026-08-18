/**
 * Раздел «Задачи» на дашборде БАРХАТ
 * Бэклог идей/фич владельца: статус, описание, "на чём остановились". Admin-only.
 */

(function() {
    'use strict';

    let isAdmin = false;
    let loaded = false;
    let editingTaskId = null;

    const STATUS_LABELS = {
        idea: 'Идея',
        in_progress: 'В работе',
        done: 'Выполнено',
    };

    const elements = {};

    function init() {
        elements.section = document.getElementById('tasksSection');
        if (!elements.section) return; // Раздела нет в DOM — модуль не нужен

        elements.list = document.getElementById('tasks-list');
        elements.createBtn = document.getElementById('create-task-btn');

        elements.modal = document.getElementById('task-modal');
        elements.overlay = document.getElementById('task-modal-overlay');
        elements.modalTitle = document.getElementById('task-modal-title');
        elements.closeBtn = document.getElementById('close-task-modal-btn');
        elements.cancelBtn = document.getElementById('cancel-task-modal-btn');
        elements.confirmBtn = document.getElementById('confirm-task-modal-btn');

        elements.titleInput = document.getElementById('task-title');
        elements.descriptionInput = document.getElementById('task-description');
        elements.statusSelect = document.getElementById('task-status');
        elements.progressNotesInput = document.getElementById('task-progress-notes');

        elements.createBtn?.addEventListener('click', openCreateModal);
        elements.closeBtn?.addEventListener('click', closeModal);
        elements.cancelBtn?.addEventListener('click', closeModal);
        elements.overlay?.addEventListener('click', closeModal);
        elements.confirmBtn?.addEventListener('click', submitTask);

        elements.list?.addEventListener('click', handleListClick);
        elements.list?.addEventListener('change', handleStatusChange);
    }

    document.addEventListener('userRoleChanged', function(e) {
        isAdmin = e.detail && e.detail.role === 'admin';
        if (!elements.section) return;

        elements.section.style.display = isAdmin ? '' : 'none';

        if (isAdmin && !loaded) {
            loaded = true;
            loadTasks();
        }
    });

    async function loadTasks() {
        try {
            const response = await fetch('/api/tasks', { credentials: 'include' });
            const data = await response.json();

            if (!response.ok) {
                console.error('Ошибка загрузки задач:', data.error);
                return;
            }

            renderTasks(data.tasks || []);
        } catch (error) {
            console.error('Ошибка загрузки задач:', error);
        }
    }

    function renderTasks(tasks) {
        if (!elements.list) return;

        if (tasks.length === 0) {
            elements.list.innerHTML = '<p class="summary-placeholder">Задач пока нет</p>';
            return;
        }

        elements.list.innerHTML = tasks.map(taskCardHtml).join('');
    }

    function taskCardHtml(task) {
        const description = task.description
            ? `<p class="task-card-description">${escapeHtml(task.description)}</p>`
            : '';
        const notes = task.progress_notes
            ? `<p class="task-card-notes"><strong>На чём остановились:</strong> ${escapeHtml(task.progress_notes)}</p>`
            : '';

        return `
            <div class="task-card" data-id="${task.id}">
                <div class="task-card-header">
                    <span class="status-badge status-${task.status.replace('_', '-')}">${STATUS_LABELS[task.status]}</span>
                    <h4 class="task-card-title">${escapeHtml(task.title)}</h4>
                    <div class="task-card-actions">
                        <select class="task-status-select" data-id="${task.id}">
                            <option value="idea" ${task.status === 'idea' ? 'selected' : ''}>Идея</option>
                            <option value="in_progress" ${task.status === 'in_progress' ? 'selected' : ''}>В работе</option>
                            <option value="done" ${task.status === 'done' ? 'selected' : ''}>Выполнено</option>
                        </select>
                        <button class="btn btn-secondary btn-sm" data-action="edit" data-id="${task.id}">Редактировать</button>
                        <button class="admin-item-delete" data-action="delete" data-id="${task.id}" title="Удалить">
                            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <polyline points="3 6 5 6 21 6"/>
                                <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
                            </svg>
                        </button>
                    </div>
                </div>
                ${description}
                ${notes}
            </div>
        `;
    }

    // Не div.textContent/innerHTML — не экранирует кавычки, значения с "
    // обрывались при подстановке в value="${...}" (см. историю сессий,
    // инцидент 2026-08-18, invoices.js).
    function escapeHtml(text) {
        return String(text ?? '').replace(/[&<>"']/g, (c) => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
        }[c]));
    }

    function handleListClick(e) {
        const btn = e.target.closest('button[data-action]');
        if (!btn) return;

        const taskId = parseInt(btn.getAttribute('data-id'), 10);
        const action = btn.getAttribute('data-action');

        if (action === 'edit') {
            openEditModal(taskId);
        } else if (action === 'delete') {
            deleteTask(taskId);
        }
    }

    async function handleStatusChange(e) {
        const select = e.target.closest('.task-status-select');
        if (!select) return;

        const taskId = parseInt(select.getAttribute('data-id'), 10);
        try {
            const response = await fetch(`/api/tasks/${taskId}/status`, {
                method: 'POST',
                credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ status: select.value }),
            });
            const data = await response.json();
            if (!response.ok) {
                alert(data.error || 'Не удалось сменить статус');
                return;
            }
            await loadTasks();
        } catch (error) {
            console.error('Ошибка смены статуса:', error);
            alert('Не удалось сменить статус');
        }
    }

    function openCreateModal() {
        editingTaskId = null;
        elements.modalTitle.textContent = 'Новая задача';
        elements.titleInput.value = '';
        elements.descriptionInput.value = '';
        elements.statusSelect.value = 'idea';
        elements.progressNotesInput.value = '';

        elements.modal.classList.add('active');
        elements.overlay.classList.add('active');
    }

    async function openEditModal(taskId) {
        try {
            const response = await fetch('/api/tasks', { credentials: 'include' });
            const data = await response.json();
            const task = (data.tasks || []).find(t => t.id === taskId);
            if (!task) return;

            editingTaskId = taskId;
            elements.modalTitle.textContent = 'Редактировать задачу';
            elements.titleInput.value = task.title;
            elements.descriptionInput.value = task.description || '';
            elements.statusSelect.value = task.status;
            elements.progressNotesInput.value = task.progress_notes || '';

            elements.modal.classList.add('active');
            elements.overlay.classList.add('active');
        } catch (error) {
            console.error('Ошибка загрузки задачи:', error);
        }
    }

    function closeModal() {
        elements.modal.classList.remove('active');
        elements.overlay.classList.remove('active');
        editingTaskId = null;
    }

    async function submitTask() {
        const title = elements.titleInput.value.trim();
        if (!title) {
            alert('Укажите название задачи');
            return;
        }

        const payload = {
            title,
            description: elements.descriptionInput.value.trim(),
            progress_notes: elements.progressNotesInput.value.trim(),
        };

        try {
            let response;
            if (editingTaskId) {
                response = await fetch(`/api/tasks/${editingTaskId}`, {
                    method: 'PUT',
                    credentials: 'include',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload),
                });
            } else {
                payload.status = elements.statusSelect.value;
                response = await fetch('/api/tasks', {
                    method: 'POST',
                    credentials: 'include',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload),
                });
            }

            const data = await response.json();
            if (!response.ok) {
                alert(data.error || 'Не удалось сохранить задачу');
                return;
            }

            // Статус при редактировании меняется отдельным селектом в карточке,
            // но если его поменяли прямо в модалке — применяем тем же эндпоинтом смены статуса
            if (editingTaskId && elements.statusSelect.value !== data.task.status) {
                await fetch(`/api/tasks/${editingTaskId}/status`, {
                    method: 'POST',
                    credentials: 'include',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ status: elements.statusSelect.value }),
                });
            }

            closeModal();
            await loadTasks();
        } catch (error) {
            console.error('Ошибка сохранения задачи:', error);
            alert('Не удалось сохранить задачу');
        }
    }

    async function deleteTask(taskId) {
        if (!confirm('Удалить задачу?')) return;

        try {
            const response = await fetch(`/api/tasks/${taskId}`, {
                method: 'DELETE',
                credentials: 'include',
            });
            const data = await response.json();
            if (!response.ok) {
                alert(data.error || 'Не удалось удалить задачу');
                return;
            }
            await loadTasks();
        } catch (error) {
            console.error('Ошибка удаления задачи:', error);
            alert('Не удалось удалить задачу');
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

})();
