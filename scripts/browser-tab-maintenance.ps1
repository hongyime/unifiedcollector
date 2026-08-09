$ErrorActionPreference = "Stop"

$repo = "C:\unifiedcollector"
$tmp = Join-Path $repo "tmp"
$log = Join-Path $tmp "browser_tab_maintenance.log"
$statusPath = Join-Path $tmp "browser_tab_maintenance_status.json"
$loopPidPath = Join-Path $tmp "browser_tab_maintenance_loop.pid"
$script:LastCdpError = $null

if (-not (Test-Path $tmp)) {
    New-Item -ItemType Directory -Path $tmp | Out-Null
}

function Write-Log($message) {
    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -LiteralPath $log -Value "[$stamp] $message"
}

function Get-ChromeCdpDiagnostics {
    $processes = @(Get-CimInstance Win32_Process -Filter "name='chrome.exe'" -ErrorAction SilentlyContinue)
    $withCdp = @()
    $withUserData = @()
    $browserRoots = @()
    $visibleWindows = @(Get-Process chrome -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 })
    foreach ($proc in $processes) {
        $cmd = [string]$proc.CommandLine
        if ($cmd -match "--remote-debugging-port(?:=|\s+)9222\b") {
            $withCdp += $proc
        }
        if ($cmd -match "--user-data-dir(?:=|\s+)") {
            $withUserData += $proc
        }
        if ($cmd -and $cmd -notmatch "--type=") {
            $browserRoots += $proc
        }
    }
    $repairCommand = "powershell -ExecutionPolicy Bypass -File scripts\start-scraper-chrome-cdp.ps1"
    $hint = "Close Chrome, then run scripts\start-scraper-chrome-cdp.ps1 so the scraper Chrome starts with --remote-debugging-port=9222; do not open extra Chrome windows manually for maintenance."
    $reason = "chrome_cdp_unavailable"
    if ($processes.Count -eq 0) {
        $reason = "chrome_not_running"
        $hint = "Run scripts\start-scraper-chrome-cdp.ps1 to open the collector scraper Chrome profile with --remote-debugging-port=9222, then reload the extension tabs."
    } elseif ($withCdp.Count -eq 0) {
        $reason = "chrome_running_without_cdp"
        if ($visibleWindows.Count -eq 0) {
            $repairCommand = "powershell -ExecutionPolicy Bypass -File scripts\start-scraper-chrome-cdp.ps1 -CloseExistingIfNoVisibleWindows -FallbackOpenControlIfCleanupBlocked -NoOpenAll -OpenIds x -NoTest"
            $hint = "Chrome has no visible windows and no CDP. Run scripts\start-scraper-chrome-cdp.ps1 -CloseExistingIfNoVisibleWindows -FallbackOpenControlIfCleanupBlocked -NoOpenAll -OpenIds x -NoTest to close orphaned background Chrome and relaunch only the X scraper tab with CDP. If Windows denies protected Chrome PIDs, the script will at least nudge the collector extension control page in the existing Chrome session; close Chrome from Task Manager or restart the Windows Chrome session for full CDP recovery."
        } else {
            $hint = "Chrome has visible windows but was not launched with --remote-debugging-port=9222. Do not use -CloseExistingIfNoVisibleWindows; save/finish browser work, close Chrome normally, then run scripts\start-scraper-chrome-cdp.ps1 so tab maintenance and cookie backup can reconnect."
        }
    }
    return [ordered]@{
        reason = $reason
        chrome_process_count = $processes.Count
        chrome_root_process_count = $browserRoots.Count
        chrome_visible_window_count = $visibleWindows.Count
        chrome_cdp_process_count = $withCdp.Count
        chrome_user_data_process_count = $withUserData.Count
        hint = $hint
        repair_command = $repairCommand
    }
}

function Get-LoopStatus {
    if (-not (Test-Path -LiteralPath $loopPidPath)) {
        return [ordered]@{
            pid_path = $loopPidPath
            pid = $null
            alive = $false
            detail = "loop pid file not found"
        }
    }
    $rawPid = (Get-Content -LiteralPath $loopPidPath -ErrorAction SilentlyContinue | Select-Object -First 1)
    $loopPid = 0
    if (-not [int]::TryParse([string]$rawPid, [ref]$loopPid) -or $loopPid -le 0) {
        return [ordered]@{
            pid_path = $loopPidPath
            pid = $rawPid
            alive = $false
            detail = "loop pid file is invalid"
        }
    }
    $proc = Get-Process -Id $loopPid -ErrorAction SilentlyContinue
    return [ordered]@{
        pid_path = $loopPidPath
        pid = $loopPid
        alive = $null -ne $proc
        detail = if ($proc) { "maintenance loop process is running" } else { "loop pid process is not running" }
    }
}

