from __future__ import annotations

import json
from typing import Any

import asyncpg

from shared.db import create_pool

from .config import settings
from .db_retry import with_db_retry


class Database:
    def __init__(self) -> None:
        self.pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        if self.pool:
            return
        self.pool = await create_pool(settings.DATABASE_URL)

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()
            self.pool = None

    def _pool(self) -> asyncpg.Pool:
        if not self.pool:
            raise RuntimeError("Database pool is not initialized")
        return self.pool

    @with_db_retry()
    async def seed_cursor(self) -> None:
        async with self._pool().acquire() as conn:
            await conn.execute(
                """
                INSERT INTO collector.service_cursors(service_name, last_message_id)
                VALUES ($1, 0)
                ON CONFLICT (service_name) DO NOTHING
                """,
                settings.SERVICE_NAME,
            )

    @with_db_retry()
    async def get_cursor(self) -> int:
        async with self._pool().acquire() as conn:
            value = await conn.fetchval(
                "SELECT COALESCE(last_message_id, 0) FROM collector.service_cursors WHERE service_name = $1",
                settings.SERVICE_NAME,
            )
            return int(value or 0)

    @with_db_retry()
    async def advance_cursor(self, sighting_id: int, conn: asyncpg.Connection | None = None) -> None:
        if conn is not None:
            await conn.execute(
                """
                UPDATE collector.service_cursors
                SET last_message_id = GREATEST(last_message_id, $2), updated_at = NOW()
                WHERE service_name = $1
                """,
                settings.SERVICE_NAME,
                sighting_id,
            )
            return

        async with self._pool().acquire() as acquired:
            await self.advance_cursor(sighting_id, conn=acquired)

    @with_db_retry()
    async def list_sightings(self, after_id: int, limit: int) -> list[asyncpg.Record]:
        async with self._pool().acquire() as conn:
            return await conn.fetch(
                """
                SELECT id, user_jid, seen_in_chat_jid, seen_at, payload
                FROM collector.user_sightings
                WHERE id > $1
                ORDER BY id ASC
                LIMIT $2
                """,
                after_id,
                limit,
            )

    @with_db_retry()
    async def get_last_known_fields(self, user_jid: str, conn: asyncpg.Connection) -> dict[str, str]:
        rows = await conn.fetch(
            """
            SELECT DISTINCT ON (field_name) field_name, new_value
            FROM user_intelligence.user_history
            WHERE user_jid = $1
            ORDER BY field_name, changed_at DESC, id DESC
            """,
            user_jid,
        )
        return {str(row["field_name"]): str(row["new_value"] or "") for row in rows}

    @with_db_retry()
    async def insert_user_history(
        self,
        user_jid: str,
        field_name: str,
        old_value: str,
        new_value: str,
        conn: asyncpg.Connection,
    ) -> None:
        await conn.execute(
            """
            INSERT INTO user_intelligence.user_history(user_jid, field_name, old_value, new_value, changed_at)
            VALUES ($1, $2, $3, $4, NOW())
            """,
            user_jid,
            field_name,
            old_value,
            new_value,
        )

    @with_db_retry()
    async def upsert_membership(self, user_jid: str, chat_jid: str, conn: asyncpg.Connection) -> bool:
        row = await conn.fetchrow(
            """
            INSERT INTO user_intelligence.user_chat_memberships(user_jid, chat_jid, first_seen, last_seen, message_count)
            VALUES ($1, $2, NOW(), NOW(), 1)
            ON CONFLICT (user_jid, chat_jid)
            DO UPDATE SET last_seen = NOW(), message_count = user_intelligence.user_chat_memberships.message_count + 1
            RETURNING (xmax = 0) AS inserted
            """,
            user_jid,
            chat_jid,
        )
        return bool(row and row["inserted"])

    @with_db_retry()
    async def list_other_chat_members(self, chat_jid: str, user_jid: str, conn: asyncpg.Connection) -> list[str]:
        rows = await conn.fetch(
            """
            SELECT user_jid
            FROM user_intelligence.user_chat_memberships
            WHERE chat_jid = $1 AND user_jid <> $2
            """,
            chat_jid,
            user_jid,
        )
        return [str(row["user_jid"]) for row in rows]

    @with_db_retry()
    async def upsert_connection(self, user_a: str, user_b: str, conn: asyncpg.Connection) -> None:
        ordered = sorted([user_a, user_b])
        await conn.execute(
            """
            INSERT INTO user_intelligence.user_connections(user_jid_a, user_jid_b, shared_chat_count, last_updated)
            VALUES ($1, $2, 1, NOW())
            ON CONFLICT (user_jid_a, user_jid_b)
            DO UPDATE SET shared_chat_count = user_intelligence.user_connections.shared_chat_count + 1,
                          last_updated = NOW()
            """,
            ordered[0],
            ordered[1],
        )

    @with_db_retry()
    async def summary_stats(self) -> dict[str, int]:
        async with self._pool().acquire() as conn:
            users = int(await conn.fetchval("SELECT COUNT(DISTINCT user_jid) FROM user_intelligence.user_chat_memberships") or 0)
            changes = int(await conn.fetchval("SELECT COUNT(*) FROM user_intelligence.user_history WHERE changed_at::date = CURRENT_DATE") or 0)
            links = int(await conn.fetchval("SELECT COUNT(*) FROM user_intelligence.user_connections") or 0)
            return {"users": users, "changes_today": changes, "connections": links}

    @with_db_retry()
    async def search_users(self, query: str) -> list[asyncpg.Record]:
        async with self._pool().acquire() as conn:
            # Search by JID, push_name, display_name, or phone_number in collector.users
            return await conn.fetch(
                """
                SELECT jid, display_name, push_name, phone_number
                FROM collector.users
                WHERE jid ILIKE $1
                   OR push_name ILIKE $1
                   OR display_name ILIKE $1
                   OR phone_number ILIKE $1
                LIMIT 50
                """,
                f"%{query}%",
            )

    @with_db_retry()
    async def get_user_history_timeline(self, user_jid: str) -> list[dict[str, Any]]:
        async with self._pool().acquire() as conn:
            timeline = []
            
            # Get profile changes
            history = await conn.fetch(
                """
                SELECT field_name, old_value, new_value, changed_at as occurred_at, 'profile_change' as event_type
                FROM user_intelligence.user_history
                WHERE user_jid = $1
                ORDER BY changed_at DESC
                """,
                user_jid
            )
            for row in history:
                timeline.append(dict(row))
                
            # Get sightings (limited)
            sightings = await conn.fetch(
                """
                SELECT seen_in_chat_jid as target, 'sighting' as event_type, seen_at as occurred_at
                FROM collector.user_sightings
                WHERE user_jid = $1
                ORDER BY seen_at DESC
                LIMIT 100
                """,
                user_jid
            )
            for row in sightings:
                timeline.append(dict(row))
                
            # Sort by time
            timeline.sort(key=lambda x: x["occurred_at"], reverse=True)
            return timeline


database = Database()
