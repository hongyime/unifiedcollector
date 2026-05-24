@echo off
setlocal EnableDelayedExpansion

:menu
cls
echo.
echo    ╔══════════════════════════════════════════════════════════════╗
echo    ║           UNIFIED SEARCH TOOLKIT v2 - ENHANCED              ║
echo    ╚══════════════════════════════════════════════════════════════╝
echo.
echo    🆕 Enhanced Features:
echo      • Tor proxy support - Avoid rate limits with IP rotation
echo      • State persistence - Resume interrupted downloads
echo      • Smart rate limiting - Prevents 429 errors automatically
echo      • Search caching - Reduces API costs by reusing results
echo.
echo    ──────────────── PRIMARY MODES (RECOMMENDED) ────────────────
echo.
echo      [1]  🔍 Search ^& Extract
echo           • Multi-engine search (DuckDuckGo, Bing, Serper)
echo           • Download images and PDFs, convert to JPG
echo           • Page spidering for hidden content
echo           • Parallel downloads with quality gates
echo           • Interactive prompts guide you through each step
echo.
echo      [2]  🖼️  Bing Image Downloader
echo           • Search Bing Images with advanced filters
echo           • Format filtering (JPG, PNG, etc.)
echo           • Quality control (resolution thresholds)
echo           • Organized subfolders per keyword
echo           • Interactive prompts for all settings
echo.
echo      [3]  🎯 Dork Runner
echo           • Run Google dorks across multiple engines
echo           • Automatic fallback: DDG → Bing → Serper
echo           • Save collected URLs to text files
echo           • Interactive dork selection
echo.
echo    ───────────────────── UTILITIES ────────────────────────
echo.
echo      [4]  🛠️  Setup Environment
echo           Install/update Python dependencies
echo           Creates required folders (downloads, state)
echo.
echo      [5]  📂 Open Downloads Folder
echo           View your downloaded files
echo.
echo      [6]  💾 Open State Folder
echo           View SQLite database, cache files, and progress
echo           Useful for monitoring and troubleshooting
echo.
echo      [7]  ❓ View CLI Help
echo           Show all command-line options for automation
echo           Examples: --use-tor, --resume, --query, etc.
echo.
echo      [0]  🚪 Exit
echo.
echo    ════════════════════════════════════════════════════════
set /p choice="    Enter your choice (0-7): "

if "!choice!"=="1" goto mode1
if "!choice!"=="2" goto mode2
if "!choice!"=="3" goto mode3
if "!choice!"=="4" goto run_setup
if "!choice!"=="5" goto open_downloads
if "!choice!"=="6" goto open_state
if "!choice!"=="7" goto show_help
if "!choice!"=="0" goto exit_script

echo.
echo    ❌ Invalid choice. Please enter a number between 0 and 7.
pause >nul
goto menu

:mode1
cls
echo ========================================
echo    🔍 Search ^& Extract
echo    Multi-engine search with file extraction
echo ========================================
echo.
echo    Starting interactive mode...
echo    You will be guided through:
echo      1. Enter search queries (keywords)
echo      2. Choose file types (images, PDFs, or both)
echo      3. Enable page spidering (optional)
echo      4. Download and convert files to JPG
echo.
echo    Press Ctrl+C at any time to cancel.
echo.
cd /d "%~dp0\.."
.venv\Scripts\python.exe main.py 1
echo.
pause
goto menu

:mode2
cls
echo ========================================
echo    🖼️  Bing Image Downloader
echo    Advanced image search with filters
echo ========================================
echo.
echo    Starting interactive mode...
echo    You will be guided through:
echo      1. Enter search keywords
echo      2. Set image format preferences
echo      3. Choose quality level
echo      4. Select naming format
echo      5. Specify number of images per keyword
echo.
echo    Press Ctrl+C at any time to cancel.
echo.
cd /d "%~dp0\.."
.venv\Scripts\python.exe main.py 2
echo.
pause
goto menu

:mode3
cls
echo ========================================
echo    🎯 Dork Runner
echo    Multi-engine dork execution
echo ========================================
echo.
echo    Starting interactive mode...
echo    You will be guided through:
echo      1. Choose dork list (default or custom)
echo      2. Enable Chrome fallback (optional)
echo      3. Run dorks across all engines
echo      4. Save results to text files
echo.
echo    Press Ctrl+C at any time to cancel.
echo.
cd /d "%~dp0\.."
.venv\Scripts\python.exe main.py 3
echo.
pause
goto menu

:run_setup
cls
echo ========================================
echo    🛠️  Setup Environment
echo    Installing dependencies and creating folders
echo ========================================
echo.
cd /d "%~dp0\.."
call scripts\setup.bat
echo.
pause
goto menu

:open_downloads
cd /d "%~dp0\.."
if exist "downloads" (
    explorer "downloads"
) else (
    echo    ❌ Downloads folder does not exist yet.
    echo       Run a download first (Mode 1, 2, or 3)!
    pause
)
goto menu

:open_state
cd /d "%~dp0\.."
if exist "state" (
    explorer "state"
) else (
    echo    ❌ State folder does not exist yet.
    echo       Run with enhanced features first!
    pause
)
goto menu

:show_help
cls
echo ========================================
echo    ❓ CLI Help - Command-Line Options
echo ========================================
echo.
echo    For automation and scripting, use command-line arguments:
echo.
echo    Usage: .venv\Scripts\python.exe main.py [OPTIONS]
echo.
echo    Core Options:
echo      --mode {1,2,3}        Operation mode
echo                            1 = Search ^& Extract
echo                            2 = Bing Image Downloader
echo                            3 = Dork Runner
echo      --query QUERY         Search query (comma-separated for multiple)
echo      --dorks-file FILE     Path to dorks file (one per line, mode 3)
echo.
echo    Enhanced Features:
echo      --use-tor             Enable Tor proxy for IP rotation
echo                            Avoids rate limits and bans
echo      --resume              Resume from last checkpoint
echo                            Continues interrupted downloads
echo      --state-dir PATH      Custom state directory
echo                            Default: ./state
echo      --cache-ttl HOURS     Cache TTL in hours
echo                            Default: 24 hours
echo      --no-cache            Disable search result caching
echo                            Forces fresh searches every time
echo      --rate-limit-delay S  Base delay between requests
echo                            Default: 2.0 seconds
echo      --output-dir PATH     Output directory for downloads
echo                            Overrides interactive prompt
echo.
echo    Examples:
echo      .venv\Scripts\python.exe main.py --mode 1 --query "yearbook 2024" --use-tor --resume
echo      .venv\Scripts\python.exe main.py --mode 2 --query "cat,dog" --output-dir downloads
echo      .venv\Scripts\python.exe main.py --mode 3 --dorks-file data\search.txt --state-dir ./mystate
echo      .venv\Scripts\python.exe main.py                           (interactive mode)
echo.
echo    For full documentation, see README.md
echo.
pause
goto menu

:exit_script
echo.
echo    👋 Goodbye!
echo.
exit /b 0
