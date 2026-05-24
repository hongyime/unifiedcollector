@echo off
echo Testing TikTok download with full fallback chain...
echo.
echo This will test: gallery-dl -> yt-dlp -> Playwright browser
echo.
.venv\Scripts\python main.py download user --user tiktok --limit 1 --out downloads/test
echo.
echo Check the output above to see which method succeeded.
pause
