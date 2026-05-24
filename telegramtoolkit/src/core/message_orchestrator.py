#!/usr/bin/env python3
"""
Message Orchestrator for Telegram Toolkit
Centralized message scanning that routes to multiple feature processors.
"""
import asyncio
import random
from datetime import datetime
from typing import Any, Dict, List, Optional
from telethon import TelegramClient
from telethon.tl.types import Channel, Chat

from src.core.account_health import AccountFailureError, AccountHealthPolicy
from src.core.state_manager import get_state_manager
from src.core.base_feature import BaseFeature
from src.core.feature_processor import FeatureProcessor
from src.core.dynamic_config import get_config_value
from src.core.progress_logger import log_info, log_success, log_error, log_warning
from src.core.scan_targets import cleanup_scan_target


class MessageOrchestrator(BaseFeature):
    """
    Centralized message scanner that:
    1. Scans messages once per group/account combination
    2. Routes each message to multiple feature processors
    3. Manages progress centrally
    4. Handles all common patterns (offset, retries, rate limiting)
    """
    
    def __init__(self):
        super().__init__(name="MessageOrchestrator")
        self.processors: List[FeatureProcessor] = []
        self.state = get_state_manager()
        self.account_health = AccountHealthPolicy()
        self.scan_min_delay = float(get_config_value('SCAN_MIN_DELAY', 0.0) or 0.0)
        self.scan_max_delay = float(get_config_value('SCAN_MAX_DELAY', 0.15) or 0.15)
        self.scan_delay_every_messages = int(get_config_value('SCAN_DELAY_EVERY_MESSAGES', 25) or 25)
        self.scan_group_delay_seconds = float(get_config_value('SCAN_GROUP_DELAY_SECONDS', 0.25) or 0.25)
        self.scan_progress_save_interval = max(1, int(get_config_value('SCAN_PROGRESS_SAVE_INTERVAL', 50) or 50))
        self.shutdown_cancel_timeout_seconds = float(get_config_value('SHUTDOWN_CANCEL_TIMEOUT_SECONDS', 1.5) or 1.5)
        self.shutdown_poll_interval_seconds = float(get_config_value('SHUTDOWN_POLL_INTERVAL_SECONDS', 0.2) or 0.2)
        self.stats = {
            'groups_scanned': 0,
            'messages_processed': 0,
            'processors_active': 0
        }
    
    def register_processor(self, processor: FeatureProcessor) -> None:
        """Register a feature processor to receive message events"""
        self.processors.append(processor)
        self.stats['processors_active'] = len(self.processors)
        log_info(f"🔌 Registered processor: {processor.name}")

    def _get_account_health(self) -> AccountHealthPolicy:
        """Provide account-health policy even for atypical construction paths."""
        policy = getattr(self, 'account_health', None)
        if policy is None:
            policy = AccountHealthPolicy()
            self.account_health = policy
        return policy

    def get_processor_feature_keys(self) -> List[str]:
        """Return stable feature-progress keys for all registered processors."""
        feature_keys: List[str] = []
        for processor in self.processors:
            feature_key = getattr(processor, 'feature_key', '') or processor.name
            if feature_key not in feature_keys:
                feature_keys.append(feature_key)
        return feature_keys

    def get_unified_progress_snapshot(self, account_name: str, group_id: str) -> Dict[str, int]:
        """Return progress for all registered processors, defaulting missing keys to zero."""
        feature_progress = self.state.get_feature_progress_all(account_name, group_id)
        return {
            feature_key: feature_progress.get(feature_key) or 0
            for feature_key in self.get_processor_feature_keys()
        }

    def get_unified_start_message_id(self, account_name: str, group_id: str) -> int:
        """Start from the earliest registered feature checkpoint to avoid data loss."""
        progress_snapshot = self.get_unified_progress_snapshot(account_name, group_id)
        if not progress_snapshot:
            return 0
        return min(progress_snapshot.values())
    
    async def initialize_processors(self) -> None:
        """Initialize all registered processors"""
        for processor in self.processors:
            try:
                await processor.initialize()
                log_success(f"✅ Processor {processor.name} initialized")
            except Exception as e:
                log_error(f"❌ Failed to initialize processor {processor.name}: {e}")
    
    async def shutdown_processors(self) -> None:
        """Gracefully shutdown all processors"""
        for processor in self.processors:
            try:
                await processor.shutdown()
                log_success(f"✅ Processor {processor.name} shut down cleanly")
            except Exception as e:
                log_error(f"⚠️ Error shutting down processor {processor.name}: {e}")
    
    async def _scan_account_impl(
        self,
        client: TelegramClient,
        account: Dict[str, Any],
        group_ids: Optional[List[str]] = None,
        *,
        unified: bool = False,
    ) -> None:
        """Shared implementation for per-account group scanning."""
        account_name = account['name']
        mode_label = "unified all-features" if unified else "unified message"
        log_info(f"\n📱 [{account_name}] Starting {mode_label} scan...")

        scan_targets = await self._discover_scan_targets(client, account, group_ids)
        log_success(f"📊 [{account_name}] Found {len(scan_targets)} groups to scan")
        health = self._get_account_health()

        for i, target in enumerate(scan_targets, 1):
            if self.should_exit:
                break
            if not await health.ensure_connected(client, account):
                log_warning(f"🚫 [{account_name}] Account retired for this run; stopping remaining groups")
                break

            entity = target['entity']
            group_id = target['group_id']
            group_name = target['group_name']
            scan_label = "Unified scanning" if unified else "Scanning"

            log_info(f"\n[{i}/{len(scan_targets)}] [{account_name}] {scan_label}: {group_name}")

            try:
                if unified:
                    await self.scan_all_features(client, entity, group_id, group_name, account_name)
                else:
                    await self.scan_group(client, entity, group_id, group_name, account_name)
                self.stats['groups_scanned'] += 1
            except AccountFailureError as e:
                phase_prefix = "scan_all_features" if unified else "scan_group"
                recovered = await health.handle_account_failure(
                    client, account, e.original_error,
                    e.phase or f"{phase_prefix}:{group_name}",
                )
                if not recovered:
                    break
            except Exception as e:
                log_error(f"❌ [{account_name}] Error scanning {group_name}: {e}")
            finally:
                await cleanup_scan_target(client, target)

            if self.scan_group_delay_seconds > 0:
                await asyncio.sleep(self.scan_group_delay_seconds)

        log_success(f"✅ [{account_name}] {scan_label} complete! Groups: {self.stats['groups_scanned']}, Messages: {self.stats['messages_processed']}")

    async def scan_account(
        self,
        client: TelegramClient,
        account: Dict[str, Any],
        group_ids: Optional[List[str]] = None
    ) -> None:
        """Scan all groups for an account and route messages to processors."""
        await self._scan_account_impl(client, account, group_ids, unified=False)
    
    async def scan_all_features(
        self,
        client: TelegramClient,
        entity,
        group_id: str,
        group_name: str,
        account_name: str
    ) -> None:
        """
        Greedy unified scan: start from minimum progress across all features.
        This ensures no data loss - all features process all messages from the earliest point.
        
        Args:
            client: TelegramClient instance
            entity: The group/channel entity
            group_id: String ID of the group
            group_name: Display name of the group
            account_name: Name of the account
        """
        # Check if we can access this group
        if not await self.verify_entity_access(client, entity):
            log_warning(f"🚫 [{account_name}] Cannot access {group_name}, skipping")
            return
        
        # Greedy approach: start from the earliest registered feature checkpoint.
        progress_snapshot = self.get_unified_progress_snapshot(account_name, group_id)
        start_from = self.get_unified_start_message_id(account_name, group_id)
        
        # Handle offset correctly (0 instead of None to prevent Telethon crash)
        offset = start_from if start_from and start_from > 0 else 0
        
        progress_summary = ", ".join(
            f"{feature_key}@{message_id}"
            for feature_key, message_id in progress_snapshot.items()
        ) or "no feature progress yet"
        log_info(
            f"🔥 [{account_name}] Unified scan for {group_name}: "
            f"{progress_summary} -> Starting from {start_from}"
        )
        
        health = self._get_account_health()

        context = {
            'client': client,
            'entity': entity,
            'group_id': group_id,
            'group_name': group_name,
            'account_name': account_name,
            'account_health': health,
            'clients_map': getattr(self, '_clients_map', None) or {},
            'start_from': start_from,
            'feature_progress': progress_snapshot,
            'unified': True
        }

        for processor in self.processors:
            try:
                await processor.on_scan_start(context)
            except AccountFailureError:
                raise
            except Exception as e:
                log_error(f"⚠️ Processor {processor.name} on_scan_start error: {e}")

        messages_processed = 0
        last_processed_message_id = start_from
        try:
            async for message in client.iter_messages(
                entity,
                offset_id=offset,
                reverse=True
            ):
                if self.should_exit:
                    break

                event = {
                    'message': message,
                    'entity': entity,
                    'group_id': group_id,
                    'group_name': group_name,
                    'account_name': account_name,
                    'account_health': health,
                    'clients_map': getattr(self, '_clients_map', None) or {},
                    'client': client
                }
                
                # Route to all processors
                for processor in self.processors:
                    try:
                        await processor.process_message(event)
                    except AccountFailureError:
                        raise
                    except Exception as e:
                        log_error(f"❌ Processor {processor.name} error: {e}")
                
                messages_processed += 1
                last_processed_message_id = message.id
                self.stats['messages_processed'] += 1
                
                # Apply scan pacing in batches instead of every single message
                await self._maybe_apply_scan_rate_limit(messages_processed)
                
                # Print progress every 100 messages
                if messages_processed % 100 == 0:
                    log_info(f"   📊 Processed {messages_processed} messages...")
        
        except AccountFailureError:
            raise
        except Exception as e:
            log_error(f"❌ [{account_name}] Error during unified scan of {group_name}: {e}")
        
        # Notify processors that scanning is complete
        for processor in self.processors:
            try:
                await processor.on_scan_complete(context)
            except Exception as e:
                log_error(f"⚠️ Processor {processor.name} on_scan_complete error: {e}")

        if messages_processed > 0:
            self.state.flush_all_buffers()
    
    async def scan_account_unified(
        self,
        client: TelegramClient,
        account: Dict[str, Any],
        group_ids: Optional[List[str]] = None
    ) -> None:
        """Scan all groups using unified mode (greedy approach)."""
        await self._scan_account_impl(client, account, group_ids, unified=True)
    
    async def scan_group(
        self,
        client: TelegramClient,
        entity,
        group_id: str,
        group_name: str,
        account_name: str
    ) -> None:
        """
        Scan a single group and route messages to all processors.
        
        Args:
            client: TelegramClient instance
            entity: The group/channel entity
            group_id: String ID of the group
            group_name: Display name of the group
            account_name: Name of the account
        """
        # Check if we can access this group
        if not await self.verify_entity_access(client, entity):
            log_warning(f"🚫 [{account_name}] Cannot access {group_name}, skipping")
            return
        
        # Get progress for this group (unified across all processors)
        last_message_id = self.state.get_chat_progress(account_name, group_id)
        
        # Handle offset correctly (0 instead of None to prevent Telethon crash)
        offset = last_message_id if last_message_id and last_message_id > 0 else 0
        
        health = self._get_account_health()

        context = {
            'client': client,
            'entity': entity,
            'group_id': group_id,
            'group_name': group_name,
            'account_name': account_name,
            'account_health': health,
            'clients_map': getattr(self, '_clients_map', None) or {},
            'last_message_id': last_message_id
        }

        for processor in self.processors:
            try:
                await processor.on_scan_start(context)
            except AccountFailureError:
                raise
            except Exception as e:
                log_error(f"⚠️ Processor {processor.name} on_scan_start error: {e}")

        messages_processed = 0
        last_processed_message_id = offset
        try:
            async for message in client.iter_messages(
                entity,
                offset_id=offset,
                reverse=True
            ):
                if self.should_exit:
                    break

                event = {
                    'message': message,
                    'entity': entity,
                    'group_id': group_id,
                    'group_name': group_name,
                    'account_name': account_name,
                    'account_health': health,
                    'clients_map': getattr(self, '_clients_map', None) or {},
                    'client': client
                }
                
                # Route to all processors
                for processor in self.processors:
                    try:
                        await processor.process_message(event)
                    except AccountFailureError:
                        raise
                    except Exception as e:
                        log_error(f"❌ Processor {processor.name} error: {e}")
                
                # Update progress in batches to reduce SQLite churn
                if self._should_checkpoint_progress(messages_processed + 1):
                    self.state.update_scan_progress(account_name, group_id, message.id)
                
                messages_processed += 1
                last_processed_message_id = message.id
                self.stats['messages_processed'] += 1
                
                # Apply scan pacing in batches instead of every single message
                await self._maybe_apply_scan_rate_limit(messages_processed)
                
                # Print progress every 100 messages
                if messages_processed % 100 == 0:
                    log_info(f"   📊 Processed {messages_processed} messages...")
        
        except AccountFailureError:
            raise
        except Exception as e:
            log_error(f"❌ Error scanning {group_name}: {e}")
        
        # Notify processors that scanning is complete
        context['messages_processed'] = messages_processed
        
        for processor in self.processors:
            try:
                await processor.on_scan_complete(context)
            except Exception as e:
                log_error(f"⚠️ Processor {processor.name} on_scan_complete error: {e}")

        if messages_processed > 0:
            self.state.update_scan_progress(account_name, group_id, last_processed_message_id)
            self.state.flush_all_buffers()
        
        log_success(f"✅ [{account_name}] {group_name}: {messages_processed} messages processed")

    async def _discover_scan_targets(
        self,
        client: TelegramClient,
        account: Dict[str, Any],
        group_ids: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Resolve scan targets, allowing processors to override legacy dialog discovery."""
        explicit_targets_by_id: Dict[str, Dict[str, Any]] = {}
        processors_with_custom_targets = 0

        for processor in self.processors:
            try:
                processor_targets = await processor.discover_scan_targets(client, account, group_ids)
            except Exception as e:
                log_error(f"⚠️ Processor {processor.name} target discovery error: {e}")
                continue

            if processor_targets is None:
                continue

            processors_with_custom_targets += 1
            for target in processor_targets:
                group_id = str(target.get('group_id') or '')
                if not group_id:
                    continue

                existing = explicit_targets_by_id.get(group_id)
                if existing is None:
                    explicit_targets_by_id[group_id] = dict(target)
                    existing_cleanup = explicit_targets_by_id[group_id].get('cleanup_entities') or []
                    explicit_targets_by_id[group_id]['cleanup_entities'] = list(existing_cleanup)
                    continue

                existing_cleanup_ids = {
                    str(getattr(cleanup_entity, 'id', id(cleanup_entity)))
                    for cleanup_entity in existing.get('cleanup_entities', [])
                }
                for cleanup_entity in target.get('cleanup_entities', []) or []:
                    cleanup_key = str(getattr(cleanup_entity, 'id', id(cleanup_entity)))
                    if cleanup_key not in existing_cleanup_ids:
                        existing.setdefault('cleanup_entities', []).append(cleanup_entity)

        if processors_with_custom_targets > 0:
            return sorted(
                explicit_targets_by_id.values(),
                key=lambda target: (
                    target.get('scan_priority', 99),
                    target.get('discovery_order', 0),
                ),
            )

        return await self._discover_default_scan_targets(client, group_ids)

    async def _discover_default_scan_targets(
        self,
        client: TelegramClient,
        group_ids: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Fallback dialog discovery for processors that do not customize targets."""
        from src.core.config import MAX_DIALOGS_LIMIT
        
        targets: List[Dict[str, Any]] = []
        requested_ids = {str(group_id) for group_id in group_ids} if group_ids else None
        dialogs_scanned = 0

        async for dialog in client.iter_dialogs():
            if self.should_exit:
                break

            # Check dialog limit to prevent unbounded iteration on large accounts
            dialogs_scanned += 1
            if MAX_DIALOGS_LIMIT > 0 and dialogs_scanned > MAX_DIALOGS_LIMIT:
                print(f"⚠️ Reached dialog limit ({MAX_DIALOGS_LIMIT}). Set MAX_DIALOGS_LIMIT=0 in .env for unlimited.")
                break

            entity = getattr(dialog, 'entity', None)
            if entity is None:
                continue

            if not (getattr(dialog, 'is_group', False) or getattr(dialog, 'is_channel', False)):
                continue

            group_id = str(getattr(entity, 'id', ''))
            if not group_id:
                continue

            if requested_ids is not None and group_id not in requested_ids:
                continue

            group_name = getattr(entity, 'title', None) or getattr(dialog, 'name', None) or f'ID_{group_id}'
            targets.append(
                {
                    'entity': entity,
                    'group_id': group_id,
                    'group_name': group_name,
                    'scan_priority': 1,
                    'cleanup_entities': [],
                    'discovery_order': len(targets),
                }
            )

        return targets
    
    async def run(
        self,
        accounts: List[Dict[str, Any]],
        group_ids: Optional[List[str]] = None,
        unified_mode: bool = False
    ) -> Dict[str, Any]:
        """
        Main entry point: Run unified scanning across all accounts.

        Args:
            accounts: List of account dictionaries
            group_ids: Optional list of specific group IDs to scan
            unified_mode: If True, use scan_all_features (greedy approach)

        Returns:
            Statistics dictionary
        """
        log_info("\n" + "="*60)
        log_info("🚀 STARTING UNIFIED MESSAGE ORCHESTRATOR")
        if unified_mode:
            log_info("🔥 Mode: ALL-FEATURES UNIFIED SCAN (Greedy)")
        log_info("="*60)

        start_time = datetime.now()

        # Initialize all processors
        await self.initialize_processors()

        # Create and authenticate ALL clients upfront so processors can
        # fall back to alternate accounts for entity resolution / downloads.
        clients_map: Dict[str, TelegramClient] = {}
        for account in accounts:
            if self.should_exit:
                break
            try:
                client = TelegramClient(
                    account['session_file'],
                    account['api_id'],
                    account['api_hash']
                )
                await client.start(account['phone'])
                clients_map[account['name']] = client
                log_success(f"🔐 [{account['name']}] Logged in")
            except Exception as e:
                recovered = await self._get_account_health().handle_account_failure(
                    client, account, e, "account startup"
                )
                if recovered:
                    clients_map[account['name']] = client
                else:
                    log_error(f"❌ [{account['name']}] Could not start: {e}")

        self._clients_map = clients_map
        log_info(f"🔄 Cross-account pool: {len(clients_map)} account(s) available for round-robin")

        try:
            account_tasks = [
                asyncio.create_task(
                    self._run_account(account, group_ids, unified_mode, clients_map)
                )
                for account in accounts
                if account['name'] in clients_map
            ]

            if account_tasks:
                pending = set(account_tasks)
                while pending:
                    done, pending = await asyncio.wait(
                        pending,
                        timeout=self.shutdown_poll_interval_seconds,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    # If Ctrl+C was received, cancel remaining tasks immediately.
                    if self.should_exit and pending:
                        log_warning("🛑 Shutdown requested. Cancelling pending account scans...")
                        for task in pending:
                            task.cancel()
                        done_cancelled, still_pending = await asyncio.wait(
                            pending,
                            timeout=self.shutdown_cancel_timeout_seconds,
                        )
                        # Best-effort hard stop: any stragglers are left cancelled and ignored.
                        pending = still_pending
                        done |= done_cancelled
                        break

                # Drain exceptions to avoid 'Task exception was never retrieved'.
                for task in account_tasks:
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    except Exception as e:
                        log_error(f"❌ Account task error: {e}")

        finally:
            # Shutdown all processors
            await self.shutdown_processors()
            # Disconnect all clients
            for name, client in clients_map.items():
                try:
                    await client.disconnect()
                except Exception:
                    pass

        elapsed = (datetime.now() - start_time).total_seconds()

        log_info("\n" + "="*60)
        log_info("📊 ORCHESTRATOR SUMMARY")
        log_info("="*60)
        log_success(f"⏱️  Time: {elapsed:.1f} seconds")
        log_success(f"📱 Accounts: {len(accounts)}")
        log_success(f"📝 Groups: {self.stats['groups_scanned']}")
        log_success(f"💬 Messages: {self.stats['messages_processed']}")
        log_success(f"🔌 Processors: {self.stats['processors_active']}")

        return self.stats

    async def _run_account(
        self,
        account: Dict[str, Any],
        group_ids: Optional[List[str]],
        unified_mode: bool,
        clients_map: Optional[Dict[str, TelegramClient]] = None,
    ) -> None:
        """Run one account scan so all valid accounts can progress concurrently."""
        if self.should_exit:
            return

        client = (clients_map or {}).get(account['name'])
        if client is None:
            log_error(f"❌ [{account['name']}] No client available, skipping")
            return

        try:
            if unified_mode:
                await self.scan_account_unified(client, account, group_ids)
            else:
                await self.scan_account(client, account, group_ids)

        except asyncio.CancelledError:
            log_warning(f"🛑 [{account['name']}] Account scan cancelled")
            raise

        except Exception as e:
            recovered = await self._get_account_health().handle_account_failure(client, account, e, "account scan")
            if recovered:
                try:
                    if unified_mode:
                        await self.scan_account_unified(client, account, group_ids)
                    else:
                        await self.scan_account(client, account, group_ids)
                except Exception as resumed_error:
                    log_error(f"❌ [{account['name']}] Account error after reconnect: {resumed_error}")

    def _should_checkpoint_progress(self, messages_processed: int) -> bool:
        """Checkpoint scan progress periodically instead of on every message."""
        return messages_processed % self.scan_progress_save_interval == 0

    async def _maybe_apply_scan_rate_limit(self, messages_processed: int) -> None:
        """Apply lightweight pacing every N messages to avoid throttling."""
        if self.should_exit:
            return
        if self.scan_delay_every_messages <= 0:
            return
        if messages_processed % self.scan_delay_every_messages != 0:
            return

        if self.scan_max_delay <= self.scan_min_delay:
            delay = self.scan_min_delay
        else:
            delay = random.uniform(self.scan_min_delay, self.scan_max_delay)

        if delay > 0:
            await asyncio.sleep(delay)
