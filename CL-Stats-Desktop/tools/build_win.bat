@echo off
REM One-dir PyInstaller: dist\WoTB Stats by Clark\WoTB Stats by Clark.exe
REM Лише Python 3.12. У 3.14/3.15 немає готового lz4, pip збирає його з сирців і падає без Visual C++.
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0.."

py -3.12 --version >nul 2>&1
if errorlevel 1 (
  echo Потрібен Python 3.12, команда py -3.12. Новіший Python для цієї збірки не підходить.
  pause
  exit /b 1
)

echo === Python 3.12 ===
if not exist ".venv312\Scripts\python.exe" (
  py -3.12 -m venv .venv312
  if errorlevel 1 goto :fail
)
call ".venv312\Scripts\activate.bat"
if errorlevel 1 goto :fail

python -c "import sys; raise SystemExit(0 if sys.version_info[:2]==(3,12) else 1)"
if errorlevel 1 (
  echo Тека .venv312 не на Python 3.12. Видали її і запусти цей файл ще раз.
  pause
  exit /b 1
)

echo === pip ===
python -m pip install --upgrade pip
if errorlevel 1 goto :fail
python -m pip install --only-binary=lz4 -r requirements.txt "pyinstaller>=6.6" pillow tzdata numpy
if errorlevel 1 goto :fail

echo === PyInstaller ===
python -m PyInstaller --noconfirm --clean "%~dp0wotb_stats.spec"
if errorlevel 1 goto :fail

echo.
echo Готово: dist\WoTB Stats by Clark\WoTB Stats by Clark.exe
echo Копіюй усю теку dist\WoTB Stats by Clark — не один exe.
echo.
pause
exit /b 0

:fail
echo.
echo Збірка не вдалась.
pause
exit /b 1
