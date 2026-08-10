from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _read_script(name: str) -> str:
    return (REPO_ROOT / "scripts" / name).read_text(encoding="utf-8")


def test_chrome_cdp_launcher_uses_robust_startup_flags():
    script = _read_script("start-scraper-chrome-cdp.ps1")

    assert "[int]$RemoteDebuggingPort = 9333" in script
    assert "--disable-dev-shm-usage" in script
    assert "--remote-debugging-address=0.0.0.0" in script
    assert "--remote-allow-origins=*" in script
    assert "--no-first-run" in script
    assert "--no-default-browser-check" in script
    assert "--enable-extensions" in script
    assert "--disable-extensions-except=$extension" in script
    assert "[switch]$IsolateExtensions" in script
    assert "if ($IsolateExtensions)" in script
    assert "--disable-background-mode" in script
    assert "--disable-background-timer-throttling" in script
    assert "--js-flags=--max-old-space-size=512" in script
    assert "UC_CHROME_RENDERER_PROCESS_LIMIT" in script
    assert "--renderer-process-limit=$rendererLimit" in script
    assert "[switch]$CloseExistingCdpProfile" in script
    assert "function Get-CdpProfileChromeProcesses" in script
    assert "function Stop-CdpProfileChrome" in script


def test_chrome_cdp_launcher_prefers_extension_capable_chromium():
    script = _read_script("start-scraper-chrome-cdp.ps1")

    assert "Chrome 137+ removed command-line unpacked extension loading" in script
    assert "Chrome for Testing" in script
    assert "ms-playwright" in script
    assert 'Sort-Object LastWriteTime -Descending' in script


def test_chrome_cdp_launcher_prefers_dedicated_automation_profile():
    script = _read_script("start-scraper-chrome-cdp.ps1")

    assert "ChromeCdpRecoveredProfile" in script
    assert "ChromeCdpAutomationProfile" in script
    assert script.index("ChromeCdpAutomationProfile") < script.index("ChromeCdpRecoveredProfile")
    assert "Sort-Object -Descending" in script
    assert 'Join-Path $_ "Default\\Network\\Cookies"' in script
    assert 'return $automation' in script
    assert "incompatible with Chrome-for-Testing / Playwright Chromium" in script


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
    assert "ChromeCdpAutomationProfile" in script
    assert "$existing[0].FullName" not in script
    assert "^ChromeCdpProfile" not in script
    assert "$env:LOCALAPPDATA\\Google\\Chrome\\User Data" in script
    assert "does not expose CDP for the default Chrome profile" in script


def test_chrome_cdp_launcher_ignores_stale_wmi_chrome_rows():
    script = _read_script("start-scraper-chrome-cdp.ps1")

    assert "function Get-ChromeProcesses" in script
    assert "Get-Process chrome" in script
    assert "$proc.HandleCount -le 0" in script
    assert "$liveIds.ContainsKey([int]$_.ProcessId)" in script


def test_chrome_cdp_launcher_cleans_stale_debug_port_owner():
    script = _read_script("start-scraper-chrome-cdp.ps1")

    assert "function Get-PortListenerPids" in script
    assert "netstat.exe" in script
    assert "function Stop-ProcessIds" in script
    assert "function Wait-PortReleased" in script
    assert "Port $RemoteDebuggingPort is still owned by stale PID(s)" in script
    assert "Wait-PortReleased -Port $RemoteDebuggingPort" in script


def test_chrome_cdp_launcher_discovers_extension_id_from_cdp():
    script = _read_script("start-scraper-chrome-cdp.ps1")

    assert "function Get-ExtensionIdFromCdp" in script
    assert "function Get-KnownExtensionIds" in script
    assert "function Open-ExtensionControlPage" in script
    assert "function Open-CdpTarget" in script
    assert "function Try-OpenCdpTarget" in script
    assert "Could not open CDP target" in script
    assert "$knownIds = @(Get-KnownExtensionIds)" in script
    assert "function Get-PrimaryKnownExtensionId" in script
    assert '("service_worker", "background_page")' in script
    assert "'^chrome-extension://([a-p]{32})/'" in script
    assert '$knownIds -contains $match.Groups[1].Value' in script
    assert "nkeimhogjdpnpccoofpliimaahmaaome" in script
    assert 'chrome-extension://$(Get-PrimaryKnownExtensionId)/$tabsUrlPath' in script
    assert '"about:blank"' in script
    assert "chrome-extension://$extensionId/$TabsUrlPath" in script
    assert "Opened extension control page" in script
    assert "Chrome CDP is already reachable" in script


