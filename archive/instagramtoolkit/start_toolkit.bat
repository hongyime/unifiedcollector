@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
set "PYTHON_EXE=.venv\Scripts\python.exe"
title Instagram Toolkit
REM Force UTF-8 output so emojis render correctly in Windows Terminal
set PYTHONUTF8=1
chcp 65001 >nul 2>&1

if not exist "%PYTHON_EXE%" (
    echo [ERROR] .venv not found. Run setup.bat first.
    call :WAIT
    exit /b 1
)

REM ══════════════════════════════════════════════════════════════════
REM  MAIN MENU
REM ══════════════════════════════════════════════════════════════════
:MAIN_MENU
cls
echo.
echo   Unified Instagram Toolkit
echo   ================================================================
echo.
echo    #   Action                          Account          Risk
echo   ---  ------------------------------  ---------------  ------
echo    1   List configured accounts        no login         SAFE
echo    2   Analyze collected data          no login         SAFE
echo    3   View access statistics          no login         SAFE
echo    4   Priority analysis               no login         SAFE
echo    5   Progress manager                no login         SAFE
echo    6   Reset ALL data                  no login         DANGER
echo   ---  ------------------------------  ---------------  ------
echo    7   Login to one account            you pick 1       LOW
echo    8   Test all account logins         tests each       LOW
echo   ---  ------------------------------  ---------------  ------
echo    9   Spider one user                 you pick 1       MEDIUM
echo   10   Download one user's media       you pick 1       MEDIUM
echo   11   Download from 1 followed user   you pick 1       MEDIUM
echo   ---  ------------------------------  ---------------  ------
echo   12   Spider batch (all users)        rotates all      HIGH
echo   13   Seed from my accounts           all / custom     HIGH
echo   14   Download media batch            rotates all      HIGH
echo   15   Following download ALL          you pick 1       HIGH
echo   16   Selective download manager      rotates all      HIGH
echo   17   Browser download (stealth)      you pick 1       HIGH
echo   18   Browser batch download          rotates all      HIGH
echo   ---  ------------------------------  ---------------  ------
echo   19   Quick Actions menu
echo   20   Run custom command
echo    0   Exit
echo.
echo   Type a number and press Enter.  0 = exit.  Ctrl+C = cancel.
echo.
set "choice="
set /p "choice=  Enter choice: "
if not defined choice goto MAIN_MENU

if "%choice%"=="1" goto LIST_ACCOUNTS
if "%choice%"=="2" goto ANALYZE
if "%choice%"=="3" goto ACCESS_STATS
if "%choice%"=="4" goto PRIORITY_ANALYSIS
if "%choice%"=="5" goto PROGRESS
if "%choice%"=="6" goto RESET_DATA
if "%choice%"=="7" goto LOGIN_ONE
if "%choice%"=="8" goto TEST_ALL
if "%choice%"=="9" goto SPIDER_SINGLE
if "%choice%"=="10" goto DOWNLOAD_SINGLE
if "%choice%"=="11" goto FOLLOWING_SINGLE
if "%choice%"=="12" goto SPIDER_BATCH
if "%choice%"=="13" goto SEED_ACCOUNTS
if "%choice%"=="14" goto DOWNLOAD_BATCH
if "%choice%"=="15" goto FOLLOWING_ALL
if "%choice%"=="16" goto SELECTIVE_DOWNLOAD
if "%choice%"=="17" goto BROWSER_DOWNLOAD_SINGLE
if "%choice%"=="18" goto BROWSER_DOWNLOAD_BATCH
if "%choice%"=="19" goto QUICK_ACTIONS
if "%choice%"=="20" goto CUSTOM_CMD
if "%choice%"=="0" goto EXIT
goto MAIN_MENU

REM ══════════════════════════════════════════════════════════════════
REM  SAFE  (no API calls)
REM ══════════════════════════════════════════════════════════════════

:LIST_ACCOUNTS
cls
echo  List Configured Accounts
echo  ========================
echo.
"%PYTHON_EXE%" main.py list
echo.
    call :WAIT
goto MAIN_MENU