function Write-Status([string]$state, [string]$detail = "", [object]$diagnostics = $null) {
    if ($null -eq $diagnostics) {
        $diagnostics = Get-ChromeCdpDiagnostics
    }
    $previous = $null
    if (Test-Path -LiteralPath $statusPath) {
        try {
            $previous = Get-Content -LiteralPath $statusPath -Raw | ConvertFrom-Json
        } catch {
            $previous = $null
        }
    }
    $checkedAt = (Get-Date).ToString("o")
    $previousTerminalState = if ($previous -and $previous.last_terminal_state) {
        [string]$previous.last_terminal_state
    } elseif ($previous) {
        [string]$previous.state
    } else {
        ""
    }
    $previousCount = 0
    if ($previous -and [int]::TryParse([string]$previous.consecutive_cdp_unavailable_count, [ref]$previousCount)) {
        # Parsed into $previousCount.
    } else {
        $previousCount = 0
    }
    $terminalState = if ($state -eq "running") { $previousTerminalState } else { $state }
    $consecutiveCdpUnavailable = if ($state -eq "running" -and $previous) { $previousCount } else { 0 }
    $cdpUnavailableSince = if ($state -eq "running" -and $previous -and $previous.cdp_unavailable_since) {
        [string]$previous.cdp_unavailable_since
    } else {
        $null
    }
    if ($state -eq "cdp_unavailable") {
        $consecutiveCdpUnavailable = if ($previousTerminalState -eq "cdp_unavailable") { $previousCount + 1 } else { 1 }
        $cdpUnavailableSince = if ($previousTerminalState -eq "cdp_unavailable" -and $previous.cdp_unavailable_since) {
            [string]$previous.cdp_unavailable_since
        } else {
            $checkedAt
        }
    }
    $payload = [ordered]@{
        checked_at = $checkedAt
        state = $state
        detail = $detail
        cdp_url = "http://127.0.0.1:9222"
        audit_result = (Join-Path $tmp "browser_tab_audit_result.json")
        reload_plan = (Join-Path $tmp "browser_tab_reload_plan.json")
        pid = $PID
        last_terminal_state = $terminalState
        consecutive_cdp_unavailable_count = $consecutiveCdpUnavailable
        cdp_unavailable_since = $cdpUnavailableSince
        loop = Get-LoopStatus
        diagnostics = $diagnostics
    }
    $payload | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $statusPath -Encoding UTF8
}

function Resolve-Python {
    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($python) { return @($python.Source) }
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) { return @($py.Source, "-3") }
    throw "python.exe/py.exe not found in PATH"
}

function Test-CdpAvailable {
    $url = "http://127.0.0.1:9222/json/version"
    try {
        $response = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 3
        $script:LastCdpError = $null
        return ($response.StatusCode -ge 200 -and $response.StatusCode -lt 300)
    } catch {
        $script:LastCdpError = $_.Exception.Message
        Write-Log ("Chrome CDP unavailable at ${url}: " + $script:LastCdpError)
        return $false
    }
}

function Get-PositiveIntEnv([string]$name, [int]$fallback) {
    $value = [Environment]::GetEnvironmentVariable($name)
    $parsed = 0
    if ([int]::TryParse($value, [ref]$parsed) -and $parsed -gt 0) {
        return $parsed
    }
    return $fallback
}

function Write-OutputLines($text) {
    if (-not $text) { return }
    $text -split "`r?`n" | Where-Object { $_ -ne "" } | ForEach-Object { Write-Log $_ }
}

function Quote-ProcessArgument($arg) {
    $text = [string]$arg
    return '"' + $text.Replace('"', '\"') + '"'
}

function Invoke-PythonScript([object[]]$command, [string]$script, [int]$timeoutSeconds = 180) {
    $parts = @($command)
    $psi = [System.Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = $parts[0]
    $psi.WorkingDirectory = $repo
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $args = @()
    if ($parts.Count -gt 1) {
        foreach ($arg in $parts[1..($parts.Count - 1)]) {
            $args += [string]$arg
        }
    }
    $args += $script
    $psi.Arguments = ($args | ForEach-Object { Quote-ProcessArgument $_ }) -join " "

    $proc = [System.Diagnostics.Process]::Start($psi)
    $stdoutTask = $proc.StandardOutput.ReadToEndAsync()
    $stderrTask = $proc.StandardError.ReadToEndAsync()
    if (-not $proc.WaitForExit($timeoutSeconds * 1000)) {
        try {
            $proc.Kill($true)
        } catch {
            try { $proc.Kill() } catch {}
        }
        Write-Log "$script timed out after ${timeoutSeconds}s and was killed"
        throw "$script timed out"
    }
    Write-OutputLines $stdoutTask.Result
    Write-OutputLines $stderrTask.Result
    if ($proc.ExitCode -ne 0) {
        throw "$script exited $($proc.ExitCode)"
    }
}

$audit = Join-Path $repo "tools\browser_tab_audit.py"
$reload = Join-Path $repo "tools\browser_tab_reload.py"

Write-Log "browser tab maintenance start"
Write-Status "running" "maintenance pass started"

Push-Location $repo
try {
    if (-not (Test-CdpAvailable)) {
        $diagnostics = Get-ChromeCdpDiagnostics
        if ($diagnostics.reason -eq "chrome_running_without_cdp") {
            Write-Log "Chrome is running, but no process has --remote-debugging-port=9222"
        } elseif ($diagnostics.reason -eq "chrome_not_running") {
            Write-Log "Chrome is not running"
        }
        Write-Log "browser tab maintenance skipped because Chrome CDP is unavailable"
        Write-Status "cdp_unavailable" $script:LastCdpError $diagnostics
        exit 3
    }
    $python = Resolve-Python
    $auditTimeout = Get-PositiveIntEnv "UC_BROWSER_AUDIT_TIMEOUT_SECONDS" 90
    $reloadTimeout = Get-PositiveIntEnv "UC_BROWSER_RELOAD_TIMEOUT_SECONDS" 90
    Write-Log ("using python command: " + ($python -join " "))
    Invoke-PythonScript -command $python -script $audit -timeoutSeconds $auditTimeout
    Invoke-PythonScript -command $python -script $reload -timeoutSeconds $reloadTimeout
    Write-Log "browser tab maintenance complete"
    Write-Status "ok" "audit and reload completed"
} catch {
    Write-Log ("browser tab maintenance failed: " + $_.Exception.Message)
    Write-Status "failed" $_.Exception.Message
    throw
} finally {
    Pop-Location
}
