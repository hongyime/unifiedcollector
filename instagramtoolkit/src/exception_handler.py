"""
Centralized exception handling for Instagram operations.

This module provides a structured approach to handling Instaloader exceptions
by categorizing them into recovery strategies rather than relying on string
matching against error messages.
"""
from __future__ import annotations

import instaloader.exceptions
from typing import Dict, Type, Callable, Any, Optional
from dataclasses import dataclass
from enum import Enum, auto


class RecoveryStrategy(Enum):
    """Recovery strategies for different exception types."""
    RETRY = auto()              # Retry with exponential backoff
    SWITCH_ACCOUNT = auto()     # Switch to another account and retry
    COOLDOWN = auto()           # Put account on cooldown, then retry
    LONG_COOLDOWN = auto()      # Extended cooldown (4x normal)
    SKIP = auto()               # Skip this operation entirely
    ABORT = auto()              # Abort the entire batch


@dataclass
class ExceptionPolicy:
    """Policy for handling a specific exception type."""
    strategy: RecoveryStrategy
    message: str                # User-friendly error message
    cooldown_minutes: Optional[int] = None  # Cooldown duration if applicable
    is_rate_limit: bool = False  # Whether this is a rate-limiting issue


# Comprehensive exception mapping
EXCEPTION_POLICY_MAP: Dict[Type[Exception], ExceptionPolicy] = {
    # --- Non-retryable client errors ---
    instaloader.exceptions.ProfileNotExistsException: ExceptionPolicy(
        strategy=RecoveryStrategy.SKIP,
        message="Profile does not exist",
    ),
    instaloader.exceptions.PrivateProfileNotFollowedException: ExceptionPolicy(
        strategy=RecoveryStrategy.SKIP,
        message="Cannot access private profile (not following)",
    ),
    instaloader.exceptions.LoginRequiredException: ExceptionPolicy(
        strategy=RecoveryStrategy.SWITCH_ACCOUNT,
        message="Login required - trying different account",
    ),
    instaloader.exceptions.BadCredentialsException: ExceptionPolicy(
        strategy=RecoveryStrategy.SWITCH_ACCOUNT,
        message="Invalid credentials - switching account",
    ),
    instaloader.exceptions.TwoFactorAuthRequiredException: ExceptionPolicy(
        strategy=RecoveryStrategy.SWITCH_ACCOUNT,
        message="2FA required - switching account",
    ),
    instaloader.exceptions.LoginException: ExceptionPolicy(
        strategy=RecoveryStrategy.SWITCH_ACCOUNT,
        message="Login failed - trying different account",
    ),
    
    # --- True rate-limiting (HTTP 429 / 403) — warrants long backoff ---
    instaloader.exceptions.TooManyRequestsException: ExceptionPolicy(
        strategy=RecoveryStrategy.COOLDOWN,
        message="Rate limited (429) - cooling down",
        cooldown_minutes=15,
        is_rate_limit=True,
    ),
    instaloader.exceptions.QueryReturnedForbiddenException: ExceptionPolicy(
        strategy=RecoveryStrategy.LONG_COOLDOWN,
        message="Forbidden (403) - extended cooldown required",
        cooldown_minutes=60,
        is_rate_limit=True,
    ),

    # --- Transient API errors — retry quickly, NOT rate-limit floor ---
    instaloader.exceptions.ConnectionException: ExceptionPolicy(
        strategy=RecoveryStrategy.RETRY,
        message="Connection error - will retry",
        is_rate_limit=False,  # network blip, not a 429 — retry in seconds not minutes
    ),
    instaloader.exceptions.QueryReturnedBadRequestException: ExceptionPolicy(
        strategy=RecoveryStrategy.RETRY,
        message="Bad request - will retry",
        is_rate_limit=False,
    ),
    instaloader.exceptions.QueryReturnedNotFoundException: ExceptionPolicy(
        strategy=RecoveryStrategy.SKIP,
        message="Query returned not found",
    ),
    instaloader.exceptions.BadResponseException: ExceptionPolicy(
        strategy=RecoveryStrategy.RETRY,
        message="Bad response - will retry",
        is_rate_limit=False,
    ),

    # --- Network and system errors — retry quickly ---
    ConnectionError: ExceptionPolicy(
        strategy=RecoveryStrategy.RETRY,
        message="Network connection error - will retry",
        is_rate_limit=False,
    ),
    TimeoutError: ExceptionPolicy(
        strategy=RecoveryStrategy.RETRY,
        message="Request timeout - will retry",
        is_rate_limit=False,
    ),
    OSError: ExceptionPolicy(
        strategy=RecoveryStrategy.RETRY,
        message="System error - will retry",
        is_rate_limit=False,
    ),
}