:ANALYZE
cls
echo  Analyze Collected Data
echo  ======================
echo.
echo   1  Analyze local relationship data (offline, fast)
echo   2  Fetch profile metadata from Instagram (public/private, follower counts)
echo      ^ Uses 1 API call per user — run after spider to enrich data
echo   0  Back
echo.
set "achoice="
set /p "achoice=  Choose: "
if "%achoice%"=="0" goto MAIN_MENU
if "%achoice%"=="2" (
    set "account="
    set "limit="
    set /p "account=  Account to use (Enter = default): "
    set /p "limit=  Max profiles to fetch (Enter = all): "
    set "cmd="%PYTHON_EXE%" main.py analyze-profiles --fetch"
    if defined account set "cmd=!cmd! --account !account!"
    if defined limit set "cmd=!cmd! --limit !limit!"
    !cmd!
) else (
    "%PYTHON_EXE%" main.py analyze
    echo.
    echo  Reports saved to data\users_summary.json and .csv
)
echo.
    call :WAIT
goto MAIN_MENU

:ACCESS_STATS
cls
echo  Profile Access Statistics [offline]
echo  ===================================
echo.
"%PYTHON_EXE%" main.py access-stats
echo.
    call :WAIT
goto MAIN_MENU

:PRIORITY_ANALYSIS
cls
echo  Priority Analysis [offline]
echo  ===========================
echo.
"%PYTHON_EXE%" -c "import sys; sys.path.insert(0,'src'); from config import INSTAGRAM_ACCOUNTS; names=','.join(a['name'] for a in INSTAGRAM_ACCOUNTS); print(f'  Your accounts: {names}')" 2>nul
set "account="
set /p "account=  Account name (Enter=default): "
if not defined account (
    "%PYTHON_EXE%" main.py priority-analysis
) else (
    "%PYTHON_EXE%" main.py priority-analysis --account %account%
)
echo.
    call :WAIT
goto MAIN_MENU

:PROGRESS
cls
echo  Progress Manager [offline]
echo  ==========================
echo.
echo   1  Show progress for all operations
echo   2  Resume spider
echo   3  Resume download
echo   4  Retry failed spider users
echo   5  Retry failed download users
echo   6  Retry rate-limited download queue
echo   7  Clear spider progress
echo   8  Clear download progress
echo   9  Clear ALL progress
echo   0  Back
echo.
set "pchoice="
set /p "pchoice=  Choose: "
if not defined pchoice goto PROGRESS
if "%pchoice%"=="0" goto MAIN_MENU
if "%pchoice%"=="1" (
    "%PYTHON_EXE%" main.py progress show
)
if "%pchoice%"=="2" (
    "%PYTHON_EXE%" main.py progress resume --operation spider
)
if "%pchoice%"=="3" (
    "%PYTHON_EXE%" main.py progress resume --operation download
)
if "%pchoice%"=="4" (
    "%PYTHON_EXE%" main.py progress resume --operation spider --retry-failed
)
if "%pchoice%"=="5" (
    "%PYTHON_EXE%" main.py progress resume --operation download --retry-failed
)
if "%pchoice%"=="6" (
    "%PYTHON_EXE%" main.py retry-queue
)
if "%pchoice%"=="7" (
    "%PYTHON_EXE%" main.py progress clear --operation spider --confirm
)
if "%pchoice%"=="8" (
    "%PYTHON_EXE%" main.py progress clear --operation download --confirm
)
if "%pchoice%"=="9" (
    echo.
    set "confirm="
    set /p "confirm=  Clear ALL progress? Type YES: "
    if /i "!confirm!"=="YES" "%PYTHON_EXE%" main.py progress clear --confirm
)
echo.
    call :WAIT
goto PROGRESS

:RESET_DATA
cls
echo  Reset ALL Data [DANGER]
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
if not defined confirm goto MAIN_MENU
if not "%confirm%"=="DELETE" goto MAIN_MENU
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
goto MAIN_MENU

REM ══════════════════════════════════════════════════════════════════
REM  LOW  (account management)
REM ══════════════════════════════════════════════════════════════════

