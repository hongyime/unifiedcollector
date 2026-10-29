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

# Paypal has no explicit probe gate: compose does not set PAYPAL_PROBE_ENABLED
# and no code reads it, so Paypal probing is always on. Only the services below
# set an explicit <SOURCE>_PROBE_ENABLED gate.
_EXPLICIT_PROBE_GATES = (
    "collector_snapchat",
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
    for name in _EXPLICIT_PROBE_GATES:
        probe = name.removeprefix("collector_").upper() + "_PROBE_ENABLED"
        assert probe in inline_env[name]
