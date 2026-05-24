"""Unit tests for InvalidUsernameDetector — edge cases and specific patterns."""

import os
import socket
import tempfile
import pytest

from src.invalid_username_detector import InvalidUsernameDetector
from src.invalid_username_tracker import InvalidUsernameTracker
from src.models import InvalidReason


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def detector(tmp_path):
    tracker = InvalidUsernameTracker(db_path=str(tmp_path / "det.db"))
    return InvalidUsernameDetector(tracker=tracker)


@pytest.fixture
def tracker(tmp_path):
    return InvalidUsernameTracker(db_path=str(tmp_path / "trk.db"))


# ── is_rate_limit_error ───────────────────────────────────────────────────────

class TestIsRateLimitError:
    def test_http_429_is_rate_limit(self, detector):
        assert detector.is_rate_limit_error(Exception("error"), http_status=429)

    def test_rate_limit_in_message(self, detector):
        assert detector.is_rate_limit_error(Exception("rate limit exceeded"))

    def test_too_many_requests_in_message(self, detector):
        assert detector.is_rate_limit_error(Exception("too many requests"))

    def test_ratelimit_no_space(self, detector):
        assert detector.is_rate_limit_error(Exception("ratelimit hit"))

    def test_http_404_is_not_rate_limit(self, detector):
        assert not detector.is_rate_limit_error(Exception("not found"), http_status=404)

    def test_generic_error_is_not_rate_limit(self, detector):
        assert not detector.is_rate_limit_error(Exception("something went wrong"))

    def test_no_http_status_no_keywords_is_not_rate_limit(self, detector):
        assert not detector.is_rate_limit_error(Exception("connection error"))

    def test_case_insensitive_rate_limit(self, detector):
        assert detector.is_rate_limit_error(Exception("Rate Limit Exceeded"))

    def test_case_insensitive_too_many_requests(self, detector):
        assert detector.is_rate_limit_error(Exception("Too Many Requests"))


# ── is_not_found_error ────────────────────────────────────────────────────────

class TestIsNotFoundError:
    def test_http_404_is_not_found(self, detector):
        assert detector.is_not_found_error(Exception("error"), http_status=404)

    def test_user_not_found_message(self, detector):
        assert detector.is_not_found_error(Exception("user not found"))

    def test_account_doesnt_exist(self, detector):
        assert detector.is_not_found_error(Exception("account doesn't exist"))

    def test_couldnt_find_this_user(self, detector):
        assert detector.is_not_found_error(Exception("couldn't find this user"))

    def test_in_response_body(self, detector):
        assert detector.is_not_found_error(
            Exception("error"),
            response_body="user not found in our system",
        )

    def test_http_429_is_not_not_found(self, detector):
        assert not detector.is_not_found_error(Exception("rate limit"), http_status=429)

    def test_generic_error_is_not_not_found(self, detector):
        assert not detector.is_not_found_error(Exception("server error"), http_status=500)

    def test_case_insensitive(self, detector):
        assert detector.is_not_found_error(Exception("User Not Found"))


# ── is_network_error ──────────────────────────────────────────────────────────

class TestIsNetworkError:
    def test_connection_error_is_network(self, detector):
        assert detector.is_network_error(ConnectionError("refused"))

    def test_timeout_error_is_network(self, detector):
        assert detector.is_network_error(TimeoutError("timed out"))

    def test_os_error_is_network(self, detector):
        assert detector.is_network_error(OSError("network unreachable"))

    def test_socket_timeout_is_network(self, detector):
        assert detector.is_network_error(socket.timeout("timed out"))

    def test_socket_gaierror_is_network(self, detector):
        assert detector.is_network_error(socket.gaierror("name resolution failed"))

    def test_timeout_keyword_in_message(self, detector):
        assert detector.is_network_error(Exception("connection timed out"))

    def test_ssl_keyword_in_message(self, detector):
        assert detector.is_network_error(Exception("ssl handshake failed"))

    def test_value_error_is_not_network(self, detector):
        assert not detector.is_network_error(ValueError("invalid value"))

    def test_generic_exception_is_not_network(self, detector):
        assert not detector.is_network_error(Exception("user not found"))


# ── analyze_error ─────────────────────────────────────────────────────────────

