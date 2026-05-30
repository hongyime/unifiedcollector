# P3-5: register the daily unifiedcollector Postgres backup as a Windows Scheduled Task.
# Run ONCE in an elevated PowerShell:  powershell -ExecutionPolicy Bypass -File scripts\register-backup-task.ps1
# Idempotent: re-running replaces the existing task.
#
# Schedule: daily at 03:30 local. Runs scripts\backup.bat (pg_dump -Fc + 7-day prune + validate).
# Note: PowerShell 5 constraints (no em-dash, no backtick continuation, ASCII only).

$ErrorActionPreference = "Stop"

$taskName = "UnifiedCollectorBackup"
$repo     = "C:\unifiedcollector"
$batch    = Join-Path $repo "scripts\backup.bat"
$logFile  = Join-Path $repo "backups\backup_task.log"

if (-not (Test-Path $batch)) {
    throw "backup.bat not found at $batch"
}

# Action: run the batch, append stdout+stderr to a log.
$action = New-ScheduledTaskAction -Execute "cmd.exe" `
    -Argument "/c `"$batch`" >> `"$logFile`" 2>&1"

# Daily at 03:30.
$trigger = New-ScheduledTaskTrigger -Daily -At 3:30AM

# Run whether or not the user is logged on; highest privileges (docker access).
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Highest

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -DontStopOnIdleEnd -ExecutionTimeLimit (New-TimeSpan -Hours 1)

# Replace if it already exists.
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings `
    -Description "Daily pg_dump backup of the unifiedcollector Postgres DB (P3-5). Validates dump and prunes >7 days."

Write-Host "Registered scheduled task '$taskName' (daily 03:30)."
Write-Host "Test it now with:  Start-ScheduledTask -TaskName $taskName"
Write-Host "Then check:        backups\backup_task.log  and  backups\*.dump"
