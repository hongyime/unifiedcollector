"""Durable, provider-safe source recovery profiles.

Recovery is deliberately separate from a manual source pause and the ordinary
Retry-After wall: it describes what a source is allowed to do after a provider
restriction, not whether an operator has enabled the source.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

INSTAGRAM_RECOVERY_HOURS = 45
MAX_RECOVERY_DAYS = 7
INSTAGRAM_OBSERVATION_MINUTES = (90, 150)


def _account(account: str | None) -> str:
    return (account or "").strip()


def _as_dict(row) -> dict | None:
    if not row:
        return None
    data = dict(row)
    for key in ("started_at", "restricted_until", "stale_since", "last_observation_at", "updated_at"):
        if data.get(key) and hasattr(data[key], "isoformat"):
            data[key] = data[key].isoformat()
    data["active"] = bool(data.get("restricted_until")) and _parse_time(data["restricted_until"]) > datetime.now(timezone.utc)
    data["clean"] = int(data.get("clean_passes") or 0)
    return data


def _parse_time(value) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def observation_due(profile: dict, *, now: datetime | None = None) -> bool:
    """Keep recovery observation no more frequent than the 90-minute floor."""
    last = profile.get("last_observation_at")
    if not last:
        return True
    now = now or datetime.now(timezone.utc)
    return now - _parse_time(last) >= timedelta(minutes=INSTAGRAM_OBSERVATION_MINUTES[0])


async def enter_recovery(
    pool,
    source: str,
    *,
    account: str | None = None,
    reason: str,
    now: datetime | None = None,
) -> dict | None:
    """Enter (or extend) a provider restriction profile.

    A first Instagram restriction lasts 45 hours. Further restrictions while
    the profile is active extend it in 45-hour steps but never beyond seven
    days from the original restriction.  The UPSERT keeps this atomic across
    collector restarts and concurrent browser workers.
    """
    if pool is None:
        return None
    now = now or datetime.now(timezone.utc)
    source = source.strip().lower()
    account = _account(account)
    duration = timedelta(hours=INSTAGRAM_RECOVERY_HOURS if source == "instagram" else 24)
    cap = timedelta(days=MAX_RECOVERY_DAYS)
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
            """
            INSERT INTO source_recovery_profiles
                (source_name, account_identity, state, reason, started_at,
                 restricted_until, stale_since, clean_passes, restriction_count, updated_at)
            VALUES ($1,$2,'restricted',$3,$4,$4+$5::interval,$4,0,1,$4)
            ON CONFLICT (source_name, account_identity) DO UPDATE SET
                state='restricted',
                reason=EXCLUDED.reason,
                started_at=CASE WHEN source_recovery_profiles.restricted_until <= $4
                                THEN $4 ELSE source_recovery_profiles.started_at END,
                restricted_until=CASE WHEN source_recovery_profiles.restricted_until <= $4
                                      THEN $4 + $5::interval
                                      ELSE LEAST(source_recovery_profiles.started_at + $6::interval,
                                                 GREATEST(source_recovery_profiles.restricted_until, $4) + $5::interval)
                                 END,
                stale_since=CASE WHEN source_recovery_profiles.restricted_until <= $4
                                 THEN $4 ELSE COALESCE(source_recovery_profiles.stale_since, $4) END,
                clean_passes=0,
                restriction_count=source_recovery_profiles.restriction_count+1,
                updated_at=$4
            RETURNING *
            """,
                source, account, reason[:500], now,
                f"{int(duration.total_seconds())} seconds", f"{int(cap.total_seconds())} seconds",
            )
    except Exception:
        return None
    return _as_dict(row)


async def get_recovery_profile(pool, source: str, *, account: str | None = None) -> dict | None:
    if pool is None:
        return None
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM source_recovery_profiles WHERE source_name=$1 AND account_identity=$2",
                source.strip().lower(), _account(account),
            )
    except Exception:
        return None
    return _as_dict(row)


async def get_active_recovery(pool, source: str) -> dict | None:
    """Return the newest uncleared restriction, including its clean-pass exit."""
    if pool is None:
        return None
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT * FROM source_recovery_profiles
                   WHERE source_name=$1 AND state='restricted'
                   ORDER BY restricted_until DESC LIMIT 1""",
                source.strip().lower(),
            )
    except Exception:
        return None
    return _as_dict(row)


async def record_clean_recovery_pass(pool, source: str, *, account: str | None = None) -> dict | None:
    """Record one safe pass; three after expiry are required to clear recovery."""
    if pool is None:
        return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            WITH matching_profile AS (
                SELECT source_name, account_identity
                FROM source_recovery_profiles
                WHERE source_name=$1 AND state='restricted' AND account_identity IN ($2, '')
                ORDER BY (account_identity = $2) DESC
                LIMIT 1
            )
            UPDATE source_recovery_profiles profile
            SET clean_passes=CASE WHEN profile.restricted_until <= now() THEN profile.clean_passes+1 ELSE 0 END,
                state=CASE WHEN profile.restricted_until <= now() AND profile.clean_passes+1 >= 3 THEN 'normal' ELSE profile.state END,
                stale_since=CASE WHEN profile.restricted_until <= now() AND profile.clean_passes+1 >= 3 THEN NULL ELSE profile.stale_since END,
                updated_at=now()
            FROM matching_profile
            WHERE profile.source_name=matching_profile.source_name
              AND profile.account_identity=matching_profile.account_identity
            RETURNING profile.*
            """,
            source.strip().lower(), _account(account),
        )
    return _as_dict(row)


async def mark_recovery_observation(pool, source: str, *, account: str | None = None) -> dict | None:
    """Persist a passive browser observation without treating it as a clean pass."""
    if pool is None:
        return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """WITH matching_profile AS (
                   SELECT source_name, account_identity
                   FROM source_recovery_profiles
                   WHERE source_name=$1 AND state='restricted' AND account_identity IN ($2, '')
                   ORDER BY (account_identity = $2) DESC
                   LIMIT 1
               )
               UPDATE source_recovery_profiles profile
               SET last_observation_at=now(), updated_at=now()
               FROM matching_profile
               WHERE profile.source_name=matching_profile.source_name
                 AND profile.account_identity=matching_profile.account_identity
                 AND (profile.last_observation_at IS NULL
                      OR profile.last_observation_at <= now() - interval '90 minutes')
               RETURNING profile.*""",
            source.strip().lower(), _account(account),
        )
    return _as_dict(row)


async def list_recovery_profiles(pool, *, limit: int = 100) -> list[dict]:
    if pool is None:
        return []
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM source_recovery_profiles ORDER BY updated_at DESC LIMIT $1",
                max(1, min(limit, 500)),
            )
    except Exception:
        return []
    return [_as_dict(row) for row in rows]
