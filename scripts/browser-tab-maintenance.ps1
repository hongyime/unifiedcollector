$ErrorActionPreference = "Stop"

$repo = "C:\unifiedcollector"
$tmp = Join-Path $repo "tmp"
$log = Join-Path $tmp "browser_tab_maintenance.log"

if (-not (Test-Path $tmp)) {
    New-Item -ItemType Directory -Path $tmp | Out-Null
}

function Write-Log($message) {
    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -LiteralPath $log -Value "[$stamp] $message"
}

function Resolve-Python {
    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($python) { return @($python.Source) }
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) { return @($py.Source, "-3") }
    throw "python.exe/py.exe not found in PATH"
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

Push-Location $repo
try {
    $python = Resolve-Python
    Write-Log ("using python command: " + ($python -join " "))
    Invoke-PythonScript -command $python -script $audit -timeoutSeconds 180
    Invoke-PythonScript -command $python -script $reload -timeoutSeconds 120
    Write-Log "browser tab maintenance complete"
} catch {
    Write-Log ("browser tab maintenance failed: " + $_.Exception.Message)
    throw
} finally {
    Pop-Location
}
