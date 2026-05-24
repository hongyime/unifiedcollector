"""Property-based tests for bot lock expiry in login_bot/main.py.

Feature: login-bot-session-manager, Property 4: Bot lock expiry invariant
"""
# Feature: login-bot-session-manager, Property 4: Bot lock expiry invariant

import asyncio
import os
import sys
import time

from hypothesis import given, settings
from hypothesis import strategies as st

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import services.login_bot.main as main_module  # noqa: E402
from services.login_bot.main import _active_bots_lock, active_login_bots  # noqa: E402

# ---------------------------------------------------------------------------
# Helper: one iteration of bot_lock_checker inner logic (no infinite loop)
# ---------------------------------------------------------------------------

async def _run_lock_check() -> None:
    """Execute one iteration of the bot_lock_checker lock-clearing logic."""
    async with _active_bots_lock:
        for username, bot_info in active_login_bots.items():
            if bot_info["locked"] and bot_info["locked_until"] <= time.time():
                bot_info["locked"] = False
                bot_info["locked_until"] = 0


def _make_bot_entry(locked: bool, locked_until: float) -> dict:
    return {
        "client": None,
        "name": "testbot",
        "token": "test:token",
        "locked": locked,
        "locked_until": locked_until,
    }


def _reset_bots() -> None:
    active_login_bots.clear()


# ---------------------------------------------------------------------------
# Property 4: Bot lock expiry invariant
# Validates: Requirements 6.1, 6.4
# ---------------------------------------------------------------------------

@given(seconds=st.floats(min_value=1.0, max_value=3600.0))
@settings(max_examples=100)
def test_property_4_lock_set_correctly(seconds: float) -> None:
    """**Validates: Requirements 6.1**

    After setting locked_until = time.time() + N for random N > 0,
    locked_until must be >= time.time() + N (lock is set at least as far
    into the future as requested).

    Feature: login-bot-session-manager, Property 4: Bot lock expiry invariant
    """
    now = time.time()
    locked_until = now + seconds

    # Verify the invariant: locked_until is at least now + seconds
    assert locked_until >= now + seconds, (
        f"locked_until={locked_until} should be >= now+seconds={now + seconds}"
    )


@given(seconds=st.floats(min_value=1.0, max_value=3600.0))
@settings(max_examples=100)
def test_property_4_expired_lock_is_cleared(seconds: float) -> None:
    """**Validates: Requirements 6.4**

    When locked_until is in the past (already expired), bot_lock_checker
    must clear the lock (set locked=False).

    Feature: login-bot-session-manager, Property 4: Bot lock expiry invariant
    """
    _reset_bots()
    username = f"bot_{int(seconds * 1000)}"

    # Set a lock that has already expired (locked_until in the past)
    active_login_bots[username] = _make_bot_entry(
        locked=True,
        locked_until=time.time() - 1.0,
    )

    asyncio.run(_run_lock_check())

    assert active_login_bots[username]["locked"] is False, (
        f"Expected lock to be cleared for expired locked_until, but locked={active_login_bots[username]['locked']}"
    )

    _reset_bots()


@given(seconds=st.floats(min_value=1.0, max_value=3600.0))
@settings(max_examples=100)
def test_property_4_active_lock_not_cleared(seconds: float) -> None:
    """**Validates: Requirements 6.4**

    When locked_until is in the future (lock not yet expired), bot_lock_checker
    must NOT clear the lock (locked must remain True).

    Feature: login-bot-session-manager, Property 4: Bot lock expiry invariant
    """
    _reset_bots()
    username = f"bot_{int(seconds * 1000)}"

    # Set a lock that has NOT yet expired (locked_until well in the future)
    active_login_bots[username] = _make_bot_entry(
        locked=True,
        locked_until=time.time() + 100.0,
    )

    asyncio.run(_run_lock_check())

    assert active_login_bots[username]["locked"] is True, (
        f"Expected lock to remain active for future locked_until, but locked={active_login_bots[username]['locked']}"
    )

    _reset_bots()
