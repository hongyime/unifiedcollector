"""Type definitions for the toolkit."""

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional, Dict, Any, Literal


@dataclass
class DownloadResult:
    """Result of a download operation."""
    ok: bool
    url: str
    status: Literal['downloaded', 'skipped', 'failed'] = 'downloaded'
    filepath: Optional[Path] = None
    reason: Optional[str] = None
    meta: Optional[Dict[str, Any]] = None


class InvalidReason(Enum):
    """Reasons why a username is considered invalid."""
    NOT_FOUND = "not_found"              # 404: User doesn't exist
    ACCOUNT_DELETED = "account_deleted"  # Account was deleted
    USERNAME_CHANGED = "username_changed" # Username was changed
    PRIVATE_BANNED = "private_banned"    # Account is private or banned
    UNKNOWN = "unknown"                  # Other validation failure


@dataclass
class InvalidUsernameRecord:
    """Record of an invalid username detection."""
    username: str
    reason: InvalidReason
    detected_at: float  # Unix timestamp
    error_message: Optional[str] = None
    retry_count: int = 0


@dataclass
class ValidationResult:
    """Result of username validation attempt."""
    is_valid: bool
    is_rate_limited: bool
    is_network_error: bool
    invalid_reason: Optional[InvalidReason] = None
    error_message: Optional[str] = None
    should_retry: bool = False
