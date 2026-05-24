"""Tests for BulkSenderWorker TaskSupervisor wrapping job_manager: BUG-5."""
from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_worker():
    with patch("services.bulk_sender.bulk_sender.worker.database"), \
         patch("services.bulk_sender.bulk_sender.worker.job_manager"), \
         patch("services.bulk_sender.bulk_sender.worker.start_metrics_server"), \
         patch("services.bulk_sender.bulk_sender.worker.TaskSupervisor") as MockSupervisor:
        from services.bulk_sender.bulk_sender.worker import BulkSenderWorker
        w = BulkSenderWorker()
    return w


# ---------------------------------------------------------------------------
# Test: job_manager processing coroutine is wrapped in TaskSupervisor
# ---------------------------------------------------------------------------

class TestJobManagerWrappedInSupervisor:
    def test_supervisor_created_for_job_manager(self):
        import services.bulk_sender.bulk_sender.worker as wmod

        supervisor_instances = []

        class FakeSupervisor:
            def __init__(self, name, coro):
                self.name = name
                self.coro = coro
                supervisor_instances.append(self)

            async def start(self):
                pass

            async def stop(self):
                pass

        wmod.TaskSupervisor = FakeSupervisor

        with patch.object(wmod.database, "connect", new=AsyncMock()), \
             patch.object(wmod.job_manager, "start", new=AsyncMock()), \
             patch.object(wmod.job_manager, "_run_loop", create=True):
            from services.bulk_sender.bulk_sender.worker import BulkSenderWorker
            worker = BulkSenderWorker()
            _run(worker.start())
            worker.running = False

        assert any(s.name == "job_manager_loop" for s in supervisor_instances), (
            "A TaskSupervisor named 'job_manager_loop' must be created in start()"
        )


# ---------------------------------------------------------------------------
# Test: stop() calls supervisor.stop() before job_manager.stop()
# ---------------------------------------------------------------------------

class TestStopOrderSupervisorBeforeJobManager:
    def test_supervisor_stopped_before_job_manager(self):
        import services.bulk_sender.bulk_sender.worker as wmod

        stop_order = []

        class FakeSupervisor:
            def __init__(self, name, coro):
                self.name = name

            async def start(self):
                pass

            async def stop(self):
                stop_order.append("supervisor")

        wmod.TaskSupervisor = FakeSupervisor

        async def fake_job_manager_stop():
            stop_order.append("job_manager")

        with patch.object(wmod.database, "connect", new=AsyncMock()), \
             patch.object(wmod.database, "close", new=AsyncMock()), \
             patch.object(wmod.job_manager, "start", new=AsyncMock()), \
             patch.object(wmod.job_manager, "stop", new=fake_job_manager_stop), \
             patch.object(wmod.job_manager, "_run_loop", create=True):
            from services.bulk_sender.bulk_sender.worker import BulkSenderWorker
            worker = BulkSenderWorker()
            _run(worker.start())
            _run(worker.stop())

        supervisor_idx = next((i for i, v in enumerate(stop_order) if v == "supervisor"), None)
        jm_idx = next((i for i, v in enumerate(stop_order) if v == "job_manager"), None)
        assert supervisor_idx is not None, "Supervisor stop not called"
        assert jm_idx is not None, "job_manager stop not called"
        assert supervisor_idx < jm_idx, "Supervisor must be stopped before job_manager"


# ---------------------------------------------------------------------------
# Test: normal operation — _metrics_loop reports running count
# ---------------------------------------------------------------------------

class TestNormalOperationMetricsLoop:
    def test_metrics_loop_calls_summary_stats(self):
        import services.bulk_sender.bulk_sender.worker as wmod

        stats_calls = []

        async def fake_summary_stats():
            stats_calls.append(True)
            return {"running": 1}

        class FakeSupervisor:
            def __init__(self, name, coro):
                self.name = name

            async def start(self):
                pass

            async def stop(self):
                pass

        wmod.TaskSupervisor = FakeSupervisor

        with patch.object(wmod.database, "connect", new=AsyncMock()), \
             patch.object(wmod.database, "close", new=AsyncMock()), \
             patch.object(wmod.database, "summary_stats", new=fake_summary_stats), \
             patch.object(wmod.job_manager, "start", new=AsyncMock()), \
             patch.object(wmod.job_manager, "stop", new=AsyncMock()), \
             patch.object(wmod.job_manager, "_run_loop", create=True), \
             patch("services.bulk_sender.bulk_sender.worker.job_status_gauge") as mock_gauge:
            from services.bulk_sender.bulk_sender.worker import BulkSenderWorker
            worker = BulkSenderWorker()
            _run(worker.start())

            # Run one metrics loop tick then stop
            async def one_tick():
                worker.running = True
                # Temporarily override sleep to not actually wait
                original_sleep = asyncio.sleep

                async def fast_sleep(_):
                    worker.running = False  # Stop after first iteration

                with patch("asyncio.sleep", fast_sleep):
                    await worker._metrics_loop()

            _run(one_tick())

        assert len(stats_calls) >= 1
