param(
    [string]$Repo = "C:\unifiedcollector",
    [string]$DashboardHealthUrl = "http://127.0.0.1:8700/health",
    [string]$CdpUrl = "http://127.0.0.1:9333",
    [int]$TimeoutSeconds = 8
)

$ErrorActionPreference = "Stop"

function Add-Check {
    param(
        [System.Collections.Generic.List[object]]$Checks,
        [string]$Name,
        [bool]$Ok,
        [string]$Detail = ""
    )
    $Checks.Add([pscustomobject]@{
        name = $Name
        ok = $Ok
        detail = $Detail
    }) | Out-Null
}

function Invoke-JsonGet {
    param([string]$Url)
    $oldProgress = $ProgressPreference
    $ProgressPreference = "SilentlyContinue"
    try {
        return Invoke-RestMethod -Uri $Url -TimeoutSec $TimeoutSeconds
    } finally {
        $ProgressPreference = $oldProgress
    }
}

function Test-UrlHostMatches {
    param(
        [string]$Url,
        [string]$ExpectedHost
    )
    try {
        $uri = [System.Uri]$Url
        $actual = $uri.Host.ToLowerInvariant()
        $expected = $ExpectedHost.ToLowerInvariant()
        return $actual -eq $expected -or $actual.EndsWith(".$expected")
    } catch {
        return $false
    }
}

$checks = [System.Collections.Generic.List[object]]::new()
$repoPath = (Resolve-Path -LiteralPath $Repo).Path
$composePath = Join-Path $repoPath "docker\docker-compose.yml"
$startup = [Environment]::GetFolderPath("Startup")
$startupNames = @(
    "UnifiedCollector_Startup.bat",
    "UnifiedCollectorBrowserMaintenance.cmd",
    "UnifiedCollectorDockerWatchdog.cmd",
    "UnifiedCollectorBrowserCookieVault.cmd"
)

Add-Check $checks "repo path" (Test-Path -LiteralPath $repoPath) $repoPath
Add-Check $checks "compose file" (Test-Path -LiteralPath $composePath) $composePath

foreach ($name in $startupNames) {
    $path = Join-Path $startup $name
    Add-Check $checks "startup: $name" (Test-Path -LiteralPath $path) $path
}

try {
    $psRows = docker compose -f $composePath ps --format json 2>$null
    $services = @()
    foreach ($row in $psRows) {
        if (-not $row) { continue }
        $services += ($row | ConvertFrom-Json)
    }
    $required = @(
        "postgres",
        "redis",
        "dashboard",
        "scheduler",
        "watchdog",
        "collector",
        "collector_telegram",
        "collector_instagram",
        "collector_tiktok",
        "collector_lemon8",
        "realtime_feed",
        "browser_cookie_vault"
    )
    foreach ($svc in $required) {
        $row = $services | Where-Object { $_.Service -eq $svc } | Select-Object -First 1
        $ok = $null -ne $row -and $row.State -eq "running" -and ($row.Health -in @("", "healthy", $null))
        $detail = if ($row) { "$($row.Name): $($row.Status)" } else { "missing" }
        Add-Check $checks "service: $svc" $ok $detail
    }
} catch {
    Add-Check $checks "docker compose ps" $false $_.Exception.Message
}

try {
    $health = Invoke-JsonGet $DashboardHealthUrl
    $ok = $health.status -eq "ok" -and $health.database -eq "healthy"
    Add-Check $checks "dashboard health" $ok ($health | ConvertTo-Json -Compress -Depth 6)
} catch {
    Add-Check $checks "dashboard health" $false $_.Exception.Message
}

try {
    $version = Invoke-JsonGet "$CdpUrl/json/version"
    Add-Check $checks "chrome cdp" ($version.Browser -match "^Chrome/") $version.Browser
} catch {
    Add-Check $checks "chrome cdp" $false $_.Exception.Message
}

try {
    $tabsPayload = Invoke-JsonGet "$CdpUrl/json/list"
    $tabs = @()
    foreach ($tab in $tabsPayload) {
        if ($tab -and $tab.url) {
            $tabs += $tab
        }
    }
    $urls = @()
    foreach ($tab in $tabs) {
        $urls += [string]$tab.url
    }
    $platforms = @{
        instagram = "instagram.com"
        tiktok = "tiktok.com"
        lemon8 = "lemon8-app.com"
        x = "x.com"
        threads = "threads.com"
        facebook = "facebook.com"
        strava = "strava.com"
    }
    foreach ($platform in $platforms.Keys) {
        $needle = $platforms[$platform]
        $found = @($urls | Where-Object { Test-UrlHostMatches -Url $_ -ExpectedHost $needle } | Select-Object -First 1)
        $detail = if ($found.Count -gt 0) { [string]$found[0] } else { "missing" }
        Add-Check $checks "tab: $platform" ($found.Count -gt 0) $detail
    }
    $ext = @($urls | Where-Object { $_ -like "chrome-extension://*/tabs.html*" } | Select-Object -First 1)
    $extDetail = if ($ext.Count -gt 0) { [string]$ext[0] } else { "missing" }
    Add-Check $checks "extension control tab" ($ext.Count -gt 0) $extDetail
} catch {
    Add-Check $checks "chrome tabs" $false $_.Exception.Message
}

$pidPath = Join-Path $repoPath "tmp\browser_tab_maintenance_loop.pid"
$pidOk = $false
$pidDetail = "missing"
if (Test-Path -LiteralPath $pidPath) {
    $rawPid = Get-Content -LiteralPath $pidPath -ErrorAction SilentlyContinue | Select-Object -First 1
    $parsed = 0
    if ([int]::TryParse([string]$rawPid, [ref]$parsed)) {
        $proc = Get-Process -Id $parsed -ErrorAction SilentlyContinue
        $pidOk = $null -ne $proc
        $pidDetail = if ($proc) { "pid=$parsed running" } else { "pid=$parsed not running" }
    } else {
        $pidDetail = "invalid pid: $rawPid"
    }
}
Add-Check $checks "browser maintenance loop" $pidOk $pidDetail

$failed = @($checks | Where-Object { -not $_.ok })
$result = [pscustomobject]@{
    ok = $failed.Count -eq 0
    failed_count = $failed.Count
    checked_at = (Get-Date).ToString("o")
    checks = $checks
}

$result | ConvertTo-Json -Depth 8
if ($failed.Count -gt 0) {
    exit 1
}
