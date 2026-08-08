param(
    [string]$ChromePath = "",
    [string]$UserDataDir = "",
    [string]$ExtensionPath = "C:\unifiedcollector\extension",
    [int]$RemoteDebuggingPort = 9222,
    [switch]$AllowWhileChromeRunning,
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
$chromeProcesses = @(Get-CimInstance Win32_Process -Filter "name='chrome.exe'" -ErrorAction SilentlyContinue)
$cdpAlreadyUp = Test-CdpAvailable $RemoteDebuggingPort

if ($cdpAlreadyUp) {
    Write-Host "Chrome CDP is already reachable on 127.0.0.1:$RemoteDebuggingPort."
    exit 0
}

$tabsUrl = "chrome-extension://pkmdmcklnjdeocoeigmlakhomhhcpafb/tabs.html?openAll=1&scrape=1&test=1"
$args = @(
    "--remote-debugging-port=$RemoteDebuggingPort",
    "--user-data-dir=$profile",
    "--load-extension=$extension",
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
    Write-Host $runningNote
    Write-Host "Arguments:"
    $args | ForEach-Object { Write-Host "  $_" }
    exit 0
}

if ($chromeProcesses.Count -gt 0 -and -not $AllowWhileChromeRunning) {
    Write-Error (
        "Chrome is already running without CDP. Close all Chrome windows first, then rerun this script. " +
        "Starting Chrome with the same profile while it is already open usually ignores --remote-debugging-port " +
        "and can create extra windows. Use -AllowWhileChromeRunning only for an intentional isolated/debug profile."
    )
}

$proc = Start-Process -FilePath $chrome -ArgumentList $argumentLine -PassThru
Start-Sleep -Seconds 4

if (Test-CdpAvailable $RemoteDebuggingPort) {
    Write-Host "Started Chrome PID $($proc.Id); CDP is reachable on 127.0.0.1:$RemoteDebuggingPort."
    exit 0
}

Write-Error "Chrome started as PID $($proc.Id), but CDP did not become reachable on 127.0.0.1:$RemoteDebuggingPort."
