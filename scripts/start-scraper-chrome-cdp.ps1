param(
    [string]$ChromePath = "",
    [string]$UserDataDir = "",
    [string]$ExtensionPath = "C:\unifiedcollector\extension",
    [int]$RemoteDebuggingPort = 9333,
    [switch]$AllowWhileChromeRunning,
    [switch]$CloseExistingIfNoVisibleWindows,
    [switch]$CloseExistingCdpProfile,
    [switch]$FallbackOpenControlIfCleanupBlocked,
    [string[]]$OpenIds = @(),
    [switch]$OpenAll,
    [switch]$NoOpenAll,
    [switch]$NoScrape,
    [switch]$NoTest,
    [switch]$IsolateExtensions,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

if (-not $PSBoundParameters.ContainsKey("IsolateExtensions")) {
    $isolateDefault = [Environment]::GetEnvironmentVariable("UC_CHROME_ISOLATE_EXTENSIONS")
    if ([string]::IsNullOrWhiteSpace($isolateDefault) -or $isolateDefault.Trim().ToLowerInvariant() -notin @("0", "false", "no", "off")) {
        $IsolateExtensions = $true
    }
}

function Resolve-ChromePath {
    param([string]$Requested)
    if ($Requested -and (Test-Path -LiteralPath $Requested)) {
        return (Resolve-Path -LiteralPath $Requested).Path
    }
    # Branded Chrome 137+ removed command-line unpacked extension loading.
    # Prefer Chrome-for-Testing/Playwright Chromium when available so the
    # UnifiedCollector MV3 extension can be restored automatically after reboot.
    $extensionCapableCandidates = @(
        "$env:LOCALAPPDATA\Google\Chrome for Testing\Application\chrome.exe",
        "$env:ProgramFiles\Google\Chrome for Testing\Application\chrome.exe",
        "${env:ProgramFiles(x86)}\Google\Chrome for Testing\Application\chrome.exe"
    )
    foreach ($candidate in $extensionCapableCandidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) {
            return $candidate
        }
    }
    $playwrightRoot = Join-Path $env:LOCALAPPDATA "ms-playwright"
    if (Test-Path -LiteralPath $playwrightRoot) {
        $playwrightDirs = @(Get-ChildItem -LiteralPath $playwrightRoot -Directory -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending |
            Select-Object -First 12)
        foreach ($dir in $playwrightDirs) {
            foreach ($relative in @("chrome-win64\chrome.exe", "chrome-win\chrome.exe", "chromium\chrome-win\chrome.exe", "chrome.exe")) {
                $candidate = Join-Path $dir.FullName $relative
                if (Test-Path -LiteralPath $candidate) {
                    return $candidate
                }
            }
        }
    }
    $fallbackCandidates = @(
        "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
        "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
        "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
    )
    foreach ($candidate in $fallbackCandidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) {
            return $candidate
        }
    }
    throw "chrome.exe not found; pass -ChromePath"
}

function Resolve-UserDataDir {
    param([string]$Requested)
    if ($Requested) {
        return $Requested
    }
    # Chrome 136+ ignores --remote-debugging-port when pointed at the default
    # Chrome profile directory. Prefer the dedicated automation profile because
    # older profile folders may have been created by branded Chrome and can be
    # incompatible with Chrome-for-Testing / Playwright Chromium.
    $base = Join-Path $env:LOCALAPPDATA "UnifiedCollector"
    $automation = Join-Path $base "ChromeCdpAutomationProfile"
    if (Test-Path -LiteralPath $automation) {
        return $automation
    }
    $recovered = Join-Path $base "ChromeCdpRecoveredProfile"
    if (Test-Path -LiteralPath $recovered) {
        return $recovered
    }
    return $automation
}

function Test-CdpAvailable {
    param([int]$Port)
    try {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/json/version" -UseBasicParsing -TimeoutSec 3
        return ($response.StatusCode -ge 200 -and $response.StatusCode -lt 300)
    } catch {
        try {
            $targets = @(Invoke-RestMethod -Uri "http://127.0.0.1:$Port/json/list" -Method Get -TimeoutSec 5)
            return $targets.Count -ge 0
        } catch {
            return $false
        }
    }
}

