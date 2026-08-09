param(
    [int]$IntervalMinutes = 30,
    [int]$InitialDelaySeconds = 0
)

$ErrorActionPreference = "Continue"

$repo = "C:\unifiedcollector"
$tmp = Join-Path $repo "tmp"
$pidPath = Join-Path $tmp "browser_tab_maintenance_loop.pid"
$log = Join-Path $tmp "browser_tab_maintenance_loop.log"
$maintenance = Join-Path $repo "scripts\browser-tab-maintenance.ps1"

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
        $existing = Get-Process -Id $parsedPid -ErrorAction SilentlyContinue
        if ($existing) {
            Write-LoopLog "loop already running as pid=$parsedPid; refusing duplicate direct start pid=$PID"
            exit 0
        }
    }
}

Set-Content -LiteralPath $pidPath -Value $PID
Write-LoopLog "loop start pid=$PID interval=${IntervalMinutes}m initial_delay=${InitialDelaySeconds}s"

try {
    if ($InitialDelaySeconds -gt 0) {
        Write-LoopLog "sleeping initial delay ${InitialDelaySeconds}s"
        Start-Sleep -Seconds $InitialDelaySeconds
    }
    while ($true) {
        try {
            Write-LoopLog "maintenance pass start"
            & powershell.exe -ExecutionPolicy Bypass -File $maintenance
            if ($LASTEXITCODE -eq 0) {
                Write-LoopLog "maintenance pass exit=0"
            } elseif ($LASTEXITCODE -eq 3) {
                Write-LoopLog "maintenance pass degraded: Chrome CDP unavailable"
            } else {
                Write-LoopLog "maintenance pass exit=$LASTEXITCODE"
            }
        } catch {
            Write-LoopLog ("maintenance pass failed: " + $_.Exception.Message)
        }
        Start-Sleep -Seconds ([Math]::Max(60, $IntervalMinutes * 60))
    }
} finally {
    try {
        $current = (Get-Content -LiteralPath $pidPath -ErrorAction SilentlyContinue | Select-Object -First 1)
        if ([string]$current -eq [string]$PID) {
            Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
        }
    } catch {}
    Write-LoopLog "loop stop pid=$PID"
}
