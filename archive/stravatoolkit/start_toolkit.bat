@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
set "VENV_DIR=%~dp0.venv"
set "VENV_ACTIVATE=%VENV_DIR%\Scripts\activate.bat"
set "PYTHON_EXE=%VENV_DIR%\Scripts\python.exe"

call :ensure_venv_ready
if errorlevel 1 (
  echo CRITICAL: Could not prepare virtual environment.
  pause
  exit /b 1
)

if "%VENV_NEEDS_BOOTSTRAP%"=="1" (
  echo Installing Python dependencies into virtual environment...
  "%PYTHON_EXE%" -m pip install --upgrade pip -q
  "%PYTHON_EXE%" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo CRITICAL: Failed to install Python dependencies.
    pause
    exit /b 1
  )
)


set "COOKIE_FILE=%~dp0cookies.txt"
set "DEFAULT_OUTPUT=%~dp0downloads"
set "DEFAULT_PARALLELISM=1"
set "DEFAULT_YEAR_CAP=25"
set "DEFAULT_WATCH_INTERVAL=15"
set "DEFAULT_WATCH_BACKFILL_STEPS=2"
set "DEEP_BACKFILL_STEPS=100000"
set "DEFAULT_AUTH_MODE=cookiestxt"
set "DEFAULT_AUTH_FALLBACK=auto"

if /I "%~1"=="sync-today" goto sync_today_direct
if /I "%~1"=="sync-date" goto sync_specific_direct
if /I "%~1"=="watch" goto watch_today_direct
if /I "%~1"=="backfill" goto backfill_only_direct
if /I "%~1"=="photos-profiles" goto photos_profiles_direct
if /I "%~1"=="photos-activities" goto photos_activities_direct
if /I "%~1"=="photos-date" goto photos_date_direct
if /I "%~1"=="photos-all" goto photos_all_direct
if /I "%~1"=="backend" goto start_backend_only
if /I "%~1"=="backend-stop" goto stop_backend_only
if /I "%~1"=="backend-status" goto backend_status
if /I "%~1"=="status" goto show_toolkit_status
if /I "%~1"=="build" goto build_frontend
if /I "%~1"=="archive" goto archive_menu
if /I "%~1"=="media" goto media_menu
if /I "%~1"=="viewer" goto viewer_menu
if /I "%~1"=="setup" goto run_setup
if /I "%~1"=="help" goto help_screen

::
:: Interactive mode ? Python menu
::
"%PYTHON_EXE%" main.py
exit /b %errorlevel%

:sync_today_direct
call :resolve_today
call :safe_stop_hint "sync"
call :run_sync_command "!TARGET_DATE!" "0"
exit /b %errorlevel%

:sync_specific_direct
set "TARGET_DATE=%~2"
if "%TARGET_DATE%"=="" (
  echo Missing date. Usage: start_toolkit.bat sync-date YYYY-MM-DD
  exit /b 1
)
call :safe_stop_hint "sync"
call :run_sync_command "%TARGET_DATE%" "0"
exit /b %errorlevel%

:watch_loop
call :resolve_today
echo [%date% %time%] Syncing !TARGET_DATE! and advancing history...
"%PYTHON_EXE%" -m ingestion.main --date !TARGET_DATE! --backfill-steps %BACKFILL_STEPS% --backfill-parallelism !PARALLELISM_VALUE! --backfill-year-cap !YEAR_CAP_VALUE! --auth-mode %DEFAULT_AUTH_MODE% --auth-fallback %DEFAULT_AUTH_FALLBACK% --cookies-file "%COOKIE_FILE%"
echo.
echo Waiting %INTERVAL% minute(s) before the next cycle...
timeout /t %WAIT_SECONDS% >nul
goto watch_loop

:watch_today_direct
set "INTERVAL=%~2"
if "%INTERVAL%"=="" set "INTERVAL=%DEFAULT_WATCH_INTERVAL%"
set "BACKFILL_STEPS=%~3"
if "%BACKFILL_STEPS%"=="" set "BACKFILL_STEPS=%DEFAULT_WATCH_BACKFILL_STEPS%"
set "PARALLELISM_VALUE=%~4"
if "%PARALLELISM_VALUE%"=="" set "PARALLELISM_VALUE=%DEFAULT_PARALLELISM%"
set "YEAR_CAP_VALUE=%~5"
if "%YEAR_CAP_VALUE%"=="" set "YEAR_CAP_VALUE=%DEFAULT_YEAR_CAP%"
set /a WAIT_SECONDS=%INTERVAL%*60
echo.
echo Keeping today fresh every %INTERVAL% minute(s).
echo Press Ctrl+C to stop safely.
echo.
goto watch_loop