function Get-PortListenerPids {
    param([int]$Port)
    $pids = @()
    try {
        $lines = & "$env:SystemRoot\System32\netstat.exe" -ano -p tcp
        foreach ($line in @($lines)) {
            if ($line -notmatch "\sLISTENING\s+(\d+)\s*$") {
                continue
            }
            if ($line -notmatch "(:|\])$Port\s+") {
                continue
            }
            $pid = 0
            if ([int]::TryParse($Matches[1], [ref]$pid) -and $pid -gt 0) {
                $pids += $pid
            }
        }
    } catch {
        return @()
    }
    return @($pids | Sort-Object -Unique)
}

function Stop-ProcessIds {
    param([int[]]$Pids)
    foreach ($processId in @($Pids | Sort-Object -Unique)) {
        if ($processId -le 0) {
            continue
        }
        try {
            Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
            Start-Sleep -Milliseconds 300
        } catch {
            # Fall through to taskkill below.
        }
        if (-not (Get-Process -Id $processId -ErrorAction SilentlyContinue)) {
            continue
        }
        try {
            $taskkillOutput = & "$env:SystemRoot\System32\taskkill.exe" /PID $processId /F /T 2>&1
            $taskkillExitCode = $LASTEXITCODE
            if ($taskkillExitCode -ne 0 -and (Get-Process -Id $processId -ErrorAction SilentlyContinue)) {
                $detail = (($taskkillOutput | Select-Object -First 3) -join " ").Trim()
                Write-Warning "taskkill failed for PID ${processId} with exit code ${taskkillExitCode}: $detail"
            }
        } catch {
            if (Get-Process -Id $processId -ErrorAction SilentlyContinue) {
                Write-Warning "Could not kill PID ${processId}: $($_.Exception.Message)"
            }
        }
    }
}

function Get-CdpProfileChromeProcesses {
    param([string]$UserDataDir, [int]$Port)
    return @(Get-ScraperProfileChromeProcesses -UserDataDir $UserDataDir | Where-Object {
        $cmd = [string]$_.CommandLine
        $cmd -match "--remote-debugging-port(?:=|\s+)$Port\b"
    })
}

