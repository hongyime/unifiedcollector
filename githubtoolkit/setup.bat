@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo ======================================================
echo GitHub Toolkit - Setup
echo ======================================================
echo.

REM Check for Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Please install Python 3.8 or higher.
    pause
    exit /b 1
)

REM Create virtual environment if it doesn't exist
if not exist ".venv" (
    echo Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo ERROR: Failed to create virtual environment.
        pause
        exit /b 1
    )
    echo Virtual environment created.
) else (
    echo Virtual environment already exists.
)

REM Activate virtual environment
call .venv\Scripts\activate.bat

REM Upgrade pip
echo.
echo Upgrading pip...
python -m pip install --upgrade pip -q

REM Install requirements
echo.
echo Installing dependencies...
pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: Failed to install dependencies.
    pause
    exit /b 1
)

echo.
echo ======================================================
echo Setup Complete!
echo ======================================================
echo.
echo Next steps:
echo 1. Copy .env.template to .env and configure settings
echo 2. Run start_toolkit.bat to launch the toolkit
echo.
pause
