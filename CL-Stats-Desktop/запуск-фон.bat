@echo off
REM CL-Stats — лише фоновий сервер (без вікна). Зупинка: закрий консоль або kill pid з data\cl_stats.pid
setlocal EnableExtensions
cd /d "%~dp0"

if not exist "data" mkdir data

call "%~dp0..\activate_venv.bat"
set "PYTHONPATH=%~dp0..;%PYTHONPATH%"
set "HTTP_PROXY="
set "HTTPS_PROXY="
set "ALL_PROXY="
set "http_proxy="
set "https_proxy="
set "all_proxy="
set "CL_STATS_NO_BROWSER=1"

echo [CL-Stats] Фон на http://127.0.0.1:17321
python main.py
endlocal
