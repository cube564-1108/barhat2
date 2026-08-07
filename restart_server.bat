@echo off
echo ================================
echo PEREZAPUSK API SERVERA
echo ================================
echo.
echo 1. UDALYAEM KESh PYTHON...
cd /d "%~dp0"
rmdir /s /q scripts\__pycache__ 2>nul
del /q scripts\*.pyc 2>nul
echo    Cache cleared
echo.
echo 2. ZAPUSKAEM SERVER...
python -m src.pyrus.server
pause
