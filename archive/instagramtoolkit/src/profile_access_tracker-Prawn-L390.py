# Profile Access Tracker - Track which accounts can access which profiles
import os
import sys
import time
import random
from datetime import datetime

_LIB_DIR = os.path.dirname(os.path.abspath(__file__))
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from src.config import DATA_DIR


def _get_db():
    """Return a module-level DatabaseManager singleton."""
    import os as _os
    from db.manager import DatabaseManager
    if not hasattr(_get_db, "_instance") or _get_db._instance is None:
        _get_db._instance = DatabaseManager(_os.environ.get("DATABASE_URL", ""))
    return _get_db._instance


_get_db._instance = None


class ProfileAccessTracker:
    """Track which Instagram accounts can access which profiles.

    All persistence is delegated to ProfileAccessRepository.
    """

    def __init__(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        from db.repositories.profile_access_repository import ProfileAccessRepository
        self._repo = ProfileAccessRepository(_get_db())

        # Periodic cleanup (10% chance on startup, BUG-009 fix)
        if random.random() < 0.1:
            self.cleanup_old_profiles(days_inactive=30)

    # ── Deprecated persistence helpers (kept for backward compat) ─────────

    def _load_access_data(self):
        """Deprecated: data now lives in the DB."""
        return {"profiles": {}, "last_updated": datetime.now().isoformat(), "version": "1.0"}

    def save_access_data(self):
        """Deprecated: data is persisted immediately via the repository."""
        return True

    # ── Public API (unchanged signatures) ─────────────────────────────────

    def record_profile_access(self, target_username, accessing_account, access_result):
        """Record the result of an access attempt.

        Args:
            target_username: The profile being accessed
            accessing_account: Account name attempting access
            access_result: Dict with keys like 'can_access', 'is_public', 'is_followed', 'error'
        """
        self._repo.record_attempt(
            target=target_username,
            account=accessing_account,
            can_access=bool(access_result.get('can_access', False)),
            is_public=access_result.get('is_public', None),
            is_followed=bool(access_result.get('is_followed', False)),
            error=access_result.get('error', None),
        )

    def get_best_account_for_profile(self, target_username, available_accounts):
        """Get the best account to use for accessing a specific profile.

        Args:
            target_username: The profile to access
            available_accounts: List of account names available for use

        Returns:
            str: Best account name to use, or None if no preference
        """
        summary = self.get_profile_summary(target_username)
        if summary.get('is_public'):
            return None  # Any account works for public profiles

        return self._repo.get_best_account(target_username, available_accounts)

    def get_profile_summary(self, target_username):
        """Get summary of what we know about a profile's accessibility."""
        return self._repo.get_profile_summary(target_username)

    def get_following_accounts(self, target_username):
        """Return the list of accounts known to follow (have access to) a profile.

        Requirements: 3.6, 3.7
        """
        summary = self.get_profile_summary(target_username)
        return summary.get('accessible_by', [])

    def get_access_statistics(self):
        """Get overall statistics about profile access."""
        stats = self._repo.get_statistics()
        return {
            'total_profiles_tracked': stats.get('unique_profiles', 0),
            'public_profiles': 0,   # not tracked separately in DB summary
            'private_profiles': 0,
            'unknown_visibility': 0,
            'account_access_counts': {},
        }

    def cleanup_old_data(self, days_old=30):
        """Remove access attempts older than specified days."""
        removed = self._repo.cleanup_old_attempts(days_old)
        print(f"[CLEAN] Cleaned up access data older than {days_old} days ({removed} rows)")

    def cleanup_old_profiles(self, days_inactive=30):
        """Remove inactive private profiles from tracking.

        Args:
            days_inactive: Remove private profiles inactive for this many days

        Returns:
            int: Number of profiles removed
        """
        removed = self._repo.cleanup_inactive_profiles(days_inactive)
        if removed:
            print(f"[CLEAN] Removed {removed} inactive private profiles (older than {days_inactive} days)")
        return removed


def print_access_statistics():
    """Print profile access statistics."""
    tracker = ProfileAccessTracker()
    stats = tracker.get_access_statistics()

    print("\n[STATS] Profile Access Statistics")
    print("=" * 50)
    print(f"Total profiles tracked: {stats['total_profiles_tracked']}")
    print(f"Public profiles: {stats['public_profiles']}")
    print(f"Private profiles: {stats['private_profiles']}")
    print(f"Unknown visibility: {stats['unknown_visibility']}")

    print("\n🔑 Account Access Counts:")
    for account, count in stats['account_access_counts'].items():
        print(f"  {account}: {count} profiles accessible")


