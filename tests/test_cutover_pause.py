"""Behavioral boundary for the recovery cutover pause marker."""
from __future__ import annotations

import asyncio
import importlib

import pytest


@pytest.mark.asyncio
async def test_cutover_marker_prevents_watchdog_restart_paths(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgres://fixture:fixture@localhost/fixture")
    import src.watchdog.freshness as freshness

    freshness = importlib.reload(freshness)
    marker = tmp_path / "cutover.pause"
    marker.write_text("owner-controlled cutover\n", encoding="utf-8")
    monkeypatch.setattr(freshness, "CUTOVER_PAUSE_FILE", marker)

    async def forbidden_connect(*_args, **_kwargs):
        raise AssertionError("cutover-paused watchdog tried to open the restart path")

    async def stop_after_pause(_seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(freshness.asyncpg, "connect", forbidden_connect)
    monkeypatch.setattr(freshness.asyncio, "sleep", stop_after_pause)

    with pytest.raises(asyncio.CancelledError):
        await freshness.main()