:backfill_only_direct
set "PARALLELISM_VALUE=%~2"
if "%PARALLELISM_VALUE%"=="" set "PARALLELISM_VALUE=%DEFAULT_PARALLELISM%"
set "YEAR_CAP_VALUE=%~3"
if "%YEAR_CAP_VALUE%"=="" set "YEAR_CAP_VALUE=%DEFAULT_YEAR_CAP%"
call :safe_stop_hint "historical backfill"
python -m ingestion.main --backfill-only --backfill-steps %DEEP_BACKFILL_STEPS% --backfill-parallelism !PARALLELISM_VALUE! --backfill-year-cap !YEAR_CAP_VALUE! --auth-mode %DEFAULT_AUTH_MODE% --auth-fallback %DEFAULT_AUTH_FALLBACK% --cookies-file "%COOKIE_FILE%"
exit /b %errorlevel%

:show_toolkit_status
echo.
echo Current toolkit status:
echo.
"%PYTHON_EXE%" -m ingestion.status_report
if /I "%~1"=="status" exit /b %errorlevel%
pause
exit /b 0

:photos_profiles_direct
call :set_output_from_arg "%~2"
call :safe_stop_hint "photo download"
python -m ingestion.photo_downloader --mode profiles --output-dir "%OUTPUT_DIR%" --auth-mode %DEFAULT_AUTH_MODE% --cookies-file "%COOKIE_FILE%"
exit /b %errorlevel%

:photos_activities_direct
call :set_output_from_arg "%~2"
call :safe_stop_hint "photo download"
python -m ingestion.photo_downloader --mode activities --output-dir "%OUTPUT_DIR%" --auth-mode %DEFAULT_AUTH_MODE% --cookies-file "%COOKIE_FILE%"
exit /b %errorlevel%

:photos_date_direct
set "TARGET_DATE=%~2"
if "%TARGET_DATE%"=="" (
  echo Missing date. Usage: start_toolkit.bat photos-date YYYY-MM-DD [output-dir]
  exit /b 1
)
call :set_output_from_arg "%~3"
call :safe_stop_hint "photo download"
python -m ingestion.photo_downloader --mode activities --date %TARGET_DATE% --output-dir "%OUTPUT_DIR%" --auth-mode %DEFAULT_AUTH_MODE% --cookies-file "%COOKIE_FILE%"
exit /b %errorlevel%

:photos_all_direct
call :set_output_from_arg "%~2"
call :safe_stop_hint "photo download"
python -m ingestion.photo_downloader --mode all --output-dir "%OUTPUT_DIR%" --auth-mode %DEFAULT_AUTH_MODE% --cookies-file "%COOKIE_FILE%"
exit /b %errorlevel%

:start_backend_only
call :backend_port_guard
if errorlevel 1 exit /b 1
echo.
echo Starting local viewer at http://127.0.0.1:8000
echo Open your browser when you are ready.
echo Close the window or press Ctrl+C to stop it.
echo.
"%PYTHON_EXE%" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
exit /b %errorlevel%

:stop_backend_only
for /f "usebackq delims=" %%i in (`powershell -NoProfile -Command "$conn = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 8000 -ErrorAction SilentlyContinue | Select-Object -First 1; if ($null -eq $conn) { Write-Output 'NONE' } else { Write-Output $conn.OwningProcess }"`) do set "BACKEND_PID=%%i"
if "%BACKEND_PID%"=="NONE" (
  echo.
  echo No local viewer is currently using http://127.0.0.1:8000
  echo.
  exit /b 0
)
echo.
echo Stopping backend process PID %BACKEND_PID% on port 8000...
powershell -NoProfile -Command "Stop-Process -Id %BACKEND_PID% -Force"
echo Viewer stopped.
echo.
exit /b 0

:backend_status
for /f "usebackq delims=" %%i in (`powershell -NoProfile -Command "$conn = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 8000 -ErrorAction SilentlyContinue | Select-Object -First 1; if ($null -eq $conn) { Write-Output 'NONE' } else { $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue; if ($null -eq $proc) { Write-Output ('PID=' + $conn.OwningProcess) } else { Write-Output ('PID=' + $conn.OwningProcess + ';NAME=' + $proc.ProcessName) } }"`) do set "BACKEND_STATUS=%%i"
if "%BACKEND_STATUS%"=="NONE" (
  echo No backend is using port 8000.
) else (
  echo %BACKEND_STATUS%
)
exit /b 0

:build_frontend
echo ======================================================
echo Rebuild Frontend
echo ======================================================
echo.
echo This turns the React frontend into static files served by FastAPI.
echo.
npm --prefix frontend run build
exit /b %errorlevel%

