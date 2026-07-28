"""Bounded historical backfill for generic discovered_links rows."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Literal

from src.core.discovered_links import persist_discovered_links

SUPPORTED_SOURCES = ("youtube", "telegram")
SourceName = Literal["youtube", "telegram"]


@dataclass
class DiscoveredLinksBackfillResult:
    source: str
    cursor_service: str
    scanned: int = 0
    links_written: int = 0
    last_processed_id: str | None = None
    last_processed_at: datetime | None = None
    has_more: bool = False

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.last_processed_at is not None:
            data["last_processed_at"] = self.last_processed_at.isoformat()
        return data


def _cursor_service(source: str) -> str:
    return f"discovered_links_backfill_{source}"


async def _load_cursor(conn, service: str):
    return await conn.fetchrow(
        """
        INSERT INTO service_cursors (service, last_processed_id, last_processed_at, status)
        VALUES ($1, NULL, NULL, 'idle')
        ON CONFLICT (service) DO UPDATE SET service = EXCLUDED.service
        RETURNING last_processed_id, last_processed_at
        """,
        service,
    )


async def _save_cursor(conn, service: str, *, last_id: str | None, last_at, status: str = "idle") -> None:
    await conn.execute(
        """
        UPDATE service_cursors
        SET last_processed_id = $2,
            last_processed_at = $3,
            status = $4
        WHERE service = $1
        """,
        service,
        last_id,
        last_at,
        status,
    )


async def backfill_discovered_links_for_source(
    conn,
    source: SourceName,
    *,
    limit: int = 100,
) -> DiscoveredLinksBackfillResult:
    """Scan one source for historical text URLs and persist occurrence rows.

    The cursor is independent of source collectors. It advances by
    ``collected_at, platform_*_id`` so each invocation only handles a bounded
    slice and can run during normal collection.
    """
    if source not in SUPPORTED_SOURCES:
        raise ValueError(f"unsupported discovered-links source: {source}")
    limit = max(1, int(limit))
    service = _cursor_service(source)
    cursor = await _load_cursor(conn, service)
    last_at = cursor["last_processed_at"] if cursor else None
    last_id = cursor["last_processed_id"] if cursor else None

    if source == "youtube":
        rows = await _fetch_youtube_candidates(conn, last_at, last_id, limit)
    else:
        rows = await _fetch_telegram_candidates(conn, last_at, last_id, limit)

    result = DiscoveredLinksBackfillResult(
        source=source,
        cursor_service=service,
        scanned=len(rows),
        has_more=len(rows) >= limit,
    )
    if not rows:
        await _save_cursor(conn, service, last_id=last_id, last_at=last_at, status="idle")
        result.last_processed_id = last_id
        result.last_processed_at = last_at
        return result

    for row in rows:
        if source == "youtube":
            result.links_written += await _persist_youtube_row(conn, row)
            last_id = row["platform_video_id"]
        else:
            result.links_written += await _persist_telegram_row(conn, row)
            last_id = row["platform_message_id"]
        last_at = row["collected_at"]

    await _save_cursor(conn, service, last_id=last_id, last_at=last_at, status="idle")
    result.last_processed_id = last_id
    result.last_processed_at = last_at
    return result


async def backfill_discovered_links(
    conn,
    *,
    source: str = "all",
    limit: int = 100,
) -> list[DiscoveredLinksBackfillResult]:
    sources: tuple[str, ...]
    if source == "all":
        sources = SUPPORTED_SOURCES
    elif source in SUPPORTED_SOURCES:
        sources = (source,)
    else:
        raise ValueError(f"unsupported discovered-links source: {source}")
    return [
        await backfill_discovered_links_for_source(conn, src, limit=limit)  # type: ignore[arg-type]
        for src in sources
    ]


async def _fetch_youtube_candidates(conn, last_at, last_id: str | None, limit: int):
    return await conn.fetch(
        """
        SELECT
            v.platform_video_id,
            v.title,
            v.description,
            v.collected_at,
            c.platform_channel_id
        FROM youtube_videos v
        LEFT JOIN youtube_channels c ON c.id = v.channel_id
        WHERE COALESCE(v.description, '') ILIKE '%http%'
          AND (
                $1::timestamptz IS NULL
                OR (v.collected_at, v.platform_video_id) > ($1::timestamptz, COALESCE($2, ''))
              )
        ORDER BY v.collected_at ASC, v.platform_video_id ASC
        LIMIT $3
        """,
        last_at,
        last_id,
        limit,
        timeout=30,
    )


async def _fetch_telegram_candidates(conn, last_at, last_id: str | None, limit: int):
    return await conn.fetch(
        """
        SELECT
            m.platform_message_id,
            m.text,
            m.caption,
            m.collected_at,
            c.platform_chat_id,
            u.platform_user_id
        FROM telegram_messages m
        LEFT JOIN telegram_chats c ON c.id = m.chat_id
        LEFT JOIN telegram_users u ON u.id = m.sender_id
        WHERE (
                COALESCE(m.text, '') ILIKE '%http%'
                OR COALESCE(m.caption, '') ILIKE '%http%'
              )
          AND (
                $1::timestamptz IS NULL
                OR (m.collected_at, m.platform_message_id) > ($1::timestamptz, COALESCE($2, ''))
              )
        ORDER BY m.collected_at ASC, m.platform_message_id ASC
        LIMIT $3
        """,
        last_at,
        last_id,
        limit,
        timeout=30,
    )


async def _persist_youtube_row(conn, row) -> int:
    channel_id = row["platform_channel_id"]
    return await persist_discovered_links(
        conn,
        source="youtube",
        source_table="youtube_videos",
        source_record_id=row["platform_video_id"],
        context_id=channel_id,
        entity_id=channel_id,
        text=row["description"],
        metadata={
            "platform_video_id": row["platform_video_id"],
            "platform_channel_id": channel_id,
            "title": row["title"],
            "backfill": "discovered_links",
        },
    )


async def _persist_telegram_row(conn, row) -> int:
    chat_id = row["platform_chat_id"]
    sender_id = row["platform_user_id"]
    text = " ".join(v for v in (row["text"], row["caption"]) if v)
    return await persist_discovered_links(
        conn,
        source="telegram",
        source_table="telegram_messages",
        source_record_id=row["platform_message_id"],
        context_id=chat_id,
        entity_id=sender_id,
        text=text,
        metadata={
            "platform_message_id": row["platform_message_id"],
            "platform_chat_id": chat_id,
            "platform_sender_id": sender_id,
            "backfill": "discovered_links",
        },
    )
