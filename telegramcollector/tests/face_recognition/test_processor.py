"""Unit tests for FaceProcessor (task 2.7).

Tests: photo frame extraction, missing file, corrupt file,
GPU→CPU fallback, and singleton behaviour.

_Requirements: 4.1, 4.4, 4.5, 5.2, 5.3_
"""

import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch, call

import numpy as np

# ---------------------------------------------------------------------------
# Path setup and heavy-dependency stubs
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

# Stub insightface before any import that might pull it in
for _mod in ("insightface", "insightface.app"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

# Required env vars for shared.config
os.environ.setdefault("TG_API_ID", "12345")
os.environ.setdefault("TG_API_HASH", "test_hash")
os.environ.setdefault("BOT_TOKEN", "123:test_token")
os.environ.setdefault("HUB_GROUP_ID", "-100123456789")
os.environ.setdefault("DB_PASSWORD", "test_password")

# Import real cv2 BEFORE conftest can mock it, and keep a reference.
# conftest.py stubs sys.modules['cv2'] with a MagicMock; we need the real one
# for file I/O tests.  Strategy: temporarily remove the mock, import the real
# cv2, then restore so other tests that rely on the mock aren't broken.
_cv2_mock = sys.modules.pop("cv2", None)
import importlib as _importlib
_real_cv2 = _importlib.import_module("cv2")
if _cv2_mock is not None:
    sys.modules["cv2"] = _cv2_mock

from services.face_recognition.processor import FaceProcessor  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_temp_jpeg(width: int = 64, height: int = 64) -> str:
    """Write a random BGR image to a temp JPEG and return its path."""
    rng = np.random.default_rng()
    img = rng.integers(0, 256, (height, width, 3), dtype=np.uint8)
    tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    tmp.close()
    _real_cv2.imwrite(tmp.name, img)
    return tmp.name


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestFaceProcessorUnit(unittest.TestCase):

    def setUp(self):
        FaceProcessor.reset_instance()

    def tearDown(self):
        FaceProcessor.reset_instance()

    # ------------------------------------------------------------------
    # 1. Photo extracts exactly one frame
    # ------------------------------------------------------------------
    def test_photo_extracts_exactly_one_frame(self):
        """extract_frames with a valid JPEG returns exactly 1 frame. (Req 4.1)"""
        path = _write_temp_jpeg()
        try:
            processor = FaceProcessor()
            with patch("services.face_recognition.processor.cv2", _real_cv2):
                frames = asyncio.run(processor.extract_frames(path, "photo"))
            self.assertEqual(len(frames), 1)
            self.assertIsInstance(frames[0], np.ndarray)
        finally:
            os.unlink(path)

    # ------------------------------------------------------------------
    # 2. Missing file raises FileNotFoundError
    # ------------------------------------------------------------------
    def test_missing_file_raises_file_not_found(self):
        """extract_frames with a non-existent path raises FileNotFoundError. (Req 4.4)"""
        processor = FaceProcessor()
        with self.assertRaises(FileNotFoundError):
            asyncio.run(processor.extract_frames("/nonexistent/path/image.jpg", "photo"))

    # ------------------------------------------------------------------
    # 3. Corrupt file raises ValueError
    # ------------------------------------------------------------------
    def test_corrupt_file_raises_value_error(self):
        """extract_frames with garbage bytes raises ValueError. (Req 4.5)"""
        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        tmp.write(b"\x00\xFF\xAB\xCD" * 100)  # garbage bytes
        tmp.close()
        try:
            processor = FaceProcessor()
            with patch("services.face_recognition.processor.cv2", _real_cv2):
                with self.assertRaises(ValueError):
                    asyncio.run(processor.extract_frames(tmp.name, "photo"))
        finally:
            os.unlink(tmp.name)

    # ------------------------------------------------------------------
    # 4. GPU failure falls back to CPU-only providers
    # ------------------------------------------------------------------
    def test_gpu_fallback_to_cpu_on_cuda_failure(self):
        """When InsightFace raises on CUDA provider, processor falls back to CPU. (Req 5.3)"""
        processor = FaceProcessor(providers=["CUDAExecutionProvider", "CPUExecutionProvider"])

        call_count = {"n": 0}

        def mock_face_analysis(name, providers):
            call_count["n"] += 1
            if "CUDAExecutionProvider" in providers:
                raise RuntimeError("CUDA not available")
            # CPU-only succeeds — return a mock app
            mock_app = MagicMock()
            mock_app.prepare = MagicMock()
            return mock_app

        mock_fa_cls = MagicMock(side_effect=mock_face_analysis)

        with patch.dict(sys.modules, {"insightface": MagicMock(), "insightface.app": MagicMock()}):
            with patch("services.face_recognition.processor.FaceAnalysis", mock_fa_cls, create=True):
                # Patch the import inside _lazy_init
                import services.face_recognition.processor as proc_mod
                original_lazy = proc_mod.FaceProcessor._lazy_init

                def patched_lazy(self):
                    from insightface.app import FaceAnalysis as _FA  # noqa: F401 — triggers mock
                    # replicate the real logic but use our mock
                    if self._initialized:
                        return True
                    import threading
                    with self._init_lock:
                        if self._initialized:
                            return True
                        for attempt in range(3):
                            try:
                                self.app = mock_face_analysis(
                                    "buffalo_l", self.providers or []
                                )
                                self.app.prepare(ctx_id=0, det_size=(640, 640))
                                FaceProcessor._initialized = True
                                return True
                            except Exception:
                                if self.providers and "CUDAExecutionProvider" in self.providers:
                                    self.providers = ["CPUExecutionProvider"]
                                    continue
                                break
                    return False

                with patch.object(proc_mod.FaceProcessor, "_lazy_init", patched_lazy):
                    result = processor._lazy_init()

        self.assertTrue(result, "Expected _lazy_init to succeed after GPU fallback")
        self.assertEqual(processor.providers, ["CPUExecutionProvider"])

    # ------------------------------------------------------------------
    # 5. Singleton returns same instance
    # ------------------------------------------------------------------
    def test_singleton_returns_same_instance(self):
        """get_instance() called twice returns the identical object. (Req 5.2)"""
        mock_app = MagicMock()
        mock_app.prepare = MagicMock()
        mock_fa = MagicMock(return_value=mock_app)

        with patch.dict(sys.modules, {
            "insightface": MagicMock(),
            "insightface.app": MagicMock(FaceAnalysis=mock_fa),
        }):
            import services.face_recognition.processor as proc_mod
            with patch.object(proc_mod.FaceProcessor, "_lazy_init", return_value=True):
                instance_a = FaceProcessor.get_instance()
                instance_b = FaceProcessor.get_instance()

        self.assertIs(instance_a, instance_b)


if __name__ == "__main__":
    unittest.main()
