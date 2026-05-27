@echo off
cd /d "%~dp0"
set "PYTHON_EXE=.venv\Scripts\python.exe"
echo.
echo ===============================================
echo    UNIFIED WEBSITE TOOLKIT - QUICK SETUP
echo ===============================================
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

echo Installing required dependencies...
"%PYTHON_EXE%" -m pip install -r requirements.txt

echo.
echo ===============================================
echo    SETUP COMPLETE!
echo ===============================================
echo.
echo The toolkit is now ready to use with these options:
echo.
echo MAIN LAUNCHER:
echo   start_toolkit.bat   - Interactive menu with all features
echo.
echo QUICK START:
echo   1. Run start_toolkit.bat for the main menu
echo   2. Use menu option 3 (Bulk Import) to add many websites
echo   3. Edit sample_import.txt with your site list
echo.
echo SAMPLE FILES:
echo   sample_import.txt - Template for bulk imports
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
echo [ERROR] Could not find a usable Python 3 interpreter in PATH.
exit /b 1