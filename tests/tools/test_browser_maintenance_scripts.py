from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _read_script(name: str) -> str:
    return (REPO_ROOT / "scripts" / name).read_text(encoding="utf-8")


def test_chrome_cdp_launcher_uses_robust_startup_flags():
    script = _read_script("start-scraper-chrome-cdp.ps1")

    assert "[int]$RemoteDebuggingPort = 9336" in script
    assert "--disable-dev-shm-usage" in script
    assert "--remote-debugging-address=0.0.0.0" in script
    assert "--remote-allow-origins=*" in script
    assert "--no-first-run" in script
    assert "--no-default-browser-check" in script
    assert "--enable-extensions" in script
    assert "--disable-extensions-except=$extension" in script
    assert "[switch]$IsolateExtensions" in script
    assert "UC_CHROME_ISOLATE_EXTENSIONS" in script
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
    assert "if ($CloseExistingCdpProfile)" in script
    assert "if ($scraperProfileProcesses.Count -gt 0)" in script
    assert "$profileProcesses = @(Get-ScraperProfileChromeProcesses -UserDataDir $UserDataDir)" in script
    assert "A failed Chrome startup can keep the scraper profile open" in script
    assert "UC_CHROME_OPEN_TARGET_TIMEOUT_SECONDS" in script
    assert "UC_CHROME_OPEN_EXPANDED_PLATFORM_TABS" in script
    assert "$delayMs = 1200" in script
    assert "scraper_chrome_launch.lock" in script
    assert "scraper_chrome_state.json" in script
    assert "launch_skipped_lock_busy_reused_cdp" in script


def test_chrome_cdp_launcher_prefers_extension_capable_chromium():
    script = _read_script("start-scraper-chrome-cdp.ps1")

    assert "Chrome 137+ removed command-line unpacked extension loading" in script
    assert "Chrome for Testing" in script
    assert "ms-playwright" in script
    assert 'Sort-Object LastWriteTime -Descending' in script
    assert "chrome-win64\\chrome.exe" in script
    assert "-Recurse -Filter chrome.exe" not in script
    assert "Select-Object -First 12" in script


def test_chrome_cdp_launcher_prefers_dedicated_automation_profile():
    script = _read_script("start-scraper-chrome-cdp.ps1")

    assert "ChromeCdpAutomationProfile_recover_x" in script
    assert "ChromeCdpRecoveredProfile" in script
    assert "ChromeCdpAutomationProfile" in script
    resolver = script[script.index("function Resolve-UserDataDir") : script.index("function Test-CdpAvailable")]
    assert "$state.user_data_dir" in resolver
    assert "Could not reuse last scraper Chrome profile from state" in resolver
    assert resolver.index("$state.user_data_dir") < resolver.index('"ChromeCdpAutomationProfile_recover_x"')
    assert resolver.index('"ChromeCdpAutomationProfile_recover_x"') < resolver.index('"ChromeCdpAutomationProfile"')
    assert resolver.index('"ChromeCdpAutomationProfile"') < resolver.index('"ChromeCdpRecoveredProfile"')
    assert "Sort-Object -Descending" not in resolver
    assert "Test-Path -LiteralPath $script:statePath" in resolver
    assert "return $lastProfile" in resolver
    assert "if (Test-Path -LiteralPath $recoverX)" in resolver
    assert "if (Test-Path -LiteralPath $automation)" in resolver
    assert "if (Test-Path -LiteralPath $recovered)" in resolver
    assert 'return $recoverX' in script
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
    assert "function Normalize-CdpTargetUrlForReuse" in script
    assert "function Find-ExistingCdpTarget" in script
    assert "function Activate-CdpTarget" in script
    assert "function Try-OpenCdpTarget" in script
    assert "function Test-ExtensionControlTargetUsable" in script
    assert "--disable-features=DisableLoadExtensionCommandLineSwitch" in script
    assert "Invoke-WebRequest -Uri \"http://127.0.0.1:$Port/json/list\"" in script
    assert "$targets = @($response.Content | ConvertFrom-Json)" in script
    assert "$desiredUrl = Normalize-CdpTargetUrlForReuse -Url $Url" in script
    assert "$targetUrl = Normalize-CdpTargetUrlForReuse -Url ([string]$target.url)" in script
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
    assert "Opened primary known extension control page" in script
    assert "Test-ExtensionControlTargetUsable -Port $Port -ExtensionId $knownId" in script
    assert "$extensionId -and $extensionId -eq $knownId -and $existingTargetId" in script
    assert '"about:blank"' in script
    assert "chrome-extension://$extensionId/$TabsUrlPath" in script
    assert "Opened extension control page" in script
    assert "Chrome CDP is already reachable" in script


