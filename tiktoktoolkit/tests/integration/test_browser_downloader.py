"""Unit tests for browser downloader module."""

import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch
from src.browser_downloader import BrowserDownloader, PLAYWRIGHT_AVAILABLE
from src.models import DownloadResult
from src import resilience


@pytest.fixture
def mock_playwright():
    """Mock Playwright browser and page objects."""
    with patch('src.browser_downloader.sync_playwright') as mock_pw:
        mock_playwright_instance = MagicMock()
        mock_pw.return_value.__enter__.return_value = mock_playwright_instance

        mock_browser = MagicMock()
        mock_playwright_instance.chromium.launch.return_value = mock_browser

        mock_context = MagicMock()
        mock_browser.new_context.return_value = mock_context
        mock_context.cookies.return_value = [{'name': 'sessionid', 'domain': '.tiktok.com'}]

        mock_page = MagicMock()
        mock_page.url = "https://www.tiktok.com/@testuser"
        mock_page.title.return_value = "@testuser - TikTok"
        mock_page.query_selector.return_value = True
        mock_context.new_page.return_value = mock_page

        yield {
            'playwright': mock_playwright_instance,
            'browser': mock_browser,
            'context': mock_context,
            'page': mock_page,
        }


@pytest.fixture
def temp_output_dir(tmp_path):
    """Create temporary output directory."""
    output_dir = tmp_path / "downloads"
    output_dir.mkdir()
    return output_dir


@pytest.fixture
def temp_cookies_file(tmp_path):
    """Create temporary cookies file."""
    cookies_file = tmp_path / "cookies.txt"
    cookies_file.write_text("""# Netscape HTTP Cookie File
.tiktok.com	TRUE	/	TRUE	9999999999	sessionid	test_session
""")
    return cookies_file


