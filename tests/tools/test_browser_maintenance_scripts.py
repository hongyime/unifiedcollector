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
    assert "function Get-ScraperProfileChromeProcesses" in script
    assert "function Get-CdpProfileChromeProcesses" in script
    assert "function Stop-CdpProfileChrome" in script
    assert "function Test-UnifiedCollectorControlWindow" in script
    assert "function Get-UnsafeVisibleChromeWindows" in script
    assert "only UnifiedCollector control windows are visible" in script
    assert "$scraperProfileProcesses = @(Get-ScraperProfileChromeProcesses -UserDataDir $profile)" in script
    assert "if ($CloseExistingCdpProfile -and $scraperProfileProcesses.Count -gt 0)" in script
    assert "$profileProcesses = @(Get-ScraperProfileChromeProcesses -UserDataDir $UserDataDir)" in script
    assert "A failed Chrome startup can keep the scraper profile open" in script
    assert "UC_CHROME_OPEN_TARGET_TIMEOUT_SECONDS" in script
    assert "UC_CHROME_OPEN_EXPANDED_PLATFORM_TABS" in script
    assert "$delayMs = 1200" in script


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
    resolver = script[script.index("function Resolve-UserDataDir") : script.index("function Test-CdpAvailable")]
    assert "Sort-Object -Descending" not in resolver
    assert "if (Test-Path -LiteralPath $automation)" in resolver
    assert "if (Test-Path -LiteralPath $recovered)" in resolver
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
    assert 'Start-Process -FilePath "$env:SystemRoot\\System32\\taskkill.exe"' not in script
    assert '$taskkillOutput = & "$env:SystemRoot\\System32\\taskkill.exe"' in script
    assert "$taskkillExitCode = $LASTEXITCODE" in script
    assert '$previousErrorActionPreference = $ErrorActionPreference' in script
    assert "Ignoring stale Chrome WMI rows after taskkill" in script
    assert "taskkill failed for PID ${processId} with exit code ${taskkillExitCode}" in script


def test_chrome_cdp_launcher_discovers_extension_id_from_cdp():
    script = _read_script("start-scraper-chrome-cdp.ps1")

    assert "function Get-ExtensionIdFromCdp" in script
    assert "function Get-KnownExtensionIds" in script
    assert "function Open-ExtensionControlPage" in script
    assert "function Open-CdpTarget" in script
    assert "function Find-ExistingCdpTarget" in script
    assert "function Activate-CdpTarget" in script
    assert "function Try-OpenCdpTarget" in script
    assert "Invoke-WebRequest -Uri \"http://127.0.0.1:$Port/json/list\"" in script
    assert "$targets = @($response.Content | ConvertFrom-Json)" in script
    assert "$existingTargetId = Find-ExistingCdpTarget" in script
    assert "json/activate/$TargetId" in script
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


def test_chrome_cdp_launcher_defaults_to_one_startup_tab_per_platform():
    script = _read_script("start-scraper-chrome-cdp.ps1")
    launch_section = script[script.index("function Get-PlatformLaunchUrls") : script.index("function Get-ChromeProcesses")]

    assert 'UC_CHROME_OPEN_EXPANDED_PLATFORM_TABS") -eq "1"' in launch_section
    assert '"https://www.tiktok.com/foryou"' in launch_section
    assert '"https://www.tiktok.com/following"' in launch_section
    assert '"https://www.lemon8-app.com/topic/singapore?region=sg"' in launch_section
    assert '"https://www.lemon8-app.com/topic/food?region=sg"' not in launch_section
    assert 'tiktok = if ($expandedPlatformTabs)' in launch_section
    assert 'lemon8 = "https://www.lemon8-app.com/topic/singapore?region=sg"' in launch_section


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


def test_browser_tab_reload_recovers_dom_error_shells_and_stale_auth_duplicates():
    script = (REPO_ROOT / "tools" / "browser_tab_reload.py").read_text(encoding="utf-8")

    assert 'tab.get("page_health_status") == "recoverable_error_shell"' in script
    assert 'return True, f"page health: {reason}"' in script
    assert '"/i/flow/login"' in script
    assert '"redirect_after_login"' in script
    assert '"?logout="' in script
    assert "stale_auth_wall_close_tabs = {" in script
    assert 'and p["platform"] in healthy_platforms' in script
    assert '"close_duplicate_auth_wall"' in script
    assert "duplicate_healthy_close_tabs" in script
    assert '"close_duplicate_healthy_url"' in script
    assert "if str(p[\"target_id\"]) in stale_auth_wall_close_tabs:" in script