def test_chrome_cdp_launcher_defaults_to_one_startup_tab_per_platform():
    script = _read_script("start-scraper-chrome-cdp.ps1")
    launch_section = script[script.index("function Get-PlatformLaunchUrls") : script.index("function Get-ChromeProcesses")]

    assert 'UC_CHROME_OPEN_EXPANDED_PLATFORM_TABS") -eq "1"' in launch_section
    assert '"https://www.tiktok.com/following"' in launch_section
    assert '"https://www.tiktok.com/foryou"' in launch_section
    assert '"https://www.lemon8-app.com/topic/singapore?region=sg"' in launch_section
    assert '"https://www.lemon8-app.com/topic/food?region=sg"' not in launch_section
    assert 'tiktok = if ($expandedPlatformTabs)' in launch_section
    assert 'lemon8 = "https://www.lemon8-app.com/topic/singapore?region=sg"' in launch_section


def test_browser_tab_audit_accepts_dynamic_extension_worlds():
    script = (REPO_ROOT / "tools" / "browser_tab_audit.py").read_text(encoding="utf-8")

    assert 'os.getenv("UC_EXTENSION_ID"' in script
    assert 'origin.startswith("chrome-extension://")' in script
    assert "UC_CHROME_CDP_PORT" in script
    assert "'9336'" in script
    assert "iso_ctx_ids: list[int]" in script
    assert "if p.get(\"cs\") is True:" in script
    assert "out[\"iso_context_id\"]" in script


def test_browser_tab_audit_uses_load_tolerant_default_deadlines():
    script = (REPO_ROOT / "tools" / "browser_tab_audit.py").read_text(encoding="utf-8")

    assert 'UC_TAB_AUDIT_RUNTIME_ENABLE_TIMEOUT_SECONDS", 4.0' in script
    assert 'UC_TAB_AUDIT_MAIN_TIMEOUT_SECONDS", 8.0' in script


def test_browser_tab_audit_detects_meta_login_walls():
    script = (REPO_ROOT / "tools" / "browser_tab_audit.py").read_text(encoding="utf-8")

    assert 'input[type=\\"password\\"]' in script
    assert "log\\\\s+in\\\\s+to\\\\s+facebook" in script
    assert "log\\\\s+in\\\\s+to\\\\s+threads" in script
    assert "threads\\\\s+log\\\\s+in" in script
    assert "health_reason='login_wall_text'" in script
    assert "use\\\\s+another\\\\s+profile" in script
    assert "health_reason='account_chooser'" in script
    assert "error=invalid_post" in script
    assert "health_reason='threads_invalid_post'" in script
    assert "\\\\bpost\\\\s+unavailable\\\\b" in script
    assert "health_reason='threads_post_unavailable'" in script


