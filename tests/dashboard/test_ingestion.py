import os

import pytest

os.environ.setdefault("DASHBOARD_JWT_SECRET", "test-ingestion-dashboard-secret")

from src.dashboard.api.ingestion import get_backfill_equilibrium


class _Conn:
    async def fetchval(self, _sql, table_name):
        return table_name == "public.backfill_lanes"

    async def fetch(self, sql, *_args):
        if "FROM backfill_lanes" in sql:
            return [{
                "source": "whatsapp", "total": 5, "complete": 1,
                "queue_depth": 4, "leased": 1, "failed": 0,
                "latest_durable_write_at": None, "historical_lag_since": None,
                "historical_lag_seconds": 90,
            }]
        return []


class _Acquire:
    async def __aenter__(self):
        return _Conn()

    async def __aexit__(self, *_args):
        return False


class _Pool:
    def acquire(self):
        return _Acquire()


@pytest.mark.asyncio
async def test_backfill_equilibrium_includes_common_lane_only_source():
    result = await get_backfill_equilibrium(_Pool())

    whatsapp = next(row for row in result["platforms"] if row["platform"] == "whatsapp")
    assert whatsapp["queue_table"] == "backfill_lanes"
    assert whatsapp["queue_depth"] == 4
    assert whatsapp["common_lane"]["leased"] == 1
    assert whatsapp["common_lane"]["historical_lag_seconds"] == 90
