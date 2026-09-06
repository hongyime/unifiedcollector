"""RealtimeMixin — Telethon ``@client.on(...)`` handlers + realtime loop.

Extracted from :mod:`src.collectors.telegram` (PERF-004 sub-plan 4C step 5).
Mixed into :class:`TelegramCollector` via multiple inheritance; every
``self.*`` reference resolves at MRO-time to the composed class so cross-mixin
state still flows through the collector instance.

Ported from ``telegramcollector/services/collector/realtime_worker.py``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from src.collectors.telegram.helpers import (
    _format_exception,
    _is_transient_realtime_write_error,
    _telethon_payload,
    _tg_json,
    _tg_jsonb,
)
from src.core.discovered_links import persist_discovered_links

if TYPE_CHECKING:  # pragma: no cover — forward-ref hints only
    from src.collectors.telegram.session import TelegramWorker


logger = logging.getLogger("src.collectors.telegram")


class RealtimeMixin:
    """Realtime ingestion — Telethon @client.on(...) handlers."""

    async def _register_realtime_handlers(self):
        """Attach @client.on(NewMessage/Edited/Deleted/ChatAction/UserUpdate/Reactions)
        handlers to every connected worker. Idempotent (guarded) and cheap — telethon's
        per-client update loop fires these as soon as they're registered, independent of
        whether collect() is parked or busy backfilling. We therefore call this EARLY in
        collect() so LIVE messages stream in during the initial historical backfill
        (previously realtime only started AFTER backfill finished, so live capture stalled
        for hours behind a multi-thousand-chat backfill)."""
        from telethon import events

        if not self._workers:
            self._workers = await self._spawn_workers()
        if not self._workers:
            logger.error("_register_realtime_handlers: no Telegram workers connected")
            return
        # Warm up the self-bot filter's Layer 1 (sender_id) cache once, so the
        # very first message we see in the logs chat is filtered without a
        # per-event getMe lookup. Failure here is non-fatal — Layer 2 (chat_id)
        # still applies.
        try:
            await self._ensure_notify_bot_user_id()
        except Exception as exc:
            logger.debug("notify bot user_id warmup failed: %s", _format_exception(exc))
        registered = 0
        for worker in self._workers:
            if self._register_realtime_handlers_for_worker(worker, events):
                registered += 1
        if registered == 0:
            logger.debug("telegram realtime handlers already registered for all connected workers")
        self._handlers_registered = True

    def _register_realtime_handlers_for_worker(self, worker, events_module=None) -> bool:
        """Attach realtime handlers to one connected worker client, once per client."""
        if events_module is None:
            from telethon import events as events_module

        client = getattr(worker, "client", None)
        if client is None:
            logger.warning(
                "[worker=%d account=%s] realtime handler registration skipped: no client",
                worker.worker_id, worker.account.name,
            )
            return False

        client_key = (worker.worker_id, id(client))
        if client_key in self._realtime_handler_clients:
            return False

        client.add_event_handler(
            lambda e, w=worker: self._on_new_message(w, e),
            events_module.NewMessage(),
        )
        client.add_event_handler(
            lambda e, w=worker: self._on_message_edited(w, e),
            events_module.MessageEdited(),
        )
        client.add_event_handler(
            lambda e, w=worker: self._on_message_deleted(w, e),
            events_module.MessageDeleted(),
        )
        client.add_event_handler(
            lambda e, w=worker: self._on_chat_action(w, e),
            events_module.ChatAction(),
        )
        client.add_event_handler(
            lambda e, w=worker: self._on_user_update(w, e),
            events_module.UserUpdate(),
        )
        # Reactions — Telethon delivers these via Raw updates rather than a
        # dedicated event class. We listen for both message-level reaction
        # updates (humans on channels/groups) and bot-message reactions.
        try:
            from telethon.tl.types import (
                UpdateMessageReactions,
                UpdateBotMessageReactions,
            )
            client.add_event_handler(
                lambda e, w=worker: self._on_raw_reactions(w, e),
                events_module.Raw(types=[UpdateMessageReactions, UpdateBotMessageReactions]),
            )
        except Exception as exc:
            # Older Telethon may not expose UpdateBotMessageReactions; degrade.
            logger.warning(
                "Reaction event registration failed (older Telethon?): %s",
                exc,
            )

        self._realtime_handler_clients.add(client_key)
        logger.info(
            "[worker=%d account=%s] realtime handlers registered",
            worker.worker_id, worker.account.name,
        )
        return True

    async def collect_realtime(self):
        """Register Telethon event handlers on every connected worker and run forever.

        This is the @client.on(events.NewMessage) listener equivalent. New /
        edited / deleted messages and chat-action / user-update events are
        persisted to the unified telegram_* schema. Media is downloaded
        inline via download_message_media() rather than enqueued to Redis
        (the unified collector replaces the microservices' Redis queue).

        Runs until self._stop is set.
        """
        # Handlers may already be registered (we register them EARLY in collect()
        # so live messages stream during the initial backfill instead of waiting
        # for it to finish). Idempotent.
        await self._register_realtime_handlers()
        if not self._workers:
            logger.error("collect_realtime: no Telegram workers connected — bailing")
            return
        self._realtime_running = True
        logger.info(
            "Realtime listener running across %d worker(s); awaiting events…",
            len(self._workers),
        )
        # Independent resolve-only sweep so dead chats get reclassified even while
        # the drain gather below is busy deep-backfilling a large channel for hours.
        if not getattr(self, "_sweep_task", None):
            self._sweep_task = asyncio.create_task(self._resolve_sweep_loop())
        # Park until stop. Telethon delivers events under each client's own task.
        # While parked we ALSO keep historical backfill flowing: every
        # TELEGRAM_BACKFILL_DRAIN_INTERVAL seconds drain the spider/backfill queue
        # across ALL connected workers in parallel (SKIP LOCKED makes this safe).
        # This is the "all accounts backfill + scrape at once" behaviour — live
        # listening and historical catch-up run concurrently instead of backfill
        # only happening once per (re)launch.
        drain_interval = float(os.getenv("TELEGRAM_BACKFILL_DRAIN_INTERVAL", "60"))
        health_interval = float(os.getenv("TELEGRAM_HEALTH_INTERVAL", "60"))
        spider_on = os.getenv("TELEGRAM_SPIDER_ENABLED", "true").lower() == "true"
        last_drain = asyncio.get_event_loop().time()
        last_health = asyncio.get_event_loop().time()
        while self._realtime_running and not self._stop.is_set():
            await asyncio.sleep(1.0)
            now = asyncio.get_event_loop().time()
            # SELF-HEAL: reconnect any worker whose Telethon (MTProto) client dropped.
            # The container healthcheck only tests HTTP, and the worker watchdog exempts
            # realtime sources from restart — so a dead connection used to sit silently
            # (this happened: telegram dead ~26h "Cannot send requests while disconnected").
            # Reconnecting the same client preserves the registered event handlers.
            if now - last_health >= health_interval:
                last_health = now
                for w in self._workers:
                    try:
                        if not w.client.is_connected():
                            logger.warning("telegram: worker=%d account=%s DISCONNECTED — reconnecting",
                                           w.worker_id, w.account.name)
                            await w.client.connect()
                            logger.info("telegram: worker=%d reconnected", w.worker_id)
                    except Exception as exc:
                        logger.error("telegram: worker=%d reconnect failed: %s", w.worker_id, exc)
            if not spider_on:
                continue
            if now - last_drain < drain_interval:
                continue
            last_drain = now
            try:
                configured_spider_workers = [
                    w for w in self._workers
                    if self._is_spider_allowed(w)
                ]
                spider_workers = [
                    w for w in configured_spider_workers
                    if self._worker_can_resolve(w)
                ]
                if not spider_workers:
                    if configured_spider_workers:
                        logger.debug("realtime backfill drain skipped: allowed Telegram accounts are cooling down")
                    else:
                        logger.debug("realtime backfill drain skipped: no worker matches TELEGRAM_SPIDER_ACCOUNTS")
                    continue
                await asyncio.gather(
                    *(self._process_spider_queue(w) for w in spider_workers),
                    return_exceptions=True,
                )
            except Exception as exc:
                logger.debug("realtime backfill drain failed: %s", exc)
            # Bounded sweep: download profile photos for users that lack one
            # (user: "why doesn't tg collector scrape photo of users").
            try:
                await self._collect_user_photos_pass(
                    self._workers[0], batch=int(os.getenv("TELEGRAM_USER_PHOTO_BATCH", "15"))
                )
            except Exception as exc:
                logger.debug("user photo pass failed: %s", exc)

    async def _ensure_notify_bot_user_id(self) -> int | None:
        """Return the realtime-feed bot's user_id, resolving via /getMe if
        UC_NOTIFY_BOT_USER_ID is unset. Safe to call repeatedly (cached).

        On any failure we log once and return None; Layer 2 (chat_id skip)
        remains the primary defence when Layer 1 can't identify the bot.
        """
        if self._notify_bot_user_id is not None:
            return self._notify_bot_user_id
        if self._notify_bot_resolve_attempted:
            # Already tried and failed; don't hammer the API each call.
            return None
        token = os.getenv("NOTIFY_TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            self._notify_bot_resolve_attempted = True
            return None
        if self._notify_bot_user_id_lock is None:
            self._notify_bot_user_id_lock = asyncio.Lock()
        async with self._notify_bot_user_id_lock:
            if self._notify_bot_user_id is not None:
                return self._notify_bot_user_id
            if self._notify_bot_resolve_attempted:
                return None
            self._notify_bot_resolve_attempted = True
            try:
                import aiohttp

                url = f"https://api.telegram.org/bot{token}/getMe"
                timeout = aiohttp.ClientTimeout(total=10)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(url) as resp:
                        data = await resp.json()
                if not isinstance(data, dict) or not data.get("ok"):
                    logger.warning(
                        "telegram self-bot filter: getMe returned not-ok "
                        "(layer-1 sender-id skip disabled; layer-2 chat-id skip still active)"
                    )
                    return None
                uid = (data.get("result") or {}).get("id")
                if not uid:
                    logger.warning(
                        "telegram self-bot filter: getMe returned no id field"
                    )
                    return None
                self._notify_bot_user_id = int(uid)
                logger.info(
                    "telegram self-bot filter: resolved notify bot user_id=%d via getMe",
                    self._notify_bot_user_id,
                )
                return self._notify_bot_user_id
            except Exception as exc:
                logger.warning(
                    "telegram self-bot filter: getMe resolution failed (%s); "
                    "layer-1 sender-id skip disabled — layer-2 chat-id skip still active",
                    _format_exception(exc),
                )
                return None

    def _should_skip_self_bot_message(self, chat_id, message) -> str | None:
        """Return a reason string if this realtime message should be skipped
        because it originated from our own realtime-feed bot, else None.

        Checks, in order:
          1. sender_id matches the resolved notify bot user_id (Layer 1).
          2. sender_id or via_bot_id is in COLLECTOR_TELEGRAM_IGNORE_SENDER_IDS
             (defence-in-depth: catches messages our notify bot authored
             directly AND messages routed via_bot through it).
          3. chat_id equals TELEGRAM_LOGS_CHAT_ID and the bot uid is unresolved
             (Layer 2 fallback).
        """
        # Hub-group loop guard: our own collector accounts populate this group,
        # so its messages must never be ingested, regardless of sender.
        if self._hub_group_ids and chat_id in self._hub_group_ids:
            return "hub_group"

        sender_id = getattr(message, "sender_id", None)
        via_bot_id = getattr(message, "via_bot_id", None)

        # Layer 1: sender_id match (bot_uid resolved from token)
        bot_uid = self._notify_bot_user_id
        if bot_uid is not None and sender_id is not None and int(sender_id) == int(bot_uid):
            return "notify_bot"

        # Layer 1b (SHIP #2): configurable ignore set — covers sender_id AND
        # via_bot_id so inline-bot echoes cannot loop either.
        ignore = self._ignore_sender_ids
        if ignore:
            try:
                if sender_id is not None and int(sender_id) in ignore:
                    return "ignore_sender_id"
            except (TypeError, ValueError):
                pass
            try:
                if via_bot_id is not None and int(via_bot_id) in ignore:
                    return "ignore_via_bot_id"
            except (TypeError, ValueError):
                pass

        # Layer 2: fallback if we couldn't resolve the bot's UID (e.g. network failure on boot).
        # To avoid a catastrophic loop, we must skip the bot's messages in the logs chat.
        # But if we know the bot's UID and it didn't match above, it's a human message!
        # So we only fallback to skipping the logs chat if bot_uid is None.
        if self._logs_chat_id is not None and chat_id == self._logs_chat_id:
            if bot_uid is None:
                return "logs_chat_unresolved_bot"

        return None

    async def _write_realtime_message_with_retry(
        self,
        worker: "TelegramWorker",
        message,
        chat_id: int,
        is_edit: bool = False,
    ):
        attempts = max(1, int(getattr(self, "_realtime_write_attempts", 3) or 3))
        delay = max(0.0, float(getattr(self, "_realtime_write_retry_delay", 0.75) or 0.0))
        for attempt in range(1, attempts + 1):
            try:
                await self._write_realtime_message(worker, message, chat_id, is_edit=is_edit)
                if attempt > 1:
                    logger.info(
                        "telegram realtime write recovered after %d attempt(s): chat=%s msg=%s edit=%s",
                        attempt, chat_id, getattr(message, "id", None), is_edit,
                    )
                return
            except Exception as exc:
                if attempt >= attempts or not _is_transient_realtime_write_error(exc):
                    raise
                logger.warning(
                    "telegram realtime write transient failure; retrying %d/%d: chat=%s msg=%s edit=%s error=%s",
                    attempt + 1, attempts, chat_id, getattr(message, "id", None), is_edit,
                    _format_exception(exc),
                )
                if delay:
                    await asyncio.sleep(delay * attempt)

    async def _on_new_message(self, worker: "TelegramWorker", event):
        try:
            chat_id = event.chat_id
            message = event.message
            # Circular-loop guard: skip messages authored by our own realtime-feed
            # bot in the logs chat. Without this, the bot's per-post notifications
            # would be re-ingested as new telegram media_items, which the feed
            # would then re-send, ad infinitum.
            skip_reason = self._should_skip_self_bot_message(chat_id, message)
            if skip_reason is not None:
                logger.debug(
                    "telegram skip: self_bot reason=%s chat=%s msg=%s sender=%s",
                    skip_reason, chat_id, getattr(message, "id", None),
                    getattr(message, "sender_id", None),
                )
                return
            await self._write_realtime_message_with_retry(worker, message, chat_id)
            # Sender resolution hits the network and can raise ChannelPrivateError
            # for private/restricted channels (or if we were removed). Isolate it so
            # it neither aborts persistence nor skips the media download below, and
            # downgrade the noise to debug.
            try:
                sender = await event.get_sender()
                if sender is not None:
                    await self._upsert_user_full(sender)
            except Exception as exc:
                logger.debug("get_sender failed (private/restricted channel?): %s", _format_exception(exc))
            if getattr(message, "media", None) is not None:
                # Download inline rather than queueing.
                try:
                    await self.download_message_media(message, worker=worker, chat_id=chat_id)
                except Exception as exc:
                    logger.debug("realtime media download failed: %s", _format_exception(exc))
        except Exception as exc:
            logger.error("_on_new_message error: %s", _format_exception(exc), exc_info=True)

    async def _on_message_edited(self, worker: "TelegramWorker", event):
        try:
            chat_id = event.chat_id
            message = event.message
            # Circular-loop guard: same rationale as _on_new_message. An edited
            # self-bot message must not be ingested either.
            skip_reason = self._should_skip_self_bot_message(chat_id, message)
            if skip_reason is not None:
                logger.debug(
                    "telegram skip: self_bot reason=%s chat=%s msg=%s sender=%s edit=1",
                    skip_reason, chat_id, getattr(message, "id", None),
                    getattr(message, "sender_id", None),
                )
                return
            await self._write_realtime_message_with_retry(worker, message, chat_id, is_edit=True)
        except Exception as exc:
            logger.error("_on_message_edited error: %s", _format_exception(exc), exc_info=True)

    async def _on_message_deleted(self, worker: "TelegramWorker", event):
        try:
            from telethon.utils import resolve_id
            # event.chat_id is the marked id (-100… for channels); the stored
            # platform_message_id uses the bare id, so normalize first. Telegram can
            # deliver delete events without chat context. Message IDs are only unique
            # inside a chat, so a chatless delete cannot be matched safely.
            raw_chat = event.chat_id
            chat_id = None
            if raw_chat is not None:
                try:
                    chat_id, _ = resolve_id(raw_chat)
                except Exception:
                    chat_id = raw_chat
            if chat_id is None:
                logger.debug(
                    "telegram delete event without chat context skipped: message_ids=%s",
                    event.deleted_ids or [],
                )
                return
            # capture WHEN we observed the deletion (telegram doesn't tell us the
            # exact delete time; observation time is the best available).
            deleted_at_iso = datetime.now(tz=timezone.utc).isoformat()
            patch = json.dumps({"deleted": True, "deleted_at": deleted_at_iso})
            deleted_ids = list(event.deleted_ids or [])
            attempts = max(1, int(getattr(self, "_realtime_write_attempts", 3) or 3))
            delay = max(0.0, float(getattr(self, "_realtime_write_retry_delay", 0.75) or 0.0))
            for attempt in range(1, attempts + 1):
                try:
                    async with self.pool.acquire() as conn:
                        for msg_id in deleted_ids:
                            # Merge a {deleted, deleted_at} patch into metadata; no-op if the
                            # row doesn't exist (deletion of a message we never saw).
                            await conn.execute("""
                                UPDATE telegram_messages
                                SET metadata = COALESCE(metadata,'{}'::jsonb) || $2::jsonb
                                WHERE platform_message_id = $1
                            """, f"{chat_id}:{msg_id}", patch)
                    return
                except Exception as exc:
                    if attempt >= attempts or not _is_transient_realtime_write_error(exc):
                        raise
                    logger.warning(
                        "telegram delete event transient DB failure; retrying %d/%d: chat=%s deleted=%d error=%s",
                        attempt + 1, attempts, chat_id, len(deleted_ids), _format_exception(exc),
                    )
                    if delay:
                        await asyncio.sleep(delay * attempt)
        except Exception as exc:
            logger.error("_on_message_deleted error: %s", _format_exception(exc), exc_info=True)

    async def _on_chat_action(self, worker: "TelegramWorker", event):
        """Translate Telethon chat actions into telegram_chat_members upserts."""
        try:
            from telethon.utils import resolve_id
            chat_id, _ = resolve_id(event.chat_id)  # marked (-100…) -> bare id
            role = "member"
            if getattr(event, "user_kicked", False):
                role = "banned"
            elif getattr(event, "user_left", False):
                role = "left"
            user_ids: list[int] = []
            user_ids.extend(self._coerce_telegram_user_ids(getattr(event, "user_id", None)))
            user_ids.extend(self._coerce_telegram_user_ids(getattr(event, "user_ids", None)))
            user_ids.extend(self._coerce_telegram_user_ids(getattr(event, "user", None)))
            user_ids.extend(self._coerce_telegram_user_ids(getattr(event, "users", None)))
            user_ids = list(dict.fromkeys(user_ids))
            if not user_ids:
                return
            async with self.pool.acquire() as conn:
                # telegram_chat_members.chat_id/user_id are uuids (FK) — resolve
                # the bare platform ids to internal uuids; skip if not collected yet.
                chat_uuid = await conn.fetchval(
                    "SELECT id FROM telegram_chats WHERE platform_chat_id = $1",
                    str(chat_id))
                if chat_uuid is None:
                    return
                observed_at = datetime.now(timezone.utc)
                for user_id in user_ids:
                    user_uuid = await self._ensure_telegram_user_stub(conn, user_id)
                    if user_uuid is None:
                        continue
                    await self._upsert_chat_member_observation(
                        conn,
                        chat_uuid,
                        user_uuid,
                        role=role,
                        joined_at=observed_at if role == "member" else None,
                        last_seen_at=observed_at,
                    )
        except Exception as exc:
            logger.error("_on_chat_action error: %s", _format_exception(exc), exc_info=True)

    async def _on_user_update(self, worker: "TelegramWorker", event):
        try:
            user = await event.get_user()
            if user is not None:
                await self._upsert_user_full(user)
        except Exception as exc:
            logger.error("_on_user_update error: %s", _format_exception(exc), exc_info=True)

    async def _on_raw_reactions(self, worker: "TelegramWorker", update):
        """Handle UpdateMessageReactions / UpdateBotMessageReactions raw events.

        Phase 1 scope: write/update the per-message reaction *counts* into
        ``telegram_reaction_counts`` so dashboards can display engagement.
        Per-user reactor enumeration (Phase 2.2) calls
        ``GetMessageReactionsListRequest`` separately and writes
        ``telegram_reactions`` rows.

        Telethon raw payload shape:
          UpdateMessageReactions(peer, msg_id, top_msg_id, reactions)
          .reactions.results: list[ReactionCount]
              .reaction: ReactionEmoji(emoticon=str) | ReactionCustomEmoji(...)
              .count: int
        """
        try:
            from telethon.tl.types import (
                ReactionEmoji,
                ReactionCustomEmoji,
            )

            peer = getattr(update, "peer", None)
            msg_id = getattr(update, "msg_id", None)
            reactions = getattr(update, "reactions", None)
            if peer is None or msg_id is None or reactions is None:
                return

            # Resolve peer → platform_chat_id string.
            chat_pid: str | None = None
            for attr in ("channel_id", "chat_id", "user_id"):
                v = getattr(peer, attr, None)
                if v is not None:
                    chat_pid = str(v)
                    break
            if chat_pid is None:
                return

            # Build emoji -> count dict from the results list.
            counts: dict[str, int] = {}
            total = 0
            results = getattr(reactions, "results", None) or []
            for rc in results:
                emoji_obj = getattr(rc, "reaction", None)
                count = int(getattr(rc, "count", 0) or 0)
                if count <= 0:
                    continue
                if isinstance(emoji_obj, ReactionEmoji):
                    key = emoji_obj.emoticon
                elif isinstance(emoji_obj, ReactionCustomEmoji):
                    key = f"custom:{emoji_obj.document_id}"
                else:
                    key = str(emoji_obj)
                counts[key] = count
                total += count

            if not counts:
                return

            # Resolve message UUID via (chat_uuid, platform_message_id) lookup.
            # platform_message_id is namespaced as "{chat_pid}:{msg_id}" by _upsert_message.
            platform_message_id = f"{chat_pid}:{msg_id}"
            async with self.pool.acquire() as conn:
                msg_row = await conn.fetchrow(
                    "SELECT id FROM telegram_messages WHERE platform_message_id = $1",
                    platform_message_id,
                )
                if msg_row is None:
                    # Reaction arrived before we ingested the message — skip;
                    # the next backfill will refresh counts via this handler.
                    return
                msg_uuid = msg_row["id"]

                await conn.execute(
                    """
                    INSERT INTO telegram_reaction_counts
                        (message_id, counts, total_reactions, refreshed_at)
                    VALUES ($1, $2::jsonb, $3, NOW())
                    ON CONFLICT (message_id) DO UPDATE SET
                        counts = EXCLUDED.counts,
                        total_reactions = EXCLUDED.total_reactions,
                        refreshed_at = NOW()
                    """,
                    msg_uuid,
                    _tg_jsonb(counts),
                    total,
                )
        except Exception as exc:
            logger.debug("_on_raw_reactions failed: %s", exc)

    async def _write_realtime_message(
        self,
        worker: "TelegramWorker",
        message,
        chat_id: int,
        is_edit: bool = False,
    ):
        """INSERT (or UPDATE-on-edit) the message into telegram_messages."""
        # Resolve UUIDs via the existing chat upsert chain. We don't have the
        # entity here so just key off platform_chat_id.
        async with self.pool.acquire() as conn:
            chat_row = await conn.fetchrow(
                "SELECT id FROM telegram_chats WHERE platform_chat_id = $1",
                str(chat_id),
            )
            if chat_row is None:
                # The chat_id passed here doesn't always match telegram_chats'
                # platform_chat_id (raw peer id) format — some backfill/discussion
                # paths pass a different form, so the lookup missed and the message
                # landed with a NULL chat_id, orphaning it from the dashboard
                # (was ~8% / 101k rows). Resolve via the message's OWN peer (raw
                # id) and create a minimal chat row if it's genuinely new, so a
                # message is never orphaned. Existing rows are healed by
                # tmp/backfill_telegram_chat_id.py.
                peer = getattr(message, "peer_id", None)
                raw_pid = None
                if peer is not None:
                    raw_pid = (getattr(peer, "channel_id", None)
                               or getattr(peer, "chat_id", None)
                               or getattr(peer, "user_id", None))
                if raw_pid is not None:
                    ptype = type(peer).__name__
                    ctype = ("channel" if ptype == "PeerChannel"
                             else "group" if ptype == "PeerChat"
                             else "user" if ptype == "PeerUser" else None)
                    chat_row = await conn.fetchrow(
                        """
                        INSERT INTO telegram_chats (platform_chat_id, type)
                        VALUES ($1, $2)
                        ON CONFLICT (platform_chat_id) DO UPDATE
                            SET platform_chat_id = EXCLUDED.platform_chat_id
                        RETURNING id
                        """,
                        str(raw_pid), ctype,
                    )
            chat_uuid = chat_row["id"] if chat_row else None

            sender_uuid = None
            sender_id = getattr(message, "sender_id", None)
            if sender_id is not None:
                user_row = await conn.fetchrow(
                    "SELECT id FROM telegram_users WHERE platform_user_id = $1",
                    str(sender_id),
                )
                sender_uuid = user_row["id"] if user_row else None

            media_type = self._detect_message_type(message)
            platform_msg_id = f"{chat_id}:{message.id}"
            payload = _telethon_payload(message)
            payload_json = json.dumps(payload, default=_tg_json)
            reply_to, fwd_chat, fwd_msg, via_bot = self._msg_refs(message)
            # Tier 6: Telethon exposes message.pinned (bool) on Message objects;
            # backfill re-fetches also refresh it via the edit/upsert branches.
            is_pinned = bool(getattr(message, "pinned", False) or False)

            wrote_row = False
            if is_edit:
                # Update existing if present; else insert.
                await conn.execute("""
                    INSERT INTO telegram_messages (
                        platform_message_id, chat_id, sender_id, text, caption,
                        media_type, platform_created_at, metadata,
                        reply_to_message_id, forward_from_chat_id, forward_from_message_id, via_bot_id,
                        is_pinned
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                    ON CONFLICT (platform_message_id) DO UPDATE SET
                        text = EXCLUDED.text,
                        caption = EXCLUDED.caption,
                        metadata = EXCLUDED.metadata,
                        is_pinned = EXCLUDED.is_pinned
                """,
                platform_msg_id, chat_uuid, sender_uuid,
                getattr(message, "message", None),
                getattr(message, "caption", None),
                media_type, message.date, payload_json,
                reply_to, fwd_chat, fwd_msg, via_bot,
                is_pinned,
                )
                wrote_row = True
            else:
                row = await conn.fetchrow("""
                    INSERT INTO telegram_messages (
                        platform_message_id, chat_id, sender_id, text, caption,
                        media_type, platform_created_at, metadata,
                        reply_to_message_id, forward_from_chat_id, forward_from_message_id, via_bot_id,
                        is_pinned
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                    ON CONFLICT (platform_message_id) DO NOTHING
                    RETURNING id
                """,
                platform_msg_id, chat_uuid, sender_uuid,
                getattr(message, "message", None),
                getattr(message, "caption", None),
                media_type, message.date, payload_json,
                reply_to, fwd_chat, fwd_msg, via_bot,
                is_pinned,
                )
                wrote_row = row is not None

            message_uuid = await conn.fetchval(
                "SELECT id FROM telegram_messages WHERE platform_message_id = $1",
                platform_msg_id,
            )
            if message_uuid is not None:
                await self._capture_message_reaction_counts(conn, message_uuid, message)
                await self._capture_poll(conn, message_uuid, message)
                await self._enumerate_poll_votes_and_enqueue(
                    worker, message, str(chat_id), message_uuid, conn=conn, chat_uuid=chat_uuid
                )
            # Tier 6: venue/event extraction (best effort — never breaks the
            # hot realtime path; helper swallows all exceptions internally).
            await self._extract_message_event(message, chat_uuid, platform_msg_id, conn=conn)
            await self._record_message_membership_signals(
                conn, message, chat_uuid, sender_uuid
            )
            await self._record_message_mentions(
                conn,
                message,
                message_uuid,
                chat_platform_id=str(chat_id),
                sender_platform_id=getattr(message, "sender_id", None),
            )
            await persist_discovered_links(
                conn,
                source="telegram",
                source_table="telegram_messages",
                source_record_id=platform_msg_id,
                context_id=str(chat_id),
                entity_id=str(sender_id or ""),
                text=" ".join(
                    v for v in (
                        getattr(message, "message", None),
                        getattr(message, "caption", None),
                    )
                    if v
                ),
                metadata={
                    "platform_message_id": platform_msg_id,
                    "platform_chat_id": str(chat_id),
                    "platform_sender_id": str(sender_id or ""),
                    "ingest_path": self.INGEST_PATH,
                    "raw_payload_kind": "message_edit" if is_edit else "message",
                },
            )
        if wrote_row:
            self._progress_count += 1
        self._archive_raw_payload(
            artifact_id=f"messages/{platform_msg_id}",
            payload=payload,
            target_tables=["telegram_messages"],
            metadata={
                "platform_message_id": platform_msg_id,
                "platform_chat_id": str(chat_id),
                "platform_sender_id": str(getattr(message, "sender_id", "") or ""),
                "ingest_path": self.INGEST_PATH,
                "raw_payload_kind": "message_edit" if is_edit else "message",
            },
        )

    async def _extract_message_event(self, message, chat_uuid, platform_msg_id, conn=None):
        """Tier 6 (best effort): extract venue/event info → telegram_events.

        Handles:
          - MessageMediaVenue → event_type 'venue' (title/address/venue_type).
            Lat/lng are deliberately NOT captured here — geo extraction lives
            in telegram_message_locations (Tier 5, separate agent/path).
          - MessageActionPinMessage service messages → event_type 'pin'.

        Isolated try/except: must NEVER break the message write path. Reuses
        the caller's connection when given, else acquires one from the pool.
        """
        try:
            event_type = title = address = venue_type = None
            starts_at = None

            media = getattr(message, "media", None)
            if media is not None and type(media).__name__ == "MessageMediaVenue":
                event_type = "venue"
                title = getattr(media, "title", None)
                address = getattr(media, "address", None)
                venue_type = getattr(media, "venue_type", None)
            else:
                action = getattr(message, "action", None)
                if action is not None and type(action).__name__ == "MessageActionPinMessage":
                    event_type = "pin"
                    starts_at = getattr(message, "date", None)

            if event_type is None:
                return

            sql = """
                INSERT INTO telegram_events (
                    platform_message_id, chat_id, event_type,
                    title, address, venue_type, starts_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (platform_message_id) DO NOTHING
            """
            args = (platform_msg_id, chat_uuid, event_type,
                    title, address, venue_type, starts_at)
            if conn is not None:
                await conn.execute(sql, *args)
            else:
                async with self.pool.acquire() as _conn:
                    await _conn.execute(sql, *args)
        except Exception as exc:
            logger.debug("_extract_message_event failed for %s: %s", platform_msg_id, exc)
