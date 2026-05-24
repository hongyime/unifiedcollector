from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import asyncpg

from shared.db import create_pool

from .config import settings
from .db_retry import with_db_retry


def _as_vector_literal(embedding: Iterable[float]) -> str:
    return "[" + ",".join(f"{float(value):.8f}" for value in embedding) + "]"


def _identity_sort_column(sort_by: str) -> str:
    # Kept for callers that still need a validated column name string.
    # New code should use the CASE-based parameterized sort in list_identities.
    allowed = {"last_seen", "first_seen", "occurrence_count", "label"}
    return sort_by if sort_by in allowed else "last_seen"


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

    async def _fetch(self, query: str, *args: Any, conn: asyncpg.Connection | None = None):
        if conn is not None:
            return await conn.fetch(query, *args)
        async with self._pool().acquire() as acquired:
            return await acquired.fetch(query, *args)

    async def _fetchrow(self, query: str, *args: Any, conn: asyncpg.Connection | None = None):
        if conn is not None:
            return await conn.fetchrow(query, *args)
        async with self._pool().acquire() as acquired:
            return await acquired.fetchrow(query, *args)

    async def _fetchval(self, query: str, *args: Any, conn: asyncpg.Connection | None = None):
        if conn is not None:
            return await conn.fetchval(query, *args)
        async with self._pool().acquire() as acquired:
            return await acquired.fetchval(query, *args)

    async def _execute(self, query: str, *args: Any, conn: asyncpg.Connection | None = None):
        if conn is not None:
            return await conn.execute(query, *args)
        async with self._pool().acquire() as acquired:
            return await acquired.execute(query, *args)

    async def ensure_compatibility(self) -> None:
        # The vector extension is provisioned by the database migration layer.
        # This hook intentionally stays lightweight so service startup does not
        # require elevated privileges in restricted environments.
        return None

    @with_db_retry()
    async def seed_cursor(self) -> None:
        await self._execute(
            """
            INSERT INTO collector.service_cursors(service_name, last_message_id)
            VALUES ($1, 0)
            ON CONFLICT (service_name) DO NOTHING
            """,
            settings.SERVICE_CURSOR_NAME,
        )

    @with_db_retry()
    async def get_cursor(self) -> int:
        value = await self._fetchval(
            "SELECT COALESCE(last_message_id, 0) FROM collector.service_cursors WHERE service_name = $1",
            settings.SERVICE_CURSOR_NAME,
        )
        return int(value or 0)

    @with_db_retry()
    async def advance_cursor(self, last_message_id: int, conn: asyncpg.Connection | None = None) -> None:
        await self._execute(
            """
            UPDATE collector.service_cursors
            SET last_message_id = GREATEST(last_message_id, $2), updated_at = NOW()
            WHERE service_name = $1
            """,
            settings.SERVICE_CURSOR_NAME,
            last_message_id,
            conn=conn,
        )

    @with_db_retry()
    async def list_pending_media(self, after_message_id: int, limit: int = 50):
        return await self._fetch(
            """
            SELECT
                mf.id AS media_file_id,
                mf.message_id,
                mf.chat_jid,
                mf.file_unique_id,
                mf.mime_type,
                mf.file_size_bytes,
                mf.by_id_path,
                mf.by_message_path,
                mf.sha256,
                rm.id AS raw_message_id,
                rm.message_type,
                rm.session_name
            FROM media_archival.media_files mf
            JOIN collector.raw_messages rm
              ON rm.message_id = mf.message_id AND rm.chat_jid = mf.chat_jid
            LEFT JOIN face_recognition.processed_media pm
              ON pm.source_message_id = mf.message_id AND pm.source_chat_jid = mf.chat_jid
            WHERE mf.download_status = 'complete'
              AND mf.mime_type IS NOT NULL
              AND (mf.mime_type ILIKE 'image/%' OR mf.mime_type ILIKE 'video/%')
              AND rm.id > $1
              AND pm.id IS NULL
            ORDER BY rm.id ASC
            LIMIT $2
            """,
            after_message_id,
            limit,
        )

    @with_db_retry()
    async def has_processed_media(
        self,
        source_message_id: str,
        source_chat_jid: str,
        conn: asyncpg.Connection | None = None,
    ) -> bool:
        value = await self._fetchval(
            """
            SELECT 1
            FROM face_recognition.processed_media
            WHERE source_message_id = $1 AND source_chat_jid = $2
            LIMIT 1
            """,
            source_message_id,
            source_chat_jid,
            conn=conn,
        )
        return value is not None

    @with_db_retry()
    async def mark_processed_media(
        self,
        source_message_id: str,
        source_chat_jid: str,
        faces_found: int,
        conn: asyncpg.Connection | None = None,
    ) -> None:
        await self._execute(
            """
            INSERT INTO face_recognition.processed_media(source_message_id, source_chat_jid, faces_found, processed_at)
            VALUES ($1, $2, $3, NOW())
            ON CONFLICT (source_message_id, source_chat_jid)
            DO UPDATE SET faces_found = EXCLUDED.faces_found, processed_at = NOW()
            """,
            source_message_id,
            source_chat_jid,
            faces_found,
            conn=conn,
        )

    @with_db_retry()
    async def insert_face_embedding(
        self,
        *,
        identity_id: str,
        embedding: Iterable[float],
        source_message_id: str,
        source_chat_jid: str,
        frame_index: int,
        is_valid: bool = True,
        conn: asyncpg.Connection | None = None,
    ) -> None:
        await self._execute(
            """
            INSERT INTO face_recognition.face_embeddings(
                identity_id, embedding, source_message_id, source_chat_jid, frame_index, is_valid
            ) VALUES ($1, $2::vector, $3, $4, $5, $6)
            """,
            identity_id,
            _as_vector_literal(embedding),
            source_message_id,
            source_chat_jid,
            frame_index,
            is_valid,
            conn=conn,
        )

    @with_db_retry()
    async def list_identities(self, limit: int = 100, sort_by: str = "last_seen"):
        return await self._fetch(
            """
            SELECT id, label, occurrence_count, first_seen, last_seen
            FROM face_recognition.identity_entities
            ORDER BY
              CASE WHEN $2 = 'occurrence_count' THEN occurrence_count END DESC NULLS LAST,
              CASE WHEN $2 = 'first_seen'       THEN EXTRACT(EPOCH FROM first_seen) END DESC NULLS LAST,
              CASE WHEN $2 = 'label'            THEN label END DESC NULLS LAST,
              CASE WHEN $2 NOT IN ('occurrence_count','first_seen','label')
                   THEN EXTRACT(EPOCH FROM last_seen) END DESC NULLS LAST,
              occurrence_count DESC
            LIMIT $1
            """,
            limit,
            sort_by,
        )

    @with_db_retry()
    async def search_identities(self, embedding: Iterable[float], limit: int = 5):
        return await self._fetch(
            """
            SELECT id, label, occurrence_count, first_seen, last_seen,
                   centroid <-> $1::vector AS distance
            FROM face_recognition.identity_entities
            ORDER BY centroid <-> $1::vector
            LIMIT $2
            """,
            _as_vector_literal(embedding),
            limit,
        )

    @with_db_retry()
    async def get_identity(self, identity_id: str):
        rows = await self._fetch(
            """
            SELECT id, label, centroid, occurrence_count, first_seen, last_seen
            FROM face_recognition.identity_entities
            WHERE id = $1
            LIMIT 1
            """,
            identity_id,
        )
        return rows[0] if rows else None

    @with_db_retry()
    async def rename_identity(self, identity_id: str, label: str, conn: asyncpg.Connection | None = None) -> None:
        await self._execute(
            "UPDATE face_recognition.identity_entities SET label = $2 WHERE id = $1",
            identity_id,
            label,
            conn=conn,
        )

    @with_db_retry()
    async def merge_identities(
        self,
        source_identity_id: str,
        target_identity_id: str,
        conn: asyncpg.Connection | None = None,
    ) -> None:
        source = await self._fetchrow(
            """
            SELECT centroid, occurrence_count, first_seen, last_seen
            FROM face_recognition.identity_entities
            WHERE id = $1
            """,
            source_identity_id,
            conn=conn,
        )
        target = await self._fetchrow(
            """
            SELECT centroid, occurrence_count, first_seen, last_seen
            FROM face_recognition.identity_entities
            WHERE id = $1
            """,
            target_identity_id,
            conn=conn,
        )
        if not source or not target:
            return

        source_count = int(source["occurrence_count"] or 0)
        target_count = int(target["occurrence_count"] or 0)
        total_count = max(source_count + target_count, 1)
        merged_centroid = [
            ((float(target_value) * target_count) + (float(source_value) * source_count)) / total_count
            for source_value, target_value in zip(source["centroid"], target["centroid"])
        ]

        await self._execute(
            """
            UPDATE face_recognition.identity_entities
            SET centroid = $2::vector,
                occurrence_count = $3,
                last_seen = NOW()
            WHERE id = $1
            """,
            target_identity_id,
            _as_vector_literal(merged_centroid),
            total_count,
            conn=conn,
        )
        await self._execute(
            """
            UPDATE face_recognition.face_embeddings
            SET identity_id = $2
            WHERE identity_id = $1
            """,
            source_identity_id,
            target_identity_id,
            conn=conn,
        )
        await self._execute(
            "DELETE FROM face_recognition.identity_entities WHERE id = $1",
            source_identity_id,
            conn=conn,
        )

    @with_db_retry()
    async def update_identity_centroid(
        self,
        identity_id: str,
        centroid: Iterable[float],
        occurrence_count: int,
        conn: asyncpg.Connection | None = None,
    ) -> None:
        await self._execute(
            """
            UPDATE face_recognition.identity_entities
            SET centroid = $2::vector,
                occurrence_count = $3,
                last_seen = NOW()
            WHERE id = $1
            """,
            identity_id,
            _as_vector_literal(centroid),
            occurrence_count,
            conn=conn,
        )

    @with_db_retry()
    async def insert_identity(
        self,
        centroid: Iterable[float],
        label: str = "Unknown",
        conn: asyncpg.Connection | None = None,
    ) -> str:
        return str(
            await self._fetchval(
                """
                INSERT INTO face_recognition.identity_entities(label, centroid, occurrence_count, first_seen, last_seen)
                VALUES ($1, $2::vector, 1, NOW(), NOW())
                RETURNING id
                """,
                label,
                _as_vector_literal(centroid),
                conn=conn,
            )
        )

    @with_db_retry()
    async def split_identity(
        self,
        identity_id: str,
        embedding_ids: list[int],
        new_label: str = "Unknown",
        conn: asyncpg.Connection | None = None,
    ) -> str:
        selected_embeddings = await self._fetch(
            """
            SELECT id, embedding
            FROM face_recognition.face_embeddings
            WHERE id = ANY($1::int[])
            """,
            embedding_ids,
            conn=conn,
        )
        if not selected_embeddings:
            return identity_id

        centroid_values = [0.0] * 128
        for row in selected_embeddings:
            for index, value in enumerate(row["embedding"]):
                centroid_values[index] += float(value)
        centroid_values = [value / float(len(selected_embeddings)) for value in centroid_values]

        target_identity_id = str(
            await self._fetchval(
                """
                INSERT INTO face_recognition.identity_entities(label, centroid, occurrence_count, first_seen, last_seen)
                VALUES ($1, $2::vector, $3, NOW(), NOW())
                RETURNING id
                """,
                new_label,
                _as_vector_literal(centroid_values),
                len(selected_embeddings),
                conn=conn,
            )
        )

        await self._execute(
            "UPDATE face_recognition.face_embeddings SET identity_id = $2 WHERE id = ANY($1::int[])",
            embedding_ids,
            target_identity_id,
            conn=conn,
        )
        await self._execute(
            """
            UPDATE face_recognition.identity_entities
            SET occurrence_count = GREATEST(occurrence_count - $2, 1),
                last_seen = NOW()
            WHERE id = $1
            """,
            identity_id,
            len(selected_embeddings),
            conn=conn,
        )
        return target_identity_id


database = Database()
