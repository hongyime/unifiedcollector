@echo off
REM Scheduled Postgres backup for unifiedcollector.
REM Runs pg_dump -Fc against the running postgres container, writes a timestamped
REM dump to Z:\unifiedcollector\backups\db, validates it, and applies bounded
REM retention: 7 daily, 4 weekly, 3 monthly by default.
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

REM Task Scheduler uses a minimal PATH. Prefer the Windows Python launcher, then
REM fall back to python on PATH for interactive runs.
set "PYTHON=python"
if exist "%SystemRoot%\py.exe" set "PYTHON=%SystemRoot%\py.exe -3"

REM Defaults can be overridden by the scheduled task environment if needed.
if not defined COLLECTOR_DB_BACKUP_DIR set "COLLECTOR_DB_BACKUP_DIR=Z:\unifiedcollector\backups\db"
if not defined COLLECTOR_DB_BACKUP_DAILY set "COLLECTOR_DB_BACKUP_DAILY=7"
if not defined COLLECTOR_DB_BACKUP_WEEKLY set "COLLECTOR_DB_BACKUP_WEEKLY=4"
if not defined COLLECTOR_DB_BACKUP_MONTHLY set "COLLECTOR_DB_BACKUP_MONTHLY=3"

echo [%date% %time%] starting DB backup to %COLLECTOR_DB_BACKUP_DIR%>>"%LOG%"
%PYTHON% -m src.backup.db_backup run --docker-container unifiedcollector_postgres >>"%LOG%" 2>&1
if errorlevel 1 (
  echo [%date% %time%] ERROR: DB backup failed>>"%LOG%"
  exit /b 1
)

echo [%date% %time%] DB backup OK>>"%LOG%"
exit /b 0
