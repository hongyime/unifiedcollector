from __future__ import annotations

import pytest

from datetime import datetime, timedelta, timezone

from src.core.bluesky_stream_canary import (
    profile_change_event,
    run_bluesky_profile_change_stream,
    run_bluesky_stream_canary,
    stream_sequence,
)


def test_stream_sequence_reads_two_object_cbor_frame_and_rejects_bad_frames():
    # {"op": 1}, then {"seq": 42}; AT Protocol frames concatenate both maps.
    assert stream_sequence(b"\xa1bop\x01\xa1cseq\x18*") == 42
    assert stream_sequence(b"\xa1bop\x01") is None
    assert stream_sequence(b"\xa1bop \xa1cseq\x18*") is None


def test_profile_change_event_requires_a_profile_record_path():
    did = b"did:plc:abcdefghijklmnopqrstuvwx"
    path = b"app.bsky.actor.profile/self"
    frame = (
        b"\xa1bop\x01"
        + b"\xa3cseq\x18*drepo\x78" + bytes([len(did)]) + did
        + b"cops\x81\xa1dpath\x78" + bytes([len(path)]) + path
    )

    assert profile_change_event(frame) == (42, did.decode())


class _Conn:
    def __init__(self):
        self.row = None

    async def execute(self, _query, state, first, second, failures, _delay, error):
        self.row = {
            "state": state, "first_cursor": first, "reconnect_cursor": second,
            "failure_count": failures, "last_error": error,
        }

    async def fetchrow(self, _query):
        return self.row


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_args):
        return None


class _Pool:
    def __init__(self):
        self.conn = _Conn()

    def acquire(self):
        return _Acquire(self.conn)


@pytest.mark.asyncio
async def test_canary_requires_reconnect_and_durable_readback(monkeypatch):
    monkeypatch.setenv("BLUESKY_STREAM_CANARY_ENABLED", "1")
    seen = []

    def reader(_endpoint, cursor, _timeout):
        seen.append(cursor)
        return 42 if cursor is None else 43

    outcome = await run_bluesky_stream_canary(_Pool(), reader=reader)

    assert outcome["ready"] is True
    assert outcome["reconnect_cursor"] == 43
    assert seen == [None, 42]


@pytest.mark.asyncio
async def test_canary_persists_failed_read_and_honors_reconnect_backoff(monkeypatch):
    monkeypatch.setenv("BLUESKY_STREAM_CANARY_ENABLED", "1")
    pool = _Pool()

    first = await run_bluesky_stream_canary(pool, reader=lambda *_args: None)

    assert first == {"processed": 0, "skipped": "no_cursor"}
    assert pool.conn.row["state"] == "failed"
    assert pool.conn.row["failure_count"] == 1

    pool.conn.row.update({
        "checked_at": datetime.now(timezone.utc),
        "next_attempt_at": datetime.now(timezone.utc) + timedelta(minutes=5),
    })
    second = await run_bluesky_stream_canary(pool, reader=lambda *_args: (_ for _ in ()).throw(AssertionError("reader must be backed off")))

    assert second == {"processed": 0, "skipped": "reconnect_backoff"}


@pytest.mark.asyncio
async def test_canary_retries_after_its_reconnect_backoff_expires(monkeypatch):
    monkeypatch.setenv("BLUESKY_STREAM_CANARY_ENABLED", "1")
    pool = _Pool()
    pool.conn.row = {
        "state": "failed", "reconnect_cursor": None, "failure_count": 1,
        "checked_at": datetime.now(timezone.utc),
        "next_attempt_at": datetime.now(timezone.utc) - timedelta(seconds=1),
    }
    cursors = iter((8, 9))

    outcome = await run_bluesky_stream_canary(pool, reader=lambda *_args: next(cursors))

    assert outcome["ready"] is True
    assert outcome["reconnect_cursor"] == 9


class _LiveConn:
    def __init__(self):
        self.row = {"state": "ready", "reconnect_cursor": 42}

    async def fetchrow(self, query, *args):
        if query.lstrip().startswith("UPDATE"):
            self.row["reconnect_cursor"] = args[0]
        return self.row

    async def execute(self, _query, state, first, second, failures, _delay, error):
        self.row = {
            "state": state,
            "first_cursor": first,
            "reconnect_cursor": second,
            "failure_count": failures,
            "last_error": error,
        }


class _LivePool:
    def __init__(self):
        self.conn = _LiveConn()

    def acquire(self):
        return _Acquire(self.conn)


@pytest.mark.asyncio
async def test_live_profile_stream_requires_receipt_and_durable_cursor_readback(monkeypatch):
    monkeypatch.setenv("BLUESKY_STREAM_CANARY_ENABLED", "1")
    monkeypatch.setenv("BLUESKY_STREAM_LIVE_ENABLED", "1")
    pool = _LivePool()
    did = "did:plc:abcdefghijklmnopqrstuvwx"

    outcome = await run_bluesky_profile_change_stream(
        pool,
        reader=lambda *_args: (43, (did,)),
    )

    assert outcome == {"processed": 1, "cursor": 43, "profile_dids": [did]}
    assert pool.conn.row["reconnect_cursor"] == 43


@pytest.mark.asyncio
async def test_live_profile_stream_failure_reuses_canary_backoff_receipt(monkeypatch):
    monkeypatch.setenv("BLUESKY_STREAM_CANARY_ENABLED", "1")
    pool = _LivePool()

    outcome = await run_bluesky_profile_change_stream(
        pool,
        reader=lambda *_args: (_ for _ in ()).throw(TimeoutError()),
    )

    assert outcome == {"processed": 0, "error": "TimeoutError"}
    assert pool.conn.row["state"] == "failed"
    assert pool.conn.row["reconnect_cursor"] is None
