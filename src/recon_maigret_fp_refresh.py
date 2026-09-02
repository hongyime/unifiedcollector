"""One-shot / cron CLI to refresh the maigret false-positive-site blocklist.

Runs maigret HTTP-only against N random 16-char alphanumeric CONTROL usernames
(default 5) and stores the union of their Claimed sitenames in
``recon_maigret_fp_sites``. The recon worker consults that table on every real
target to drop universal-200 FP sites (xvideos, roblox, op.gg, twitchtracker,
...) from observations.

Backfill-friendly by design: real targets run maigret exactly ONCE and consult
the CACHED blocklist. Refresh is amortized across many targets and only needs
to be re-run periodically (default TTL 7 days).

Usage (inside the recon container, which has ``maigret`` + ``asyncpg`` on PATH):
    docker exec unifiedcollector_spiderfoot \\
        python -m src.recon_maigret_fp_refresh --force --controls 5

Flags:
    --controls N   Number of random control usernames to run (default 5).
    --force        TRUNCATE the table before repopulating (fresh rebuild).
                   Also acquires the advisory lock in BLOCKING mode so it
                   won't silently skip if another refresh is running.
    --json         Emit the summary as pretty JSON (default: True).

Safety:
    * HTTP-only: no --cloudflare-bypass, no browser.
    * Controls run SEQUENTIALLY inside this CLI so peak RAM stays at ~1x a
      single maigret process even with N=5. (The recon worker separately
      caps its own subprocess concurrency.)
    * Serialized across workers via ``pg_advisory_lock`` on a fixed key.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from src.core.recon_spiderfoot import (
    _ensure_maigret_fp_table,
    refresh_maigret_fp_blocklist,
)
from src.db.connection import close_pool, get_pool

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)


async def _run(controls: int, force: bool) -> dict:
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            await _ensure_maigret_fp_table(conn)
            summary = await refresh_maigret_fp_blocklist(
                conn,
                num_controls=controls,
                worker_label="fp-refresh",
                force=force,
            )
            # Attach post-refresh table stats so operators can see what happened
            # without a second SQL round-trip.
            size = await conn.fetchval("SELECT COUNT(*) FROM recon_maigret_fp_sites")
            summary["blocklist_size_after"] = int(size or 0)
    finally:
        await close_pool()
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Refresh the maigret false-positive-site blocklist"
    )
    parser.add_argument(
        "--controls",
        type=int,
        default=5,
        help="Number of random controls (default: 5).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="TRUNCATE the table before repopulating; blocking-lock semantics.",
    )
    args = parser.parse_args()
    summary = asyncio.run(_run(args.controls, args.force))
    print(json.dumps(summary, indent=2, sort_keys=True, default=str), flush=True)


if __name__ == "__main__":
    main()
