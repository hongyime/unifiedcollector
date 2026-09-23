"""Execute recovery boundaries in isolation; never launch the managed browser."""

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
PWSH = shutil.which("pwsh.exe")
pytestmark = pytest.mark.skipif(os.name != "nt" or not PWSH, reason="Windows PowerShell QA")


def _ps_literal(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _run_owned(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Suspend before assigning a Job: cleanup includes WScript's hidden children."""
    class JobAccounting(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64),
            ("ThisPeriodTotalUserTime", ctypes.c_int64), ("ThisPeriodTotalKernelTime", ctypes.c_int64),
            ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateJobObject.restype = wintypes.BOOL
    kernel.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
    ]
    kernel.QueryInformationJobObject.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    ntdll = ctypes.WinDLL("ntdll")
    ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    ntdll.NtResumeProcess.restype = wintypes.LONG
    job = kernel.CreateJobObjectW(None, None)
    assert job, ctypes.WinError(ctypes.get_last_error())
    process = None
    try:
        process = subprocess.Popen(
            command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, creationflags=0x00000004 | subprocess.CREATE_NO_WINDOW,
        )
        assert kernel.AssignProcessToJobObject(job, int(process._handle)), ctypes.WinError(ctypes.get_last_error())
        assert ntdll.NtResumeProcess(int(process._handle)) == 0
        stdout, stderr = process.communicate(timeout=40)
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    finally:
        # This job owns only this test's suspended root and descendants.
        assert kernel.TerminateJobObject(job, 99), ctypes.WinError(ctypes.get_last_error())
        if process is not None:
            if process.poll() is None:
                process.kill()  # Also covers a failed job assignment.
            process.communicate(timeout=10)
        try:
            accounting = JobAccounting()
            deadline = time.monotonic() + 10
            while True:
                assert kernel.QueryInformationJobObject(job, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None)
                if accounting.ActiveProcesses == 0:
                    break
                assert time.monotonic() < deadline, "Owned child processes survived job termination"
                time.sleep(0.05)
            print(f"cleanup: job tracked {accounting.TotalProcesses} processes; active=0; root reaped")
        finally:
            kernel.CloseHandle(job)


def _run_ps(script: Path) -> subprocess.CompletedProcess[str]:
    return _run_owned([PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)], script.parent)


@pytest.mark.parametrize(
    ("registrar", "target", "expected_args"),
    [
        ("register-browser-autorecover-task.ps1", "browser-autorecover.ps1",
         ["-Loop", "-IntervalMinutes", "7", "-CdpPort", "9447"]),
        ("register-browser-maintenance-task.ps1", "start-browser-maintenance-loop.ps1",
         ["-IntervalMinutes", "7", "-InitialDelaySeconds", "60"]),
    ],
)
def test_startup_command_roundtrip(tmp_path: Path, registrar: str, target: str, expected_args: list[str]) -> None:
    # Both repo/target and the selected installed PowerShell executable contain spaces.
    assert " " in PWSH, "This regression requires a PowerShell installation path with spaces"
    repo = tmp_path / "inert repo with spaces"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    startup = tmp_path / "inert Startup"
    startup.mkdir()
    record = repo / "record.json"
    (scripts / target).write_text(
        "@{ args = @($args); commandLine = @([Environment]::GetCommandLineArgs()); "
        "exe = (Get-Process -Id $PID).Path; policy = [string](Get-ExecutionPolicy) } | "
        f"ConvertTo-Json -Depth 5 | Set-Content -LiteralPath {_ps_literal(record)}\nexit 37\n",
        encoding="utf-8",
    )
    shutil.copyfile(REPO_ROOT / "scripts" / "run_hidden.vbs", scripts / "run_hidden.vbs")
    source = (REPO_ROOT / "scripts" / registrar).read_text(encoding="utf-8")
    # Redirect the two hardcoded location seams BEFORE executing the registrar.
    repo_seam = '$repo = "C:\\unifiedcollector"'
    startup_seam = '$startup = [Environment]::GetFolderPath("Startup")'
    assert source.count(repo_seam) == source.count(startup_seam) == 1
    source = source.replace(repo_seam, f"$repo = {_ps_literal(repo)}")
    source = source.replace(startup_seam, f"$startup = {_ps_literal(startup)}")
    copied_registrar = tmp_path / registrar
    copied_registrar.write_text(source, encoding="utf-8")
    harness = tmp_path / "generate.ps1"
    extra = " -CdpPort 9447" if "autorecover" in registrar else ""
    harness.write_text(
        "function New-ScheduledTaskAction { @{} }\n"
        "function New-ScheduledTaskTrigger { [pscustomobject]@{ Delay = '' } }\n"
        "function New-ScheduledTaskSettingsSet { @{} }\n"
        "function Register-ScheduledTask { throw 'Access is denied' }\n"
        f"& {_ps_literal(copied_registrar)} -TaskName InertRecovery -IntervalMinutes 7{extra}\n",
        encoding="utf-8",
    )
    generated = _run_ps(harness)
    assert generated.returncode == 0, generated.stderr
    launcher = startup / "InertRecovery.cmd"
    assert launcher.is_file()
    # Exercise the exact generated .cmd and unmodified run_hidden.vbs, not a decoder.
    result = _run_owned([os.environ["COMSPEC"], "/d", "/c", str(launcher)], tmp_path)
    assert result.returncode == 37, (result.returncode, result.stdout, result.stderr)
    observed = json.loads(record.read_text(encoding="utf-8-sig"))
    assert observed["args"] == expected_args
    assert Path(observed["exe"]).resolve() == Path(PWSH).resolve()
    assert observed["commandLine"][1:] == [
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(scripts / target), *expected_args,
    ]
    assert observed["policy"] == "Bypass"
    print(f"roundtrip: {registrar}; flags={observed['commandLine'][1:]}; exit=37")


