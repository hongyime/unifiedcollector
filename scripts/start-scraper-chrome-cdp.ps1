param(
    [string]$ChromePath = "",
    [string]$UserDataDir = "",
    [string]$ExtensionPath = "C:\unifiedcollector\extension",
    [int]$RemoteDebuggingPort = 9222,
    [switch]$AllowWhileChromeRunning,
    [switch]$CloseExistingIfNoVisibleWindows,
    [switch]$FallbackOpenControlIfCleanupBlocked,
    [string[]]$OpenIds = @(),
    [switch]$OpenAll,
    [switch]$NoOpenAll,
    [switch]$NoScrape,
    [switch]$NoTest,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

function Resolve-ChromePath {
    param([string]$Requested)
    if ($Requested -and (Test-Path -LiteralPath $Requested)) {
        return (Resolve-Path -LiteralPath $Requested).Path
    }
    $candidates = @(
        "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
        "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
        "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
    )
    foreach ($candidate in $candidates) {
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
    # Chrome profile directory. Use a durable, non-standard scraper profile so
    # CDP stays available and scraper logins persist across restarts.
    return "$env:LOCALAPPDATA\UnifiedCollector\ChromeCdpProfile"
}

function Test-CdpAvailable {
    param([int]$Port)
    try {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/json/version" -UseBasicParsing -TimeoutSec 3
        return ($response.StatusCode -ge 200 -and $response.StatusCode -lt 300)
    } catch {
        return $false
    }
}

function Get-ExtensionIdFromCdp {
    param([int]$Port)
    try {
        $targets = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/json/list" -Method Get -TimeoutSec 5
        foreach ($target in @($targets)) {
            $url = [string]($target.url)
            $targetType = [string]($target.type)
            if ($targetType -notin @("service_worker", "background_page")) {
                continue
            }
            $match = [regex]::Match($url, '^chrome-extension://([a-p]{32})/')
            if ($match.Success) {
                return $match.Groups[1].Value
            }
        }
        foreach ($target in @($targets)) {
            $url = [string]($target.url)
            $match = [regex]::Match($url, '^chrome-extension://([a-p]{32})/')
            if ($match.Success) {
                return $match.Groups[1].Value
            }
        }
    } catch {
        return ""
    }
    return ""
}

function Open-CdpTarget {
    param([int]$Port, [string]$Url)
    $encoded = [uri]::EscapeDataString($Url)
    Invoke-WebRequest -Uri "http://127.0.0.1:$Port/json/new?$encoded" -Method Put -UseBasicParsing -TimeoutSec 10 | Out-Null
}

function Get-PlatformLaunchUrls {
    param([string[]]$Ids, [bool]$All)
    $platforms = [ordered]@{
        instagram = "https://www.instagram.com/"
        tiktok = "https://www.tiktok.com/following"
        lemon8 = "https://www.lemon8-app.com/topic/food?region=sg"
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
            $platforms[$id]
        }
    }
}

function Get-ChromeProcesses {
    $liveIds = @{}
    foreach ($proc in @(Get-Process chrome -ErrorAction SilentlyContinue)) {
        $liveIds[[int]$proc.Id] = $true
    }
    return @(Get-CimInstance Win32_Process -Filter "name='chrome.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $liveIds.ContainsKey([int]$_.ProcessId) })
}

function Get-VisibleChromeWindows {
    return @(Get-Process chrome -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 })
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
    $taskkill = Start-Process -FilePath "$env:SystemRoot\System32\taskkill.exe" -ArgumentList "/IM chrome.exe /F /T" -Wait -PassThru -NoNewWindow
    Start-Sleep -Seconds 2
    $remaining = @(Get-ChromeProcesses)
    if ($remaining.Count -gt 0) {
        $ids = ($remaining | Select-Object -ExpandProperty ProcessId) -join ", "
        throw "Chrome is still running after repair attempt; remaining PIDs: $ids"
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
$chromeProcesses = @(Get-ChromeProcesses)
$visibleChromeWindows = @(Get-VisibleChromeWindows)
$cdpAlreadyUp = Test-CdpAvailable $RemoteDebuggingPort
$OpenIds = @($OpenIds | ForEach-Object { ([string]$_) -split "," } | ForEach-Object { $_.Trim() } | Where-Object { $_ })

$tabsParams = [System.Collections.Generic.List[string]]::new()
if ($FallbackOpenControlIfCleanupBlocked -and $OpenIds.Count -eq 0 -and -not $OpenAll) {
    $NoOpenAll = $true
}
if ($OpenAll -and -not $NoOpenAll -and $OpenIds.Count -eq 0) {
    $tabsParams.Add("openAll=1")
}
if ($OpenIds.Count -gt 0) {
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
$fallbackTabsUrl = "chrome-extension://pkmdmcklnjdeocoeigmlakhomhhcpafb/$tabsUrlPath"

if ($cdpAlreadyUp) {
    $extensionId = Get-ExtensionIdFromCdp $RemoteDebuggingPort
    if ($extensionId) {
        $tabsUrl = "chrome-extension://$extensionId/$tabsUrlPath"
        Open-CdpTarget -Port $RemoteDebuggingPort -Url $tabsUrl
        Write-Host "Opened extension control page: $tabsUrl"
    }
    foreach ($url in @(Get-PlatformLaunchUrls -Ids $OpenIds -All ($OpenAll -and -not $NoOpenAll))) {
        Open-CdpTarget -Port $RemoteDebuggingPort -Url $url
        Start-Sleep -Milliseconds 500
    }
    Write-Host "Chrome CDP is already reachable on 127.0.0.1:$RemoteDebuggingPort."
    exit 0
}
$args = @(
    "--remote-debugging-port=$RemoteDebuggingPort",
    "--remote-debugging-address=127.0.0.1",
    "--remote-allow-origins=http://127.0.0.1:$RemoteDebuggingPort",
    "--user-data-dir=$profile",
    "--load-extension=$extension",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-dev-shm-usage",
    "--disable-background-mode",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
    "--js-flags=--max-old-space-size=512",
    "--renderer-process-limit=10",
    "about:blank"
)
$argumentLine = ($args | ForEach-Object { Quote-Argument $_ }) -join " "

if ($DryRun) {
    $runningNote = if ($chromeProcesses.Count -gt 0) { "Chrome is currently running; normal launch would refuse until it is closed." } else { "Chrome is not currently running." }
    Write-Host "Chrome path: $chrome"
    Write-Host "User data dir: $profile"
    Write-Host "Extension path: $extension"
    Write-Host "Visible Chrome windows: $($visibleChromeWindows.Count)"
    Write-Host $runningNote
    Write-Host "Arguments:"
    $args | ForEach-Object { Write-Host "  $_" }
    if ($FallbackOpenControlIfCleanupBlocked) {
        Write-Host "Fallback if hidden Chrome cleanup is blocked: open existing Chrome to $tabsUrl"
    }
    exit 0
}

if ($chromeProcesses.Count -gt 0 -and -not $AllowWhileChromeRunning -and $CloseExistingIfNoVisibleWindows) {
    if ($visibleChromeWindows.Count -gt 0) {
        Write-Error "Chrome has visible windows open; refusing automatic close. Close Chrome manually, then rerun this script."
    }
    Write-Host "Chrome is running without CDP and has no visible windows; closing orphaned/background Chrome processes first."
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
Start-Sleep -Seconds 4

if (Test-CdpAvailable $RemoteDebuggingPort) {
    $extensionId = Get-ExtensionIdFromCdp $RemoteDebuggingPort
    if ($extensionId) {
        $tabsUrl = "chrome-extension://$extensionId/$tabsUrlPath"
        Open-CdpTarget -Port $RemoteDebuggingPort -Url $tabsUrl
        Write-Host "Opened extension control page: $tabsUrl"
    } else {
        Write-Warning "CDP is reachable, but no loaded extension target was found yet; open the UnifiedCollector extension options page manually."
    }
    foreach ($url in @(Get-PlatformLaunchUrls -Ids $OpenIds -All ($OpenAll -and -not $NoOpenAll))) {
        Open-CdpTarget -Port $RemoteDebuggingPort -Url $url
        Start-Sleep -Milliseconds 500
    }
    Write-Host "Started Chrome PID $($proc.Id); CDP is reachable on 127.0.0.1:$RemoteDebuggingPort."
    exit 0
}

Write-Error "Chrome started as PID $($proc.Id), but CDP did not become reachable on 127.0.0.1:$RemoteDebuggingPort."
