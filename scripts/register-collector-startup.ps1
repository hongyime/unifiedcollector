param(
    [string]$Repo = "C:\unifiedcollector",
    [string[]]$TelegramSessions = @("6592348112", "6584731565", "6596647252", "60197282165"),
    [int]$BrowserMaintenanceIntervalMinutes = 10
)

$ErrorActionPreference = "Stop"

$repoPath = (Resolve-Path -LiteralPath $Repo).Path
$composePath = Join-Path $repoPath "docker\docker-compose.yml"
if (-not (Test-Path -LiteralPath $composePath)) {
    throw "Missing compose file: $composePath"
}

$startup = [Environment]::GetFolderPath("Startup")
if (-not $startup) {
    throw "Could not resolve current-user Startup folder."
}

$sessionList = ($TelegramSessions | ForEach-Object { $_.Trim() } | Where-Object { $_ }) -join " "
$cmdPath = Join-Path $startup "UnifiedCollector_Startup.bat"
$lines = @(
    "@echo off",
    "REM UnifiedCollector startup script. Installed by scripts\register-collector-startup.ps1.",
    "REM Idempotent: safe to run at every Windows login after Docker Desktop starts.",
    "setlocal EnableExtensions",
    "set LOGFILE=$repoPath\scripts\boot_start.log",
    "set COMPOSE=$composePath",
    "set BROWSER_MAINTENANCE=$repoPath\scripts\start-browser-maintenance-loop.ps1",
    "echo [%date% %time%] UnifiedCollector startup triggered >> %LOGFILE%",
    "",
    "set RETRIES=60",
    ":WAIT_LOOP",
    "docker info >nul 2>&1",
    "if %errorlevel% == 0 goto DOCKER_READY",
    "set /a RETRIES=%RETRIES%-1",
    "if %RETRIES% == 0 goto DOCKER_TIMEOUT",
    "echo [%date% %time%] Waiting for Docker... (%RETRIES% retries left) >> %LOGFILE%",
    "powershell.exe -NoProfile -Command `"Start-Sleep -Seconds 10`" >nul 2>&1",
    "goto WAIT_LOOP",
    "",
    ":DOCKER_TIMEOUT",
    "echo [%date% %time%] ERROR: Docker not ready after 600s >> %LOGFILE%",
    "exit /b 1",
    "",
    ":DOCKER_READY",
    "echo [%date% %time%] Docker ready. Waiting for internet... >> %LOGFILE%",
    "",
    ":WAIT_INTERNET",
    "ping -n 1 -w 2000 8.8.8.8 >nul 2>&1",
    "if %errorlevel% == 0 goto INTERNET_READY",
    "echo [%date% %time%] No internet, retrying in 10s... >> %LOGFILE%",
    "powershell.exe -NoProfile -Command `"Start-Sleep -Seconds 10`" >nul 2>&1",
    "goto WAIT_INTERNET",
    "",
    ":INTERNET_READY",
    "echo [%date% %time%] Internet available. Starting containers... >> %LOGFILE%",
    "cd /d $repoPath",
    "docker compose -f %COMPOSE% up -d >> %LOGFILE% 2>&1",
    "echo [%date% %time%] docker compose up -d done (exit %errorlevel%) >> %LOGFILE%",
    "if exist `"%BROWSER_MAINTENANCE%`" (",
    "    echo [%date% %time%] Starting browser maintenance loop... >> %LOGFILE%",
    "    start `"UnifiedCollectorBrowserMaintenance`" /min powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"%BROWSER_MAINTENANCE%`" -IntervalMinutes $BrowserMaintenanceIntervalMinutes -InitialDelaySeconds 15",
    ") else (",
    "    echo [%date% %time%] WARN browser maintenance launcher missing: %BROWSER_MAINTENANCE% >> %LOGFILE%",
    ")",
    "",
    "powershell.exe -NoProfile -Command `"Start-Sleep -Seconds 20`" >nul 2>&1",
    "echo [%date% %time%] Syncing authorized Telegram sessions into shared sessions volume... >> %LOGFILE%",
    "set SESSION_TARGET=unifiedcollector_collector_telegram",
    "docker inspect %SESSION_TARGET% >nul 2>&1",
    "if not %errorlevel% == 0 set SESSION_TARGET=unifiedcollector_collector",
    "for %%S in ($sessionList) do (",
    "    if exist `"$repoPath\sessions\%%S.session`" (",
    "        docker cp `"$repoPath\sessions\%%S.session`" `"%SESSION_TARGET%:/app/sessions/%%S.session`" >> %LOGFILE% 2>&1",
    "        echo [%date% %time%] synced session %%S via %SESSION_TARGET% >> %LOGFILE%",
    "    ) else (",
    "        echo [%date% %time%] WARN authorized session missing on host: %%S >> %LOGFILE%",
    "    )",
    ")",
    "",
    "REM Telegram reads session files during startup; restart it after session sync.",
    "docker restart unifiedcollector_collector_telegram >> %LOGFILE% 2>&1",
    "echo [%date% %time%] collector_telegram restarted after session sync >> %LOGFILE%",
    "docker restart unifiedcollector_collector >> %LOGFILE% 2>&1",
    "echo [%date% %time%] collector restarted after session sync >> %LOGFILE%",
    "echo [%date% %time%] Startup complete. >> %LOGFILE%",
    "exit /b 0"
)

Set-Content -LiteralPath $cmdPath -Value $lines -Encoding ASCII
Write-Host "Installed current-user startup launcher: $cmdPath"
