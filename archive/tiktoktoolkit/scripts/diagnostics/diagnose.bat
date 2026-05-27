@echo off
echo ========================================
echo TikTok Toolkit Diagnostic
echo ========================================
echo.

echo Checking Python environment...
.venv\Scripts\python --version
echo.

echo Checking installed packages...
.venv\Scripts\python -m pip list | findstr /I "gallery-dl yt-dlp playwright curl-cffi"
echo.

echo Checking Playwright browsers...
.venv\Scripts\python -m playwright install --list
echo.

echo Checking cookies file...
if exist "configs\tiktok_cookies.txt" (
    echo Cookies file exists
    for %%A in ("configs\tiktok_cookies.txt") do echo Size: %%~zA bytes
) else (
    echo WARNING: Cookies file not found!
)
echo.

echo Checking gallery-dl version...
gallery-dl --version
echo.

echo Testing cookie validity...
.venv\Scripts\python main.py utils check-cookies --test-user tiktok
echo.

echo ========================================
echo Diagnostic complete!
echo ========================================
pause
