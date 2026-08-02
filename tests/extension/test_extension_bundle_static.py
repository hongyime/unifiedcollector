import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _read(relpath: str) -> str:
    return (ROOT / relpath).read_text(encoding="utf-8")


def test_extension_expected_version_matches_manifest():
    manifest = json.loads(_read("extension/manifest.json"))
    compose = _read("docker/docker-compose.yml")

    versions = re.findall(r'UC_EXTENSION_EXPECTED_VERSION:\s*"([^"]+)"', compose)

    assert versions
    assert set(versions) == {manifest["version"]}


def test_content_script_can_be_reinjected_without_top_level_redeclare():
    content = _read("extension/content.js").strip()

    assert content.startswith("(() => {")
    assert content.endswith("})();")
    assert "const UC_CONTENT_INSTALL_ID" in content
    assert "UC_CONTENT_STATE.running = false" in content
    assert "UC_CONTENT_STATE.superseded_by" in content
    assert "function ucContentScriptCurrent()" in content
    assert "while (LOOP_RUNNING && ucContentScriptCurrent())" in content


def test_x_twitter_alias_is_registered_for_tabs_and_background():
    background = _read("extension/background.js")
    platforms = _read("extension/platforms.js")

    assert 'aliasHosts: ["twitter.com"]' in background
    assert 'aliasHosts: ["twitter.com"]' in platforms
    assert "function platformHosts(p)" in background
    assert "function platformUrlPatterns(p)" in background
    assert "platformHosts(p).includes(host)" in background


def test_x_failed_script_url_is_canonicalized():
    background = _read("extension/background.js")
    content = _read("extension/content.js")

    assert 'u.searchParams.has("failedScript")' in background
    assert 'reason: "failed_script_url"' in content
    assert 'location.href = "https://x.com/home"' in content


def test_scraper_refresh_runs_before_dashboard_content_stale_window():
    background = _read("extension/background.js")

    watchdog = re.search(r"const WATCHDOG_MIN = (\d+);", background)
    refresh = re.search(r"const REFRESH_MIN = (\d+);", background)

    assert watchdog
    assert refresh
    assert int(watchdog.group(1)) <= 10
    assert int(refresh.group(1)) < 60


def test_scraper_heartbeat_summary_is_not_blocked_by_recovery():
    background = _read("extension/background.js")
    heartbeat_fn = background.split("async function reportScraperTabHeartbeats", 1)[1].split(
        "async function reportScraperHeartbeatSummary", 1
    )[0]
    page_health_fn = background.split("async function recordPageHealth", 1)[1].split(
        "function recoveryDelayMs", 1
    )[0]

    assert "function scheduleMaybeForceScrapeCycle" in background
    assert "scheduleMaybeForceScrapeCycle(" in heartbeat_fn
    assert "await maybeForceScrapeCycle(" not in heartbeat_fn
    assert "scheduleMaybeForceScrapeCycle(" in page_health_fn
    assert "await maybeForceScrapeCycle(" not in page_health_fn


def test_content_script_recovery_bounds_tab_messages():
    background = _read("extension/background.js")

    assert "const TAB_MESSAGE_TIMEOUT_MS = 30000" in background
    assert "const FORCED_CYCLE_RELOAD_DEBOUNCE_MS = 4 * 60 * 1000" in background
    assert "const FORCED_CYCLE_FAILURE_DEBOUNCE_MS = 90 * 1000" in background
    assert "async function sendTabMessageWithTimeout" in background
    assert background.count("sendTabMessageWithTimeout(") >= 5
    assert "new Error(\"tab message timed out\")" in background
    assert "function isTabMessageTimeout" in background
    no_receiver_block = background.split("function isNoReceiverError", 1)[1].split(
        "function isTabMessageTimeout", 1
    )[0]
    assert "tab message timed out" not in no_receiver_block


