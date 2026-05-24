"""
Preservation Property Tests - Non-Timing Features (Task 2)

These tests verify that non-timing features remain unchanged after the fix.
They should PASS on UNFIXED code to establish baseline behavior, and continue
to PASS on FIXED code to confirm no regressions.

**Property 2: Preservation** - Non-Timing Features Remain Unchanged

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10**

**IMPORTANT**: Use max_examples=5 for faster test execution
"""

import os
import sys
import time
import threading
from unittest.mock import MagicMock, patch, call

import pytest
from hypothesis import given, settings, strategies as st, assume

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.rate_limiter import RateLimiter, _SHUTDOWN_EVENT, _interruptible_sleep
from src.conservative_rate_limiter import ConservativeRateLimiter, _DELAY_MULTIPLIERS
from src.account_cooldown import AccountCooldownManager, AccountQuotaManager
from src.operation_classifier import OperationType
from src.config import (
    MIN_DELAY,
    MAX_DELAY,
    ACCOUNT_COOLDOWN_MINUTES,
    DAILY_QUOTA_PROFILE_VIEWS,
    DAILY_QUOTA_ACTIONS,
    RISKY_HOUR_DELAY_MULTIPLIER,
)


# ---------------------------------------------------------------------------
# Property 2.1: Delays Respect MIN_DELAY and MAX_DELAY Bounds
# Validates: Requirement 3.1
# ---------------------------------------------------------------------------

class TestDelayBoundsPreservation:
    """
    **Validates: Requirement 3.1**
    
    Property: For all delay calculations, result is within [MIN_DELAY, MAX_DELAY] bounds
    """

    @given(
        min_delay=st.floats(min_value=1.0, max_value=20.0),
        max_delay=st.floats(min_value=20.0, max_value=60.0)
    )
    @settings(max_examples=5, deadline=None)
    def test_property_rate_limiter_respects_bounds(self, min_delay, max_delay):
        """
        Property: RateLimiter._human_delay() respects configured bounds.
        
        For any min/max delay configuration, the generated delay should fall
        within reasonable bounds (mean/3 to mean*3 for Gaussian).
        """
        assume(min_delay < max_delay)
        
        limiter = RateLimiter(min_delay=min_delay, max_delay=max_delay, label="test")
        mean = (min_delay + max_delay) / 2
        
        # Generate multiple delays and verify they're within bounds
        for _ in range(10):
            delay = limiter._human_delay(mean)
            # Gaussian is clamped to [mean/3, mean*3]
            assert mean / 3 <= delay <= mean * 3, \
                f"Delay {delay} should be within [{mean/3}, {mean*3}]"

    @given(
        min_delay=st.floats(min_value=1.0, max_value=20.0),
        max_delay=st.floats(min_value=20.0, max_value=60.0)
    )
    @settings(max_examples=5, deadline=None)
    def test_property_conservative_limiter_respects_bounds(self, min_delay, max_delay):
        """
        Property: ConservativeRateLimiter._base_delay() respects configured bounds.
        
        For any min/max delay configuration, the base delay should fall
        within [min_delay, max_delay].
        """
        assume(min_delay < max_delay)
        
        limiter = ConservativeRateLimiter(min_delay=min_delay, max_delay=max_delay)
        
        # Generate multiple delays and verify they're within bounds
        for _ in range(10):
            delay = limiter._base_delay()
            assert min_delay <= delay <= max_delay, \
                f"Base delay {delay} should be within [{min_delay}, {max_delay}]"


# ---------------------------------------------------------------------------
# Property 2.2: 429 Errors Trigger Emergency Cooldown for 15+ Minutes
# Validates: Requirement 3.2
# ---------------------------------------------------------------------------

