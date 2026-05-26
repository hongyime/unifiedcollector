"""AccountState persistence (Wave 2.4).

Persists AccountPool runtime state across worker restarts:

  * daily quota counters (profile_views, actions)
  * active cooldown deadlines (locked_until + reason)
  * last_error_kind for routing decisions

Why not roll into AccountPool itself?
-------------------------------------
AccountPool is intentionally synchronous (uses ``threading.Lock``) and
must work without a database for unit tests / dry-runs. Persistence
is OPT-IN: callers wire ``AccountStateRepository`` separately and call
``flush_pool()`` / ``load_into_pool()`` at shutdown / boot.

Wall-clock vs monotonic
-----------------------
``Account.locked_until`` is ``time.monotonic()`` based, which resets
to zero at process boot. The DB stores wall-clock TIMESTAMPTZ so a
deadline survives restart. We convert at the boundary:

    wall_deadline = time.time() + (acct.locked_until - time.monotonic())

and on load:

    seconds_left = (db_locked_until_wall - now_utc).total_seconds()
    acct.locked_until = time.monotonic() + max(0, seconds_left)

Concurrency
-----------
flush + load are async. Inside each, we hold the AccountPool's lock
only long enough to snapshot (flush) or write (load). DB I/O happens
WITHOUT the lock to avoid blocking pool consumers.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

import asyncpg

from src.core.account_pool import AccountPool, _Quota, _quota_date

logger = logging.getLogger(__name__)


def _wall_deadline_from_monotonic(locked_until_mono: float) -> Optional[datetime]:
    """Convert a monotonic deadline to wall-clock UTC, or None if expired."""
    if locked_until_mono <= 0:
        return None
    seconds_left = locked_until_mono - time.monotonic()
    if seconds_left <= 0:
        return None
    return datetime.fromtimestamp(time.time() + seconds_left, tz=timezone.utc)


def _monotonic_from_wall(wall_deadline: Optional[datetime]) -> float:
    """Convert a wall-clock deadline to monotonic, clamping past deadlines to 0."""
    if wall_deadline is None:
        return 0.0
    if wall_deadline.tzinfo is None:
        wall_deadline = wall_deadline.replace(tzinfo=timezone.utc)
    seconds_left = (wall_deadline - datetime.now(timezone.utc)).total_seconds()
    if seconds_left <= 0:
        return 0.0
    return time.monotonic() + seconds_left


class AccountStateRepository:
    """Asyncpg-backed persistence for AccountPool state."""

    def __init__(self, pool: asyncpg.Pool):
        if pool is None:
            raise ValueError("pool must not be None")
        self._pool = pool

    # -- low-level CRUD -------------------------------------------------

    async def upsert(
        self,
        name: str,
        *,
        quota_date: str,
        profile_views: int,
        actions: int,
        locked_until_wall: Optional[datetime],
        cooldown_reason: str,
        last_error_kind: str,
        error_count: int,
        success_count: int,
        total_requests: int,
    ) -> None:
        """Upsert one row of account state."""
        if not name:
            raise ValueError("name must not be empty")
        # Convert quota_date string ("YYYY-MM-DD") to a TIMESTAMPTZ at
        # 00:00 UTC for that day. Empty string -> NULL.
        if quota_date:
            quota_window_start = datetime.strptime(
                quota_date, "%Y-%m-%d"
            ).replace(tzinfo=timezone.utc)
        else:
            quota_window_start = None

        await self._pool.execute(
            """
            INSERT INTO account_state
                (account_name, quota_window_start, profile_views_today,
                 actions_today, locked_until_wall, cooldown_reason,
                 last_error_kind, error_count, success_count,
                 total_requests, updated_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10, NOW())
            ON CONFLICT (account_name) DO UPDATE SET
                quota_window_start  = EXCLUDED.quota_window_start,
                profile_views_today = EXCLUDED.profile_views_today,
                actions_today       = EXCLUDED.actions_today,
                locked_until_wall   = EXCLUDED.locked_until_wall,
                cooldown_reason     = EXCLUDED.cooldown_reason,
                last_error_kind     = EXCLUDED.last_error_kind,
                error_count         = EXCLUDED.error_count,
                success_count       = EXCLUDED.success_count,
                total_requests      = EXCLUDED.total_requests,
                updated_at          = NOW()
            """,
            name, quota_window_start, profile_views, actions,
            locked_until_wall, cooldown_reason, last_error_kind,
            error_count, success_count, total_requests,
        )

    async def fetch(self, name: str) -> Optional[dict]:
        """Fetch one row by account name."""
        row = await self._pool.fetchrow(
            "SELECT * FROM account_state WHERE account_name=$1", name
        )
        if row is None:
            return None
        return dict(row)

    async def fetch_all(self) -> list[dict]:
        """Fetch ALL rows. Used at boot to load_into_pool."""
        rows = await self._pool.fetch(
            "SELECT * FROM account_state ORDER BY account_name"
        )
        return [dict(r) for r in rows]

    async def clear_expired_cooldowns(self) -> int:
        """Drop locked_until_wall for rows whose deadline has passed.

        Optional housekeeping. Returns rows updated.
        """
        result = await self._pool.execute(
            """
            UPDATE account_state
            SET locked_until_wall = NULL,
                cooldown_reason   = '',
                updated_at        = NOW()
            WHERE locked_until_wall IS NOT NULL
              AND locked_until_wall <= NOW()
            """
        )
        try:
            return int(result.split()[-1])
        except (ValueError, IndexError):
            return 0

    # -- AccountPool integration ---------------------------------------

    async def flush_pool(self, acct_pool: AccountPool) -> int:
        """Snapshot every account in *acct_pool* and persist.

        Holds the pool's threading lock only long enough to grab a
        consistent snapshot of all account state. The DB writes
        happen WITHOUT the lock.
        """
        snapshots = []
        with acct_pool._lock:  # noqa: SLF001 — internal pool lock
            for a in acct_pool._accounts:
                snapshots.append({
                    "name": a.name,
                    "quota_date": a.quota.quota_date,
                    "profile_views": a.quota.profile_views,
                    "actions": a.quota.actions,
                    "locked_until_wall": _wall_deadline_from_monotonic(a.locked_until),
                    "cooldown_reason": a.cooldown_reason,
                    "last_error_kind": a.last_error_kind,
                    "error_count": a.error_count,
                    "success_count": a.success_count,
                    "total_requests": a.total_requests,
                })

        # I/O outside the lock
        for snap in snapshots:
            try:
                await self.upsert(**snap)
            except Exception:
                logger.exception(
                    "AccountStateRepository: flush failed for %s", snap["name"]
                )
        return len(snapshots)

    async def load_into_pool(
        self, acct_pool: AccountPool, *, only_existing: bool = True,
    ) -> int:
        """Restore persisted state into *acct_pool*.

        Args:
            acct_pool: the AccountPool to populate.
            only_existing: if True (default) only update accounts already
                in the pool. If False, rows for unknown accounts are
                silently skipped (we don't synthesize Account objects
                from credentials we don't have).

        Returns count of accounts updated.
        """
        rows = await self.fetch_all()
        if not rows:
            return 0

        # Build today's quota window key once, in same convention as the pool
        today = _quota_date(getattr(acct_pool, "quota_reset_hour", 0))

        updated = 0
        with acct_pool._lock:  # noqa: SLF001
            existing_by_name = {a.name: a for a in acct_pool._accounts}
            for row in rows:
                name = row["account_name"]
                acct = existing_by_name.get(name)
                if acct is None:
                    if only_existing:
                        continue
                    # not synthesizing — caller should add_account first
                    continue

                # Restore quota — but ONLY if window is still today's.
                # Old window (yesterday) -> reset.
                qws = row["quota_window_start"]
                if qws is not None:
                    if qws.tzinfo is None:
                        qws = qws.replace(tzinfo=timezone.utc)
                    persisted_date = qws.strftime("%Y-%m-%d")
                else:
                    persisted_date = ""

                if persisted_date == today:
                    acct.quota = _Quota(
                        quota_date=today,
                        profile_views=row["profile_views_today"] or 0,
                        actions=row["actions_today"] or 0,
                    )
                else:
                    # Window rolled while we were offline; start fresh
                    acct.quota = _Quota(quota_date=today)

                # Restore cooldown deadline (clamps past deadlines to 0)
                acct.locked_until = _monotonic_from_wall(row["locked_until_wall"])
                acct.cooldown_reason = row["cooldown_reason"] or ""
                acct.last_error_kind = row["last_error_kind"] or ""

                # Counters
                acct.error_count = row["error_count"] or 0
                acct.success_count = row["success_count"] or 0
                acct.total_requests = row["total_requests"] or 0
                updated += 1

        logger.info(
            "AccountStateRepository: loaded state for %d accounts (today=%s)",
            updated, today,
        )
        return updated


__all__ = ["AccountStateRepository"]
