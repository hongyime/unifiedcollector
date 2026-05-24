# Feature: dashboards-index-page, Property 1: no unconsumed message is ever pruned
# Validates: Requirements 7.1, 7.6
from hypothesis import given, settings as h_settings
from hypothesis import strategies as st
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.collector.dashboard.pruning import compute_prunable_set

@given(
    cursors=st.lists(st.integers(min_value=1, max_value=10_000), min_size=1),
    message_ids=st.lists(st.integers(min_value=1, max_value=10_000))
)
@h_settings(max_examples=100)
def test_pruning_safety(cursors, message_ids):
    # Feature: dashboards-index-page, Property 1: no unconsumed message is ever pruned
    min_cursor = min(cursors)
    prunable = compute_prunable_set(message_ids, min_cursor)
    assert all(mid <= min_cursor for mid in prunable)
