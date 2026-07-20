# P3-5: register the daily unifiedcollector Postgres backup as a Windows Scheduled Task.
# Run ONCE in an elevated PowerShell:  powershell -ExecutionPolicy Bypass -File scripts\register-backup-task.ps1
# Idempotent: re-running replaces the existing task.
#
# Schedule: daily at 03:30 local. Runs scripts\backup.bat (pg_dump -Fc + validation
# + 7 daily / 4 weekly / 3 monthly retention to Z:\unifiedcollector\backups\db).
# Note: PowerShell 5 constraints (no em-dash, no backtick continuation, ASCII only).

$ErrorActionPreference = "Stop"

$taskName = "UnifiedCollectorBackup"
$repo     = "C:\unifiedcollector"
$batch    = Join-Path $repo "scripts\backup.bat"
$logFile  = Join-Path $repo "backups\backup_task.log"

if (-not (Test-Path $batch)) {
    throw "backup.bat not found at $batch"
}

# Action: run the batch through run_hidden.vbs so NO visible cmd.exe window flashes.
# Interactive logon (below) is required for Docker access but normally pops a console
# window; wscript + Shell.Run(...,0,...) launches the .bat hidden in the same session.
# The batch writes its OWN log (backups\backup_task.log) -- no shell redirect here
# (the nested-quote form was mangled by Task Scheduler).
$vbs = Join-Path $repo "scripts\run_hidden.vbs"
if (-not (Test-Path $vbs)) { throw "run_hidden.vbs not found at $vbs" }
$action = New-ScheduledTaskAction -Execute "wscript.exe" `
    -Argument ("`"$vbs`" `"$batch`"")

# Daily at 03:30.
$trigger = New-ScheduledTaskTrigger -Daily -At 3:30AM

# Run only when the user is logged on (Interactive). REQUIRED: Docker Desktop is a
# per-user service and is NOT reachable from an S4U/no-profile session, so a batch
# that calls docker exec fails with result 1 under S4U. Interactive inherits the
# logged-on user's Docker session. Highest privileges for docker access.
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -DontStopOnIdleEnd -ExecutionTimeLimit (New-TimeSpan -Hours 1)

# Replace if it already exists.
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings `
    -Description "Daily pg_dump backup of the unifiedcollector Postgres DB. Writes to Z:\unifiedcollector\backups\db and keeps 7 daily, 4 weekly, 3 monthly."

Write-Host "Registered scheduled task '$taskName' (daily 03:30)."
Write-Host "Test it now with:  Start-ScheduledTask -TaskName $taskName"
Write-Host "Then check:        backups\backup_task.log  and  Z:\unifiedcollector\backups\db\*.dump"