def test_tab_message_timeouts_do_not_trigger_receiver_missing_refresh():
    background = _read("extension/background.js")

    assert "content_script_programmatic_nudge_timed_out" in background
    assert "forced_cycle_request_timed_out" in background
    assert "content_script_message_timeout" in background
    assert "message_timeout_programmatic_inject" in background
    assert "MESSAGE_TIMEOUT_STALE_RELOAD_SECONDS_BY_PLATFORM" in background
    assert "function messageTimeoutStaleReloadSeconds" in background

    forced_catch = background.split("const messageTimedOut = isTabMessageTimeout(firstErr);", 1)[1].split(
        "await recordServiceWorkerRecovery(base, tab, platform, \"forced_cycle_request_failed\"",
        1,
    )[0]
    ensure_catch = background.split("if (platform && messageTimedOut)", 1)[1].split(
        "else if (platform && receiverMissing)",
        1,
    )[0]

    assert "return;" in forced_catch
    assert "contentAgeSeconds >= staleReloadSeconds" in forced_catch
    assert "\"message_timeout_content_stale\"" in forced_catch
    assert "\"message_timeout_stale_refresh\"" in forced_catch
    assert "injectContentScriptAndNudge(base, tab, platform, \"forced_cycle_message_timeout\"" in forced_catch
    assert "reinject_attempted: true" in forced_catch
    assert "injectContentScriptAndNudge(base, t, platform, \"ensure_loop_message_timeout\"" in ensure_catch
    assert "refreshTabForMissingContentScript" not in ensure_catch


def test_recoverable_page_shells_try_native_retry_before_waiting():
    content = _read("extension/content.js")

    assert "function findRecoverablePageActionButton" in content
    assert "async function attemptRecoverablePageInteraction" in content
    assert "await attemptRecoverablePageInteraction(p.id, shell)" in content


def test_facebook_scrape_pass_is_bounded_but_not_reload_happy():
    content = _read("extension/content.js")

    one_shot_block = content.split("const ONE_SHOT_TIMEOUT_MS_BY_PLATFORM = {", 1)[1].split(
        "};", 1
    )[0]
    loop_block = content.split("const LOOP_CYCLE_TIMEOUT_MS_BY_PLATFORM = {", 1)[1].split(
        "};", 1
    )[0]
    facebook_block = content.split("const facebook = {", 1)[1].split(
        "const strava = {", 1
    )[0]

    assert re.search(r"facebook:\s*5\s*\*\s*60\s*\*\s*1000", one_shot_block)
    assert re.search(r"facebook:\s*7\s*\*\s*60\s*\*\s*1000", loop_block)
    assert "autoScroll(forcedRecovery ? 4 : 7" in facebook_block


def test_background_forced_recovery_waits_longer_than_content_one_shot():
    background = _read("extension/background.js")
    content = _read("extension/content.js")

    reload_block = background.split("const FORCED_CYCLE_HARD_RELOAD_MS_BY_PLATFORM = {", 1)[1].split(
        "};", 1
    )[0]
    one_shot_block = content.split("const ONE_SHOT_TIMEOUT_MS_BY_PLATFORM = {", 1)[1].split(
        "};", 1
    )[0]
    fallback = re.search(
        r"return\s+FORCED_CYCLE_HARD_RELOAD_MS_BY_PLATFORM\[platformId\]\s*\|\|\s*(\d+)\s*\*\s*60\s*\*\s*1000",
        background,
    )
    assert fallback

    def minutes(block: str, platform: str, default: int | None = None) -> int:
        match = re.search(rf"{platform}:\s*(\d+)\s*\*\s*60\s*\*\s*1000", block)
        if match:
            return int(match.group(1))
        assert default is not None
        return default

    hard_reload_fallback_min = int(fallback.group(1))
    for platform in ("instagram", "strava", "tiktok", "lemon8", "threads", "x", "facebook"):
        assert minutes(reload_block, platform, hard_reload_fallback_min) > minutes(one_shot_block, platform)


def test_x_page_recovery_limit_cools_then_retries():
    background = _read("extension/background.js")

    assert "const PAGE_RECOVERY_LIMIT_COOLDOWN_MS_BY_PLATFORM = {" in background
    assert "x: 10 * 60 * 1000" in background
    assert "limitUntil" in background
    assert "attempt_limit_cooling" in background


def test_post_reload_scrape_nudge_waits_and_retries_for_heavy_tabs():
    background = _read("extension/background.js")

    delay_block = background.split("const POST_RELOAD_NUDGE_DELAY_MS_BY_PLATFORM = {", 1)[1].split(
        "};", 1
    )[0]
    retry_block = background.split("const POST_RELOAD_NUDGE_RETRY_DELAY_MS_BY_PLATFORM = {", 1)[1].split(
        "};", 1
    )[0]

    assert re.search(r"tiktok:\s*75000", delay_block)
    assert re.search(r"x:\s*75000", delay_block)
    assert re.search(r"tiktok:\s*90000", retry_block)
    assert re.search(r"x:\s*90000", retry_block)
    assert "post_reload_scrape_nudge_retry_scheduled" in background
    assert "post_reload_retry: true" in background