class TestAnalyzeError:
    def test_404_returns_not_found_result(self, detector):
        result = detector.analyze_error("user", Exception("not found"), http_status=404)
        assert result.invalid_reason == InvalidReason.NOT_FOUND
        assert result.should_retry is False
        assert result.is_valid is False

    def test_429_returns_rate_limited_result(self, detector):
        result = detector.analyze_error("user", Exception("rate limit"), http_status=429)
        assert result.is_rate_limited is True
        assert result.should_retry is True
        assert result.invalid_reason is None

    def test_connection_error_returns_network_result(self, detector):
        result = detector.analyze_error("user", ConnectionError("refused"))
        assert result.is_network_error is True
        assert result.should_retry is True
        assert result.invalid_reason is None

    def test_account_deleted_message(self, detector):
        result = detector.analyze_error("user", Exception("account has been deleted"))
        assert result.invalid_reason == InvalidReason.ACCOUNT_DELETED
        assert result.should_retry is False

    def test_username_changed_message(self, detector):
        result = detector.analyze_error("user", Exception("username changed"))
        assert result.invalid_reason == InvalidReason.USERNAME_CHANGED
        assert result.should_retry is False

    def test_private_account_message(self, detector):
        result = detector.analyze_error("user", Exception("private account"))
        assert result.invalid_reason == InvalidReason.PRIVATE_BANNED
        assert result.should_retry is False

    def test_unknown_error_returns_unknown_reason(self, detector):
        result = detector.analyze_error("user", Exception("something completely unknown"))
        assert result.invalid_reason == InvalidReason.UNKNOWN
        assert result.should_retry is False

    def test_rate_limit_takes_priority_over_not_found(self, detector):
        """If both 429 and 'user not found' appear, rate limit wins."""
        result = detector.analyze_error(
            "user",
            Exception("user not found"),
            http_status=429,
        )
        assert result.is_rate_limited is True
        assert result.should_retry is True

    def test_network_error_takes_priority_over_not_found_message(self, detector):
        """ConnectionError with 'user not found' in message → network error wins."""
        result = detector.analyze_error(
            "user",
            ConnectionError("user not found in connection"),
        )
        assert result.is_network_error is True
        assert result.should_retry is True

    def test_missing_http_status_uses_message(self, detector):
        result = detector.analyze_error("user", Exception("user not found"))
        assert result.invalid_reason == InvalidReason.NOT_FOUND

    def test_none_response_body_handled(self, detector):
        result = detector.analyze_error("user", Exception("error"), response_body=None)
        assert result is not None  # must not raise

    def test_error_message_preserved_in_result(self, detector):
        result = detector.analyze_error("user", Exception("specific error text"), http_status=404)
        assert "specific error text" in result.error_message


# ── should_record_as_invalid ──────────────────────────────────────────────────

class TestShouldRecordAsInvalid:
    def test_not_found_is_recorded(self, tmp_path):
        tracker = InvalidUsernameTracker(db_path=str(tmp_path / "t.db"))
        detector = InvalidUsernameDetector(tracker=tracker)
        result = detector.analyze_error("user", Exception("not found"), http_status=404)
        assert detector.should_record_as_invalid("user", result) is True
        assert "user" in tracker.get_invalid_usernames()

    def test_rate_limit_not_recorded(self, tmp_path):
        tracker = InvalidUsernameTracker(db_path=str(tmp_path / "t.db"))
        detector = InvalidUsernameDetector(tracker=tracker)
        result = detector.analyze_error("user", Exception("rate limit"), http_status=429)
        assert detector.should_record_as_invalid("user", result) is False
        assert "user" not in tracker.get_invalid_usernames()

    def test_network_error_not_recorded(self, tmp_path):
        tracker = InvalidUsernameTracker(db_path=str(tmp_path / "t.db"))
        detector = InvalidUsernameDetector(tracker=tracker)
        result = detector.analyze_error("user", ConnectionError("refused"))
        assert detector.should_record_as_invalid("user", result) is False
        assert "user" not in tracker.get_invalid_usernames()

    def test_account_deleted_is_recorded(self, tmp_path):
        tracker = InvalidUsernameTracker(db_path=str(tmp_path / "t.db"))
        detector = InvalidUsernameDetector(tracker=tracker)
        result = detector.analyze_error("user", Exception("account has been deleted"))
        assert detector.should_record_as_invalid("user", result) is True
        assert "user" in tracker.get_invalid_usernames()

    def test_unknown_error_is_recorded(self, tmp_path):
        tracker = InvalidUsernameTracker(db_path=str(tmp_path / "t.db"))
        detector = InvalidUsernameDetector(tracker=tracker)
        result = detector.analyze_error("user", Exception("some unknown error"))
        assert detector.should_record_as_invalid("user", result) is True
