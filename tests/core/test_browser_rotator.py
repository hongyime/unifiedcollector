from datetime import datetime, timezone

import pytest

from src.core import browser_rotator as br

SOURCES = ["x", "instagram", "facebook", "threads", "tiktok", "lemon8", "strava"]
IV = 1800


def _at(slot):
    return datetime.fromtimestamp(slot * IV, tz=timezone.utc)


def test_select_active_rotates_deterministically():
    assert br.select_active(SOURCES, 2, _at(0), IV) == ["x", "instagram"]
    assert br.select_active(SOURCES, 2, _at(1), IV) == ["facebook", "threads"]
    assert br.select_active(SOURCES, 2, _at(2), IV) == ["tiktok", "lemon8"]
    assert br.select_active(SOURCES, 2, _at(3), IV) == ["strava", "x"]  # wraps
    assert br.select_active(SOURCES, 2, _at(0), IV) == ["x", "instagram"]  # deterministic


def test_select_active_edge_cases():
    assert br.select_active(["x"], 5, _at(0), IV) == ["x"]  # width capped to len
    assert br.select_active([], 2, _at(0), IV) == []


@pytest.mark.asyncio
async def test_rotate_activates_slot_and_pauses_rest():
    calls = []

    class _Conn:
        async def execute(self, sql, *args):
            calls.append((sql, args))
            return "UPDATE 0"

    sources = ["x", "instagram", "facebook", "threads"]
    res = await br.rotate_browser_schedules(
        _Conn(), sources=sources, width=2, now=_at(0), interval_seconds=IV
    )

    assert res["active"] == ["instagram", "x"]
    assert res["paused"] == ["facebook", "threads"]
    # First execute enables the active pair, second disables the rest.
    assert "enabled = true" in calls[0][0]
    assert set(calls[0][1][0]) == {"x", "instagram"}
    assert "enabled = false" in calls[1][0]
    assert calls[1][1][0] == ["facebook", "threads"]


@pytest.mark.asyncio
async def test_rotate_never_touches_pinned_messaging():
    """Only browser-group sources are ever written — msg/backend stay untouched."""
    touched = []

    class _Conn:
        async def execute(self, sql, *args):
            touched.extend(args[0])
            return "UPDATE 0"

    sources = ["x", "instagram", "facebook"]
    await br.rotate_browser_schedules(_Conn(), sources=sources, width=1, now=_at(0), interval_seconds=IV)
    for msg in ("telegram", "beeper", "whatsapp", "instagram_dm"):
        assert msg not in touched
