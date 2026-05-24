@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
set "PYTHON_EXE=.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
    echo [ERROR] .venv not found. Run setup.bat first.
    call :WAIT
    exit /b 1
)
title Instagram Toolkit - Quick Actions


REM ══════════════════════════════════════════════════════════════════
REM  QUICK ACTIONS MENU
REM ══════════════════════════════════════════════════════════════════
:QUICK_MENU
cls
echo.
echo   Quick Actions - common workflows in fewer clicks
echo   ================================================================
echo.
echo    #   Action                          Account          Risk
echo   ---  ------------------------------  ---------------  ------
echo    1   View usernames list             no login         SAFE
echo    2   View statistics / reports       no login         SAFE
echo    3   Backup all data                 no login         SAFE
echo    4   Add username to list            no login         SAFE
echo   ---  ------------------------------  ---------------  ------
echo    5   Quick spider + download 1 user  you pick 1       MEDIUM
echo   ---  ------------------------------  ---------------  ------
echo    6   Full pipeline (test+spider+dl)  rotates all      HIGH
echo    7   Seed from my accounts           all / custom     HIGH
echo    8   Batch download (profile/media)  rotates all      HIGH
echo   ---  ------------------------------  ---------------  ------
echo    9   Clear all data / Emergency stop                  DANGER
echo    0   Back to main menu
echo.
echo   Type a number and press Enter.  0 = back.  Ctrl+C = cancel.
echo.
set "choice="
set /p "choice=  Choose: "
if not defined choice goto QUICK_MENU
if "%choice%"=="0" exit /b 0
if "%choice%"=="1" goto VIEW_USERNAMES
if "%choice%"=="2" goto REPORTS
if "%choice%"=="3" goto BACKUP_DATA
if "%choice%"=="4" goto ADD_USERNAME
if "%choice%"=="5" goto QUICK_COMPLETE
if "%choice%"=="6" goto FULL_PIPELINE
if "%choice%"=="7" goto SEED_FRESH
if "%choice%"=="8" goto BATCH_DOWNLOAD
if "%choice%"=="9" goto DANGER_MENU
goto QUICK_MENU

REM ══════════════════════════════════════════════════════════════════
REM  SAFE
REM ══════════════════════════════════════════════════════════════════

:VIEW_USERNAMES
cls
echo  Current Usernames List [offline]
echo  ================================
echo.
"%PYTHON_EXE%" main.py list-usernames
echo.
    call :WAIT
goto QUICK_MENU

:REPORTS
cls
echo  Statistics and Reports [offline]
echo  =================================
echo.
"%PYTHON_EXE%" main.py analyze
echo.
"%PYTHON_EXE%" -c "import sys,os; sys.path.insert(0,'src'); os.environ.setdefault('DATABASE_URL','sqlite:///data/instagram_toolkit.db'); from db.manager import DatabaseManager; db=DatabaseManager(); rels=db.fetchall('SELECT type, source, target FROM relationships'); print(f'  Total relationships: {len(rels)}'); print(f'  Follower links: {sum(1 for r in rels if r[\"type\"]==\"followers\")}'); print(f'  Following links: {sum(1 for r in rels if r[\"type\"]==\"following\")}'); srcs=set(r[\"source\"] for r in rels); tgts=set(r[\"target\"] for r in rels); print(f'  Unique sources: {len(srcs)}'); print(f'  Unique targets: {len(tgts)}') if rels else print('  No relationship data yet. Run spider first.')"
echo.
echo  Profile data summary:
"%PYTHON_EXE%" -c "import sys,os; sys.path.insert(0,'src'); os.environ.setdefault('DATABASE_URL','sqlite:///data/instagram_toolkit.db'); from db.manager import DatabaseManager; db=DatabaseManager(); p=db.fetchone('SELECT COUNT(*) as cnt FROM profiles'); u=db.fetchone('SELECT COUNT(*) as cnt FROM usernames'); h=db.fetchone('SELECT COUNT(DISTINCT user_id) as cnt FROM username_history WHERE user_id IS NOT NULL'); print(f'  Tracked usernames : {u[\"cnt\"] if u else 0}'); print(f'  Profiles scanned  : {p[\"cnt\"] if p else 0}'); print(f'  Unique account IDs: {h[\"cnt\"] if h else 0} (rename tracking)')" 2>nul
echo.
    call :WAIT
