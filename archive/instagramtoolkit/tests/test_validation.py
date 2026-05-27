"""Tests for src/validation.py — input validation utilities."""
import pytest
import os
import tempfile
from validation import (
    validate_username,
    validate_username_list,
    validate_file_path,
    validate_directory,
    validate_instagram_accounts,
    validate_download_limit,
    validate_max_relationships,
    ValidationError,
    safe_validate,
)


class TestValidateUsername:
    """Test username validation."""
    
    def test_valid_username_simple(self):
        is_valid, error = validate_username("therock")
        assert is_valid is True
        assert error == ""
    
    def test_valid_username_with_numbers(self):
        is_valid, error = validate_username("user123")
        assert is_valid is True
    
    def test_valid_username_with_underscores(self):
        is_valid, error = validate_username("user_name")
        assert is_valid is True
    
    def test_valid_username_with_dots(self):
        is_valid, error = validate_username("user.name")
        assert is_valid is True
    
    def test_valid_username_with_hyphens(self):
        is_valid, error = validate_username("user-name")
        assert is_valid is True
    
    def test_valid_username_max_length(self):
        is_valid, error = validate_username("a" * 30)
        assert is_valid is True
    
    def test_empty_username(self):
        is_valid, error = validate_username("")
        assert is_valid is False
        assert "cannot be empty" in error.lower()
    
    def test_username_too_long(self):
        is_valid, error = validate_username("a" * 31)
        assert is_valid is False
        assert "too long" in error.lower()
    
    def test_username_with_invalid_chars(self):
        is_valid, error = validate_username("user@name")
        assert is_valid is False
        assert "invalid characters" in error.lower()
    
    def test_username_with_spaces(self):
        is_valid, error = validate_username("user name")
        assert is_valid is False
    
    def test_username_starts_with_period(self):
        is_valid, error = validate_username(".username")
        assert is_valid is False
        assert "cannot start or end with a period" in error.lower()
    
    def test_username_ends_with_period(self):
        is_valid, error = validate_username("username.")
        assert is_valid is False
    
    def test_username_consecutive_periods(self):
        is_valid, error = validate_username("user..name")
        assert is_valid is False
    
    def test_username_with_leading_trailing_whitespace(self):
        is_valid, error = validate_username("  therock  ")
        assert is_valid is True  # Should strip and validate
    
    def test_username_not_string(self):
        is_valid, error = validate_username(123)
        assert is_valid is False
        assert "must be a string" in error.lower()


class TestValidateUsernameList:
    """Test username list validation."""
    
    def test_all_valid_usernames(self):
        valid, invalid = validate_username_list(["alice", "bob", "charlie"])
        assert len(valid) == 3
        assert len(invalid) == 0
        assert valid == ["alice", "bob", "charlie"]
    
    def test_mixed_valid_invalid(self):
        valid, invalid = validate_username_list(["alice", "bob@", "charlie"])
        assert len(valid) == 2
        assert len(invalid) == 1
        assert valid == ["alice", "charlie"]
        assert invalid[0][0] == "bob@"
    
    def test_empty_list(self):
        valid, invalid = validate_username_list([])
        assert len(valid) == 0
        assert len(invalid) == 0
    
    def test_strips_whitespace(self):
        valid, invalid = validate_username_list(["  alice  ", "bob"])
        assert "alice" in valid
        assert "bob" in valid


class TestValidateFilePath:
    """Test file path validation."""
    
    def test_valid_path(self, tmp_path):
        path = str(tmp_path / "test.txt")
        is_valid, error = validate_file_path(path)
        assert is_valid is True
    
    def test_empty_path(self):
        is_valid, error = validate_file_path("")
        assert is_valid is False
        assert "cannot be empty" in error.lower()
    
    def test_path_with_invalid_chars(self):
        is_valid, error = validate_file_path("test<file>.txt")
        assert is_valid is False
        assert "invalid character" in error.lower()
    
    def test_path_too_long(self):
        is_valid, error = validate_file_path("a" * 201)
        assert is_valid is False
        assert "too long" in error.lower()
    
    def test_must_exist_true_with_existing_file(self, tmp_path):
        file_path = tmp_path / "existing.txt"
        file_path.write_text("content")
        is_valid, error = validate_file_path(str(file_path), must_exist=True)
        assert is_valid is True
    
    def test_must_exist_true_with_missing_file(self):
        is_valid, error = validate_file_path("/nonexistent/file.txt", must_exist=True)
        assert is_valid is False
        assert "does not exist" in error.lower()


