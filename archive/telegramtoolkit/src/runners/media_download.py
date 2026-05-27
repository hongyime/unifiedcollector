#!/usr/bin/env python3
"""
Standalone runner for processor-backed media downloads.
"""

from __future__ import annotations

import asyncio
import os
from typing import Optional

from src.core.dynamic_config import get_accounts
from src.core.feature_registry import build_processors
from src.core.login_verifier import verify_accounts_for_feature
from src.core.message_orchestrator import MessageOrchestrator


def prompt_for_download_path(context=None):
    """Prompt for a download destination with a default fallback."""
    default = "downloads"
    label = context or "Telegram media"
    path = input(f"Enter download path for {label} (default: {default}): ").strip()
    return path or default


class MediaDownloadRunner:
    """Run media downloads through the unified processor/orchestrator stack."""

    def __init__(self, save_path: str = "downloads"):
        self.save_path = save_path

    def build_orchestrator(self) -> MessageOrchestrator:
        orchestrator = MessageOrchestrator()
        for processor in build_processors(
            ["media"],
            runtime_options_by_key={"media": {"save_path": self.save_path}},
        ):
            orchestrator.register_processor(processor)
        return orchestrator

    async def run(self, accounts: Optional[list[dict]] = None) -> None:
        """Run the consolidated media-download workflow."""
        selected_accounts = accounts or get_accounts()
        if not selected_accounts:
            print("❌ No accounts configured")
            return

        os.makedirs(self.save_path, exist_ok=True)
        print(f"✨ Running processor-backed media download: {os.path.abspath(self.save_path)}")
        orchestrator = self.build_orchestrator()
        await orchestrator.run(selected_accounts)


async def main() -> None:
    """Standalone entry point."""
    all_accounts = get_accounts()
    if not all_accounts:
        print("❌ Error: No accounts configured!")
        print("💡 Please add accounts using the Account Manager in the main menu.")
        return

    print("\n📋 Media Download Setup")
    print(f"Found {len(all_accounts)} configured accounts")

    print("\n🔐 Verifying account logins before starting...")
    _, valid_accounts = await verify_accounts_for_feature("Media Download", None)
    if not valid_accounts:
        print("❌ No valid accounts available. Please check your sessions.")
        return

    print(f"✅ {len(valid_accounts)} accounts verified and ready!")
    download_path = input("Enter download directory path (default: downloads): ").strip() or "downloads"
    download_path = os.path.abspath(os.path.expanduser(download_path))
    await MediaDownloadRunner(download_path).run(valid_accounts)


if __name__ == "__main__":
    asyncio.run(main())
