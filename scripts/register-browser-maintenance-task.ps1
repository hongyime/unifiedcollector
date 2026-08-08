param(
    [string]$TaskName = "UnifiedCollectorBrowserMaintenance",
    [int]$IntervalMinutes = 30
)

$ErrorActionPreference = "Stop"

$repo = "C:\unifiedcollector"
$starter = Join-Path $repo "scripts\start-browser-maintenance-loop.ps1"

if (-not (Test-Path -LiteralPath $starter)) {
    throw "Missing browser maintenance starter: $starter"
}

$pwsh = Get-Command pwsh.exe -ErrorAction SilentlyContinue
$psExe = if ($pwsh) { $pwsh.Source } else { "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" }

$argument = @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", "`"$starter`"",
    "-IntervalMinutes", [string]$IntervalMinutes,
    "-InitialDelaySeconds", "60"
) -join " "

$action = New-ScheduledTaskAction -Execute $psExe -Argument $argument -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Keeps UnifiedCollector browser tab audit/reload maintenance loop alive." `
    -Force | Out-Null

Write-Host "Registered scheduled task $TaskName."
Write-Host "Start it now with: Start-ScheduledTask -TaskName $TaskName"
