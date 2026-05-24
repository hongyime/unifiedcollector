"""
Corrections — merge, split, and rename identity operations.

All DB operations run inside a single transaction (atomicity per Requirement 13.4).
Telegram topic deletion after merge is best-effort (outside the transaction).

No imports from collector/. Self-contained.
"""
import logging
from typing import List

import asyncpg

from shared.bot_pool import BotPool
from shared.config import get_hub_group_id

logger = logging.getLogger(__name__)


class Corrections:
    def __init__(self, db_pool: asyncpg.Pool, bot_pool: BotPool) -> None:
        """
        db_pool:  asyncpg pool (face_recog_user)
        bot_pool: for deleting Telegram forum topics after merge
        """
        self._db_pool = db_pool
        self._bot_pool = bot_pool

    # ------------------------------------------------------------------
    # Task 8.2 — merge_identities
    # ------------------------------------------------------------------

    async def merge_identities(
        self,
        source_topic_db_id: int,
        target_topic_db_id: int,
    ) -> None:
        """
        Merges Identity A (source) into Identity B (target) in a single transaction:
          1. UPDATE face_recognition.face_embeddings SET topic_id=target WHERE topic_id=source
          2. UPDATE face_recognition.uploaded_media SET topic_id=target WHERE topic_id=source
          3. UPDATE face_recognition.telegram_topics
               SET face_count=(SELECT COUNT(*) FROM face_embeddings WHERE topic_id=target),
                   updated_at=NOW()
             WHERE id=target
          4. DELETE FROM face_recognition.telegram_topics WHERE id=source
          5. Delete Telegram forum topic for source via bot_pool (outside transaction,
             best-effort — failure is logged but does not roll back the DB changes)
        Raises on any DB error → full rollback.
        Requirements: 13.1, 13.4, 13.5
        """
        # Fetch the source Telegram topic_id before the transaction so we can
        # delete it from Telegram after the DB changes commit.
        async with self._db_pool.acquire() as conn:
            source_row = await conn.fetchrow(
                "SELECT topic_id FROM face_recognition.telegram_topics WHERE id = $1",
                source_topic_db_id,
            )

        source_telegram_topic_id = source_row["topic_id"] if source_row else None

        # Single transaction for all DB mutations
        async with self._db_pool.acquire() as conn:
            async with conn.transaction():
                # Step 1: move embeddings
                await conn.execute(
                    """
                    UPDATE face_recognition.face_embeddings
                       SET topic_id = $1
                     WHERE topic_id = $2
                    """,
                    target_topic_db_id,
                    source_topic_db_id,
                )

                # Step 2: move uploaded_media references
                await conn.execute(
                    """
                    UPDATE face_recognition.uploaded_media
                       SET topic_id = $1
                     WHERE topic_id = $2
                    """,
                    target_topic_db_id,
                    source_topic_db_id,
                )

                # Step 3: recount face_count for target
                await conn.execute(
                    """
                    UPDATE face_recognition.telegram_topics
                       SET face_count = (
                               SELECT COUNT(*)
                                 FROM face_recognition.face_embeddings
                                WHERE topic_id = $1
                           ),
                           updated_at = NOW()
                     WHERE id = $1
                    """,
                    target_topic_db_id,
                )

                # Step 4: delete source topic row
                await conn.execute(
                    "DELETE FROM face_recognition.telegram_topics WHERE id = $1",
                    source_topic_db_id,
                )

        logger.info(
            "merge_identities: source=%d merged into target=%d",
            source_topic_db_id,
            target_topic_db_id,
        )

        # Step 5: best-effort Telegram forum topic deletion (outside transaction)
        if source_telegram_topic_id and source_telegram_topic_id != 0:
            await self._delete_telegram_topic(source_telegram_topic_id)

    # ------------------------------------------------------------------
    # Task 8.3 — split_identity
    # ------------------------------------------------------------------

    async def split_identity(
        self,
        source_topic_db_id: int,
        embedding_ids_to_split: List[int],
    ) -> int:
        """
        Splits selected embeddings from Identity A into a new Identity B.
        All steps in one transaction. Returns new_topic_db_id.
        Requirements: 13.2, 13.4
        """
        async with self._db_pool.acquire() as conn:
            async with conn.transaction():
                # Step 1: create new telegram_topics row
                new_topic_db_id: int = await conn.fetchval(
                    """
                    INSERT INTO face_recognition.telegram_topics
                        (label, face_count, message_count)
                    VALUES ('Unknown Person', 0, 0)
                    RETURNING id
                    """
                )

                # Step 2: reassign selected embeddings to new topic
                await conn.execute(
                    """
                    UPDATE face_recognition.face_embeddings
                       SET topic_id = $1
                     WHERE id = ANY($2::bigint[])
                       AND topic_id = $3
                    """,
                    new_topic_db_id,
                    embedding_ids_to_split,
                    source_topic_db_id,
                )

                # Step 3: recount face_count for source
                await conn.execute(
                    """
                    UPDATE face_recognition.telegram_topics
                       SET face_count = (
                               SELECT COUNT(*)
                                 FROM face_recognition.face_embeddings
                                WHERE topic_id = $1
                           ),
                           updated_at = NOW()
                     WHERE id = $1
                    """,
                    source_topic_db_id,
                )

                # Step 4: recount face_count for new topic
                await conn.execute(
                    """
                    UPDATE face_recognition.telegram_topics
                       SET face_count = (
                               SELECT COUNT(*)
                                 FROM face_recognition.face_embeddings
                                WHERE topic_id = $1
                           ),
                           updated_at = NOW()
                     WHERE id = $1
                    """,
                    new_topic_db_id,
                )

        logger.info(
            "split_identity: source=%d → new_topic=%d (%d embeddings moved)",
            source_topic_db_id,
            new_topic_db_id,
            len(embedding_ids_to_split),
        )
        return new_topic_db_id

    # ------------------------------------------------------------------
    # Task 8.4 — rename_identity
    # ------------------------------------------------------------------

    async def rename_identity(self, topic_db_id: int, new_label: str) -> None:
        """
        UPDATE face_recognition.telegram_topics SET label=$2, updated_at=NOW() WHERE id=$1
        Requirements: 13.3
        """
        async with self._db_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE face_recognition.telegram_topics
                   SET label      = $2,
                       updated_at = NOW()
                 WHERE id = $1
                """,
                topic_db_id,
                new_label,
            )

        logger.info(
            "rename_identity: topic_db_id=%d renamed to %r",
            topic_db_id,
            new_label,
        )

    # ------------------------------------------------------------------
    # Internal helper — delete Telegram forum topic (best-effort)
    # ------------------------------------------------------------------

    async def _delete_telegram_topic(self, telegram_topic_id: int) -> None:
        """
        Attempts to delete the Telegram forum topic from the Hub Group.
        Failure is logged but never propagated — this is best-effort only.
        """
        hub_id = get_hub_group_id()
        if hub_id is None:
            logger.warning(
                "_delete_telegram_topic: HUB_GROUP_ID not configured, skipping deletion "
                "of telegram_topic_id=%d",
                telegram_topic_id,
            )
            return

        try:
            bot = self._bot_pool.get_bot()
            await bot.client(_DeleteForumTopicRequest(hub_id, telegram_topic_id))
            logger.info(
                "_delete_telegram_topic: deleted telegram_topic_id=%d from hub",
                telegram_topic_id,
            )
        except Exception as exc:
            logger.warning(
                "_delete_telegram_topic: failed to delete telegram_topic_id=%d: %s",
                telegram_topic_id,
                exc,
            )


# ---------------------------------------------------------------------------
# Telethon helper — DeleteForumTopic request (lazy import)
# ---------------------------------------------------------------------------

def _DeleteForumTopicRequest(peer, topic_id: int):
    """
    Returns a Telethon DeleteForumTopicRequest for the given peer and topic_id.
    Imported lazily so the module can be imported without Telethon installed.
    """
    from telethon.tl.functions.channels import DeleteForumTopicRequest
    return DeleteForumTopicRequest(channel=peer, topic_id=topic_id)
