from __future__ import annotations

import json
from datetime import datetime, timezone

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
        await self.ensure_compatibility()

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()
            self.pool = None

    def _pool(self) -> asyncpg.Pool:
        if not self.pool:
            raise RuntimeError("Database pool is not initialized")
        return self.pool

    @with_db_retry()
    async def ensure_compatibility(self) -> None:
        async with self._pool().acquire() as conn:
            await conn.execute("ALTER TABLE bulk_sender.send_jobs ADD COLUMN IF NOT EXISTS cooldown_until TIMESTAMP")
            await conn.execute("ALTER TABLE bulk_sender.send_jobs ADD COLUMN IF NOT EXISTS last_error TEXT")

    @with_db_retry()
    async def list_runnable_jobs(self, limit: int = 10) -> list[asyncpg.Record]:
        async with self._pool().acquire() as conn:
            return await conn.fetch(
                """
                SELECT *
                FROM bulk_sender.send_jobs
                WHERE status IN ('pending', 'running')
                  AND (cooldown_until IS NULL OR cooldown_until <= NOW())
                ORDER BY updated_at ASC, id ASC
                LIMIT $1
                """,
                limit,
            )

    @with_db_retry()
    async def set_job_status(self, job_id: int, status: str, error: str | None = None) -> None:
        async with self._pool().acquire() as conn:
            await conn.execute(
                """
                UPDATE bulk_sender.send_jobs
                SET status = $2, last_error = COALESCE($3, last_error), updated_at = NOW()
                WHERE id = $1
                """,
                job_id,
                status,
                error,
            )

    @with_db_retry()
    async def set_job_cooldown(self, job_id: int, minutes: int = 30) -> None:
        async with self._pool().acquire() as conn:
            await conn.execute(
                """
                UPDATE bulk_sender.send_jobs
                SET status = 'paused',
                    cooldown_until = NOW() + ($2::text || ' minutes')::interval,
                    updated_at = NOW()
                WHERE id = $1
                """,
                job_id,
                minutes,
            )

    @with_db_retry()
    async def update_sent_count(self, job_id: int, increment: int = 1) -> None:
        async with self._pool().acquire() as conn:
            await conn.execute(
                """
                UPDATE bulk_sender.send_jobs
                SET sent_count = sent_count + $2,
                    updated_at = NOW()
                WHERE id = $1
                """,
                job_id,
                increment,
            )

    @with_db_retry()
    async def list_targets(self, job_id: int) -> list[asyncpg.Record]:
        async with self._pool().acquire() as conn:
            return await conn.fetch(
                "SELECT * FROM bulk_sender.send_targets WHERE job_id = $1 ORDER BY id ASC",
                job_id,
            )

    @with_db_retry()
    async def has_sent_hash(self, job_id: int, target_chat_jid: str, file_hash: str) -> bool:
        async with self._pool().acquire() as conn:
            value = await conn.fetchval(
                """
                SELECT 1 FROM bulk_sender.sent_items
                WHERE job_id = $1 AND target_chat_jid = $2 AND file_hash = $3
                LIMIT 1
                """,
                job_id,
                target_chat_jid,
                file_hash,
            )
            return value is not None

    @with_db_retry()
    async def record_sent_item(self, job_id: int, target_chat_jid: str, file_path: str, file_hash: str, wa_message_id: str | None) -> None:
        async with self._pool().acquire() as conn:
            await conn.execute(
                """
                INSERT INTO bulk_sender.sent_items(job_id, target_chat_jid, file_path, file_hash, sent_at, wa_message_id)
                VALUES ($1, $2, $3, $4, NOW(), $5)
                ON CONFLICT (job_id, target_chat_jid, file_hash) DO NOTHING
                """,
                job_id,
                target_chat_jid,
                file_path,
                file_hash,
                wa_message_id,
            )

    @with_db_retry()
    async def mark_target_status(self, target_id: int, status: str) -> None:
        async with self._pool().acquire() as conn:
            await conn.execute(
                "UPDATE bulk_sender.send_targets SET status = $2 WHERE id = $1",
                target_id,
                status,
            )

    @with_db_retry()
    async def get_membership_joined_at(self, session_name: str, chat_jid: str):
        async with self._pool().acquire() as conn:
            return await conn.fetchval(
                """
                SELECT MIN(gp.joined_at)
                FROM collector.group_participants gp
                JOIN collector.wa_sessions ws ON ws.phone_jid = gp.user_jid
                WHERE ws.session_name = $1
                  AND gp.chat_jid = $2
                """,
                session_name,
                chat_jid,
            )

    @with_db_retry()
    async def is_session_connected(self, session_name: str) -> bool:
        async with self._pool().acquire() as conn:
            status = await conn.fetchval(
                "SELECT status FROM collector.wa_sessions WHERE session_name = $1 LIMIT 1",
                session_name,
            )
            return str(status or "").lower() in {"active", "connected", "connecting"}

    @with_db_retry()
    async def summary_stats(self) -> dict[str, int]:
        async with self._pool().acquire() as conn:
            pending = int(await conn.fetchval("SELECT COUNT(*) FROM bulk_sender.send_jobs WHERE status='pending'") or 0)
            running = int(await conn.fetchval("SELECT COUNT(*) FROM bulk_sender.send_jobs WHERE status='running'") or 0)
            sent = int(await conn.fetchval("SELECT COUNT(*) FROM bulk_sender.sent_items") or 0)
            return {"pending": pending, "running": running, "sent": sent}

    @with_db_retry()
    async def create_send_job(
        self,
        *,
        session_name: str,
        mode: str,
        source_path: str,
        target_chat_jids: list[str],
        operator_confirmed: bool,
        requested_by: str,
    ) -> int:
        mode_normalized = str(mode or "").strip().lower()
        if mode_normalized not in {"internal", "external"}:
            raise ValueError("mode must be 'internal' or 'external'")

        normalized_targets = sorted({str(jid).strip() for jid in (target_chat_jids or []) if str(jid).strip()})
        audit_payload = {
            "requested_by": requested_by,
            "requested_at": datetime.now(timezone.utc).isoformat(),
            "target_count": len(normalized_targets),
            "source": "bulk_sender_dashboard",
        }

        async with self._pool().acquire() as conn:
            async with conn.transaction():
                job_id = await conn.fetchval(
                    """
                    INSERT INTO bulk_sender.send_jobs (
                        session_name, mode, source_type, source_path,
                        collector_query, status, operator_confirmed,
                        total_files, sent_count, created_at, updated_at
                    )
                    VALUES (
                        $1, $2, 'filesystem', $3,
                        $4::jsonb, 'pending', $5,
                        0, 0, NOW(), NOW()
                    )
                    RETURNING id
                    """,
                    session_name,
                    mode_normalized,
                    source_path,
                    json.dumps(audit_payload),
                    bool(operator_confirmed),
                )

                for target_jid in normalized_targets:
                    await conn.execute(
                        """
                        INSERT INTO bulk_sender.send_targets (job_id, chat_jid, status)
                        VALUES ($1, $2, 'pending')
                        ON CONFLICT (job_id, chat_jid) DO NOTHING
                        """,
                        job_id,
                        target_jid,
                    )

        return int(job_id)

    @with_db_retry()
    async def list_recent_jobs(self, limit: int = 25) -> list[asyncpg.Record]:
        async with self._pool().acquire() as conn:
            return await conn.fetch(
                """
                SELECT id, session_name, mode, status, operator_confirmed,
                       source_path, sent_count, last_error, cooldown_until, updated_at
                FROM bulk_sender.send_jobs
                ORDER BY id DESC
                LIMIT $1
                """,
                limit,
            )


database = Database()
