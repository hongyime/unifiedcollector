"""Shared collector work-priority policy.

Higher scores run first. The order mirrors the collector PRD:
Tier 1 freshness, rich media, low rate-limit risk, historical backfill,
then broad discovery.
"""

from __future__ import annotations

from enum import IntEnum


class CollectionPhase(IntEnum):
    BROAD_DISCOVERY = 10
    HISTORICAL_BACKFILL = 20
    LOW_RATE_LIMIT = 30
    RICH_MEDIA = 40
    TIER1_FRESHNESS = 50


PHASE_ORDER = (
    CollectionPhase.TIER1_FRESHNESS,
    CollectionPhase.RICH_MEDIA,
    CollectionPhase.LOW_RATE_LIMIT,
    CollectionPhase.HISTORICAL_BACKFILL,
    CollectionPhase.BROAD_DISCOVERY,
)


def classify_work(*, work_kind: str, proximity_tier: int | None = None) -> CollectionPhase:
    kind = work_kind.strip().lower()
    if proximity_tier in (1, 2):
        return CollectionPhase.TIER1_FRESHNESS
    if kind in {"freshness", "live", "live_messages", "messages_tail", "root_target", "follow_graph", "route_capture"}:
        return CollectionPhase.TIER1_FRESHNESS
    if kind in {"media", "media_download", "rich_media", "attachment_download"}:
        return CollectionPhase.RICH_MEDIA
    if kind in {"lowrisk", "low_rate_limit", "safe_api"}:
        return CollectionPhase.LOW_RATE_LIMIT
    if kind in {"history", "historical_backfill", "backfill"}:
        return CollectionPhase.HISTORICAL_BACKFILL
    if kind in {"spider", "discovery", "broad_discovery"}:
        return CollectionPhase.BROAD_DISCOVERY
    if proximity_tier == 3:
        return CollectionPhase.LOW_RATE_LIMIT
    return CollectionPhase.HISTORICAL_BACKFILL


def proximity_priority_score_sql(tier_expr: str) -> str:
    """Return SQL CASE that scores analyzer proximity with the shared policy."""
    return (
        "CASE "
        f"WHEN {tier_expr} IN (1, 2) THEN {int(CollectionPhase.TIER1_FRESHNESS)} "
        f"WHEN {tier_expr} = 3 THEN {int(CollectionPhase.LOW_RATE_LIMIT)} "
        f"ELSE {int(CollectionPhase.HISTORICAL_BACKFILL)} "
        "END"
    )
