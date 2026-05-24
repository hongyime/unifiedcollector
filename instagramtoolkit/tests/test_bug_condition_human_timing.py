"""
Bug Condition Exploration Test - More Human-Like Rate Limiting

Property 1: Bug Condition - Predictable Timing Patterns Lead to Rate Limiting

This test was written on UNFIXED code (Task 1) to confirm the bug existed.
After the fix (Tasks 3.1–3.7), re-running this same test confirms the fix works:
all assertions that previously FAILED should now PASS.

**Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.7, 1.8 / 2.1, 2.2, 2.3, 2.4, 2.7, 2.8**
"""

from __future__ import annotations

import math
import random
from typing import List
from unittest.mock import patch

import pytest
from hypothesis import given, settings, strategies as st

from src.config import (
    MICRO_PAUSE_PROBABILITY,
    ENUM_PAUSE_INTERVAL_MIN,
    ENUM_PAUSE_INTERVAL_MAX,
    ENUM_PAUSE_DURATION_MIN,
    ENUM_PAUSE_DURATION_MAX,
    DISTRIBUTION_GAUSSIAN_WEIGHT,
    DISTRIBUTION_UNIFORM_WEIGHT,
    DISTRIBUTION_EXPONENTIAL_WEIGHT,
    CONTENT_AWARE_ENABLED,
    CONTENT_AWARE_MAX_MULTIPLIER,
    MIN_DELAY,
    MAX_DELAY,
)
from src.rate_limiter import RateLimiter, _OPERATION_STRATEGIES


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_limiter() -> RateLimiter:
    return RateLimiter(min_delay=MIN_DELAY, max_delay=MAX_DELAY)


def _generate_delays_via_limiter(count: int, seed: int = 42) -> List[float]:
    """Generate delays by calling the real RateLimiter._human_delay() with
    variable ranges and mixed distributions — the fixed implementation."""
    random.seed(seed)
    limiter = _make_limiter()
    delays = []
    for _ in range(count):
        min_v, max_v = limiter._variable_delay_range()
        mean = (min_v + max_v) / 2
        dist = limiter._choose_distribution()
        delays.append(limiter._human_delay(mean, distribution=dist))
    return delays


def _calculate_entropy(delays: List[float], num_bins: int = 10) -> float:
    """Shannon entropy of the delay distribution (bits)."""
    if not delays:
        return 0.0
    min_val, max_val = min(delays), max(delays)
    if max_val == min_val:
        return 0.0
    bin_width = (max_val - min_val) / num_bins
    bins = [0] * num_bins
    for d in delays:
        idx = min(int((d - min_val) / bin_width), num_bins - 1)
        bins[idx] += 1
    total = len(delays)
    entropy = 0.0
    for c in bins:
        if c > 0:
            p = c / total
            entropy -= p * math.log2(p)
    return entropy


