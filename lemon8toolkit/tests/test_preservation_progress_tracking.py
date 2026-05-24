"""
Preservation Property Tests - Progress Tracking Operations

These tests verify that existing progress tracking functionality remains unchanged after bugfixes.
They test session management, status updates, and progress statistics.

EXPECTED OUTCOME ON UNFIXED CODE: Tests PASS (confirms baseline behavior)
After fixes are implemented, these tests should STILL PASS (confirms no regressions)

**Validates: Requirements 3.8 (Preservation)**
"""
import os
import sys
import tempfile
import shutil
import json
from pathlib import Path
from datetime import datetime, timedelta

import pytest
from hypothesis import given, strategies as st, settings, assume

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import config


# Test fixtures and helpers
@pytest.fixture
def test_env():
    """Create isolated test environment with temporary directories"""
    test_dir = tempfile.mkdtemp(prefix="lemon8_progress_preservation_test_")
    
    # Save original config values
    original_data_dir = config.DATA_DIR
    original_progress_file = config.DOWNLOAD_PROGRESS_FILE
    
    # Set up test directories
    test_data_dir = os.path.join(test_dir, "data")
    os.makedirs(test_data_dir, exist_ok=True)
    
    # Override config for testing
    config.DATA_DIR = test_data_dir
    config.DOWNLOAD_PROGRESS_FILE = os.path.join(test_data_dir, "download_progress.json")
    
    # Also patch the progress module's DOWNLOAD_PROGRESS_FILE (imported at module level)
    import progress as progress_module
    progress_module.DOWNLOAD_PROGRESS_FILE = config.DOWNLOAD_PROGRESS_FILE
    
    yield {
        'test_dir': test_dir,
        'data_dir': test_data_dir,
    }
    
    # Restore original config
    config.DATA_DIR = original_data_dir
    config.DOWNLOAD_PROGRESS_FILE = original_progress_file
    
    # Restore progress module's DOWNLOAD_PROGRESS_FILE
    import progress as progress_module
    progress_module.DOWNLOAD_PROGRESS_FILE = original_progress_file
    
    # Clean up test directory
    shutil.rmtree(test_dir, ignore_errors=True)


class TestEnvContext:
    """Context manager for test environment (for use with Hypothesis)"""
    def __init__(self):
        self.test_dir = None
        self.original_data_dir = None
        self.original_progress_file = None
    
    def __enter__(self):
        self.test_dir = tempfile.mkdtemp(prefix="lemon8_progress_prop_test_")
        
        # Save original config values
        self.original_data_dir = config.DATA_DIR
        self.original_progress_file = config.DOWNLOAD_PROGRESS_FILE
        
        # Set up test directories
        test_data_dir = os.path.join(self.test_dir, "data")
        os.makedirs(test_data_dir, exist_ok=True)
        
        # Override config for testing
        config.DATA_DIR = test_data_dir
        config.DOWNLOAD_PROGRESS_FILE = os.path.join(test_data_dir, "download_progress.json")
        
        # Also patch the progress module's DOWNLOAD_PROGRESS_FILE (imported at module level)
        import progress as progress_module
        progress_module.DOWNLOAD_PROGRESS_FILE = config.DOWNLOAD_PROGRESS_FILE
        
        # Ensure data directory exists (call ensure_data_directory)
        from config import ensure_data_directory
        ensure_data_directory()
        
        return {
            'test_dir': self.test_dir,
            'data_dir': test_data_dir,
        }
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        # Restore original config
        config.DATA_DIR = self.original_data_dir
        config.DOWNLOAD_PROGRESS_FILE = self.original_progress_file
        
        # Restore progress module's DOWNLOAD_PROGRESS_FILE
        import progress as progress_module
        progress_module.DOWNLOAD_PROGRESS_FILE = self.original_progress_file
        
        # Clean up test directory
        if self.test_dir:
            try:
                shutil.rmtree(self.test_dir, ignore_errors=True)
            except Exception:
                pass  # Ignore cleanup errors


