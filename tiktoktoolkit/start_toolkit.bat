@echo off
:: ========================================
:: Unified TikTok Toolkit (UTTk)
:: ========================================
:: Downloads TikTok videos via gallery-dl.
:: All downloads go to: downloads\username_USER\
:: Cookie auth is optional for public accounts.
:: ========================================

setlocal enabledelayedexpansion
cd /d "%~dp0"
set "PYTHON_EXE=.venv\Scripts\python.exe"
set PYTHONUTF8=1

if exist "%PYTHON_EXE%" (
    "%PYTHON_EXE%" --version >nul 2>&1
    if errorlevel 1 (
        echo [WARN] Existing .venv is broken. Re-running setup...
        rmdir /s /q .venv
    )
)

if not exist "%PYTHON_EXE%" (
    echo [INFO] .venv not found. Running setup.bat...
    call setup.bat
    if errorlevel 1 exit /b 1
    if not exist "%PYTHON_EXE%" (
        echo [ERROR] Setup did not produce a usable .venv.
        exit /b 1
    )
)

:: Check if Playwright browsers are installed
"%PYTHON_EXE%" -c "from playwright.sync_api import sync_playwright; p = sync_playwright().start(); p.chromium.launch(); p.stop()" >nul 2>&1
if errorlevel 1 (
    echo.
    echo [WARN] Playwright browsers not installed or outdated.
    echo [INFO] Installing Chromium browser for automation fallback...
    "%PYTHON_EXE%" -m playwright install chromium
    if errorlevel 1 (
        echo [WARN] Browser installation failed. Browser automation may not work.
        echo You can install manually with: .venv\Scripts\python.exe -m playwright install chromium
        echo.
        set /p "_=  Press ENTER to continue anyway..."
    ) else (
        echo [OK] Playwright browsers installed successfully.
        echo.
    )
)

:MAIN_MENU
cls
echo.
echo   Unified TikTok Toolkit (UTTk)
echo   ================================================================
echo   Output:   downloads\username_USER\
echo   Users:    data\usernames.txt
echo   Cookies:  configs\tiktok_cookies.txt
echo   DB:       data\tiktok_toolkit.db
echo   Logs:     logs\uttk.log
echo   ================================================================
echo.
echo    #   Action                          Notes
echo   ---  ------------------------------  -----------------------------------------
echo        -- DOWNLOAD --
echo    1   Download single user            cookies auto-applied
echo    2   Bulk download from file         reads data\usernames.txt
echo    3   Bulk download (type usernames)  comma/space separated list
echo    4   Bulk download (interactive)     enter one username at a time
echo    5   Download profile pictures       reads data\usernames.txt
echo    6   Download videos only            reads data\usernames.txt
echo   ---  ------------------------------  -----------------------------------------
echo        -- UTILS --
echo    7   List usernames from file        reads data\usernames.txt
echo    8   Check download folders          scans downloads\
echo    9   Import existing downloads       register files in tracker
echo   10   Clean empty folders             remove leftover empty dirs
echo   ---  ------------------------------  -----------------------------------------
echo        -- COOKIES --
echo   11   Setup cookie authentication     export cookies from browser
echo   12   Check cookie status             verify configs\tiktok_cookies.txt
echo   13   Refresh cookies                 re-export if cookies expired
echo   ---  ------------------------------  -----------------------------------------
echo        -- MAINTENANCE --
echo   14   Find ^& delete duplicate videos  scan for duplicate files
echo   15   Maintain tracker                VACUUM + ANALYZE SQLite DB
echo   16   Reset tracker                   clear all download history (DANGER)
echo   ---  ------------------------------  -----------------------------------------
echo        -- SPIDER (discover accounts) --
echo   17   Spider seed from usernames.txt  fetch following lists, enqueue discovered
echo   18   Spider batch                    process pending discovered accounts
echo   19   Spider single user              spider one account by username
echo   ---  ------------------------------  -----------------------------------------
echo        -- DATABASE --
echo   20   Reconcile database              check tracked files exist on disk
echo   21   Reconcile deep                  verify file hashes too (slow)
echo   22   Photo history for user          show profile photo change log
echo   ---  ------------------------------  -----------------------------------------
echo        -- DIAGNOSTICS --
echo   23   Debug gallery-dl                diagnose download issues
echo    0   Exit
echo.
echo   Type a number and press Enter.  0 = exit.  Ctrl+C = cancel.
echo.
set "choice="
set /p "choice=  Enter choice: "
if not defined choice goto MAIN_MENU

