from datetime import datetime, timezone

import pytest

from src.core.handoff_freshness import check_handoff_freshness


class _Conn:
    def __init__(self, changed_at):
        self.changed_at = changed_at

    async def fetchval(self, _sql, _table):
        return _table

    async def fetchrow(self, sql):
        if "source_health" in sql:
            return {"changed_at": self.changed_at, "attention_count": 1}
        if "backfill_lanes" in sql:
            return {"changed_at": self.changed_at, "pending_count": 2, "active_leases": 0}
        return {"changed_at": self.changed_at, "receipt_count": 3}


@pytest.mark.asyncio
async def test_handoff_freshness_rejects_a_marker_older_than_live_state(tmp_path):
    state = tmp_path / "STATE.md"
    state.write_text("<!-- handoff-freshness: 2026-11-03T00:00:00Z -->\n", encoding="utf-8")
    report = await check_handoff_freshness(
        _Conn(datetime(2026, 11, 3, 1, tzinfo=timezone.utc)), state_path=state,
    )
    assert report["ok"] is False
    assert report["reason"] == "live operational state is newer than STATE.md"
    assert report["snapshot"]["backfill_lanes"]["pending_count"] == 2


@pytest.mark.asyncio
async def test_handoff_freshness_accepts_current_marker(tmp_path):
    state = tmp_path / "STATE.md"
    state.write_text("<!-- handoff-freshness: 2026-11-03T02:00:00+00:00 -->\n", encoding="utf-8")
    report = await check_handoff_freshness(
        _Conn(datetime(2026, 11, 3, 1, tzinfo=timezone.utc)), state_path=state,
    )
    assert report["ok"] is True


@pytest.mark.asyncio
async def test_handoff_freshness_reports_missing_state_file(tmp_path):
    report = await check_handoff_freshness(
        _Conn(datetime(2026, 11, 3, 1, tzinfo=timezone.utc)), state_path=tmp_path / "missing.md",
    )
    assert report["ok"] is False
    assert report["reason"] == "STATE.md is missing a handoff-freshness marker"
