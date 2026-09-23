<#
Host-side auto-recovery for the managed CDP Chrome + UnifiedCollector extension.

WHY THIS EXISTS: After a machine restart or crash, Windows/Chrome may auto-start
the managed Chrome WITHOUT the extension loading cleanly -- a dead MV3 service
worker, or two duplicate extension copies -- which silently stops ALL browser
scraping (the "worker not fresh" / "browser tabs crashed" Telegram alerts).
The in-container watchdog cannot fix this because the browser lives on the HOST.

WHAT IT DOES: detects unreachable CDP or no browser ingest in the last 25 minutes
and relaunches Chrome via the canonical launcher. Unavailable database evidence
never triggers a relaunch when CDP is reachable. Run on a schedule or with -Loop.

USAGE:
  pwsh scripts\browser-autorecover.ps1                 # one check + recover
  pwsh scripts\browser-autorecover.ps1 -Loop           # continuous (every 10 min)
  pwsh scripts\browser-autorecover.ps1 -CheckOnly      # report health, no action

SCHEDULE (recommended): Windows Task Scheduler, trigger "At log on", action:
pwsh -File C:\unifiedcollector\scripts\browser-autorecover.ps1 -Loop
#>
param(
    [int]$IntervalMinutes = 10,
    [switch]$Loop,
    [switch]$CheckOnly,
    [int]$CdpPort = 9336,
    [int]$MaxRecoveriesPerHour = 4
)
$ErrorActionPreference = "Continue"
$repo = "C:\unifiedcollector"
$launcher = Join-Path $repo "scripts\start-scraper-chrome-cdp.ps1"
$tmp = Join-Path $repo "tmp"
if (-not (Test-Path $tmp)) { New-Item -ItemType Directory -Path $tmp | Out-Null }
$log = Join-Path $tmp "browser_autorecover.log"
$statusPath = Join-Path $tmp "browser_autorecover_status.json"

function Log($m) {
    $s = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -LiteralPath $log -Value "[$s] $m"
    Write-Output "[$s] $m"
}

function Get-ExtHealth {
    # Health = CDP reachable AND the extension is actually PRODUCING. An
    # "Inactive" MV3 service worker is NORMAL when idle (Chrome suspends it);
    # the real failure is a STALE ingest stream. Authoritative signal =
    # recent browser_ingest_events, not the service-worker target state.
    $staleMin = 25
    $res = @{ reachable = $false; recent_events = -1; healthy = $false; reason = "" }
    try {
        $null = Invoke-RestMethod "http://127.0.0.1:$CdpPort/json/version" -TimeoutSec 8
        $res.reachable = $true
    } catch {
        $res.reason = "cdp_unreachable"
        return $res
    }
    try {
        $q = "SELECT count(*) FROM browser_ingest_events WHERE created_at > now() - interval '$staleMin minutes'"
        $out = docker exec unifiedcollector_postgres psql -U collector -d unifiedcollector -t -A -c $q 2>$null
        # Native failures need an explicit check; casting empty output would yield zero.
        $countText = ("$out").Trim()
        $recentEvents = 0
        if ($LASTEXITCODE -ne 0 -or $countText -notmatch '^[0-9]+$' -or
            -not [int]::TryParse($countText, [ref]$recentEvents)) {
            throw "Database probe did not return a successful nonnegative integer count"
        }
        $res.recent_events = $recentEvents
    } catch {
        # Never relaunch Chrome on a DB blip - fail safe to healthy.
        $res.healthy = $true
        $res.reason = "db_probe_failed_assume_ok"
        return $res
    }
    if ($res.recent_events -gt 0) {
        $res.healthy = $true
        $res.reason = "ok"
    } else {
        $res.reason = "scraping_stale_${staleMin}m"
    }
    return $res
}

function Write-Status($health, $action) {
    $obj = @{
        checked_at = (Get-Date).ToUniversalTime().ToString("o")
        reachable = $health.reachable
        healthy = $health.healthy
        reason = $health.reason
        recent_events = $health.recent_events
        action = $action
    }
    try { $obj | ConvertTo-Json -Compress | Set-Content -LiteralPath $statusPath -Encoding UTF8 } catch {}
}

# Rate-limit recoveries so a persistently-broken state does not thrash Chrome.
function Test-RecoveryBudget {
    $windowStart = (Get-Date).AddHours(-1)
    if (-not (Test-Path $log)) { return $true }
    $recent = @(Get-Content -LiteralPath $log -Tail 200 | Where-Object { $_ -match 'RECOVERY: relaunching' })
    $count = 0
    foreach ($line in $recent) {
        if ($line -match '^\[(.+?)\]') {
            try { if ([datetime]::Parse($matches[1]) -gt $windowStart) { $count++ } } catch {}
        }
    }
    return ($count -lt $MaxRecoveriesPerHour)
}

function Invoke-Recovery {
    if (-not (Test-RecoveryBudget)) {
        Log "SKIP: recovery budget exhausted ($MaxRecoveriesPerHour/hr) - persistent failure, leaving for manual review"
        return
    }
    Log "RECOVERY: relaunching managed Chrome (killing 9336/ChromeCdpAutomationProfile procs)"
    Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'ChromeCdpAutomationProfile|9336|UnifiedCollector' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep 5
    try { & $launcher -AllowWhileChromeRunning 2>&1 | ForEach-Object { Log "launcher: $_" } }
    catch { Log "launcher error: $($_.Exception.Message)" }
}

function Invoke-Cycle {
    $health = Get-ExtHealth
    if ($health.healthy) {
        Log "NO RECOVERY: $($health.reason) (recent_events=$($health.recent_events))"
        Write-Status $health "none"
        return
    }
    Log "UNHEALTHY: $($health.reason) (reachable=$($health.reachable) recent_events=$($health.recent_events))"
    if ($CheckOnly) { Write-Status $health "check_only"; return }
    Invoke-Recovery
    Start-Sleep 12
    $after = Get-ExtHealth
    if ($after.healthy) {
        Log "POST-RECOVERY: $($after.reason) (recent_events=$($after.recent_events))"
        Write-Status $after "recovered"
    } else {
        Log "INCOMPLETE: still $($after.reason). Chrome was relaunched; inspect CDP reachability and browser ingest before further recovery."
        Write-Status $after "incomplete"
    }
}

if ($Loop) {
    Log "browser-autorecover loop starting (interval ${IntervalMinutes}m, port $CdpPort)"
    while ($true) {
        Invoke-Cycle
        Start-Sleep -Seconds ($IntervalMinutes * 60)
    }
} else {
    Invoke-Cycle
}
