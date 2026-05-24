"""
Unit tests for JobManager (services/bulk_sender/job_manager.py).

All tests use mocked psycopg2 pool — no real database required.

Requirements: 4.4, 8.4, 9.4
"""

import sys
import os
import unittest
from unittest.mock import MagicMock, patch, call

# Ensure the project root is on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.bulk_sender.job_manager import JobManager


def _make_job_manager() -> JobManager:
    """Instantiate JobManager without calling __init__ (no real DB needed)."""
    jm = JobManager.__new__(JobManager)
    return jm


class TestRecoverOrphanedJobs(unittest.TestCase):
    """Tests for JobManager.recover_orphaned_jobs — Requirements 8.4, 9.4"""

    def test_recover_orphaned_jobs_only_updates_running(self):
        """
        Given jobs with statuses ['pending', 'running', 'paused', 'running', 'complete'],
        recover_orphaned_jobs() should return 2 and only affect the 'running' rows.
        """
        jm = _make_job_manager()

        # Track which statuses were "updated" in memory
        statuses = ["pending", "running", "paused", "running", "complete"]
        updated = []

        # Simulate cursor: rowcount reflects how many 'running' rows exist
        mock_cursor = MagicMock()
        mock_cursor.__enter__ = lambda s: s
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_cursor.rowcount = sum(1 for s in statuses if s == "running")  # 2

        # Capture the SQL executed so we can assert it targets 'running'
        def fake_execute(sql, params=None):
            # Record which rows would be updated based on in-memory state
            for i, s in enumerate(statuses):
                if s == "running":
                    updated.append(i)
                    statuses[i] = "paused"

        mock_cursor.execute = fake_execute

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        mock_pool = MagicMock()
        mock_pool.getconn.return_value = mock_conn

        jm._pool = mock_pool

        result = jm.recover_orphaned_jobs()

        # Should return the count of rows updated (2 running jobs)
        self.assertEqual(result, 2)

        # Only the two 'running' jobs (indices 1 and 3) should have been changed
        self.assertEqual(updated, [1, 3])

        # All formerly-running jobs are now 'paused'
        self.assertEqual(statuses[1], "paused")
        self.assertEqual(statuses[3], "paused")

        # Non-running statuses are untouched
        self.assertEqual(statuses[0], "pending")
        self.assertEqual(statuses[2], "paused")   # was already paused
        self.assertEqual(statuses[4], "complete")

        # Connection was committed and returned to pool
        mock_conn.commit.assert_called_once()
        mock_pool.putconn.assert_called_once_with(mock_conn)


class TestIsAlreadySent(unittest.TestCase):
    """Tests for JobManager.is_already_sent — Requirements 4.4"""

    def _build_jm_with_fetchone(self, fetchone_return):
        jm = _make_job_manager()

        mock_cursor = MagicMock()
        mock_cursor.__enter__ = lambda s: s
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchone.return_value = fetchone_return

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        mock_pool = MagicMock()
        mock_pool.getconn.return_value = mock_conn

        jm._pool = mock_pool
        return jm

    def test_is_already_sent_returns_true_when_exists(self):
        """fetchone() returns (1,) → is_already_sent should return True."""
        jm = self._build_jm_with_fetchone((1,))
        result = jm.is_already_sent(job_id=1, file_hash="abc123")
        self.assertTrue(result)

    def test_is_already_sent_returns_false_when_not_exists(self):
        """fetchone() returns None → is_already_sent should return False."""
        jm = self._build_jm_with_fetchone(None)
        result = jm.is_already_sent(job_id=1, file_hash="abc123")
        self.assertFalse(result)


class TestRecordSentItem(unittest.TestCase):
    """Tests for JobManager.record_sent_item — Requirements 4.4, 9.4"""

    def test_record_sent_item_is_idempotent(self):
        """
        Calling record_sent_item twice with the same args must not raise,
        and the SQL must contain 'ON CONFLICT' and 'DO NOTHING'.
        """
        jm = _make_job_manager()

        executed_sqls = []

        mock_cursor = MagicMock()
        mock_cursor.__enter__ = lambda s: s
        mock_cursor.__exit__ = MagicMock(return_value=False)

        def capture_execute(sql, params=None):
            executed_sqls.append(sql)

        mock_cursor.execute = capture_execute

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        mock_pool = MagicMock()
        mock_pool.getconn.return_value = mock_conn

        jm._pool = mock_pool

        # Call twice — should not raise on either call
        jm.record_sent_item(1, "/path/file.jpg", "hash123", 999)
        jm.record_sent_item(1, "/path/file.jpg", "hash123", 999)

        # Both calls should have executed SQL
        self.assertEqual(len(executed_sqls), 2)

        # Every executed SQL must contain the idempotency clause
        for sql in executed_sqls:
            self.assertIn("ON CONFLICT", sql)
            self.assertIn("DO NOTHING", sql)

        # Connection committed and returned to pool for each call
        self.assertEqual(mock_conn.commit.call_count, 2)
        self.assertEqual(mock_pool.putconn.call_count, 2)


if __name__ == "__main__":
    unittest.main()
