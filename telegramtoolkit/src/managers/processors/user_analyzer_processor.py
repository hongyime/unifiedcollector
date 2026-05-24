#!/usr/bin/env python3
"""
User Analyzer Processor for Unified Message Orchestrator
Extracts users from multiple message and group sources and stores them in SQLite.
"""
import re
from contextvars import ContextVar
from typing import Any, Dict, Iterable, Optional, Set, Tuple

# Task-local account name — each asyncio task gets its own copy so concurrent
# account tasks cannot clobber each other's value during awaits.
_scan_account_name: ContextVar[str] = ContextVar('scan_account_name', default='unknown')

from telethon import errors

from src.core.account_health import AccountFailureError, is_account_error
from src.core.base_feature import BaseFeature
from src.core.dynamic_config import get_config_value
from src.core.feature_processor import FeatureProcessor
from src.core.progress_logger import log_info, log_success, log_warning
from src.core.scan_targets import discover_scan_targets
from src.core.state_manager import get_state_manager


class UserAnalyzerProcessor(FeatureProcessor, BaseFeature):
    """
    Processor that analyzes users from multiple sources.
    It prefers to continue on non-fatal extraction errors so one bad message,
    participant lookup, or entity resolution does not abort the full scan.
    """

    name = "user_analyzer"
    feature_key = "users"

    def __init__(self):
        BaseFeature.__init__(self, name=self.name)
        self.state = get_state_manager()
        self.collect_participants = bool(get_config_value("USER_ANALYZER_COLLECT_PARTICIPANTS", True))
        self.collect_linked_chat_participants = bool(
            get_config_value("USER_ANALYZER_COLLECT_LINKED_CHAT_PARTICIPANTS", True)
        )
        self.collect_reply_senders = bool(get_config_value("USER_ANALYZER_COLLECT_REPLY_SENDERS", True))
        self.collect_mentioned_users = bool(get_config_value("USER_ANALYZER_COLLECT_MENTIONED_USERS", True))
        self.collect_via_bots = bool(get_config_value("USER_ANALYZER_COLLECT_VIA_BOTS", True))
        self.collect_forwarded_users = bool(get_config_value("USER_ANALYZER_COLLECT_FORWARDED_USERS", True))
        self.collect_action_users = bool(get_config_value("USER_ANALYZER_COLLECT_ACTION_USERS", True))
        self.collect_linked_chat_messages = bool(get_config_value("USER_ANALYZER_COLLECT_LINKED_CHAT_MESSAGES", True))
        self.collect_admin_log = bool(get_config_value("USER_ANALYZER_COLLECT_ADMIN_LOG", True))
        self.linked_chat_message_limit = max(0, int(get_config_value("USER_ANALYZER_LINKED_CHAT_MESSAGE_LIMIT", 250) or 0))
        self.admin_log_limit = max(0, int(get_config_value("USER_ANALYZER_ADMIN_LOG_LIMIT", 200) or 0))
        self.stats = {
            'users_found': 0,
            'memberships_added': 0,
            'failed_lookups': 0,
            'participant_users_found': 0,
            'linked_chat_messages_processed': 0,
            'admin_events_processed': 0,
            'non_fatal_errors': 0,
        }
        self._participant_scans_attempted: Set[Tuple[str, str]] = set()
        self._linked_history_scans_attempted: Set[Tuple[str, str]] = set()
        self._admin_log_scans_attempted: Set[Tuple[str, str]] = set()
        self._entity_cache: Dict[str, Any] = {}
        self._clients_map: Dict[str, Any] = {}
        self._account_health: Any = None

    async def initialize(self) -> None:
        """Initialize the processor"""
        log_info(f"🔍 [{self.name}] Initializing user analyzer...")

    async def shutdown(self) -> None:
        """Clean shutdown"""
        log_info(
            f"💾 [{self.name}] Shutting down... Users found: {self.stats['users_found']}, "
            f"non-fatal errors: {self.stats['non_fatal_errors']}"
        )

    async def discover_scan_targets(
        self,
        client,
        account: Dict[str, Any],
        group_ids: Optional[list[str]] = None,
    ) -> Optional[list[Dict[str, Any]]]:
        """Prefer linked discussion groups for broadcast channels."""
        return await discover_scan_targets(
            client,
            group_ids=group_ids,
            include_private_chats=False,
            prefer_linked_discussions=True,
        )

    async def on_scan_start(self, context: Dict[str, Any]) -> None:
        """Called when scanning starts for a group"""
        group_name = context['group_name']
        _scan_account_name.set(context.get('account_name', self.name))
        self._clients_map = context.get('clients_map') or {}
        self._account_health = context.get('account_health')
        log_info(f"👥 [{self.name}] Starting user analysis for: {group_name}")

        if self.collect_participants:
            await self._collect_participants_for_context(context)

        if self.collect_linked_chat_participants:
            await self._collect_linked_chat_participants_for_context(context)

        if self.collect_linked_chat_messages and self.linked_chat_message_limit > 0:
            await self._collect_linked_chat_history_for_context(context)

        if self.collect_admin_log and self.admin_log_limit > 0:
            await self._collect_admin_log_for_context(context)

    async def on_scan_complete(self, context: Dict[str, Any]) -> None:
        """Called when scanning completes for a group"""
        group_name = context['group_name']
        log_success(
            f"✅ [{self.name}] {group_name}: Found {self.stats['users_found']} users "
            f"({self.stats['participant_users_found']} via participants)"
        )

    async def process_message(self, event: Dict[str, Any]) -> None:
        """Process a message event to extract user information from many sources."""
        message = event['message']
        group_id = event['group_id']
        group_name = event['group_name']
        client = event['client']
        _scan_account_name.set(event.get('account_name', self.name))
        self._clients_map = event.get('clients_map') or self._clients_map
        self._account_health = event.get('account_health') or self._account_health

        try:
            await self._collect_users_from_message_sources(client, message, group_id, group_name)
        except AccountFailureError:
            raise
        except Exception as e:
            self._record_non_fatal_error(f"processing message {getattr(message, 'id', 'unknown')}", e)
        finally:
            self.state.save_feature_progress(
                event['account_name'],
                group_id,
                'users',
                getattr(message, 'id', 0) or 0,
                self.stats['users_found']
            )

    async def _collect_participants_for_context(self, context: Dict[str, Any]) -> None:
        """Collect currently visible participants for the current chat."""
        group_id = context['group_id']
        group_name = context['group_name']
        account_name = context['account_name']
        scan_key = (account_name, group_id)
        if scan_key in self._participant_scans_attempted:
            return
        self._participant_scans_attempted.add(scan_key)

        await self._collect_participants_for_entity(
            context['client'],
            context['entity'],
            group_id,
            group_name,
            source="participants",
        )

    async def _collect_linked_chat_participants_for_context(self, context: Dict[str, Any]) -> None:
        """Collect participants from a linked discussion chat when it is directly accessible."""
        linked_details = await self._get_linked_chat_details(context)
        if linked_details is None:
            return
        linked_entity, linked_group_id, linked_group_name = linked_details
        client = context['client']
        scan_key = (context['account_name'], linked_group_id)
        if scan_key in self._participant_scans_attempted:
            return
        self._participant_scans_attempted.add(scan_key)

        await self._collect_participants_for_entity(
            client,
            linked_entity,
            linked_group_id,
            linked_group_name,
            source="linked_chat_participants",
        )

    async def _collect_linked_chat_history_for_context(self, context: Dict[str, Any]) -> None:
        """Best-effort scan of recent linked discussion messages when accessible."""
        linked_details = await self._get_linked_chat_details(context)
        if linked_details is None:
            return
        linked_entity, linked_group_id, linked_group_name = linked_details
        scan_key = (context['account_name'], linked_group_id)
        if scan_key in self._linked_history_scans_attempted:
            return
        self._linked_history_scans_attempted.add(scan_key)

        client = context['client']
        try:
            processed = 0
            async for message in client.iter_messages(linked_entity, limit=self.linked_chat_message_limit):
                await self._collect_users_from_message_sources(
                    client,
                    message,
                    linked_group_id,
                    linked_group_name,
                )
                processed += 1
            self.stats['linked_chat_messages_processed'] += processed
        except Exception as e:
            self._record_non_fatal_error(f"linked chat history for {linked_group_name}", e)

    async def _collect_admin_log_for_context(self, context: Dict[str, Any]) -> None:
        """Best-effort admin log scan. Permission failures are non-fatal."""
        account_name = context['account_name']
        group_id = context['group_id']
        scan_key = (account_name, group_id)
        if scan_key in self._admin_log_scans_attempted:
            return
        self._admin_log_scans_attempted.add(scan_key)

        client = context['client']
        entity = context['entity']
        group_name = context['group_name']

        try:
            processed = 0
            async for admin_event in client.iter_admin_log(
                entity,
                join=True,
                leave=True,
                invite=True,
                restrict=True,
                unrestrict=True,
                ban=True,
                unban=True,
                promote=True,
                demote=True,
                pinned=True,
                edit=True,
                delete=True,
            ):
                await self._collect_admin_event_users(
                    client,
                    admin_event,
                    group_id,
                    group_name,
                )
                processed += 1
                if processed >= self.admin_log_limit:
                    break
            self.stats['admin_events_processed'] += processed
        except Exception as e:
            self._record_non_fatal_error(f"admin log for {group_name}", e)

    async def _get_linked_chat_details(
        self,
        context: Dict[str, Any],
    ) -> Optional[Tuple[Any, str, str]]:
        """Resolve linked discussion chat details when present and accessible."""
        entity = context['entity']
        linked_chat_id = getattr(entity, 'linked_chat_id', None)
        if not linked_chat_id:
            return None

        client = context['client']
        linked_entity = await self._resolve_reference(
            client,
            linked_chat_id,
            source="linked_chat_resolution",
        )
        if linked_entity is None:
            return None

        linked_group_id = str(getattr(linked_entity, 'id', linked_chat_id))
        linked_group_name = getattr(linked_entity, 'title', f"ID_{linked_group_id}")
        return linked_entity, linked_group_id, linked_group_name

    async def _collect_participants_for_entity(
        self,
        client,
        entity,
        group_id: str,
        group_name: str,
        *,
        source: str,
    ) -> None:
        """Best-effort participant collection that keeps running on non-fatal errors."""
        seen_user_ids: Set[int] = set()
        try:
            async for participant in client.iter_participants(entity):
                participant_id = getattr(participant, 'id', None)
                if participant_id is None or participant_id in seen_user_ids:
                    continue
                seen_user_ids.add(participant_id)
                saved = await self._store_user_entity(participant, group_id, group_name, source)
                if saved:
                    self.stats['participant_users_found'] += 1
        except Exception as e:
            self._record_non_fatal_error(f"{source} for {group_name}", e)

    async def _collect_reply_sender(
        self,
        client,
        message,
        group_id: str,
        group_name: str,
        seen_user_ids: Set[int],
    ) -> None:
        """Collect the sender of the replied-to message when available."""
        if not getattr(message, 'reply_to_msg_id', None):
            return

        get_reply_message = getattr(message, 'get_reply_message', None)
        if not callable(get_reply_message):
            return

        try:
            reply_message = await get_reply_message()
        except Exception as e:
            self._record_non_fatal_error("loading reply message", e)
            return

        reply_sender_id = getattr(reply_message, 'sender_id', None)
        if reply_sender_id is not None:
            await self._collect_reference(
                client,
                reply_sender_id,
                group_id,
                group_name,
                "reply_sender",
                seen_user_ids,
            )
            await self._collect_forward_users(client, reply_message, group_id, group_name, seen_user_ids)

    async def _collect_mentions(
        self,
        client,
        message,
        group_id: str,
        group_name: str,
        seen_user_ids: Set[int],
    ) -> None:
        """Collect users referenced by entity mentions and raw @username text."""
        text = self._get_message_text(message)
        entity_lists = [
            getattr(message, 'entities', None) or [],
            getattr(message, 'caption_entities', None) or [],
        ]

        for entity_group in entity_lists:
            for entity in entity_group:
                user_id = getattr(entity, 'user_id', None)
                if user_id is not None:
                    await self._collect_reference(
                        client,
                        user_id,
                        group_id,
                        group_name,
                        "text_mention",
                        seen_user_ids,
                    )
                    continue

                if entity.__class__.__name__ == "MessageEntityMention":
                    username = self._slice_entity_text(text, entity)
                    if username:
                        await self._collect_reference(
                            client,
                            username,
                            group_id,
                            group_name,
                            "mention_entity",
                            seen_user_ids,
                        )

        for username in self._extract_raw_usernames(text):
            await self._collect_reference(
                client,
                username,
                group_id,
                group_name,
                "raw_mention",
                seen_user_ids,
            )

    async def _collect_forward_users(
        self,
        client,
        message,
        group_id: str,
        group_name: str,
        seen_user_ids: Set[int],
    ) -> None:
        """Collect user references from forwarded message metadata when resolvable."""
        forward = getattr(message, 'forward', None) or getattr(message, 'fwd_from', None)
        if not forward:
            return

        for attr_name in ("sender_id", "from_id"):
            reference = getattr(forward, attr_name, None)
            if reference is None:
                continue
            await self._collect_reference(
                client,
                reference,
                group_id,
                group_name,
                f"forward_{attr_name}",
                seen_user_ids,
            )

    async def _collect_action_users_from_message(
        self,
        client,
        message,
        group_id: str,
        group_name: str,
        seen_user_ids: Set[int],
    ) -> None:
        """Collect users referenced by service actions such as joins, invites, and removals."""
        action = getattr(message, 'action', None)
        if not action:
            return

        for attr_name, reference in self._iter_action_references(action):
            await self._collect_reference(
                client,
                reference,
                group_id,
                group_name,
                f"action_{attr_name}",
                seen_user_ids,
            )

    async def _collect_users_from_message_sources(
        self,
        client,
        message,
        group_id: str,
        group_name: str,
    ) -> None:
        """Run the per-message extraction pipeline for one message object."""
        seen_user_ids: Set[int] = set()
        sender_id = getattr(message, 'sender_id', None)
        if sender_id is not None:
            await self._collect_reference(
                client,
                sender_id,
                group_id,
                group_name,
                "message_sender",
                seen_user_ids,
            )

        if self.collect_via_bots:
            via_bot_id = getattr(message, 'via_bot_id', None)
            if via_bot_id is not None:
                await self._collect_reference(
                    client,
                    via_bot_id,
                    group_id,
                    group_name,
                    "via_bot",
                    seen_user_ids,
                )

        if self.collect_reply_senders:
            await self._collect_reply_sender(client, message, group_id, group_name, seen_user_ids)

        if self.collect_mentioned_users:
            await self._collect_mentions(client, message, group_id, group_name, seen_user_ids)

        if self.collect_forwarded_users:
            await self._collect_forward_users(client, message, group_id, group_name, seen_user_ids)

        if self.collect_action_users:
            await self._collect_action_users_from_message(
                client,
                message,
                group_id,
                group_name,
                seen_user_ids,
            )

    async def _collect_admin_event_users(
        self,
        client,
        admin_event: Any,
        group_id: str,
        group_name: str,
    ) -> None:
        """Collect user references from an admin-log event without requiring admin success."""
        seen_user_ids: Set[int] = set()

        admin_user_id = getattr(admin_event, 'user_id', None)
        if admin_user_id is not None:
            await self._collect_reference(
                client,
                admin_user_id,
                group_id,
                group_name,
                "admin_event_actor",
                seen_user_ids,
            )

        for attr_name in ("old", "new"):
            value = getattr(admin_event, attr_name, None)
            if value is None:
                continue
            if hasattr(value, 'sender_id') or hasattr(value, 'action'):
                await self._collect_users_from_message_sources(client, value, group_id, group_name)
                continue

            for nested_attr_name, reference in self._iter_action_references(value):
                await self._collect_reference(
                    client,
                    reference,
                    group_id,
                    group_name,
                    f"admin_{attr_name}_{nested_attr_name}",
                    seen_user_ids,
                )

    def _get_reference_type(self, reference: Any) -> str:
        """Detect reference type: 'username' for strings, 'user_id' for integers"""
        return 'username' if isinstance(reference, str) else 'user_id'

    async def _collect_reference(
        self,
        client,
        reference: Any,
        group_id: str,
        group_name: str,
        source: str,
        seen_user_ids: Set[int],
    ) -> None:
        """Resolve and persist one user reference while continuing on recoverable failures."""
        normalized_reference = self._normalize_reference(reference)
        if normalized_reference is None:
            return

        reference_type = self._get_reference_type(normalized_reference)

        # Check if this reference (username or user_id) has previously failed
        if self.state.is_failed_lookup(normalized_reference, reference_type):
            return

        entity = await self._resolve_reference(client, normalized_reference, source=source)
        if entity is None:
            # Resolution failed - failed_lookup already tracked in _resolve_reference
            # with appropriate error type and retry timing
            return

        if not self._is_user_entity(entity):
            return

        user_id = getattr(entity, 'id', None)
        if user_id is None or user_id in seen_user_ids:
            return

        seen_user_ids.add(user_id)
        await self._store_user_entity(entity, group_id, group_name, source)

    async def _resolve_reference(self, client, reference: Any, *, source: str) -> Optional[Any]:
        """Resolve a Telethon entity reference with three-tier caching: memory → database → API."""
        cache_key = self._make_cache_key(reference)
        reference_type = self._get_reference_type(reference)
        
        # Tier 1: Check in-memory cache first (fastest)
        if cache_key in self._entity_cache:
            return self._entity_cache[cache_key]
        
        # Tier 2: Check database cache (fast, shared across accounts)
        cached_entity_data = self.state.get_cached_entity(cache_key)
        if cached_entity_data:
            # Update in-memory cache and return
            self._entity_cache[cache_key] = cached_entity_data
            return cached_entity_data

        # Tier 3: Make API call (slowest, but necessary for cache miss)
        try:
            entity = await self.retry_api_call(
                client.get_entity,
                reference,
                _client=client,
                _account_name=_scan_account_name.get(),
                _clients_map=self._clients_map,
                _account_health=self._account_health,
            )
        except AccountFailureError:
            raise
        except errors.UsernameNotOccupiedError as e:
            # Username doesn't exist - permanent failure, cache it
            await self.state.add_failed_lookup(
                reference,
                'username_not_occupied',
                reference_type,
                retry_after_days=None  # Permanent, don't retry
            )
            self.stats['failed_lookups'] += 1
            self._record_non_fatal_error(f"resolving {source}", e)
            entity = None
        except errors.ChannelPrivateError as e:
            # Private user/channel - permanent failure (most likely)
            await self.state.add_failed_lookup(
                reference,
                'channel_private',
                reference_type,
                retry_after_days=90  # Long retry in case privacy settings change
            )
            self._record_non_fatal_error(f"resolving {source}", e)
            entity = None
        except errors.UserIdInvalidError as e:
            # Invalid user ID - permanent failure
            await self.state.add_failed_lookup(
                reference,
                'user_id_invalid',
                reference_type,
                retry_after_days=None  # Permanent, don't retry
            )
            self.stats['failed_lookups'] += 1
            self._record_non_fatal_error(f"resolving {source}", e)
            entity = None
        except errors.FloodWaitError as e:
            # Temporary rate limit - short retry window
            wait_seconds = getattr(e, 'seconds', 60)
            # Convert seconds to a small fraction of days
            retry_after_minutes = min(wait_seconds / 60, 60)  # Max 60 minutes
            # Note: add_failed_lookup expects days, so we pass None and handle differently
            await self.state.add_failed_lookup(
                reference,
                'flood_wait',
                reference_type,
                retry_after_days=None  # Don't set standard retry, flood wait handled by retry_api_call
            )
            self._record_non_fatal_error(f"resolving {source} (flood wait {wait_seconds}s)", e)
            entity = None
        except errors.PeerIdInvalidError as e:
            # Invalid peer ID - could be temporary (not synced) or permanent
            await self.state.add_failed_lookup(
                reference,
                'peer_id_invalid',
                reference_type,
                retry_after_days=30  # Retry after a month (might become accessible)
            )
            self.stats['failed_lookups'] += 1
            self._record_non_fatal_error(f"resolving {source}", e)
            entity = None
        except Exception as e:
            # Generic error - track it but allow retry
            error_type = type(e).__name__
            await self.state.add_failed_lookup(
                reference,
                error_type,
                reference_type,
                retry_after_days=7  # Retry after a week
            )
            self.stats['failed_lookups'] += 1
            self._record_non_fatal_error(f"resolving {source}", e)
            entity = None

        # Cache successful resolutions in both memory and database
        if entity is not None:
            # Determine entity type for serialization
            entity_type = entity.__class__.__name__ if hasattr(entity, '__class__') else 'Unknown'
            
            # Save to database cache (shared across accounts)
            self.state.save_cached_entity(cache_key, entity, entity_type)
            
            # Update in-memory cache (existing behavior)
            self._entity_cache[cache_key] = entity
        else:
            # Cache None results in memory to avoid repeated failed lookups in same session
            self._entity_cache[cache_key] = entity
        
        return entity

    async def _store_user_entity(
        self,
        entity: Any,
        group_id: str,
        group_name: str,
        source: str,
    ) -> bool:
        """Persist a resolved user and keep going if the database write hits a recoverable issue."""
        try:
            user_info = self.format_user_info(entity)
            user_id = int(user_info['id'])
            await self.state.upsert_user(user_info)
            await self.state.add_membership(user_id, group_id, group_name)
            self.stats['users_found'] += 1
            self.stats['memberships_added'] += 1
            return True
        except Exception as e:
            self._record_non_fatal_error(f"saving {source}", e)
            return False

    def _iter_action_references(self, action: Any) -> Iterable[Tuple[str, Any]]:
        """Yield likely user references from a Telethon action object."""
        for attr_name in ("user_id", "inviter_id", "from_id", "participant_id"):
            reference = getattr(action, attr_name, None)
            if reference is not None:
                yield attr_name, reference

        peer = getattr(action, 'peer', None)
        peer_user_id = getattr(peer, 'user_id', None)
        if peer_user_id is not None:
            yield "peer.user_id", peer_user_id

        for attr_name in ("users", "user_ids"):
            references = getattr(action, attr_name, None) or []
            for reference in references:
                yield attr_name, reference

    def _get_message_text(self, message: Any) -> str:
        """Return the best available text payload for entity slicing and regex mentions."""
        for attr_name in ("raw_text", "message", "text"):
            value = getattr(message, attr_name, None)
            if isinstance(value, str) and value:
                return value
        return ""

    def _slice_entity_text(self, text: str, entity: Any) -> Optional[str]:
        """Slice a mention entity safely from message text."""
        offset = getattr(entity, 'offset', None)
        length = getattr(entity, 'length', None)
        if offset is None or length is None:
            return None
        try:
            fragment = text[offset:offset + length].strip()
        except Exception:
            return None
        if not fragment.startswith("@"):
            return None
        return fragment

    def _extract_raw_usernames(self, text: str) -> Set[str]:
        """Extract candidate @username mentions from raw text, including t.me/username and https://t.me/username patterns."""
        if not text:
            return set()
        
        # Pattern 1: @username (existing pattern)
        pattern_at = r'(?<![\w@])@([A-Za-z0-9_]{5,32})'
        
        # Pattern 2: t.me/username (bare domain)
        pattern_tme = r'(?:t\.me/)([A-Za-z0-9_]{5,32})'
        
        # Pattern 3: https://t.me/username or http://t.me/username
        pattern_https_tme = r'(?:https?://t\.me/)([A-Za-z0-9_]{5,32})'
        
        # Combine patterns with OR logic
        combined_pattern = f'{pattern_at}|{pattern_tme}|{pattern_https_tme}'
        
        # Extract all matches and filter out empty groups
        usernames = set()
        for match in re.finditer(combined_pattern, text):
            # finditer returns match objects with groups; get the first non-None group
            username = next((g for g in match.groups() if g is not None), None)
            if username:
                usernames.add(f"@{username}")
        
        return usernames

    def _normalize_reference(self, reference: Any) -> Optional[Any]:
        """Normalize references into a shape suitable for get_entity lookups."""
        if reference is None:
            return None
        if isinstance(reference, str):
            normalized = reference.strip()
            if not normalized:
                return None
            if not normalized.startswith("@"):
                normalized = f"@{normalized.lstrip('@')}"
            return normalized
        return reference

    def _make_cache_key(self, reference: Any) -> str:
        """Create a stable cache key for entity resolution."""
        if isinstance(reference, (int, str)):
            return str(reference)
        if hasattr(reference, 'user_id'):
            return f"user:{getattr(reference, 'user_id')}"
        if hasattr(reference, 'channel_id'):
            return f"channel:{getattr(reference, 'channel_id')}"
        if hasattr(reference, 'chat_id'):
            return f"chat:{getattr(reference, 'chat_id')}"
        return repr(reference)

    def _is_user_entity(self, entity: Any) -> bool:
        """Best-effort check that a resolved entity is a user-like object."""
        if entity is None:
            return False

        class_name = entity.__class__.__name__.lower()
        if class_name == "user":
            return True

        return any(
            hasattr(entity, attr_name)
            for attr_name in ("first_name", "last_name", "phone", "bot")
        )

    def _record_non_fatal_error(self, context: str, error: Exception) -> None:
        """Track and log recoverable extraction errors without interrupting the scan."""
        if is_account_error(error):
            raise AccountFailureError(_scan_account_name.get(), error, phase=f"{self.name}:{context}") from error
        self.stats['non_fatal_errors'] += 1
        log_warning(f"⚠️ [{self.name}] Non-fatal error while {context}: {error}")
