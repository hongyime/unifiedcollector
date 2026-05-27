# Pre-build validation script to catch common errors before Docker build
# PowerShell version for Windows

[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseDeclaredVarsMoreThanAssignments', '', Justification='False-positive diagnostic in editor; script variables are intentionally assigned for command output/logging.')]
param()

# Get the directory where this script is located
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
# Navigate to workspace root (2 levels up from infrastructure/scripts/)
$WorkspaceRoot = Resolve-Path (Join-Path $ScriptDir "..\..")

Write-Host "=========================================="  -ForegroundColor Cyan
Write-Host "Pre-Build Validation Script" -ForegroundColor Cyan
Write-Host "=========================================="  -ForegroundColor Cyan
Write-Host ""
Write-Host "Workspace root: $WorkspaceRoot"
Write-Host ""

# Change to workspace root
Set-Location $WorkspaceRoot

# Force UTF-8 so validator output with Unicode symbols does not fail on cp1252 consoles
$env:PYTHONUTF8 = "1"

$ErrorCount = 0

# Check if Python is available
Write-Host "Checking Python installation..."
try {
    $pythonVersion = python --version 2>&1
    Write-Host "✅ Python found: $pythonVersion" -ForegroundColor Green
} catch {
    Write-Host "❌ Python is not installed or not in PATH" -ForegroundColor Red
    $ErrorCount++
}

# Check if pip is available
Write-Host "Checking pip installation..."
try {
    $pipVersion = pip --version 2>&1
    Write-Host "✅ pip found: $pipVersion" -ForegroundColor Green
} catch {
    Write-Host "❌ pip is not installed or not in PATH" -ForegroundColor Red
    $ErrorCount++
}

# Validate processor-py requirements
Write-Host ""
Write-Host "Validating processor-py requirements.txt..."
if (Test-Path "services\processor-py\requirements.txt") {
    try {
        python services\processor-py\scripts\validate_requirements.py services\processor-py\requirements.txt | Out-Null
        if ($LASTEXITCODE -ne 0) {
            $ErrorCount++
        }
    } catch {
        Write-Host "⚠️  Warning: Could not validate requirements" -ForegroundColor Yellow
    }
} else {
    Write-Host "❌ requirements.txt not found" -ForegroundColor Red
    $ErrorCount++
}

# Check Docker
Write-Host ""
Write-Host "Checking Docker installation..."
try {
    $dockerVersion = docker --version 2>&1
    Write-Host "✅ Docker found: $dockerVersion" -ForegroundColor Green
} catch {
    Write-Host "❌ Docker is not installed or not in PATH" -ForegroundColor Red
    $ErrorCount++
}

# Check Docker Compose
Write-Host ""
Write-Host "Checking Docker Compose..."
try {
    $composeVersion = docker compose version 2>&1
    Write-Host "✅ Docker Compose found: $composeVersion" -ForegroundColor Green
} catch {
    Write-Host "❌ Docker Compose is not available" -ForegroundColor Red
    $ErrorCount++
}

# Check for common file issues
Write-Host ""
Write-Host "Checking for common issues..."

# Check if docker-compose files exist
$composeFiles = @("docker-compose.yml", "docker-compose.dev.yml")
foreach ($file in $composeFiles) {
    if (Test-Path $file) {
        Write-Host "✅ Found $file" -ForegroundColor Green
    } else {
        Write-Host "⚠️  Warning: $file not found" -ForegroundColor Yellow
    }
}

# Check for secret rotation hygiene
Write-Host ""
Write-Host "Checking secrets rotation hygiene..."

function Get-EnvValue {
    param([string]$Path, [string]$Key)
    if (!(Test-Path $Path)) { return "" }
    $line = Get-Content $Path | Where-Object { $_ -match "^$Key=" } | Select-Object -Last 1
    if (-not $line) { return "" }
    return ($line -replace "^$Key=", "").Trim()
}

function Test-WeakSecret {
    param([string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) { return $true }
    $weak = @(
        'password', 'changeme', 'guest', 'wac_pass', 'wac_redis_pass',
        'whatsappcollector_cookie', '091128'
    )
    if ($Value.StartsWith('CHANGE_ME_')) { return $true }
    return $weak -contains $Value
}

if (!(Test-Path ".env")) {
    Write-Host "❌ .env not found. Copy from .env.template and set rotated secrets." -ForegroundColor Red
    $ErrorCount++
} else {
    $secretKeys = @(
        'POSTGRES_PASSWORD',
        'REDIS_PASSWORD',
        'RABBITMQ_PASSWORD',
        'RABBITMQ_ERLANG_COOKIE'
    )

    foreach ($key in $secretKeys) {
        $value = Get-EnvValue -Path ".env" -Key $key
        if (Test-WeakSecret -Value $value) {
            Write-Host "❌ $key is unset or uses a weak/default value. Rotate it in .env." -ForegroundColor Red
            $ErrorCount++
        }
    }

    $bridgeSecret = Get-EnvValue -Path ".env" -Key "MEDIA_BRIDGE_SECRET"
    if ((Test-WeakSecret -Value $bridgeSecret) -or $bridgeSecret.Length -lt 32) {
        Write-Host "❌ MEDIA_BRIDGE_SECRET must be rotated and at least 32 characters." -ForegroundColor Red
        $ErrorCount++
    } else {
        Write-Host "✅ MEDIA_BRIDGE_SECRET length and rotation check passed" -ForegroundColor Green
    }
}

# Verify historical secret-bearing env blobs are purged
Write-Host ""
Write-Host "Checking git history for .env/.env.example blobs..."
try {
    $localBlobCount = (git rev-list HEAD -- .env .env.example | Measure-Object -Line).Lines
    if ($localBlobCount -gt 0) {
        Write-Host "❌ Found $localBlobCount historical env blob commit(s) on current branch. Run secret-history purge before release." -ForegroundColor Red
        $ErrorCount++
    } else {
        Write-Host "✅ No historical .env/.env.example blobs found on current branch history" -ForegroundColor Green
    }

    $remoteRefCount = (git for-each-ref refs/remotes --format="%(refname)" | Measure-Object -Line).Lines
    if ($remoteRefCount -gt 0) {
        $remoteBlobCount = (git rev-list --remotes -- .env .env.example | Measure-Object -Line).Lines
        if ($remoteBlobCount -gt 0) {
            Write-Host "⚠️  Remote-tracking refs still contain $remoteBlobCount env-blob commit(s). Coordinate remote history cleanup separately." -ForegroundColor Yellow
        }
    }
} catch {
    Write-Host "⚠️  Warning: git not available; skipping history hygiene check" -ForegroundColor Yellow
}

# Summary
Write-Host ""
Write-Host "==========================================" -ForegroundColor Cyan
if ($ErrorCount -eq 0) {
    Write-Host "✅ All pre-build checks passed!" -ForegroundColor Green
    Write-Host "You can now safely run: docker compose build" -ForegroundColor Green
    exit 0
} else {
    Write-Host "❌ Found $ErrorCount error(s). Please fix them before building." -ForegroundColor Red
    exit 1
}