goto QUICK_MENU

:BACKUP_DATA
cls
echo  Backup Data [offline]
echo  =====================
echo.
set timestamp=%date:~-4,4%%date:~-10,2%%date:~-7,2%_%time:~0,2%%time:~3,2%%time:~6,2%
set timestamp=%timestamp: =0%
set backup_dir=backup_%timestamp%

mkdir %backup_dir% 2>nul
if exist "data" xcopy data %backup_dir%\data\ /e /i /q
if exist "sessions" xcopy sessions %backup_dir%\sessions\ /e /i /q
if exist "downloads" (
    echo  Downloads folder may be large, creating file list instead...
    dir downloads /s /b > %backup_dir%\downloads_list.txt
)
echo.
echo  Backup created: %backup_dir%
echo.
    call :WAIT
goto QUICK_MENU

:ADD_USERNAME
cls
echo  Add Username to Tracking List [offline]
echo  ========================================
echo.
set "new_user="
set /p "new_user=  Username to add (Ctrl+C = cancel): "
if not defined new_user goto QUICK_MENU
"%PYTHON_EXE%" main.py add-username %new_user%
echo.
    call :WAIT
goto QUICK_MENU

REM ══════════════════════════════════════════════════════════════════
REM  MEDIUM
REM ══════════════════════════════════════════════════════════════════

:QUICK_COMPLETE
cls
echo  Quick Spider + Download One User [MEDIUM]
echo  ==========================================
echo  Step 1: Collect followers/following
echo  Step 2: Download media (limit 20 posts)
echo  Uses ONE account only (no rotation).
echo.
set "username="
set /p "username=  Instagram username (Ctrl+C = cancel): "
if not defined username goto QUICK_MENU
"%PYTHON_EXE%" -c "import sys; sys.path.insert(0,'src'); from config import INSTAGRAM_ACCOUNTS; names=','.join(a['name'] for a in INSTAGRAM_ACCOUNTS); print(f'  Your accounts: {names}')" 2>nul
set "account="
set /p "account=  Account name to use (Enter=default): "

echo.
echo  What to collect?
echo   1  Followers + Following
echo   2  Followers only
echo   3  Following only
echo.
set "scollect="
set /p "scollect=  Choose (1-3): "
if not defined scollect set "scollect=1"

echo.
echo  [1/2] Collecting relationships...
set "cmd=%PYTHON_EXE% main.py spider --username %username%"
if defined account set "cmd=!cmd! --account !account!"
if "%scollect%"=="2" set "cmd=!cmd! --max-following 0"
if "%scollect%"=="3" set "cmd=!cmd! --max-followers 0"
%cmd%

echo.
echo  [2/2] Downloading media (limit 20 posts)...
set "cmd=%PYTHON_EXE% main.py download --username %username% --limit 20"
if defined account set "cmd=!cmd! --account !account!"
%cmd%

echo.
echo  Done for %username%!
echo.
    call :WAIT
goto QUICK_MENU

REM ══════════════════════════════════════════════════════════════════
REM  HIGH
REM ══════════════════════════════════════════════════════════════════

:FULL_PIPELINE
cls
echo  Full Pipeline [HIGH]
echo  ====================
echo  1. Test all account logins
echo  2. Spider all users in usernames.txt
echo  3. Download media for all users
echo.
echo  Account: starts with the one you pick, then AUTO-ROTATES
echo  through all accounts when one gets rate-limited.
echo.
"%PYTHON_EXE%" -c "import sys; sys.path.insert(0,'src'); from config import INSTAGRAM_ACCOUNTS; names=','.join(a['name'] for a in INSTAGRAM_ACCOUNTS); print(f'  Your accounts: {names}')" 2>nul
set "account="
set /p "account=  Start with account name (Enter=default): "
echo.
set "confirm="
set /p "confirm=  Continue with full pipeline? (y/n): "
if /i not "!confirm!"=="y" goto QUICK_MENU

