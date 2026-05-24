@echo off
setlocal EnableDelayedExpansion
title Telegram Collector — Setup & Control

:: ============================================================
::  Telegram Media Intelligence Platform — Setup & Control
::  Run this file to set up, start, stop, or manage the system
:: ============================================================

:MAIN_MENU
cls
echo.
echo  ============================================================
echo   Telegram Media Intelligence Platform
echo  ============================================================
echo.
echo   SETUP
echo   [1] Check requirements  (Docker, Git, Python)
echo   [2] First-time setup    (copy .env, build images)
echo   [3] Edit configuration  (open .env in Notepad)
echo.
echo   CONTROL
echo   [4] Start all services
echo   [5] Stop all services
echo   [6] Restart all services
echo   [7] View live logs
echo.
echo   STATUS
echo   [8] Show running containers
echo   [9] Show dashboard URLs
echo.
echo   MAINTENANCE
echo   [10] Pull latest code (git pull)
echo   [11] Rebuild images (after code changes)
echo   [12] Reset database  (WARNING: deletes all data)
echo.
echo   [0] Exit
echo.
set /p CHOICE="  Enter option: "

if "%CHOICE%"=="1"  goto CHECK_REQUIREMENTS
if "%CHOICE%"=="2"  goto FIRST_TIME_SETUP
if "%CHOICE%"=="3"  goto EDIT_CONFIG
if "%CHOICE%"=="4"  goto START_SERVICES
if "%CHOICE%"=="5"  goto STOP_SERVICES
if "%CHOICE%"=="6"  goto RESTART_SERVICES
if "%CHOICE%"=="7"  goto VIEW_LOGS
if "%CHOICE%"=="8"  goto SHOW_STATUS
if "%CHOICE%"=="9"  goto SHOW_URLS
if "%CHOICE%"=="10" goto GIT_PULL
if "%CHOICE%"=="11" goto REBUILD
if "%CHOICE%"=="12" goto RESET_DB
if "%CHOICE%"=="0"  goto EXIT
echo   Invalid option. Press any key to try again.
pause >nul
goto MAIN_MENU


:: ============================================================
::  [1] CHECK REQUIREMENTS
:: ============================================================
:CHECK_REQUIREMENTS
cls
echo.
echo  ============================================================
echo   Checking Requirements
echo  ============================================================
echo.

:: Check Docker
echo   Checking Docker...
docker --version >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo   [MISSING] Docker is NOT installed.
    echo.
    echo   Please install Docker Desktop from:
    echo   https://www.docker.com/products/docker-desktop/
    echo.
    echo   After installing, restart this script.
) else (
    for /f "tokens=*" %%v in ('docker --version 2^>^&1') do echo   [OK]      %%v
)

:: Check Docker is running
echo   Checking Docker daemon...
docker info >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo   [STOPPED] Docker Desktop is installed but NOT running.
    echo             Please open Docker Desktop and wait for it to start.
) else (
    echo   [OK]      Docker daemon is running.
)

:: Check Docker Compose
echo   Checking Docker Compose...
docker compose version >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo   [MISSING] Docker Compose v2 not found.
    echo             Update Docker Desktop to get Compose v2 included.
) else (
    for /f "tokens=*" %%v in ('docker compose version 2^>^&1') do echo   [OK]      %%v
)

:: Check Git
echo   Checking Git...
git --version >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo   [MISSING] Git is NOT installed.
    echo             Download from: https://git-scm.com/download/win
) else (
    for /f "tokens=*" %%v in ('git --version 2^>^&1') do echo   [OK]      %%v
)

:: Check .env file
echo   Checking .env file...
if exist ".env" (
    echo   [OK]      .env file found.
) else (
    echo   [MISSING] .env file not found. Run option [2] First-time setup.
)

echo.
echo  ============================================================
echo   Summary: Fix any [MISSING] or [STOPPED] items above
echo   before running the system.
echo  ============================================================
echo.
pause
goto MAIN_MENU


:: ============================================================
::  [2] FIRST-TIME SETUP
:: ============================================================
:FIRST_TIME_SETUP
cls
echo.
echo  ============================================================
echo   First-Time Setup
echo  ============================================================
echo.

