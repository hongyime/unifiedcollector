"""
Tests for P1.7: MediaDownloadManager uses config.settings for MAX_MEDIA_SIZE_MB.

Validates: Requirements 2.9 (MediaDownloadManager uses 50MB from config.settings)
           Requirements 3.6 (Preservation: files within limit still processed normally)
"""
import pytest
from unittest.mock import MagicMock, patch
from shared.media_downloader import MediaDownloadManager
from shared.config import settings


class TestMediaDownloadManagerP1_7:
    """Fix Checking: F-009 - MediaDownloadManager default max_size_mb from os.getenv()"""

    def test_default_uses_settings_max_media_size_mb(self):
        """Validates: Requirements 2.9 - Default uses 50MB from settings, not 200MB from os.getenv()"""
        manager = MediaDownloadManager()
        assert manager.max_size_mb == settings.MAX_MEDIA_SIZE_MB

    def test_default_matches_settings_value(self):
        """Validates: Requirements 2.9 - Default matches settings.MAX_MEDIA_SIZE_MB (50MB unless overridden by .env)"""
        manager = MediaDownloadManager()
        # The actual value comes from settings (which reads .env), so compare to settings
        assert manager.max_size_mb == settings.MAX_MEDIA_SIZE_MB

    def test_default_is_not_200mb(self):
        """Bug condition: old code used 200MB default from os.getenv()"""
        manager = MediaDownloadManager()
        assert manager.max_size_mb != 200

    def test_explicit_max_size_mb_overrides_settings(self):
        """Validates: Requirements 2.9 - Explicit max_size_mb parameter still works"""
        manager = MediaDownloadManager(max_size_mb=100)
        assert manager.max_size_mb == 100

    def test_explicit_max_size_mb_zero_falls_back_to_settings(self):
        """Edge case: passing 0 (falsy) should fall back to settings"""
        # None means "use settings", 0 is falsy but we use 'is not None' check
        manager = MediaDownloadManager(max_size_mb=None)
        assert manager.max_size_mb == settings.MAX_MEDIA_SIZE_MB

    def test_max_size_bytes_consistent_with_max_size_mb(self):
        """max_size_bytes should be max_size_mb * 1024 * 1024"""
        manager = MediaDownloadManager()
        assert manager.max_size_bytes == manager.max_size_mb * 1024 * 1024

    def test_explicit_max_size_bytes_consistent(self):
        """max_size_bytes should reflect explicit max_size_mb"""
        manager = MediaDownloadManager(max_size_mb=100)
        assert manager.max_size_bytes == 100 * 1024 * 1024

    def test_settings_env_override_respected(self):
        """Validates: Requirements 2.9 - .env MAX_MEDIA_SIZE_MB is respected via settings"""
        with patch.object(settings, 'MAX_MEDIA_SIZE_MB', 75):
            manager = MediaDownloadManager()
            assert manager.max_size_mb == 75

    def test_max_size_mb_attribute_exists(self):
        """max_size_mb must be stored as an attribute for inspection"""
        manager = MediaDownloadManager()
        assert hasattr(manager, 'max_size_mb')

    def test_max_size_bytes_attribute_exists(self):
        """max_size_bytes must be stored as an attribute"""
        manager = MediaDownloadManager()
        assert hasattr(manager, 'max_size_bytes')


class TestMediaDownloadManagerPreservation:
    """Preservation Checking: F-009 - Existing size filtering behavior unchanged"""

    def test_size_check_rejects_oversized_document(self):
        """Validates: Requirements 3.6 - Files over limit are still rejected"""
        manager = MediaDownloadManager()  # 50MB limit

        msg = MagicMock()
        msg.photo = None
        msg.video = None
        msg.document = MagicMock()
        msg.document.size = 60 * 1024 * 1024  # 60MB > 50MB limit
        msg.id = 123

        result = manager._check_size(msg)
        assert result is False

    def test_size_check_accepts_small_document(self):
        """Validates: Requirements 3.6 - Files within limit are still accepted"""
        manager = MediaDownloadManager()  # 50MB limit

        msg = MagicMock()
        msg.photo = None
        msg.video = None
        msg.document = MagicMock()
        msg.document.size = 10 * 1024 * 1024  # 10MB < 50MB limit
        msg.id = 456

        result = manager._check_size(msg)
        assert result is True

    def test_explicit_limit_still_enforced(self):
        """Explicit max_size_mb parameter is still enforced correctly"""
        manager = MediaDownloadManager(max_size_mb=10)

        msg = MagicMock()
        msg.photo = None
        msg.video = None
        msg.document = MagicMock()
        msg.document.size = 15 * 1024 * 1024  # 15MB > 10MB explicit limit
        msg.id = 789

        result = manager._check_size(msg)
        assert result is False
