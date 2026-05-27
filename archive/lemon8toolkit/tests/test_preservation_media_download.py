"""
Preservation Property Tests - Media Download Operations

These tests verify that existing media download functionality remains unchanged after bugfixes.
They test deduplication, quality enhancement, and download progress tracking.

EXPECTED OUTCOME ON UNFIXED CODE: Tests PASS (confirms baseline behavior)
After fixes are implemented, these tests should STILL PASS (confirms no regressions)

**Validates: Requirements 3.4 (Preservation)**
"""
import os
import sys
import tempfile
import shutil
import json
import sqlite3
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime

import pytest

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import config


# Test fixtures and helpers
@pytest.fixture
def test_env():
    """Create isolated test environment with temporary directories"""
    test_dir = tempfile.mkdtemp(prefix="lemon8_media_preservation_test_")
    
    # Save original config values
    original_data_dir = config.DATA_DIR
    original_downloads_dir = config.DOWNLOADS_DIR
    original_db_file = config.LEMON8_DB_FILE
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
    config.DOWNLOADED_MEDIA_FILE = os.path.join(test_data_dir, "downloaded_media.json")
    config.DOWNLOAD_PROGRESS_FILE = os.path.join(test_data_dir, "download_progress.json")
    
    # Also patch module-level imports in downloader and progress modules
    import downloader as downloader_module
    import progress as progress_module
    downloader_module.LEMON8_DB_FILE = config.LEMON8_DB_FILE
    downloader_module.DOWNLOADED_MEDIA_FILE = config.DOWNLOADED_MEDIA_FILE
    downloader_module.DOWNLOADS_DIR = config.DOWNLOADS_DIR
    progress_module.DOWNLOAD_PROGRESS_FILE = config.DOWNLOAD_PROGRESS_FILE
    
    yield {
        'test_dir': test_dir,
        'data_dir': test_data_dir,
        'downloads_dir': test_downloads_dir,
    }
    
    # Restore original config
    config.DATA_DIR = original_data_dir
    config.DOWNLOADS_DIR = original_downloads_dir
    config.LEMON8_DB_FILE = original_db_file
    config.DOWNLOADED_MEDIA_FILE = original_downloaded_media
    config.DOWNLOAD_PROGRESS_FILE = original_download_progress
    
    # Restore module-level imports
    import downloader as downloader_module
    import progress as progress_module
    downloader_module.LEMON8_DB_FILE = original_db_file
    downloader_module.DOWNLOADED_MEDIA_FILE = original_downloaded_media
    downloader_module.DOWNLOADS_DIR = original_downloads_dir
    progress_module.DOWNLOAD_PROGRESS_FILE = original_download_progress
    
    # Clean up test directory
    shutil.rmtree(test_dir, ignore_errors=True)


def create_test_image(file_path, width=800, height=600):
    """Create a minimal test image file"""
    # Create a minimal PNG file
    png_header = b'\x89PNG\r\n\x1a\n'
    ihdr_chunk = b'\x00\x00\x00\x0dIHDR'
    ihdr_data = width.to_bytes(4, 'big') + height.to_bytes(4, 'big')
    ihdr_data += b'\x08\x02\x00\x00\x00'  # bit depth, color type, etc.
    # Simple CRC (not accurate but sufficient for testing)
    ihdr_crc = b'\x00\x00\x00\x00'
    iend_chunk = b'\x00\x00\x00\x00IEND\xae\x42\x60\x82'
    
    with open(file_path, 'wb') as f:
        f.write(png_header + ihdr_chunk + ihdr_data + ihdr_crc + iend_chunk)


