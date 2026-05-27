"""Wave 1 Phase 1: matrix event normalizer + writer.

The collector pipeline is:

    SyncResponse  ─┐
                   ├─►  EventNormalizer.normalize(event_dict, room_id)
                   │         │
                   │         ▼
                   │    row dict (matches matrix_events columns)
                   │         │
                   └─►  MatrixEventWriter.write_event / write_batch
                            │
                            ▼
                       INSERT INTO matrix_events ... ON CONFLICT DO NOTHING

Two classes live here:

* `EventNormalizer` is pure — given a raw matrix-nio event (or its `.source`
  dict) plus the room it came from, it returns a fully populated row-shaped
  dict.  Zero I/O, fully unit-testable.

* `MatrixEventWriter` wraps an asyncpg pool and exposes the four operations
  the rest of the system needs:
      - write_event / write_batch          (collector-side, primary path)
      - mark_media_downloaded              (media-download worker)
      - get_undecrypted_events             (decryption worker, scan)
      - update_decrypted                   (decryption worker, settle)

The decryption-worker contract is shared with the parallel crypto agent:
their `MatrixDecryptionService.decrypt_pending(writer)` calls
`writer.get_undecrypted_events()` and then `writer.update_decrypted(...)`
once it has the plaintext.  Signatures here are the source of truth.

Idempotency:
    All inserts use `ON CONFLICT (event_id) DO NOTHING`.  Re-running collect()
    over an overlapping sync window is therefore safe; we never duplicate
    rows and we never overwrite a decrypted body with a stale encrypted one.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)


# ── normalizer ─────────────────────────────────────────────────────────────


# Event types we explicitly skip at the writer boundary.  The collector
# may still log them, but they don't get a row in matrix_events.
_SKIP_EVENT_TYPES = frozenset({
    "m.room.member",
    "m.room.name",
    "m.room.topic",
    "m.room.avatar",
    "m.room.canonical_alias",
    "m.room.power_levels",
    "m.room.join_rules",
    "m.room.history_visibility",
    "m.room.guest_access",
    "m.room.create",
    "m.room.encryption",
})

# Message msgtypes that carry media (used to populate media_mxc).
_MEDIA_MSGTYPES = frozenset({"m.image", "m.video", "m.audio", "m.file"})


class EventNormalizer:
    """Pure normalizer: raw event dict → matrix_events row dict.

    `normalize()` is a static method; the class exists as a namespace and
    a stable import target.
    """

    SKIP_EVENT_TYPES = _SKIP_EVENT_TYPES

    @staticmethod
    def should_skip(event_type: str) -> bool:
        """True if the event_type is one we do not persist."""
        return event_type in _SKIP_EVENT_TYPES

    @staticmethod
    def _ts_to_dt(server_ts: Any) -> datetime:
        """Convert Matrix `origin_server_ts` (ms epoch) to aware datetime.

        Matrix spec guarantees ms-precision integer; we tolerate floats and
        already-datetime inputs for test convenience.
        """
        if isinstance(server_ts, datetime):
            return server_ts if server_ts.tzinfo else server_ts.replace(tzinfo=timezone.utc)
        try:
            ms = int(server_ts)
        except (TypeError, ValueError):
            # Defensive fallback — we'd rather store "now" than reject the
            # row entirely.  Logged loud so we notice in practice.
            logger.warning("normalize: bad origin_server_ts=%r — using now()", server_ts)
            return datetime.now(timezone.utc)
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)

    @staticmethod
    def normalize(event: dict, room_id: str) -> Optional[dict]:
        """Map a raw Matrix event dict to a matrix_events row.

        Returns None if the event should be skipped (state events we don't
        persist, or malformed events missing required fields).

        Required input shape (Matrix client-server spec):
            {
              "event_id":          "$abc:server",
              "type":              "m.room.message",
              "sender":            "@u:server",
              "origin_server_ts":  1700000000000,
              "content": { ... },
            }
        """
        event_type = event.get("type")
        event_id = event.get("event_id")
        sender = event.get("sender")
        if not event_id or not event_type or not sender:
            logger.warning("normalize: missing required field in event %r", event)
            return None

        if EventNormalizer.should_skip(event_type):
            return None

        content = event.get("content") or {}
        server_ts = EventNormalizer._ts_to_dt(event.get("origin_server_ts"))

        # Defaults
        msgtype: Optional[str] = None
        body: Optional[str] = None
        formatted_body: Optional[str] = None
        is_encrypted = False
        is_decrypted = False
        media_mxc: Optional[str] = None
        relates_to: Optional[str] = None
        relation_type: Optional[str] = None
        is_edit = False
        is_reaction = False
        is_redacted = bool(event.get("redacted_because")) or event_type == "m.room.redaction"

        # ── per-type extraction ───────────────────────────────────────
        if event_type == "m.room.message":
            msgtype = content.get("msgtype")
            body = content.get("body")
            formatted_body = content.get("formatted_body")
            if msgtype in _MEDIA_MSGTYPES:
                # Plain (un-encrypted) media: url is at top-level of content.
                # Encrypted media: nested under content.file.url (per MSC).
                media_mxc = content.get("url")
                if not media_mxc:
                    file_block = content.get("file") or {}
                    media_mxc = file_block.get("url")

        elif event_type == "m.room.encrypted":
            # Could not decrypt (or not yet attempted).  Body stays None;
            # raw_content holds the full ciphertext blob so the decryption
            # worker can pick it up later.
            is_encrypted = True
            is_decrypted = False
            # If the event came in already-decrypted via nio
            # (i.e. "decrypted":true was injected into content), promote it
            # to its underlying msgtype/body.  This is the path the crypto
            # agent's `update_decrypted` also uses retroactively.
            if content.get("decrypted") is True:
                is_decrypted = True
                msgtype = content.get("msgtype")
                body = content.get("body")
                formatted_body = content.get("formatted_body")
                if msgtype in _MEDIA_MSGTYPES:
                    media_mxc = content.get("url") or (content.get("file") or {}).get("url")

        elif event_type == "m.reaction":
            is_reaction = True
            rel = content.get("m.relates_to") or {}
            relates_to = rel.get("event_id")
            relation_type = rel.get("rel_type") or "m.annotation"
            body = rel.get("key")  # the emoji/key the reaction carries

        elif event_type == "m.room.redaction":
            is_redacted = True
            # `redacts` is a top-level field for redaction events; we mirror
            # it into relates_to for thread-rebuild convenience.
            relates_to = event.get("redacts")
            relation_type = "m.replace"  # treat as overwrite-by-redaction

        # ── universal m.relates_to handling (replies/edits) ──────────
        rel = content.get("m.relates_to")
        if isinstance(rel, dict) and not is_reaction and not is_redacted:
            # m.replace = edit; in_reply_to = reply
            if "rel_type" in rel and rel.get("rel_type") == "m.replace":
                relates_to = rel.get("event_id") or relates_to
                relation_type = "m.replace"
                is_edit = True
            elif "m.in_reply_to" in rel:
                in_reply = rel.get("m.in_reply_to") or {}
                relates_to = in_reply.get("event_id") or relates_to
                relation_type = "m.in_reply_to"

        return {
            "event_id":         event_id,
            "room_id":          room_id,
            "sender":           sender,
            "event_type":       event_type,
            "msgtype":          msgtype,
            "body":             body,
            "raw_content":      content,
            "formatted_body":   formatted_body,
            "is_encrypted":     is_encrypted,
            "is_decrypted":     is_decrypted,
            "media_mxc":        media_mxc,
            "media_local_path": None,
            "media_sha256":     None,
            "server_ts":        server_ts,
            "relates_to":       relates_to,
            "relation_type":    relation_type,
            "is_edit":          is_edit,
            "is_reaction":      is_reaction,
            "is_redacted":      is_redacted,
        }


# ── writer ─────────────────────────────────────────────────────────────────


_INSERT_SQL = """
INSERT INTO matrix_events (
    event_id, room_id, sender, event_type, msgtype,
    body, raw_content, formatted_body,
    is_encrypted, is_decrypted,
    media_mxc, media_local_path, media_sha256,
    server_ts,
    relates_to, relation_type,
    is_edit, is_reaction, is_redacted
) VALUES (
    $1, $2, $3, $4, $5,
    $6, $7::jsonb, $8,
    $9, $10,
    $11, $12, $13,
    $14,
    $15, $16,
    $17, $18, $19
)
ON CONFLICT (event_id) DO NOTHING
"""


def _row_to_args(row: dict) -> tuple:
    """Turn a normalized row dict into the positional tuple _INSERT_SQL needs.

    raw_content is JSON-encoded here so callers don't need to know about
    asyncpg's jsonb codec setup.
    """
    return (
        row["event_id"],
        row["room_id"],
        row["sender"],
        row["event_type"],
        row.get("msgtype"),
        row.get("body"),
        json.dumps(row.get("raw_content") or {}),
        row.get("formatted_body"),
        bool(row.get("is_encrypted", False)),
        bool(row.get("is_decrypted", False)),
        row.get("media_mxc"),
        row.get("media_local_path"),
        row.get("media_sha256"),
        row["server_ts"],
        row.get("relates_to"),
        row.get("relation_type"),
        bool(row.get("is_edit", False)),
        bool(row.get("is_reaction", False)),
        bool(row.get("is_redacted", False)),
    )


class MatrixEventWriter:
    """Asyncpg-backed writer for matrix_events.

    All methods are async.  Pass an asyncpg pool (or anything quack-typed
    enough to expose `acquire()` returning an async-context-manager
    connection with `.execute / .executemany / .fetch / .fetchrow`).
    """

    def __init__(self, pool: Any) -> None:
        if pool is None:
            raise ValueError("pool is required")
        self.pool = pool

    # ── primary ingest path ───────────────────────────────────────────

    async def write_event(self, room_id: str, event: dict) -> bool:
        """Normalize + insert a single event.  Returns True if a new row
        was written, False if skipped (state event / dup / malformed).

        `event` is the raw event dict (matrix-nio's `event.source`, or the
        equivalent shape from /messages chunks).
        """
        row = EventNormalizer.normalize(event, room_id)
        if row is None:
            return False
        async with self.pool.acquire() as conn:
            status = await conn.execute(_INSERT_SQL, *_row_to_args(row))
        # asyncpg returns "INSERT 0 1" on real insert, "INSERT 0 0" on conflict.
        return isinstance(status, str) and status.endswith(" 1")

    async def write_batch(self, room_id: str, events: Iterable[dict]) -> int:
        """Bulk insert.  Returns the count of NEW rows actually inserted
        (i.e. excludes ON CONFLICT skips and pre-normalize skips).

        Uses executemany — for our event volumes this is well within
        what's reasonable; a COPY path can be added later if profiling
        says so.
        """
        rows = []
        for ev in events:
            r = EventNormalizer.normalize(ev, room_id)
            if r is not None:
                rows.append(_row_to_args(r))
        if not rows:
            return 0
        # We can't trust executemany's status string for per-row insert
        # counts, so we count distinct event_ids that DIDN'T already exist
        # by querying just before+after.  For now: do per-row execute so
        # write_event's return semantics carry over without surprises.
        inserted = 0
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                for args in rows:
                    status = await conn.execute(_INSERT_SQL, *args)
                    if isinstance(status, str) and status.endswith(" 1"):
                        inserted += 1
        return inserted

    # ── media-download worker hook ────────────────────────────────────

    async def mark_media_downloaded(
        self,
        event_id: str,
        local_path: str,
        sha256: str,
    ) -> bool:
        """Settle media columns for a row whose attachment we've downloaded.

        Returns True iff exactly one row was updated.
        """
        async with self.pool.acquire() as conn:
            status = await conn.execute(
                """
                UPDATE matrix_events
                   SET media_local_path = $2,
                       media_sha256     = $3
                 WHERE event_id = $1
                """,
                event_id,
                local_path,
                sha256,
            )
        return isinstance(status, str) and status.endswith(" 1")

    # ── decryption worker hooks ───────────────────────────────────────

    async def get_undecrypted_events(self, limit: int = 100) -> list[dict]:
        """Return rows still awaiting decryption, oldest first.

        Returned dicts contain at minimum: event_id, room_id, sender,
        server_ts, raw_content (the encrypted blob).  The crypto agent's
        decryption service uses raw_content to drive olm/megolm.
        """
        if limit <= 0:
            return []
        async with self.pool.acquire() as conn:
            records = await conn.fetch(
                """
                SELECT event_id, room_id, sender, server_ts, raw_content
                  FROM matrix_events
                 WHERE is_encrypted = TRUE
                   AND is_decrypted = FALSE
                 ORDER BY server_ts ASC
                 LIMIT $1
                """,
                limit,
            )
        out: list[dict] = []
        for rec in records:
            d = dict(rec)
            rc = d.get("raw_content")
            # asyncpg returns jsonb as already-decoded objects in modern
            # versions, but if it's still a string (older codec config),
            # decode here so the caller always sees a dict.
            if isinstance(rc, str):
                try:
                    d["raw_content"] = json.loads(rc)
                except json.JSONDecodeError:
                    d["raw_content"] = {}
            out.append(d)
        return out

    async def update_decrypted(
        self,
        event_id: str,
        body: Optional[str],
        raw_content: dict,
        msgtype: Optional[str] = None,
        formatted_body: Optional[str] = None,
        media_mxc: Optional[str] = None,
    ) -> bool:
        """Settle a row once the crypto worker has the plaintext.

        Sets is_decrypted=TRUE, fills body/raw_content + the optional
        derived fields the normalizer would have populated.  Idempotent:
        repeated calls are no-ops if is_decrypted is already TRUE for
        this event_id (we still update the body in case the prior decrypt
        stored a partial value).

        Returns True iff a row was matched + updated.
        """
        async with self.pool.acquire() as conn:
            status = await conn.execute(
                """
                UPDATE matrix_events
                   SET body           = $2,
                       raw_content    = $3::jsonb,
                       msgtype        = COALESCE($4, msgtype),
                       formatted_body = COALESCE($5, formatted_body),
                       media_mxc      = COALESCE($6, media_mxc),
                       is_decrypted   = TRUE
                 WHERE event_id = $1
                   AND is_encrypted = TRUE
                """,
                event_id,
                body,
                json.dumps(raw_content or {}),
                msgtype,
                formatted_body,
                media_mxc,
            )
        return isinstance(status, str) and status.endswith(" 1")

    # ── media worker hooks ─────────────────────────────────────────────

    async def get_pending_media(self, limit: int = 50) -> list[dict]:
        """Return rows whose media has not yet been downloaded.

        Filters: media_mxc IS NOT NULL AND media_local_path IS NULL AND
        (is_encrypted = FALSE OR is_decrypted = TRUE) — i.e. we know how
        to interpret the file block. Oldest first so backlog drains FIFO.
        """
        if limit <= 0:
            return []
        async with self.pool.acquire() as conn:
            records = await conn.fetch(
                """
                SELECT event_id, room_id, media_mxc, raw_content
                  FROM matrix_events
                 WHERE media_mxc IS NOT NULL
                   AND media_local_path IS NULL
                   AND (is_encrypted = FALSE OR is_decrypted = TRUE)
                 ORDER BY server_ts ASC
                 LIMIT $1
                """,
                limit,
            )
        out: list[dict] = []
        for rec in records:
            d = dict(rec)
            rc = d.get("raw_content")
            if isinstance(rc, str):
                try:
                    d["raw_content"] = json.loads(rc)
                except json.JSONDecodeError:
                    d["raw_content"] = {}
            out.append(d)
        return out


__all__ = [
    "EventNormalizer",
    "MatrixEventWriter",
]
