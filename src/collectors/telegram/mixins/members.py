"""MembersMixin — chat member enumeration (daily 03:00 SGT cron).

Extracted from :mod:`src.collectors.telegram` (PERF-004 sub-plan 4C step 7).
Mixed into :class:`TelegramCollector` via multiple inheritance. All ``self.*``
references resolve at MRO-time to the composed class.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from src.collectors.telegram.helpers import _format_exception, _is_flood_wait

if TYPE_CHECKING:  # pragma: no cover — forward-ref hints only
    from src.collectors.telegram.session import TelegramWorker


logger = logging.getLogger("src.collectors.telegram")


class MembersMixin:
    """Iterate chat participants and upsert into ``telegram_chat_members``."""

    async def collect_chat_members(self, chat_id, worker: "TelegramWorker | None" = None) -> int:
        """Iterate participants of chat_id and upsert into telegram_chat_members.

        Per-memory PRD: refreshed daily at 03:00 SGT for common-chat-membership
        analytics. Sets refreshed_at = NOW() so stale rows can be pruned.

        Schema (post-Phase-1): chat_id and user_id are UUIDs that FK back to
        telegram_chats(id) and telegram_users(id). We resolve from the platform
        bigint IDs to UUIDs by querying after _upsert_chat / _upsert_user_full.

        Returns the number of upserted member rows.
        """
        if worker is None:
            if not self._workers:
                self._workers = await self._spawn_workers()
            if not self._workers:
                logger.error("collect_chat_members: no workers — bailing")
                return 0
            worker = self._workers[0]

        client = worker.client
        try:
            entity = await client.get_entity(int(chat_id))
        except (ValueError, TypeError):
            entity = await client.get_entity(chat_id)

        chat_platform_id = str(getattr(entity, "id"))
        await self._upsert_chat(entity)

        # Resolve chat UUID once.
        async with self.pool.acquire() as conn:
            chat_row = await conn.fetchrow(
                "SELECT id FROM telegram_chats WHERE platform_chat_id = $1",
                chat_platform_id,
            )
        if chat_row is None:
            logger.error(
                "collect_chat_members: chat %s not in DB after upsert — bailing",
                chat_platform_id,
            )
            return 0
        chat_uuid = chat_row["id"]

        seen: set[int] = set()
        upserted = 0
        try:
            async for participant in client.iter_participants(entity):
                if self._stop.is_set():
                    break
                pid = getattr(participant, "id", None)
                if pid is None or pid in seen:
                    continue
                seen.add(pid)

                # Best-effort upsert into telegram_users so the FK target exists.
                try:
                    await self._upsert_user_full(participant)
                except Exception:
                    pass

                # Resolve user UUID — _upsert_user_full just guaranteed the row exists.
                async with self.pool.acquire() as conn:
                    user_row = await conn.fetchrow(
                        "SELECT id FROM telegram_users WHERE platform_user_id = $1",
                        str(pid),
                    )
                if user_row is None:
                    continue
                user_uuid = user_row["id"]

                # Determine role from participant.participant.* attributes.
                role = "member"
                p = getattr(participant, "participant", None)
                if p is not None:
                    pname = type(p).__name__
                    if "Creator" in pname:
                        role = "creator"
                    elif "Admin" in pname:
                        role = "admin"
                    elif "Banned" in pname:
                        role = "banned"
                    elif "Left" in pname:
                        role = "left"

                joined_at = None
                if p is not None:
                    joined_at = getattr(p, "date", None)

                async with self.pool.acquire() as conn:
                    await conn.execute("""
                        INSERT INTO telegram_chat_members
                            (chat_id, user_id, role, joined_at, last_seen_at, refreshed_at)
                        VALUES ($1, $2, $3, $4, NOW(), NOW())
                        ON CONFLICT (chat_id, user_id) DO UPDATE SET
                            role = EXCLUDED.role,
                            joined_at = COALESCE(EXCLUDED.joined_at, telegram_chat_members.joined_at),
                            last_seen_at = NOW(),
                            refreshed_at = NOW()
                    """, chat_uuid, user_uuid, role, joined_at)
                upserted += 1
        except Exception as exc:
            if _is_flood_wait(exc):
                await self._handle_flood_wait(worker, exc)
            else:
                logger.error(
                    "collect_chat_members chat=%s failed: %s",
                    chat_platform_id, _format_exception(exc),
                )

        logger.info(
            "collect_chat_members: chat=%s upserted=%d",
            chat_platform_id, upserted,
        )
        return upserted
