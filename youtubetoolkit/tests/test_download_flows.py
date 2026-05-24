import os
from pathlib import Path
from unittest.mock import patch

import src.data_manager_streamlined as data_manager_streamlined
import src.video_processor as video_processor
import scripts.batch_downloader as batch_downloader


class FakeYoutubeDL:
    def __init__(self, options):
        self.options = options

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def extract_info(self, url, download=True):
        info = {
            "title": "Resume Test",
            "ext": "mp4",
            "description": "",
            "uploader": "Uploader",
            "uploader_id": "channel123",
        }
        if download:
            file_path = Path(self.prepare_filename(info))
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text("video-bytes", encoding="utf-8")
        return info

    def prepare_filename(self, info):
        return self.options["outtmpl"].replace("%(title)s", info["title"]).replace("%(ext)s", info["ext"])


def test_resume_interrupted_downloads_updates_db_with_mocked_ytdlp(tmp_path, monkeypatch):
    db_path = tmp_path / "youtube_data.db"
    download_dir = tmp_path / "downloads"
    url = "https://www.youtube.com/watch?v=resume123"
    original_database_manager = data_manager_streamlined.DatabaseManager

    db = original_database_manager(db_path=str(db_path))
    db.add_video(url, title="Resume Test", channel="Test Channel")
    db.update_download_status(url, "downloading")

    # Create a test DatabaseManager class that always uses the test database
    class TestDatabaseManager(data_manager_streamlined.DatabaseManager):
        def __init__(self, db_path_arg=None):
            # Always use the test database path, ignore the argument
            super().__init__(db_path=str(db_path))
    
    monkeypatch.setattr(video_processor.yt_dlp, "YoutubeDL", FakeYoutubeDL)
    monkeypatch.setattr(video_processor, "get_best_cookie_option", lambda: None)

    # Patch the DatabaseManager where it's imported from
    with patch('data_manager_streamlined.DatabaseManager', TestDatabaseManager):
        resumed_count = video_processor.resume_interrupted_downloads(download_folder=str(download_dir))

    refreshed_db = original_database_manager(db_path=str(db_path))
    refreshed_video = refreshed_db.get_video_by_url(url)

    assert resumed_count == 1
    assert refreshed_video is not None
    assert refreshed_video["download_status"] == "completed"
    assert refreshed_video["status"] == "completed"
    assert refreshed_video["file_path"] is not None
    assert Path(refreshed_video["file_path"]).exists()


def test_batch_downloader_download_all_updates_db_with_mocked_downloader(tmp_path, monkeypatch):
    db_path = tmp_path / "youtube_data.db"
    download_dir = tmp_path / "downloads"
    url = "https://www.youtube.com/watch?v=batch123"
    original_database_manager = data_manager_streamlined.DatabaseManager

    db = original_database_manager(db_path=str(db_path))
    db.add_video(url, title="Batch Test", channel="Batch Channel")

    def fake_download_youtube_video(video_url, output_dir, **kwargs):
        file_path = Path(output_dir) / "batch-test.mp4"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(f"downloaded:{video_url}", encoding="utf-8")
        return str(file_path)

    monkeypatch.setattr("builtins.input", lambda _: "y")

    # Use patch context manager to mock the function in the batch_downloader namespace
    with patch('scripts.batch_downloader.download_youtube_video', side_effect=fake_download_youtube_video):
        with patch('scripts.batch_downloader.VIDEO_PROCESSOR_AVAILABLE', True):
            with patch('scripts.batch_downloader.download_with_rate_limiting', None):
                downloader = batch_downloader.BatchDownloader(download_folder=str(download_dir))
                # Replace the downloader's db instance with our test database
                downloader.db = db
                downloader.download_all()

    refreshed_db = original_database_manager(db_path=str(db_path))
    refreshed_video = refreshed_db.get_video_by_url(url)

    assert refreshed_video is not None
    assert refreshed_video["download_status"] == "completed"
    assert refreshed_video["status"] == "completed"
    assert refreshed_video["file_path"] is not None
    assert Path(refreshed_video["file_path"]).exists()
    assert downloader.stats["successful"] == 1
    assert downloader.stats["failed"] == 0


def test_batch_downloader_retry_failed_keeps_failure_state_when_download_errors(tmp_path, monkeypatch):
    db_path = tmp_path / "youtube_data.db"
    download_dir = tmp_path / "downloads"
    url = "https://www.youtube.com/watch?v=failed123"
    original_database_manager = data_manager_streamlined.DatabaseManager

    db = original_database_manager(db_path=str(db_path))
    db.add_video(url, title="Retry Test", channel="Retry Channel")
    db.update_download_status(url, "failed", error_message="original failure")

    def fake_download_youtube_video(*args, **kwargs):
        raise RuntimeError("mocked retry failure")

    monkeypatch.setattr("builtins.input", lambda _: "y")

    # Use patch context manager to mock the function in the batch_downloader namespace
    with patch('scripts.batch_downloader.download_youtube_video', side_effect=fake_download_youtube_video):
        with patch('scripts.batch_downloader.VIDEO_PROCESSOR_AVAILABLE', True):
            with patch('scripts.batch_downloader.download_with_rate_limiting', None):
                downloader = batch_downloader.BatchDownloader(download_folder=str(download_dir))
                # Replace the downloader's db instance with our test database
                downloader.db = db
                downloader.retry_failed()

    refreshed_db = original_database_manager(db_path=str(db_path))
    refreshed_video = refreshed_db.get_video_by_url(url)

    assert refreshed_video is not None
    assert refreshed_video["download_status"] == "failed"
    assert refreshed_video["status"] == "failed"
    assert "mocked retry failure" in (refreshed_video["error_message"] or "")
    assert downloader.stats["successful"] == 0
    assert downloader.stats["failed"] == 1
