"""Per-account cooldown and daily quota tracking.

When an account hits a rate-limit or block, it is placed on cooldown and
skipped during rotation until the cooldown expires.

Daily quotas prevent exceeding Instagram's known thresholds proactively,
rather than waiting to get blocked.

Persistence is delegated to AccountCooldownRepository / AccountQuotaRepository.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime
from typing import Any

_LIB_DIR = os.path.dirname(os.path.abspath(__file__))
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from src.config import (
    DATA_DIR,
    ACCOUNT_COOLDOWN_MINUTES,
    DAILY_QUOTA_PROFILE_VIEWS,
    DAILY_QUOTA_ACTIONS,
    QUOTA_RESET_HOUR,
)


def _get_db():
    """Return a module-level DatabaseManager singleton."""
    import os as _os
    from db.manager import DatabaseManager
    if not hasattr(_get_db, "_instance") or _get_db._instance is None:
        _get_db._instance = DatabaseManager(_os.environ.get("DATABASE_URL", ""))
    return _get_db._instance


_get_db._instance = None


class AccountCooldownManager:
    """Tracks per-account cooldowns so blocked accounts are not reused immediately."""

    def __init__(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        from db.repositories.account_cooldown_repository import AccountCooldownRepository
        self._repo = AccountCooldownRepository(_get_db())

    # ── Public API (unchanged signatures) ─────────────────────────────────

    def put_on_cooldown(self, account_name: str, minutes: int = ACCOUNT_COOLDOWN_MINUTES, reason: str = "rate-limit"):
        """Place *account_name* on cooldown for *minutes* minutes."""
        until = time.time() + minutes * 60
        self._repo.put_on_cooldown(account_name, until, reason)
        print(f"[COOLDOWN] {account_name} on cooldown for {minutes}m (reason: {reason})")

    def is_on_cooldown(self, account_name: str) -> bool:
        """Return True if *account_name* is still cooling down."""
        return self._repo.is_on_cooldown(account_name)

    def get_cooldown_remaining(self, account_name: str) -> float:
        """Return seconds remaining on cooldown, or 0."""
        return self._repo.get_remaining(account_name)

    def get_available_accounts(self, account_names: list[str]) -> list[str]:
        """Filter to accounts NOT on cooldown."""
        return self._repo.get_available(account_names)

    def clear_cooldown(self, account_name: str):
        """Manually clear cooldown (e.g. after login success)."""
        self._repo.clear_cooldown(account_name)


class AccountQuotaManager:
    """Tracks daily API usage per account to stay under Instagram thresholds."""

    def __init__(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        from db.repositories.account_quota_repository import AccountQuotaRepository
        self._repo = AccountQuotaRepository(_get_db())

    def _ensure_account(self, account_name: str):
        """Ensure quota row exists and reset if new day."""
        self._repo.reset_if_new_day(account_name)

    # ── Public API (unchanged signatures) ─────────────────────────────────

    def record_profile_view(self, account_name: str, count: int = 1):
        self._repo.reset_if_new_day(account_name)
        self._repo.record_profile_view(account_name, count)

    def record_action(self, account_name: str, count: int = 1):
        self._repo.reset_if_new_day(account_name)
        self._repo.record_action(account_name, count)

    def get_daily_usage(self, account_name: str) -> dict:
        """Return raw usage dict for account: {'profile_views': int, 'actions': int}."""
        self._repo.reset_if_new_day(account_name)
        return self._repo.get_usage(account_name)

    def can_view_profiles(self, account_name: str) -> bool:
        """Return True if account hasn't exceeded daily profile-view budget."""
        if DAILY_QUOTA_PROFILE_VIEWS <= 0:
            return True
        self._repo.reset_if_new_day(account_name)
        usage = self._repo.get_usage(account_name)
        return usage.get('profile_views', 0) < DAILY_QUOTA_PROFILE_VIEWS

    def can_perform_action(self, account_name: str) -> bool:
        """Return True if account hasn't exceeded daily action budget."""
        if DAILY_QUOTA_ACTIONS <= 0:
            return True
        self._repo.reset_if_new_day(account_name)
        usage = self._repo.get_usage(account_name)
        return usage.get('actions', 0) < DAILY_QUOTA_ACTIONS

    def get_usage_summary(self, account_name: str) -> dict[str, str]:
        self._repo.reset_if_new_day(account_name)
        usage = self._repo.get_usage(account_name)
        return {
            "profile_views": f"{usage.get('profile_views', 0)}/{DAILY_QUOTA_PROFILE_VIEWS}",
            "actions": f"{usage.get('actions', 0)}/{DAILY_QUOTA_ACTIONS}",
            "date": usage.get('quota_date', ''),
        }

    def print_all_usage(self):
        """Print usage for all tracked accounts."""
        print("\n[QUOTA] Daily usage summary:")
        print("=" * 50)
        rows = _get_db().fetchall("SELECT * FROM account_quotas")
        for row in rows:
            print(
                f"  {row['account_name']}: "
                f"views={row['profile_views']}/{DAILY_QUOTA_PROFILE_VIEWS}  "
                f"actions={row['actions']}/{DAILY_QUOTA_ACTIONS}  "
                f"({row['quota_date']})"
            )
        if not rows:
            print("  No usage data yet.")


__all__ = ["AccountCooldownManager", "AccountQuotaManager"]


