@echo off
REM CL-Stats — сервер + вікно (Chrome/Edge --app). Хрестик зупиняє і сервер.
REM Лише фон: запуск-фон.bat
setlocal EnableExtensions
cd /d "%~dp0"

if not exist "data" mkdir data

call "%~dp0..\activate_venv.bat"
set "PYTHONPATH=%~dp0..;%PYTHONPATH%"

REM Без проксі з IDE — інакше WG API може падати
set "HTTP_PROXY="
set "HTTPS_PROXY="
set "ALL_PROXY="
set "http_proxy="
set "https_proxy="
set "all_proxy="
set "CL_STATS_NO_BROWSER="

echo [CL-Stats] Запуск (вікно + сервер; хрестик = повний вихід)...
python main.py
if errorlevel 1 (
  echo.
  echo Помилка запуску. Спочатку запусти ..\setup_windows.bat
  pause
)
endlocal
