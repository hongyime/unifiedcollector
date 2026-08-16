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

function Set-StatusProperty {
    param(
        [object]$Status,
        [string]$Name,
        [object]$Value
    )
    if ($Status -is [System.Collections.IDictionary]) {
        $Status[$Name] = $Value
    } else {
        $Status | Add-Member -NotePropertyName $Name -NotePropertyValue $Value -Force
    }
}

function Update-LoopStatusMetadata {
    param(
        [string]$Detail,
        [int]$ChildPid
    )
    try {
        if (Test-Path -LiteralPath $statusPath) {
            $status = Get-Content -LiteralPath $statusPath -Raw | ConvertFrom-Json
        } else {
            $status = [ordered]@{
                checked_at = (Get-Date).ToString("o")
                state = "running"
                detail = $Detail
                cdp_url = Get-LoopCdpUrl
                pid = $ChildPid
                last_terminal_state = "running"
            }
        }
        Set-StatusProperty $status "checked_at" (Get-Date).ToString("o")
        Set-StatusProperty $status "pid" $ChildPid
        Set-StatusProperty $status "loop" ([ordered]@{
            pid_path = $pidPath
            pid = $PID
            alive = $true
            detail = $Detail
        })
        $status | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $statusPath
    } catch {
        Write-LoopLog ("could not update maintenance loop metadata: " + $_.Exception.Message)
    }
}

function Stop-MaintenanceChildProcess {
    param([int]$ChildPid)
    if ($ChildPid -le 0) {
        return
    }
    try {
        $taskkillOutput = & "$env:SystemRoot\System32\taskkill.exe" /PID $ChildPid /F /T 2>&1
        $taskkillExitCode = $LASTEXITCODE
        if ($taskkillExitCode -ne 0) {
            $detail = (($taskkillOutput | Select-Object -First 3) -join " ").Trim()
            Write-LoopLog "taskkill failed for maintenance child pid=${ChildPid} exit=${taskkillExitCode}: $detail"
        }
    } catch {
        Write-LoopLog ("taskkill failed for maintenance child pid=${ChildPid}: " + $_.Exception.Message)
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
    return "http://127.0.0.1:9336"
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
Update-LoopStatusMetadata "maintenance loop is running" 0

try {
    $passTimeoutSeconds = Get-LoopPositiveIntEnv "UC_BROWSER_MAINTENANCE_PASS_TIMEOUT_SECONDS" 600
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
            Update-LoopStatusMetadata "maintenance loop running child pass" $child.Id
            $timeoutMilliseconds = [Math]::Max(60000, $passTimeoutSeconds * 1000)
            if (-not $child.WaitForExit($timeoutMilliseconds)) {
                Write-LoopLog "maintenance pass timed out after ${passTimeoutSeconds}s; terminating pid=$($child.Id)"
                Stop-Process -Id $child.Id -Force -ErrorAction SilentlyContinue
                Stop-MaintenanceChildProcess -ChildPid $child.Id
                Write-LoopStatus "failed" "maintenance pass timed out after ${passTimeoutSeconds}s" $child.Id
                Start-Sleep -Seconds ([Math]::Max(60, $IntervalMinutes * 60))
                continue
            }
            $exitCode = $child.ExitCode
            if ($exitCode -eq 0) {
                Write-LoopLog "maintenance pass exit=0"
                Update-LoopStatusMetadata "maintenance loop sleeping after successful pass" $child.Id
            } elseif ($exitCode -eq 3) {
                Write-LoopLog "maintenance pass degraded: Chrome CDP unavailable"
                Update-LoopStatusMetadata "maintenance loop sleeping after CDP-unavailable pass" $child.Id
            } else {
                Write-LoopLog "maintenance pass exit=$exitCode"
                Update-LoopStatusMetadata "maintenance loop sleeping after nonzero pass" $child.Id
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