if "%choice%"=="1"  goto DOWNLOAD_USER
if "%choice%"=="2"  goto BULK_FILE
if "%choice%"=="3"  goto BULK_MANUAL
if "%choice%"=="4"  goto BULK_INTERACTIVE
if "%choice%"=="5"  goto DOWNLOAD_PROFILE_PICS
if "%choice%"=="6"  goto DOWNLOAD_VIDEOS_ONLY
if "%choice%"=="7"  goto LIST_USERS
if "%choice%"=="8"  goto CHECK_FOLDERS
if "%choice%"=="9"  goto IMPORT_EXISTING
if "%choice%"=="10" goto CLEAN_EMPTY_FOLDERS
if "%choice%"=="11" goto SETUP_COOKIES
if "%choice%"=="12" goto CHECK_COOKIES
if "%choice%"=="13" goto REFRESH_COOKIES
if "%choice%"=="14" goto FIND_DUPLICATES
if "%choice%"=="15" goto MAINTAIN_TRACKER
if "%choice%"=="16" goto RESET_TRACKER
if "%choice%"=="17" goto SPIDER_SEED
if "%choice%"=="18" goto SPIDER_BATCH
if "%choice%"=="19" goto SPIDER_USER
if "%choice%"=="20" goto RECONCILE
if "%choice%"=="21" goto RECONCILE_DEEP
if "%choice%"=="22" goto PHOTO_HISTORY
if "%choice%"=="23" goto DEBUG_GDL
if "%choice%"=="0"  goto END

echo   Invalid choice. Try again.
goto MAIN_MENU


:: ================================================================
::  [1] DOWNLOAD SINGLE USER
:: ================================================================
:DOWNLOAD_USER
cls
echo.
echo   Download Single User
echo   ================================================================
echo   Downloads videos from one TikTok user profile.
echo   You'll provide the username (without @) and a video limit.
echo   Output goes to: downloads\username_USER\
echo   ================================================================
echo.
set /p "username=  Username (without @): "
if "%username%"=="" (
    echo   ERROR: No username provided.
    set /p "_=  Press ENTER to go back..."
    goto MAIN_MENU
)

echo.
set /p "limit=  Max videos to download [30]: "
if "%limit%"=="" set limit=30

call :ASK_OUTPUT_DIR

echo.
echo   Downloading %limit% videos from @%username%...
echo   Output: !output_label!
echo.
"%PYTHON_EXE%" main.py download user --user "%username%" --limit %limit% !output_arg!
echo.
echo   Done. Check !output_label!
set /p "_=  Press ENTER to return to menu..."
goto MAIN_MENU


:: ================================================================
::  [2] BULK DOWNLOAD FROM FILE
:: ================================================================
:BULK_FILE
cls
echo.
echo   Bulk Download from File
echo   ================================================================
echo   Reads usernames from data\usernames.txt (one per line).
echo   Downloads up to N videos per user, sequentially.
echo   Lines starting with # are skipped (comments).
echo   ================================================================
echo.

if not exist "data\usernames.txt" (
    echo   ERROR: data\usernames.txt not found!
    echo   Create this file with one TikTok username per line.
    set /p "_=  Press ENTER to go back..."
    goto MAIN_MENU
)

set "ucount=0"
for /f "usebackq tokens=*" %%A in ("data\usernames.txt") do (
    set "line=%%A"
    if defined line (
        set "first=!line:~0,1!"
        if not "!first!"=="#" set /a ucount+=1
    )
)
echo   Found !ucount! usernames in data\usernames.txt
echo.

