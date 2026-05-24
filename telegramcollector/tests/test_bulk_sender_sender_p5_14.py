# Feature: bulk-sender-service, Property 11: Sent Count Accuracy
"""
Property test for Sender sent count accuracy.

For any job execution, bulk_sender.send_jobs.sent_count at completion SHALL
equal the exact number of files for which record_sent_item() was successfully
called. Files skipped due to dedup, validation failure, or exhausted retries
SHALL NOT be counted.

Validates: Requirements 2.5, 2.7, 5.3, 10.4
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
            alphabet=st.characters(whitelist_categories=("Ll", "Nd")),
            min_size=1,
            max_size=10,
        ),
        min_size=1,
        max_size=10,
        unique=True,
    ),
    st.integers(min_value=0, max_value=10),
    st.integers(min_value=0, max_value=10),
)
@settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
def test_sent_count_accuracy(
    stems: list,
    already_sent_count: int,
    corrupt_count: int,
) -> None:
    """increment_sent is called exactly N times, where N = files that passed
    all checks (not already-sent, not corrupt).

    **Validates: Requirements 2.5, 2.7, 5.3, 10.4**
    """
    tmp_dir = tempfile.mkdtemp()
    tmp_files: list[str] = []

    try:
        # Create a real temp file for each stem
        file_contents: dict[str, bytes] = {}
        for stem in stems:
            content = stem.encode("utf-8") + b"\x00accuracy"
            path = os.path.join(tmp_dir, stem + ".jpg")
            with open(path, "wb") as f:
                f.write(content)
            tmp_files.append(path)
            file_contents[path] = content

        # Compute real SHA-256 hashes
        file_hashes: dict[str, str] = {
            path: _hash_bytes(content)
            for path, content in file_contents.items()
        }

        n = len(tmp_files)

        # Clamp counts to valid range
        actual_already_sent = min(already_sent_count, n)
        already_sent_paths = set(tmp_files[:actual_already_sent])
        already_sent_hashes = {file_hashes[p] for p in already_sent_paths}

        # Corrupt files are chosen from the remaining (non-already-sent) files
        remaining_paths = [p for p in tmp_files if p not in already_sent_paths]
        actual_corrupt = min(corrupt_count, len(remaining_paths))
        corrupt_paths = set(remaining_paths[:actual_corrupt])

        # Expected: files that are neither already-sent nor corrupt
        expected_sent_count = n - actual_already_sent - actual_corrupt

        # Build Sender with mocked job_manager
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

        job = {"id": 7, "target_chat_id": 55555}
        stop_event = asyncio.Event()

        async def fake_send_file(client, target_chat_id, file_path, job_id):
            return 42

        def fake_validate_image(file_path: str) -> None:
            if file_path in corrupt_paths:
                raise ValueError(f"Corrupt image: {file_path}")

        with patch.object(sender, "_send_file", side_effect=fake_send_file), \
             patch.object(sender, "_validate_image", side_effect=fake_validate_image):
            asyncio.run(sender.send_job(job, stop_event))

        actual_increment_calls = mock_jm.increment_sent.call_count

        assert actual_increment_calls == expected_sent_count, (
            f"Expected increment_sent called {expected_sent_count} times, "
            f"got {actual_increment_calls}. "
            f"total={n}, already_sent={actual_already_sent}, corrupt={actual_corrupt}"
        )

    finally:
        for path in tmp_files:
            if os.path.exists(path):
                os.unlink(path)
        if os.path.exists(tmp_dir):
            os.rmdir(tmp_dir)
