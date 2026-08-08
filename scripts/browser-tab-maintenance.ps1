$ErrorActionPreference = "Stop"

$repo = "C:\unifiedcollector"
$tmp = Join-Path $repo "tmp"
$log = Join-Path $tmp "browser_tab_maintenance.log"
$statusPath = Join-Path $tmp "browser_tab_maintenance_status.json"
$script:LastCdpError = $null

if (-not (Test-Path $tmp)) {
    New-Item -ItemType Directory -Path $tmp | Out-Null
}

function Write-Log($message) {
    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -LiteralPath $log -Value "[$stamp] $message"
}

function Write-Status([string]$state, [string]$detail = "") {
    $payload = [ordered]@{
        checked_at = (Get-Date).ToString("o")
        state = $state
        detail = $detail
        cdp_url = "http://127.0.0.1:9222"
        audit_result = (Join-Path $tmp "browser_tab_audit_result.json")
        reload_plan = (Join-Path $tmp "browser_tab_reload_plan.json")
        pid = $PID
    }
    $payload | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $statusPath -Encoding UTF8
}

function Resolve-Python {
    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($python) { return @($python.Source) }
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) { return @($py.Source, "-3") }
    throw "python.exe/py.exe not found in PATH"
}

function Test-CdpAvailable {
    $url = "http://127.0.0.1:9222/json/version"
    try {
        $response = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 3
        $script:LastCdpError = $null
        return ($response.StatusCode -ge 200 -and $response.StatusCode -lt 300)
    } catch {
        $script:LastCdpError = $_.Exception.Message
        Write-Log ("Chrome CDP unavailable at ${url}: " + $script:LastCdpError)
        return $false
    }
}

function Get-PositiveIntEnv([string]$name, [int]$fallback) {
    $value = [Environment]::GetEnvironmentVariable($name)
    $parsed = 0
    if ([int]::TryParse($value, [ref]$parsed) -and $parsed -gt 0) {
        return $parsed
    }
    return $fallback
}

function Write-OutputLines($text) {
    if (-not $text) { return }
    $text -split "`r?`n" | Where-Object { $_ -ne "" } | ForEach-Object { Write-Log $_ }
}

function Quote-ProcessArgument($arg) {
    $text = [string]$arg
    return '"' + $text.Replace('"', '\"') + '"'
}

function Invoke-PythonScript([object[]]$command, [string]$script, [int]$timeoutSeconds = 180) {
    $parts = @($command)
    $psi = [System.Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = $parts[0]
    $psi.WorkingDirectory = $repo
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $args = @()
    if ($parts.Count -gt 1) {
        foreach ($arg in $parts[1..($parts.Count - 1)]) {
            $args += [string]$arg
        }
    }
    $args += $script
    $psi.Arguments = ($args | ForEach-Object { Quote-ProcessArgument $_ }) -join " "

    $proc = [System.Diagnostics.Process]::Start($psi)
    $stdoutTask = $proc.StandardOutput.ReadToEndAsync()
    $stderrTask = $proc.StandardError.ReadToEndAsync()
    if (-not $proc.WaitForExit($timeoutSeconds * 1000)) {
        try {
            $proc.Kill($true)
        } catch {
            try { $proc.Kill() } catch {}
        }
        Write-Log "$script timed out after ${timeoutSeconds}s and was killed"
        throw "$script timed out"
    }
    Write-OutputLines $stdoutTask.Result
    Write-OutputLines $stderrTask.Result
    if ($proc.ExitCode -ne 0) {
        throw "$script exited $($proc.ExitCode)"
    }
}

$audit = Join-Path $repo "tools\browser_tab_audit.py"
$reload = Join-Path $repo "tools\browser_tab_reload.py"

Write-Log "browser tab maintenance start"
Write-Status "running" "maintenance pass started"

Push-Location $repo
try {
    if (-not (Test-CdpAvailable)) {
        Write-Log "browser tab maintenance skipped because Chrome CDP is unavailable"
        Write-Status "cdp_unavailable" $script:LastCdpError
        return
    }
    $python = Resolve-Python
    $auditTimeout = Get-PositiveIntEnv "UC_BROWSER_AUDIT_TIMEOUT_SECONDS" 90
    $reloadTimeout = Get-PositiveIntEnv "UC_BROWSER_RELOAD_TIMEOUT_SECONDS" 90
    Write-Log ("using python command: " + ($python -join " "))
    Invoke-PythonScript -command $python -script $audit -timeoutSeconds $auditTimeout
    Invoke-PythonScript -command $python -script $reload -timeoutSeconds $reloadTimeout
    Write-Log "browser tab maintenance complete"
    Write-Status "ok" "audit and reload completed"
} catch {
    Write-Log ("browser tab maintenance failed: " + $_.Exception.Message)
    Write-Status "failed" $_.Exception.Message
    throw
} finally {
    Pop-Location
}
