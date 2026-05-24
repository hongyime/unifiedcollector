#!/usr/bin/env python3
"""
Link Collector Processor for Unified Message Orchestrator
Extracts links from message text and stores them in SQLite.
"""
import re
from typing import Any, Dict, List, Optional, Set
from src.core.feature_processor import FeatureProcessor
from src.core.scan_targets import discover_scan_targets
from src.core.base_feature import BaseFeature
from src.core.state_manager import get_state_manager
from src.core.progress_logger import log_info, log_success, log_warning


class LinkCollectorProcessor(FeatureProcessor, BaseFeature):
    """
    Processor that collects Telegram links from message text.
    Stores links in SQLite (link_collection table) with in-memory dedup for speed.
    """

    name = "link_collector"
    feature_key = "links"

    def __init__(self):
        BaseFeature.__init__(self, name=self.name)
        self.state = get_state_manager()
        self.stats = {
            'links_found': 0,
            'bot_links_filtered': 0,
            'duplicates_skipped': 0,
        }
        self.existing_links: Set[str] = set()

    async def initialize(self) -> None:
        """Load existing links from SQLite for in-memory dedup."""
        log_info(f"🔗 [{self.name}] Initializing link collector...")
        self.existing_links = self.state.load_existing_links('telegram')
        log_info(f"📚 [{self.name}] Loaded {len(self.existing_links)} existing links from DB")

    async def shutdown(self) -> None:
        """Flush any pending writes and log stats."""
        self.state._flush_links()
        log_info(
            f"💾 [{self.name}] Shutting down... "
            f"Links found: {self.stats['links_found']}, "
            f"filtered: {self.stats['bot_links_filtered']}, "
            f"dupes skipped: {self.stats['duplicates_skipped']}"
        )

    async def discover_scan_targets(
        self,
        client,
        account: Dict[str, Any],
        group_ids: Optional[List[str]] = None,
    ) -> Optional[List[Dict[str, Any]]]:
        """Use linked discussion groups for broadcast channels."""
        return await discover_scan_targets(
            client,
            group_ids=group_ids,
            include_private_chats=False,
            prefer_linked_discussions=True,
        )

    async def on_scan_start(self, context: Dict[str, Any]) -> None:
        log_info(f"🔍 [{self.name}] Starting link collection for: {context['group_name']}")

    async def on_scan_complete(self, context: Dict[str, Any]) -> None:
        # Flush any buffered links to DB at group boundary
        self.state._flush_links()
        log_success(
            f"✅ [{self.name}] {context['group_name']}: Found {self.stats['links_found']} new links"
        )

    async def process_message(self, event: Dict[str, Any]) -> None:
        """Extract and store links from a message."""
        message = event['message']
        group_name = event['group_name']
        group_id = event['group_id']
        account_name = event['account_name']

        text_sources = []
        if hasattr(message, 'text') and message.text:
            text_sources.append(message.text)
        if hasattr(message, 'forward') and message.forward and hasattr(message.forward, 'message') and message.forward.message:
            text_sources.append(message.forward.message)
        if hasattr(message, 'media') and message.media and hasattr(message, 'message') and message.message:
            text_sources.append(message.message)

        for text in text_sources:
            if text:
                for link in self.extract_links_from_text(text):
                    self._process_link(link, group_name, account_name)

        self.state.save_feature_progress(
            account_name,
            group_id,
            'links',
            message.id,
            self.stats['links_found'],
        )

    def extract_links_from_text(self, text: str) -> List[str]:
        """Return list of username/path fragments found in text."""
        patterns = [
            r'https?://t\.me/([a-zA-Z0-9_]{5,32}(?![a-zA-Z0-9_]))',
            r'(?:^|\s)@([a-zA-Z0-9_]{5,32}(?![a-zA-Z0-9_]))',
            r't\.me/([a-zA-Z0-9_]{5,32}(?![a-zA-Z0-9_]))',
        ]
        links = []
        for pattern in patterns:
            for match in re.findall(pattern, text):
                username = match.strip().rstrip('.,;:!?)')
                if username and not self._is_bot_keyword(username):
                    links.append(username)
        return links

    def _process_link(self, link: str, group_name: str, account_name: str) -> None:
        """Save a single link to SQLite (dedup via in-memory set + DB UNIQUE constraint)."""
        full_link = f"https://t.me/{link}"

        if full_link in self.existing_links:
            self.stats['duplicates_skipped'] += 1
            return

        if self._is_bot_link(link):
            self.stats['bot_links_filtered'] += 1
            log_warning(f"🤖 [{self.name}] Skipped bot link: {full_link}")
            return

        self.state.save_link(full_link, 'telegram', group_name, account_name)
        self.existing_links.add(full_link)
        self.stats['links_found'] += 1
        log_success(f"✅ [{self.name}] New link: {full_link}")

    def _is_bot_keyword(self, text: str) -> bool:
        bot_keywords = ['bot', 'Bot', 'BOT', 'robot', 'Robot', 'info', 'Info', 'news', 'News']
        return any(keyword in text for keyword in bot_keywords)

    def _is_bot_link(self, link: str) -> bool:
        return self._is_bot_keyword(link)
