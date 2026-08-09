param(
    [string]$ChromePath = "",
    [string]$UserDataDir = "",
    [string]$ExtensionPath = "C:\unifiedcollector\extension",
    [int]$RemoteDebuggingPort = 9222,
    [switch]$AllowWhileChromeRunning,
    [switch]$CloseExistingIfNoVisibleWindows,
    [string[]]$OpenIds = @(),
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
    return "$env:LOCALAPPDATA\Google\Chrome\User Data"
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

function Get-ChromeProcesses {
    return @(Get-CimInstance Win32_Process -Filter "name='chrome.exe'" -ErrorAction SilentlyContinue)
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

function Quote-Argument {
    param([string]$Value)
    if ($Value -notmatch '[\s"]') {
        return $Value
    }
    return '"' + $Value.Replace('"', '\"') + '"'
}

$chrome = Resolve-ChromePath $ChromePath
$profile = Resolve-UserDataDir $UserDataDir
$extension = (Resolve-Path -LiteralPath $ExtensionPath).Path
$chromeProcesses = @(Get-ChromeProcesses)
$visibleChromeWindows = @(Get-VisibleChromeWindows)
$cdpAlreadyUp = Test-CdpAvailable $RemoteDebuggingPort

if ($cdpAlreadyUp) {
    Write-Host "Chrome CDP is already reachable on 127.0.0.1:$RemoteDebuggingPort."
    exit 0
}

$tabsParams = [System.Collections.Generic.List[string]]::new()
if (-not $NoOpenAll -and $OpenIds.Count -eq 0) {
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
$tabsUrl = "chrome-extension://pkmdmcklnjdeocoeigmlakhomhhcpafb/tabs.html"
if ($tabsQuery) {
    $tabsUrl = "$tabsUrl?$tabsQuery"
}
$args = @(
    "--remote-debugging-port=$RemoteDebuggingPort",
    "--user-data-dir=$profile",
    "--load-extension=$extension",
    "--disable-dev-shm-usage",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
    "--js-flags=--max-old-space-size=512",
    "--renderer-process-limit=10",
    $tabsUrl
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
    exit 0
}

if ($chromeProcesses.Count -gt 0 -and -not $AllowWhileChromeRunning -and $CloseExistingIfNoVisibleWindows) {
    if ($visibleChromeWindows.Count -gt 0) {
        Write-Error "Chrome has visible windows open; refusing automatic close. Close Chrome manually, then rerun this script."
    }
    Write-Host "Chrome is running without CDP and has no visible windows; closing orphaned/background Chrome processes first."
    Stop-ChromeProcessTree -Processes $chromeProcesses
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

$proc = Start-Process -FilePath $chrome -ArgumentList $argumentLine -PassThru
Start-Sleep -Seconds 4

if (Test-CdpAvailable $RemoteDebuggingPort) {
    Write-Host "Started Chrome PID $($proc.Id); CDP is reachable on 127.0.0.1:$RemoteDebuggingPort."
    exit 0
}

Write-Error "Chrome started as PID $($proc.Id), but CDP did not become reachable on 127.0.0.1:$RemoteDebuggingPort."