def test_browser_tab_maintenance_closes_duplicate_cdp_page_targets():
    script = (REPO_ROOT / "scripts" / "browser-tab-maintenance.ps1").read_text(encoding="utf-8")

    assert "function Remove-DuplicateCdpPageTargets" in script
    assert "function Remove-DuplicateExtensionControlTabs" in script
    assert "function Remove-BlankStartupTabs" in script
    assert "^chrome-extension://[^/]+/tabs\\.html(?:[?#].*)?$" in script
    assert "Invoke-WebRequest -Uri \"http://127.0.0.1:$script:CdpPort/json/list\"" in script
    assert "return @($response.Content | ConvertFrom-Json)" in script
    assert "Group-Object -Property url" in script
    assert "Select-Object -Skip 1" in script
    assert "closed duplicate CDP page target" in script
    assert "closed duplicate extension control tab" in script
    assert "closed blank Chrome startup tab" in script
    assert '$_.type -eq "page" -and [string]$_.url -eq "about:blank"' in script
    assert '[string]$_.url -eq "chrome-extension://$primaryId/tabs.html"' in script
    assert "Remove-DuplicateCdpPageTargets" in script
    assert "function Ensure-ExtensionControlTab {\n    Remove-DuplicateCdpPageTargets" in script
    assert "Remove-DuplicateExtensionControlTabs" in script
    assert "Remove-BlankStartupTabs" in script
    assert "Ensure-ExtensionControlTab | Out-Null\n            Remove-DuplicateCdpPageTargets" in script
    assert "Ensure-ExtensionControlTab | Out-Null\n    Remove-DuplicateCdpPageTargets" in script


def test_browser_tab_reload_hard_reopens_repeatedly_stuck_tiktok_tabs():
    script = (REPO_ROOT / "tools" / "browser_tab_reload.py").read_text(encoding="utf-8")

    assert "UC_BROWSER_HARD_REOPEN_PLATFORMS" in script
    assert '"instagram,threads,tiktok,x,facebook,strava"' in script
    assert '"https://www.tiktok.com/foryou"' in script
    assert '"https://www.tiktok.com/following"' in script
    assert '"https://www.tiktok.com/explore"' in script
    assert '"x": [' in script
    assert '"https://x.com/home"' in script
    hard_reopen_block = script.split("HARD_REOPEN_URLS = {", 1)[1].split("def _target_version", 1)[0]
    assert '"lemon8": [' not in hard_reopen_block
    assert "def _platform_had_previous_unresponsive_reload" in script
    assert "def _hard_reopen_platform" in script
    assert "reopen_urls = HARD_REOPEN_URLS.get(platform)" in script
    assert "dict.fromkeys" in script
    assert "hard_reopen_close" in script
    assert "hard_reopen_open" in script
    assert "shell_tabs = [" in script
    assert '"page health:" in str(p["reason"]).lower()' in script
    assert "hard_reopen_tabs.update" in script


def test_browser_tab_reload_hard_reopens_individual_repeated_stuck_tabs():
    script = (REPO_ROOT / "tools" / "browser_tab_reload.py").read_text(encoding="utf-8")

    assert "def _hard_reopen_repeated_tabs" in script
    assert "reopen repeated stuck tab after prior soft reload" in script
    assert "_previous_reload_for_url(previous_plan, platform" in script
    assert "hard_reopen_tabs.add" in script
    assert "reopen_urls = list(dict.fromkeys" in script
    assert '"page health:" in text' in script
    assert '"recoverable_error_shell" in text' in script


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
    assert 'UC_CHROME_OPEN_EXPANDED_PLATFORM_TABS") -eq "1"' in script
    assert "tiktok = if ($expandedPlatformTabs)" in script
    assert '"https://www.tiktok.com/foryou"' in script
    assert '"https://www.tiktok.com/following"' in script
    assert '"https://www.tiktok.com/explore"' in script
    assert "lemon8 = if ($expandedPlatformTabs)" not in script
    assert '"https://www.lemon8-app.com/topic/food?region=sg"' not in script
    assert '"https://www.lemon8-app.com/topic/travel?region=sg"' not in script
    assert '"https://www.lemon8-app.com/topic/singapore?region=sg"' in script
    assert "foreach ($url in @($platforms[$id]))" in script
    assert 'strava = "https://www.strava.com/dashboard"' in script
    assert "$platforms.Keys -contains $id" in script
    assert "Open-RequestedPlatformTabs -Port $RemoteDebuggingPort" in script


