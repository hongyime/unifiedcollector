"""
Resolver — calls the Telegram API to fetch metadata for a discovered link.

Rate-limited via a sliding-window algorithm.
Accounts are selected round-robin from collector.telegram_accounts WHERE status='active'.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ResolvedMetadata:
    chat_title: str | None    # resolved display name of the group/channel
    member_count: int | None  # participant count (None if unavailable)
    link_type: str            # 'group', 'channel', 'bot', 'user', or 'unknown'
    is_bot: bool              # True if entity type is bot


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------

class Resolver:
    def __init__(
        self,
        db_pool,
        tg_api_id: int,
        tg_api_hash: str,
        rate_limit_per_minute: int,
    ) -> None:
        """
        db_pool:               asyncpg pool — used to read collector.telegram_accounts.
        tg_api_id/hash:        Telegram API credentials from settings.
        rate_limit_per_minute: max API calls per minute (LINK_DISCOVERY_RESOLVE_RATE_LIMIT).
        """
        self._pool = db_pool
        self._tg_api_id = tg_api_id
        self._tg_api_hash = tg_api_hash
        self._rate_limit_per_minute = rate_limit_per_minute
        self._accounts: list[dict] = []
        self._account_index: int = 0
        self._call_timestamps: deque = deque()

    async def _refresh_accounts(self) -> None:
        """Load active accounts from DB into self._accounts."""
        rows = await self._pool.fetch(
            "SELECT id, phone_number FROM collector.telegram_accounts "
            "WHERE status = 'active' ORDER BY id ASC;"
        )
        self._accounts = [dict(r) for r in rows]
        if not self._accounts:
            raise RuntimeError("No active Telegram accounts available for metadata resolution.")

    async def _pick_account(self) -> dict:
        """
        Returns the next active account from the pool using round-robin selection.
        Refreshes the account list from DB if the pool is empty.
        Raises RuntimeError if no active accounts are available.
        """
        if not self._accounts:
            await self._refresh_accounts()
        account = self._accounts[self._account_index % len(self._accounts)]
        self._account_index += 1
        return account

    async def _enforce_rate_limit(self) -> None:
        """
        Enforces the sliding-window rate limit of rate_limit_per_minute calls/minute.

        Algorithm:
          1. now = time.monotonic()
          2. Purge entries from self._call_timestamps older than 60 seconds.
          3. If len(self._call_timestamps) >= rate_limit_per_minute:
               oldest = self._call_timestamps[0]
               wait_seconds = 60.0 - (now - oldest)
               if wait_seconds > 0: await asyncio.sleep(wait_seconds)
          4. Append now to self._call_timestamps.
        """
        now = time.monotonic()
        # Purge entries older than 60 seconds
        while self._call_timestamps and (now - self._call_timestamps[0]) > 60.0:
            self._call_timestamps.popleft()
        # If at limit, sleep until oldest entry expires
        if len(self._call_timestamps) >= self._rate_limit_per_minute:
            oldest = self._call_timestamps[0]
            wait_seconds = 60.0 - (now - oldest)
            if wait_seconds > 0:
                await asyncio.sleep(wait_seconds)
        self._call_timestamps.append(time.monotonic())

    async def resolve(self, link: str) -> ResolvedMetadata | None:
        """
        Resolves metadata for a single link by calling the Telegram API.

        Returns ResolvedMetadata on success, None on any exception.
        """
        try:
            await self._enforce_rate_limit()
            account = await self._pick_account()

            from telethon import TelegramClient

            session_name = f"resolver_{account['id']}"
            async with TelegramClient(session_name, self._tg_api_id, self._tg_api_hash) as client:
                entity = await client.get_entity(link)

                # Determine link_type and is_bot
                try:
                    from telethon import types as _tl_types
                    _Channel = _tl_types.Channel
                    _User = _tl_types.User
                    _is_real_types = isinstance(_Channel, type)
                except Exception:
                    _is_real_types = False

                if _is_real_types and isinstance(entity, _Channel):
                    if entity.megagroup:
                        link_type = 'group'
                    else:
                        link_type = 'channel'
                    is_bot = False
                    chat_title = getattr(entity, 'title', None)
                    try:
                        participants = await client.get_participants(entity, limit=0)
                        member_count = participants.total
                    except Exception:
                        member_count = None
                elif _is_real_types and isinstance(entity, _User):
                    if entity.bot:
                        link_type = 'bot'
                        is_bot = True
                    else:
                        link_type = 'user'
                        is_bot = False
                    chat_title = entity.first_name
                    member_count = None
                elif hasattr(entity, 'megagroup'):
                    # Fallback for mocked/non-typed entities (tests)
                    if entity.megagroup:
                        link_type = 'group'
                    else:
                        link_type = 'channel'
                    is_bot = False
                    chat_title = getattr(entity, 'title', None)
                    try:
                        participants = await client.get_participants(entity, limit=0)
                        member_count = participants.total
                    except Exception:
                        member_count = None
                elif hasattr(entity, 'bot'):
                    if entity.bot:
                        link_type = 'bot'
                        is_bot = True
                    else:
                        link_type = 'user'
                        is_bot = False
                    chat_title = getattr(entity, 'first_name', None)
                    member_count = None
                else:
                    link_type = 'unknown'
                    is_bot = False
                    chat_title = getattr(entity, 'title', getattr(entity, 'first_name', None))
                    member_count = None

                return ResolvedMetadata(
                    chat_title=chat_title,
                    member_count=member_count,
                    link_type=link_type,
                    is_bot=is_bot,
                )

        except Exception as e:
            try:
                from telethon.errors import FloodWaitError, UsernameNotOccupiedError, ChannelPrivateError
                _flood_cls = FloodWaitError if isinstance(FloodWaitError, type) else None
                _username_cls = UsernameNotOccupiedError if isinstance(UsernameNotOccupiedError, type) else None
                _private_cls = ChannelPrivateError if isinstance(ChannelPrivateError, type) else None
            except Exception:
                _flood_cls = _username_cls = _private_cls = None

            if _flood_cls and isinstance(e, _flood_cls):
                logger.warning(f"FloodWaitError resolving {link}: wait {e.seconds}s")
            elif _username_cls and isinstance(e, _username_cls):
                logger.debug(f"UsernameNotOccupiedError resolving {link}: not found")
            elif _private_cls and isinstance(e, _private_cls):
                logger.debug(f"ChannelPrivateError resolving {link}: private channel")
            else:
                logger.warning(f"Error resolving {link}: {e}", exc_info=True)
            return None
