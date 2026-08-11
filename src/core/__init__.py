from __future__ import annotations

from importlib import import_module
from typing import Any


_EXPORTS = {
    "check_drive": ("src.core.drive_check", "check_drive"),
    "wait_for_drive": ("src.core.drive_check", "wait_for_drive"),
    "build_filename": ("src.core.file_naming", "build_filename"),
    "sanitize_name": ("src.core.file_naming", "sanitize_name"),
    "parse_filename": ("src.core.file_naming", "parse_filename"),
    "with_retry": ("src.core.resilience", "with_retry"),
    "async_retry": ("src.core.resilience", "async_retry"),
    "CircuitBreaker": ("src.core.resilience", "CircuitBreaker"),
    "interruptible_sleep": ("src.core.resilience", "interruptible_sleep"),
    "wait_for_internet": ("src.core.resilience", "wait_for_internet"),
    "AdaptiveRateLimiter": ("src.core.rate_limiter", "AdaptiveRateLimiter"),
    "HumanLikeRateLimiter": ("src.core.human_rate_limiter", "HumanLikeRateLimiter"),
    "OperationType": ("src.core.human_rate_limiter", "OperationType"),
    "AccountPool": ("src.core.account_pool", "AccountPool"),
    "ProfilePhotoTracker": ("src.core.profile_photo_tracker", "ProfilePhotoTracker"),
    "UserAgentPool": ("src.core.user_agent", "UserAgentPool"),
    "SearchCache": ("src.core.search_cache", "SearchCache"),
    "CheckpointManager": ("src.core.checkpoint", "CheckpointManager"),
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr_name = _EXPORTS[name]
    value = getattr(import_module(module_name), attr_name)
    globals()[name] = value
    return value
