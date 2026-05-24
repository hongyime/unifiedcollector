"""
Unit tests for BulkSenderService (services/bulk_sender/main.py).

Requirements: 3.2, 8.4, 9.4, 13.6
"""

import asyncio
import sys
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch, call

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.bulk_sender.main import BulkSenderService


def _make_mocks():
    """Return a (mock_job_manager, mock_sender) pair."""
    mock_jm = MagicMock()
    mock_sender = MagicMock()
    return mock_jm, mock_sender


class TestInitSendDelay(unittest.TestCase):
    """Tests for __init__ send-delay clamping — Requirements 3.2, 13.6"""

    def test_init_send_delay_clamped_to_minimum(self):
        """BULK_SENDER_SEND_DELAY=0.5 (below minimum) → _effective_delay == 1.0"""
        mock_jm, mock_sender = _make_mocks()
        with patch("shared.config.settings.BULK_SENDER_SEND_DELAY", 0.5):
            svc = BulkSenderService(job_manager=mock_jm, sender=mock_sender)
        self.assertEqual(svc._effective_delay, 1.0)

    def test_init_send_delay_not_clamped_when_above_minimum(self):
        """BULK_SENDER_SEND_DELAY=2.5 (above minimum) → _effective_delay == 2.5"""
        mock_jm, mock_sender = _make_mocks()
        with patch("shared.config.settings.BULK_SENDER_SEND_DELAY", 2.5):
            svc = BulkSenderService(job_manager=mock_jm, sender=mock_sender)
        self.assertEqual(svc._effective_delay, 2.5)

    def test_init_logs_warning_when_delay_below_minimum(self):
        """BULK_SENDER_SEND_DELAY=0.1 → logger.warning called exactly once"""
        mock_jm, mock_sender = _make_mocks()
        with patch("shared.config.settings.BULK_SENDER_SEND_DELAY", 0.1), \
             patch("services.bulk_sender.main.logger") as mock_logger:
            BulkSenderService(job_manager=mock_jm, sender=mock_sender)
        mock_logger.warning.assert_called_once()


class TestOrphanRecoveryOnStartup(unittest.TestCase):
    """Tests for orphan recovery called before the job runner loop — Requirements 8.4, 9.4"""

    def test_orphan_recovery_called_on_startup(self):
        """recover_orphaned_jobs() must be called exactly once before the poll loop."""
        mock_jm, mock_sender = _make_mocks()

        call_order = []

        def track_recover():
            call_order.append("recover")
            return 0

        def track_get_pending():
            call_order.append("get_pending")
            return []  # empty → no tasks spawned

        mock_jm.recover_orphaned_jobs.side_effect = track_recover
        mock_jm.get_pending_jobs.side_effect = track_get_pending

        svc = BulkSenderService(
            job_manager=mock_jm,
            sender=mock_sender,
            poll_interval=0.01,
        )

        # Patch _spawn_dashboard to avoid subprocess creation
        svc._spawn_dashboard = MagicMock(return_value=None)

        async def run_one_iteration():
            # Stop after the first poll cycle
            original_sleep = asyncio.sleep

            async def stop_after_first_sleep(delay):
                svc._running = False
                await original_sleep(0)

            # add_signal_handler is not supported on Windows; patch it out
            loop = asyncio.get_running_loop()
            loop.add_signal_handler = MagicMock()

            with patch("asyncio.sleep", side_effect=stop_after_first_sleep):
                await svc.start()

        asyncio.run(run_one_iteration())

        mock_jm.recover_orphaned_jobs.assert_called_once()
        # recover must have happened before any get_pending_jobs call
        self.assertEqual(call_order[0], "recover")
        self.assertIn("get_pending", call_order)


class TestRunJobSetsFailedOnException(unittest.TestCase):
    """Tests for _run_job exception handling — Requirements 9.4"""

    def test_run_job_sets_failed_on_exception(self):
        """sender.send_job raising RuntimeError → set_status(job_id, 'failed') called"""
        mock_jm, mock_sender = _make_mocks()

        mock_jm.get_job.return_value = {
            "id": 1,
            "status": "pending",
            "source_type": "folder",
            "source_path": "/tmp/files",
            "target_chat_id": 123,
        }
        mock_sender.send_job = AsyncMock(side_effect=RuntimeError("test error"))

        svc = BulkSenderService(job_manager=mock_jm, sender=mock_sender)

        asyncio.run(svc._run_job(job_id=1))

        mock_jm.set_status.assert_called_with(1, "failed")


if __name__ == "__main__":
    unittest.main()
