from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import asyncpg

from shared.db import create_pool

from .config import settings
from .db_retry import with_db_retry
from .observability import get_logger

logger = get_logger(__name__)


def _dt(value: Any) -> datetime:
    """Convert a value to a naive-UTC datetime matching TIMESTAMP columns."""
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).replace(tzinfo=None)
    return datetime.utcnow()


class Database:
    def __init__(self) -> None:
        self.pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        if self.pool:
            return
        self.pool = await create_pool(
            settings.DATABASE_URL,
            min_size=settings.DB_POOL_SIZE,
            max_size=settings.DB_POOL_SIZE + settings.DB_MAX_OVERFLOW,
            command_timeout=float(settings.DB_POOL_TIMEOUT),
            max_inactive_connection_lifetime=float(settings.DB_POOL_RECYCLE),
        )
        await self.ensure_compatibility()

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()
            self.pool = None

    def _pool(self) -> asyncpg.Pool:
        if not self.pool:
            raise RuntimeError("Database pool is not initialized")
        return self.pool

    async def ensure_compatibility(self) -> None:
        pool = self._pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "ALTER TABLE media_archival.media_files ADD COLUMN IF NOT EXISTS raw_message_id BIGINT"
            )

    @with_db_retry()
    async def get_media_cursor(self, service_name: str = "media_archival") -> int:
        pool = self._pool()
        async with pool.acquire() as conn:
            value = await conn.fetchval(
                "SELECT COALESCE(last_message_id, 0) FROM collector.service_cursors WHERE service_name = $1",
                service_name,
            )
            return int(value or 0)

    @with_db_retry()
    async def seed_cursor(self, service_name: str = "media_archival") -> None:
        pool = self._pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO collector.service_cursors(service_name, last_message_id)
                VALUES ($1, 0)
                ON CONFLICT (service_name) DO NOTHING
                """,
                service_name,
            )

    @with_db_retry()
    async def advance_cursor(self, service_name: str, last_message_id: int) -> None:
        pool = self._pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE collector.service_cursors
                SET last_message_id = GREATEST(last_message_id, $2), updated_at = NOW()
                WHERE service_name = $1
                """,
                service_name,
                last_message_id,
            )

    @with_db_retry()
    async def get_pending_media_messages(self, after_message_id: int, limit: int = 50) -> list[asyncpg.Record]:
        pool = self._pool()
        async with pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT rm.id AS raw_message_id, rm.message_id, rm.chat_jid, rm.message_type,
                       rm.body, rm.session_name, rm.raw_payload
                FROM collector.raw_messages rm
                WHERE rm.has_media = TRUE
                  AND rm.id > $1
                ORDER BY rm.id ASC
                LIMIT $2
                """,
                after_message_id,
                limit,
            )

    @with_db_retry()
    async def upsert_media_file(
        self,
        *,
        raw_message_id: int,
        message_id: str,
        chat_jid: str,
        file_unique_id: str | None,
        mime_type: str | None,
        file_size_bytes: int | None,
        by_id_path: str | None,
        by_message_path: str | None,
        sha256: str | None,
        download_status: str,
        downloaded_at: datetime | None,
        expiry_at: datetime | None,
    ) -> None:
        pool = self._pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO media_archival.media_files (
                    raw_message_id, message_id, chat_jid, file_unique_id, mime_type,
                    file_size_bytes, by_id_path, by_message_path, sha256, download_status,
                    downloaded_at, collected_at, expiry_at
                ) VALUES (
                    $1, $2, $3, $4, $5,
                    $6, $7, $8, $9, $10,
                    $11, NOW(), $12
                )
                ON CONFLICT (message_id, chat_jid)
                DO UPDATE SET
                    raw_message_id = EXCLUDED.raw_message_id,
                    file_unique_id = COALESCE(EXCLUDED.file_unique_id, media_archival.media_files.file_unique_id),
                    mime_type = COALESCE(EXCLUDED.mime_type, media_archival.media_files.mime_type),
                    file_size_bytes = COALESCE(EXCLUDED.file_size_bytes, media_archival.media_files.file_size_bytes),
                    by_id_path = COALESCE(EXCLUDED.by_id_path, media_archival.media_files.by_id_path),
                    by_message_path = COALESCE(EXCLUDED.by_message_path, media_archival.media_files.by_message_path),
                    sha256 = COALESCE(EXCLUDED.sha256, media_archival.media_files.sha256),
                    download_status = EXCLUDED.download_status,
                    downloaded_at = COALESCE(EXCLUDED.downloaded_at, media_archival.media_files.downloaded_at),
                    expiry_at = COALESCE(EXCLUDED.expiry_at, media_archival.media_files.expiry_at)
                """,
                raw_message_id,
                message_id,
                chat_jid,
                file_unique_id,
                mime_type,
                file_size_bytes,
                by_id_path,
                by_message_path,
                sha256,
                download_status,
                downloaded_at,
                expiry_at,
            )

    @with_db_retry()
    async def get_cleanup_candidates(self, min_cursor: int, cutoff: datetime) -> list[asyncpg.Record]:
        pool = self._pool()
        async with pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT mf.id, mf.message_id, mf.chat_jid, mf.file_unique_id,
                       mf.mime_type, mf.by_id_path, mf.by_message_path, mf.sha256,
                       mf.download_status, mf.downloaded_at, mf.expiry_at,
                       rm.id AS raw_message_id
                FROM media_archival.media_files mf
                JOIN collector.raw_messages rm
                  ON rm.message_id = mf.message_id AND rm.chat_jid = mf.chat_jid
                WHERE rm.id < $1
                  AND (mf.expiry_at IS NULL OR mf.expiry_at < $2)
                ORDER BY rm.id ASC
                LIMIT 500
                """,
                min_cursor,
                cutoff,
            )

    @with_db_retry()
    async def get_min_service_cursor(self) -> int:
        pool = self._pool()
        async with pool.acquire() as conn:
            value = await conn.fetchval(
                "SELECT COALESCE(MIN(last_message_id), 0) FROM collector.service_cursors"
            )
            return int(value or 0)

    @with_db_retry()
    async def count_file_references(self, file_unique_id: str | None, sha256: str | None) -> int:
        pool = self._pool()
        async with pool.acquire() as conn:
            if sha256:
                return int(
                    await conn.fetchval(
                        """
                        SELECT COUNT(*) FROM media_archival.media_files
                        WHERE file_unique_id = $1 OR sha256 = $2
                        """,
                        file_unique_id,
                        sha256,
                    )
                    or 0
                )
            return int(
                await conn.fetchval(
                    "SELECT COUNT(*) FROM media_archival.media_files WHERE file_unique_id = $1",
                    file_unique_id,
                )
                or 0
            )

    @with_db_retry()
    async def list_expiring_media(self, lookahead_hours: int) -> list[asyncpg.Record]:
        pool = self._pool()
        async with pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT mf.*, rm.raw_payload, rm.session_name
                FROM media_archival.media_files mf
                JOIN collector.raw_messages rm
                  ON rm.message_id = mf.message_id AND rm.chat_jid = mf.chat_jid
                WHERE mf.expiry_at IS NOT NULL
                  AND mf.expiry_at <= NOW() + make_interval(hours => $1)
                  AND mf.download_status = 'complete'
                ORDER BY mf.expiry_at ASC
                LIMIT 250
                """,
                lookahead_hours,
            )

    @with_db_retry()
    async def mark_download_failure(
        self,
        *,
        message_id: str,
        chat_jid: str,
        error_message: str,
        next_retry_at: datetime | None,
        is_permanent: bool = False,
    ) -> None:
        pool = self._pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO media_archival.download_failures (
                    message_id, chat_jid, error_message, attempt_count,
                    next_retry_at, last_attempted_at, is_permanent
                ) VALUES ($1, $2, $3, 1, $4, NOW(), $5)
                ON CONFLICT (message_id, chat_jid)
                DO UPDATE SET
                    error_message = EXCLUDED.error_message,
                    attempt_count = media_archival.download_failures.attempt_count + 1,
                    next_retry_at = EXCLUDED.next_retry_at,
                    last_attempted_at = NOW(),
                    is_permanent = EXCLUDED.is_permanent
                """,
                message_id,
                chat_jid,
                error_message,
                next_retry_at,
                is_permanent,
            )


    async def delete_stale_failures(self, older_than_days: int = 30) -> int:
        """Delete permanent download failures not retried in >30 days."""
        pool = self._pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                """
                DELETE FROM media_archival.download_failures
                WHERE is_permanent = TRUE
                  AND last_attempted_at < NOW() - ($1 || ' days')::INTERVAL
                """,
                str(older_than_days),
            )
            # asyncpg returns "DELETE N" — parse the count
            try:
                return int(result.split()[-1])
            except (IndexError, ValueError):
                return 0


database = Database()
