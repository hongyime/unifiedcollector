from __future__ import annotations

import asyncpg

from shared.db import create_pool

from .config import settings
from .db_retry import with_db_retry
from .queue_rules import QueueRule


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
    async def advance_cursor(self, raw_message_id: int, conn: asyncpg.Connection | None = None) -> None:
        if conn is None:
            async with self._pool().acquire() as acquired:
                await self.advance_cursor(raw_message_id, conn=acquired)
                return

        await conn.execute(
            """
            UPDATE collector.service_cursors
            SET last_message_id = GREATEST(last_message_id, $2), updated_at = NOW()
            WHERE service_name = $1
            """,
            settings.SERVICE_NAME,
            raw_message_id,
        )

    @with_db_retry()
    async def list_candidate_messages(self, after_id: int, limit: int) -> list[asyncpg.Record]:
        async with self._pool().acquire() as conn:
            return await conn.fetch(
                """
                SELECT id, body, raw_payload, message_type
                FROM collector.raw_messages
                WHERE id > $1
                  AND message_type IN ('text', 'image', 'video', 'document', 'extendedTextMessage', 'conversation')
                ORDER BY id ASC
                LIMIT $2
                """,
                after_id,
                limit,
            )

    @with_db_retry()
    async def insert_discovered_link(self, raw_message_id: int, link: str, link_type: str, conn: asyncpg.Connection) -> bool:
        result = await conn.execute(
            """
            INSERT INTO link_discovery.discovered_links(raw_message_id, link, link_type, status, discovered_at)
            VALUES ($1, $2, $3, 'new', NOW())
            ON CONFLICT (link) DO NOTHING
            """,
            raw_message_id,
            link,
            link_type,
        )
        return result.endswith("1")

    @with_db_retry()
    async def list_active_rules(self, conn: asyncpg.Connection) -> list[QueueRule]:
        rows = await conn.fetch(
            """
            SELECT id, name, keyword_whitelist, keyword_blacklist, auto_queue, is_active,
                   preferred_session, session_allowlist
            FROM link_discovery.queue_rules
            WHERE is_active = TRUE
            ORDER BY id ASC
            """
        )
        return [
            QueueRule(
                id=int(row["id"]),
                name=str(row["name"]),
                keyword_whitelist=list(row["keyword_whitelist"] or []),
                keyword_blacklist=list(row["keyword_blacklist"] or []),
                auto_queue=bool(row["auto_queue"]),
                is_active=bool(row["is_active"]),
                preferred_session=row["preferred_session"],
                session_allowlist=list(row["session_allowlist"]) if row["session_allowlist"] else None,
            )
            for row in rows
        ]

    @with_db_retry()
    async def enqueue_join(self, link: str, source: str, conn: asyncpg.Connection, session_name: str | None = None) -> None:
        await conn.execute(
            """
            INSERT INTO link_discovery.join_queue(link, session_name, status, source, added_at)
            VALUES ($1, $3, 'pending', $2, NOW())
            """,
            link,
            source,
            session_name,
        )
        await conn.execute(
            "UPDATE link_discovery.discovered_links SET status = 'queued' WHERE link = $1",
            link,
        )

    @with_db_retry()
    async def summary_stats(self) -> dict[str, int]:
        async with self._pool().acquire() as conn:
            discovered = int(await conn.fetchval("SELECT COUNT(*) FROM link_discovery.discovered_links") or 0)
            queued = int(await conn.fetchval("SELECT COUNT(*) FROM link_discovery.join_queue WHERE status='pending'") or 0)
            unassigned = int(await conn.fetchval("SELECT COUNT(*) FROM link_discovery.join_queue WHERE status='pending' AND session_name IS NULL") or 0)
            return {"discovered": discovered, "queued": queued, "unassigned": unassigned}

    @with_db_retry()
    async def bulk_assign_session(self, session_name: str) -> int:
        async with self._pool().acquire() as conn:
            tag = await conn.execute(
                "UPDATE link_discovery.join_queue SET session_name=$1 WHERE status='pending' AND session_name IS NULL",
                session_name,
            )
            return int(tag.split()[-1])

    @with_db_retry()
    async def list_pending_joins(self) -> list[asyncpg.Record]:
        async with self._pool().acquire() as conn:
            return await conn.fetch(
                """
                SELECT id, link, session_name, status, source, added_at
                FROM link_discovery.join_queue
                WHERE status = 'pending'
                ORDER BY added_at ASC
                """
            )

    @with_db_retry()
    async def update_join_status(self, join_id: int, session_name: str | None = None, status: str | None = None) -> None:
        async with self._pool().acquire() as conn:
            if session_name and status:
                await conn.execute(
                    "UPDATE link_discovery.join_queue SET session_name = $1, status = $2 WHERE id = $3",
                    session_name,
                    status,
                    join_id,
                )
            elif session_name:
                await conn.execute(
                    "UPDATE link_discovery.join_queue SET session_name = $1 WHERE id = $2",
                    session_name,
                    join_id,
                )
            elif status:
                await conn.execute(
                    "UPDATE link_discovery.join_queue SET status = $1 WHERE id = $2",
                    status,
                    join_id,
                )

    @with_db_retry()
    async def list_active_sessions(self) -> list[str]:
        async with self._pool().acquire() as conn:
            rows = await conn.fetch("SELECT session_name FROM collector.wa_sessions WHERE status = 'active'")
            return [str(row["session_name"]) for row in rows]


database = Database()