:LOGIN_ONE
cls
echo  Login to One Account [LOW]
echo  ==========================
echo.
python main.py list
echo.
set "account="
set /p "account=  Account name to login, e.g. b or sbs (Ctrl+C = cancel): "
if not defined account goto MAIN_MENU
echo.
"%PYTHON_EXE%" main.py login %account%
echo.
    call :WAIT
goto MAIN_MENU

:TEST_ALL
cls
echo  Test All Account Logins [LOW]
echo  =============================
echo.
"%PYTHON_EXE%" main.py test-all
echo.
    call :WAIT
goto MAIN_MENU

REM ══════════════════════════════════════════════════════════════════
REM  MEDIUM  (single-user, 1 account, no rotation)
REM ══════════════════════════════════════════════════════════════════

:SPIDER_SINGLE
cls
echo  Spider One User [MEDIUM]
echo  ========================
echo  Fetches 1 profile + enumerates followers/following.
echo  Uses ONE account only (no rotation).
echo  Default limits: 1000 followers, 1000 following.
echo  ^(Use option 18 for custom limits^)
echo.
set "username="
set /p "username=  Instagram username to spider (Ctrl+C = cancel): "
if not defined username goto MAIN_MENU
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
echo  Spidering %username%...
set "cmd="%PYTHON_EXE%" main.py spider --username %username%"
if defined account set "cmd=!cmd! --account !account!"
if "%scollect%"=="2" set "cmd=!cmd! --max-following 0"
if "%scollect%"=="3" set "cmd=!cmd! --max-followers 0"
%cmd%
echo.
    call :WAIT
goto MAIN_MENU

:DOWNLOAD_SINGLE
cls
echo  Download One User's Media [MEDIUM]
echo  ===================================
echo  Uses ONE account only (no rotation).
echo.
set "username="
set /p "username=  Instagram username (Ctrl+C = cancel): "
if not defined username goto MAIN_MENU
"%PYTHON_EXE%" -c "import sys; sys.path.insert(0,'src'); from config import INSTAGRAM_ACCOUNTS; names=','.join(a['name'] for a in INSTAGRAM_ACCOUNTS); print(f'  Your accounts: {names}')" 2>nul
set "account="
set /p "account=  Account name to use (Enter=default): "
echo.
echo  What to download?
echo   1  Everything (posts + stories + highlights + profile photo)
echo   2  Profile photo only
echo   3  Posts only
echo   4  Stories only
echo   5  Highlights only
echo.
set "mtype="
set /p "mtype=  Choose (1-5): "
if not defined mtype goto MAIN_MENU
echo.
set "limit="
set /p "limit=  Post limit (Enter=no limit): "

set "cmd="%PYTHON_EXE%" main.py download --username %username%"
if defined account set "cmd=!cmd! --account !account!"
if "%mtype%"=="2" set "cmd=!cmd! --profile-only"
if "%mtype%"=="3" set "cmd=!cmd! --posts-only"
if "%mtype%"=="4" set "cmd=!cmd! --stories-only"
if "%mtype%"=="5" set "cmd=!cmd! --highlights-only"
if defined limit set "cmd=!cmd! --limit !limit!"
echo.
%cmd%
echo.
    call :WAIT
goto MAIN_MENU

:FOLLOWING_SINGLE
cls
echo  Download from One Followed User [MEDIUM]
echo  =========================================
echo  Downloads media from a specific user you follow.
echo  You pick 1 account interactively (no rotation).
echo.
set "username="
set /p "username=  Followed username (Ctrl+C = cancel): "
if not defined username goto MAIN_MENU
echo.
"%PYTHON_EXE%" main.py following-download --username %username%
echo.
    call :WAIT
goto MAIN_MENU

REM ══════════════════════════════════════════════════════════════════
REM  HIGH  (batch operations, heavy API)
REM ══════════════════════════════════════════════════════════════════

