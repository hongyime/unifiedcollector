"""TikTok URL-ID -> UTC timestamp decoder.

Source: tools.myosint.training bookmarklet "TikTok URL Date Decoder"
(attributed there to Bellingcat's original tool). TikTok video/photo
post IDs are 64-bit numbers whose high 32 bits are seconds elapsed
since the TikTok epoch (2016-06-01 00:00:00 UTC).

Decoded timestamps are stored in `tiktok_posts.id_decoded_utc` alongside
the scraped `created_at`. They give us a ground-truth creation time even
when the scrape returns a missing or wrong `created_at`.

Reference: Griffin (@hatless1der) tools.myosint.training bookmarklet
library, "TikTok URL Date Decoder Bookmarklet" (2026-07-15 v1).
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional

# TikTok epoch = 2016-06-01 00:00:00 UTC (Bellingcat's calibration,
# stable since 2016). High 32 bits of the 64-bit ID = seconds since
# this epoch.
TIKTOK_EPOCH_UNIX = 1_464_739_200  # 2016-06-01 00:00:00 UTC

# One year of future slack for clock-skew tolerance during backfills.
_FUTURE_SLACK_SECONDS = 86_400 * 365


def tiktok_id_to_utc(post_id: object) -> Optional[datetime]:
    """Decode a TikTok post ID into its creation UTC timestamp.

    Returns None on any of:
    - non-numeric / empty / negative input
    - decoded seconds <= 0 (short/malformed ID)
    - decoded timestamp before the TikTok epoch
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

    seconds_since_epoch = pid >> 32
    if seconds_since_epoch <= 0:
        return None

    unix_ts = TIKTOK_EPOCH_UNIX + seconds_since_epoch
    now_ts = int(time.time())
    if unix_ts < TIKTOK_EPOCH_UNIX or unix_ts > now_ts + _FUTURE_SLACK_SECONDS:
        return None

    return datetime.fromtimestamp(unix_ts, tz=timezone.utc)


__all__ = ["TIKTOK_EPOCH_UNIX", "tiktok_id_to_utc"]
