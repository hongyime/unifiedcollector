#!/usr/bin/env python3
"""
Standalone runner for multi-platform link collection.
"""

from __future__ import annotations

import asyncio
import os
import re

from telethon import TelegramClient

from src.core.account_health import AccountHealthPolicy, is_account_error
from src.core.dynamic_config import (
    MULTI_PLATFORM_LINKS_FILE,
    MULTI_PLATFORM_PATTERNS,
    get_accounts,
)
from src.core.parallel_processor import (
    AccountManager as ParallelAccountManager,
    TelegramParallelProcessor,
)


class MultiPlatformLinkCollector:
    """Collect non-Telegram links from dialogs across configured accounts."""

    def __init__(self, parallel_processor=None):
        self.parallel_processor = parallel_processor
        self.account_health = AccountHealthPolicy()
        self.multi_platform_buffer = []
        self.multi_platform_progress = {}
        self._link_flush_interval = 100

    def extract_multi_platform_links(self, text: str):
        """Extract non-Telegram social media links from text."""
        if not text:
            return []

        found_links = []
        for platform, pattern in MULTI_PLATFORM_PATTERNS.items():
            matches = re.findall(pattern, text, re.IGNORECASE)
            for match in matches:
                if platform == "whatsapp":
                    url = f"https://chat.whatsapp.com/{match}"
                elif platform in ["discord", "discord_alt"]:
                    url = f"https://discord.gg/{match}"
                elif platform in ["facebook", "facebook_alt"]:
                    url = f"https://facebook.com/groups/{match}"
                elif platform == "instagram":
                    url = f"https://instagram.com/{match}"
                elif platform in ["twitter", "twitter_alt"]:
                    url = f"https://twitter.com/{match}"
                elif platform in ["reddit", "reddit_alt"]:
                    url = f"https://reddit.com/r/{match}"
                else:
                    continue
                found_links.append((url, platform))

        return found_links

    def load_multi_platform_links(self):
        """Load previously collected multi-platform links for deduplication."""
        if not os.path.exists(MULTI_PLATFORM_LINKS_FILE):
            return set()

        links = set()
        try:
            with open(MULTI_PLATFORM_LINKS_FILE, "r", encoding="utf-8") as handle:
                for line in handle:
                    url = line.split("  #")[0].strip()
                    if url:
                        links.add(url)
        except Exception as e:
            print(f"❌ Error loading multi-platform links: {e}")
        return links

    def flush_multi_platform_buffer(self):
        """Flush buffered multi-platform links to disk."""
        if not self.multi_platform_buffer:
            return

        try:
            os.makedirs(os.path.dirname(MULTI_PLATFORM_LINKS_FILE), exist_ok=True)
            with open(MULTI_PLATFORM_LINKS_FILE, "a", encoding="utf-8") as handle:
                handle.writelines(self.multi_platform_buffer)
            self.multi_platform_buffer.clear()
        except Exception as e:
            print(f"❌ Error flushing multi-platform links: {e}")

    async def buffer_multi_platform_link(self, url, platform, source_name, account_name):
        """Buffer a multi-platform link for batch writing."""
        from datetime import datetime

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"{url}  # From: {source_name} ({account_name}) [{platform}] {timestamp}\n"
        self.multi_platform_buffer.append(line)
        if len(self.multi_platform_buffer) >= self._link_flush_interval:
            self.flush_multi_platform_buffer()

    async def collect_multi_platform_links_from_account(self, account):
        """Collect multi-platform links from all chats for an account."""
        print(f"\n🌐 Starting multi-platform link collection for {account['name']}")

        client = TelegramClient(account["session_file"], account["api_id"], account["api_hash"])
        try:
            await client.start(account["phone"])
        except Exception as e:
            recovered = await self.account_health.handle_account_failure(client, account, e, "multi_platform startup")
            if not recovered:
                return

        existing_links = self.load_multi_platform_links()
        account_progress_key = f"{account['name']}_multi_platform_progress"
        account_progress = self.multi_platform_progress.get(account_progress_key, {})
        new_links_count = 0

        try:
            async for dialog in client.iter_dialogs():
                entity = getattr(dialog, "entity", None)
                if entity is None or not hasattr(entity, "id"):
                    continue

                chat_id = f"{account['name']}_{entity.id}"
                chat_name = getattr(dialog, "name", None) or getattr(dialog, "title", None) or str(entity.id)
                last_scanned_id = account_progress.get(chat_id) or 0
                max_seen_id = last_scanned_id

                async for message in client.iter_messages(entity, offset_id=last_scanned_id, reverse=True):
                    if not hasattr(message, "id") or message.id is None:
                        continue

                    max_seen_id = max(max_seen_id, message.id)
                    text_sources = []
                    if getattr(message, "text", None):
                        text_sources.append(message.text)
                    if getattr(getattr(message, "forward", None), "message", None):
                        text_sources.append(message.forward.message)
                    if getattr(message, "media", None) and getattr(message, "message", None):
                        text_sources.append(message.message)

                    for text in text_sources:
                        for url, platform in self.extract_multi_platform_links(text):
                            if url in existing_links:
                                continue
                            try:
                                await self.buffer_multi_platform_link(url, platform, chat_name, account["name"])
                                existing_links.add(url)
                                new_links_count += 1
                                print(f"✅ [{account['name']}] New {platform} link: {url}")
                            except Exception as e:
                                print(f"❌ [{account['name']}] Error saving multi-platform link: {e}")

                    account_progress[chat_id] = max_seen_id
                    self.multi_platform_progress[account_progress_key] = account_progress
        except Exception as e:
            if is_account_error(e):
                await self.account_health.handle_account_failure(client, account, e, "multi_platform collect")
                return
            raise

        self.flush_multi_platform_buffer()

        try:
            from src.core.resilience import atomic_json_write

            progress_file = os.path.join("data", "multi_platform_progress.json")
            os.makedirs(os.path.dirname(progress_file), exist_ok=True)
            atomic_json_write(progress_file, self.multi_platform_progress)
        except Exception as e:
            print(f"❌ Error saving multi-platform progress: {e}")

        await client.disconnect()
        print(f"✅ [{account['name']}] Multi-platform link collection completed. Found {new_links_count} new links")

    async def collect_all_multi_platform_links(self, accounts=None):
        """Main entry point for multi-platform link collection."""
        print("\n🌐 Starting multi-platform link collection across selected accounts")
        print("=" * 60)

        self.multi_platform_buffer = []
        self.multi_platform_progress = {}

        if accounts is None:
            accounts = get_accounts()

        for account in accounts:
            try:
                await self.collect_multi_platform_links_from_account(account)
            except Exception as e:
                print(f"❌ Error processing {account['name']}: {e}")
                continue

        self.flush_multi_platform_buffer()
        print("\n🎉 Multi-platform collection complete!")
        print(f"📁 Links saved to: {os.path.abspath(MULTI_PLATFORM_LINKS_FILE)}")
        print("💡 Note: Telegram links are collected by the unified processor-backed scanner")


