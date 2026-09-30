"""TikTok URL-ID -> UTC timestamp decoder.

Source: tools.myosint.training bookmarklet "TikTok URL Date Decoder"
(attributed there to Bellingcat's original tool). TikTok video/photo
post IDs are 64-bit numbers whose high 32 bits are seconds since the
UNIX epoch directly (not an offset from a custom TikTok epoch as
originally claimed). Verified empirically against 10 rows of live
tiktok_posts data (2026-09-30): high32(pid) matched create_time within
a few seconds for every row.

Decoded timestamps are stored in `tiktok_posts.id_decoded_utc` alongside
the scraped `create_time`. They give us a ground-truth creation time even
when the scrape returns a missing or wrong `create_time`.

Reference: Griffin (@hatless1der) tools.myosint.training bookmarklet
library, "TikTok URL Date Decoder Bookmarklet" (2026-07-15 v1).
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional

# TikTok started in 2016; guard against pre-existing epoch timestamps.
_MIN_UNIX_TS = 1_451_606_400  # 2016-01-01 00:00:00 UTC

# One year of future slack for clock-skew tolerance during backfills.
_FUTURE_SLACK_SECONDS = 86_400 * 365

# Legacy alias for anyone importing this constant.
TIKTOK_EPOCH_UNIX = _MIN_UNIX_TS


def tiktok_id_to_utc(post_id: object) -> Optional[datetime]:
    """Decode a TikTok post ID into its creation UTC timestamp.

    Returns None on any of:
    - non-numeric / empty / negative input
    - decoded seconds <= 0 (short/malformed ID)
    - decoded timestamp before 2016-01-01 (before TikTok existed)
    - decoded timestamp more than a year in the future (clock-skew guard)
    """
    if post_id is None:
        return None

    # Accept int, str-of-digits, or anything with an int() coercion.
    try:
        pid = int(str(post_id).strip())
    except (TypeError, ValueError):
        return None

    if pid <= 0:
        return None

    unix_ts = pid >> 32
    if unix_ts <= 0:
        return None

    now_ts = int(time.time())
    if unix_ts < _MIN_UNIX_TS or unix_ts > now_ts + _FUTURE_SLACK_SECONDS:
        return None

    return datetime.fromtimestamp(unix_ts, tz=timezone.utc)


__all__ = ["TIKTOK_EPOCH_UNIX", "tiktok_id_to_utc"]
