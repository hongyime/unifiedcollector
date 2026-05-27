@echo off
cd /d "%~dp0"
set "PYTHON_EXE=.venv\Scripts\python.exe"
set "PYTHONIOENCODING=utf-8"
echo Installing Lemon8 Toolkit Dependencies...
echo ===============================================

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

echo Using local virtual environment...

REM Install dependencies from requirements.txt
"%PYTHON_EXE%" -m pip install --upgrade pip
"%PYTHON_EXE%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: Failed to install dependencies!
    pause
    exit /b 1
)

echo.
echo Setup complete!
echo.
echo To start the toolkit, run:
echo   start_toolkit.bat
echo.
if /I "%~1"=="--no-pause" exit /b 0
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
