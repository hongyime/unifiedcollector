"""Non-secret realtime group-feed policy shared by dashboard and control room."""
from __future__ import annotations

import os


def get_realtime_feed_policy() -> dict:
    excluded = sorted({
        item.strip().lower()
        for item in os.getenv("REALTIME_POST_FEED_EXCLUDED_SOURCES", "").split(",")
        if item.strip()
    })
    enabled = os.getenv("REALTIME_POST_FEED_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}
    return {
        "enabled": enabled,
        "excluded_sources": excluded,
        "whatsapp_group_media_excluded": {"whatsapp", "beeper_whatsapp"}.issubset(excluded),
    }
