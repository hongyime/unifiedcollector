"""Small adaptive history scheduler for Telegram's already-connected accounts."""
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class AccountThroughput:
    claim_budget: int = 1
    clean_windows: int = 0
    active_claims: int = 0
    claims_done: int = 0
    written_messages: int = 0
    elapsed_seconds: float = 0.0
    flood_waits: int = 0
    cooldown_seconds: int = 0
    started_at: float = field(default_factory=time.monotonic)

    @property
    def write_rate(self) -> float:
        return self.written_messages / self.elapsed_seconds if self.elapsed_seconds > 0 else 0.0


class AdaptiveTelegramHistoryScheduler:
    """Start at one history claim/account and earn modest additional claims.

    Realtime handlers are never part of this budget. A FloodWait resets only
    the account which received it; other connected accounts continue draining.
    This scheduler is confined to one asyncio event loop; do not call it from
    worker threads without external synchronization.
    """

    def __init__(self, *, max_claims: int = 4, clean_windows_to_raise: int = 3):
        self.max_claims = max(1, max_claims)
        self.clean_windows_to_raise = max(1, clean_windows_to_raise)
        self._accounts: dict[str, AccountThroughput] = {}

    def state(self, account: str) -> AccountThroughput:
        return self._accounts.setdefault(account, AccountThroughput())

    def claim_budget(self, account: str) -> int:
        return self.state(account).claim_budget

    def claimed(self, account: str) -> None:
        state = self.state(account)
        if state.active_claims == 0:
            state.started_at = time.monotonic()
        state.active_claims += 1

    def wrote(self, account: str, count: int = 1) -> AccountThroughput:
        """Record durable history writes before a long claim reaches its end."""
        state = self.state(account)
        state.written_messages += max(0, int(count))
        return state

    def completed(self, account: str, *, written: int, elapsed_seconds: float) -> AccountThroughput:
        state = self.state(account)
        state.active_claims = max(0, state.active_claims - 1)
        state.claims_done += 1
        state.written_messages += max(0, int(written))
        state.elapsed_seconds += max(0.0, float(elapsed_seconds))
        state.clean_windows += 1
        if state.clean_windows >= self.clean_windows_to_raise and state.claim_budget < self.max_claims:
            state.claim_budget += 1
            state.clean_windows = 0
        return state

    def flood_wait(self, account: str, seconds: int) -> AccountThroughput:
        state = self.state(account)
        state.active_claims = max(0, state.active_claims - 1)
        state.claim_budget = 1
        state.clean_windows = 0
        state.flood_waits += 1
        state.cooldown_seconds = max(0, int(seconds))
        return state

    def snapshot(self, account: str) -> dict:
        state = self.state(account)
        elapsed = state.elapsed_seconds
        if state.active_claims:
            elapsed += max(0.0, time.monotonic() - state.started_at)
        return {
            "account_identity": account,
            "claim_budget": state.claim_budget,
            "active_claims": state.active_claims,
            "claims_done": state.claims_done,
            "write_rate": round(state.written_messages / elapsed, 3) if elapsed > 0 else 0.0,
            "written_messages": state.written_messages,
            "flood_waits": state.flood_waits,
            "cooldown_seconds": state.cooldown_seconds,
        }
