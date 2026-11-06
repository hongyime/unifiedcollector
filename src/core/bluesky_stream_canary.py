"""Bounded Bluesky firehose canary; it never turns on a live collector itself."""
from __future__ import annotations

import asyncio
import os
import struct
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode
from urllib.parse import urlsplit


_DEFAULT_ENDPOINT = "wss://bsky.network/xrpc/com.atproto.sync.subscribeRepos"
_MAX_FRAME_BYTES = 5 * 1024 * 1024
_RECONNECT_BACKOFF_MIN_SECONDS = 300
_RECONNECT_BACKOFF_MAX_SECONDS = 6 * 3600
_PROFILE_COLLECTION = "app.bsky.actor.profile/"


class CborFrameError(ValueError):
    pass


def _read_size(data: bytes, offset: int, info: int) -> tuple[int, int]:
    if info < 24:
        return info, offset
    widths = {24: 1, 25: 2, 26: 4, 27: 8}
    width = widths.get(info)
    if width is None or offset + width > len(data):
        raise CborFrameError("invalid CBOR length")
    return int.from_bytes(data[offset:offset + width], "big"), offset + width


def _decode(data: bytes, offset: int = 0, depth: int = 0) -> tuple[Any, int]:
    if depth > 32 or offset >= len(data):
        raise CborFrameError("invalid CBOR nesting")
    lead = data[offset]
    offset += 1
    major, info = lead >> 5, lead & 31
    size, offset = _read_size(data, offset, info)
    if major == 0:
        return size, offset
    if major == 1:
        return -1 - size, offset
    if major in {2, 3}:
        if offset + size > len(data):
            raise CborFrameError("truncated CBOR value")
        value = data[offset:offset + size]
        if major == 3:
            try:
                value = value.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise CborFrameError("invalid CBOR text") from exc
        return value, offset + size
    if major == 4:
        if size > 2048:
            raise CborFrameError("CBOR array too large")
        values = []
        for _ in range(size):
            value, offset = _decode(data, offset, depth + 1)
            values.append(value)
        return values, offset
    if major == 5:
        if size > 2048:
            raise CborFrameError("CBOR map too large")
        values: dict[Any, Any] = {}
        for _ in range(size):
            key, offset = _decode(data, offset, depth + 1)
            value, offset = _decode(data, offset, depth + 1)
            values[key] = value
        return values, offset
    if major == 6:
        return _decode(data, offset, depth + 1)
    if major == 7:
        if info == 20:
            return False, offset
        if info == 21:
            return True, offset
        if info in {22, 23}:
            return None, offset
        if info == 25:
            return struct.unpack(">e", data[offset - 2:offset])[0], offset
        if info == 26:
            return struct.unpack(">f", data[offset - 4:offset])[0], offset
        if info == 27:
            return struct.unpack(">d", data[offset - 8:offset])[0], offset
    raise CborFrameError("unsupported CBOR value")


def _stream_payload(frame: bytes) -> dict[str, Any] | None:
    if not isinstance(frame, bytes) or not frame or len(frame) > _MAX_FRAME_BYTES:
        return None
    try:
        header, offset = _decode(frame)
        payload, offset = _decode(frame, offset)
    except CborFrameError:
        return None
    if offset != len(frame) or not isinstance(header, dict) or not isinstance(payload, dict):
        return None
    if header.get("op") != 1:
        return None
    return payload


def stream_sequence(frame: bytes) -> int | None:
    """Read the sequence only from a valid two-object AT Protocol frame."""
    payload = _stream_payload(frame)
    if payload is None:
        return None
    sequence = payload.get("seq")
    return sequence if isinstance(sequence, int) and sequence >= 0 else None


def profile_change_event(frame: bytes) -> tuple[int, str] | None:
    """Return a profile-record change without decoding or storing record data."""
    payload = _stream_payload(frame)
    if payload is None:
        return None
    sequence = payload.get("seq")
    repo = payload.get("repo")
    operations = payload.get("ops")
    if not isinstance(sequence, int) or sequence < 0 or not isinstance(repo, str):
        return None
    if not isinstance(operations, list):
        return None
    if any(
        isinstance(operation, dict)
        and isinstance(operation.get("path"), str)
        and operation["path"].startswith(_PROFILE_COLLECTION)
        for operation in operations
    ):
        return sequence, repo
    return None


