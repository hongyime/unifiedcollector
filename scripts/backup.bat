@echo off
REM P3-5: scheduled Postgres backup for unifiedcollector.
REM Runs pg_dump -Fc against the running postgres container, writes a timestamped
REM dump to .\backups\, and prunes dumps older than 7 days.
REM Invoked by the Windows Scheduled Task "UnifiedCollectorBackup" (see
REM register-backup-task.ps1). Exits non-zero on failure so the task shows an error.

setlocal
cd /d C:\unifiedcollector

REM Timestamp YYYYMMDD_HHMMSS (locale-independent via wmic)
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set TS=%%i
set OUT=backups\unifiedcollector_%TS%.dump

if not exist backups mkdir backups

echo [%date% %time%] starting pg_dump -^> %OUT%
docker exec unifiedcollector_postgres sh -c "PGPASSWORD=$POSTGRES_PASSWORD pg_dump -U $POSTGRES_USER -Fc unifiedcollector" > "%OUT%"
if errorlevel 1 (
  echo [%date% %time%] ERROR: pg_dump failed
  del "%OUT%" 2>nul
  exit /b 1
)

REM Validate the dump is a readable pg_restore archive before trusting it.
type "%OUT%" | docker exec -i unifiedcollector_postgres pg_restore --list >nul 2>&1
if errorlevel 1 (
  echo [%date% %time%] ERROR: dump failed pg_restore --list validation
  exit /b 1
)

echo [%date% %time%] backup OK: %OUT%

REM Prune dumps older than 7 days.
forfiles /p backups /m *.dump /d -7 /c "cmd /c del @path" 2>nul

exit /b 0
