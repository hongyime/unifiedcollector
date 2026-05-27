@echo off
echo ========================================
echo PRODUCTION READINESS VERIFICATION
echo ========================================
echo.

echo Checking project structure...
echo.

echo [1/8] Checking scripts folder...
if exist "scripts\diagnose.bat" (
    echo   ✓ scripts\diagnose.bat
) else (
    echo   ✗ scripts\diagnose.bat MISSING
)
if exist "scripts\verify_dedup.py" (
    echo   ✓ scripts\verify_dedup.py
) else (
    echo   ✗ scripts\verify_dedup.py MISSING
)
echo.

echo [2/8] Checking docs folder...
if exist "docs\TROUBLESHOOTING.md" (
    echo   ✓ docs\TROUBLESHOOTING.md
) else (
    echo   ✗ docs\TROUBLESHOOTING.md MISSING
)
if exist "docs\DEDUPLICATION.md" (
    echo   ✓ docs\DEDUPLICATION.md
) else (
    echo   ✗ docs\DEDUPLICATION.md MISSING
)
if exist "docs\PRODUCTION_CHECKLIST.md" (
    echo   ✓ docs\PRODUCTION_CHECKLIST.md
) else (
    echo   ✗ docs\PRODUCTION_CHECKLIST.md MISSING
)
echo.

echo [3/8] Checking root directory cleanup...
if exist "test_download.py" (
    echo   ✗ test_download.py still in root
) else (
    echo   ✓ test_download.py removed from root
)
if exist "diagnose.bat" (
    echo   ✗ diagnose.bat still in root
) else (
    echo   ✓ diagnose.bat removed from root
)
echo.

echo [4/8] Checking protected files...
if exist ".env" (
    echo   ✓ .env preserved
) else (
    echo   ℹ .env not found (optional)
)
if exist "configs\download_tracker.sqlite" (
    echo   ✓ configs\download_tracker.sqlite preserved
    for %%A in ("configs\download_tracker.sqlite") do echo     Size: %%~zA bytes
) else (
    echo   ℹ configs\download_tracker.sqlite not found (will be created)
)
echo.

echo [5/8] Checking configuration files...
if exist "configs\config.yaml" (
    echo   ✓ configs\config.yaml
) else (
    echo   ✗ configs\config.yaml MISSING
)
if exist "configs\providers.yaml" (
    echo   ✓ configs\providers.yaml
) else (
    echo   ✗ configs\providers.yaml MISSING
)
echo.

echo [6/8] Checking core modules...
if exist "src\cli.py" (
    echo   ✓ src\cli.py
) else (
    echo   ✗ src\cli.py MISSING
)
if exist "src\provider.py" (
    echo   ✓ src\provider.py
) else (
    echo   ✗ src\provider.py MISSING
)
if exist "src\tracker.py" (
    echo   ✓ src\tracker.py
) else (
    echo   ✗ src\tracker.py MISSING
)
echo.

echo [7/8] Checking documentation...
if exist "README.md" (
    echo   ✓ README.md
) else (
    echo   ✗ README.md MISSING
)
if exist "CHANGELOG.md" (
    echo   ✓ CHANGELOG.md
) else (
    echo   ✗ CHANGELOG.md MISSING
)
if exist "QUICK_START.md" (
    echo   ✓ QUICK_START.md
) else (
    echo   ✗ QUICK_START.md MISSING
)
echo.

echo [8/8] Checking Python environment...
.venv\Scripts\python --version
if %ERRORLEVEL% EQU 0 (
    echo   ✓ Python environment working
) else (
    echo   ✗ Python environment not working
)
echo.

echo ========================================
echo VERIFICATION COMPLETE
echo ========================================
echo.
echo Next steps:
echo   1. Run: scripts\diagnose.bat
echo   2. Run: scripts\test_idempotency.bat
echo   3. Read: QUICK_START.md
echo.
pause
