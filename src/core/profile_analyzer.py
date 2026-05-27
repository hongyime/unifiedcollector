"""Generic profile / network analyzer.

Ported from ``instagramtoolkit/src/profile_analyzer.py`` (metadata-driven
network analytics) and generalised so other platform collectors can reuse
the same statistics shape.

This module is INTENTIONALLY backend-agnostic:

  * The analyser receives an iterable of profile dicts. It does not fetch
    them itself — collectors hand them in.
  * Optional image-side hooks (``analyze_profile_image``) are defined as
    pure functions returning ``Optional[Dict]`` so callers can wrap them
    in ``try/except`` and ignore when Pillow / classifier deps aren't
    installed. The default implementation only inspects byte-level
    features (size, format magic bytes) — no ML weights are required.

Callers
-------

Instagram collector:
    from src.core.profile_analyzer import ProfileAnalyzer
    analyzer = ProfileAnalyzer()
    stats = analyzer.analyze_profiles([user_data])
    # stats is a dict — log or persist it.

The shape of each profile dict is the same the toolkit's
``UserMetadataManager`` produced:

    {
        "username": str,
        "full_name": str | None,
        "followers_count": int,
        "following_count": int,
        "is_verified": bool,
        "is_private": bool,
        ...
    }

Missing keys are treated as zero / empty.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

# Influencer tier thresholds. Public so tests / dashboards can re-use them.
TIERS = (
    ("micro_influencers", 1_000, 10_000),
    ("small_influencers", 10_000, 50_000),
    ("medium_influencers", 50_000, 100_000),
    ("large_influencers", 100_000, 500_000),
    ("mega_influencers", 500_000, 1_000_000),
    ("celebrities", 1_000_000, float("inf")),
)


class ProfileAnalyzer:
    """Generate network insights from a collection of profile dicts."""

    def analyze_profiles(self, profiles: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
        """Generate comprehensive network analysis.

        Args:
            profiles: iterable of profile dicts (any shape with the keys
                listed in the module docstring; missing keys are tolerated).

        Returns:
            Dict with totals, tiers, top-N lists, and high-engagement
            candidates. Always includes ``analysis_timestamp``.
        """
        plist: List[Dict[str, Any]] = list(profiles)
        stats: Dict[str, Any] = {
            "total_profiles": len(plist),
            "public_profiles": 0,
            "private_profiles": 0,
            "verified_profiles": 0,
            "avg_followers": 0,
            "avg_following": 0,
            "avg_follower_to_following_ratio": 0.0,
            "influencer_tiers": {name: 0 for name, _, _ in TIERS},
            "top_followers": [],
            "top_following": [],
            "high_engagement_potential": [],
        }

        if not plist:
            stats["analysis_timestamp"] = time.time()
            stats["analysis_date"] = time.strftime("%Y-%m-%d %H:%M:%S")
            return stats

        followers_total = 0
        following_total = 0
        ratios: List[float] = []
        high_engagement: List[Dict[str, Any]] = []

        for p in plist:
            f = int(p.get("followers_count", 0) or 0)
            g = int(p.get("following_count", 0) or 0)
            followers_total += f
            following_total += g

            if p.get("is_private"):
                stats["private_profiles"] += 1
            else:
                stats["public_profiles"] += 1
            if p.get("is_verified"):
                stats["verified_profiles"] += 1

            if g > 0:
                ratios.append(f / g)

            for name, lo, hi in TIERS:
                if lo <= f < hi:
                    stats["influencer_tiers"][name] += 1
                    break

            if f > 5_000 and g < 1_000:
                high_engagement.append({
                    "username": p.get("username"),
                    "followers_count": f,
                    "following_count": g,
                    "ratio": (f / g) if g > 0 else float(f),
                })

        stats["avg_followers"] = followers_total / len(plist)
        stats["avg_following"] = following_total / len(plist)
        stats["avg_follower_to_following_ratio"] = (
            sum(ratios) / len(ratios) if ratios else 0.0
        )

        # Top-N — stable, sorted desc by the metric.
        top_followers_sorted = sorted(
            plist, key=lambda p: int(p.get("followers_count", 0) or 0), reverse=True,
        )[:10]
        top_following_sorted = sorted(
            plist, key=lambda p: int(p.get("following_count", 0) or 0), reverse=True,
        )[:10]

        stats["top_followers"] = [
            {
                "username": p.get("username"),
                "followers_count": int(p.get("followers_count", 0) or 0),
                "is_verified": bool(p.get("is_verified")),
            }
            for p in top_followers_sorted
        ]
        stats["top_following"] = [
            {
                "username": p.get("username"),
                "following_count": int(p.get("following_count", 0) or 0),
            }
            for p in top_following_sorted
        ]

        high_engagement.sort(key=lambda x: x["ratio"], reverse=True)
        stats["high_engagement_potential"] = high_engagement[:20]

        stats["analysis_timestamp"] = time.time()
        stats["analysis_date"] = time.strftime("%Y-%m-%d %H:%M:%S")
        return stats

    def get_influential_users(
        self, profiles: Iterable[Dict[str, Any]], min_followers: int = 10_000,
    ) -> List[Dict[str, Any]]:
        """Return profiles meeting a follower threshold."""
        return [
            p for p in profiles
            if int(p.get("followers_count", 0) or 0) >= min_followers
        ]


def analyze_profile_image(image_bytes: Optional[bytes]) -> Optional[Dict[str, Any]]:
    """Lightweight backend-agnostic image inspection hook.

    Returns shape:
        { "size_bytes": int, "format": str, "ok": bool }
    or ``None`` on error / empty input. Callers should wrap in try/except
    so a malformed image never breaks a collection run.

    The intention is to leave a slot where heavier ML classifiers (NSFW,
    face detection, etc.) can be plugged in later without changing the
    collector wire-up. Today it just sniffs magic bytes — enough to make
    the contract real and unit-testable without optional deps.
    """
    if not image_bytes:
        return None
    try:
        n = len(image_bytes)
        if n < 4:
            return None
        head = image_bytes[:12]
        fmt = "unknown"
        if head.startswith(b"\xff\xd8\xff"):
            fmt = "jpeg"
        elif head.startswith(b"\x89PNG\r\n\x1a\n"):
            fmt = "png"
        elif head.startswith(b"GIF8"):
            fmt = "gif"
        elif head[0:4] == b"RIFF" and head[8:12] == b"WEBP":
            fmt = "webp"
        return {"size_bytes": n, "format": fmt, "ok": fmt != "unknown"}
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("analyze_profile_image error: %s", e)
        return None
