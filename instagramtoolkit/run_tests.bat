@echo off
setlocal
cd /d "%~dp0"
set "PYTHON_EXE=.venv\Scripts\python.exe"

REM ── Helper: wait for Enter key ────────────────────────────────────────────
:WAIT
echo.
echo  ════════════════════════════════════════════════════════════════
set "_dummy="
set /p "_dummy=  Press Enter to continue (or copy any errors above)... "
echo.
goto :EOF
REM ─────────────────────────────────────────────────────────────────────────

if not exist "%PYTHON_EXE%" (
    echo [ERROR] .venv not found. Run setup.bat first.
    call :WAIT
    exit /b 1
)
title Instagram Toolkit - Run Tests

echo.
echo  ╔══════════════════════════════════════╗
echo  ║   Instagram Toolkit - Test Runner    ║
echo  ╚══════════════════════════════════════╝
echo.
echo  [1] Unit tests only (offline, fast)
echo  [2] Integration tests only (calls API)
echo  [3] All tests (unit + integration)
echo.

set /p "choice=Select [1]: "
if "%choice%"=="" set "choice=1"

if "%choice%"=="1" (
    echo.
    echo  Running unit tests...
    echo  =====================
    echo.
    "%PYTHON_EXE%" -m pytest tests/ -v --tb=short %*
) else if "%choice%"=="2" (
    echo.
    echo  Running integration tests (API calls)...
    echo  =========================================
    echo.
    "%PYTHON_EXE%" -m pytest tests/ -v --tb=short --run-integration -m integration %*
) else if "%choice%"=="3" (
    echo.
    echo  Running ALL tests (unit + integration)...
    echo  ==========================================
    echo.
    "%PYTHON_EXE%" -m pytest tests/ -v --tb=short --run-integration %*
) else (
    echo  Invalid choice.
)

echo.
    call :WAIT
