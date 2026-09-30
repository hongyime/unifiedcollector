"""One-shot backfill of tiktok_posts.id_decoded_utc from platform_post_id.

Bounded batches, sleep between batches so we don't spike the shared 8GB VM.
Safe to re-run: only updates rows where id_decoded_utc IS NULL.

Ref: Do Now #5 spec, decoder module src/collectors/tiktok/decoder.py.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import time
from typing import Optional

import asyncpg

from src.collectors.tiktok.decoder import tiktok_id_to_utc

logger = logging.getLogger("backfill_tiktok_id_utc")


async def _process_batch(conn: asyncpg.Connection, batch_size: int) -> int:
    rows = await conn.fetch(
        """
        SELECT id, platform_post_id
        FROM tiktok_posts
        WHERE id_decoded_utc IS NULL
          AND platform_post_id IS NOT NULL
        LIMIT $1
        """,
        batch_size,
    )
    if not rows:
        return 0

    updates: list[tuple] = []
    for row in rows:
        decoded = tiktok_id_to_utc(row["platform_post_id"])
        if decoded is not None:
            updates.append((decoded, row["id"]))

    if updates:
        await conn.executemany(
            "UPDATE tiktok_posts SET id_decoded_utc = $1 WHERE id = $2",
            updates,
        )
    return len(rows)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=10_000)
    parser.add_argument("--sleep-ms", type=int, default=100)
    parser.add_argument("--max-rows", type=int, default=0, help="0 = all")
    parser.add_argument("--dsn", default=os.getenv("DATABASE_URL"))
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not args.dsn:
        raise SystemExit("DATABASE_URL not set and --dsn not provided")

    conn = await asyncpg.connect(args.dsn)
    try:
        total = 0
        started = time.monotonic()
        while True:
            processed = await _process_batch(conn, args.batch_size)
            total += processed
            if processed == 0:
                break
            elapsed = time.monotonic() - started
            logger.info("backfill: processed=%d total=%d elapsed=%.1fs", processed, total, elapsed)
            if args.max_rows and total >= args.max_rows:
                break
            await asyncio.sleep(args.sleep_ms / 1000.0)

        # Discrepancy audit: median |created_at - id_decoded_utc|.
        row = await conn.fetchrow(
            """
            SELECT
              count(*)                                          AS n,
              percentile_cont(0.5) WITHIN GROUP (
                ORDER BY EXTRACT(EPOCH FROM ABS(created_at - id_decoded_utc))
              )                                                 AS median_abs_seconds
            FROM tiktok_posts
            WHERE created_at IS NOT NULL AND id_decoded_utc IS NOT NULL
            """
        )
        if row and row["n"]:
            logger.info(
                "audit: n=%d median|created_at - id_decoded_utc|=%.1fs",
                row["n"],
                row["median_abs_seconds"] or 0.0,
            )
        else:
            logger.info("audit: no rows have both created_at and id_decoded_utc yet")

        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
