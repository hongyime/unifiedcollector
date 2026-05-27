"""Tests for narrowed semaphore scope in face_recognition _process_one(): BUG-4."""
from __future__ import annotations

import asyncio
import os
import sys
import time
from dataclasses import dataclass, field
from typing import List
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@dataclass
class _FakeEmbedding:
    embedding: list = field(default_factory=lambda: [0.1, 0.2])
    bbox: tuple = (0, 0, 10, 10)
    confidence: float = 0.9
    frame_index: int = 0
    source_path: str = "/tmp/img.jpg"


def _make_worker():
    with patch("services.face_recognition.face_recognition_service.worker.database"), \
         patch("services.face_recognition.face_recognition_service.worker.face_processor"), \
         patch("services.face_recognition.face_recognition_service.worker.identity_matcher"), \
         patch("services.face_recognition.face_recognition_service.worker.findings_publisher"), \
         patch("services.face_recognition.face_recognition_service.worker.RabbitMQBroker"), \
         patch("services.face_recognition.face_recognition_service.worker.start_metrics_server"):
        from services.face_recognition.face_recognition_service.worker import FaceRecognitionWorker
        w = FaceRecognitionWorker()
    return w


def _make_row():
    return {
        "message_id": "msg1",
        "chat_jid": "chat1",
        "raw_message_id": 1,
        "mime_type": "image/jpeg",
        "message_type": "image",
        "by_message_path": "/data/media/img.jpg",
        "by_id_path": "",
    }


# ---------------------------------------------------------------------------
# Test: semaphore is released before database.pool.acquire() is called
# ---------------------------------------------------------------------------

class TestSemaphoreReleasedBeforeDB:
    def test_semaphore_released_before_db_acquire(self):
        import services.face_recognition.face_recognition_service.worker as wmod

        worker = _make_worker()
        worker.is_ready = True
        semaphore_released_at = []
        db_acquired_at = []
        timeline = []

        original_semaphore = asyncio.Semaphore(1)

        class TrackingSemaphore:
            async def __aenter__(self):
                await original_semaphore.acquire()
                return self

            async def __aexit__(self, *args):
                timeline.append("semaphore_released")
                original_semaphore.release()

        worker._semaphore = TrackingSemaphore()

        conn_mock = MagicMock()
        txn_mock = MagicMock()
        txn_mock.__aenter__ = AsyncMock(return_value=None)
        txn_mock.__aexit__ = AsyncMock(return_value=False)
        conn_mock.transaction = MagicMock(return_value=txn_mock)

        class TrackingAcquire:
            async def __aenter__(self):
                timeline.append("db_acquired")
                return conn_mock

            async def __aexit__(self, *args):
                return False

        pool_mock = MagicMock()
        pool_mock.acquire = MagicMock(return_value=TrackingAcquire())
        wmod.database.pool = pool_mock
        wmod.database.has_processed_media = AsyncMock(return_value=False)
        wmod.database.insert_face_embedding = AsyncMock()
        wmod.database.mark_processed_media = AsyncMock()
        wmod.database.advance_cursor = AsyncMock()
        wmod.face_processor.process_media_file = MagicMock(return_value=[_FakeEmbedding()])
        wmod.identity_matcher.match_embedding = AsyncMock(return_value=("id1", False))
        wmod.findings_publisher.publish_sighting = AsyncMock()

        _run(worker._process_one(_make_row()))

        assert timeline.index("semaphore_released") < timeline.index("db_acquired"), (
            "Semaphore must be released before DB connection is acquired"
        )


# ---------------------------------------------------------------------------
# Test: two concurrent _process_one calls can overlap in DB phase
# ---------------------------------------------------------------------------

