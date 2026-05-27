#!/usr/bin/env python3
"""
Tests for the DB-first ProfilePhotoDownloader refactor.

All tests use an in-memory SQLite StateManager — no real filesystem downloads
or Telegram calls are made.
"""
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.state_manager import StateManager


def _make_state() -> StateManager:
    """Return a fresh in-memory StateManager (singleton reset)."""
    StateManager._instance = None
    state = StateManager(":memory:")
    state._shutdown = True
    return state


def _make_downloader(save_path: str, state: StateManager):
    """
    Build a ProfilePhotoDownloader with a patched state manager and
    signal handler disabled (so tests don't install real signal handlers).
    """
    from src.managers.download_profile_photos import ProfilePhotoDownloader

    with patch.object(ProfilePhotoDownloader, "_setup_signal_handlers"):
        with patch("src.managers.download_profile_photos.get_state_manager", return_value=state):
            downloader = ProfilePhotoDownloader(save_path)
    return downloader


class TestLoadDownloadedPhotos(unittest.TestCase):
    def setUp(self):
        self.state = _make_state()
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        self.state.close()
        StateManager._instance = None
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_loads_from_db(self):
        # Insert tracking rows directly
        self.state.conn.execute(
            "INSERT INTO profile_photo_tracking (user_id, photo_id, downloaded) VALUES (1, 'abc', 1)"
        )
        self.state.conn.execute(
            "INSERT INTO profile_photo_tracking (user_id, photo_id, downloaded) VALUES (2, 'def', 1)"
        )
        self.state.conn.commit()

        downloader = _make_downloader(self.tmp, self.state)
        self.assertIn("1_abc", downloader.downloaded_photos)
        self.assertIn("2_def", downloader.downloaded_photos)

    def test_empty_db_gives_empty_set(self):
        downloader = _make_downloader(self.tmp, self.state)
        self.assertEqual(downloader.downloaded_photos, set())


class TestIsPhotoAlreadyProcessed(unittest.TestCase):
    def setUp(self):
        self.state = _make_state()
        self.tmp = tempfile.mkdtemp()
        self.downloader = _make_downloader(self.tmp, self.state)

    def tearDown(self):
        self.state.close()
        StateManager._instance = None
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_db_hit_and_file_exists(self):
        # Write a real file
        filepath = os.path.join(self.tmp, "profile_1_999_20240101_120000.jpg")
        Path(filepath).write_bytes(b"fake_photo_data")

        # Mark as downloaded in DB
        self.state.save_profile_photo(1, "999", downloaded=True)

        is_done, reason = self.downloader.is_photo_already_processed(filepath, "1_999")
        self.assertTrue(is_done)
        self.assertIn("DB tracking", reason)

    def test_db_hit_but_file_missing(self):
        filepath = os.path.join(self.tmp, "profile_1_999_20240101_120000.jpg")
        # File does NOT exist on disk
        self.state.save_profile_photo(1, "999", downloaded=True)

        is_done, reason = self.downloader.is_photo_already_processed(filepath, "1_999")
        self.assertFalse(is_done)
        self.assertIn("missing", reason)

    def test_hash_hit_backfills_tracking(self):
        filepath = os.path.join(self.tmp, "profile_3_777_unknown_date.jpg")
        Path(filepath).write_bytes(b"some_photo_bytes")

        # Compute and pre-register the hash
        file_hash = self.downloader.file_hash(filepath)
        self.downloader.downloaded_hashes.add(file_hash)

        is_done, reason = self.downloader.is_photo_already_processed(filepath, "3_777")
        self.assertTrue(is_done)
        self.assertIn("Hash tracking", reason)
        # Should have been backfilled into the in-memory cache
        self.assertIn("3_777", self.downloader.downloaded_photos)

    def test_not_processed_returns_false(self):
        filepath = os.path.join(self.tmp, "profile_5_111_unknown_date.jpg")
        # File doesn't exist, not in DB, not in hash set
        is_done, reason = self.downloader.is_photo_already_processed(filepath, "5_111")
        self.assertFalse(is_done)
        self.assertEqual(reason, "Not processed")


class TestSaveDownloadedPhoto(unittest.TestCase):
    def setUp(self):
        self.state = _make_state()
        self.tmp = tempfile.mkdtemp()
        self.downloader = _make_downloader(self.tmp, self.state)

    def tearDown(self):
        self.state.close()
        StateManager._instance = None
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_writes_to_db_and_cache(self):
        self.downloader.save_downloaded_photo("10_555")
        self.assertIn("10_555", self.downloader.downloaded_photos)
        self.assertTrue(self.state.is_profile_photo_downloaded(10, "555"))

    def test_idempotent(self):
        self.downloader.save_downloaded_photo("10_555")
        self.downloader.save_downloaded_photo("10_555")  # second call should be no-op
        cursor = self.state.conn.execute(
            "SELECT COUNT(*) as c FROM profile_photo_tracking WHERE user_id=10 AND photo_id='555'"
        )
        self.assertEqual(cursor.fetchone()['c'], 1)