@pytest.mark.parametrize(
    ("exit_code", "output", "cdp", "healthy", "reason", "count", "recoveries"),
    [
        (1, "", True, True, "db_probe_failed_assume_ok", -1, 0),
        (1, "0", True, True, "db_probe_failed_assume_ok", -1, 0),
        (1, "42", True, True, "db_probe_failed_assume_ok", -1, 0),
        (0, "", True, True, "db_probe_failed_assume_ok", -1, 0),
        (0, "garbage", True, True, "db_probe_failed_assume_ok", -1, 0),
        (0, "-1", True, True, "db_probe_failed_assume_ok", -1, 0),
        (0, "1.5", True, True, "db_probe_failed_assume_ok", -1, 0),
        (0, "  42  ", True, True, "ok", 42, 0),
        (0, "0", True, False, "scraping_stale_25m", 0, 1),
        (0, "42", False, False, "cdp_unreachable", -1, 1),
    ],
)
def test_probe_evidence_controls_recovery(
    tmp_path: Path, exit_code: int, output: str, cdp: bool,
    healthy: bool, reason: str, count: int, recoveries: int,
) -> None:
    native = tmp_path / "inert docker.cmd"
    native.write_text(f"@echo off\n{('echo ' + output) if output else ''}\nexit /b {exit_code}\n", encoding="ascii")
    harness = tmp_path / "probe.ps1"
    http_response = "@{}" if cdp else "throw 'CDP unavailable'"
    harness.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        "$PSNativeCommandUseErrorActionPreference = $false\n"
        "$tokens = $null; $errors = $null\n"
        f"$ast = [System.Management.Automation.Language.Parser]::ParseFile({_ps_literal(REPO_ROOT / 'scripts' / 'browser-autorecover.ps1')}, [ref]$tokens, [ref]$errors)\n"
        "if ($errors.Count) { throw $errors[0] }\n"
        # Load only the two functions under test; no top-level paths, loop, or recovery body.
        "$ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -in @('Get-ExtHealth', 'Invoke-Cycle') }, $false) | ForEach-Object { . ([scriptblock]::Create($_.Extent.Text)) }\n"
        f"function docker {{ & $env:ComSpec /d /c {_ps_literal(native)}; $global:LASTEXITCODE = $LASTEXITCODE }}\n"
        f"function Invoke-RestMethod {{ {http_response} }}\n"
        "function Invoke-Recovery { $script:recoveries++ }\n"
        "function Start-Sleep {}\n"
        "function Log($m) {}\n"
        "function Write-Status($health, $action) { $script:action = $action }\n"
        "$CdpPort = 9447; $CheckOnly = $false; $script:recoveries = 0\n"
        "$health = Get-ExtHealth\n"
        "Invoke-Cycle\n"
        "@{ health = $health; recoveries = $script:recoveries; action = $script:action } | ConvertTo-Json -Compress\n",
        encoding="utf-8",
    )
    result = _run_ps(harness)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed["health"] == {
        "reachable": cdp, "healthy": healthy, "reason": reason, "recent_events": count,
    }
    assert observed["recoveries"] == recoveries
    if recoveries == 0:
        assert observed["action"] == "none"
