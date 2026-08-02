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
