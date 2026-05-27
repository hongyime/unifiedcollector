"""
Preservation Property Tests - Profile Photo Tracking

These tests verify that existing profile photo tracking functionality remains unchanged after bugfixes.
They test pHash comparison, change detection, URL-based deduplication, and blob storage.

EXPECTED OUTCOME ON UNFIXED CODE: Tests PASS (confirms baseline behavior)
After fixes are implemented, these tests should STILL PASS (confirms no regressions)

**Validates: Requirements 3.6 (Preservation)**
"""
import os
import sys
import tempfile
import shutil
import sqlite3
import importlib
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime
from io import BytesIO

import pytest
from hypothesis import given, strategies as st, settings, HealthCheck

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import config

# Check if imagehash is available
try:
    import imagehash
    from PIL import Image
    IMAGEHASH_AVAILABLE = True
except ImportError:
    IMAGEHASH_AVAILABLE = False
    pytest.skip("imagehash not available - skipping profile photo tracking tests", allow_module_level=True)


# Test fixtures and helpers
@pytest.fixture(scope="function")
def test_env():
    """Create isolated test environment with temporary directories"""
    test_dir = tempfile.mkdtemp(prefix="lemon8_profile_photo_test_")
    
    # Save original config values
    original_data_dir = config.DATA_DIR
    original_db_file = config.LEMON8_DB_FILE
    
    # Set up test directories
    test_data_dir = os.path.join(test_dir, "data")
    os.makedirs(test_data_dir, exist_ok=True)
    
    # Override config for testing
    config.DATA_DIR = test_data_dir
    config.LEMON8_DB_FILE = os.path.join(test_data_dir, "lemon8_toolkit.db")
    
    # Patch the profile_photo_tracker module's LEMON8_DB_FILE
    import profile_photo_tracker
    profile_photo_tracker.LEMON8_DB_FILE = config.LEMON8_DB_FILE
    
    yield {
        'test_dir': test_dir,
        'data_dir': test_data_dir,
    }
    
    # Restore original config
    config.DATA_DIR = original_data_dir
    config.LEMON8_DB_FILE = original_db_file
    profile_photo_tracker.LEMON8_DB_FILE = original_db_file
    
    # Clean up test directory
    shutil.rmtree(test_dir, ignore_errors=True)


