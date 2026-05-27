@echo off
echo ========================================
echo IDEMPOTENCY TEST
echo ========================================
echo.
echo This test will download from @tiktok twice to prove deduplication works.
echo.
echo FIRST RUN: Should download 3 videos
echo ----------------------------------------
.venv\Scripts\python main.py download user --user tiktok --limit 3 --out downloads/test
echo.
echo.
echo SECOND RUN: Should skip all 3 videos (0 downloads)
echo ----------------------------------------
.venv\Scripts\python main.py download user --user tiktok --limit 3 --out downloads/test
echo.
echo.
echo ========================================
echo RESULT:
echo ========================================
echo If second run shows "0 downloads" or "all tracked", deduplication is working!
echo.
pause
