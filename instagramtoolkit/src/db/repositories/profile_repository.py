"""ProfileRepository — replaces UserMetadataManager JSON I/O."""
from __future__ import annotations

import time
from typing import Any

from ..manager import DatabaseManager


class ProfileRepository:
    """Repository for profile data (profiles + profile_snapshots tables)."""

    def __init__(self, db: DatabaseManager) -> None:
        self._db = db

    def upsert_profile(self, username: str, data: dict) -> None:
        """Insert or update a profile row, insert a snapshot, and track username history."""
        now = time.time()
        user_id = data.get("user_id")
        full_name = data.get("full_name") or data.get("username", username)
        biography = data.get("biography", "")
        followers_count = int(data.get("followers_count", 0))
        following_count = int(data.get("following_count", 0))
        media_count = int(data.get("media_count", 0))
        is_public = 1 if data.get("is_public", True) else 0
        is_verified = 1 if data.get("is_verified", False) else 0
        collected_by = data.get("collected_by", data.get("collected_by_account", ""))

        with self._db.get_connection() as conn:
            # Upsert main profile row
            conn.execute(
                """
                INSERT INTO profiles
                    (username, full_name, biography, external_url, profile_pic_url,
                     followers_count, following_count, media_count,
                     is_public, is_verified, last_collected_ts, collected_by,
                     created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(username) DO UPDATE SET
                    full_name           = excluded.full_name,
                    biography           = excluded.biography,
                    external_url        = excluded.external_url,
                    profile_pic_url     = excluded.profile_pic_url,
                    followers_count     = excluded.followers_count,
                    following_count     = excluded.following_count,
                    media_count         = excluded.media_count,
                    is_public           = excluded.is_public,
                    is_verified         = excluded.is_verified,
                    last_collected_ts   = excluded.last_collected_ts,
                    collected_by        = excluded.collected_by,
                    updated_at          = excluded.updated_at
                """,
                (
                    username, full_name, biography,
                    data.get("external_url"), data.get("profile_pic_url"),
                    followers_count, following_count, media_count,
                    is_public, is_verified,
                    float(data.get("last_collected_ts", now)),
                    collected_by, now, now,
                ),
            )

            # Insert rich snapshot (includes bio/name so changes are tracked over time)
            conn.execute(
                """
                INSERT INTO profile_snapshots
                    (username, user_id, full_name, biography,
                     followers_count, following_count, media_count,
                     is_public, is_verified, collected_by, snapshot_ts)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    username, str(user_id) if user_id else None,
                    full_name, biography,
                    followers_count, following_count, media_count,
                    is_public, is_verified, collected_by, now,
                ),
            )

            # Track username history by user_id so renames are detected
            if user_id:
                conn.execute(
                    """
                    INSERT INTO username_history (user_id, username, first_seen_ts, last_seen_ts)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(user_id, username) DO UPDATE SET
                        last_seen_ts = excluded.last_seen_ts
                    """,
                    (str(user_id), username, now, now),
                )

    def get_profile(self, username: str) -> dict | None:
        """Return the profile dict for *username*, or None if not found."""
        return self._db.fetchone(
            "SELECT * FROM profiles WHERE username = ?", (username,)
        )

    def get_all_profiles(self) -> dict[str, dict]:
        """Return all profiles as a {username: dict} mapping."""
        rows = self._db.fetchall("SELECT * FROM profiles")
        return {r["username"]: r for r in rows}

    def get_top_by_followers(self, n: int) -> list[dict]:
        """Return the top *n* profiles by follower count (descending)."""
        return self._db.fetchall(
            "SELECT * FROM profiles ORDER BY followers_count DESC LIMIT ?", (n,)
        )

    def get_top_by_following(self, n: int) -> list[dict]:
        """Return the top *n* profiles by following count (descending)."""
        return self._db.fetchall(
            "SELECT * FROM profiles ORDER BY following_count DESC LIMIT ?", (n,)
        )

    def filter_by_follower_range(
        self, min_f: int, max_f: int | None = None
    ) -> list[str]:
        """Return usernames whose follower count is in [min_f, max_f]."""
        if max_f is None:
            rows = self._db.fetchall(
                "SELECT username FROM profiles WHERE followers_count >= ?", (min_f,)
            )
        else:
            rows = self._db.fetchall(
                "SELECT username FROM profiles WHERE followers_count >= ? AND followers_count <= ?",
                (min_f, max_f),
            )
        return [r["username"] for r in rows]

    def get_snapshots(self, username: str, limit: int = 90) -> list[dict]:
        """Return up to *limit* snapshots for *username*, newest first."""
        return self._db.fetchall(
            """
            SELECT * FROM profile_snapshots
            WHERE username = ?
            ORDER BY snapshot_ts DESC
            LIMIT ?
            """,
            (username, limit),
        )

    def get_username_history(self, user_id: str) -> list[dict]:
        """Return all known usernames for a given user_id (rename history)."""
        return self._db.fetchall(
            """
            SELECT username, first_seen_ts, last_seen_ts
            FROM username_history
            WHERE user_id = ?
            ORDER BY first_seen_ts ASC
            """,
            (user_id,),
        )

    def get_profile_changes(self, username: str, limit: int = 10) -> list[dict]:
        """Return snapshots where bio or full_name changed vs the previous snapshot."""
        snapshots = self.get_snapshots(username, limit=limit + 1)
        if len(snapshots) < 2:
            return []
        changes = []
        for i in range(len(snapshots) - 1):
            curr = snapshots[i]
            prev = snapshots[i + 1]
            diff = {}
            for field in ("full_name", "biography", "followers_count",
                          "following_count", "media_count", "is_public", "is_verified"):
                if curr.get(field) != prev.get(field):
                    diff[field] = {"from": prev.get(field), "to": curr.get(field)}
            if diff:
                changes.append({
                    "snapshot_ts": curr["snapshot_ts"],
                    "changes": diff,
                })
        return changes

    def needs_refresh(self, username: str, max_age_hours: float = 24.0) -> bool:
        """Return True if profile has never been scanned or was last scanned > max_age_hours ago."""
        row = self._db.fetchone(
            "SELECT last_collected_ts FROM profiles WHERE username = ?", (username,)
        )
        if row is None:
            return True
        age_hours = (time.time() - row["last_collected_ts"]) / 3600
        return age_hours > max_age_hours


__all__ = ["ProfileRepository"]


