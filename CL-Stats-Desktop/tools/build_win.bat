@echo off
REM One-dir PyInstaller: dist\WoTB Stats by Clark\WoTB Stats by Clark.exe
REM Запускати на Windows. Linux-збірку цим файлом не роблять.
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0.."

call "%~dp0..\..\activate_venv.bat"

where python >nul 2>&1
if errorlevel 1 (
  echo Потрібен Python 3.10+ у PATH. Спочатку запусти ..\..\setup_windows.bat
  pause
  exit /b 1
)

python -m pip install --upgrade pip
if errorlevel 1 goto :fail
python -m pip install -r requirements.txt "pyinstaller>=6.6" pillow tzdata numpy
if errorlevel 1 goto :fail

python -m PyInstaller --noconfirm --clean "%~dp0wotb_stats.spec"
if errorlevel 1 goto :fail

echo.
echo Готово: dist\WoTB Stats by Clark\WoTB Stats by Clark.exe
echo Копіюй усю теку dist\WoTB Stats by Clark — не один exe.
echo data\ і chrome_profile\ з'являться поруч із exe при першому запуску.
echo Запасний запуск із джерел: запуск.bat
echo.
pause
exit /b 0

:fail
echo.
echo Збірка не вдалась.
pause
exit /b 1
