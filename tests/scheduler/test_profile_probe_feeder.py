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
    assert "existing.source IN ('snapchat', 'paypal', 'bluesky', 'pinterest')" in _FEED_SQL
    assert "HAVING count(DISTINCT existing.source) < 4" in _FEED_SQL
