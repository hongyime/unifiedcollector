"""AdminLogPoller — periodic admin log event capture for monitored channels.

Design constraints:
- Polls admin log events for each channel where the account has admin rights
- Calls rate_limiter.acquire(account_id) before every Telegram API call
- Writes events to collector.admin_log_events with ON CONFLICT DO NOTHING
- Routes deletions → collector.message_deletions
- Routes edits → collector.message_edits
- Routes member changes → collector.chat_members (upsert)
- Tracks last_event_id per channel in collector.backfill_state (poll_type='admin_log')
- last_event_id is monotonically increasing (only updated when new_id > current)
- Handles FloodWaitError: wait error.seconds + 10, coordinate with rate_limiter
- Skips channels where account lacks admin log read permission (ChatAdminRequiredError)
- Per-channel error isolation: catch exceptions, log, continue to next channel
- NO imports from face_processor, identity_matcher, processing_queue
"""

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from shared.database import get_db_connection
from shared.config import settings

if TYPE_CHECKING:
    from services.collector.rate_limiter import RateLimiter
    from shared.telegram_client import TelegramClientManager

logger = logging.getLogger(__name__)


async def _retry_with_backoff(func, max_attempts: int = 3, base_delay: float = 1.0):
    """Retry an async callable with exponential backoff."""
    for attempt in range(max_attempts):
        try:
            return await func()
        except Exception as exc:
            if attempt == max_attempts - 1:
                raise
            delay = base_delay * (2 ** attempt)
            logger.warning(f"Retry {attempt + 1}/{max_attempts} after {delay}s: {exc}")
            await asyncio.sleep(delay)


def _is_flood_wait(exc: Exception) -> bool:
    """Return True if exc is a FloodWaitError."""
    name = type(exc).__name__
    return name == "FloodWaitError" or (
        hasattr(exc, "seconds") and "flood" in name.lower()
    )


def _is_admin_required(exc: Exception) -> bool:
    """Return True if exc indicates missing admin permissions."""
    name = type(exc).__name__
    return "ChatAdminRequired" in name or "AdminRequired" in name


