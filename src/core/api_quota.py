"""Small helpers for durable external API quota snapshots."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any


def target_units(quota_units: int, target_ratio: float) -> int:
    quota = max(0, int(quota_units or 0))
    ratio = min(1.0, max(0.0, float(target_ratio or 0.0)))
    return int(quota * ratio)


async def upsert_api_quota_snapshot(
    pool: Any,
    *,
    service: str,
    account: str,
    bucket: str,
    quota_date: date | str | None = None,
    reset_at: datetime | None = None,
    used_units: int = 0,
    remaining_units: int | None = None,
    quota_units: int = 0,
    target_ratio: float = 0.9,
    paused: bool = False,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Best-effort upsert of an absolute quota snapshot."""
    if pool is None:
        return
    qdate = quota_date or datetime.now(timezone.utc).date()
    quota = max(0, int(quota_units or 0))
    target = target_units(quota, target_ratio)
    used = max(0, int(used_units or 0))
    remaining = remaining_units
    if remaining is None and quota > 0:
        remaining = max(0, quota - used)
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO collector_api_quota_snapshots (
                    service, account, bucket, quota_date, reset_at, used_units,
                    remaining_units, quota_units, target_units, target_ratio,
                    paused, metadata, updated_at
                )
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12::jsonb,NOW())
                ON CONFLICT (service, account, bucket, quota_date) DO UPDATE SET
                    reset_at = EXCLUDED.reset_at,
                    used_units = EXCLUDED.used_units,
                    remaining_units = EXCLUDED.remaining_units,
                    quota_units = EXCLUDED.quota_units,
                    target_units = EXCLUDED.target_units,
                    target_ratio = EXCLUDED.target_ratio,
                    paused = EXCLUDED.paused,
                    metadata = COALESCE(collector_api_quota_snapshots.metadata, '{}'::jsonb)
                               || EXCLUDED.metadata,
                    updated_at = NOW()
                """,
                service,
                account,
                bucket,
                qdate,
                reset_at,
                used,
                remaining,
                quota,
                target,
                float(target_ratio),
                bool(paused),
                json.dumps(metadata or {}, default=str),
            )
    except Exception:
        return
