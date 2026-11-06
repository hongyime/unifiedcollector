import os

import pytest

os.environ.setdefault("DASHBOARD_JWT_SECRET", "test-telegram-history-matrix")

from src.dashboard.api._shared import _telegram_history_matrix_status


class _Conn:
    async def fetchval(self, sql):
        if "telegram_account_throughput" in sql:
            return "telegram_account_throughput"
        if "backfill_lanes" in sql:
            return "backfill_lanes"
        raise AssertionError(sql)

    async def fetch(self, sql):
        assert "telegram_account_throughput" in sql
        return [{
            "account_identity": "account-1", "claim_budget": 2, "active_claims": 1,
            "claims_done": 4, "written_messages": 12, "write_rate": 1.5,
            "flood_waits": 3, "cooldown_until": None, "updated_at": None,
        }]

    async def fetchrow(self, sql):
        assert "historical_lag_seconds" in sql
        return {"queue_depth": 7, "historical_lag_since": None, "historical_lag_seconds": 90}


@pytest.mark.asyncio
async def test_telegram_matrix_telemetry_exposes_accounts_workers_and_historical_lag():
    status = await _telegram_history_matrix_status(_Conn())

    assert status["active_history_workers"] == 1
    assert status["queue_depth"] == 7
    assert status["historical_lag_seconds"] == 90
    assert status["accounts"][0]["write_rate"] == 1.5
