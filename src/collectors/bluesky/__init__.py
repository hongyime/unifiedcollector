"""Bluesky profile probe (Do Next #3).

The profile feeder resolves usernames with ``com.atproto.identity.resolveHandle``
and queues the returned DID. This collector calls
``app.bsky.actor.getProfile`` with that DID and stores the structured profile.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

import httpx

from src.core.profile_only_collector import ProfileOnlyCollector, ProfileResult

logger = logging.getLogger(__name__)

_DID_RE = re.compile(r"(did:plc:[a-z0-9]+)", re.IGNORECASE)


def _parse_indexed_at(value: object) -> datetime | None:
    """Convert the API's RFC 3339 indexedAt string for Postgres timestamptz."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


class BlueskyCollector(ProfileOnlyCollector):
    SOURCE_NAME = "bluesky"
    PROFILE_TABLE = "bluesky_profiles"

    async def probe_profile(self, username: str) -> ProfileResult:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "Accept": "application/json",
        }
        # Fast path: hit the public API directly with the handle as actor.
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    "https://public.api.bsky.app/xrpc/app.bsky.actor.getProfile",
                    params={"actor": username},
                    headers=headers,
                    timeout=10.0,
                )
        except Exception as exc:
            return ProfileResult(exists=False, username=username, fields={},
                                 error=f"http:{type(exc).__name__}")

        if r.status_code == 400:
            # Bluesky returns 400 for "unknown actor" in some cases.
            return ProfileResult(exists=False, username=username, fields={})
        if r.status_code == 404:
            return ProfileResult(exists=False, username=username, fields={})
        if r.status_code >= 400:
            return ProfileResult(exists=False, username=username, fields={},
                                 error=f"http:{r.status_code}")

        try:
            payload = r.json()
        except Exception:
            return ProfileResult(exists=False, username=username, fields={},
                                 error="bad_json")

        did = payload.get("did")
        if not did:
            return ProfileResult(exists=False, username=username, fields={})

        return ProfileResult(
            exists=True,
            username=username,
            fields={
                "did": did,
                "display_name": payload.get("displayName"),
                "bio": payload.get("description"),
                "avatar_url": payload.get("avatar"),
                "follower_count": payload.get("followersCount"),
                "following_count": payload.get("followsCount"),
                "posts_count": payload.get("postsCount"),
                "indexed_at": _parse_indexed_at(payload.get("indexedAt")),
            },
            raw_payload=payload,
        )


__all__ = ["BlueskyCollector"]
