"""
Bug Condition Exploration Test - Instagram Rate Limit Ban Fix

This test confirms the bug exists on UNFIXED code by demonstrating that
current delay settings (MIN_DELAY=3, MAX_DELAY=8) produce request rates
exceeding Instagram's safe limit of 200 requests/hour.

**CRITICAL**: This test MUST FAIL on unfixed code - failure confirms the bug exists.

**KEY INSIGHT**: The bug is that base delays (3-8s) are too short, allowing
450-1200 req/hr. Rest periods exist but are:
1. Not consistently applied across all code paths
2. Too infrequent (5-15 ops) and too short (3-8 min) to prevent bans
3. The base rate without rest periods is the core issue

This test focuses on the BASE REQUEST RATE to confirm the bug condition.

Property 1: Bug Condition - Request Rate Exceeds Safe Limit

Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5
"""

import time
from unittest.mock import patch, MagicMock

import pytest
from hypothesis import given, settings, strategies as st, assume

from src.rate_limiter import RateLimiter
from src.config import (
    MIN_DELAY,
    MAX_DELAY,
    OPS_BEFORE_BREAK_MIN,
    OPS_BEFORE_BREAK_MAX,
    BREAK_DURATION_MIN,
    BREAK_DURATION_MAX,
)


class TestBugConditionRateLimit:
    """
    Bug Condition Exploration: Confirm current delays produce excessive request rates.
    
    This test simulates request sequences with current delay settings and measures
    the actual request rate. On UNFIXED code (MIN_DELAY=3, MAX_DELAY=8), this test
    should FAIL because request rates exceed 200 req/hr.
    
    On FIXED code (MIN_DELAY=20, MAX_DELAY=40), this test should PASS because
    request rates stay below 180 req/hr.
    """

    def _simulate_base_request_rate(
        self,
        num_requests: int,
        min_delay: float,
        max_delay: float,
    ) -> tuple[float, float]:
        """
        Simulate a sequence of requests with ONLY base delays (no rest periods).
        
        This measures the BASE REQUEST RATE, which is the core bug condition.
        Rest periods are not consistently applied across all code paths, and
        even when they are, the base rate is too high.
        
        Returns:
            (total_time_seconds, requests_per_hour)
        """
        total_time = 0.0
        
        import random
        
        for i in range(num_requests):
            # Simulate the delay between requests (short_delay or user_delay)
            mean_delay = (min_delay + max_delay) / 2
            # RateLimiter uses gaussian distribution with stddev = mean * 0.3
            stddev = mean_delay * 0.3
            delay = random.gauss(mean_delay, stddev)
            # Clamped to [mean/3, mean*3]
            delay = max(mean_delay / 3, min(delay, mean_delay * 3))
            total_time += delay
        
        # Calculate requests per hour
        if total_time > 0:
            requests_per_hour = (num_requests / total_time) * 3600
        else:
            requests_per_hour = float('inf')
        
        return total_time, requests_per_hour

    def test_minimum_delay_produces_excessive_rate(self):
        """
        **Property 1: Bug Condition** - Request Rate Exceeds Safe Limit
        
        Test with minimum delay (3s) → expect ~1200 req/hr on unfixed code.
        This should FAIL on unfixed code (confirms bug exists).
        
        Validates: Requirements 1.1, 1.2
        """
        # Simulate 100 requests with minimum delay (3s)
        # Expected: ~1200 req/hr (3600s / 3s = 1200 requests)
        # This exceeds 200 req/hr limit significantly
        
        import random
        random.seed(42)  # Fixed seed for reproducibility
        
        # Use current config values (unfixed: MIN_DELAY=3, MAX_DELAY=8)
        total_time, req_per_hour = self._simulate_base_request_rate(
            num_requests=100,
            min_delay=MIN_DELAY,
            max_delay=MIN_DELAY,  # Use minimum delay only
        )
        
        # On UNFIXED code: MIN_DELAY=3 → ~1200 req/hr → FAILS (req_per_hour > 200)
        # On FIXED code: MIN_DELAY=20 → ~180 req/hr → PASSES (req_per_hour <= 200)
        assert req_per_hour <= 200, (
            f"Bug confirmed: Base request rate {req_per_hour:.1f} req/hr exceeds safe limit of 200 req/hr "
            f"with MIN_DELAY={MIN_DELAY}s (total_time={total_time:.1f}s for 100 requests). "
            f"This demonstrates the core bug: base delays are too short."
        )

    def test_maximum_delay_produces_excessive_rate(self):
        """
        **Property 1: Bug Condition** - Request Rate Exceeds Safe Limit
        
        Test with maximum delay (8s) → expect ~450 req/hr on unfixed code.
        This should FAIL on unfixed code (confirms bug exists).
        
        Validates: Requirements 1.1, 1.2
        """
        import random
        random.seed(43)
        
        # Use current config values (unfixed: MAX_DELAY=8)
        total_time, req_per_hour = self._simulate_base_request_rate(
            num_requests=100,
            min_delay=MAX_DELAY,
            max_delay=MAX_DELAY,  # Use maximum delay only
        )
        
        # On UNFIXED code: MAX_DELAY=8 → ~450 req/hr → FAILS (req_per_hour > 200)
        # On FIXED code: MAX_DELAY=40 → ~90 req/hr → PASSES (req_per_hour <= 200)
        assert req_per_hour <= 200, (
            f"Bug confirmed: Base request rate {req_per_hour:.1f} req/hr exceeds safe limit of 200 req/hr "
            f"with MAX_DELAY={MAX_DELAY}s (total_time={total_time:.1f}s for 100 requests). "
            f"Even with maximum delays, the rate is too high."
        )

    def test_average_delay_produces_excessive_rate(self):
        """
        **Property 1: Bug Condition** - Request Rate Exceeds Safe Limit
        
        Test with average delay (5.5s) → expect ~654 req/hr on unfixed code.
        This should FAIL on unfixed code (confirms bug exists).
        
        Validates: Requirements 1.1, 1.2, 1.3
        """
        import random
        random.seed(44)
        
        # Use current config values (unfixed: MIN_DELAY=3, MAX_DELAY=8)
        total_time, req_per_hour = self._simulate_base_request_rate(
            num_requests=100,
            min_delay=MIN_DELAY,
            max_delay=MAX_DELAY,
        )
        
        # On UNFIXED code: avg delay 5.5s → ~654 req/hr → FAILS (req_per_hour > 200)
        # On FIXED code: avg delay 30s → ~120 req/hr → PASSES (req_per_hour <= 200)
        assert req_per_hour <= 200, (
            f"Bug confirmed: Base request rate {req_per_hour:.1f} req/hr exceeds safe limit of 200 req/hr "
            f"with MIN_DELAY={MIN_DELAY}s, MAX_DELAY={MAX_DELAY}s (avg={(MIN_DELAY+MAX_DELAY)/2}s). "
            f"(total_time={total_time:.1f}s for 100 requests). "
            f"This is the primary bug condition."
        )

    @given(
        num_requests=st.integers(min_value=50, max_value=300),
        seed=st.integers(min_value=1, max_value=1000),
    )
    @settings(max_examples=20, deadline=None)
    def test_property_base_request_rate_exceeds_safe_limit(self, num_requests, seed):
        """
        **Property 1: Bug Condition** - Request Rate Exceeds Safe Limit (Property-Based)
        
        For any sequence of requests with current delay settings, the BASE request rate
        should stay below 200 req/hr. This property should FAIL on unfixed code,
        surfacing counterexamples that demonstrate the bug.
        
        Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5
        """
        import random
        random.seed(seed)
        
        total_time, req_per_hour = self._simulate_base_request_rate(
            num_requests=num_requests,
            min_delay=MIN_DELAY,
            max_delay=MAX_DELAY,
        )
        
        # This assertion should FAIL on unfixed code, providing counterexamples
        # On FIXED code, this should PASS consistently
        assert req_per_hour <= 200, (
            f"Counterexample found: {num_requests} requests with seed={seed} "
            f"produced {req_per_hour:.1f} req/hr (exceeds 200 req/hr limit). "
            f"Config: MIN_DELAY={MIN_DELAY}s, MAX_DELAY={MAX_DELAY}s (avg={(MIN_DELAY+MAX_DELAY)/2}s). "
            f"Total time: {total_time:.1f}s. "
            f"This demonstrates the core bug: base delays are too short, allowing excessive request rates."
        )

    def test_base_rate_calculation_accuracy(self):
        """
        Verify that our base rate calculation is accurate by testing known scenarios.
        
        This is a sanity check to ensure the simulation logic is correct.
        """
        import random
        random.seed(100)
        
        # Test 1: If we make 1 request per second, we should get ~3600 req/hr
        total_time, req_per_hour = self._simulate_base_request_rate(
            num_requests=10,
            min_delay=1.0,
            max_delay=1.0,
        )
        # Should be close to 3600 req/hr (allowing for gaussian jitter)
        assert 2000 <= req_per_hour <= 5000, f"Expected ~3600 req/hr, got {req_per_hour:.1f}"
        
        # Test 2: If we make 1 request per 10 seconds, we should get ~360 req/hr
        random.seed(101)
        total_time, req_per_hour = self._simulate_base_request_rate(
            num_requests=10,
            min_delay=10.0,
            max_delay=10.0,
        )
        # Should be close to 360 req/hr
        assert 200 <= req_per_hour <= 500, f"Expected ~360 req/hr, got {req_per_hour:.1f}"
        
        # Test 3: Current unfixed config should produce ~654 req/hr
        random.seed(102)
        total_time, req_per_hour = self._simulate_base_request_rate(
            num_requests=100,
            min_delay=3.0,
            max_delay=8.0,
        )
        # Average delay 5.5s → 3600/5.5 = 654 req/hr (allowing for jitter)
        assert 500 <= req_per_hour <= 800, f"Expected ~654 req/hr with 3-8s delays, got {req_per_hour:.1f}"
