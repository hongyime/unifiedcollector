from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch
import uuid

import pytest


FACE_ROOT = Path(__file__).resolve().parents[2] / "services" / "face_recognition"
if str(FACE_ROOT) not in sys.path:
    sys.path.insert(0, str(FACE_ROOT))


def load_face_module(name: str):
    with patch.object(Path, "exists", return_value=True):
        return importlib.import_module(f"face_recognition_service.{name}")


def test_processor_dispatches_image_and_video_branches(tmp_path):
    processor_module = load_face_module("processor")
    processor = processor_module.face_processor

    image_path = tmp_path / "photo.jpg"
    image_path.write_bytes(b"image")
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"video")

    fake_embedding = processor_module.FaceEmbedding(
        embedding=[0.0] * 128,
        bbox=(0, 1, 1, 0),
        confidence=1.0,
        frame_index=0,
        source_path=str(image_path),
    )

    def fake_encode(path: str, frame_index: int = 0):
        return [
            processor_module.FaceEmbedding(
                embedding=[float(frame_index)] * 128,
                bbox=(0, 1, 1, 0),
                confidence=1.0,
                frame_index=frame_index,
                source_path=path,
            )
        ]

    processor.encode_image = fake_encode  # type: ignore[method-assign]
    processor.extract_video_frames = lambda path, message_type=None: ["frame-1.jpg", "frame-2.jpg"]  # type: ignore[method-assign]

    image_embeddings = processor.process_media_file(str(image_path), "image/jpeg")
    video_embeddings = processor.process_media_file(str(video_path), "video/mp4")

    assert len(image_embeddings) == 1
    assert image_embeddings[0].source_path == str(image_path)
    assert len(video_embeddings) == 2
    assert [embedding.frame_index for embedding in video_embeddings] == [0, 1]
    assert fake_embedding.confidence == 1.0


def test_extract_video_frames_deduplicates_nearby_frames(tmp_path, monkeypatch):
    processor_module = load_face_module("processor")
    processor = processor_module.face_processor

    monkeypatch.setattr(processor_module.settings, "VIDEO_FRAME_RATE", 10)
    monkeypatch.setattr(processor_module.settings, "VIDEO_NOTE_FRAME_RATE", 10)
    monkeypatch.setattr(processor_module.settings, "PHASH_DEDUP_THRESHOLD", 6)

    video_path = tmp_path / "dedup.mp4"
    video_path.write_bytes(b"video")

    class FakeCapture:
        def __init__(self, path: str):
            self.frames = ["frame-a", "frame-b", "frame-c"]
            self.index = 0

        def isOpened(self) -> bool:
            return True

        def get(self, prop: int) -> int:
            if prop == 5:
                return 10
            if prop == 7:
                return len(self.frames)
            return 0

        def read(self):
            if self.index >= len(self.frames):
                return False, None
            frame = self.frames[self.index]
            self.index += 1
            return True, frame

        def release(self) -> None:
            return None

    class FakeHash:
        def __init__(self, value: int):
            self.value = value

        def __sub__(self, other: object) -> int:
            return abs(self.value - getattr(other, "value", 0))

    class FakeImage:
        def __init__(self, label: str):
            self.label = label
            self.width = 100
            self.height = 100

        def thumbnail(self, size, resample):
            return None

        def save(self, path, format="JPEG", quality=90):
            Path(path).write_text(self.label)

    cv2_module = ModuleType("cv2")
    cv2_module.VideoCapture = FakeCapture
    cv2_module.cvtColor = lambda frame, code: frame
    cv2_module.resize = lambda frame, size, interpolation=None: frame
    cv2_module.COLOR_BGR2RGB = 1
    cv2_module.INTER_AREA = 1
    cv2_module.CAP_PROP_FPS = 5
    cv2_module.CAP_PROP_FRAME_COUNT = 7

    image_module = ModuleType("PIL.Image")
    image_module.fromarray = lambda frame: FakeImage(frame)
    image_module.Resampling = SimpleNamespace(LANCZOS=1)

    pil_module = ModuleType("PIL")
    pil_module.Image = image_module

    imagehash_module = ModuleType("imagehash")
    imagehash_module.phash = lambda image: FakeHash({"frame-a": 1, "frame-b": 1, "frame-c": 10}[image.label])

    monkeypatch.setitem(sys.modules, "cv2", cv2_module)
    monkeypatch.setitem(sys.modules, "PIL", pil_module)
    monkeypatch.setitem(sys.modules, "PIL.Image", image_module)
    monkeypatch.setitem(sys.modules, "imagehash", imagehash_module)

    frames = processor.extract_video_frames(str(video_path))

    assert len(frames) == 2
    assert frames[0] != frames[1]


