@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "VTW_PYTHON=%~dp0backend\.venv\Scripts\python.exe"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

if not exist "%VTW_PYTHON%" (
    echo [ERROR] Python virtual environment was not found.
    echo Follow README.md to install backend dependencies first.
    pause
    exit /b 1
)

"%VTW_PYTHON%" "%~dp0launcher.py" %*

if errorlevel 1 (
    echo.
    echo Startup failed. Check the error message above.
    pause
)

endlocal
