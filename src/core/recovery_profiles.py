"""Durable recovery-profile writes used by restricted browser observation."""
from __future__ import annotations


def _account(account: str | None) -> str:
    return (account or "").strip()


async def mark_recovery_observation(pool, source: str, *, account: str | None = None) -> dict | None:
    """Persist a passive observation without counting it as a clean pass."""
    if pool is None:
        return None
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """WITH matching_profile AS (
                       SELECT source_name, account_identity
                       FROM source_recovery_profiles
                       WHERE source_name=$1 AND account_identity IN ($2, '')
                       ORDER BY (account_identity = $2) DESC
                       LIMIT 1
                   )
                   UPDATE source_recovery_profiles profile
                   SET last_observation_at=now(), updated_at=now()
                   FROM matching_profile
                   WHERE profile.source_name=matching_profile.source_name
                     AND profile.account_identity=matching_profile.account_identity
                   RETURNING profile.*""",
                source.strip().lower(), _account(account),
            )
    except Exception:
        return None
    return dict(row) if row else None