class TestEmergencyCooldownPreservation:
    """
    **Validates: Requirement 3.2**
    
    Property: For all 429 errors, cooldown duration is ≥15 minutes
    """

    @given(
        account_name=st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"))),
        requested_minutes=st.integers(min_value=1, max_value=30)
    )
    @settings(max_examples=5, deadline=None)
    def test_property_emergency_cooldown_minimum_15_minutes(self, account_name, requested_minutes):
        """
        Property: Emergency cooldowns enforce minimum 15-minute duration.
        
        For any requested cooldown duration, the effective duration should be
        at least 15 minutes.
        """
        mock_cooldown = MagicMock()
        limiter = ConservativeRateLimiter(cooldown_manager=mock_cooldown)
        
        # Apply emergency cooldown
        limiter.emergency_cooldown(account_name, duration_minutes=requested_minutes)
        
        # Verify put_on_cooldown was called
        mock_cooldown.put_on_cooldown.assert_called_once()
        
        # Extract the actual minutes applied
        call_kwargs = mock_cooldown.put_on_cooldown.call_args
        applied_minutes = call_kwargs[1].get("minutes") or call_kwargs[0][1]
        
        # Verify minimum 15 minutes enforced
        assert applied_minutes >= 15, \
            f"Emergency cooldown should be at least 15 minutes, got {applied_minutes}"
        
        # If requested > 15, should use requested duration
        if requested_minutes > 15:
            assert applied_minutes == requested_minutes, \
                f"Should use requested {requested_minutes} minutes, got {applied_minutes}"


# ---------------------------------------------------------------------------
# Property 2.3: Smart Scheduling Applies 1.5x Multiplier During Risky Hours
# Validates: Requirement 3.3
# ---------------------------------------------------------------------------

class TestSmartSchedulingPreservation:
    """
    **Validates: Requirement 3.3**
    
    Property: For all operations during risky hours, delay multiplier is 1.5x
    """

    def test_property_risky_hours_apply_multiplier(self):
        """
        Property: get_delay_multiplier() returns 1.5x during risky hours.
        
        When smart scheduling is enabled and current hour is in RISKY_HOURS,
        the multiplier should be 1.5x.
        """
        from src.config import SMART_SCHEDULING_ENABLED, RISKY_HOURS
        
        if not SMART_SCHEDULING_ENABLED:
            pytest.skip("Smart scheduling is disabled")
        
        limiter = RateLimiter(label="test")
        
        # Mock time to be in risky hours
        with patch('time.localtime') as mock_time:
            # Use first risky hour
            risky_hour = RISKY_HOURS[0]
            mock_time.return_value = time.struct_time((2024, 1, 1, risky_hour, 0, 0, 0, 1, 0))
            
            multiplier = limiter.get_delay_multiplier()
            assert multiplier == RISKY_HOUR_DELAY_MULTIPLIER, \
                f"Risky hour multiplier should be {RISKY_HOUR_DELAY_MULTIPLIER}, got {multiplier}"

    def test_property_safe_hours_use_1x_multiplier(self):
        """
        Property: get_delay_multiplier() returns 1.0x during safe hours.
        
        When smart scheduling is enabled and current hour is in SAFE_HOURS,
        the multiplier should be 1.0x.
        """
        from src.config import SMART_SCHEDULING_ENABLED, SAFE_HOURS
        
        if not SMART_SCHEDULING_ENABLED:
            pytest.skip("Smart scheduling is disabled")
        
        limiter = RateLimiter(label="test")
        
        # Mock time to be in safe hours
        with patch('time.localtime') as mock_time:
            # Use first safe hour
            safe_hour = SAFE_HOURS[0]
            mock_time.return_value = time.struct_time((2024, 1, 1, safe_hour, 0, 0, 0, 1, 0))
            
            multiplier = limiter.get_delay_multiplier()
            assert multiplier == 1.0, \
                f"Safe hour multiplier should be 1.0, got {multiplier}"


# ---------------------------------------------------------------------------
# Property 2.4: Ctrl+C Interrupts Delays Immediately
# Validates: Requirement 3.4
# ---------------------------------------------------------------------------

