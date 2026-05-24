# Feature: dashboards-index-page, Property 4: only valid status values are written
# Validates: Requirements 3.3
from hypothesis import given, settings as h_settings
from hypothesis import strategies as st
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.collector.dashboard.app import get_status_for_action

VALID_BACKFILL_STATUSES = {"pending", "running", "paused", "complete", "failed"}

@given(
    action=st.sampled_from(["pause", "resume", "cancel"]),
    job_id=st.integers(min_value=1, max_value=100_000)
)
@h_settings(max_examples=100)
def test_backfill_status_write_correctness(action, job_id):
    # Feature: dashboards-index-page, Property 4: only valid status values are written
    written_status = get_status_for_action(action)
    assert written_status in VALID_BACKFILL_STATUSES
