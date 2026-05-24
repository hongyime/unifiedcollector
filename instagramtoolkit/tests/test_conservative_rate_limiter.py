"""
Unit tests for ConservativeRateLimiter.

Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8
"""

import time
from unittest.mock import MagicMock, patch, call

import pytest

from src.operation_classifier import OperationType
from src.conservative_rate_limiter import ConservativeRateLimiter, _DELAY_MULTIPLIERS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_limiter(min_delay=0.0, max_delay=0.0):
    """Create a limiter with zero delays and a mock cooldown manager."""
    mock_cooldown = MagicMock()
    mock_cooldown.is_on_cooldown.return_value = False
    mock_cooldown.get_cooldown_remaining.return_value = 0.0
    mock_cooldown.get_available_accounts.side_effect = lambda names: names
    limiter = ConservativeRateLimiter(
        min_delay=min_delay,
        max_delay=max_delay,
        cooldown_manager=mock_cooldown,
    )
    return limiter, mock_cooldown


# ---------------------------------------------------------------------------
# Operation-specific delays (Requirements 4.1–4.4)
# ---------------------------------------------------------------------------

class TestOperationSpecificDelays:
    """Requirements 4.1–4.4: operation_delay() applies correct multipliers."""

    def test_operation_delay_calls_sleep(self):
        """operation_delay() triggers a sleep call."""
        limiter, _ = _make_limiter(min_delay=0.001, max_delay=0.001)
        with patch.object(limiter, "_sleep") as mock_sleep:
            limiter.operation_delay(OperationType.PUBLIC)
            mock_sleep.assert_called_once()

    def test_public_delay_uses_1x_multiplier(self):
        """PUBLIC operations use 1.0x multiplier."""
        assert _DELAY_MULTIPLIERS[OperationType.PUBLIC] == 1.0

    def test_following_required_delay_uses_1_5x_multiplier(self):
        """FOLLOWING_REQUIRED operations use 1.5x multiplier."""
        assert _DELAY_MULTIPLIERS[OperationType.FOLLOWING_REQUIRED] == 1.5

    def test_mutual_following_delay_uses_2x_multiplier(self):
        """MUTUAL_FOLLOWING operations use 2.0x multiplier."""
        assert _DELAY_MULTIPLIERS[OperationType.MUTUAL_FOLLOWING] == 2.0

    def test_all_operation_types_accepted(self):
        """operation_delay() accepts all OperationType values without error."""
        limiter, _ = _make_limiter(min_delay=0.0, max_delay=0.0)
        with patch.object(limiter, "_sleep"):
            for op_type in OperationType:
                limiter.operation_delay(op_type)  # should not raise


# ---------------------------------------------------------------------------
# Account switch delay (Requirement 4.5)
# ---------------------------------------------------------------------------

class TestAccountSwitchDelay:
    """Requirement 4.5: account_switch_delay() enforces mandatory delay."""

    def test_account_switch_delay_calls_sleep(self):
        """account_switch_delay() triggers a sleep call."""
        limiter, _ = _make_limiter()
        with patch.object(limiter, "_sleep") as mock_sleep:
            limiter.account_switch_delay()
            mock_sleep.assert_called_once()

    def test_account_switch_delay_positive_duration(self):
        """account_switch_delay() sleeps for a positive duration."""
        limiter, _ = _make_limiter(min_delay=0.001, max_delay=0.001)
        slept = []
        with patch.object(limiter, "_sleep", side_effect=lambda s, **kw: slept.append(s)):
            limiter.account_switch_delay()
        assert slept[0] > 0


# ---------------------------------------------------------------------------
# Emergency cooldown (Requirement 4.6)
# ---------------------------------------------------------------------------

class TestEmergencyCooldown:
    """Requirement 4.6: emergency_cooldown() applies ≥15 minute cooldown."""

    def test_emergency_cooldown_calls_put_on_cooldown(self):
        """emergency_cooldown() delegates to AccountCooldownManager."""
        limiter, mock_cooldown = _make_limiter()
        limiter.emergency_cooldown("account1")
        mock_cooldown.put_on_cooldown.assert_called_once()

    def test_emergency_cooldown_minimum_15_minutes(self):
        """emergency_cooldown() enforces minimum 15 minutes."""
        limiter, mock_cooldown = _make_limiter()
        limiter.emergency_cooldown("account1", duration_minutes=5)
        call_kwargs = mock_cooldown.put_on_cooldown.call_args
        applied = call_kwargs[1].get("minutes") or call_kwargs[0][1]
        assert applied >= 15

    def test_emergency_cooldown_respects_longer_duration(self):
        """emergency_cooldown() uses requested duration when > 15 minutes."""
        limiter, mock_cooldown = _make_limiter()
        limiter.emergency_cooldown("account1", duration_minutes=30)
        call_kwargs = mock_cooldown.put_on_cooldown.call_args
        applied = call_kwargs[1].get("minutes") or call_kwargs[0][1]
        assert applied == 30

    def test_emergency_cooldown_passes_account_name(self):
        """emergency_cooldown() passes the correct account name."""
        limiter, mock_cooldown = _make_limiter()
        limiter.emergency_cooldown("my_account", duration_minutes=20)
        call_args = mock_cooldown.put_on_cooldown.call_args[0]
        assert call_args[0] == "my_account"


