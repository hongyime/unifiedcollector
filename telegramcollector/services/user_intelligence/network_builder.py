from __future__ import annotations

import asyncpg


class NetworkBuilder:
    def __init__(self, db_pool: asyncpg.Pool) -> None:
        self._pool = db_pool

    async def process_new_membership(self, new_user_id: int, chat_id: int) -> None:
        """
        Called only when MembershipTracker returns is_new_membership=True.
        Fetches all existing members of the chat and upserts an edge between
        the new user and each of them.
        """
        other_users = await self._fetch_chat_members(chat_id, exclude_user_id=new_user_id)
        for other_user_id in other_users:
            await self._upsert_edge(new_user_id, other_user_id)

    async def _fetch_chat_members(self, chat_id: int, exclude_user_id: int) -> list[int]:
        """
        Returns all user_ids in user_chat_memberships for this chat_id,
        excluding the newly joined user.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT user_id
                  FROM user_intelligence.user_chat_memberships
                 WHERE chat_id = $1
                   AND user_id != $2;
                """,
                chat_id,
                exclude_user_id,
            )
        return [row["user_id"] for row in rows]

    async def _upsert_edge(self, user_id_x: int, user_id_y: int) -> None:
        """
        Upserts one edge into user_connections, enforcing user_id_a < user_id_b
        via LEAST/GREATEST. Increments shared_chat_count on conflict.
        """
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO user_intelligence.user_connections
                    (user_id_a, user_id_b, shared_chat_count, last_updated)
                VALUES (LEAST($1, $2), GREATEST($1, $2), 1, NOW())
                ON CONFLICT (user_id_a, user_id_b)
                DO UPDATE SET shared_chat_count = user_connections.shared_chat_count + 1,
                              last_updated      = NOW();
                """,
                user_id_x,
                user_id_y,
            )
