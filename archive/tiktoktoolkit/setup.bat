@echo off
cd /d "%~dp0"
set "PYTHON_EXE=.venv\Scripts\python.exe"
REM Main setup for Unified TikTok Toolkit
setlocal

echo ====================================
echo   Unified TikTok Toolkit Setup
echo ====================================
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

echo [1/2] Using local virtual environment...
"%PYTHON_EXE%" -m pip install --upgrade pip

echo.
echo [2/2] Installing requirements...
"%PYTHON_EXE%" -m pip install -r requirements.txt

echo.
echo [3/4] Installing Playwright browsers...
echo This may take a few minutes on first run...
"%PYTHON_EXE%" -m playwright install chromium
if errorlevel 1 (
    echo [WARN] Playwright browser installation failed. Browser automation may not work.
    echo You can install manually later with: playwright install chromium
) else (
    echo [OK] Playwright browsers installed successfully
)

echo.
echo [4/4] Verifying core dependencies...
"%PYTHON_EXE%" -c "import click, pydantic, yaml; print('+ Core dependencies OK')" || echo "! Core dependency issue"
"%PYTHON_EXE%" -c "import gallery_dl; print('+ gallery-dl available')" || echo "! gallery-dl issue"
"%PYTHON_EXE%" -c "from playwright.sync_api import sync_playwright; print('+ Playwright available')" || echo "! Playwright issue"
"%PYTHON_EXE%" -c "import rookiepy; print('+ rookiepy available (Chrome cookie decryption supported)')" || echo "! rookiepy issue - cookie extraction may fail"
"%PYTHON_EXE%" -c "import yt_dlp; print('+ yt-dlp available (v' + yt_dlp.version.__version__ + ')')" || echo "! yt-dlp issue - fallback downloader unavailable"
"%PYTHON_EXE%" -c "import curl_cffi; print('+ curl-cffi available (TLS impersonation supported)')" || echo "! curl-cffi issue - yt-dlp impersonation may not work"

echo.
echo ====================================
echo ✓ Setup completed successfully!
echo ====================================
echo.
echo Dependencies installed in .venv.
echo.
echo You can now use the toolkit:
echo  + start_toolkit.bat  - Main menu with all download and utility options
echo.
echo Press any key to exit...
pause > nul

endlocal
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
