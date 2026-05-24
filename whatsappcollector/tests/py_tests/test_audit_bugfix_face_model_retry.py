"""Tests for face recognition model load retry (non-fatal startup): BUG-11."""
from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_worker():
    with patch("services.face_recognition.face_recognition_service.worker.database"), \
         patch("services.face_recognition.face_recognition_service.worker.face_processor") as mock_fp, \
         patch("services.face_recognition.face_recognition_service.worker.identity_matcher"), \
         patch("services.face_recognition.face_recognition_service.worker.findings_publisher"), \
         patch("services.face_recognition.face_recognition_service.worker.RabbitMQBroker"), \
         patch("services.face_recognition.face_recognition_service.worker.start_metrics_server"):
        from services.face_recognition.face_recognition_service.worker import FaceRecognitionWorker
        w = FaceRecognitionWorker()
    return w


# ---------------------------------------------------------------------------
# Test: start with models absent — worker does not crash, is_ready=False
# ---------------------------------------------------------------------------

class TestModelsAbsentNoCrash:
    def test_worker_does_not_crash_when_models_absent(self):
        import services.face_recognition.face_recognition_service.worker as wmod

        wmod.face_processor.models_ready = False
        wmod.face_processor._verify_models = MagicMock()  # sets models_ready=False (already set)

        worker = _make_worker()
        assert not worker.is_ready

        worker.try_load_models()
        assert not worker.is_ready

    def test_model_reload_loop_task_created_on_deferred_start(self):
        import services.face_recognition.face_recognition_service.worker as wmod

        wmod.face_processor.models_ready = False
        wmod.face_processor._verify_models = MagicMock()

        worker = _make_worker()
        worker.is_ready = False

        with patch.object(wmod.database, "connect", new=AsyncMock()), \
             patch.object(wmod.database, "seed_cursor", new=AsyncMock()), \
             patch.object(wmod.database, "close", new=AsyncMock()):
            broker_mock = MagicMock()
            broker_mock.connect = AsyncMock()
            broker_mock.declare_topology = AsyncMock()
            broker_mock.close = AsyncMock()
            worker._broker = broker_mock
            wmod.findings_publisher.start = MagicMock()

            _run(worker.start())

        # At least 3 tasks: _process_loop, _dashboard_probe_loop, _model_reload_loop
        assert len(worker._tasks) >= 3, "Expected _model_reload_loop task when models not ready"
        # Clean up tasks
        for t in worker._tasks:
            if not t.done():
                t.cancel()


# ---------------------------------------------------------------------------
# Test: models available after 1 retry — is_ready becomes True
# ---------------------------------------------------------------------------

class TestModelsAvailableAfterRetry:
    def test_is_ready_becomes_true_after_retry(self):
        import services.face_recognition.face_recognition_service.worker as wmod

        call_count = [0]

        def fake_verify_models():
            call_count[0] += 1
            if call_count[0] >= 2:
                wmod.face_processor.models_ready = True
            else:
                wmod.face_processor.models_ready = False

        wmod.face_processor.models_ready = False
        wmod.face_processor._verify_models = fake_verify_models

        worker = _make_worker()
        worker.is_ready = False

        async def run_reload_with_fast_sleep():
            sleep_count = [0]
            original_sleep = asyncio.sleep

            async def fast_sleep(seconds):
                sleep_count[0] += 1
                # Allow one iteration then let it run normally
                await original_sleep(0)

            with patch("asyncio.sleep", fast_sleep):
                await worker._model_reload_loop()

        _run(run_reload_with_fast_sleep())
        assert worker.is_ready is True


# ---------------------------------------------------------------------------
# Test: start with models present — is_ready=True, no reload loop
# ---------------------------------------------------------------------------

class TestModelsPresent:
    def test_is_ready_true_immediately_when_models_present(self):
        import services.face_recognition.face_recognition_service.worker as wmod

        wmod.face_processor.models_ready = True
        wmod.face_processor._verify_models = MagicMock(side_effect=lambda: setattr(wmod.face_processor, 'models_ready', True))

        worker = _make_worker()
        worker.try_load_models()
        assert worker.is_ready is True

    def test_no_reload_loop_when_models_ready_on_start(self):
        import services.face_recognition.face_recognition_service.worker as wmod

        wmod.face_processor.models_ready = True
        wmod.face_processor._verify_models = MagicMock(side_effect=lambda: setattr(wmod.face_processor, 'models_ready', True))

        worker = _make_worker()

        with patch.object(wmod.database, "connect", new=AsyncMock()), \
             patch.object(wmod.database, "seed_cursor", new=AsyncMock()), \
             patch.object(wmod.database, "close", new=AsyncMock()):
            broker_mock = MagicMock()
            broker_mock.connect = AsyncMock()
            broker_mock.declare_topology = AsyncMock()
            broker_mock.close = AsyncMock()
            worker._broker = broker_mock
            wmod.findings_publisher.start = MagicMock()

            _run(worker.start())

        # Should only have _process_loop and _dashboard_probe_loop (not _model_reload_loop)
        assert len(worker._tasks) == 2
        for t in worker._tasks:
            if not t.done():
                t.cancel()


# ---------------------------------------------------------------------------
# Test: _process_loop skips processing when is_ready=False
# ---------------------------------------------------------------------------

class TestProcessLoopSkipsWhenNotReady:
    def test_process_loop_skips_processing_when_not_ready(self):
        import services.face_recognition.face_recognition_service.worker as wmod

        worker = _make_worker()
        worker.is_ready = False
        worker.running = True

        get_cursor_called = []

        async def fake_get_cursor():
            get_cursor_called.append(True)
            return 0

        wmod.database.get_cursor = fake_get_cursor

        iteration_count = [0]

        async def run_one_tick():
            original_sleep = asyncio.sleep

            async def controlled_sleep(seconds):
                iteration_count[0] += 1
                if iteration_count[0] >= 2:
                    worker.running = False
                await original_sleep(0)

            with patch("asyncio.sleep", controlled_sleep):
                await worker._process_loop()

        _run(run_one_tick())

        assert not get_cursor_called, "get_cursor should not be called when is_ready=False"
