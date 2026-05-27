"""Wave 1 Phase 2 — Matrix room backfill driver.

The Phase 1 collector handles forward-sync (NEW events arriving via
/sync). This module handles BACKFILL: pulling HISTORICAL events for the
~2055 Beeper rooms that already exist before the collector started.

Strategy
────────
For every joined room, walk backwards via /rooms/{id}/messages?dir=b,
page by page (limit=100 events). Each page's events are normalized + bulk
inserted by the existing `MatrixEventWriter` (which uses INSERT ON
CONFLICT DO NOTHING — re-runs are safe and idempotent). Per-room
progress (last pagination token, counters, done flag, last error) lives
in `matrix_backfill_state`, written through `MatrixBackfillStateRepo`.

Per cycle we cap each room at `target_depth` events and `max_pages`
HTTP requests so a single noisy room can't monopolize the cycle. Rooms
where the homeserver returns end-of-history (empty chunk or no `end`
token) are marked done=TRUE and skipped on subsequent cycles.

Concurrency is bounded by an asyncio.Semaphore so we never hammer Beeper
beyond what `concurrency` permits — the operator script defaults to 4,
which is comfortable inside Beeper's per-account rate envelope.

NO outbound calls beyond the existing `client.fetch_history` path. NO
fan-out beyond `concurrency`. Drops cleanly on persistent error: the
driver stores the error message on the row and tries again next cycle.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from src.core.matrix_backfill_state_repo import MatrixBackfillStateRepo

logger = logging.getLogger(__name__)


# ── helpers ────────────────────────────────────────────────────────────


def _events_from_response(resp: Any) -> list[dict]:
    """Extract the chunk of raw event dicts from an nio RoomMessagesResponse.

    matrix-nio returns Event-typed objects in `resp.chunk`; each carries
    a `.source` dict matching the Matrix client-server JSON shape that
    `EventNormalizer.normalize` expects. We tolerate a plain list-of-dicts
    too (test stubs use that shape).
    """
    chunk = getattr(resp, "chunk", None)
    if chunk is None and isinstance(resp, dict):
        chunk = resp.get("chunk")
    if not chunk:
        return []
    out: list[dict] = []
    for ev in chunk:
        if isinstance(ev, dict):
            out.append(ev)
            continue
        src = getattr(ev, "source", None)
        if isinstance(src, dict):
            out.append(src)
    return out


def _end_token(resp: Any) -> Optional[str]:
    """The `end` field from a /messages response — token for the NEXT page.

    When the homeserver has no more history, `end` is either missing or
    equal to `start`; nio surfaces this as an attribute that may be None
    or empty string. Callers treat falsy as end-of-history.
    """
    if isinstance(resp, dict):
        return resp.get("end")
    return getattr(resp, "end", None)


def _earliest_event_ts(events: Iterable[dict]) -> Optional[datetime]:
    """Min origin_server_ts across `events`, or None on empty/malformed."""
    best: Optional[int] = None
    for ev in events:
        ts = ev.get("origin_server_ts")
        try:
            ts_i = int(ts)
        except (TypeError, ValueError):
            continue
        if best is None or ts_i < best:
            best = ts_i
    if best is None:
        return None
    return datetime.fromtimestamp(best / 1000.0, tz=timezone.utc)


# ── driver ─────────────────────────────────────────────────────────────


class MatrixBackfillDriver:
    """Resumable historical backfill across all Beeper rooms.

    Composition:
        client — `BeeperMatrixClient` already logged in + warmed (so its
                 in-memory `rooms` map is populated).
        writer — `MatrixEventWriter` against the live asyncpg pool.
        repo   — `MatrixBackfillStateRepo`. If the caller passes None we
                 build one from `writer.pool`.
        log    — optional logger override.

    All public methods are async. Returns plain dicts (no domain
    classes) so the operator script can render summaries trivially.
    """

    def __init__(
        self,
        client: Any,
        writer: Any,
        repo: Optional[MatrixBackfillStateRepo] = None,
        log: Optional[logging.Logger] = None,
    ) -> None:
        if client is None:
            raise ValueError("client is required")
        if writer is None:
            raise ValueError("writer is required")
        self.client = client
        self.writer = writer
        if repo is None:
            pool = getattr(writer, "pool", None)
            if pool is None:
                raise ValueError(
                    "repo is required when writer has no .pool attribute"
                )
            repo = MatrixBackfillStateRepo(pool)
        self.repo = repo
        self.log = log or logger

    # ── room enumeration ──────────────────────────────────────────────

    async def list_priority_rooms(self) -> list[tuple[str, Optional[int], Optional[str]]]:
        """Joined rooms sorted by last_activity_ts DESC.

        Returns tuples (room_id, last_activity_ts, display_name). Rooms
        with no last_activity_ts sort to the end so the freshest history
        gets backfilled first within any given cycle's budget.
        """
        rooms = await self.client.list_rooms()
        items = list(rooms.items()) if isinstance(rooms, dict) else list(rooms)

        def sort_key(entry):
            room_id, summary = entry
            ts = getattr(summary, "last_activity_ts", None)
            # NULL last → smallest sort key → ends up at the end after reverse
            return ts if ts is not None else -1

        items.sort(key=sort_key, reverse=True)
        out: list[tuple[str, Optional[int], Optional[str]]] = []
        for room_id, summary in items:
            out.append((
                room_id,
                getattr(summary, "last_activity_ts", None),
                getattr(summary, "display_name", None),
            ))
        return out

    # ── per-room backfill ─────────────────────────────────────────────

    async def backfill_room(
        self,
        room_id: str,
        target_depth: int = 1000,
        max_pages: int = 20,
    ) -> dict:
        """Backfill a single room from its persisted cursor.

        Loops:
            1. read state row (last_token, done?)
            2. if done -> return early
            3. for each page: fetch_history -> writer.write_batch ->
               upsert_progress
            4. stop when target_depth events fetched, max_pages hit,
               end-of-history, or persistent error
            5. on natural end-of-history: mark_done

        Returns: {
            "room_id":        str,
            "events_fetched": int,    # NEW rows actually inserted this call
            "pages_used":     int,    # /messages requests this call
            "done":           bool,   # True iff end-of-history reached
            "error":          str|None,
        }
        """
        state = await self.repo.get(room_id)
        if state and state.get("done"):
            return {
                "room_id": room_id,
                "events_fetched": 0,
                "pages_used": 0,
                "done": True,
                "error": None,
            }

        from_token: Optional[str] = state.get("last_token") if state else None
        events_fetched = 0
        pages_used = 0
        error: Optional[str] = None
        natural_end = False

        for _ in range(max(1, int(max_pages))):
            if events_fetched >= target_depth:
                break
            try:
                resp = await self.client.fetch_history(
                    room_id=room_id,
                    limit=100,
                    before_token=from_token,
                )
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                self.log.warning(
                    "backfill_room: fetch_history failed for %s: %s",
                    room_id, error,
                )
                break

            pages_used += 1
            events = _events_from_response(resp)
            new_token = _end_token(resp)
            earliest = _earliest_event_ts(events)

            inserted = 0
            if events:
                try:
                    inserted = await self.writer.write_batch(room_id, events)
                except Exception as exc:
                    error = f"write_batch:{type(exc).__name__}: {exc}"
                    self.log.error(
                        "backfill_room: write_batch failed for %s: %s",
                        room_id, error,
                    )
                    # Persist the cursor we already advanced past so a
                    # retry doesn't re-fetch the same page; partial work
                    # is fine because the writer is ON CONFLICT DO NOTHING.
                    await self.repo.upsert_progress(
                        room_id=room_id,
                        last_token=new_token or from_token,
                        events_fetched_inc=0,
                        pages_inc=1,
                        error=error,
                        earliest_ts=earliest,
                    )
                    break

            events_fetched += inserted
            await self.repo.upsert_progress(
                room_id=room_id,
                last_token=new_token,
                events_fetched_inc=inserted,
                pages_inc=1,
                error=None,
                earliest_ts=earliest,
            )

            # End-of-history detection: empty chunk OR no/empty end token
            # OR end token unchanged from what we sent (homeserver echoing
            # our cursor = nothing earlier exists).
            if not events:
                natural_end = True
                break
            if not new_token:
                natural_end = True
                break
            if from_token is not None and new_token == from_token:
                natural_end = True
                break

            from_token = new_token

        if natural_end and error is None:
            await self.repo.mark_done(room_id)

        return {
            "room_id": room_id,
            "events_fetched": events_fetched,
            "pages_used": pages_used,
            "done": bool(natural_end and error is None),
            "error": error,
        }

    # ── cycle ─────────────────────────────────────────────────────────

    async def backfill_all(
        self,
        concurrency: int = 4,
        per_room_target: int = 1000,
        max_pages: int = 20,
        room_limit: Optional[int] = None,
    ) -> dict:
        """Run one backfill cycle across all (or `room_limit`) rooms.

        Skips rooms where matrix_backfill_state.done = TRUE. Bounded by
        an asyncio.Semaphore(concurrency) so we never exceed the caller's
        fan-out budget — the default 4 is a safe pick for Beeper.
        """
        rooms = await self.list_priority_rooms()
        if room_limit is not None:
            rooms = rooms[: int(room_limit)]

        sem = asyncio.Semaphore(max(1, int(concurrency)))

        async def _one(entry):
            room_id, _ts, _name = entry
            state = await self.repo.get(room_id)
            if state and state.get("done"):
                return {
                    "room_id": room_id,
                    "events_fetched": 0,
                    "pages_used": 0,
                    "done": True,
                    "error": None,
                    "skipped": True,
                }
            async with sem:
                result = await self.backfill_room(
                    room_id=room_id,
                    target_depth=per_room_target,
                    max_pages=max_pages,
                )
            result["skipped"] = False
            return result

        results = await asyncio.gather(
            *[_one(r) for r in rooms],
            return_exceptions=True,
        )

        rooms_processed = 0
        events_total = 0
        errors = 0
        per_room: list[dict] = []
        for r in results:
            if isinstance(r, Exception):
                errors += 1
                per_room.append({"error": f"{type(r).__name__}: {r}"})
                continue
            if r.get("skipped"):
                per_room.append(r)
                continue
            rooms_processed += 1
            events_total += int(r.get("events_fetched") or 0)
            if r.get("error"):
                errors += 1
            per_room.append(r)

        return {
            "rooms_processed": rooms_processed,
            "events_fetched": events_total,
            "errors": errors,
            "rooms_total": len(rooms),
            "per_room": per_room,
        }


__all__ = ["MatrixBackfillDriver"]
