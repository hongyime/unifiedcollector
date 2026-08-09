from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _read_script(name: str) -> str:
    return (REPO_ROOT / "scripts" / name).read_text(encoding="utf-8")


def test_chrome_cdp_launcher_uses_robust_startup_flags():
    script = _read_script("start-scraper-chrome-cdp.ps1")

    assert "--disable-dev-shm-usage" in script
    assert "--disable-background-timer-throttling" in script
    assert "--js-flags=--max-old-space-size=512" in script
    assert "--renderer-process-limit=10" in script


def test_chrome_cdp_launcher_makes_open_all_explicit():
    script = _read_script("start-scraper-chrome-cdp.ps1")

    assert "[switch]$OpenAll" in script
    assert 'if ($OpenAll -and -not $NoOpenAll -and $OpenIds.Count -eq 0)' in script
    assert 'if (-not $NoOpenAll -and $OpenIds.Count -eq 0)' not in script


def test_chrome_cdp_fallback_does_not_open_all_by_default():
    script = _read_script("start-scraper-chrome-cdp.ps1")

    assert (
        "if ($FallbackOpenControlIfCleanupBlocked -and "
        "$OpenIds.Count -eq 0 -and -not $OpenAll)"
    ) in script
    assert "$NoOpenAll = $true" in script


def test_browser_maintenance_loop_refuses_duplicate_direct_start():
    script = _read_script("browser-tab-maintenance-loop.ps1")

    assert "loop already running as pid=$parsedPid; refusing duplicate direct start" in script
    assert "Get-Process -Id $parsedPid" in script
