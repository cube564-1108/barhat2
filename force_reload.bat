@echo off
echo ================================
echo COMPLETE PYTHON CACHE RESET
echo ================================
echo.

cd /d "%~dp0"

echo 1. Stopping any Python processes...
taskkill /F /IM python.exe 2>nul
timeout /t 2 /nobreak >nul

echo 2. Removing ALL cache files...
rmdir /s /q __pycache__ 2>nul
rmdir /s /q scripts\__pycache__ 2>nul
rmdir /s /q src\__pycache__ 2>nul
rmdir /s /q src\pyrus\__pycache__ 2>nul
del /s /q *.pyc 2>nul

echo 3. Starting server with fresh cache...
python -B -m src.pyrus.server

pause
