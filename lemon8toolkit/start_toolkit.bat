@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
set "PYTHON_EXE=.venv\Scripts\python.exe"
set "PYTHONIOENCODING=utf-8"

if not exist "%PYTHON_EXE%" (
    echo [INFO] .venv not found. Running setup.bat...
    call setup.bat --no-pause
    if errorlevel 1 (
        echo [ERROR] setup.bat failed.
        pause
        exit /b 1
    )
)

title Lemon8 Toolkit

:MAIN_MENU
cls
echo ========================================
echo        Lemon8 Toolkit - Main Menu
echo ========================================
echo.
echo [1] Scrape
echo [2] Download
echo [3] Profile History
echo [4] Network Graph
echo [5] Account Management
echo [6] Database
echo [7] System
echo.
echo [0] Exit
echo.
set /p "choice=Select option: "

if "%choice%"=="1" goto SCRAPE_MENU
if "%choice%"=="2" goto DOWNLOAD_MENU
if "%choice%"=="3" goto HISTORY_MENU
if "%choice%"=="4" goto GRAPH_MENU
if "%choice%"=="5" goto ACCOUNT_MENU
if "%choice%"=="6" goto DATABASE_MENU
if "%choice%"=="7" goto SYSTEM_MENU
if "%choice%"=="0" exit /b 0
goto MAIN_MENU

