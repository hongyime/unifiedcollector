from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_collector_startup_restarts_telegram_after_session_sync():
    script = (REPO_ROOT / "scripts" / "register-collector-startup.ps1").read_text(encoding="utf-8")

    assert "UnifiedCollector_Startup.bat" in script
    assert "docker compose -f %COMPOSE% up -d" in script
    assert "set SESSION_TARGET=unifiedcollector_collector_telegram" in script
    assert "docker cp" in script
    assert "docker restart unifiedcollector_collector_telegram" in script
    assert script.index("docker cp") < script.index("docker restart unifiedcollector_collector_telegram")


def test_collector_startup_launches_browser_maintenance_loop():
    script = (REPO_ROOT / "scripts" / "register-collector-startup.ps1").read_text(encoding="utf-8")

    assert "[int]$BrowserMaintenanceIntervalMinutes = 10" in script
    assert "set BROWSER_MAINTENANCE=" in script
    assert "start-browser-maintenance-loop.ps1" in script
    assert "Starting browser maintenance loop" in script
    assert "-IntervalMinutes $BrowserMaintenanceIntervalMinutes" in script
    assert script.index("docker compose -f %COMPOSE% up -d") < script.index("Starting browser maintenance loop")


def test_browser_maintenance_startup_defaults_to_fast_recovery():
    task_script = (REPO_ROOT / "scripts" / "register-browser-maintenance-task.ps1").read_text(encoding="utf-8")
    startup_script = (REPO_ROOT / "scripts" / "install-browser-maintenance-startup.ps1").read_text(encoding="utf-8")

    assert "[int]$IntervalMinutes = 10" in task_script
    assert "[int]$IntervalMinutes = 10" in startup_script


def test_browser_maintenance_startup_runs_hidden():
    script = (REPO_ROOT / "scripts" / "install-browser-maintenance-startup.ps1").read_text(encoding="utf-8")

    assert "UnifiedCollectorBrowserMaintenance.cmd" in script
    assert "run_hidden.vbs" in script
    assert "wscript.exe" in script
    assert "start-browser-maintenance-loop.ps1" in script


def test_boot_verifier_checks_reboot_critical_surfaces():
    script = (REPO_ROOT / "scripts" / "verify-collector-boot.ps1").read_text(encoding="utf-8")

    assert "docker\\docker-compose.yml" in script
    assert "UnifiedCollector_Startup.bat" in script
    assert "UnifiedCollectorBrowserMaintenance.cmd" in script
    assert "UnifiedCollectorDockerWatchdog.cmd" in script
    assert "UnifiedCollectorBrowserCookieVault.cmd" in script
    assert "docker compose -f $composePath ps --format json" in script
    assert "http://127.0.0.1:8700/health" in script
    assert "http://127.0.0.1:9333" in script
    assert "browser_tab_maintenance_loop.pid" in script
    assert "instagram.com" in script
    assert "tiktok.com" in script
    assert "lemon8-app.com" in script
    assert "strava.com" in script
