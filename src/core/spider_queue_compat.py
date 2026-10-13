"""Bounded shadow synchronization for source-specific spider queues.

The platform collectors continue claiming their existing queues. This module
copies a small keyset page into ``spider_queue_legacy_shadow`` so operators can
compare status distributions before any consumer is moved to ``spider_queue``.
It never calls a platform API or writes to a legacy queue.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class LegacyQueueSpec:
    platform: str
    table: str
    node_field: str
    record_id_is_uuid: bool = True
    record_id_field: str = "id"
    target_type_field: str | None = None
    metadata_fields: tuple[str, ...] = ("source",)
    hop_field: str | None = None


LEGACY_QUEUE_SPECS: dict[str, LegacyQueueSpec] = {
    "instagram": LegacyQueueSpec(
        "instagram", "instagram_spider_queue", "platform_user_id",
        metadata_fields=("source", "source_post_id", "username"),
    ),
    "instagram_targets": LegacyQueueSpec(
        "instagram", "instagram_spider_targets", "username",
        record_id_is_uuid=False,
        record_id_field="username",
        metadata_fields=("discovered_from", "follower_count"),
        hop_field="hop",
    ),
    "tiktok": LegacyQueueSpec(
        "tiktok", "tiktok_spider_queue", "platform_user_id",
        metadata_fields=("source", "source_post_id", "username"),
    ),
    "github": LegacyQueueSpec(
        "github", "github_spider_queue", "target_identifier",
        target_type_field="target_type",
        metadata_fields=("source", "target_type"),
    ),
    "strava": LegacyQueueSpec(
        "strava", "strava_spider_queue", "platform_athlete_id",
        metadata_fields=("source", "source_activity_id"),
    ),
    "lemon8": LegacyQueueSpec(
        "lemon8", "lemon8_spider_queue", "platform_user_id",
    ),
    "youtube": LegacyQueueSpec(
        "youtube", "youtube_spider_queue", "platform_channel_id",
    ),
    "x": LegacyQueueSpec(
        "x", "x_profile_targets", "username",
        record_id_is_uuid=False,
        record_id_field="username",
        metadata_fields=("source", "metadata"),
    ),
    "telegram": LegacyQueueSpec(
        "telegram", "telegram_spider_queue", "platform_chat_id",
        metadata_fields=("source", "title"),
    ),
}

_STATUS_ALIASES = {
    "pending": "pending",
    "queued": "pending",
    "active": "pending",
    "processing": "in_progress",
    "in_progress": "in_progress",
    "claimed": "in_progress",
    "completed": "completed",
    "complete": "completed",
    "done": "completed",
    "failed": "failed",
    "error": "failed",
    "unresolvable": "failed",
    "unavailable": "failed",
}


def normalize_legacy_status(status: Any) -> str:
    """Map known legacy vocabularies while retaining unknown values for audit."""
    raw = str(status or "pending").strip().lower()
    return _STATUS_ALIASES.get(raw, "unknown")


def _metadata(spec: LegacyQueueSpec, record: dict[str, Any]) -> dict[str, Any]:
    return {
        field: record[field]
        for field in spec.metadata_fields
        if record.get(field) is not None
    }


def _node_id(spec: LegacyQueueSpec, record: dict[str, Any]) -> str:
    value = record.get(spec.node_field)
    if value is None or not str(value).strip():
        raise ValueError(
            f"{spec.table} record has no {spec.node_field} target identifier"
        )
    node_id = str(value).strip()
    if spec.target_type_field:
        target_type = str(record.get(spec.target_type_field) or "unknown").strip()
        node_id = f"{target_type}:{node_id}"
    return node_id


def _timestamp(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


async def mirror_legacy_queue_batch(
    pool,
    source: str,
    *,
    after_id: str | None = None,
    limit: int = 100,
    generation: str = "legacy",
) -> dict[str, Any]:
    """Copy at most ``limit`` legacy rows into the isolated shadow table.

    Use the returned ``next_cursor`` to continue. ``limit`` is capped at 500 so
    a reconciliation pass cannot accidentally materialize a whole queue.
    """
    if source not in LEGACY_QUEUE_SPECS:
        raise ValueError(f"unknown legacy spider queue: {source}")
    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    if not generation or len(generation) > 128:
        raise ValueError("generation must contain 1 to 128 characters")

    spec = LEGACY_QUEUE_SPECS[source]
    cursor = UUID(after_id) if after_id and spec.record_id_is_uuid else after_id
    async with pool.acquire() as conn:
        if spec.record_id_is_uuid:
            rows = await conn.fetch(
                f"""SELECT q.id::text AS source_record_id, to_jsonb(q)::text AS record
                    FROM {spec.table} q
                    WHERE ($1::uuid IS NULL OR q.id > $1::uuid)
                    ORDER BY q.id ASC
                    LIMIT $2""",
                cursor, limit,
            )
        else:
            rows = await conn.fetch(
                f"""SELECT q.{spec.record_id_field}::text AS source_record_id,
                           to_jsonb(q)::text AS record
                    FROM {spec.table} q
                    WHERE ($1::text IS NULL OR q.{spec.record_id_field}::text > $1::text)
                    ORDER BY q.{spec.record_id_field}::text ASC
                    LIMIT $2""",
                cursor, limit,
            )

        values = []
        status_counts: Counter[str] = Counter()
        for row in rows:
            record = json.loads(row["record"])
            raw_status = str(record.get("status") or "pending").strip().lower()
            status = normalize_legacy_status(raw_status)
            status_counts[status] += 1
            terminal_reason = (
                "unresolvable" if raw_status == "unresolvable"
                else "legacy_failed" if status == "failed"
                else None
            )
            values.append((
                spec.table,
                str(row["source_record_id"]),
                spec.platform,
                _node_id(spec, record),
                raw_status,
                status,
                int(5 if record.get("priority") is None else record["priority"]),
                int(record.get("attempts") or 0),
                _timestamp(record.get("next_attempt_at")),
                record.get("parent_node_id"),
                record.get(spec.hop_field) if spec.hop_field else record.get("hop_distance"),
                record.get("source") or record.get("discovered_from"),
                record.get("edge_type"),
                json.dumps(_metadata(spec, record), default=str),
                terminal_reason,
            ))

        if values:
            await conn.executemany(
                """INSERT INTO spider_queue_legacy_shadow (
                       source_table, source_record_id, platform, node_id,
                       legacy_status, status, priority, attempts,
                       next_attempt_at, parent_node_id, hop_distance,
                       discovery_type, edge_type, source_metadata,
                       terminal_reason, sync_generation, retired_at, synced_at
                   ) VALUES (
                       $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                       $12, $13, $14::jsonb, $15, $16, NULL, NOW()
                   )
                   ON CONFLICT (source_table, source_record_id) DO UPDATE SET
                       platform = EXCLUDED.platform,
                       node_id = EXCLUDED.node_id,
                       legacy_status = EXCLUDED.legacy_status,
                       status = EXCLUDED.status,
                       priority = EXCLUDED.priority,
                       attempts = EXCLUDED.attempts,
                       next_attempt_at = EXCLUDED.next_attempt_at,
                       parent_node_id = EXCLUDED.parent_node_id,
                       hop_distance = EXCLUDED.hop_distance,
                       discovery_type = EXCLUDED.discovery_type,
                       edge_type = EXCLUDED.edge_type,
                       source_metadata = EXCLUDED.source_metadata,
                       terminal_reason = EXCLUDED.terminal_reason,
                       sync_generation = EXCLUDED.sync_generation,
                       retired_at = NULL,
                       synced_at = NOW()""",
                [(*value, generation) for value in values],
            )

    next_cursor = str(rows[-1]["source_record_id"]) if len(rows) == limit else None
    return {
        "source": source,
        "source_table": spec.table,
        "mirrored": len(rows),
        "next_cursor": next_cursor,
        "status_counts": dict(status_counts),
        "generation": generation,
    }


async def _identity_mismatches(
    conn, spec: LegacyQueueSpec, generation: str
) -> tuple[int, int, int]:
    record_id = spec.record_id_field
    missing_from_shadow = await conn.fetchval(
        f"""SELECT COUNT(*)
              FROM {spec.table} q
              LEFT JOIN spider_queue_legacy_shadow s
                ON s.source_table = $1
               AND s.source_record_id = q.{record_id}::text
               AND s.sync_generation = $2
               AND s.retired_at IS NULL
             WHERE s.source_record_id IS NULL""",
        spec.table, generation,
    )
    extra_in_shadow = await conn.fetchval(
        f"""SELECT COUNT(*)
              FROM spider_queue_legacy_shadow s
              LEFT JOIN {spec.table} q
                ON q.{record_id}::text = s.source_record_id
             WHERE s.source_table = $1
               AND s.sync_generation = $2
               AND s.retired_at IS NULL
               AND q.{record_id} IS NULL""",
        spec.table, generation,
    )
    status_mismatches = await conn.fetchval(
        f"""SELECT COUNT(*)
              FROM {spec.table} q
              JOIN spider_queue_legacy_shadow s
                ON s.source_table = $1
               AND s.source_record_id = q.{record_id}::text
               AND s.sync_generation = $2
               AND s.retired_at IS NULL
             WHERE COALESCE(LOWER(BTRIM(q.status)), 'pending') <> s.legacy_status""",
        spec.table, generation,
    )
    return int(missing_from_shadow), int(extra_in_shadow), int(status_mismatches)


async def finalize_legacy_queue_reconciliation(
    pool, source: str, *, generation: str, statement_timeout_ms: int = 10_000
) -> dict[str, Any]:
    """Retire stale shadow rows after validating a complete keyset sweep.

    A partial generation fails identity validation and cannot retire prior
    rows. Start a new generation for each full sweep, page until
    ``next_cursor`` is ``None``, then call this finalizer.
    """
    if source not in LEGACY_QUEUE_SPECS:
        raise ValueError(f"unknown legacy spider queue: {source}")
    if not generation or generation == "legacy" or len(generation) > 128:
        raise ValueError("finalization requires a non-default generation")
    if not 1_000 <= statement_timeout_ms <= 60_000:
        raise ValueError("statement_timeout_ms must be between 1000 and 60000")
    spec = LEGACY_QUEUE_SPECS[source]
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('statement_timeout', $1, true)",
                f"{statement_timeout_ms}ms",
            )
            missing, extra, statuses = await _identity_mismatches(
                conn, spec, generation
            )
            if missing or extra or statuses:
                raise ValueError(
                    "shadow generation does not match the live source rows "
                    f"(missing={missing}, extra={extra}, status_mismatches={statuses})"
                )
            result = await conn.execute(
                """UPDATE spider_queue_legacy_shadow
                      SET retired_at = NOW()
                    WHERE source_table = $1
                      AND sync_generation <> $2
                      AND retired_at IS NULL""",
                spec.table, generation,
            )
    try:
        retired = int(result.split()[-1])
    except (ValueError, IndexError):
        retired = 0
    return {
        "source": source,
        "source_table": spec.table,
        "generation": generation,
        "retired_rows": retired,
    }


async def compare_legacy_queue_statuses(
    pool,
    source: str,
    *,
    generation: str = "legacy",
    statement_timeout_ms: int = 10_000,
) -> dict[str, Any]:
    """Compare source/shadow counts and exact row identity for one generation."""
    if source not in LEGACY_QUEUE_SPECS:
        raise ValueError(f"unknown legacy spider queue: {source}")
    if not generation or len(generation) > 128:
        raise ValueError("generation must contain 1 to 128 characters")
    if not 1_000 <= statement_timeout_ms <= 60_000:
        raise ValueError("statement_timeout_ms must be between 1000 and 60000")
    spec = LEGACY_QUEUE_SPECS[source]
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('statement_timeout', $1, true)",
                f"{statement_timeout_ms}ms",
            )
            source_rows = await conn.fetch(
                f"SELECT status, COUNT(*)::bigint AS count FROM {spec.table} GROUP BY status"
            )
            shadow_rows = await conn.fetch(
                """SELECT status, COUNT(*)::bigint AS count
                   FROM spider_queue_legacy_shadow
                   WHERE source_table = $1 AND sync_generation = $2
                     AND retired_at IS NULL
                   GROUP BY status""",
                spec.table, generation,
            )
            missing, extra, statuses = await _identity_mismatches(
                conn, spec, generation
            )

    expected: Counter[str] = Counter()
    actual: Counter[str] = Counter()
    for row in source_rows:
        expected[normalize_legacy_status(row["status"])] += int(row["count"])
    for row in shadow_rows:
        actual[str(row["status"])] += int(row["count"])
    expected_counts = dict(sorted(expected.items()))
    shadow_counts = dict(sorted(actual.items()))
    return {
        "source": source,
        "source_table": spec.table,
        "generation": generation,
        "legacy_status_counts": expected_counts,
        "shadow_status_counts": shadow_counts,
        "missing_from_shadow": missing,
        "extra_in_shadow": extra,
        "status_mismatches": statuses,
        "in_sync": (
            expected_counts == shadow_counts
            and missing == 0 and extra == 0 and statuses == 0
        ),
    }


__all__ = [
    "LEGACY_QUEUE_SPECS",
    "compare_legacy_queue_statuses",
    "finalize_legacy_queue_reconciliation",
    "mirror_legacy_queue_batch",
    "normalize_legacy_status",
]