# Test 1: Session creation and tracking works correctly
def test_session_creation_preserves_behavior(test_env):
    """
    Observation: ProgressManager session creation works on unfixed code
    
    Verifies that:
    1. start_session() creates a new session with unique ID
    2. Session data includes all required fields
    3. Session is added to sessions list
    4. Current session is set correctly
    5. Session ID format is consistent (type_identifier_timestamp)
    
    **Validates: Requirement 3.8 - Session creation**
    """
    from progress import ProgressManager
    
    # Create progress manager
    manager = ProgressManager(auto_save=False)
    
    # Test 1: Start a user session
    session_id_1 = manager.start_session('user', 'testuser', metadata={'pages': 5})
    assert session_id_1 is not None, "Session ID should be returned"
    assert session_id_1.startswith('user_testuser_'), "Session ID should have correct format"
    
    # Test 2: Verify session data structure
    session = manager._get_session(session_id_1)
    assert session is not None, "Session should exist"
    assert session['session_id'] == session_id_1, "Session ID should match"
    assert session['session_type'] == 'user', "Session type should be 'user'"
    assert session['identifier'] == 'testuser', "Identifier should be 'testuser'"
    assert session['status'] == 'in_progress', "Status should be 'in_progress'"
    assert session['start_time'] is not None, "Start time should be set"
    assert session['end_time'] is None, "End time should be None initially"
    assert session['total_scraped'] == 0, "Total scraped should be 0 initially"
    assert session['total_downloaded'] == 0, "Total downloaded should be 0 initially"
    assert session['total_failed'] == 0, "Total failed should be 0 initially"
    assert session['metadata']['pages'] == 5, "Metadata should be stored"
    
    # Test 3: Verify current session is set
    current_session = manager.get_current_session()
    assert current_session is not None, "Current session should be set"
    assert current_session['session_id'] == session_id_1, "Current session should match"
    
    # Test 4: Start another session (feed type)
    session_id_2 = manager.start_session('feed', 'foryou', metadata={'pages': 10})
    assert session_id_2 is not None, "Second session ID should be returned"
    assert session_id_2.startswith('feed_foryou_'), "Session ID should have correct format"
    assert session_id_2 != session_id_1, "Session IDs should be unique"
    
    # Test 5: Verify both sessions exist
    all_sessions = manager.get_all_sessions_summary()
    assert len(all_sessions) == 2, f"Should have 2 sessions, got {len(all_sessions)}"
    
    # Test 6: Verify current session is updated to latest
    current_session = manager.get_current_session()
    assert current_session['session_id'] == session_id_2, "Current session should be latest"