:SPIDER_BATCH
cls
echo  Spider Batch [HIGH]
echo  ====================
echo  Fetches profile + followers/following for every user in
echo  usernames.txt. Most API-intensive operation.
echo.
echo  Account: starts with the one you pick, then AUTO-ROTATES
echo  through all configured accounts when one gets rate-limited.
echo.
"%PYTHON_EXE%" -c "import sys; sys.path.insert(0,'src'); from config import INSTAGRAM_ACCOUNTS; names=','.join(a['name'] for a in INSTAGRAM_ACCOUNTS); print(f'  Your accounts: {names}')" 2>nul
set "account="
set /p "account=  Start with account name (Enter=default): "
set "maxusers="
set /p "maxusers=  Max users to process (Enter=no limit): "
echo.
echo  Priority filter?
echo   1  Process all users
echo   2  High-priority only (your followers/following first)
echo.
set "prio="
set /p "prio=  Choose (1-2): "
if not defined prio set "prio=1"

echo.
echo  What to collect?
echo   1  Followers + Following
echo   2  Followers only
echo   3  Following only
echo.
set "scollect="
set /p "scollect=  Choose (1-3): "
if not defined scollect set "scollect=1"

set "cmd="%PYTHON_EXE%" main.py spider --batch"
if defined account set "cmd=!cmd! --account !account!"
REM Only add --max-users if it's a number (not empty or "all")
if defined maxusers (
    if not "!maxusers!"=="all" (
        set "cmd=!cmd! --max-users !maxusers!"
    )
)
if "%prio%"=="2" set "cmd=!cmd! --high-priority-only"
if "%scollect%"=="2" set "cmd=!cmd! --max-following 0"
if "%scollect%"=="3" set "cmd=!cmd! --max-followers 0"
echo.
%cmd%
echo.
    call :WAIT
goto MAIN_MENU

:SEED_ACCOUNTS
cls
echo  Seed from My Accounts [HIGH]
echo  =============================
echo  Logs into your accounts, fetches their followers and/or
echo  following, and combines them into usernames.txt.
echo.
echo  Your configured accounts:
"%PYTHON_EXE%" -c "import sys; sys.path.insert(0,'src'); from config import INSTAGRAM_ACCOUNTS; [print(f'    {i+1}. {a[\"name\"]}') for i,a in enumerate(INSTAGRAM_ACCOUNTS)]"
echo.
echo  ---- Step 1: Which accounts? ----
echo   1  All accounts
echo   2  Pick specific accounts
echo   0  Cancel
echo.
set "acct_mode="
set /p "acct_mode=  Choose: "
if not defined acct_mode goto MAIN_MENU
if "%acct_mode%"=="0" goto MAIN_MENU
set "seed_accts_arg="

if "%acct_mode%"=="2" (
    echo.
    echo  Enter account names separated by commas
    echo  ^(use the short name, e.g. b,oops^)
    echo.
    set "seednames="
    set /p "seednames=  Accounts: "
    if not defined seednames goto MAIN_MENU
    set "seed_accts_arg=--seed-accounts !seednames!"
)

echo.
echo  ---- Step 2: What to collect? ----
echo   1  Followers + Following (both)
echo   2  Followers only
echo   3  Following only
echo.
set "collect_mode="
set /p "collect_mode=  Choose: "
if not defined collect_mode goto MAIN_MENU
set "collect_arg="
if "%collect_mode%"=="2" set "collect_arg=--seed-followers-only"
if "%collect_mode%"=="3" set "collect_arg=--seed-following-only"

echo.
echo  ---- Step 3: After seeding? ----
echo   1  Seed only (just build usernames.txt)
echo   2  Seed + Spider (build list, then spider all)
echo   3  Reset first, then seed + spider
echo   4  Reset first, then seed only
echo.
set "after_mode="
set /p "after_mode=  Choose: "
if not defined after_mode goto MAIN_MENU

set "cmd="%PYTHON_EXE%" main.py spider"
if "%after_mode%"=="1" set "cmd=!cmd! --seed-only"
if "%after_mode%"=="2" set "cmd=!cmd! --seed"
if "%after_mode%"=="3" set "cmd=!cmd! --reset --seed"
if "%after_mode%"=="4" set "cmd=!cmd! --reset --seed-only"
if defined seed_accts_arg set "cmd=!cmd! !seed_accts_arg!"
if defined collect_arg set "cmd=!cmd! !collect_arg!"

