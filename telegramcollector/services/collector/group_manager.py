"""GroupManager — polls collector.group_join_queue and executes join requests."""

import asyncio
import logging
import re
import time
from collections import defaultdict

from telethon.tl.functions.channels import JoinChannelRequest, LeaveChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.errors import (
    FloodWaitError,
    InviteHashExpiredError,
    UserAlreadyParticipantError,
    ChatAdminRequiredError,
    UserPrivacyRestrictedError,
)

from shared.database import get_db_connection
from services.collector.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)


class GroupManager:
    POLL_INTERVAL = 60          # seconds between polls
    MAX_JOINS_PER_HOUR = 5      # max joins per account per rolling 60-minute window
    MIN_JOIN_INTERVAL = 30      # minimum seconds between consecutive joins on same account

    def __init__(self, rate_limiter: RateLimiter, clients: list) -> None:
        self.rate_limiter = rate_limiter
        self.clients = clients
        # account_id → list of monotonic timestamps of completed joins
        self._join_history: dict[int, list[float]] = defaultdict(list)
        # account_id → monotonic timestamp of last join
        self._last_join_time: dict[int, float] = defaultdict(float)
        self._running: bool = False
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Set running flag and launch the poll loop as an asyncio task."""
        self._running = True
        self._task = asyncio.create_task(self._poll_loop(), name="group_manager_poll")
        logger.info("GroupManager started")

    async def stop(self) -> None:
        """Cancel the poll loop task and wait for it to finish."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("GroupManager stopped")

    # ------------------------------------------------------------------
    # Poll loop
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        """Main loop: fetch pending rows, process each, then sleep."""
        while self._running:
            try:
                rows = await self._fetch_pending_rows()
                for row in rows:
                    if not self._running:
                        break
                    await self._process_row(row)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("GroupManager poll loop error: %s", exc, exc_info=True)
            await asyncio.sleep(self.POLL_INTERVAL)

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    async def _fetch_pending_rows(self) -> list[dict]:
        """Return up to 10 pending join-queue rows ordered by added_at ASC."""
        try:
            async with get_db_connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        SELECT id, link, account_id, language_filter
                        FROM collector.group_join_queue
                        WHERE status = 'pending'
                        ORDER BY added_at ASC
                        LIMIT 10
                        """
                    )
                    cols = [desc[0] for desc in cur.description]
                    rows = await cur.fetchall()
                    return [dict(zip(cols, row)) for row in rows]
        except Exception as exc:
            logger.error("Failed to fetch pending group_join_queue rows: %s", exc)
            return []

    async def _update_row(self, row_id: int, status: str, error: str | None = None) -> None:
        """UPDATE group_join_queue status/processed_at/error for a given row id."""
        try:
            async with get_db_connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        UPDATE collector.group_join_queue
                        SET status = %s, processed_at = NOW(), error = %s
                        WHERE id = %s
                        """,
                        (status, error, row_id),
                    )
        except Exception as exc:
            logger.error("Failed to update group_join_queue row %s: %s", row_id, exc)

    # ------------------------------------------------------------------
    # Client lookup
    # ------------------------------------------------------------------

    def _get_client(self, account_id: int | None):
        """Return the TelegramClientManager for account_id, or the first available."""
        if account_id is not None:
            for client in self.clients:
                if getattr(client, "account_id", None) == account_id:
                    return client
        # Fallback: return first available client
        if self.clients:
            return self.clients[0]
        return None

    # ------------------------------------------------------------------
    # Link parsing
    # ------------------------------------------------------------------

    def _parse_invite_link(self, link: str) -> tuple[str, bool]:
        """Parse a Telegram invite/username link.

        Returns:
            (identifier, is_invite_hash)
            is_invite_hash=True  → use ImportChatInviteRequest(identifier)
            is_invite_hash=False → use JoinChannelRequest(identifier)
        """
        link = link.strip()

        # t.me/+HASH  or  t.me/joinchat/HASH
        m = re.match(r"(?:https?://)?t\.me/\+([A-Za-z0-9_-]+)", link)
        if m:
            return (m.group(1), True)

        m = re.match(r"(?:https?://)?t\.me/joinchat/([A-Za-z0-9_-]+)", link)
        if m:
            return (m.group(1), True)

        # t.me/username
        m = re.match(r"(?:https?://)?t\.me/([A-Za-z0-9_]+)", link)
        if m:
            return (m.group(1), False)

        # @username
        m = re.match(r"@([A-Za-z0-9_]+)", link)
        if m:
            return (m.group(1), False)

        # bare username
        return (link, False)

    # ------------------------------------------------------------------
    # Language filter
    # ------------------------------------------------------------------

    def _should_skip_language(self, title: str) -> bool:
        """Return True if >50% of non-whitespace chars are Cyrillic/Japanese/Chinese."""
        non_ws = [ch for ch in title if not ch.isspace()]
        if not non_ws:
            return False

        def _in_target_block(ch: str) -> bool:
            cp = ord(ch)
            return (
                0x0400 <= cp <= 0x04FF  # Cyrillic
                or 0x3040 <= cp <= 0x309F  # Hiragana
                or 0x30A0 <= cp <= 0x30FF  # Katakana
                or 0x4E00 <= cp <= 0x9FFF  # CJK Unified Ideographs
                or 0x3400 <= cp <= 0x4DBF  # CJK Extension A
            )

        target_count = sum(1 for ch in non_ws if _in_target_block(ch))
        return target_count / len(non_ws) > 0.5

    # ------------------------------------------------------------------
    # Row processing
    # ------------------------------------------------------------------

    async def _process_row(self, row: dict) -> None:
        """Execute a single join-queue row with all safety checks."""
        row_id: int = row["id"]
        link: str = row["link"]
        account_id: int | None = row.get("account_id")
        language_filter: bool = bool(row.get("language_filter", False))

        now = time.monotonic()

        # 1. Hourly cap check
        history = self._join_history[account_id]
        recent = [t for t in history if now - t < 3600]
        self._join_history[account_id] = recent  # prune old entries
        if len(recent) >= self.MAX_JOINS_PER_HOUR:
            logger.warning(
                "GroupManager: hourly join cap reached for account %s — skipping row %s",
                account_id,
                row_id,
            )
            return

        # 2. Minimum interval check
        last = self._last_join_time[account_id]
        if last:
            elapsed = now - last
            if elapsed < self.MIN_JOIN_INTERVAL:
                remainder = self.MIN_JOIN_INTERVAL - elapsed
                logger.debug(
                    "GroupManager: waiting %.1fs before next join for account %s",
                    remainder,
                    account_id,
                )
                await asyncio.sleep(remainder)

        # 3. Language filter
        if language_filter:
            identifier, is_hash = self._parse_invite_link(link)
            # For invite hashes we can't know the title without joining; skip filter.
            # For username links we can resolve the entity title first.
            if not is_hash:
                client_mgr = self._get_client(account_id)
                if client_mgr is not None:
                    try:
                        entity = await client_mgr.client.get_entity(identifier)
                        title = getattr(entity, "title", "") or ""
                        if self._should_skip_language(title):
                            logger.info(
                                "GroupManager: skipping row %s — language filter matched title %r",
                                row_id,
                                title,
                            )
                            await self._update_row(row_id, "skipped")
                            return
                    except Exception as exc:
                        logger.warning(
                            "GroupManager: could not resolve entity for language filter (row %s): %s",
                            row_id,
                            exc,
                        )

        # 4. Rate limiter
        await self.rate_limiter.acquire(account_id)

        # 5. Get client
        client_mgr = self._get_client(account_id)
        if client_mgr is None:
            logger.error("GroupManager: no client available for account %s — skipping row %s", account_id, row_id)
            await self._update_row(row_id, "failed", "no_client_available")
            return

        tg_client = client_mgr.client

        # 6. Parse link and execute join
        identifier, is_invite_hash = self._parse_invite_link(link)
        try:
            if is_invite_hash:
                result = await tg_client(ImportChatInviteRequest(identifier))
            else:
                result = await tg_client(JoinChannelRequest(identifier))

            # 7. Check for linked discussion group (channels only)
            try:
                from telethon.tl.functions.channels import GetFullChannelRequest
                from telethon.tl.types import Channel

                # Determine the joined entity
                joined_entity = None
                if hasattr(result, "chats") and result.chats:
                    joined_entity = result.chats[0]

                if joined_entity is not None and isinstance(joined_entity, Channel) and not joined_entity.megagroup:
                    full = await tg_client(GetFullChannelRequest(joined_entity))
                    linked_chat_id = getattr(full.full_chat, "linked_chat_id", None)
                    if not linked_chat_id:
                        # Leave the channel and mark failed
                        try:
                            await tg_client(LeaveChannelRequest(joined_entity))
                        except Exception as leave_exc:
                            logger.warning("GroupManager: failed to leave channel after no discussion group: %s", leave_exc)
                        await self._update_row(row_id, "failed", "no_discussion_group")
                        logger.info("GroupManager: left channel (no discussion group) for row %s", row_id)
                        return
            except (ChatAdminRequiredError, UserPrivacyRestrictedError):
                pass  # Can't get full info; proceed as joined
            except Exception as check_exc:
                logger.warning("GroupManager: discussion group check failed for row %s: %s", row_id, check_exc)

            # 8. Mark joined
            await self._update_row(row_id, "joined")
            logger.info("GroupManager: joined successfully for row %s (link=%s)", row_id, link)

        except FloodWaitError as exc:
            wait = exc.seconds + 10
            logger.warning("GroupManager: FloodWaitError for row %s — sleeping %ds", row_id, wait)
            self.rate_limiter.set_flood_wait(account_id, exc.seconds)
            await asyncio.sleep(wait)
            raise  # re-raise so poll_loop can handle / retry next cycle

        except InviteHashExpiredError:
            logger.warning("GroupManager: invite hash expired for row %s", row_id)
            await self._update_row(row_id, "failed", "invite_hash_expired")
            return

        except UserAlreadyParticipantError:
            logger.info("GroupManager: already a participant for row %s", row_id)
            await self._update_row(row_id, "joined")

        except Exception as exc:
            logger.error("GroupManager: join failed for row %s: %s", row_id, exc, exc_info=True)
            await self._update_row(row_id, "failed", str(exc))
            return

        # 9. Record join in history
        ts = time.monotonic()
        self._join_history[account_id].append(ts)
        self._last_join_time[account_id] = ts
