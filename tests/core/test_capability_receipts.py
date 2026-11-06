from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.core.capability_receipts import record_field_readback_receipt, record_profile_field_receipts
from src.core.capability_receipts import latest_capability_receipts


class _Conn:
    def __init__(self, row):
        self.row = row
        self.calls = []

    async def fetchrow(self, query, *_args):
        self.calls.append((query, _args))
        return self.row

    async def fetchval(self, query, *args):
        self.calls.append((query, args))
        return "receipt"


class _ListConn:
    async def fetch(self, _query):
        return [{"source": "lemon8", "capability": "profile.followers_count", "state": "proven",
                 "path": "collector_native_profile", "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)}]


@pytest.mark.asyncio
async def test_fresh_field_readback_creates_a_proven_receipt_without_profile_data():
    conn = _Conn({"id": "row", "updated_at": datetime(2026, 1, 1, tzinfo=timezone.utc)})

    receipt = await record_field_readback_receipt(
        conn, "lemon8", "profile.followers_count", max_age=timedelta(hours=1),
    )

    assert receipt["state"] == "proven"
    assert receipt["proof"]["durable_row_id"] == "row"
    assert "followers_count" in conn.calls[0][0]
    assert conn.calls[0][1] == (timedelta(hours=1),)


@pytest.mark.asyncio
async def test_missing_field_readback_records_failed_receipt():
    conn = _Conn(None)

    receipt = await record_field_readback_receipt(
        conn, "lemon8", "profile.followers_count", max_age=timedelta(hours=1),
    )

    assert receipt["state"] == "failed"
    assert receipt["proof"]["durable_row_id"] is None


@pytest.mark.asyncio
async def test_facebook_profile_stat_receipt_uses_current_browser_profile_rows():
    conn = _Conn(None)

    receipt = await record_field_readback_receipt(
        conn, "facebook", "profile.followers_count", max_age=timedelta(hours=1),
    )

    assert receipt["state"] == "failed"
    assert "facebook_profiles" in conn.calls[0][0]
    assert "followers_count" in conn.calls[0][0]


@pytest.mark.asyncio
async def test_whatsapp_avatar_receipt_uses_provider_canary_url_field():
    conn = _Conn({"id": "contact", "updated_at": datetime(2026, 1, 1, tzinfo=timezone.utc)})

    receipt = await record_field_readback_receipt(
        conn, "whatsapp", "profile.avatar_url", max_age=timedelta(hours=1),
    )

    assert receipt["state"] == "proven"
    assert "whatsapp_users" in conn.calls[0][0]
    assert "photo_url" in conn.calls[0][0]


@pytest.mark.asyncio
async def test_unsupported_capability_is_not_silently_enabled():
    with pytest.raises(ValueError, match="no bounded readback canary"):
        await record_field_readback_receipt(
            _Conn(None), "facebook", "graph.two_hop", max_age=timedelta(hours=1),
        )


@pytest.mark.asyncio
async def test_pinterest_provenance_receipt_requires_verified_owner_predicate():
    conn = _Conn({"id": "pin", "updated_at": datetime(2026, 1, 1, tzinfo=timezone.utc)})

    receipt = await record_field_readback_receipt(
        conn, "pinterest", "pin.creator_provenance", max_age=timedelta(hours=6),
    )

    assert "owner_verified" in conn.calls[0][0]
    assert receipt["state"] == "proven"
    assert receipt["proof"]["path"] == "collector_verified_owner_pin"


@pytest.mark.asyncio
async def test_threads_post_receipt_uses_collected_timestamp():
    conn = _Conn({"id": "post", "updated_at": datetime(2026, 1, 1, tzinfo=timezone.utc)})

    receipt = await record_field_readback_receipt(
        conn, "threads", "post.author_username", max_age=timedelta(hours=24),
    )

    assert receipt["state"] == "proven"
    assert "threads_posts" in conn.calls[0][0]
    assert "collected_at AS updated_at" in conn.calls[0][0]


@pytest.mark.asyncio
async def test_threads_avatar_receipt_requires_threads_profile_observation():
    conn = _Conn(None)

    receipt = await record_field_readback_receipt(
        conn, "threads", "profile.avatar_url", max_age=timedelta(hours=24),
    )

    assert receipt["state"] == "failed"
    assert "social_users" in conn.calls[0][0]
    assert "platform='threads'" in conn.calls[0][0]


@pytest.mark.asyncio
async def test_tiktok_avatar_receipt_uses_native_profile_rows():
    conn = _Conn(None)

    receipt = await record_field_readback_receipt(
        conn, "tiktok", "profile.avatar_url", max_age=timedelta(hours=24),
    )

    assert receipt["state"] == "failed"
    assert "tiktok_profiles" in conn.calls[0][0]
    assert "avatar_url" in conn.calls[0][0]


@pytest.mark.asyncio
async def test_snapchat_bitmoji_receipt_names_the_browser_dom_path():
    conn = _Conn(None)

    receipt = await record_field_readback_receipt(
        conn, "snapchat", "profile.bitmoji_url", max_age=timedelta(hours=24),
    )

    assert receipt["state"] == "failed"
    assert receipt["proof"]["path"] == "browser_snapchat_public_profile_dom"


@pytest.mark.asyncio
async def test_snapchat_bitmoji_receipt_is_limited_to_public_profile_rows():
    conn = _Conn(None)

    receipt = await record_field_readback_receipt(
        conn, "snapchat", "profile.bitmoji_url", max_age=timedelta(hours=24),
    )

    assert receipt["state"] == "failed"
    assert "snapchat_profiles" in conn.calls[0][0]
    assert "bitmoji_url" in conn.calls[0][0]


@pytest.mark.asyncio
async def test_pinterest_avatar_receipt_uses_profile_probe_timestamp():
    conn = _Conn({"id": "creator", "updated_at": datetime(2026, 1, 1, tzinfo=timezone.utc)})

    receipt = await record_field_readback_receipt(
        conn, "pinterest", "profile.avatar_url", max_age=timedelta(hours=24),
    )

    assert receipt["state"] == "proven"
    assert "pinterest_profiles" in conn.calls[0][0]
    assert "username::text AS id" in conn.calls[0][0]
    assert "default" in conn.calls[0][0]
    assert "probed_at AS updated_at" in conn.calls[0][0]


@pytest.mark.asyncio
async def test_latest_receipts_excludes_durable_row_identity():
    rows = await latest_capability_receipts(_ListConn())

    assert rows == [{"source": "lemon8", "capability": "profile.followers_count", "state": "proven",
                     "path": "collector_native_profile", "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)}]


@pytest.mark.asyncio
async def test_browser_profile_receipts_are_field_specific_and_fail_closed():
    conn = _Conn(None)

    receipts = await record_profile_field_receipts(conn, "facebook")

    assert [receipt["capability"] for receipt in receipts] == [
        "profile.avatar_url", "profile.bio", "profile.followers_count",
    ]
    assert all(receipt["state"] == "failed" for receipt in receipts)
