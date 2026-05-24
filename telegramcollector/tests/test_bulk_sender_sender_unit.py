"""
Unit tests for services/bulk_sender/sender.py — Sender class.

Requirements: 2.3, 3.1, 4.1, 5.1, 6.2, 6.4, 7.5, 8.1, 10.2, 10.3, 10.5
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock, patch

from PIL import Image

from services.bulk_sender.sender import Sender


def _make_sender(max_retries: int = 3) -> Sender:
    """Construct a Sender bypassing __init__ and set minimal attributes."""
    s = Sender.__new__(Sender)
    s.max_retries = max_retries
    s.send_delay = 1.0
    s.sessions_path = "/tmp"
    s.bot_tokens = []
    s.job_manager = MagicMock()
    return s


# ---------------------------------------------------------------------------
# _compute_hash
# ---------------------------------------------------------------------------

class TestComputeHash(unittest.TestCase):

    def test_compute_hash_same_bytes_same_hash(self):
        """Requirement 4.1 — SHA-256 is deterministic for identical content."""
        data = b"hello bulk sender"
        with tempfile.NamedTemporaryFile(delete=False) as f1, \
             tempfile.NamedTemporaryFile(delete=False) as f2:
            f1.write(data)
            f2.write(data)
            path1, path2 = f1.name, f2.name
        try:
            s = _make_sender()
            self.assertEqual(s._compute_hash(path1), s._compute_hash(path2))
        finally:
            os.unlink(path1)
            os.unlink(path2)

    def test_compute_hash_different_bytes_different_hash(self):
        """Requirement 4.1 — Different content produces different hashes."""
        with tempfile.NamedTemporaryFile(delete=False) as f1, \
             tempfile.NamedTemporaryFile(delete=False) as f2:
            f1.write(b"aaa")
            f2.write(b"bbb")
            path1, path2 = f1.name, f2.name
        try:
            s = _make_sender()
            self.assertNotEqual(s._compute_hash(path1), s._compute_hash(path2))
        finally:
            os.unlink(path1)
            os.unlink(path2)


# ---------------------------------------------------------------------------
# _validate_image
# ---------------------------------------------------------------------------

class TestValidateImage(unittest.TestCase):

    def test_validate_image_valid_passes(self):
        """Requirement 5.1 — A valid PNG does not raise."""
        buf = BytesIO()
        img = Image.new("RGB", (1, 1), color=(255, 0, 0))
        img.save(buf, format="PNG")
        buf.seek(0)

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(buf.read())
            path = f.name
        try:
            s = _make_sender()
            # Should not raise
            s._validate_image(path)
        finally:
            os.unlink(path)

    def test_validate_image_corrupt_raises(self):
        """Requirement 5.1 — Random bytes raise an exception."""
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(b"\x00\x01\x02\x03not an image at all")
            path = f.name
        try:
            s = _make_sender()
            with self.assertRaises(Exception):
                s._validate_image(path)
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# _get_file_list
# ---------------------------------------------------------------------------

class TestGetFileList(unittest.TestCase):

    def test_get_file_list_filters_extensions(self):
        """Requirement 6.2 — Only image extensions are returned."""
        with tempfile.TemporaryDirectory() as tmpdir:
            for name in ("photo.jpg", "doc.txt", "image.png", "report.pdf"):
                open(os.path.join(tmpdir, name), "w").close()

            s = _make_sender()
            result = s._get_file_list(tmpdir)
            basenames = [os.path.basename(p) for p in result]

            self.assertIn("photo.jpg", basenames)
            self.assertIn("image.png", basenames)
            self.assertNotIn("doc.txt", basenames)
            self.assertNotIn("report.pdf", basenames)

    def test_get_file_list_lexicographic_order(self):
        """Requirement 6.4 — Files are returned sorted by full path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            for name in ("c.jpg", "a.jpg", "b.jpg"):
                open(os.path.join(tmpdir, name), "w").close()

            s = _make_sender()
            result = s._get_file_list(tmpdir)
            basenames = [os.path.basename(p) for p in result]

            self.assertEqual(basenames, ["a.jpg", "b.jpg", "c.jpg"])

    def test_get_file_list_nonexistent_raises(self):
        """Requirement 6.3 — Non-existent path raises FileNotFoundError."""
        s = _make_sender()
        with self.assertRaises(FileNotFoundError):
            s._get_file_list("/nonexistent/path/xyz")


