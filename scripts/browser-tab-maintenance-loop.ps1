param(
    [int]$IntervalMinutes = 30,
    [int]$InitialDelaySeconds = 0
)

$ErrorActionPreference = "Continue"

$repo = "C:\unifiedcollector"
$tmp = Join-Path $repo "tmp"
$pidPath = Join-Path $tmp "browser_tab_maintenance_loop.pid"
$log = Join-Path $tmp "browser_tab_maintenance_loop.log"
$statusPath = Join-Path $tmp "browser_tab_maintenance_status.json"
$maintenance = Join-Path $repo "scripts\browser-tab-maintenance.ps1"

if (-not (Test-Path $tmp)) {
    New-Item -ItemType Directory -Path $tmp | Out-Null
}

function Write-LoopLog($message) {
    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -LiteralPath $log -Value "[$stamp] $message"
}

function Get-LoopPositiveIntEnv {
    param(
        [string]$Name,
        [int]$Default
    )
    $raw = [Environment]::GetEnvironmentVariable($Name)
    $parsed = 0
    if ([int]::TryParse([string]$raw, [ref]$parsed) -and $parsed -gt 0) {
        return $parsed
    }
    return $Default
}

function Write-LoopStatus {
    param(
        [string]$State,
        [string]$Detail,
        [int]$ChildPid
    )
    $status = [ordered]@{
        checked_at = (Get-Date).ToString("o")
        state = $State
        detail = $Detail
        cdp_url = Get-LoopCdpUrl
        pid = $ChildPid
        last_terminal_state = $State
        loop = [ordered]@{
            pid_path = $pidPath
            pid = $PID
            alive = $true
            detail = "maintenance loop is running"
        }
    }
    try {
        $status | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $statusPath
    } catch {
        Write-LoopLog ("could not write maintenance loop status: " + $_.Exception.Message)
    }
}

function Get-LoopCdpUrl {
    $rawUrl = [Environment]::GetEnvironmentVariable("CHROME_CDP_URL")
    if (-not [string]::IsNullOrWhiteSpace($rawUrl)) {
        return $rawUrl.Trim()
    }
    $rawPort = [Environment]::GetEnvironmentVariable("UC_CHROME_CDP_PORT")
    $parsedPort = 0
    if ([int]::TryParse([string]$rawPort, [ref]$parsedPort) -and $parsedPort -gt 0) {
        return "http://127.0.0.1:$parsedPort"
    }
    return "http://127.0.0.1:9333"
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
    $passTimeoutSeconds = Get-LoopPositiveIntEnv "UC_BROWSER_MAINTENANCE_PASS_TIMEOUT_SECONDS" 420
    if ($InitialDelaySeconds -gt 0) {
        Write-LoopLog "sleeping initial delay ${InitialDelaySeconds}s"
        Start-Sleep -Seconds $InitialDelaySeconds
    }
    while ($true) {
        try {
            Write-LoopLog "maintenance pass start"
            $child = Start-Process -FilePath "powershell.exe" -ArgumentList @(
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                $maintenance
            ) -WindowStyle Hidden -PassThru
            Write-LoopLog "maintenance pass pid=$($child.Id) timeout=${passTimeoutSeconds}s"
            $timeoutMilliseconds = [Math]::Max(60000, $passTimeoutSeconds * 1000)
            if (-not $child.WaitForExit($timeoutMilliseconds)) {
                Write-LoopLog "maintenance pass timed out after ${passTimeoutSeconds}s; terminating pid=$($child.Id)"
                Stop-Process -Id $child.Id -Force -ErrorAction SilentlyContinue
                Write-LoopStatus "failed" "maintenance pass timed out after ${passTimeoutSeconds}s" $child.Id
                Start-Sleep -Seconds ([Math]::Max(60, $IntervalMinutes * 60))
                continue
            }
            $exitCode = $child.ExitCode
            if ($exitCode -eq 0) {
                Write-LoopLog "maintenance pass exit=0"
            } elseif ($exitCode -eq 3) {
                Write-LoopLog "maintenance pass degraded: Chrome CDP unavailable"
            } else {
                Write-LoopLog "maintenance pass exit=$exitCode"
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
