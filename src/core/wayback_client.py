"""Archive.org Wayback Machine client (Explore #3).

Read-only. Public API. No auth. Fetches the closest snapshot to a
given URL and returns the snapshot metadata + raw HTML.

Rate limits: Wayback is polite; we self-throttle to 1 QPS. On 429 we
bail the cycle and retry next time.

Griffin's Telegram OSINT posts (2022-06-24 / 2022-08-03) mention
archive.org fallback as the recovery path for deleted channels /
messages. This client implements that path.
"""
from __future__ import annotations

import gzip
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_WAYBACK_AVAIL_API = "https://archive.org/wayback/available"
_WAYBACK_TIMEOUT = 15.0


@dataclass
class WaybackSnapshot:
    target_url: str
    snapshot_url: Optional[str] = None
    snapshot_captured_at: Optional[datetime] = None
    raw_html_gzip: Optional[bytes] = None
    content_hash: Optional[bytes] = None
    error: Optional[str] = None


async def lookup(client: httpx.AsyncClient, target_url: str) -> WaybackSnapshot:
    """Ask Wayback for the closest snapshot of target_url.

    Returns metadata + gzipped HTML if a snapshot exists; error on
    network / rate-limit / no-snapshot cases.
    """
    result = WaybackSnapshot(target_url=target_url)
    try:
        r = await client.get(
            _WAYBACK_AVAIL_API,
            params={"url": target_url},
            timeout=_WAYBACK_TIMEOUT,
        )
    except Exception as exc:
        result.error = f"http:{type(exc).__name__}"
        return result

    if r.status_code == 429:
        result.error = "rate_limited"
        return result
    if r.status_code >= 400:
        result.error = f"http:{r.status_code}"
        return result

    try:
        payload = r.json()
    except Exception:
        result.error = "bad_json"
        return result

    closest = (payload.get("archived_snapshots") or {}).get("closest") or {}
    snap_url = closest.get("url")
    if not snap_url or not closest.get("available"):
        result.error = "no_snapshot"
        return result

    result.snapshot_url = snap_url
    ts = closest.get("timestamp")
    if ts and len(ts) >= 8:
        try:
            result.snapshot_captured_at = datetime.strptime(ts[:14].ljust(14, "0"), "%Y%m%d%H%M%S")
        except ValueError:
            result.snapshot_captured_at = None

    # Fetch the snapshot content.
    try:
        r2 = await client.get(snap_url, timeout=_WAYBACK_TIMEOUT, follow_redirects=True)
    except Exception as exc:
        result.error = f"snapshot_http:{type(exc).__name__}"
        return result

    if r2.status_code >= 400:
        result.error = f"snapshot_http:{r2.status_code}"
        return result

    body = r2.content or b""
    if len(body) > 5 * 1024 * 1024:  # cap 5MB
        body = body[: 5 * 1024 * 1024]
    result.raw_html_gzip = gzip.compress(body, compresslevel=6)
    result.content_hash = hashlib.sha256(body).digest()
    return result


__all__ = ["WaybackSnapshot", "lookup"]