:: Step 1 — Check Docker
docker info >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo   ERROR: Docker is not running. Please start Docker Desktop first.
    echo.
    pause
    goto MAIN_MENU
)

:: Step 2 — Copy .env if missing
if not exist ".env" (
    if exist ".env.template" (
        echo   [1/4] Creating .env from template...
        copy ".env.template" ".env" >nul
        echo         Done. You MUST edit .env before starting.
        echo         Opening .env in Notepad now...
        echo.
        notepad .env
        echo.
        echo   Please fill in at minimum:
        echo     TG_API_ID       — from https://my.telegram.org
        echo     TG_API_HASH     — from https://my.telegram.org
        echo     BOT_TOKEN       — from @BotFather on Telegram
        echo     HUB_GROUP_ID    — your Telegram group ID
        echo     DB_PASSWORD     — choose a strong password
        echo.
        pause
    ) else (
        echo   ERROR: .env.template not found. Is this the right folder?
        pause
        goto MAIN_MENU
    )
) else (
    echo   [1/4] .env already exists — skipping copy.
)

:: Step 3 — Create required directories
echo   [2/4] Creating required directories...
if not exist "sessions"  mkdir sessions
if not exist "media"     mkdir media
if not exist "logs"      mkdir logs
echo         Done.

:: Step 4 — Pull base images
echo   [3/4] Pulling Docker base images (this may take a few minutes)...
docker compose pull postgres redis 2>&1
echo         Done.

:: Step 5 — Build application images
echo   [4/4] Building application images (this may take 10-20 minutes)...
echo         The face recognition model (~330MB) will be downloaded.
echo.
docker compose build
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo   ERROR: Build failed. Check the output above for details.
    echo   Common fixes:
    echo     - Make sure Docker Desktop has enough memory (8GB+ recommended)
    echo     - Check your internet connection
    echo     - Try running: docker compose build --no-cache
    echo.
    pause
    goto MAIN_MENU
)

echo.
echo  ============================================================
echo   Setup complete!
echo.
echo   Next steps:
echo   1. Make sure .env is fully configured (option [3])
echo   2. Start the system with option [4]
echo   3. Open a browser to http://localhost:8500 for the dashboard
echo  ============================================================
echo.
pause
goto MAIN_MENU


:: ============================================================
::  [3] EDIT CONFIGURATION
:: ============================================================
:EDIT_CONFIG
if exist ".env" (
    notepad .env
) else (
    echo   .env not found. Run option [2] First-time setup first.
    pause
)
goto MAIN_MENU


:: ============================================================
::  [4] START SERVICES
:: ============================================================
:START_SERVICES
cls
echo.
echo   Starting all services...
echo   (This may take 30-60 seconds on first start)
echo.
docker info >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo   ERROR: Docker is not running. Please start Docker Desktop first.
    pause
    goto MAIN_MENU
)
docker compose up -d
if %ERRORLEVEL% EQU 0 (
    echo.
    echo   Services started successfully!
    echo.
    echo   Dashboards will be available at:
    echo     Index:          http://localhost:8500
    echo     Collector:      http://localhost:8501
    echo     Face Recog:     http://localhost:8502
    echo     User Intel:     http://localhost:8503
    echo     Link Discovery: http://localhost:8504
    echo     Bulk Sender:    http://localhost:8505
    echo.
    echo   Note: Services may take 1-2 minutes to fully initialise.
    echo   Use option [8] to check container status.
) else (
    echo.
    echo   ERROR: Some services failed to start.
    echo   Run option [7] to view logs for details.
)
echo.
pause
goto MAIN_MENU


:: ============================================================
::  [5] STOP SERVICES
:: ============================================================
:STOP_SERVICES
cls
echo.
echo   Stopping all services...
echo.
docker compose down
echo.
echo   All services stopped.
echo.
pause
goto MAIN_MENU


:: ============================================================
::  [6] RESTART SERVICES
:: ============================================================
:RESTART_SERVICES
cls
echo.
echo   Restarting all services...
echo.
docker compose restart
echo.
echo   Done.
echo.
pause
goto MAIN_MENU