class TestResetProfileDownloadProgress(unittest.TestCase):
    def setUp(self):
        self.state = _make_state()
        self.tmp = tempfile.mkdtemp()
        # Insert some users
        self.state.conn.executemany(
            "INSERT INTO users (user_id, username, is_bot, profile_photo_downloaded, profile_photo_count) "
            "VALUES (?, ?, 0, 1, 3)",
            [(1, "alice"), (2, "bob"), (3, "carol")]
        )
        self.state.conn.executemany(
            "INSERT INTO profile_photo_tracking (user_id, photo_id, downloaded) VALUES (?, ?, 1)",
            [(1, "p1"), (2, "p2"), (3, "p3")]
        )
        self.state.conn.commit()
        self.downloader = _make_downloader(self.tmp, self.state)

    def tearDown(self):
        self.state.close()
        StateManager._instance = None
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_full_reset_clears_all(self):
        count = self.downloader.reset_profile_download_progress()
        self.assertGreater(count, 0)

        rows = self.state.conn.execute(
            "SELECT COUNT(*) as c FROM users WHERE profile_photo_downloaded = 1"
        ).fetchone()
        self.assertEqual(rows['c'], 0)

        tracking = self.state.conn.execute(
            "SELECT COUNT(*) as c FROM profile_photo_tracking"
        ).fetchone()
        self.assertEqual(tracking['c'], 0)

        self.assertEqual(self.downloader.downloaded_photos, set())

    def test_scoped_reset_only_touches_specified_users(self):
        self.downloader.downloaded_photos = {"1_p1", "2_p2", "3_p3"}
        count = self.downloader.reset_profile_download_progress(user_ids=[1, 2])
        self.assertEqual(count, 2)

        # User 3 should still be marked downloaded
        row = self.state.conn.execute(
            "SELECT profile_photo_downloaded FROM users WHERE user_id = 3"
        ).fetchone()
        self.assertEqual(row['profile_photo_downloaded'], 1)

        # Users 1 and 2 should be reset
        for uid in (1, 2):
            row = self.state.conn.execute(
                "SELECT profile_photo_downloaded FROM users WHERE user_id = ?", (uid,)
            ).fetchone()
            self.assertEqual(row['profile_photo_downloaded'], 0)

        # In-memory cache should not contain user 1 or 2 entries
        self.assertNotIn("1_p1", self.downloader.downloaded_photos)
        self.assertNotIn("2_p2", self.downloader.downloaded_photos)
        self.assertIn("3_p3", self.downloader.downloaded_photos)


class TestVerifyFilesOnDisk(unittest.TestCase):
    def setUp(self):
        self.state = _make_state()
        self.tmp = tempfile.mkdtemp()
        # Insert a user marked as downloaded
        self.state.conn.execute(
            "INSERT INTO users (user_id, username, is_bot, profile_photo_downloaded) VALUES (42, 'dave', 0, 1)"
        )
        self.state.conn.execute(
            "INSERT INTO profile_photo_tracking (user_id, photo_id, downloaded) VALUES (42, 'ph1', 1)"
        )
        self.state.conn.commit()

    def tearDown(self):
        self.state.close()
        StateManager._instance = None
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_marks_missing_file_as_not_downloaded(self):
        """User folder doesn't exist → should be reset."""
        downloader = _make_downloader(self.tmp, self.state)
        # No folder created for user 42 → file is missing
        result = downloader.verify_files_on_disk()

        self.assertEqual(result['checked'], 1)
        self.assertEqual(result['missing'], 1)

        row = self.state.conn.execute(
            "SELECT profile_photo_downloaded FROM users WHERE user_id = 42"
        ).fetchone()
        self.assertEqual(row['profile_photo_downloaded'], 0)

    def test_existing_file_not_reset(self):
        """User folder with a profile file → should NOT be reset."""
        # Create the folder and a fake photo file
        user_folder = os.path.join(self.tmp, "user_42_dave")
        os.makedirs(user_folder)
        Path(os.path.join(user_folder, "profile_42_ph1_unknown_date.jpg")).write_bytes(b"x")

        downloader = _make_downloader(self.tmp, self.state)
        result = downloader.verify_files_on_disk()

        self.assertEqual(result['missing'], 0)

        row = self.state.conn.execute(
            "SELECT profile_photo_downloaded FROM users WHERE user_id = 42"
        ).fetchone()
        self.assertEqual(row['profile_photo_downloaded'], 1)


class TestGracefulShutdown(unittest.TestCase):
    def setUp(self):
        self.state = _make_state()
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        self.state.close()
        StateManager._instance = None
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_shutdown_flushes_db_buffers(self):
        """Ctrl+C handler must call state.flush_all_buffers before exiting."""
        from src.managers.download_profile_photos import ProfilePhotoDownloader

        flush_called = []

        def fake_flush():
            flush_called.append(True)

        with patch("src.managers.download_profile_photos.get_state_manager", return_value=self.state):
            downloader = ProfilePhotoDownloader.__new__(ProfilePhotoDownloader)
            downloader.save_path = self.tmp
            downloader.state = self.state
            downloader.downloaded_photos = set()
            downloader.downloaded_hashes = set()
            downloader.account_round_robin_index = 0
            downloader.account_stats = {}
            downloader.account_last_used = {}
            downloader.failed_accounts = set()
            downloader.user_folder_index = {}
            downloader.user_folder_index_ready = True
            downloader.folder_photo_index = {}
            import re
            downloader._profile_file_pattern = re.compile(r"^profile_(\d+)_(\d+)_(.+)$")
            downloader._valid_profile_extensions = ('.jpg', '.jpeg', '.png', '.webp')
            downloader.error_log_file = os.path.join(self.tmp, "errors.log")

        self.state.flush_all_buffers = fake_flush

        # Capture the installed signal handler
        captured_handler = []
        original_signal = signal.signal

        def capture_signal(sig, handler):
            if sig == signal.SIGINT:
                captured_handler.append(handler)
            return original_signal(sig, handler)

        with patch("signal.signal", side_effect=capture_signal):
            downloader._setup_signal_handlers()

        self.assertTrue(captured_handler, "Signal handler was not installed")

        # Simulate Ctrl+C — should call flush then sys.exit
        with self.assertRaises(SystemExit):
            captured_handler[0](signal.SIGINT, None)

        self.assertTrue(flush_called, "flush_all_buffers was not called on shutdown")


if __name__ == "__main__":
    unittest.main()

