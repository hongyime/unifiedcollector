"""AccountCooldownRepository — replaces AccountCooldownManager JSON I/O."""
from __future__ import annotations

import time

from ..manager import DatabaseManager


class AccountCooldownRepository:
    """Repository for per-account cooldown tracking."""

    def __init__(self, db: DatabaseManager) -> None:
        self._db = db

    def put_on_cooldown(self, account: str, until_ts: float, reason: str = "rate-limit") -> None:
        """Insert or replace the cooldown row for *account*."""
        self._db.execute(
            """
            INSERT OR REPLACE INTO account_cooldowns
                (account_name, until_ts, reason, created_at)
            VALUES (?,?,?,?)
            """,
            (account, until_ts, reason, time.time()),
        )

    def is_on_cooldown(self, account: str) -> bool:
        """Return True if *account* is still cooling down.

        Lazily deletes the row when the cooldown has expired.
        """
        row = self._db.fetchone(
            "SELECT until_ts FROM account_cooldowns WHERE account_name=?", (account,)
        )
        if row is None:
            return False
        if time.time() >= row["until_ts"]:
            # Expired — clean up
            self._db.execute(
                "DELETE FROM account_cooldowns WHERE account_name=?", (account,)
            )
            return False
        return True

    def get_remaining(self, account: str) -> float:
        """Return seconds remaining on cooldown, or 0.0."""
        row = self._db.fetchone(
            "SELECT until_ts FROM account_cooldowns WHERE account_name=?", (account,)
        )
        if row is None:
            return 0.0
        remaining = row["until_ts"] - time.time()
        return max(remaining, 0.0)

    def clear_cooldown(self, account: str) -> None:
        """Delete the cooldown row for *account* if it exists."""
        self._db.execute(
            "DELETE FROM account_cooldowns WHERE account_name=?", (account,)
        )

    def get_available(self, accounts: list[str]) -> list[str]:
        """Return accounts from *accounts* that are NOT on cooldown."""
        return [a for a in accounts if not self.is_on_cooldown(a)]


__all__ = ["AccountCooldownRepository"]


