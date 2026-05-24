#!/usr/bin/env python3
"""
Shared account-health policy for Telegram client lifecycle handling.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, Optional

from src.core.dynamic_config import get_config_value
from src.core.progress_logger import log_error, log_info, log_success, log_warning


@dataclass(frozen=True)
class AccountErrorClassification:
    """Normalized account-level fault classification."""

    code: str
    reconnectable: bool


@dataclass
class AccountHealthState:
    """Mutable state for one account within a single run."""

    status: str = "active"
    retry_count: int = 0
    last_error: str = ""
    last_phase: str = ""


class AccountFailureError(RuntimeError):
    """Raised when an account-level fault should be surfaced to the runner."""

    def __init__(self, account_name: str, error: Exception, phase: str = ""):
        self.account_name = account_name
        self.original_error = error
        self.phase = phase
        super().__init__(str(error))


ACCOUNT_ERROR_MATCHERS: tuple[tuple[str, AccountErrorClassification], ...] = (
    ("cannot send requests while disconnected", AccountErrorClassification("disconnected", True)),
    ("client is disconnected", AccountErrorClassification("disconnected", True)),
    ("not connected", AccountErrorClassification("disconnected", True)),
    ("connection lost", AccountErrorClassification("connection_lost", True)),
    ("connection closed", AccountErrorClassification("connection_lost", True)),
    ("timed out", AccountErrorClassification("timeout", True)),
    ("timeout", AccountErrorClassification("timeout", True)),
    ("network is unreachable", AccountErrorClassification("network", True)),
    ("server closed the connection", AccountErrorClassification("network", True)),
    ("auth key", AccountErrorClassification("auth", False)),
    ("session revoked", AccountErrorClassification("session", False)),
    ("session expired", AccountErrorClassification("session", False)),
    ("user_deactivated", AccountErrorClassification("auth", False)),
    ("phone number banned", AccountErrorClassification("auth", False)),
    ("api_id_invalid", AccountErrorClassification("auth", False)),
    ("auth_key_unregistered", AccountErrorClassification("auth", False)),
)


def classify_account_error(error: Exception) -> Optional[AccountErrorClassification]:
    """Return account-level classification when an exception indicates client/session failure."""
    message = str(error).lower().strip()
    if not message:
        return None

    for needle, classification in ACCOUNT_ERROR_MATCHERS:
        if needle in message:
            return classification
    return None


def is_account_error(error: Exception) -> bool:
    """Convenience predicate for account-level faults."""
    return classify_account_error(error) is not None


class AccountHealthPolicy:
    """Run-scoped circuit breaker and reconnect policy for Telegram accounts."""

    def __init__(self) -> None:
        self.max_reconnect_attempts = int(get_config_value("ACCOUNT_RECONNECT_MAX_ATTEMPTS", 3) or 3)
        self.base_delay = float(get_config_value("ACCOUNT_RECONNECT_BASE_DELAY", 2.0) or 2.0)
        self._states: Dict[str, AccountHealthState] = {}
        self._flood_wait_until: Dict[str, float] = {}
        # Load persisted cooldowns from DB (survive restarts)
        try:
            import time
            from src.core.state_manager import get_state_manager
            state = get_state_manager()
            rows = state.conn.execute(
                'SELECT account_name, until_ts FROM account_cooldowns WHERE until_ts > ?',
                (time.time(),)
            ).fetchall()
            for row in rows:
                self._flood_wait_until[row['account_name']] = row['until_ts']
        except Exception:
            pass  # DB may not exist yet on first run

    def record_flood_wait(self, account_name: str, seconds: int) -> None:
        """Record that an account hit a FloodWait and persist to DB for cross-restart survival."""
        import time
        until_ts = time.time() + seconds
        self._flood_wait_until[account_name] = until_ts
        try:
            from src.core.state_manager import get_state_manager
            state = get_state_manager()
            state.conn.execute(
                'INSERT OR REPLACE INTO account_cooldowns'
                ' (account_name, until_ts, reason, flood_wait_seconds)'
                ' VALUES (?, ?, ?, ?)',
                (account_name, until_ts, 'flood-wait', seconds)
            )
            state.conn.commit()
        except Exception:
            pass  # DB persistence is best-effort; memory cache is primary

    def is_available(self, account_name: str) -> bool:
        """Return True if the account is active and not currently flood-waited."""
        if self.is_retired(account_name):
            return False
        import time
        deadline = self._flood_wait_until.get(account_name, 0)
        return time.time() >= deadline

    def get_best_account(
        self,
        account_names: list[str],
        exclude: Optional[str] = None,
    ) -> Optional[str]:
        """Pick the best available account — not retired, not flood-waited, fewest retries.

        Args:
            account_names: candidate account names to choose from.
            exclude: an account name to skip (typically the one that just failed).

        Returns:
            The best account name, or None if all are unavailable.
        """
        candidates = [
            name for name in account_names
            if name != exclude and self.is_available(name)
        ]
        if not candidates:
            return None
        # Prefer the account with the fewest cumulative retries
        return min(candidates, key=lambda n: self.get_state(n).retry_count)

    def get_state(self, account_name: str) -> AccountHealthState:
        """Get or create per-account state."""
        return self._states.setdefault(account_name, AccountHealthState())

    def is_retired(self, account_name: str) -> bool:
        """Return True when an account has been retired for the current run."""
        return self.get_state(account_name).status == "retired_for_run"

    async def ensure_connected(self, client: Any, account: Dict[str, Any] | str) -> bool:
        """Verify the client is connected, reconnecting if needed."""
        account_name = account["name"] if isinstance(account, dict) else str(account)
        if self.is_retired(account_name):
            return False

        if await self._client_is_connected(client):
            return True

        return await self._attempt_reconnect(client, account, RuntimeError("Client is disconnected"), "connectivity check")

    async def handle_account_failure(
        self,
        client: Any,
        account: Dict[str, Any] | str,
        error: Exception,
        phase: str,
    ) -> bool:
        """
        Handle a surfaced account-level error.

        Returns True if the account recovered and can continue.
        Returns False if it has been retired for the current run.
        """
        account_name = account["name"] if isinstance(account, dict) else str(account)
        classification = classify_account_error(error)
        if classification is None:
            return True

        state = self.get_state(account_name)
        state.last_error = str(error)
        state.last_phase = phase

        if self.is_retired(account_name):
            return False

        log_warning(
            f"[account_health] Account fault detected for {account_name} during {phase}: {error}"
        )

        if not classification.reconnectable:
            state.status = "retired_for_run"
            log_error(
                f"[account_health] Retiring {account_name} for this run due to non-recoverable {classification.code} error"
            )
            await self._safe_disconnect(client)
            return False

        return await self._attempt_reconnect(client, account, error, phase)

    async def _attempt_reconnect(
        self,
        client: Any,
        account: Dict[str, Any] | str,
        error: Exception,
        phase: str,
    ) -> bool:
        account_name = account["name"] if isinstance(account, dict) else str(account)
        state = self.get_state(account_name)
        state.status = "reconnecting"

        for attempt in range(1, self.max_reconnect_attempts + 1):
            state.retry_count = attempt
            delay = self.base_delay * (2 ** (attempt - 1))
            log_warning(
                f"[account_health] Reconnect attempt {attempt}/{self.max_reconnect_attempts} for {account_name} "
                f"after {phase}; backing off {delay:.1f}s"
            )
            await asyncio.sleep(delay)
            try:
                await self._safe_disconnect(client)
                if hasattr(client, "connect"):
                    await client.connect()

                if not await self._client_is_connected(client) and hasattr(client, "start"):
                    phone = account.get("phone") if isinstance(account, dict) else None
                    if phone:
                        await client.start(phone)
                    else:
                        await client.start()

                if hasattr(client, "is_user_authorized"):
                    try:
                        authorized = await client.is_user_authorized()
                    except TypeError:
                        authorized = await client.is_user_authorized  # pragma: no cover
                    if not authorized and hasattr(client, "start"):
                        phone = account.get("phone") if isinstance(account, dict) else None
                        if phone:
                            await client.start(phone)
                        else:
                            await client.start()

                if await self._client_is_connected(client):
                    state.status = "active"
                    state.retry_count = 0
                    log_success(
                        f"[account_health] Reconnected {account_name}; resuming current run"
                    )
                    return True
            except Exception as reconnect_error:
                state.last_error = str(reconnect_error)
                log_warning(
                    f"[account_health] Reconnect failed for {account_name} on attempt {attempt}: {reconnect_error}"
                )

        state.status = "retired_for_run"
        log_error(
            f"[account_health] Retiring {account_name} for this run after failed reconnect attempts"
        )
        await self._safe_disconnect(client)
        return False

    async def _client_is_connected(self, client: Any) -> bool:
        checker = getattr(client, "is_connected", None)
        if checker is None:
            return False
        try:
            if callable(checker):
                result = checker()
                if asyncio.iscoroutine(result):
                    result = await result
                return bool(result)
            return bool(checker)
        except Exception:
            return False

    async def _safe_disconnect(self, client: Any) -> None:
        disconnect = getattr(client, "disconnect", None)
        if disconnect is None:
            return
        try:
            result = disconnect()
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            return


def summarize_account_health_drift() -> str:
    """Return a concise audit summary used by docs/tests."""
    return (
        "Unified scan paths now share a single account-health policy. "
        "Legacy multi-account flows route reconnect/retire decisions through the same core classifier, "
        "while task-specific business logic like join semantics and photo validation remains intentionally separate."
    )
