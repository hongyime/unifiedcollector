@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
chcp 65001 >nul

REM Check for virtual environment
if not exist ".venv\Scripts\python.exe" (
    echo ERROR: Virtual environment not found.
    echo Please run setup.bat first.
    pause
    exit /b 1
)

REM Activate virtual environment
call .venv\Scripts\activate.bat

REM Check for .env file
if not exist ".env" (
    echo WARNING: .env file not found.
    echo Copying .env.template to .env...
    copy .env.template .env >nul
    echo.
    echo Please edit .env file to configure your settings.
    echo Press any key to continue with defaults...
    pause >nul
)

REM Run main.py
python main.py

REM Deactivate on exit
deactivate
