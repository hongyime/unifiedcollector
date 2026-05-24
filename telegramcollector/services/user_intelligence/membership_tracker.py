"""
MembershipTracker: upserts user_chat_memberships rows from user sightings.
"""
from __future__ import annotations

import asyncpg


class MembershipTracker:
    def __init__(self, db_pool: asyncpg.Pool) -> None:
        """
        db_pool: asyncpg pool (user_intel_user credentials).
        """
        self._pool = db_pool

    async def process_sighting(self, sighting: dict) -> bool:
        """
        Upserts a user_chat_memberships row for this sighting.

        sighting keys used: user_id, seen_in_chat_id

        Returns:
          True  — the (user_id, chat_id) pair is new (INSERT path was taken)
          False — the pair already existed (UPDATE path), or seen_in_chat_id is NULL

        Algorithm:
          1. If sighting['seen_in_chat_id'] is None: return False
          2. Execute upsert with RETURNING (xmax = 0) AS is_insert
          3. Return True if INSERT path, False if UPDATE path
        """
        chat_id = sighting.get("seen_in_chat_id")
        if chat_id is None:
            return False

        user_id: int = sighting["user_id"]

        row = await self._pool.fetchrow(
            """
            INSERT INTO user_intelligence.user_chat_memberships
                (user_id, chat_id, first_seen, last_seen, message_count)
            VALUES ($1, $2, NOW(), NOW(), 1)
            ON CONFLICT (user_id, chat_id)
            DO UPDATE SET last_seen     = NOW(),
                          message_count = user_chat_memberships.message_count + 1
            RETURNING (xmax = 0) AS is_insert;
            """,
            user_id,
            chat_id,
        )

        return bool(row["is_insert"])
