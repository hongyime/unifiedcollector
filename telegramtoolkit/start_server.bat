@echo off
echo ========================================
echo   SIMPLE SERVER - DIRECT ACCESS
echo ========================================
echo.
echo 🚀 Starting simple HTTP server...
echo 💡 This is the reliable fallback server
echo.
echo 🌐 Server will be available at:
echo   Dashboard:  http://localhost:8000/web/enhanced_dashboard.html
echo 🔗 Visualizer: http://localhost:8000/web/visualize.html
echo.
echo Press Ctrl+C to stop the server
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

"%PYTHON_EXE%" -m src.server.simple_server no-browser

pause
