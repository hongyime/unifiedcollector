<#
.SYNOPSIS
    Register the browser_cookie_vault daemon as a Windows Scheduled Task
    (fallback path when the Docker service cannot reach Chrome on :9333).

.DESCRIPTION
    UnifiedCollector's cookie vault normally runs as the
    `browser_cookie_vault` service in docker-compose.yml. In some Windows
    setups (e.g. Docker Desktop networking wedged, corp firewall dropping
    host-gateway traffic) the container cannot reach the host Chrome on
    localhost:9333. This script installs the exact same daemon on the host
    as a Scheduled Task, so cookie snapshots keep flowing without Docker in
    the loop.

    The task runs `python -m src.tools.browser_cookie_vault` under the
    current user's context at logon, with the working directory set to the
    repo root, and restarts on failure. Output is logged to
    `logs\browser_cookie_vault.log` next to the repo.

.PARAMETER RepoRoot
    Absolute path to the unifiedcollector repo root. Defaults to the parent
    of this script's directory.

.PARAMETER PythonExe
    Python executable to invoke. Defaults to whichever `python` is on PATH.

.PARAMETER TaskName
    Name of the Scheduled Task. Defaults to `UnifiedCollectorBrowserCookieVault`.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\register-cookie-vault-task.ps1

.EXAMPLE
    .\scripts\register-cookie-vault-task.ps1 -PythonExe "C:\Python312\python.exe"
#>
[CmdletBinding()]
param(
    [string]$RepoRoot = "",
    [string]$PythonExe = "python",
    [string]$TaskName = "UnifiedCollectorBrowserCookieVault"
)

$ErrorActionPreference = "Stop"

if (-not $RepoRoot) {
    $scriptRoot = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
    if ($scriptRoot) {
        $RepoRoot = Split-Path -Parent $scriptRoot
    }
}

if (-not $RepoRoot) {
    throw "RepoRoot could not be resolved; pass -RepoRoot C:\unifiedcollector"
}

if (-not (Test-Path (Join-Path $RepoRoot "src\tools\browser_cookie_vault.py"))) {
    throw "Repo root '$RepoRoot' does not contain src\tools\browser_cookie_vault.py"
}

$logDir = Join-Path $RepoRoot "logs"
if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
}
$logFile = Join-Path $logDir "browser_cookie_vault.log"

# Env vars the daemon needs on the host: it talks directly to localhost:9333
# and writes to credentials\browser_cookies under the repo.
$backupDir = Join-Path $RepoRoot "credentials\browser_cookies"
if (-not (Test-Path $backupDir)) {
    New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
}

# Wrap in cmd /c so we can capture stdout+stderr to the log; PowerShell's
# native redirection doesn't survive detached Scheduled Task contexts well.
$argString = @(
    "/c",
    "set CHROME_CDP_URL=http://localhost:9333",
    "&& set BROWSER_COOKIE_VAULT_DIR=$backupDir",
    "&& set BROWSER_COOKIE_VAULT_HEALTH_PORT=8791",
    "&& set BROWSER_COOKIE_VAULT_INTERVAL_SECONDS=300",
    "&& set PYTHONPATH=$RepoRoot",
    "&& `"$PythonExe`" -m src.tools.browser_cookie_vault >> `"$logFile`" 2>&1"
) -join " "

$action = New-ScheduledTaskAction `
    -Execute "cmd.exe" `
    -Argument $argString `
    -WorkingDirectory $RepoRoot

$trigger = New-ScheduledTaskTrigger -AtLogOn
# Retry every minute if the process exits (Chrome briefly missing, etc.).
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 0) `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Highest

# Idempotent register/replace.
try {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Write-Host "Removing existing task '$TaskName' before re-registering."
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Description "UnifiedCollector browser_cookie_vault daemon (Chrome CDP cookie backup)." | Out-Null

    Write-Host "Registered Scheduled Task '$TaskName'."
    Write-Host "  Log:        $logFile"
    Write-Host "  Backup dir: $backupDir"
    Write-Host ""
    Write-Host "Start it now with:"
    Write-Host "  Start-ScheduledTask -TaskName $TaskName"
    Write-Host ""
    Write-Host "Verify with:"
    Write-Host "  curl http://localhost:8791/health"
} catch {
    $message = $_.Exception.Message
    if ($message -notmatch "Access is denied|0x80070005") {
        throw
    }
    $startup = [Environment]::GetFolderPath("Startup")
    if (-not $startup) {
        throw "Scheduled task registration was denied and the user Startup folder could not be resolved."
    }
    $cmdPath = Join-Path $startup "$TaskName.cmd"
    $cmd = @(
        "@echo off",
        "cd /d `"$RepoRoot`"",
        "set CHROME_CDP_URL=http://localhost:9333",
        "set BROWSER_COOKIE_VAULT_DIR=$backupDir",
        "set BROWSER_COOKIE_VAULT_HEALTH_PORT=8791",
        "set BROWSER_COOKIE_VAULT_INTERVAL_SECONDS=300",
        "set PYTHONPATH=$RepoRoot",
        "`"$PythonExe`" -m src.tools.browser_cookie_vault >> `"$logFile`" 2>&1"
    ) -join "`r`n"
    Set-Content -LiteralPath $cmdPath -Value $cmd -Encoding ASCII
    Write-Warning "Scheduled task registration was denied; installed current-user Startup fallback instead."
    Write-Host "Startup fallback: $cmdPath"
    Write-Host "Host fallback health port: http://localhost:8791/health"
}
