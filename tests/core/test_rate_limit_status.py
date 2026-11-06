from datetime import datetime, timedelta, timezone

import pytest

from src.core.rate_limit_status import get_recent_rate_limits


class _Conn:
    async def fetchval(self, *_args, **_kwargs):
        return True

    async def fetch(self, sql, *_args, **_kwargs):
        if "FROM rate_limit_events" in sql and "GROUP BY" not in sql:
            return [{"source": "x", "account": "owner", "metadata": '{"retry": 30}'}]
        if "FROM rate_limit_events" in sql:
            return [{
                "source": "x", "account": "owner", "scope": "profile", "count": 2,
                "reason": "429", "active_until": datetime.now(timezone.utc) + timedelta(minutes=1),
            }]
        return [{"service": "x_rate_limit", "last_processed_id": "4102444800:3", "status": "blocked"}]


@pytest.mark.asyncio
async def test_recent_rate_limits_normalizes_metadata_and_active_state():
    payload = await get_recent_rate_limits(_Conn())

    assert payload["events"][0]["metadata"] == {"retry": 30}
    assert payload["recent_summary"][0]["active_now"] is True
    assert payload["active_event_summary"][0]["source"] == "x"
    assert payload["active"][0]["streak"] == 3
