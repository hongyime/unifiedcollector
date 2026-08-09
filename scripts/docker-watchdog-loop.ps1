param(
    [int]$IntervalSeconds = 300,
    [int]$InitialDelaySeconds = 60
)

$ErrorActionPreference = "Continue"

$repo = "C:\unifiedcollector"
$tmp = Join-Path $repo "tmp"
$pidPath = Join-Path $tmp "docker_watchdog_loop.pid"
$log = Join-Path $tmp "docker_watchdog_loop.log"
$watchdog = Join-Path $repo "scripts\docker_watchdog.ps1"

if (-not (Test-Path $tmp)) {
    New-Item -ItemType Directory -Path $tmp | Out-Null
}

function Write-LoopLog($message) {
    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -LiteralPath $log -Value "[$stamp] $message"
}

if (Test-Path -LiteralPath $pidPath) {
    $oldPid = Get-Content -LiteralPath $pidPath -ErrorAction SilentlyContinue | Select-Object -First 1
    $parsedPid = 0
    if ([int]::TryParse([string]$oldPid, [ref]$parsedPid) -and $parsedPid -gt 0) {
        if (Get-Process -Id $parsedPid -ErrorAction SilentlyContinue) {
            Write-LoopLog "loop already running as pid=$parsedPid; refusing duplicate direct start pid=$PID"
            exit 0
        }
    }
}

Set-Content -LiteralPath $pidPath -Value $PID
Write-LoopLog "loop start pid=$PID interval=${IntervalSeconds}s initial_delay=${InitialDelaySeconds}s"

try {
    if ($InitialDelaySeconds -gt 0) {
        Start-Sleep -Seconds $InitialDelaySeconds
    }
    while ($true) {
        try {
            & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $watchdog
            Write-LoopLog "watchdog pass exit=$LASTEXITCODE"
        } catch {
            Write-LoopLog ("watchdog pass failed: " + $_.Exception.Message)
        }
        Start-Sleep -Seconds ([Math]::Max(60, $IntervalSeconds))
    }
} finally {
    try {
        $current = Get-Content -LiteralPath $pidPath -ErrorAction SilentlyContinue | Select-Object -First 1
        if ([string]$current -eq [string]$PID) {
            Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
        }
    } catch {}
    Write-LoopLog "loop stop pid=$PID"
}
