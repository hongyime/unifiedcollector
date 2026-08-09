from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _read_script(name: str) -> str:
    return (REPO_ROOT / "scripts" / name).read_text(encoding="utf-8")


def test_chrome_cdp_launcher_uses_robust_startup_flags():
    script = _read_script("start-scraper-chrome-cdp.ps1")

    assert "--disable-dev-shm-usage" in script
    assert "--remote-debugging-address=127.0.0.1" in script
    assert "--remote-allow-origins=http://127.0.0.1:$RemoteDebuggingPort" in script
    assert "--no-first-run" in script
    assert "--no-default-browser-check" in script
    assert "--disable-background-mode" in script
    assert "--disable-background-timer-throttling" in script
    assert "--js-flags=--max-old-space-size=512" in script
    assert "--renderer-process-limit=10" in script


def test_chrome_cdp_launcher_disables_profile_background_mode():
    script = _read_script("start-scraper-chrome-cdp.ps1")

    assert "function Disable-ChromeBackgroundMode" in script
    assert 'Join-Path $UserDataDir "Local State"' in script
    assert 'Join-Path $UserDataDir "Default\\Preferences"' in script
    assert '"background_mode"' in script
    assert '"enabled"' in script
    assert "$false" in script


def test_chrome_cdp_launcher_uses_non_default_profile_for_cdp():
    script = _read_script("start-scraper-chrome-cdp.ps1")

    assert "Chrome 136+ ignores --remote-debugging-port" in script
    assert "UnifiedCollector\\ChromeCdpProfile" in script
    assert "$env:LOCALAPPDATA\\Google\\Chrome\\User Data" in script
    assert "does not expose CDP for the default Chrome profile" in script


def test_chrome_cdp_launcher_ignores_stale_wmi_chrome_rows():
    script = _read_script("start-scraper-chrome-cdp.ps1")

    assert "function Get-ChromeProcesses" in script
    assert "Get-Process chrome" in script
    assert "$liveIds.ContainsKey([int]$_.ProcessId)" in script


def test_chrome_cdp_launcher_discovers_extension_id_from_cdp():
    script = _read_script("start-scraper-chrome-cdp.ps1")

    assert "function Get-ExtensionIdFromCdp" in script
    assert "function Open-CdpTarget" in script
    assert '("service_worker", "background_page")' in script
    assert "'^chrome-extension://([a-p]{32})/'" in script
    assert '"about:blank"' in script
    assert '"chrome-extension://$extensionId/$tabsUrlPath"' in script
    assert "Opened extension control page" in script
    assert "Chrome CDP is already reachable" in script


def test_chrome_cdp_launcher_opens_requested_platform_urls_directly():
    script = _read_script("start-scraper-chrome-cdp.ps1")

    assert '([string]$_) -split ","' in script
    assert "function Get-PlatformLaunchUrls" in script
    assert 'instagram = "https://www.instagram.com/"' in script
    assert 'tiktok = "https://www.tiktok.com/following"' in script
    assert 'strava = "https://www.strava.com/dashboard"' in script
    assert "$platforms.Keys -contains $id" in script
    assert "Get-PlatformLaunchUrls -Ids $OpenIds" in script


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
