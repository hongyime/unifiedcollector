"""Input validation utilities for the toolkit.

This module provides validation functions to prevent injection attacks
and ensure input data meets expected formats.
"""

import re
from typing import Optional

from .errors import ValidationError


# TikTok username pattern: alphanumeric, dots, underscores, hyphens, 1-30 chars
USERNAME_PATTERN = re.compile(r'^[a-zA-Z0-9._-]{1,30}$')


def validate_username(username: str) -> str:
    """Validate and sanitize a TikTok username.
    
    TikTok usernames must:
    - Be 1-30 characters long
    - Contain only alphanumeric characters, dots, underscores, and hyphens
    - Not contain spaces or special characters
    
    Args:
        username: The username to validate (may include leading @)
        
    Returns:
        Sanitized username (without @ prefix)
        
    Raises:
        ValidationError: If username is invalid
        
    Examples:
        >>> validate_username("@tiktok")
        'tiktok'
        >>> validate_username("user.name_123")
        'user.name_123'
        >>> validate_username("invalid user")
        ValidationError: Invalid username format
    """
    if not username:
        raise ValidationError("Username cannot be empty")
    
    # Strip leading @ symbol if present
    sanitized = username.lstrip('@')
    
    if not sanitized:
        raise ValidationError("Username cannot be just '@'")
    
    # Validate pattern
    if not USERNAME_PATTERN.match(sanitized):
        raise ValidationError(
            f"Invalid username format: '{username}'. "
            "TikTok usernames must be 1-30 characters and contain only "
            "letters, numbers, dots, underscores, and hyphens."
        )
    
    return sanitized


def validate_limit(limit: Optional[int], max_limit: int = 10000) -> int:
    """Validate download limit parameter.
    
    Args:
        limit: Number of items to download (None means no limit)
        max_limit: Maximum allowed limit
        
    Returns:
        Validated limit value
        
    Raises:
        ValidationError: If limit is invalid
    """
    if limit is None:
        return max_limit
    
    if not isinstance(limit, int):
        raise ValidationError(f"Limit must be an integer, got {type(limit).__name__}")
    
    if limit < 1:
        raise ValidationError(f"Limit must be at least 1, got {limit}")
    
    if limit > max_limit:
        raise ValidationError(f"Limit cannot exceed {max_limit}, got {limit}")
    
    return limit


def validate_download_type(download_type: str) -> str:
    """Validate download type parameter.
    
    Args:
        download_type: Type of content to download
        
    Returns:
        Validated download type
        
    Raises:
        ValidationError: If download type is invalid
    """
    valid_types = {'videos', 'profile_pictures'}
    
    if download_type not in valid_types:
        raise ValidationError(
            f"Invalid download type: '{download_type}'. "
            f"Must be one of: {', '.join(sorted(valid_types))}"
        )
    
    return download_type