async def main():
    """Standalone entry point with compatibility account selection."""
    print("\n🔗 Multi-Platform Link Collector")
    print("=" * 60)

    available_accounts = ParallelAccountManager.get_available_accounts()
    if not available_accounts:
        print("❌ No accounts found! Please run Account Manager first.")
        return

    print(f"Found {len(available_accounts)} accounts")
    print("\n⚙️ Parallel Processing Options:")
    print("1. Use all available accounts")
    print("2. Use optimal number of accounts")
    print("3. Choose specific number of accounts")
    print("4. Use single account")

    choice = input("Choose option (1-4): ").strip()
    if choice == "1":
        selected_accounts = available_accounts
    elif choice == "2":
        selected_accounts = available_accounts
    elif choice == "3":
        max_accounts = min(len(available_accounts), 8)
        num_accounts = input(f"Enter number of accounts to use (1-{max_accounts}): ").strip()
        try:
            count = max(1, min(int(num_accounts), max_accounts))
            selected_accounts = available_accounts[:count]
        except ValueError:
            print("Invalid input, using all accounts")
            selected_accounts = available_accounts
    else:
        selected_accounts = available_accounts[:1]

    account_dicts = ParallelAccountManager.get_accounts_by_names(selected_accounts)
    collector = MultiPlatformLinkCollector(parallel_processor=TelegramParallelProcessor())
    await collector.collect_all_multi_platform_links()


if __name__ == "__main__":
    asyncio.run(main())