# Test 2: Session status updates work correctly
def test_session_status_updates_preserve_behavior(test_env):
    """
    Observation: ProgressManager session status updates work on unfixed code
    
    Verifies that:
    1. update_session_scraped_media() adds media URLs to session
    2. update_session_downloaded_media() tracks downloaded files
    3. update_session_failed_download() tracks failures
    4. Counters (total_scraped, total_downloaded, total_failed) update correctly
    5. last_updated timestamp is set on updates
    
    **Validates: Requirement 3.8 - Session status updates**
    """
    from progress import ProgressManager
    
    # Create progress manager and start session
    manager = ProgressManager(auto_save=False)
    session_id = manager.start_session('user', 'testuser')
    
    # Test 1: Update scraped media
    media_urls = [
        'https://example.com/media1.jpg',
        'https://example.com/media2.jpg',
        'https://example.com/media3.jpg'
    ]
    manager.update_session_scraped_media(session_id, media_urls)
    
    session = manager._get_session(session_id)
    assert session['total_scraped'] == 3, "Total scraped should be 3"
    assert len(session['scraped_media']) == 3, "Scraped media list should have 3 items"
    assert session['last_updated'] is not None, "Last updated should be set"
    
    # Test 2: Update downloaded media
    manager.update_session_downloaded_media(session_id, media_urls[0], '/path/to/media1.jpg')
    manager.update_session_downloaded_media(session_id, media_urls[1], '/path/to/media2.jpg')
    
    session = manager._get_session(session_id)
    assert session['total_downloaded'] == 2, "Total downloaded should be 2"
    assert len(session['downloaded_media']) == 2, "Downloaded media list should have 2 items"
    assert session['downloaded_media'][0]['url'] == media_urls[0], "First download URL should match"
    assert session['downloaded_media'][0]['file_path'] == '/path/to/media1.jpg', "First download path should match"
    assert session['downloaded_media'][0]['download_time'] is not None, "Download time should be set"
    
    # Test 3: Update failed download
    manager.update_session_failed_download(session_id, media_urls[2], 'Network error')
    
    session = manager._get_session(session_id)
    assert session['total_failed'] == 1, "Total failed should be 1"
    assert len(session['failed_downloads']) == 1, "Failed downloads list should have 1 item"
    assert session['failed_downloads'][0]['url'] == media_urls[2], "Failed URL should match"
    assert session['failed_downloads'][0]['error'] == 'Network error', "Error message should match"
    assert session['failed_downloads'][0]['failure_time'] is not None, "Failure time should be set"
    
    # Test 4: Verify counters are correct
    assert session['total_scraped'] == 3, "Total scraped should still be 3"
    assert session['total_downloaded'] == 2, "Total downloaded should be 2"
    assert session['total_failed'] == 1, "Total failed should be 1"


# Test 3: Session completion works correctly
def test_session_completion_preserves_behavior(test_env):
    """
    Observation: ProgressManager session completion works on unfixed code
    
    Verifies that:
    1. end_session() sets end_time
    2. end_session() updates status (completed, failed, cancelled)
    3. Current session is cleared when ending current session
    4. Session summary includes duration and success rate
    5. Completed sessions remain in history
    
    **Validates: Requirement 3.8 - Session completion**
    """
    from progress import ProgressManager
    
    # Create progress manager and start session
    manager = ProgressManager(auto_save=False)
    session_id = manager.start_session('user', 'testuser')
    
    # Add some activity
    manager.update_session_scraped_media(session_id, ['url1', 'url2', 'url3'])
    manager.update_session_downloaded_media(session_id, 'url1', '/path/1.jpg')
    manager.update_session_downloaded_media(session_id, 'url2', '/path/2.jpg')
    manager.update_session_failed_download(session_id, 'url3', 'Error')
    
    # Test 1: End session with 'completed' status
    manager.end_session(session_id, status='completed')
    
    session = manager._get_session(session_id)
    assert session['status'] == 'completed', "Status should be 'completed'"
    assert session['end_time'] is not None, "End time should be set"
    
    # Test 2: Verify current session is cleared
    current_session = manager.get_current_session()
    assert current_session is None, "Current session should be None after ending"
    
    # Test 3: Verify session summary
    summary = manager.get_session_summary(session_id)
    assert summary is not None, "Summary should exist"
    assert summary['status'] == 'completed', "Summary status should be 'completed'"
    assert summary['total_scraped'] == 3, "Summary should show 3 scraped"
    assert summary['total_downloaded'] == 2, "Summary should show 2 downloaded"
    assert summary['total_failed'] == 1, "Summary should show 1 failed"
    assert summary['duration'] is not None, "Duration should be calculated"
    assert summary['success_rate'] > 0, "Success rate should be calculated"
    
    # Test 4: Start and end another session with 'failed' status
    session_id_2 = manager.start_session('feed', 'foryou')
    manager.end_session(session_id_2, status='failed')
    
    session_2 = manager._get_session(session_id_2)
    assert session_2['status'] == 'failed', "Status should be 'failed'"
    
    # Test 5: Verify both sessions are in history
    all_sessions = manager.get_all_sessions_summary()
    assert len(all_sessions) == 2, "Should have 2 sessions in history"


