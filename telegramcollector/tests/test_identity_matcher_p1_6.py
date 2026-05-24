"""
Tests for P1.6 fix: IdentityMatcher uses config.settings for thresholds.

Validates Requirements 2.8 (bugfix.md):
  WHEN IdentityMatcher reads SIMILARITY_THRESHOLD and MIN_QUALITY_THRESHOLD
  THEN the system SHALL use the centralized Settings pydantic model from
  config.py instead of direct os.getenv() calls.

Bug condition F-008:
  RETURN X.similarity_threshold READS os.getenv()
      OR X.min_quality_threshold READS os.getenv()
"""
import inspect
import pytest
from unittest.mock import patch


# ============================================================
# Fix Checking (F-008): IdentityMatcher uses settings, not os.getenv()
# ============================================================

class TestIdentityMatcherUsesSettings:
    """
    Fix Checking (F-008): IdentityMatcher must read thresholds from settings.

    Validates Requirements 2.8 (bugfix.md)
    """

    def test_identity_matcher_imports_settings(self):
        """
        identity_matcher.py must import settings from config.

        Validates: Requirements 2.8
        """
        import identity_matcher
        source = inspect.getsource(identity_matcher)
        assert 'from shared.config import settings' in source, (
            "identity_matcher.py must import settings from config"
        )

    def test_identity_matcher_no_os_getenv_for_thresholds(self):
        """
        identity_matcher.py must NOT use os.getenv() for threshold values.

        Validates: Requirements 2.8
        """
        import identity_matcher
        source = inspect.getsource(identity_matcher)
        assert 'os.getenv' not in source, (
            "identity_matcher.py must not use os.getenv() — use settings instead"
        )

    def test_similarity_threshold_reads_from_settings(self):
        """
        IdentityMatcher.similarity_threshold must equal settings.SIMILARITY_THRESHOLD.

        Validates: Requirements 2.8
        """
        from shared.config import settings
        import identity_matcher

        matcher = identity_matcher.IdentityMatcher()
        assert matcher.similarity_threshold == settings.SIMILARITY_THRESHOLD, (
            "similarity_threshold must come from settings.SIMILARITY_THRESHOLD"
        )

    def test_min_quality_threshold_reads_from_settings(self):
        """
        IdentityMatcher.min_quality must equal settings.MIN_QUALITY_THRESHOLD.

        Validates: Requirements 2.8
        """
        from shared.config import settings
        import identity_matcher

        matcher = identity_matcher.IdentityMatcher()
        assert matcher.min_quality == settings.MIN_QUALITY_THRESHOLD, (
            "min_quality must come from settings.MIN_QUALITY_THRESHOLD"
        )

    def test_similarity_threshold_is_float(self):
        """
        similarity_threshold must be a float (valid threshold value).

        Validates: Requirements 2.8
        """
        import identity_matcher

        matcher = identity_matcher.IdentityMatcher()
        assert isinstance(matcher.similarity_threshold, float), (
            "similarity_threshold must be a float"
        )
        assert 0.0 < matcher.similarity_threshold <= 1.0, (
            "similarity_threshold must be in range (0, 1]"
        )

    def test_min_quality_threshold_is_float(self):
        """
        min_quality must be a float (valid threshold value).

        Validates: Requirements 2.8
        """
        import identity_matcher

        matcher = identity_matcher.IdentityMatcher()
        assert isinstance(matcher.min_quality, float), (
            "min_quality must be a float"
        )
        assert 0.0 < matcher.min_quality <= 1.0, (
            "min_quality must be in range (0, 1]"
        )


# ============================================================
# Preservation Checking (F-008): threshold behavior unchanged
# ============================================================

class TestIdentityMatcherThresholdPreservation:
    """
    Preservation Checking (F-008): threshold behavior must remain consistent.

    Validates Requirements 3.8 (bugfix.md)
    """

    def test_custom_similarity_threshold_via_env(self):
        """
        IdentityMatcher must respect SIMILARITY_THRESHOLD set via environment.

        Validates: Requirements 3.8
        """
        import importlib
        import os

        with patch.dict(os.environ, {'SIMILARITY_THRESHOLD': '0.75'}):
            import shared.config as cfg_module
            importlib.reload(cfg_module)

            import identity_matcher as im_module
            importlib.reload(im_module)

            matcher = im_module.IdentityMatcher()
            assert matcher.similarity_threshold == 0.75, (
                "IdentityMatcher must pick up SIMILARITY_THRESHOLD from .env"
            )

        # Restore original modules
        importlib.reload(cfg_module)
        importlib.reload(im_module)

    def test_custom_min_quality_threshold_via_env(self):
        """
        IdentityMatcher must respect MIN_QUALITY_THRESHOLD set via environment.

        Validates: Requirements 3.8
        """
        import importlib
        import os

        with patch.dict(os.environ, {'MIN_QUALITY_THRESHOLD': '0.80'}):
            import shared.config as cfg_module
            importlib.reload(cfg_module)

            import identity_matcher as im_module
            importlib.reload(im_module)

            matcher = im_module.IdentityMatcher()
            assert matcher.min_quality == 0.80, (
                "IdentityMatcher must pick up MIN_QUALITY_THRESHOLD from .env"
            )

        # Restore original modules
        importlib.reload(cfg_module)
        importlib.reload(im_module)

    def test_multiple_instances_share_same_threshold(self):
        """
        Multiple IdentityMatcher instances must use the same settings values.

        Validates: Requirements 3.8
        """
        import identity_matcher

        matcher1 = identity_matcher.IdentityMatcher()
        matcher2 = identity_matcher.IdentityMatcher()

        assert matcher1.similarity_threshold == matcher2.similarity_threshold
        assert matcher1.min_quality == matcher2.min_quality

    def test_topic_manager_injection_still_works(self):
        """
        Passing topic_manager to IdentityMatcher constructor still works after fix.

        Validates: Requirements 3.8
        """
        from unittest.mock import MagicMock
        import identity_matcher

        mock_tm = MagicMock()
        matcher = identity_matcher.IdentityMatcher(topic_manager=mock_tm)

        assert matcher.topic_manager is mock_tm
        # Thresholds still set correctly
        from shared.config import settings
        assert matcher.similarity_threshold == settings.SIMILARITY_THRESHOLD
        assert matcher.min_quality == settings.MIN_QUALITY_THRESHOLD
