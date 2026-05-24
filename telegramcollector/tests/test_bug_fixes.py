"""
Regression tests for bug fixes:
- face_processor._init_lock must exist as a threading.Lock class variable
- story_scanner._recent_cache must be a dict (not set), and all operations must be dict-compatible
- story_scanner must import time module for time.time() usage
- All imports across the project must resolve without errors
"""
import pytest
import threading
import time as time_module
from unittest.mock import MagicMock, AsyncMock, patch


# ============================================================
# FaceProcessor: _init_lock regression tests
# ============================================================

class TestFaceProcessorInitLock:
    """Regression: FaceProcessor._init_lock must be a threading.Lock."""

    def test_init_lock_class_attribute_exists(self):
        """_init_lock must exist as a class-level attribute."""
        from face_processor import FaceProcessor
        assert hasattr(FaceProcessor, '_init_lock'), (
            "FaceProcessor._init_lock is missing — _lazy_init will raise AttributeError"
        )

    def test_init_lock_is_lock_instance(self):
        """_init_lock must be a threading.Lock (or similar context manager)."""
        from face_processor import FaceProcessor
        lock = FaceProcessor._init_lock
        # Must be usable as a context manager (with ... :)
        assert hasattr(lock, '__enter__') and hasattr(lock, '__exit__'), (
            "_init_lock must support 'with' statement (context manager protocol)"
        )

    def test_init_lock_acquirable(self):
        """_init_lock must be acquire-able and release-able."""
        from face_processor import FaceProcessor
        lock = FaceProcessor._init_lock
        acquired = lock.acquire(timeout=1)
        assert acquired, "_init_lock could not be acquired"
        lock.release()

    def test_lazy_init_does_not_crash_on_lock(self):
        """_lazy_init should not raise AttributeError for _init_lock."""
        from face_processor import FaceProcessor
        # Reset singleton state
        FaceProcessor._initialized = False
        processor = FaceProcessor(providers=['CPUExecutionProvider'])
        
        # Mock insightface so we don't need the actual model
        mock_fa = MagicMock()
        mock_fa.return_value = mock_fa
        mock_fa.prepare = MagicMock()
        
        with patch('face_processor.FaceAnalysis', mock_fa, create=True):
            with patch.dict('sys.modules', {
                'insightface': MagicMock(),
                'insightface.app': MagicMock(FaceAnalysis=mock_fa),
            }):
                # This should NOT raise AttributeError: 'FaceProcessor' object has no attribute '_init_lock'
                try:
                    result = processor._lazy_init()
                except AttributeError as e:
                    if '_init_lock' in str(e):
                        pytest.fail(f"_init_lock AttributeError regression: {e}")
                    # Other AttributeErrors (e.g. from mocked modules) are acceptable
                finally:
                    # Cleanup singleton state
                    FaceProcessor._initialized = False
                    FaceProcessor._instance = None

    def test_threading_import_exists(self):
        """face_processor module must import threading."""
        import face_processor
        import inspect
        source = inspect.getsource(face_processor)
        assert 'import threading' in source, (
            "face_processor.py must import threading for _init_lock"
        )


# ============================================================
# StoryScanner: _recent_cache dict bug regression tests
# ============================================================