echo.
echo  Running: !cmd!
echo.
%cmd%
echo.
    call :WAIT
goto MAIN_MENU

:DOWNLOAD_BATCH
cls
echo  Download Media Batch [HIGH]
echo  ===========================
echo  Downloads posts/stories/highlights/profile photos
echo  for every user in usernames.txt.
echo.
echo  Account: starts with the one you pick, then AUTO-ROTATES
echo  through all configured accounts when one gets rate-limited.
echo.
"%PYTHON_EXE%" -c "import sys; sys.path.insert(0,'src'); from config import INSTAGRAM_ACCOUNTS; names=','.join(a['name'] for a in INSTAGRAM_ACCOUNTS); print(f'  Your accounts: {names}')" 2>nul
set "account="
set /p "account=  Start with account name (Enter=default): "
set "limit="
set /p "limit=  Post limit per user (Enter=no limit): "
echo.
echo  Priority filter?
echo   1  Download for all users
echo   2  High-priority only
echo.
set "prio="
set /p "prio=  Choose (1-2): "
if not defined prio set "prio=1"

set "cmd="%PYTHON_EXE%" main.py download --batch"
if defined account set "cmd=!cmd! --account !account!"
if defined limit set "cmd=!cmd! --limit !limit!"
if "%prio%"=="2" set "cmd=!cmd! --high-priority-only"
echo.
%cmd%
echo.
    call :WAIT
goto MAIN_MENU

:FOLLOWING_ALL
cls
echo  Following Download [HIGH]
echo  =========================
echo  Downloads media from accounts you follow.
echo  You pick 1 account interactively (no rotation).
echo  Only downloads from THAT account's following list.
echo.
echo   1  Interactive menu (recommended)
echo   2  Download from ALL followed accounts
echo   3  Show download progress
echo   4  Reset download progress
echo   0  Back
echo.
set "fchoice="
set /p "fchoice=  Choose: "
if not defined fchoice goto FOLLOWING_ALL
if "%fchoice%"=="0" goto MAIN_MENU
if "%fchoice%"=="1" (
    "%PYTHON_EXE%" main.py following-download --interactive
    echo.
    call :WAIT
    goto FOLLOWING_ALL
)
if "%fchoice%"=="2" (
    echo.
    set "confirm="
    set /p "confirm=  Download from ALL followed accounts? (y/n): "
    if /i "!confirm!"=="y" (
        "%PYTHON_EXE%" main.py following-download --all
    )
    echo.
    call :WAIT
    goto FOLLOWING_ALL
)
if "%fchoice%"=="3" (
    "%PYTHON_EXE%" main.py following-download --progress
    echo.
    call :WAIT
    goto FOLLOWING_ALL
)
if "%fchoice%"=="4" (
    "%PYTHON_EXE%" main.py following-download --reset
    echo.
    call :WAIT
    goto FOLLOWING_ALL
)
goto FOLLOWING_ALL

:SELECTIVE_DOWNLOAD
cls
echo  Selective Download Manager [HIGH]
echo  ==================================
echo  Hand-pick usernames to download media for.
echo  Single user = 1 account.  Multiple = auto-rotates all.
echo.
echo   1  Select usernames interactively
echo   2  Show current selection
echo   3  Add a username
echo   4  Remove a username
echo   5  Clear selection
echo   6  Download from selection
echo   0  Back
echo.
set "sdchoice="
set /p "sdchoice=  Choose: "
if not defined sdchoice goto SELECTIVE_DOWNLOAD
if "%sdchoice%"=="0" goto MAIN_MENU
if "%sdchoice%"=="1" (
    "%PYTHON_EXE%" main.py selective-download --select
    echo.
    call :WAIT
    goto SELECTIVE_DOWNLOAD
)
if "%sdchoice%"=="2" (
    "%PYTHON_EXE%" main.py selective-download --list
    echo.
    call :WAIT
    goto SELECTIVE_DOWNLOAD
)
if "%sdchoice%"=="3" (
    set "adduser="
    set /p "adduser=  Username to add: "
    if defined adduser "%PYTHON_EXE%" main.py selective-download --add !adduser!
    echo.
    call :WAIT
    goto SELECTIVE_DOWNLOAD
)
if "%sdchoice%"=="4" (
    set "rmuser="
    set /p "rmuser=  Username to remove: "
    if defined rmuser "%PYTHON_EXE%" main.py selective-download --remove !rmuser!
    echo.
    call :WAIT
    goto SELECTIVE_DOWNLOAD
)
if "%sdchoice%"=="5" (
    "%PYTHON_EXE%" main.py selective-download --clear
    echo.
    call :WAIT
    goto SELECTIVE_DOWNLOAD
)
if "%sdchoice%"=="6" (
    "%PYTHON_EXE%" -c "import sys; sys.path.insert(0,'src'); from config import INSTAGRAM_ACCOUNTS; names=','.join(a['name'] for a in INSTAGRAM_ACCOUNTS); print(f'  Your accounts: {names}')" 2>nul
    set "account="
    set /p "account=  Account name to use (Enter=default): "
    if defined account (
        "%PYTHON_EXE%" main.py selective-download --download --account !account!
    ) else (
        "%PYTHON_EXE%" main.py selective-download --download
    )
    echo.
    call :WAIT
    goto SELECTIVE_DOWNLOAD
)
goto SELECTIVE_DOWNLOAD

