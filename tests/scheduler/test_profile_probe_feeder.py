from __future__ import annotations

import re

from src.scheduler.handlers.profile_probe_feeder import (
    _BLUESKY_CANDIDATES_SQL,
    _FEED_SQL,
)


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


def test_bluesky_candidates_are_resolved_before_they_reach_the_probe_queue():
    bluesky_rule = next(
        line for line in _FEED_SQL.splitlines() if "('bluesky'" in line
    )
    assert "bsky\\.app/profile/" in bluesky_rule
    assert "did:plc:" in bluesky_rule
    assert "WHERE source <> 'bluesky'" in _FEED_SQL
    assert "FROM failed_queue_candidates" in _BLUESKY_CANDIDATES_SQL
    assert "FROM social_candidates" in _BLUESKY_CANDIDATES_SQL
    assert "bluesky_handle_resolution_cache" in _BLUESKY_CANDIDATES_SQL
