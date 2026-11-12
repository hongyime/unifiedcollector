from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.collectors import pinterest, snapchat


@pytest.mark.asyncio
async def test_pinterest_feed_failure_does_not_block_existing_native_targets(monkeypatch):
    collector = pinterest.PinterestCollector()
    collector.pool = MagicMock()
    collector._refresh_own_following_edges = AsyncMock(return_value=0)
    collector._collect_authenticated_feed = AsyncMock(side_effect=RuntimeError("feed_auth_required"))
    monkeypatch.setattr(pinterest, "seed_native_targets", AsyncMock())
    monkeypatch.setattr(pinterest, "claim_native_target", AsyncMock(return_value=None))

    summary = await collector.run_native_cycle()

    assert summary["following_feed_error"] == "runtimeerror"
    assert summary["for_you_feed_error"] == "runtimeerror"


@pytest.mark.asyncio
async def test_snapchat_feed_failure_does_not_block_existing_native_targets(monkeypatch):
    collector = snapchat.SnapchatCollector()
    collector.pool = MagicMock()
    collector._collect_authenticated_feeds = AsyncMock(side_effect=RuntimeError("feed_auth_required"))
    monkeypatch.setattr(snapchat, "seed_native_targets", AsyncMock())
    monkeypatch.setattr(snapchat, "claim_native_target", AsyncMock(return_value=None))

    summary = await collector.run_native_cycle()

    assert summary["feed_error"] == "runtimeerror"
