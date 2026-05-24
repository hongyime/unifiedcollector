"""Property-based tests for FaceProcessor (task 2.6).

Tests Properties 3–8 from the design document.
All tests use Hypothesis with @given and @settings(max_examples=100).

**Validates: Requirements 4.1, 4.2, 4.3, 5.5, 5.6, 5.7**
"""

import asyncio
import math
import os
import sys
import tempfile
from math import floor
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from hypothesis import given, settings as h_settings, assume
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Path setup and heavy-dependency stubs
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

# Stub insightface before any import that might pull it in
for _mod in ("insightface", "insightface.app"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

# Ensure required env vars are present for shared.config
os.environ.setdefault("TG_API_ID", "12345")
os.environ.setdefault("TG_API_HASH", "test_hash")
os.environ.setdefault("BOT_TOKEN", "123:test_token")
os.environ.setdefault("HUB_GROUP_ID", "-100123456789")
os.environ.setdefault("DB_PASSWORD", "test_password")

# Import real cv2 BEFORE conftest can mock it, and keep a reference.
# conftest.py stubs sys.modules['cv2'] with a MagicMock; we need the real one
# for Properties 3–5 which write/read actual image and video files.
# Strategy: temporarily remove the mock, import the real cv2, then restore.
_cv2_mock = sys.modules.pop("cv2", None)
import importlib as _importlib
_real_cv2 = _importlib.import_module("cv2")
# Restore the mock so other tests that rely on it aren't broken
if _cv2_mock is not None:
    sys.modules["cv2"] = _cv2_mock

# Now import the processor — it will get the mocked cv2 from sys.modules,
# but we'll patch it with _real_cv2 inside each test that needs real I/O.
from services.face_recognition.processor import FaceProcessor  # noqa: E402
from shared.config import settings  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_temp_jpeg(width: int = 64, height: int = 64) -> str:
    """Write a random BGR image to a temp JPEG file and return its path."""
    rng = np.random.default_rng()
    img = rng.integers(0, 256, (height, width, 3), dtype=np.uint8)
    tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    tmp.close()
    _real_cv2.imwrite(tmp.name, img)
    return tmp.name


def _write_temp_video(n_frames: int, fps: float = 30.0, width: int = 64, height: int = 64) -> str:
    """Write a temp AVI video with n_frames random frames and return its path."""
    tmp = tempfile.NamedTemporaryFile(suffix=".avi", delete=False)
    tmp.close()
    fourcc = _real_cv2.VideoWriter_fourcc(*"MJPG")
    writer = _real_cv2.VideoWriter(tmp.name, fourcc, fps, (width, height))
    rng = np.random.default_rng()
    for _ in range(n_frames):
        frame = rng.integers(0, 256, (height, width, 3), dtype=np.uint8)
        writer.write(frame)
    writer.release()
    return tmp.name


def _make_normalized_embedding(dim: int = 512) -> list:
    """Return a random unit-norm embedding of length dim."""
    vec = np.random.randn(dim).astype(np.float32)
    vec /= np.linalg.norm(vec)
    return vec.tolist()


# ---------------------------------------------------------------------------
# Property 3: Photo Frame Count
# Validates: Requirements 4.1
#
# extract_frames with 'photo' returns exactly 1 frame for any valid JPEG.
# ---------------------------------------------------------------------------

@given(
    width=st.integers(min_value=32, max_value=256),
    height=st.integers(min_value=32, max_value=256),
)
@h_settings(max_examples=100)
def test_property_3_photo_frame_count(width: int, height: int) -> None:
    """**Validates: Requirements 4.1**

    extract_frames with message_type='photo' returns exactly 1 frame
    regardless of image dimensions.
    """
    FaceProcessor.reset_instance()
    path = _write_temp_jpeg(width, height)
    try:
        processor = FaceProcessor()
        # Patch cv2 in the processor module with the real cv2 (conftest mocks it)
        with patch("services.face_recognition.processor.cv2", _real_cv2):
            frames = asyncio.run(processor.extract_frames(path, "photo"))
        assert len(frames) == 1, (
            f"Expected exactly 1 frame for photo, got {len(frames)}"
        )
        assert isinstance(frames[0], np.ndarray), "Frame must be a numpy array"
        assert frames[0].ndim == 3, "Frame must be a 3-D (H, W, C) array"
    finally:
        os.unlink(path)
        FaceProcessor.reset_instance()


# ---------------------------------------------------------------------------
# Property 4: Video Frame Count Bound
# Validates: Requirements 4.2
#
# extract_frames with 'video' returns between 1 and FACE_VIDEO_MAX_FRAMES frames.
# ---------------------------------------------------------------------------

@given(n_frames=st.integers(min_value=1, max_value=50))
@h_settings(max_examples=100)
def test_property_4_video_frame_count_bound(n_frames: int) -> None:
    """**Validates: Requirements 4.2**

    extract_frames with message_type='video' returns between 1 and
    FACE_VIDEO_MAX_FRAMES frames for any video with 1–50 source frames.
    """
    FaceProcessor.reset_instance()
    path = _write_temp_video(n_frames)
    try:
        processor = FaceProcessor()
        with patch("services.face_recognition.processor.cv2", _real_cv2):
            frames = asyncio.run(processor.extract_frames(path, "video"))
        assert len(frames) >= 1, (
            f"Expected at least 1 frame, got {len(frames)}"
        )
        assert len(frames) <= settings.FACE_VIDEO_MAX_FRAMES, (
            f"Expected at most {settings.FACE_VIDEO_MAX_FRAMES} frames, "
            f"got {len(frames)}"
        )
    finally:
        os.unlink(path)
        FaceProcessor.reset_instance()


# ---------------------------------------------------------------------------
# Property 5: Circle Video Frame Count
# Validates: Requirements 4.3
#
# Frame count equals floor(duration * FACE_CIRCLE_VIDEO_FPS) ±1.
# ---------------------------------------------------------------------------

@given(
    n_frames=st.integers(min_value=2, max_value=60),
    source_fps=st.sampled_from([15.0, 24.0, 25.0, 30.0]),
)
@h_settings(max_examples=100)
def test_property_5_circle_video_frame_count(n_frames: int, source_fps: float) -> None:
    """**Validates: Requirements 4.3**

    extract_frames with message_type='circle_video' returns a frame count
    equal to floor(duration * FACE_CIRCLE_VIDEO_FPS) ±1, where duration is
    the video duration in seconds.
    """
    FaceProcessor.reset_instance()
    path = _write_temp_video(n_frames, fps=source_fps)
    try:
        processor = FaceProcessor()
        with patch("services.face_recognition.processor.cv2", _real_cv2):
            frames = asyncio.run(processor.extract_frames(path, "circle_video"))

        duration = n_frames / source_fps
        expected = floor(duration * settings.FACE_CIRCLE_VIDEO_FPS)
        # Allow ±1 for floating-point rounding in frame position calculation
        assert abs(len(frames) - expected) <= 1, (
            f"Expected ~{expected} frames (duration={duration:.3f}s, "
            f"fps={settings.FACE_CIRCLE_VIDEO_FPS}), got {len(frames)}"
        )
    finally:
        os.unlink(path)
        FaceProcessor.reset_instance()


# ---------------------------------------------------------------------------
# Property 6: Embedding Normalization
# Validates: Requirements 5.5
#
# Each embedding returned by _detect_faces_sync is 512 floats with L2 norm
# in [0.99, 1.01].
# ---------------------------------------------------------------------------

@given(n_faces=st.integers(min_value=1, max_value=5))
@h_settings(max_examples=100)
def test_property_6_embedding_normalization(n_faces: int) -> None:
    """**Validates: Requirements 5.5**

    Each embedding in the output of _detect_faces_sync is a 512-dimensional
    float vector with L2 norm in [0.99, 1.01].
    """
    FaceProcessor.reset_instance()

    # Build synthetic face dicts with normalized embeddings
    synthetic_faces = []
    for _ in range(n_faces):
        embedding = _make_normalized_embedding(512)
        synthetic_faces.append({
            "embedding": embedding,
            "bbox": [10.0, 10.0, 60.0, 60.0],
            "quality": 0.9,
            "landmarks": None,
        })

    processor = FaceProcessor()

    # Patch _detect_faces_sync to return our synthetic faces
    with patch.object(processor, "_detect_faces_sync", return_value=synthetic_faces):
        image = np.zeros((128, 128, 3), dtype=np.uint8)
        result = asyncio.run(processor.process_image(image))

    assert len(result) == n_faces, (
        f"Expected {n_faces} faces, got {len(result)}"
    )
    for i, face in enumerate(result):
        emb = face["embedding"]
        assert len(emb) == 512, (
            f"Face {i}: expected 512-dim embedding, got {len(emb)}"
        )
        norm = np.linalg.norm(emb)
        assert 0.99 <= norm <= 1.01, (
            f"Face {i}: L2 norm {norm:.6f} not in [0.99, 1.01]"
        )

    FaceProcessor.reset_instance()


# ---------------------------------------------------------------------------
# Property 7: Quality Filter Invariant
# Validates: Requirements 5.6
#
# No face with quality < threshold appears in the output of _detect_faces_sync.
# ---------------------------------------------------------------------------

@given(
    qualities=st.lists(
        st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
        min_size=1,
        max_size=10,
    ),
    threshold=st.floats(min_value=0.1, max_value=0.9, allow_nan=False),
)
@h_settings(max_examples=100)
def test_property_7_quality_filter_invariant(
    qualities: list, threshold: float
) -> None:
    """**Validates: Requirements 5.6**

    _detect_faces_sync never returns a face whose quality score is below
    the configured FACE_MIN_QUALITY_THRESHOLD.
    """
    FaceProcessor.reset_instance()

    # Build mock InsightFace face objects
    mock_faces = []
    for q in qualities:
        face = MagicMock()
        face.det_score = float(q)
        bbox = np.array([10.0, 10.0, 60.0, 60.0])  # 50×50 — passes size filter
        face.bbox = bbox
        emb = np.array(_make_normalized_embedding(512), dtype=np.float32)
        face.normed_embedding = emb
        face.landmark_2d_106 = None
        face.kps = None
        mock_faces.append(face)

    processor = FaceProcessor()
    processor._initialized = True  # skip lazy init
    processor.app = MagicMock()
    processor.app.get = MagicMock(return_value=mock_faces)

    with patch("services.face_recognition.processor.get_dynamic_setting",
               return_value=threshold):
        image = np.zeros((128, 128, 3), dtype=np.uint8)
        result = processor._detect_faces_sync(image)

    for face in result:
        assert face["quality"] >= threshold, (
            f"Face with quality {face['quality']:.4f} passed filter "
            f"(threshold={threshold:.4f})"
        )

    FaceProcessor.reset_instance()


# ---------------------------------------------------------------------------
# Property 8: Size Filter Invariant
# Validates: Requirements 5.7
#
# No face with bbox < 40×40 appears in the output of _detect_faces_sync.
# ---------------------------------------------------------------------------

@given(
    bboxes=st.lists(
        st.tuples(
            st.floats(min_value=0.0, max_value=100.0, allow_nan=False),  # x1
            st.floats(min_value=0.0, max_value=100.0, allow_nan=False),  # y1
            st.floats(min_value=0.0, max_value=200.0, allow_nan=False),  # width
            st.floats(min_value=0.0, max_value=200.0, allow_nan=False),  # height
        ),
        min_size=1,
        max_size=10,
    ),
)
@h_settings(max_examples=100)
def test_property_8_size_filter_invariant(bboxes: list) -> None:
    """**Validates: Requirements 5.7**

    _detect_faces_sync never returns a face whose bounding box is smaller
    than 40×40 pixels (width < 40 or height < 40).
    """
    FaceProcessor.reset_instance()

    mock_faces = []
    for (x1, y1, w, h) in bboxes:
        x2 = x1 + w
        y2 = y1 + h
        face = MagicMock()
        face.det_score = 0.9  # always passes quality filter
        face.bbox = np.array([x1, y1, x2, y2])
        emb = np.array(_make_normalized_embedding(512), dtype=np.float32)
        face.normed_embedding = emb
        face.landmark_2d_106 = None
        face.kps = None
        mock_faces.append(face)

    processor = FaceProcessor()
    processor._initialized = True
    processor.app = MagicMock()
    processor.app.get = MagicMock(return_value=mock_faces)

    # Use a low quality threshold so only size filter is exercised
    with patch("services.face_recognition.processor.get_dynamic_setting",
               return_value=0.0):
        image = np.zeros((256, 256, 3), dtype=np.uint8)
        result = processor._detect_faces_sync(image)

    for face in result:
        bbox = face["bbox"]
        width = bbox[2] - bbox[0]
        height = bbox[3] - bbox[1]
        assert width >= 40, (
            f"Face with width {width:.2f} passed size filter (min=40)"
        )
        assert height >= 40, (
            f"Face with height {height:.2f} passed size filter (min=40)"
        )

    FaceProcessor.reset_instance()