class TestShutdownHandlingPreservation:
    """
    **Validates: Requirement 3.4**
    
    Property: For all shutdown requests, delays interrupt within 1 second
    """

    def test_property_shutdown_interrupts_sleep(self):
        """
        Property: _interruptible_sleep() respects shutdown event.
        
        When shutdown event is set, sleep should terminate immediately
        (within check_interval time).
        """
        # Clear shutdown event first
        _SHUTDOWN_EVENT.clear()
        
        start_time = time.time()
        
        # Start sleep in background thread
        def sleep_task():
            _interruptible_sleep(10.0, label="test", check_interval=0.1)
        
        thread = threading.Thread(target=sleep_task)
        thread.start()
        
        # Wait a bit then trigger shutdown
        time.sleep(0.2)
        _SHUTDOWN_EVENT.set()
        
        # Wait for thread to complete
        thread.join(timeout=2.0)
        
        elapsed = time.time() - start_time
        
        # Should complete much faster than 10 seconds
        assert elapsed < 1.0, \
            f"Sleep should interrupt within 1 second, took {elapsed}s"
        
        # Clear shutdown event for other tests
        _SHUTDOWN_EVENT.clear()

    def test_property_rate_limiter_sleep_respects_shutdown(self):
        """
        Property: RateLimiter._sleep() respects shutdown event.
        
        When shutdown event is set, RateLimiter sleep should terminate immediately.
        """
        _SHUTDOWN_EVENT.clear()
        
        limiter = RateLimiter(label="test")
        
        start_time = time.time()
        
        # Start sleep in background thread
        def sleep_task():
            limiter._sleep(10.0, message="test sleep")
        
        thread = threading.Thread(target=sleep_task)
        thread.start()
        
        # Wait a bit then trigger shutdown
        time.sleep(0.2)
        _SHUTDOWN_EVENT.set()
        
        # Wait for thread to complete
        thread.join(timeout=2.0)
        
        elapsed = time.time() - start_time
        
        # Should complete much faster than 10 seconds
        assert elapsed < 1.0, \
            f"RateLimiter sleep should interrupt within 1 second, took {elapsed}s"
        
        # Clear shutdown event for other tests
        _SHUTDOWN_EVENT.clear()


# ---------------------------------------------------------------------------
# Property 2.5: Session Statistics Track Operation Counts and Elapsed Time
# Validates: Requirement 3.5
# ---------------------------------------------------------------------------

class TestStatisticsTrackingPreservation:
    """
    **Validates: Requirement 3.5**
    
    Property: For all operations, statistics increment correctly
    """

    @given(
        num_operations=st.integers(min_value=1, max_value=20)
    )
    @settings(max_examples=5, deadline=None)
    def test_property_operation_counter_increments(self, num_operations):
        """
        Property: track_operation() increments operation counter.
        
        For any number of operations, the counter should increment correctly.
        """
        limiter = RateLimiter(label="test")
        
        # Mock sleep to avoid actual delays
        with patch.object(limiter, '_sleep'):
            initial_count = limiter._op_counter
            initial_total = limiter._total_ops
            
            # Track operations
            for _ in range(num_operations):
                limiter.track_operation()
            
            # Verify counters incremented (may reset _op_counter on long breaks)
            assert limiter._total_ops == initial_total + num_operations, \
                f"Total ops should increment by {num_operations}"

    def test_property_session_stats_format(self):
        """
        Property: _session_stats() returns formatted string with time and ops.
        
        The session stats should include elapsed time and operation count.
        """
        limiter = RateLimiter(label="test")
        
        # Mock sleep to avoid actual delays
        with patch.object(limiter, '_sleep'):
            # Track some operations
            for _ in range(5):
                limiter.track_operation()
            
            stats = limiter._session_stats()
            
            # Should contain "session", time info, and "ops"
            assert "session" in stats, "Stats should contain 'session'"
            assert "ops" in stats, "Stats should contain 'ops'"
            assert "5" in stats, "Stats should show 5 operations"


