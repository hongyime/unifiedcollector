"""Exercise route bodies that lost helper bindings during the package split."""
from contextlib import asynccontextmanager
import importlib
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from PIL import Image

os.environ.setdefault("DASHBOARD_JWT_SECRET", "test-secret-only-for-pytest-do-not-use")
os.environ.setdefault("DASHBOARD_ADMIN_PASSWORD", "x")
api = importlib.import_module("src.dashboard.api")
media = importlib.import_module("src.dashboard.api.media")
youtube = importlib.import_module("src.dashboard.api.youtube")


class Pool:
    def __init__(self, conn=None):
        self.conn = conn or SimpleNamespace()

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


@pytest.mark.asyncio
async def test_media_stats_uses_exported_helpers_and_keeps_source_totals(monkeypatch):
    monkeypatch.setattr(media, "get_pool", AsyncMock(return_value=Pool()))
    monkeypatch.setattr(api, "_source_media_totals", AsyncMock(return_value={"fixture": {"total_media_items": 3, "total_media_bytes": 17}}))
    monkeypatch.setattr(api, "_with_bridge_overrides", AsyncMock(return_value=([], {})))
    monkeypatch.setattr(api, "_beeper_subsource_liveness", AsyncMock(return_value=[]))
    monkeypatch.setattr("src.core.source_freshness.compute_liveness", AsyncMock(return_value=[]))
    result = await media.media_stats(_user={})
    assert len(result) == 1
    assert result[0]["source"] == "fixture" and result[0]["total_items"] == 3
    assert result[0]["total_bytes"] == 17 and result[0]["stats_error"] is None


@pytest.mark.asyncio
async def test_youtube_completeness_calls_the_actual_exported_helper(monkeypatch):
    monkeypatch.setattr(youtube, "get_pool", AsyncMock(return_value=Pool()))
    helper = AsyncMock(return_value={"channels": 7})
    monkeypatch.setattr(api, "_youtube_completeness", helper)
    assert await youtube.youtube_completeness(_user={}) == {"channels": 7}
    helper.assert_awaited_once()


@pytest.mark.asyncio
async def test_media_file_rejects_bad_identity_before_acquiring_database():
    with pytest.raises(HTTPException) as error:
        await media.media_file("not-a-uuid", _user={})
    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_media_thumbnail_returns_jpeg_bytes_from_a_synthetic_local_image(monkeypatch, tmp_path):
    path = tmp_path / "fixture.png"
    Image.new("RGB", (8, 8), (15, 30, 45)).save(path)
    conn = SimpleNamespace(fetchrow=AsyncMock(return_value={"file_path": str(path), "content_type": "photo"}))
    monkeypatch.setattr(media, "get_pool", AsyncMock(return_value=Pool(conn)))
    monkeypatch.setattr(api, "_resolve_media_path", lambda value: path)
    response = await media.media_thumbnail("00000000-0000-4000-8000-000000000001", _user={})
    assert response.status_code == 200 and response.media_type == "image/jpeg"
    body = b"".join([chunk async for chunk in response.body_iterator])
    assert body.startswith(b"\xff\xd8")
