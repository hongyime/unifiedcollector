"""Resolve Bluesky handles before adding profile-probe targets."""
from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Sequence
from typing import Any

import httpx

_RESOLVE_HANDLE_URL = (
    "https://bsky.social/xrpc/com.atproto.identity.resolveHandle"
)
_CACHE_TTL_DAYS = 7
_REQUEST_INTERVAL_SECONDS = 1.0
_HANDLE_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$"
)
_DID_RE = re.compile(r"^did:(?:plc:[a-z0-9]{10,}|web:[a-z0-9._:%-]+)$", re.I)


def _canonical_handle(username: str, *, cross_platform: bool) -> str:
    """Return the handle to resolve; cross-platform usernames use bsky.social."""
    value = username.strip().lstrip("@").lower()
    if cross_platform or "." not in value:
        value = f"{value}.bsky.social"
    return value


def _valid_handle(handle: str) -> bool:
    return len(handle) <= 253 and bool(_HANDLE_RE.fullmatch(handle))


def _valid_did(did: Any) -> bool:
    return isinstance(did, str) and bool(_DID_RE.fullmatch(did))


async def _resolve_handle(
    client: httpx.AsyncClient, handle: str,
) -> tuple[str, str | None]:
    """Resolve one qualified handle, returning (outcome, DID)."""
    response = await client.get(_RESOLVE_HANDLE_URL, params={"handle": handle})
    if response.status_code in (400, 404):
        return "unresolvable", None
    if response.status_code != 200:
        return "deferred", None

    try:
        did = response.json().get("did")
    except (ValueError, AttributeError):
        return "deferred", None
    if not _valid_did(did):
        return "deferred", None
    return "resolved", did


async def resolve_and_enqueue_bluesky_candidates(
    pool: Any,
    candidates: Sequence[Any],
    *,
    client_factory: Callable[..., Any] = httpx.AsyncClient,
    request_interval_seconds: float = _REQUEST_INTERVAL_SECONDS,
) -> dict[str, int]:
    """Resolve handles and enqueue only DIDs accepted by the public API.

    Definitive misses are cached separately from ``profile_probe_queue`` so
    they are never presented to a profile probe and are not re-requested every
    feeder cycle. Transient HTTP/network failures are not cached.
    """
    summary = {"resolved": 0, "unresolvable": 0, "queued": 0, "deferred": 0}
    cache_rows: dict[str, str | None] = {}
    did_rows: dict[str, str] = {}
    attempted_handles: set[str] = set()

    handle_candidates: list[tuple[str, str]] = []
    for candidate in candidates:
        username = candidate["username"]
        enqueued_by = candidate["enqueued_by"] or "profile_probe_feeder"
        cross_platform = bool(candidate["cross_platform"])
        value = username.strip().lstrip("@").lower()
        if _valid_did(value):
            did_rows.setdefault(value, enqueued_by)
            summary["resolved"] += 1
            continue
        handle = _canonical_handle(value, cross_platform=cross_platform)
        if handle in attempted_handles:
            continue
        attempted_handles.add(handle)
        handle_candidates.append((handle, enqueued_by))

    if handle_candidates:
        async with client_factory(
            timeout=10.0,
            headers={"User-Agent": "UnifiedCollector/1.0 Bluesky profile resolver"},
        ) as client:
            for index, (handle, enqueued_by) in enumerate(handle_candidates):
                if not _valid_handle(handle):
                    cache_rows[handle] = None
                    summary["unresolvable"] += 1
                    continue
                try:
                    outcome, did = await _resolve_handle(client, handle)
                except Exception:
                    outcome, did = "deferred", None

                if outcome == "resolved" and did is not None:
                    cache_rows[handle] = did
                    did_rows.setdefault(did, f"bluesky_handle:{handle}:{enqueued_by}")
                    summary["resolved"] += 1
                elif outcome == "unresolvable":
                    cache_rows[handle] = None
                    summary["unresolvable"] += 1
                else:
                    # Includes 429 and server errors. Stop this batch so the
                    # API is not probed repeatedly during an outage/cooldown.
                    summary["deferred"] = len(handle_candidates) - index
                    break

                if request_interval_seconds > 0 and index + 1 < len(handle_candidates):
                    await asyncio.sleep(request_interval_seconds)

    async with pool.acquire() as conn:
        async with conn.transaction():
            if cache_rows:
                await conn.executemany(
                    """
                    INSERT INTO bluesky_handle_resolution_cache (handle, did, resolved_at)
                    VALUES ($1, $2, now())
                    ON CONFLICT (handle) DO UPDATE
                    SET did = EXCLUDED.did, resolved_at = now()
                    """,
                    [(handle, did) for handle, did in cache_rows.items()],
                )
            if did_rows:
                queued = await conn.fetch(
                    """
                    INSERT INTO profile_probe_queue (source, username, enqueued_by)
                    SELECT 'bluesky', candidate.did, candidate.enqueued_by
                    FROM unnest($1::text[], $2::text[])
                         AS candidate(did, enqueued_by)
                    ON CONFLICT (source, username) DO NOTHING
                    RETURNING username
                    """,
                    list(did_rows),
                    list(did_rows.values()),
                )
                summary["queued"] = len(queued)

    return summary


__all__ = ["resolve_and_enqueue_bluesky_candidates"]