# ---------------------------------------------------------------------------
# Property 2.6: Human-Readable Messages Display Clear Delay Explanations
# Validates: Requirement 3.6
# ---------------------------------------------------------------------------

class TestMessageDisplayPreservation:
    """
    **Validates: Requirement 3.6**
    
    Property: For all delays, human-readable message is displayed
    """

    def test_property_short_delay_displays_message(self, capsys):
        """
        Property: short_delay() displays human-readable message.
        
        When short_delay() is called, a message should be printed.
        """
        limiter = RateLimiter(min_delay=0.1, max_delay=0.2, label="test")
        
        # Call short_delay with minimal time
        limiter.short_delay()
        
        # Capture output
        captured = capsys.readouterr()
        
        # Should have printed something
        assert len(captured.out) > 0, "short_delay should print a message"
        assert "test" in captured.out.lower() or "pause" in captured.out.lower() or "wait" in captured.out.lower(), \
            "Message should be human-readable"

    def test_property_user_delay_displays_message(self, capsys):
        """
        Property: user_delay() displays human-readable message.
        
        When user_delay() is called, a message should be printed.
        """
        limiter = RateLimiter(min_delay=0.1, max_delay=0.2, label="test")
        
        # Call user_delay with minimal time
        limiter.user_delay()
        
        # Capture output
        captured = capsys.readouterr()
        
        # Should have printed something
        assert len(captured.out) > 0, "user_delay should print a message"
        assert "test" in captured.out.lower() or "profile" in captured.out.lower() or "user" in captured.out.lower(), \
            "Message should be human-readable"


# ---------------------------------------------------------------------------
# Property 2.7: Countdown Timers Appear for Waits ≥30 Seconds
# Validates: Requirement 3.7
# ---------------------------------------------------------------------------

class TestCountdownTimerPreservation:
    """
    **Validates: Requirement 3.7**
    
    Property: For all waits ≥30s, countdown timer is shown
    """

    def test_property_long_wait_shows_countdown(self, capsys):
        """
        Property: _interruptible_sleep() shows countdown for waits ≥30s.
        
        When sleep duration is ≥30 seconds and show_countdown=True,
        a countdown message should be displayed.
        """
        # Use very short actual sleep but test the message logic
        with patch('time.time') as mock_time:
            # Mock time to simulate countdown without waiting
            mock_time.side_effect = [0, 0.1, 0.2, 0.3, 0.4, 0.5, 35.0]  # Jump to end
            
            _interruptible_sleep(30.0, label="test", show_countdown=True, check_interval=0.1)
        
        # Capture output
        captured = capsys.readouterr()
        
        # Should show countdown indicator
        assert "⏳" in captured.out or "waiting" in captured.out.lower() or "resuming" in captured.out.lower(), \
            "Long waits should show countdown timer"

    def test_property_short_wait_no_countdown(self, capsys):
        """
        Property: _interruptible_sleep() does not show countdown for waits <30s.
        
        When sleep duration is <30 seconds, countdown should not be shown
        (unless explicitly requested).
        """
        _interruptible_sleep(0.1, label="test", show_countdown=False, check_interval=0.05)
        
        # Capture output
        captured = capsys.readouterr()
        
        # Should not show "resuming at" message (specific to long countdowns)
        assert "resuming at" not in captured.out.lower(), \
            "Short waits should not show 'resuming at' countdown"


# ---------------------------------------------------------------------------
# Property 2.8: Account Availability Checking Filters Out Cooldown Accounts
# Validates: Requirement 3.8
# ---------------------------------------------------------------------------

