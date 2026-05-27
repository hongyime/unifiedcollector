"""Per-account TLS fingerprint pinning + rotation.

Each Instagram (or other platform) account is pinned to a stable
``curl_cffi`` impersonate target — e.g. ``chrome120``, ``safari17_2``,
``edge101`` — so its TLS/JA3 fingerprint is consistent across requests.
Per-request rotation defeats the purpose: detection systems flag
*changing* fingerprints on a single session as bot-like.

Rotation is therefore driven by FAILURE ONLY (HTTP 403 / 429) and gated
by a cooldown so a flurry of failures doesn't chew through the whole
profile list in one second.

Persistence
-----------

State lives in ``instagram_tls_state``:

    account_id           PK
    impersonate_target   currently active fingerprint
    last_rotation_at     wall-clock timestamp of last rotation
    rotation_count       monotonic counter
    last_failure_reason  free-form last failure code/reason

The rotator works without a DB (in-memory mode for tests / dry-runs).
Pass ``db_pool=None`` and call ``persist()`` only when you're ready.

Usage
-----

    rotator = TLSFingerprintRotator(
        account_id="ACCOUNT_1",
        available_impersonates=["chrome120", "safari17_2", "edge101"],
        cooldown_secs=600,
    )
    await rotator.load(pool)             # optional — re-uses prior pin
    kwargs = rotator.get_curl_cffi_kwargs()
    # pass kwargs to curl_cffi.requests.AsyncSession or similar

    # on a 429:
    await rotator.rotate_on_failure(reason="429", pool=pool)
"""
from __future__ import annotations

import hashlib
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Sensible curl_cffi impersonate defaults — keep modern, drop ancient.
DEFAULT_IMPERSONATES: List[str] = [
    "chrome120",
    "chrome119",
    "safari17_2",
    "edge101",
]


class TLSFingerprintRotator:
    """Stable-per-account, cooldown-gated impersonate rotator."""

    def __init__(
        self,
        account_id: str,
        available_impersonates: Optional[List[str]] = None,
        cooldown_secs: int = 600,
        *,
        clock=None,
    ):
        if not account_id:
            raise ValueError("account_id is required")
        self.account_id = account_id
        if available_impersonates is None:
            self.available: List[str] = list(DEFAULT_IMPERSONATES)
        else:
            self.available = list(available_impersonates)
        if not self.available:
            raise ValueError("available_impersonates must not be empty")
        self.cooldown_secs = int(cooldown_secs)
        self._clock = clock or time.time

        # Deterministic initial selection — same account always boots on
        # the same fingerprint until rotation.
        self._index = self._stable_index_for(account_id, len(self.available))
        self._last_rotation_at: float = 0.0
        self._rotation_count: int = 0
        self._last_failure_reason: Optional[str] = None

    # ---- selection / rotation ------------------------------------------

    @staticmethod
    def _stable_index_for(account_id: str, n: int) -> int:
        """Hash ``account_id`` to a stable index in [0, n)."""
        h = hashlib.sha256(account_id.encode("utf-8")).digest()
        return int.from_bytes(h[:4], "big") % max(1, n)

    def get_current(self) -> str:
        """Return the currently pinned impersonate target."""
        return self.available[self._index]

    def get_curl_cffi_kwargs(self) -> Dict[str, Any]:
        """kwargs to splat into ``curl_cffi.requests.AsyncSession`` / ``Session``."""
        return {"impersonate": self.get_current()}

    def can_rotate(self) -> bool:
        """True if the cooldown window has elapsed since the last rotation."""
        if self._last_rotation_at <= 0:
            return True
        return (self._clock() - self._last_rotation_at) >= self.cooldown_secs

    def rotate_on_failure(self, reason: Optional[str] = None) -> str:
        """Advance to the next impersonate target if cooldown allows.

        Returns the impersonate target in effect after the call (may be
        unchanged if still in cooldown).
        """
        if not self.can_rotate():
            logger.debug(
                "TLS rotator: cooldown active for %s (%.0fs remaining), staying on %s",
                self.account_id,
                self.cooldown_secs - (self._clock() - self._last_rotation_at),
                self.get_current(),
            )
            return self.get_current()

        old = self.get_current()
        self._index = (self._index + 1) % len(self.available)
        self._last_rotation_at = self._clock()
        self._rotation_count += 1
        self._last_failure_reason = reason
        logger.info(
            "TLS rotator: %s rotated %s -> %s (reason=%s, count=%d)",
            self.account_id, old, self.get_current(), reason, self._rotation_count,
        )
        return self.get_current()

    # ---- persistence (optional — DB-backed) ----------------------------

    def to_row(self) -> Dict[str, Any]:
        """Snapshot state as a dict suitable for asyncpg execute params."""
        return {
            "account_id": self.account_id,
            "impersonate_target": self.get_current(),
            "rotation_count": self._rotation_count,
            "last_failure_reason": self._last_failure_reason,
        }

    def apply_row(self, row: Dict[str, Any]) -> None:
        """Restore state from a previously persisted row.

        Unknown impersonate strings are dropped — we'll re-pick the
        deterministic default. This makes config rolls (e.g. dropping a
        retired chrome version) safe.
        """
        target = row.get("impersonate_target")
        if target in self.available:
            self._index = self.available.index(target)
        rc = row.get("rotation_count")
        if isinstance(rc, int) and rc >= 0:
            self._rotation_count = rc
        last_rot = row.get("last_rotation_at")
        # Accept either datetime or unix-seconds; convert to float seconds.
        if last_rot is not None:
            try:
                if hasattr(last_rot, "timestamp"):
                    self._last_rotation_at = float(last_rot.timestamp())
                else:
                    self._last_rotation_at = float(last_rot)
            except (TypeError, ValueError):
                pass
        self._last_failure_reason = row.get("last_failure_reason")

    async def load(self, pool) -> bool:
        """Load row from DB; returns True if a prior row existed."""
        if pool is None:
            return False
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT account_id, impersonate_target, last_rotation_at, "
                    "rotation_count, last_failure_reason "
                    "FROM instagram_tls_state WHERE account_id = $1",
                    self.account_id,
                )
        except Exception as e:
            logger.warning("TLS rotator load failed for %s: %s", self.account_id, e)
            return False
        if not row:
            # First-time bootstrap: write our deterministic pick so other
            # workers see a stable target.
            await self.persist(pool)
            return False
        self.apply_row(dict(row))
        return True

    async def persist(self, pool) -> None:
        """Upsert current state to ``instagram_tls_state``."""
        if pool is None:
            return
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO instagram_tls_state
                        (account_id, impersonate_target, last_rotation_at,
                         rotation_count, last_failure_reason)
                    VALUES ($1, $2, NOW(), $3, $4)
                    ON CONFLICT (account_id) DO UPDATE SET
                        impersonate_target  = EXCLUDED.impersonate_target,
                        last_rotation_at    = NOW(),
                        rotation_count      = EXCLUDED.rotation_count,
                        last_failure_reason = EXCLUDED.last_failure_reason
                    """,
                    self.account_id,
                    self.get_current(),
                    self._rotation_count,
                    self._last_failure_reason,
                )
        except Exception as e:
            logger.warning("TLS rotator persist failed for %s: %s", self.account_id, e)
