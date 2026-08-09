param(
    [string]$TaskName = "UnifiedCollectorBrowserMaintenance",
    [int]$IntervalMinutes = 10
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

try {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Description "Keeps UnifiedCollector browser tab audit/reload maintenance loop alive." `
        -Force | Out-Null

    Write-Host "Registered scheduled task $TaskName."
    Write-Host "Start it now with: Start-ScheduledTask -TaskName $TaskName"
    exit 0
} catch {
    $message = $_.Exception.Message
    if ($message -notmatch "Access is denied|0x80070005") {
        throw
    }
    $startup = [Environment]::GetFolderPath("Startup")
    if (-not $startup) {
        throw "Scheduled task registration was denied and the user Startup folder could not be resolved."
    }
    $cmdPath = Join-Path $startup "$TaskName.cmd"
    $cmd = @(
        "@echo off",
        "cd /d `"$repo`"",
        "`"$psExe`" -NoProfile -ExecutionPolicy Bypass -File `"$starter`" -IntervalMinutes $IntervalMinutes -InitialDelaySeconds 60"
    ) -join "`r`n"
    Set-Content -LiteralPath $cmdPath -Value $cmd -Encoding ASCII
    Write-Warning "Scheduled task registration was denied; installed current-user Startup fallback instead."
    Write-Host "Startup fallback: $cmdPath"
    Write-Host "Start it now with: powershell -ExecutionPolicy Bypass -File `"$starter`" -IntervalMinutes $IntervalMinutes -InitialDelaySeconds 0"
}
