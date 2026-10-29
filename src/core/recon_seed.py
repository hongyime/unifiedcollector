from __future__ import annotations

import hashlib
import os
from typing import Any
from urllib.parse import urlparse

from src.core.recon import normalize_recon_target, queue_recon_target


DEFAULT_SOURCE_LIMIT = 25
DEFAULT_TOTAL_LIMIT = 200
# Site-enumeration modules require operator selection via RECON_USERNAME_MODULES.
DEFAULT_USERNAME_MODULES: tuple[str, ...] = ()


async def _table_exists(conn, table: str) -> bool:
    return bool(await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", table))


def _source_list(sources: list[str] | None) -> list[str]:
    return [source.strip().lower() for source in (sources or []) if source.strip()]


def _target_host(target_value: str) -> str | None:
    parsed = urlparse(target_value if "://" in target_value else f"//{target_value}")
    return parsed.hostname


def _sample_preview(row: dict[str, Any]) -> dict[str, Any]:
    preview = {key: value for key, value in row.items() if key not in {"target_value", "source_record_id", "source_evidence"}}
    target_value = str(row.get("target_value") or "")
    preview["target_hash"] = hashlib.sha256(target_value.encode("utf-8")).hexdigest()[:12]
    source_record_id = str(row.get("source_record_id") or "")
    if source_record_id:
        preview["source_record_hash"] = hashlib.sha256(source_record_id.encode("utf-8")).hexdigest()[:12]
    if row.get("source_evidence"):
        preview["source_evidence"] = [
            {
                "collector_source": item.get("collector_source"),
                "source_table": item.get("source_table"),
                "source_record_hash": hashlib.sha256(
                    str(item.get("source_record_id") or "").encode("utf-8")
                ).hexdigest()[:12],
            }
            for item in row["source_evidence"]
        ]
    if row.get("target_type") in {"domain", "url", "email"}:
        preview["target_host"] = _target_host(target_value)
    return preview


def _module_scope_for(row: dict[str, Any]) -> list[str] | None:
    if row.get("target_type") != "username":
        return None
    raw = os.getenv("RECON_USERNAME_MODULES", ",".join(DEFAULT_USERNAME_MODULES))
    modules = [item.strip() for item in raw.split(",") if item.strip()]
    return modules or None


def _source_selected(source_filter: list[str], *names: str) -> bool:
    return not source_filter or any(name.lower() in source_filter for name in names)


async def _append_source_rows(
    conn,
    candidates: list[dict[str, Any]],
    *,
    table: str,
    query: str,
    target_type: str,
    per_source_limit: int,
    collector_sources: tuple[str, ...],
    source_filter: list[str],
) -> None:
    if not _source_selected(source_filter, *collector_sources) or not await _table_exists(conn, table):
        return
    rows = await conn.fetch(query, per_source_limit)
    for row in rows:
        value = str(row.get("target_value") or "").strip()
        if not value:
            continue
        candidates.append({
            "target_type": target_type,
            "target_value": value,
            "collector_source": str(row.get("collector_source") or collector_sources[0]),
            "source_table": table,
            "source_record_id": str(row.get("source_record_id") or ""),
            "seen_at": row.get("seen_at"),
            **({"platform": row["platform"]} if row.get("platform") else {}),
        })


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
    approved_source_identities: list[str] | None = None,
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

    # Durable source snapshots. These tables are written by the Website,
    # Search, Exposure, native-platform, and shared collector target paths.
    # They are only read here; recon_targets remains the visible work queue.
    if include_domains:
        await _append_source_rows(
            conn, candidates, table="website_targets", target_type="domain",
            collector_sources=("website",), source_filter=source_filter,
            per_source_limit=per_source_limit,
            query="""
                SELECT id::text AS source_record_id, 'website' AS collector_source,
                       NULLIF(domain, '') AS target_value,
                       COALESCE(updated_at, collected_at) AS seen_at
                FROM website_targets WHERE NULLIF(domain, '') IS NOT NULL
                ORDER BY COALESCE(updated_at, collected_at) DESC NULLS LAST LIMIT $1
            """,
        )
        await _append_source_rows(
            conn, candidates, table="search_results", target_type="domain",
            collector_sources=("search", "waterfall"), source_filter=source_filter,
            per_source_limit=per_source_limit,
            query="""
                SELECT id::text AS source_record_id, 'search' AS collector_source,
                       NULLIF(domain, '') AS target_value, collected_at AS seen_at
                FROM search_results WHERE NULLIF(domain, '') IS NOT NULL
                ORDER BY collected_at DESC NULLS LAST LIMIT $1
            """,
        )
        await _append_source_rows(
            conn, candidates, table="exposure_findings", target_type="domain",
            collector_sources=("exposure",), source_filter=source_filter,
            per_source_limit=per_source_limit,
            query="""
                SELECT id::text AS source_record_id, 'exposure' AS collector_source,
                       NULLIF(domain, '') AS target_value, collected_at AS seen_at
                FROM exposure_findings WHERE NULLIF(domain, '') IS NOT NULL
                ORDER BY collected_at DESC NULLS LAST LIMIT $1
            """,
        )

    if include_usernames:
        await _append_source_rows(
            conn, candidates, table="browser_native_scrape_queue", target_type="username",
            collector_sources=("pinterest", "snapchat", "airbnb"), source_filter=source_filter,
            per_source_limit=per_source_limit,
            query="""
                SELECT id::text AS source_record_id, platform AS collector_source,
                       platform, NULLIF(username, '') AS target_value,
                       updated_at AS seen_at
                FROM browser_native_scrape_queue
                WHERE NULLIF(username, '') IS NOT NULL
                ORDER BY updated_at DESC NULLS LAST, id DESC LIMIT $1
            """,
        )

    # Existing collection_targets cover collector queues whose resolved
    # identities have not yet produced a social_users/discovered_links row.
    if include_usernames and await _table_exists(conn, "collection_targets"):
        rows = await conn.fetch(
            """
            SELECT id::text AS source_record_id, source AS collector_source,
                   target_type, NULLIF(target_id, '') AS target_value, created_at AS seen_at
            FROM collection_targets
            WHERE target_type IN ('user', 'username', 'channel', 'domain')
              AND NULLIF(target_id, '') IS NOT NULL
              AND ($2::text[] = '{}'::text[] OR source = ANY($2::text[]))
            ORDER BY COALESCE(last_collection_at, created_at) DESC NULLS LAST, id DESC
            LIMIT $1
            """,
            per_source_limit,
            source_filter,
        )
        for row in rows:
            raw_type = str(row["target_type"] or "username").lower()
            candidates.append({
                "target_type": raw_type,
                "target_value": row["target_value"],
                "collector_source": row["collector_source"],
                "source_table": "collection_targets",
                "source_record_id": row["source_record_id"],
                "seen_at": row["seen_at"],
            })

    if approved_source_identities is not None:
        approved_sources = set(approved_source_identities)
        candidates = [
            row for row in candidates
            if f"collector:{row.get('source_table')}" in approved_sources
        ]

    # Collapse duplicate identities from overlapping sources before admission.
    # Keep all provenance references so later seed cycles remain auditable.
    deduped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in candidates:
        try:
            identity = normalize_recon_target(row["target_type"], row["target_value"])
        except ValueError:
            continue
        row["target_type"], row["target_value"] = identity
        existing = deduped.get(identity)
        provenance = {
            "collector_source": row.get("collector_source"),
            "source_table": row.get("source_table"),
            "source_record_id": row.get("source_record_id"),
        }
        if existing is None:
            row["source_evidence"] = [provenance]
            deduped[identity] = row
        elif provenance not in existing["source_evidence"]:
            existing["source_evidence"].append(provenance)
    # Rotate across source tables before applying the total cap so a large
    # discovered_links/social_users backlog cannot starve newer durable feeds.
    by_table: dict[str, list[dict[str, Any]]] = {}
    for row in deduped.values():
        by_table.setdefault(str(row.get("source_table") or "other"), []).append(row)
    fair_candidates: list[dict[str, Any]] = []
    depth = 0
    while len(fair_candidates) < total_limit:
        added = False
        for rows in by_table.values():
            if depth < len(rows):
                fair_candidates.append(rows[depth])
                added = True
                if len(fair_candidates) >= total_limit:
                    break
        if not added:
            break
        depth += 1
    candidates = fair_candidates
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
    queued_by_source: dict[str, int] = {}
    for row in candidates:
        modules = _module_scope_for(row)
        scope = {
            "collector_derived": True,
            "collector_source": row["collector_source"],
            "source_table": row["source_table"],
            "source_record_id": row["source_record_id"],
            "seen_at": row["seen_at"].isoformat() if row.get("seen_at") else None,
            "source_evidence": row.get("source_evidence", []),
        }
        if row.get("platform"):
            scope["platform"] = row["platform"]
        if modules:
            scope["modules"] = modules
        try:
            result = await queue_recon_target(
                conn,
                target_type=row["target_type"],
                target_value=row["target_value"],
                source=f"collector:{row['source_table']}",
                priority=priority,
                scope=scope,
                reset_existing=False,
            )
            inserted = bool(result.get("inserted"))
            queued += int(inserted)
            if inserted:
                queued_by_source[row["source_table"]] = queued_by_source.get(row["source_table"], 0) + 1
        except ValueError:
            skipped += 1

    return {
        "dry_run": False,
        "candidates": len(candidates),
        "queued": queued,
        "skipped": skipped,
        "queued_by_source": queued_by_source,
        "sources": sorted({str(row["collector_source"]) for row in candidates if row.get("collector_source")}),
        "types": {
            target_type: sum(1 for row in candidates if row["target_type"] == target_type)
            for target_type in ("domain", "url", "username")
        },
    }


# ---------------------------------------------------------------------------
# Email seeding (ghunt engine)
# ---------------------------------------------------------------------------
# Emails are enriched via GHunt (email -> Google account). We ONLY seed
# @gmail.com / @googlemail.com addresses because ghunt only understands Google
# accounts; other domains would just fail every lookup and waste creds. Seeded
# rows carry scope.modules=['ghunt'] so the recon worker routes them to the
# ghunt handler (recon_spiderfoot._ghunt_selected).
GHUNT_ENGINE = "ghunt"
_GOOGLE_EMAIL_SQL = "(LOWER(%s) LIKE '%%@gmail.com' OR LOWER(%s) LIKE '%%@googlemail.com')"


async def _seed_from_github_users(conn, per_source_limit: int) -> list[dict[str, Any]]:
    """Pull @gmail/@googlemail addresses from github_users (public API-supplied)."""
    if not await _table_exists(conn, "github_users"):
        return []
    rows = await conn.fetch(
        """
        SELECT id::text AS source_record_id,
               'github' AS collector_source,
               LOWER(email) AS target_value,
               COALESCE(collected_at, NOW()) AS seen_at
        FROM github_users
        WHERE email IS NOT NULL
          AND (LOWER(email) LIKE '%@gmail.com' OR LOWER(email) LIKE '%@googlemail.com')
          AND email !~ '\\+.*@'  -- skip plus-addressed variants for the pilot
        ORDER BY collected_at DESC NULLS LAST
        LIMIT $1
        """,
        per_source_limit,
    )
    return [
        {
            "target_type": "email",
            "target_value": row["target_value"],
            "collector_source": row["collector_source"],
            "source_table": "github_users",
            "source_record_id": row["source_record_id"],
            "seen_at": row["seen_at"],
        }
        for row in rows
    ]


async def _seed_from_github_commits(conn, per_source_limit: int) -> list[dict[str, Any]]:
    """Pull @gmail/@googlemail addresses from github_commits.author_email.

    github_commits has ~8M rows; unbounded aggregation exceeds our timeouts.
    We use a small LIMIT + ORDER BY date DESC (indexed) and dedupe in-python.
    Bounded set is fine — the scheduler runs this repeatedly."""
    if not await _table_exists(conn, "github_commits"):
        return []
    # Cap the raw scan at 10x per_source_limit so we can dedupe in-python without
    # blowing a huge window through Postgres. The date column is indexed.
    scan_limit = max(per_source_limit * 10, 50)
    rows = await conn.fetch(
        """
        SELECT sha AS source_record_id,
               'github' AS collector_source,
               LOWER(author_email) AS target_value,
               date AS seen_at
        FROM github_commits
        WHERE author_email IS NOT NULL
          AND (LOWER(author_email) LIKE '%@gmail.com' OR LOWER(author_email) LIKE '%@googlemail.com')
          AND author_email NOT ILIKE '%noreply%'
          AND author_email NOT ILIKE '%users.noreply%'
          AND author_email !~ '\\+.*@'
        ORDER BY date DESC NULLS LAST
        LIMIT $1
        """,
        scan_limit,
    )
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        email = str(row["target_value"]).strip().lower()
        if not email or email in seen:
            continue
        seen.add(email)
        out.append({
            "target_type": "email",
            "target_value": email,
            "collector_source": row["collector_source"],
            "source_table": "github_commits",
            "source_record_id": str(row["source_record_id"]),
            "seen_at": row["seen_at"],
        })
        if len(out) >= per_source_limit:
            break
    return out


async def seed_email_targets_for_ghunt(
    conn,
    *,
    per_source_limit: int = 25,
    total_limit: int = 50,
    priority: int = 7,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Seed recon_targets(target_type='email', scope.modules=['ghunt']) from Gmail-ish
    addresses across the collector's tables.

    Idempotent: queue_recon_target de-duplicates via (target_type, target_value)
    on the DB side (see src/core/recon.py) so re-running is safe. Only fields
    tagged @gmail.com / @googlemail.com are pulled — ghunt on non-Google
    addresses is a wasted lookup."""
    per_source_limit = max(1, per_source_limit)
    total_limit = max(1, total_limit)
    candidates: list[dict[str, Any]] = []
    candidates.extend(await _seed_from_github_users(conn, per_source_limit))
    candidates.extend(await _seed_from_github_commits(conn, per_source_limit))

    # Dedupe across sources (github_users email may also appear in commits).
    deduped: dict[str, dict[str, Any]] = {}
    for row in candidates:
        key = str(row["target_value"]).strip().lower()
        if key and key not in deduped:
            deduped[key] = row
    candidates = list(deduped.values())[:total_limit]

    if dry_run:
        return {
            "dry_run": True,
            "candidates": len(candidates),
            "queued": 0,
            "sample": [_sample_preview(row) for row in candidates[:10]],
        }

    queued = 0
    skipped = 0
    for row in candidates:
        scope = {
            "collector_derived": True,
            "collector_source": row["collector_source"],
            "source_table": row["source_table"],
            "source_record_id": row["source_record_id"],
            "seen_at": row["seen_at"].isoformat() if row.get("seen_at") else None,
            "modules": [GHUNT_ENGINE],
        }
        try:
            await queue_recon_target(
                conn,
                target_type="email",
                target_value=row["target_value"],
                source=f"collector:{row['source_table']}",
                priority=priority,
                scope=scope,
            )
            queued += 1
        except ValueError:
            skipped += 1
    return {
        "dry_run": False,
        "candidates": len(candidates),
        "queued": queued,
        "skipped": skipped,
        "engine": GHUNT_ENGINE,
    }
