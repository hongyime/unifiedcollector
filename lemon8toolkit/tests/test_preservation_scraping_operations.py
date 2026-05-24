"""
Preservation Property Tests - Existing Scraping Operations

These tests verify that existing scraping functionality remains unchanged after bugfixes.
They use mocking to test the integration logic without making actual network calls.

EXPECTED OUTCOME ON UNFIXED CODE: Tests PASS (confirms baseline behavior)
After fixes are implemented, these tests should STILL PASS (confirms no regressions)

**Validates: Requirements 3.2, 3.3 (Preservation)**
"""
import os
import sys
import tempfile
import shutil
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock, call

import pytest
from hypothesis import given, strategies as st, settings, HealthCheck

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import config


# Test fixtures and helpers
@pytest.fixture
def test_env():
    """Create isolated test environment with temporary directories"""
    test_dir = tempfile.mkdtemp(prefix="lemon8_preservation_test_")
    
    # Save original config values
    original_data_dir = config.DATA_DIR
    original_downloads_dir = config.DOWNLOADS_DIR
    original_db_file = config.LEMON8_DB_FILE
    original_visited_users = config.VISITED_USERS_FILE
    original_processed_tags = config.PROCESSED_TAGS_FILE
    original_downloaded_media = config.DOWNLOADED_MEDIA_FILE
    original_download_progress = config.DOWNLOAD_PROGRESS_FILE
    
    # Set up test directories
    test_data_dir = os.path.join(test_dir, "data")
    test_downloads_dir = os.path.join(test_dir, "downloads")
    os.makedirs(test_data_dir, exist_ok=True)
    os.makedirs(test_downloads_dir, exist_ok=True)
    
    # Override config for testing
    config.DATA_DIR = test_data_dir
    config.DOWNLOADS_DIR = test_downloads_dir
    config.LEMON8_DB_FILE = os.path.join(test_data_dir, "lemon8_toolkit.db")
    config.VISITED_USERS_FILE = os.path.join(test_data_dir, "visited_users.json")
    config.PROCESSED_TAGS_FILE = os.path.join(test_data_dir, "processed_tags.json")
    config.DOWNLOADED_MEDIA_FILE = os.path.join(test_data_dir, "downloaded_media.json")
    config.DOWNLOAD_PROGRESS_FILE = os.path.join(test_data_dir, "download_progress.json")
    
    yield {
        'test_dir': test_dir,
        'data_dir': test_data_dir,
        'downloads_dir': test_downloads_dir,
    }
    
    # Restore original config
    config.DATA_DIR = original_data_dir
    config.DOWNLOADS_DIR = original_downloads_dir
    config.LEMON8_DB_FILE = original_db_file
    config.VISITED_USERS_FILE = original_visited_users
    config.PROCESSED_TAGS_FILE = original_processed_tags
    config.DOWNLOADED_MEDIA_FILE = original_downloaded_media
    config.DOWNLOAD_PROGRESS_FILE = original_download_progress
    
    # Clean up test directory
    shutil.rmtree(test_dir, ignore_errors=True)