def test_browser_tab_audit_has_hard_tab_budget_assertion():
    script = (REPO_ROOT / "tools" / "browser_tab_audit.py").read_text(encoding="utf-8")

    assert "UC_TAB_AUDIT_FAIL_ON_BUDGET" in script
    assert "UC_TAB_AUDIT_EXCLUDED_PLATFORMS" in script
    assert '_csv_env("UC_TAB_AUDIT_EXCLUDED_PLATFORMS", "")' in script
    assert '"allowed": allowed' in script
    assert "extension_control_tab_count" in script
    assert "platform_tab_budget_exceeded" in script
    assert "blank_startup_tabs" in script
    assert 'results["_tab_budget"]' in script


def test_browser_tab_reload_sweeps_live_excluded_platform_targets():
    script = (REPO_ROOT / "tools" / "browser_tab_reload.py").read_text(encoding="utf-8")

    assert "PLATFORM_ALIAS_HOSTS" in script
    assert '"x": {"twitter.com", "www.twitter.com"}' in script
    assert "def _excluded_platform_for_url" in script
    assert "def _append_live_excluded_target_closures" in script
    assert "_append_live_excluded_target_closures(plan, platform_filter)" in script
    assert '"action": "close_excluded"' in script
    assert "live CDP target excluded from automatic browser maintenance" in script


def test_browser_tab_reload_treats_disappeared_targets_as_skips():
    script = (REPO_ROOT / "tools" / "browser_tab_reload.py").read_text(encoding="utf-8")

    assert "import os" in script
    assert "UC_CHROME_CDP_PORT" in script
    assert "'9336'" in script
    assert "def _target_disappeared" in script
    assert '"no such target" in text' in script
    assert '"404: not found" in text' in script
    assert '"target_disappeared"' in script
    assert "SKIP: target disappeared before reload" in script
    assert "target already disappeared before close" in script


def test_browser_tab_reload_ignores_audit_metadata_sections():
    script = (REPO_ROOT / "tools" / "browser_tab_reload.py").read_text(encoding="utf-8")

    assert 'str(plat).startswith("_")' in script
    assert "not isinstance(tabs, list)" in script


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
    assert "function Invoke-CdpPageTargetCleanup" in script
    assert "function Test-BlockedExtensionControlTab" in script
    assert "function Test-ExtensionDisabledOrCorrupted" in script
    assert "^chrome-extension://[^/]+/tabs\\.html(?:[?#].*)?$" in script
    assert "Invoke-WebRequest -Uri \"http://127.0.0.1:$script:CdpPort/json/list\"" in script
    assert "return @($response.Content | ConvertFrom-Json)" in script
    assert "Group-Object -Property url" in script
    assert "Select-Object -Skip 1" in script
    assert "closed duplicate CDP page target" in script
    assert "closed duplicate extension control tab" in script
    assert '$title -match "^chrome-extension://"' in script
    assert '$title -match "^chrome-error://"' in script
    assert "$usableTargets = @($targets | Where-Object" in script
    assert "closed blank/newtab Chrome startup tab" in script
    assert '[string]$_.url -eq "about:blank"' in script
    assert '[string]::IsNullOrWhiteSpace([string]$_.url)' in script
    assert '[string]$_.url -eq "chrome://newtab/"' in script
    assert "minimal tab cleanup while waiting for mutex failed" in script
    assert "last_repair_action" in script
    assert "X external auth/page shell is contained" in script
    assert '[string]$_.url -eq "chrome-extension://$primaryId/tabs.html"' in script
    assert '$keepId = if ($keep.Count -gt 0)' in script
    assert '^chrome-extension://pkmdmcklnjdeocoeigmlakhomhhcpafb/tabs\\.html' in script
    assert "Invoke-CdpPageTargetCleanup -Passes 2 -DelaySeconds 1" in script
    assert "Invoke-CdpPageTargetCleanup -Passes 3 -DelaySeconds 1" in script
    assert "function Ensure-ExtensionControlTab {\n    Invoke-CdpPageTargetCleanup" in script
    assert "(Test-ExtensionControlTab) -and -not (Test-BlockedExtensionControlTab)" in script
    assert "UnifiedCollector extension is disabled or corrupted in Chrome profile" in script
    assert "Remove-BlankStartupTabs" in script
    assert "Ensure-ExtensionControlTab | Out-Null\n            Invoke-CdpPageTargetCleanup" in script
    assert "Ensure-ExtensionControlTab | Out-Null\n    Invoke-CdpPageTargetCleanup" in script


