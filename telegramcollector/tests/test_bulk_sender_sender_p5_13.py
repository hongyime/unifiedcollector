# Feature: bulk-sender-service, Property 5: Resume Completeness
"""
Property test for Sender resume completeness.

For any paused job with a non-empty sent_items set, when the job is resumed,
the Sender SHALL skip every file whose hash appears in sent_items for that job
and SHALL NOT send any such file a second time. The set of files sent after
resume SHALL be exactly the complement of the already-sent set within the full
file list.

Validates: Requirements 4.5, 8.2, 8.5
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
)
@settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
def test_resume_completeness(stems: list, split_index: int) -> None:
    """When a job is resumed, send_job sends exactly the complement of the
    already-sent subset — no more, no less.

    **Validates: Requirements 4.5, 8.2, 8.5**
    """
    tmp_dir = tempfile.mkdtemp()
    tmp_files: list[str] = []

    try:
        # Create a real temp file for each stem
        file_contents: dict[str, bytes] = {}
        for stem in stems:
            content = stem.encode("utf-8") + b"\x00resume"
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

        # Use split_index to pick the already-sent subset (clamped to list length)
        actual_split = split_index % (len(tmp_files) + 1)
        already_sent_paths = set(tmp_files[:actual_split])
        already_sent_hashes = {file_hashes[p] for p in already_sent_paths}
        expected_sent_paths = set(tmp_files[actual_split:])

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

        job = {"id": 42, "target_chat_id": 99999}
        stop_event = asyncio.Event()

        # Track which files _send_file was called for
        send_file_calls: list[str] = []

        async def fake_send_file(client, target_chat_id, file_path, job_id):
            send_file_calls.append(file_path)
            return 1

        with patch.object(sender, "_send_file", side_effect=fake_send_file), \
             patch.object(sender, "_validate_image", return_value=None):
            asyncio.run(sender.send_job(job, stop_event))

        actual_sent_paths = set(send_file_calls)

        # Assert _send_file was called for exactly the complement subset
        assert actual_sent_paths == expected_sent_paths, (
            f"Expected sends: {expected_sent_paths}, got: {actual_sent_paths}"
        )

        # Assert _send_file was NEVER called for already-sent files
        for path in already_sent_paths:
            assert path not in actual_sent_paths, (
                f"_send_file was called for already-sent file: {path}"
            )

    finally:
        for path in tmp_files:
            if os.path.exists(path):
                os.unlink(path)
        if os.path.exists(tmp_dir):
            os.rmdir(tmp_dir)
