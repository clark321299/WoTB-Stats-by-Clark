@echo off
REM Спільна активація venv для Cl-Apps на Windows (дзеркало activate_venv.sh).
REM Виклик: call "%~dp0activate_venv.bat"

set "CL_APPS_ROOT=%~dp0"
if "%CL_APPS_ROOT:~-1%"=="\" set "CL_APPS_ROOT=%CL_APPS_ROOT:~0,-1%"

set "_CL_VENV="
if defined CL_APPS_VENV if exist "%CL_APPS_VENV%\Scripts\activate.bat" set "_CL_VENV=%CL_APPS_VENV%"
if not defined _CL_VENV if exist "%CL_APPS_ROOT%\.cl_apps_venv" (
  set /p _CL_VENV=<"%CL_APPS_ROOT%\.cl_apps_venv"
)
if not defined _CL_VENV if exist "%CL_APPS_ROOT%\.venv\Scripts\activate.bat" set "_CL_VENV=%CL_APPS_ROOT%\.venv"
if not defined _CL_VENV if exist "%LOCALAPPDATA%\cl-apps\venv\Scripts\activate.bat" (
  set "_CL_VENV=%LOCALAPPDATA%\cl-apps\venv"
)

if defined _CL_VENV if exist "%_CL_VENV%\Scripts\activate.bat" (
  call "%_CL_VENV%\Scripts\activate.bat"
)
set "_CL_VENV="