def test_cleanup_ext_tabs_handles_cdp_list_timeout():
    script = _read_script("cleanup_ext_tabs.py")

    assert "UC_CHROME_CDP_URL" in script
    assert "UC_CHROME_CDP_PORT" in script
    assert "'9336'" in script
    assert "except (TimeoutError, socket.timeout)" in script
    assert "CDP target list timed out" in script
    assert "if targets is None:" in script
    assert 'parsed.scheme == "chrome-extension" and parsed.path == "/tabs.html"' in script
    assert "def is_blank_page" in script
    assert "def is_blocked_control_page" in script
    assert "title.startswith(\"chrome-extension://\")" in script
    assert 'url in {"", "about:blank", "chrome://newtab/"}' in script
    assert "closed blank/newtab" in script
    assert "blank_tabs = [t for t in targets if is_blank_page(t)]" in script
    assert "primary or known or usable" in script
    assert "to_close = [t for t in tabs if not keep_id or t.get(\"id\") != keep_id]" in script


def test_manual_extension_helpers_reuse_control_tab_instead_of_spawning_duplicates():
    helper = _read_script("cdp_ext_tabs.py")
    assert "def open_or_activate_control_tab" in helper
    assert "def close_duplicate_control_tabs" in helper
    assert "def primary_control_tab_targets" in helper
    assert "preferred_control_tab" in helper
    assert "preferred_control_tab(primary_only=True)" in helper
    assert "/json/new?" in helper

    for name in [
        "hard_reload_ext.py",
        "force_reload_ext3.py",
        "force_normalize_tabs.py",
        "test_normalize_call.py",
        "threads_sw_log.py",
    ]:
        script = _read_script(name)
        assert "open_or_activate_control_tab" in script
        assert "/json/new?chrome-extension://" not in script
        assert "Target.createTarget" not in script or "tabs.html" not in script


def test_browser_tab_reload_hard_reopens_repeatedly_stuck_tiktok_tabs():
    script = (REPO_ROOT / "tools" / "browser_tab_reload.py").read_text(encoding="utf-8")

    assert "UC_BROWSER_HARD_REOPEN_PLATFORMS" in script
    assert "UC_BROWSER_EXCLUDED_PLATFORMS" in script
    assert "UC_BROWSER_CLOSE_EXCLUDED_PLATFORM_TABS" in script
    assert "def _append_duplicate_control_tab_closures" in script
    assert "close_duplicate_control_tab" in script
    assert "duplicate extension control tab" in script
    assert "EXPANDED_PLATFORM_TABS" in script
    assert "UC_CHROME_OPEN_EXPANDED_PLATFORM_TABS" in script
    assert "UC_BROWSER_EXPANDED_PLATFORM_TABS" in script
    assert '"instagram,threads,tiktok,lemon8,facebook,strava,x"' in script
    assert 'os.getenv("UC_BROWSER_EXCLUDED_PLATFORMS", "")' in script
    assert '"close_excluded"' in script
    assert '"https://www.tiktok.com/following"' in script
    assert '"https://www.tiktok.com/foryou"' in script
    assert '"https://www.tiktok.com/explore"' in script
    assert '"lemon8": [' in script
    assert '"https://www.lemon8-app.com/topic/singapore?region=sg"' in script
    assert '"instagram": [' in script
    assert '"threads": [' in script
    assert '"x": [' in script
    assert '"facebook": [' in script
    assert '"strava": [' in script
    assert '"https://www.instagram.com/explore/"' in script
    assert '"https://www.threads.com/following"' in script
    assert '"https://x.com/home"' in script
    assert '"https://www.facebook.com/"' in script
    assert '"https://www.strava.com/dashboard"' in script
    hard_reopen_block = script.split("HARD_REOPEN_URLS = {", 1)[1].split("def _target_version", 1)[0]
    assert '"lemon8": [' in hard_reopen_block
    assert '"https://www.tiktok.com/foryou"' not in hard_reopen_block
    assert '"https://www.tiktok.com/explore"' not in hard_reopen_block
    expanded_block = script.split("if EXPANDED_PLATFORM_TABS:", 1)[1].split("HARD_REOPEN_URLS = {", 1)[0]
    assert '"https://www.tiktok.com/foryou"' in expanded_block
    assert '"https://www.tiktok.com/explore"' in expanded_block
    assert "def _platform_had_previous_unresponsive_reload" in script
    assert "def _is_canonical_x_recovery_url" in script
    assert "def _is_canonical_platform_url" in script
    assert '"x non-canonical recovery URL"' in script
    assert 'f"{platform} non-canonical platform URL"' in script
    assert "def _hard_reopen_platform" in script
    assert "reopen_urls = HARD_REOPEN_URLS.get(platform)" in script
    assert "dict.fromkeys" in script
    assert "hard_reopen_close" in script
    assert "hard_reopen_open" in script
    assert "shell_tabs = [" in script
    assert '"page health:" in str(p["reason"]).lower()' in script
    assert '"non-canonical recovery url" in str(p["reason"]).lower()' in script
    assert '"non-canonical platform url" in str(p["reason"]).lower()' in script
    assert "hard_reopen_tabs.update" in script


