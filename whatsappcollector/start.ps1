# Startup wrapper — auto-resolves port conflicts then launches docker compose.
#
# Usage:
#   .\start.ps1                  # production stack
#   .\start.ps1 -Dev             # include docker-compose.dev.yml overlay
#   .\start.ps1 -Build           # force rebuild images
#   .\start.ps1 -Dev -Build      # both
#   .\start.ps1 -Down            # tear down the stack
#   .\start.ps1 -Logs            # tail all logs
#   .\start.ps1 -Logs collector  # tail a specific service

param(
    [switch]$Dev,
    [switch]$Build,
    [switch]$Down,
    [switch]$Logs,
    [string]$Service = ""
)

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

# ── Port conflict resolution ──────────────────────────────────────────────────
Write-Host ""
Write-Host "▶  Checking ports..." -ForegroundColor Cyan
& powershell -ExecutionPolicy Bypass -File "infrastructure\scripts\check_ports.ps1"
Write-Host ""

# ── Build compose file list ───────────────────────────────────────────────────
$ComposeFiles = @("-f", "docker-compose.yml")
if ($Dev) { $ComposeFiles += @("-f", "docker-compose.dev.yml") }

# ── Merge env files ───────────────────────────────────────────────────────────
$EnvFiles = @("--env-file", ".env")
if (Test-Path ".env.ports") { $EnvFiles += @("--env-file", ".env.ports") }

# ── Build command ─────────────────────────────────────────────────────────────
if ($Down) {
    $ComposeCmd = @("down")
} elseif ($Logs) {
    $ComposeCmd = @("logs", "-f")
    if ($Service) { $ComposeCmd += $Service }
} else {
    $ComposeCmd = @("up", "-d")
    if ($Build) { $ComposeCmd += "--build" }
}

$FullArgs = $ComposeFiles + $EnvFiles + $ComposeCmd
Write-Host "▶  Running: docker compose $($FullArgs -join ' ')" -ForegroundColor Cyan
Write-Host ""

docker compose @FullArgs
