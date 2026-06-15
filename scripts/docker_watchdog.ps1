# docker_watchdog.ps1 - keep Docker Desktop (and the collector stack) alive.
#
# This machine is prone to restarts/updates/crashes. `restart: unless-stopped`
# brings containers back once the Docker engine is up, and Docker Desktop's
# autoStart handles a login after reboot -- but a *mid-session Docker Desktop
# crash* has no recovery (autoStart only fires at login). This watchdog fills
# that gap: run it on a short schedule; if the Docker engine API is down, it
# (re)launches Docker Desktop. The unless-stopped containers then resume.
#
# Safe + quiet: logs to backups\docker_watchdog.log, no console window when run
# via run_hidden.vbs (see register-docker-watchdog.ps1). Does nothing if the
# engine is already responsive.
$ErrorActionPreference = "SilentlyContinue"
$log = "C:\unifiedcollector\backups\docker_watchdog.log"
function Log($m) { "$([DateTime]::Now.ToString('yyyy-MM-dd HH:mm:ss')) $m" | Out-File -FilePath $log -Append -Encoding utf8 }

# Is the engine responding? (cheap, ~2s)
$null = & docker version --format '{{if .Server}}1{{end}}' 2>$null
if ($LASTEXITCODE -eq 0) { exit 0 }   # engine up -> nothing to do

# Engine down. Only act if Docker Desktop isn't already starting up (give a
# crash-loop room to not thrash): relaunch the app.
$dd = Get-Process "Docker Desktop" -ErrorAction SilentlyContinue
if (-not $dd) {
    Log "engine DOWN + Docker Desktop not running -> relaunching"
    Start-Process "C:\Program Files\Docker\Docker\Docker Desktop.exe"
} else {
    Log "engine DOWN but Docker Desktop process exists (pid $($dd.Id -join ',')) -> waiting for it to come up"
}
exit 0