def test_browser_tab_reload_hard_reopens_individual_repeated_stuck_tabs():
    script = (REPO_ROOT / "tools" / "browser_tab_reload.py").read_text(encoding="utf-8")

    assert "def _hard_reopen_repeated_tabs" in script
    assert "reopen repeated stuck tab after prior soft reload" in script
    assert "_previous_reload_for_url(previous_plan, platform" in script
    assert "hard_reopen_tabs.add" in script
    assert "reopen_urls = HARD_REOPEN_URLS.get(platform)" in script
    assert "reopen_urls = list(dict.fromkeys" in script
    assert '"page health:" in text' in script
    assert '"recoverable_error_shell" in text' in script
    assert '"non-canonical recovery url" in text' in script
    assert '"non-canonical platform url" in text' in script


def test_browser_tab_reload_uses_dashboard_stale_browser_issues():
    script = (REPO_ROOT / "tools" / "browser_tab_reload.py").read_text(encoding="utf-8")

    assert "UC_DASHBOARD_HEALTH_URL" in script
    assert "def _stale_browser_issue_platforms" in script
    assert "payload.get(\"source_issues\")" in script
    assert "payload.get(\"sources\")" in script
    assert 'issue.get("source") or issue.get("platform")' in script
    assert "issue.get(\"browser_content_stale\") is True" in script
    assert "def _append_missing_stale_platform_opens" in script
    assert '"source health: stale browser content and no tab open"' in script
    assert '"open_missing"' in script
    assert '"source health: stale browser content"' in script
    assert '"stale browser content" in text' in script


def test_browser_tab_reload_uses_stable_instagram_threads_recovery_urls():
    script = (REPO_ROOT / "tools" / "browser_tab_reload.py").read_text(encoding="utf-8")

    assert '"https://www.instagram.com/explore/"' in script
    assert '"https://www.threads.com/following"' in script


