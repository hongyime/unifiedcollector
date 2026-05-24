"""
Property-based test for BulkSenderService — Property 6: Rate Minimum Enforcement.

Feature: bulk-sender-service, Property 6: Rate Minimum Enforcement
Validates: Requirements 3.2, 13.6
"""

from unittest.mock import MagicMock, patch

from hypothesis import given, settings
import hypothesis.strategies as st

from services.bulk_sender.main import BulkSenderService


@given(st.floats(allow_nan=False, allow_infinity=False))
@settings(max_examples=100)
def test_rate_minimum_enforcement(v: float) -> None:
    # Feature: bulk-sender-service, Property 6: Rate Minimum Enforcement
    job_manager = MagicMock()
    sender = MagicMock()

    with patch("shared.config.settings.BULK_SENDER_SEND_DELAY", v, create=False), \
         patch("services.bulk_sender.main.logger") as mock_logger:
        svc = BulkSenderService(job_manager=job_manager, sender=sender)

    assert svc._effective_delay >= 1.0

    if v < 1.0:
        mock_logger.warning.assert_called_once()
