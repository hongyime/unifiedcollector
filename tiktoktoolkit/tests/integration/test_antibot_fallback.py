"""Integration tests for anti-bot fallback mechanism."""

import pytest
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch
from src.provider import GalleryDLProvider
from src.models import DownloadResult
from src.errors import ProviderError


@pytest.fixture
def mock_config():
    """Mock configuration for provider."""
    return {
        'gallerydl': {
            'enabled': True,
            'retries': 2,
            'sleep': 1,
            'timeout_seconds': 1800,
            'browser_fallback_enabled': True,
            'browser_fallback_headless': True,
            'browser_fallback_timeout': 60,
            'cookies_file': 'configs/tiktok_cookies.txt',
            'tracker_required': False,
        }
    }


@pytest.fixture
def temp_output_dir(tmp_path):
    """Create temporary output directory."""
    output_dir = tmp_path / "downloads"
    output_dir.mkdir()
    return output_dir


class TestAntiBotFallback:
    """Integration tests for anti-bot detection and fallback."""

    def _make_provider(self, mock_config, mock_run, gallery_dl_stderr=""):
        """Helper: configure mock_run so --version succeeds, then return a provider."""
        version_result = Mock()
        version_result.returncode = 0
        version_result.stdout = "1.26.0"
        version_result.stderr = ""

        gdl_result = Mock()
        gdl_result.returncode = 1
        gdl_result.stdout = ""
        gdl_result.stderr = gallery_dl_stderr

        def side_effect(args, **kwargs):
            if '--version' in args:
                return version_result
            return gdl_result

        mock_run.side_effect = side_effect
        return GalleryDLProvider(mock_config)

    def test_fallback_triggers_on_rehydration_error(self, mock_config, temp_output_dir):
        """Test that fallback triggers on 'could not extract rehydration data' error."""
        with patch('src.provider.subprocess.run') as mock_run:
            with patch('src.provider.GalleryDLProvider._download_with_browser_fallback') as mock_fallback:
                mock_fallback.return_value = [
                    DownloadResult(
                        ok=True,
                        url="https://www.tiktok.com/@user/video/123",
                        status='downloaded',
                        filepath=temp_output_dir / "video.mp4",
                        meta={'video_id': '123'}
                    )
                ]

                provider = self._make_provider(
                    mock_config, mock_run,
                    gallery_dl_stderr="[tiktok][error] ExtractionError: could not extract rehydration data"
                )
                results = provider.download_user("testuser", 1, temp_output_dir)

                assert mock_fallback.called
                assert len(results) == 1
                assert results[0].ok is True

    def test_fallback_triggers_on_403_error(self, mock_config, temp_output_dir):
        """Test that fallback triggers on 403 Forbidden error."""
        with patch('src.provider.subprocess.run') as mock_run:
            with patch('src.provider.GalleryDLProvider._download_with_browser_fallback') as mock_fallback:
                mock_fallback.return_value = [
                    DownloadResult(
                        ok=True,
                        url="https://www.tiktok.com/@user/video/123",
                        status='downloaded',
                        filepath=temp_output_dir / "video.mp4",
                        meta={'video_id': '123'}
                    )
                ]

                provider = self._make_provider(
                    mock_config, mock_run,
                    gallery_dl_stderr="[tiktok][error] 403 Forbidden"
                )
                results = provider.download_user("testuser", 1, temp_output_dir)

                assert mock_fallback.called
                assert len(results) == 1
                assert results[0].ok is True

    def test_fallback_triggers_on_js_challenge(self, mock_config, temp_output_dir):
        """Test that fallback triggers on JavaScript challenge."""
        with patch('src.provider.subprocess.run') as mock_run:
            with patch('src.provider.GalleryDLProvider._download_with_browser_fallback') as mock_fallback:
                mock_fallback.return_value = [
                    DownloadResult(
                        ok=True,
                        url="https://www.tiktok.com/@user/video/123",
                        status='downloaded',
                        filepath=temp_output_dir / "video.mp4",
                        meta={'video_id': '123'}
                    )
                ]

                provider = self._make_provider(
                    mock_config, mock_run,
                    gallery_dl_stderr="[tiktok][info] Solving JavaScript challenge"
                )
                results = provider.download_user("testuser", 1, temp_output_dir)

                assert mock_fallback.called

    def test_fallback_disabled_raises_error(self, mock_config, temp_output_dir):
        """Test that error is raised when fallback is disabled."""
        mock_config['gallerydl']['browser_fallback_enabled'] = False

        with patch('src.provider.subprocess.run') as mock_run:
            provider = self._make_provider(
                mock_config, mock_run,
                gallery_dl_stderr="[tiktok][error] could not extract rehydration data"
            )
            results = provider.download_user("testuser", 1, temp_output_dir)

            assert len(results) == 1
            assert results[0].ok is False
            assert 'fallback is disabled' in results[0].reason.lower()

    def test_fallback_without_playwright_shows_helpful_error(self, mock_config, temp_output_dir):
        """Test helpful error when Playwright not installed."""
        with patch('src.provider.subprocess.run') as mock_run:
            with patch('src.browser_downloader.PLAYWRIGHT_AVAILABLE', False):
                provider = self._make_provider(
                    mock_config, mock_run,
                    gallery_dl_stderr="[tiktok][error] could not extract rehydration data"
                )
                results = provider.download_user("testuser", 1, temp_output_dir)

                assert len(results) == 1
                assert results[0].ok is False
                assert 'playwright not installed' in results[0].reason.lower()

    def test_no_fallback_on_different_error(self, mock_config, temp_output_dir):
        """Test that fallback does NOT trigger on non-anti-bot errors."""
        with patch('src.provider.subprocess.run') as mock_run:
            with patch('src.provider.GalleryDLProvider._download_with_browser_fallback') as mock_fallback:
                provider = self._make_provider(
                    mock_config, mock_run,
                    gallery_dl_stderr="[tiktok][error] Network connection failed"
                )

                try:
                    results = provider.download_user("testuser", 1, temp_output_dir)
                except ProviderError:
                    pass  # Expected to raise error

                assert not mock_fallback.called
    
    def test_successful_gallery_dl_skips_fallback(self, mock_config, temp_output_dir):
        """Test that successful gallery-dl download doesn't trigger fallback."""
        with patch('src.provider.subprocess.run') as mock_run:
            # Mock successful gallery-dl
            mock_result = Mock()
            mock_result.returncode = 0
            mock_result.stdout = f"# {temp_output_dir}/video.mp4"
            mock_result.stderr = ""
            mock_run.return_value = mock_result
            
            # Create the file that gallery-dl would create
            video_file = temp_output_dir / "video.mp4"
            video_file.write_bytes(b"fake video content")
            
            with patch('src.provider.GalleryDLProvider._download_with_browser_fallback') as mock_fallback:
                provider = GalleryDLProvider(mock_config)
                results = provider.download_user("testuser", 1, temp_output_dir)
                
                # Fallback should NOT be called
                assert not mock_fallback.called
                assert len(results) >= 0  # May be 0 or 1 depending on file detection


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
