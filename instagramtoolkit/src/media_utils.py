"""Shared media download utilities.

Provides helper functions to reduce duplication between MediaDownloader and
FollowingMediaDownloader for profile retrieval and common safety checks.
"""
from __future__ import annotations

from typing import Optional, Dict
import instaloader


def get_profile(loader: instaloader.Instaloader, username: str) -> Optional[instaloader.Profile]:
    """Fetch an Instaloader Profile object for a username with basic validation.

    Raises retryable exceptions (ConnectionException, etc.) so that callers
    using retry_with_backoff() can retry them.  Only returns None for
    non-retryable errors like ProfileNotExistsException.
    """
    if not loader or not hasattr(loader, "context"):
        raise RuntimeError("Invalid loader context")
    try:
        return instaloader.Profile.from_username(loader.context, username)
    except instaloader.exceptions.ProfileNotExistsException:
        return None
    # All other exceptions (ConnectionException, QueryReturnedBadRequestException,
    # LoginRequiredException, etc.) propagate up to retry_with_backoff.


def is_accessible_private(profile: instaloader.Profile) -> bool:
    """Return True if profile is private but followed (accessible)."""
    if profile is None:
        return False
    return profile.is_private and getattr(profile, "followed_by_viewer", False)


def profile_access_blocked(profile: instaloader.Profile) -> bool:
    """Return True if profile is private and not followed (blocked content)."""
    if profile is None:
        return True
    return profile.is_private and not getattr(profile, "followed_by_viewer", False)


def summarize_profile(profile: instaloader.Profile) -> Dict[str, object]:
    """Produce a lightweight summary dict for debug logs or analytics."""
    if profile is None:
        return {"exists": False}
    return {
        "exists": True,
        "username": profile.username,
        "is_private": profile.is_private,
        "followed_by_viewer": getattr(profile, "followed_by_viewer", None),
        "has_blocked_viewer": getattr(profile, "has_blocked_viewer", None),
    }

__all__ = [
    "get_profile",
    "is_accessible_private",
    "profile_access_blocked",
    "summarize_profile",
]