def test_chrome_cdp_launcher_directly_opens_requested_platforms_after_control():
    script = _read_script("start-scraper-chrome-cdp.ps1")

    assert "$controlOpened = Open-ExtensionControlPage" in script
    assert "function Open-RequestedPlatformTabs" in script
    assert "function Find-ExistingCdpTarget" in script
    assert "$targets = @($response.Content | ConvertFrom-Json)" in script
    assert script.count("Open-RequestedPlatformTabs -Port $RemoteDebuggingPort") == 2
    assert "if ($OpenIds.Count -gt 0 -or ($OpenAll -and -not $NoOpenAll))" in script
    assert "UC_CHROME_OPEN_TAB_DELAY_MS" in script
    assert "$delayMs = 1200" in script
    assert "Opened requested platform tab:" in script
    assert "Failed to open requested platform tab via CDP:" in script


def test_chrome_cdp_launcher_treats_dedicated_profile_windows_as_safe_to_recover():
    script = _read_script("start-scraper-chrome-cdp.ps1")

    assert 'param($Process, [string]$UserDataDir = "")' in script
    assert "$profileFull = [IO.Path]::GetFullPath($UserDataDir).TrimEnd('\\')" in script
    assert 'ProcessId=$($Process.Id)' in script
    assert "Get-UnsafeVisibleChromeWindows -VisibleWindows $visibleChromeWindows -UserDataDir $profile" in script


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
    assert "chrome_visible_control_window_count" in script
    assert "chrome_unsafe_visible_window_count" in script
    assert "chrome-extension://.*tabs\\.html" in script
    assert "\\\\UnifiedCollector\\\\ChromeCdp" in script
    assert "--remote-debugging-port(?:=|\\s+)$script:CdpPort\\b" in script
    assert "--user-data-dir(?:=|\\s+).*\\\\UnifiedCollector\\\\ChromeCdp" in script
    assert "-CloseExistingCdpProfile -CloseExistingIfNoVisibleWindows" in script
    assert "collector-controlled unreachable CDP Chrome" in script
    assert "-FallbackOpenControlIfCleanupBlocked" not in script
    assert "-OpenIds instagram,tiktok,x,threads,facebook,strava" in script
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
    assert 'UC_BROWSER_PROFILE_RESTART_SETTLE_SECONDS" 90' in script


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
    assert "dedicated scraper Chrome restart left CDP unavailable; fallback repair reason=" in script
    assert "running second targeted browser tab reload pass before profile restart" in script
    assert "browser tab audit health after second reload" in script
    assert "browser tab audit still unhealthy after second reload" in script
    assert "function Test-AuditHealthNeedsProfileRestart" in script
    assert "UC_BROWSER_PROFILE_RESTART_ON_TAB_HEALTH" in script
    assert "profile restart on tab-health failure is disabled by default" in script
    assert "UC_BROWSER_PROFILE_RESTART_MIN_UNHEALTHY_PLATFORMS" in script
    assert 'Get-PositiveIntEnv "UC_BROWSER_PROFILE_RESTART_MIN_UNHEALTHY_PLATFORMS" 3' in script
    assert "below profile restart threshold" in script
    assert "skipped profile restart" in script
    assert "Invoke-PostReloadSettle -seconds $profileRestartSettleSeconds" in script
    assert "Invoke-ChromeCdpRepair -Diagnostics $diagnostics" in script
    assert "browser extension tabs unhealthy after reload/profile restart" in script
    assert 'Write-Status "degraded"' in script


def test_browser_maintenance_reopens_missing_extension_control_tab():
    script = _read_script("browser-tab-maintenance.ps1")

    assert "function Test-ExtensionControlTab" in script
    assert "function Ensure-ExtensionControlTab" in script
    assert "chrome-extension://$extensionId/tabs.html" in script
    assert "opened missing extension control tab" in script
    assert "Ensure-ExtensionControlTab | Out-Null" in script


def test_browser_maintenance_does_not_profile_restart_for_manual_auth():
    script = _read_script("browser-tab-maintenance.ps1")

    assert "function Test-AuditHealthNeedsManualAuth" in script
    assert "auth_challenge|logout=" in script
    assert "manual platform auth is required; skipping profile restart" in script
    assert 'Write-Status "degraded" "browser tab requires manual platform auth"' in script


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
