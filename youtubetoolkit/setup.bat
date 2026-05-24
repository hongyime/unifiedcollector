@echo off
cd /d "%~dp0"
set "PYTHON_EXE=.venv\Scripts\python.exe"
title YouTube Toolkit Setup
echo ============================================
echo   🔧 YOUTUBE TOOLKIT SETUP 🔧
echo ============================================
echo.
echo This script will install all required Python packages
echo for the Streamlined YouTube Toolkit.
echo.

if not exist "%PYTHON_EXE%" (
    call :resolve_base_python
    if errorlevel 1 exit /b 1
    if /I "%BASE_PYTHON_SOURCE%"=="py" (
        py -3 -m venv .venv
    ) else (
        python -m venv .venv
    )
    if errorlevel 1 exit /b 1
)

if exist "%PYTHON_EXE%" (
    "%PYTHON_EXE%" --version >nul 2>&1
    if errorlevel 1 (
        echo [WARN] Existing .venv is broken. Recreating...
        rmdir /s /q .venv
        call :resolve_base_python
        if errorlevel 1 exit /b 1
        if /I "%BASE_PYTHON_SOURCE%"=="py" (
            py -3 -m venv .venv
        ) else (
            python -m venv .venv
        )
        if errorlevel 1 exit /b 1
    )
)

echo [1/2] Upgrading pip...
"%PYTHON_EXE%" -m pip install --upgrade pip

echo.
echo [2/2] Installing all requirements from requirements.txt...
"%PYTHON_EXE%" -m pip install -r requirements.txt

echo.
echo ============================================
echo ✅ Setup complete!
echo ============================================
echo.
echo You can now safely run start_toolkit.bat
echo.
pause
exit /b 0

:resolve_base_python
set "BASE_PYTHON_SOURCE="
py -3 --version >nul 2>&1
if not errorlevel 1 (
    set "BASE_PYTHON_SOURCE=py"
    exit /b 0
)
python -c "import sys; raise SystemExit(0 if sys.version_info.major == 3 else 1)" >nul 2>&1
if not errorlevel 1 (
    set "BASE_PYTHON_SOURCE=python"
    exit /b 0
)
echo [ERROR] Could not find a usable Python 3 interpreter in PATH.
exit /b 1
