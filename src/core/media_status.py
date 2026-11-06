"""Shared bounded media listing for dashboard and control room."""
from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID


def resolve_private_media_path(file_path: str, *, roots: tuple[Path, ...] | None = None) -> Path:
    """Resolve an indexed file only when it remains beneath a mounted media root."""
    if not file_path:
        raise ValueError("media path missing")
    path = Path(file_path).resolve()
    roots = roots or (
        Path(os.getenv("COLLECTOR_DRIVE_PATH", "/media")).resolve(),
        (Path(os.getenv("COLLECTOR_VAULT_ROOT", "/vault")).resolve() / "media").resolve(),
    )
    if not any(path.is_relative_to(root) for root in roots):
        raise ValueError("media path outside mounted roots")
    if not path.is_file():
        raise ValueError("media file unavailable")
    return path


async def list_recent_media(pool, *, source: str | None = None, limit: int = 50) -> list[dict]:
    limit = max(1, min(limit, 500))
    async with pool.acquire() as conn:
        if source:
            rows = await conn.fetch(
                """
                SELECT id, source, entity_name, content_type, filename, file_size, collected_at
                FROM media_items WHERE source=$1 ORDER BY collected_at DESC LIMIT $2
                """, source, limit,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT id, source, entity_name, content_type, filename, file_size, collected_at
                FROM media_items ORDER BY collected_at DESC LIMIT $1
                """, limit,
            )
    return [dict(row) for row in rows]


async def list_realtime_media_deliveries(pool, *, source: str | None = None,
                                         status: str | None = None, limit: int = 100) -> dict:
    """Read bounded delivery outcomes without exposing media files or URLs."""
    source = (source or "").strip().lower() or None
    status = (status or "").strip().lower() or None
    limit = max(1, min(limit, 500))
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('public.realtime_media_deliveries')") is None:
            return {"available": False, "items": []}
        if source and status:
            rows = await conn.fetch(
                """
                SELECT id::text, media_item_id::text, source, content_id, status, reason,
                       file_size, content_type, target_name, queued_at, sent_at, created_at, updated_at
                FROM realtime_media_deliveries WHERE source=$1 AND status=$2
                ORDER BY updated_at DESC LIMIT $3
                """,
                source, status, limit,
            )
        elif source:
            rows = await conn.fetch(
                """
                SELECT id::text, media_item_id::text, source, content_id, status, reason,
                       file_size, content_type, target_name, queued_at, sent_at, created_at, updated_at
                FROM realtime_media_deliveries WHERE source=$1
                ORDER BY updated_at DESC LIMIT $2
                """,
                source, limit,
            )
        elif status:
            rows = await conn.fetch(
                """
                SELECT id::text, media_item_id::text, source, content_id, status, reason,
                       file_size, content_type, target_name, queued_at, sent_at, created_at, updated_at
                FROM realtime_media_deliveries WHERE status=$1
                ORDER BY updated_at DESC LIMIT $2
                """,
                status, limit,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT id::text, media_item_id::text, source, content_id, status, reason,
                       file_size, content_type, target_name, queued_at, sent_at, created_at, updated_at
                FROM realtime_media_deliveries ORDER BY updated_at DESC LIMIT $1
                """,
                limit,
            )
    return {"available": True, "items": [dict(row) for row in rows]}


async def get_private_media(pool, media_id: str) -> dict:
    try:
        parsed_id = UUID(media_id)
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("invalid media id") from exc
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id::text, source, filename, content_type, file_size, file_path FROM media_items WHERE id=$1",
            parsed_id,
        )
    if not row:
        raise ValueError("media not found")
    item = dict(row)
    item["path"] = resolve_private_media_path(str(item.pop("file_path") or ""))
    return item
