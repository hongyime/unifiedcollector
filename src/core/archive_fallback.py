"""Archive-fallback pipeline (Explore #3).

Drains ``archive_fallback_queue``: for each pending row, looks up the
closest Wayback Machine snapshot of ``target_url`` and stores it into
``archive_snapshots`` (gzipped raw HTML + metadata). Marks the queue row
done / not_found / error.

**Enable** with ``ARCHIVE_FALLBACK_ENABLED=1``. Default DISABLED.

Griffin's Telegram OSINT posts (2022-06-24 / 2022-08-03) document the
archive-fallback technique: when a channel goes dark or a message is
deleted, Wayback often has a snapshot.

v1 scope: Wayback only. archive.today has cloudflare challenges we
don't want to solve; Google Cache is dying.
"""
from __future__ import annotations

import asyncio
import logging
import os

import httpx

from src.core.wayback_client import lookup as wayback_lookup

logger = logging.getLogger(__name__)

_ENABLED = "ARCHIVE_FALLBACK_ENABLED"
_BATCH = int(os.getenv("ARCHIVE_FALLBACK_BATCH", "10"))
_QPS = float(os.getenv("ARCHIVE_FALLBACK_QPS", "1"))
_DAILY_CAP = int(os.getenv("ARCHIVE_FALLBACK_DAILY_CAP", "500"))


def _is_enabled() -> bool:
    return os.getenv(_ENABLED, "0") == "1"


async def enqueue(pool, source_table: str, source_record_id: str, target_url: str) -> None:
    """Callers (collectors that just observed a 404) put targets here."""
    if not target_url:
        return
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO archive_fallback_queue
                (source_table, source_record_id, target_url)
            VALUES ($1, $2, $3)
            ON CONFLICT (source_table, source_record_id, target_url) DO NOTHING
            """,
            source_table,
            source_record_id,
            target_url,
        )


async def _daily_count(pool) -> int:
    async with pool.acquire() as conn:
        val = await conn.fetchval(
            "SELECT count(*) FROM archive_snapshots "
            "WHERE fetched_at > now() - interval '24 hours' "
            "AND archive_service = 'wayback'"
        )
    return int(val or 0)


async def run_archive_fallback(pool) -> dict:
    summary = {"skipped": None, "processed": 0, "found": 0,
               "not_found": 0, "errors": 0, "daily_cap_hit": False}
    if not _is_enabled():
        summary["skipped"] = "disabled"
        return summary

    daily = await _daily_count(pool)
    if daily >= _DAILY_CAP:
        summary["skipped"] = "daily_cap"
        summary["daily_cap_hit"] = True
        return summary

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, source_table, source_record_id, target_url
            FROM archive_fallback_queue
            WHERE status = 'pending'
            ORDER BY added_at ASC
            LIMIT $1
            """,
            _BATCH,
        )

    if not rows:
        return summary

    interval = 1.0 / max(_QPS, 0.01)
    headers = {
        "User-Agent": "Mozilla/5.0 (unifiedcollector-osint) wayback-fallback",
        "Accept": "*/*",
    }
    async with httpx.AsyncClient(headers=headers) as client:
        for row in rows:
            summary["processed"] += 1
            queue_id = row["id"]
            snap = await wayback_lookup(client, row["target_url"])

            if snap.error == "rate_limited":
                logger.warning("archive_fallback: rate-limited, bailing")
                break

            if snap.error == "no_snapshot":
                summary["not_found"] += 1
                async with pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE archive_fallback_queue "
                        "SET status='not_found', processed_at=now() WHERE id=$1",
                        queue_id,
                    )
                await asyncio.sleep(interval)
                continue

            if snap.error:
                summary["errors"] += 1
                async with pool.acquire() as conn:
                    await conn.execute(
                        """
                        UPDATE archive_fallback_queue
                        SET status='error', processed_at=now(), error_message=$2
                        WHERE id=$1
                        """,
                        queue_id,
                        snap.error[:200],
                    )
                await asyncio.sleep(interval)
                continue

            # Success - persist snapshot.
            summary["found"] += 1
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO archive_snapshots
                        (source_table, source_record_id, archive_service,
                         snapshot_url, snapshot_captured_at,
                         content_hash, raw_html_gzip)
                    VALUES ($1, $2, 'wayback', $3, $4, $5, $6)
                    ON CONFLICT DO NOTHING
                    """,
                    row["source_table"],
                    row["source_record_id"],
                    snap.snapshot_url,
                    snap.snapshot_captured_at,
                    snap.content_hash,
                    snap.raw_html_gzip,
                )
                await conn.execute(
                    "UPDATE archive_fallback_queue "
                    "SET status='done', processed_at=now() WHERE id=$1",
                    queue_id,
                )
            await asyncio.sleep(interval)

    logger.info("archive_fallback: %s", summary)
    return summary


__all__ = ["run_archive_fallback", "enqueue"]
