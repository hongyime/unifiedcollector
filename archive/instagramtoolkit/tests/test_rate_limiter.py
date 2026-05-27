"""Tests for src/rate_limiter.py — delay logic with _interruptible_sleep mocked.

All sleep/time mocks go through _interruptible_sleep to avoid infinite loops
that crash the IDE.
"""
import random
from unittest.mock import patch, call, MagicMock

import pytest

from src.rate_limiter import RateLimiter, _interruptible_sleep


class TestRateLimiterInit:
    """Construction and initial state."""

    def test_default_values(self):
        rl = RateLimiter()
        assert rl._op_counter == 0
        assert rl.label == "general"

    def test_custom_delays(self):
        rl = RateLimiter(min_delay=10, max_delay=20, label="test")
        assert rl.min_delay == 10
        assert rl.max_delay == 20
        assert rl.label == "test"


class TestHumanDelay:
    """Tests for _human_delay (gaussian with clamp)."""

    def test_stays_within_bounds(self):
        rl = RateLimiter(min_delay=2, max_delay=5)
        random.seed(0)
        for _ in range(100):
            mean = 3.5
            d = rl._human_delay(mean)
            assert mean / 3 <= d <= mean * 3


class TestShortDelay:
    """Tests for short_delay — verifies _interruptible_sleep is called."""

    @patch("src.rate_limiter._interruptible_sleep")
    def test_calls_sleep_with_positive_value(self, mock_sleep):
        rl = RateLimiter(min_delay=1, max_delay=2)
        rl.short_delay()
        mock_sleep.assert_called_once()
        slept = mock_sleep.call_args[0][0]
        assert slept > 0


class TestUserDelay:
    """Tests for user_delay with multiplier."""

    @patch("src.rate_limiter._interruptible_sleep")
    def test_multiplier_scales_delay(self, mock_sleep):
        rl = RateLimiter(min_delay=2, max_delay=4)
        rl.user_delay(multiplier=3.0)
        mock_sleep.assert_called_once()
        slept = mock_sleep.call_args[0][0]
        assert slept > 0


class TestPeriodic:
    """Tests for periodic pauses."""

    @patch("src.rate_limiter._interruptible_sleep")
    def test_does_not_pause_at_zero(self, mock_sleep):
        rl = RateLimiter()
        rl.periodic(current_index=0, every=10, seconds=30)
        mock_sleep.assert_not_called()

    @patch("src.rate_limiter._interruptible_sleep")
    def test_pauses_at_multiple(self, mock_sleep):
        rl = RateLimiter()
        # Set _next_enum_pause to 10 so index 10 triggers a pause
        rl._next_enum_pause = 10
        rl.periodic(current_index=10, every=10, seconds=30)
        mock_sleep.assert_called_once()

    @patch("src.rate_limiter._interruptible_sleep")
    def test_no_pause_between_multiples(self, mock_sleep):
        rl = RateLimiter()
        rl.periodic(current_index=7, every=10, seconds=30)
        mock_sleep.assert_not_called()


class TestEmergencyBreak:
    """Tests for emergency_break — verifies _interruptible_sleep called."""

    @patch("src.rate_limiter._interruptible_sleep")
    def test_sleeps_correct_seconds(self, mock_sleep):
        rl = RateLimiter()
        rl.emergency_break(minutes=5)
        mock_sleep.assert_called_once()
        slept = mock_sleep.call_args[0][0]
        assert slept == 5 * 60  # 5 minutes in seconds


class TestTrackOperation:
    """Tests for automatic long-break logic."""

    @patch("src.rate_limiter._interruptible_sleep")
    @patch("src.rate_limiter.random.random", return_value=1.0)  # Disable micro-pause
    @patch("src.rate_limiter.random.randint", return_value=3)
    def test_resets_counter_after_break(self, mock_randint, mock_random, mock_sleep):
        rl = RateLimiter()
        rl._ops_before_long_pause = 3
        rl._long_pause_minutes = 1

        rl.track_operation()  # 1
        rl.track_operation()  # 2
        mock_sleep.assert_not_called()

        rl.track_operation()  # 3 → triggers break
        assert mock_sleep.call_count == 1
        assert rl._op_counter == 0  # reset after break

    @patch("src.rate_limiter._interruptible_sleep")
    @patch("src.rate_limiter.random.random", return_value=1.0)  # Disable micro-pause
    @patch("src.rate_limiter.random.randint", return_value=100)
    def test_no_break_below_threshold(self, mock_randint, mock_random, mock_sleep):
        rl = RateLimiter()
        rl._ops_before_long_pause = 100
        for _ in range(50):
            rl.track_operation()
        mock_sleep.assert_not_called()