class TestBrowserDownloader:
    """Test suite for BrowserDownloader."""

    @pytest.mark.skipif(not PLAYWRIGHT_AVAILABLE, reason="Playwright not installed")
    def test_initialization(self):
        downloader = BrowserDownloader(headless=True, timeout=30)
        assert downloader.headless is True
        assert downloader.timeout == 30 * 1000
        assert downloader.user_data_dir is None

    @pytest.mark.skipif(not PLAYWRIGHT_AVAILABLE, reason="Playwright not installed")
    def test_initialization_with_user_data_dir(self, tmp_path):
        user_data_dir = tmp_path / "user_data"
        downloader = BrowserDownloader(user_data_dir=user_data_dir)
        assert downloader.user_data_dir == user_data_dir

    def test_parse_netscape_cookies(self, temp_cookies_file):
        if not PLAYWRIGHT_AVAILABLE:
            pytest.skip("Playwright not installed")

        downloader = BrowserDownloader()
        cookies = downloader._parse_netscape_cookies(temp_cookies_file)

        assert len(cookies) == 1
        assert cookies[0]['name'] == 'sessionid'
        assert cookies[0]['value'] == 'test_session'
        assert cookies[0]['domain'] == '.tiktok.com'

    def test_parse_netscape_cookies_with_comments(self, tmp_path):
        if not PLAYWRIGHT_AVAILABLE:
            pytest.skip("Playwright not installed")

        cookies_file = tmp_path / "cookies_with_comments.txt"
        cookies_file.write_text("""# Netscape HTTP Cookie File
# This is a comment

.tiktok.com	TRUE	/	TRUE	9999999999	sessionid	test_session

# Another comment
.tiktok.com	TRUE	/	TRUE	9999999999	sid_tt	test_sid
""")

        downloader = BrowserDownloader()
        cookies = downloader._parse_netscape_cookies(cookies_file)

        assert len(cookies) == 2
        assert cookies[0]['name'] == 'sessionid'
        assert cookies[1]['name'] == 'sid_tt'

    def test_has_login_cookies_detects_session_cookie(self):
        if not PLAYWRIGHT_AVAILABLE:
            pytest.skip("Playwright not installed")

        downloader = BrowserDownloader()
        assert downloader._has_login_cookies([
            {'name': 'ttwid', 'domain': '.tiktok.com'},
            {'name': 'sessionid', 'domain': '.tiktok.com'},
        ]) is True
        assert downloader._has_login_cookies([
            {'name': 'csrftoken', 'domain': '.tiktok.com'},
        ]) is False

    def test_extract_video_id_from_url(self):
        if not PLAYWRIGHT_AVAILABLE:
            pytest.skip("Playwright not installed")

        downloader = BrowserDownloader()
        url1 = "https://www.tiktok.com/@user/video/7123456789012345678"
        assert downloader._extract_video_id(url1) == "7123456789012345678"

        url2 = "https://www.tiktok.com/@user/video/7123456789012345678?is_from_webapp=1"
        assert downloader._extract_video_id(url2) == "7123456789012345678"

        url3 = "https://www.tiktok.com/@user"
        assert downloader._extract_video_id(url3) is None

    @pytest.mark.skipif(not PLAYWRIGHT_AVAILABLE, reason="Playwright not installed")
    def test_create_context_logs_missing_login_cookie_warning(self, tmp_path):
        cookies_file = tmp_path / "cookies.txt"
        cookies_file.write_text("""# Netscape HTTP Cookie File
.tiktok.com	TRUE	/	TRUE	9999999999	csrftoken	test_token
""")

        downloader = BrowserDownloader(headless=True, timeout=30)
        mock_browser = MagicMock()
        mock_context = MagicMock()
        mock_context.cookies.return_value = [{'name': 'csrftoken', 'domain': '.tiktok.com'}]
        mock_browser.new_context.return_value = mock_context

        with patch('src.browser_downloader.logger.warning') as mock_warning:
            context = downloader._create_context(mock_browser, cookies_file)

        assert context is mock_context
        assert mock_context.add_cookies.called
        assert any('no TikTok login cookies detected' in str(call.args[0]) for call in mock_warning.call_args_list)

    @pytest.mark.skipif(not PLAYWRIGHT_AVAILABLE, reason="Playwright not installed")
    def test_download_user_with_browser_mock(self, mock_playwright, temp_output_dir, temp_cookies_file):
        mock_page = mock_playwright['page']
        mock_video_element = MagicMock()
        mock_video_element.get_attribute.return_value = "https://www.tiktok.com/@user/video/7123456789012345678"
        mock_page.query_selector_all.return_value = [mock_video_element]
        mock_page.evaluate.return_value = "https://www.tiktok.com/@user/video/7123456789012345678"

        downloader = BrowserDownloader(headless=True, timeout=30)

        with patch.object(downloader, '_launch_browser', return_value=mock_playwright['browser']):
            with patch.object(downloader, '_create_context', return_value=mock_playwright['context']):
                with patch.object(downloader, '_download_video') as mock_download:
                    mock_download.return_value = DownloadResult(
                        ok=True,
                        url="https://www.tiktok.com/@user/video/7123456789012345678",
                        status='downloaded',
                        filepath=temp_output_dir / "video.mp4",
                        meta={'video_id': '7123456789012345678', 'size': 1024}
                    )

                    results = downloader.download_user_with_browser(
                        username="testuser",
                        limit=1,
                        output_dir=temp_output_dir,
                        cookies_file=temp_cookies_file
                    )

                    assert len(results) == 1
                    assert results[0].ok is True
                    assert results[0].status == 'downloaded'

    def test_download_user_without_playwright(self, temp_output_dir):
        with patch('src.browser_downloader.PLAYWRIGHT_AVAILABLE', False):
            from src.browser_downloader import BrowserDownloader as BD

            downloader = BD()
            results = downloader.download_user_with_browser(
                username="testuser",
                limit=1,
                output_dir=temp_output_dir
            )

            assert len(results) == 1
            assert results[0].ok is False
            assert 'Playwright not installed' in results[0].reason

    def test_download_user_with_browser_returns_shutdown_requested_when_already_stopping(self, temp_output_dir):
        downloader = BrowserDownloader(headless=True, timeout=30)
        resilience.reset_shutdown()
        resilience.signal_shutdown()
        try:
            results = downloader.download_user_with_browser(
                username="testuser",
                limit=1,
                output_dir=temp_output_dir
            )
        finally:
            resilience.reset_shutdown()

        assert len(results) == 1
        assert results[0].reason == 'Shutdown requested'


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
