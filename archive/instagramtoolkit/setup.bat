@echo off
cd /d "%~dp0"
set "PYTHON_EXE=.venv\Scripts\python.exe"
REM Setup script for Unified Instagram Toolkit (Windows)
title Instagram Toolkit - Setup

echo.
echo 🚀 Setting up Unified Instagram Toolkit
echo =====================================
echo.

REM Check if Python is installed
echo 🐍 Checking Python installation...
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
        echo ⚠️  Existing .venv is broken. Recreating...
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

"%PYTHON_EXE%" --version
echo ✅ Using local virtual environment
echo.

REM Install required packages
echo 📦 Installing required packages...
"%PYTHON_EXE%" -m pip install -r requirements.txt
if %ERRORLEVEL% neq 0 (
    echo ❌ Package installation failed
    echo.
    echo 💡 Troubleshooting:
    echo 1. Check your internet connection
    echo 2. Try running as administrator
    echo 3. Update Python: python -m pip install --upgrade pip
    pause
    exit /b 1
)

echo ✅ Packages installed successfully
echo.

REM Verify key packages
echo 🧪 Verifying installation...
"%PYTHON_EXE%" -c "import instaloader; print(f'  ✅ Instaloader {instaloader.__version__}')"
if %ERRORLEVEL% neq 0 (
    echo ❌ Instaloader verification failed
    pause
    exit /b 1
)
"%PYTHON_EXE%" -c "import dotenv; print(f'  ✅ python-dotenv')"
if %ERRORLEVEL% neq 0 (
    echo ❌ python-dotenv verification failed
    pause
    exit /b 1
)
echo.

REM Create data directories
echo 📁 Creating data directories...
if not exist "data" mkdir data
if not exist "downloads" mkdir downloads
if not exist "sessions" mkdir sessions
if not exist "archived_logs" mkdir archived_logs
echo ✅ Directories created
echo.

REM Quick import test
echo 🧪 Testing toolkit imports...
"%PYTHON_EXE%" -c "import sys; sys.path.insert(0, 'src'); from config import INSTAGRAM_ACCOUNTS; print(f'  ✅ Toolkit loaded — {len(INSTAGRAM_ACCOUNTS)} accounts configured')"
if %ERRORLEVEL% neq 0 (
    echo ❌ Toolkit import test failed
    pause
    exit /b 1
)
echo.

echo ✅ Setup completed successfully!
echo.
echo 📋 Next steps:
echo 1. Edit .env to add your Instagram account credentials
echo 2. Run start_toolkit.bat to begin using the toolkit
echo 3. Use quick_actions.bat for quick operations
echo.
echo 📁 Project structure:
echo    main.py          - CLI entrypoint
echo    src/             - Python library modules
echo    scripts/         - Standalone utility scripts
echo    data/            - Collected data (JSON, CSV, TXT)
echo    sessions/        - Instaloader session files
echo    web/             - Dashboard interface
echo.
echo 📖 See README.md for detailed usage instructions
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
echo ❌ Could not find a usable Python 3 interpreter in PATH.
exit /b 1
