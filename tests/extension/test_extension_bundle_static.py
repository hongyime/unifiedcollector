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
