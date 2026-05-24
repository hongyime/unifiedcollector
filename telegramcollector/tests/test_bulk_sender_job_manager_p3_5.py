# Feature: bulk-sender-service, Property 4: Orphan Recovery
"""
Property 4: Orphan Recovery

For any set of jobs with status='running' present in bulk_sender.send_jobs at
the time recover_orphaned_jobs() is called, every such job SHALL have its status
set to 'paused'. No job with any other status SHALL be modified.

Validates: Requirements 8.4, 9.4
"""

import sys
import os
import unittest.mock as mock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# Ensure the repo root is on the path so we can import the service package
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.bulk_sender.job_manager import JobManager


def _make_job_manager_with_statuses(statuses: list[str]):
    """
    Build a JobManager whose pool is fully mocked.

    The in-memory job list is a list of dicts: [{'id': i, 'status': s}, ...].
    When recover_orphaned_jobs() calls cur.execute() with the orphan-recovery
    SQL we intercept it and flip every 'running' entry to 'paused'.
    cur.rowcount then returns the number of rows that were changed.
    """
    jobs = [{"id": i, "status": s} for i, s in enumerate(statuses)]

    updated_count_holder = [0]

    def fake_execute(sql, params=None):
        # Detect the orphan-recovery UPDATE by looking for the WHERE clause
        if "status = 'running'" in sql or "status='running'" in sql:
            count = 0
            for job in jobs:
                if job["status"] == "running":
                    job["status"] = "paused"
                    count += 1
            updated_count_holder[0] = count

    mock_cursor = mock.MagicMock()
    mock_cursor.__enter__ = lambda s: s
    mock_cursor.__exit__ = mock.MagicMock(return_value=False)
    mock_cursor.execute.side_effect = fake_execute
    # rowcount is read after execute; use a property-like side_effect via PropertyMock
    type(mock_cursor).rowcount = mock.PropertyMock(
        side_effect=lambda: updated_count_holder[0]
    )

    mock_conn = mock.MagicMock()
    mock_conn.cursor.return_value = mock_cursor
    mock_conn.commit = mock.MagicMock()
    mock_conn.rollback = mock.MagicMock()

    mock_pool = mock.MagicMock()
    mock_pool.getconn.return_value = mock_conn
    mock_pool.putconn = mock.MagicMock()

    # Bypass __init__ so no real DB connection is attempted
    jm = JobManager.__new__(JobManager)
    jm._pool = mock_pool

    return jm, jobs


@given(
    st.lists(
        st.sampled_from(["pending", "running", "paused", "complete", "failed", "cancelled"]),
        min_size=0,
        max_size=20,
    )
)
@settings(max_examples=100)
def test_orphan_recovery_property(statuses):
    """
    **Validates: Requirements 8.4, 9.4**

    Property 4: Orphan Recovery — for any mix of job statuses, calling
    recover_orphaned_jobs() must:
      1. Set every 'running' job to 'paused'.
      2. Leave every non-'running' job's status unchanged.
      3. Return the exact count of jobs that were 'running'.
    """
    # Snapshot the original statuses before recovery
    original_statuses = list(statuses)

    jm, jobs = _make_job_manager_with_statuses(statuses)

    returned_count = jm.recover_orphaned_jobs()

    # Count how many were originally 'running'
    expected_recovered = original_statuses.count("running")

    # 1. Return value must equal the number of running jobs
    assert returned_count == expected_recovered, (
        f"recover_orphaned_jobs() returned {returned_count}, "
        f"expected {expected_recovered} (original statuses: {original_statuses})"
    )

    for i, job in enumerate(jobs):
        original = original_statuses[i]
        if original == "running":
            # 2. All previously-running jobs must now be 'paused'
            assert job["status"] == "paused", (
                f"Job {i} was 'running' but is now '{job['status']}' after recovery"
            )
        else:
            # 3. All other jobs must be unchanged
            assert job["status"] == original, (
                f"Job {i} had status '{original}' but was changed to "
                f"'{job['status']}' — non-running jobs must not be modified"
            )