:run_setup
echo ======================================================
echo First-Time Setup
echo ======================================================
echo.

call :ensure_venv_ready
if errorlevel 1 (
  echo Failed to prepare virtual environment. Aborting setup.
  pause
  goto main_menu
)

if "%VENV_NEEDS_BOOTSTRAP%"=="1" (
  echo Virtual environment created.
  echo.
)

echo Activating virtual environment...
call "%VENV_ACTIVATE%"

echo Installing Python dependencies into virtual environment
"%PYTHON_EXE%" -m pip install --upgrade pip -q
"%PYTHON_EXE%" -m pip install -r requirements.txt
if errorlevel 1 echo Failed to install dependencies. & pause & exit /b 1
echo Dependencies installed successfully.
echo.
echo Installing Playwright browser runtime
"%PYTHON_EXE%" -m playwright install chromium
if errorlevel 1 pause & exit /b 1
echo.
echo Installing frontend dependencies
npm --prefix frontend install
if errorlevel 1 pause & exit /b 1
echo.
call :confirm_cookie_file
call :prompt_bootstrap_auth
echo.
echo Setup complete.
pause
exit /b 0

:help_screen
cls
echo ======================================================
echo Help
echo ======================================================
echo.
echo Main archive behaviour:
echo   - Sync today once is the fastest manual refresh.
echo   - Keep today fresh repeats sync plus a small backfill batch.
echo   - Deepen saved history is the heavy catch-up tool.
echo   - Your own athlete account is included in tracking and backfill.
echo.
echo Authentication:
echo   - The batch workflow prioritizes cookies.txt first.
echo   - If the active session expires, the toolkit can auto-recover from saved session state.
echo   - If needed, it can prompt Playwright using real Chrome or Edge before bundled Chromium.
echo   - Successful Playwright recovery updates cookies.txt automatically.
echo   - First-time setup can check for cookies.txt and offer a Playwright sign-in immediately.
echo.
echo Python environment:
echo   - The toolkit detects shared-Python dependency drift and summarizes it once as [env-warning].
echo   - The safest long-term fix is a project virtual environment or aligned global package versions.
echo.
echo Backfill rules:
echo   - Backfill is month-based, not time-budget based.
echo   - It can scrape several athletes in parallel, but the batch default is now 1.
echo   - It stops at January of the earliest allowed year.
echo   - With the default 25-year cap, a run in 2026 stops at 2001-01.
echo   - Watch mode now defaults to a 15 minute interval and only 2 backfill steps per cycle.
echo   - Failure logs now report specific causes such as HTTP 429, login redirect, blank page, or parse failure.
echo.
echo Safe stopping:
echo   - For sync, backfill, and media downloads, press Ctrl+C.
echo   - Finished work stays saved and the next run resumes from saved progress.
echo.
echo Viewer app:
echo   - Start local viewer runs the app at http://127.0.0.1:8000
echo   - Rebuild frontend is only needed after frontend code changes.
echo.
echo Media downloads:
echo   - If you do not choose a path, files go into the repo downloads folder.
echo.
exit /b 0

:confirm_cookie_file
echo Checking for cookies.txt in this toolkit folder...
if exist "%COOKIE_FILE%" (
  echo Found cookies.txt at:
  echo   %COOKIE_FILE%
  set "COOKIE_CONFIRM="
  set /p COOKIE_CONFIRM=Use this cookies.txt as the main browser cookie source? [Y/n]: 
  if /I "%COOKIE_CONFIRM%"=="N" (
    echo This toolkit still expects cookies.txt at:
    echo   %COOKIE_FILE%
    echo Please place or update the file there before running sync or backfill with cookies.txt.
  ) else (
    echo cookies.txt confirmed.
  )
) else (
  echo No cookies.txt was found at:
  echo   %COOKIE_FILE%
  echo You can still continue and sign in with Playwright now, or add cookies.txt later.
)
exit /b 0

:prompt_bootstrap_auth
echo.
set "AUTH_BOOTSTRAP="
set /p AUTH_BOOTSTRAP=Open Playwright sign-in now to capture a fresh Strava session? [Y/n]: 
if /I "%AUTH_BOOTSTRAP%"=="N" exit /b 0
echo.
echo A real Chrome or Edge browser will be tried first for sign-in.
echo If sign-in succeeds, the toolkit will save the session and update cookies.txt automatically.
echo.
"%PYTHON_EXE%" -m ingestion.tools.auth.bootstrap --auth-mode playwright --auth-fallback auto --cookies-file "%COOKIE_FILE%"
if errorlevel 1 (
  echo.
  echo Playwright sign-in did not complete successfully.
  echo You can still add or update cookies.txt manually at:
  echo   %COOKIE_FILE%
)
exit /b 0

