# Feature: bulk-sender-service, Property 2: Dedup Invariant
"""
Property test for Sender dedup invariant.

For any job and any file whose SHA-256 hash is already present in
bulk_sender.sent_items for that job, the Sender SHALL NOT call the Telegram
send API for that file, and SHALL NOT insert a second row into
bulk_sender.sent_items for that (job_id, file_hash) pair.

Validates: Requirements 4.2, 4.3, 4.4
"""

import asyncio
import hashlib
import os
import tempfile
from unittest.mock import MagicMock, patch

from hypothesis import given, settings, HealthCheck
import hypothesis.strategies as st

from services.bulk_sender.sender import Sender


def _hash_bytes(data: bytes) -> str:
    """Compute SHA-256 hex digest of raw bytes."""
    return hashlib.sha256(data).hexdigest()


@given(
    st.lists(
        st.text(
            alphabet=st.characters(whitelist_categories=('Ll', 'Nd')),
            min_size=1,
            max_size=10,
        ),
        min_size=1,
        max_size=10,
        unique=True,
    )
)
@settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
def test_dedup_invariant(stems: list) -> None:
    """For any already-sent file hashes, send_job never calls _send_file or
    record_sent_item for those files.

    **Validates: Requirements 4.2, 4.3, 4.4**
    """
    tmp_dir = tempfile.mkdtemp()
    tmp_files: list[str] = []

    try:
        # Create a real temp file for each stem so _compute_hash can read it
        file_contents: dict[str, bytes] = {}
        for stem in stems:
            content = stem.encode("utf-8") + b"\x00extra"
            path = os.path.join(tmp_dir, stem + ".jpg")
            with open(path, "wb") as f:
                f.write(content)
            tmp_files.append(path)
            file_contents[path] = content

        # Compute the real SHA-256 hash for each file
        file_hashes: dict[str, str] = {
            path: _hash_bytes(content)
            for path, content in file_contents.items()
        }

        # Mark the first half as "already sent"
        already_sent_paths = set(tmp_files[: len(tmp_files) // 2])
        already_sent_hashes = {file_hashes[p] for p in already_sent_paths}

        # Build the Sender with a mocked job_manager
        sender = Sender.__new__(Sender)
        sender.send_delay = 0.0
        sender.max_retries = 0
        sender.sessions_path = "/tmp"
        sender.bot_tokens = []

        mock_jm = MagicMock()
        mock_jm.resolve_file_list.return_value = list(tmp_files)
        mock_jm.is_already_sent.side_effect = (
            lambda job_id, file_hash: file_hash in already_sent_hashes
        )
        mock_jm.record_sent_item = MagicMock()
        mock_jm.increment_sent = MagicMock()
        sender.job_manager = mock_jm

        job = {"id": 1, "target_chat_id": 12345}
        stop_event = asyncio.Event()

        # Mock _send_file to track calls and return a fake message id
        send_file_calls: list[str] = []

        async def fake_send_file(client, target_chat_id, file_path, job_id):
            send_file_calls.append(file_path)
            return 999

        # Mock _validate_image to always pass (we only care about dedup here)
        with patch.object(sender, "_send_file", side_effect=fake_send_file), \
             patch.object(sender, "_validate_image", return_value=None):
            asyncio.run(sender.send_job(job, stop_event))

        # Assert _send_file was NEVER called for already-sent files
        for path in already_sent_paths:
            assert path not in send_file_calls, (
                f"_send_file was called for already-sent file: {path}"
            )

        # Assert record_sent_item was NEVER called for already-sent files
        record_calls = [
            call.args[1]  # file_path argument
            for call in mock_jm.record_sent_item.call_args_list
        ]
        for path in already_sent_paths:
            assert path not in record_calls, (
                f"record_sent_item was called for already-sent file: {path}"
            )

    finally:
        for path in tmp_files:
            if os.path.exists(path):
                os.unlink(path)
        if os.path.exists(tmp_dir):
            os.rmdir(tmp_dir)
