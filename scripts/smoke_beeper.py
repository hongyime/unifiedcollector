"""Smoke test the Beeper Desktop Local API connection from inside the collector container.

Usage (from PowerShell on the host):
    docker exec unifiedcollector_collector python scripts/smoke_beeper.py

Verifies:
  1. /v1/info reachable + token valid
  2. /v1/accounts returns the connected networks
  3. /v1/chats returns at least one chat
  4. One full collect() cycle (accounts + chats only — NOT the message backfill,
     which can take minutes for 2055 chats)
"""

from __future__ import annotations

import asyncio
import os
import sys


async def main() -> int:
    from src.collectors.beeper import BeeperClient, BeeperWriter
    from src.db.connection import get_pool

    print("=" * 60)
    print("Beeper Desktop Local API smoke test")
    print("=" * 60)
    print(f"BEEPER_DESKTOP_API_URL   = {os.environ.get('BEEPER_DESKTOP_API_URL', '(unset)')}")
    token = os.environ.get('BEEPER_DESKTOP_API_TOKEN', '')
    print(f"BEEPER_DESKTOP_API_TOKEN = {token[:12]}…{token[-6:] if len(token) > 18 else ''}")
    print()

    client = BeeperClient()

    print("→ /v1/info")
    info = await client.info()
    app_block = info.get('app', info) if isinstance(info.get('app'), dict) else info
    print(f"  name:     {app_block.get('name') or info.get('name')}")
    print(f"  version:  {app_block.get('version') or info.get('version')}")
    print(f"  bundle:   {app_block.get('bundle_id') or info.get('bundleID') or info.get('bundle_id')}")
    print(f"  remote:   {info.get('remote_access', False)}")
    print()

    print("→ /v1/accounts")
    accs = await client.accounts()
    print(f"  {len(accs)} connected account(s):")
    for a in accs:
        print(f"    - {a.get('network'):20s} ({a.get('accountID')})  status={a.get('status')}")
    print()

    print("→ /v1/chats?limit=3")
    page = await client.chats(limit=3)
    items = page.get('items', []) if isinstance(page, dict) else page
    print(f"  {len(items)} chat(s) sampled:")
    for c in items[:3]:
        title = (c.get('title') or '(no title)')[:40]
        print(f"    - [{c.get('network'):10s}] {title:42s}  type={c.get('type')}")
    print()

    print("→ Persisting accounts + first 3 chats to DB")
    pool = await get_pool()
    writer = BeeperWriter(pool)
    for a in accs:
        await writer.upsert_account(a)
    for c in items[:3]:
        await writer.upsert_chat(c)

    async with pool.acquire() as conn:
        n_accs = await conn.fetchval("SELECT count(*) FROM beeper_shadow_accounts")
        n_chats = await conn.fetchval("SELECT count(*) FROM beeper_shadow_chats")
        n_parts = await conn.fetchval("SELECT count(*) FROM beeper_shadow_participants")
    print(f"  beeper_shadow_accounts     rows = {n_accs}")
    print(f"  beeper_shadow_chats        rows = {n_chats}")
    print(f"  beeper_shadow_participants rows = {n_parts}")

    await client.close()
    print()
    print("✓ Smoke test PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
