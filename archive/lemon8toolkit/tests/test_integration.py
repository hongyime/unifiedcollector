import pytest
import requests
from unittest.mock import MagicMock, patch
import sys
from pathlib import Path

# Add parent directory and src folder to sys.path
_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_root / 'src'))
sys.path.insert(0, str(_root))

from scraper import Lemon8Scraper

@pytest.fixture
def scraper():
    return Lemon8Scraper()

@patch('requests.Session.get')
def test_scrape_user_profile_success(mock_get, scraper):
    # Mock response
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = """
    <html>
        <body>
            <script type="application/json">
                {"user": {"uniqueId": "test_user"}}
            </script>
            <img src="https://p16-va.lemon8cdn.com/tos-alisg-i-sdweummd6v-sg/image1.jpg">
            <video src="https://p16-va.lemon8cdn.com/tos-alisg-v-a3e477-sg/video1.mp4"></video>
            <a href="/@another_user">Related User</a>
        </body>
    </html>
    """
    mock_get.return_value = mock_response
    
    result = scraper.scrape_user_profile("test_user", use_api=False)
    
    assert result['username'] == "test_user"
    assert "https://p16-va.lemon8cdn.com/tos-alisg-i-sdweummd6v-sg/image1.jpg" in result['media_urls']
    assert "https://p16-va.lemon8cdn.com/tos-alisg-v-a3e477-sg/video1.mp4" in result['media_urls']
    assert "another_user" in result['related_users']

@patch('requests.Session.get')
def test_scrape_for_you_feed_success(mock_get, scraper):
    # Mock response
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = """
    <html>
        <body>
            <script type="application/json">
                {"items": [{"id": "1"}, {"id": "2"}], "cursor": "next_page_cursor"}
            </script>
            <img src="https://p16-va.lemon8cdn.com/tos-alisg-i-sdweummd6v-sg/feed_image.jpg">
        </body>
    </html>
    """
    mock_get.return_value = mock_response
    
    # We only scrape 1 page to avoid multiple calls in this simple test
    result = scraper.scrape_for_you_feed(pages=1, use_api=False)
    
    assert result['feed_type'] == "foryou"
    assert "https://p16-va.lemon8cdn.com/tos-alisg-i-sdweummd6v-sg/feed_image.jpg" in result['media_urls']
    assert result['total_media'] > 0

@patch('requests.Session.get')
def test_scrape_tag_topic_success(mock_get, scraper):
    # Mock response
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = """
    <html>
        <body>
            <script type="application/json">
                {"topicId": "123456789"}
            </script>
            <img src="https://p16-va.lemon8cdn.com/tos-alisg-i-sdweummd6v-sg/tag_image.jpg">
            <a href="/topic/987654321">Related Tag</a>
        </body>
    </html>
    """
    mock_get.return_value = mock_response
    
    result = scraper.scrape_tag_topic("123456789")
    
    assert result['tag_id'] == "123456789"
    assert "https://p16-va.lemon8cdn.com/tos-alisg-i-sdweummd6v-sg/tag_image.jpg" in result['media_urls']
    assert "987654321" in result['related_tags']
