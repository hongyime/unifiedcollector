import pytest
import os
import tempfile
import gc

from data_manager_streamlined import DatabaseManager

@pytest.fixture
def memory_db():
    # Use a temporary file for the database to ensure connection persistence
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    
    db = DatabaseManager(db_path=path)
    yield db
    
    # Cleanup after test
    try:
        os.remove(path)
    except Exception:
        pass

def test_database_initialization(memory_db):
    assert os.path.exists(memory_db.db_path)
    # Test that the database was created successfully without errors

def test_add_video(memory_db):
    url = "https://www.youtube.com/watch?v=test1"
    video_id = memory_db.add_video(url, title="Test Video", channel="Test Channel")
    
    assert video_id is not None
    
    # Retrieve the video
    video = memory_db.get_video_by_url(url)
    assert video is not None
    assert video['title'] == "Test Video"
    assert video['channel'] == "Test Channel"
    assert video['status'] == "pending"

def test_batch_add_videos(memory_db):
    videos = [
        {"url": "https://www.youtube.com/watch?v=batch1", "title": "Batch 1"},
        {"url": "https://www.youtube.com/watch?v=batch2", "title": "Batch 2"}
    ]
    
    added_count = memory_db.batch_add_videos(videos)
    assert added_count == 2
    
    # Adding the same videos again should return 0 (duplicates skipped)
    duplicate_count = memory_db.batch_add_videos(videos)
    assert duplicate_count == 0

def test_update_download_status(memory_db):
    url = "https://www.youtube.com/watch?v=status1"
    memory_db.add_video(url, title="Status Test")
    
    # Test updating to 'downloading'
    memory_db.update_download_status(url, 'downloading')
    video = memory_db.get_video_by_url(url)
    assert video['download_status'] == 'downloading'
    
    # Test updating to 'completed'
    memory_db.update_download_status(url, 'completed', file_path="/path/to/video.mp4", file_size=1024)
    video = memory_db.get_video_by_url(url)
    assert video['download_status'] == 'completed'
    assert video['file_path'] == "/path/to/video.mp4"
    assert video['file_size'] == 1024
    assert video['status'] == 'completed'

def test_database_restores_missing_db_from_json_backup(tmp_path):
    db_path = tmp_path / "youtube_data.db"
    db = DatabaseManager(db_path=str(db_path))
    url = "https://www.youtube.com/watch?v=restore1"

    db.add_video(url, title="Restore Test", channel="Restore Channel")
    backup_path = db_path.with_name(db_path.name + ".json")

    assert backup_path.exists()

    del db
    gc.collect()
    os.remove(db_path)
    restored_db = DatabaseManager(db_path=str(db_path))
    restored_video = restored_db.get_video_by_url(url)

    assert os.path.exists(db_path)
    assert restored_video is not None
    assert restored_video["title"] == "Restore Test"
    assert restored_video["channel"] == "Restore Channel"
