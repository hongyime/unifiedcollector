@echo off
REM P3-5: scheduled Postgres backup for unifiedcollector.
REM Runs pg_dump -Fc against the running postgres container, writes a timestamped
REM dump to .\backups\, and prunes dumps older than 7 days.
REM Invoked by the Windows Scheduled Task "UnifiedCollectorBackup" (see
REM register-backup-task.ps1). Exits non-zero on failure so the task shows an error.

setlocal
cd /d C:\unifiedcollector

REM This script writes its OWN log (backups\backup_task.log) so the Task Scheduler
REM action can call it with NO output redirect -- the nested-quote redirect in the
REM action arg ( /c "bat" >> "log" 2>&1 ) was being mangled by the scheduler, making
REM cmd.exe exit 1 before the batch ran. Self-logging avoids that quoting trap.
set "LOG=C:\unifiedcollector\backups\backup_task.log"
if not exist backups mkdir backups

REM Resolve docker.exe by absolute path -- the Task Scheduler session uses a minimal
REM PATH that does NOT include Docker Desktop's resources\bin, so a bare "docker"
REM call fails instantly (result 1, no dump). Prefer the standard install path; fall
REM back to whatever is on PATH for interactive runs.
set "DOCKER=C:\Program Files\Docker\Docker\resources\bin\docker.exe"
if not exist "%DOCKER%" set "DOCKER=docker"

REM Timestamp YYYYMMDD_HHMMSS (locale-independent via powershell)
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set TS=%%i
set OUT=backups\unifiedcollector_%TS%.dump

echo [%date% %time%] starting pg_dump (docker=%DOCKER%) -^> %OUT%>>"%LOG%"
"%DOCKER%" exec unifiedcollector_postgres sh -c "PGPASSWORD=$POSTGRES_PASSWORD pg_dump -U $POSTGRES_USER -Fc unifiedcollector" > "%OUT%" 2>>"%LOG%"
if errorlevel 1 (
  echo [%date% %time%] ERROR: pg_dump failed>>"%LOG%"
  del "%OUT%" 2>nul
  exit /b 1
)

REM Validate the dump is a readable pg_restore archive before trusting it.
type "%OUT%" | "%DOCKER%" exec -i unifiedcollector_postgres pg_restore --list >nul 2>>"%LOG%"
if errorlevel 1 (
  echo [%date% %time%] ERROR: dump failed pg_restore --list validation>>"%LOG%"
  exit /b 1
)

echo [%date% %time%] backup OK: %OUT%>>"%LOG%"

REM Prune dumps older than 7 days. forfiles exits 1 when it matches nothing, which
REM would poison the task result -- swallow it so a clean prune never fails the task.
forfiles /p backups /m *.dump /d -7 /c "cmd /c del @path" 2>nul
exit /b 0
