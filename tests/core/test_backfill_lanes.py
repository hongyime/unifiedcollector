import pytest

from src.core.backfill_lanes import claim_backfill_lane, finish_backfill_lane, list_backfill_lanes, mirror_backfill_lane


class _Conn:
    def __init__(self): self.args = None
    async def fetchrow(self, sql, *args):
        self.args = args
        assert "FOR UPDATE SKIP LOCKED" in sql
        assert "fence_token=fence_token+1" in sql
        return {"fence_token": 2}


@pytest.mark.asyncio
async def test_claim_backfill_lane_is_fenced_and_scoped():
    conn = _Conn()
    lane = await claim_backfill_lane(conn, source="telegram", account_identity="a", target_identity="c", owner="worker", lease_seconds=30)
    assert lane["fence_token"] == 2
    assert conn.args[:4] == ("telegram", "a", "c", "worker")


class _MirrorConn:
    def __init__(self):
        self.args = None
        self.sql = ""

    async def execute(self, sql, *args):
        self.sql, self.args = sql, args


@pytest.mark.asyncio
async def test_mirror_preserves_active_lease_and_defers_errors():
    conn = _MirrorConn()

    await mirror_backfill_lane(
        conn, source="whatsapp", account_identity="wid", target_identity="chat",
        cursor={"message_id": "1"}, complete=False, error="provider timeout",
    )

    assert "backfill_lanes.status='leased' AND backfill_lanes.lease_expires_at > now()" in conn.sql
    assert "now()+interval '5 minutes'" in conn.sql
    assert conn.args[-1] == "provider timeout"


class _FinishConn:
    def __init__(self):
        self.args = None

    async def fetchrow(self, sql, *args):
        assert "WHERE id=$1 AND lease_owner=$2 AND fence_token=$3" in sql
        self.args = args
        return {"status": "complete"}


@pytest.mark.asyncio
async def test_finish_backfill_lane_requires_owner_and_fence():
    conn = _FinishConn()
    lane = await finish_backfill_lane(
        conn, lane_id="lane-id", owner="worker", fence_token=4, complete=True,
    )

    assert lane["status"] == "complete"
    assert conn.args[:3] == ("lane-id", "worker", 4)


class _ListConn:
    async def fetchval(self, *_args):
        return "backfill_lanes"

    async def fetch(self, sql, *args):
        assert "lease_owner" in sql
        assert "fence_token" in sql
        assert "item_cost" in sql
        assert args == ("whatsapp", 500)
        return [{"source": "whatsapp", "cursor": {"message_id": "1"}, "fence_token": 2}]


class _ListAcquire:
    async def __aenter__(self):
        return _ListConn()

    async def __aexit__(self, *_args):
        return False


class _ListPool:
    def acquire(self):
        return _ListAcquire()


@pytest.mark.asyncio
async def test_list_backfill_lanes_returns_bounded_contract_details():
    rows = await list_backfill_lanes(_ListPool(), source="WhatsApp", limit=999)

    assert rows == [{"source": "whatsapp", "cursor": {"message_id": "1"}, "fence_token": 2}]
