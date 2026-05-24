@echo off
echo Refreshing TikTok cookies...
echo.
echo STEP 1: Make sure you're logged into TikTok in Chrome
echo STEP 2: Press any key to extract cookies
pause
.venv\Scripts\python main.py utils setup-cookies --browser chrome
echo.
echo Done! Now try downloading again.
pause
