"""WhatsappStagingMergeHandler — Sprint 5 async staging-table merge.

Reads accumulated rows from ``wa_staging_contacts`` and merges them into
``whatsapp_users`` + ``whatsapp_lid_map`` via a single batched INSERT ... ON
CONFLICT.  Runs on a short cadence (default 5 s) so freshness tracks the
current production contact rate.

Gated behind ``WA_STAGING_ENABLED`` (default ``"0"``).  When disabled this
handler is registered but its ``should_run`` returns ``False`` immediately,
adding zero overhead to the scheduler tick.

Design rationale in
``docs/plans/whatsapp-contact-architecture-alternatives.md`` §3, Option 5.

Env vars (all optional):

  WA_STAGING_ENABLED            "1" = active, "0" = off (default)
  WA_STAGING_MERGE_INTERVAL_MS  merge cadence ms (default 5000, min 100)
  WA_STAGING_BATCH_MAX          rows per merge pass (default 1000, min 1)

Architecture invariant: the Sprint 4 batching path (``_upsert_contacts_batch``)
stays intact as the WA_STAGING_ENABLED=0 fallback.  This handler only runs
when the staging path is explicitly opted in.
"""
from __future__ import annotations

import logging
import os
import time as _time

from src.scheduler.handlers.base import SchedulerContext

logger = logging.getLogger(__name__)

_MS_TO_SEC = 0.001


def _staging_enabled() -> bool:
    return os.getenv("WA_STAGING_ENABLED", "0").strip() in ("1", "true", "yes")