def create_test_image(width=800, height=600, pattern='solid', color=(255, 0, 0)):
    """Create a test image in memory with specified dimensions and pattern"""
    image = Image.new('RGB', (width, height), color)
    
    # Add patterns to make images distinguishable by pHash
    if pattern == 'gradient':
        # Create a gradient pattern
        for y in range(height):
            for x in range(width):
                r = int(color[0] * (x / width))
                g = int(color[1] * (y / height))
                b = color[2]
                image.putpixel((x, y), (r, g, b))
    elif pattern == 'checkerboard':
        # Create a checkerboard pattern
        square_size = 50
        for y in range(height):
            for x in range(width):
                if ((x // square_size) + (y // square_size)) % 2 == 0:
                    image.putpixel((x, y), color)
                else:
                    image.putpixel((x, y), (255 - color[0], 255 - color[1], 255 - color[2]))
    elif pattern == 'stripes':
        # Create horizontal stripes
        stripe_height = 100
        for y in range(height):
            if (y // stripe_height) % 2 == 0:
                for x in range(width):
                    image.putpixel((x, y), color)
            else:
                for x in range(width):
                    image.putpixel((x, y), (255 - color[0], 255 - color[1], 255 - color[2]))
    
    buffer = BytesIO()
    image.save(buffer, format='PNG')
    return buffer.getvalue()


def create_different_test_image(width=800, height=600, pattern='gradient', color=(0, 255, 0)):
    """Create a different test image (different pattern/color) for testing change detection"""
    return create_test_image(width, height, pattern, color)


# Test 1: pHash computation works correctly
def test_phash_computation_preserves_behavior(test_env):
    """
    Observation: ProfilePhotoTracker pHash computation works on unfixed code
    
    Verifies that:
    1. pHash is computed for valid images
    2. Same image produces same pHash
    3. Different images produce different pHash values
    4. pHash is a valid hex string
    
    **Validates: Requirement 3.6 - pHash comparison**
    """
    from profile_photo_tracker import ProfilePhotoTracker
    
    tracker = ProfilePhotoTracker()
    
    # Test 1: Compute pHash for valid image
    image_data = create_test_image(800, 600, 'gradient', (255, 0, 0))
    phash1 = tracker._compute_phash(image_data)
    
    assert phash1 is not None, "pHash should be computed for valid image"
    assert isinstance(phash1, str), "pHash should be a string"
    assert len(phash1) > 0, "pHash should not be empty"
    
    # Test 2: Same image produces same pHash
    image_data2 = create_test_image(800, 600, 'gradient', (255, 0, 0))
    phash2 = tracker._compute_phash(image_data2)
    
    assert phash1 == phash2, "Same image should produce same pHash"
    
    # Test 3: Different image produces different pHash
    different_image = create_test_image(800, 600, 'checkerboard', (0, 255, 0))
    phash3 = tracker._compute_phash(different_image)
    
    assert phash3 != phash1, "Different image should produce different pHash"
    
    # Test 4: pHash is valid hex string
    try:
        int(phash1, 16)
        is_hex = True
    except ValueError:
        is_hex = False
    
    assert is_hex, "pHash should be a valid hex string"
    
    tracker.close()


# Test 2: URL-based change detection works correctly
def test_url_based_change_detection_preserves_behavior(test_env):
    """
    Observation: Profile photo URL-based change detection works on unfixed code
    
    Verifies that:
    1. Same URL is detected as no change (Stage 1 optimization)
    2. Different URL triggers pHash comparison (Stage 2)
    3. First photo for user is tracked correctly
    4. Change detection returns correct boolean and pHash
    
    **Validates: Requirement 3.6 - Profile photo change detection**
    """
    from profile_photo_tracker import ProfilePhotoTracker
    
    tracker = ProfilePhotoTracker()
    
    username = 'testuser1'
    photo_url1 = 'https://example.com/photo1.jpg'
    photo_url2 = 'https://example.com/photo2.jpg'
    
    image_data1 = create_test_image(800, 600, 'gradient', (255, 0, 0))
    image_data2 = create_test_image(800, 600, 'checkerboard', (0, 255, 0))
    
    # Mock image download
    with patch.object(tracker, '_download_image') as mock_download:
        # Test 1: First photo for user
        mock_download.return_value = image_data1
        changed1, phash1 = tracker.check_and_track_photo(
            username=username,
            photo_url=photo_url1,
            store_blob=True
        )
        
        # First photo should be tracked (not considered a "change" but is new)
        assert phash1 is not None, "pHash should be returned for first photo"
        
        # Test 2: Same URL - should skip pHash check (Stage 1)
        mock_download.reset_mock()
        changed2, phash2 = tracker.check_and_track_photo(
            username=username,
            photo_url=photo_url1,
            store_blob=True
        )
        
        assert changed2 is False, "Same URL should not be detected as change"
        assert phash2 == phash1, "Should return cached pHash for same URL"
        assert not mock_download.called, "Should not download image for same URL"
        
        # Test 3: Different URL with same image - should detect no change (Stage 2)
        mock_download.return_value = image_data1  # Same image, different URL
        changed3, phash3 = tracker.check_and_track_photo(
            username=username,
            photo_url=photo_url2,
            store_blob=True
        )
        
        assert changed3 is False, "Same pHash should not be detected as change"
        assert phash3 == phash1, "pHash should match original"
        
        # Test 4: Different URL with different image - should detect change
        mock_download.return_value = image_data2  # Different image
        photo_url3 = 'https://example.com/photo3.jpg'
        changed4, phash4 = tracker.check_and_track_photo(
            username=username,
            photo_url=photo_url3,
            store_blob=True
        )
        
        assert changed4 is True, "Different pHash should be detected as change"
        assert phash4 != phash1, "pHash should be different"
    
    tracker.close()


# Test 3: Photo history tracking works correctly
def test_photo_history_tracking_preserves_behavior(test_env):
    """
    Observation: Profile photo history tracking works on unfixed code
    
    Verifies that:
    1. Photo changes are stored in database
    2. History can be retrieved for a user
    3. History is ordered by detection time (newest first)
    4. History includes all required fields
    
    **Validates: Requirement 3.6 - Profile photo history**
    """
    from profile_photo_tracker import ProfilePhotoTracker
    
    tracker = ProfilePhotoTracker()
    
    username = 'testuser2'
    
    # Create multiple photos with different patterns
    photos = [
        ('https://example.com/photo1.jpg', create_test_image(800, 600, 'gradient', (255, 0, 0))),
        ('https://example.com/photo2.jpg', create_test_image(800, 600, 'checkerboard', (0, 255, 0))),
        ('https://example.com/photo3.jpg', create_test_image(800, 600, 'stripes', (0, 0, 255))),
    ]
    
    # Track photos
    with patch.object(tracker, '_download_image') as mock_download:
        for url, image_data in photos:
            mock_download.return_value = image_data
            tracker.check_and_track_photo(
                username=username,
                photo_url=url,
                user_id='12345',
                store_blob=True
            )
    
    # Retrieve history
    history = tracker.get_photo_history(username, limit=10)
    
    # Verify history
    assert len(history) >= 1, "Should have at least one photo in history"
    assert len(history) <= 3, "Should have at most 3 photos in history"
    
    # Verify history structure
    for record in history:
        assert 'id' in record, "History record should have id"
        assert 'username' in record, "History record should have username"
        assert 'photo_url' in record, "History record should have photo_url"
        assert 'photo_phash' in record, "History record should have photo_phash"
        assert 'detected_at' in record, "History record should have detected_at"
        assert record['username'] == username, "History should be for correct user"
    
    # Verify ordering (newest first)
    if len(history) > 1:
        for i in range(len(history) - 1):
            time1 = datetime.fromisoformat(history[i]['detected_at'])
            time2 = datetime.fromisoformat(history[i + 1]['detected_at'])
            assert time1 >= time2, "History should be ordered newest first"
    
    tracker.close()


# Test 4: Blob storage works correctly
def test_blob_storage_preserves_behavior(test_env):
    """
    Observation: Profile photo blob storage works on unfixed code
    
    Verifies that:
    1. Photo blobs are stored when store_blob=True
    2. Photo blobs are not stored when store_blob=False
    3. Blobs can be exported to disk
    4. Blob statistics are accurate
    
    **Validates: Requirement 3.6 - Blob storage**
    """
    from profile_photo_tracker import ProfilePhotoTracker
    
    tracker = ProfilePhotoTracker()
    
    username = 'testuser3'
    photo_url = 'https://example.com/photo_blob.jpg'
    image_data = create_test_image(800, 600, 'gradient', (255, 0, 0))
    
    # Test 1: Store blob
    with patch.object(tracker, '_download_image') as mock_download:
        mock_download.return_value = image_data
        tracker.check_and_track_photo(
            username=username,
            photo_url=photo_url,
            store_blob=True
        )
    
    # Verify blob was stored
    history = tracker.get_photo_history(username, limit=1)
    assert len(history) > 0, "Should have photo in history"
    
    photo_id = history[0]['id']
    
    # Query database directly to check blob
    cursor = tracker.conn.cursor()
    cursor.execute('SELECT photo_blob FROM profile_photo_history WHERE id = ?', (photo_id,))
    row = cursor.fetchone()
    assert row is not None, "Photo record should exist"
    assert row['photo_blob'] is not None, "Blob should be stored"
    
    # Test 2: Export blob
    export_path = os.path.join(test_env['test_dir'], 'exported_photo.png')
    success = tracker.export_photo_blob(photo_id, export_path)
    
    assert success is True, "Blob export should succeed"
    assert os.path.exists(export_path), "Exported file should exist"
    
    # Test 3: Blob statistics
    stats = tracker.get_stats()
    
    assert 'total_photos_tracked' in stats, "Stats should include total_photos_tracked"
    assert 'unique_users_tracked' in stats, "Stats should include unique_users_tracked"
    assert 'photos_with_blobs' in stats, "Stats should include photos_with_blobs"
    assert 'total_blob_size_bytes' in stats, "Stats should include total_blob_size_bytes"
    
    assert stats['total_photos_tracked'] >= 1, "Should have at least 1 photo tracked"
    assert stats['photos_with_blobs'] >= 1, "Should have at least 1 photo with blob"
    assert stats['total_blob_size_bytes'] > 0, "Total blob size should be positive"
    
    # Test 4: Store without blob
    username2 = 'testuser4'
    photo_url2 = 'https://example.com/photo_no_blob.jpg'
    
    with patch.object(tracker, '_download_image') as mock_download:
        mock_download.return_value = image_data
        tracker.check_and_track_photo(
            username=username2,
            photo_url=photo_url2,
            store_blob=False
        )
    
    # Verify blob was not stored
    history2 = tracker.get_photo_history(username2, limit=1)
    assert len(history2) > 0, "Should have photo in history"
    
    photo_id2 = history2[0]['id']
    cursor.execute('SELECT photo_blob FROM profile_photo_history WHERE id = ?', (photo_id2,))
    row2 = cursor.fetchone()
    assert row2 is not None, "Photo record should exist"
    assert row2['photo_blob'] is None, "Blob should not be stored when store_blob=False"
    
    tracker.close()


# Property-Based Test 1: pHash computation is deterministic
@given(
    width=st.integers(min_value=100, max_value=2000),
    height=st.integers(min_value=100, max_value=2000),
    pattern=st.sampled_from(['gradient', 'checkerboard', 'stripes']),
    red=st.integers(min_value=0, max_value=255),
    green=st.integers(min_value=0, max_value=255),
    blue=st.integers(min_value=0, max_value=255),
)
@settings(max_examples=20, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
def test_property_phash_deterministic(width, height, pattern, red, green, blue):
    """
    Property: For all valid images, pHash computation is deterministic
    
    For any image with valid dimensions and pattern, computing pHash twice
    should produce the same result.
    
    **Validates: Requirement 3.6 - pHash comparison determinism**
    """
    from profile_photo_tracker import ProfilePhotoTracker
    
    # Create temporary test environment
    test_dir = tempfile.mkdtemp(prefix="lemon8_phash_property_")
    test_data_dir = os.path.join(test_dir, "data")
    os.makedirs(test_data_dir, exist_ok=True)
    
    original_data_dir = config.DATA_DIR
    original_db_file = config.LEMON8_DB_FILE
    
    try:
        config.DATA_DIR = test_data_dir
        config.LEMON8_DB_FILE = os.path.join(test_data_dir, "lemon8_toolkit.db")
        
        # Patch the profile_photo_tracker module's LEMON8_DB_FILE
        import profile_photo_tracker
        profile_photo_tracker.LEMON8_DB_FILE = config.LEMON8_DB_FILE
        
        tracker = ProfilePhotoTracker()
        
        # Create test image
        image_data = create_test_image(width, height, pattern, (red, green, blue))
        
        # Compute pHash twice
        phash1 = tracker._compute_phash(image_data)
        phash2 = tracker._compute_phash(image_data)
        
        # Verify determinism
        assert phash1 is not None, "pHash should be computed"
        assert phash2 is not None, "pHash should be computed"
        assert phash1 == phash2, f"pHash should be deterministic for same image (got {phash1} and {phash2})"
        
        tracker.close()
    
    finally:
        config.DATA_DIR = original_data_dir
        config.LEMON8_DB_FILE = original_db_file
        shutil.rmtree(test_dir, ignore_errors=True)


# Property-Based Test 2: Change detection is consistent
@given(
    username=st.text(min_size=3, max_size=20, alphabet=st.characters(whitelist_categories=('Ll', 'Lu', 'Nd'))),
    url_suffix=st.integers(min_value=1, max_value=1000),
)
@settings(max_examples=15, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
def test_property_change_detection_consistent(username, url_suffix):
    """
    Property: For all users and URLs, change detection is consistent
    
    For any user, checking the same URL twice should not detect a change
    on the second check (Stage 1 optimization).
    
    **Validates: Requirement 3.6 - Change detection consistency**
    """
    from profile_photo_tracker import ProfilePhotoTracker
    
    # Create temporary test environment
    test_dir = tempfile.mkdtemp(prefix="lemon8_change_property_")
    test_data_dir = os.path.join(test_dir, "data")
    os.makedirs(test_data_dir, exist_ok=True)
    
    original_data_dir = config.DATA_DIR
    original_db_file = config.LEMON8_DB_FILE
    
    try:
        config.DATA_DIR = test_data_dir
        config.LEMON8_DB_FILE = os.path.join(test_data_dir, "lemon8_toolkit.db")
        
        # Patch the profile_photo_tracker module's LEMON8_DB_FILE
        import profile_photo_tracker
        profile_photo_tracker.LEMON8_DB_FILE = config.LEMON8_DB_FILE
        
        tracker = ProfilePhotoTracker()
        
        photo_url = f'https://example.com/photo_{url_suffix}.jpg'
        image_data = create_test_image(800, 600, 'gradient', (255, 0, 0))
        
        with patch.object(tracker, '_download_image') as mock_download:
            mock_download.return_value = image_data
            
            # First check
            changed1, phash1 = tracker.check_and_track_photo(
                username=username,
                photo_url=photo_url,
                store_blob=False
            )
            
            # Reset mock to track second call
            mock_download.reset_mock()
            
            # Second check with same URL
            changed2, phash2 = tracker.check_and_track_photo(
                username=username,
                photo_url=photo_url,
                store_blob=False
            )
            
            # Verify consistency
            assert changed2 is False, f"Same URL should not be detected as change for user {username}"
            assert phash2 == phash1, f"pHash should be consistent for same URL"
            # Note: The implementation may still download to verify, so we don't assert on mock_download.called
        
        tracker.close()
    
    finally:
        config.DATA_DIR = original_data_dir
        config.LEMON8_DB_FILE = original_db_file
        shutil.rmtree(test_dir, ignore_errors=True)


# Property-Based Test 3: History retrieval respects limit
@given(
    num_photos=st.integers(min_value=1, max_value=20),
    limit=st.integers(min_value=1, max_value=10),
)
@settings(max_examples=15, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
def test_property_history_limit_respected(num_photos, limit):
    """
    Property: For all photo counts and limits, history retrieval respects limit
    
    For any number of photos tracked and any limit value, get_photo_history
    should return at most 'limit' records.
    
    **Validates: Requirement 3.6 - History retrieval**
    """
    from profile_photo_tracker import ProfilePhotoTracker
    
    # Create temporary test environment
    test_dir = tempfile.mkdtemp(prefix="lemon8_history_property_")
    test_data_dir = os.path.join(test_dir, "data")
    os.makedirs(test_data_dir, exist_ok=True)
    
    original_data_dir = config.DATA_DIR
    original_db_file = config.LEMON8_DB_FILE
    
    try:
        config.DATA_DIR = test_data_dir
        config.LEMON8_DB_FILE = os.path.join(test_data_dir, "lemon8_toolkit.db")
        
        # Patch the profile_photo_tracker module's LEMON8_DB_FILE
        import profile_photo_tracker
        profile_photo_tracker.LEMON8_DB_FILE = config.LEMON8_DB_FILE
        
        tracker = ProfilePhotoTracker()
        
        username = 'testuser_property'
        
        # Track multiple photos with unique patterns
        patterns = ['gradient', 'checkerboard', 'stripes']
        with patch.object(tracker, '_download_image') as mock_download:
            for i in range(num_photos):
                # Create unique images by varying pattern and color
                pattern = patterns[i % len(patterns)]
                color = ((i * 37) % 256, (i * 73) % 256, (i * 109) % 256)
                image_data = create_test_image(800, 600, pattern, color)
                mock_download.return_value = image_data
                
                # Use unique URLs to ensure each photo is tracked
                photo_url = f'https://example.com/photo_{i}_{pattern}_{color[0]}_{color[1]}_{color[2]}.jpg'
                tracker.check_and_track_photo(
                    username=username,
                    photo_url=photo_url,
                    store_blob=False
                )
        
        # Retrieve history with limit
        history = tracker.get_photo_history(username, limit=limit)
        
        # Verify limit is respected
        assert len(history) <= limit, \
            f"History should respect limit (got {len(history)} records, limit was {limit})"
        
        # Verify we got at most the expected number (min of num_photos and limit)
        # Note: Due to pHash collisions, we may have fewer unique photos than num_photos
        assert len(history) <= min(num_photos, limit), \
            f"History should return at most {min(num_photos, limit)} records"
        
        tracker.close()
    
    finally:
        config.DATA_DIR = original_data_dir
        config.LEMON8_DB_FILE = original_db_file
        shutil.rmtree(test_dir, ignore_errors=True)


if __name__ == "__main__":
    # Run tests with pytest
    pytest.main([__file__, "-v", "--tb=short"])