# Test 4: Progress statistics work correctly
def test_progress_statistics_preserve_behavior(test_env):
    """
    Observation: ProgressManager statistics work on unfixed code
    
    Verifies that:
    1. get_stats() returns total_sessions count
    2. completed_sessions count is accurate
    3. in_progress_sessions count is accurate
    4. Total media counts are aggregated correctly
    5. Overall success rate is calculated correctly
    
    **Validates: Requirement 3.8 - Progress statistics**
    """
    from progress import ProgressManager
    
    # Create progress manager
    manager = ProgressManager(auto_save=False)
    
    # Test 1: Initial stats (no sessions)
    stats = manager.get_stats()
    assert stats['total_sessions'] == 0, "Should have 0 sessions initially"
    assert stats['completed_sessions'] == 0, "Should have 0 completed initially"
    assert stats['in_progress_sessions'] == 0, "Should have 0 in progress initially"
    assert stats['total_media_scraped'] == 0, "Should have 0 scraped initially"
    assert stats['total_media_downloaded'] == 0, "Should have 0 downloaded initially"
    assert stats['total_failed_downloads'] == 0, "Should have 0 failed initially"
    
    # Create first session and complete it
    session_id_1 = manager.start_session('user', 'user1')
    manager.update_session_scraped_media(session_id_1, ['url1', 'url2', 'url3'])
    manager.update_session_downloaded_media(session_id_1, 'url1', '/path/1.jpg')
    manager.update_session_downloaded_media(session_id_1, 'url2', '/path/2.jpg')
    manager.update_session_failed_download(session_id_1, 'url3', 'Error')
    manager.end_session(session_id_1, status='completed')
    
    # Test 2: Stats after first completed session
    stats = manager.get_stats()
    assert stats['total_sessions'] == 1, "Should have 1 session"
    assert stats['completed_sessions'] == 1, "Should have 1 completed"
    assert stats['in_progress_sessions'] == 0, "Should have 0 in progress"
    assert stats['total_media_scraped'] == 3, "Should have 3 scraped"
    assert stats['total_media_downloaded'] == 2, "Should have 2 downloaded"
    assert stats['total_failed_downloads'] == 1, "Should have 1 failed"
    
    # Create second session (in progress)
    session_id_2 = manager.start_session('feed', 'foryou')
    manager.update_session_scraped_media(session_id_2, ['url4', 'url5'])
    manager.update_session_downloaded_media(session_id_2, 'url4', '/path/4.jpg')
    
    # Test 3: Stats with one completed and one in progress
    stats = manager.get_stats()
    assert stats['total_sessions'] == 2, "Should have 2 sessions"
    assert stats['completed_sessions'] == 1, "Should have 1 completed"
    assert stats['in_progress_sessions'] == 1, "Should have 1 in progress"
    assert stats['total_media_scraped'] == 5, "Should have 5 scraped total"
    assert stats['total_media_downloaded'] == 3, "Should have 3 downloaded total"
    assert stats['total_failed_downloads'] == 1, "Should have 1 failed total"
    assert stats['current_session'] == session_id_2, "Current session should be session_id_2"
    
    # Complete second session
    manager.end_session(session_id_2, status='completed')
    
    # Test 4: Stats after both completed
    stats = manager.get_stats()
    assert stats['total_sessions'] == 2, "Should have 2 sessions"
    assert stats['completed_sessions'] == 2, "Should have 2 completed"
    assert stats['in_progress_sessions'] == 0, "Should have 0 in progress"
    assert stats['current_session'] is None, "Current session should be None"