set /p "limit=  Max videos per user [10]: "
if "%limit%"=="" set limit=10

call :ASK_OUTPUT_DIR

echo.
echo   Bulk downloading (!ucount! users, %limit% videos each)...
echo   Output: !output_label!
echo.
"%PYTHON_EXE%" main.py download bulk --file data/usernames.txt --limit %limit% !output_arg!
echo.
echo   Bulk download complete. Check !output_label!
set /p "_=  Press ENTER to return to menu..."
goto MAIN_MENU


:: ================================================================
::  [3] BULK DOWNLOAD (MANUAL INPUT)
:: ================================================================
:BULK_MANUAL
cls
echo.
echo   Bulk Download (Type Usernames)
echo   ================================================================
echo   Type multiple usernames separated by commas or spaces.
echo   Example: user1,user2,user3
echo   Each user's videos go to: downloads\username_USER\
echo   ================================================================
echo.
set /p "usernames=  Usernames: "
if "%usernames%"=="" (
    echo   ERROR: No usernames provided.
    set /p "_=  Press ENTER to go back..."
    goto MAIN_MENU
)

echo.
set /p "limit=  Max videos per user [10]: "
if "%limit%"=="" set limit=10

call :ASK_OUTPUT_DIR

echo.
echo   Bulk downloading from typed usernames (%limit% videos each)...
echo   Output: !output_label!
echo.
"%PYTHON_EXE%" main.py download bulk --users "%usernames%" --limit %limit% !output_arg!
echo.
echo   Bulk download complete. Check !output_label!
set /p "_=  Press ENTER to return to menu..."
goto MAIN_MENU


:: ================================================================
::  [4] BULK DOWNLOAD (INTERACTIVE)
:: ================================================================
:BULK_INTERACTIVE
cls
echo.
echo   Bulk Download (Interactive)
echo   ================================================================
echo   Add usernames one at a time. Press Enter on an empty line
echo   when done. Duplicates are automatically skipped.
echo   ================================================================
echo.

set /p "limit=  Max videos per user [10]: "
if "%limit%"=="" set limit=10

call :ASK_OUTPUT_DIR

echo.
echo   Starting interactive mode...
echo.
"%PYTHON_EXE%" main.py download bulk --interactive --limit %limit% !output_arg!
echo.
echo   Interactive download complete. Check !output_label!
set /p "_=  Press ENTER to return to menu..."
goto MAIN_MENU


:: ================================================================
::  [5] DOWNLOAD PROFILE PICTURES
:: ================================================================
:DOWNLOAD_PROFILE_PICS
cls
echo.
echo   Download Profile Pictures
echo   ================================================================
echo   Downloads profile pictures for usernames listed in
echo   data\usernames.txt (one per line).
echo   ================================================================
echo.

if not exist "data\usernames.txt" (
    echo   ERROR: data\usernames.txt not found!
    echo   Create this file with one TikTok username per line.
    set /p "_=  Press ENTER to go back..."
    goto MAIN_MENU
)

set "ucount=0"
for /f "usebackq tokens=*" %%A in ("data\usernames.txt") do (
    set "line=%%A"
    if defined line (
        set "first=!line:~0,1!"
        if not "!first!"=="#" set /a ucount+=1
    )
)
echo   Found !ucount! usernames in data\usernames.txt
echo.

call :ASK_OUTPUT_DIR

echo.
echo   Downloading profile pictures for !ucount! users...
echo   Output: !output_label!
echo.
"%PYTHON_EXE%" main.py download bulk --file data/usernames.txt --type profile_pictures !output_arg!
echo.
echo   Profile pictures download complete. Check !output_label!
set /p "_=  Press ENTER to return to menu..."
goto MAIN_MENU


:: ================================================================
::  [6] DOWNLOAD VIDEOS ONLY
:: ================================================================
:DOWNLOAD_VIDEOS_ONLY
cls
echo.
echo   Download Videos Only
echo   ================================================================
echo   Downloads only videos (skipping profile pictures) for usernames
echo   listed in data\usernames.txt (one per line).
echo   ================================================================
echo.