def _enabled() -> bool:
    return os.getenv("BLUESKY_STREAM_CANARY_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def _live_enabled() -> bool:
    """A canary opt-in enables live changes only after its receipt exists."""
    return _enabled()


def _endpoint() -> str:
    value = os.getenv("BLUESKY_STREAM_CANARY_ENDPOINT", _DEFAULT_ENDPOINT).strip()
    parsed = urlsplit(value)
    if parsed.scheme != "wss" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Bluesky stream canary endpoint must be a credential-free wss URL")
    return value


def _retry_interval() -> timedelta:
    try:
        seconds = int(os.getenv("BLUESKY_STREAM_CANARY_INTERVAL_SECONDS", "21600"))
    except ValueError:
        seconds = 21600
    return timedelta(seconds=max(300, min(seconds, 86400)))


def _reconnect_backoff_seconds(failure_count: int) -> int:
    """Bound retry pressure after a failed canary connection/read."""
    return min(
        _RECONNECT_BACKOFF_MAX_SECONDS,
        _RECONNECT_BACKOFF_MIN_SECONDS * (2 ** max(0, min(int(failure_count) - 1, 12))),
    )


async def _store_receipt(pool, *, state: str, first_cursor: int | None,
                         reconnect_cursor: int | None, failure_count: int = 0,
                         error: str | None = None) -> dict | None:
    """Persist either a successful receipt or a bounded failed-attempt wall."""
    delay = _reconnect_backoff_seconds(failure_count) if state == "failed" else None
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO bluesky_stream_receipts
                   (source_name, state, first_cursor, reconnect_cursor, failure_count,
                    next_attempt_at, last_error, checked_at, updated_at)
               VALUES('bluesky',$1,$2,$3,$4,
                      CASE WHEN $5::interval IS NULL THEN NULL ELSE now()+$5::interval END,
                      $6,now(),now())
               ON CONFLICT(source_name) DO UPDATE SET
                   state=EXCLUDED.state,
                   first_cursor=EXCLUDED.first_cursor,
                   reconnect_cursor=EXCLUDED.reconnect_cursor,
                   failure_count=EXCLUDED.failure_count,
                   next_attempt_at=EXCLUDED.next_attempt_at,
                   last_error=EXCLUDED.last_error,
                   checked_at=now(), updated_at=now()""",
            state, first_cursor, reconnect_cursor,
            failure_count if state == "failed" else 0,
            f"{delay} seconds" if delay is not None else None,
            error[:500] if error else None,
        )
        if state != "ready":
            return None
        return await conn.fetchrow(
            "SELECT state, reconnect_cursor FROM bluesky_stream_receipts WHERE source_name='bluesky'"
        )


def _read_cursor(endpoint: str, cursor: int | None, timeout_seconds: float) -> int | None:
    # websocket-client is already a project dependency. Import lazily so a
    # disabled canary cannot make normal profile polling fail at import time.
    import websocket

    query = urlencode({"cursor": cursor}) if cursor is not None else ""
    url = endpoint + ("&" if "?" in endpoint else "?") + query if query else endpoint
    deadline = time.monotonic() + timeout_seconds
    ws = websocket.create_connection(url, timeout=timeout_seconds)
    try:
        while time.monotonic() < deadline:
            frame = ws.recv()
            if isinstance(frame, str):
                continue
            sequence = stream_sequence(frame)
            if sequence is not None:
                return sequence
    finally:
        ws.close()
    return None


def _read_profile_changes(
    endpoint: str,
    cursor: int | None,
    timeout_seconds: float,
    max_events: int,
) -> tuple[int | None, tuple[str, ...]]:
    """Read a finite firehose slice and return only profile-change DIDs."""
    import websocket

    query = urlencode({"cursor": cursor}) if cursor is not None else ""
    url = endpoint + ("&" if "?" in endpoint else "?") + query if query else endpoint
    deadline = time.monotonic() + timeout_seconds
    latest = cursor
    profile_dids: set[str] = set()
    received = 0
    ws = websocket.create_connection(url, timeout=timeout_seconds)
    try:
        while time.monotonic() < deadline and received < max_events:
            frame = ws.recv()
            if isinstance(frame, str):
                continue
            received += 1
            sequence = stream_sequence(frame)
            if sequence is not None and (latest is None or sequence > latest):
                latest = sequence
            change = profile_change_event(frame)
            if change is not None:
                profile_dids.add(change[1])
    finally:
        ws.close()
    return latest, tuple(sorted(profile_dids))


@dataclass(frozen=True)
class CanaryReceipt:
    first_cursor: int | None
    reconnect_cursor: int | None
    ready: bool


async def run_bluesky_stream_canary(pool, *, reader=_read_cursor) -> dict:
    """Require cursor persistence, reconnect, and DB readback before success.

    A successful receipt is deliberately not a live-mode switch. It is the
    prerequisite a future stream worker must check before advertising native
    realtime capability.
    """
    if not _enabled() or pool is None:
        return {"processed": 0, "skipped": "disabled"}
    try:
        endpoint = _endpoint()
        async with pool.acquire() as conn:
            previous = await conn.fetchrow(
                """SELECT state, reconnect_cursor, checked_at, failure_count, next_attempt_at
                   FROM bluesky_stream_receipts WHERE source_name='bluesky'"""
            )
        if previous and previous["state"] == "ready":
            return {"processed": 0, "skipped": "receipt_ready"}
        now = datetime.now(timezone.utc)
        next_attempt_at = previous.get("next_attempt_at") if previous else None
        if next_attempt_at:
            next_attempt_at = next_attempt_at if next_attempt_at.tzinfo else next_attempt_at.replace(tzinfo=timezone.utc)
            if next_attempt_at > now:
                return {"processed": 0, "skipped": "reconnect_backoff"}
        checked_at = previous["checked_at"] if previous else None
        # Existing failed rows from before ``next_attempt_at`` was introduced
        # retain the old bounded interval once; newly written rows use their
        # precise exponential reconnect wall above.
        if checked_at and not next_attempt_at:
            checked_at = checked_at if checked_at.tzinfo else checked_at.replace(tzinfo=timezone.utc)
            if checked_at + _retry_interval() > now:
                return {"processed": 0, "skipped": "retry_interval"}
        prior = previous["reconnect_cursor"] if previous else None
        failure_count = int(previous.get("failure_count") or 0) + 1 if previous else 1
        try:
            first = await asyncio.to_thread(reader, endpoint, int(prior) if prior is not None else None, 5.0)
        except Exception as exc:
            await _store_receipt(
                pool, state="failed", first_cursor=int(prior) if prior is not None else None,
                reconnect_cursor=None, failure_count=failure_count, error=type(exc).__name__,
            )
            return {"processed": 0, "skipped": "reader_error"}
        if first is None:
            await _store_receipt(
                pool, state="failed", first_cursor=int(prior) if prior is not None else None,
                reconnect_cursor=None, failure_count=failure_count, error="no_cursor",
            )
            return {"processed": 0, "skipped": "no_cursor"}
        try:
            second = await asyncio.to_thread(reader, endpoint, first, 5.0)
        except Exception as exc:
            second = None
            error = type(exc).__name__
        else:
            error = "reconnect_cursor_missing" if second is None else None
        receipt = CanaryReceipt(first, second, second is not None and second >= first)
        readback = await _store_receipt(
            pool,
            state="ready" if receipt.ready else "failed",
            first_cursor=receipt.first_cursor,
            reconnect_cursor=receipt.reconnect_cursor,
            failure_count=failure_count,
            error=error,
        )
        durable = bool(readback and readback["state"] == "ready" and int(readback["reconnect_cursor"]) == receipt.reconnect_cursor)
        return {"processed": int(durable), "first_cursor": first, "reconnect_cursor": second, "ready": durable}
    except Exception as exc:
        return {"processed": 0, "error": type(exc).__name__}


async def run_bluesky_profile_change_stream(pool, *, reader=_read_profile_changes) -> dict:
    """Advance a bounded, receipt-gated Bluesky profile-change stream.

    The caller filters returned DIDs to profiles it already owns before
    hydration, so this never persists arbitrary firehose users or records.
    """
    if not _live_enabled() or pool is None:
        return {"processed": 0, "skipped": "disabled"}
    cursor: int | None = None
    failure_count = 1
    try:
        endpoint = _endpoint()
        async with pool.acquire() as conn:
            receipt = await conn.fetchrow(
                """SELECT state, reconnect_cursor, failure_count FROM bluesky_stream_receipts
                   WHERE source_name='bluesky'"""
            )
        if not receipt or receipt["state"] != "ready" or receipt["reconnect_cursor"] is None:
            return {"processed": 0, "skipped": "receipt_required"}
        try:
            max_events = max(1, min(512, int(os.getenv("BLUESKY_STREAM_MAX_EVENTS", "64"))))
        except ValueError:
            max_events = 64
        cursor = int(receipt["reconnect_cursor"])
        failure_count = int(receipt.get("failure_count") or 0) + 1
        latest, dids = await asyncio.to_thread(reader, endpoint, cursor, 5.0, max_events)
        if latest is None or latest < cursor:
            await _store_receipt(
                pool, state="failed", first_cursor=cursor, reconnect_cursor=None,
                failure_count=1, error="no_cursor",
            )
            return {"processed": 0, "skipped": "no_cursor"}
        async with pool.acquire() as conn:
            readback = await conn.fetchrow(
                """UPDATE bluesky_stream_receipts
                      SET reconnect_cursor=$1, checked_at=now(), updated_at=now()
                    WHERE source_name='bluesky' AND state='ready'
                  RETURNING state, reconnect_cursor""",
                latest,
            )
        if not readback or readback["state"] != "ready" or int(readback["reconnect_cursor"]) != latest:
            return {"processed": 0, "skipped": "cursor_readback_failed"}
        return {"processed": len(dids), "cursor": latest, "profile_dids": list(dids)}
    except Exception as exc:
        if cursor is not None:
            try:
                await _store_receipt(
                    pool, state="failed", first_cursor=cursor, reconnect_cursor=None,
                    failure_count=failure_count, error=type(exc).__name__,
                )
            except Exception:
                pass
        return {"processed": 0, "error": type(exc).__name__}
