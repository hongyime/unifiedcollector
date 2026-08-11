$ErrorActionPreference = "Stop"

$repo = "C:\unifiedcollector"
$tmp = Join-Path $repo "tmp"
$log = Join-Path $tmp "browser_tab_maintenance.log"
$statusPath = Join-Path $tmp "browser_tab_maintenance_status.json"
$loopPidPath = Join-Path $tmp "browser_tab_maintenance_loop.pid"
$script:LastCdpError = $null
$script:CdpPort = 9333
$envCdpPort = [Environment]::GetEnvironmentVariable("UC_CHROME_CDP_PORT")
$parsedCdpPort = 0
if ([int]::TryParse($envCdpPort, [ref]$parsedCdpPort) -and $parsedCdpPort -gt 0) {
    $script:CdpPort = $parsedCdpPort
}

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
    $processById = @{}
    foreach ($proc in $processes) {
        $processById[[int]$proc.ProcessId] = $proc
    }
    $visibleControlWindows = @()
    $unsafeVisibleWindows = @()
    foreach ($window in $visibleWindows) {
        $proc = $processById[[int]$window.Id]
        $cmd = if ($proc) { [string]$proc.CommandLine } else { "" }
        $isCollectorControlled =
            $cmd -match "chrome-extension://.*tabs\.html" -or
            $cmd -match "\\UnifiedCollector\\ChromeCdp" -or
            $cmd -match "--remote-debugging-port(?:=|\s+)$script:CdpPort\b" -or
            $cmd -match "--user-data-dir(?:=|\s+).*\\UnifiedCollector\\ChromeCdp"
        if ($isCollectorControlled) {
            $visibleControlWindows += $window
        } else {
            $unsafeVisibleWindows += $window
        }
    }
    foreach ($proc in $processes) {
        $cmd = [string]$proc.CommandLine
        if ($cmd -match "--remote-debugging-port(?:=|\s+)$script:CdpPort\b") {
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
    $hint = "Close Chrome, then run scripts\start-scraper-chrome-cdp.ps1 so the scraper Chrome starts with --remote-debugging-port=$script:CdpPort; do not open extra Chrome windows manually for maintenance."
    $reason = "chrome_cdp_unavailable"
    if ($withCdp.Count -gt 0) {
        $reason = "chrome_cdp_available"
        $hint = "Chrome CDP is reachable. If browser heartbeats are stale, reload platform tabs or the UnifiedCollector extension control page."
        $repairCommand = "powershell -ExecutionPolicy Bypass -File scripts\start-scraper-chrome-cdp.ps1 -RemoteDebuggingPort $script:CdpPort -NoOpenAll -OpenIds instagram,tiktok,lemon8,x,threads,facebook,strava -NoTest"
        if ($script:LastCdpError) {
            $reason = "chrome_cdp_process_unreachable"
            if ($unsafeVisibleWindows.Count -eq 0) {
                $repairCommand = "powershell -ExecutionPolicy Bypass -File scripts\start-scraper-chrome-cdp.ps1 -RemoteDebuggingPort $script:CdpPort -CloseExistingCdpProfile -CloseExistingIfNoVisibleWindows -NoOpenAll -OpenIds instagram,tiktok,lemon8,x,threads,facebook,strava -NoTest"
                $hint = "Chrome has no unsafe visible windows and the CDP socket is unreachable. The maintenance task can close collector-controlled Chrome windows/processes and relaunch Chrome with CDP."
            } else {
                $hint = "Chrome has a CDP command line, but the CDP socket is unreachable. Save/finish visible browser work, close Chrome normally, then run scripts\start-scraper-chrome-cdp.ps1."
            }
        }
    } elseif ($processes.Count -eq 0) {
        $reason = "chrome_not_running"
        $repairCommand = "powershell -ExecutionPolicy Bypass -File scripts\start-scraper-chrome-cdp.ps1 -RemoteDebuggingPort $script:CdpPort -NoOpenAll -OpenIds instagram,tiktok,lemon8,x,threads,facebook,strava -NoTest"
        $hint = "Chrome is not running. The maintenance task can relaunch scraper Chrome with CDP and the UnifiedCollector extension."
    } elseif ($withCdp.Count -eq 0) {
        $reason = "chrome_running_without_cdp"
        if ($unsafeVisibleWindows.Count -eq 0) {
            $repairCommand = "powershell -ExecutionPolicy Bypass -File scripts\start-scraper-chrome-cdp.ps1 -RemoteDebuggingPort $script:CdpPort -CloseExistingIfNoVisibleWindows -NoOpenAll -OpenIds instagram,tiktok,lemon8,x,threads,facebook,strava -NoTest"
            $hint = "Chrome has no unsafe visible windows and no CDP. The maintenance task can close collector-controlled Chrome windows/processes and relaunch scraper Chrome with CDP."
        } else {
            $hint = "Chrome has visible windows but was not launched with --remote-debugging-port=$script:CdpPort. Do not use -CloseExistingIfNoVisibleWindows; save/finish browser work, close Chrome normally, then run scripts\start-scraper-chrome-cdp.ps1 so tab maintenance and cookie backup can reconnect."
        }
    }
    return [ordered]@{
        reason = $reason
        chrome_process_count = $processes.Count
        chrome_root_process_count = $browserRoots.Count
        chrome_visible_window_count = $visibleWindows.Count
        chrome_visible_control_window_count = $visibleControlWindows.Count
        chrome_unsafe_visible_window_count = $unsafeVisibleWindows.Count
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
        cdp_url = "http://127.0.0.1:$script:CdpPort"
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
    $url = "http://127.0.0.1:$script:CdpPort/json/version"
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

function Get-CdpPageTargets {
    try {
        return @(Invoke-RestMethod -Uri "http://127.0.0.1:$script:CdpPort/json/list" -Method Get -TimeoutSec 5)
    } catch {
        Write-Log ("could not list Chrome CDP targets: " + $_.Exception.Message)
        return @()
    }
}

function Test-ExtensionControlTab {
    foreach ($target in @(Get-CdpPageTargets)) {
        $url = [string]$target.url
        if ($url -match "^chrome-extension://[a-p]{32}/tabs\.html") {
            return $true
        }
    }
    return $false
}

function Open-CdpTargetUrl {
    param([string]$Url)
    try {
        $encoded = [uri]::EscapeDataString($Url)
        Invoke-WebRequest -Uri "http://127.0.0.1:$script:CdpPort/json/new?$encoded" -Method Put -UseBasicParsing -TimeoutSec 10 | Out-Null
        return $true
    } catch {
        Write-Log ("could not open CDP target ${Url}: " + $_.Exception.Message)
        return $false
    }
}

function Ensure-ExtensionControlTab {
    if (Test-ExtensionControlTab) {
        return $true
    }
    $knownIds = @(
        "pkmdmcklnjdeocoeigmlakhomhhcpafb",
        "nkeimhogjdpnpccoofpliimaahmaaome"
    )
    foreach ($extensionId in $knownIds) {
        $url = "chrome-extension://$extensionId/tabs.html"
        if (Open-CdpTargetUrl -Url $url) {
            Start-Sleep -Seconds 2
            if (Test-ExtensionControlTab) {
                Write-Log "opened missing extension control tab: $url"
                return $true
            }
        }
    }
    Write-Log "extension control tab is still missing after CDP open attempts"
    return $false
}

function Invoke-ChromeCdpRepair {
    param([object]$Diagnostics)
    $reason = [string]$Diagnostics.reason
    $launcher = Join-Path $repo "scripts\start-scraper-chrome-cdp.ps1"
    if (-not (Test-Path -LiteralPath $launcher)) {
        Write-Log "Chrome CDP repair skipped: launcher not found at $launcher"
        return $false
    }
    if ($reason -eq "chrome_not_running") {
        Write-Log "Chrome CDP repair: relaunching scraper Chrome because Chrome is not running"
        & powershell.exe -ExecutionPolicy Bypass -File $launcher -RemoteDebuggingPort $script:CdpPort -NoOpenAll -OpenIds instagram,tiktok,lemon8,x,threads,facebook,strava -NoTest
        return (Test-CdpAvailable)
    }
    $unsafeVisibleWindowCount = 0
    if ($Diagnostics.PSObject.Properties.Name -contains "chrome_unsafe_visible_window_count") {
        $unsafeVisibleWindowCount = [int]$Diagnostics.chrome_unsafe_visible_window_count
    } else {
        $unsafeVisibleWindowCount = [int]$Diagnostics.chrome_visible_window_count
    }
    if ($reason -eq "chrome_running_without_cdp" -and $unsafeVisibleWindowCount -eq 0) {
        Write-Log "Chrome CDP repair: closing collector-controlled Chrome and relaunching scraper Chrome with CDP"
        & powershell.exe -ExecutionPolicy Bypass -File $launcher -RemoteDebuggingPort $script:CdpPort -CloseExistingCdpProfile -CloseExistingIfNoVisibleWindows -NoOpenAll -OpenIds instagram,tiktok,lemon8,x,threads,facebook,strava -NoTest
        return (Test-CdpAvailable)
    }
    if ($reason -eq "chrome_cdp_process_unreachable" -and $unsafeVisibleWindowCount -eq 0) {
        Write-Log "Chrome CDP repair: closing collector-controlled unreachable CDP Chrome and relaunching scraper Chrome"
        & powershell.exe -ExecutionPolicy Bypass -File $launcher -RemoteDebuggingPort $script:CdpPort -CloseExistingCdpProfile -CloseExistingIfNoVisibleWindows -NoOpenAll -OpenIds instagram,tiktok,lemon8,x,threads,facebook,strava -NoTest
        return (Test-CdpAvailable)
    }
    Write-Log "Chrome CDP repair skipped for reason=$reason visible_windows=$($Diagnostics.chrome_visible_window_count)"
    return $false
}

function Get-PositiveIntEnv([string]$name, [int]$fallback) {
    $value = [Environment]::GetEnvironmentVariable($name)
    $parsed = 0
    if ([int]::TryParse($value, [ref]$parsed) -and $parsed -gt 0) {
        return $parsed
    }
    return $fallback
}

function Invoke-PostReloadSettle([int]$seconds) {
    if ($seconds -le 0) {
        return
    }
    Write-Log "settling browser tabs for ${seconds}s before follow-up audit"
    Start-Sleep -Seconds $seconds
}

function Set-DefaultEnv([string]$name, [string]$value) {
    if (-not [Environment]::GetEnvironmentVariable($name)) {
        [Environment]::SetEnvironmentVariable($name, $value, "Process")
    }
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

function Test-AuthWallUrl([string]$Url) {
    if (-not $Url) {
        return $false
    }
    try {
        $uri = [System.Uri]$Url
        $path = $uri.AbsolutePath.ToLowerInvariant()
        $query = $uri.Query.ToLowerInvariant()
        return (
            $path -match "/login|/signin|/checkpoint|/challenge|/i/flow/login|/i/jf/onboarding" -or
            $query -match "mode=login|redirect_after_login|auth_platform|recaptcha"
        )
    } catch {
        return $false
    }
}

function Get-AuditTabUrl($Tab) {
    if ($null -ne $Tab -and [string]$Tab.url_snapshot) {
        return [string]$Tab.url_snapshot
    }
    if ($null -ne $Tab -and [string]$Tab.url) {
        return [string]$Tab.url
    }
    return ""
}

function Test-AuditTabContentWall($Tab) {
    if ($null -eq $Tab) {
        return $false
    }
    $status = [string]$Tab.page_health_status
    return $status -eq "recoverable_error_shell"
}

function Get-AuditTabWallDetail($Tab) {
    if (-not (Test-AuditTabContentWall $Tab)) {
        return ""
    }
    $reason = [string]$Tab.page_health_reason
    $sample = [string]$Tab.page_health_sample
    $url = Get-AuditTabUrl $Tab
    return "page_health=recoverable_error_shell, reason=$reason, url=$url, sample=$sample"
}

function Get-AuditHealth {
    $auditPath = Join-Path $tmp "browser_tab_audit_result.json"
    if (-not (Test-Path -LiteralPath $auditPath)) {
        return [ordered]@{
            ok = $false
            healthy = 0
            total = 0
            unhealthy = @("missing audit result")
        }
    }
    try {
        $auditJson = Get-Content -LiteralPath $auditPath -Raw | ConvertFrom-Json
    } catch {
        return [ordered]@{
            ok = $false
            healthy = 0
            total = 0
            unhealthy = @("could not parse audit result: $($_.Exception.Message)")
        }
    }
    $platforms = @("instagram", "threads", "tiktok", "lemon8", "x", "facebook", "strava")
    $healthy = 0
    $total = 0
    $unhealthy = @()
    foreach ($platform in $platforms) {
        $tabs = @($auditJson.$platform)
        if ($tabs.Count -eq 0) {
            $unhealthy += "${platform}: missing tab"
            continue
        }
        $total += 1
        $good = @($tabs | Where-Object {
            $_.responsive_main -eq $true -and
            $_.cs -eq $true -and
            $_.cs_running -eq $true -and
            [string]$_.cs_version -and
            -not (Test-AuthWallUrl (Get-AuditTabUrl $_)) -and
            -not (Test-AuditTabContentWall $_)
        })
        if ($good.Count -gt 0) {
            $healthy += 1
        } else {
            $bad = @($tabs | Where-Object {
                -not (
                    $_.responsive_main -eq $true -and
                    $_.cs -eq $true -and
                    $_.cs_running -eq $true -and
                    [string]$_.cs_version -and
                    -not (Test-AuthWallUrl (Get-AuditTabUrl $_)) -and
                    -not (Test-AuditTabContentWall $_)
                )
            })
            $sample = $bad | Select-Object -First 1
            $sampleUrl = Get-AuditTabUrl $sample
            $authWall = Test-AuthWallUrl $sampleUrl
            $contentWall = Get-AuditTabWallDetail $sample
            $reason = if ($authWall) { "auth_wall_url=$sampleUrl" } elseif ($contentWall) { $contentWall } else { "err=$($sample.error)" }
            $unhealthy += "${platform}: $($good.Count)/$($tabs.Count) healthy; first_bad resp=$($sample.responsive_main), cs=$($sample.cs), running=$($sample.cs_running), $reason"
        }
    }
    # Default to all expected platform groups. A lower env override is still
    # available for manual degraded operation, but normal boot/self-heal must
    # not report success while a collector tab is missing its content script.
    $minHealthy = Get-PositiveIntEnv "UC_BROWSER_MIN_HEALTHY_PLATFORMS" $platforms.Count
    return [ordered]@{
        ok = ($healthy -ge $minHealthy)
        healthy = $healthy
        total = $total
        min_healthy = $minHealthy
        unhealthy = $unhealthy
        unhealthy_count = $unhealthy.Count
    }
}

function Test-AuditHealthNeedsProfileRestart($AuditHealth) {
    if ($null -eq $AuditHealth -or $AuditHealth.ok) {
        return $false
    }
    $minUnhealthyForRestart = Get-PositiveIntEnv "UC_BROWSER_PROFILE_RESTART_MIN_UNHEALTHY_PLATFORMS" 3
    $unhealthyCount = [int]$AuditHealth.unhealthy_count
    if ($unhealthyCount -lt $minUnhealthyForRestart) {
        Write-Log "browser tab maintenance degraded: $unhealthyCount unhealthy platform(s), below profile restart threshold $minUnhealthyForRestart"
        return $false
    }
    return $true
}

function Test-AuditHealthNeedsManualAuth($AuditHealth) {
    if ($null -eq $AuditHealth -or $AuditHealth.ok) {
        return $false
    }
    $items = @($AuditHealth.unhealthy)
    if ($items.Count -eq 0) {
        return $false
    }
    foreach ($item in $items) {
        $text = [string]$item
        if ($text -notmatch "page_health=recoverable_error_shell" -or $text -notmatch "auth_challenge|logout=") {
            return $false
        }
    }
    return $true
}

function Invoke-ScraperChromeProfileRestart {
    $launcher = Join-Path $repo "scripts\start-scraper-chrome-cdp.ps1"
    Write-Log "browser tab maintenance escalation: restarting dedicated scraper Chrome profile"
    try {
        & powershell.exe -ExecutionPolicy Bypass -File $launcher -RemoteDebuggingPort $script:CdpPort -CloseExistingCdpProfile -CloseExistingIfNoVisibleWindows -NoOpenAll -OpenIds instagram,tiktok,lemon8,x,threads,facebook,strava -NoTest
    } catch {
        Write-Log ("dedicated scraper Chrome profile restart command failed: " + $_.Exception.Message)
    }
    if (Test-CdpAvailable) {
        return $true
    }
    $diagnostics = Get-ChromeCdpDiagnostics
    Write-Log "dedicated scraper Chrome restart left CDP unavailable; fallback repair reason=$($diagnostics.reason)"
    return (Invoke-ChromeCdpRepair -Diagnostics $diagnostics)
}

$audit = Join-Path $repo "tools\browser_tab_audit.py"
$reload = Join-Path $repo "tools\browser_tab_reload.py"

$mutexName = "Global\UnifiedCollectorBrowserTabMaintenance"
$mutex = [System.Threading.Mutex]::new($false, $mutexName)
$haveMutex = $false
try {
    $haveMutex = $mutex.WaitOne(0)
} catch {
    $haveMutex = $false
}
if (-not $haveMutex) {
    Write-Log "browser tab maintenance skipped because another pass is already running"
    Write-Status "running" "another maintenance pass is already running"
    exit 0
}

Write-Log "browser tab maintenance start"
Write-Status "running" "maintenance pass started"

Push-Location $repo
try {
    if (-not (Test-CdpAvailable)) {
        $diagnostics = Get-ChromeCdpDiagnostics
        if ($diagnostics.reason -eq "chrome_running_without_cdp") {
            Write-Log "Chrome is running, but no process has --remote-debugging-port=$script:CdpPort"
        } elseif ($diagnostics.reason -eq "chrome_not_running") {
            Write-Log "Chrome is not running"
        }
        if (Invoke-ChromeCdpRepair -Diagnostics $diagnostics) {
            Write-Log "Chrome CDP repair succeeded; continuing maintenance pass"
        } else {
            Write-Log "browser tab maintenance skipped because Chrome CDP is unavailable"
            Write-Status "cdp_unavailable" $script:LastCdpError $diagnostics
            exit 3
        }
    }
    $python = Resolve-Python
    $auditTimeout = Get-PositiveIntEnv "UC_BROWSER_AUDIT_TIMEOUT_SECONDS" 240
    $reloadTimeout = Get-PositiveIntEnv "UC_BROWSER_RELOAD_TIMEOUT_SECONDS" 180
    $settleSeconds = Get-PositiveIntEnv "UC_BROWSER_POST_RELOAD_SETTLE_SECONDS" 30
    $profileRestartSettleSeconds = Get-PositiveIntEnv "UC_BROWSER_PROFILE_RESTART_SETTLE_SECONDS" 90
    # Maintenance should heal the browser without pinning the machine for many
    # minutes. Keep live-audit probes short here; deeper manual audits can still
    # override these env vars.
    Set-DefaultEnv "UC_TAB_AUDIT_RUNTIME_ENABLE_TIMEOUT_SECONDS" "3.0"
    Set-DefaultEnv "UC_TAB_AUDIT_MAIN_TIMEOUT_SECONDS" "4.0"
    Set-DefaultEnv "UC_TAB_AUDIT_ISO_TIMEOUT_SECONDS" "2.0"
    Set-DefaultEnv "UC_TAB_AUDIT_PERF_TIMEOUT_SECONDS" "0.5"
    Write-Log ("using python command: " + ($python -join " "))
    Invoke-PythonScript -command $python -script $audit -timeoutSeconds $auditTimeout
    Invoke-PythonScript -command $python -script $reload -timeoutSeconds $reloadTimeout
    # The reload step can open or replace tabs. Re-audit after a brief settle so
    # verifiers and dashboards read the repaired browser state, not the
    # pre-reload snapshot.
    Invoke-PostReloadSettle -seconds $settleSeconds
    Invoke-PythonScript -command $python -script $audit -timeoutSeconds $auditTimeout
    $auditHealth = Get-AuditHealth
    Write-Log "browser tab audit health: healthy=$($auditHealth.healthy)/$($auditHealth.total), min=$($auditHealth.min_healthy)"
    if (-not $auditHealth.ok) {
        Write-Log ("browser tab audit still unhealthy after reload: " + (($auditHealth.unhealthy) -join " | "))
        Write-Log "running second targeted browser tab reload pass before profile restart"
        Invoke-PythonScript -command $python -script $reload -timeoutSeconds $reloadTimeout
        Invoke-PostReloadSettle -seconds $settleSeconds
        Invoke-PythonScript -command $python -script $audit -timeoutSeconds $auditTimeout
        $auditHealth = Get-AuditHealth
        Write-Log "browser tab audit health after second reload: healthy=$($auditHealth.healthy)/$($auditHealth.total), min=$($auditHealth.min_healthy)"
    }
    if (-not $auditHealth.ok) {
        Write-Log ("browser tab audit still unhealthy after second reload: " + (($auditHealth.unhealthy) -join " | "))
        if (Test-AuditHealthNeedsManualAuth $auditHealth) {
            Ensure-ExtensionControlTab | Out-Null
            Write-Log "browser tab maintenance degraded: manual platform auth is required; skipping profile restart"
            Write-Status "degraded" "browser tab requires manual platform auth"
            exit 4
        }
        if (-not (Test-AuditHealthNeedsProfileRestart $auditHealth)) {
            Ensure-ExtensionControlTab | Out-Null
            Write-Status "degraded" "browser extension tabs unhealthy after targeted reload; skipped profile restart"
            exit 4
        }
        if (Invoke-ScraperChromeProfileRestart) {
            Invoke-PostReloadSettle -seconds $profileRestartSettleSeconds
            Invoke-PythonScript -command $python -script $audit -timeoutSeconds $auditTimeout
            $auditHealth = Get-AuditHealth
            Write-Log "browser tab audit health after profile restart: healthy=$($auditHealth.healthy)/$($auditHealth.total), min=$($auditHealth.min_healthy)"
        }
    }
    if (-not $auditHealth.ok) {
        Write-Log ("browser tab maintenance degraded after profile restart: " + (($auditHealth.unhealthy) -join " | "))
        Write-Status "degraded" "browser extension tabs unhealthy after reload/profile restart"
        exit 4
    }
    Ensure-ExtensionControlTab | Out-Null
    Write-Log "browser tab maintenance complete"
    Write-Status "ok" "audit and reload completed"
} catch {
    Write-Log ("browser tab maintenance failed: " + $_.Exception.Message)
    Write-Status "failed" $_.Exception.Message
    throw
} finally {
    Pop-Location
    if ($haveMutex) {
        try { $mutex.ReleaseMutex() | Out-Null } catch {}
    }
    if ($mutex) {
        $mutex.Dispose()
    }
}