# ---------------------------------------------------------------------------
# Account availability checking (Requirement 4.7)
# ---------------------------------------------------------------------------

class TestAccountAvailabilityChecking:
    """Requirement 4.7: check_account_available() respects cooldown state."""

    def test_available_account_returns_true(self):
        """Account not in cooldown returns True."""
        limiter, mock_cooldown = _make_limiter()
        mock_cooldown.is_on_cooldown.return_value = False
        assert limiter.check_account_available("account1") is True

    def test_cooldown_account_returns_false(self):
        """Account in cooldown returns False."""
        limiter, mock_cooldown = _make_limiter()
        mock_cooldown.is_on_cooldown.return_value = True
        assert limiter.check_account_available("account1") is False

    def test_get_available_accounts_filters_cooldown(self):
        """get_available_accounts() excludes accounts in cooldown."""
        limiter, mock_cooldown = _make_limiter()
        # Override the side_effect set in _make_limiter so return_value takes effect
        mock_cooldown.get_available_accounts.side_effect = None
        mock_cooldown.get_available_accounts.return_value = ["account2"]
        result = limiter.get_available_accounts(["account1", "account2"])
        assert result == ["account2"]

    def test_get_cooldown_remaining_delegates_to_manager(self):
        """get_cooldown_remaining() returns value from cooldown manager."""
        limiter, mock_cooldown = _make_limiter()
        mock_cooldown.get_cooldown_remaining.return_value = 300.0
        assert limiter.get_cooldown_remaining("account1") == 300.0


# ---------------------------------------------------------------------------
# Progressive delays during enumeration (Requirement 4.8)
# ---------------------------------------------------------------------------

class TestFollowingEnumerationDelay:
    """Requirement 4.8: following_enumeration_delay() triggers every N operations."""

    def test_no_delay_at_zero(self):
        """No delay at count=0."""
        limiter, _ = _make_limiter()
        with patch.object(limiter, "_sleep") as mock_sleep:
            limiter.following_enumeration_delay(0)
            mock_sleep.assert_not_called()

    def test_delay_triggers_at_enum_pause_every(self):
        """Delay triggers at multiples of ENUM_PAUSE_EVERY."""
        from config import ENUM_PAUSE_EVERY
        limiter, _ = _make_limiter()
        with patch.object(limiter, "_sleep") as mock_sleep:
            limiter.following_enumeration_delay(ENUM_PAUSE_EVERY)
            mock_sleep.assert_called_once()

    def test_no_delay_between_multiples(self):
        """No delay at non-multiples of ENUM_PAUSE_EVERY."""
        from config import ENUM_PAUSE_EVERY
        limiter, _ = _make_limiter()
        with patch.object(limiter, "_sleep") as mock_sleep:
            limiter.following_enumeration_delay(ENUM_PAUSE_EVERY - 1)
            mock_sleep.assert_not_called()

    def test_delay_triggers_at_second_multiple(self):
        """Delay triggers at 2x ENUM_PAUSE_EVERY."""
        from config import ENUM_PAUSE_EVERY
        limiter, _ = _make_limiter()
        with patch.object(limiter, "_sleep") as mock_sleep:
            limiter.following_enumeration_delay(ENUM_PAUSE_EVERY * 2)
            mock_sleep.assert_called_once()

    def test_progressive_delay_increases_with_count(self):
        """Delay duration increases as count grows (progressive)."""
        from config import ENUM_PAUSE_EVERY
        limiter, _ = _make_limiter()
        delays = []
        with patch.object(limiter, "_sleep", side_effect=lambda s, **kw: delays.append(s)):
            # Patch jitter to be deterministic
            with patch.object(limiter, "_jitter", side_effect=lambda x: x):
                limiter.following_enumeration_delay(ENUM_PAUSE_EVERY)
                limiter.following_enumeration_delay(ENUM_PAUSE_EVERY * 2)

        assert len(delays) == 2
        assert delays[1] > delays[0]
