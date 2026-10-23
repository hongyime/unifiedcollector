"""The profile-probe anchor must not look like extra services."""
from __future__ import annotations

from scripts.verify_env_split import parse_compose

_PROBES = (
    "collector_snapchat",
    "collector_paypal",
    "collector_airbnb",
    "collector_bluesky",
    "collector_pinterest",
)


def test_profile_anchor_is_applied_to_each_service() -> None:
    env_files, inline_env = parse_compose()
    assert "volumes" not in env_files
    assert "depends_on" not in env_files
    assert "env_file" not in env_files
    assert "collector" not in env_files
    for name in _PROBES:
        assert any(entry.endswith("common.env") for entry in env_files[name])
        source = name.removeprefix("collector_")
        probe = source.upper() + "_PROBE_ENABLED"
        if source == "paypal":
            # PayPal is intentionally always-enabled, so it has no opt-out gate.
            assert probe not in inline_env[name]
        else:
            assert probe in inline_env[name]
