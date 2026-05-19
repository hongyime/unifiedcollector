import logging
import uuid
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class MatchResult:
    identity_id: str
    is_new: bool
    distance: float


class FaceMatcher:

    def __init__(self, match_threshold: float = 0.6):
        self._threshold = match_threshold

    async def match_or_create(self, pool, embedding: list[float]) -> MatchResult:
        embedding_str = "[" + ",".join(f"{v:.6f}" for v in embedding) + "]"

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, centroid <-> $1::vector AS distance
                FROM wa_face_identities
                WHERE centroid IS NOT NULL
                ORDER BY centroid <-> $1::vector
                LIMIT 1
                """,
                embedding_str,
            )

            if row and row["distance"] <= self._threshold:
                identity_id = str(row["id"])
                await self._update_centroid(conn, identity_id, embedding)
                return MatchResult(identity_id=identity_id, is_new=False, distance=row["distance"])

            new_id = str(uuid.uuid4())
            await conn.execute(
                """
                INSERT INTO wa_face_identities (id, centroid, occurrence_count)
                VALUES ($1, $2::vector, 1)
                """,
                new_id, embedding_str,
            )
            return MatchResult(identity_id=new_id, is_new=True, distance=0.0)

    async def _update_centroid(self, conn, identity_id: str, new_embedding: list[float]):
        row = await conn.fetchrow(
            "SELECT centroid, occurrence_count FROM wa_face_identities WHERE id = $1",
            identity_id,
        )
        if not row or row["centroid"] is None:
            return

        count = row["occurrence_count"]
        old_centroid = list(row["centroid"])
        new_centroid = [
            (old * count + new) / (count + 1)
            for old, new in zip(old_centroid, new_embedding)
        ]
        centroid_str = "[" + ",".join(f"{v:.6f}" for v in new_centroid) + "]"

        await conn.execute(
            """
            UPDATE wa_face_identities
            SET centroid = $1::vector,
                occurrence_count = occurrence_count + 1,
                last_seen = NOW()
            WHERE id = $2
            """,
            centroid_str, identity_id,
        )

    async def store_embedding(self, pool, identity_id: str, embedding: list[float],
                               source_content_id: str, source_entity_id: str,
                               frame_index: int = 0, confidence: float = 0.0,
                               bbox: tuple[int, int, int, int] | None = None):
        embedding_str = "[" + ",".join(f"{v:.6f}" for v in embedding) + "]"
        bx, by, bw, bh = bbox if bbox else (0, 0, 0, 0)

        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO wa_face_embeddings
                    (identity_id, embedding, source_content_id, source_entity_id,
                     frame_index, confidence, bbox_x, bbox_y, bbox_w, bbox_h)
                VALUES ($1, $2::vector, $3, $4, $5, $6, $7, $8, $9, $10)
                """,
                identity_id, embedding_str, source_content_id, source_entity_id,
                frame_index, confidence, bx, by, bw, bh,
            )

    async def merge_identities(self, pool, source_id: str, target_id: str):
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE wa_face_embeddings SET identity_id = $1 WHERE identity_id = $2",
                    target_id, source_id,
                )
                source = await conn.fetchrow(
                    "SELECT occurrence_count FROM wa_face_identities WHERE id = $1", source_id,
                )
                if source:
                    await conn.execute(
                        """
                        UPDATE wa_face_identities
                        SET occurrence_count = occurrence_count + $1
                        WHERE id = $2
                        """,
                        source["occurrence_count"], target_id,
                    )
                await conn.execute("DELETE FROM wa_face_identities WHERE id = $1", source_id)

        logger.info("Merged identity %s into %s", source_id, target_id)

    async def rename_identity(self, pool, identity_id: str, label: str):
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE wa_face_identities SET label = $1 WHERE id = $2",
                label, identity_id,
            )
