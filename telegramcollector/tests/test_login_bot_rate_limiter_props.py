"""Property-based tests for check_rate_limit in login_bot/main.py.

Feature: login-bot-session-manager
Property 1: Rate limiter rolling window
"""
import os
import sys
import time
from collections import defaultdict

from hypothesis import given, settings
from hypothesis import strategies as st

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import services.login_bot.main as main_module  # noqa: E402
from services.login_bot.main import check_rate_limit  # noqa: E402


def _reset_state(user_id: int) -> None:
    """Clear rate-limit state for a single user between test runs."""
    with main_module._global_lock:
        main_module.login_attempts[user_id] = []


# ---------------------------------------------------------------------------
# Property 1: Rate limiter rolling window
# Validates: Requirements 5.1, 5.2, 5.3
# ---------------------------------------------------------------------------

@given(
    user_id=st.integers(min_value=1, max_value=10**12),
    extra_attempts=st.integers(min_value=0, max_value=10),
)
@settings(max_examples=300)
def test_property_1_rate_limiter_rolling_window(
    user_id: int, extra_attempts: int
) -> None:
    """**Validates: Requirements 5.1, 5.2, 5.3**

    For any user ID, after 5 successful attempts within the rolling 5-minute
    window, all subsequent attempts must be rejected (return False).
    Attempts older than 5 minutes must not count toward the current window.

    Feature: login-bot-session-manager, Property 1: Rate limiter rolling window
    """
    _reset_state(user_id)

    # First 5 attempts must all succeed
    for i in range(5):
        result = check_rate_limit(user_id)
        assert result is True, (
            f"Attempt {i + 1}/5 should be allowed for user {user_id}, got False"
        )

    # Any further attempts within the same window must be rejected
    for i in range(extra_attempts):
        result = check_rate_limit(user_id)
        assert result is False, (
            f"Attempt {5 + i + 1} should be rejected for user {user_id}, got True"
        )

    _reset_state(user_id)


@given(user_id=st.integers(min_value=1, max_value=10**12))
@settings(max_examples=100)
def test_property_1_expired_attempts_not_counted(user_id: int) -> None:
    """**Validates: Requirements 5.3**

    Timestamps older than 300 seconds must be pruned and not counted toward
    the current window, allowing new attempts to succeed.

    Feature: login-bot-session-manager, Property 1: Rate limiter rolling window
    """
    _reset_state(user_id)

    # Inject 5 timestamps that are already expired (> 300 s ago)
    expired_ts = time.time() - 301.0
    with main_module._global_lock:
        main_module.login_attempts[user_id] = [expired_ts] * 5

    # A fresh attempt should succeed because all prior timestamps are expired
    result = check_rate_limit(user_id)
    assert result is True, (
        f"Attempt after expired window should be allowed for user {user_id}, got False"
    )

    _reset_state(user_id)
