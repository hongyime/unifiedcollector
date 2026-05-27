"""AccountQuotaRepository — replaces AccountQuotaManager JSON I/O."""
from __future__ import annotations

import time
from datetime import date

from ..manager import DatabaseManager


def _today() -> str:
    return date.today().isoformat()


class AccountQuotaRepository:
    """Repository for per-account daily quota tracking."""

    def __init__(self, db: DatabaseManager) -> None:
        self._db = db

    def _ensure_row(self, account: str) -> None:
        """Insert a quota row for *account* if one doesn't exist."""
        self._db.execute(
            """
            INSERT OR IGNORE INTO account_quotas
                (account_name, quota_date, profile_views, actions, updated_at)
            VALUES (?,?,0,0,?)
            """,
            (account, _today(), time.time()),
        )

    def record_profile_view(self, account: str, count: int = 1) -> None:
        """Increment profile_views for *account* by *count*."""
        self._ensure_row(account)
        self._db.execute(
            """
            UPDATE account_quotas
            SET profile_views = profile_views + ?,
                updated_at    = ?
            WHERE account_name = ?
            """,
            (count, time.time(), account),
        )

    def record_action(self, account: str, count: int = 1) -> None:
        """Increment actions for *account* by *count*."""
        self._ensure_row(account)
        self._db.execute(
            """
            UPDATE account_quotas
            SET actions    = actions + ?,
                updated_at = ?
            WHERE account_name = ?
            """,
            (count, time.time(), account),
        )

    def get_usage(self, account: str) -> dict:
        """Return {profile_views, actions, quota_date} for *account*."""
        self._ensure_row(account)
        row = self._db.fetchone(
            "SELECT profile_views, actions, quota_date FROM account_quotas WHERE account_name=?",
            (account,),
        )
        if row is None:
            return {"profile_views": 0, "actions": 0, "quota_date": _today()}
        return dict(row)

    def reset_if_new_day(self, account: str) -> None:
        """Reset counters to 0 if the stored quota_date differs from today."""
        self._ensure_row(account)
        today = _today()
        self._db.execute(
            """
            UPDATE account_quotas
            SET profile_views = 0,
                actions       = 0,
                quota_date    = ?,
                updated_at    = ?
            WHERE account_name = ? AND quota_date != ?
            """,
            (today, time.time(), account, today),
        )


__all__ = ["AccountQuotaRepository"]


