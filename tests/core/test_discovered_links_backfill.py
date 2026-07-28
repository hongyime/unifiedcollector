from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.core.discovered_links_backfill import backfill_discovered_links_for_source


class _FakeConn:
    def __init__(self, rows):
        self.rows = list(rows)
        self.executed = []

    async def fetchrow(self, *_args, **_kwargs):
        return {"last_processed_id": None, "last_processed_at": None}

    async def fetch(self, *_args, **_kwargs):
        return self.rows

    async def execute(self, query, *args, **kwargs):
        self.executed.append((query, args, kwargs))
        return "INSERT 0 1"


@pytest.mark.asyncio
async def test_youtube_discovered_links_backfill_advances_cursor():
    conn = _FakeConn([
        {
            "platform_video_id": "yt1",
            "title": "demo",
            "description": "watch https://example.com/a and https://youtu.be/x",
            "platform_channel_id": "UC123",
            "collected_at": datetime(2026, 7, 28, tzinfo=timezone.utc),
        }
    ])

    result = await backfill_discovered_links_for_source(conn, "youtube", limit=10)

    assert result.scanned == 1
    assert result.links_written == 2
    assert result.last_processed_id == "yt1"
    inserts = [q for q, _args, _kwargs in conn.executed if "INSERT INTO discovered_links" in q]
    assert len(inserts) == 2
    cursor_updates = [args for q, args, _kwargs in conn.executed if "UPDATE service_cursors" in q]
    assert cursor_updates[-1][0] == "discovered_links_backfill_youtube"
    assert cursor_updates[-1][1] == "yt1"


@pytest.mark.asyncio
async def test_telegram_discovered_links_backfill_uses_message_and_caption():
    conn = _FakeConn([
        {
            "platform_message_id": "-1001:42",
            "text": "profile https://instagram.com/example",
            "caption": "mirror https://t.me/example",
            "platform_chat_id": "-1001",
            "platform_user_id": "1234",
            "collected_at": datetime(2026, 7, 28, tzinfo=timezone.utc),
        }
    ])

    result = await backfill_discovered_links_for_source(conn, "telegram", limit=10)

    assert result.scanned == 1
    assert result.links_written == 2
    assert result.last_processed_id == "-1001:42"
    link_args = [
        args
        for q, args, _kwargs in conn.executed
        if "INSERT INTO discovered_links" in q
    ]
    assert {args[5] for args in link_args} == {
        "https://instagram.com/example",
        "https://t.me/example",
    }