def test_facebook_has_post_text_fallback_when_permalink_ids_are_missing():
    content = _read("extension/content.js")
    facebook_block = content.split("function harvestFacebookPosts(entity)", 1)[1].split(
        "// Facebook", 1
    )[0]

    assert "function facebookPostIdFromHref" in content
    assert "function facebookAuthorFromArticle" in content
    assert "fbdom_" in facebook_block
    assert 'metadata: {' in facebook_block
    assert 'source: linkId ? "facebook_dom_article" : "facebook_dom_article_fallback"' in facebook_block
    assert "const posts = harvestFacebookPosts(entity)" in content


def test_stalled_scrape_passes_are_force_cleared_on_timeout():
    content = _read("extension/content.js")
    timeout_table = content.split("const TIMEOUT_RELOAD_STREAK_BY_PLATFORM = {", 1)[1].split("};", 1)[0]

    assert "function forceClearScrapePass()" in content
    assert "forceClearScrapePass();" in content
    assert "scrape_pass_forced_clear: true" in content
    assert re.search(r"tiktok:\s*1", timeout_table)
    assert re.search(r"x:\s*1", timeout_table)
    assert re.search(r"facebook:\s*1", timeout_table)


def test_direct_fallback_fetches_are_bounded():
    content = _read("extension/content.js")

    direct_block = content.split("async function directSendFallback", 1)[1].split(
        "async function send(", 1
    )[0]

    assert "const DEFAULT_DIRECT_SEND_TIMEOUT_MS = 20000" in content
    assert "const DIRECT_SEND_TIMEOUT_MS_BY_TYPE" in content
    assert "function directSendTimeoutMs(msg)" in content
    assert "UCDirectSendTimeout" in direct_block
    assert "withDeadline(" in direct_block
    assert "fetch(DIRECT_INGEST_BASE + request.path" in direct_block


def test_browser_recovery_optional_writes_are_nonblocking():
    content = _read("extension/content.js")
    lemon8_block = content.split("const lemon8 = {", 1)[1].split(
        "// ===========================================================================\n// Twitter / X",
        1,
    )[0]
    x_block = content.split("const x = {", 1)[1].split("function harvestFacebookPosts", 1)[0]
    facebook_block = content.split("const facebook = {", 1)[1].split(
        "const STRAVA_ROUTE_NAV_MIN_MS", 1
    )[0]

    assert "function sendSideEffect" in content
    assert "Lemon8 author write" in lemon8_block
    assert "Lemon8 ${entity} forced media write" in lemon8_block
    assert "X seen-user write" in x_block
    assert "Facebook seen-user write" in facebook_block
    assert "forcedRecovery\n      ? (sendSideEffect(" in lemon8_block
    assert "await send(ingestPayload, { timeoutMs: 45000 })" in lemon8_block
    assert "sendSideEffect(usersPayload, \"lemon8\"" in lemon8_block
    assert 'send({ type: "posts", platform: "x"' not in x_block
    assert 'await send({ type: "posts", platform: "facebook"' not in facebook_block
    assert "{ timeoutMs: forcedRecovery ? 30000 : 45000 }" in x_block
    assert "{ timeoutMs: forcedRecovery ? 30000 : 45000 }" in facebook_block


def test_forced_recovery_probe_does_not_block_scrape_cycle():
    content = _read("extension/content.js")
    probe_block = content.split("async function reportBrowserRecoveryProbe", 1)[1].split(
        "function browserMediaRevisitUrlOk", 1
    )[0]

    assert "probe_reason: \"forced_recovery_started\"" in probe_block
    assert "send({" in probe_block
    assert "return send({" not in probe_block
    assert "await send({" not in probe_block
    assert "{ timeoutMs: 8000 }" in probe_block
    assert "return null;" in probe_block


def test_x_error_shell_can_switch_host_when_native_retry_is_missing():
    content = _read("extension/content.js")
    recover_block = content.split("async function attemptRecoverablePageInteraction", 1)[1].split(
        "// Capture is ALWAYS ON", 1
    )[0]
    switch_block = content.split("function switchXHostForRecoverableShell", 1)[1].split(
        "async function attemptRecoverablePageInteraction", 1
    )[0]

    assert "function switchXHostForRecoverableShell" in content
    assert "uc_x_shell_nav_" in switch_block
    assert '"https://twitter.com/home"' in switch_block
    assert '"https://x.com/home"' in switch_block
    assert "switching host to recover" in switch_block
    assert "last && Date.now() - last < 10 * 60000" in recover_block