function Get-ScraperProfileChromeProcesses {
    param([string]$UserDataDir)
    $profileFull = [IO.Path]::GetFullPath($UserDataDir).TrimEnd('\')
    $profileSlash = $profileFull.Replace('\', '/')
    return @(Get-ChromeProcesses | Where-Object {
        $cmd = [string]$_.CommandLine
        if (-not $cmd) { return $false }
        return (
            $cmd.IndexOf($profileFull, [StringComparison]::OrdinalIgnoreCase) -ge 0 -or
            $cmd.IndexOf($profileSlash, [StringComparison]::OrdinalIgnoreCase) -ge 0
        )
    })
}

function Stop-CdpProfileChrome {
    param([string]$UserDataDir, [int]$Port)
    # Close by the dedicated UnifiedCollector profile, not only by the CDP flag.
    # A failed Chrome startup can keep the scraper profile open while dropping
    # --remote-debugging-port, which otherwise traps maintenance in
    # "visible Chrome without CDP" and prevents automatic recovery.
    $profileProcesses = @(Get-ScraperProfileChromeProcesses -UserDataDir $UserDataDir)
    if ($profileProcesses.Count -eq 0) {
        return
    }
    $rootIds = @($profileProcesses |
        Where-Object { [string]$_.CommandLine -notmatch "--type=" } |
        Select-Object -ExpandProperty ProcessId |
        Sort-Object -Unique)
    $childIds = @($profileProcesses |
        Where-Object { [string]$_.CommandLine -match "--type=" } |
        Select-Object -ExpandProperty ProcessId |
        Sort-Object -Unique)
    $ids = @($rootIds + $childIds | Sort-Object -Unique)
    Write-Host "Closing scraper Chrome profile process(es): $($ids -join ', ')"
    if ($rootIds.Count -gt 0) {
        Stop-ProcessIds -Pids $rootIds
        Start-Sleep -Seconds 2
    }
    $remaining = @(Get-ScraperProfileChromeProcesses -UserDataDir $UserDataDir |
        Select-Object -ExpandProperty ProcessId |
        Sort-Object -Unique)
    if ($remaining.Count -gt 0) {
        Stop-ProcessIds -Pids $remaining
    }
    if (-not (Wait-PortReleased -Port $Port -TimeoutSeconds 20)) {
        $remainingPortOwners = @(Get-PortListenerPids -Port $Port)
        Write-Error "Port $Port is still busy after scraper profile cleanup; remaining PID(s): $($remainingPortOwners -join ', ')"
    }
}

function Wait-PortReleased {
    param([int]$Port, [int]$TimeoutSeconds = 15)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (@(Get-PortListenerPids -Port $Port).Count -eq 0) {
            return $true
        }
        Start-Sleep -Milliseconds 500
    }
    return (@(Get-PortListenerPids -Port $Port).Count -eq 0)
}

function Get-ExtensionIdFromCdp {
    param([int]$Port)
    $knownIds = @(Get-KnownExtensionIds)
    try {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/json/list" -UseBasicParsing -TimeoutSec 5
        $targets = @($response.Content | ConvertFrom-Json)
        foreach ($target in @($targets)) {
            $url = [string]($target.url)
            $targetType = [string]($target.type)
            if ($targetType -notin @("service_worker", "background_page")) {
                continue
            }
            $match = [regex]::Match($url, '^chrome-extension://([a-p]{32})/')
            if ($match.Success -and $knownIds -contains $match.Groups[1].Value) {
                return $match.Groups[1].Value
            }
        }
        foreach ($target in @($targets)) {
            $url = [string]($target.url)
            $match = [regex]::Match($url, '^chrome-extension://([a-p]{32})/')
            if ($match.Success -and $knownIds -contains $match.Groups[1].Value) {
                return $match.Groups[1].Value
            }
        }
    } catch {
        return ""
    }
    return ""
}

function Get-KnownExtensionIds {
    # Historical and current unpacked-extension ids used by this repo. Keep
    # these as fallbacks because a dormant MV3 worker may not appear in
    # /json/list until its options page is opened.
    return @(
        "pkmdmcklnjdeocoeigmlakhomhhcpafb",
        "nkeimhogjdpnpccoofpliimaahmaaome"
    )
}

function Get-PrimaryKnownExtensionId {
    return @(Get-KnownExtensionIds)[0]
}

function Normalize-CdpTargetUrlForReuse {
    param([string]$Url)
    $text = [string]$Url
    if ($text -match '^chrome-extension://([a-p]{32})/tabs\.html(?:[?#].*)?$') {
        return "chrome-extension://$($Matches[1])/tabs.html"
    }
    return $text
}

function Open-CdpTarget {
    param([int]$Port, [string]$Url, [int]$TimeoutSeconds = 5)
    $encoded = [uri]::EscapeDataString($Url)
    Invoke-WebRequest -Uri "http://127.0.0.1:$Port/json/new?$encoded" -Method Put -UseBasicParsing -TimeoutSec $TimeoutSeconds | Out-Null
}

function Find-ExistingCdpTarget {
    param([int]$Port, [string]$Url)
    try {
        $desiredUrl = Normalize-CdpTargetUrlForReuse -Url $Url
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/json/list" -UseBasicParsing -TimeoutSec 5
        $targets = @($response.Content | ConvertFrom-Json)
        foreach ($target in @($targets)) {
            if ([string]$target.type -ne "page") {
                continue
            }
            $targetUrl = Normalize-CdpTargetUrlForReuse -Url ([string]$target.url)
            if ($targetUrl -eq $desiredUrl) {
                return [string]$target.id
            }
        }
    } catch {
        return ""
    }
    return ""
}

function Activate-CdpTarget {
    param([int]$Port, [string]$TargetId, [int]$TimeoutSeconds = 5)
    if (-not $TargetId) {
        return $false
    }
    try {
        Invoke-WebRequest -Uri "http://127.0.0.1:$Port/json/activate/$TargetId" -UseBasicParsing -TimeoutSec $TimeoutSeconds | Out-Null
        return $true
    } catch {
        return $false
    }
}

function Try-OpenCdpTarget {
    param([int]$Port, [string]$Url)
    try {
        $existingTargetId = Find-ExistingCdpTarget -Port $Port -Url $Url
        if ($existingTargetId) {
            Activate-CdpTarget -Port $Port -TargetId $existingTargetId | Out-Null
            return $true
        }
        $targetTimeout = 5
        $rawTargetTimeout = [Environment]::GetEnvironmentVariable("UC_CHROME_OPEN_TARGET_TIMEOUT_SECONDS")
        $parsedTargetTimeout = 0
        if ([int]::TryParse($rawTargetTimeout, [ref]$parsedTargetTimeout) -and $parsedTargetTimeout -ge 2) {
            $targetTimeout = $parsedTargetTimeout
        }
        Open-CdpTarget -Port $Port -Url $Url -TimeoutSeconds $targetTimeout
        return $true
    } catch {
        Write-Warning "Could not open CDP target ${Url}: $($_.Exception.Message)"
        return $false
    }
}

function Open-ExtensionControlPage {
    param([int]$Port, [string]$TabsUrlPath)
    $extensionId = Get-ExtensionIdFromCdp $Port
    if ($extensionId) {
        $tabsUrl = "chrome-extension://$extensionId/$TabsUrlPath"
        if (Try-OpenCdpTarget -Port $Port -Url $tabsUrl) {
            Write-Host "Opened extension control page: $tabsUrl"
            return $true
        }
        return $false
    }
    foreach ($knownId in @(Get-KnownExtensionIds)) {
        $tabsUrl = "chrome-extension://$knownId/$TabsUrlPath"
        if (Try-OpenCdpTarget -Port $Port -Url $tabsUrl) {
            Start-Sleep -Seconds 2
            $extensionId = Get-ExtensionIdFromCdp $Port
            if ($extensionId) {
                Write-Host "Opened extension control page via known id: $tabsUrl"
                return $true
            }
        }
    }
    return $false
}

function Get-PlatformLaunchUrls {
    param([string[]]$Ids, [bool]$All)
    $expandedPlatformTabs = [Environment]::GetEnvironmentVariable("UC_CHROME_OPEN_EXPANDED_PLATFORM_TABS") -eq "1"
    $platforms = [ordered]@{
        instagram = "https://www.instagram.com/"
        tiktok = if ($expandedPlatformTabs) {
            @(
                "https://www.tiktok.com/following",
                "https://www.tiktok.com/foryou",
                "https://www.tiktok.com/explore"
            )
        } else {
            "https://www.tiktok.com/following"
        }
        lemon8 = "https://www.lemon8-app.com/topic/singapore?region=sg"
        x = "https://x.com/home"
        threads = "https://www.threads.com/"
        facebook = "https://www.facebook.com/"
        strava = "https://www.strava.com/dashboard"
    }
    $selected = @()
    if ($All) {
        $selected = @($platforms.Keys)
    } else {
        $selected = @($Ids)
    }
    foreach ($id in $selected) {
        if ($platforms.Keys -contains $id) {
            foreach ($url in @($platforms[$id])) {
                $url
            }
        }
    }
}

function Get-ChromeProcesses {
    $liveIds = @{}
    foreach ($proc in @(Get-Process chrome -ErrorAction SilentlyContinue)) {
        if ($proc.HandleCount -le 0) {
            continue
        }
        $liveIds[[int]$proc.Id] = $true
    }
    return @(Get-CimInstance Win32_Process -Filter "name='chrome.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $liveIds.ContainsKey([int]$_.ProcessId) })
}

function Get-VisibleChromeWindows {
    return @(Get-Process chrome -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 })
}

function Test-UnifiedCollectorControlWindow {
    param($Process, [string]$UserDataDir = "")
    $title = [string]$Process.MainWindowTitle
    foreach ($knownId in @(Get-KnownExtensionIds)) {
        if ($title -like "chrome-extension://$knownId/tabs.html*") {
            return $true
        }
    }
    if ($UserDataDir) {
        $profileFull = [IO.Path]::GetFullPath($UserDataDir).TrimEnd('\')
        $profileSlash = $profileFull.Replace('\', '/')
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$($Process.Id)" -ErrorAction SilentlyContinue
        $cmd = if ($proc) { [string]$proc.CommandLine } else { "" }
        if (
            $cmd.IndexOf($profileFull, [StringComparison]::OrdinalIgnoreCase) -ge 0 -or
            $cmd.IndexOf($profileSlash, [StringComparison]::OrdinalIgnoreCase) -ge 0
        ) {
            return $true
        }
    }
    return $false
}

function Get-UnsafeVisibleChromeWindows {
    param([array]$VisibleWindows, [string]$UserDataDir = "")
    return @($VisibleWindows | Where-Object { -not (Test-UnifiedCollectorControlWindow -Process $_ -UserDataDir $UserDataDir) })
}

function Stop-ChromeProcessTree {
    param([array]$Processes)
    if ($Processes.Count -eq 0) {
        return
    }
    foreach ($proc in $Processes) {
        try {
            Stop-Process -Id $proc.ProcessId -Force -ErrorAction Stop
        } catch {
            # Some Chrome profile/session owners refuse Stop-Process. Fall back to taskkill below.
        }
    }
    Start-Sleep -Seconds 2
    $remaining = @(Get-ChromeProcesses)
    if ($remaining.Count -eq 0) {
        return
    }
    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $taskkillOutput = & "$env:SystemRoot\System32\taskkill.exe" /IM chrome.exe /F /T 2>&1
        $taskkillExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    Start-Sleep -Seconds 2
    $remaining = @(Get-ChromeProcesses)
    if ($remaining.Count -gt 0) {
        $ids = ($remaining | Select-Object -ExpandProperty ProcessId) -join ", "
        $detail = (($taskkillOutput | Select-Object -First 3) -join " ").Trim()
        $staleOnly = $true
        $taskkillText = ($taskkillOutput -join "`n")
        foreach ($proc in $remaining) {
            $pidText = [string]$proc.ProcessId
            if (
                $taskkillText -notmatch "PID $pidText\b" -or
                $taskkillText -notmatch "no running instance"
            ) {
                $staleOnly = $false
                break
            }
        }
        if ($staleOnly) {
            Write-Warning "Ignoring stale Chrome WMI rows after taskkill: $ids"
            return
        }
        throw "Chrome is still running after repair attempt; taskkill exit code ${taskkillExitCode}; remaining PIDs: $ids; $detail"
    }
}

function Disable-ChromeBackgroundMode {
    param([string]$UserDataDir)

    $candidateFiles = @(
        (Join-Path $UserDataDir "Local State"),
        (Join-Path $UserDataDir "Default\Preferences")
    )
    foreach ($file in $candidateFiles) {
        if (-not (Test-Path -LiteralPath $file)) {
            continue
        }
        try {
            $raw = Get-Content -LiteralPath $file -Raw -ErrorAction Stop
            if (-not $raw.Trim()) {
                continue
            }
            $json = $raw | ConvertFrom-Json -ErrorAction Stop
            if (-not $json.background_mode) {
                $json | Add-Member -NotePropertyName "background_mode" -NotePropertyValue ([pscustomobject]@{}) -Force
            }
            $json.background_mode | Add-Member -NotePropertyName "enabled" -NotePropertyValue $false -Force
            $tmp = "$file.uc_tmp"
            $json | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath $tmp -Encoding UTF8
            Move-Item -LiteralPath $tmp -Destination $file -Force
        } catch {
            Write-Warning "Could not disable Chrome background mode in ${file}: $($_.Exception.Message)"
        }
    }
}

function Clear-ChromeSessionRestore {
    param([string]$UserDataDir)

    # Only removes crashed-tab/session restore state. Cookies, local storage,
    # IndexedDB, extension storage, and saved logins stay in the profile.
    $sessionDirs = @(
        (Join-Path $UserDataDir "Default\Sessions"),
        (Join-Path $UserDataDir "Profile 1\Sessions")
    )
    foreach ($dir in $sessionDirs) {
        if (-not (Test-Path -LiteralPath $dir)) {
            continue
        }
        try {
            Get-ChildItem -LiteralPath $dir -Force -ErrorAction Stop |
                Where-Object { $_.Name -match '^(Session|Tabs)_' } |
                ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force -ErrorAction Stop }
        } catch {
            Write-Warning "Could not clear Chrome session restore files in ${dir}: $($_.Exception.Message)"
        }
    }
}

function Open-ControlInExistingChrome {
    param([string]$ChromePath, [string]$Url)
    Write-Host "Opening collector extension control page in the existing Chrome session: $Url"
    Start-Process -FilePath $ChromePath -ArgumentList @($Url) | Out-Null
}

function Quote-Argument {
    param([string]$Value)
    if ($Value -notmatch '[\s"]') {
        return $Value
    }
    return '"' + $Value.Replace('"', '\"') + '"'
}

$chrome = Resolve-ChromePath $ChromePath
$profile = Resolve-UserDataDir $UserDataDir
$defaultChromeProfile = "$env:LOCALAPPDATA\Google\Chrome\User Data"
if (([IO.Path]::GetFullPath($profile).TrimEnd('\')) -ieq ([IO.Path]::GetFullPath($defaultChromeProfile).TrimEnd('\'))) {
    Write-Warning "Chrome 136+ does not expose CDP for the default Chrome profile. Use a non-standard -UserDataDir such as $env:LOCALAPPDATA\UnifiedCollector\ChromeCdpProfile."
}
$extension = (Resolve-Path -LiteralPath $ExtensionPath).Path
$OpenIds = @($OpenIds | ForEach-Object { ([string]$_) -split "," } | ForEach-Object { $_.Trim() } | Where-Object { $_ })

$tabsParams = [System.Collections.Generic.List[string]]::new()
if ($FallbackOpenControlIfCleanupBlocked -and $OpenIds.Count -eq 0 -and -not $OpenAll) {
    $NoOpenAll = $true
}
if ($OpenAll -and -not $NoOpenAll -and $OpenIds.Count -eq 0) {
    $tabsParams.Add("openAll=1")
}
if ($OpenIds.Count -gt 0 -and $NoOpenAll -and $FallbackOpenControlIfCleanupBlocked) {
    $encodedIds = @($OpenIds | ForEach-Object { [uri]::EscapeDataString([string]$_) }) -join ","
    $tabsParams.Add("open=$encodedIds")
}
if (-not $NoScrape) {
    $tabsParams.Add("scrape=1")
}
if (-not $NoTest) {
    $tabsParams.Add("test=1")
}
$tabsQuery = $tabsParams -join "&"
$tabsUrlPath = "tabs.html"
if ($tabsQuery) {
    $tabsUrlPath = "${tabsUrlPath}?$tabsQuery"
}
$fallbackTabsUrl = "chrome-extension://$(Get-PrimaryKnownExtensionId)/$tabsUrlPath"

function Open-RequestedPlatformTabs {
    param([int]$Port, [string[]]$Ids, [bool]$All)
    $delayMs = 1200
    $rawDelay = [Environment]::GetEnvironmentVariable("UC_CHROME_OPEN_TAB_DELAY_MS")
    $parsedDelay = 0
    if ([int]::TryParse($rawDelay, [ref]$parsedDelay) -and $parsedDelay -ge 250) {
        $delayMs = $parsedDelay
    }
    foreach ($url in @(Get-PlatformLaunchUrls -Ids $Ids -All $All)) {
        $opened = Try-OpenCdpTarget -Port $Port -Url $url
        if ($opened) {
            Write-Host "Opened requested platform tab: $url"
        } else {
            Write-Warning "Failed to open requested platform tab via CDP: $url"
        }
        Start-Sleep -Milliseconds $delayMs
    }
}

$args = @(
    "--remote-debugging-port=$RemoteDebuggingPort",
    "--remote-debugging-address=0.0.0.0",
    "--remote-allow-origins=*",
    "--user-data-dir=$profile",
    "--enable-extensions",
    "--load-extension=$extension",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-dev-shm-usage",
    "--disable-background-mode",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
    "--js-flags=--max-old-space-size=512",
    "about:blank"
)
$rendererLimitRaw = [Environment]::GetEnvironmentVariable("UC_CHROME_RENDERER_PROCESS_LIMIT")
$rendererLimit = 0
if ([int]::TryParse($rendererLimitRaw, [ref]$rendererLimit) -and $rendererLimit -gt 0) {
    $args = @($args[0..($args.Count - 2)] + "--renderer-process-limit=$rendererLimit" + $args[-1])
}
if ($IsolateExtensions) {
    $insertAt = [Math]::Max(0, [Array]::IndexOf($args, "--load-extension=$extension"))
    $before = if ($insertAt -gt 0) { $args[0..($insertAt - 1)] } else { @() }
    $after = $args[$insertAt..($args.Count - 1)]
    $args = @($before + "--disable-extensions-except=$extension" + $after)
}
$argumentLine = ($args | ForEach-Object { Quote-Argument $_ }) -join " "

if ($DryRun) {
    Write-Host "Chrome path: $chrome"
    Write-Host "User data dir: $profile"
    Write-Host "Extension path: $extension"
    Write-Host "Dry run: skipped live Chrome/CDP probes."
    Write-Host "Arguments:"
    $args | ForEach-Object { Write-Host "  $_" }
    if ($FallbackOpenControlIfCleanupBlocked) {
        Write-Host "Fallback if hidden Chrome cleanup is blocked: open existing Chrome to $fallbackTabsUrl"
    }
    if ($CloseExistingCdpProfile) {
        Write-Host "CloseExistingCdpProfile: would close only Chrome processes using $profile"
    }
    exit 0
}

$scraperProfileProcesses = @(Get-ScraperProfileChromeProcesses -UserDataDir $profile)
if ($CloseExistingCdpProfile -and $scraperProfileProcesses.Count -gt 0) {
    Stop-CdpProfileChrome -UserDataDir $profile -Port $RemoteDebuggingPort
    Clear-ChromeSessionRestore -UserDataDir $profile
    Start-Sleep -Seconds 2
}

$chromeProcesses = @(Get-ChromeProcesses)
$visibleChromeWindows = @(Get-VisibleChromeWindows)
$cdpAlreadyUp = Test-CdpAvailable $RemoteDebuggingPort

if ($cdpAlreadyUp) {
    $controlOpened = Open-ExtensionControlPage -Port $RemoteDebuggingPort -TabsUrlPath $tabsUrlPath
    if ($OpenIds.Count -gt 0 -or ($OpenAll -and -not $NoOpenAll)) {
        Open-RequestedPlatformTabs -Port $RemoteDebuggingPort -Ids $OpenIds -All ($OpenAll -and -not $NoOpenAll)
    }
    if (-not $controlOpened) {
        Write-Warning "Chrome CDP is reachable, but the UnifiedCollector extension target was not visible."
    }
    Write-Host "Chrome CDP is already reachable on 127.0.0.1:$RemoteDebuggingPort."
    exit 0
}

if ($chromeProcesses.Count -gt 0 -and -not $AllowWhileChromeRunning -and $CloseExistingIfNoVisibleWindows) {
    $unsafeVisibleChromeWindows = @(Get-UnsafeVisibleChromeWindows -VisibleWindows $visibleChromeWindows -UserDataDir $profile)
    if ($unsafeVisibleChromeWindows.Count -gt 0) {
        Write-Error "Chrome has visible windows open; refusing automatic close. Close Chrome manually, then rerun this script."
    }
    if ($visibleChromeWindows.Count -gt 0) {
        Write-Host "Chrome is running without CDP and only UnifiedCollector control windows are visible; closing them before relaunch."
    } else {
        Write-Host "Chrome is running without CDP and has no visible windows; closing orphaned/background Chrome processes first."
    }
    try {
        Stop-ChromeProcessTree -Processes $chromeProcesses
    } catch {
        if ($FallbackOpenControlIfCleanupBlocked) {
            Write-Warning ("Could not close hidden Chrome for CDP relaunch: " + $_.Exception.Message)
            Open-ControlInExistingChrome -ChromePath $chrome -Url $fallbackTabsUrl
            Write-Warning "CDP is still unavailable, but the extension control page was nudged in the existing Chrome session."
            exit 2
        }
        throw
    }
    $chromeProcesses = @(Get-ChromeProcesses)
}

$portOwners = @(Get-PortListenerPids -Port $RemoteDebuggingPort)
if ($portOwners.Count -gt 0 -and -not $cdpAlreadyUp -and -not $AllowWhileChromeRunning -and $CloseExistingIfNoVisibleWindows) {
    $visibleChromeWindows = @(Get-VisibleChromeWindows)
    $unsafeVisibleChromeWindows = @(Get-UnsafeVisibleChromeWindows -VisibleWindows $visibleChromeWindows -UserDataDir $profile)
    if ($unsafeVisibleChromeWindows.Count -gt 0) {
        Write-Error "Port $RemoteDebuggingPort is owned by PID(s) $($portOwners -join ', '), but Chrome has visible windows open. Close Chrome manually, then rerun this script."
    }
    Write-Host "Port $RemoteDebuggingPort is still owned by stale PID(s) $($portOwners -join ', '); killing them before relaunch."
    Stop-ProcessIds -Pids $portOwners
    if (-not (Wait-PortReleased -Port $RemoteDebuggingPort -TimeoutSeconds 20)) {
        $remainingPortOwners = @(Get-PortListenerPids -Port $RemoteDebuggingPort)
        Write-Error "Port $RemoteDebuggingPort is still busy after cleanup; remaining PID(s): $($remainingPortOwners -join ', ')"
    }
    $chromeProcesses = @(Get-ChromeProcesses)
}

if ($chromeProcesses.Count -gt 0 -and -not $AllowWhileChromeRunning) {
    Write-Error (
        "Chrome is already running without CDP. Close all Chrome windows first, then rerun this script. " +
        "Starting Chrome with the same profile while it is already open usually ignores --remote-debugging-port " +
        "and can create extra windows. Use -CloseExistingIfNoVisibleWindows only when Chrome has no visible windows, " +
        "or -AllowWhileChromeRunning only for an intentional isolated/debug profile."
    )
}

Disable-ChromeBackgroundMode -UserDataDir $profile

$proc = Start-Process -FilePath $chrome -ArgumentList $argumentLine -PassThru
$deadline = (Get-Date).AddSeconds(20)
do {
    Start-Sleep -Milliseconds 500
    if (Test-CdpAvailable $RemoteDebuggingPort) {
        break
    }
} while ((Get-Date) -lt $deadline)

if (Test-CdpAvailable $RemoteDebuggingPort) {
    $controlOpened = Open-ExtensionControlPage -Port $RemoteDebuggingPort -TabsUrlPath $tabsUrlPath
    if ($OpenIds.Count -gt 0 -or ($OpenAll -and -not $NoOpenAll)) {
        Open-RequestedPlatformTabs -Port $RemoteDebuggingPort -Ids $OpenIds -All ($OpenAll -and -not $NoOpenAll)
    }
    if (-not $controlOpened) {
        Write-Warning "CDP is reachable, but no loaded UnifiedCollector extension target was found yet; reload the unpacked extension from chrome://extensions if browser heartbeats stay stale."
    }
    Write-Host "Started Chrome PID $($proc.Id); CDP is reachable on 127.0.0.1:$RemoteDebuggingPort."
    exit 0
}

Write-Error "Chrome started as PID $($proc.Id), but CDP did not become reachable on 127.0.0.1:$RemoteDebuggingPort."
