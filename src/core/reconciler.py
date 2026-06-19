"""Media reconciler — bounded, drive-gated, tombstoning media re-download.

Promotes the disaster-recovery ``COLLECTOR_RECOVER_MISSING`` hack into a
permanent, safe feature (feature_gap_analysis #5). A collector's known media
items are checked against the filesystem; an item whose backing file is gone is
re-downloaded — but:

* **drive-gated**: if the media drive isn't mounted we skip entirely, so a brief
  unmount can't make every file look "missing" and trigger a re-download storm.
* **bounded**: only ``budget`` re-downloads are released per collect cycle, so
  refill never starves live scraping. New content (not previously known) is never
  budgeted.
* **tombstoning**: an item that fails to re-download ``tombstone_after`` times is
  marked permanently unavailable and never retried again — this is what stops the
  forever-retry of expired/lost source assets (beeper/telegram/whatsapp refs).
* **self-completing**: after ``done_after`` consecutive cycles finding zero
  missing (non-tombstoned) files, the source is marked done and drops to fast
  mode (persisted), so the feature needs no manual flip-off.

The pure decision logic (:meth:`should_recover`, :meth:`record_failure`) is
side-effect-free and unit-testable with an injected ``exists`` probe; DB
persistence is isolated in the async ``*_state`` methods.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Callable

from .drive_check import check_drive

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


# DDL is idempotent; created on first DB touch so the module is self-contained.
_RECOVER_STATE_DDL = (
    "CREATE TABLE IF NOT EXISTS recover_state ("
    "source TEXT PRIMARY KEY, done_at TIMESTAMPTZ NOT NULL DEFAULT now())"
)
_TOMBSTONE_DDL = (
    "CREATE TABLE IF NOT EXISTS media_recover_state ("
    "source TEXT NOT NULL, content_id TEXT NOT NULL, attempts INT NOT NULL DEFAULT 0, "
    "tombstoned_at TIMESTAMPTZ, last_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
    "PRIMARY KEY (source, content_id))"
)


class Reconciler:
    """Per-source media reconciliation state + decisions."""

    def __init__(self, source: str, *, exists: Callable[[str], bool] = os.path.exists):
        self.source = source
        self.pool = None
        self._exists = exists  # injectable for tests

        self.enabled: bool = _env_bool("COLLECTOR_RECOVER_MISSING")
        self.budget: int = _env_int("COLLECTOR_RECOVER_PER_CYCLE", 200)
        self.done_after: int = _env_int("COLLECTOR_RECOVER_DONE_CYCLES", 3)
        self.tombstone_after: int = _env_int("RECONCILE_TOMBSTONE_ATTEMPTS", 5)
        # Sharded sweep: only check 1/shards of the corpus per cycle (rotating),
        # so a large corpus isn't fully stat-ed every cycle. 1 = check everything.
        self.shards: int = max(_env_int("RECONCILE_SHARDS", 1), 1)
        self._shard_index = 0
        # Anomaly alert: if more than this fraction of known items are missing in
        # one cycle, alert instead of silently refilling (signals drive trouble).
        self.alert_missing_pct: float = float(os.getenv("RECONCILE_ALERT_MISSING_PCT", "0.5"))
        self._alerted = False
        # Tier-2 (opt-in): sampled sha256 re-verification to catch corruption
        # (file present but wrong content). 0 disables. Expensive — keep small.
        self.sha256_sample_rate: float = float(os.getenv("RECONCILE_SHA256_SAMPLE_RATE", "0"))

        # Per-cycle counters (reset each cycle).
        self._released = 0
        self._missing_seen = 0

        # State.
        self._paths: dict[str, str] = {}        # content_id -> file_path
        self._tombstoned: set[str] = set()      # given-up content_ids
        self._attempts: dict[str, int] = {}     # content_id -> failed attempts
        self._dirty: set[str] = set()           # content_ids with unpersisted attempts
        self._clean_cycles = 0
        self._done = False

    # --- lifecycle ---------------------------------------------------------

    @property
    def active(self) -> bool:
        """True when reconciliation should run (enabled, not already complete)."""
        return self.enabled and not self._done

    def set_pool(self, pool):
        self.pool = pool

    def drive_ok(self) -> bool:
        """Drive-presence gate — never reconcile against an absent drive."""
        return check_drive()

    def note_known(self, content_id: str, file_path: str | None):
        """Register a known item's backing path (called while seeding). Only
        items in the current shard are registered, spreading the scan over cycles."""
        if self.active and file_path and self._in_shard(content_id):
            self._paths[content_id] = file_path

    def reset_cycle(self):
        self._released = 0
        self._missing_seen = 0

    def _in_shard(self, content_id: str) -> bool:
        """Stable (process-independent) shard membership via crc32."""
        if self.shards <= 1:
            return True
        import zlib
        return (zlib.crc32(content_id.encode("utf-8", "replace")) % self.shards) == self._shard_index

    def advance_shard(self):
        """Rotate to the next shard for the next cycle (full sweep over `shards` cycles)."""
        if self.shards > 1:
            self._shard_index = (self._shard_index + 1) % self.shards
            self._paths.clear()  # next cycle re-seeds only the new shard

    # --- pure decision logic (unit-tested) ---------------------------------

    def should_recover(self, content_id: str) -> bool:
        """True if this known item's file is missing and we should re-download it
        this cycle: bounded by budget and skipping tombstoned (given-up) items."""
        if not self.active:
            return False
        fp = self._paths.get(content_id)
        if not fp or self._exists(fp):
            return False  # not a reconciler-tracked item, or file is present
        if content_id in self._tombstoned:
            return False  # permanently unavailable — never retry
        self._missing_seen += 1
        if self.budget <= 0 or self._released < self.budget:
            self._released += 1
            return True
        return False  # per-cycle budget spent; will retry a later cycle

    def record_failure(self, content_id: str):
        """A re-download attempt failed — count it and tombstone at the threshold."""
        if not self.active:
            return
        n = self._attempts.get(content_id, 0) + 1
        self._attempts[content_id] = n
        self._dirty.add(content_id)
        if self.tombstone_after > 0 and n >= self.tombstone_after:
            self._tombstoned.add(content_id)
            logger.info("%s: tombstoned %s after %d failed re-download attempts",
                        self.source, content_id, n)

    def missing_rate(self, known_total: int) -> float:
        """Fraction of known items found missing this cycle (anomaly signal)."""
        if known_total <= 0:
            return 0.0
        return self._missing_seen / known_total

    async def maybe_alert(self, known_total: int):
        """One-shot Telegram alert when an abnormal fraction of files went missing
        this cycle — a drive failure / bug looks like a missing-rate spike, and we
        want to be told, not silently refill into the void."""
        if self._alerted or self.alert_missing_pct <= 0:
            return
        rate = self.missing_rate(known_total)
        if rate >= self.alert_missing_pct:
            self._alerted = True
            try:
                from src.notifications import alerts
                await alerts.notify_status({
                    "ok": False,
                    "error": (f"{self.source}: {rate:.0%} of {known_total} known media "
                              f"missing this cycle ({self._missing_seen}) — possible drive issue"),
                })
            except Exception:
                logger.warning("%s: reconciler alert failed", self.source, exc_info=True)

    # --- tier-2 corruption check (opt-in, sampled) -------------------------

    def sha256_due(self, content_id: str) -> bool:
        """Stable sampling decision for the opt-in sha256 re-verification."""
        if self.sha256_sample_rate <= 0:
            return False
        import zlib
        bucket = zlib.crc32(("sha2:" + content_id).encode("utf-8", "replace")) % 10000
        return bucket < int(self.sha256_sample_rate * 10000)

    @staticmethod
    def file_sha256(path: str) -> str | None:
        """Streamed sha256 of a file, or None if unreadable."""
        import hashlib
        try:
            h = hashlib.sha256()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
            return h.hexdigest()
        except OSError:
            return None

    # --- DB persistence (isolated; integration-tested) ---------------------

    async def load_state(self):
        """Load done-flag + tombstones + attempt counts from the DB. Tolerant."""
        if self.pool is None or not self.enabled:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(_RECOVER_STATE_DDL)
                await conn.execute(_TOMBSTONE_DDL)
                done = await conn.fetchval(
                    "SELECT true FROM recover_state WHERE source=$1", self.source)
                if done:
                    self._done = True
                    return
                rows = await conn.fetch(
                    "SELECT content_id, attempts, tombstoned_at "
                    "FROM media_recover_state WHERE source=$1", self.source)
            for r in rows:
                self._attempts[r["content_id"]] = r["attempts"]
                if r["tombstoned_at"] is not None:
                    self._tombstoned.add(r["content_id"])
        except Exception:
            logger.debug("%s: reconciler load_state failed", self.source, exc_info=True)

    async def persist(self):
        """Flush dirty attempt/tombstone rows. Cheap: only changed content_ids."""
        if self.pool is None or not self._dirty:
            return
        rows = [
            (self.source, cid, self._attempts.get(cid, 0),
             cid in self._tombstoned)
            for cid in self._dirty
        ]
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(_TOMBSTONE_DDL)
                await conn.executemany(
                    "INSERT INTO media_recover_state "
                    "(source, content_id, attempts, tombstoned_at, last_attempt_at) "
                    "VALUES ($1, $2, $3, CASE WHEN $4 THEN now() END, now()) "
                    "ON CONFLICT (source, content_id) DO UPDATE SET "
                    "attempts = EXCLUDED.attempts, "
                    "tombstoned_at = COALESCE(media_recover_state.tombstoned_at, EXCLUDED.tombstoned_at), "
                    "last_attempt_at = now()",
                    rows)
            self._dirty.clear()
        except Exception:
            logger.debug("%s: reconciler persist failed", self.source, exc_info=True)

    async def finalize_cycle(self) -> bool:
        """Called after a successful collect cycle. Returns True if the source
        just transitioned to done. Persists dirty state regardless."""
        await self.persist()
        if not self.active or self.done_after <= 0:
            return False
        if self._missing_seen == 0:
            self._clean_cycles += 1
            if self._clean_cycles >= self.done_after:
                await self._mark_done()
                self._done = True
                logger.info("%s: media refill complete (%d clean cycles) — "
                            "reconciler auto-OFF", self.source, self._clean_cycles)
                return True
        else:
            self._clean_cycles = 0
        return False

    async def _mark_done(self):
        if self.pool is None:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(_RECOVER_STATE_DDL)
                await conn.execute(
                    "INSERT INTO recover_state (source) VALUES ($1) "
                    "ON CONFLICT (source) DO NOTHING", self.source)
        except Exception:
            logger.debug("%s: reconciler _mark_done failed", self.source, exc_info=True)
