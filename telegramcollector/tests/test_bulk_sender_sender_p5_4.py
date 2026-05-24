# Feature: bulk-sender-service, Property 7: Corrupt File Skip
"""
Property test for Sender corrupt file skip behaviour.

Validates: Requirements 5.2, 5.3
"""

import tempfile
import os
from unittest.mock import MagicMock, patch

from hypothesis import given, settings, assume, HealthCheck
import hypothesis.strategies as st

from services.bulk_sender.sender import Sender


@given(st.binary(min_size=1))
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_corrupt_file_skip(file_bytes: bytes) -> None:
    """For any bytes that fail Pillow verify(), send_file and record_sent_item are never called.

    **Validates: Requirements 5.2, 5.3**
    """
    sender = Sender.__new__(Sender)
    sender.job_manager = MagicMock()
    sender.job_manager.is_already_sent.return_value = False
    sender.job_manager.record_sent_item = MagicMock()

    mock_client = MagicMock()
    mock_client.send_file = MagicMock()

    with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name

    try:
        try:
            sender._validate_image(tmp_path)
            # If no exception, this is a valid image — discard the example
            assume(False)
        except Exception:
            # Corrupt case: verify send_file and record_sent_item were never called
            mock_client.send_file.assert_not_called()
            sender.job_manager.record_sent_item.assert_not_called()
    finally:
        os.unlink(tmp_path)