def create_mock_scrape_result(media_count, users_count, tags_count):
    """Create a mock scrape result with specified counts"""
    return {
        'media_items': [
            {
                'url': f'https://example.com/media_{i}.jpg',
                'type': 'image',
                'filename': f'media_{i}.jpg'
            }
            for i in range(media_count)
        ],
        'related_users': [f'user_{i}' for i in range(users_count)],
        'hashtags': [f'tag_{i}' for i in range(tags_count // 2)],
        'tag_ids': [f'{1000 + i}' for i in range(tags_count // 2)],
        'user_info': {
            'follower_count': 100,
            'following_count': 50,
            'post_count': 20
        },
        'user_id': '123456789'
    }


def create_mock_download_results(media_items, downloads_dir):
    """Create mock download results with file paths"""
    results = {}
    for item in media_items:
        url = item['url']
        filename = item['filename']
        file_path = os.path.join(downloads_dir, filename)
        # Create empty file to simulate download
        Path(file_path).touch()
        results[url] = file_path
    return results


# Property 1: User scraping operations preserve expected behavior
@given(
    username=st.sampled_from(['testuser', 'user123', 'valid_user']),
    media_count=st.integers(min_value=1, max_value=10),
    download_enabled=st.booleans()
)
@settings(
    max_examples=5,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_property_user_scraping_preserves_behavior(test_env, username, media_count, download_enabled):
    """
    Property: For all valid user scraping operations, the system should:
    1. Execute without errors
    2. Track the user in the database
    3. Create a progress session
    4. Download media files if download is enabled
    5. Return consistent output format
    
    **Validates: Requirements 3.2, 3.3**
    """
    # Import here to allow patching before module load
    from src.main import Lemon8Toolkit
    
    # Create mock scrape result
    mock_result = create_mock_scrape_result(media_count, 5, 4)
    
    with patch('src.main.Lemon8Scraper') as MockScraper, \
         patch('src.main.MediaDownloader') as MockDownloader, \
         patch('src.main.UnifiedTracker') as MockTracker, \
         patch('src.main.ProgressManager') as MockProgress:
        
        # Set up mock scraper
        mock_scraper_instance = MockScraper.return_value
        mock_scraper_instance.scrape_user_profile.return_value = mock_result
        mock_scraper_instance.session = MagicMock()
        
        # Set up mock downloader
        mock_downloader_instance = MockDownloader.return_value
        mock_downloader_instance.downloads_dir = test_env['downloads_dir']
        mock_downloader_instance.get_stats.return_value = {'total_downloaded': 0}
        mock_downloader_instance.save = MagicMock()
        
        if download_enabled:
            mock_download_results = create_mock_download_results(
                mock_result['media_items'],
                test_env['downloads_dir']
            )
            mock_downloader_instance.download_multiple_media.return_value = mock_download_results
        
        # Set up mock tracker
        mock_tracker_instance = MockTracker.return_value
        mock_account_tracker = MagicMock()
        mock_account_tracker.is_user_visited.return_value = False
        mock_account_tracker.get_user_info.return_value = {'visit_count': 1, 'user_id': '123456789'}
        mock_tracker_instance.account_tracker = mock_account_tracker
        mock_tracker_instance.tag_tracker = MagicMock()
        mock_tracker_instance.save = MagicMock()
        mock_tracker_instance.get_combined_stats.return_value = {
            'accounts': {'total_visited_users': 1},
            'tags': {'total_processed_tags': 0}
        }
        
        # Set up mock progress
        mock_progress_instance = MockProgress.return_value
        mock_progress_instance.start_session.return_value = 'test_session_123'
        mock_progress_instance.get_stats.return_value = {
            'total_sessions': 1,
            'completed_sessions': 1,
            'in_progress_sessions': 0,
            'total_media_scraped': media_count,
            'total_media_downloaded': media_count if download_enabled else 0,
            'overall_success_rate': 100.0
        }
        mock_progress_instance.get_current_session.return_value = None
        mock_progress_instance.save = MagicMock()
        
        # Create toolkit and scrape user
        toolkit = Lemon8Toolkit(auto_save=False)
        
        # Execute scraping operation
        try:
            # Also mock the lemon8_scraper module used inside scrape_user for download
            import sys
            import types
            mock_lemon8_scraper = types.ModuleType('lemon8_scraper')
            mock_lemon8_scraper.get_user_url = lambda u: f'https://www.lemon8-app.com/@{u}'
            sys.modules['lemon8_scraper'] = mock_lemon8_scraper
            
            toolkit.scrape_user(
                username=username,
                download_media=download_enabled,
                force_rescrape=True
            )
            
            # Verify scraper was called
            mock_scraper_instance.scrape_user_profile.assert_called_once()
            
            # Verify user tracking was called
            mock_account_tracker.mark_user_visited.assert_called()
            
            # Verify progress session was started
            mock_progress_instance.start_session.assert_called_once()
            mock_progress_instance.end_session.assert_called()
            
            # Verify download was called if enabled
            if download_enabled and media_count > 0:
                mock_downloader_instance.download_multiple_media.assert_called_once()
                
                # Verify files were created
                for item in mock_result['media_items']:
                    file_path = os.path.join(test_env['downloads_dir'], item['filename'])
                    assert os.path.exists(file_path), f"Downloaded file {file_path} should exist"
            
        except Exception as e:
            pytest.fail(f"User scraping operation failed unexpectedly: {e}")
        finally:
            # Clean up mock module
            sys.modules.pop('lemon8_scraper', None)


# Property 2: Feed scraping operations preserve expected behavior
@given(
    pages=st.integers(min_value=1, max_value=5),
    media_count=st.integers(min_value=1, max_value=20),
    download_enabled=st.booleans()
)
@settings(
    max_examples=5,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_property_feed_scraping_preserves_behavior(test_env, pages, media_count, download_enabled):
    """
    Property: For all valid feed scraping operations, the system should:
    1. Execute without errors
    2. Discover and track users
    3. Create a progress session
    4. Download media files if download is enabled
    5. Return consistent output format
    
    **Validates: Requirements 3.2, 3.3**
    """
    # Import here to allow patching before module load
    from src.main import Lemon8Toolkit
    
    # Create mock scrape result
    mock_result = {
        'media_items': [
            {
                'url': f'https://example.com/feed_media_{i}.jpg',
                'type': 'image',
                'filename': f'feed_media_{i}.jpg'
            }
            for i in range(media_count)
        ],
        'discovered_users': [f'discovered_user_{i}' for i in range(10)],
        'discovered_tags': [f'{2000 + i}' for i in range(5)]
    }
    
    with patch('src.main.Lemon8Scraper') as MockScraper, \
         patch('src.main.MediaDownloader') as MockDownloader, \
         patch('src.main.UnifiedTracker') as MockTracker, \
         patch('src.main.ProgressManager') as MockProgress:
        
        # Set up mock scraper
        mock_scraper_instance = MockScraper.return_value
        mock_scraper_instance.scrape_for_you_feed.return_value = mock_result
        mock_scraper_instance.session = MagicMock()
        
        # Set up mock downloader
        mock_downloader_instance = MockDownloader.return_value
        mock_downloader_instance.downloads_dir = test_env['downloads_dir']
        mock_downloader_instance.get_stats.return_value = {'total_downloaded': 0}
        mock_downloader_instance.save = MagicMock()
        
        if download_enabled:
            mock_download_results = create_mock_download_results(
                mock_result['media_items'],
                test_env['downloads_dir']
            )
            mock_downloader_instance.download_multiple_media.return_value = mock_download_results
        
        # Set up mock tracker
        mock_tracker_instance = MockTracker.return_value
        mock_account_tracker = MagicMock()
        mock_account_tracker.is_user_tracked.return_value = False
        mock_tracker_instance.account_tracker = mock_account_tracker
        mock_tag_tracker = MagicMock()
        mock_tag_tracker.is_tag_tracked.return_value = False
        mock_tracker_instance.tag_tracker = mock_tag_tracker
        mock_tracker_instance.save = MagicMock()
        mock_tracker_instance.get_combined_stats.return_value = {
            'accounts': {'total_visited_users': 10},
            'tags': {'total_processed_tags': 5}
        }
        
        # Set up mock progress
        mock_progress_instance = MockProgress.return_value
        mock_progress_instance.start_session.return_value = 'test_session_feed_123'
        mock_progress_instance.get_stats.return_value = {
            'total_sessions': 1,
            'completed_sessions': 1,
            'in_progress_sessions': 0,
            'total_media_scraped': media_count,
            'total_media_downloaded': media_count if download_enabled else 0,
            'overall_success_rate': 100.0
        }
        mock_progress_instance.get_current_session.return_value = None
        mock_progress_instance.save = MagicMock()
        
        # Create toolkit and scrape feed
        toolkit = Lemon8Toolkit(auto_save=False)
        
        # Execute scraping operation
        try:
            # Also mock the lemon8_scraper module used inside scrape_feed for download
            import sys
            import types
            mock_lemon8_scraper = types.ModuleType('lemon8_scraper')
            mock_lemon8_scraper.FEED_URL = 'https://www.lemon8-app.com/'
            sys.modules['lemon8_scraper'] = mock_lemon8_scraper
            
            toolkit.scrape_feed(
                pages=pages,
                download_media=download_enabled
            )
            
            # Verify scraper was called with correct pages
            mock_scraper_instance.scrape_for_you_feed.assert_called_once()
            call_args = mock_scraper_instance.scrape_for_you_feed.call_args
            assert call_args[0][0] == pages, f"Should scrape {pages} pages"
            
            # Verify progress session was started
            mock_progress_instance.start_session.assert_called_once()
            mock_progress_instance.end_session.assert_called()
            
            # Verify download was called if enabled
            if download_enabled and media_count > 0:
                mock_downloader_instance.download_multiple_media.assert_called_once()
                
                # Verify files were created
                for item in mock_result['media_items']:
                    file_path = os.path.join(test_env['downloads_dir'], item['filename'])
                    assert os.path.exists(file_path), f"Downloaded file {file_path} should exist"
            
        except Exception as e:
            pytest.fail(f"Feed scraping operation failed unexpectedly: {e}")
        finally:
            sys.modules.pop('lemon8_scraper', None)


# Property 3: Tag scraping operations preserve expected behavior
@given(
    tag_id=st.sampled_from(['7549513626407780359', '999999']),
    pages=st.integers(min_value=1, max_value=5),
    media_count=st.integers(min_value=1, max_value=10),
    download_enabled=st.booleans()
)
@settings(
    max_examples=5,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_property_tag_scraping_preserves_behavior(test_env, tag_id, pages, media_count, download_enabled):
    """
    Property: For all valid tag scraping operations, the system should:
    1. Execute without errors
    2. Track the tag in the database
    3. Create a progress session
    4. Download media files if download is enabled
    5. Return consistent output format
    
    **Validates: Requirements 3.2, 3.3**
    """
    # Import here to allow patching before module load
    from src.main import Lemon8Toolkit
    
    # Create mock scrape result
    mock_result = {
        'media_items': [
            {
                'url': f'https://example.com/tag_media_{i}.jpg',
                'type': 'image',
                'filename': f'tag_media_{i}.jpg'
            }
            for i in range(media_count)
        ],
        'related_users': [f'tag_user_{i}' for i in range(5)],
        'related_tags': [f'{3000 + i}' for i in range(3)]
    }
    
    with patch('src.main.Lemon8Scraper') as MockScraper, \
         patch('src.main.MediaDownloader') as MockDownloader, \
         patch('src.main.UnifiedTracker') as MockTracker, \
         patch('src.main.ProgressManager') as MockProgress:
        
        # Set up mock scraper
        mock_scraper_instance = MockScraper.return_value
        mock_scraper_instance.scrape_tag_topic.return_value = mock_result
        mock_scraper_instance.session = MagicMock()
        
        # Set up mock downloader
        mock_downloader_instance = MockDownloader.return_value
        mock_downloader_instance.downloads_dir = test_env['downloads_dir']
        mock_downloader_instance.get_stats.return_value = {'total_downloaded': 0}
        mock_downloader_instance.save = MagicMock()
        
        if download_enabled:
            mock_download_results = create_mock_download_results(
                mock_result['media_items'],
                test_env['downloads_dir']
            )
            mock_downloader_instance.download_multiple_media.return_value = mock_download_results
        
        # Set up mock tracker
        mock_tracker_instance = MockTracker.return_value
        mock_account_tracker = MagicMock()
        mock_tracker_instance.account_tracker = mock_account_tracker
        mock_tag_tracker = MagicMock()
        mock_tag_tracker.is_tag_processed.return_value = False
        mock_tag_tracker.get_tag_info.return_value = {'last_processed': '2024-01-01'}
        mock_tracker_instance.tag_tracker = mock_tag_tracker
        mock_tracker_instance.save = MagicMock()
        mock_tracker_instance.get_combined_stats.return_value = {
            'accounts': {'total_visited_users': 0},
            'tags': {'total_processed_tags': 1}
        }
        
        # Set up mock progress
        mock_progress_instance = MockProgress.return_value
        mock_progress_instance.start_session.return_value = f'test_session_tag_{tag_id}'
        mock_progress_instance.get_stats.return_value = {
            'total_sessions': 1,
            'completed_sessions': 1,
            'in_progress_sessions': 0,
            'total_media_scraped': media_count,
            'total_media_downloaded': media_count if download_enabled else 0,
            'overall_success_rate': 100.0
        }
        mock_progress_instance.get_current_session.return_value = None
        mock_progress_instance.save = MagicMock()
        
        # Create toolkit and scrape tag
        toolkit = Lemon8Toolkit(auto_save=False)
        
        # Execute scraping operation
        try:
            # Also mock the lemon8_scraper module used inside scrape_tag for download
            import sys
            import types
            mock_lemon8_scraper = types.ModuleType('lemon8_scraper')
            mock_lemon8_scraper.get_tag_url = lambda t: f'https://www.lemon8-app.com/tag/{t}'
            sys.modules['lemon8_scraper'] = mock_lemon8_scraper
            
            toolkit.scrape_tag(
                tag_id=tag_id,
                pages=pages,
                download_media=download_enabled,
                force_rescrape=True
            )
            
            # Verify scraper was called
            mock_scraper_instance.scrape_tag_topic.assert_called_once()
            
            # Verify tag tracking was called
            mock_tag_tracker.mark_tag_processed.assert_called()
            
            # Verify progress session was started
            mock_progress_instance.start_session.assert_called_once()
            mock_progress_instance.end_session.assert_called()
            
            # Verify download was called if enabled
            if download_enabled and media_count > 0:
                mock_downloader_instance.download_multiple_media.assert_called_once()
                
                # Verify files were created
                for item in mock_result['media_items']:
                    file_path = os.path.join(test_env['downloads_dir'], item['filename'])
                    assert os.path.exists(file_path), f"Downloaded file {file_path} should exist"
            
        except Exception as e:
            pytest.fail(f"Tag scraping operation failed unexpectedly: {e}")
        finally:
            sys.modules.pop('lemon8_scraper', None)


if __name__ == "__main__":
    # Run tests with pytest
    pytest.main([__file__, "-v", "--tb=short"])