:: ============================================================
::  [7] VIEW LIVE LOGS
:: ============================================================
:VIEW_LOGS
cls
echo.
echo   Which service logs do you want to view?
echo.
echo   [1] All services
echo   [2] Collector
echo   [3] Face Recognition
echo   [4] User Intelligence
echo   [5] Link Discovery
echo   [6] Bulk Sender
echo   [7] Login Bot
echo   [8] Database (Postgres)
echo   [0] Back
echo.
set /p LOG_CHOICE="  Enter option: "

if "%LOG_CHOICE%"=="1" docker compose logs -f --tail=100
if "%LOG_CHOICE%"=="2" docker compose logs -f --tail=100 collector
if "%LOG_CHOICE%"=="3" docker compose logs -f --tail=100 face_recognition
if "%LOG_CHOICE%"=="4" docker compose logs -f --tail=100 user_intelligence
if "%LOG_CHOICE%"=="5" docker compose logs -f --tail=100 link_discovery
if "%LOG_CHOICE%"=="6" docker compose logs -f --tail=100 bulk_sender
if "%LOG_CHOICE%"=="7" docker compose logs -f --tail=100 login_bot
if "%LOG_CHOICE%"=="8" docker compose logs -f --tail=100 postgres
if "%LOG_CHOICE%"=="0" goto MAIN_MENU
goto MAIN_MENU


:: ============================================================
::  [8] SHOW STATUS
:: ============================================================
:SHOW_STATUS
cls
echo.
echo  ============================================================
echo   Running Containers
echo  ============================================================
echo.
docker compose ps
echo.
pause
goto MAIN_MENU


:: ============================================================
::  [9] SHOW DASHBOARD URLS
:: ============================================================
:SHOW_URLS
cls
echo.
echo  ============================================================
echo   Dashboard URLs
echo  ============================================================
echo.
echo   Open these in your browser:
echo.
echo   Index (all dashboards):  http://localhost:8500
echo   Collector:               http://localhost:8501
echo   Face Recognition:        http://localhost:8502
echo   User Intelligence:       http://localhost:8503
echo   Link Discovery:          http://localhost:8504
echo   Bulk Sender:             http://localhost:8505
echo.
echo   If a port shows as 0, the service auto-assigned a port.
echo   Check option [8] for the actual port mapping.
echo.
pause
goto MAIN_MENU


:: ============================================================
::  [10] GIT PULL
:: ============================================================
:GIT_PULL
cls
echo.
echo   Pulling latest code from GitHub...
echo.
git pull
echo.
echo   Done. Run option [11] to rebuild images with the new code.
echo.
pause
goto MAIN_MENU


:: ============================================================
::  [11] REBUILD IMAGES
:: ============================================================
:REBUILD
cls
echo.
echo   Rebuilding Docker images with latest code...
echo   (This may take several minutes)
echo.
docker compose build
if %ERRORLEVEL% EQU 0 (
    echo.
    echo   Build complete. Run option [6] to restart services.
) else (
    echo.
    echo   Build failed. Check the output above.
)
echo.
pause
goto MAIN_MENU


:: ============================================================
::  [12] RESET DATABASE
:: ============================================================
:RESET_DB
cls
echo.
echo  ============================================================
echo   WARNING: Reset Database
echo  ============================================================
echo.
echo   This will DELETE ALL DATA including:
echo     - All collected messages and media
echo     - All face recognition data
echo     - All user intelligence data
echo     - All Telegram accounts (you will need to re-register)
echo.
echo   The .env configuration will NOT be deleted.
echo.
set /p CONFIRM="   Type YES to confirm, or anything else to cancel: "
if /i "%CONFIRM%"=="YES" (
    echo.
    echo   Stopping services...
    docker compose down
    echo   Removing database volume...
    docker volume rm telegramcollector_postgres 2>nul
    echo   Removing Redis volume...
    docker volume rm telegramcollector_redis 2>nul
    echo.
    echo   Database reset complete.
    echo   Run option [4] to start fresh.
) else (
    echo.
    echo   Cancelled. No data was deleted.
)
echo.
pause
goto MAIN_MENU


:: ============================================================
::  [0] EXIT
:: ============================================================
:EXIT
echo.
echo   Goodbye!
echo.
exit /b 0
