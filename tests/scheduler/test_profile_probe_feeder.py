from __future__ import annotations

import re

from src.scheduler.handlers.profile_probe_feeder import _FEED_SQL


def test_airbnb_feeder_requires_a_direct_numeric_user_id_link():
    airbnb_rule = next(
        line for line in _FEED_SQL.splitlines() if "('airbnb'" in line
    )
    assert re.search(r"/users/show/\(\[0-9\]\{2,20\}\)", airbnb_rule)
    social_candidates = _FEED_SQL.split("social_candidates AS (")[1].split("),\nraw_candidates", 1)[0]
    assert "('airbnb')" not in social_candidates


def test_cross_platform_probe_budget_counts_only_supported_social_targets():
    assert "existing.source IN ('snapchat', 'paypal', 'pinterest')" in _FEED_SQL
    assert "HAVING count(DISTINCT existing.source) < 3" in _FEED_SQL
    social_candidates = _FEED_SQL.split("social_candidates AS (")[1].split("),\nraw_candidates", 1)[0]
    assert "('bluesky')" not in social_candidates


def test_bluesky_feeder_accepts_only_direct_full_handles_or_dids():
    bluesky_rule = next(
        line for line in _FEED_SQL.splitlines() if "('bluesky'" in line
    )
    assert "bsky\\.app/profile/" in bluesky_rule
    assert "did:plc:" in bluesky_rule
    validation = re.search(
        r"source = 'bluesky' AND username ~ '([^']+)'", _FEED_SQL,
    )
    assert validation is not None
    pattern = validation.group(1)
    assert re.fullmatch(pattern, "alice") is None
    assert re.fullmatch(pattern, "alice.bsky.social")
    assert re.fullmatch(pattern, "did:plc:abcdefghijklmnopqrstuvwx")
