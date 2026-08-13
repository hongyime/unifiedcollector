"""Canonical backlog/progress registry for Collector targets.

The registry is populated from existing Collector fact tables. It does not
replace ``collection_targets`` as the execution queue; it gives ops and Analyzer
one consistent read model for discovered users, channels, domains, and URLs.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)

DEFAULT_LIMIT_PER_SOURCE = 5000
DEFAULT_FRESH_DAYS = 7

STATUS_RANK = {
    "seen": 0,
    "new": 1,
    "pending": 2,
    "skipped": 2,
    "failed": 2,
    "backfilled": 3,
    "stale": 4,
    "fresh": 5,
}


@dataclass(slots=True)
class SeenTarget:
    platform: str
    target_type: str
    target_key: str
    target_display: str | None = None
    origin: str = "collector"
    priority: int = 5
    evidence_count: int = 1
    first_seen_at: Any = None
    last_seen_at: Any = None
    last_backfill_at: Any = None
    next_backfill_at: Any = None
    status: str = "pending"
    source_table: str | None = None
    source_record_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _ProfileSpec:
    platform: str
    table: str
    target_type: str
    key_expr: str
    display_expr: str
    record_id_expr: str
    first_seen_expr: str
    last_seen_expr: str
    evidence_expr: str
    metadata_expr: str = "'{}'::jsonb"
    status_expr: str = "NULL::text"


@dataclass(frozen=True, slots=True)
class _QueueSpec:
    platform: str
    table: str
    target_type: str
    key_expr: str
    display_expr: str
    record_id_expr: str
    status_expr: str
    priority_expr: str = "priority"
    seen_expr: str = "collected_at"
    metadata_expr: str = "'{}'::jsonb"


@dataclass(frozen=True, slots=True)
class _PostAuthorSpec:
    platform: str
    table: str
    author_expr: str = "author_username"
    seen_expr: str = "collected_at"


PROFILE_SPECS: tuple[_ProfileSpec, ...] = (
    _ProfileSpec(
        "instagram",
        "instagram_profiles",
        "user",
        "COALESCE(NULLIF(username, ''), platform_user_id::text)",
        "COALESCE(NULLIF(full_name, ''), NULLIF(username, ''), platform_user_id::text)",
        "id::text",
        "collected_at",
        "COALESCE(updated_at, collected_at)",
        "GREATEST(COALESCE(posts_count, 0), 1)",
        "jsonb_build_object('platform_user_id', platform_user_id, 'is_private', is_private, 'is_verified', is_verified)",
    ),
    _ProfileSpec(
        "threads",
        "threads_profiles",
        "user",
        "COALESCE(NULLIF(username, ''), platform_user_id::text)",
        "COALESCE(NULLIF(full_name, ''), NULLIF(username, ''), platform_user_id::text)",
        "id::text",
        "collected_at",
        "COALESCE(updated_at, collected_at)",
        "1",
        "jsonb_build_object('platform_user_id', platform_user_id)",
    ),
    _ProfileSpec(
        "facebook",
        "facebook_profiles",
        "user",
        "COALESCE(NULLIF(username, ''), platform_user_id::text)",
        "COALESCE(NULLIF(display_name, ''), NULLIF(username, ''), platform_user_id::text)",
        "id::text",
        "collected_at",
        "COALESCE(updated_at, collected_at)",
        "1",
        "jsonb_build_object('platform_user_id', platform_user_id)",
    ),
    _ProfileSpec(
        "x",
        "x_profiles",
        "user",
        "COALESCE(NULLIF(username, ''), platform_user_id::text)",
        "COALESCE(NULLIF(display_name, ''), NULLIF(username, ''), platform_user_id::text)",
        "id::text",
        "collected_at",
        "COALESCE(updated_at, collected_at)",
        "GREATEST(COALESCE(followers_count, 0), 1)",
        "jsonb_build_object('platform_user_id', platform_user_id, 'is_private', is_private, 'is_verified', is_verified)",
    ),
    _ProfileSpec(
        "tiktok",
        "tiktok_profiles",
        "user",
        "COALESCE(NULLIF(username, ''), platform_user_id::text)",
        "COALESCE(NULLIF(nickname, ''), NULLIF(username, ''), platform_user_id::text)",
        "id::text",
        "collected_at",
        "COALESCE(updated_at, collected_at)",
        "GREATEST(COALESCE(video_count, 0), 1)",
        "jsonb_build_object('platform_user_id', platform_user_id, 'is_private', is_private, 'is_verified', is_verified)",
    ),
    _ProfileSpec(
        "lemon8",
        "lemon8_profiles",
        "user",
        "COALESCE(NULLIF(username, ''), platform_user_id::text)",
        "COALESCE(NULLIF(nickname, ''), NULLIF(username, ''), platform_user_id::text)",
        "id::text",
        "collected_at",
        "COALESCE(updated_at, collected_at)",
        "GREATEST(COALESCE(like_count, 0), 1)",
        "jsonb_build_object('platform_user_id', platform_user_id)",
    ),
    _ProfileSpec(
        "github",
        "github_users",
        "user",
        "login",
        "COALESCE(NULLIF(name, ''), login)",
        "id::text",
        "collected_at",
        "collected_at",
        "GREATEST(COALESCE(public_repos_count, 0) + COALESCE(followers_count, 0), 1)",
        "jsonb_build_object('platform_user_id', platform_user_id, 'company', company, 'location', location)",
    ),
    _ProfileSpec(
        "youtube",
        "youtube_channels",
        "channel",
        "COALESCE(NULLIF(custom_url, ''), platform_channel_id)",
        "COALESCE(NULLIF(title, ''), NULLIF(custom_url, ''), platform_channel_id)",
        "id::text",
        "collected_at",
        "COALESCE(updated_at, collected_at)",
        "GREATEST(COALESCE(video_count, 0), 1)",
        "jsonb_build_object('platform_channel_id', platform_channel_id, 'country', country)",
    ),
    _ProfileSpec(
        "telegram",
        "telegram_users",
        "user",
        "COALESCE(NULLIF(username, ''), platform_user_id)",
        "COALESCE(NULLIF(CONCAT_WS(' ', first_name, last_name), ''), NULLIF(username, ''), platform_user_id)",
        "id::text",
        "collected_at",
        "COALESCE(updated_at, collected_at)",
        "1",
        "jsonb_build_object('platform_user_id', platform_user_id, 'is_deleted', is_deleted)",
    ),
    _ProfileSpec(
        "telegram",
        "telegram_chats",
        "chat",
        "COALESCE(NULLIF(username, ''), platform_chat_id)",
        "COALESCE(NULLIF(title, ''), NULLIF(username, ''), platform_chat_id)",
        "id::text",
        "collected_at",
        "COALESCE(updated_at, collected_at)",
        "GREATEST(COALESCE(members_count, 0), 1)",
        "jsonb_build_object('platform_chat_id', platform_chat_id, 'type', type)",
    ),
    _ProfileSpec(
        "whatsapp",
        "whatsapp_users",
        "user",
        "platform_user_id",
        "COALESCE(NULLIF(name, ''), NULLIF(pushname, ''), NULLIF(phone_number, ''), platform_user_id)",
        "id::text",
        "collected_at",
        "COALESCE(updated_at, collected_at)",
        "1",
        "jsonb_build_object('platform_user_id', platform_user_id, 'is_business', is_business)",
    ),
    _ProfileSpec(
        "whatsapp",
        "whatsapp_chats",
        "chat",
        "platform_chat_id",
        "COALESCE(NULLIF(name, ''), platform_chat_id)",
        "id::text",
        "collected_at",
        "COALESCE(updated_at, collected_at)",
        "GREATEST(COALESCE(participant_count, 0), 1)",
        "jsonb_build_object('platform_chat_id', platform_chat_id, 'is_group', is_group, 'chat_type', chat_type)",
    ),
    _ProfileSpec(
        "strava",
        "strava_athletes",
        "user",
        "platform_athlete_id::text",
        "COALESCE(NULLIF(CONCAT_WS(' ', firstname, lastname), ''), NULLIF(username, ''), platform_athlete_id::text)",
        "id::text",
        "collected_at",
        "COALESCE(updated_at, collected_at)",
        "GREATEST(COALESCE(follower_count, 0) + COALESCE(following_count, 0), 1)",
        "jsonb_build_object('platform_athlete_id', platform_athlete_id, 'country', country)",
    ),
    _ProfileSpec(
        "website",
        "website_targets",
        "domain",
        "domain",
        "COALESCE(NULLIF(name, ''), domain)",
        "id::text",
        "collected_at",
        "COALESCE(updated_at, collected_at)",
        "1",
        "jsonb_build_object('start_url', start_url, 'has_robots', robots_txt IS NOT NULL, 'status', status)",
        "status",
    ),
)

QUEUE_SPECS: tuple[_QueueSpec, ...] = (
    _QueueSpec("instagram", "instagram_spider_queue", "user", "COALESCE(NULLIF(username, ''), platform_user_id::text)", "COALESCE(NULLIF(username, ''), platform_user_id::text)", "id::text", "status", metadata_expr="jsonb_build_object('queue_source', source)"),
    _QueueSpec("tiktok", "tiktok_spider_queue", "user", "COALESCE(NULLIF(username, ''), platform_user_id::text)", "COALESCE(NULLIF(username, ''), platform_user_id::text)", "id::text", "status", metadata_expr="jsonb_build_object('queue_source', source)"),
    _QueueSpec("lemon8", "lemon8_spider_queue", "user", "platform_user_id::text", "platform_user_id::text", "id::text", "status", metadata_expr="jsonb_build_object('queue_source', source)"),
    _QueueSpec("strava", "strava_spider_queue", "user", "platform_athlete_id::text", "platform_athlete_id::text", "id::text", "status", metadata_expr="jsonb_build_object('queue_source', source)"),
    _QueueSpec("telegram", "telegram_spider_queue", "chat", "platform_chat_id", "COALESCE(NULLIF(title, ''), platform_chat_id)", "id::text", "status", metadata_expr="jsonb_build_object('queue_source', source)"),
    _QueueSpec("github", "github_spider_queue", "user", "target_identifier", "target_identifier", "id::text", "status", metadata_expr="jsonb_build_object('queue_source', source, 'target_type', target_type)"),
    _QueueSpec("youtube", "youtube_spider_queue", "channel", "platform_channel_id", "platform_channel_id", "id::text", "status", metadata_expr="jsonb_build_object('queue_source', source)"),
    _QueueSpec("youtube", "youtube_profile_queue", "channel", "profile_key", "COALESCE(NULLIF(handle, ''), platform_channel_id, profile_key)", "profile_key", "status", "priority", "first_seen", "jsonb_build_object('key_type', key_type, 'queue_source', source)"),
    _QueueSpec("x", "x_profile_targets", "user", "username", "username", "username", "status", "priority", "updated_at", "metadata"),
)

POST_AUTHOR_SPECS: tuple[_PostAuthorSpec, ...] = (
    _PostAuthorSpec("threads", "threads_posts"),
    _PostAuthorSpec("facebook", "facebook_posts"),
)


async def _table_exists(conn, table: str) -> bool:
    try:
        return bool(await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", table))
    except Exception:
        logger.debug("seen target table check failed for %s", table, exc_info=True)
        return False


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (KeyError, TypeError):
        if isinstance(row, dict):
            return row.get(key, default)
        return default


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_platform(value: Any) -> str | None:
    text = _clean_text(value)
    return text.lower() if text else None


def _normalize_type(value: Any) -> str | None:
    text = _clean_text(value)
    if not text:
        return None
    text = text.lower().replace(" ", "_")
    aliases = {
        "profile": "user",
        "username": "user",
        "channel_id": "channel",
        "repo": "repository",
        "site": "domain",
    }
    return aliases.get(text, text)


def _normalize_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value.strip()
    scheme = parsed.scheme.lower() or "https"
    netloc = parsed.netloc.lower()
    path = parsed.path or ""
    query = parsed.query
    return urlunsplit((scheme, netloc, path, query, ""))


def _normalize_key(target_type: str, value: Any) -> str | None:
    text = _clean_text(value)
    if not text:
        return None
    if target_type == "url":
        return _normalize_url(text)
    if target_type in {"user", "channel"}:
        text = text.lstrip("@")
    if target_type in {"user", "channel", "chat", "domain", "repository"}:
        return text.lower()
    return text


def _as_utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _json_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _status_for(raw_status: Any, last_backfill_at: Any = None, next_backfill_at: Any = None) -> str:
    status = (_clean_text(raw_status) or "").lower()
    if status in {"failed", "error", "dead"}:
        return "failed"
    if status in {"skipped", "disabled", "blocked", "unavailable"}:
        return "skipped"
    if status in {"pending", "queued", "processing", "in_progress", "running"}:
        return "pending"

    now = datetime.now(timezone.utc)
    next_backfill = _as_utc(next_backfill_at)
    if next_backfill and next_backfill <= now:
        return "stale"

    last_backfill = _as_utc(last_backfill_at)
    if last_backfill:
        fresh_days = max(1, int(os.getenv("COLLECTOR_SEEN_TARGET_FRESH_DAYS", str(DEFAULT_FRESH_DAYS))))
        return "fresh" if now - last_backfill <= timedelta(days=fresh_days) else "stale"

    if status in {"completed", "success", "active", "ok", "fresh"}:
        return "backfilled"
    return "seen"


def _coerce_record(record: SeenTarget) -> SeenTarget | None:
    platform = _normalize_platform(record.platform)
    target_type = _normalize_type(record.target_type)
    if not platform or not target_type:
        return None
    target_key = _normalize_key(target_type, record.target_key)
    if not target_key:
        return None
    display = _clean_text(record.target_display)
    origin = _clean_text(record.origin) or "collector"
    status = _status_for(record.status, record.last_backfill_at, record.next_backfill_at)
    evidence = max(1, int(record.evidence_count or 1))
    return SeenTarget(
        platform=platform,
        target_type=target_type,
        target_key=target_key,
        target_display=display,
        origin=origin[:100],
        priority=int(record.priority or 5),
        evidence_count=evidence,
        first_seen_at=record.first_seen_at,
        last_seen_at=record.last_seen_at,
        last_backfill_at=record.last_backfill_at,
        next_backfill_at=record.next_backfill_at,
        status=status,
        source_table=_clean_text(record.source_table),
        source_record_id=_clean_text(record.source_record_id),
        metadata=_json_dict(record.metadata),
    )


def merge_seen_targets(records: list[SeenTarget]) -> list[SeenTarget]:
    merged: dict[tuple[str, str, str], SeenTarget] = {}
    for raw in records:
        record = _coerce_record(raw)
        if record is None:
            continue
        key = (record.platform, record.target_type, record.target_key)
        existing = merged.get(key)
        if existing is None:
            merged[key] = record
            continue

        metadata = dict(existing.metadata)
        metadata.update(record.metadata)
        first_seen = min(
            (dt for dt in (_as_utc(existing.first_seen_at), _as_utc(record.first_seen_at)) if dt),
            default=_as_utc(existing.first_seen_at) or _as_utc(record.first_seen_at),
        )
        last_seen = max(
            (dt for dt in (_as_utc(existing.last_seen_at), _as_utc(record.last_seen_at)) if dt),
            default=_as_utc(existing.last_seen_at) or _as_utc(record.last_seen_at),
        )
        last_backfill = max(
            (dt for dt in (_as_utc(existing.last_backfill_at), _as_utc(record.last_backfill_at)) if dt),
            default=_as_utc(existing.last_backfill_at) or _as_utc(record.last_backfill_at),
        )
        selected = record if STATUS_RANK.get(record.status, 0) >= STATUS_RANK.get(existing.status, 0) else existing
        merged[key] = SeenTarget(
            platform=existing.platform,
            target_type=existing.target_type,
            target_key=existing.target_key,
            target_display=record.target_display or existing.target_display,
            origin=selected.origin,
            priority=max(existing.priority, record.priority),
            evidence_count=max(existing.evidence_count, record.evidence_count),
            first_seen_at=first_seen,
            last_seen_at=last_seen,
            last_backfill_at=last_backfill,
            next_backfill_at=record.next_backfill_at or existing.next_backfill_at,
            status=selected.status,
            source_table=selected.source_table,
            source_record_id=selected.source_record_id,
            metadata=metadata,
        )
    return list(merged.values())


async def _fetch_social_user_records(conn, source: str | None, limit: int) -> list[SeenTarget]:
    if not await _table_exists(conn, "social_users"):
        return []
    rows = await conn.fetch(
        """
        SELECT platform, uid, platform_user_id, username, display_name,
               first_seen, last_seen, times_seen, contexts, profile_photo_url,
               metadata
        FROM social_users
        WHERE ($1::text IS NULL OR platform = $1)
        ORDER BY last_seen DESC NULLS LAST
        LIMIT $2
        """,
        source,
        limit,
    )
    records: list[SeenTarget] = []
    for row in rows:
        username = _row_value(row, "username")
        platform_user_id = _row_value(row, "platform_user_id")
        uid = _row_value(row, "uid")
        records.append(SeenTarget(
            platform=_row_value(row, "platform"),
            target_type="user",
            target_key=username or platform_user_id or uid,
            target_display=_row_value(row, "display_name") or username or platform_user_id or uid,
            origin="social_users",
            evidence_count=int(_row_value(row, "times_seen", 1) or 1),
            first_seen_at=_row_value(row, "first_seen"),
            last_seen_at=_row_value(row, "last_seen"),
            status="seen",
            source_table="social_users",
            source_record_id=str(uid) if uid is not None else None,
            metadata={
                "platform_user_id": platform_user_id,
                "contexts": _row_value(row, "contexts") or [],
                "has_profile_photo": bool(_row_value(row, "profile_photo_url")),
            },
        ))
    return records


async def _fetch_collection_target_records(conn, source: str | None, limit: int) -> list[SeenTarget]:
    if not await _table_exists(conn, "collection_targets"):
        return []
    rows = await conn.fetch(
        """
        SELECT source, target_type, target_id, target_name, priority, status,
               collection_count, last_collection_at, created_at, metadata
        FROM collection_targets
        WHERE ($1::text IS NULL OR source = $1)
        ORDER BY priority DESC, created_at DESC
        LIMIT $2
        """,
        source,
        limit,
    )
    records: list[SeenTarget] = []
    for row in rows:
        target_type = _row_value(row, "target_type") or "user"
        last_collection_at = _row_value(row, "last_collection_at")
        raw_status = _row_value(row, "status")
        records.append(SeenTarget(
            platform=_row_value(row, "source"),
            target_type=target_type,
            target_key=_row_value(row, "target_id"),
            target_display=_row_value(row, "target_name") or _row_value(row, "target_id"),
            origin="collection_targets",
            priority=int(_row_value(row, "priority", 0) or 0),
            evidence_count=max(1, int(_row_value(row, "collection_count", 0) or 0)),
            first_seen_at=_row_value(row, "created_at"),
            last_seen_at=last_collection_at or _row_value(row, "created_at"),
            last_backfill_at=last_collection_at,
            status=raw_status,
            source_table="collection_targets",
            source_record_id=str(_row_value(row, "target_id")),
            metadata=_json_dict(_row_value(row, "metadata")),
        ))
    return records


async def _fetch_profile_records(conn, spec: _ProfileSpec, limit: int) -> list[SeenTarget]:
    if not await _table_exists(conn, spec.table):
        return []
    rows = await conn.fetch(
        f"""
        SELECT {spec.key_expr} AS target_key,
               {spec.display_expr} AS target_display,
               {spec.record_id_expr} AS source_record_id,
               {spec.first_seen_expr} AS first_seen_at,
               {spec.last_seen_expr} AS last_seen_at,
               {spec.last_seen_expr} AS last_backfill_at,
               {spec.evidence_expr} AS evidence_count,
               {spec.metadata_expr} AS metadata,
               {spec.status_expr} AS raw_status
        FROM {spec.table}
        WHERE {spec.key_expr} IS NOT NULL
        ORDER BY {spec.last_seen_expr} DESC NULLS LAST
        LIMIT $1
        """,
        limit,
    )
    return [
        SeenTarget(
            platform=spec.platform,
            target_type=spec.target_type,
            target_key=_row_value(row, "target_key"),
            target_display=_row_value(row, "target_display"),
            origin=spec.table,
            evidence_count=int(_row_value(row, "evidence_count", 1) or 1),
            first_seen_at=_row_value(row, "first_seen_at"),
            last_seen_at=_row_value(row, "last_seen_at"),
            last_backfill_at=_row_value(row, "last_backfill_at"),
            status=_row_value(row, "raw_status"),
            source_table=spec.table,
            source_record_id=_row_value(row, "source_record_id"),
            metadata=_json_dict(_row_value(row, "metadata")),
        )
        for row in rows
    ]


async def _fetch_queue_records(conn, spec: _QueueSpec, limit: int) -> list[SeenTarget]:
    if not await _table_exists(conn, spec.table):
        return []
    rows = await conn.fetch(
        f"""
        SELECT {spec.key_expr} AS target_key,
               {spec.display_expr} AS target_display,
               {spec.record_id_expr} AS source_record_id,
               {spec.status_expr} AS raw_status,
               {spec.priority_expr} AS priority,
               {spec.seen_expr} AS seen_at,
               {spec.metadata_expr} AS metadata
        FROM {spec.table}
        WHERE {spec.key_expr} IS NOT NULL
        ORDER BY {spec.seen_expr} DESC NULLS LAST
        LIMIT $1
        """,
        limit,
    )
    return [
        SeenTarget(
            platform=spec.platform,
            target_type=spec.target_type,
            target_key=_row_value(row, "target_key"),
            target_display=_row_value(row, "target_display"),
            origin=spec.table,
            priority=int(_row_value(row, "priority", 5) or 5),
            first_seen_at=_row_value(row, "seen_at"),
            last_seen_at=_row_value(row, "seen_at"),
            status=_row_value(row, "raw_status"),
            source_table=spec.table,
            source_record_id=_row_value(row, "source_record_id"),
            metadata=_json_dict(_row_value(row, "metadata")),
        )
        for row in rows
    ]


async def _fetch_follow_edge_records(conn, source: str | None, limit: int) -> list[SeenTarget]:
    if not await _table_exists(conn, "follow_edges"):
        return []
    rows = await conn.fetch(
        """
        SELECT platform,
               COALESCE(NULLIF(target_username, ''), target_uid) AS target_key,
               NULLIF(target_username, '') AS target_display,
               MIN(first_seen) AS first_seen_at,
               MAX(last_seen) AS last_seen_at,
               COUNT(*)::bigint AS evidence_count,
               jsonb_build_object(
                   'directions', array_agg(DISTINCT direction),
                   'owner_accounts_seen', COUNT(DISTINCT owner_account)
               ) AS metadata
        FROM follow_edges
        WHERE ($1::text IS NULL OR platform = $1)
          AND COALESCE(NULLIF(target_username, ''), target_uid) IS NOT NULL
        GROUP BY platform, COALESCE(NULLIF(target_username, ''), target_uid), NULLIF(target_username, '')
        ORDER BY MAX(last_seen) DESC NULLS LAST
        LIMIT $2
        """,
        source,
        limit,
    )
    return [
        SeenTarget(
            platform=_row_value(row, "platform"),
            target_type="user",
            target_key=_row_value(row, "target_key"),
            target_display=_row_value(row, "target_display") or _row_value(row, "target_key"),
            origin="follow_edges",
            evidence_count=int(_row_value(row, "evidence_count", 1) or 1),
            first_seen_at=_row_value(row, "first_seen_at"),
            last_seen_at=_row_value(row, "last_seen_at"),
            status="seen",
            source_table="follow_edges",
            source_record_id=_row_value(row, "target_key"),
            metadata=_json_dict(_row_value(row, "metadata")),
        )
        for row in rows
    ]


async def _fetch_post_author_records(conn, spec: _PostAuthorSpec, limit: int) -> list[SeenTarget]:
    if not await _table_exists(conn, spec.table):
        return []
    rows = await conn.fetch(
        f"""
        SELECT {spec.author_expr} AS target_key,
               MIN({spec.seen_expr}) AS first_seen_at,
               MAX({spec.seen_expr}) AS last_seen_at,
               COUNT(*)::bigint AS evidence_count
        FROM {spec.table}
        WHERE {spec.author_expr} IS NOT NULL
          AND trim({spec.author_expr}) <> ''
        GROUP BY {spec.author_expr}
        ORDER BY MAX({spec.seen_expr}) DESC NULLS LAST
        LIMIT $1
        """,
        limit,
    )
    return [
        SeenTarget(
            platform=spec.platform,
            target_type="user",
            target_key=_row_value(row, "target_key"),
            target_display=_row_value(row, "target_key"),
            origin=spec.table,
            evidence_count=int(_row_value(row, "evidence_count", 1) or 1),
            first_seen_at=_row_value(row, "first_seen_at"),
            last_seen_at=_row_value(row, "last_seen_at"),
            status="seen",
            source_table=spec.table,
            source_record_id=_row_value(row, "target_key"),
            metadata={"role": "post_author"},
        )
        for row in rows
    ]


async def _fetch_discovered_link_records(conn, source: str | None, limit: int) -> list[SeenTarget]:
    if not await _table_exists(conn, "discovered_links"):
        return []
    rows = await conn.fetch(
        """
        SELECT source, url, domain, status, source_table, source_record_id,
               discovered_at, fetched_at, link_type, metadata
        FROM discovered_links
        WHERE ($1::text IS NULL OR source = $1)
        ORDER BY discovered_at DESC
        LIMIT $2
        """,
        source,
        limit,
    )
    records: list[SeenTarget] = []
    for row in rows:
        metadata = _json_dict(_row_value(row, "metadata"))
        metadata.update({
            "origin_source": _row_value(row, "source"),
            "domain": _row_value(row, "domain"),
            "link_type": _row_value(row, "link_type"),
        })
        records.append(SeenTarget(
            platform="website",
            target_type="url",
            target_key=_row_value(row, "url"),
            target_display=_row_value(row, "domain") or _row_value(row, "url"),
            origin="discovered_links",
            first_seen_at=_row_value(row, "discovered_at"),
            last_seen_at=_row_value(row, "fetched_at") or _row_value(row, "discovered_at"),
            last_backfill_at=_row_value(row, "fetched_at"),
            status=_row_value(row, "status"),
            source_table=_row_value(row, "source_table") or "discovered_links",
            source_record_id=_row_value(row, "source_record_id"),
            metadata=metadata,
        ))
    return records


async def _fetch_search_result_records(conn, source: str | None, limit: int) -> list[SeenTarget]:
    if source not in {None, "search", "website"}:
        return []
    if not await _table_exists(conn, "search_results"):
        return []
    rows = await conn.fetch(
        """
        SELECT id::text AS source_record_id, url, domain, title, collected_at
        FROM search_results
        WHERE url IS NOT NULL
        ORDER BY collected_at DESC
        LIMIT $1
        """,
        limit,
    )
    return [
        SeenTarget(
            platform="search",
            target_type="url",
            target_key=_row_value(row, "url"),
            target_display=_row_value(row, "title") or _row_value(row, "domain") or _row_value(row, "url"),
            origin="search_results",
            first_seen_at=_row_value(row, "collected_at"),
            last_seen_at=_row_value(row, "collected_at"),
            last_backfill_at=_row_value(row, "collected_at"),
            status="backfilled",
            source_table="search_results",
            source_record_id=_row_value(row, "source_record_id"),
            metadata={"domain": _row_value(row, "domain")},
        )
        for row in rows
    ]


async def collect_seen_target_records(
    conn,
    *,
    source: str | None = None,
    limit_per_source: int = DEFAULT_LIMIT_PER_SOURCE,
) -> list[SeenTarget]:
    """Build registry rows from existing Collector tables without writing."""
    platform = _normalize_platform(source) if source else None
    limit = max(1, min(int(limit_per_source or DEFAULT_LIMIT_PER_SOURCE), 50000))

    records: list[SeenTarget] = []
    records.extend(await _fetch_social_user_records(conn, platform, limit))
    records.extend(await _fetch_follow_edge_records(conn, platform, limit))
    records.extend(await _fetch_collection_target_records(conn, platform, limit))

    for spec in QUEUE_SPECS:
        if platform and spec.platform != platform:
            continue
        try:
            records.extend(await _fetch_queue_records(conn, spec, limit))
        except Exception:
            logger.debug("seen target queue read skipped for %s", spec.table, exc_info=True)

    for spec in PROFILE_SPECS:
        if platform and spec.platform != platform:
            continue
        try:
            records.extend(await _fetch_profile_records(conn, spec, limit))
        except Exception:
            logger.debug("seen target profile read skipped for %s", spec.table, exc_info=True)

    for spec in POST_AUTHOR_SPECS:
        if platform and spec.platform != platform:
            continue
        try:
            records.extend(await _fetch_post_author_records(conn, spec, limit))
        except Exception:
            logger.debug("seen target post-author read skipped for %s", spec.table, exc_info=True)

    records.extend(await _fetch_discovered_link_records(conn, platform, limit))
    records.extend(await _fetch_search_result_records(conn, platform, limit))
    return merge_seen_targets(records)


async def upsert_seen_targets(conn, records: list[SeenTarget]) -> int:
    normalized = merge_seen_targets(records)
    if not normalized:
        return 0
    await conn.executemany(
        """
        INSERT INTO collector_seen_targets (
            platform, target_type, target_key, target_display, origin, priority,
            evidence_count, first_seen_at, last_seen_at, last_backfill_at,
            next_backfill_at, status, source_table, source_record_id, metadata,
            updated_at
        )
        VALUES (
            $1, $2, $3, $4, $5, $6, $7,
            COALESCE($8::timestamptz, NOW()),
            COALESCE($9::timestamptz, COALESCE($8::timestamptz, NOW())),
            $10::timestamptz,
            $11::timestamptz,
            $12, $13, $14, $15::jsonb, NOW()
        )
        ON CONFLICT (platform, target_type, target_key) DO UPDATE SET
            target_display = COALESCE(EXCLUDED.target_display, collector_seen_targets.target_display),
            origin = EXCLUDED.origin,
            priority = GREATEST(collector_seen_targets.priority, EXCLUDED.priority),
            evidence_count = GREATEST(collector_seen_targets.evidence_count, EXCLUDED.evidence_count),
            first_seen_at = LEAST(collector_seen_targets.first_seen_at, EXCLUDED.first_seen_at),
            last_seen_at = GREATEST(collector_seen_targets.last_seen_at, EXCLUDED.last_seen_at),
            last_backfill_at = CASE
                WHEN collector_seen_targets.last_backfill_at IS NULL AND EXCLUDED.last_backfill_at IS NULL THEN NULL
                ELSE GREATEST(
                    COALESCE(collector_seen_targets.last_backfill_at, '-infinity'::timestamptz),
                    COALESCE(EXCLUDED.last_backfill_at, '-infinity'::timestamptz)
                )
            END,
            next_backfill_at = COALESCE(EXCLUDED.next_backfill_at, collector_seen_targets.next_backfill_at),
            status = EXCLUDED.status,
            source_table = COALESCE(EXCLUDED.source_table, collector_seen_targets.source_table),
            source_record_id = COALESCE(EXCLUDED.source_record_id, collector_seen_targets.source_record_id),
            metadata = COALESCE(collector_seen_targets.metadata, '{}'::jsonb) || EXCLUDED.metadata,
            updated_at = NOW()
        """,
        [
            (
                row.platform,
                row.target_type,
                row.target_key,
                row.target_display,
                row.origin,
                row.priority,
                row.evidence_count,
                _as_utc(row.first_seen_at),
                _as_utc(row.last_seen_at),
                _as_utc(row.last_backfill_at),
                _as_utc(row.next_backfill_at),
                row.status,
                row.source_table,
                row.source_record_id,
                json.dumps(row.metadata, default=str),
            )
            for row in normalized
        ],
    )
    return len(normalized)


async def refresh_seen_targets_from_sources(
    conn,
    *,
    source: str | None = None,
    limit_per_source: int = DEFAULT_LIMIT_PER_SOURCE,
    write: bool = True,
) -> dict[str, Any]:
    records = await collect_seen_target_records(
        conn,
        source=source,
        limit_per_source=limit_per_source,
    )
    written = 0
    available = await _table_exists(conn, "collector_seen_targets")
    if write and available:
        written = await upsert_seen_targets(conn, records)
    summary = (
        await seen_target_summary_by_source(conn, source=source)
        if write and available
        else _summarize_records(records)
    )
    return {
        "available": available,
        "records": len(records),
        "written": written,
        "sources": summary,
    }


def _summarize_records(records: list[SeenTarget]) -> dict[str, dict[str, int]]:
    summary: dict[str, dict[str, int]] = {}
    now = datetime.now(timezone.utc)
    for row in merge_seen_targets(records):
        bucket = summary.setdefault(row.platform, {
            "total": 0,
            "backfilled": 0,
            "pending": 0,
            "fresh": 0,
            "stale": 0,
            "newly_discovered": 0,
            "failed": 0,
        })
        bucket["total"] += 1
        if row.status in {"backfilled", "fresh", "stale"}:
            bucket["backfilled"] += 1
        if row.status == "pending":
            bucket["pending"] += 1
        if row.status == "fresh":
            bucket["fresh"] += 1
        if row.status == "stale":
            bucket["stale"] += 1
        if row.status == "failed":
            bucket["failed"] += 1
        first_seen = _as_utc(row.first_seen_at)
        if first_seen and now - first_seen <= timedelta(hours=24):
            bucket["newly_discovered"] += 1
    return summary


async def seen_target_summary_by_source(conn, *, source: str | None = None) -> dict[str, dict[str, int]]:
    if not await _table_exists(conn, "collector_seen_targets"):
        return {}
    rows = await conn.fetch(
        """
        SELECT platform AS source,
               COUNT(*)::bigint AS total,
               COUNT(*) FILTER (WHERE status IN ('backfilled', 'fresh', 'stale'))::bigint AS backfilled,
               COUNT(*) FILTER (WHERE status = 'pending')::bigint AS pending,
               COUNT(*) FILTER (WHERE status = 'fresh')::bigint AS fresh,
               COUNT(*) FILTER (WHERE status = 'stale')::bigint AS stale,
               COUNT(*) FILTER (WHERE status = 'failed')::bigint AS failed,
               COUNT(*) FILTER (WHERE first_seen_at >= NOW() - INTERVAL '24 hours')::bigint AS newly_discovered
        FROM collector_seen_targets
        WHERE ($1::text IS NULL OR platform = $1)
        GROUP BY platform
        ORDER BY platform
        """,
        _normalize_platform(source) if source else None,
    )
    return {
        str(row["source"]): {
            "total": int(row["total"] or 0),
            "backfilled": int(row["backfilled"] or 0),
            "pending": int(row["pending"] or 0),
            "fresh": int(row["fresh"] or 0),
            "stale": int(row["stale"] or 0),
            "newly_discovered": int(row["newly_discovered"] or 0),
            "failed": int(row["failed"] or 0),
        }
        for row in rows
    }


async def list_seen_targets(
    conn,
    *,
    source: str | None = None,
    status: str | None = None,
    target_type: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    if not await _table_exists(conn, "collector_seen_targets"):
        return []
    rows = await conn.fetch(
        """
        SELECT platform AS source, target_type, target_key, target_display,
               origin, priority, evidence_count, first_seen_at, last_seen_at,
               last_backfill_at, next_backfill_at, status, source_table,
               source_record_id, updated_at
        FROM collector_seen_targets
        WHERE ($1::text IS NULL OR platform = $1)
          AND ($2::text IS NULL OR status = $2)
          AND ($3::text IS NULL OR target_type = $3)
        ORDER BY last_seen_at DESC, priority DESC
        LIMIT $4
        """,
        _normalize_platform(source) if source else None,
        (_clean_text(status) or "").lower() if status else None,
        _normalize_type(target_type) if target_type else None,
        max(1, min(int(limit or 200), 1000)),
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        for key in ("first_seen_at", "last_seen_at", "last_backfill_at", "next_backfill_at", "updated_at"):
            value = item.get(key)
            item[key] = value.isoformat() if value else None
        out.append(item)
    return out
