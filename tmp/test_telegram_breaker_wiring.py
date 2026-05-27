"""Wiring smoke test for Telegram CircuitBreaker integration.

Verifies the worker has a breaker, breaker classifies real errors, and
the run_targets path correctly surfaces CircuitOpenError without
crashing the loop.

This does NOT touch Telegram. It mocks _collect_chat to fail N times
and confirms the breaker opens, plus that record_error_classified +
record_flood_wait are actually called.
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

# Stub telethon so importing telegram.py doesn't pull a real client.
_telethon = types.ModuleType("telethon")
_telethon_sync = types.ModuleType("telethon.sync")
_telethon_sync.TelegramClient = MagicMock()
_telethon_tl = types.ModuleType("telethon.tl")
_telethon_tl_types = types.ModuleType("telethon.tl.types")
_telethon_tl_types.MessageMediaPhoto = type("MessageMediaPhoto", (), {})
_telethon_tl_types.MessageMediaDocument = type("MessageMediaDocument", (), {})
_telethon_tl_functions = types.ModuleType("telethon.tl.functions")
_telethon_tl_functions_stories = types.ModuleType("telethon.tl.functions.stories")
_telethon_tl_functions_stories.GetPeerStoriesRequest = MagicMock()
sys.modules.update({
    "telethon": _telethon,
    "telethon.sync": _telethon_sync,
    "telethon.tl": _telethon_tl,
    "telethon.tl.types": _telethon_tl_types,
    "telethon.tl.functions": _telethon_tl_functions,
    "telethon.tl.functions.stories": _telethon_tl_functions_stories,
})

from src.collectors.telegram import TelegramWorker
from src.core.circuit_breaker import CircuitBreaker, CircuitOpenError
from src.core.account_pool import Account, AccountPool


def make_worker(parent_pool: AccountPool, account_name: str = "tg_test") -> TelegramWorker:
    parent = MagicMock()
    parent.account_pool = parent_pool
    parent._stop = MagicMock()
    parent._stop.is_set = MagicMock(return_value=False)
    parent.checkpoint = MagicMock()
    parent.checkpoint.save_progress = AsyncMock()
    parent.send_to_dlq = AsyncMock()
    parent._handle_flood_wait = AsyncMock()

    parent_pool.add_account(account_name, {"api_id": "0", "api_hash": "x"})
    acct = parent_pool._accounts[-1]

    w = TelegramWorker(parent, acct, worker_id=0)
    return w, parent


async def test_breaker_attached():
    pool = AccountPool()
    w, _ = make_worker(pool)
    assert isinstance(w.breaker, CircuitBreaker), "worker missing CircuitBreaker"
    assert w.breaker.failure_threshold == 5
    print("PASS test_breaker_attached")


async def test_breaker_opens_after_threshold():
    pool = AccountPool()
    w, parent = make_worker(pool, "tg_open")

    parent._collect_chat = AsyncMock(side_effect=RuntimeError("network died"))

    # 5 failures should open the breaker.
    await w.run_targets(["c1", "c2", "c3", "c4", "c5"])
    assert w.breaker.state == CircuitBreaker.OPEN, f"expected OPEN, got {w.breaker.state}"
    # And the next target should fast-fail with circuit_open in DLQ.
    await w.run_targets(["c6"])
    # Final call to send_to_dlq should mention circuit_open
    last_call = parent.send_to_dlq.call_args
    assert last_call is not None and "circuit_open" in last_call.args[2], \
        f"expected circuit_open in last DLQ msg, got {last_call}"
    print("PASS test_breaker_opens_after_threshold")


async def test_floodwait_routed_to_record_flood_wait():
    pool = AccountPool()
    w, parent = make_worker(pool, "tg_flood")

    class FloodWaitError(Exception):
        seconds = 1
    parent._collect_chat = AsyncMock(side_effect=FloodWaitError("flood"))
    # Inline _handle_flood_wait so we test the real method (not the mock).
    from src.collectors.telegram import TelegramCollector
    real_handler = TelegramCollector._handle_flood_wait
    parent._handle_flood_wait = lambda worker, e: real_handler(parent, worker, e)
    parent.account_pool = pool

    # Patch asyncio.sleep INSIDE the telegram module so the real flood-wait
    # cap (42s) doesn't actually block this test for 42 seconds.
    import src.collectors.telegram as tg_mod
    with patch.object(tg_mod.asyncio, "sleep", new=AsyncMock()):
        await w.run_targets(["chan1"])
    # cooldown should have been set via record_flood_wait → cooldown_reason set
    acct = pool._accounts[0]
    assert acct.cooldown_reason == "flood-wait", \
        f"expected cooldown_reason=flood-wait, got {acct.cooldown_reason!r}"
    assert acct.locked_until > 0, "locked_until should be set after flood-wait"
    print("PASS test_floodwait_routed_to_record_flood_wait")


async def test_classified_error_on_non_flood():
    pool = AccountPool()
    w, parent = make_worker(pool, "tg_auth")

    parent._collect_chat = AsyncMock(side_effect=RuntimeError("Unauthorized: invalid session"))

    await w.run_targets(["t1"])
    acct = pool._accounts[0]
    assert acct.last_error_kind == "auth_failure", \
        f"expected auth_failure, got {acct.last_error_kind!r}"
    print("PASS test_classified_error_on_non_flood")


async def test_success_records_to_pool():
    pool = AccountPool()
    w, parent = make_worker(pool, "tg_ok")

    parent._collect_chat = AsyncMock()  # default returns None
    await w.run_targets(["good"])
    acct = pool._accounts[0]
    assert acct.error_count == 0
    assert w.breaker.state == CircuitBreaker.CLOSED
    print("PASS test_success_records_to_pool")


async def main():
    await test_breaker_attached()
    await test_breaker_opens_after_threshold()
    await test_floodwait_routed_to_record_flood_wait()
    await test_classified_error_on_non_flood()
    await test_success_records_to_pool()
    print("\nAll telegram-breaker wiring tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
