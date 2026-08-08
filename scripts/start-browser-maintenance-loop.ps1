param(
    [int]$IntervalMinutes = 30,
    [int]$InitialDelaySeconds = 1800
)

$ErrorActionPreference = "Stop"

$repo = "C:\unifiedcollector"
$tmp = Join-Path $repo "tmp"
$pidPath = Join-Path $tmp "browser_tab_maintenance_loop.pid"
$loopScript = Join-Path $repo "scripts\browser-tab-maintenance-loop.ps1"

if (-not (Test-Path $tmp)) {
    New-Item -ItemType Directory -Path $tmp | Out-Null
}

if (Test-Path -LiteralPath $pidPath) {
    $oldPid = Get-Content -LiteralPath $pidPath -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($oldPid -and (Get-Process -Id ([int]$oldPid) -ErrorAction SilentlyContinue)) {
        Write-Host "Browser maintenance loop already running as PID $oldPid."
        exit 0
    }
    Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
}

$pwsh = Get-Command pwsh.exe -ErrorAction SilentlyContinue
$psExe = if ($pwsh) { $pwsh.Source } else { "powershell.exe" }
$args = @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", $loopScript,
    "-IntervalMinutes", [string]$IntervalMinutes,
    "-InitialDelaySeconds", [string]$InitialDelaySeconds
)

$proc = Start-Process -FilePath $psExe -ArgumentList $args -WindowStyle Hidden -PassThru
Start-Sleep -Seconds 2
Write-Host "Started browser maintenance loop PID $($proc.Id)."
Write-Host "Loop log: C:\unifiedcollector\tmp\browser_tab_maintenance_loop.log"
Write-Host "Maintenance log: C:\unifiedcollector\tmp\browser_tab_maintenance.log"