class TestAccountAvailabilityPreservation:
    """
    **Validates: Requirement 3.8**
    
    Property: For all account selections, cooldown accounts are excluded
    """

    @given(
        account_names=st.lists(
            st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"))),
            min_size=2,
            max_size=10,
            unique=True
        )
    )
    @settings(max_examples=5, deadline=None)
    def test_property_get_available_accounts_filters_cooldown(self, account_names):
        """
        Property: get_available_accounts() excludes accounts on cooldown.
        
        For any list of accounts, get_available_accounts should return only
        those not on cooldown.
        """
        manager = AccountCooldownManager()
        
        # Clear any existing cooldowns first
        for account in account_names:
            manager.clear_cooldown(account)
        
        # Put half the accounts on cooldown
        cooldown_accounts = account_names[::2]
        available_accounts = account_names[1::2]
        
        for account in cooldown_accounts:
            manager.put_on_cooldown(account, minutes=15)
        
        # Get available accounts
        result = manager.get_available_accounts(account_names)
        
        # Verify only non-cooldown accounts are returned
        for account in cooldown_accounts:
            assert account not in result, \
                f"Cooldown account {account} should not be available"
        
        for account in available_accounts:
            assert account in result, \
                f"Available account {account} should be in result"

    @given(
        account_name=st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd")))
    )
    @settings(max_examples=5, deadline=None)
    def test_property_check_account_available(self, account_name):
        """
        Property: check_account_available() returns correct availability status.
        
        For any account, check_account_available should return True if not
        on cooldown, False otherwise.
        """
        limiter = ConservativeRateLimiter()
        
        # Clear cooldown first
        limiter._cooldown_manager.clear_cooldown(account_name)
        
        # Should be available
        assert limiter.check_account_available(account_name), \
            f"Account {account_name} should be available when not on cooldown"
        
        # Put on cooldown
        limiter._cooldown_manager.put_on_cooldown(account_name, minutes=15)
        
        # Should not be available
        assert not limiter.check_account_available(account_name), \
            f"Account {account_name} should not be available when on cooldown"
        
        # Clean up
        limiter._cooldown_manager.clear_cooldown(account_name)


# ---------------------------------------------------------------------------
# Property 2.9: Daily Quotas Enforce Limits on Profile Views Per Account
# Validates: Requirement 3.9
# ---------------------------------------------------------------------------

class TestDailyQuotaPreservation:
    """
    **Validates: Requirement 3.9**
    
    Property: For all operations, daily quota is checked and enforced
    """

    @given(
        profile_views=st.integers(min_value=0, max_value=300)
    )
    @settings(max_examples=5, deadline=None)
    def test_property_profile_view_quota_enforced(self, profile_views):
        """
        Property: Profile view quota limits are enforced correctly.
        
        For any account, can_view_profiles should return False when quota
        is exceeded (>= DAILY_QUOTA_PROFILE_VIEWS).
        """
        import uuid
        account_name = f"test_pv_{uuid.uuid4().hex[:8]}"
        
        manager = AccountQuotaManager()
        
        # Record profile views
        manager.record_profile_view(account_name, count=profile_views)
        
        # Check if can view profiles
        can_view = manager.can_view_profiles(account_name)
        
        # Verify quota enforcement
        if DAILY_QUOTA_PROFILE_VIEWS > 0:
            if profile_views < DAILY_QUOTA_PROFILE_VIEWS:
                assert can_view, \
                    f"Should be able to view profiles with {profile_views}/{DAILY_QUOTA_PROFILE_VIEWS} views"
            else:
                assert not can_view, \
                    f"Should not be able to view profiles with {profile_views}/{DAILY_QUOTA_PROFILE_VIEWS} views"
        else:
            # Unlimited quota
            assert can_view, "Should always be able to view profiles when quota is unlimited"

    @given(
        actions=st.integers(min_value=0, max_value=8000)
    )
    @settings(max_examples=5, deadline=None)
    def test_property_action_quota_enforced(self, actions):
        """
        Property: Action quota limits are enforced correctly.
        
        For any account, can_perform_action should return False when quota
        is exceeded (>= DAILY_QUOTA_ACTIONS).
        """
        import uuid
        account_name = f"test_act_{uuid.uuid4().hex[:8]}"
        
        manager = AccountQuotaManager()
        
        # Record actions
        manager.record_action(account_name, count=actions)
        
        # Check if can perform action
        can_act = manager.can_perform_action(account_name)
        
        # Verify quota enforcement
        if DAILY_QUOTA_ACTIONS > 0:
            if actions < DAILY_QUOTA_ACTIONS:
                assert can_act, \
                    f"Should be able to perform actions with {actions}/{DAILY_QUOTA_ACTIONS} actions"
            else:
                assert not can_act, \
                    f"Should not be able to perform actions with {actions}/{DAILY_QUOTA_ACTIONS} actions"
        else:
            # Unlimited quota
            assert can_act, "Should always be able to perform actions when quota is unlimited"


