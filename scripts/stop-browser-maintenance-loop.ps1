$ErrorActionPreference = "Continue"

$repo = "C:\unifiedcollector"
$pidPath = Join-Path $repo "tmp\browser_tab_maintenance_loop.pid"

if (-not (Test-Path -LiteralPath $pidPath)) {
    Write-Host "Browser maintenance loop pid file not found."
    exit 0
}

$pidValue = Get-Content -LiteralPath $pidPath -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $pidValue) {
    Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
    Write-Host "Browser maintenance loop pid file was empty; removed it."
    exit 0
}

$proc = Get-Process -Id ([int]$pidValue) -ErrorAction SilentlyContinue
if ($proc) {
    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    Write-Host "Stopped browser maintenance loop PID $($proc.Id)."
} else {
    Write-Host "Browser maintenance loop PID $pidValue is not running."
}
Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
