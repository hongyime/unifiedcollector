@echo off
cd /d "%~dp0"
call start_toolkit.bat setup %*
exit /b %errorlevel%