REM ══════════════════════════════════════════════════════════════════
REM  BROWSER STEALTH DOWNLOAD
REM ══════════════════════════════════════════════════════════════════

:BROWSER_DOWNLOAD_SINGLE
cls
echo  Browser Download (Stealth) [HIGH]
echo  ===================================
echo  Uses a real Chrome browser to download media.
echo  First run: a browser window opens for you to log in.
echo  Subsequent runs: headless, reuses saved session.
echo.
set /p "username=  Instagram username to download (Ctrl+C = cancel): "
if not defined username goto MAIN_MENU
set /p "account=  Account name (Enter = default): "
set /p "limit=  Max posts (Enter = all): "
echo.
set "cmd="%PYTHON_EXE%" main.py download --browser --username %username%"
if defined account set "cmd=!cmd! --account !account!"
if defined limit set "cmd=!cmd! --limit !limit!"
echo  Running: !cmd!
echo.
!cmd!
echo.
pause
goto MAIN_MENU

:BROWSER_DOWNLOAD_BATCH
cls
echo  Browser Batch Download (Stealth) [HIGH]
echo  =========================================
echo  Downloads media for all tracked usernames using the browser.
echo  Uses browser-fallback mode: tries Instaloader first (fast),
echo  switches to browser automatically if challenged/blocked.
echo.
set /p "account=  Account name (Enter = default): "
set /p "limit=  Max posts per user (Enter = all): "
echo.
set "cmd="%PYTHON_EXE%" main.py download --browser --batch"
if defined account set "cmd=!cmd! --account !account!"
if defined limit set "cmd=!cmd! --limit !limit!"
echo  Running: !cmd!
echo.
!cmd!
echo.
pause
goto MAIN_MENU

REM ══════════════════════════════════════════════════════════════════
REM  OTHER
REM ══════════════════════════════════════════════════════════════════

:QUICK_ACTIONS
cls
call quick_actions.bat
goto MAIN_MENU

:CUSTOM_CMD
cls
echo  Run Custom Command
echo  ===================
echo  Type any arguments after "python main.py".
echo.
echo  Examples:
echo    spider --username target --max-followers 500
echo    download --batch --posts-only --limit 20
echo    spider --reset --seed-only
echo    spider --seed --seed-accounts b --seed-followers-only
echo.
set "custom_cmd="
set /p "custom_cmd=  python main.py "
if not defined custom_cmd goto MAIN_MENU
echo.
"%PYTHON_EXE%" main.py %custom_cmd%
echo.
    call :WAIT
goto MAIN_MENU

:EXIT
echo.
echo  Goodbye!
timeout /t 1 >nul
exit

REM ── Helper: wait for Enter key (mouse clicks won't dismiss this) ──────────
:WAIT
echo.
echo  ================================================================
set "_dummy="
set /p "_dummy=  Press Enter to continue (or copy any errors above)... "
echo.
goto :EOF
