import logging
import re
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
        # Single-statement update so we can't lose updates between concurrent matches.
        # We compute the new centroid in SQL using the current row's count + centroid.
        new_str = "[" + ",".join(f"{v:.6f}" for v in new_embedding) + "]"
        await conn.execute(
            """
            UPDATE wa_face_identities
            SET centroid = (
                (centroid * occurrence_count + $1::vector) / (occurrence_count + 1)
            ),
                occurrence_count = occurrence_count + 1,
                last_seen = NOW()
            WHERE id = $2 AND centroid IS NOT NULL
            """,
            new_str, identity_id,
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
        if source_id == target_id:
            logger.warning("merge_identities: source == target (%s) — refusing self-merge",
                           source_id)
            return
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

    _LABEL_RE = re.compile(r"^[A-Za-z0-9 _.\-]{1,80}$")

    async def rename_identity(self, pool, identity_id: str, label: str):
        # Validate label to prevent stored garbage / control chars / oversize.
        if not isinstance(label, str):
            raise ValueError("label must be a string")
        label = label.strip()
        if not self._LABEL_RE.match(label):
            raise ValueError(
                "label must be 1-80 chars of [A-Za-z0-9 _.-] only"
            )
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE wa_face_identities SET label = $1 WHERE id = $2",
                label, identity_id,
            )
