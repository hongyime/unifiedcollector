from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from uuid import UUID

import asyncpg

from .config import settings
from .database import database
from .observability import identity_matches_total, get_logger

logger = get_logger(__name__)


def _vector_literal(embedding: Iterable[float]) -> str:
    return "[" + ",".join(f"{float(value):.8f}" for value in embedding) + "]"


def _update_centroid(old_centroid: Iterable[float], new_embedding: Iterable[float], occurrence_count: int) -> list[float]:
    old_values = [float(value) for value in old_centroid]
    new_values = [float(value) for value in new_embedding]
    divisor = float(occurrence_count + 1)
    return [((old * occurrence_count) + new) / divisor for old, new in zip(old_values, new_values)]


class IdentityMatcher:
    def __init__(self, match_threshold: float | None = None) -> None:
        self.match_threshold = settings.FACE_MATCH_THRESHOLD if match_threshold is None else match_threshold

    async def match_embedding(
        self,
        *,
        embedding: Iterable[float],
        source_message_id: str,
        source_chat_jid: str,
        frame_index: int = 0,
        confidence: float | None = 1.0,
        conn: asyncpg.Connection | None = None,
    ) -> tuple[UUID, bool]:
        own_transaction = conn is None
        if conn is None:
            if not database.pool:
                raise RuntimeError("Database pool is not initialized")
            async with database.pool.acquire() as acquired:
                async with acquired.transaction():
                    return await self._match_embedding(
                        conn=acquired,
                        embedding=embedding,
                        source_message_id=source_message_id,
                        source_chat_jid=source_chat_jid,
                        frame_index=frame_index,
                        confidence=confidence,
                    )

        if own_transaction:
            async with conn.transaction():
                return await self._match_embedding(
                    conn=conn,
                    embedding=embedding,
                    source_message_id=source_message_id,
                    source_chat_jid=source_chat_jid,
                    frame_index=frame_index,
                    confidence=confidence,
                )

        return await self._match_embedding(
            conn=conn,
            embedding=embedding,
            source_message_id=source_message_id,
            source_chat_jid=source_chat_jid,
            frame_index=frame_index,
            confidence=confidence,
        )

    async def _match_embedding(
        self,
        *,
        conn: asyncpg.Connection,
        embedding: Iterable[float],
        source_message_id: str,
        source_chat_jid: str,
        frame_index: int,
        confidence: float | None,
    ) -> tuple[UUID, bool]:
        embedding_values = [float(value) for value in embedding]
        # ef_search=100 raises the HNSW candidate list from the default 40,
        # giving better recall for the nearest-neighbor lookup at moderate cost.
        await conn.execute("SET LOCAL hnsw.ef_search = 100")
        row = await conn.fetchrow(
            """
            SELECT id, centroid, occurrence_count, centroid <-> $1::vector AS distance
            FROM face_recognition.identity_entities
            ORDER BY centroid <-> $1::vector
            LIMIT 1
            """,
            _vector_literal(embedding_values),
        )

        if row and float(row["distance"]) <= self.match_threshold:
            identity_id = UUID(str(row["id"]))
            centroid = _update_centroid(row["centroid"], embedding_values, int(row["occurrence_count"] or 1))
            await conn.execute(
                """
                UPDATE face_recognition.identity_entities
                SET centroid = $2::vector,
                    occurrence_count = occurrence_count + 1,
                    last_seen = NOW()
                WHERE id = $1
                """,
                identity_id,
                _vector_literal(centroid),
            )
            identity_matches_total.labels(result="match").inc()
            logger.debug("face_identity_matched", identity_id=str(identity_id), source_message_id=source_message_id)
            is_new = False
        else:
            identity_id = UUID(
                str(
                    await conn.fetchval(
                        """
                        INSERT INTO face_recognition.identity_entities(label, centroid, occurrence_count, first_seen, last_seen)
                        VALUES ('Unknown', $1::vector, 1, NOW(), NOW())
                        RETURNING id
                        """,
                        _vector_literal(embedding_values),
                    )
                )
            )
            identity_matches_total.labels(result="new").inc()
            logger.debug("face_identity_new", identity_id=str(identity_id), source_message_id=source_message_id)
            is_new = True

        await conn.execute(
            """
            INSERT INTO face_recognition.face_embeddings(
                identity_id, embedding, source_message_id, source_chat_jid, frame_index, is_valid, created_at
            ) VALUES ($1, $2::vector, $3, $4, $5, TRUE, NOW())
            ON CONFLICT DO NOTHING
            """,
            identity_id,
            _vector_literal(embedding_values),
            source_message_id,
            source_chat_jid,
            frame_index,
        )

        return identity_id, is_new

    async def rename_identity(self, identity_id: str, label: str, conn: asyncpg.Connection | None = None) -> None:
        if conn is None:
            if not database.pool:
                raise RuntimeError("Database pool is not initialized")
            async with database.pool.acquire() as acquired:
                async with acquired.transaction():
                    await database.rename_identity(identity_id, label, conn=acquired)
                    return

        await database.rename_identity(identity_id, label, conn=conn)

    async def merge_identities(
        self,
        source_identity_id: str,
        target_identity_id: str,
        conn: asyncpg.Connection | None = None,
    ) -> None:
        if conn is None:
            if not database.pool:
                raise RuntimeError("Database pool is not initialized")
            async with database.pool.acquire() as acquired:
                async with acquired.transaction():
                    await database.merge_identities(source_identity_id, target_identity_id, conn=acquired)
                    return

        await database.merge_identities(source_identity_id, target_identity_id, conn=conn)

    async def split_identity(
        self,
        identity_id: str,
        embedding_ids: list[int],
        new_label: str = "Unknown",
        conn: asyncpg.Connection | None = None,
    ) -> str:
        if conn is None:
            if not database.pool:
                raise RuntimeError("Database pool is not initialized")
            async with database.pool.acquire() as acquired:
                async with acquired.transaction():
                    return await self.split_identity(identity_id, embedding_ids, new_label=new_label, conn=acquired)

        target_id = UUID(
            str(
                await conn.fetchval(
                    """
                    INSERT INTO face_recognition.identity_entities(label, centroid, occurrence_count, first_seen, last_seen)
                    SELECT $2, centroid, 1, NOW(), NOW()
                    FROM face_recognition.identity_entities
                    WHERE id = $1
                    RETURNING id
                    """,
                    identity_id,
                    new_label,
                )
            )
        )
        await conn.execute(
            "UPDATE face_recognition.face_embeddings SET identity_id = $2 WHERE id = ANY($1::int[])",
            embedding_ids,
            target_id,
        )
        return str(target_id)


identity_matcher = IdentityMatcher()
