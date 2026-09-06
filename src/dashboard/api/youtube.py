"""``youtube`` route handlers for the dashboard.

Extracted from ``src/dashboard/api/__init__.py`` during PERF-002 4A refactor.
Routes registered on ``router`` (APIRouter) and included by ``__init__.py``.

Cross-module helpers that still live in ``__init__.py`` are late-imported
inside thin wrappers to preserve test monkey-patch semantics.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from src.db.connection import get_pool
from src.dashboard.api.auth import require_role
from src.dashboard.api.helpers import (
    _acquire_dashboard_conn,
    _release_dashboard_conn,
)

logger = logging.getLogger(__name__)


def _lookup(name: str):
    """Look up a name on the parent dashboard_api module at call time."""
    import sys as _sys
    root = _sys.modules.get("src.dashboard.api")
    if root is None:
        return None
    return getattr(root, name, None)


router = APIRouter()


@router.get("/youtube/completeness")
async def youtube_completeness(_user: dict = Depends(require_role("viewer"))):
    """YouTube collection completeness and discovery graph health."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await _youtube_completeness(conn)


@router.get("/youtube/channels")
async def list_youtube_channels(limit: int = 100, _user: dict = Depends(require_role("viewer"))):
    """YouTube channels and collection stats."""
    limit = max(1, min(limit, 500))
    pool = await get_pool()
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('youtube_channels')") is None:
            return []
        rows = await conn.fetch(
            """
            SELECT c.id,
                   c.platform_channel_id,
                   c.title,
                   c.custom_url,
                   c.thumbnail_url,
                   c.description,
                   c.subscriber_count,
                   c.video_count,
                   c.view_count,
                   c.updated_at,
                   c.profile_photo_media_id,
                   c.external_links,
                   c.last_video_scan_at,
                   c.last_community_scan_at,
                   c.last_skip_reason,
                   c.last_error,
                   (SELECT COUNT(*) FROM youtube_videos WHERE channel_id = c.id) AS videos_collected,
                   (SELECT MAX(platform_published_at) FROM youtube_videos WHERE channel_id = c.id) AS last_video_at
            FROM youtube_channels c
            ORDER BY c.subscriber_count DESC NULLS LAST, c.updated_at DESC
            LIMIT $1
            """,
            limit,
            timeout=12,
        )
    return [dict(r) for r in rows]


@router.get("/youtube/channel/{channel_id}")
async def youtube_channel_detail(channel_id: str, limit: int = 200, _user: dict = Depends(require_role("viewer"))):
    """Videos for one YouTube channel."""
    limit = max(1, min(limit, 500))
    pool = await get_pool()
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('youtube_channels')") is None:
            return {"channel": None, "videos": []}
            
        channel_row = await conn.fetchrow(
            """
            SELECT c.id,
                   c.platform_channel_id,
                   c.title,
                   c.custom_url,
                   c.thumbnail_url,
                   c.description,
                   c.subscriber_count,
                   c.video_count,
                   c.view_count,
                   c.profile_photo_media_id,
                   c.external_links,
                   c.last_video_scan_at,
                   c.last_community_scan_at,
                   c.last_skip_reason,
                   c.last_error,
                   c.updated_at
            FROM youtube_channels c
            WHERE c.platform_channel_id = $1
            """,
            channel_id,
            timeout=10,
        )
        if not channel_row:
            return {"channel": None, "videos": []}
            
        channel = dict(channel_row)
        channel_uuid = channel.pop("id")
            
        videos = await conn.fetch(
            """
            SELECT v.platform_video_id,
                   v.title,
                   v.description,
                   v.view_count,
                   v.like_count,
                   v.comment_count,
                   v.duration,
                   v.platform_published_at,
                   v.collected_at,
                   v.media_status,
                   v.media_skip_reason,
                   v.transcript_status,
                   v.comments_status,
                   COALESCE(thumb.id, video_mi.id) AS media_item_id,
                   COALESCE(thumb.content_type, video_mi.content_type) AS media_content_type,
                   thumb.id AS thumbnail_media_item_id,
                   video_mi.id AS video_media_item_id
            FROM youtube_videos v
            LEFT JOIN media_items thumb
                   ON thumb.source = 'youtube'
                  AND thumb.content_id = v.platform_video_id
                  AND thumb.content_type = 'thumbnail'
            LEFT JOIN media_items video_mi
                   ON video_mi.source = 'youtube'
                  AND video_mi.content_id = 'video_' || v.platform_video_id
            WHERE v.channel_id = $1
            ORDER BY v.platform_published_at DESC NULLS LAST, v.collected_at DESC
            LIMIT $2
            """,
            channel_uuid, limit,
            timeout=15,
        )
        
    out_videos = []
    for r in videos:
        d = dict(r)
        d["video_url"] = f"https://www.youtube.com/watch?v={d['platform_video_id']}"
        out_videos.append(d)
        
    return {"channel": channel, "videos": out_videos}