:SCRAPE_MENU
cls
echo ========================================
echo           Scrape Menu
echo ========================================
echo.
echo [1.1] Seed from For You page (then spider)
echo [1.2] Scrape user profile
echo [1.3] Scrape tag (#singapore etc.)
echo [1.4] Spider batch (all pending users)
echo [1.5] Force rescrape all tracked users
echo.
echo [0] Back to Main Menu
echo.
set /p "choice=Select option: "

if "%choice%"=="1.1" goto SEED_FEED
if "%choice%"=="1.2" goto SCRAPE_USER
if "%choice%"=="1.3" goto SCRAPE_TAG
if "%choice%"=="1.4" goto SPIDER_BATCH
if "%choice%"=="1.5" goto FORCE_RESCRAPE
if "%choice%"=="0" goto MAIN_MENU
goto SCRAPE_MENU

:SEED_FEED
cls
echo Seed from For You page
echo.
set /p "pages=Number of pages to scrape (default 10): "
if "%pages%"=="" set "pages=10"
set /p "download=Download media? (y/n, default n): "
if /i "%download%"=="y" (
    "%PYTHON_EXE%" main.py seed --pages %pages% --download
) else (
    "%PYTHON_EXE%" main.py seed --pages %pages%
)
pause
goto SCRAPE_MENU

:SCRAPE_USER
cls
echo Scrape user profile
echo.
set /p "username=Enter username (with or without @): "
if "%username%"=="" (
    echo Error: Username required
    pause
    goto SCRAPE_USER
)
set /p "download=Download media? (y/n, default y): "
if /i "%download%"=="n" (
    "%PYTHON_EXE%" main.py user %username%
) else (
    "%PYTHON_EXE%" main.py user %username% --download
)
pause
goto SCRAPE_MENU

:SCRAPE_TAG
cls
echo Scrape tag/topic
echo.
set /p "tag=Enter tag ID or keyword: "
if "%tag%"=="" (
    echo Error: Tag required
    pause
    goto SCRAPE_TAG
)
set /p "pages=Number of pages (default 10): "
if "%pages%"=="" set "pages=10"
set /p "download=Download media? (y/n, default y): "
if /i "%download%"=="n" (
    "%PYTHON_EXE%" main.py tag %tag% --pages %pages%
) else (
    "%PYTHON_EXE%" main.py tag %tag% --pages %pages% --download
)
pause
goto SCRAPE_MENU

:SPIDER_BATCH
cls
echo Spider batch of pending users
echo.
set /p "batch=Number of users to spider (default 10): "
if "%batch%"=="" set "batch=10"
set /p "download=Download media? (y/n, default y): "
if /i "%download%"=="n" (
    "%PYTHON_EXE%" main.py spider --batch %batch%
) else (
    "%PYTHON_EXE%" main.py spider --batch %batch% --download
)
pause
goto SCRAPE_MENU

:FORCE_RESCRAPE
cls
echo Force rescrape all tracked users
echo.
echo WARNING: This will rescrape ALL users in the database
set /p "confirm=Continue? (y/n): "
if /i not "%confirm%"=="y" goto SCRAPE_MENU
"%PYTHON_EXE%" main.py user --force --download
pause
goto SCRAPE_MENU

:DOWNLOAD_MENU
cls
echo ========================================
echo          Download Menu
echo ========================================
echo.
echo [2.1] Download pending media
echo [2.2] Reconcile (re-download missing files)
echo.
echo [0] Back to Main Menu
echo.
set /p "choice=Select option: "

if "%choice%"=="2.1" goto DOWNLOAD_PENDING
if "%choice%"=="2.2" goto RECONCILE
if "%choice%"=="0" goto MAIN_MENU
goto DOWNLOAD_MENU

:DOWNLOAD_PENDING
cls
echo Download pending media
echo.
set /p "limit=Limit (default 100): "
if "%limit%"=="" set "limit=100"
"%PYTHON_EXE%" main.py download-pending --limit %limit%
pause
goto DOWNLOAD_MENU

:RECONCILE
cls
echo Reconcile missing files
echo.
set /p "session=Session ID (leave empty for all): "
if "%session%"=="" (
    "%PYTHON_EXE%" main.py reconcile
) else (
    "%PYTHON_EXE%" main.py reconcile --session %session%
)
pause
goto DOWNLOAD_MENU

:HISTORY_MENU
cls
echo ========================================
echo        Profile History Menu
echo ========================================
echo.
echo [3.1] View follower/following history for user
echo [3.2] View profile photo change history
echo.
echo [0] Back to Main Menu
echo.
set /p "choice=Select option: "

if "%choice%"=="3.1" goto VIEW_USER_HISTORY
if "%choice%"=="3.2" goto VIEW_PHOTO_HISTORY
if "%choice%"=="0" goto MAIN_MENU
goto HISTORY_MENU

:VIEW_USER_HISTORY
cls
echo View user follower/following history
echo.
set /p "username=Enter username: "
if "%username%"=="" (
    echo Error: Username required
    pause
    goto HISTORY_MENU
)
set /p "limit=Number of snapshots (default 10): "
if "%limit%"=="" set "limit=10"
"%PYTHON_EXE%" main.py history user %username% --limit %limit%
pause
goto HISTORY_MENU

:VIEW_PHOTO_HISTORY
cls
echo View profile photo change history
echo.
set /p "username=Enter username: "
if "%username%"=="" (
    echo Error: Username required
    pause
    goto HISTORY_MENU
)
set /p "limit=Number of photos (default 10): "
if "%limit%"=="" set "limit=10"
"%PYTHON_EXE%" main.py history photo %username% --limit %limit%
pause
goto HISTORY_MENU

:GRAPH_MENU
cls
echo ========================================
echo         Network Graph Menu
echo ========================================
echo.
echo [4.1] Build graph (compute edges)
echo [4.2] View graph stats
echo.
echo [0] Back to Main Menu
echo.
set /p "choice=Select option: "

if "%choice%"=="4.1" goto BUILD_GRAPH
if "%choice%"=="4.2" goto GRAPH_STATS
if "%choice%"=="0" goto MAIN_MENU
goto GRAPH_MENU

:BUILD_GRAPH
cls
echo Build network graph
echo.
set /p "limit=Limit users to process (leave empty for all): "
if "%limit%"=="" (
    "%PYTHON_EXE%" main.py graph build
) else (
    "%PYTHON_EXE%" main.py graph build --limit %limit%
)
pause
goto GRAPH_MENU

:GRAPH_STATS
cls
echo Network graph statistics
echo.
"%PYTHON_EXE%" main.py graph stats
pause
goto GRAPH_MENU

:ACCOUNT_MENU
cls
echo ========================================
echo       Account Management Menu
echo ========================================
echo.
echo [5.1] List configured accounts
echo [5.2] Setup cookies for account
echo [5.3] View account cooldowns
echo [5.4] Test all accounts
echo.
echo [0] Back to Main Menu
echo.
set /p "choice=Select option: "

if "%choice%"=="5.1" goto LIST_ACCOUNTS
if "%choice%"=="5.2" goto SETUP_ACCOUNT
if "%choice%"=="5.3" goto VIEW_COOLDOWNS
if "%choice%"=="5.4" goto TEST_ACCOUNTS
if "%choice%"=="0" goto MAIN_MENU
goto ACCOUNT_MENU

:LIST_ACCOUNTS
cls
echo List configured accounts
echo.
"%PYTHON_EXE%" main.py accounts list
pause
goto ACCOUNT_MENU

:SETUP_ACCOUNT
cls
echo Setup account cookies
echo.
set /p "name=Account name: "
if "%name%"=="" (
    echo Error: Account name required
    pause
    goto ACCOUNT_MENU
)
set /p "cookies=Path to cookies.txt: "
if "%cookies%"=="" (
    echo Error: Cookies file path required
    pause
    goto ACCOUNT_MENU
)
"%PYTHON_EXE%" main.py accounts add %name% %cookies%
pause
goto ACCOUNT_MENU

:VIEW_COOLDOWNS
cls
echo View account cooldowns
echo.
"%PYTHON_EXE%" main.py accounts cooldowns
pause
goto ACCOUNT_MENU

:TEST_ACCOUNTS
cls
echo Test all accounts
echo.
"%PYTHON_EXE%" main.py accounts test
pause
goto ACCOUNT_MENU

:DATABASE_MENU
cls
echo ========================================
echo          Database Menu
echo ========================================
echo.
echo [6.1] Show stats
echo [6.2] View recent sessions
echo [6.3] Backup database
echo [6.4] Manage blob storage
echo [6.5] Reset database
echo.
echo [0] Back to Main Menu
echo.
set /p "choice=Select option: "

if "%choice%"=="6.1" goto SHOW_STATS
if "%choice%"=="6.2" goto VIEW_SESSIONS
if "%choice%"=="6.3" goto BACKUP_DB
if "%choice%"=="6.4" goto MANAGE_BLOBS
if "%choice%"=="6.5" goto RESET_DB
if "%choice%"=="0" goto MAIN_MENU
goto DATABASE_MENU

:SHOW_STATS
cls
echo Database statistics
echo.
"%PYTHON_EXE%" main.py stats
pause
goto DATABASE_MENU

:VIEW_SESSIONS
cls
echo View recent sessions
echo.
"%PYTHON_EXE%" main.py sessions
pause
goto DATABASE_MENU

:BACKUP_DB
cls
echo Backup database
echo.
set /p "output=Backup directory (leave empty for default): "
if "%output%"=="" (
    "%PYTHON_EXE%" main.py backup
) else (
    "%PYTHON_EXE%" main.py backup --output %output%
)
pause
goto DATABASE_MENU

:MANAGE_BLOBS
cls
echo Manage blob storage
echo.
echo [1] Show statistics
echo [2] Export blob (coming soon)
echo [3] Cleanup old blobs (coming soon)
echo [0] Back
echo.
set /p "choice=Select option: "
if "%choice%"=="1" "%PYTHON_EXE%" main.py blobs stats
if "%choice%"=="2" echo Export feature coming soon
if "%choice%"=="3" echo Cleanup feature coming soon
if "%choice%"=="0" goto DATABASE_MENU
pause
goto MANAGE_BLOBS

:RESET_DB
cls
echo Reset database
echo.
echo WARNING: This will clear all tracking data
set /p "confirm=Continue? (y/n): "
if /i not "%confirm%"=="y" goto DATABASE_MENU
"%PYTHON_EXE%" main.py clear
pause
goto DATABASE_MENU

:SYSTEM_MENU
cls
echo ========================================
echo           System Menu
echo ========================================
echo.
echo [7.1] Health check
echo [7.2] View logs
echo [7.3] Clear session cache
echo.
echo [0] Back to Main Menu
echo.
set /p "choice=Select option: "

if "%choice%"=="7.1" goto HEALTH_CHECK
if "%choice%"=="7.2" goto VIEW_LOGS
if "%choice%"=="7.3" goto CLEAR_CACHE
if "%choice%"=="0" goto MAIN_MENU
goto SYSTEM_MENU

:HEALTH_CHECK
cls
echo System health check
echo.
echo Checking Python environment...
"%PYTHON_EXE%" --version
echo.
echo Checking dependencies...
"%PYTHON_EXE%" -c "import requests; import imagehash; print('All dependencies OK')"
echo.
pause
goto SYSTEM_MENU

:VIEW_LOGS
cls
echo View logs
echo.
if exist "logs\" (
    dir /b logs\*.log
) else (
    echo No logs directory found
)
pause
goto SYSTEM_MENU

:CLEAR_CACHE
cls
echo Clear session cache
echo.
echo This will reset in-progress sessions and stuck spiders.
set /p "confirm=Continue? (y/n): "
if /i not "%confirm%"=="y" goto SYSTEM_MENU
"%PYTHON_EXE%" main.py cache
pause
goto SYSTEM_MENU