def test_browser_tab_reload_honors_platform_filter():
    script = (REPO_ROOT / "tools" / "browser_tab_reload.py").read_text(encoding="utf-8")

    assert "argparse.ArgumentParser" in script
    assert '"--platforms"' in script
    assert "def _parse_platform_filter" in script
    assert "platform_filter = _parse_platform_filter(args.platforms)" in script
    assert "if platform_filter is not None and platform not in platform_filter:" in script
    assert "_stale_browser_issue_platforms(platform_filter)" in script
    assert "_append_live_excluded_target_closures(plan, platform_filter)" in script
    assert "_append_missing_stale_platform_opens(plan, stale_platforms, platform_filter)" in script


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
    assert '"https://www.tiktok.com/following"' in script
    assert '"https://www.tiktok.com/foryou"' in script
    assert '"https://www.tiktok.com/explore"' in script
    assert "lemon8 = if ($expandedPlatformTabs)" not in script
    assert '"https://www.lemon8-app.com/topic/food?region=sg"' not in script
    assert '"https://www.lemon8-app.com/topic/travel?region=sg"' not in script
    assert '"https://www.lemon8-app.com/topic/singapore?region=sg"' in script
    assert "foreach ($url in @($platforms[$id]))" in script
    assert 'strava = "https://www.strava.com/dashboard"' in script
    assert "$platforms.Keys -contains $id" in script
    assert "Open-RequestedPlatformTabs -Port $RemoteDebuggingPort" in script
    assert '$tabsParams.Add("open=$encodedIds")' in script
    assert '$tabsParams.Add("expanded=0")' in script


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


def test_browser_maintenance_loop_bounds_child_pass_runtime():
    script = _read_script("browser-tab-maintenance-loop.ps1")

    assert "UC_BROWSER_MAINTENANCE_PASS_TIMEOUT_SECONDS" in script
    assert 'Get-LoopPositiveIntEnv "UC_BROWSER_MAINTENANCE_PASS_TIMEOUT_SECONDS" 1800' in script
    assert 'Start-Process -FilePath "powershell.exe"' in script
    assert "-WindowStyle Hidden -PassThru" in script
    assert "$child.WaitForExit($timeoutMilliseconds)" in script
    assert "maintenance pass timed out after ${passTimeoutSeconds}s" in script
    assert "Stop-Process -Id $child.Id -Force" in script
    assert "function Stop-MaintenanceChildProcess" in script
    assert 'taskkill.exe" /PID $ChildPid /F /T' in script
    assert "Update-LoopStatusMetadata" in script
    assert 'Set-StatusProperty $status "state" "running"' in script
    assert "Complete-LoopStatusMetadata" in script
    assert 'Write-LoopStatus "failed" "maintenance pass timed out after ${passTimeoutSeconds}s" $child.Id' in script
    assert 'Complete-LoopStatusMetadata "maintenance loop sleeping after successful pass" $child.Id "ok"' in script
    assert 'Complete-LoopStatusMetadata "maintenance loop sleeping after CDP-unavailable pass" $child.Id "cdp_unavailable"' in script
    assert 'Complete-LoopStatusMetadata "maintenance loop sleeping after nonzero pass" $child.Id "degraded"' in script


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
    assert "-TimeoutSeconds 300" in script
    assert "collector-controlled unreachable CDP Chrome" in script
    assert "-FallbackOpenControlIfCleanupBlocked" not in script
    assert "-IsolateExtensions" in script
    assert "-OpenIds instagram,tiktok,lemon8,threads,facebook,strava,x" in script
    assert '"instagram,tiktok,threads,facebook,strava", "-NoTest"' not in script
    assert "Chrome CDP repair succeeded; continuing maintenance pass" in script
    assert "chrome_cdp_available" in script
    assert "browser tab maintenance final CDP repair before degrade" in script
    assert '"final_cdp_repair"' in script


def test_browser_maintenance_refuses_overlapping_passes():
    script = _read_script("browser-tab-maintenance.ps1")

    assert "Global\\UnifiedCollectorBrowserTabMaintenance" in script
    assert "$mutex.WaitOne(0)" in script
    assert "another pass is already running" in script
    assert "another maintenance pass is already running; previous terminal state retained" in script
    assert 'Write-Status "overlap_skipped"' in script
    assert 'Write-Status "ok" "another maintenance pass is already running' not in script
    assert '"overlap_skipped"' in script
    assert "$mutex.ReleaseMutex()" in script


