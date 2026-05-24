@echo off
setlocal enabledelayedexpansion
title WhatsApp Collector — Setup Menu

:MENU
cls
echo.
echo  ============================================================
echo   WhatsApp Collector — Setup ^& Management Menu
echo  ============================================================
echo.
echo   REQUIREMENTS (must be installed before using this tool):
echo     - Docker Desktop  https://www.docker.com/products/docker-desktop
echo     - Python 3.12+    https://www.python.org/downloads/
echo.
echo  ============================================================
echo.
echo   [1]  Check requirements (Docker, Python, ports)
echo   [2]  First-time setup  (copy .env, run pre-build checks)
echo   [3]  Build images      (docker compose build)
echo   [4]  Start stack       (docker compose up)
echo   [5]  Stop stack        (docker compose down)
echo   [6]  Run DB migrations
echo   [7]  View logs         (all services)
echo   [8]  View service status
echo   [9]  Open dashboard index in browser
echo   [0]  Exit
echo.
set /p CHOICE="  Enter option: "

if "%CHOICE%"=="1" goto CHECK_REQS
if "%CHOICE%"=="2" goto FIRST_SETUP
if "%CHOICE%"=="3" goto BUILD
if "%CHOICE%"=="4" goto START
if "%CHOICE%"=="5" goto STOP
if "%CHOICE%"=="6" goto MIGRATE
if "%CHOICE%"=="7" goto LOGS
if "%CHOICE%"=="8" goto STATUS
if "%CHOICE%"=="9" goto OPEN_DASH
if "%CHOICE%"=="0" goto EXIT

echo   Invalid option. Try again.
timeout /t 2 >nul
goto MENU

:: ─────────────────────────────────────────────────────────────────────────────
:CHECK_REQS
cls
echo.
echo  Checking requirements...
echo  ─────────────────────────────────────────────────────────────
echo.

:: Check Docker
docker --version >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo  [MISSING]  Docker is NOT installed or not running.
    echo             Download: https://www.docker.com/products/docker-desktop
) else (
    for /f "tokens=*" %%v in ('docker --version 2^>^&1') do echo  [OK]       %%v
)

:: Check Docker Compose
docker compose version >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo  [MISSING]  Docker Compose not available. Update Docker Desktop.
) else (
    for /f "tokens=*" %%v in ('docker compose version 2^>^&1') do echo  [OK]       %%v
)

:: Check Python
python --version >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo  [MISSING]  Python is NOT installed.
    echo             Download: https://www.python.org/downloads/
) else (
    for /f "tokens=*" %%v in ('python --version 2^>^&1') do echo  [OK]       %%v
)

:: Check .env file
if exist ".env" (
    echo  [OK]       .env file found.
) else (
    echo  [MISSING]  .env file not found. Run option [2] to create it.
)

echo.
echo  ─────────────────────────────────────────────────────────────
echo.
echo  Running full pre-build check script...
echo.
powershell -ExecutionPolicy Bypass -File "infrastructure\scripts\pre_build_check.ps1"
echo.
pause
goto MENU

:: ─────────────────────────────────────────────────────────────────────────────
:FIRST_SETUP
cls
echo.
echo  First-Time Setup
echo  ─────────────────────────────────────────────────────────────
echo.

if not exist ".env" (
    if exist ".env.template" (
        copy ".env.template" ".env" >nul
        echo  [DONE]  Copied .env.template to .env
        echo.
        echo  IMPORTANT: Open .env in a text editor and replace ALL values
        echo             marked CHANGE_ME_* with strong random secrets before
        echo             continuing. Do NOT skip this step.
        echo.
        echo  Tip: Use a password manager or run this in PowerShell to generate
        echo       a secret:  [System.Web.Security.Membership]::GeneratePassword(32,4)
        echo.
    ) else (
        echo  [ERROR]  .env.template not found. Cannot create .env automatically.
    )
) else (
    echo  [SKIP]  .env already exists. Delete it first if you want a fresh copy.
)

echo.
echo  Checking for CHANGE_ME_ secrets still in .env...
findstr /i "CHANGE_ME_" .env >nul 2>&1
if %ERRORLEVEL% equ 0 (
    echo  [WARNING]  Found unrotated CHANGE_ME_ secrets in .env!
    echo             Edit .env and replace them before starting the stack.
) else (
    echo  [OK]  No CHANGE_ME_ secrets found in .env.
)

echo.
echo  Running pre-build validation...
echo.
powershell -ExecutionPolicy Bypass -File "infrastructure\scripts\pre_build_check.ps1"
echo.
echo  ─────────────────────────────────────────────────────────────
echo  Next steps:
echo    1. Edit .env and set EXTERNAL_STORAGE_ROOT to your HDD path
echo       e.g.  EXTERNAL_STORAGE_ROOT=D:\whatsapp_data
echo    2. Run option [3] to build images
echo    3. Run option [4] to start the stack
echo    4. Run option [6] to apply database migrations
echo  ─────────────────────────────────────────────────────────────
echo.
pause
goto MENU

:: ─────────────────────────────────────────────────────────────────────────────
:BUILD
cls
echo.
echo  Building Docker images...
echo  This may take several minutes on first run.
echo.
docker compose build
if %ERRORLEVEL% neq 0 (
    echo.
    echo  [ERROR]  Build failed. Check the output above for details.
) else (
    echo.
    echo  [DONE]  Build complete. Run option [4] to start the stack.
)
echo.
pause
goto MENU

:: ─────────────────────────────────────────────────────────────────────────────
:START
cls
echo.
echo  Starting the stack...
echo.
powershell -ExecutionPolicy Bypass -File "start.ps1"
if %ERRORLEVEL% neq 0 (
    echo.
    echo  [ERROR]  Start failed. Check the output above.
) else (
    echo.
    echo  [DONE]  Stack started. Allow 2-3 minutes for health checks to pass.
    echo          Run option [8] to check service status.
    echo          Run option [9] to open the dashboard.
)
echo.
pause
goto MENU

:: ─────────────────────────────────────────────────────────────────────────────
:STOP
cls
echo.
echo  Stopping the stack...
echo.
docker compose down
echo.
echo  [DONE]  Stack stopped.
echo.
pause
goto MENU

:: ─────────────────────────────────────────────────────────────────────────────
:MIGRATE
cls
echo.
echo  Running database migrations...
echo.
python infrastructure\scripts\run_migrations.py
if %ERRORLEVEL% neq 0 (
    echo.
    echo  [ERROR]  Migration failed. Make sure the stack is running (option [4])
    echo           and the database is healthy (option [8]).
) else (
    echo.
    echo  [DONE]  Migrations applied successfully.
)
echo.
pause
goto MENU

:: ─────────────────────────────────────────────────────────────────────────────
:LOGS
cls
echo.
echo  Tailing logs for all services (Ctrl+C to stop)...
echo.
docker compose logs -f
echo.
pause
goto MENU

:: ─────────────────────────────────────────────────────────────────────────────
:STATUS
cls
echo.
echo  Service status:
echo.
docker compose ps
echo.
pause
goto MENU

:: ─────────────────────────────────────────────────────────────────────────────
:OPEN_DASH
cls
echo.
echo  Opening dashboard index in your browser...
echo  (Dashboard runs on http://localhost:8500)
echo.
start "" "http://localhost:8500"
timeout /t 2 >nul
goto MENU

:: ─────────────────────────────────────────────────────────────────────────────
:EXIT
echo.
echo  Goodbye!
echo.
endlocal
exit /b 0