# ---------------------------------------------------------------------------
# Property 2.10: Operation Multipliers Use Correct Values
# Validates: Requirement 3.10
# ---------------------------------------------------------------------------

class TestOperationMultipliersPreservation:
    """
    **Validates: Requirement 3.10**
    
    Property: For all operation types, correct multiplier is applied (1.0x/1.5x/2.0x)
    """

    def test_property_public_operations_use_1x_multiplier(self):
        """
        Property: PUBLIC operations use 1.0x delay multiplier.
        
        **Validates: Requirements 3.10**
        """
        assert _DELAY_MULTIPLIERS[OperationType.PUBLIC] == 1.0, \
            "PUBLIC operations should use 1.0x multiplier"

    def test_property_following_required_operations_use_1_5x_multiplier(self):
        """
        Property: FOLLOWING_REQUIRED operations use 1.5x delay multiplier.
        
        **Validates: Requirements 3.10**
        """
        assert _DELAY_MULTIPLIERS[OperationType.FOLLOWING_REQUIRED] == 1.5, \
            "FOLLOWING_REQUIRED operations should use 1.5x multiplier"

    def test_property_mutual_following_operations_use_2x_multiplier(self):
        """
        Property: MUTUAL_FOLLOWING operations use 2.0x delay multiplier.
        
        **Validates: Requirements 3.10**
        """
        assert _DELAY_MULTIPLIERS[OperationType.MUTUAL_FOLLOWING] == 2.0, \
            "MUTUAL_FOLLOWING operations should use 2.0x multiplier"

    @given(
        operation_type=st.sampled_from(list(OperationType))
    )
    @settings(max_examples=5, deadline=None)
    def test_property_all_operation_types_have_multipliers(self, operation_type):
        """
        Property: All operation types have defined delay multipliers.
        
        For any operation type, there should be a corresponding multiplier.
        
        **Validates: Requirements 3.10**
        """
        assert operation_type in _DELAY_MULTIPLIERS, \
            f"Operation type {operation_type} should have a delay multiplier"
        
        multiplier = _DELAY_MULTIPLIERS[operation_type]
        assert multiplier > 0, \
            f"Multiplier for {operation_type} should be positive, got {multiplier}"

    @given(
        operation_type=st.sampled_from(list(OperationType))
    )
    @settings(max_examples=5, deadline=None)
    def test_property_operation_delay_accepts_all_types(self, operation_type):
        """
        Property: operation_delay() accepts all OperationType values.
        
        For any operation type, operation_delay() should execute without error.
        
        **Validates: Requirements 3.10**
        """
        mock_cooldown = MagicMock()
        limiter = ConservativeRateLimiter(
            min_delay=0.0,
            max_delay=0.0,
            cooldown_manager=mock_cooldown
        )
        
        with patch.object(limiter, "_sleep"):
            # Should not raise any exception
            limiter.operation_delay(operation_type)