if not exist "data\usernames.txt" (
    echo   ERROR: data\usernames.txt not found!
    echo   Create this file with one TikTok username per line.
    set /p "_=  Press ENTER to go back..."
    goto MAIN_MENU
)

set "ucount=0"
for /f "usebackq tokens=*" %%A in ("data\usernames.txt") do (
    set "line=%%A"
    if defined line (
        set "first=!line:~0,1!"
        if not "!first!"=="#" set /a ucount+=1
    )
)
echo   Found !ucount! usernames in data\usernames.txt
echo.

set /p "limit=  Max videos per user [10]: "
if "%limit%"=="" set limit=10

call :ASK_OUTPUT_DIR

echo.
echo   Downloading videos only for !ucount! users (%limit% videos each)...
echo   Output: !output_label!
echo.
"%PYTHON_EXE%" main.py download bulk --file data/usernames.txt --limit %limit% --type videos !output_arg!
echo.
echo   Videos download complete. Check !output_label!
set /p "_=  Press ENTER to return to menu..."
goto MAIN_MENU


:: ================================================================
::  [7] LIST USERNAMES FROM FILE
:: ================================================================
:LIST_USERS
cls
echo.
echo   List Usernames from File
echo   ================================================================
echo   Shows all valid usernames from data\usernames.txt.
echo   Comments (#) and blank lines are skipped.
echo   ================================================================
echo.

if not exist "data\usernames.txt" (
    echo   ERROR: data\usernames.txt not found!
    set /p "_=  Press ENTER to go back..."
    goto MAIN_MENU
)

"%PYTHON_EXE%" main.py utils list-users --file data/usernames.txt
echo.
set /p "_=  Press ENTER to return to menu..."
goto MAIN_MENU


:: ================================================================
::  [8] CHECK DOWNLOAD FOLDERS
:: ================================================================
:CHECK_FOLDERS
cls
echo.
echo   Check Download Folders
echo   ================================================================
echo   Scans the downloads\ directory and shows which users have
echo   been downloaded and how many video files each has.
echo   ================================================================
echo.

set /p "check_dir=  Directory to check [downloads]: "
if "%check_dir%"=="" set check_dir=downloads

"%PYTHON_EXE%" main.py utils check-folders --out "%check_dir%"
echo.
set /p "_=  Press ENTER to return to menu..."
goto MAIN_MENU


:: ================================================================
::  [9] IMPORT EXISTING DOWNLOADS
:: ================================================================
:IMPORT_EXISTING
cls
echo.
echo   Import Existing Downloads
echo   ================================================================
echo   Scans a folder for previously downloaded videos and registers
echo   them in the duplicate-prevention tracker. This prevents the
echo   toolkit from re-downloading videos you already have.
echo.
echo   Folder structure expected: downloads\username_USER\*.mp4
echo   ================================================================
echo.

set /p "import_dir=  Directory to import from [downloads]: "
if "%import_dir%"=="" set import_dir=downloads

echo.
set /p "force_user=  Force all files to one username (blank = auto-detect): "

set "user_arg="
if not "%force_user%"=="" set "user_arg=--username %force_user%"

echo.
echo   Importing from %import_dir%...
"%PYTHON_EXE%" main.py utils import-existing --root "%import_dir%" %user_arg%
echo.
set /p "_=  Press ENTER to return to menu..."
goto MAIN_MENU


:: ================================================================
::  [10] CLEAN EMPTY FOLDERS
:: ================================================================
:CLEAN_EMPTY_FOLDERS
cls
echo.
echo   Clean Empty Folders
echo   ================================================================
echo   Removes leftover empty directories inside the downloads folder.
echo   gallery-dl sometimes creates date subfolders that end up empty.
echo   ================================================================
echo.

set /p "clean_dir=  Directory to clean [downloads]: "
if "%clean_dir%"=="" set clean_dir=downloads

echo.
echo   Scanning %clean_dir% for empty folders...
"%PYTHON_EXE%" main.py utils clean-empty-folders --dir "%clean_dir%"
echo.
set /p "_=  Press ENTER to return to menu..."
goto MAIN_MENU


:: ================================================================
::  [11] SETUP COOKIE AUTHENTICATION
:: ================================================================
:SETUP_COOKIES
cls
echo.
echo   Setup Cookie Authentication
echo   ================================================================
echo   Extracts your TikTok login cookies from a browser so the
echo   toolkit can access private accounts you follow.
echo.
echo   Prerequisites:
echo     1. Open your browser (Chrome/Firefox/Edge)
echo     2. Log into tiktok.com
echo     3. Keep the browser open, then run this
echo   ================================================================
echo.

echo   Which browser are you logged into?
echo     [1] Chrome
echo     [2] Firefox
echo     [3] Edge
echo     [4] Safari
echo.
set /p "browser_choice=  Browser [1]: "
if "%browser_choice%"=="" set browser_choice=1

set "browser=chrome"
if "%browser_choice%"=="2" set "browser=firefox"
if "%browser_choice%"=="3" set "browser=edge"
if "%browser_choice%"=="4" set "browser=safari"

echo.
"%PYTHON_EXE%" main.py utils setup-cookies --browser %browser%
echo.
set /p "_=  Press ENTER to return to menu..."
goto MAIN_MENU


:: ================================================================
::  [12] CHECK COOKIE STATUS
:: ================================================================
:CHECK_COOKIES
cls
echo.
echo   Check Cookie Status
echo   ================================================================
echo   Tests whether your cookie file is valid and gallery-dl can
echo   use it to access TikTok content. Compares access with and
echo   without cookies to show if they provide additional access.
echo   ================================================================
echo.

set /p "test_user=  Username to test with [tiktok]: "
if "%test_user%"=="" set test_user=tiktok

echo.
"%PYTHON_EXE%" main.py utils check-cookies --test-user %test_user%
echo.
set /p "_=  Press ENTER to return to menu..."
goto MAIN_MENU


:: ================================================================
::  [13] REFRESH COOKIES
:: ================================================================
:REFRESH_COOKIES
cls
echo.
echo   Refresh Cookies
echo   ================================================================
echo   Shows current cookie status, then re-extracts fresh cookies
echo   from your browser. Use this when existing cookies have expired
echo   (you'll see 403 errors or authentication failures).
echo.
echo   Prerequisites: be logged into TikTok in your browser.
echo   ================================================================
echo.

echo   Which browser are you logged into?
echo     [1] Chrome
echo     [2] Firefox
echo     [3] Edge
echo     [4] Safari
echo.
set /p "browser_choice=  Browser [1]: "
if "%browser_choice%"=="" set browser_choice=1

set "browser=chrome"
if "%browser_choice%"=="2" set "browser=firefox"
if "%browser_choice%"=="3" set "browser=edge"
if "%browser_choice%"=="4" set "browser=safari"

echo.
"%PYTHON_EXE%" main.py utils refresh-cookies --browser %browser%
echo.
set /p "_=  Press ENTER to return to menu..."
goto MAIN_MENU


:: ================================================================
::  [14] FIND & DELETE DUPLICATE VIDEOS
:: ================================================================
:FIND_DUPLICATES
cls
echo.
echo   Find ^& Delete Duplicate Videos
echo   ================================================================
echo   Scans your download folder for videos downloaded more than once
echo   (files like 7417893060190817554_1.mp4, _2.mp4 etc).
echo   Shows duplicates first, then asks before deleting anything.
echo   Also flattens any date subfolders back to flat layout.
echo   ================================================================
echo.

set /p "scan_dir=  Directory to scan [downloads]: "
if "%scan_dir%"=="" set scan_dir=downloads

echo.
echo   Scanning %scan_dir% for duplicates...
echo.
"%PYTHON_EXE%" main.py utils find-duplicates --dir "%scan_dir%" --delete
echo.
set /p "_=  Press ENTER to return to menu..."
goto MAIN_MENU


:: ================================================================
::  [15] MAINTAIN TRACKER
:: ================================================================
:MAINTAIN_TRACKER
cls
echo.
echo   Maintain Tracker
echo   ================================================================
echo   Runs VACUUM and ANALYZE on the SQLite download tracker.
echo   Reclaims disk space from deleted rows and updates query stats.
echo   Safe to run at any time. Usually takes a few seconds.
echo   ================================================================
echo.
echo   Running tracker maintenance...
"%PYTHON_EXE%" main.py utils maintain-tracker
echo.
set /p "_=  Press ENTER to return to menu..."
goto MAIN_MENU


:: ================================================================
::  [16] RESET TRACKER
:: ================================================================
:RESET_TRACKER
cls
echo.
echo   Reset Tracker
echo   ================================================================
echo   WARNING: Clears ALL download history from the SQLite tracker
echo   and JSON backup. You will lose track of what has been
echo   downloaded. The toolkit will re-download everything next run.
echo.
echo   A backup is created automatically unless you say no.
echo   ================================================================
echo.
echo   Are you sure you want to reset? This cannot be undone.
set /p "confirm_reset=  Type YES to continue: "
if not "%confirm_reset%"=="YES" (
    echo   Cancelled.
    set /p "_=  Press ENTER to return to menu..."
    goto MAIN_MENU
)

echo.
"%PYTHON_EXE%" main.py utils reset-tracker
echo.
set /p "_=  Press ENTER to return to menu..."
goto MAIN_MENU


:: ================================================================
::  [17] SPIDER SEED FROM USERNAMES.TXT
:: ================================================================
:SPIDER_SEED
cls
echo.
echo   Spider Seed from data\usernames.txt
echo   ================================================================
echo   For each account in data\usernames.txt:
echo     - Fetches their TikTok profile counts (following/followers/videos)
echo     - Stores all counts in the database
echo     - If following ^<= 500 AND followers ^<= 500: fetches their
echo       following list and enqueues discovered accounts for spidering
echo     - If either count exceeds 500: stores counts only (no list fetch)
echo.
echo   After seeding, run option 18 (Spider batch) to process
echo   the newly discovered accounts.
echo.
echo   Requires: configs\tiktok_cookies.txt (cookie authentication)
echo   ================================================================
echo.
echo   This will open browser windows for each account. May take a while.
set /p "confirm_seed=  Start seeding? (y/n): "
if /i not "%confirm_seed%"=="y" (
    echo   Cancelled.
    set /p "_=  Press ENTER to return to menu..."
    goto MAIN_MENU
)

echo.
"%PYTHON_EXE%" main.py spider --seed
echo.
set /p "_=  Press ENTER to return to menu..."
goto MAIN_MENU


:: ================================================================
::  [18] SPIDER BATCH
:: ================================================================
:SPIDER_BATCH
cls
echo.
echo   Spider Batch
echo   ================================================================
echo   Processes all pending accounts in the spider queue.
echo   These are accounts discovered during a seed run (option 17).
echo.
echo   For each pending account:
echo     - Fetches profile counts (always stored)
echo     - If within thresholds: fetches their following list too
echo.
echo   Requires: configs\tiktok_cookies.txt (cookie authentication)
echo   ================================================================
echo.

set /p "batch_limit=  Max accounts to process [500]: "
if "%batch_limit%"=="" set batch_limit=500

echo.
echo   Processing pending accounts (limit: %batch_limit%)...
"%PYTHON_EXE%" main.py spider --batch --limit %batch_limit%
echo.
set /p "_=  Press ENTER to return to menu..."
goto MAIN_MENU


:: ================================================================
::  [19] SPIDER SINGLE USER
:: ================================================================
:SPIDER_USER
cls
echo.
echo   Spider Single User
echo   ================================================================
echo   Fetches profile stats for one user and (if within thresholds)
echo   enqueues everyone they follow for future spidering.
echo.
echo   Requires: configs\tiktok_cookies.txt (cookie authentication)
echo   ================================================================
echo.

set /p "spider_username=  Username (without @): "
if "%spider_username%"=="" (
    echo   ERROR: No username provided.
    set /p "_=  Press ENTER to go back..."
    goto MAIN_MENU
)

echo.
echo   Spidering @%spider_username%...
"%PYTHON_EXE%" main.py spider --username "%spider_username%"
echo.
set /p "_=  Press ENTER to return to menu..."
goto MAIN_MENU


:: ================================================================
::  [20] RECONCILE DATABASE
:: ================================================================
:RECONCILE
cls
echo.
echo   Reconcile Database
echo   ================================================================
echo   Checks every file path recorded in the download tracker
echo   and verifies the file still exists on disk.
echo   Reports how many tracked files are missing.
echo   Does NOT modify or delete any files.
echo.
echo   Database: data\tiktok_toolkit.db
echo   ================================================================
echo.
echo   Running reconciliation (no hash check)...
"%PYTHON_EXE%" main.py reconcile --no-photos
echo.
set /p "_=  Press ENTER to return to menu..."
goto MAIN_MENU


:: ================================================================
::  [21] RECONCILE DEEP
:: ================================================================
:RECONCILE_DEEP
cls
echo.
echo   Reconcile Deep (Hash Verification)
echo   ================================================================
echo   Same as option 20, plus re-computes file hashes and compares
echo   against stored values. Slower — reads every video file.
echo   Reports any hash mismatches (potential file corruption).
echo   Does NOT modify or delete any files.
echo.
echo   Database: data\tiktok_toolkit.db
echo   ================================================================
echo.
echo   Running deep reconciliation (with hash check)...
"%PYTHON_EXE%" main.py reconcile --deep --no-photos
echo.
set /p "_=  Press ENTER to return to menu..."
goto MAIN_MENU


:: ================================================================
::  [22] PHOTO HISTORY FOR USER
:: ================================================================
:PHOTO_HISTORY
cls
echo.
echo   Photo History for User
echo   ================================================================
echo   Shows the profile photo change history for a spidered user.
echo   Only works for accounts that have been processed by the spider.
echo   ================================================================
echo.

set /p "photo_username=  Username (without @): "
if "%photo_username%"=="" (
    echo   ERROR: No username provided.
    set /p "_=  Press ENTER to go back..."
    goto MAIN_MENU
)

set /p "photo_limit=  Max entries to show [10]: "
if "%photo_limit%"=="" set photo_limit=10

echo.
"%PYTHON_EXE%" main.py photo-history --username "%photo_username%" --limit %photo_limit%
echo.
set /p "_=  Press ENTER to return to menu..."
goto MAIN_MENU


:: ================================================================
::  [23] DEBUG GALLERY-DL
:: ================================================================
:DEBUG_GDL
cls
echo.
echo   Debug Gallery-dl
echo   ================================================================
echo   Runs diagnostic checks on your gallery-dl installation:
echo     - Is gallery-dl installed?
echo     - Can it reach TikTok?
echo     - Does a simulated download succeed?
echo   Use this when downloads silently fail.
echo   ================================================================
echo.

set /p "debug_url=  Test URL (Enter = official @tiktok): "

set "url_arg="
if not "%debug_url%"=="" set "url_arg=--url %debug_url%"

"%PYTHON_EXE%" main.py utils debug-gallery-dl %url_arg%
echo.
set /p "_=  Press ENTER to return to menu..."
goto MAIN_MENU


:: ================================================================
::  SHARED SUB-ROUTINES
:: ================================================================

:: ---- Ask for output directory ----
:ASK_OUTPUT_DIR
echo.
echo   Output directory:
echo     Leave blank to let the Python CLI prompt for a path
echo.
set "output_dir="
set "output_arg="
set "output_label=prompt"
set /p "output_dir=  Custom path (optional): "
if not "!output_dir!"=="" (
    set "output_arg=--out "!output_dir!""
    set "output_label=!output_dir!"
)
goto :eof


:: ================================================================
::  EXIT
:: ================================================================
:END
echo.
echo   Thank you for using Unified TikTok Toolkit!
echo.
set /p "_=  Press ENTER to exit..."
