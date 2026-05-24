@echo off
echo ========================================
echo    UNIFIED SEARCH TOOLKIT v2
echo    Enhanced with Tor, caching, and state persistence
echo ========================================
echo.

REM Change to the project directory (parent of script directory)
cd /d "%~dp0\.."
set "PYTHON_EXE=.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
    echo ERROR: .venv not found. Please run scripts\setup.bat first
    pause
    exit /b 1
)

REM Check if Python is available
"%PYTHON_EXE%" -V >nul 2>&1
if errorlevel 1 (
    echo ERROR: .venv is not usable. Please run scripts\setup.bat first
    pause
    exit /b 1
)

REM Show help if requested
if "%~1"=="--help" goto show_help
if "%~1"=="-h" goto show_help

REM If no arguments, show guidance to use quick_actions.bat
if "%~1"=="" (
    echo.
    echo    💡 For the best experience, use:
    echo       scripts\quick_actions.bat
    echo.
    echo    This will show you an interactive menu with all options.
    echo.
    echo    Or run directly with arguments:
    echo       "%PYTHON_EXE%" -m searchtoolkit.app --help
    echo.
    echo    Starting interactive mode in 3 seconds...
    echo.
    timeout /t 3 /nobreak >nul
    echo.
)

REM Run the toolkit
if "%~1"=="" (
    "%PYTHON_EXE%" main.py
) else (
    echo Starting with arguments: %*
    echo.
    "%PYTHON_EXE%" main.py %*
)

echo.
echo ========================================
echo Search Toolkit has finished
echo ========================================
pause
goto end

:show_help
echo.
echo    Usage: main.py [OPTIONS]  (or use .venv\Scripts\python.exe main.py [OPTIONS])
echo.
echo    Core Options:
echo      --mode {1,2,3}        Operation mode
echo      --query QUERY         Search query
echo      --dorks-file FILE     Dorks file (mode 3)
echo.
echo    Enhanced Features:
echo      --use-tor             Enable Tor proxy
echo      --resume              Resume from checkpoint
echo      --state-dir PATH      Custom state directory
echo      --cache-ttl HOURS     Cache TTL (default: 24)
echo      --no-cache            Disable caching
echo      --rate-limit-delay S  Request delay (default: 2.0)
echo      --output-dir PATH     Output directory
echo.
echo    Examples:
echo      .venv\Scripts\python.exe main.py --mode 1 --query "test" --use-tor --resume
echo      .venv\Scripts\python.exe main.py --mode 2 --query "cat,dog"
echo      .venv\Scripts\python.exe main.py --mode 3 --dorks-file data\search.txt
echo.
echo    For interactive menu, run: scripts\quick_actions.bat
echo.
pause
goto end

:end