echo.
echo  [1/3] Testing account logins...
"%PYTHON_EXE%" main.py test-all

echo.
echo  [2/3] Batch spider...
echo  What to collect?
echo   1  Followers + Following
echo   2  Followers only
echo   3  Following only
echo.
set "scollect="
set /p "scollect=  Choose (1-3): "
if not defined scollect set "scollect=1"

echo.
set "cmd=%PYTHON_EXE% main.py spider --batch"
if defined account set "cmd=!cmd! --account !account!"
if "%scollect%"=="2" set "cmd=!cmd! --max-following 0"
if "%scollect%"=="3" set "cmd=!cmd! --max-followers 0"
%cmd%

echo.
echo  [3/3] Batch download (limit 10 posts per user)...
set "cmd=%PYTHON_EXE% main.py download --batch --limit 10"
if defined account set "cmd=!cmd! --account !account!"
%cmd%

echo.
echo  Full pipeline completed!
echo.
    call :WAIT
goto QUICK_MENU

:SEED_FRESH
cls
echo  Seed from My Accounts [HIGH]
echo  =============================
echo  Clears old data, fetches your accounts' followers/following,
echo  and builds a fresh usernames.txt.
echo.
echo  Your configured accounts:
"%PYTHON_EXE%" -c "import sys; sys.path.insert(0,'src'); from config import INSTAGRAM_ACCOUNTS; [print(f'    {i+1}. {a[\"name\"]}') for i,a in enumerate(INSTAGRAM_ACCOUNTS)]"
echo.
echo  ---- Which accounts? ----
echo   1  All accounts
echo   2  Pick specific accounts
echo   0  Cancel
echo.
set "acct_mode="
set /p "acct_mode=  Choose: "
if not defined acct_mode goto QUICK_MENU
if "%acct_mode%"=="0" goto QUICK_MENU
set "seed_accts_arg="

if "%acct_mode%"=="2" (
    echo.
    echo  Enter account names separated by commas:
    set "seednames="
    set /p "seednames=  Accounts: "
    if not defined seednames goto QUICK_MENU
    set "seed_accts_arg=--seed-accounts !seednames!"
)

echo.
echo  ---- What to collect? ----
echo   1  Followers + Following (both)
echo   2  Followers only
echo   3  Following only
echo.
set "collect_mode="
set /p "collect_mode=  Choose: "
if not defined collect_mode goto QUICK_MENU
set "collect_arg="
if "%collect_mode%"=="2" set "collect_arg=--seed-followers-only"
if "%collect_mode%"=="3" set "collect_arg=--seed-following-only"

echo.
echo  ---- After seeding? ----
echo   1  Seed only (just build usernames.txt)
echo   2  Reset first, then seed only
echo   3  Seed + Spider (build list, then spider all)
echo   4  Reset first, then seed + spider
echo.
set "after_mode="
set /p "after_mode=  Choose: "
if not defined after_mode goto QUICK_MENU

set "cmd=%PYTHON_EXE% main.py spider"
if "%after_mode%"=="1" set "cmd=!cmd! --seed-only"
if "%after_mode%"=="2" set "cmd=!cmd! --reset --seed-only"
if "%after_mode%"=="3" set "cmd=!cmd! --seed"
if "%after_mode%"=="4" set "cmd=!cmd! --reset --seed"
if defined seed_accts_arg set "cmd=!cmd! !seed_accts_arg!"
if defined collect_arg set "cmd=!cmd! !collect_arg!"

echo.
echo  Running: !cmd!
echo.
%cmd%
echo.
    call :WAIT
goto QUICK_MENU

:BATCH_DOWNLOAD
cls
echo  Batch Download [HIGH]
echo  =====================
echo  Account: pick 1 to start, auto-rotates all on rate-limit.
echo.
echo  What to download?
echo   1  Profile photos only
echo   2  Posts only
echo   3  Stories only
echo   4  Highlights only
echo   5  Everything
echo   0  Cancel
echo.
set "media_choice="
set /p "media_choice=  Choose: "
if not defined media_choice goto QUICK_MENU
if "%media_choice%"=="0" goto QUICK_MENU

