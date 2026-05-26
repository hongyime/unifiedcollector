"""Profile-access tracking + smart account selection.

Wave 2.3 — port of instagramtoolkit's ProfileAccessRepository +
SmartAccountSelector to asyncpg, cross-source.

Why this exists
---------------
When you have N accounts in a pool, naive LRU rotation will eventually
pick an account that CAN'T see the target (private profile, blocked,
not following). That's a wasted request that:

  * costs against rate-limit / quota budgets
  * may itself trip cooldown ("403 Forbidden" -> auth cooldown)
  * leaves you with no data

ProfileAccessRepository remembers every attempt's outcome. Next time
we want to scrape ``target_id`` we ask: "which of my available
accounts most recently SUCCEEDED on this target?" — and route there.

Source-agnostic
---------------
The original toolkit was Instagram-specific. We add a ``source`` column
so the same table serves every collector that has the "private content
visible to subset of accounts" problem (instagram, tiktok, lemon8).

Public API
----------
ProfileAccessRepository(pool):
    await repo.record_attempt(source, target_id, account, can_access,
                              is_public=None, is_followed=False,
                              error=None)
    await repo.get_profile_summary(source, target_id) -> dict
    await repo.get_accessible_accounts(source, target_id) -> list[str]
    await repo.get_best_account(source, target_id, available) -> str | None
    await repo.cleanup_old_attempts(days=30) -> int
    await repo.cleanup_inactive_profiles(days=30) -> int
    await repo.get_statistics(source=None) -> dict

SmartAccountSelector(repo, account_pool):
    await sel.select_for_operation(source, target_id, available)
        -> account_name | None
    await sel.select_for_batch(source, target_ids, available)
        -> dict[target_id, account_name]
    await sel.get_following_overlap(account, source, target_ids)
        -> set[target_id]   # which of these is `account` known to access?

Concurrency
-----------
``record_attempt`` runs in a single transaction (insert attempt +
upsert summary) so two concurrent attempts on the same target can't
clobber each other's accessible_by JSON list. We use SELECT FOR UPDATE
on the summary row inside the transaction.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

import asyncpg

logger = logging.getLogger(__name__)


class ProfileAccessRepository:
    """Asyncpg-backed tracker for which accounts can access which targets."""

    def __init__(self, pool: asyncpg.Pool):
        if pool is None:
            raise ValueError("pool must not be None")
        self._pool = pool

    async def record_attempt(
        self,
        source: str,
        target_id: str,
        account: str,
        can_access: bool,
        is_public: Optional[bool] = None,
        is_followed: bool = False,
        error: Optional[str] = None,
    ) -> None:
        """Record an access attempt + upsert summary atomically.

        Two concurrent calls on the same (source, target_id) are
        serialized via SELECT ... FOR UPDATE on the summary row, so the
        accessible_by JSONB list cannot be lost-update'd.
        """
        if not source or not target_id or not account:
            raise ValueError(
                "source, target_id, account are required "
                f"(got source={source!r}, target_id={target_id!r}, account={account!r})"
            )

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                # 1. Append attempt row (always)
                await conn.execute(
                    """
                    INSERT INTO profile_access_attempts
                        (source, target_id, accessing_account, can_access,
                         is_public, is_followed, error_msg)
                    VALUES ($1,$2,$3,$4,$5,$6,$7)
                    """,
                    source, target_id, account, can_access,
                    is_public, is_followed, error,
                )

                # 2. Lock + read existing summary (or NULL)
                row = await conn.fetchrow(
                    """
                    SELECT is_public, accessible_by, total_attempts
                    FROM profile_access_summary
                    WHERE source=$1 AND target_id=$2
                    FOR UPDATE
                    """,
                    source, target_id,
                )

                if row is None:
                    # New summary
                    accessible_by = [account] if can_access else []
                    await conn.execute(
                        """
                        INSERT INTO profile_access_summary
                            (source, target_id, is_public,
                             last_checked_ts, last_success_ts,
                             total_attempts, accessible_by)
                        VALUES ($1,$2,$3, NOW(),
                                CASE WHEN $4 THEN NOW() ELSE NULL END,
                                1, $5::jsonb)
                        """,
                        source, target_id, is_public, can_access,
                        json.dumps(accessible_by),
                    )
                else:
                    # Update existing
                    raw_known = row["accessible_by"]
                    if isinstance(raw_known, str):
                        known: list[str] = json.loads(raw_known) if raw_known else []
                    elif isinstance(raw_known, list):
                        known = list(raw_known)
                    else:
                        known = []
                    if can_access and account not in known:
                        known.append(account)
                    new_is_public = (
                        is_public if is_public is not None else row["is_public"]
                    )
                    await conn.execute(
                        """
                        UPDATE profile_access_summary
                        SET is_public        = $3,
                            last_checked_ts  = NOW(),
                            last_success_ts  = CASE WHEN $4
                                                    THEN NOW()
                                                    ELSE last_success_ts END,
                            total_attempts   = total_attempts + 1,
                            accessible_by    = $5::jsonb
                        WHERE source=$1 AND target_id=$2
                        """,
                        source, target_id, new_is_public, can_access,
                        json.dumps(known),
                    )

    async def get_profile_summary(
        self, source: str, target_id: str,
    ) -> dict[str, Any]:
        """Return summary dict for (source, target_id), with sensible defaults."""
        row = await self._pool.fetchrow(
            """
            SELECT is_public, last_checked_ts, last_success_ts,
                   total_attempts, accessible_by
            FROM profile_access_summary
            WHERE source=$1 AND target_id=$2
            """,
            source, target_id,
        )
        if row is None:
            return {
                "status": "unknown",
                "is_public": None,
                "accessible_by": [],
                "last_checked": None,
                "last_success": None,
                "total_attempts": 0,
            }
        raw = row["accessible_by"]
        if isinstance(raw, str):
            accessible_by = json.loads(raw) if raw else []
        elif isinstance(raw, list):
            accessible_by = list(raw)
        else:
            accessible_by = []
        return {
            "status": "tracked",
            "is_public": row["is_public"],
            "accessible_by": accessible_by,
            "last_checked": row["last_checked_ts"],
            "last_success": row["last_success_ts"],
            "total_attempts": row["total_attempts"],
        }

    async def get_accessible_accounts(
        self, source: str, target_id: str,
    ) -> list[str]:
        """Accounts known to be able to access (source, target_id)."""
        row = await self._pool.fetchrow(
            """
            SELECT accessible_by FROM profile_access_summary
            WHERE source=$1 AND target_id=$2
            """,
            source, target_id,
        )
        if row is None:
            return []
        raw = row["accessible_by"]
        if isinstance(raw, str):
            return json.loads(raw) if raw else []
        if isinstance(raw, list):
            return list(raw)
        return []

    async def get_best_account(
        self, source: str, target_id: str, available: list[str],
    ) -> Optional[str]:
        """Return the available account that most recently succeeded on the target."""
        if not available:
            return None
        row = await self._pool.fetchrow(
            """
            SELECT accessing_account
            FROM profile_access_attempts
            WHERE source=$1 AND target_id=$2
              AND can_access = TRUE
              AND accessing_account = ANY($3::text[])
            ORDER BY attempt_ts DESC
            LIMIT 1
            """,
            source, target_id, available,
        )
        return row["accessing_account"] if row else None

    async def cleanup_old_attempts(self, days: int = 30) -> int:
        """Delete attempt rows older than *days* days. Returns deleted row count."""
        if days < 1:
            raise ValueError(f"days must be >=1, got {days!r}")
        result = await self._pool.execute(
            f"DELETE FROM profile_access_attempts "
            f"WHERE attempt_ts < NOW() - INTERVAL '{int(days)} days'"
        )
        # asyncpg returns "DELETE <n>"
        try:
            return int(result.split()[-1])
        except (ValueError, IndexError):
            return 0

    async def cleanup_inactive_profiles(self, days: int = 30) -> int:
        """Drop private-or-unknown profiles not checked in *days* days."""
        if days < 1:
            raise ValueError(f"days must be >=1, got {days!r}")
        result = await self._pool.execute(
            f"""
            DELETE FROM profile_access_summary
            WHERE (is_public = FALSE OR is_public IS NULL)
              AND (last_checked_ts IS NULL
                   OR last_checked_ts < NOW() - INTERVAL '{int(days)} days')
            """
        )
        try:
            return int(result.split()[-1])
        except (ValueError, IndexError):
            return 0

    async def get_statistics(self, source: Optional[str] = None) -> dict:
        """Aggregate stats. If source is given, scope to that source."""
        if source is None:
            attempts_row = await self._pool.fetchrow(
                """
                SELECT COUNT(*) AS total_attempts,
                       COUNT(*) FILTER (WHERE can_access) AS successful_attempts
                FROM profile_access_attempts
                """
            )
            profiles_row = await self._pool.fetchrow(
                "SELECT COUNT(*) AS unique_profiles FROM profile_access_summary"
            )
        else:
            attempts_row = await self._pool.fetchrow(
                """
                SELECT COUNT(*) AS total_attempts,
                       COUNT(*) FILTER (WHERE can_access) AS successful_attempts
                FROM profile_access_attempts
                WHERE source=$1
                """,
                source,
            )
            profiles_row = await self._pool.fetchrow(
                "SELECT COUNT(*) AS unique_profiles "
                "FROM profile_access_summary WHERE source=$1",
                source,
            )
        return {
            "source": source,
            "total_attempts": attempts_row["total_attempts"] if attempts_row else 0,
            "successful_attempts": (
                attempts_row["successful_attempts"] if attempts_row else 0
            ),
            "unique_profiles": (
                profiles_row["unique_profiles"] if profiles_row else 0
            ),
        }


class SmartAccountSelector:
    """Pick optimal account for (source, target) using ProfileAccessRepository.

    Selection priority:
        1. Account known to have successfully accessed this target before
           (most recent winner via ProfileAccessRepository.get_best_account).
        2. Fall back to the AccountPool's LRU rotation (caller is expected
           to handle this layer; we return None and caller picks).
    """

    def __init__(self, repo: ProfileAccessRepository, account_pool: Any = None):
        self._repo = repo
        self._pool = account_pool

    async def select_for_operation(
        self,
        source: str,
        target_id: str,
        available: list[str],
    ) -> Optional[str]:
        """Pick the best account among ``available`` for this target.

        Returns None if no available account has known access; caller
        should then fall back to ``account_pool.get_next()``.
        """
        if not available:
            return None
        return await self._repo.get_best_account(source, target_id, available)

    async def select_for_batch(
        self,
        source: str,
        target_ids: list[str],
        available: list[str],
    ) -> dict[str, Optional[str]]:
        """Map each target_id -> best account (or None if no known winner).

        Single round-trip per target. For very large batches consider
        a DataLoader-style group query; current N+1 is fine for batches
        up to ~hundreds of targets per pass.
        """
        if not target_ids:
            return {}
        out: dict[str, Optional[str]] = {}
        for tid in target_ids:
            out[tid] = await self._repo.get_best_account(source, tid, available)
        return out

    async def get_following_overlap(
        self,
        account: str,
        source: str,
        target_ids: list[str],
    ) -> set[str]:
        """Of these ``target_ids``, which is ``account`` known to access?

        Useful for "rank my N accounts by how much of the batch they
        cover" — caller can then assign batches to accounts greedily.
        """
        if not account or not target_ids:
            return set()
        rows = await self._repo._pool.fetch(
            """
            SELECT DISTINCT target_id
            FROM profile_access_attempts
            WHERE source=$1
              AND accessing_account=$2
              AND can_access = TRUE
              AND target_id = ANY($3::text[])
            """,
            source, account, target_ids,
        )
        return {r["target_id"] for r in rows}


__all__ = ["ProfileAccessRepository", "SmartAccountSelector"]
