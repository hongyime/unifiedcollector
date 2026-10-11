"""Collector source failure taxonomy (Fudan pattern).

Defines explicit collector states with stop conditions. On 429/403/CAPTCHA,
collectors STOP immediately and record the reason — they do NOT retry
indefinitely.

States:
- HEALTHY: Collecting normally
- RATE_LIMITED: Hit rate limit; blocked_until indicates recovery time
- AUTH_FAILED: Authentication failed; needs credential refresh (not human login)
- SOURCE_DEAD: Source permanently unavailable; disabled pending review
- PAUSED: Manually paused by operator

Usage:
    from src.core.source_states import SourceState, should_stop_collector

    if response.status == 429:
        # Stop, don't retry
        await set_source_state(pool, "instagram", SourceState.RATE_LIMITED,
                              blocked_until=reset_time,
                              stop_reason="Rate limit hit, reset at ...")
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum


class SourceState(str, Enum):
    """Explicit collector source states (Fudan failure taxonomy)."""
    HEALTHY = "HEALTHY"
    RATE_LIMITED = "RATE_LIMITED"
    AUTH_FAILED = "AUTH_FAILED"
    SOURCE_DEAD = "SOURCE_DEAD"
    PAUSED = "PAUSED"


# HTTP statuses that trigger immediate stop (no retry)
STOP_ON_STATUS = frozenset({429, 403})

# Keywords in error messages indicating CAPTCHA/challenge
CHALLENGE_KEYWORDS = frozenset({
    "captcha", "challenge", "checkpoint", "verify you",
    "unusual activity", "suspicious",
})


def should_stop_collector(status_code: int | None, error_text: str = "") -> tuple[bool, str]:
    """Determine if a collector should stop based on response.

    Returns (should_stop, reason).
    Fudan rule: on 429/403/CAPTCHA → stop immediately, no retry.
    """
    if status_code in STOP_ON_STATUS:
        return True, f"HTTP {status_code} — stopping per Fudan rule (no infinite retry)"
    error_lower = error_text.lower()
    for kw in CHALLENGE_KEYWORDS:
        if kw in error_lower:
            return True, f"Challenge detected ({kw}) — stopping, needs review"
    return False, ""


def classify_state(status_code: int | None, error_text: str = "") -> SourceState:
    """Classify a failure into a SourceState."""
    if status_code == 429:
        return SourceState.RATE_LIMITED
    if status_code == 403:
        return SourceState.AUTH_FAILED
    error_lower = error_text.lower()
    for kw in CHALLENGE_KEYWORDS:
        if kw in error_lower:
            return SourceState.AUTH_FAILED
    return SourceState.SOURCE_DEAD