# Test 5: Session persistence works correctly
def test_session_persistence_preserves_behavior(test_env):
    """
    Observation: ProgressManager session persistence works on unfixed code
    
    Verifies that:
    1. Sessions are saved to JSON file
    2. Sessions are loaded from JSON file on initialization
    3. Session data is preserved across manager instances
    4. File format is valid JSON
    
    **Validates: Requirement 3.8 - Session persistence**
    """
    from progress import ProgressManager
    
    # Create progress manager and start session
    manager1 = ProgressManager(auto_save=True)
    session_id = manager1.start_session('user', 'testuser')
    manager1.update_session_scraped_media(session_id, ['url1', 'url2'])
    manager1.update_session_downloaded_media(session_id, 'url1', '/path/1.jpg')
    manager1.save()
    
    # Test 1: Verify file exists
    assert os.path.exists(config.DOWNLOAD_PROGRESS_FILE), "Progress file should exist"
    
    # Test 2: Verify file is valid JSON
    with open(config.DOWNLOAD_PROGRESS_FILE, 'r') as f:
        data = json.load(f)
    assert 'sessions' in data, "Data should have 'sessions' key"
    assert 'current_session' in data, "Data should have 'current_session' key"
    
    # Test 3: Create new manager instance and verify data is loaded
    manager2 = ProgressManager(auto_save=False)
    loaded_session = manager2._get_session(session_id)
    assert loaded_session is not None, "Session should be loaded"
    assert loaded_session['session_id'] == session_id, "Session ID should match"
    assert loaded_session['total_scraped'] == 2, "Scraped count should be preserved"
    assert loaded_session['total_downloaded'] == 1, "Downloaded count should be preserved"
    
    # Test 4: Verify current session is preserved
    current_session = manager2.get_current_session()
    assert current_session is not None, "Current session should be loaded"
    assert current_session['session_id'] == session_id, "Current session ID should match"


# ============================================================================
# PROPERTY-BASED TESTS - Progress Tracking Preservation
# ============================================================================

@given(
    num_sessions=st.integers(min_value=1, max_value=10),
    media_per_session=st.integers(min_value=1, max_value=20)
)
@settings(max_examples=50, deadline=None)
def test_property_session_counters_consistency(num_sessions, media_per_session):
    """
    Property: Session counters are always consistent
    
    For any number of sessions and media items per session,
    the session counters should always satisfy:
    1. total_scraped >= total_downloaded + total_failed
    2. total_downloaded >= 0
    3. total_failed >= 0
    4. len(scraped_media) == total_scraped
    5. len(downloaded_media) == total_downloaded
    6. len(failed_downloads) == total_failed
    
    **Validates: Requirement 3.8 - Session counter consistency**
    """
    from progress import ProgressManager
    
    with TestEnvContext() as test_env:
        manager = ProgressManager(auto_save=False)
        
        try:
            for session_idx in range(num_sessions):
                session_id = manager.start_session('user', f'user{session_idx}')
                
                # Add scraped media
                media_urls = [f'https://example.com/media_{session_idx}_{i}.jpg' 
                             for i in range(media_per_session)]
                manager.update_session_scraped_media(session_id, media_urls)
                
                # Randomly download or fail each media
                import random
                for i, url in enumerate(media_urls):
                    if random.random() < 0.7:  # 70% success rate
                        manager.update_session_downloaded_media(session_id, url, f'/path/{i}.jpg')
                    else:
                        manager.update_session_failed_download(session_id, url, 'Random error')
                
                # Verify session counters
                session = manager._get_session(session_id)
                
                # Property 1: total_scraped >= total_downloaded + total_failed
                assert session['total_scraped'] >= session['total_downloaded'] + session['total_failed'], \
                    f"Scraped ({session['total_scraped']}) should be >= downloaded ({session['total_downloaded']}) + failed ({session['total_failed']})"
                
                # Property 2-3: Counters are non-negative
                assert session['total_downloaded'] >= 0, "Downloaded count should be non-negative"
                assert session['total_failed'] >= 0, "Failed count should be non-negative"
                
                # Property 4-6: List lengths match counters
                assert len(session['scraped_media']) == session['total_scraped'], \
                    f"Scraped media list length ({len(session['scraped_media'])}) should match counter ({session['total_scraped']})"
                assert len(session['downloaded_media']) == session['total_downloaded'], \
                    f"Downloaded media list length ({len(session['downloaded_media'])}) should match counter ({session['total_downloaded']})"
                assert len(session['failed_downloads']) == session['total_failed'], \
                    f"Failed downloads list length ({len(session['failed_downloads'])}) should match counter ({session['total_failed']})"
        finally:
            pass