def get_exception_policy(exception: Exception) -> Optional[ExceptionPolicy]:
    """
    Get the recovery policy for an exception.
    
    Searches the exception hierarchy to find the most specific policy.
    Returns None if no policy is found (treat as non-recoverable).
    """
    # Check exact type first
    exc_type = type(exception)
    if exc_type in EXCEPTION_POLICY_MAP:
        return EXCEPTION_POLICY_MAP[exc_type]
    
    # Check base classes (MRO)
    for base_class in exc_type.__mro__[1:]:  # Skip the exact type
        if base_class in EXCEPTION_POLICY_MAP:
            return EXCEPTION_POLICY_MAP[base_class]
    
    # Check for rate limit phrases in error message as fallback
    error_msg = str(exception).lower()
    rate_limit_phrases = (
        "please wait a few minutes",
        "rate limit",
        "too many requests",
        "temporarily blocked",
        "401 unauthorized",
        "try again later",
    )
    if any(phrase in error_msg for phrase in rate_limit_phrases):
        return ExceptionPolicy(
            strategy=RecoveryStrategy.COOLDOWN,
            message="Rate limit detected (from message)",
            cooldown_minutes=15,
            is_rate_limit=True,
        )
    
    challenge_phrases = (
        "checkpoint_required",
        "challenge_required",
        "consent_required",
        "feedback_required",
        "login_required",
        "suspicious activity",
        "account has been disabled",
        "your account has been temporarily locked",
    )
    if any(phrase in error_msg for phrase in challenge_phrases):
        return ExceptionPolicy(
            strategy=RecoveryStrategy.LONG_COOLDOWN,
            message="Challenge required (from message)",
            cooldown_minutes=60,
        )
    
    return None


def is_retryable_exception(exception: Exception) -> bool:
    """Check if an exception should be retried."""
    policy = get_exception_policy(exception)
    if policy is None:
        return False
    return policy.strategy in (
        RecoveryStrategy.RETRY,
        RecoveryStrategy.COOLDOWN,
        RecoveryStrategy.LONG_COOLDOWN,
    )


def should_switch_account(exception: Exception) -> bool:
    """Check if account switching is the appropriate recovery."""
    policy = get_exception_policy(exception)
    if policy is None:
        return False
    return policy.strategy == RecoveryStrategy.SWITCH_ACCOUNT


def get_cooldown_minutes(exception: Exception) -> Optional[int]:
    """Get cooldown duration for an exception, if applicable."""
    policy = get_exception_policy(exception)
    if policy is None:
        return None
    return policy.cooldown_minutes


def is_rate_limit_exception(exception: Exception) -> bool:
    """Check if exception is rate-limit related."""
    policy = get_exception_policy(exception)
    if policy is None:
        return False
    return policy.is_rate_limit


def format_exception_message(exception: Exception) -> str:
    """
    Format an exception with its recovery strategy for logging.
    
    Returns a user-friendly message including the recovery action.
    """
    policy = get_exception_policy(exception)
    if policy is None:
        return f"Non-recoverable error: {exception}"
    
    strategy_names = {
        RecoveryStrategy.RETRY: "will retry",
        RecoveryStrategy.SWITCH_ACCOUNT: "switching account",
        RecoveryStrategy.COOLDOWN: f"cooldown {policy.cooldown_minutes}m",
        RecoveryStrategy.LONG_COOLDOWN: f"extended cooldown {policy.cooldown_minutes}m",
        RecoveryStrategy.SKIP: "skipping",
        RecoveryStrategy.ABORT: "aborting batch",
    }
    
    action = strategy_names.get(policy.strategy, "unknown action")
    return f"{policy.message} [{action}]"


# Legacy compatibility functions
def is_challenge_exception(exception: Exception) -> bool:
    """Check if exception requires manual intervention (legacy)."""
    policy = get_exception_policy(exception)
    if policy is None:
        return False
    return policy.strategy == RecoveryStrategy.LONG_COOLDOWN


def is_account_switch_exception(exception: Exception) -> bool:
    """Check if exception should trigger account switching (legacy)."""
    return should_switch_account(exception)


__all__ = [
    "RecoveryStrategy",
    "ExceptionPolicy",
    "EXCEPTION_POLICY_MAP",
    "get_exception_policy",
    "is_retryable_exception",
    "should_switch_account",
    "get_cooldown_minutes",
    "is_rate_limit_exception",
    "format_exception_message",
    "is_challenge_exception",  # Legacy
    "is_account_switch_exception",  # Legacy
]


