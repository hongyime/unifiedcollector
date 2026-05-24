"""Property tests for InvalidUsernameDetector.

Property 1: HTTP 404 Classification
Property 2: Rate Limit Non-Recording
Property 3: Network Error Non-Recording
"""

import os
import tempfile
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from src.invalid_username_detector import InvalidUsernameDetector
from src.invalid_username_tracker import InvalidUsernameTracker
from src.models import InvalidReason


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_detector() -> InvalidUsernameDetector:
    """Create a detector backed by a fresh temp-file tracker."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    tracker = InvalidUsernameTracker(db_path=path)
    return InvalidUsernameDetector(tracker=tracker)


_username_st = st.text(
    alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd")),
    min_size=1,
    max_size=30,
)

_error_msg_st = st.text(max_size=200)


# ── Property 1: HTTP 404 Classification ──────────────────────────────────────

class TestHTTP404Classification:
    """Property 1: HTTP 404 errors are classified as NOT_FOUND with should_retry=False.

    Validates: Requirements 1.1, 7.3
    """

    @given(username=_username_st, msg=_error_msg_st)
    @settings(max_examples=30)
    def test_http_404_classified_as_not_found(self, username, msg):
        detector = _make_detector()
        result = detector.analyze_error(username, Exception(msg), http_status=404)

        assert result.invalid_reason == InvalidReason.NOT_FOUND
        assert result.should_retry is False
        assert result.is_valid is False
        assert result.is_rate_limited is False
        assert result.is_network_error is False

    @given(username=_username_st, msg=_error_msg_st)
    @settings(max_examples=30)
    def test_http_404_should_record_as_invalid(self, username, msg):
        detector = _make_detector()
        result = detector.analyze_error(username, Exception(msg), http_status=404)
        recorded = detector.should_record_as_invalid(username, result)
        assert recorded is True

    def test_not_found_message_classified_correctly(self):
        detector = _make_detector()
        result = detector.analyze_error(
            "testuser",
            Exception("user not found"),
        )
        assert result.invalid_reason == InvalidReason.NOT_FOUND
        assert result.should_retry is False

    def test_account_doesnt_exist_classified_correctly(self):
        detector = _make_detector()
        result = detector.analyze_error(
            "testuser",
            Exception("account doesn't exist"),
        )
        assert result.invalid_reason == InvalidReason.NOT_FOUND
        assert result.should_retry is False


# ── Property 2: Rate Limit Non-Recording ─────────────────────────────────────

class TestRateLimitNonRecording:
    """Property 2: Rate limit errors have should_retry=True and are NOT recorded.

    Validates: Requirements 1.2, 7.2, 7.5
    """

    @given(username=_username_st, msg=_error_msg_st)
    @settings(max_examples=30)
    def test_http_429_has_should_retry_true(self, username, msg):
        detector = _make_detector()
        result = detector.analyze_error(username, Exception(msg), http_status=429)

        assert result.is_rate_limited is True
        assert result.should_retry is True
        assert result.is_valid is False

    @given(username=_username_st, msg=_error_msg_st)
    @settings(max_examples=30)
    def test_http_429_not_recorded_as_invalid(self, username, msg):
        detector = _make_detector()
        result = detector.analyze_error(username, Exception(msg), http_status=429)
        recorded = detector.should_record_as_invalid(username, result)
        assert recorded is False

    @given(username=_username_st)
    @settings(max_examples=20)
    def test_rate_limit_message_not_recorded(self, username):
        detector = _make_detector()
        result = detector.analyze_error(username, Exception("rate limit exceeded"))
        assert result.is_rate_limited is True
        assert result.should_retry is True
        assert detector.should_record_as_invalid(username, result) is False

    @given(username=_username_st)
    @settings(max_examples=20)
    def test_too_many_requests_message_not_recorded(self, username):
        detector = _make_detector()
        result = detector.analyze_error(username, Exception("too many requests"))
        assert result.is_rate_limited is True
        assert result.should_retry is True
        assert detector.should_record_as_invalid(username, result) is False

    def test_rate_limited_username_not_in_tracker(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(path)
        tracker = InvalidUsernameTracker(db_path=path)
        detector = InvalidUsernameDetector(tracker=tracker)

        result = detector.analyze_error("ratelimited_user", Exception("rate limit"), http_status=429)
        detector.should_record_as_invalid("ratelimited_user", result)

        assert "ratelimited_user" not in tracker.get_invalid_usernames()


# ── Property 3: Network Error Non-Recording ───────────────────────────────────

class TestNetworkErrorNonRecording:
    """Property 3: Network errors have should_retry=True and are NOT recorded.

    Validates: Requirements 1.3, 7.4, 7.5
    """

    @given(username=_username_st)
    @settings(max_examples=20)
    def test_connection_error_not_recorded(self, username):
        detector = _make_detector()
        result = detector.analyze_error(username, ConnectionError("connection refused"))
        assert result.is_network_error is True
        assert result.should_retry is True
        assert detector.should_record_as_invalid(username, result) is False

    @given(username=_username_st)
    @settings(max_examples=20)
    def test_timeout_error_not_recorded(self, username):
        detector = _make_detector()
        result = detector.analyze_error(username, TimeoutError("timed out"))
        assert result.is_network_error is True
        assert result.should_retry is True
        assert detector.should_record_as_invalid(username, result) is False

    @given(username=_username_st)
    @settings(max_examples=20)
    def test_os_error_not_recorded(self, username):
        detector = _make_detector()
        result = detector.analyze_error(username, OSError("network unreachable"))
        assert result.is_network_error is True
        assert result.should_retry is True
        assert detector.should_record_as_invalid(username, result) is False

    def test_network_error_username_not_in_tracker(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(path)
        tracker = InvalidUsernameTracker(db_path=path)
        detector = InvalidUsernameDetector(tracker=tracker)

        result = detector.analyze_error("network_user", ConnectionError("connection reset"))
        detector.should_record_as_invalid("network_user", result)

        assert "network_user" not in tracker.get_invalid_usernames()

    @given(username=_username_st)
    @settings(max_examples=20)
    def test_network_error_is_not_rate_limited(self, username):
        detector = _make_detector()
        result = detector.analyze_error(username, ConnectionError("connection refused"))
        assert result.is_rate_limited is False
        assert result.is_network_error is True
