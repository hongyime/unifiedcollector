"""Tests for src/collectors/matrix_writer.py — Wave 1 Phase 1.

Two layers:

  1. Pure unit tests for `EventNormalizer.normalize` over 8 fixture events
     covering each event_type/msgtype combo we ingest.

  2. Writer tests that drive `MatrixEventWriter` with both a fake-pool
     mock (covers SQL shape, dispatch, decryption-worker hooks) and a
     real DB-tagged path (gated by env DB_TESTS=1) that exercises
     ON CONFLICT DO NOTHING against the live postgres container.

No matrix-nio imports here — every event is a plain dict that mirrors
the canonical Matrix client-server JSON wire format.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.collectors.matrix_writer import (
    EventNormalizer,
    MatrixEventWriter,
)


# ── canonical sample events (8) ───────────────────────────────────────────


ROOM_ID = "!room:beeper.com"


def _msg(event_id: str, msgtype: str, body: str, **content_extra) -> dict:
    return {
        "event_id":         event_id,
        "type":             "m.room.message",
        "sender":           "@alice:beeper.com",
        "origin_server_ts": 1_700_000_000_000,
        "content":          {"msgtype": msgtype, "body": body, **content_extra},
    }


SAMPLE_EVENTS = {
    # 1. plain text message
    "text": _msg("$1:srv", "m.text", "hello world"),

    # 2. formatted (HTML) text
    "text_formatted": _msg(
        "$2:srv", "m.text", "hello world",
        format="org.matrix.custom.html",
        formatted_body="<b>hello world</b>",
    ),

    # 3. image with mxc URL
    "image": _msg(
        "$3:srv", "m.image", "cat.jpg",
        url="mxc://beeper.com/abc123",
    ),

    # 4. video (mxc)
    "video": _msg(
        "$4:srv", "m.video", "clip.mp4",
        url="mxc://beeper.com/vid456",
    ),

    # 5. audio (mxc)
    "audio": _msg(
        "$5:srv", "m.audio", "voice.ogg",
        url="mxc://beeper.com/audio789",
    ),

    # 6. encrypted-but-not-yet-decrypted (the fallback path)
    "encrypted": {
        "event_id":         "$6:srv",
        "type":             "m.room.encrypted",
        "sender":           "@bob:beeper.com",
        "origin_server_ts": 1_700_000_001_000,
        "content": {
            "algorithm":  "m.megolm.v1.aes-sha2",
            "ciphertext": "AwgAEnAk...==",
            "device_id":  "DEV1",
            "sender_key": "Curve25519Key",
            "session_id": "SESSIONID",
        },
    },

    # 7. reaction (annotation)
    "reaction": {
        "event_id":         "$7:srv",
        "type":             "m.reaction",
        "sender":           "@carol:beeper.com",
        "origin_server_ts": 1_700_000_002_000,
        "content": {
            "m.relates_to": {
                "rel_type":  "m.annotation",
                "event_id":  "$1:srv",
                "key":       "👍",
            }
        },
    },

    # 8. redaction
    "redaction": {
        "event_id":         "$8:srv",
        "type":             "m.room.redaction",
        "sender":           "@alice:beeper.com",
        "origin_server_ts": 1_700_000_003_000,
        "redacts":          "$1:srv",
        "content": {"reason": "typo"},
    },
}


# ── normalizer tests ──────────────────────────────────────────────────────


class TestEventNormalizer:

    def test_text_message(self):
        row = EventNormalizer.normalize(SAMPLE_EVENTS["text"], ROOM_ID)
        assert row is not None
        assert row["event_id"] == "$1:srv"
        assert row["room_id"] == ROOM_ID
        assert row["sender"] == "@alice:beeper.com"
        assert row["event_type"] == "m.room.message"
        assert row["msgtype"] == "m.text"
        assert row["body"] == "hello world"
        assert row["formatted_body"] is None
        assert row["is_encrypted"] is False
        assert row["is_decrypted"] is False
        assert row["media_mxc"] is None
        assert isinstance(row["server_ts"], datetime)
        assert row["server_ts"].tzinfo is not None
        assert row["is_edit"] is False
        assert row["is_reaction"] is False
        assert row["is_redacted"] is False

    def test_text_formatted(self):
        row = EventNormalizer.normalize(SAMPLE_EVENTS["text_formatted"], ROOM_ID)
        assert row is not None
        assert row["body"] == "hello world"
        assert row["formatted_body"] == "<b>hello world</b>"

    def test_image_extracts_media_mxc(self):
        row = EventNormalizer.normalize(SAMPLE_EVENTS["image"], ROOM_ID)
        assert row is not None
        assert row["msgtype"] == "m.image"
        assert row["media_mxc"] == "mxc://beeper.com/abc123"
        assert row["media_local_path"] is None  # not yet downloaded
        assert row["media_sha256"] is None

    def test_video_extracts_media_mxc(self):
        row = EventNormalizer.normalize(SAMPLE_EVENTS["video"], ROOM_ID)
        assert row is not None
        assert row["msgtype"] == "m.video"
        assert row["media_mxc"] == "mxc://beeper.com/vid456"

    def test_audio_extracts_media_mxc(self):
        row = EventNormalizer.normalize(SAMPLE_EVENTS["audio"], ROOM_ID)
        assert row is not None
        assert row["msgtype"] == "m.audio"
        assert row["media_mxc"] == "mxc://beeper.com/audio789"

    def test_encrypted_undecrypted(self):
        row = EventNormalizer.normalize(SAMPLE_EVENTS["encrypted"], ROOM_ID)
        assert row is not None
        assert row["event_type"] == "m.room.encrypted"
        assert row["body"] is None
        assert row["msgtype"] is None
        assert row["is_encrypted"] is True
        assert row["is_decrypted"] is False
        # Full ciphertext blob retained for the decryption worker.
        assert row["raw_content"]["ciphertext"] == "AwgAEnAk...=="
        assert row["raw_content"]["session_id"] == "SESSIONID"

    def test_encrypted_already_decrypted(self):
        """If nio decrypted inline, content carries decrypted=True + msgtype."""
        ev = {
            "event_id":         "$6b:srv",
            "type":             "m.room.encrypted",
            "sender":           "@bob:beeper.com",
            "origin_server_ts": 1_700_000_001_500,
            "content": {
                "decrypted": True,
                "msgtype":   "m.text",
                "body":      "secret message",
            },
        }
        row = EventNormalizer.normalize(ev, ROOM_ID)
        assert row is not None
        assert row["is_encrypted"] is True
        assert row["is_decrypted"] is True
        assert row["body"] == "secret message"
        assert row["msgtype"] == "m.text"

    def test_reaction(self):
        row = EventNormalizer.normalize(SAMPLE_EVENTS["reaction"], ROOM_ID)
        assert row is not None
        assert row["event_type"] == "m.reaction"
        assert row["is_reaction"] is True
        assert row["relates_to"] == "$1:srv"
        assert row["relation_type"] == "m.annotation"
        assert row["body"] == "👍"

    def test_redaction(self):
        row = EventNormalizer.normalize(SAMPLE_EVENTS["redaction"], ROOM_ID)
        assert row is not None
        assert row["event_type"] == "m.room.redaction"
        assert row["is_redacted"] is True
        assert row["relates_to"] == "$1:srv"

    def test_skips_state_events(self):
        for skip_type in ("m.room.member", "m.room.name", "m.room.topic"):
            ev = {
                "event_id":         f"${skip_type}:srv",
                "type":             skip_type,
                "sender":           "@u:srv",
                "origin_server_ts": 1_700_000_000_000,
                "content":          {},
            }
            assert EventNormalizer.normalize(ev, ROOM_ID) is None
            assert EventNormalizer.should_skip(skip_type) is True

    def test_skips_malformed_event(self):
        # missing event_id
        bad = {"type": "m.room.message", "sender": "@u:s", "origin_server_ts": 1, "content": {}}
        assert EventNormalizer.normalize(bad, ROOM_ID) is None

    def test_edit_relation(self):
        ev = _msg(
            "$edit:srv", "m.text", "* corrected text",
            **{"m.relates_to": {"rel_type": "m.replace", "event_id": "$1:srv"}},
        )
        # _msg merges via **content_extra; m.relates_to lives under content
        ev["content"]["m.relates_to"] = {"rel_type": "m.replace", "event_id": "$1:srv"}
        row = EventNormalizer.normalize(ev, ROOM_ID)
        assert row is not None
        assert row["is_edit"] is True
        assert row["relates_to"] == "$1:srv"
        assert row["relation_type"] == "m.replace"

    def test_reply_relation(self):
        ev = _msg("$reply:srv", "m.text", "yes!")
        ev["content"]["m.relates_to"] = {"m.in_reply_to": {"event_id": "$1:srv"}}
        row = EventNormalizer.normalize(ev, ROOM_ID)
        assert row is not None
        assert row["is_edit"] is False
        assert row["relates_to"] == "$1:srv"
        assert row["relation_type"] == "m.in_reply_to"

    def test_ts_conversion_handles_int_ms(self):
        row = EventNormalizer.normalize(SAMPLE_EVENTS["text"], ROOM_ID)
        # 1_700_000_000_000 ms = 2023-11-14 22:13:20 UTC
        assert row["server_ts"] == datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)

    def test_ts_conversion_handles_garbage(self, caplog):
        ev = dict(SAMPLE_EVENTS["text"])
        ev["origin_server_ts"] = "not-a-number"
        with caplog.at_level("WARNING"):
            row = EventNormalizer.normalize(ev, ROOM_ID)
        assert row is not None
        assert isinstance(row["server_ts"], datetime)


# ── writer unit tests (mock pool) ─────────────────────────────────────────


class FakeConn:
    """Minimal asyncpg connection stub, records every (sql, args) call."""

    def __init__(self, execute_returns="INSERT 0 1"):
        self.calls: list[tuple[str, tuple]] = []
        self._execute_returns = execute_returns
        self._fetch_records: list[dict] = []

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        if callable(self._execute_returns):
            return self._execute_returns(sql, args)
        return self._execute_returns

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return self._fetch_records

    # transaction context manager
    def transaction(self):
        conn = self
        class _Tx:
            async def __aenter__(self_):
                return conn
            async def __aexit__(self_, *exc):
                return False
        return _Tx()


class FakePool:
    def __init__(self, conn: FakeConn):
        self.conn = conn

    def acquire(self):
        conn = self.conn
        class _Acq:
            async def __aenter__(self_):
                return conn
            async def __aexit__(self_, *exc):
                return False
        return _Acq()


@pytest.mark.asyncio
async def test_writer_requires_pool():
    with pytest.raises(ValueError):
        MatrixEventWriter(pool=None)


@pytest.mark.asyncio
async def test_write_event_inserts_text():
    conn = FakeConn(execute_returns="INSERT 0 1")
    pool = FakePool(conn)
    writer = MatrixEventWriter(pool)

    ok = await writer.write_event(ROOM_ID, SAMPLE_EVENTS["text"])
    assert ok is True
    assert len(conn.calls) == 1
    sql, args = conn.calls[0]
    assert "INSERT INTO matrix_events" in sql
    assert "ON CONFLICT (event_id) DO NOTHING" in sql
    # event_id, room_id, sender are positional args 1-3.
    assert args[0] == "$1:srv"
    assert args[1] == ROOM_ID
    assert args[2] == "@alice:beeper.com"
    # raw_content arg (index 6) is JSON-encoded
    raw = json.loads(args[6])
    assert raw["msgtype"] == "m.text"


@pytest.mark.asyncio
async def test_write_event_skips_state_event():
    conn = FakeConn()
    pool = FakePool(conn)
    writer = MatrixEventWriter(pool)
    ok = await writer.write_event(ROOM_ID, {
        "event_id": "$st:srv", "type": "m.room.member", "sender": "@a:b",
        "origin_server_ts": 1, "content": {},
    })
    assert ok is False
    assert conn.calls == []  # never went to db


@pytest.mark.asyncio
async def test_write_event_returns_false_on_conflict():
    conn = FakeConn(execute_returns="INSERT 0 0")  # ON CONFLICT skipped
    pool = FakePool(conn)
    writer = MatrixEventWriter(pool)
    ok = await writer.write_event(ROOM_ID, SAMPLE_EVENTS["text"])
    assert ok is False


@pytest.mark.asyncio
async def test_write_batch_counts_only_new_rows():
    # Alternate INSERT 0 1 / INSERT 0 0 to simulate one new + one dup.
    seq = iter(["INSERT 0 1", "INSERT 0 0", "INSERT 0 1"])
    conn = FakeConn(execute_returns=lambda *_: next(seq))
    pool = FakePool(conn)
    writer = MatrixEventWriter(pool)

    # 3 valid events + 1 state event (skipped pre-DB).
    events = [
        SAMPLE_EVENTS["text"],
        SAMPLE_EVENTS["image"],
        SAMPLE_EVENTS["reaction"],
        {"event_id": "$skip:s", "type": "m.room.member", "sender": "@a:b",
         "origin_server_ts": 1, "content": {}},
    ]
    inserted = await writer.write_batch(ROOM_ID, events)
    assert inserted == 2  # only the two "INSERT 0 1" results count


@pytest.mark.asyncio
async def test_mark_media_downloaded():
    conn = FakeConn(execute_returns="UPDATE 1")
    pool = FakePool(conn)
    writer = MatrixEventWriter(pool)
    ok = await writer.mark_media_downloaded("$3:srv", "/Z/media/cat.jpg", "deadbeef")
    assert ok is True
    sql, args = conn.calls[0]
    assert "UPDATE matrix_events" in sql
    assert args == ("$3:srv", "/Z/media/cat.jpg", "deadbeef")


@pytest.mark.asyncio
async def test_get_undecrypted_events():
    conn = FakeConn()
    conn._fetch_records = [
        {"event_id": "$6:srv", "room_id": ROOM_ID, "sender": "@bob:beeper.com",
         "server_ts": datetime.now(timezone.utc),
         "raw_content": {"ciphertext": "..."}},
    ]
    pool = FakePool(conn)
    writer = MatrixEventWriter(pool)
    rows = await writer.get_undecrypted_events(limit=50)
    assert len(rows) == 1
    assert rows[0]["event_id"] == "$6:srv"
    assert rows[0]["raw_content"]["ciphertext"] == "..."
    sql, args = conn.calls[0]
    assert "is_encrypted = TRUE" in sql
    assert "is_decrypted = FALSE" in sql
    assert args == (50,)


@pytest.mark.asyncio
async def test_get_undecrypted_decodes_string_jsonb():
    """If asyncpg gave us raw_content as a string (older codec), we decode it."""
    conn = FakeConn()
    conn._fetch_records = [
        {"event_id": "$x", "room_id": ROOM_ID, "sender": "@s",
         "server_ts": datetime.now(timezone.utc),
         "raw_content": '{"ciphertext":"abc"}'},
    ]
    pool = FakePool(conn)
    writer = MatrixEventWriter(pool)
    rows = await writer.get_undecrypted_events()
    assert rows[0]["raw_content"] == {"ciphertext": "abc"}


@pytest.mark.asyncio
async def test_get_undecrypted_zero_limit_short_circuits():
    conn = FakeConn()
    pool = FakePool(conn)
    writer = MatrixEventWriter(pool)
    assert await writer.get_undecrypted_events(limit=0) == []
    assert conn.calls == []


@pytest.mark.asyncio
async def test_update_decrypted():
    conn = FakeConn(execute_returns="UPDATE 1")
    pool = FakePool(conn)
    writer = MatrixEventWriter(pool)
    ok = await writer.update_decrypted(
        "$6:srv",
        body="secret",
        raw_content={"msgtype": "m.text", "body": "secret"},
        msgtype="m.text",
    )
    assert ok is True
    sql, args = conn.calls[0]
    assert "is_decrypted   = TRUE" in sql
    assert "is_encrypted = TRUE" in sql  # only update rows that were encrypted
    assert args[0] == "$6:srv"
    assert args[1] == "secret"
    assert json.loads(args[2]) == {"msgtype": "m.text", "body": "secret"}
    assert args[3] == "m.text"


@pytest.mark.asyncio
async def test_update_decrypted_returns_false_when_no_match():
    conn = FakeConn(execute_returns="UPDATE 0")
    pool = FakePool(conn)
    writer = MatrixEventWriter(pool)
    ok = await writer.update_decrypted("$missing", body="x", raw_content={})
    assert ok is False


# ── live DB tests (gated) ─────────────────────────────────────────────────


_DB_TESTS = os.environ.get("DB_TESTS", "0") in ("1", "true", "yes")


@pytest.mark.skipif(not _DB_TESTS, reason="DB_TESTS not set")
@pytest.mark.asyncio
async def test_db_on_conflict_do_nothing():
    """Hit the real postgres container: insert twice, second is a no-op."""
    import asyncpg
    dsn = os.environ.get(
        "DATABASE_URL",
        "postgresql://collector:collector@localhost:5432/unifiedcollector",
    )
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        writer = MatrixEventWriter(pool)
        ev = dict(SAMPLE_EVENTS["text"])
        ev["event_id"] = f"$test_{uuid.uuid4().hex}:srv"
        try:
            ok1 = await writer.write_event(ROOM_ID, ev)
            ok2 = await writer.write_event(ROOM_ID, ev)
            assert ok1 is True
            assert ok2 is False  # ON CONFLICT skipped the second attempt
        finally:
            async with pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM matrix_events WHERE event_id = $1",
                    ev["event_id"],
                )
    finally:
        await pool.close()


@pytest.mark.skipif(not _DB_TESTS, reason="DB_TESTS not set")
@pytest.mark.asyncio
async def test_db_decryption_lifecycle():
    """End-to-end: insert encrypted row, fetch via get_undecrypted_events,
    settle via update_decrypted, confirm body + flags."""
    import asyncpg
    dsn = os.environ.get(
        "DATABASE_URL",
        "postgresql://collector:collector@localhost:5432/unifiedcollector",
    )
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        writer = MatrixEventWriter(pool)
        ev = dict(SAMPLE_EVENTS["encrypted"])
        ev["event_id"] = f"$enc_{uuid.uuid4().hex}:srv"
        try:
            await writer.write_event(ROOM_ID, ev)
            pending = await writer.get_undecrypted_events(limit=1000)
            assert any(r["event_id"] == ev["event_id"] for r in pending)

            ok = await writer.update_decrypted(
                ev["event_id"],
                body="plaintext-now",
                raw_content={"msgtype": "m.text", "body": "plaintext-now"},
                msgtype="m.text",
            )
            assert ok is True

            pending2 = await writer.get_undecrypted_events(limit=1000)
            assert not any(r["event_id"] == ev["event_id"] for r in pending2)

            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT body, is_decrypted, msgtype FROM matrix_events WHERE event_id=$1",
                    ev["event_id"],
                )
                assert row["body"] == "plaintext-now"
                assert row["is_decrypted"] is True
                assert row["msgtype"] == "m.text"
        finally:
            async with pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM matrix_events WHERE event_id = $1",
                    ev["event_id"],
                )
    finally:
        await pool.close()
