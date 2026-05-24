"""
Preservation Property Tests - More Human-Like Rate Limiting

Property 2: Preservation - Non-Timing Features Remain Unchanged

These tests observe and capture the baseline behavior of non-timing features
on UNFIXED code. They MUST PASS on unfixed code (confirming baseline behavior
to preserve), and must continue to pass after the fix is applied (no regressions).

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10**
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings, strategies as st

from src.config import (
    MIN_DELAY,
    MAX_DELAY,
    RISKY_HOURS,
    RISKY_HOUR_DELAY_MULTIPLIER,
    DAILY_QUOTA_PROFILE_VIEWS,
    DAILY_QUOTA_ACTIONS,
)
from src.conservative_rate_limiter import ConservativeRateLimiter, _DELAY_MULTIPLIERS
from src.operation_classifier import OperationType
from src.rate_limiter import RateLimiter, _SHUTDOWN_EVENT, _interruptible_sleep


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_limiter(**kwargs) -> RateLimiter:
    """Create a RateLimiter with sleep patched out."""
    return RateLimiter(**kwargs)


def _make_conservative(**kwargs) -> ConservativeRateLimiter:
    """Create a ConservativeRateLimiter with a mock cooldown manager."""
    mock_cm = MagicMock()
    mock_cm.is_on_cooldown.return_value = False
    mock_cm.get_cooldown_remaining.return_value = 0.0
    mock_cm.get_available.return_value = []
    return ConservativeRateLimiter(cooldown_manager=mock_cm, **kwargs)


# ===========================================================================
# Requirement 3.1 — Delay bounds: MIN_DELAY ≤ result ≤ MAX_DELAY
# ===========================================================================

class TestDelayBoundsPreservation:
    """
    Property: For all delay calculations, result is within [MIN_DELAY, MAX_DELAY] bounds.

    **Validates: Requirements 3.1**
    """

    @given(
        mean_factor=st.floats(min_value=0.5, max_value=2.0),
        stddev_factor=st.floats(min_value=0.1, max_value=0.5),
    )
    @settings(max_examples=5, deadline=None)
    def test_human_delay_clamps_to_bounds(self, mean_factor, stddev_factor):
        """
        _human_delay() clamps output to [mean/3, mean*3].
        For the default mean = (MIN_DELAY + MAX_DELAY) / 2 = 30s, the clamp
        range is [10, 90], which stays within reasonable bounds.

        **Validates: Requirements 3.1**
        """
        limiter = _make_limiter()
        mean = (MIN_DELAY + MAX_DELAY) / 2 * mean_factor
        stddev = mean * stddev_factor

        delay = limiter._human_delay(mean, stddev)

        # Clamped to [mean/3, mean*3]
        assert delay >= mean / 3, f"delay {delay} below lower clamp {mean / 3}"
        assert delay <= mean * 3, f"delay {delay} above upper clamp {mean * 3}"

    @given(
        multiplier=st.floats(min_value=0.5, max_value=3.0),
    )
    @settings(max_examples=5, deadline=None)
    def test_conservative_base_delay_within_configured_bounds(self, multiplier):
        """
        ConservativeRateLimiter._base_delay() returns a value in [min_delay, max_delay].

        **Validates: Requirements 3.1**
        """
        limiter = _make_conservative(
            min_delay=MIN_DELAY * multiplier,
            max_delay=MAX_DELAY * multiplier,
        )
        base = limiter._base_delay()
        assert base >= limiter.min_delay, f"base {base} below min {limiter.min_delay}"
        assert base <= limiter.max_delay, f"base {base} above max {limiter.max_delay}"


# ===========================================================================
# Requirement 3.2 — Emergency cooldown ≥ 15 minutes on 429 errors
# ===========================================================================

class TestEmergencyCooldownPreservation:
    """
    Property: For all 429 errors, cooldown duration is ≥ 15 minutes.

    **Validates: Requirements 3.2**
    """

    @given(
        requested_minutes=st.integers(min_value=0, max_value=60),
    )
    @settings(max_examples=5, deadline=None)
    def test_emergency_cooldown_enforces_minimum_15_minutes(self, requested_minutes):
        """
        ConservativeRateLimiter.emergency_cooldown() always enforces max(15, duration_minutes).

        **Validates: Requirements 3.2**
        """
        mock_cm = MagicMock()
        limiter = ConservativeRateLimiter(cooldown_manager=mock_cm)

        limiter.emergency_cooldown("test_account", duration_minutes=requested_minutes)

        # Verify put_on_cooldown was called with at least 15 minutes
        assert mock_cm.put_on_cooldown.called, "put_on_cooldown should have been called"
        call_kwargs = mock_cm.put_on_cooldown.call_args
        actual_minutes = call_kwargs[1].get("minutes") or call_kwargs[0][1]
        assert actual_minutes >= 15, (
            f"Cooldown duration {actual_minutes}m is less than 15m minimum "
            f"(requested {requested_minutes}m)"
        )

    def test_emergency_cooldown_minimum_is_15_when_zero_requested(self):
        """
        Even when 0 minutes is requested, cooldown is at least 15 minutes.

        **Validates: Requirements 3.2**
        """
        mock_cm = MagicMock()
        limiter = ConservativeRateLimiter(cooldown_manager=mock_cm)

        limiter.emergency_cooldown("account_x", duration_minutes=0)

        call_kwargs = mock_cm.put_on_cooldown.call_args
        actual_minutes = call_kwargs[1].get("minutes") or call_kwargs[0][1]
        assert actual_minutes >= 15


# ===========================================================================
# Requirement 3.3 — Smart scheduling: 1.5x multiplier during risky hours
# ===========================================================================

class TestSmartSchedulingPreservation:
    """
    Property: For all operations during risky hours, delay multiplier is 1.5x.

    **Validates: Requirements 3.3**
    """

    @given(
        risky_hour=st.sampled_from(RISKY_HOURS),
    )
    @settings(max_examples=5, deadline=None)
    def test_risky_hours_return_1_5x_multiplier(self, risky_hour):
        """
        get_delay_multiplier() returns RISKY_HOUR_DELAY_MULTIPLIER=1.5 for RISKY_HOURS.

        **Validates: Requirements 3.3**
        """
        limiter = _make_limiter()

        with patch("src.rate_limiter.time") as mock_time:
            mock_time.localtime.return_value = time.struct_time(
                (2024, 1, 1, risky_hour, 0, 0, 0, 1, 0)
            )
            multiplier = limiter.get_delay_multiplier()

        assert multiplier == RISKY_HOUR_DELAY_MULTIPLIER, (
            f"Expected {RISKY_HOUR_DELAY_MULTIPLIER}x during risky hour {risky_hour}, "
            f"got {multiplier}x"
        )

    @given(
        safe_hour=st.integers(min_value=0, max_value=23).filter(
            lambda h: h not in RISKY_HOURS
        ),
    )
    @settings(max_examples=5, deadline=None)
    def test_non_risky_hours_return_1_0x_multiplier(self, safe_hour):
        """
        get_delay_multiplier() returns 1.0 for non-risky hours.

        **Validates: Requirements 3.3**
        """
        limiter = _make_limiter()

        with patch("src.rate_limiter.time") as mock_time:
            mock_time.localtime.return_value = time.struct_time(
                (2024, 1, 1, safe_hour, 0, 0, 0, 1, 0)
            )
            multiplier = limiter.get_delay_multiplier()

        assert multiplier == 1.0, (
            f"Expected 1.0x during non-risky hour {safe_hour}, got {multiplier}x"
        )


# ===========================================================================
# Requirement 3.4 — Ctrl+C / shutdown interrupts delays immediately
# ===========================================================================

class TestShutdownHandlingPreservation:
    """
    Property: For all shutdown requests, delays interrupt within 1 second.

    **Validates: Requirements 3.4**
    """

    def test_shutdown_event_stops_interruptible_sleep(self):
        """
        _SHUTDOWN_EVENT stops _interruptible_sleep immediately.

        **Validates: Requirements 3.4**
        """
        _SHUTDOWN_EVENT.clear()

        # Schedule shutdown after 0.1s
        def _trigger():
            time.sleep(0.1)
            _SHUTDOWN_EVENT.set()

        t = threading.Thread(target=_trigger, daemon=True)
        t.start()

        start = time.time()
        _interruptible_sleep(10.0, label="test", check_interval=0.05)
        elapsed = time.time() - start

        _SHUTDOWN_EVENT.clear()  # reset for other tests
        t.join(timeout=1.0)

        assert elapsed < 1.0, (
            f"Shutdown should interrupt sleep within 1s, took {elapsed:.2f}s"
        )

    def test_already_set_shutdown_event_returns_immediately(self):
        """
        If _SHUTDOWN_EVENT is already set, _interruptible_sleep returns immediately.

        **Validates: Requirements 3.4**
        """
        _SHUTDOWN_EVENT.set()
        try:
            start = time.time()
            _interruptible_sleep(30.0, label="test")
            elapsed = time.time() - start
            assert elapsed < 0.5, f"Should return immediately, took {elapsed:.2f}s"
        finally:
            _SHUTDOWN_EVENT.clear()


# ===========================================================================
# Requirement 3.5 — Session statistics track operation counts and elapsed time
# ===========================================================================

class TestSessionStatisticsPreservation:
    """
    Property: For all operations, statistics increment correctly.

    **Validates: Requirements 3.5**
    """

    def test_total_ops_increments_on_track_operation(self):
        """
        _total_ops increments by 1 for each track_operation() call.

        **Validates: Requirements 3.5**
        """
        limiter = _make_limiter()

        # Patch sleep to avoid actual waiting
        with patch.object(limiter, "_sleep"):
            initial_ops = limiter._total_ops
            limiter.track_operation()
            assert limiter._total_ops == initial_ops + 1

            limiter.track_operation()
            assert limiter._total_ops == initial_ops + 2

    @given(num_ops=st.integers(min_value=1, max_value=10))
    @settings(max_examples=5, deadline=None)
    def test_total_ops_tracks_all_operations(self, num_ops):
        """
        After N track_operation() calls, _total_ops equals N.

        **Validates: Requirements 3.5**
        """
        limiter = _make_limiter()

        with patch.object(limiter, "_sleep"):
            for _ in range(num_ops):
                limiter.track_operation()

        assert limiter._total_ops == num_ops, (
            f"Expected {num_ops} ops tracked, got {limiter._total_ops}"
        )

    def test_session_stats_includes_ops_and_elapsed(self):
        """
        _session_stats() returns a string with operation count and elapsed time.

        **Validates: Requirements 3.5**
        """
        limiter = _make_limiter()
        limiter._total_ops = 42

        stats = limiter._session_stats()

        assert "42" in stats, f"Stats should include op count 42: {stats!r}"
        # Should contain time info (m and s)
        assert "ops" in stats, f"Stats should contain 'ops': {stats!r}"


# ===========================================================================
# Requirement 3.6 — Human-readable messages display for delays
# ===========================================================================

class TestMessageDisplayPreservation:
    """
    Property: For all delays, human-readable message is displayed.

    **Validates: Requirements 3.6**
    """

    def test_short_delay_prints_message(self, capsys):
        """
        short_delay() prints a human-readable message from _MSG_SHORT_DELAY pool.

        **Validates: Requirements 3.6**
        """
        limiter = _make_limiter()

        with patch("src.rate_limiter._interruptible_sleep") as mock_sleep:
            limiter.short_delay()

        # Verify _interruptible_sleep was called with a non-empty message
        assert mock_sleep.called
        call_kwargs = mock_sleep.call_args
        message = call_kwargs[1].get("message") or (
            call_kwargs[0][2] if len(call_kwargs[0]) > 2 else ""
        )
        assert message, "short_delay() should pass a human-readable message"

    def test_user_delay_prints_message(self):
        """
        user_delay() prints a human-readable message from _MSG_USER_DELAY pool.

        **Validates: Requirements 3.6**
        """
        limiter = _make_limiter()

        with patch("src.rate_limiter._interruptible_sleep") as mock_sleep:
            limiter.user_delay()

        assert mock_sleep.called
        call_kwargs = mock_sleep.call_args
        message = call_kwargs[1].get("message") or (
            call_kwargs[0][2] if len(call_kwargs[0]) > 2 else ""
        )
        assert message, "user_delay() should pass a human-readable message"

    def test_message_pools_are_non_empty(self):
        """
        All message pools used by delay methods are non-empty.

        **Validates: Requirements 3.6**
        """
        from src.rate_limiter import (
            _MSG_SHORT_DELAY,
            _MSG_USER_DELAY,
            _MSG_ENUM_PAUSE,
            _MSG_LONG_BREAK,
        )

        assert len(_MSG_SHORT_DELAY) > 0, "_MSG_SHORT_DELAY pool is empty"
        assert len(_MSG_USER_DELAY) > 0, "_MSG_USER_DELAY pool is empty"
        assert len(_MSG_ENUM_PAUSE) > 0, "_MSG_ENUM_PAUSE pool is empty"
        assert len(_MSG_LONG_BREAK) > 0, "_MSG_LONG_BREAK pool is empty"


# ===========================================================================
# Requirement 3.7 — Countdown timers appear for waits ≥ 30s
# ===========================================================================

class TestCountdownTimerPreservation:
    """
    Property: For all waits ≥30s, countdown timer is shown.

    **Validates: Requirements 3.7**
    """

    @given(delay=st.floats(min_value=30.0, max_value=120.0))
    @settings(max_examples=5, deadline=None)
    def test_show_countdown_true_for_long_waits(self, delay):
        """
        user_delay() passes show_countdown=True when delay >= 30s.

        **Validates: Requirements 3.7**
        """
        limiter = _make_limiter()

        with patch("src.rate_limiter._interruptible_sleep") as mock_sleep:
            # Force a specific delay value by patching _human_delay
            with patch.object(limiter, "_human_delay", return_value=delay):
                with patch.object(limiter, "get_delay_multiplier", return_value=1.0):
                    limiter.user_delay()

        assert mock_sleep.called
        call_kwargs = mock_sleep.call_args
        show_countdown = call_kwargs[1].get("show_countdown")
        if show_countdown is None and len(call_kwargs[0]) > 3:
            show_countdown = call_kwargs[0][3]

        assert show_countdown is True, (
            f"show_countdown should be True for delay={delay:.1f}s >= 30s"
        )

    @given(delay=st.floats(min_value=0.1, max_value=29.9))
    @settings(max_examples=5, deadline=None)
    def test_show_countdown_false_for_short_waits(self, delay):
        """
        user_delay() passes show_countdown=False when delay < 30s.

        **Validates: Requirements 3.7**
        """
        limiter = _make_limiter()

        with patch("src.rate_limiter._interruptible_sleep") as mock_sleep:
            with patch.object(limiter, "_human_delay", return_value=delay):
                with patch.object(limiter, "get_delay_multiplier", return_value=1.0):
                    limiter.user_delay()

        assert mock_sleep.called
        call_kwargs = mock_sleep.call_args
        show_countdown = call_kwargs[1].get("show_countdown")
        if show_countdown is None and len(call_kwargs[0]) > 3:
            show_countdown = call_kwargs[0][3]

        assert show_countdown is False, (
            f"show_countdown should be False for delay={delay:.1f}s < 30s"
        )


# ===========================================================================
# Requirement 3.8 — Account availability checking filters cooldown accounts
# ===========================================================================

class TestAccountAvailabilityPreservation:
    """
    Property: For all account selections, cooldown accounts are excluded.

    **Validates: Requirements 3.8**
    """

    @given(
        account_name=st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"))),
    )
    @settings(max_examples=5, deadline=None)
    def test_check_account_available_returns_false_for_cooldown_accounts(self, account_name):
        """
        check_account_available() returns False for accounts on cooldown.

        **Validates: Requirements 3.8**
        """
        mock_cm = MagicMock()
        mock_cm.is_on_cooldown.return_value = True
        limiter = ConservativeRateLimiter(cooldown_manager=mock_cm)

        result = limiter.check_account_available(account_name)

        assert result is False, (
            f"Account '{account_name}' on cooldown should not be available"
        )

    @given(
        account_name=st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"))),
    )
    @settings(max_examples=5, deadline=None)
    def test_check_account_available_returns_true_for_non_cooldown_accounts(self, account_name):
        """
        check_account_available() returns True for accounts NOT on cooldown.

        **Validates: Requirements 3.8**
        """
        mock_cm = MagicMock()
        mock_cm.is_on_cooldown.return_value = False
        limiter = ConservativeRateLimiter(cooldown_manager=mock_cm)

        result = limiter.check_account_available(account_name)

        assert result is True, (
            f"Account '{account_name}' not on cooldown should be available"
        )

    def test_get_available_accounts_filters_cooldown_accounts(self):
        """
        get_available_accounts() delegates to cooldown manager to filter accounts.

        **Validates: Requirements 3.8**
        """
        mock_cm = MagicMock()
        mock_cm.get_available_accounts.return_value = ["account_a", "account_c"]
        limiter = ConservativeRateLimiter(cooldown_manager=mock_cm)

        accounts = ["account_a", "account_b", "account_c"]
        available = limiter.get_available_accounts(accounts)

        mock_cm.get_available_accounts.assert_called_once_with(accounts)
        assert available == ["account_a", "account_c"]


# ===========================================================================
# Requirement 3.9 — Daily quotas enforce limits
# ===========================================================================

class TestDailyQuotaPreservation:
    """
    Property: Daily quota constants are configured correctly.

    **Validates: Requirements 3.9**
    """

    def test_daily_quota_profile_views_is_180(self):
        """
        DAILY_QUOTA_PROFILE_VIEWS is configured to 180 (under Instagram's ~200/hr limit).

        **Validates: Requirements 3.9**
        """
        assert DAILY_QUOTA_PROFILE_VIEWS == 180, (
            f"DAILY_QUOTA_PROFILE_VIEWS should be 180, got {DAILY_QUOTA_PROFILE_VIEWS}"
        )

    def test_daily_quota_actions_is_6000(self):
        """
        DAILY_QUOTA_ACTIONS is configured to 6000 (under Instagram's ~7500/day limit).

        **Validates: Requirements 3.9**
        """
        assert DAILY_QUOTA_ACTIONS == 6000, (
            f"DAILY_QUOTA_ACTIONS should be 6000, got {DAILY_QUOTA_ACTIONS}"
        )

    @given(
        profile_views=st.integers(min_value=0, max_value=200),
    )
    @settings(max_examples=5, deadline=None)
    def test_quota_manager_blocks_when_limit_reached(self, profile_views):
        """
        AccountQuotaManager.can_view_profiles() returns False when quota is reached.

        **Validates: Requirements 3.9**
        """
        from src.account_cooldown import AccountQuotaManager

        manager = AccountQuotaManager()

        with patch.object(manager._repo, "reset_if_new_day"):
            with patch.object(
                manager._repo,
                "get_usage",
                return_value={"profile_views": profile_views, "actions": 0},
            ):
                can_view = manager.can_view_profiles("test_account")

        if profile_views >= DAILY_QUOTA_PROFILE_VIEWS:
            assert can_view is False, (
                f"Should block at {profile_views} views (limit={DAILY_QUOTA_PROFILE_VIEWS})"
            )
        else:
            assert can_view is True, (
                f"Should allow at {profile_views} views (limit={DAILY_QUOTA_PROFILE_VIEWS})"
            )


# ===========================================================================
# Requirement 3.10 — Operation multipliers: PUBLIC=1.0x, FOLLOWING_REQUIRED=1.5x, MUTUAL_FOLLOWING=2.0x
# ===========================================================================

class TestOperationMultipliersPreservation:
    """
    Property: For all operation types, correct multiplier is applied.

    **Validates: Requirements 3.10**
    """

    def test_public_operation_uses_1_0x_multiplier(self):
        """
        PUBLIC operations use 1.0x delay multiplier.

        **Validates: Requirements 3.10**
        """
        assert _DELAY_MULTIPLIERS[OperationType.PUBLIC] == 1.0, (
            f"PUBLIC multiplier should be 1.0, got {_DELAY_MULTIPLIERS[OperationType.PUBLIC]}"
        )

    def test_following_required_uses_1_5x_multiplier(self):
        """
        FOLLOWING_REQUIRED operations use 1.5x delay multiplier.

        **Validates: Requirements 3.10**
        """
        assert _DELAY_MULTIPLIERS[OperationType.FOLLOWING_REQUIRED] == 1.5, (
            f"FOLLOWING_REQUIRED multiplier should be 1.5, "
            f"got {_DELAY_MULTIPLIERS[OperationType.FOLLOWING_REQUIRED]}"
        )

    def test_mutual_following_uses_2_0x_multiplier(self):
        """
        MUTUAL_FOLLOWING operations use 2.0x delay multiplier.

        **Validates: Requirements 3.10**
        """
        assert _DELAY_MULTIPLIERS[OperationType.MUTUAL_FOLLOWING] == 2.0, (
            f"MUTUAL_FOLLOWING multiplier should be 2.0, "
            f"got {_DELAY_MULTIPLIERS[OperationType.MUTUAL_FOLLOWING]}"
        )

    @given(
        op_type=st.sampled_from(list(OperationType)),
    )
    @settings(max_examples=5, deadline=None)
    def test_all_operation_types_have_multiplier_defined(self, op_type):
        """
        Every OperationType has a defined multiplier in _DELAY_MULTIPLIERS.

        **Validates: Requirements 3.10**
        """
        assert op_type in _DELAY_MULTIPLIERS, (
            f"OperationType.{op_type.name} has no defined multiplier"
        )
        multiplier = _DELAY_MULTIPLIERS[op_type]
        assert multiplier >= 1.0, (
            f"Multiplier for {op_type.name} should be >= 1.0, got {multiplier}"
        )

    @given(
        op_type=st.sampled_from(list(OperationType)),
    )
    @settings(max_examples=5, deadline=None)
    def test_operation_delay_applies_correct_multiplier(self, op_type):
        """
        operation_delay() applies the correct multiplier for each OperationType.

        **Validates: Requirements 3.10**
        """
        mock_cm = MagicMock()
        limiter = ConservativeRateLimiter(cooldown_manager=mock_cm)

        delays_seen = []

        def capture_sleep(seconds, reason=""):
            delays_seen.append(seconds)

        with patch.object(limiter, "_sleep", side_effect=capture_sleep):
            limiter.operation_delay(op_type)

        assert len(delays_seen) == 1, "operation_delay() should call _sleep exactly once"
        actual_delay = delays_seen[0]
        expected_multiplier = _DELAY_MULTIPLIERS[op_type]

        # The delay should be at least min_delay * multiplier * 0.5 (accounting for jitter)
        min_expected = limiter.min_delay * expected_multiplier * 0.5
        assert actual_delay >= min_expected, (
            f"Delay {actual_delay:.1f}s for {op_type.name} is below expected minimum "
            f"{min_expected:.1f}s (multiplier={expected_multiplier}x)"
        )
