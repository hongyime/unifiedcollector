@echo off

:start
echo.
echo ========================================
echo     TELEGRAM TOOLKIT - QUICK ACTIONS
echo ========================================
echo.
echo   CORE OPERATIONS
echo   ----------------------------------------
echo    1. Unified Scan         - Extract users, links, media (FASTEST!)
echo    2. Join Groups          - Auto-join from collected links
echo    3. Leave Groups         - Bulk leave unwanted groups
echo.
echo   TARGETED EXTRACTION
echo   ----------------------------------------
echo    4. Download Media       - Photos, videos, files (resumable)
echo    5. Analyze Users        - Scrape member lists to CSV/DB
echo    6. Collect Links        - Scan chats for invite links
echo    7. Multi-Platform Links - Collect non-Telegram links
echo    8. Profile Photos       - Download user profile pics
echo    9. Send Photos          - Bulk send photos to a chat
echo.
echo   VISUALIZATION ^& DATA
echo   ----------------------------------------
echo   10. Open Dashboard       - Browse users + groups in browser
echo   11. Open Visualizer      - Network graph of connections
echo   12. Data Export           - Export DB to CSV/JSON
echo.
echo   TOOLS ^& MANAGEMENT
echo   ----------------------------------------
echo   13. Account Manager      - Add/remove/test accounts
echo   14. Tracking / Reset     - View or reset scan progress
echo   15. Backup Messages      - Export deleted msgs (admin only)
echo   16. Resend Messages      - Re-send backed-up messages
echo   17. Full Pipeline        - Run links-join-media-users auto
echo   18. Start Server         - HTTP server for web dashboards
echo    0. Exit
echo.

set /p choice="Enter your choice: "

cd /d "%~dp0"
set "PYTHON_EXE=.venv\Scripts\python.exe"

if exist "%PYTHON_EXE%" (
    "%PYTHON_EXE%" --version >nul 2>&1
    if errorlevel 1 (
        echo [WARN] Existing .venv is broken. Re-running setup...
        rmdir /s /q .venv
    )
)

if not exist "%PYTHON_EXE%" (
    echo [INFO] .venv not found. Running setup.bat...
    call setup.bat --no-pause
    if errorlevel 1 exit /b 1
    if not exist "%PYTHON_EXE%" (
        echo [ERROR] Setup did not produce a usable .venv.
        exit /b 1
    )
)

if "%choice%"=="1" (
    echo Starting unified all-features scan...
    "%PYTHON_EXE%" main.py unified
) else if "%choice%"=="2" (
    echo Starting group joining...
    "%PYTHON_EXE%" main.py join
) else if "%choice%"=="3" (
    echo Starting group cleanup...
    "%PYTHON_EXE%" main.py leave
) else if "%choice%"=="4" (
    echo Starting media download...
    "%PYTHON_EXE%" main.py media
) else if "%choice%"=="5" (
    echo Starting user analysis...
    "%PYTHON_EXE%" main.py users
) else if "%choice%"=="6" (
    echo Starting link collection...
    "%PYTHON_EXE%" main.py links
) else if "%choice%"=="7" (
    echo Starting multi-platform link collection...
    "%PYTHON_EXE%" main.py multi
) else if "%choice%"=="8" (
    echo Starting profile photo download...
    "%PYTHON_EXE%" main.py profiles
) else if "%choice%"=="9" (
    echo Starting photo sending to chat...
    "%PYTHON_EXE%" main.py photos
) else if "%choice%"=="10" (
    echo.
    echo ========================================
    echo     STARTING DASHBOARD WITH SERVER
    echo ========================================
    echo Starting HTTP server and opening dashboard...
    echo.
    "%PYTHON_EXE%" -c "from main import TelegramToolkit; toolkit = TelegramToolkit(); toolkit.open_dashboard()" 2>nul
    if errorlevel 1 (
        echo Using fallback server...
        "%PYTHON_EXE%" -m src.server.simple_server dashboard
    )
) else if "%choice%"=="11" (
    echo.
    echo ========================================
    echo    STARTING VISUALIZER WITH SERVER
    echo ========================================
    echo Starting HTTP server and opening visualizer...
    echo.
    "%PYTHON_EXE%" -c "from main import TelegramToolkit; toolkit = TelegramToolkit(); toolkit.open_visualizer()" 2>nul
    if errorlevel 1 (
        echo Using fallback server...
        "%PYTHON_EXE%" -m src.server.simple_server visualize
    )
) else if "%choice%"=="12" (
    echo Starting data export...
    "%PYTHON_EXE%" main.py export
) else if "%choice%"=="13" (
    echo Opening Account Manager...
    "%PYTHON_EXE%" main.py accounts
) else if "%choice%"=="14" (
    echo Opening Tracking / Reset State Manager...
    "%PYTHON_EXE%" main.py state
) else if "%choice%"=="15" (
    echo Starting message backup...
    "%PYTHON_EXE%" main.py backup
) else if "%choice%"=="16" (
    echo Starting message resend...
    "%PYTHON_EXE%" main.py resend
) else if "%choice%"=="17" (
    echo Running full pipeline...
    "%PYTHON_EXE%" main.py pipeline
) else if "%choice%"=="18" (
    echo.
    echo ========================================
    echo       STARTING HTTP SERVER ONLY
    echo ========================================
    echo Server will be available at:
    echo   Dashboard:  http://localhost:8000/web/enhanced_dashboard.html
    echo   Visualizer: http://localhost:8000/web/visualize.html
    echo.
    echo Press Ctrl+C to stop the server
    echo.
    "%PYTHON_EXE%" -m src.server.simple_server no-browser
) else if "%choice%"=="0" (
    echo Goodbye!
    exit /b 0
) else (
    echo Invalid choice. Please try again.
    goto :start
)

echo.
echo Task completed!
pause
goto :start
