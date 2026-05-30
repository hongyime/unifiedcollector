# unifiedcollector boot autostart
# Waits for Docker Desktop engine to be ready, then brings the compose stack up.
# Idempotent: safe to run repeatedly. Designed for a Scheduled Task at logon.

$ErrorActionPreference = "Stop"
$composeFile = "C:\unifiedcollector\docker\docker-compose.yml"
$logFile = "C:\unifiedcollector\scripts\boot_start.log"

function Write-Log {
    param([string]$msg)
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $logFile -Value ($ts + " " + $msg)
}

Write-Log "boot_start invoked"

# Ensure Docker Desktop is launched (AutoStart should handle it, but be safe).
$dockerProc = Get-Process "Docker Desktop" -ErrorAction SilentlyContinue
if (-not $dockerProc) {
    Write-Log "Docker Desktop not running, launching"
    Start-Process "C:\Program Files\Docker\Docker\Docker Desktop.exe"
}

# Wait up to 5 minutes for the docker engine to answer.
$ready = $false
for ($i = 0; $i -lt 60; $i++) {
    docker info 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
        $ready = $true
        break
    }
    Start-Sleep -Seconds 5
}

if (-not $ready) {
    Write-Log "Docker engine did not become ready in time, aborting"
    exit 1
}

Write-Log "Docker engine ready, bringing stack up"
docker compose -f $composeFile up -d 2>&1 | Out-Null
if ($LASTEXITCODE -eq 0) {
    Write-Log "compose up succeeded"
} else {
    Write-Log "compose up returned exit code $LASTEXITCODE"
}

# Re-sync authorized phone-named Telegram session files into the named volume.
# The sessions live on a docker named volume (not a host bind mount), so the
# authoritative authorized .session files on the host must be copied in after
# the container exists. Without this, a wiped/fresh volume leaves the collector
# with unauthorized sessions and telegram connect fails with "EOF when reading
# a line". Idempotent: docker cp overwrites.
Start-Sleep -Seconds 20
$sessions = @("6592348112", "6584731565", "6596647252", "60197282165")
foreach ($s in $sessions) {
    $src = "C:\unifiedcollector\sessions\$s.session"
    if (Test-Path $src) {
        docker cp $src ("unifiedcollector_collector:/app/sessions/" + $s + ".session") 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-Log ("synced session " + $s)
        }
    } else {
        Write-Log ("WARN authorized session missing on host: " + $src)
    }
}

# Restart collector so it picks up freshly-synced sessions.
docker restart unifiedcollector_collector 2>$null | Out-Null
Write-Log "collector restarted after session sync"

