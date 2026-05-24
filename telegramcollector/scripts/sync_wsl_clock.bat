@echo off
setlocal EnableExtensions

REM ============================================
REM WSL2 + Docker Clock Recovery for Telethon
REM ============================================
REM Why this script exists:
REM - WSL2 clock can drift after sleep/hibernate
REM - MTProto is time-sensitive; drift can cause "message too old/new"
REM - Even after time is fixed, stale session update_state can keep failing
REM
REM This script performs a full recovery sequence:
REM   1) (Best effort) resync Windows host clock
REM   2) hard refresh WSL clock via `wsl --shutdown`
REM   3) verify Docker/WSL UTC time
REM   4) clear stale Telethon update_state in local session files
REM ============================================

echo [WSL Clock Sync] Starting full clock/session recovery...

echo [1/4] Resync Windows host clock (best effort)...
where w32tm >nul 2>&1
if %ERRORLEVEL% EQU 0 (
	w32tm /resync /force >nul 2>&1
	if %ERRORLEVEL% EQU 0 (
		echo   [OK] Windows clock resynced.
	) else (
		echo   [WARN] Windows clock resync failed (try running as Administrator).
	)
) else (
	echo   [WARN] w32tm not available.
)

echo [2/4] Restarting WSL to refresh kernel clock...
wsl --shutdown >nul 2>&1
if %ERRORLEVEL% EQU 0 (
	echo   [OK] WSL shutdown complete.
) else (
	echo   [WARN] wsl --shutdown failed.
)

timeout /t 3 /nobreak >nul

echo [3/4] Checking UTC time inside docker-desktop distro:
wsl -d docker-desktop -e sh -c "date -u" 2>nul
if %ERRORLEVEL% NEQ 0 (
	echo   [WARN] Could not query docker-desktop distro time (is Docker Desktop running?).
)

echo [3/4] Checking UTC time from Docker engine:
docker run --rm alpine date -u 2>nul
if %ERRORLEVEL% NEQ 0 (
	echo   [WARN] Docker engine not ready yet. Start Docker Desktop, then rerun this script.
)

echo [4/4] Clearing stale Telethon session update_state...
if exist "%~dp0fix_session_time.py" (
	py -3 "%~dp0fix_session_time.py" >nul 2>&1
	if %ERRORLEVEL% NEQ 0 (
		python "%~dp0fix_session_time.py"
		if %ERRORLEVEL% NEQ 0 (
			echo   [WARN] Could not run fix_session_time.py (Python launcher unavailable).
		) else (
			echo   [OK] Session state cleanup completed.
		)
	) else (
		echo   [OK] Session state cleanup completed.
	)
) else (
	echo   [WARN] scripts\fix_session_time.py not found. Skipping session cleanup.
)

echo [WSL Clock Sync] Windows UTC time:
powershell -command "Get-Date -Format 'yyyy-MM-dd HH:mm:ss UTC' -AsUTC"

echo [WSL Clock Sync] Recovery complete.
echo [WSL Clock Sync] Next step: restart your docker compose stack.

endlocal
