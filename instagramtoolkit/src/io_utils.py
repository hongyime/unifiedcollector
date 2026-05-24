"""Shared I/O utilities for safe file operations and network retry logic.

Provides:
- safe_json_write: Atomic JSON writes (temp file + rename) to prevent corruption
- retry_with_backoff: Retry wrapper with exponential backoff for Instaloader API calls
"""
from __future__ import annotations

import json
import os
import random
import tempfile
import time
from typing import Any, Callable, Optional, TypeVar

import instaloader.exceptions
from src.exception_handler import (
    is_retryable_exception,
    is_rate_limit_exception,
    format_exception_message,
)

T = TypeVar("T")

# --------------- Atomic JSON Writes ---------------

def safe_json_write(path: str, data: Any, indent: int = 2) -> None:
    """Write JSON data atomically: write to temp file, then rename over target.

    This prevents data corruption if the process is killed mid-write.
    The temp file is created in the same directory so os.replace is always
    an atomic same-filesystem rename.
    """
    target_dir = os.path.dirname(path) or "."
    os.makedirs(target_dir, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(dir=target_dir, suffix=".tmp", prefix=".safe_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent, ensure_ascii=False)
        os.replace(tmp_path, path)
    except BaseException:
        # Clean up temp file on any failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# --------------- Retry with Exponential Backoff ---------------


def retry_with_backoff(
    func: Callable[..., T],
    *args: Any,
    max_retries: int = 3,
    base_delay: float = 30.0,
    max_delay: float = 600.0,
    label: str = "",
    **kwargs: Any,
) -> Optional[T]:
    """Call *func* with retries and exponential backoff on transient failures.

    Uses the centralized exception handling system to determine which exceptions
    are retryable. Non-retryable exceptions (e.g. ProfileNotExistsException) 
    propagate immediately.
    
    Returns the function result, or None if all retries are exhausted.
    """
    for attempt in range(max_retries + 1):
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            # Use centralized exception handling
            if not is_retryable_exception(exc):
                # Non-retryable exception - propagate immediately
                raise
            
            # Determine if this is a rate limit exception
            rate_limit = is_rate_limit_exception(exc)
            
            if attempt >= max_retries:
                tag = f"[RETRY:{label}]" if label else "[RETRY]"
                print(f"{tag} All {max_retries} retries exhausted: {exc}")
                return None

            # Exponential backoff with jitter
            delay = min(base_delay * (2 ** attempt) + random.uniform(0, 10), max_delay)
            if rate_limit:
                delay = max(delay, 60)  # At least 60s on genuine 429/403

            tag = f"[RETRY:{label}]" if label else "[RETRY]"
            print(f"{tag} Attempt {attempt + 1}/{max_retries} failed: {format_exception_message(exc)}")
            print(f"{tag} Retrying in {delay:.0f}s...")
            time.sleep(delay)

    return None  # Shouldn't reach here, but for safety


# --------------- File Locking (Cross-Platform) ---------------

class FileLock:
    """Cross-platform file locking to prevent concurrent writes.
    
    Uses msvcrt on Windows and fcntl on Unix-like systems.
    
    Usage:
        with FileLock("/path/to/file"):
            safe_json_write("/path/to/file", data)
    """
    
    def __init__(self, filepath: str, timeout: float = 10.0):
        """
        Initialize file lock.
        
        Args:
            filepath: Path to the file to lock
            timeout: Maximum time to wait for lock (seconds)
        """
        self.filepath = filepath
        self.lockfile = f"{filepath}.lock"
        self.timeout = timeout
        self._lockfile_obj = None
        self._locked = False
        self._platform = os.name  # 'nt' for Windows, 'posix' for Unix
    
    def acquire(self) -> bool:
        """Acquire file lock with timeout.
        
        Returns:
            True if lock acquired, False if timeout
        """
        start_time = time.time()
        
        if self._platform == 'nt':
            return self._acquire_windows(start_time)
        else:
            return self._acquire_unix(start_time)
    
    def _acquire_unix(self, start_time: float) -> bool:
        """Unix file locking using fcntl."""
        import fcntl
        
        while time.time() - start_time < self.timeout:
            try:
                self._lockfile_obj = open(self.lockfile, 'w')
                fcntl.flock(self._lockfile_obj.fileno(), 
                          fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._locked = True
                return True
            except (IOError, OSError) as e:
                if self._lockfile_obj:
                    self._lockfile_obj.close()
                    self._lockfile_obj = None
                # Check if timeout reached
                if time.time() - start_time >= self.timeout:
                    return False
                # Wait and retry
                time.sleep(0.1)
        
        return False
    
    def _acquire_windows(self, start_time: float) -> bool:
        """Windows file locking using msvcrt."""
        import msvcrt
        
        while time.time() - start_time < self.timeout:
            try:
                self._lockfile_obj = os.open(
                    self.lockfile,
                    os.O_CREAT | os.O_EXCL | os.O_RDWR  # O_EXCL makes creation atomic
                )
                # Try to lock first byte
                msvcrt.locking(self._lockfile_obj, msvcrt.LK_NBLCK, 1)
                self._locked = True
                return True
            except FileExistsError:  # Lock held by another process
                if self._lockfile_obj:
                    try:
                        os.close(self._lockfile_obj)
                    except:
                        pass
                    self._lockfile_obj = None
                # Check if timeout reached
                if time.time() - start_time >= self.timeout:
                    return False
                # Wait and retry
                time.sleep(0.1)
            except (OSError, IOError):
                if self._lockfile_obj:
                    try:
                        os.close(self._lockfile_obj)
                    except:
                        pass
                    self._lockfile_obj = None
                # Check if timeout reached
                if time.time() - start_time >= self.timeout:
                    return False
                # Wait and retry
                time.sleep(0.1)
        
        return False
    
    def release(self):
        """Release file lock."""
        if not self._locked:
            return
        
        try:
            if self._platform == 'nt':
                import msvcrt
                if self._lockfile_obj:
                    # Unlock
                    msvcrt.locking(self._lockfile_obj, msvcrt.LK_UNLCK, 1)
                    os.close(self._lockfile_obj)
            else:
                import fcntl
                if self._lockfile_obj:
                    fcntl.flock(self._lockfile_obj.fileno(), fcntl.LOCK_UN)
                    self._lockfile_obj.close()
            
            # Remove lock file
            try:
                os.unlink(self.lockfile)
            except (OSError, FileNotFoundError):
                pass
                
        except Exception:
            # Ignore cleanup errors
            pass
        finally:
            self._locked = False
            self._lockfile_obj = None
    
    def __enter__(self):
        """Context manager entry."""
        if not self.acquire():
            raise TimeoutError(f"Could not acquire lock for {self.filepath} within {self.timeout}s")
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.release()
        return False  # Don't suppress exceptions


__all__ = [
    "safe_json_write",
    "retry_with_backoff", 
    "FileLock",
]


