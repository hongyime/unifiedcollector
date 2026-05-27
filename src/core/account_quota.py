"""Per-account daily / weekly / hourly request quota tracker (Wave 0, Batch 2).

Why this exists
---------------
``account_pool.py`` already tracks status (active / cooldown / banned) and a
small set of "feature counters" (profile_views / actions / media_downloads),
but it does NOT enforce general per-platform request quotas (e.g. IG=200/day,
TikTok=500/day, GitHub=5000/h authenticated). This module adds that layer.

What it does NOT do
-------------------
- Outbound rate limiting (we are read-only ingest).
- Global / cross-account quotas. Always per-(platform, account).
- Quota purchase / billing. Out of scope.
- Migrate existing ``_Quota`` counters from ``account_pool``. That's Wave 2.

Design
------
1. **QuotaConfig per platform**, registered at boot:
       quota.register("instagram", QuotaConfig(daily_limit=200, weekly_limit=1200))
       quota.register("github", QuotaConfig(daily_limit=0, hourly_limit=5000))

   ``daily_limit=0`` (or ``None``) means "unlimited on that axis"; only set
   axes are checked. If no config is registered for a platform, ``has_quota``
   is a no-op returning True (backward-compat for not-yet-migrated platforms).

2. **Day boundary = SGT midnight (UTC+8)**, NOT UTC midnight. We ingest from
   a Singapore-based pipeline; users expect "today" to align with local time.

3. **Persistence**: Postgres table ``account_quota_usage``. Counters are
   maintained by atomic ``INSERT ... ON CONFLICT DO UPDATE`` upserts, so two
   workers consuming the same account at the same time don't lose counts.

4. **In-memory cache**: short TTL on ``has_quota`` reads, so a tight pull-loop
   that's nowhere near the limit doesn't query the DB on every request.
   ``consume`` always writes through (no buffering — counters must reflect
   reality on crash).

5. **Telemetry**: optional callback receives ``QuotaMetric`` on every consume.

6. **Reset semantics**:
       day        = (now_utc + 8h).date()                       # SGT day
       week_iso   = (now_utc + 8h).isocalendar()[:2] -> "YYYY-Www"
       hour_bucket= (now_utc + 8h).strftime("%Y-%m-%d %H:00")    # SGT hour

   When today differs from the row's ``day``, we INSERT a new row (the old
   one is preserved for reporting). The hour bucket is reset whenever the
   stored ``hour_bucket`` differs from the current one — atomic via SQL.

Concurrency model
-----------------
- ``has_quota`` and ``consume`` are async and safe to call concurrently from
  many tasks. The DB does the heavy lifting via row-level locking on the
  ``ON CONFLICT`` upsert; we additionally hold an asyncio.Lock per
  (platform, account) for the read-modify-write of the in-memory cache.
- The cache is a soft optimisation; the DB is authoritative.

Integration sketch (Wave 2 — not done in this batch)
----------------------------------------------------
``AccountPool.acquire(platform)`` will, after picking a candidate account,
call ``await quota.has_quota(platform, account.name)`` and skip + mark the
account ``state='quota_exhausted'`` (with a cooldown until SGT midnight) if
quota is exhausted. After a successful collector call, the collector wraps
the work in ``await quota.consume(platform, account.name, weight=1)``.

Author: Hermes (Wave 0 batch 2 agent — account_quota)
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── time helpers ────────────────────────────────────────────────────────────

# SGT = Singapore Standard Time = UTC+8, no DST.
SGT_OFFSET = timedelta(hours=8)
SGT_TZ = timezone(SGT_OFFSET, name="SGT")


def _sgt_now(now_utc: Optional[datetime] = None) -> datetime:
    """Current wall-clock time in SGT. Injectable for tests."""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    elif now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    return now_utc.astimezone(SGT_TZ)


def _sgt_day(now_utc: Optional[datetime] = None) -> date:
    """SGT calendar date for *now_utc* (or current time)."""
    return _sgt_now(now_utc).date()


def _sgt_week_iso(now_utc: Optional[datetime] = None) -> str:
    """ISO week string ``"YYYY-Www"`` keyed off SGT date.

    e.g. ``2026-W22``. Distinct weeks always sort lexicographically.
    """
    iso_year, iso_week, _ = _sgt_now(now_utc).isocalendar()
    return f"{iso_year:04d}-W{iso_week:02d}"


def _sgt_hour_bucket(now_utc: Optional[datetime] = None) -> str:
    """SGT hour bucket ``"YYYY-MM-DD HH:00"``."""
    return _sgt_now(now_utc).strftime("%Y-%m-%d %H:00")


# ── public types ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class QuotaConfig:
    """Per-platform quota limits.

    A limit of 0 (or None) means "unlimited on that axis". At least one axis
    SHOULD be set for the config to be useful, but an all-zero config is
    accepted (no-op behaviour, useful as a placeholder).
    """
    daily_limit: int = 0
    weekly_limit: int = 0
    hourly_limit: int = 0    # optional; 0 = no per-hour cap

    def __post_init__(self) -> None:
        for fname, fval in (
            ("daily_limit", self.daily_limit),
            ("weekly_limit", self.weekly_limit),
            ("hourly_limit", self.hourly_limit),
        ):
            if fval < 0:
                raise ValueError(f"{fname} must be >= 0, got {fval}")


@dataclass
class QuotaMetric:
    """Snapshot emitted on every successful ``consume`` call."""
    platform: str
    account: str
    weight: int
    requests_today: int
    requests_week: int
    requests_hour: int
    daily_limit: int
    weekly_limit: int
    hourly_limit: int

    @property
    def daily_pct(self) -> float:
        return (self.requests_today / self.daily_limit) if self.daily_limit else 0.0


class QuotaExhaustedError(Exception):
    """Raised by ``consume_strict`` when a quota would be exceeded.

    ``has_quota`` does NOT raise; it returns False. ``consume`` does NOT
    raise either (so it remains a fire-and-forget bookkeeping call). Use
    ``consume_strict`` only when callers want a hard guard.
    """
    def __init__(self, platform: str, account: str, axis: str, current: int, limit: int):
        super().__init__(
            f"quota exhausted on {axis} for {platform}/{account}: "
            f"{current} >= {limit}"
        )
        self.platform = platform
        self.account = account
        self.axis = axis
        self.current = current
        self.limit = limit


TelemetryHook = Callable[[QuotaMetric], None]


# ── internal cache record ───────────────────────────────────────────────────


@dataclass
class _CacheEntry:
    day: date
    week_iso: str
    hour_bucket: str
    requests_today: int
    requests_week: int
    requests_hour: int
    fetched_at_mono: float = 0.0

    def is_fresh(self, ttl_s: float) -> bool:
        return (time.monotonic() - self.fetched_at_mono) < ttl_s


# ── main class ──────────────────────────────────────────────────────────────


class AccountQuotaTracker:
    """Per-(platform, account) request counter with daily/weekly/hourly caps.

    Backed by a Postgres ``account_quota_usage`` table. Safe for concurrent
    use across asyncio tasks; safe for multi-worker use because all writes
    go through ``INSERT ... ON CONFLICT DO UPDATE`` upserts.

    Backward-compat: a platform with no registered ``QuotaConfig`` is a
    no-op — ``has_quota`` returns True and ``consume`` does nothing.
    """

    TABLE = "account_quota_usage"

    def __init__(
        self,
        pool: Any = None,                     # asyncpg.Pool or None for in-mem mode
        *,
        cache_ttl_s: float = 5.0,
        telemetry: Optional[TelemetryHook] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ):
        if cache_ttl_s < 0:
            raise ValueError(f"cache_ttl_s must be >= 0, got {cache_ttl_s}")
        self._pool = pool
        self._cache_ttl_s = cache_ttl_s
        self._telemetry = telemetry
        self._clock = clock or (lambda: datetime.now(timezone.utc))

        self._configs: dict[str, QuotaConfig] = {}
        self._cache: dict[tuple[str, str], _CacheEntry] = {}
        self._key_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._dict_lock = asyncio.Lock()  # guards _cache + _key_locks

        # In-memory fallback (only used when pool is None — for tests/dry-runs).
        # Keyed by (platform, account, day).
        self._mem_rows: dict[tuple[str, str, date], dict[str, Any]] = {}

    # ── config ─────────────────────────────────────────────────────────────

    def register(self, platform: str, config: QuotaConfig) -> None:
        """Register / overwrite the QuotaConfig for *platform*.

        Platforms not registered before the first ``has_quota`` call are
        treated as unlimited (returns True, consume is a no-op).
        """
        if not platform:
            raise ValueError("platform must be non-empty")
        self._configs[platform.lower()] = config

    def get_config(self, platform: str) -> Optional[QuotaConfig]:
        return self._configs.get(platform.lower())

    # ── public API ─────────────────────────────────────────────────────────

    async def has_quota(self, platform: str, account: str, weight: int = 1) -> bool:
        """Return True iff consuming *weight* requests would NOT exceed any
        registered limit on the (platform, account).

        Backward-compat: returns True if no config is registered for the
        platform (the caller is opted out of quota tracking).
        """
        if weight < 1:
            raise ValueError(f"weight must be >= 1, got {weight}")
        cfg = self.get_config(platform)
        if cfg is None:
            return True  # no-op opt-out
        if not self._has_any_limit(cfg):
            return True

        usage = await self._get_usage(platform, account)
        return self._would_fit(usage, cfg, weight)

    async def consume(
        self, platform: str, account: str, weight: int = 1
    ) -> Optional[QuotaMetric]:
        """Persist *weight* requests against (platform, account).

        Does NOT raise on exhausted quota — that's the caller's job to check
        via ``has_quota``. We always record actual usage for visibility.

        Returns the post-consume QuotaMetric, or None if the platform is
        unregistered (no-op).
        """
        if weight < 1:
            raise ValueError(f"weight must be >= 1, got {weight}")
        cfg = self.get_config(platform)
        if cfg is None:
            return None

        now_utc = self._clock()
        day = _sgt_day(now_utc)
        week = _sgt_week_iso(now_utc)
        hour = _sgt_hour_bucket(now_utc)

        usage = await self._upsert_consume(
            platform, account, day, week, hour, weight,
        )

        metric = QuotaMetric(
            platform=platform,
            account=account,
            weight=weight,
            requests_today=usage["requests_today"],
            requests_week=usage["requests_week"],
            requests_hour=usage["requests_hour"],
            daily_limit=cfg.daily_limit,
            weekly_limit=cfg.weekly_limit,
            hourly_limit=cfg.hourly_limit,
        )
        if self._telemetry is not None:
            try:
                self._telemetry(metric)
            except Exception:
                logger.exception("account_quota: telemetry hook raised")
        return metric

    async def consume_strict(
        self, platform: str, account: str, weight: int = 1
    ) -> QuotaMetric:
        """Like ``consume`` but raises ``QuotaExhaustedError`` if *post-consume*
        usage would breach a cap. Useful as a hard guard.

        Implementation note: the check is "would *this* consume cause a
        breach", computed atomically inside the upsert; we accept the write
        first then check, then no rollback (we want to record real traffic).
        Most callers should prefer the cheaper ``has_quota`` + ``consume``
        pattern.
        """
        cfg = self.get_config(platform)
        if cfg is None:
            # Still call consume so the no-op path returns a metric-shaped
            # object; but with no config there's nothing to enforce.
            await self.consume(platform, account, weight)
            return QuotaMetric(
                platform=platform, account=account, weight=weight,
                requests_today=0, requests_week=0, requests_hour=0,
                daily_limit=0, weekly_limit=0, hourly_limit=0,
            )
        metric = await self.consume(platform, account, weight)
        assert metric is not None  # cfg present
        # After-the-fact check
        for axis, current, limit in (
            ("daily", metric.requests_today, cfg.daily_limit),
            ("weekly", metric.requests_week, cfg.weekly_limit),
            ("hourly", metric.requests_hour, cfg.hourly_limit),
        ):
            if limit and current > limit:
                raise QuotaExhaustedError(platform, account, axis, current, limit)
        return metric

    async def get_usage(
        self, platform: str, account: str
    ) -> dict[str, Any]:
        """Return the current usage snapshot. Always reads through the cache
        if fresh, else hits the DB. Not a public-API requirement but useful
        for diagnostics / dashboards.
        """
        return await self._get_usage(platform, account, force_fresh=True)

    async def reset(
        self, platform: str, account: str, *, day: Optional[date] = None
    ) -> None:
        """Delete the row for (platform, account, day). Test/admin helper."""
        d = day or _sgt_day(self._clock())
        async with await self._lock_for(platform, account):
            self._cache.pop((platform, account), None)
            if self._pool is None:
                self._mem_rows.pop((platform, account, d), None)
                return
            await self._pool.execute(
                f"DELETE FROM {self.TABLE} "
                "WHERE platform=$1 AND account=$2 AND day=$3",
                platform, account, d,
            )

    # ── internals ──────────────────────────────────────────────────────────

    @staticmethod
    def _has_any_limit(cfg: QuotaConfig) -> bool:
        return bool(cfg.daily_limit or cfg.weekly_limit or cfg.hourly_limit)

    @staticmethod
    def _would_fit(usage: dict[str, int], cfg: QuotaConfig, weight: int) -> bool:
        if cfg.daily_limit and usage["requests_today"] + weight > cfg.daily_limit:
            return False
        if cfg.weekly_limit and usage["requests_week"] + weight > cfg.weekly_limit:
            return False
        if cfg.hourly_limit and usage["requests_hour"] + weight > cfg.hourly_limit:
            return False
        return True

    async def _lock_for(self, platform: str, account: str) -> asyncio.Lock:
        key = (platform, account)
        async with self._dict_lock:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._key_locks[key] = lock
        return lock

    async def _get_usage(
        self, platform: str, account: str, *, force_fresh: bool = False,
    ) -> dict[str, int]:
        now_utc = self._clock()
        day = _sgt_day(now_utc)
        week = _sgt_week_iso(now_utc)
        hour = _sgt_hour_bucket(now_utc)
        key = (platform, account)

        if not force_fresh:
            cached = self._cache.get(key)
            if (
                cached is not None
                and cached.is_fresh(self._cache_ttl_s)
                and cached.day == day
                and cached.week_iso == week
                and cached.hour_bucket == hour
            ):
                return {
                    "requests_today": cached.requests_today,
                    "requests_week": cached.requests_week,
                    "requests_hour": cached.requests_hour,
                }

        # Need to read from store
        row = await self._fetch_row(platform, account, day)
        if row is None:
            usage = {"requests_today": 0, "requests_week": 0, "requests_hour": 0}
            stored_week = week
            stored_hour = hour
        else:
            stored_week = row.get("week_iso") or week
            stored_hour = row.get("hour_bucket") or hour
            requests_today = int(row.get("requests_today") or 0)
            # Week can roll while day is the same (only on Mondays in practice
            # for ISO weeks). If the stored week doesn't match, our weekly
            # counter is stale; fall back to 0 — weekly counts are summed via
            # SQL for accuracy in get_usage paths but the row's stored value
            # is a fast read.
            if stored_week == week:
                requests_week = int(row.get("requests_week") or 0)
            else:
                requests_week = 0
            if stored_hour == hour:
                requests_hour = int(row.get("requests_hour") or 0)
            else:
                requests_hour = 0
            usage = {
                "requests_today": requests_today,
                "requests_week": requests_week,
                "requests_hour": requests_hour,
            }

        # If we want a "true" weekly count and the row is missing or stale,
        # query the sum across the week. Cheap because the index covers it.
        if self._pool is not None and (row is None or stored_week != week):
            usage["requests_week"] = await self._fetch_week_sum(
                platform, account, week,
            )

        self._cache[key] = _CacheEntry(
            day=day, week_iso=week, hour_bucket=hour,
            requests_today=usage["requests_today"],
            requests_week=usage["requests_week"],
            requests_hour=usage["requests_hour"],
            fetched_at_mono=time.monotonic(),
        )
        return usage

    async def _fetch_row(
        self, platform: str, account: str, day: date,
    ) -> Optional[dict[str, Any]]:
        if self._pool is None:
            return self._mem_rows.get((platform, account, day))
        row = await self._pool.fetchrow(
            f"SELECT requests_today, week_iso, requests_week, "
            f"hour_bucket, requests_hour FROM {self.TABLE} "
            "WHERE platform=$1 AND account=$2 AND day=$3",
            platform, account, day,
        )
        return dict(row) if row else None

    async def _fetch_week_sum(
        self, platform: str, account: str, week: str,
    ) -> int:
        if self._pool is None:
            total = 0
            for (p, a, _d), rec in self._mem_rows.items():
                if p == platform and a == account and rec.get("week_iso") == week:
                    total += int(rec.get("requests_today") or 0)
            return total
        # SUM today's counter across all rows in the same ISO week.
        val = await self._pool.fetchval(
            f"SELECT COALESCE(SUM(requests_today), 0) FROM {self.TABLE} "
            "WHERE platform=$1 AND account=$2 AND week_iso=$3",
            platform, account, week,
        )
        return int(val or 0)

    async def _upsert_consume(
        self,
        platform: str,
        account: str,
        day: date,
        week: str,
        hour: str,
        weight: int,
    ) -> dict[str, int]:
        """Atomically increment counters; returns post-consume row."""
        async with await self._lock_for(platform, account):
            if self._pool is None:
                rec = self._mem_rows.get((platform, account, day))
                if rec is None:
                    rec = {
                        "platform": platform,
                        "account": account,
                        "day": day,
                        "week_iso": week,
                        "hour_bucket": hour,
                        "requests_today": 0,
                        "requests_week": 0,
                        "requests_hour": 0,
                    }
                    self._mem_rows[(platform, account, day)] = rec
                rec["requests_today"] += weight
                # Hour rolls within a day
                if rec["hour_bucket"] != hour:
                    rec["hour_bucket"] = hour
                    rec["requests_hour"] = 0
                rec["requests_hour"] += weight
                # Week stays attached to the row (may differ vs current week
                # in the new-day case, but this is in-mem fallback only).
                if rec["week_iso"] != week:
                    rec["week_iso"] = week
                rec["requests_week"] = await self._fetch_week_sum(
                    platform, account, week,
                )
                # Recompute weekly from sum *including this row*.
                # (If today's row is the only row this week, sum == today's.)
                # Keep cache consistent.
                usage = {
                    "requests_today": rec["requests_today"],
                    "requests_week": rec["requests_week"],
                    "requests_hour": rec["requests_hour"],
                }
            else:
                # SQL upsert. The CASE expressions handle hour-rollover within
                # the same day-row: if the stored hour_bucket no longer
                # matches the current one, reset hour counter to weight;
                # otherwise add weight.
                row = await self._pool.fetchrow(
                    f"""
                    INSERT INTO {self.TABLE}
                        (platform, account, day, week_iso, requests_today,
                         requests_week, hour_bucket, requests_hour, updated_at)
                    VALUES ($1, $2, $3, $4, $5, $5, $6, $5, NOW())
                    ON CONFLICT (platform, account, day) DO UPDATE SET
                        requests_today = {self.TABLE}.requests_today + EXCLUDED.requests_today,
                        week_iso       = EXCLUDED.week_iso,
                        hour_bucket    = EXCLUDED.hour_bucket,
                        requests_hour  = CASE
                            WHEN {self.TABLE}.hour_bucket = EXCLUDED.hour_bucket
                                THEN {self.TABLE}.requests_hour + EXCLUDED.requests_hour
                            ELSE EXCLUDED.requests_hour
                        END,
                        updated_at     = NOW()
                    RETURNING requests_today, requests_hour
                    """,
                    platform, account, day, week, weight, hour,
                )
                requests_today = int(row["requests_today"])
                requests_hour = int(row["requests_hour"])
                requests_week = await self._fetch_week_sum(
                    platform, account, week,
                )
                # Persist accurate weekly back into row for fast reads.
                await self._pool.execute(
                    f"UPDATE {self.TABLE} SET requests_week=$4 "
                    "WHERE platform=$1 AND account=$2 AND day=$3",
                    platform, account, day, requests_week,
                )
                usage = {
                    "requests_today": requests_today,
                    "requests_week": requests_week,
                    "requests_hour": requests_hour,
                }

            # Refresh cache
            self._cache[(platform, account)] = _CacheEntry(
                day=day, week_iso=week, hour_bucket=hour,
                requests_today=usage["requests_today"],
                requests_week=usage["requests_week"],
                requests_hour=usage["requests_hour"],
                fetched_at_mono=time.monotonic(),
            )
            return usage


# ── module-level singleton (opt-in) ─────────────────────────────────────────

_default_tracker: Optional[AccountQuotaTracker] = None
_default_lock = asyncio.Lock() if False else None  # placeholder to avoid event-loop bind at import


def get_default_tracker() -> Optional[AccountQuotaTracker]:
    """Return the process-wide default tracker, if one was set."""
    return _default_tracker


def set_default_tracker(tracker: Optional[AccountQuotaTracker]) -> None:
    """Set / clear the process-wide default tracker.

    Tests use this with ``None`` in an autouse fixture to avoid leaking
    state across cases. Production code calls it once at boot.
    """
    global _default_tracker
    _default_tracker = tracker


__all__ = [
    "AccountQuotaTracker",
    "QuotaConfig",
    "QuotaMetric",
    "QuotaExhaustedError",
    "get_default_tracker",
    "set_default_tracker",
]
