# Register the Docker watchdog as a Scheduled Task that runs every 5 minutes,
# HIDDEN (no console window), to relaunch Docker Desktop if the engine is down.
# Run once:  powershell -ExecutionPolicy Bypass -File scripts\register-docker-watchdog.ps1
# Idempotent. Uses Interactive logon (Docker Desktop is a per-user app) + a
# Limited run level so it does NOT require elevation to register or run.
$ErrorActionPreference = "Stop"
$taskName = "UnifiedCollectorDockerWatchdog"
$repo = "C:\unifiedcollector"
$vbs  = Join-Path $repo "scripts\run_hidden.vbs"
$ps1  = Join-Path $repo "scripts\docker_watchdog.ps1"
foreach ($p in @($vbs, $ps1)) { if (-not (Test-Path $p)) { throw "missing $p" } }

# wscript run_hidden.vbs "<command>" -> runs the command with NO window.
$inner  = "powershell -NoProfile -ExecutionPolicy Bypass -File `"$ps1`""
$action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument ("`"$vbs`" `"$inner`"")

# Every 5 minutes, indefinitely, starting at logon + now.
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 5)
$atLogon = New-ScheduledTaskTrigger -AtLogOn

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$settings  = New-ScheduledTaskSettingsSet -StartWhenAvailable -Hidden `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 4)

if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}
Register-ScheduledTask -TaskName $taskName -Action $action `
    -Trigger @($trigger, $atLogon) -Principal $principal -Settings $settings `
    -Description "Relaunch Docker Desktop if the engine is down (every 5 min, hidden). unless-stopped containers then resume." | Out-Null
Write-Host "Registered '$taskName' (every 5 min, hidden)."
