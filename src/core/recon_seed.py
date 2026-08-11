from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import urlparse

from src.core.recon import queue_recon_target


DEFAULT_SOURCE_LIMIT = 25
DEFAULT_TOTAL_LIMIT = 200


async def _table_exists(conn, table: str) -> bool:
    return bool(await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", table))


def _source_list(sources: list[str] | None) -> list[str]:
    return [source.strip().lower() for source in (sources or []) if source.strip()]


def _target_host(target_value: str) -> str | None:
    parsed = urlparse(target_value if "://" in target_value else f"//{target_value}")
    return parsed.hostname


def _sample_preview(row: dict[str, Any]) -> dict[str, Any]:
    preview = {key: value for key, value in row.items() if key not in {"target_value", "source_record_id"}}
    target_value = str(row.get("target_value") or "")
    preview["target_hash"] = hashlib.sha256(target_value.encode("utf-8")).hexdigest()[:12]
    source_record_id = str(row.get("source_record_id") or "")
    if source_record_id:
        preview["source_record_hash"] = hashlib.sha256(source_record_id.encode("utf-8")).hexdigest()[:12]
    if row.get("target_type") in {"domain", "url", "email"}:
        preview["target_host"] = _target_host(target_value)
    return preview


async def seed_recon_targets_from_collector(
    conn,
    *,
    sources: list[str] | None = None,
    include_domains: bool = True,
    include_urls: bool = False,
    include_usernames: bool = True,
    per_source_limit: int = DEFAULT_SOURCE_LIMIT,
    total_limit: int = DEFAULT_TOTAL_LIMIT,
    priority: int = 7,
    dry_run: bool = False,
) -> dict[str, Any]:
    source_filter = _source_list(sources)
    per_source_limit = max(1, per_source_limit)
    total_limit = max(1, total_limit)
    candidates: list[dict[str, Any]] = []

    if include_domains and await _table_exists(conn, "discovered_links"):
        rows = await conn.fetch(
            """
            WITH ranked AS (
                SELECT id::text AS source_record_id,
                       source AS collector_source,
                       NULLIF(domain, '') AS target_value,
                       COALESCE(discovered_at, fetched_at) AS seen_at,
                       row_number() OVER (
                           PARTITION BY source
                           ORDER BY COALESCE(discovered_at, fetched_at) DESC NULLS LAST, id DESC
                       ) AS rn
                FROM discovered_links
                WHERE NULLIF(domain, '') IS NOT NULL
                  AND ($1::text[] = '{}'::text[] OR source = ANY($1::text[]))
            )
            SELECT *
            FROM ranked
            WHERE rn <= $2
            ORDER BY seen_at DESC NULLS LAST
            LIMIT $3
            """,
            source_filter,
            per_source_limit,
            total_limit,
        )
        for row in rows:
            candidates.append({
                "target_type": "domain",
                "target_value": row["target_value"],
                "collector_source": row["collector_source"],
                "source_table": "discovered_links",
                "source_record_id": row["source_record_id"],
                "seen_at": row["seen_at"],
            })

    if include_urls and await _table_exists(conn, "discovered_links"):
        rows = await conn.fetch(
            """
            WITH ranked AS (
                SELECT id::text AS source_record_id,
                       source AS collector_source,
                       NULLIF(url, '') AS target_value,
                       COALESCE(discovered_at, fetched_at) AS seen_at,
                       row_number() OVER (
                           PARTITION BY source
                           ORDER BY COALESCE(discovered_at, fetched_at) DESC NULLS LAST, id DESC
                       ) AS rn
                FROM discovered_links
                WHERE NULLIF(url, '') IS NOT NULL
                  AND ($1::text[] = '{}'::text[] OR source = ANY($1::text[]))
            )
            SELECT *
            FROM ranked
            WHERE rn <= $2
            ORDER BY seen_at DESC NULLS LAST
            LIMIT $3
            """,
            source_filter,
            per_source_limit,
            total_limit,
        )
        for row in rows:
            candidates.append({
                "target_type": "url",
                "target_value": row["target_value"],
                "collector_source": row["collector_source"],
                "source_table": "discovered_links",
                "source_record_id": row["source_record_id"],
                "seen_at": row["seen_at"],
            })

    if include_usernames and await _table_exists(conn, "social_users"):
        rows = await conn.fetch(
            """
            WITH ranked AS (
                SELECT CONCAT(platform, ':', COALESCE(uid, platform_user_id, username)) AS source_record_id,
                       platform AS collector_source,
                       NULLIF(username, '') AS target_value,
                       COALESCE(last_seen, first_seen) AS seen_at,
                       row_number() OVER (
                           PARTITION BY platform
                           ORDER BY COALESCE(last_seen, first_seen) DESC NULLS LAST, username
                       ) AS rn
                FROM social_users
                WHERE NULLIF(username, '') IS NOT NULL
                  AND ($1::text[] = '{}'::text[] OR platform = ANY($1::text[]))
            )
            SELECT *
            FROM ranked
            WHERE rn <= $2
            ORDER BY seen_at DESC NULLS LAST
            LIMIT $3
            """,
            source_filter,
            per_source_limit,
            total_limit,
        )
        for row in rows:
            candidates.append({
                "target_type": "username",
                "target_value": row["target_value"],
                "collector_source": row["collector_source"],
                "source_table": "social_users",
                "source_record_id": row["source_record_id"],
                "seen_at": row["seen_at"],
            })

    candidates = candidates[:total_limit]
    if dry_run:
        return {
            "dry_run": True,
            "candidates": len(candidates),
            "queued": 0,
            "sources": sorted({str(row["collector_source"]) for row in candidates if row.get("collector_source")}),
            "types": {
                target_type: sum(1 for row in candidates if row["target_type"] == target_type)
                for target_type in ("domain", "url", "username")
            },
            "sample": [_sample_preview(row) for row in candidates[:10]],
        }

    queued = 0
    skipped = 0
    for row in candidates:
        try:
            await queue_recon_target(
                conn,
                target_type=row["target_type"],
                target_value=row["target_value"],
                source=f"collector:{row['source_table']}",
                priority=priority,
                scope={
                    "collector_derived": True,
                    "collector_source": row["collector_source"],
                    "source_table": row["source_table"],
                    "source_record_id": row["source_record_id"],
                    "seen_at": row["seen_at"].isoformat() if row.get("seen_at") else None,
                },
            )
            queued += 1
        except ValueError:
            skipped += 1

    return {
        "dry_run": False,
        "candidates": len(candidates),
        "queued": queued,
        "skipped": skipped,
        "sources": sorted({str(row["collector_source"]) for row in candidates if row.get("collector_source")}),
        "types": {
            target_type: sum(1 for row in candidates if row["target_type"] == target_type)
            for target_type in ("domain", "url", "username")
        },
    }