def test_browser_tab_audit_accepts_dynamic_extension_worlds():
    script = (REPO_ROOT / "tools" / "browser_tab_audit.py").read_text(encoding="utf-8")

    assert 'os.getenv("UC_EXTENSION_ID"' in script
    assert 'origin.startswith("chrome-extension://")' in script
    assert "UC_CHROME_CDP_PORT" in script
    assert "'9333'" in script


def test_browser_tab_audit_uses_load_tolerant_default_deadlines():
    script = (REPO_ROOT / "tools" / "browser_tab_audit.py").read_text(encoding="utf-8")

    assert 'UC_TAB_AUDIT_RUNTIME_ENABLE_TIMEOUT_SECONDS", 4.0' in script
    assert 'UC_TAB_AUDIT_MAIN_TIMEOUT_SECONDS", 8.0' in script


def test_browser_tab_reload_treats_disappeared_targets_as_skips():
    script = (REPO_ROOT / "tools" / "browser_tab_reload.py").read_text(encoding="utf-8")

    assert "import os" in script
    assert "UC_CHROME_CDP_PORT" in script
    assert "'9333'" in script
    assert "def _target_disappeared" in script
    assert '"no such target" in text' in script
    assert '"target_disappeared"' in script
    assert "SKIP: target disappeared before reload" in script


def test_browser_tab_reload_hard_reopens_repeatedly_stuck_tiktok_tabs():
    script = (REPO_ROOT / "tools" / "browser_tab_reload.py").read_text(encoding="utf-8")

    assert "UC_BROWSER_HARD_REOPEN_PLATFORMS" in script
    assert '"instagram,threads,tiktok,lemon8,x,facebook,strava"' in script
    assert '"https://www.tiktok.com/following"' in script
    hard_reopen_block = script.split("HARD_REOPEN_URLS = {", 1)[1].split("def _target_version", 1)[0]
    assert '"https://www.tiktok.com/foryou"' not in hard_reopen_block
    assert '"https://www.tiktok.com/explore"' not in hard_reopen_block
    assert '"https://www.lemon8-app.com/topic/food?region=sg"' in script
    assert "def _platform_had_previous_unresponsive_reload" in script
    assert "def _hard_reopen_platform" in script
    assert "reopen_urls = HARD_REOPEN_URLS.get(platform)" in script
    assert "dict.fromkeys" in script
    assert "hard_reopen_close" in script
    assert "hard_reopen_open" in script


def test_browser_tab_reload_hard_reopens_individual_repeated_stuck_tabs():
    script = (REPO_ROOT / "tools" / "browser_tab_reload.py").read_text(encoding="utf-8")

    assert "def _hard_reopen_repeated_tabs" in script
    assert "reopen repeated stuck tab after prior soft reload" in script
    assert "_previous_reload_for_url(previous_plan, platform" in script
    assert "hard_reopen_tabs.add" in script


def test_chrome_cdp_launcher_dry_run_skips_live_probes():
    script = _read_script("start-scraper-chrome-cdp.ps1")

    assert "Dry run: skipped live Chrome/CDP probes." in script
    assert script.index("if ($DryRun)") < script.index("$chromeProcesses = @(Get-ChromeProcesses)")
    assert script.index("if ($DryRun)") < script.index("$cdpAlreadyUp = Test-CdpAvailable")


def test_chrome_cdp_launcher_opens_requested_platform_urls_directly():
    script = _read_script("start-scraper-chrome-cdp.ps1")

    assert '([string]$_) -split ","' in script
    assert "function Get-PlatformLaunchUrls" in script
    assert 'instagram = "https://www.instagram.com/"' in script
    assert 'tiktok = "https://www.tiktok.com/following"' in script
    assert 'lemon8 = "https://www.lemon8-app.com/topic/7011425874067619842?region=sg"' in script
    assert 'strava = "https://www.strava.com/dashboard"' in script
    assert "$platforms.Keys -contains $id" in script
    assert "Open-RequestedPlatformTabs -Port $RemoteDebuggingPort" in script


def test_chrome_cdp_launcher_directly_opens_requested_platforms_after_control():
    script = _read_script("start-scraper-chrome-cdp.ps1")

    assert "$controlOpened = Open-ExtensionControlPage" in script
    assert "function Open-RequestedPlatformTabs" in script
    assert script.count("Open-RequestedPlatformTabs -Port $RemoteDebuggingPort") == 2
    assert "if ($OpenIds.Count -gt 0 -or ($OpenAll -and -not $NoOpenAll))" in script
    assert "UC_CHROME_OPEN_TAB_DELAY_MS" in script
    assert "$delayMs = 5000" in script


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


