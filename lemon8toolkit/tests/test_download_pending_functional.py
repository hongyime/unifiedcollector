"""
Functional Test for download_pending_media method

This test verifies that the download_pending_media method correctly:
1. Queries the progress database for sessions with scraped but not downloaded media
2. Downloads pending media using MediaDownloader
3. Updates the progress database with download results
"""
import os
import sys
import json
import tempfile
import shutil
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from src.main import Lemon8Toolkit
from progress import ProgressManager


def test_download_pending_media_no_pending():
    """Test download_pending_media when there are no pending media"""
    # Create a temporary directory for test data
    with tempfile.TemporaryDirectory() as temp_dir:
        # Create a mock progress file with no pending media
        progress_file = Path(temp_dir) / "download_progress.json"
        progress_data = {
            "sessions": [
                {
                    "session_id": "test_session_1",
                    "session_type": "user",
                    "identifier": "testuser",
                    "start_time": "2024-01-01T00:00:00",
                    "end_time": "2024-01-01T00:01:00",
                    "status": "completed",
                    "scraped_media": ["https://example.com/media1.jpg"],
                    "downloaded_media": [
                        {
                            "url": "https://example.com/media1.jpg",
                            "file_path": "/path/to/media1.jpg",
                            "download_time": "2024-01-01T00:00:30"
                        }
                    ],
                    "failed_downloads": [],
                    "total_scraped": 1,
                    "total_downloaded": 1,
                    "total_failed": 0,
                    "metadata": {}
                }
            ],
            "current_session": None
        }
        
        with open(progress_file, 'w') as f:
            json.dump(progress_data, f)
        
        # Patch the config to use our temp directory
        with patch('config.DOWNLOAD_PROGRESS_FILE', str(progress_file)):
            with patch('config.ensure_data_directory'):
                # Create toolkit instance
                toolkit = Lemon8Toolkit(auto_save=False)
                
                # Call download_pending_media
                # This should print "No pending media found"
                toolkit.download_pending_media(limit=10)
                
                # Verify no changes were made to the progress file
                with open(progress_file, 'r') as f:
                    updated_data = json.load(f)
                
                assert len(updated_data['sessions']) == 1
                assert updated_data['sessions'][0]['total_downloaded'] == 1


def test_download_pending_media_with_pending():
    """Test download_pending_media when there are pending media"""
    # Create a temporary directory for test data
    with tempfile.TemporaryDirectory() as temp_dir:
        # Create a mock progress file with pending media
        progress_file = Path(temp_dir) / "download_progress.json"
        progress_data = {
            "sessions": [
                {
                    "session_id": "test_session_2",
                    "session_type": "user",
                    "identifier": "testuser",
                    "start_time": "2024-01-01T00:00:00",
                    "end_time": "2024-01-01T00:01:00",
                    "status": "completed",
                    "scraped_media": [
                        "https://example.com/media1.jpg",
                        "https://example.com/media2.jpg"
                    ],
                    "downloaded_media": [
                        {
                            "url": "https://example.com/media1.jpg",
                            "file_path": "/path/to/media1.jpg",
                            "download_time": "2024-01-01T00:00:30"
                        }
                    ],
                    "failed_downloads": [],
                    "total_scraped": 2,
                    "total_downloaded": 1,
                    "total_failed": 0,
                    "metadata": {}
                }
            ],
            "current_session": None
        }
        
        with open(progress_file, 'w') as f:
            json.dump(progress_data, f)
        
        # Patch the config to use our temp directory
        with patch('config.DOWNLOAD_PROGRESS_FILE', str(progress_file)):
            with patch('config.ensure_data_directory'):
                # Create toolkit instance
                toolkit = Lemon8Toolkit(auto_save=False)
                
                # Mock the downloader methods
                toolkit.downloader.is_already_downloaded = Mock(return_value=False)
                toolkit.downloader.download_media = Mock(return_value="/path/to/media2.jpg")
                
                # Call download_pending_media
                toolkit.download_pending_media(limit=10)
                
                # Verify the downloader was called
                assert toolkit.downloader.download_media.called
                
                # Verify the download_media was called with correct arguments
                call_args = toolkit.downloader.download_media.call_args
                assert call_args[1]['url'] == "https://example.com/media2.jpg"
                assert call_args[1]['scrape_type'] == "user"
                assert call_args[1]['identifier'] == "testuser"


def test_download_pending_media_respects_limit():
    """Test that download_pending_media respects the limit parameter"""
    # Create a temporary directory for test data
    with tempfile.TemporaryDirectory() as temp_dir:
        # Create a mock progress file with many pending media
        progress_file = Path(temp_dir) / "download_progress.json"
        
        # Create 20 scraped media URLs
        scraped_media = [f"https://example.com/media{i}.jpg" for i in range(20)]
        
        progress_data = {
            "sessions": [
                {
                    "session_id": "test_session_3",
                    "session_type": "user",
                    "identifier": "testuser",
                    "start_time": "2024-01-01T00:00:00",
                    "end_time": "2024-01-01T00:01:00",
                    "status": "completed",
                    "scraped_media": scraped_media,
                    "downloaded_media": [],
                    "failed_downloads": [],
                    "total_scraped": 20,
                    "total_downloaded": 0,
                    "total_failed": 0,
                    "metadata": {}
                }
            ],
            "current_session": None
        }
        
        with open(progress_file, 'w') as f:
            json.dump(progress_data, f)
        
        # Patch the config to use our temp directory
        with patch('config.DOWNLOAD_PROGRESS_FILE', str(progress_file)):
            with patch('config.ensure_data_directory'):
                # Create toolkit instance
                toolkit = Lemon8Toolkit(auto_save=False)
                
                # Mock the downloader methods
                toolkit.downloader.is_already_downloaded = Mock(return_value=False)
                toolkit.downloader.download_media = Mock(return_value="/path/to/media.jpg")
                
                # Call download_pending_media with limit=5
                toolkit.download_pending_media(limit=5)
                
                # Verify the downloader was called exactly 5 times
                assert toolkit.downloader.download_media.call_count == 5


if __name__ == "__main__":
    print("\n" + "="*80)
    print("Functional Test - download_pending_media method")
    print("="*80)
    
    tests = [
        ("Test 1: No pending media", test_download_pending_media_no_pending),
        ("Test 2: With pending media", test_download_pending_media_with_pending),
        ("Test 3: Respects limit parameter", test_download_pending_media_respects_limit),
    ]
    
    results = []
    for test_name, test_func in tests:
        print(f"\n{'='*80}")
        print(f"Running: {test_name}")
        print('='*80)
        try:
            test_func()
            results.append((test_name, "PASS"))
            print(f"✅ {test_name}: PASS")
        except AssertionError as e:
            results.append((test_name, f"FAIL: {str(e)}"))
            print(f"❌ {test_name}: FAIL - {str(e)}")
        except Exception as e:
            results.append((test_name, f"ERROR: {str(e)}"))
            print(f"❌ {test_name}: ERROR - {str(e)}")
    
    print("\n" + "="*80)
    print("Test Results Summary")
    print("="*80)
    for test_name, result in results:
        status_icon = "✅" if result == "PASS" else "❌"
        print(f"{status_icon} {test_name}: {result}")
    
    passed = sum(1 for _, result in results if result == "PASS")
    total = len(results)
    print(f"\nTotal: {passed}/{total} tests passed")
    print("="*80)
