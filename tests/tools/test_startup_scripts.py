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
