"""Tests for src/exception_handler.py — centralized exception handling."""
import pytest
import instaloader.exceptions
from exception_handler import (
    get_exception_policy,
    is_retryable_exception,
    should_switch_account,
    get_cooldown_minutes,
    is_rate_limit_exception,
    format_exception_message,
    is_challenge_exception,
    is_account_switch_exception,
    RecoveryStrategy,
)


class TestExceptionPolicyLookup:
    """Test exception policy mapping and lookup."""
    
    def test_profile_not_exists_returns_skip_policy(self):
        exc = instaloader.exceptions.ProfileNotExistsException("Profile not found")
        policy = get_exception_policy(exc)
        
        assert policy is not None
        assert policy.strategy == RecoveryStrategy.SKIP
        assert "does not exist" in policy.message
    
    def test_private_profile_not_followed_returns_skip(self):
        exc = instaloader.exceptions.PrivateProfileNotFollowedException("Private")
        policy = get_exception_policy(exc)
        
        assert policy is not None
        assert policy.strategy == RecoveryStrategy.SKIP
    
    def test_bad_credentials_returns_switch_account(self):
        exc = instaloader.exceptions.BadCredentialsException("Bad credentials")
        policy = get_exception_policy(exc)
        
        assert policy is not None
        assert policy.strategy == RecoveryStrategy.SWITCH_ACCOUNT
    
    def test_connection_exception_returns_retry(self):
        exc = instaloader.exceptions.ConnectionException("Connection error")
        policy = get_exception_policy(exc)
        
        assert policy is not None
        assert policy.strategy == RecoveryStrategy.RETRY
        assert policy.is_rate_limit is False  # network blip, not a 429
    
    def test_too_many_requests_returns_cooldown(self):
        exc = instaloader.exceptions.TooManyRequestsException("429")
        policy = get_exception_policy(exc)
        
        assert policy is not None
        assert policy.strategy == RecoveryStrategy.COOLDOWN
        assert policy.cooldown_minutes == 15
        assert policy.is_rate_limit is True
    
    def test_forbidden_returns_long_cooldown(self):
        exc = instaloader.exceptions.QueryReturnedForbiddenException("403")
        policy = get_exception_policy(exc)
        
        assert policy is not None
        assert policy.strategy == RecoveryStrategy.LONG_COOLDOWN
        assert policy.cooldown_minutes == 60


class TestHelperFunctions:
    """Test convenience helper functions."""
    
    def test_is_retryable_exception_true_for_retry_policy(self):
        exc = instaloader.exceptions.ConnectionException("Connection error")
        assert is_retryable_exception(exc) is True
    
    def test_is_retryable_exception_false_for_skip_policy(self):
        exc = instaloader.exceptions.ProfileNotExistsException("Not found")
        assert is_retryable_exception(exc) is False
    
    def test_should_switch_account_true_for_bad_credentials(self):
        exc = instaloader.exceptions.BadCredentialsException("Bad")
        assert should_switch_account(exc) is True
    
    def test_should_switch_account_false_for_other_errors(self):
        exc = instaloader.exceptions.ConnectionException("Error")
        assert should_switch_account(exc) is False
    
    def test_get_cooldown_minutes_returns_value_for_cooldown_policy(self):
        exc = instaloader.exceptions.TooManyRequestsException("429")
        minutes = get_cooldown_minutes(exc)
        assert minutes == 15
    
    def test_get_cooldown_minutes_returns_none_for_non_cooldown(self):
        exc = instaloader.exceptions.ProfileNotExistsException("Not found")
        minutes = get_cooldown_minutes(exc)
        assert minutes is None
    
    def test_is_rate_limit_exception_false_for_connection_error(self):
        # ConnectionException is a transient network error, NOT a 429 — should retry quickly
        exc = instaloader.exceptions.ConnectionException("Connection error")
        assert is_rate_limit_exception(exc) is False
    
    def test_is_rate_limit_exception_false_for_skip(self):
        exc = instaloader.exceptions.ProfileNotExistsException("Not found")
        assert is_rate_limit_exception(exc) is False


class TestFallbackStringMatching:
    """Test fallback string matching for unknown exceptions."""
    
    def test_rate_limit_phrase_in_unknown_exception(self):
        exc = Exception("You are being rate limited. please wait a few minutes")
        policy = get_exception_policy(exc)
        
        assert policy is not None
        assert policy.strategy == RecoveryStrategy.COOLDOWN
        assert policy.is_rate_limit is True
    
    def test_challenge_phrase_in_unknown_exception(self):
        exc = Exception("checkpoint_required: verify your account")
        policy = get_exception_policy(exc)
        
        assert policy is not None
        assert policy.strategy == RecoveryStrategy.LONG_COOLDOWN


class TestFormatExceptionMessage:
    """Test exception message formatting."""
    
    def test_formats_skip_exception(self):
        exc = instaloader.exceptions.ProfileNotExistsException("Not found")
        msg = format_exception_message(exc)
        
        assert "does not exist" in msg
        assert "skipping" in msg
    
    def test_formats_cooldown_exception(self):
        exc = instaloader.exceptions.TooManyRequestsException("429")
        msg = format_exception_message(exc)
        
        assert "cooldown" in msg or "Rate limited" in msg
        assert "15m" in msg
    
    def test_formats_unknown_exception(self):
        exc = Exception("Some random error")
        msg = format_exception_message(exc)
        
        assert "Non-recoverable" in msg or "random error" in msg


class TestLegacyCompatibility:
    """Test legacy compatibility functions."""
    
    def test_is_challenge_exception_true_for_forbidden(self):
        exc = instaloader.exceptions.QueryReturnedForbiddenException("403")
        assert is_challenge_exception(exc) is True  # Forbidden triggers LONG_COOLDOWN
    
    def test_is_challenge_exception_false_for_other(self):
        exc = instaloader.exceptions.ConnectionException("Error")
        assert is_challenge_exception(exc) is False  # ConnectionException triggers RETRY
    
    def test_is_account_switch_exception_true_for_bad_credentials(self):
        exc = instaloader.exceptions.BadCredentialsException("Bad")
        assert is_account_switch_exception(exc) is True
    
    def test_is_account_switch_exception_false_for_other(self):
        exc = instaloader.exceptions.ProfileNotExistsException("Not found")
        assert is_account_switch_exception(exc) is False
