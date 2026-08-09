param(
    [int]$IntervalMinutes = 10,
    [int]$InitialDelaySeconds = 60
)

$ErrorActionPreference = "Stop"

$repo = "C:\unifiedcollector"
$starter = Join-Path $repo "scripts\start-browser-maintenance-loop.ps1"
if (-not (Test-Path -LiteralPath $starter)) {
    throw "Missing browser maintenance starter: $starter"
}

$startup = [Environment]::GetFolderPath("Startup")
if (-not $startup) {
    throw "Could not resolve current-user Startup folder."
}

$cmdPath = Join-Path $startup "UnifiedCollectorBrowserMaintenance.cmd"
$lines = @(
    "@echo off",
    "cd /d `"$repo`"",
    "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$starter`" -IntervalMinutes $IntervalMinutes -InitialDelaySeconds $InitialDelaySeconds"
)

Set-Content -LiteralPath $cmdPath -Value $lines -Encoding ASCII
Write-Host "Installed current-user startup launcher: $cmdPath"
