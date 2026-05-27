"""RelationshipRepository — replaces RelationshipCollector JSON I/O."""
from __future__ import annotations

import time
from typing import Any

from ..manager import DatabaseManager


class RelationshipRepository:
    """Repository for follower/following relationship data."""

    def __init__(self, db: DatabaseManager) -> None:
        self._db = db

    def upsert_relationship(
        self,
        source: str,
        target: str,
        rel_type: str,
        collected_by: str,
        source_is_public: bool,
    ) -> None:
        """Insert or replace a single relationship row."""
        self._db.execute(
            """
            INSERT OR REPLACE INTO relationships
                (source, target, type, collected_by, source_is_public, collected_ts)
            VALUES (?,?,?,?,?,?)
            """,
            (source, target, rel_type, collected_by, 1 if source_is_public else 0, time.time()),
        )

    def bulk_upsert(self, relationships: list[dict]) -> int:
        """Insert or replace a batch of relationships, deduplicating within the batch.

        Returns the count of unique rows inserted/updated.
        """
        if not relationships:
            return 0

        seen: set[tuple] = set()
        deduped: list[dict] = []
        for r in relationships:
            key = (r["source"], r["target"], r["type"])
            if key not in seen:
                seen.add(key)
                deduped.append(r)

        now = time.time()
        params_seq = [
            (
                r["source"],
                r["target"],
                r["type"],
                r.get("collected_by", r.get("collected_by_account", "")),
                1 if r.get("source_is_public", True) else 0,
                r.get("source_followed_by_collector", 0),
                now,
            )
            for r in deduped
        ]
        with self._db.get_connection() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO relationships
                    (source, target, type, collected_by, source_is_public,
                     source_followed_by_collector, collected_ts)
                VALUES (?,?,?,?,?,?,?)
                """,
                params_seq,
            )
        return len(deduped)

    def get_relationships(
        self,
        source: str | None = None,
        rel_type: str | None = None,
    ) -> list[dict]:
        """Return relationships filtered by optional source and/or type."""
        if source and rel_type:
            return self._db.fetchall(
                "SELECT * FROM relationships WHERE source=? AND type=?",
                (source, rel_type),
            )
        elif source:
            return self._db.fetchall(
                "SELECT * FROM relationships WHERE source=?", (source,)
            )
        elif rel_type:
            return self._db.fetchall(
                "SELECT * FROM relationships WHERE type=?", (rel_type,)
            )
        return self._db.fetchall("SELECT * FROM relationships")

    def get_followers(self, username: str) -> list[str]:
        """Return all sources that have a 'followers' row with target=username."""
        rows = self._db.fetchall(
            "SELECT source FROM relationships WHERE target=? AND type='followers'",
            (username,),
        )
        return [r["source"] for r in rows]

    def get_following(self, username: str) -> list[str]:
        """Return all targets that username follows (type='following')."""
        rows = self._db.fetchall(
            "SELECT target FROM relationships WHERE source=? AND type='following'",
            (username,),
        )
        return [r["target"] for r in rows]

    def get_mutual(self, username: str) -> list[str]:
        """Return usernames that both follow username and are followed by username."""
        rows = self._db.fetchall(
            """
            SELECT DISTINCT r1.source AS mutual
            FROM relationships r1
            JOIN relationships r2
              ON r1.source = r2.target
             AND r2.source = ?
             AND r2.type   = 'following'
            WHERE r1.target = ?
              AND r1.type   = 'followers'
            """,
            (username, username),
        )
        return [r["mutual"] for r in rows]

    def relationship_exists(self, source: str, target: str, rel_type: str) -> bool:
        """Return True if the given relationship row exists."""
        row = self._db.fetchone(
            "SELECT 1 FROM relationships WHERE source=? AND target=? AND type=?",
            (source, target, rel_type),
        )
        return row is not None

    def get_all_usernames(self) -> list[str]:
        """Return all distinct usernames appearing as source or target."""
        rows = self._db.fetchall(
            """
            SELECT DISTINCT username FROM (
                SELECT source AS username FROM relationships
                UNION
                SELECT target AS username FROM relationships
            )
            """
        )
        return [r["username"] for r in rows]


__all__ = ["RelationshipRepository"]