class AdminLogPoller:
    """Periodic admin log event capture for monitored channels."""

    def __init__(
        self,
        clients: list,
        rate_limiter: "RateLimiter",
    ) -> None:
        """
        Args:
            clients: Shared TelegramClientManager pool
            rate_limiter: Shared RateLimiter instance
        """
        self.clients = clients
        self.rate_limiter = rate_limiter

        self._poll_task: asyncio.Task | None = None
        self._running = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Begin polling admin logs for all channels."""
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info("AdminLogPoller started")

    async def stop(self) -> None:
        """Cancel all tasks and cleanup."""
        self._running = False
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        logger.info("AdminLogPoller stopped")

    # ------------------------------------------------------------------
    # Poll loop
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        """Poll admin log events for each channel on a fixed interval."""
        while self._running:
            try:
                channels = await self._get_channels_with_admin_access()
                for channel in channels:
                    if not self._running:
                        break
                    chat_id = channel.get("chat_id")
                    account_id = channel.get("account_id")
                    client = self._get_client(account_id)
                    if client is None:
                        logger.warning(
                            f"AdminLogPoller: no client for account_id={account_id}, "
                            f"skipping chat_id={chat_id}"
                        )
                        continue
                    try:
                        await self._poll_channel_logs(client, chat_id)
                    except Exception as exc:
                        logger.error(
                            f"AdminLogPoller: channel chat_id={chat_id} "
                            f"account_id={account_id} failed: {exc}",
                            exc_info=True,
                        )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"AdminLogPoller _poll_loop error: {exc}", exc_info=True)

            try:
                await asyncio.sleep(settings.COLLECTOR_ADMIN_LOG_POLL_INTERVAL)
            except asyncio.CancelledError:
                break

    async def _get_channels_with_admin_access(self) -> list[dict]:
        """Return channels from collector.monitored_chats where account has admin rights."""
        try:
            async with get_db_connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        SELECT mc.chat_id, mc.account_id
                        FROM collector.monitored_chats mc
                        JOIN collector.telegram_accounts ta
                            ON ta.id = mc.account_id
                        WHERE ta.status = 'active'
                        ORDER BY mc.chat_id ASC
                        """
                    )
                    rows = await cur.fetchall()
                    if not rows:
                        return []
                    cols = [d[0] for d in cur.description]
                    return [dict(zip(cols, row)) for row in rows]
        except Exception as exc:
            logger.error(f"AdminLogPoller: failed to fetch channels: {exc}", exc_info=True)
            return []

    # ------------------------------------------------------------------
    # Per-channel polling
    # ------------------------------------------------------------------

    async def _poll_channel_logs(
        self, client: "TelegramClientManager", chat_id: int
    ) -> None:
        """Fetch and process admin log events for a single channel."""
        account_id = getattr(client, "account_id", None)

        # Get last processed event ID for incremental fetch
        last_event_id = await self._get_last_event_id(chat_id)

        try:
            await self.rate_limiter.acquire(account_id)
            events = []
            async for event in client.client.iter_admin_log(
                chat_id,
                min_id=last_event_id or 0,
            ):
                events.append(event)
        except Exception as exc:
            if _is_flood_wait(exc):
                await self._handle_flood_wait(exc, account_id)
                # Retry once after flood wait
                await self.rate_limiter.acquire(account_id)
                events = []
                async for event in client.client.iter_admin_log(
                    chat_id,
                    min_id=last_event_id or 0,
                ):
                    events.append(event)
            elif _is_admin_required(exc):
                logger.warning(
                    f"AdminLogPoller: no admin permission for chat_id={chat_id}, skipping"
                )
                return
            else:
                raise

        for event in events:
            try:
                await self._process_admin_event(event, chat_id)
            except Exception as exc:
                logger.warning(
                    f"AdminLogPoller: failed to process event "
                    f"chat_id={chat_id} event_id={getattr(event, 'id', '?')}: {exc}"
                )

    # ------------------------------------------------------------------
    # Event processing
    # ------------------------------------------------------------------

    async def _process_admin_event(self, event, chat_id: int) -> None:
        """Write event to admin_log_events and route to appropriate table."""
        event_id = getattr(event, "id", None)
        if event_id is None:
            return

        # Determine event type string
        action = getattr(event, "action", None)
        event_type = type(action).__name__ if action is not None else "unknown"

        user_id = getattr(event, "user_id", None)
        # Some events have user via .user attribute
        if user_id is None:
            user = getattr(event, "user", None)
            if user is not None:
                user_id = getattr(user, "id", None)

        message_id = None
        if action is not None:
            message_id = getattr(action, "id", None)
            if message_id is None:
                msg = getattr(action, "message", None)
                if msg is not None:
                    message_id = getattr(msg, "id", None)

        event_data = {}
        if hasattr(event, "to_dict"):
            try:
                event_data = event.to_dict()
            except Exception:
                pass

        event_data_json = json.dumps(event_data) if event_data else "{}"

        # Write to collector.admin_log_events (idempotent)
        async def _do_write():
            async with get_db_connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO collector.admin_log_events
                            (chat_id, event_id, event_type, user_id, message_id,
                             event_data, collected_at)
                        VALUES (%s, %s, %s, %s, %s, %s, NOW())
                        ON CONFLICT (chat_id, event_id) DO NOTHING
                        """,
                        (chat_id, event_id, event_type, user_id, message_id, event_data_json),
                    )

        await _retry_with_backoff(_do_write)

        # Route to specialised tables based on event type
        event_type_lower = event_type.lower()

        if "delete" in event_type_lower and message_id is not None:
            await self._write_deletion(chat_id, message_id)

        elif "edit" in event_type_lower and message_id is not None:
            payload = event_data if isinstance(event_data, dict) else {}
            await self._write_edit(chat_id, message_id, payload)

        elif any(
            kw in event_type_lower
            for kw in ("join", "leave", "member", "ban", "kick")
        ):
            role = _extract_member_role(action)
            if user_id is not None:
                await self._update_chat_member(chat_id, user_id, role)

        # Update last_event_id monotonically
        await self._set_last_event_id(chat_id, event_id)

    # ------------------------------------------------------------------
    # Specialised write helpers
    # ------------------------------------------------------------------

    async def _write_deletion(self, chat_id: int, message_id: int) -> None:
        """Insert into collector.message_deletions (idempotent)."""
        async def _do_write():
            async with get_db_connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO collector.message_deletions
                            (chat_id, message_id, deleted_at)
                        VALUES (%s, %s, NOW())
                        ON CONFLICT (chat_id, message_id) DO NOTHING
                        """,
                        (chat_id, message_id),
                    )

        await _retry_with_backoff(_do_write)

    async def _write_edit(
        self, chat_id: int, message_id: int, payload: dict
    ) -> None:
        """Insert into collector.message_edits (idempotent)."""
        payload_json = json.dumps(payload) if isinstance(payload, dict) else "{}"

        async def _do_write():
            async with get_db_connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO collector.message_edits
                            (chat_id, message_id, edited_at, payload)
                        VALUES (%s, %s, NOW(), %s)
                        ON CONFLICT (chat_id, message_id) DO NOTHING
                        """,
                        (chat_id, message_id, payload_json),
                    )

        await _retry_with_backoff(_do_write)

    async def _update_chat_member(
        self, chat_id: int, user_id: int, role: str
    ) -> None:
        """Upsert collector.chat_members with new role."""
        async def _do_upsert():
            async with get_db_connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO collector.chat_members
                            (chat_id, user_id, role, updated_at)
                        VALUES (%s, %s, %s, NOW())
                        ON CONFLICT (chat_id, user_id) DO UPDATE SET
                            role = EXCLUDED.role,
                            updated_at = NOW()
                        """,
                        (chat_id, user_id, role),
                    )

        await _retry_with_backoff(_do_upsert)

    # ------------------------------------------------------------------
    # last_event_id tracking
    # ------------------------------------------------------------------

    async def _get_last_event_id(self, chat_id: int) -> int | None:
        """Return last_event_id from backfill_state for this channel, or None."""
        try:
            async with get_db_connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        SELECT last_event_id
                        FROM collector.backfill_state
                        WHERE chat_id = %s AND poll_type = 'admin_log'
                        """,
                        (chat_id,),
                    )
                    row = await cur.fetchone()
            if row is None:
                return None
            return row[0]
        except Exception as exc:
            logger.warning(f"AdminLogPoller: failed to get last_event_id for chat_id={chat_id}: {exc}")
            return None

    async def _set_last_event_id(self, chat_id: int, event_id: int) -> None:
        """Update last_event_id in backfill_state only if event_id > current value (monotonic)."""
        async def _do_upsert():
            async with get_db_connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO collector.backfill_state
                            (chat_id, account_id, poll_type, last_event_id, updated_at)
                        VALUES (%s, NULL, 'admin_log', %s, NOW())
                        ON CONFLICT (chat_id, account_id, poll_type) DO UPDATE SET
                            last_event_id = GREATEST(
                                collector.backfill_state.last_event_id,
                                EXCLUDED.last_event_id
                            ),
                            updated_at = NOW()
                        """,
                        (chat_id, event_id),
                    )

        await _retry_with_backoff(_do_upsert)

    # ------------------------------------------------------------------
    # FloodWait handling
    # ------------------------------------------------------------------

    async def _handle_flood_wait(self, exc: Exception, account_id: int) -> None:
        """Handle FloodWaitError: wait seconds + 10, coordinate with rate_limiter."""
        seconds = getattr(exc, "seconds", 0)
        wait_seconds = seconds + 10
        logger.warning(
            f"AdminLogPoller: FloodWait for account_id={account_id}: "
            f"waiting {wait_seconds}s"
        )
        self.rate_limiter.set_flood_wait(account_id, seconds)
        await asyncio.sleep(wait_seconds)

    # ------------------------------------------------------------------
    # Client lookup
    # ------------------------------------------------------------------

    def _get_client(self, account_id: int) -> "TelegramClientManager | None":
        """Return the TelegramClientManager for account_id, or None."""
        for manager in self.clients:
            mgr_account_id = getattr(manager, "account_id", None)
            if mgr_account_id == account_id:
                return manager
        return None


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------

def _extract_member_role(action) -> str:
    """Extract a role string from a member-change action."""
    if action is None:
        return "member"
    action_name = type(action).__name__.lower()
    if "ban" in action_name or "kick" in action_name:
        return "banned"
    if "leave" in action_name:
        return "left"
    if "join" in action_name:
        return "member"
    # Try to read participant rank/role from action attributes
    new_participant = getattr(action, "new_participant", None)
    if new_participant is not None:
        if getattr(new_participant, "admin_rights", None) is not None:
            return "admin"
        if getattr(new_participant, "creator", False):
            return "creator"
    return "member"
