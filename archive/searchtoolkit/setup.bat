@echo off
echo ========================================
echo    Search Toolkit - Setup Script
echo    Enhanced Edition v2
echo ========================================
echo.

REM Change to the project directory (parent of script directory)
cd /d "%~dp0\.."
set "PYTHON_EXE=.venv\Scripts\python.exe"

echo Installing required Python packages...
echo.

REM Check if Python is installed
if not exist "%PYTHON_EXE%" (
    call :resolve_base_python
    if errorlevel 1 (
        echo ERROR: Failed to find a usable Python 3 interpreter
        pause
        exit /b 1
    )
    if /I "%BASE_PYTHON_SOURCE%"=="py" (
        py -3 -m venv .venv
    ) else (
        python -m venv .venv
    )
    if errorlevel 1 (
        echo ERROR: Failed to create .venv
        pause
        exit /b 1
    )
)

if exist "%PYTHON_EXE%" (
    "%PYTHON_EXE%" --version >nul 2>&1
    if errorlevel 1 (
        echo [WARN] Existing .venv is broken. Recreating...
        rmdir /s /q .venv
        call :resolve_base_python
        if errorlevel 1 (
            echo ERROR: Failed to find a usable Python 3 interpreter
            pause
            exit /b 1
        )
        if /I "%BASE_PYTHON_SOURCE%"=="py" (
            py -3 -m venv .venv
        ) else (
            python -m venv .venv
        )
        if errorlevel 1 (
            echo ERROR: Failed to recreate .venv
            pause
            exit /b 1
        )
    )
)

echo Using local virtual environment...
echo.

REM Upgrade pip first
echo Upgrading pip...
"%PYTHON_EXE%" -m pip install --upgrade pip

echo.
echo Installing required packages:
echo   • beautifulsoup4 - Web scraping and HTML parsing
echo   • requests - HTTP requests and sessions
echo   • Pillow - Image processing and conversion
echo   • colorama - Colored terminal output
echo   • lxml - Fast XML/HTML parsing
echo   • ddgs - DuckDuckGo search API
echo   • undetected-chromedriver - Chrome automation fallback
echo   • PyMuPDF - PDF extraction and conversion
echo.

REM Install packages from requirements.txt if it exists, otherwise install individually
if exist requirements.txt (
    echo Installing from requirements.txt...
    "%PYTHON_EXE%" -m pip install -r requirements.txt
) else (
    echo Installing packages individually...
    "%PYTHON_EXE%" -m pip install beautifulsoup4>=4.12.0
    "%PYTHON_EXE%" -m pip install requests>=2.31.0
    "%PYTHON_EXE%" -m pip install Pillow>=10.0.0
    "%PYTHON_EXE%" -m pip install colorama>=0.4.6
    "%PYTHON_EXE%" -m pip install lxml>=4.9.0
    "%PYTHON_EXE%" -m pip install ddgs==9.13.0
    "%PYTHON_EXE%" -m pip install undetected-chromedriver>=3.5.5
    "%PYTHON_EXE%" -m pip install PyMuPDF>=1.24.0
)

echo.
echo ========================================
echo Verifying installation...
echo ========================================

REM Test imports
"%PYTHON_EXE%" -c "import requests; print('  ✓ requests')" 2>nul || echo "  ✗ requests"
"%PYTHON_EXE%" -c "import bs4; print('  ✓ beautifulsoup4')" 2>nul || echo "  ✗ beautifulsoup4"
"%PYTHON_EXE%" -c "import Pillow; print('  ✓ Pillow')" 2>nul || echo "  ✗ Pillow"
"%PYTHON_EXE%" -c "import colorama; print('  ✓ colorama')" 2>nul || echo "  ✗ colorama"
"%PYTHON_EXE%" -c "import lxml; print('  ✓ lxml')" 2>nul || echo "  ✗ lxml"
"%PYTHON_EXE%" -c "from ddgs import DDGS; print('  ✓ ddgs')" 2>nul || echo "  ✗ ddgs"
"%PYTHON_EXE%" -c "import undetected_chromedriver; print('  ✓ undetected-chromedriver')" 2>nul || echo "  ✗ undetected-chromedriver"
"%PYTHON_EXE%" -c "import fitz; print('  ✓ PyMuPDF')" 2>nul || echo "  ✗ PyMuPDF"

echo.
echo ========================================
echo Creating required folders...
echo ========================================

if not exist "downloads" mkdir downloads
echo   ✓ downloads/ - Your downloaded files will go here

if not exist "state" mkdir state
if not exist "state\cache" mkdir state\cache
echo   ✓ state/ - Progress tracking and caching database
echo   ✓ state/cache/ - Search result cache

echo.
echo ========================================
echo ✅ Setup completed successfully!
echo ========================================
echo.
echo 🚀 Quick Start:
echo   1. Run: scripts\quick_actions.bat
echo      (Interactive menu - recommended for first time)
echo.
echo   2. Or run: scripts\start_toolkit.bat
echo      (Direct launch with guidance)
echo.
echo 📚 Documentation:
echo   • README.md - Full feature documentation
echo   • API_SETUP_GUIDE.md - API key setup instructions
echo.
echo 🛠️  Advanced Usage (Command-line):
echo   • .venv\Scripts\python.exe -m searchtoolkit.app --help
echo     (Show all CLI options for automation)
echo.
echo   • Example: .venv\Scripts\python.exe -m searchtoolkit.app --mode 1 --query "test" --use-tor --resume
echo.
echo ========================================
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
echo ERROR: Could not find a usable Python 3 interpreter in PATH.
exit /b 1
