# Feature: dashboards-index-page, Property 6: displayed status equals latest ping result
# Validates: Requirements 10.5, 10.3
from hypothesis import given, settings as h_settings
from hypothesis import strategies as st
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from index.app import update_service_status


@given(
    previous_status=st.booleans(),
    new_ping_result=st.booleans()
)
@h_settings(max_examples=100)
def test_status_reflects_latest_ping(previous_status, new_ping_result):
    # Feature: dashboards-index-page, Property 6: displayed status equals latest ping result
    state = {"status": previous_status}
    updated = update_service_status(state, new_ping_result)
    assert updated["status"] == new_ping_result