def test_browser_maintenance_uses_load_tolerant_wrapper_timeouts():
    script = _read_script("browser-tab-maintenance.ps1")

    assert 'UC_BROWSER_AUDIT_TIMEOUT_SECONDS" 120' in script
    assert 'UC_BROWSER_RELOAD_TIMEOUT_SECONDS" 120' in script
    assert 'UC_BROWSER_PROFILE_RESTART_SETTLE_SECONDS" 60' in script


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
    assert 'Set-DefaultEnv "UC_DASHBOARD_HEALTH_TIMEOUT_SECONDS" "5"' in script
    assert "without pinning the machine" in script


def test_browser_maintenance_restarts_dedicated_profile_when_tabs_stay_unhealthy():
    script = _read_script("browser-tab-maintenance.ps1")

    assert "function Get-AuditHealth" in script
    assert "function Get-RequiredAuditPlatforms" in script
    assert '"UC_BROWSER_REQUIRED_PLATFORMS"' in script
    assert '@("instagram", "threads", "tiktok", "x", "facebook", "strava")' in script
    assert '$knownPlatforms = @("instagram", "threads", "tiktok", "x", "facebook", "strava")' in script
    assert "UC_BROWSER_MIN_HEALTHY_PLATFORMS" in script
    assert 'Get-PositiveIntEnv "UC_BROWSER_MIN_HEALTHY_PLATFORMS" $platforms.Count' in script
    assert "function Test-AuthWallUrl" in script
    assert "function Get-AuditTabUrl" in script
    assert "function Test-AuditTabContentWall" in script
    assert "function Test-AuditHealthCoveredBySourceLiveness" in script
    assert "page_health_status" in script
    assert "recoverable_error_shell" in script
    assert "missing_or_stopped_content_script" in script
    assert "source-liveness fallback rejected: browser tab is missing or stopped content script" in script
    assert "source-liveness fallback rejected: browser tab is on an auth/login shell" in script
    assert "function Test-AuditHealthItemIsSoftRecoverableShell" in script
    assert "function Test-AuditHealthItemIsTargetedRepairOnly" in script
    assert "reason=try_again_empty_state" in script
    assert "tab-local browser shell covered by fresh source liveness" in script
    assert "source_liveness_fallback" in script
    assert "browser_content_stale -eq $true" in script
    assert "tab_budget:" in script
    assert "tab budget is still violated after targeted cleanup; skipping profile restart" in script
    assert "targeted tab/extension repairs are contained; skipping profile restart" in script
    assert "/i/flow/login" in script
    assert "redirect_after_login" in script
    assert "-not (Test-AuthWallUrl (Get-AuditTabUrl $_))" in script
    assert "-not (Test-AuditTabContentWall $_)" in script
    assert "function Invoke-ScraperChromeProfileRestart" in script
    assert "function Resolve-PreferredChromePath" in script
    assert "chrome-win64\\chrome.exe" in script
    assert "function Invoke-ChromeLauncher" in script
    assert "-ChromePath" in script
    assert "Chrome launcher timed out after" in script
    assert "[int[]]$AllowedExitCodes = @(0)" in script
    assert "-AllowedExitCodes @(0, 2)" in script
    assert "-CloseExistingCdpProfile" in script
    assert "-CloseExistingIfNoVisibleWindows" in script
    assert "dedicated scraper Chrome restart left CDP unavailable; fallback repair reason=" in script
    assert "running second targeted browser tab reload pass before profile restart" in script
    assert "browser tab audit health after second reload" in script
    assert "browser tab audit still unhealthy after second reload" in script
    assert "function Test-AuditHealthNeedsProfileRestart" in script
    assert "UC_BROWSER_PROFILE_RESTART_ON_TAB_HEALTH" in script
    assert "profile restart on tab-health failure is disabled unless UC_BROWSER_PROFILE_RESTART_ON_TAB_HEALTH=1" in script
    assert "UC_BROWSER_PROFILE_RESTART_MIN_UNHEALTHY_PLATFORMS" in script
    assert 'Get-PositiveIntEnv "UC_BROWSER_PROFILE_RESTART_MIN_UNHEALTHY_PLATFORMS" 1' in script
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
    assert "auth_challenge|logout=|login_wall_text|account_chooser" in script
    assert "Test-AuditHealthItemIsSoftRecoverableShell $text" in script
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
    assert "http_429" in script
    assert "http\\\\s+error\\\\s+429" in script
    assert "threads_invalid_post" in script
    assert "threads_post_unavailable" in script
    assert "auth_challenge" in script
    assert "recaptcha" in script
    assert 'low&&document.querySelector(\'iframe[src*=\\"recaptcha\\"]\')' in script


