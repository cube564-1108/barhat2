# Перезапуск сервера Pyrus API

## Способ 1: Если сервер запущен в отдельном окне

1. **Перейдите в окно где запущен сервер** (обычно это отдельное окно терминала)
2. **Нажмите `Ctrl+C`** — это остановит сервер
3. **Дождитесь сообщения** о завершении работы
4. **Запустите сервер заново:**
   ```bash
   cd c:\Users\Станислав\Desktop\barhat-codex
   set PYTHONPATH=src
   python -m pyrus.server
   ```

## Способ 2: Если окно сервера закрыто/потеряно

1. **Откройте новое окно терминала** (PowerShell или CMD)
2. **Найдите процесс Python:**
   ```bash
   tasklist | findstr python
   ```
   Вы увидите что-то вроде:
   ```
   python.exe                    12345 Console                    1     50,000 K
   ```
3. **Остановите процесс** (замените `12345` на реальный PID):
   ```bash
   taskkill /PID 12345 /F
   ```
4. **Запустите сервер:**
   ```bash
   cd c:\Users\Станислав\Desktop\barhat-codex
   set PYTHONPATH=src
   python -m pyrus.server
   ```

## Способ 3: Создать bat-файл для быстрого запуска

Создайте файл `start-server.bat` в корне проекта:

```batch
@echo off
cd /d "c:\Users\Станислав\Desktop\barhat-codex"
set PYTHONPATH=src
python -m pyrus.server
pause
```

Теперь можно запускать сервер двойным кликом по этому файлу.

---

## Проверка после запуска

После перезапуска сервер должен показать:

```
INFO - Запуск сервера на http://127.0.0.1:5000
INFO - БД: data/pyrus.db
 * Running on http://127.0.0.1:5000
```

**Проверьте что API работает:**
```bash
curl http://127.0.0.1:5000/api/quality
```

Должен вернуть JSON с 9 салонами.

**Проверьте endpoint salon-history:**
```bash
curl "http://127.0.0.1:5000/api/quality/salon-history?salon=ЕКБ%20Бажова&months=6"
```

Должен вернуть JSON с `success: true` и данными истории.

---

## Если возникают ошибки

**Ошибка "Port 5000 is already in use":**
- Значит старый процесс не остановился. Используйте `taskkill` как в Способе 2.

**Ошибка "No module named 'pyrus'":**
- Убедитесь что `PYTHONPATH=src` установлен перед запуском.

**Ошибка с БД:**
- Проверьте что файл `data/pyrus.db` существует и доступен.
