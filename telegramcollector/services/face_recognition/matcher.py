"""
IdentityMatcher — pgvector cosine similarity search and identity management.

Part of the Face Recognition Service (Phase 6).
Self-contained: no imports from collector/ or root-level identity_matcher.py.
Thresholds are read dynamically per-call via get_dynamic_setting().
"""
import logging
from typing import Optional

import asyncpg

from shared.config import get_dynamic_setting, settings

logger = logging.getLogger(__name__)


class IdentityMatcher:
    """
    Handles face embedding matching and identity management using asyncpg + pgvector.

    Thresholds (FACE_SIMILARITY_THRESHOLD, FACE_MIN_QUALITY_THRESHOLD) are NOT stored
    as instance attributes — they are fetched dynamically on every call so that Redis
    overrides take effect without a restart.
    """

    def __init__(self, db_pool: asyncpg.Pool) -> None:
        """
        db_pool: asyncpg pool with face_recog_user credentials.
        Thresholds are read dynamically per-call via get_dynamic_setting().
        """
        self._pool = db_pool

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def find_or_create_identity(
        self,
        embedding: list[float],
        quality_score: float,
        source_chat_id: int,
        source_message_id: int,
        frame_index: int = 0,
    ) -> tuple[int, bool]:
        """
        Main entry point.  Returns (topic_id, is_new).
        topic_id=0 and is_new=False means the embedding was below quality threshold.

        Flow:
          1. Check quality_score >= FACE_MIN_QUALITY_THRESHOLD (dynamic)
          2. _find_similar_embedding(embedding)
          3. If match  → _store_embedding(..., topic_id=match.topic_id); return (topic_id, False)
          4. If no match → _create_new_identity(...); return (new_id, True)
        """
        min_quality: float = get_dynamic_setting(
            "FACE_MIN_QUALITY_THRESHOLD", settings.FACE_MIN_QUALITY_THRESHOLD
        )
        if quality_score < min_quality:
            logger.debug(
                "Skipping low-quality embedding: %.4f < %.4f", quality_score, min_quality
            )
            return (0, False)

        match = await self._find_similar_embedding(embedding)

        if match:
            topic_id: int = match["topic_id"]
            logger.debug(
                "Matched existing identity topic_id=%d similarity=%.4f",
                topic_id,
                match["similarity"],
            )
            await self._store_embedding(
                embedding=embedding,
                topic_id=topic_id,
                quality_score=quality_score,
                source_chat_id=source_chat_id,
                source_message_id=source_message_id,
                frame_index=frame_index,
            )
            return (topic_id, False)

        new_id = await self._create_new_identity(
            embedding=embedding,
            quality_score=quality_score,
            source_chat_id=source_chat_id,
            source_message_id=source_message_id,
            frame_index=frame_index,
        )
        logger.info("Created new identity db_id=%d", new_id)
        return (new_id, True)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _find_similar_embedding(
        self, embedding: list[float]
    ) -> Optional[dict]:
        """
        Queries face_recognition.face_embeddings using pgvector <=> operator.
        Returns {'topic_id': int, 'similarity': float} if the nearest neighbour's
        similarity >= FACE_SIMILARITY_THRESHOLD (dynamic), else None.

        Uses the ivfflat index on (embedding vector_cosine_ops) — no sequential scan.
        """
        threshold: float = get_dynamic_setting(
            "FACE_SIMILARITY_THRESHOLD", settings.FACE_SIMILARITY_THRESHOLD
        )
        embedding_str = "[" + ",".join(map(str, embedding)) + "]"

        row = await self._pool.fetchrow(
            """
            SELECT topic_id,
                   1 - (embedding <=> $1::vector) AS similarity
              FROM face_recognition.face_embeddings
             ORDER BY embedding <=> $1::vector
             LIMIT 1
            """,
            embedding_str,
        )

        if row is None:
            return None

        similarity: float = float(row["similarity"])
        if similarity >= threshold:
            return {"topic_id": int(row["topic_id"]), "similarity": similarity}

        return None

    async def _store_embedding(
        self,
        embedding: list[float],
        topic_id: int,
        quality_score: float,
        source_chat_id: int,
        source_message_id: int,
        frame_index: int,
        conn=None,
    ) -> int:
        """
        INSERTs into face_recognition.face_embeddings and increments
        face_recognition.telegram_topics.face_count for topic_id.
        Returns the new embedding row id.

        When conn is provided (called from _create_new_identity inside an outer
        transaction), executes directly on that connection without opening a new
        transaction — avoids asyncpg nested-transaction InterfaceError.
        When conn is None (called from find_or_create_identity match path),
        acquires its own connection and transaction as before.
        """
        embedding_str = "[" + ",".join(map(str, embedding)) + "]"

        if conn is not None:
            # Reuse the caller's connection — no acquire, no nested transaction
            row = await conn.fetchrow(
                """
                INSERT INTO face_recognition.face_embeddings
                    (topic_id, embedding, source_chat_id, source_message_id,
                     frame_index, quality_score, is_representative, detection_timestamp)
                VALUES ($1, $2::vector, $3, $4, $5, $6, FALSE, NOW())
                RETURNING id
                """,
                topic_id,
                embedding_str,
                source_chat_id,
                source_message_id,
                frame_index,
                quality_score,
            )
            await conn.execute(
                """
                UPDATE face_recognition.telegram_topics
                   SET face_count = face_count + 1,
                       updated_at = NOW()
                 WHERE id = $1
                """,
                topic_id,
            )
            return int(row["id"])

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    INSERT INTO face_recognition.face_embeddings
                        (topic_id, embedding, source_chat_id, source_message_id,
                         frame_index, quality_score, is_representative, detection_timestamp)
                    VALUES ($1, $2::vector, $3, $4, $5, $6, FALSE, NOW())
                    RETURNING id
                    """,
                    topic_id,
                    embedding_str,
                    source_chat_id,
                    source_message_id,
                    frame_index,
                    quality_score,
                )

                await conn.execute(
                    """
                    UPDATE face_recognition.telegram_topics
                       SET face_count = face_count + 1,
                           updated_at = NOW()
                     WHERE id = $1
                    """,
                    topic_id,
                )

        return int(row["id"])

    async def _create_new_identity(
        self,
        embedding: list[float],
        quality_score: float,
        source_chat_id: int,
        source_message_id: int,
        frame_index: int,
    ) -> int:
        """
        Acquires pg_advisory_xact_lock derived from the first 10 embedding components,
        re-checks for an existing match inside the lock, then inserts a new row into
        face_recognition.telegram_topics (label='Unknown Person') and stores the embedding.
        Returns the new topic's db id (telegram_topics.id).

        Note: topic_id (Telegram forum topic ID) is set later by Publisher.
        """
        lock_id: int = hash(tuple(embedding[:10])) % (2**31)

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                # Acquire advisory lock — released automatically at transaction end.
                await conn.execute(
                    "SELECT pg_advisory_xact_lock($1)", lock_id
                )

                # Re-check inside the lock to handle concurrent workers.
                match = await self._find_similar_embedding(embedding)
                if match:
                    logger.info(
                        "Race condition avoided: found match topic_id=%d inside lock",
                        match["topic_id"],
                    )
                    await self._store_embedding(
                        embedding=embedding,
                        topic_id=match["topic_id"],
                        quality_score=quality_score,
                        source_chat_id=source_chat_id,
                        source_message_id=source_message_id,
                        frame_index=frame_index,
                        conn=conn,
                    )
                    return match["topic_id"]

                # Insert new identity row (topic_id=NULL placeholder; Publisher fills it later).
                row = await conn.fetchrow(
                    """
                    INSERT INTO face_recognition.telegram_topics
                        (topic_id, label, face_count, message_count, created_at, updated_at)
                    VALUES (NULL, 'Unknown Person', 0, 0, NOW(), NOW())
                    RETURNING id
                    """,
                )
                new_topic_db_id: int = int(row["id"])

                # Store the initial embedding (reuses the outer connection/transaction).
                await self._store_embedding(
                    embedding=embedding,
                    topic_id=new_topic_db_id,
                    quality_score=quality_score,
                    source_chat_id=source_chat_id,
                    source_message_id=source_message_id,
                    frame_index=frame_index,
                    conn=conn,
                )

        return new_topic_db_id