# ---------------------------------------------------------------------------
# _build_collector_query
# ---------------------------------------------------------------------------

class TestBuildCollectorQuery(unittest.TestCase):

    def test_build_collector_query_missing_message_type_defaults_to_photo(self):
        """Requirement 7.5 — Absent message_type defaults to 'photo'."""
        s = _make_sender()
        _sql, params = s._build_collector_query({})
        self.assertIn("photo", params)


# ---------------------------------------------------------------------------
# send_job — stop_event
# ---------------------------------------------------------------------------

class TestSendJobStopEvent(unittest.TestCase):

    def test_pause_via_stop_event(self):
        """Requirement 8.1 — stop_event set before loop prevents any send."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create 5 temp jpg files
            file_paths = []
            for i in range(5):
                p = os.path.join(tmpdir, f"file{i}.jpg")
                open(p, "w").close()
                file_paths.append(p)

            s = _make_sender()
            s.job_manager.resolve_file_list.return_value = file_paths

            stop_event = asyncio.Event()
            stop_event.set()  # set BEFORE calling send_job

            job = {"id": 1, "target_chat_id": 123}

            with patch.object(s, "_send_file", new_callable=AsyncMock) as mock_send:
                asyncio.run(s.send_job(job, stop_event))
                mock_send.assert_not_called()


# ---------------------------------------------------------------------------
# _send_file — FloodWait sleep duration
# ---------------------------------------------------------------------------

class TestFloodWaitSleepDuration(unittest.TestCase):

    def test_flood_wait_sleep_duration(self):
        """Requirement 10.5 — FloodWait sleeps flood_wait.seconds + 5."""
        # Build a fake FloodWaitError with seconds=30
        class FakeFloodWaitError(Exception):
            seconds = 30

        flood_error = FakeFloodWaitError()
        # Give it the right class name so Sender recognises it
        flood_error.__class__.__name__ = "FloodWaitError"
        # Patch the class name at the type level
        FakeFloodWaitError.__name__ = "FloodWaitError"

        mock_client = MagicMock()
        mock_client.send_file = AsyncMock(side_effect=flood_error)

        s = _make_sender(max_retries=1)

        sleep_calls = []

        async def fake_sleep(duration):
            sleep_calls.append(duration)

        async def run():
            with patch("services.bulk_sender.sender.asyncio.sleep", side_effect=fake_sleep):
                try:
                    await s._send_file(mock_client, 123, "/tmp/file.jpg", 1)
                except RuntimeError:
                    pass  # retries exhausted — expected

        asyncio.run(run())

        # Should have slept 30 + 5 = 35 seconds for the FloodWait
        self.assertIn(35, sleep_calls)


# ---------------------------------------------------------------------------
# send_job — retry exhaustion skips file
# ---------------------------------------------------------------------------

class TestRetryExhaustion(unittest.TestCase):

    def test_retry_exhaustion_skips_file(self):
        """Requirement 10.3, 10.4 — Exhausted retries: increment_sent NOT called."""
        buf = BytesIO()
        img = Image.new("RGB", (1, 1))
        img.save(buf, format="JPEG")
        buf.seek(0)

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(buf.read())
            path = f.name

        try:
            s = _make_sender(max_retries=0)
            s.job_manager.resolve_file_list.return_value = [path]
            s.job_manager.is_already_sent.return_value = False

            stop_event = asyncio.Event()
            job = {"id": 1, "target_chat_id": 123}

            async def run():
                with patch.object(
                    s, "_send_file", new_callable=AsyncMock,
                    side_effect=RuntimeError("always fails")
                ):
                    await s.send_job(job, stop_event)

            asyncio.run(run())

            s.job_manager.increment_sent.assert_not_called()
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