"%PYTHON_EXE%" -c "import sys; sys.path.insert(0,'src'); from config import INSTAGRAM_ACCOUNTS; names=','.join(a['name'] for a in INSTAGRAM_ACCOUNTS); print(f'  Your accounts: {names}')" 2>nul
set "account="
set /p "account=  Start with account name (Enter=default): "
set "limit="
set /p "limit=  Post limit per user (Enter=no limit): "

set "cmd=%PYTHON_EXE% main.py download --batch"
if defined account set "cmd=!cmd! --account !account!"
if defined limit set "cmd=!cmd! --limit !limit!"
if "%media_choice%"=="1" set "cmd=!cmd! --profile-only"
if "%media_choice%"=="2" set "cmd=!cmd! --posts-only"
if "%media_choice%"=="3" set "cmd=!cmd! --stories-only"
if "%media_choice%"=="4" set "cmd=!cmd! --highlights-only"

echo.
%cmd%
echo.
    call :WAIT
goto QUICK_MENU

REM ══════════════════════════════════════════════════════════════════
REM  DANGER
REM ══════════════════════════════════════════════════════════════════

:DANGER_MENU
cls
echo  Destructive Operations
echo  ======================
echo.
echo   1  Clear all collected data (full reset)
echo   2  Emergency stop all toolkit processes
echo   0  Back
echo.
set "dchoice="
set /p "dchoice=  Choose: "
if not defined dchoice goto QUICK_MENU
if "%dchoice%"=="0" goto QUICK_MENU
if "%dchoice%"=="1" goto CLEAR_DATA
if "%dchoice%"=="2" goto EMERGENCY_STOP
goto DANGER_MENU

:CLEAR_DATA
cls
echo  Clear All Data [DANGER]
echo  =======================
echo.
echo  This deletes ALL collected data:
echo    - All usernames, relationships, profiles in the database
echo    - All progress tracking data
echo    - Any remaining legacy JSON files
echo.
echo  Sessions (logins) will NOT be deleted.
echo.
set "confirm="
set /p "confirm=  Type DELETE to confirm (Ctrl+C = cancel): "
if not defined confirm goto QUICK_MENU
if not "%confirm%"=="DELETE" goto QUICK_MENU

echo.
echo  Clearing database...
"%PYTHON_EXE%" main.py db-reset
echo.
echo  Removing any legacy JSON files...
if exist "data\usernames.txt" del data\usernames.txt
if exist "data\relationships.json" del data\relationships.json
if exist "data\spider_progress.json" del data\spider_progress.json
if exist "data\download_progress.json" del data\download_progress.json
if exist "data\batch_state.json" del data\batch_state.json
if exist "data\account_quotas.json" del data\account_quotas.json
if exist "data\profile_access.json" del data\profile_access.json
if exist "data\users_summary.json" del data\users_summary.json
if exist "data\users_summary.csv" del data\users_summary.csv
if exist "data\selective_download_list.json" del data\selective_download_list.json
if exist "data\following_media_download_state.json" del data\following_media_download_state.json
if exist "data\login_test_report.json" del data\login_test_report.json
echo.
echo  All data cleared. Sessions preserved.
echo.
    call :WAIT
goto QUICK_MENU

:EMERGENCY_STOP
cls
echo  Emergency Stop
echo  ==============
echo.
echo  Stopping all toolkit Python processes...
for /f "tokens=2" %%a in ('wmic process where "CommandLine like '%%main.py%%' and CommandLine like '%%instagram%%'" get ProcessId 2^>nul ^| findstr /r "[0-9]"') do (
    taskkill /pid %%a /f 2>nul
)
echo  Toolkit processes stopped (other Python processes unaffected).
echo.
    call :WAIT
goto QUICK_MENU

REM ── Helper: wait for Enter key (mouse clicks won't dismiss this) ──────────
:WAIT
echo.
echo  ================================================================
set "_dummy="
set /p "_dummy=  Press Enter to continue (or copy any errors above)... "
echo.
goto :EOF