:run_sync_command
set "SYNC_DATE=%~1"
set "SYNC_REFRESH=%~2"
set "SYNC_EXTRA_ARGS="
if "%SYNC_REFRESH%"=="1" set "SYNC_EXTRA_ARGS=--refresh-following-roster"
"%PYTHON_EXE%" -m ingestion.main --date %SYNC_DATE% --sync-only %SYNC_EXTRA_ARGS% --auth-mode %DEFAULT_AUTH_MODE% --auth-fallback %DEFAULT_AUTH_FALLBACK% --cookies-file "%COOKIE_FILE%"
exit /b %errorlevel%

:set_output_from_arg
set "OUTPUT_DIR=%~1"
if "%OUTPUT_DIR%"=="" set "OUTPUT_DIR=%DEFAULT_OUTPUT%"
if not exist "%OUTPUT_DIR%" mkdir "%OUTPUT_DIR%"
exit /b 0

:resolve_today
for /f %%i in ('powershell -NoProfile -Command "(Get-Date).ToString('yyyy-MM-dd')"') do set TARGET_DATE=%%i
exit /b 0

:backend_port_guard
for /f "usebackq delims=" %%i in (`powershell -NoProfile -Command "$conn = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 8000 -ErrorAction SilentlyContinue | Select-Object -First 1; if ($null -eq $conn) { Write-Output 'FREE' } else { $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue; if ($null -eq $proc) { Write-Output ('USED|PID=' + $conn.OwningProcess) } else { Write-Output ('USED|PID=' + $conn.OwningProcess + '|NAME=' + $proc.ProcessName) } }"`) do set "PORT_STATUS=%%i"
if "%PORT_STATUS%"=="FREE" exit /b 0
echo.
echo Port 8000 is already in use.
for %%A in ("%PORT_STATUS:|=" "%") do echo %%~A
echo.
echo Stop the running viewer first.
echo.
exit /b 1

:safe_stop_hint
echo.
echo Safe stop for %~1: press Ctrl+C if you want to stop halfway.
echo Finished work stays saved, data stays consistent, and the next run resumes from the last committed point.
echo.
exit /b 0

:resolve_base_python
set "BASE_PYTHON_SOURCE="
set "BASE_PYTHON_DESC="
py -3 --version >nul 2>&1
if not errorlevel 1 (
  set "BASE_PYTHON_SOURCE=py"
  set "BASE_PYTHON_DESC=py -3"
  exit /b 0
)

python -c "import sys; raise SystemExit(0 if sys.version_info.major == 3 else 1)" >nul 2>&1
if not errorlevel 1 (
  set "BASE_PYTHON_SOURCE=python"
  set "BASE_PYTHON_DESC=python"
  exit /b 0
)

echo Could not find a usable Python 3 interpreter.
echo Install Python 3 and ensure either py -3 or python is available in PATH.
exit /b 1

:ensure_venv_ready
set "VENV_NEEDS_BOOTSTRAP=0"

if not exist "%PYTHON_EXE%" (
  if exist "venv\Scripts\python.exe" (
    ren venv .venv
  )
)

if exist "%PYTHON_EXE%" (
  "%PYTHON_EXE%" --version >nul 2>&1
  if errorlevel 1 (
    echo Existing .venv is broken and will be recreated.
    rmdir /s /q "%VENV_DIR%"
  )
)

if not exist "%PYTHON_EXE%" (
  call :resolve_base_python
  if errorlevel 1 exit /b 1

  echo Creating Python virtual environment using !BASE_PYTHON_DESC!...
  if /I "!BASE_PYTHON_SOURCE!"=="py" (
    py -3 -m venv "%VENV_DIR%"
  ) else (
    python -m venv "%VENV_DIR%"
  )

  if errorlevel 1 (
    echo Failed to create virtual environment.
    exit /b 1
  )

  set "VENV_NEEDS_BOOTSTRAP=1"
)
exit /b 0

:check_and_heal_venv
rem Check venv health and auto-heal if issues are found
"%PYTHON_EXE%" -m ingestion.main --check-venv --skip-venv-check >nul 2>&1
if errorlevel 1 (
    echo ======================================================
    echo VENV HEALTH ISSUES DETECTED
    echo ======================================================
    echo.
    echo Attempting automatic fix...
    echo.
    "%PYTHON_EXE%" -m ingestion.main --heal-venv --skip-venv-check
    if errorlevel 1 (
        echo ======================================================
        echo VENV HEAL FAILED
        echo ======================================================
        echo.
        echo Please run manually: start_toolkit.bat setup
        echo.
        exit /b 1
    )
    echo.
    echo Venv health restored. Continuing...
    echo.
)
exit /b 0
