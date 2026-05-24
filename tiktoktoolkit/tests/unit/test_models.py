"""Unit tests for data models."""

import time
import pytest
from src.models import InvalidReason, InvalidUsernameRecord, ValidationResult


class TestInvalidReason:
    """Tests for InvalidReason enum."""
    
    def test_enum_values(self):
        """Test that all enum values are defined correctly."""
        assert InvalidReason.NOT_FOUND.value == "not_found"
        assert InvalidReason.ACCOUNT_DELETED.value == "account_deleted"
        assert InvalidReason.USERNAME_CHANGED.value == "username_changed"
        assert InvalidReason.PRIVATE_BANNED.value == "private_banned"
        assert InvalidReason.UNKNOWN.value == "unknown"
    
    def test_enum_members(self):
        """Test that all expected enum members exist."""
        expected_members = {
            "NOT_FOUND",
            "ACCOUNT_DELETED",
            "USERNAME_CHANGED",
            "PRIVATE_BANNED",
            "UNKNOWN"
        }
        actual_members = {member.name for member in InvalidReason}
        assert actual_members == expected_members
    
    def test_enum_from_value(self):
        """Test that enum can be created from string value."""
        assert InvalidReason("not_found") == InvalidReason.NOT_FOUND
        assert InvalidReason("account_deleted") == InvalidReason.ACCOUNT_DELETED


class TestInvalidUsernameRecord:
    """Tests for InvalidUsernameRecord dataclass."""
    
    def test_create_minimal_record(self):
        """Test creating record with required fields only."""
        timestamp = time.time()
        record = InvalidUsernameRecord(
            username="testuser",
            reason=InvalidReason.NOT_FOUND,
            detected_at=timestamp
        )
        
        assert record.username == "testuser"
        assert record.reason == InvalidReason.NOT_FOUND
        assert record.detected_at == timestamp
        assert record.error_message is None
        assert record.retry_count == 0
    
    def test_create_full_record(self):
        """Test creating record with all fields."""
        timestamp = time.time()
        record = InvalidUsernameRecord(
            username="testuser",
            reason=InvalidReason.ACCOUNT_DELETED,
            detected_at=timestamp,
            error_message="User account has been deleted",
            retry_count=3
        )
        
        assert record.username == "testuser"
        assert record.reason == InvalidReason.ACCOUNT_DELETED
        assert record.detected_at == timestamp
        assert record.error_message == "User account has been deleted"
        assert record.retry_count == 3
    
    def test_default_values(self):
        """Test that default values are set correctly."""
        record = InvalidUsernameRecord(
            username="testuser",
            reason=InvalidReason.UNKNOWN,
            detected_at=time.time()
        )
        
        assert record.error_message is None
        assert record.retry_count == 0
    
    def test_record_with_different_reasons(self):
        """Test creating records with different invalid reasons."""
        timestamp = time.time()
        
        for reason in InvalidReason:
            record = InvalidUsernameRecord(
                username=f"user_{reason.value}",
                reason=reason,
                detected_at=timestamp
            )
            assert record.reason == reason


class TestValidationResult:
    """Tests for ValidationResult dataclass."""
    
    def test_create_valid_result(self):
        """Test creating a result for valid username."""
        result = ValidationResult(
            is_valid=True,
            is_rate_limited=False,
            is_network_error=False
        )
        
        assert result.is_valid is True
        assert result.is_rate_limited is False
        assert result.is_network_error is False
        assert result.invalid_reason is None
        assert result.error_message is None
        assert result.should_retry is False
    
    def test_create_invalid_result(self):
        """Test creating a result for invalid username."""
        result = ValidationResult(
            is_valid=False,
            is_rate_limited=False,
            is_network_error=False,
            invalid_reason=InvalidReason.NOT_FOUND,
            error_message="User not found",
            should_retry=False
        )
        
        assert result.is_valid is False
        assert result.invalid_reason == InvalidReason.NOT_FOUND
        assert result.error_message == "User not found"
        assert result.should_retry is False
    
    def test_create_rate_limited_result(self):
        """Test creating a result for rate limited request."""
        result = ValidationResult(
            is_valid=False,
            is_rate_limited=True,
            is_network_error=False,
            error_message="Rate limit exceeded",
            should_retry=True
        )
        
        assert result.is_valid is False
        assert result.is_rate_limited is True
        assert result.is_network_error is False
        assert result.should_retry is True
        assert result.invalid_reason is None
    
    def test_create_network_error_result(self):
        """Test creating a result for network error."""
        result = ValidationResult(
            is_valid=False,
            is_rate_limited=False,
            is_network_error=True,
            error_message="Connection timeout",
            should_retry=True
        )
        
        assert result.is_valid is False
        assert result.is_rate_limited is False
        assert result.is_network_error is True
        assert result.should_retry is True
        assert result.invalid_reason is None
    
    def test_default_values(self):
        """Test that default values are set correctly."""
        result = ValidationResult(
            is_valid=False,
            is_rate_limited=False,
            is_network_error=False
        )
        
        assert result.invalid_reason is None
        assert result.error_message is None
        assert result.should_retry is False
    
    def test_all_invalid_reasons(self):
        """Test creating results with all invalid reasons."""
        for reason in InvalidReason:
            result = ValidationResult(
                is_valid=False,
                is_rate_limited=False,
                is_network_error=False,
                invalid_reason=reason,
                error_message=f"Error: {reason.value}"
            )
            assert result.invalid_reason == reason
            assert result.error_message == f"Error: {reason.value}"
