"""Generic per-user field-diff tracker.

Ported from telegramcollector/services/user_intelligence/change_tracker.py and
generalised so the same machinery can back telegram_user_changes,
instagram_user_changes, lemon8_user_changes, etc. — anywhere we re-observe a
user profile and want an audit log of which fields drifted between sightings.

Schema contract (per change-log table):
    id           BIGSERIAL  PK
    user_id      BIGINT     NOT NULL
    field        VARCHAR    NOT NULL
    old_value    TEXT
    new_value    TEXT
    detected_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()

Diff semantics (matches the original change_tracker decision tree):
    new is None / empty   → skip (partial payload, never overwrite history)
    old is None / empty   → skip (first non-empty observation, baseline only)
    new != old            → log a change row
    new == old            → skip (no change)

The class is safe to use as a no-op when ``pool`` is None — callers wired into
ingestion code paths can construct the tracker unconditionally and rely on it
to silently degrade if the DB isn't available.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Mapping

logger = logging.getLogger(__name__)


def _normalize(value: Any) -> str | None:
    """Return value as str, mapping empty / falsy-ish to None."""
    if value is None:
        return None
    if isinstance(value, str):
        s = value
    else:
        s = str(value)
    if s == "":
        return None
    return s


class UserChangeTracker:
    """Detect-and-log diffs between a known DB row and an incoming snapshot.

    Generic over the change-log table name — pass ``table`` per-call (not in
    ``__init__``) so a single tracker instance can serve multiple platforms.
    """

    def __init__(self, pool) -> None:
        """``pool``: asyncpg.Pool, or None to disable persistence (no-op mode)."""
        self._pool = pool

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    async def detect_and_log(
        self,
        table: str,
        pk_col: str,
        pk_val: Any,
        current_row: Mapping[str, Any] | None,
        new_row: Mapping[str, Any],
        fields: Iterable[str],
    ) -> int:
        """Compare ``current_row`` vs ``new_row`` and log changes to ``table``.

        Args:
            table:        change-log table name (e.g. 'telegram_user_changes').
            pk_col:       column on the change-log table that identifies the
                          subject of change (e.g. 'user_id'). Used as both the
                          column name in the INSERT and as a label.
            pk_val:       the value to put under pk_col (e.g. user.id as int).
            current_row:  the row we already have in the canonical user table
                          (or None if unknown — every field is then treated as
                          baseline-only and no diffs are logged).
            new_row:      the freshly-observed payload.
            fields:       iterable of field names to diff. Both rows are read
                          via mapping access (row[field]); missing keys are
                          treated as None.

        Returns:
            Number of change rows actually inserted.
        """
        if self._pool is None:
            return 0

        # Validate table/column identifiers — defence-in-depth against injection
        # via misconfiguration. asyncpg can't parameterise identifiers.
        if not _is_safe_ident(table) or not _is_safe_ident(pk_col):
            logger.error(
                "UserChangeTracker.detect_and_log: rejecting unsafe identifier(s) "
                "table=%r pk_col=%r", table, pk_col,
            )
            return 0

        diffs: list[tuple[str, str | None, str | None]] = []
        cur = current_row or {}
        for field in fields:
            old_val = _normalize(_get(cur, field))
            new_val = _normalize(_get(new_row, field))
            if new_val is None:
                continue                     # partial payload — skip
            if old_val is None:
                continue                     # baseline observation — skip
            if new_val == old_val:
                continue                     # unchanged
            diffs.append((field, old_val, new_val))

        if not diffs:
            return 0

        sql = (
            f"INSERT INTO {table} "
            f"({pk_col}, field, old_value, new_value) "
            f"VALUES ($1, $2, $3, $4)"
        )
        written = 0
        try:
            async with self._pool.acquire() as conn:
                for field, old_v, new_v in diffs:
                    await conn.execute(sql, pk_val, field, old_v, new_v)
                    written += 1
        except Exception as exc:
            # Non-fatal: ingestion must not break because the audit log failed.
            logger.warning(
                "UserChangeTracker.detect_and_log: insert into %s failed for "
                "%s=%r: %s",
                table, pk_col, pk_val, exc,
            )
        return written

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    async def get_recent_changes(
        self,
        user_id: Any,
        limit: int = 50,
        table: str = "telegram_user_changes",
        pk_col: str = "user_id",
    ) -> list[dict]:
        """Return up to ``limit`` most-recent change rows for one user.

        Defaults to telegram_user_changes for ergonomic use from the Telegram
        collector, but ``table`` / ``pk_col`` can be overridden for other
        platforms.
        """
        if self._pool is None:
            return []
        if not _is_safe_ident(table) or not _is_safe_ident(pk_col):
            return []

        sql = (
            f"SELECT id, {pk_col} AS user_id, field, old_value, new_value, "
            f"detected_at "
            f"FROM {table} "
            f"WHERE {pk_col} = $1 "
            f"ORDER BY detected_at DESC "
            f"LIMIT $2"
        )
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, user_id, int(limit))
        return [dict(r) for r in rows]

    async def get_changes_by_field(
        self,
        field: str,
        since_ts=None,
        limit: int = 100,
        table: str = "telegram_user_changes",
        pk_col: str = "user_id",
    ) -> list[dict]:
        """Return up to ``limit`` most-recent change rows for one field.

        Optional ``since_ts`` (datetime) filters to changes detected at-or-after
        that timestamp.
        """
        if self._pool is None:
            return []
        if not _is_safe_ident(table) or not _is_safe_ident(pk_col):
            return []

        if since_ts is None:
            sql = (
                f"SELECT id, {pk_col} AS user_id, field, old_value, new_value, "
                f"detected_at "
                f"FROM {table} "
                f"WHERE field = $1 "
                f"ORDER BY detected_at DESC "
                f"LIMIT $2"
            )
            params: tuple = (field, int(limit))
        else:
            sql = (
                f"SELECT id, {pk_col} AS user_id, field, old_value, new_value, "
                f"detected_at "
                f"FROM {table} "
                f"WHERE field = $1 AND detected_at >= $2 "
                f"ORDER BY detected_at DESC "
                f"LIMIT $3"
            )
            params = (field, since_ts, int(limit))

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)
        return [dict(r) for r in rows]


# ----------------------------------------------------------------------
# Module helpers
# ----------------------------------------------------------------------


def _is_safe_ident(name: str) -> bool:
    """Allow only [A-Za-z0-9_] identifiers (no dots / quoting / SQL chars)."""
    if not name or not isinstance(name, str):
        return False
    return all(ch.isalnum() or ch == "_" for ch in name)


def _get(row: Mapping[str, Any] | Any, field: str) -> Any:
    """Mapping- or attribute-style access, whichever the caller passes."""
    if isinstance(row, Mapping):
        return row.get(field)
    return getattr(row, field, None)


# Convenience: the canonical Telegram diff field set, mirroring the original
# user_intelligence.change_tracker.TRACKED_FIELDS. Kept here as a constant so
# callers don't have to redefine it; it's not baked into the class.
TELEGRAM_TRACKED_FIELDS: tuple[str, ...] = (
    "username",
    "first_name",
    "last_name",
    "bio",
    "profile_photo_id",
    "premium",
    "verified",
    "phone",
)


# Instagram diff field set — mirrors the column subset on instagram_profiles
# that we re-observe per profile fetch. follower/following/post counts drift
# constantly so they're noisy by design; ops keeps them so timeseries-style
# dashboards can replay growth/decay events from the change-log directly.
INSTAGRAM_TRACKED_FIELDS: tuple[str, ...] = (
    "username",
    "full_name",
    "biography",
    "is_verified",
    "is_private",
    "is_business",
    "profile_pic_url",
    "follower_count",
    "following_count",
    "post_count",
    "external_url",
)


# Lemon8 diff field set — the core profile knobs we surface on lemon8_profiles
# plus a couple of stats fields that the dashboards want a timeseries on.
LEMON8_TRACKED_FIELDS: tuple[str, ...] = (
    "username",
    "nickname",
    "biography",
    "follower_count",
    "following_count",
    "like_count",
    "post_count",
    "profile_pic_url",
    "region",
)