def _detect_regular_intervals(pause_indices: List[int]) -> bool:
    """Return True if all gaps between pauses are identical (machine-like)."""
    if len(pause_indices) < 2:
        return False
    intervals = [pause_indices[i + 1] - pause_indices[i]
                 for i in range(len(pause_indices) - 1)]
    return len(set(intervals)) == 1


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBugConditionHumanTiming:
    """
    After the fix these tests PASS, confirming human-like timing patterns.
    Before the fix they FAILED, confirming the bug existed.
    """

    # ── 1. High entropy (variable delay ranges) ──────────────────────────

    def test_fixed_delay_range_pattern(self):
        """
        Timing patterns must show high relative variability (CV ≥ 0.3).
        Fixed code: _variable_delay_range() + mixed distributions → wide spread.

        CV = std_dev / mean. A pure Gaussian with fixed range gives CV ≈ 0.15.
        Mixed distributions with variable ranges should give CV ≥ 0.3.

        Validates: Requirements 1.1 / 2.1
        """
        delays = _generate_delays_via_limiter(count=200, seed=42)
        mean = sum(delays) / len(delays)
        variance = sum((d - mean) ** 2 for d in delays) / len(delays)
        std_dev = variance ** 0.5
        cv = std_dev / mean if mean > 0 else 0.0

        assert cv >= 0.3, (
            f"Low coefficient of variation {cv:.3f} (expected ≥0.3). "
            f"Mean={mean:.1f}s, StdDev={std_dev:.1f}s. "
            f"Delay range: [{min(delays):.1f}, {max(delays):.1f}]."
        )

    # ── 2. Variable enumeration intervals ────────────────────────────────

    def test_regular_enumeration_interval_pattern(self):
        """
        Enumeration pauses must occur at irregular intervals (10–15 items).
        Fixed code: _next_enum_pause is re-randomized after each pause.

        Validates: Requirements 1.2 / 2.2
        """
        limiter = _make_limiter()
        pause_indices: List[int] = []

        # Simulate 200 items; record indices where a pause fires.
        # A pause fires when current_index % _next_enum_pause == 0.
        with patch.object(limiter, "_sleep"):
            for i in range(1, 201):
                prev_interval = limiter._next_enum_pause
                limiter.periodic(i)
                # If the interval changed, a pause just fired at index i
                if limiter._next_enum_pause != prev_interval:
                    pause_indices.append(i)

        # Need at least 3 pauses to measure regularity
        assert len(pause_indices) >= 3, (
            f"Too few pauses detected ({len(pause_indices)}) over 200 items. "
            f"Cannot assess regularity."
        )

        has_regular = _detect_regular_intervals(pause_indices)

        assert not has_regular, (
            f"Enumeration pauses at regular intervals: {pause_indices}. "
            f"Expected irregular intervals (human-like)."
        )

    # ── 3. Mixed distributions ────────────────────────────────────────────

    def test_distribution_uniformity_pattern(self):
        """
        Delays must use multiple distributions, not just uniform.
        Fixed code: _choose_distribution() returns gaussian/uniform/exponential
        with weights 60/30/10.

        Validates: Requirements 1.3 / 2.3
        """
        random.seed(99)
        limiter = _make_limiter()
        counts = {"gaussian": 0, "uniform": 0, "exponential": 0}
        n = 200
        for _ in range(n):
            counts[limiter._choose_distribution()] += 1

        # Gaussian should be ~60%, Uniform ~30%, Exponential ~10%
        # Allow ±15% tolerance
        assert counts["gaussian"] / n >= 0.45, (
            f"Gaussian weight too low: {counts['gaussian']/n:.2f} (expected ≥0.45)"
        )
        assert counts["uniform"] / n >= 0.15, (
            f"Uniform weight too low: {counts['uniform']/n:.2f} (expected ≥0.15)"
        )
        assert counts["exponential"] / n >= 0.02, (
            f"Exponential weight too low: {counts['exponential']/n:.2f} (expected ≥0.02)"
        )

    # ── 4. Operation-specific variation ──────────────────────────────────

    def test_operation_similarity_pattern(self):
        """
        Different operation types must produce distinct timing patterns.
        Fixed code: _OPERATION_STRATEGIES gives profile_view a 1.2x multiplier
        and list_scroll a 0.8x multiplier.

        Validates: Requirements 1.4 / 2.4
        """
        random.seed(77)
        limiter = _make_limiter()

        profile_strategy = _OPERATION_STRATEGIES.get("profile_view", {})
        list_strategy = _OPERATION_STRATEGIES.get("list_scroll", {})

        profile_mult = profile_strategy.get("base_range_multiplier", 1.0)
        list_mult = list_strategy.get("base_range_multiplier", 1.0)

        # The multipliers must differ by at least 20%
        diff_ratio = abs(profile_mult - list_mult) / max(profile_mult, list_mult)
        assert diff_ratio >= 0.2, (
            f"profile_view multiplier={profile_mult}, list_scroll multiplier={list_mult}. "
            f"Difference ratio {diff_ratio:.2f} < 0.2 — insufficient differentiation."
        )

        # Also verify the distributions differ
        profile_dist = profile_strategy.get("distribution_override")
        list_dist = list_strategy.get("distribution_override")
        assert profile_dist != list_dist, (
            f"profile_view and list_scroll use the same distribution '{profile_dist}'. "
            f"Expected different distributions."
        )

    # ── 5. Micro-pauses present ───────────────────────────────────────────

    def test_micro_pause_absence_pattern(self):
        """
        ~70% of track_operation() calls must include a micro-pause.
        Fixed code: track_operation() calls micro_pause() with MICRO_PAUSE_PROBABILITY.

        Validates: Requirements 1.7 / 2.7
        """
        random.seed(55)
        limiter = _make_limiter()
        micro_pause_calls = []

        def _fake_micro_pause():
            micro_pause_calls.append(1)

        n_ops = 30
        with patch.object(limiter, "_sleep"):
            with patch.object(limiter, "micro_pause", side_effect=_fake_micro_pause):
                for _ in range(n_ops):
                    limiter.track_operation()

        actual_rate = len(micro_pause_calls) / n_ops
        assert actual_rate >= MICRO_PAUSE_PROBABILITY * 0.5, (
            f"Micro-pause rate {actual_rate:.2f} is below 50% of expected "
            f"{MICRO_PAUSE_PROBABILITY:.2f}. "
            f"Got {len(micro_pause_calls)} micro-pauses in {n_ops} operations."
        )

    # ── 6. Content-aware delays ───────────────────────────────────────────

    def test_content_blindness_pattern(self):
        """
        Delays must scale with content volume (up to 2.0x).
        Fixed code: content_aware_delay() applies log10-based multiplier.

        Validates: Requirements 1.8 / 2.8
        """
        assert CONTENT_AWARE_ENABLED, "CONTENT_AWARE_ENABLED must be True"

        limiter = _make_limiter()
        delays_small: List[float] = []
        delays_large: List[float] = []

        def _capture_small(seconds, **kwargs):
            delays_small.append(seconds)

        def _capture_large(seconds, **kwargs):
            delays_large.append(seconds)

        random.seed(11)
        with patch.object(limiter, "_sleep", side_effect=_capture_small):
            for _ in range(5):
                limiter.content_aware_delay(post_count=10)

        random.seed(11)
        with patch.object(limiter, "_sleep", side_effect=_capture_large):
            for _ in range(5):
                limiter.content_aware_delay(post_count=1000)

        mean_small = sum(delays_small) / len(delays_small)
        mean_large = sum(delays_large) / len(delays_large)

        # Large-profile delays should be longer
        assert mean_large > mean_small, (
            f"Content-aware delays not scaling: small={mean_small:.1f}s, "
            f"large={mean_large:.1f}s. Expected large > small."
        )

        # Multiplier must not exceed CONTENT_AWARE_MAX_MULTIPLIER
        ratio = mean_large / mean_small if mean_small > 0 else 1.0
        assert ratio <= CONTENT_AWARE_MAX_MULTIPLIER + 0.1, (
            f"Content multiplier {ratio:.2f}x exceeds cap {CONTENT_AWARE_MAX_MULTIPLIER}x"
        )

    # ── 7. Property-based entropy test ───────────────────────────────────

    @given(
        num_delays=st.integers(min_value=50, max_value=150),
        seed=st.integers(min_value=1, max_value=500),
    )
    @settings(max_examples=10, deadline=None)
    def test_property_timing_pattern_entropy(self, num_delays, seed):
        """
        For any sequence of delays generated by the fixed RateLimiter,
        the coefficient of variation must be ≥0.25 (high relative variability).

        Validates: Requirements 1.1, 1.2, 1.3 / 2.1, 2.2, 2.3
        """
        delays = _generate_delays_via_limiter(count=num_delays, seed=seed)
        mean = sum(delays) / len(delays)
        variance = sum((d - mean) ** 2 for d in delays) / len(delays)
        std_dev = variance ** 0.5
        cv = std_dev / mean if mean > 0 else 0.0

        assert cv >= 0.25, (
            f"Counterexample: {num_delays} delays (seed={seed}) → "
            f"CV={cv:.3f} (expected ≥0.25). "
            f"Mean={mean:.1f}s, StdDev={std_dev:.1f}s."
        )
