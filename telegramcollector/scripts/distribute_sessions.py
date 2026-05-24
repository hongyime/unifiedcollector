#!/usr/bin/env python3
"""CLI script for manually redistributing a session file to all service directories.

Usage:
    python scripts/distribute_sessions.py <phone_number>

Requirements: 9.1, 9.2, 9.3, 9.4, 9.5
"""
import argparse
import asyncio
import sys
import os
from pathlib import Path

# Add workspace root to path so we can import shared modules
sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.config import settings
from services.login_bot.main import sanitise_phone, session_stem_from_phone
from services.login_bot.session_router import SessionRouter


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Distribute a session file to all service directories."
    )
    parser.add_argument("phone", help="Phone number in E.164 format (e.g. +12345678900)")
    args = parser.parse_args()

    # Sanitise and derive stem
    clean = sanitise_phone(args.phone)
    stem = session_stem_from_phone(clean)

    # Locate source file
    source = Path(settings.SESSIONS_BASE_PATH) / "collector" / f"{stem}.session"
    if not source.exists():
        print(f"Error: source session file not found: {source}", file=sys.stderr)
        sys.exit(1)

    # Distribute
    router = SessionRouter(settings.SESSIONS_BASE_PATH)
    succeeded = asyncio.run(router.distribute(stem))

    # Print summary
    # Find all target dirs to report failures too
    target_dirs = [d.name for d in router._list_target_dirs()]
    for dir_name in target_dirs:
        if dir_name in succeeded:
            print(f"  ✓ {dir_name}: copied successfully")
        else:
            print(f"  ✗ {dir_name}: copy failed")

    if succeeded:
        print(f"\nDistributed to {len(succeeded)} director(ies): {', '.join(succeeded)}")
    else:
        print("\nNo directories received the session file.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
