@echo off
echo ========================================
echo   UNIFIED TELEGRAM TOOLKIT LAUNCHER
echo ========================================
echo.

cd /d "%~dp0"
set "PYTHON_EXE=.venv\Scripts\python.exe"

if exist "%PYTHON_EXE%" (
    "%PYTHON_EXE%" --version >nul 2>&1
    if errorlevel 1 (
        echo [WARN] Existing .venv is broken. Re-running setup...
        rmdir /s /q .venv
    )
)

if not exist "%PYTHON_EXE%" (
    echo [INFO] .venv not found. Running setup.bat...
    call setup.bat --no-pause
    if errorlevel 1 exit /b 1
    if not exist "%PYTHON_EXE%" (
        echo [ERROR] Setup did not produce a usable .venv.
        exit /b 1
    )
)

:: Check for HTTP server option
if "%1"=="dashboard" (
    echo.
    echo ========================================
    echo   STARTING DASHBOARD WITH SERVER
    echo ========================================
    echo 🚀 Starting HTTP server and opening dashboard...
    echo 💡 This ensures CSV files load correctly!
    echo.
    
    :: Try main.py integration first, fallback to simple server
    "%PYTHON_EXE%" -c "from main import TelegramToolkit; toolkit = TelegramToolkit(); toolkit.open_dashboard()" 2>nul
    if errorlevel 1 (
        echo 💡 Using fallback server...
        "%PYTHON_EXE%" -m src.server.simple_server dashboard
    )
    goto :end
)

if "%1"=="visualize" (
    echo.
    echo ========================================
    echo    STARTING VISUALIZER WITH SERVER
    echo ========================================
    echo 🚀 Starting HTTP server and opening visualizer...
    echo 💡 This ensures CSV files load correctly!
    echo.
    
    :: Try main.py integration first, fallback to simple server
    "%PYTHON_EXE%" -c "from main import TelegramToolkit; toolkit = TelegramToolkit(); toolkit.open_visualizer()" 2>nul
    if errorlevel 1 (
        echo 💡 Using fallback server...
        "%PYTHON_EXE%" -m src.server.simple_server visualize
    )
    goto :end
)

if "%1"=="server" (
    echo.
    echo ========================================
    echo       STARTING HTTP SERVER ONLY
    echo ========================================
    echo 🌐 Server will be available at:
    echo   Dashboard:  http://localhost:8000/web/enhanced_dashboard.html
    echo 🔗 Visualizer: http://localhost:8000/web/visualize.html
    echo.
    echo Press Ctrl+C to stop the server
    echo.
    "%PYTHON_EXE%" -m src.server.simple_server no-browser
    goto :end
)

:: Run the main toolkit
echo Starting Telegram Toolkit...
echo.
"%PYTHON_EXE%" main.py

:end
pause
