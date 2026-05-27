@echo off
cd /d "%~dp0"
set "PYTHON_EXE=.venv\Scripts\python.exe"
echo Starting YouTube Toolkit...

if not exist "%PYTHON_EXE%" (
    echo ERROR: Virtual environment not found. Run setup.bat first.
    pause
    exit /b 1
)

"%PYTHON_EXE%" main.py
if errorlevel 1 pause