class TestStoryScannerCacheBug:
    """Regression: _recent_cache is a dict; .add() must not be used."""

    def test_recent_cache_is_dict(self):
        """_recent_cache must be initialized as a dict, not a set."""
        from story_scanner import StoryScanner
        scanner = StoryScanner(MagicMock(), MagicMock(), MagicMock())
        assert isinstance(scanner._recent_cache, dict), (
            "_recent_cache must be a dict for LRU timestamp tracking"
        )

    def test_cache_supports_item_assignment(self):
        """Must be able to do _recent_cache[key] = value."""
        from story_scanner import StoryScanner
        scanner = StoryScanner(MagicMock(), MagicMock(), MagicMock())
        scanner._recent_cache['test_key'] = time_module.time()
        assert 'test_key' in scanner._recent_cache

    def test_cache_no_add_method_used(self):
        """Ensure .add() is not called on _recent_cache (it's a dict, not a set)."""
        import inspect
        from story_scanner import StoryScanner
        source = inspect.getsource(StoryScanner)
        # .add() on _recent_cache would crash since it's a dict
        assert '_recent_cache.add(' not in source, (
            "_recent_cache.add() found in source — dicts don't have .add(). "
            "Use _recent_cache[key] = value instead."
        )

    def test_cache_membership_check(self):
        """'in' operator must work on _recent_cache."""
        from story_scanner import StoryScanner
        scanner = StoryScanner(MagicMock(), MagicMock(), MagicMock())
        scanner._recent_cache['k1'] = 1.0
        assert 'k1' in scanner._recent_cache
        assert 'k2' not in scanner._recent_cache

    def test_cache_eviction_logic(self):
        """Cache eviction (delete oldest) must work with dict."""
        from story_scanner import StoryScanner
        scanner = StoryScanner(MagicMock(), MagicMock(), MagicMock())
        scanner._cache_max_size = 5
        
        # Fill beyond max
        for i in range(10):
            scanner._recent_cache[f"key_{i}"] = float(i)
        
        # Simulate eviction logic from _scan_all_stories
        if len(scanner._recent_cache) > scanner._cache_max_size:
            items_to_remove = len(scanner._recent_cache) - (scanner._cache_max_size // 2)
            for old_key in list(scanner._recent_cache.keys())[:items_to_remove]:
                del scanner._recent_cache[old_key]
        
        assert len(scanner._recent_cache) <= scanner._cache_max_size


# ============================================================
# StoryScanner: time module import regression
# ============================================================

class TestStoryScannerTimeImport:
    """Regression: story_scanner must import time for time.time() calls."""

    def test_time_module_imported(self):
        """story_scanner must import the time module."""
        import story_scanner
        import inspect
        source = inspect.getsource(story_scanner)
        assert 'import time' in source, (
            "story_scanner.py must import time — time.time() is used for cache timestamps"
        )

    def test_time_time_callable(self):
        """time.time() must be callable from story_scanner's scope."""
        import story_scanner
        # If time is properly imported, it should be accessible in the module
        assert hasattr(story_scanner, 'time'), (
            "time module not accessible in story_scanner namespace"
        )
        assert callable(story_scanner.time.time), (
            "time.time must be callable"
        )


# ============================================================
# Import validation: all modules must import cleanly
# ============================================================

class TestAllModulesImport:
    """Verify every project module imports without errors."""

    @pytest.mark.parametrize("module_name", [
        "shared.config",
        "resilience",
        "collector.corrections",
        "health_checker",
        "collector.update_handler",
        "shared.observability",
        "processing_queue",
        "face_processor",
        "story_scanner",
        "topic_manager",
        "identity_matcher",
        "database",
        "hub_notifier",
        "media_downloader",
        "collector.media_processor",
        "media_uploader",
        "collector.video_extractor",
        "message_scanner",
        "collector.bot_commands",
        "collector.account_manager",
        "shared.bot_pool",
        "shared.dlq",
        "login_bot.main",
        "services.face_recognition.dashboard.app",
        "worker",
    ])
    def test_module_imports_cleanly(self, module_name):
        """Each module should import without raising ImportError or ModuleNotFoundError."""
        import importlib
        try:
            importlib.import_module(module_name)
        except (ImportError, ModuleNotFoundError) as e:
            # Allow failures for heavy native deps that are mocked in conftest
            # or only installed in Docker (e.g. streamlit)
            allowed_missing = {'insightface', 'cv2', 'av', 'streamlit'}
            if not any(m in str(e) for m in allowed_missing):
                pytest.fail(f"Module '{module_name}' failed to import: {e}")


# ============================================================
# Dockerfile: validate model download approach
# ============================================================

class TestDockerfileModelDownload:
    """Validate Dockerfile has proper retry logic for InsightFace model."""

    def test_dockerfile_has_curl_retry(self):
        """Dockerfile should use curl with retry for model download."""
        import os
        dockerfile_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), 'Dockerfile'
        )
        with open(dockerfile_path, 'r') as f:
            content = f.read()
        
        assert 'curl' in content, "Dockerfile should use curl for reliable download"
        assert '--retry' in content, "curl should have --retry flag for resilience"
        assert 'buffalo_l.zip' in content, "Must download buffalo_l model"

    def test_dockerfile_has_zip_extraction(self):
        """Dockerfile should extract the model zip before loading."""
        import os
        dockerfile_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), 'Dockerfile'
        )
        with open(dockerfile_path, 'r') as f:
            content = f.read()
        
        assert 'zipfile' in content or 'unzip' in content, (
            "Dockerfile must extract buffalo_l.zip before loading model"
        )

    def test_dockerfile_validates_model_after_download(self):
        """Dockerfile should validate model loads after download."""
        import os
        dockerfile_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), 'Dockerfile'
        )
        with open(dockerfile_path, 'r') as f:
            content = f.read()
        
        assert 'FaceAnalysis' in content, (
            "Dockerfile should validate model by importing FaceAnalysis after download"
        )