class TestValidateDirectory:
    """Test directory validation."""
    
    def test_valid_directory(self, tmp_path):
        is_valid, error = validate_directory(str(tmp_path))
        assert is_valid is True
    
    def test_creates_directory_if_not_exists(self, tmp_path):
        new_dir = tmp_path / "new_dir"
        is_valid, error = validate_directory(str(new_dir))
        assert is_valid is True
        assert new_dir.exists()
    
    def test_invalid_path(self):
        is_valid, error = validate_directory("invalid<>path")
        assert is_valid is False


class TestValidateInstagramAccounts:
    """Test Instagram accounts validation."""
    
    def test_valid_accounts(self):
        accounts = [
            {"name": "acct1", "username": "user1", "password": "pass1"},
            {"name": "acct2", "username": "user2", "password": "pass2"},
        ]
        is_valid, error = validate_instagram_accounts(accounts)
        assert is_valid is True
    
    def test_empty_accounts(self):
        is_valid, error = validate_instagram_accounts([])
        assert is_valid is False
        assert "No accounts configured" in error
    
    def test_missing_required_fields(self):
        accounts = [{"name": "acct1"}]  # missing username and password
        is_valid, error = validate_instagram_accounts(accounts)
        assert is_valid is False
        assert "missing required field" in error.lower()
    
    def test_invalid_username_in_account(self):
        accounts = [{"name": "acct1", "username": "invalid@user", "password": "pass"}]
        is_valid, error = validate_instagram_accounts(accounts)
        assert is_valid is False
    
    def test_duplicate_account_names(self):
        accounts = [
            {"name": "acct1", "username": "user1", "password": "pass1"},
            {"name": "acct1", "username": "user2", "password": "pass2"},
        ]
        is_valid, error = validate_instagram_accounts(accounts)
        assert is_valid is False
        assert "Duplicate account name" in error
    
    def test_duplicate_usernames(self):
        accounts = [
            {"name": "acct1", "username": "user1", "password": "pass1"},
            {"name": "acct2", "username": "user1", "password": "pass2"},
        ]
        is_valid, error = validate_instagram_accounts(accounts)
        assert is_valid is False
        assert "Duplicate username" in error
    
    def test_not_a_list(self):
        is_valid, error = validate_instagram_accounts("not a list")
        assert is_valid is False
        assert "must be a list" in error.lower()


class TestValidateDownloadLimit:
    """Test download limit validation."""
    
    def test_valid_limit(self):
        is_valid, error = validate_download_limit(100)
        assert is_valid is True
    
    def test_none_limit(self):
        is_valid, error = validate_download_limit(None)
        assert is_valid is True  # None means unlimited
    
    def test_too_low_limit(self):
        is_valid, error = validate_download_limit(0)
        assert is_valid is False
        assert "at least 1" in error.lower()
    
    def test_too_high_limit(self):
        is_valid, error = validate_download_limit(10001)
        assert is_valid is False
        assert "too high" in error.lower()
    
    def test_not_integer(self):
        is_valid, error = validate_download_limit("100")
        assert is_valid is False
        assert "must be an integer" in error.lower()


class TestValidateMaxRelationships:
    """Test maximum relationships validation."""
    
    def test_valid_max(self):
        is_valid, error = validate_max_relationships(1000)
        assert is_valid is True
    
    def test_zero_is_valid(self):
        is_valid, error = validate_max_relationships(0)
        assert is_valid is True  # 0 means unlimited
    
    def test_negative_max(self):
        is_valid, error = validate_max_relationships(-1)
        assert is_valid is False
        assert "cannot be negative" in error.lower()
    
    def test_too_high_max(self):
        is_valid, error = validate_max_relationships(100001)
        assert is_valid is False
        assert "too high" in error.lower()
    
    def test_not_integer(self):
        is_valid, error = validate_max_relationships("1000")
        assert is_valid is False
        assert "must be an integer" in error.lower()


class TestSafeValidate:
    """Test safe validation wrapper."""
    
    def test_successful_validation(self):
        result = safe_validate(validate_username, "valid_user")
        assert result[0] is True
    
    def test_exception_handling(self):
        def failing_validator(x):
            raise ValueError("Test error")
        
        result = safe_validate(failing_validator, "test")
        assert result[0] is False
        assert "Validation error" in result[1]