@given(
    num_sessions=st.integers(min_value=1, max_value=20)
)
@settings(max_examples=50, deadline=None)
def test_property_stats_aggregation_correctness(num_sessions):
    """
    Property: Statistics aggregation is always correct
    
    For any number of sessions, the aggregated statistics should satisfy:
    1. total_sessions == number of sessions created
    2. completed_sessions + in_progress_sessions <= total_sessions
    3. total_media_scraped == sum of all session scraped counts
    4. total_media_downloaded == sum of all session downloaded counts
    5. total_failed_downloads == sum of all session failed counts
    
    **Validates: Requirement 3.8 - Statistics aggregation**
    """
    from progress import ProgressManager
    
    with TestEnvContext() as test_env:
        manager = ProgressManager(auto_save=False)
        
        try:
            session_ids = []
            expected_scraped = 0
            expected_downloaded = 0
            expected_failed = 0
            
            for i in range(num_sessions):
                session_id = manager.start_session('user', f'user{i}')
                session_ids.append(session_id)
                
                # Add random amounts of media
                import random
                num_scraped = random.randint(1, 10)
                num_downloaded = random.randint(0, num_scraped)
                num_failed = random.randint(0, num_scraped - num_downloaded)
                
                media_urls = [f'url_{i}_{j}' for j in range(num_scraped)]
                manager.update_session_scraped_media(session_id, media_urls)
                
                for j in range(num_downloaded):
                    manager.update_session_downloaded_media(session_id, media_urls[j], f'/path/{j}.jpg')
                
                for j in range(num_failed):
                    manager.update_session_failed_download(session_id, media_urls[num_downloaded + j], 'Error')
                
                expected_scraped += num_scraped
                expected_downloaded += num_downloaded
                expected_failed += num_failed
                
                # Randomly complete some sessions
                if random.random() < 0.5:
                    manager.end_session(session_id, status='completed')
            
            # Verify statistics
            stats = manager.get_stats()
            
            # Property 1: total_sessions matches
            assert stats['total_sessions'] == num_sessions, \
                f"Total sessions ({stats['total_sessions']}) should equal {num_sessions}"
            
            # Property 2: completed + in_progress <= total
            assert stats['completed_sessions'] + stats['in_progress_sessions'] <= stats['total_sessions'], \
                f"Completed ({stats['completed_sessions']}) + in progress ({stats['in_progress_sessions']}) should be <= total ({stats['total_sessions']})"
            
            # Property 3-5: Aggregated counts match
            assert stats['total_media_scraped'] == expected_scraped, \
                f"Total scraped ({stats['total_media_scraped']}) should equal {expected_scraped}"
            assert stats['total_media_downloaded'] == expected_downloaded, \
                f"Total downloaded ({stats['total_media_downloaded']}) should equal {expected_downloaded}"
            assert stats['total_failed_downloads'] == expected_failed, \
                f"Total failed ({stats['total_failed_downloads']}) should equal {expected_failed}"
        finally:
            pass


