"""Additive fenced lease contract for source-native backfill adapters."""
from __future__ import annotations

import json


async def list_backfill_lanes(pool, *, source: str | None = None, limit: int = 100) -> list[dict]:
    """Return bounded common-lane detail without claiming or modifying work."""
    source = (source or "").strip().lower() or None
    limit = max(1, min(limit, 500))
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('backfill_lanes')") is None:
            return []
        rows = await conn.fetch(
            """
            SELECT source, account_identity, target_identity, lane, cursor, status,
                   lease_owner, lease_expires_at, fence_token, retry_count,
                   next_retry_at, cooldown_until, item_cost, byte_cost,
                   latest_durable_write_at
            FROM backfill_lanes
            WHERE ($1::text IS NULL OR source=$1)
            ORDER BY (status='leased') DESC, latest_durable_write_at DESC NULLS LAST
            LIMIT $2
            """,
            source, limit,
        )
    return [dict(row) for row in rows]


async def mirror_backfill_lane(conn, *, source: str, account_identity: str, target_identity: str,
                               cursor: dict, complete: bool, error: str | None = None) -> None:
    """Mirror an already-durable source-native cursor without stealing a live lease."""
    await conn.execute(
        """
        INSERT INTO backfill_lanes(source,account_identity,target_identity,lane,cursor,status,
                                  next_retry_at,cooldown_until,latest_durable_write_at,updated_at)
        VALUES($1,$2,$3,'backfill',$4::jsonb,$5,
               CASE WHEN $6::text IS NULL THEN NULL ELSE now()+interval '5 minutes' END,
               CASE WHEN $6::text IS NULL THEN NULL ELSE now()+interval '5 minutes' END,
               now(),now())
        ON CONFLICT(source,account_identity,target_identity,lane) DO UPDATE SET
            cursor=EXCLUDED.cursor,
            status=CASE WHEN backfill_lanes.status='leased' AND backfill_lanes.lease_expires_at > now()
                        THEN backfill_lanes.status ELSE EXCLUDED.status END,
            lease_owner=CASE WHEN backfill_lanes.status='leased' AND backfill_lanes.lease_expires_at > now()
                             THEN backfill_lanes.lease_owner ELSE NULL END,
            lease_expires_at=CASE WHEN backfill_lanes.status='leased' AND backfill_lanes.lease_expires_at > now()
                                  THEN backfill_lanes.lease_expires_at ELSE NULL END,
            retry_count=CASE WHEN backfill_lanes.status='leased' AND backfill_lanes.lease_expires_at > now()
                             THEN backfill_lanes.retry_count
                             WHEN $6::text IS NULL THEN 0 ELSE backfill_lanes.retry_count+1 END,
            next_retry_at=CASE WHEN backfill_lanes.status='leased' AND backfill_lanes.lease_expires_at > now()
                               THEN backfill_lanes.next_retry_at
                               WHEN $6::text IS NULL THEN NULL ELSE now()+interval '5 minutes' END,
            cooldown_until=CASE WHEN backfill_lanes.status='leased' AND backfill_lanes.lease_expires_at > now()
                                THEN backfill_lanes.cooldown_until
                                WHEN $6::text IS NULL THEN NULL ELSE now()+interval '5 minutes' END,
            latest_durable_write_at=now(), updated_at=now()
        """, source, account_identity, target_identity, json.dumps(cursor),
        "complete" if complete else ("cooldown_deferred" if error else "pending"), error,
    )


async def claim_backfill_lane(conn, *, source: str, account_identity: str, target_identity: str,
                              owner: str, lease_seconds: int = 120) -> dict | None:
    """Atomically claim one eligible lane and advance its fencing token."""
    return await conn.fetchrow(
        """
        UPDATE backfill_lanes SET lease_owner=$4, lease_expires_at=now()+$5::interval,
            fence_token=fence_token+1, status='leased', updated_at=now()
        WHERE id=(
            SELECT id FROM backfill_lanes
            WHERE source=$1 AND account_identity=$2 AND target_identity=$3 AND lane='backfill'
              AND status IN ('pending','leased','cooldown_deferred')
              AND (next_retry_at IS NULL OR next_retry_at <= now())
              AND (cooldown_until IS NULL OR cooldown_until <= now())
              AND (lease_expires_at IS NULL OR lease_expires_at <= now())
            FOR UPDATE SKIP LOCKED
        ) RETURNING *
        """, source, account_identity, target_identity, owner, f"{max(1, lease_seconds)} seconds",
    )


async def finish_backfill_lane(conn, *, lane_id, owner: str, fence_token: int,
                               complete: bool, error: str | None = None) -> dict | None:
    """Release a claimed lane only when its current owner still holds its fence."""
    return await conn.fetchrow(
        """
        UPDATE backfill_lanes SET status=$4, lease_owner=NULL, lease_expires_at=NULL,
            retry_count=CASE WHEN $5::text IS NULL THEN 0 ELSE retry_count+1 END,
            next_retry_at=CASE WHEN $5::text IS NULL THEN NULL ELSE now()+interval '5 minutes' END,
            cooldown_until=CASE WHEN $5::text IS NULL THEN NULL ELSE now()+interval '5 minutes' END,
            updated_at=now()
        WHERE id=$1 AND lease_owner=$2 AND fence_token=$3
        RETURNING *
        """,
        lane_id, owner, fence_token,
        "complete" if complete else ("cooldown_deferred" if error else "pending"), error,
    )
