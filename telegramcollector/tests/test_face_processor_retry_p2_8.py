"""
Tests for P2.8: FaceProcessor._save_for_retry() orphaned directory fix.

Validates: Requirements 2.17 (Option A - Remove)
- _save_for_retry() method must NOT exist on FaceProcessor
- No failed_media_retry/ directory accumulation
- Failed media handled by DLQ (no file-based retry)

Fix Checking: Verify the bug condition (orphaned retry directory) cannot occur.
Preservation Checking: Verify normal processing still works correctly.
"""
import pytest
import inspect
import os
import sys

# Ensure project root is on path (conftest.py handles heavy mocks)
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


# ---------------------------------------------------------------------------
# Fix Checking Tests (F-017)
# ---------------------------------------------------------------------------

class TestSaveForRetryRemoved:
    """
    Validates: Requirements 2.17
    Fix Checking: _save_for_retry() must not exist on FaceProcessor.
    """

    def test_save_for_retry_method_does_not_exist(self):
        """_save_for_retry must be absent from FaceProcessor (Option A applied)."""
        from face_processor import FaceProcessor
        assert not hasattr(FaceProcessor, '_save_for_retry'), (
            "_save_for_retry() still exists on FaceProcessor. "
            "Option A requires removing it entirely to prevent orphaned file accumulation."
        )

    def test_no_failed_media_retry_method_variants(self):
        """No variant of the retry-file mechanism should exist."""
        from face_processor import FaceProcessor
        forbidden = ['_save_for_retry', 'save_for_retry', '_write_retry', '_retry_save']
        for name in forbidden:
            assert not hasattr(FaceProcessor, name), (
                f"Method '{name}' found on FaceProcessor — retry file mechanism must be removed."
            )

    def test_face_processor_has_no_retry_directory_writes(self):
        """
        Inspect FaceProcessor source for any reference to 'failed_media_retry'.
        No source code in face_processor.py should reference this directory.
        """
        from face_processor import FaceProcessor
        source = inspect.getsource(FaceProcessor)
        assert 'failed_media_retry' not in source, (
            "face_processor.py still references 'failed_media_retry' directory. "
            "All retry-file logic must be removed (Option A)."
        )

    def test_face_processor_module_has_no_retry_directory_writes(self):
        """
        Inspect the entire face_processor module for 'failed_media_retry' references.
        """
        import face_processor as fp_module
        source = inspect.getsource(fp_module)
        assert 'failed_media_retry' not in source, (
            "face_processor module still references 'failed_media_retry'. "
            "Option A requires removing all retry-file logic."
        )


class TestNoUnboundedFileAccumulation:
    """
    Validates: Requirements 2.17
    Fix Checking: Processing failures must not create files in failed_media_retry/.
    """

    def test_failed_media_retry_dir_not_created_on_import(self, tmp_path, monkeypatch):
        """
        Importing face_processor must not create a failed_media_retry/ directory.
        """
        monkeypatch.chdir(tmp_path)
        import importlib
        import face_processor
        importlib.reload(face_processor)

        retry_dir = tmp_path / 'failed_media_retry'
        assert not retry_dir.exists(), (
            "failed_media_retry/ directory was created on import — "
            "this indicates leftover retry-file logic."
        )

    def test_face_processor_instance_does_not_create_retry_dir(self, tmp_path, monkeypatch):
        """
        Instantiating FaceProcessor must not create failed_media_retry/.
        """
        monkeypatch.chdir(tmp_path)
        from face_processor import FaceProcessor
        _ = FaceProcessor()

        retry_dir = tmp_path / 'failed_media_retry'
        assert not retry_dir.exists(), (
            "failed_media_retry/ directory was created by FaceProcessor() constructor."
        )


# ---------------------------------------------------------------------------
# Preservation Checking Tests (F-017)
# ---------------------------------------------------------------------------

class TestFaceProcessorNormalOperationPreserved:
    """
    Validates: Requirements 3.14
    Preservation Checking: Normal processing must still work after removing _save_for_retry.
    """

    def test_face_processor_instantiates(self):
        """FaceProcessor can still be instantiated without errors."""
        from face_processor import FaceProcessor
        processor = FaceProcessor()
        assert processor is not None

    def test_face_processor_has_required_public_methods(self):
        """Core public API methods must still be present after the fix."""
        from face_processor import FaceProcessor
        required_methods = [
            'process_image',
            'process_frames',
            'get_embedding_vector',
            'calculate_similarity',
            'get_instance',
            'reset_instance',
        ]
        for method in required_methods:
            assert hasattr(FaceProcessor, method), (
                f"Required method '{method}' missing from FaceProcessor after P2.8 fix."
            )

    def test_calculate_similarity_still_works(self):
        """calculate_similarity must still function correctly."""
        from face_processor import FaceProcessor
        processor = FaceProcessor()

        emb1 = [1.0, 0.0, 0.0]
        emb2 = [1.0, 0.0, 0.0]
        similarity = processor.calculate_similarity(emb1, emb2)
        assert abs(similarity - 1.0) < 1e-6, "Identical embeddings should have similarity ~1.0"

    def test_calculate_similarity_orthogonal_vectors(self):
        """Orthogonal embeddings should have similarity ~0."""
        from face_processor import FaceProcessor
        processor = FaceProcessor()

        emb1 = [1.0, 0.0, 0.0]
        emb2 = [0.0, 1.0, 0.0]
        similarity = processor.calculate_similarity(emb1, emb2)
        assert abs(similarity) < 1e-6, "Orthogonal embeddings should have similarity ~0.0"

    def test_get_embedding_vector_returns_list(self):
        """get_embedding_vector must return the embedding list from a face dict."""
        from face_processor import FaceProcessor
        processor = FaceProcessor()

        face_dict = {'embedding': [0.1, 0.2, 0.3], 'quality': 0.9}
        result = processor.get_embedding_vector(face_dict)
        assert result == [0.1, 0.2, 0.3]

    def test_get_embedding_vector_missing_key_returns_empty(self):
        """get_embedding_vector must return [] when embedding key is absent."""
        from face_processor import FaceProcessor
        processor = FaceProcessor()

        result = processor.get_embedding_vector({})
        assert result == []

    def test_singleton_get_instance(self):
        """get_instance() must return the same singleton object."""
        from face_processor import FaceProcessor
        FaceProcessor.reset_instance()
        inst1 = FaceProcessor.get_instance()
        inst2 = FaceProcessor.get_instance()
        assert inst1 is inst2, "get_instance() must return the same singleton"
        FaceProcessor.reset_instance()

    def test_reset_instance_clears_singleton(self):
        """reset_instance() must clear the cached singleton."""
        from face_processor import FaceProcessor
        FaceProcessor.reset_instance()
        inst1 = FaceProcessor.get_instance()
        FaceProcessor.reset_instance()
        assert FaceProcessor._instance is None, "reset_instance() must set _instance to None"