@pytest.mark.asyncio
async def test_identity_matcher_matches_existing_and_creates_new(monkeypatch):
    matcher_module = load_face_module("matcher")
    matcher = matcher_module.IdentityMatcher(match_threshold=0.5)

    existing_identity = uuid.uuid4()
    update_calls: list[tuple[str, tuple[object, ...]]] = []

    class FakeConn:
        def __init__(self, row):
            self.row = row

        async def fetchrow(self, *args, **kwargs):
            return self.row

        async def fetchval(self, query: str, *args, **kwargs):
            if "RETURNING id" in query:
                return existing_identity if self.row else uuid.uuid4()
            return None

        async def execute(self, query: str, *args, **kwargs):
            update_calls.append((query, args))
            return "OK"

    existing_conn = FakeConn({
        "id": existing_identity,
        "centroid": [0.0] * 128,
        "occurrence_count": 1,
        "distance": 0.1,
    })
    identity_id, is_new = await matcher.match_embedding(
        embedding=[0.0] * 128,
        source_message_id="msg-1",
        source_chat_jid="chat-1",
        frame_index=0,
        confidence=0.9,
        conn=existing_conn,
    )

    assert identity_id == existing_identity
    assert is_new is False
    assert any("UPDATE face_recognition.identity_entities" in query for query, _ in update_calls)

    new_conn = FakeConn(None)
    update_calls.clear()
    new_identity_id, is_new = await matcher.match_embedding(
        embedding=[1.0] * 128,
        source_message_id="msg-2",
        source_chat_jid="chat-2",
        frame_index=1,
        confidence=0.95,
        conn=new_conn,
    )

    assert is_new is True
    assert isinstance(new_identity_id, uuid.UUID)
    assert any("INSERT INTO face_recognition.face_embeddings" in query for query, _ in update_calls)


@pytest.mark.asyncio
async def test_publisher_queues_low_confidence_and_enforces_rate_limit():
    publisher_module = load_face_module("publisher")
    publisher = publisher_module.FindingsPublisher()

    class FakeBroker:
        def __init__(self) -> None:
            self.published: list[tuple[str, dict[str, object]]] = []

        async def publish(self, routing_key: str, payload: dict[str, object]) -> None:
            self.published.append((routing_key, payload))

    fake_broker = FakeBroker()
    publisher._broker = fake_broker
    publisher._jitter_delay = lambda: 0.0  # type: ignore[method-assign]

    await publisher.publish_sighting(
        identity_id="abc",
        original_image_path="/tmp/low.jpg",
        event_type="identity_match",
        confidence=0.2,
    )
    assert len(publisher._queue) == 0

    publisher._tokens = 0.0
    await publisher.publish_sighting(
        identity_id="abc",
        original_image_path="/tmp/high.jpg",
        event_type="identity_match",
        confidence=0.95,
    )
    await publisher.flush_once()

    assert len(publisher._queue) == 1
    assert fake_broker.published == []

    publisher._tokens = 1.0
    await publisher.flush_once()

    assert len(publisher._queue) == 0
    assert fake_broker.published[0][0] == publisher_module.settings.FINDINGS_QUEUE_NAME
    assert fake_broker.published[0][1]["identity_id"] == "abc"