@given(
    session_type=st.sampled_from(['user', 'feed', 'tag']),
    identifier=st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=('Lu', 'Ll', 'Nd'))),
    num_updates=st.integers(min_value=0, max_value=10)
)
@settings(max_examples=5, deadline=None)
def test_property_session_id_uniqueness(session_type, identifier, num_updates):
    """
    Property: Session IDs are always unique
    
    For any session type, identifier, and number of updates,
    each session should have a unique ID even if created with same parameters.
    Note: Session IDs use second-level timestamps, so we use 1.1s delays between sessions.
    
    **Validates: Requirement 3.8 - Session ID uniqueness**
    """
    from progress import ProgressManager
    import time
    
    with TestEnvContext() as test_env:
        manager = ProgressManager(auto_save=False)
        
        try:
            session_ids = set()
            
            # Create multiple sessions with same parameters
            for _ in range(3):
                session_id = manager.start_session(session_type, identifier)
                
                # Property: Session ID should be unique
                assert session_id not in session_ids, \
                    f"Session ID {session_id} should be unique"
                session_ids.add(session_id)
                
                # Add some updates
                for i in range(num_updates):
                    manager.update_session_scraped_media(session_id, [f'url_{i}'])
                
                # Sleep 1.1s to ensure second-level timestamp difference
                time.sleep(1.1)
            
            # Verify all sessions exist
            all_sessions = manager.get_all_sessions_summary()
            assert len(all_sessions) == 3, "Should have 3 unique sessions"
        finally:
            pass


@given(
    num_sessions=st.integers(min_value=1, max_value=10)
)
@settings(max_examples=50, deadline=None)
def test_property_session_persistence_integrity(num_sessions):
    """
    Property: Session data persists correctly across manager instances
    
    For any number of sessions, after saving and reloading:
    1. All sessions should be present
    2. Session data should be identical
    3. Current session should be preserved
    4. Statistics should match
    
    **Validates: Requirement 3.8 - Session persistence integrity**
    """
    from progress import ProgressManager
    
    with TestEnvContext() as test_env:
        # Create first manager and add sessions
        manager1 = ProgressManager(auto_save=True)
        
        try:
            session_ids = []
            for i in range(num_sessions):
                session_id = manager1.start_session('user', f'user{i}')
                session_ids.append(session_id)
                manager1.update_session_scraped_media(session_id, [f'url_{i}_1', f'url_{i}_2'])
                manager1.update_session_downloaded_media(session_id, f'url_{i}_1', f'/path/{i}_1.jpg')
            
            # Get stats before reload
            stats_before = manager1.get_stats()
            current_session_before = manager1.progress_data.get('current_session')
            
            # Force save
            manager1.save()
            
            # Create second manager (should load from file)
            manager2 = ProgressManager(auto_save=False)
            
            # Property 1: All sessions should be present
            all_sessions = manager2.get_all_sessions_summary()
            assert len(all_sessions) == num_sessions, \
                f"Should have {num_sessions} sessions after reload, got {len(all_sessions)}"
            
            # Property 2: Session data should be identical
            for session_id in session_ids:
                session = manager2._get_session(session_id)
                assert session is not None, f"Session {session_id} should exist after reload"
                assert session['total_scraped'] == 2, "Scraped count should be preserved"
                assert session['total_downloaded'] == 1, "Downloaded count should be preserved"
            
            # Property 3: Current session should be preserved
            current_session_after = manager2.progress_data.get('current_session')
            assert current_session_after == current_session_before, \
                "Current session should be preserved after reload"
            
            # Property 4: Statistics should match
            stats_after = manager2.get_stats()
            assert stats_after['total_sessions'] == stats_before['total_sessions'], \
                "Total sessions should match after reload"
            assert stats_after['total_media_scraped'] == stats_before['total_media_scraped'], \
                "Total scraped should match after reload"
            assert stats_after['total_media_downloaded'] == stats_before['total_media_downloaded'], \
                "Total downloaded should match after reload"
        finally:
            pass


if __name__ == "__main__":
    # Run tests with pytest
    pytest.main([__file__, "-v", "--tb=short"])