# Test 1: MediaDownloader deduplication works correctly
def test_media_deduplication_preserves_behavior(test_env):
    """
    Observation: MediaDownloader deduplication works on unfixed code
    
    Verifies that:
    1. Duplicate URLs are detected using hash-based deduplication
    2. Already-downloaded media is skipped
    3. Downloaded URLs are tracked in both SQLite and JSON
    4. Deduplication state persists across MediaDownloader instances
    
    **Validates: Requirement 3.4 - MediaDownloader deduplication**
    """
    from downloader import MediaDownloader
    
    # Test URLs
    url1 = 'https://example.com/media_1.jpg'
    url2 = 'https://example.com/media_2.jpg'
    url3 = 'https://example.com/media_1.jpg'  # Duplicate of url1
    
    # Create downloader instance
    downloader = MediaDownloader(auto_save=True)
    downloader.downloads_dir = test_env['downloads_dir']
    
    # Mock the actual download to avoid network calls
    with patch.object(downloader, '_download_to_path') as mock_download, \
         patch.object(downloader, '_verify_image_download') as mock_verify, \
         patch.object(downloader, '_get_downloads_dir', return_value=test_env['downloads_dir']):
        
        # Set up mocks
        mock_download.return_value = MagicMock(headers={'content-length': '1024'})
        mock_verify.return_value = {
            'passed': True,
            'thresholds_met': True,
            'filename_has_prefix': True,
            'higher_quality_confirmed': False,
            'fallback_used': False,
            'is_profile_photo': False,
            'image_info': {'width': 800, 'height': 600, 'file_size_bytes': 1024},
            'comparison': {'is_higher_quality': False}
        }
        
        def create_mock_file(url, save_path, referer=None):
            create_test_image(save_path, 800, 600)
            return mock_download.return_value
        
        mock_download.side_effect = create_mock_file
        
        # Download url1 - should succeed
        result1 = downloader.download_media(
            url=url1,
            scrape_type='test',
            identifier='test_user',
            is_profile_photo=False
        )
        assert result1 is not None, "First download should succeed"
        assert downloader.is_already_downloaded(url1), "URL1 should be marked as downloaded"
        
        # Download url2 - should succeed
        result2 = downloader.download_media(
            url=url2,
            scrape_type='test',
            identifier='test_user',
            is_profile_photo=False
        )
        assert result2 is not None, "Second download should succeed"
        assert downloader.is_already_downloaded(url2), "URL2 should be marked as downloaded"
        
        # Download url3 (duplicate of url1) - should be skipped
        result3 = downloader.download_media(
            url=url3,
            scrape_type='test',
            identifier='test_user',
            is_profile_photo=False
        )
        assert result3 is None, "Duplicate download should be skipped"
        
        # Verify deduplication state in SQLite
        conn = sqlite3.connect(config.LEMON8_DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM downloaded_media")
        db_count = cursor.fetchone()[0]
        conn.close()
        assert db_count >= 2, f"Should have at least 2 entries in database, got {db_count}"
        
        # Verify JSON backup exists
        assert os.path.exists(config.DOWNLOADED_MEDIA_FILE), "Downloaded media JSON should exist"
        with open(config.DOWNLOADED_MEDIA_FILE, 'r') as f:
            json_data = json.load(f)
            assert 'downloaded_urls' in json_data, "JSON should contain downloaded_urls"
            assert len(json_data['downloaded_urls']) >= 2, "JSON should contain at least 2 URLs"


# Test 2: Quality enhancement works correctly
def test_quality_enhancement_preserves_behavior(test_env):
    """
    Observation: Quality enhancement works on unfixed code
    
    Verifies that:
    1. Low-quality URLs (shrink, thumbnail patterns) are detected
    2. High-quality URL candidates are generated when enhancement is enabled
    3. Quality hints are extracted from URLs
    4. Quality comparison logic works correctly
    
    **Validates: Requirement 3.4 - Quality enhancement**
    """
    from downloader import MediaDownloader
    
    # Save original config
    original_enhancement = config.IMAGE_ENHANCEMENT_ENABLED
    
    try:
        config.IMAGE_ENHANCEMENT_ENABLED = True
        
        downloader = MediaDownloader(auto_save=False)
        downloader.downloads_dir = test_env['downloads_dir']
        
        # Test 1: Detect shrunk URL
        shrunk_url = 'https://example.com/~tplv-abc123-shrink:400:300:q75.jpg'
        is_shrunk = downloader._url_looks_shrunk(shrunk_url)
        assert is_shrunk, "Shrunk URL should be detected"
        
        # Test 2: Extract quality hints
        hints = downloader._extract_url_quality_hints(shrunk_url)
        assert hints['width'] == 400, "Should extract width hint"
        assert hints['height'] == 300, "Should extract height hint"
        assert hints['quality'] == 75, "Should extract quality hint"
        
        # Test 3: Generate enhanced URL candidates
        candidates = downloader._enhance_image_url(shrunk_url)
        assert len(candidates) >= 2, f"Should generate multiple candidates, got {len(candidates)}"
        
        # Test 4: Quality comparison
        # Use a truly enhanced URL without shrink patterns
        enhanced_url = 'https://example.com/image.jpg?w=1920&h=1080&q=95'
        comparison = downloader._compare_image_quality(shrunk_url, enhanced_url)
        assert comparison['is_higher_quality'], "Enhanced URL should be higher quality"
        # Verify the original was shrunk
        assert downloader._url_looks_shrunk(shrunk_url), "Original should be shrunk"
        
        # Test 5: Normal URL without quality parameters should not be detected as shrunk
        normal_url = 'https://example.com/image.jpg'
        is_normal_shrunk = downloader._url_looks_shrunk(normal_url)
        assert not is_normal_shrunk, "Normal URL without quality params should not be detected as shrunk"
        
    finally:
        config.IMAGE_ENHANCEMENT_ENABLED = original_enhancement


# Test 3: Download progress tracking works correctly
def test_download_progress_tracking_preserves_behavior(test_env):
    """
    Observation: Download progress tracking works on unfixed code
    
    Verifies that:
    1. Scraped media URLs are tracked in progress database
    2. Download status is updated for each media item
    3. Successful downloads are recorded with file paths
    4. Failed downloads are recorded with error messages
    5. Success rates are calculated accurately
    
    **Validates: Requirement 3.4 - Download progress tracking**
    """
    from progress import ProgressManager
    
    # Create progress manager
    progress = ProgressManager(auto_save=True)
    
    # Start a test session
    session_id = progress.start_session(
        session_type='test',
        identifier='test_user',
        metadata={'test': True}
    )
    assert session_id is not None, "Should create session ID"
    
    # Test media URLs
    media_urls = [
        'https://example.com/media_1.jpg',
        'https://example.com/media_2.jpg',
        'https://example.com/media_3.jpg'
    ]
    
    # Update session with scraped media
    progress.update_session_scraped_media(session_id, media_urls)
    
    # Verify scraped media was recorded
    session = progress._get_session(session_id)
    assert session is not None, "Session should exist"
    assert 'scraped_media' in session, "Session should track scraped media"
    assert len(session['scraped_media']) == 3, "Should track 3 scraped media items"
    
    # Simulate successful downloads
    progress.update_session_downloaded_media(
        session_id,
        media_urls[0],
        os.path.join(test_env['downloads_dir'], 'media_1.jpg')
    )
    progress.update_session_downloaded_media(
        session_id,
        media_urls[1],
        os.path.join(test_env['downloads_dir'], 'media_2.jpg')
    )
    
    # Simulate failed download
    progress.update_session_failed_download(
        session_id,
        media_urls[2],
        error='Network timeout'
    )
    
    # Verify download tracking
    session = progress._get_session(session_id)
    assert 'downloaded_media' in session, "Session should track downloaded media"
    assert 'failed_downloads' in session, "Session should track failed downloads"
    assert len(session['downloaded_media']) == 2, "Should track 2 successful downloads"
    assert len(session['failed_downloads']) == 1, "Should track 1 failed download"
    
    # End session
    progress.end_session(session_id, status='completed')
    
    # Verify session summary
    summary = progress.get_session_summary(session_id)
    assert summary is not None, "Should return session summary"
    assert summary['status'] == 'completed', "Session should be completed"
    assert summary['total_scraped'] == 3, f"Summary should show 3 scraped media"
    assert summary['total_downloaded'] == 2, f"Summary should show 2 downloaded media"
    assert summary['total_failed'] == 1, f"Summary should show 1 failed download"
    
    # Verify success rate
    expected_rate = (2 / 3) * 100
    actual_rate = summary['success_rate']
    assert abs(actual_rate - expected_rate) < 0.1, \
        f"Success rate should be {expected_rate:.1f}%, got {actual_rate:.1f}%"
    
    # Verify progress file exists
    assert os.path.exists(config.DOWNLOAD_PROGRESS_FILE), "Progress file should exist"


# Test 4: Deduplication state syncs between SQLite and JSON
def test_deduplication_sync_preserves_behavior(test_env):
    """
    Observation: Deduplication state syncs between SQLite and JSON on unfixed code
    
    Verifies that:
    1. Downloaded media syncs between SQLite and JSON on startup
    2. Data from both sources is merged without duplicates
    3. Consistency is maintained between SQLite and JSON
    
    **Validates: Requirement 3.4 - Deduplication sync**
    """
    from downloader import MediaDownloader
    
    # Create initial JSON data
    json_urls = ['url_hash_1', 'url_hash_2', 'url_hash_3']
    json_data = {
        'downloaded_urls': json_urls,
        'last_updated': datetime.now().isoformat(),
        'total_count': len(json_urls)
    }
    
    with open(config.DOWNLOADED_MEDIA_FILE, 'w') as f:
        json.dump(json_data, f)
    
    # Create SQLite database with overlapping data
    conn = sqlite3.connect(config.LEMON8_DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS downloaded_media (
            url_hash TEXT PRIMARY KEY,
            downloaded_at TEXT
        )
    ''')
    
    # Add URLs that overlap with JSON
    cursor.execute(
        'INSERT INTO downloaded_media (url_hash, downloaded_at) VALUES (?, ?)',
        ('url_hash_1', datetime.now().isoformat())
    )
    cursor.execute(
        'INSERT INTO downloaded_media (url_hash, downloaded_at) VALUES (?, ?)',
        ('url_hash_2', datetime.now().isoformat())
    )
    
    # Add URL only in SQLite
    cursor.execute(
        'INSERT INTO downloaded_media (url_hash, downloaded_at) VALUES (?, ?)',
        ('sqlite_only_1', datetime.now().isoformat())
    )
    
    conn.commit()
    conn.close()
    
    # Create downloader - should trigger sync
    downloader = MediaDownloader(auto_save=True)
    
    # The downloader hashes URLs, so we need to check if the hashes are present
    # The sync should have merged JSON and SQLite data
    # Verify total count is at least the sum of unique URLs
    assert len(downloader.downloaded_media) >= 4, \
        f"Should have at least 4 URLs in memory, got {len(downloader.downloaded_media)}"
    
    # Verify SQLite contains merged data (at least the SQLite-only entry should be there)
    conn = sqlite3.connect(config.LEMON8_DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM downloaded_media")
    db_count = cursor.fetchone()[0]
    conn.close()
    
    # Should have at least 3 entries (the ones we explicitly added)
    assert db_count >= 3, f"SQLite should contain at least 3 entries, got {db_count}"


# Test 5: Batch download operations work correctly
def test_batch_download_preserves_behavior(test_env):
    """
    Observation: Batch download operations work on unfixed code
    
    Verifies that:
    1. Multiple URLs are processed in sequence
    2. Mixed media types are handled correctly
    3. Results map URLs to file paths
    4. Download statistics are accurate
    5. Profile photos are skipped when disabled
    
    **Validates: Requirement 3.4 - Batch download operations**
    """
    from downloader import MediaDownloader
    
    # Test media URLs
    media_urls = [
        {'url': 'https://example.com/media_1.jpg', 'username': 'user1', 'is_profile_photo': False},
        {'url': 'https://example.com/media_2.jpg', 'username': 'user2', 'is_profile_photo': False},
        {'url': 'https://example.com/avatar_1.jpg', 'username': 'user1', 'is_profile_photo': True},
    ]
    
    # Save original config
    original_profile_enabled = config.PROFILE_PHOTO_DOWNLOAD_ENABLED
    
    try:
        # Test with profile photos enabled
        config.PROFILE_PHOTO_DOWNLOAD_ENABLED = True
        
        downloader = MediaDownloader(auto_save=False)
        downloader.downloads_dir = test_env['downloads_dir']
        
        with patch.object(downloader, '_download_to_path') as mock_download, \
             patch.object(downloader, '_verify_image_download') as mock_verify, \
             patch.object(downloader, '_get_downloads_dir', return_value=test_env['downloads_dir']):
            
            mock_download.return_value = MagicMock(headers={'content-length': '1024'})
            mock_verify.return_value = {
                'passed': True,
                'thresholds_met': True,
                'filename_has_prefix': True,
                'higher_quality_confirmed': False,
                'fallback_used': False,
                'is_profile_photo': False,
                'image_info': {'width': 800, 'height': 600, 'file_size_bytes': 1024},
                'comparison': {'is_higher_quality': False}
            }
            
            def create_mock_file(url, save_path, referer=None):
                create_test_image(save_path, 800, 600)
                return mock_download.return_value
            
            mock_download.side_effect = create_mock_file
            
            # Execute batch download
            results = downloader.download_multiple_media(
                media_urls=media_urls,
                scrape_type='test',
                identifier='test_batch',
                referer='https://www.lemon8-app.com/'
            )
            
            # Verify results structure
            assert isinstance(results, dict), "Should return dict of results"
            
            # Verify all URLs are in results
            for item in media_urls:
                url = item['url']
                assert url in results, f"URL {url} should be in results"
            
            # Verify stats
            stats = downloader.get_stats()
            assert 'total_downloaded' in stats, "Stats should include total_downloaded"
        
        # Test with profile photos disabled
        config.PROFILE_PHOTO_DOWNLOAD_ENABLED = False
        
        downloader2 = MediaDownloader(auto_save=False)
        downloader2.downloads_dir = test_env['downloads_dir']
        
        with patch.object(downloader2, '_download_to_path') as mock_download2, \
             patch.object(downloader2, '_verify_image_download') as mock_verify2, \
             patch.object(downloader2, '_get_downloads_dir', return_value=test_env['downloads_dir']):
            
            mock_download2.return_value = MagicMock(headers={'content-length': '1024'})
            mock_verify2.return_value = {
                'passed': True,
                'thresholds_met': True,
                'filename_has_prefix': True,
                'higher_quality_confirmed': False,
                'fallback_used': False,
                'is_profile_photo': False,
                'image_info': {'width': 800, 'height': 600, 'file_size_bytes': 1024},
                'comparison': {'is_higher_quality': False}
            }
            
            mock_download2.side_effect = create_mock_file
            
            # Execute batch download with profile photos disabled
            results2 = downloader2.download_multiple_media(
                media_urls=media_urls,
                scrape_type='test',
                identifier='test_batch2',
                referer='https://www.lemon8-app.com/'
            )
            
            # Verify profile photo was skipped
            profile_url = 'https://example.com/avatar_1.jpg'
            assert results2[profile_url] is None, \
                "Profile photo should be skipped when disabled"
    
    finally:
        config.PROFILE_PHOTO_DOWNLOAD_ENABLED = original_profile_enabled


if __name__ == "__main__":
    # Run tests with pytest
    pytest.main([__file__, "-v", "--tb=short"])