@pytest.mark.asyncio
async def test_worker_processes_file_before_advancing_cursor(monkeypatch):
    worker_module = load_face_module("worker")
    worker = worker_module.FaceRecognitionWorker()

    call_order: list[str] = []

    class FakeTransaction:
        async def __aenter__(self):
            call_order.append("transaction-start")
            return self

        async def __aexit__(self, exc_type, exc, tb):
            call_order.append("transaction-end")
            return False

    class FakeConn:
        def transaction(self):
            return FakeTransaction()

    class FakeAcquire:
        def __init__(self, conn):
            self.conn = conn

        async def __aenter__(self):
            return self.conn

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakePool:
        def __init__(self, conn):
            self.conn = conn

        def acquire(self):
            return FakeAcquire(self.conn)

    worker_module.database.pool = FakePool(FakeConn())

    async def fake_has_processed_media(*args, **kwargs):
        call_order.append("check-processed")
        return False

    async def fake_mark_processed_media(*args, **kwargs):
        call_order.append("mark-processed")

    async def fake_advance_cursor(*args, **kwargs):
        call_order.append("advance-cursor")

    async def fake_insert_face_embedding(*args, **kwargs):
        call_order.append("insert-embedding")

    async def fake_match_embedding(*args, **kwargs):
        call_order.append("match-embedding")
        return uuid.uuid4(), False

    async def fake_publish_sighting(*args, **kwargs):
        call_order.append("publish-finding")

    monkeypatch.setattr(worker_module.database, "has_processed_media", fake_has_processed_media)
    monkeypatch.setattr(worker_module.database, "mark_processed_media", fake_mark_processed_media)
    monkeypatch.setattr(worker_module.database, "advance_cursor", fake_advance_cursor)
    monkeypatch.setattr(worker_module.database, "insert_face_embedding", fake_insert_face_embedding)
    monkeypatch.setattr(worker_module.identity_matcher, "match_embedding", fake_match_embedding)
    monkeypatch.setattr(worker_module.findings_publisher, "publish_sighting", fake_publish_sighting)
    monkeypatch.setattr(
        worker_module.face_processor,
        "process_media_file",
        lambda *args, **kwargs: [
            SimpleNamespace(
                embedding=[0.0] * 128,
                frame_index=0,
                confidence=1.0,
                source_path="/tmp/frame.jpg",
            )
        ],
    )

    await worker._process_one(
        {
            "message_id": "msg-1",
            "chat_jid": "chat-1",
            "raw_message_id": 42,
            "mime_type": "image/jpeg",
            "message_type": "image",
            "by_message_path": "/tmp/image.jpg",
            "by_id_path": None,
        }
    )

    assert call_order.index("mark-processed") < call_order.index("advance-cursor")
    assert call_order.count("match-embedding") == 1
    assert call_order.count("publish-finding") == 1


@pytest.mark.asyncio
async def test_worker_skips_already_processed_files(monkeypatch):
    worker_module = load_face_module("worker")
    worker = worker_module.FaceRecognitionWorker()

    class FakeTransaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeConn:
        def transaction(self):
            return FakeTransaction()

    class FakeAcquire:
        def __init__(self, conn):
            self.conn = conn

        async def __aenter__(self):
            return self.conn

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakePool:
        def __init__(self, conn):
            self.conn = conn

        def acquire(self):
            return FakeAcquire(self.conn)

    worker_module.database.pool = FakePool(FakeConn())

    async def fake_has_processed_media(*args, **kwargs):
        return True

    called = []

    async def fake_match_embedding(*args, **kwargs):
        called.append("match")
        return uuid.uuid4(), False

    monkeypatch.setattr(worker_module.database, "has_processed_media", fake_has_processed_media)
    monkeypatch.setattr(worker_module.identity_matcher, "match_embedding", fake_match_embedding)
    monkeypatch.setattr(worker_module.face_processor, "process_media_file", lambda *args, **kwargs: [])

    await worker._process_one(
        {
            "message_id": "msg-2",
            "chat_jid": "chat-2",
            "raw_message_id": 43,
            "mime_type": "image/jpeg",
            "message_type": "image",
            "by_message_path": "/tmp/image-2.jpg",
            "by_id_path": None,
        }
    )

    assert called == []
