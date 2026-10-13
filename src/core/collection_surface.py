"""Canonical collection-surface labels shared by feed writers and reports."""

from __future__ import annotations

import re
from urllib.parse import urlparse


COLLECTION_SURFACES = (
    "profile",
    "following",
    "for_you",
    "explore",
    "topic",
    "home",
    "search",
    "message_stream",
    "community",
    "reels",
    "stories",
    "tagged",
    "unknown",
)


def bounded_capture_limit(
    value: object,
    *,
    default: int,
    maximum: int,
    minimum: int = 0,
) -> int:
    """Parse an operator capture limit and clamp it to a hard safe range."""
    lower = max(0, int(minimum))
    upper = max(lower, int(maximum))
    try:
        parsed = int(value) if value is not None else int(default)
    except (TypeError, ValueError, OverflowError):
        parsed = int(default)
    return max(lower, min(parsed, upper))


def classify_collection_error(error_class: object) -> str:
    """Map stored exception/status labels to a small operator-facing taxonomy."""
    text = str(error_class or "").strip().lower()
    if not text:
        return "unknown"
    if "429" in text or any(token in text for token in ("rate_limit", "ratelimit", "throttl")):
        return "rate_limit"
    if "401" in text or any(token in text for token in ("auth", "login", "cookie", "credential")):
        return "auth"
    if "403" in text or any(token in text for token in ("private", "visibility", "forbidden", "unavailable")):
        return "visibility"
    if "timeout" in text or "timedout" in text:
        return "timeout"
    if any(token in text for token in ("parse", "decode", "json")):
        return "parse"
    if any(token in text for token in ("database", "postgres", "sql", "persistence")):
        return "persistence"
    if any(token in text for token in ("network", "connect", "http", "socket")):
        return "network"
    return "other"


_ALIASES = {
    "profile": "profile",
    "following": "following",
    "follow": "following",
    "home_following": "following",
    "home_following_feed": "following",
    "for_you": "for_you",
    "foryou": "for_you",
    "fyp": "for_you",
    "recommendation": "for_you",
    "recommendations": "for_you",
    "home_foryou": "for_you",
    "home_for_you": "for_you",
    "explore": "explore",
    "topic": "topic",
    "hashtag": "topic",
    "home": "home",
    "timeline": "home",
    "search": "search",
    "message": "message_stream",
    "messages": "message_stream",
    "message_stream": "message_stream",
    "dm": "message_stream",
    "community": "community",
    "reels": "reels",
    "stories": "stories",
    "tagged": "tagged",
    "unknown": "unknown",
}


def normalize_collection_surface(
    value: object = None,
    *,
    context: object = None,
    url: object = None,
) -> str:
    """Normalize explicit labels and common capture hints to the stable vocabulary."""
    for candidate in (value, context, url):
        text = str(candidate or "").strip().lower()
        if not text:
            continue
        if text.startswith(("http://", "https://")):
            text = urlparse(text).path.lower()
        token = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
        aliased = _ALIASES.get(token)
        if aliased and aliased != "unknown":
            return aliased
        parts = set(filter(None, token.split("_")))
        if {"for", "you"} <= parts or "foryou" in parts or "fyp" in parts:
            return "for_you"
        if "following" in parts:
            return "following"
        if "explore" in parts:
            return "explore"
        if "search" in parts:
            return "search"
        if "topic" in parts or "hashtag" in parts:
            return "topic"
        if "community" in parts:
            return "community"
        if "reels" in parts or "reel" in parts:
            return "reels"
        if "stories" in parts or "story" in parts:
            return "stories"
        if "tagged" in parts:
            return "tagged"
        if "message" in parts or "messages" in parts or "direct" in parts:
            return "message_stream"
        if token in {"home_feed", "home_timeline", "timeline_home"}:
            return "home"
        # Profile labels are intentionally conservative because most platform
        # URLs contain a username in a path segment.
        if token in {"user_profile", "account_profile", "profile_page"}:
            return "profile"
        path = urlparse(text).path.lower() if "://" in text else text
        if re.search(r"/(explore|discover)(/|$)", path):
            return "explore"
        if re.search(r"/(search|hashtag)(/|$)", path):
            return "search" if "/search" in path else "topic"
        if re.search(r"/(messages|direct)(/|$)", path):
            return "message_stream"
        if re.search(r"/(following|foryou|fyp)(/|$)", path):
            return "following" if "/following" in path else "for_you"
        if re.search(r"/(topic|tag)/", path):
            return "topic"
        if re.fullmatch(r"/@[^/]+/?", path) or re.fullmatch(r"/[a-z0-9_.-]+/?", path):
            return "profile"
    return "unknown"


__all__ = [
    "COLLECTION_SURFACES",
    "bounded_capture_limit",
    "classify_collection_error",
    "normalize_collection_surface",
]