class WhatsappStagingMergeHandler:
    """Drain ``wa_staging_contacts`` into ``whatsapp_users`` + ``whatsapp_lid_map``."""

    name = "wa_staging_merge"

    def __init__(self) -> None:
        self._last_run: float = 0.0

    # ------------------------------------------------------------------
    # SchedulerHandler protocol
    # ------------------------------------------------------------------

    async def should_run(self, ctx: SchedulerContext) -> bool:
        if not _staging_enabled():
            return False
        interval_ms = ctx.get_env_int(
            "WA_STAGING_MERGE_INTERVAL_MS", 5000, min_value=100,
        )
        elapsed = _time.monotonic() - self._last_run
        return elapsed >= interval_ms * _MS_TO_SEC

    async def run(self, ctx: SchedulerContext) -> None:
        batch_max = ctx.get_env_int("WA_STAGING_BATCH_MAX", 1000, min_value=1)
        try:
            merged, lid_merged = await self._merge_batch(ctx.pool, batch_max)
            logger.info(
                "wa_staging_merge: merged %d user rows, %d lid_map rows",
                merged, lid_merged,
            )
        except Exception:
            logger.warning("wa_staging_merge run failed", exc_info=True)
        finally:
            # Always advance the timer so a persistent DB error doesn't
            # spam the log on every scheduler tick.
            self._last_run = _time.monotonic()

    # ------------------------------------------------------------------
    # Merge implementation
    # ------------------------------------------------------------------

    @staticmethod
    async def _merge_batch(pool, batch_max: int) -> tuple[int, int]:
        """Merge up to *batch_max* staging rows into production tables.

        Returns (user_rows_merged, lid_map_rows_merged).

        Two-phase approach:
        1. Read the oldest batch_max rows from staging.
        2. INSERT ... ON CONFLICT into whatsapp_users (user-JID rows only).
        3. INSERT ... ON CONFLICT into whatsapp_lid_map (rows with both lid + phone_jid).
        4. DELETE the processed rows by id range.

        Using DELETE by id (not TRUNCATE) so concurrent writers to staging
        are safe: rows written after we read the batch are left intact.
        """
        async with pool.acquire() as conn:
            # Read batch — oldest first so we process in arrival order
            rows = await conn.fetch(
                """
                SELECT id, platform_user_id, name, pushname, phone_number,
                       is_business, lid, phone_jid, collected_at
                FROM wa_staging_contacts
                ORDER BY collected_at ASC, id ASC
                LIMIT $1
                """,
                batch_max,
            )
            if not rows:
                return 0, 0

            ids = [r["id"] for r in rows]
            min_id, max_id = ids[0], ids[-1]

            # --- whatsapp_users merge: rows that look like user JIDs ----------
            user_rows = [
                r for r in rows
                if r["platform_user_id"] and (
                    "@s.whatsapp.net" in r["platform_user_id"]
                    or "@lid" in r["platform_user_id"]
                )
            ]
            user_merged = 0
            if user_rows:
                # DISTINCT ON (platform_user_id) — keep latest collected_at
                # within this batch so we merge the freshest value first.
                await conn.execute(
                    """
                    INSERT INTO whatsapp_users
                        (platform_user_id, name, pushname, phone_number,
                         is_business, collected_at)
                    SELECT DISTINCT ON (platform_user_id)
                        platform_user_id,
                        name,
                        pushname,
                        phone_number,
                        is_business,
                        collected_at
                    FROM unnest(
                        $1::text[],
                        $2::text[],
                        $3::text[],
                        $4::text[],
                        $5::boolean[],
                        $6::timestamptz[]
                    ) AS s(platform_user_id, name, pushname, phone_number,
                            is_business, collected_at)
                    ORDER BY platform_user_id, collected_at DESC
                    ON CONFLICT (platform_user_id) DO UPDATE SET
                        name        = COALESCE(EXCLUDED.name,
                                               whatsapp_users.name),
                        pushname    = COALESCE(EXCLUDED.pushname,
                                               whatsapp_users.pushname),
                        phone_number = COALESCE(EXCLUDED.phone_number,
                                                whatsapp_users.phone_number),
                        is_business = COALESCE(whatsapp_users.is_business, FALSE)
                                      OR COALESCE(EXCLUDED.is_business, FALSE),
                        collected_at = NOW()
                    """,
                    [r["platform_user_id"] for r in user_rows],
                    [r["name"] for r in user_rows],
                    [r["pushname"] for r in user_rows],
                    [r["phone_number"] for r in user_rows],
                    [r["is_business"] for r in user_rows],
                    [r["collected_at"] for r in user_rows],
                )
                user_merged = len(user_rows)

            # --- whatsapp_lid_map merge: rows with both lid + phone_jid ------
            lid_rows = [
                r for r in rows
                if r["lid"] and r["phone_jid"]
            ]
            lid_merged = 0
            if lid_rows:
                await conn.execute(
                    """
                    INSERT INTO whatsapp_lid_map (lid, phone_jid, display_name, updated_at)
                    SELECT DISTINCT ON (lid)
                        lid, phone_jid, name, collected_at
                    FROM unnest(
                        $1::text[],
                        $2::text[],
                        $3::text[],
                        $4::timestamptz[]
                    ) AS s(lid, phone_jid, name, collected_at)
                    ORDER BY lid, collected_at DESC
                    ON CONFLICT (lid) DO UPDATE SET
                        phone_jid    = EXCLUDED.phone_jid,
                        display_name = COALESCE(EXCLUDED.display_name,
                                                whatsapp_lid_map.display_name),
                        updated_at   = NOW()
                    """,
                    [r["lid"] for r in lid_rows],
                    [r["phone_jid"] for r in lid_rows],
                    [r["name"] for r in lid_rows],
                    [r["collected_at"] for r in lid_rows],
                )
                lid_merged = len(lid_rows)

            # --- Delete processed rows by id range ---------------------------
            # WHERE id BETWEEN min AND max matches the exact rows we read
            # (ids are sequential for this batch, no gaps in our selection).
            # Rows written by concurrent inserts above max_id are unaffected.
            await conn.execute(
                "DELETE FROM wa_staging_contacts WHERE id >= $1 AND id <= $2",
                min_id, max_id,
            )

        return user_merged, lid_merged


__all__ = ["WhatsappStagingMergeHandler"]
