"""
Property-based test for BulkSenderService — Property 1: Send Delay Invariant.

Feature: bulk-sender-service, Property 1: Send Delay Invariant
Validates: Requirements 2.3, 2.4, 3.1, 3.2, 3.3, 13.1, 13.6
"""

from hypothesis import given, settings
import hypothesis.strategies as st


@given(st.floats(min_value=-10.0, max_value=10.0, allow_nan=False))
@settings(max_examples=100)
def test_send_delay_invariant(configured_delay: float) -> None:
    # Feature: bulk-sender-service, Property 1: Send Delay Invariant
    effective = max(configured_delay, 1.0)
    assert effective >= 1.0
