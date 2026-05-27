@echo off
echo.
echo ========================================
echo    UNIFIED WEBSITE TOOLKIT
echo ========================================
echo.

cd /d "%~dp0"
set "PYTHON_EXE=.venv\Scripts\python.exe"
set "PYTHONPATH=%~dp0src;%PYTHONPATH%"
if not exist "%PYTHON_EXE%" (
    echo [ERROR] .venv not found. Run setup.bat first.
    pause
    exit /b 1
)

:MENU
echo Select an option:
echo.
echo 1. Start Main Toolkit (Interactive Menu)
echo 2. Run Automated Cycle (Discovery ^& Scraping)
echo 3. Bulk Import Websites
echo 4. Create Sample Import File
echo 5. View Website Configuration
echo 6. Exit
echo.
set /p choice=Enter your choice (1-6): 

if "%choice%"=="1" goto START_MAIN
if "%choice%"=="2" goto RUN_CYCLE
if "%choice%"=="3" goto BULK_IMPORT
if "%choice%"=="4" goto CREATE_SAMPLE
if "%choice%"=="5" goto VIEW_CONFIG
if "%choice%"=="6" goto EXIT
echo Invalid choice. Please try again.
echo.
goto MENU

:START_MAIN
echo.
echo Starting main toolkit...
"%PYTHON_EXE%" main.py
goto MENU

:RUN_CYCLE
echo.
echo Starting automated cycle via main menu...
"%PYTHON_EXE%" -c "import asyncio; from main import run_automated_cycle; asyncio.run(run_automated_cycle())"
echo.
goto MENU

:BULK_IMPORT
echo.
echo Starting bulk website importer...
"%PYTHON_EXE%" -c "from bulk_website_importer import interactive_bulk_import; interactive_bulk_import()"
echo.
goto MENU

:CREATE_SAMPLE
echo.
echo Creating sample import file...
"%PYTHON_EXE%" -c "from bulk_website_importer import BulkWebsiteImporter; importer = BulkWebsiteImporter(); print('Created:', importer.create_sample_import_file())"
echo.
goto MENU

:VIEW_CONFIG
echo.
echo Viewing website configuration from database...
"%PYTHON_EXE%" -c "from db_manager import get_db_manager; db = get_db_manager(); websites = db.get_websites(); print('Total websites: ' + str(len(websites))); [print('  ' + str(i+1) + '. ' + w.get('name', 'N/A') + ' - ' + w.get('url', 'N/A') + ' [' + ('ENABLED' if w.get('enabled', False) else 'DISABLED') + ']') for i, w in enumerate(websites[:10])]"
echo.
goto MENU

:EXIT
echo.
echo Thank you for using the Unified Website Toolkit!
echo.
pause >nul
exit
