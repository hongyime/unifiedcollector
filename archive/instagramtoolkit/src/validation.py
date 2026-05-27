"""
Input validation utilities for Instagram Toolkit.

Provides centralized validation for usernames, file paths, and configuration
to prevent errors and ensure data integrity.
"""
from __future__ import annotations

import os
import re
from typing import List, Tuple, Optional
from pathlib import Path


class ValidationError(Exception):
    """Raised when validation fails."""
    pass


def validate_username(username: str) -> Tuple[bool, str]:
    """
    Validate an Instagram username.
    
    Instagram usernames:
    - Can contain letters, numbers, periods, underscores, and hyphens
    - Cannot contain consecutive periods
    - Must be 1-30 characters long
    - Cannot start or end with a period
    
    Returns:
        Tuple of (is_valid, error_message)
    """
    if not username:
        return False, "Username cannot be empty"
    
    if not isinstance(username, str):
        return False, "Username must be a string"
    
    username = username.strip()
    
    if len(username) < 1:
        return False, "Username too short (minimum 1 character)"
    
    if len(username) > 30:
        return False, "Username too long (maximum 30 characters)"
    
    # Instagram username pattern: letters, numbers, periods, underscores, hyphens
    # Cannot have consecutive periods
    pattern = r'^(?!.*\.\.)[a-zA-Z0-9._-]+$'
    if not re.match(pattern, username):
        return False, "Username contains invalid characters"
    
    # Cannot start or end with a period
    if username.startswith('.') or username.endswith('.'):
        return False, "Username cannot start or end with a period"
    
    return True, ""


def validate_username_list(usernames: List[str]) -> Tuple[List[str], List[Tuple[str, str]]]:
    """
    Validate a list of usernames.
    
    Returns:
        Tuple of (valid_usernames, list of (username, error_message) for invalid ones)
    """
    valid = []
    invalid = []
    
    for username in usernames:
        is_valid, error = validate_username(username)
        if is_valid:
            valid.append(username.strip())
        else:
            invalid.append((username, error))
    
    return valid, invalid


def validate_file_path(path: str, must_exist: bool = False) -> Tuple[bool, str]:
    """
    Validate a file path.
    
    Args:
        path: The path to validate
        must_exist: If True, the path must already exist
        
    Returns:
        Tuple of (is_valid, error_message)
    """
    if not path:
        return False, "Path cannot be empty"
    
    if not isinstance(path, str):
        return False, "Path must be a string"
    
    path = path.strip()
    
    if not path:
        return False, "Path is empty after stripping"
    
    # Check for invalid characters (Windows and Unix)
    # Note: path separators (\ and /) are VALID and should not be checked here
    invalid_chars = ['<', '>', '"', '|', '?', '*']
    
    for char in invalid_chars:
        if char in path:
            return False, f"Path contains invalid character: '{char}'"
    
    # Check path length (Windows has 260 char limit, but we use a safer 200)
    if len(path) > 200:
        return False, "Path too long (maximum 200 characters for safety)"
    
    if must_exist and not os.path.exists(path):
        return False, f"Path does not exist: {path}"
    
    return True, ""


def validate_directory(path: str, must_be_writable: bool = True) -> Tuple[bool, str]:
    """
    Validate a directory path.
    
    Args:
        path: The directory path to validate
        must_be_writable: If True, the directory must be writable
        
    Returns:
        Tuple of (is_valid, error_message)
    """
    is_valid, error = validate_file_path(path, must_exist=False)
    if not is_valid:
        return False, error
    
    # Create directory if it doesn't exist
    try:
        os.makedirs(path, exist_ok=True)
    except Exception as e:
        return False, f"Cannot create directory: {e}"
    
    # Check if writable
    if must_be_writable:
        test_file = os.path.join(path, '.write_test')
        try:
            with open(test_file, 'w') as f:
                f.write('test')
            os.remove(test_file)
        except Exception as e:
            return False, f"Directory is not writable: {e}"
    
    return True, ""


def validate_instagram_accounts(accounts: List[dict]) -> Tuple[bool, str]:
    """
    Validate Instagram account configurations.
    
    Args:
        accounts: List of account dictionaries with 'name', 'username', 'password'
        
    Returns:
        Tuple of (is_valid, error_message)
    """
    if not accounts:
        return False, "No accounts configured"
    
    if not isinstance(accounts, list):
        return False, "Accounts must be a list"
    
    for i, account in enumerate(accounts):
        if not isinstance(account, dict):
            return False, f"Account {i+1} must be a dictionary"
        
        # Check required fields
        for field in ['name', 'username', 'password']:
            if field not in account:
                return False, f"Account {i+1} missing required field: {field}"
            if not account[field]:
                return False, f"Account {i+1} has empty {field}"
        
        # Validate username format
        is_valid, error = validate_username(account['username'])
        if not is_valid:
            return False, f"Account {i+1} has invalid username: {error}"
        
        # Check for duplicate names
        if sum(1 for a in accounts if a['name'] == account['name']) > 1:
            return False, f"Duplicate account name: {account['name']}"
        
        # Check for duplicate usernames
        if sum(1 for a in accounts if a['username'] == account['username']) > 1:
            return False, f"Duplicate username: {account['username']}"
    
    return True, ""


def validate_download_limit(limit: Optional[int]) -> Tuple[bool, str]:
    """
    Validate a download limit parameter.
    
    Args:
        limit: The limit to validate (None means unlimited)
        
    Returns:
        Tuple of (is_valid, error_message)
    """
    if limit is None:
        return True, ""
    
    if not isinstance(limit, int):
        return False, "Download limit must be an integer"
    
    if limit < 1:
        return False, "Download limit must be at least 1"
    
    if limit > 10000:
        return False, "Download limit too high (maximum 10000 for safety)"
    
    return True, ""


def validate_max_relationships(max_count: int) -> Tuple[bool, str]:
    """
    Validate maximum relationships to collect.
    
    Args:
        max_count: Maximum number of followers/following to collect
        
    Returns:
        Tuple of (is_valid, error_message)
    """
    if not isinstance(max_count, int):
        return False, "Maximum relationships must be an integer"
    
    if max_count < 0:
        return False, "Maximum relationships cannot be negative"
    
    if max_count > 100000:
        return False, "Maximum relationships too high (maximum 100000 for safety)"
    
    return True, ""


def safe_validate(func, *args, **kwargs):
    """
    Safely execute a validation function, returning default on exception.
    
    Args:
        func: The validation function to call
        *args, **kwargs: Arguments to pass to the function
        
    Returns:
        The result of the validation function, or (False, "Validation error") on exception
    """
    try:
        return func(*args, **kwargs)
    except Exception as e:
        return False, f"Validation error: {e}"


__all__ = [
    "ValidationError",
    "validate_username",
    "validate_username_list",
    "validate_file_path",
    "validate_directory",
    "validate_instagram_accounts",
    "validate_download_limit",
    "validate_max_relationships",
    "safe_validate",
]



