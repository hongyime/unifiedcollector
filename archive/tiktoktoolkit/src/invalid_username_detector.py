"""Invalid username detection and error classification."""

from __future__ import annotations

import socket
from typing import Optional

from .models import InvalidReason, ValidationResult
from .invalid_username_tracker import InvalidUsernameTracker


# Network exception types that indicate transient failures
_NETWORK_EXCEPTION_TYPES = (
    ConnectionError,
    TimeoutError,
    OSError,
    socket.timeout,
    socket.gaierror,
)

# Keywords that indicate rate limiting
_RATE_LIMIT_KEYWORDS = (
    "rate limit",
    "too many requests",
    "ratelimit",
    "rate_limit",
    "429",
)

# Keywords that indicate user not found
_NOT_FOUND_KEYWORDS = (
    "user not found",
    "account doesn't exist",
    "account does not exist",
    "couldn't find this user",
    "could not find this user",
    "no user found",
    "user does not exist",
    "user doesn't exist",
    "404",
)

# Keywords that indicate account deleted
_DELETED_KEYWORDS = (
    "account deleted",
    "account has been deleted",
    "account was deleted",
    "this account has been deleted",
)

# Keywords that indicate username changed
_CHANGED_KEYWORDS = (
    "username changed",
    "username has changed",
    "account moved",
)

# Keywords that indicate private/banned account
_PRIVATE_BANNED_KEYWORDS = (
    "private account",
    "account is private",
    "account banned",
    "account has been banned",
    "account suspended",
)


def _lower_str(value: object) -> str:
    """Safely convert a value to lowercase string."""
    if value is None:
        return ""
    return str(value).lower()


class InvalidUsernameDetector:
    """Detects and classifies username validation failures.

    Analyzes API errors, HTTP status codes, and response bodies to
    determine whether a username is genuinely invalid or experiencing
    a transient issue (rate limit, network error).
    """

    def __init__(self, tracker: InvalidUsernameTracker):
        """Initialize detector with tracker reference.

        Args:
            tracker: InvalidUsernameTracker to record confirmed invalid usernames
        """
        self._tracker = tracker

    # ── Public classification helpers ────────────────────────────────────────

    def is_rate_limit_error(
        self,
        exception: Exception,
        http_status: Optional[int] = None,
    ) -> bool:
        """Determine if error indicates rate limiting.

        Checks for:
        - HTTP 429 status
        - "rate limit" in error message
        - "too many requests" in error message

        Args:
            exception: Exception raised during validation
            http_status: HTTP status code if available

        Returns:
            True if this looks like a rate limit error
        """
        if http_status == 429:
            return True
        msg = _lower_str(exception)
        return any(kw in msg for kw in _RATE_LIMIT_KEYWORDS)

    def is_not_found_error(
        self,
        exception: Exception,
        http_status: Optional[int] = None,
        response_body: Optional[str] = None,
    ) -> bool:
        """Determine if error indicates user not found.

        Checks for:
        - HTTP 404 status
        - "user not found" / "account doesn't exist" patterns

        Args:
            exception: Exception raised during validation
            http_status: HTTP status code if available
            response_body: Response body text if available

        Returns:
            True if this looks like a not-found error
        """
        if http_status == 404:
            return True
        combined = _lower_str(exception) + " " + _lower_str(response_body)
        return any(kw in combined for kw in _NOT_FOUND_KEYWORDS)

    def is_network_error(self, exception: Exception) -> bool:
        """Determine if error is a transient network failure.

        Args:
            exception: Exception raised during validation

        Returns:
            True if this is a network/connectivity error
        """
        if isinstance(exception, _NETWORK_EXCEPTION_TYPES):
            return True
        msg = _lower_str(exception)
        network_keywords = (
            "connection refused",
            "connection reset",
            "connection timed out",
            "network unreachable",
            "name or service not known",
            "temporary failure in name resolution",
            "ssl",
            "timed out",
            "timeout",
        )
        return any(kw in msg for kw in network_keywords)

    # ── Core analysis ─────────────────────────────────────────────────────────

    def analyze_error(
        self,
        username: str,
        exception: Exception,
        http_status: Optional[int] = None,
        response_body: Optional[str] = None,
    ) -> ValidationResult:
        """Analyze an error to determine if username is invalid.

        Args:
            username: Username that failed validation
            exception: Exception raised during validation
            http_status: HTTP status code if available
            response_body: Response body text if available

        Returns:
            ValidationResult with classification
        """
        error_message = str(exception)

        # Rate limit check (highest priority — never record as invalid)
        if self.is_rate_limit_error(exception, http_status):
            return ValidationResult(
                is_valid=False,
                is_rate_limited=True,
                is_network_error=False,
                invalid_reason=None,
                error_message=error_message,
                should_retry=True,
            )

        # Network error check (transient — never record as invalid)
        if self.is_network_error(exception):
            return ValidationResult(
                is_valid=False,
                is_rate_limited=False,
                is_network_error=True,
                invalid_reason=None,
                error_message=error_message,
                should_retry=True,
            )

        # Permanent failure classification
        combined = _lower_str(exception) + " " + _lower_str(response_body)

        if self.is_not_found_error(exception, http_status, response_body):
            return ValidationResult(
                is_valid=False,
                is_rate_limited=False,
                is_network_error=False,
                invalid_reason=InvalidReason.NOT_FOUND,
                error_message=error_message,
                should_retry=False,
            )

        if any(kw in combined for kw in _DELETED_KEYWORDS):
            return ValidationResult(
                is_valid=False,
                is_rate_limited=False,
                is_network_error=False,
                invalid_reason=InvalidReason.ACCOUNT_DELETED,
                error_message=error_message,
                should_retry=False,
            )

        if any(kw in combined for kw in _CHANGED_KEYWORDS):
            return ValidationResult(
                is_valid=False,
                is_rate_limited=False,
                is_network_error=False,
                invalid_reason=InvalidReason.USERNAME_CHANGED,
                error_message=error_message,
                should_retry=False,
            )

        if any(kw in combined for kw in _PRIVATE_BANNED_KEYWORDS):
            return ValidationResult(
                is_valid=False,
                is_rate_limited=False,
                is_network_error=False,
                invalid_reason=InvalidReason.PRIVATE_BANNED,
                error_message=error_message,
                should_retry=False,
            )

        # Unknown permanent failure
        return ValidationResult(
            is_valid=False,
            is_rate_limited=False,
            is_network_error=False,
            invalid_reason=InvalidReason.UNKNOWN,
            error_message=error_message,
            should_retry=False,
        )

    def should_record_as_invalid(
        self,
        username: str,
        validation_result: ValidationResult,
    ) -> bool:
        """Determine if this failure should be recorded as invalid.

        Returns False for rate limit errors, network errors, and transient
        failures. Returns True for permanent failures (NOT_FOUND, ACCOUNT_DELETED,
        USERNAME_CHANGED, PRIVATE_BANNED, UNKNOWN) and records them in the tracker.

        Args:
            username: Username that failed validation
            validation_result: Result from analyze_error()

        Returns:
            True if the username was recorded as invalid
        """
        if validation_result.should_retry:
            return False
        if validation_result.is_rate_limited or validation_result.is_network_error:
            return False
        if validation_result.invalid_reason is None:
            return False

        self._tracker.record_invalid(
            username,
            validation_result.invalid_reason,
            error_message=validation_result.error_message,
        )
        return True
