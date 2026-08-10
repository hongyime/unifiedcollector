param(
    [string]$Repo = "C:\unifiedcollector",
    [string]$DashboardHealthUrl = "http://127.0.0.1:8700/health",
    [string]$CdpUrl = "http://127.0.0.1:9333",
    [string[]]$WhatsAppBridgeHealthUrls = @("http://127.0.0.1:3011/health", "http://127.0.0.1:3012/health"),
    [string]$BackupDir = "Z:\unifiedcollector\backups\db",
    [int]$BackupFreshHours = 30,
    [int]$ActiveBackupFreshMinutes = 20,
    [int]$MaintenanceStatusFreshMinutes = 45,
    [int]$DefaultSourceFreshMinutes = 30,
    [int]$RecentIngestionMinutes = 60,
    [int]$DlqBacklogGraceMinutes = 360,
    [int]$DlqPendingThreshold = 100,
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

function Test-WhatsAppBridgeReady {
    param($Health)
    return (
        $Health.whatsapp_ready -eq $true -or
        $Health.ready -eq $true -or
        $Health.connected -eq $true -or
        $Health.registered -eq $true
    )
}

function Test-WhatsAppBridgeWaitingForPairing {
    param($Health)
    $status = ([string]$Health.status).ToLowerInvariant()
    $reason = ([string]$Health.last_disconnect_reason).ToLowerInvariant()
    return (
        $Health.qr_available -eq $true -or
        $Health.needs_scan -eq $true -or
        $status -in @("awaiting_scan", "connecting_unpaired", "pairing", "qr", "qr_expired", "refreshing_qr") -or
        $reason.Contains("qr")
    )
}

function Format-WhatsAppBridgeHealth {
    param([int]$Index, $Health)
    $status = [string]$Health.status
    $phone = [string]$Health.phone_number
    $pushName = [string]$Health.push_name
    $authNote = ""
    if ($Health.auth_state -and $Health.auth_state.note) {
        $authNote = [string]$Health.auth_state.note
    }
    $parts = @("bridge ${Index}: $status")
    if ($phone) { $parts += $phone }
    if ($pushName) { $parts += $pushName }
    if ($authNote) { $parts += $authNote }
    return ($parts -join " / ")
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

function Test-AuthWallUrl {
    param([string]$Url)
    if (-not $Url) {
        return $false
    }
    try {
        $uri = [System.Uri]$Url
        $path = $uri.AbsolutePath.ToLowerInvariant()
        $query = $uri.Query.ToLowerInvariant()
        return (
            $path -match "/login|/signin|/checkpoint|/challenge|/i/flow/login|/i/jf/onboarding|/log_out" -or
            $query -match "mode=login|redirect_after_login|auth_platform|recaptcha|(^\?|&)logout="
        )
    } catch {
        return $false
    }
}

function Get-AuditTabUrl {
    param($Tab)
    if ($null -ne $Tab -and [string]$Tab.url_snapshot) {
        return [string]$Tab.url_snapshot
    }
    if ($null -ne $Tab -and [string]$Tab.url) {
        return [string]$Tab.url
    }
    return ""
}

function Test-AuditTabContentWall {
    param($Tab)
    if ($null -eq $Tab) {
        return $false
    }
    $status = [string]$Tab.page_health_status
    return $status -eq "recoverable_error_shell"
}

function Get-AuditTabWallDetail {
    param($Tab)
    if (-not (Test-AuditTabContentWall $Tab)) {
        return ""
    }
    $reason = [string]$Tab.page_health_reason
    $sample = [string]$Tab.page_health_sample
    $url = Get-AuditTabUrl $Tab
    return "page_health=recoverable_error_shell, reason=$reason, url=$url, sample=$sample"
}

function Invoke-PostgresText {
    param(
        [string]$Sql,
        [int]$QueryTimeoutSeconds = 20
    )
    return docker exec -e PGPASSWORD=collectorpass unifiedcollector_postgres psql `
        -U collector `
        -d unifiedcollector `
        -v ON_ERROR_STOP=1 `
        -Atc $Sql 2>$null
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
    $bridgeStates = @()
    for ($i = 0; $i -lt $WhatsAppBridgeHealthUrls.Count; $i++) {
        $url = $WhatsAppBridgeHealthUrls[$i]
        try {
            $health = Invoke-JsonGet $url
            $bridgeStates += [pscustomobject]@{
                index = $i + 1
                reachable = $true
                ready = Test-WhatsAppBridgeReady $health
                waiting = Test-WhatsAppBridgeWaitingForPairing $health
                detail = Format-WhatsAppBridgeHealth -Index ($i + 1) -Health $health
            }
        } catch {
            $bridgeStates += [pscustomobject]@{
                index = $i + 1
                reachable = $false
                ready = $false
                waiting = $false
                detail = "bridge $($i + 1): unreachable / $($_.Exception.Message)"
            }
        }
    }
    $readyCount = @($bridgeStates | Where-Object { $_.ready }).Count
    $reachableCount = @($bridgeStates | Where-Object { $_.reachable }).Count
    $waitingCount = @($bridgeStates | Where-Object { $_.waiting }).Count
    $summary = "ready=$readyCount/$($bridgeStates.Count), reachable=$reachableCount/$($bridgeStates.Count), waiting_for_pairing=$waitingCount"
    $detail = $summary + "; " + ((@($bridgeStates | ForEach-Object { $_.detail })) -join "; ")
    Add-Check $checks "whatsapp bridge readiness" ($readyCount -ge 1) $detail
} catch {
    Add-Check $checks "whatsapp bridge readiness" $false $_.Exception.Message
}

try {
    $sourceThresholds = [ordered]@{
        beeper = 30
        facebook = 60
        # GitHub is drained by the shared low-risk worker. A healthy boot can
        # spend more than 30 minutes on Strava/Search/Website before GitHub's
        # next successful metadata write, so this must verify liveness rather
        # than force a per-source half-hour cadence.
        github = 240
        instagram = 60
        lemon8 = 45
        search = 240
        strava = 60
        telegram = 30
        threads = 60
        tiktok = 60
        website = 30
        whatsapp = 30
        x = 90
        youtube = 30
    }
    $sourcesSql = ($sourceThresholds.Keys | ForEach-Object { "'" + $_.Replace("'", "''") + "'" }) -join ","
    $sourceRows = Invoke-PostgresText -Sql @"
SELECT source || '|' || status || '|' || COALESCE(round(extract(epoch from (now()-last_success_at))/60, 1)::text, 'null') || '|' || COALESCE(left(last_error, 120), '')
FROM source_health
WHERE source IN ($sourcesSql)
ORDER BY source
"@
    $sourceByName = @{}
    foreach ($row in @($sourceRows)) {
        if (-not $row) { continue }
        $parts = ([string]$row).Split("|", 4)
        if ($parts.Count -lt 3) { continue }
        $sourceByName[$parts[0]] = [pscustomobject]@{
            status = $parts[1]
            age = $parts[2]
            error = if ($parts.Count -ge 4) { $parts[3] } else { "" }
        }
    }
    foreach ($source in $sourceThresholds.Keys) {
        $row = $sourceByName[$source]
        if (-not $row) {
            Add-Check $checks "source fresh: $source" $false "missing source_health row"
            continue
        }
        $age = [double]::PositiveInfinity
        $hasAge = [double]::TryParse([string]$row.age, [ref]$age)
        $threshold = [int]$sourceThresholds[$source]
        $ok = $row.status -eq "running" -and $hasAge -and $age -le $threshold
        $detail = "status=$($row.status), success_age=$($row.age)m, threshold=${threshold}m"
        if ($row.error) { $detail += ", last_error=$($row.error)" }
        Add-Check $checks "source fresh: $source" $ok $detail
    }
} catch {
    Add-Check $checks "source freshness query" $false $_.Exception.Message
}

try {
    $recentSql = @'
CREATE TEMP TABLE IF NOT EXISTS uc_recent_ingestion_counts (
    source_name text NOT NULL,
    label text NOT NULL,
    row_count bigint NOT NULL
) ON COMMIT DROP;
TRUNCATE uc_recent_ingestion_counts;
DO $$
DECLARE
    part record;
    n bigint;
BEGIN
    FOR part IN
        SELECT *
        FROM (VALUES
            ('telegram', 'telegram_messages', 'collected_at', 'messages'),
            ('whatsapp', 'whatsapp_messages', 'collected_at', 'messages'),
            ('beeper', 'beeper_shadow_messages', 'ingested_at', 'messages'),
            ('instagram', 'instagram_profiles', 'updated_at', 'profiles'),
            ('instagram', 'instagram_posts', 'collected_at', 'posts'),
            ('tiktok', 'tiktok_profiles', 'updated_at', 'profiles'),
            ('tiktok', 'tiktok_posts', 'collected_at', 'posts'),
            ('lemon8', 'lemon8_profiles', 'updated_at', 'profiles'),
            ('lemon8', 'lemon8_posts', 'collected_at', 'posts'),
            ('threads', 'threads_posts', 'collected_at', 'posts'),
            ('facebook', 'facebook_profiles', 'updated_at', 'profiles'),
            ('facebook', 'facebook_posts', 'collected_at', 'posts'),
            ('x', 'x_profiles', 'updated_at', 'profiles'),
            ('x', 'x_posts', 'collected_at', 'posts'),
            ('youtube', 'youtube_channels', 'updated_at', 'channels'),
            ('youtube', 'youtube_videos', 'collected_at', 'videos'),
            ('github', 'github_users', 'collected_at', 'users'),
            ('github', 'github_repos', 'collected_at', 'repos'),
            ('github', 'github_commits', 'collected_at', 'commits'),
            ('github', 'github_issues', 'collected_at', 'issues'),
            ('github', 'github_issue_comments', 'collected_at', 'issue comments'),
            ('github', 'github_pr_reviews', 'collected_at', 'PR reviews'),
            ('github', 'github_pr_review_comments', 'collected_at', 'PR review comments'),
            ('website', 'website_pages', 'collected_at', 'pages'),
            ('strava', 'strava_athletes', 'updated_at', 'profiles'),
            ('strava', 'strava_activities', 'collected_at', 'activities'),
            ('search', 'search_results', 'collected_at', 'results')
        ) AS v(source_name, table_name, column_name, label)
    LOOP
        IF to_regclass('public.' || part.table_name) IS NULL THEN
            CONTINUE;
        END IF;
        EXECUTE format(
            'SELECT count(*) FROM %I WHERE %I >= NOW() - make_interval(mins => %s)',
            part.table_name,
            part.column_name,
            __MINUTES__
        ) INTO n;
        IF n > 0 THEN
            INSERT INTO uc_recent_ingestion_counts VALUES (part.source_name, part.label, n);
        END IF;
    END LOOP;
    IF to_regclass('public.media_items') IS NOT NULL THEN
        INSERT INTO uc_recent_ingestion_counts
        SELECT source, 'media', count(*)
        FROM media_items
        WHERE collected_at >= NOW() - make_interval(mins => __MINUTES__)
        GROUP BY source
        HAVING count(*) > 0;
    END IF;
END $$;
SELECT source_name || ' ' || label || '=' || row_count::text
FROM uc_recent_ingestion_counts
ORDER BY row_count DESC, source_name, label
LIMIT 18;
'@.Replace("__MINUTES__", [string]$RecentIngestionMinutes)
    $rows = @(Invoke-PostgresText -Sql $recentSql -QueryTimeoutSeconds 35 | Where-Object {
        $_ -and [string]$_ -notmatch '^(CREATE TABLE|TRUNCATE TABLE|DO)$'
    })
    $detail = if ($rows.Count -gt 0) {
        "last ${RecentIngestionMinutes}m: " + (($rows | Select-Object -First 18) -join "; ")
    } else {
        "last ${RecentIngestionMinutes}m: no source rows or media rows counted; source freshness checks still passed"
    }
    Add-Check $checks "recent ingestion window" $true $detail
} catch {
    Add-Check $checks "recent ingestion window" $false $_.Exception.Message
}

try {
    $dlqRows = Invoke-PostgresText -Sql @"
SELECT source || '|' || status || '|' || count(*)::text || '|' || COALESCE(round(extract(epoch from (now()-min(created_at)))/60, 1)::text, '0')
FROM dead_letter_queue
WHERE (
    status IN ('queued', 'retry')
    OR (status = 'pending' AND next_retry_at <= NOW())
)
  AND created_at < NOW() - INTERVAL '1 minute' * $DlqBacklogGraceMinutes
GROUP BY source, status
HAVING count(*) >= $DlqPendingThreshold
ORDER BY count(*) DESC
"@
    $dlqRows = @($dlqRows | Where-Object { $_ })
    if ($dlqRows.Count -eq 0) {
        Add-Check $checks "dead letter backlog" $true "no stale actionable pending/queued/retry rows"
    } else {
        Add-Check $checks "dead letter backlog" $false ($dlqRows -join "; ")
    }
} catch {
    Add-Check $checks "dead letter backlog" $false $_.Exception.Message
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
        $found = @($urls | Where-Object {
            (Test-UrlHostMatches -Url $_ -ExpectedHost $needle) -and -not (Test-AuthWallUrl $_)
        } | Select-Object -First 1)
        $authWall = @($urls | Where-Object {
            (Test-UrlHostMatches -Url $_ -ExpectedHost $needle) -and (Test-AuthWallUrl $_)
        } | Select-Object -First 1)
        $detail = if ($found.Count -gt 0) { [string]$found[0] } else { "missing" }
        if ($found.Count -eq 0 -and $authWall.Count -gt 0) {
            $detail = "auth wall: $([string]$authWall[0])"
        }
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

$maintenanceStatusPath = Join-Path $repoPath "tmp\browser_tab_maintenance_status.json"
$maintenanceOk = $false
$maintenanceDetail = "missing status file: $maintenanceStatusPath"
$maintenanceRunningWithOkPrevious = $false
if (Test-Path -LiteralPath $maintenanceStatusPath) {
    try {
        $maintenanceStatus = Get-Content -LiteralPath $maintenanceStatusPath -Raw | ConvertFrom-Json
        $checkedAt = [datetime]$maintenanceStatus.checked_at
        $ageMinutes = ((Get-Date) - $checkedAt).TotalMinutes
        $lastTerminalState = [string]$maintenanceStatus.last_terminal_state
        $maintenanceRunningWithOkPrevious = (
            $maintenanceStatus.state -eq "running" -and
            $lastTerminalState -eq "ok" -and
            $ageMinutes -le 10
        )
        $maintenanceOk = (
            ($maintenanceStatus.state -eq "ok" -and $ageMinutes -le $MaintenanceStatusFreshMinutes) -or
            $maintenanceRunningWithOkPrevious
        )
        $maintenanceDetail = "state=$($maintenanceStatus.state), age=$([math]::Round($ageMinutes, 1))m, detail=$($maintenanceStatus.detail)"
    } catch {
        $maintenanceDetail = "could not parse status file: $($_.Exception.Message)"
    }
}
Add-Check $checks "browser maintenance latest status" $maintenanceOk $maintenanceDetail

$auditResultPath = Join-Path $repoPath "tmp\browser_tab_audit_result.json"
$extensionPlatforms = @("instagram", "threads", "tiktok", "lemon8", "x", "facebook", "strava")
if ($maintenanceRunningWithOkPrevious) {
    Add-Check $checks "extension content script audit" $true "maintenance pass in progress; last_terminal_state=ok"
} elseif (Test-Path -LiteralPath $auditResultPath) {
    try {
        $auditResult = Get-Content -LiteralPath $auditResultPath -Raw | ConvertFrom-Json
        foreach ($platform in $extensionPlatforms) {
            $tabs = @($auditResult.$platform)
            if ($tabs.Count -eq 0) {
                Add-Check $checks "extension content script: $platform" $false "missing audit row"
                continue
            }
            $healthy = @(
                $tabs | Where-Object {
                    $_.responsive_main -eq $true -and
                    $_.cs -eq $true -and
                    $_.cs_running -eq $true -and
                    [string]$_.cs_version -and
                    -not (Test-AuthWallUrl (Get-AuditTabUrl $_)) -and
                    -not (Test-AuditTabContentWall $_)
                }
            )
            $allTabsHealthy = ($healthy.Count -gt 0)
            $problemCount = [Math]::Max(0, $tabs.Count - $healthy.Count)
            $detailRows = @(
                $tabs | ForEach-Object {
                    $wall = Get-AuditTabWallDetail $_
                    $detail = "resp=$($_.responsive_main), cs=$($_.cs), running=$($_.cs_running), ver=$($_.cs_version), page_health=$($_.page_health_status), url=$($_.url_snapshot)"
                    if ($wall) { $detail += ", $wall" }
                    $detail
                }
            )
            $summary = "healthy=$($healthy.Count)/$($tabs.Count), problem_or_stale=$problemCount"
            Add-Check $checks "extension content script: $platform" $allTabsHealthy ($summary + "; " + ($detailRows -join "; "))
        }
    } catch {
        Add-Check $checks "extension content script audit" $false "could not parse audit result: $($_.Exception.Message)"
    }
} else {
    Add-Check $checks "extension content script audit" $false "missing audit result: $auditResultPath"
}

$backupOk = $false
$backupDetail = "backup directory missing: $BackupDir"
if (Test-Path -LiteralPath $BackupDir) {
    $now = Get-Date
    $completed = Get-ChildItem -LiteralPath $BackupDir -Filter "unifiedcollector_*.dump" -File -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    $active = Get-ChildItem -LiteralPath $BackupDir -Filter ".inprogress_*.dump" -File -Force -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if ($completed -and (($now - $completed.LastWriteTime).TotalHours -le $BackupFreshHours)) {
        $backupOk = $true
        $backupDetail = "latest completed $($completed.Name), age=$([math]::Round(($now - $completed.LastWriteTime).TotalHours, 1))h"
    } elseif ($active -and (($now - $active.LastWriteTime).TotalMinutes -le $ActiveBackupFreshMinutes)) {
        $backupOk = $true
        $backupDetail = "active dump $($active.Name), size=$($active.Length), touched=$([math]::Round(($now - $active.LastWriteTime).TotalMinutes, 1))m ago"
    } elseif ($completed) {
        $backupDetail = "latest completed $($completed.Name) is stale, age=$([math]::Round(($now - $completed.LastWriteTime).TotalHours, 1))h"
        if ($active) {
            $backupDetail += "; active dump $($active.Name) touched $([math]::Round(($now - $active.LastWriteTime).TotalMinutes, 1))m ago"
        }
    } elseif ($active) {
        $backupDetail = "only active dump $($active.Name), touched $([math]::Round(($now - $active.LastWriteTime).TotalMinutes, 1))m ago"
    } else {
        $backupDetail = "no completed or active dump found in $BackupDir"
    }
}
Add-Check $checks "db backup freshness" $backupOk $backupDetail

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
