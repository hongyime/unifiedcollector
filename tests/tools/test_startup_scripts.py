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
    assert "New-ScheduledTaskTrigger -AtLogOn" in task_script
    assert "AtStartup registration was denied; registered current-user AtLogOn task instead" in task_script
    assert "run_hidden.vbs" in task_script


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
    assert "Z:\\unifiedcollector\\backups\\db" in script
    assert "BackupFreshHours" in script
    assert "ActiveBackupFreshMinutes" in script
    assert "MaintenanceStatusFreshMinutes" in script
    assert "DefaultSourceFreshMinutes" in script
    assert "source_health" in script
    assert "RecentIngestionMinutes" in script
    assert "recent ingestion window" in script
    assert "telegram_messages" in script
    assert "media_items" in script
    assert "last ${RecentIngestionMinutes}m" in script
    assert "instagram = 60" in script
    assert "github = 240" in script
    assert "strava = 60" in script
    assert 'Add-Check $checks "source fresh: $source"' in script
    assert "dead_letter_queue" in script
    assert "dead letter backlog" in script
    assert "facebook = 60" in script
    assert "threads = 60" in script
    assert "x = 90" in script
    assert "$maintenanceRunningWithOkPrevious" in script
    assert '$maintenanceStatus.state -eq "running" -and' in script
    assert '$lastTerminalState -eq "ok"' in script
    assert "maintenance pass in progress; last_terminal_state=ok" in script
    assert "status IN ('queued', 'retry')" in script
    assert "status = 'pending' AND next_retry_at <= NOW()" in script
    assert "DlqBacklogGraceMinutes" in script
    assert "DlqPendingThreshold" in script
    assert "created_at < NOW() - INTERVAL '1 minute' * $DlqBacklogGraceMinutes" in script
    assert "HAVING count(*) >= $DlqPendingThreshold" in script
    assert "db backup freshness" in script
    assert ".inprogress_*.dump" in script
    assert "browser maintenance latest status" in script
    assert "browser_tab_maintenance_status.json" in script
    assert "browser_tab_audit_result.json" in script
    assert "extension content script: $platform" in script
    assert "cs_running" in script
    assert "$allTabsHealthy = ($healthy.Count -gt 0)" in script
    assert "problem_or_stale=" in script
    assert "healthy=$($healthy.Count)/$($tabs.Count)" in script
    assert "function Test-AuthWallUrl" in script
    assert "function Get-AuditTabUrl" in script
    assert "function Test-AuditTabContentWall" in script
    assert "page_health_status" in script
    assert "recoverable_error_shell" in script
    assert "/i/flow/login" in script
    assert "redirect_after_login" in script
    assert "/log_out" in script
    assert "logout=" in script
    assert "(Test-UrlHostMatches -Url $_ -ExpectedHost $needle) -and -not (Test-AuthWallUrl $_)" in script
    assert "-not (Test-AuthWallUrl (Get-AuditTabUrl $_))" in script
    assert "-not (Test-AuditTabContentWall $_)" in script
    assert "browser_tab_maintenance_loop.pid" in script
    assert "instagram.com" in script
    assert "tiktok.com" in script
    assert "lemon8-app.com" in script
    assert "strava.com" in script


def test_backup_task_allows_progress_guarded_long_dumps():
    script = (REPO_ROOT / "scripts" / "register-backup-task.ps1").read_text(encoding="utf-8")

    assert "UnifiedCollectorBackup" in script
    assert "ExecutionTimeLimit (New-TimeSpan -Hours 0)" in script
    assert "stall-progress timeout" in script
    assert "fixed wall-clock limit" in script
