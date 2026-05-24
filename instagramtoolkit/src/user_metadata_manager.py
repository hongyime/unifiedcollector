"""User metadata management for storing profile information.

Manages follower/following counts and other profile metadata
for all scraped profiles, enabling network analysis and filtering.

Persistence is delegated to ProfileRepository (SQLite/PostgreSQL).
"""
import os
import sys
import time
from typing import Dict, Any, Optional, List

# Ensure src/ is on the path when imported from tests
_LIB_DIR = os.path.dirname(os.path.abspath(__file__))
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from src.config import DATA_DIR


def _get_db():
    """Return a module-level DatabaseManager singleton."""
    import os as _os
    from db.manager import DatabaseManager
    _url = _os.environ.get("DATABASE_URL", "")
    # Use a module-level singleton
    if not hasattr(_get_db, "_instance") or _get_db._instance is None:
        _get_db._instance = DatabaseManager(_url)
    return _get_db._instance


_get_db._instance = None


class UserMetadataManager:
    """Manage profile metadata for scraped users.

    Tracks follower counts, following counts, and other profile information
    for network analysis and influence identification.

    All persistence is delegated to ProfileRepository.
    """

    def __init__(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        from db.repositories.profile_repository import ProfileRepository
        self._repo = ProfileRepository(_get_db())

    # ── Private helpers (kept for backward compat; no-ops now) ────────────

    def _load_metadata(self) -> Dict[str, Any]:
        """Deprecated: data now lives in the DB."""
        return self._repo.get_all_profiles()

    def _save_metadata(self):
        """Deprecated: data is persisted immediately via the repository."""
        pass

    # ── Public API (unchanged signatures) ─────────────────────────────────

    def update_profile(self, username: str, profile_obj, account_name: str):
        """Update metadata for a specific profile.

        Args:
            username: Profile username
            profile_obj: Instaloader Profile object
            account_name: Account used to collect this data
        """
        try:
            followers_count = getattr(profile_obj, 'followers', 0) or 0
            following_count = getattr(profile_obj, 'followees', 0) or 0
            user_id = getattr(profile_obj, 'userid', None)

            data = {
                'username': username,
                'user_id': str(user_id) if user_id else None,
                'followers_count': followers_count,
                'following_count': following_count,
                'is_public': not profile_obj.is_private,
                'is_verified': getattr(profile_obj, 'is_verified', False),
                'profile_pic_url': getattr(profile_obj, 'profile_pic_url', None),
                'biography': getattr(profile_obj, 'biography', ''),
                'full_name': getattr(profile_obj, 'full_name', username),
                'external_url': getattr(profile_obj, 'external_url', None),
                'last_collected_ts': time.time(),
                'collected_by': account_name,
                'media_count': getattr(profile_obj, 'mediacount', 0),
            }

            # upsert_profile now handles snapshot + username_history internally
            self._repo.upsert_profile(username, data)
            print(f"[METADATA] Saved profile for {username} ({followers_count} followers, {following_count} following, {data['media_count']} posts)")

        except Exception as e:
            print(f"[WARNING] Could not update metadata for {username}: {e}")

    def get_profile(self, username: str) -> Optional[Dict[str, Any]]:
        """Get metadata for a specific profile."""
        return self._repo.get_profile(username)

    def get_all_profiles(self) -> Dict[str, Any]:
        """Get all profile metadata."""
        return self._repo.get_all_profiles()

    def get_profile_count(self) -> int:
        """Get total number of profiles tracked."""
        rows = self._repo.get_all_profiles()
        return len(rows)

    def get_top_followers(self, n: int = 10) -> List[Dict[str, Any]]:
        """Get top N profiles by follower count."""
        return self._repo.get_top_by_followers(n)

    def get_top_following(self, n: int = 10) -> List[Dict[str, Any]]:
        """Get top N profiles by following count."""
        return self._repo.get_top_by_following(n)

    def get_public_profiles(self) -> List[str]:
        """Get list of public profile usernames."""
        rows = self._repo.get_all_profiles()
        return [u for u, d in rows.items() if d.get('is_public', 1)]

    def get_private_profiles(self) -> List[str]:
        """Get list of private profile usernames."""
        rows = self._repo.get_all_profiles()
        return [u for u, d in rows.items() if not d.get('is_public', 1)]

    def get_verified_profiles(self) -> List[str]:
        """Get list of verified profile usernames."""
        rows = self._repo.get_all_profiles()
        return [u for u, d in rows.items() if d.get('is_verified', 0)]

    def filter_by_follower_count(
        self, min_followers: int = 0, max_followers: int = None
    ) -> List[str]:
        """Filter profiles by follower count range."""
        return self._repo.filter_by_follower_range(min_followers, max_followers)

    def get_network_stats(self) -> Dict[str, Any]:
        """Generate network statistics from metadata."""
        profiles = self._repo.get_all_profiles()
        if not profiles:
            return {
                'total_profiles': 0,
                'public_profiles': 0,
                'private_profiles': 0,
                'verified_profiles': 0,
                'avg_followers': 0,
                'avg_following': 0,
                'top_followers': [],
                'top_following': [],
            }

        profile_list = list(profiles.values())
        public_count = sum(1 for p in profile_list if p.get('is_public', 1))
        verified_count = sum(1 for p in profile_list if p.get('is_verified', 0))
        follower_counts = [p.get('followers_count', 0) for p in profile_list]
        following_counts = [p.get('following_count', 0) for p in profile_list]

        return {
            'total_profiles': len(profile_list),
            'public_profiles': public_count,
            'private_profiles': len(profile_list) - public_count,
            'verified_profiles': verified_count,
            'avg_followers': sum(follower_counts) / len(follower_counts) if follower_counts else 0,
            'avg_following': sum(following_counts) / len(following_counts) if following_counts else 0,
            'top_followers': self.get_top_followers(10),
            'top_following': self.get_top_following(10),
        }

    def is_within_follower_limit(self, username: str, max_followers: int) -> bool:
        """Check if a profile's follower count is within the configured limit."""
        if max_followers <= 0:
            return True
        profile = self.get_profile(username)
        if profile is None:
            return True
        return profile.get('followers_count', 0) <= max_followers

    def clear_metadata(self):
        """Clear all metadata (use with caution)."""
        # Not supported in DB mode — would require DELETE FROM profiles
        print("[METADATA] clear_metadata() is a no-op in database mode")


__all__ = ['UserMetadataManager']


