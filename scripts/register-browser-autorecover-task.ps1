param(
    [string]$TaskName = "UnifiedCollectorBrowserAutorecover",
    [int]$IntervalMinutes = 10,
    [int]$CdpPort = 9336
)

# Registers Windows Task Scheduler entry for scripts/browser-autorecover.ps1.
# The autorecover script (see its header) detects a dead MV3 service worker or
# unreachable CDP and relaunches Chrome via start-scraper-chrome-cdp.ps1. When
# this registrar is missing (as it was during the audit that surfaced REL-008),
# there is no host-side safety net for a browser-extension outage.
#
# Trigger: AtLogOn + every $IntervalMinutes (default 10 min).
# Fallback: current-user AtLogOn task, then Startup folder .cmd, matching the
# same denial-tolerant pattern as register-browser-maintenance-task.ps1.

$ErrorActionPreference = "Stop"

$repo = "C:\unifiedcollector"
$script = Join-Path $repo "scripts\browser-autorecover.ps1"

if (-not (Test-Path -LiteralPath $script)) {
    throw "Missing autorecover script: $script"
}

$pwsh = Get-Command pwsh.exe -ErrorAction SilentlyContinue
$psExe = if ($pwsh) { $pwsh.Source } else { "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" }

$argument = @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", "`"$script`"",
    "-Loop",
    "-IntervalMinutes", [string]$IntervalMinutes,
    "-CdpPort", [string]$CdpPort
) -join " "

$action = New-ScheduledTaskAction -Execute $psExe -Argument $argument -WorkingDirectory $repo

# AtLogOn trigger with a 60s delay so the CDP Chrome has time to come up first.
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn
$logonTrigger.Delay = "PT60S"

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0) `
    -MultipleInstances IgnoreNew

try {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $logonTrigger `
        -Settings $settings `
        -Description "Detects dead extension service worker or CDP outage and relaunches managed Chrome. Rate-limited to 4 recoveries per hour." `
        -Force | Out-Null

    Write-Host "Registered scheduled task $TaskName (AtLogOn + internal Loop)."
    Write-Host "Start it now with: Start-ScheduledTask -TaskName $TaskName"
    exit 0
} catch {
    $message = $_.Exception.Message
    if ($message -notmatch "Access is denied|0x80070005") {
        throw
    }
    # Fallback to Startup folder .cmd (same pattern as the maintenance-task registrar).
    $startup = [Environment]::GetFolderPath("Startup")
    if (-not $startup) {
        throw "Scheduled task registration denied and Startup folder unresolved."
    }
    $cmdPath = Join-Path $startup "$TaskName.cmd"
    $hiddenRunner = Join-Path $repo "scripts\run_hidden.vbs"
    $cmd = @(
        "@echo off",
        "cd /d `"$repo`"",
        "wscript.exe `"$hiddenRunner`" `"`"$psExe`" -NoProfile -ExecutionPolicy Bypass -File `"`"`"$script`"`"`" -Loop -IntervalMinutes $IntervalMinutes -CdpPort $CdpPort`""
    ) -join "`r`n"
    Set-Content -LiteralPath $cmdPath -Value $cmd -Encoding ASCII
    Write-Warning "Scheduled task registration was denied; installed current-user Startup fallback."
    Write-Host "Startup fallback: $cmdPath"
    Write-Host "Start it now with: pwsh -NoProfile -ExecutionPolicy Bypass -File `"$script`" -Loop -IntervalMinutes $IntervalMinutes -CdpPort $CdpPort"
}