def test_browser_maintenance_repairs_missing_chrome():
    script = _read_script("browser-tab-maintenance.ps1")

    assert "function Invoke-ChromeCdpRepair" in script
    assert 'reason -eq "chrome_not_running"' in script
    assert "chrome_cdp_process_unreachable" in script
    assert "hidden unreachable CDP Chrome" in script
    assert "-OpenIds instagram,tiktok,lemon8,x,threads,facebook,strava" in script
    assert "Chrome CDP repair succeeded; continuing maintenance pass" in script
    assert "chrome_cdp_available" in script


def test_browser_maintenance_refuses_overlapping_passes():
    script = _read_script("browser-tab-maintenance.ps1")

    assert "Global\\UnifiedCollectorBrowserTabMaintenance" in script
    assert "$mutex.WaitOne(0)" in script
    assert "another pass is already running" in script
    assert "$mutex.ReleaseMutex()" in script


def test_browser_maintenance_uses_load_tolerant_wrapper_timeouts():
    script = _read_script("browser-tab-maintenance.ps1")

    assert 'UC_BROWSER_AUDIT_TIMEOUT_SECONDS" 240' in script
    assert 'UC_BROWSER_RELOAD_TIMEOUT_SECONDS" 180' in script


def test_browser_maintenance_reaudits_after_reload():
    script = _read_script("browser-tab-maintenance.ps1")

    audit_call = "Invoke-PythonScript -command $python -script $audit"
    reload_call = "Invoke-PythonScript -command $python -script $reload"
    assert script.count(audit_call) >= 2
    assert script.count(reload_call) >= 1
    assert script.index(audit_call) < script.index(reload_call)
    assert script.rindex(audit_call) > script.index(reload_call)
    assert "pre-reload snapshot" in script
    assert "UC_BROWSER_POST_RELOAD_SETTLE_SECONDS" in script


def test_browser_maintenance_uses_short_live_audit_probes():
    script = _read_script("browser-tab-maintenance.ps1")

    assert 'Set-DefaultEnv "UC_TAB_AUDIT_RUNTIME_ENABLE_TIMEOUT_SECONDS" "3.0"' in script
    assert 'Set-DefaultEnv "UC_TAB_AUDIT_MAIN_TIMEOUT_SECONDS" "4.0"' in script
    assert 'Set-DefaultEnv "UC_TAB_AUDIT_ISO_TIMEOUT_SECONDS" "2.0"' in script
    assert 'Set-DefaultEnv "UC_TAB_AUDIT_PERF_TIMEOUT_SECONDS" "0.5"' in script
    assert "without pinning the machine" in script


def test_browser_maintenance_restarts_dedicated_profile_when_tabs_stay_unhealthy():
    script = _read_script("browser-tab-maintenance.ps1")

    assert "function Get-AuditHealth" in script
    assert "UC_BROWSER_MIN_HEALTHY_PLATFORMS" in script
    assert 'Get-PositiveIntEnv "UC_BROWSER_MIN_HEALTHY_PLATFORMS" $platforms.Count' in script
    assert "function Test-AuthWallUrl" in script
    assert "function Get-AuditTabUrl" in script
    assert "function Test-AuditTabContentWall" in script
    assert "page_health_status" in script
    assert "recoverable_error_shell" in script
    assert "/i/flow/login" in script
    assert "redirect_after_login" in script
    assert "-not (Test-AuthWallUrl (Get-AuditTabUrl $_))" in script
    assert "-not (Test-AuditTabContentWall $_)" in script
    assert "function Invoke-ScraperChromeProfileRestart" in script
    assert "-CloseExistingCdpProfile" in script
    assert "-CloseExistingIfNoVisibleWindows" in script
    assert "-FallbackOpenControlIfCleanupBlocked" in script
    assert "dedicated scraper Chrome restart left CDP unavailable; fallback repair reason=" in script
    assert "Invoke-ChromeCdpRepair -Diagnostics $diagnostics" in script
    assert "browser extension tabs unhealthy after reload/profile restart" in script
    assert 'Write-Status "degraded"' in script


def test_browser_audit_reports_dom_level_error_shells():
    script = (REPO_ROOT / "tools" / "browser_tab_audit.py").read_text(encoding="utf-8")

    assert "page_health_status" in script
    assert "page_health_reason" in script
    assert "page_health_sample" in script
    assert 'ISO_TIMEOUT = _float_env("UC_TAB_AUDIT_ISO_TIMEOUT_SECONDS", 2.0)' in script
    assert "recoverable_error_shell" in script
    assert "try_again_empty_state" in script
    assert "auth_challenge" in script
    assert "recaptcha" in script
    assert 'low&&document.querySelector(\'iframe[src*=\\"recaptcha\\"]\')' in script