class TestConcurrentDBPhase:
    def test_two_calls_overlap_in_db_phase(self):
        import services.face_recognition.face_recognition_service.worker as wmod

        worker = _make_worker()
        worker.is_ready = True
        worker._semaphore = asyncio.Semaphore(1)

        db_concurrent_count = [0]
        db_max_concurrent = [0]

        conn_mock = MagicMock()

        class SlowTransaction:
            async def __aenter__(self):
                db_concurrent_count[0] += 1
                db_max_concurrent[0] = max(db_max_concurrent[0], db_concurrent_count[0])
                await asyncio.sleep(0.05)  # Simulate slow DB
                return None

            async def __aexit__(self, *args):
                db_concurrent_count[0] -= 1
                return False

        conn_mock.transaction = MagicMock(return_value=SlowTransaction())

        class ImmediateAcquire:
            async def __aenter__(self):
                return conn_mock

            async def __aexit__(self, *args):
                return False

        pool_mock = MagicMock()
        pool_mock.acquire = MagicMock(return_value=ImmediateAcquire())
        wmod.database.pool = pool_mock
        wmod.database.has_processed_media = AsyncMock(return_value=False)
        wmod.database.insert_face_embedding = AsyncMock()
        wmod.database.mark_processed_media = AsyncMock()
        wmod.database.advance_cursor = AsyncMock()
        wmod.face_processor.process_media_file = MagicMock(return_value=[_FakeEmbedding()])
        wmod.identity_matcher.match_embedding = AsyncMock(return_value=("id1", False))
        wmod.findings_publisher.publish_sighting = AsyncMock()

        row1 = _make_row()
        row2 = {"message_id": "msg2", "chat_jid": "chat2", "raw_message_id": 2,
                "mime_type": "image/jpeg", "message_type": "image",
                "by_message_path": "/data/media/img2.jpg", "by_id_path": ""}

        async def run_both():
            await asyncio.gather(
                worker._process_one(row1),
                worker._process_one(row2),
            )

        _run(run_both())
        assert db_max_concurrent[0] == 2, (
            "Both DB phases should run concurrently since semaphore serializes only CPU work"
        )


# ---------------------------------------------------------------------------
# Property-based: embedding counts match before/after scope change
# ---------------------------------------------------------------------------

try:
    from hypothesis import given, settings as h_settings
    from hypothesis import strategies as st
    HAS_HYPOTHESIS = True
except ImportError:
    HAS_HYPOTHESIS = False


if HAS_HYPOTHESIS:
    @given(num_embeddings=st.integers(min_value=0, max_value=5))
    @h_settings(max_examples=50)
    def test_embedding_count_preserved(num_embeddings):
        """face_count must equal number of embeddings produced regardless of semaphore scope."""
        import services.face_recognition.face_recognition_service.worker as wmod

        worker = _make_worker()
        worker.is_ready = True
        worker._semaphore = asyncio.Semaphore(1)

        embeddings = [_FakeEmbedding(embedding=[float(i)] * 128) for i in range(num_embeddings)]

        conn_mock = MagicMock()
        txn_mock = MagicMock()
        txn_mock.__aenter__ = AsyncMock(return_value=None)
        txn_mock.__aexit__ = AsyncMock(return_value=False)
        conn_mock.transaction = MagicMock(return_value=txn_mock)

        class ImmediateAcquire:
            async def __aenter__(self):
                return conn_mock

            async def __aexit__(self, *args):
                return False

        pool_mock = MagicMock()
        pool_mock.acquire = MagicMock(return_value=ImmediateAcquire())
        wmod.database.pool = pool_mock
        wmod.database.has_processed_media = AsyncMock(return_value=False)
        wmod.database.insert_face_embedding = AsyncMock()
        wmod.database.mark_processed_media = AsyncMock()
        wmod.database.advance_cursor = AsyncMock()
        wmod.face_processor.process_media_file = MagicMock(return_value=embeddings)
        wmod.identity_matcher.match_embedding = AsyncMock(return_value=("id1", False))
        wmod.findings_publisher.publish_sighting = AsyncMock()

        _run(worker._process_one(_make_row()))

        # mark_processed_media called with face_count == num_embeddings
        if num_embeddings > 0:
            call_args = wmod.database.mark_processed_media.call_args
            face_count_arg = call_args[0][2] if call_args[0] else call_args[1].get("face_count", -1)
            assert face_count_arg == num_embeddings