def test_browser_audit_marks_disappeared_cdp_targets_as_transient():
    script = (REPO_ROOT / "tools" / "browser_tab_audit.py").read_text(encoding="utf-8")

    assert "def _target_disappeared" in script
    assert '"no such target id" in text' in script
    assert '"404: not found" in text' in script
    assert '"target_disappeared": False' in script
    assert 'out["target_disappeared"] = True' in script
    assert "target disappeared before audit" in script

# --- A3a/A3b functional tests (importlib: tools/ is not a package) ---

def _load_tab_reload_module():
    import importlib.util

    path = REPO_ROOT / "tools" / "browser_tab_reload.py"
    spec = importlib.util.spec_from_file_location("_tab_reload_under_test", str(path))
    mod = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_x_failed_script_query_is_non_canonical():
    mod = _load_tab_reload_module()
    assert not mod._is_canonical_x_recovery_url("https://x.com/explore?failedScript=")
    assert not mod._is_canonical_x_recovery_url("https://x.com/oopspwned?uc_recover=1787674742")
    assert mod._is_canonical_x_recovery_url("https://x.com/home")
    assert mod._is_canonical_x_recovery_url("https://x.com/explore")


def test_instagram_429_reload_respects_cooldown(monkeypatch):
    import time as _time

    mod = _load_tab_reload_module()
    monkeypatch.setenv("UC_TAB_RELOAD_429_COOLDOWN_MINUTES", "75")
    tab = {
        "platform": "instagram",
        "url": "https://www.instagram.com/p/ABC123/",
        "page_health_status": "recoverable_error_shell",
        "page_health_reason": "http_429",
    }
    recent = [{
        "platform": "instagram",
        "action": "reload",
        "reason": "page health: http_429",
        "ts": _time.time() - 300,
    }]
    need, why = mod._decide_reload(tab, "1.23.72", None, previous_reloads=recent)
    assert need is False
    assert "cooldown" in why

    stale = [{**recent[0], "ts": _time.time() - 80 * 60}]
    need2, why2 = mod._decide_reload(tab, "1.23.72", None, previous_reloads=stale)
    assert need2 is True
    assert "cooldown" not in why2


def test_consecutive_shell_cycles_counts_recent_stale_reloads():
    mod = _load_tab_reload_module()
    previous = [
        {"platform": "x", "action": "reload", "reason": "x non-canonical recovery URL", "status": "ok"},
        {"platform": "x", "action": "reload", "reason": "x non-canonical recovery URL", "status": "fail"},
        {"platform": "instagram", "action": "reload", "reason": "page health: http_429", "status": "ok"},
        {"platform": "x", "action": "skip", "reason": "healthy"},
    ]
    assert mod._consecutive_shell_cycles(previous, "x") == 2
    assert mod._consecutive_shell_cycles(previous, "instagram") == 1
    assert mod._consecutive_shell_cycles([], "x") == 0
