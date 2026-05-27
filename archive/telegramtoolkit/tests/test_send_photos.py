import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.managers.send_photos import PhotoSender, collect_send_photos_inputs


class PhotoSenderTests(unittest.TestCase):
    def setUp(self):
        self.original_setup_signal_handlers = PhotoSender._setup_signal_handlers
        PhotoSender._setup_signal_handlers = lambda self: None
        self.sender = PhotoSender()

    def tearDown(self):
        PhotoSender._setup_signal_handlers = self.original_setup_signal_handlers

    def test_operation_key_is_scoped_by_chat(self):
        file_hash = "abc123"
        accounts = ["acct1"]
        key_one = self.sender.create_progress_key("chat-one", "photos", accounts)
        key_two = self.sender.create_progress_key("chat-two", "photos", accounts)

        self.assertNotEqual(
            self.sender._create_operation_file_key(key_one, file_hash),
            self.sender._create_operation_file_key(key_two, file_hash),
        )

    def test_final_reason_reports_already_sent_run(self):
        results = {
            "acct1": {
                "sent": 0,
                "failed": 0,
                "skipped": 2,
                "skipped_already_sent": 2,
                "deleted": 0,
                "deleted_already_sent": 0,
                "invalid_removed": 0,
                "errors": [],
            }
        }
        scan_results = {"scanned": 2, "queued": 2, "invalid_found": 0, "invalid_removed": 0}

        reason = self.sender._build_final_reason(results, scan_results, active_workers=1)

        self.assertIn("already marked as sent", reason)

    def test_delete_skipped_already_sent_removes_file_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            photo_path = Path(tmpdir) / "photo.jpg"
            photo_path.write_bytes(b"test")
            results = {"deleted_already_sent": 0}

            self.sender._maybe_delete_skipped_already_sent(
                photo_path,
                "acct1",
                delete_after=True,
                delete_skipped_already_sent=True,
                results=results,
            )

            self.assertFalse(photo_path.exists())
            self.assertEqual(results["deleted_already_sent"], 1)

    def test_collect_send_photos_inputs_returns_shared_request_shape(self):
        responses = iter([
            "C:/photos",
            "@target_chat",
            "all",
            "y",
            "y",
            "y",
        ])

        with patch("builtins.input", side_effect=lambda _: next(responses)):
            request = collect_send_photos_inputs(["acct1", "acct2"])

        self.assertEqual(
            request,
            {
                "directory": "C:/photos",
                "chat_id": "target_chat",
                "accounts": ["all"],
                "delete_after": True,
                "delete_skipped_already_sent": True,
            },
        )


if __name__ == "__main__":
    unittest.main()
