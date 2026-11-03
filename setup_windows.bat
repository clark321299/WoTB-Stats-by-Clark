@echo off
REM First-time setup for WoTB Stats by Clark on Windows 10/11 (Python 3.12).
setlocal EnableExtensions
cd /d "%~dp0"

echo === WoTB Stats by Clark — Windows setup ===

py -3.12 --version >nul 2>&1
if errorlevel 1 (
  echo Потрібен Python 3.12. Встанови з https://www.python.org/downloads/
  echo У інсталяторі увімкни "Add python.exe to PATH". Новіший Python ламає lz4.
  exit /b 1
)
set "PY=py -3.12"

set "VENV_DIR=%~dp0.venv"
if defined CL_APPS_VENV set "VENV_DIR=%CL_APPS_VENV%"

echo Створюю venv: %VENV_DIR%
%PY% -m venv "%VENV_DIR%"
if errorlevel 1 (
  echo Не вдалося створити venv.
  exit /b 1
)

> "%~dp0.cl_apps_venv" echo %VENV_DIR%

call "%VENV_DIR%\Scripts\activate.bat"
python -m pip install --upgrade pip
if exist "%~dp0CL-Stats-Desktop\requirements.txt" (
  echo pip install — CL-Stats-Desktop
  pip install -r "%~dp0CL-Stats-Desktop\requirements.txt"
)

echo.
echo venv: %VENV_DIR%
echo.
echo Далі:
echo   CL-Stats-Desktop\запуск.bat
echo   або CL-Stats-Desktop\запуск-фон.bat  ^(без вікна^)
echo.
echo Потрібен Chrome або Edge. Реплеї шукаються в %%LOCALAPPDATA%%\wotblitz\...
echo Якщо шлях інший — вкажи в Settings / CL_STATS_GAME_DATA_DIR.
echo.
pause
endlocal
