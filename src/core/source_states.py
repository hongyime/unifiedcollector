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

import logging
from datetime import datetime
from enum import Enum

logger = logging.getLogger(__name__)


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


_ERROR_STATES = frozenset({
    SourceState.RATE_LIMITED,
    SourceState.AUTH_FAILED,
    SourceState.SOURCE_DEAD,
})


async def set_source_state(
    pool,
    source: str,
    state: SourceState,
    *,
    enabled: bool | None = None,
    stop_reason: str | None = None,
    blocked_until: datetime | None = None,
) -> None:
    """Upsert one source's control row into ``source_controls``.

    Mirrors collector health for the dashboard. Error states (RATE_LIMITED /
    AUTH_FAILED / SOURCE_DEAD) bump ``consecutive_failures``; HEALTHY resets it
    to 0 and clears blocked_until/stop_reason; PAUSED leaves the counter alone.
    ``enabled`` is only changed when explicitly passed.

    Best-effort by design: a failed mirror write is logged, never raised, so it
    can neither abort a collector nor break the watchdog's per-source pass.
    """
    state = SourceState(state)
    try:
        await pool.execute(
            """
            INSERT INTO source_controls
                (source_name, state, enabled, last_success, last_attempt,
                 blocked_until, stop_reason, consecutive_failures, updated_at)
            VALUES ($1, $2, COALESCE($3, TRUE),
                    CASE WHEN $2 = 'HEALTHY' THEN NOW() ELSE NULL END,
                    NOW(), $4, $5,
                    CASE WHEN $6 THEN 1 ELSE 0 END, NOW())
            ON CONFLICT (source_name) DO UPDATE SET
                state = EXCLUDED.state,
                enabled = CASE WHEN $3::boolean IS NULL THEN source_controls.enabled
                               ELSE $3::boolean END,
                last_success = COALESCE(EXCLUDED.last_success, source_controls.last_success),
                last_attempt = EXCLUDED.last_attempt,
                blocked_until = CASE WHEN EXCLUDED.state = 'HEALTHY' THEN NULL
                                     ELSE COALESCE(EXCLUDED.blocked_until, source_controls.blocked_until) END,
                stop_reason = CASE WHEN EXCLUDED.state = 'HEALTHY' THEN NULL
                                   ELSE COALESCE(EXCLUDED.stop_reason, source_controls.stop_reason) END,
                consecutive_failures = CASE
                    WHEN EXCLUDED.state IN ('RATE_LIMITED', 'AUTH_FAILED', 'SOURCE_DEAD')
                        THEN source_controls.consecutive_failures + 1
                    WHEN EXCLUDED.state = 'HEALTHY' THEN 0
                    ELSE source_controls.consecutive_failures END,
                updated_at = NOW()
            """,
            source,
            state.value,
            enabled,
            blocked_until,
            stop_reason,
            state in _ERROR_STATES,
        )
    except Exception:
        logger.error("set_source_state failed for %s (%s)", source, state.value, exc_info=True)
