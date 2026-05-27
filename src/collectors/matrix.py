"""Wave 1 Phase 0: read-only Matrix collector orchestrator.

Thin orchestration layer on top of `src.core.matrix_client.BeeperMatrixClient`.

Phase 0 scope (this file):
    * verify the homeserver is reachable + the access_token is still valid
      (`warmup`)
    * enumerate joined rooms with a human-readable summary
      (`discover_rooms`)
    * perform a single /sync and persist the resulting `next_batch` token
      so we can resume across restarts (`collect`)

Phase 0 is deliberately NOT doing:
    * event ingestion / writes to media_items / messages tables
    * encrypted message decryption (no key-backup recovery)
    * media download
    * outbound sends — the underlying client is read-only

This collector is gated behind the env flag `MATRIX_COLLECTOR_ENABLED`.
While the flag is unset (or "0"/"false"/"no"/""), `is_enabled()` returns
False and the scheduler will skip registering the matrix task — so the
overall service boots cleanly even on hosts with no Beeper credentials.

The companion document `MATRIX_README.md` (sibling file) describes the
manual steps an operator must take BEFORE flipping the flag to "1".
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from src.collectors.matrix_writer import MatrixEventWriter
from src.core.matrix_client import (
    BeeperMatrixClient,
    MatrixClientError,
    MatrixLoginError,
    RoomSummary,
)
from src.core.matrix_decryption import (
    MatrixDecryptionError,
    MatrixDecryptionService,
)
from src.core.matrix_media import (
    MatrixMediaDownloader,
    MatrixMediaError,
)

logger = logging.getLogger(__name__)


# ── feature gate ──────────────────────────────────────────────────────────


_ENABLED_TRUTHY = {"1", "true", "yes", "on"}


def is_enabled() -> bool:
    """Return True iff MATRIX_COLLECTOR_ENABLED is set to a truthy value.

    Default is False so production boots without Beeper credentials.
    """
    val = os.environ.get("MATRIX_COLLECTOR_ENABLED", "").strip().lower()
    return val in _ENABLED_TRUTHY


# ── orchestrator ──────────────────────────────────────────────────────────


class MatrixCollector:
    """Phase 0 read-only Matrix collector.

    Composition (no inheritance from BaseCollector — Phase 0 doesn't share
    the targets/runs lifecycle yet; that fits Phase 1's event-ingest model
    better than this discovery shim):

        client   — `BeeperMatrixClient` already constructed + logged in
        pool     — asyncpg pool (used for matrix_sync_state persistence)
        log      — optional logger override (defaults to module logger)

    All methods are async. None of them write events. None of them attempt
    to send. Calls are idempotent: repeated `collect()` simply advances
    the next_batch cursor.
    """

    def __init__(
        self,
        client: BeeperMatrixClient,
        pool: Any = None,
        log: Optional[logging.Logger] = None,
        writer: Optional[MatrixEventWriter] = None,
        decryption_service: Optional[MatrixDecryptionService] = None,
        media_downloader: Optional[MatrixMediaDownloader] = None,
    ) -> None:
        if client is None:
            raise ValueError("client is required")
        self.client = client
        self.pool = pool
        self.log = log or logger
        # Writer is constructed lazily — collectors composed without a pool
        # (Phase 0 tests, warmup-only paths) keep working with writer=None
        # and simply skip event ingestion.
        if writer is None and pool is not None:
            writer = MatrixEventWriter(pool)
        self.writer = writer
        # Crypto + media services optional. When None, collect() skips the
        # decrypt-pending and media-download stages but still ingests
        # plaintext events. Caller wires them in once Beeper creds + key
        # backup recovery are configured.
        self.decryption_service = decryption_service
        self.media_downloader = media_downloader

    # ── warmup ────────────────────────────────────────────────────────

    async def warmup(self) -> bool:
        """Verify the homeserver is reachable and the token is valid.

        Strategy: issue a single short-timeout sync_once. Returns True on
        success, False on any MatrixClientError (auth or network). Never
        raises — the caller (scheduler) uses the boolean to decide whether
        to skip the cycle.
        """
        try:
            await self.client.sync_once(timeout_ms=5_000)
        except MatrixLoginError as exc:
            self.log.error("Matrix warmup failed — token rejected: %s", exc)
            return False
        except MatrixClientError as exc:
            self.log.error("Matrix warmup failed — homeserver unreachable: %s", exc)
            return False
        except Exception as exc:  # pragma: no cover - defensive
            self.log.error("Matrix warmup failed — unexpected: %s", exc)
            return False
        self.log.info("Matrix warmup OK for %s", self.client.user_id)
        return True

    # ── room discovery ────────────────────────────────────────────────

    async def discover_rooms(self) -> dict[str, RoomSummary]:
        """List currently-joined rooms and log a human-readable summary.

        Returns the same dict that `BeeperMatrixClient.list_rooms()` returns
        so the caller can do further inspection. Note: the underlying call
        reads from the in-memory `client.rooms` cache populated by previous
        syncs, so callers should ensure at least one sync has happened
        (warmup() does this).
        """
        rooms = await self.client.list_rooms()
        self.log.info("Matrix discovered %d room(s)", len(rooms))
        for room_id, summary in sorted(
            rooms.items(),
            key=lambda kv: (kv[1].last_activity_ts or 0),
            reverse=True,
        ):
            name = summary.display_name or "(no name)"
            ts = summary.last_activity_ts
            ts_repr = f"ts={ts}" if ts is not None else "ts=?"
            enc = "E2EE" if summary.encrypted else "plain"
            self.log.info(
                "  room %s  %-30s  members=%d  %s  %s",
                room_id, name[:30], summary.member_count, enc, ts_repr,
            )
        return rooms

    # ── collect ───────────────────────────────────────────────────────

    async def collect(self) -> Optional[str]:
        """Phase 1 collect: single /sync, ingest timeline events, persist next_batch.

        Returns the new next_batch token, or None if the underlying client
        produced no token (which would be unusual but harmless).

        For each room in the SyncResponse with a non-empty timeline, every
        event is normalized + INSERTed into matrix_events.  Inserts use
        ON CONFLICT DO NOTHING so re-running over an overlapping window is
        safe.  If `self.writer` is None (no pool supplied), event ingestion
        is skipped — we still advance the cursor so the gated-deploy path
        keeps working.
        """
        resp = await self.client.sync_once()

        # ── ingest timeline events ────────────────────────────────────
        if self.writer is not None:
            total_inserted = 0
            total_seen = 0
            for room_id, events in _iter_timeline_events(resp):
                total_seen += len(events)
                try:
                    inserted = await self.writer.write_batch(room_id, events)
                except Exception as exc:  # pragma: no cover - defensive
                    # Never let a bad batch take down the whole sync loop;
                    # the cursor advance below is what unblocks recovery.
                    self.log.error(
                        "Matrix collect: write_batch failed for %s: %s",
                        room_id, exc,
                    )
                    continue
                total_inserted += inserted
            if total_seen:
                self.log.info(
                    "Matrix collect: ingested %d/%d new event(s) across %d room(s)",
                    total_inserted,
                    total_seen,
                    sum(1 for _ in _iter_timeline_events(resp)),
                )

        next_batch = getattr(resp, "next_batch", None)
        if next_batch:
            # The underlying sync_once already persisted via the
            # MatrixSyncStateRepository; calling save() again is a no-op
            # if pool is None and an idempotent UPDATE if it isn't.
            await self.client._sync_state.save(next_batch)
            self.log.info(
                "Matrix collect: persisted next_batch (len=%d) for %s",
                len(next_batch), self.client.user_id,
            )
        else:
            self.log.warning(
                "Matrix collect: sync returned no next_batch for %s",
                self.client.user_id,
            )

        # ── decrypt pending encrypted events ──────────────────────────
        # Runs every cycle so freshly-ingested encrypted events get
        # plaintext-settled before media download tries to interpret
        # their `file` blocks.
        if self.decryption_service is not None and self.writer is not None:
            try:
                stats = await self.decryption_service.decrypt_pending(limit=50)
                if stats.get("attempted", 0):
                    self.log.info(
                        "Matrix decrypt: %s",
                        ", ".join(f"{k}={v}" for k, v in stats.items()),
                    )
            except Exception as exc:
                # Circuit breaker inside the service handles repeat failures;
                # we just log and keep going so the cursor still advances.
                self.log.error("Matrix decrypt_pending failed: %s", exc)

        # ── download pending media ────────────────────────────────────
        if self.media_downloader is not None and self.writer is not None:
            try:
                pending = await self.writer.get_pending_media(limit=20)
            except Exception as exc:
                pending = []
                self.log.error("Matrix get_pending_media failed: %s", exc)
            for row in pending:
                event_id = row.get("event_id")
                room_id = row.get("room_id")
                mxc = row.get("media_mxc")
                raw = row.get("raw_content") or {}
                # Encrypted-attachment metadata lives under
                # raw_content["file"]; plaintext attachments use
                # raw_content["url"] without a `file` block.
                encrypted_info = raw.get("file") if isinstance(raw, dict) else None
                if not (event_id and room_id and mxc):
                    continue
                try:
                    target, sha = await self.media_downloader.download(
                        event_id=event_id,
                        room_id=room_id,
                        mxc_uri=mxc,
                        encrypted_info=encrypted_info,
                    )
                    await self.writer.mark_media_downloaded(
                        event_id, str(target), sha,
                    )
                except MatrixMediaError as exc:
                    self.log.warning(
                        "Matrix media download failed for %s: %s",
                        event_id, exc,
                    )
                except Exception as exc:
                    self.log.error(
                        "Matrix media unexpected error for %s: %s",
                        event_id, exc,
                    )

        return next_batch


# ── sync-response helpers ─────────────────────────────────────────────────


def _iter_timeline_events(resp: Any):
    """Yield (room_id, [event_dict, ...]) for every joined-room timeline.

    Tolerant of three input shapes so the same helper drives both real
    matrix-nio SyncResponse objects and stubbed test fixtures:

      1. nio.SyncResponse: `resp.rooms.join` is a dict {room_id: JoinedRoom},
         each with `.timeline.events` of nio Event objects (each carrying
         a `.source` dict).
      2. plain dict shape used by tests: `{"rooms": {"join": {room_id:
         {"timeline": {"events": [event_dict, ...]}}}}}`.
      3. dict-of-list shortcut used by tests: `resp.timeline_events ==
         {room_id: [event_dict, ...]}` — used when the test wants to skip
         the rooms.join nesting.
    """
    # Shape 3: explicit override
    short = getattr(resp, "timeline_events", None)
    if isinstance(short, dict):
        for room_id, events in short.items():
            yield room_id, list(events or [])
        return

    rooms = getattr(resp, "rooms", None)
    if rooms is None and isinstance(resp, dict):
        rooms = resp.get("rooms")
    if rooms is None:
        return

    join = getattr(rooms, "join", None)
    if join is None and isinstance(rooms, dict):
        join = rooms.get("join")
    if not join:
        return

    for room_id, room in join.items():
        timeline = getattr(room, "timeline", None)
        if timeline is None and isinstance(room, dict):
            timeline = room.get("timeline")
        if timeline is None:
            continue
        events = getattr(timeline, "events", None)
        if events is None and isinstance(timeline, dict):
            events = timeline.get("events")
        if not events:
            continue
        # Each event may already be a dict, or a nio Event with `.source`.
        out: list[dict] = []
        for ev in events:
            if isinstance(ev, dict):
                out.append(ev)
            else:
                src = getattr(ev, "source", None)
                if isinstance(src, dict):
                    out.append(src)
        yield room_id, out


__all__ = [
    "MatrixCollector",
    "is_enabled",
]
