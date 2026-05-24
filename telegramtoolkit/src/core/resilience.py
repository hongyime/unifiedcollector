#!/usr/bin/env python3
"""
Resilience Utilities for Telegram Toolkit
Provides self-healing, retry logic, atomic file operations, and health checks.
"""
import asyncio
import hashlib
import json
import os
import signal
import socket
import tempfile
import time
import functools
from typing import Any, Dict, Optional


# ── Retry decorator for Telethon API calls ──────────────────────────────────

def retry_api_call(max_retries: int = 3, base_delay: float = 5.0,
                   max_flood_wait: int = 120,
                   fatal_errors: tuple[type[BaseException], ...] = ()):
    """
    Decorator for Telethon API calls with exponential back-off and FloodWait handling.

    Args:
        max_retries: Maximum number of retry attempts.
        base_delay: Base delay in seconds between retries (doubles each attempt).
        max_flood_wait: Maximum seconds to wait for FloodWaitError before skipping.
        fatal_errors: Tuple of exception types that should NOT be retried.
    """
    def decorator(func: Any) -> Any:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exception: BaseException | None = None
            for attempt in range(1, max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    # Don't retry fatal errors
                    if fatal_errors and isinstance(e, fatal_errors):
                        raise

                    error_msg = str(e).lower()

                    # Handle FloodWaitError specifically
                    if 'flood' in error_msg or 'a]wait' in error_msg:
                        import re
                        wait_match = re.search(r'(\d+)', str(e))
                        wait_time = int(wait_match.group(1)) if wait_match else base_delay
                        if wait_time > max_flood_wait:
                            print(f"⏰ FloodWait {wait_time}s exceeds max ({max_flood_wait}s), skipping.")
                            raise
                        print(f"⏰ FloodWait: sleeping {wait_time}s (attempt {attempt}/{max_retries})")
                        await asyncio.sleep(wait_time)
                        continue

                    # Don't retry permission / not-found errors
                    if any(kw in error_msg for kw in (
                        'chat_admin_required', 'forbidden', 'user_not_participant',
                        'channel_private', 'chat_write_forbidden', 'user_banned_in_channel'
                    )):
                        raise

                    # Exponential back-off for transient errors
                    if attempt < max_retries:
                        delay = base_delay * (2 ** (attempt - 1))
                        print(f"⚠️ {func.__name__} failed (attempt {attempt}/{max_retries}): {e}")
                        print(f"   Retrying in {delay:.0f}s...")
                        await asyncio.sleep(delay)
                    else:
                        print(f"❌ {func.__name__} failed after {max_retries} attempts: {e}")

            if last_exception is not None:
                raise last_exception
            raise RuntimeError("retry_api_call: no attempts were made")
        return wrapper
    return decorator


# ── Atomic JSON file operations ─────────────────────────────────────────────

def atomic_json_write(filepath: str, data: Any, indent: int = 2) -> bool:
    """
    Write JSON data atomically using write-to-temp + rename.
    Prevents data corruption on crash / power loss.

    Returns True on success, False on failure.
    """
    filepath = str(filepath)
    try:
        dir_name = os.path.dirname(filepath) or '.'
        os.makedirs(dir_name, exist_ok=True)

        # Write to a temp file in the same directory (same filesystem → atomic rename)
        fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=indent, ensure_ascii=False)
            # Atomic rename (Windows: os.replace is atomic on NTFS)
            os.replace(tmp_path, filepath)
            return True
        except Exception:
            # Clean up temp file on failure
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except Exception as e:
        print(f"⚠️ Failed to write {filepath}: {e}")
        return False


def safe_json_load(filepath: str, default: Any = None) -> Any:
    """
    Load JSON file with automatic recovery from corruption.
    Falls back to .bak if main file is corrupt, returns *default* if both fail.
    """
    filepath = str(filepath)
    backup_path = filepath + '.bak'

    for path in (filepath, backup_path):
        if not os.path.exists(path):
            continue
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            # If we recovered from backup, restore the main file
            if path == backup_path:
                print(f"🔧 Recovered {filepath} from backup")
                atomic_json_write(filepath, data)
            return data
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            print(f"⚠️ Corrupt JSON in {path}: {e}")
            continue
        except Exception as e:
            print(f"⚠️ Error reading {path}: {e}")
            continue

    return default if default is not None else {}


def safe_json_save_with_backup(filepath: str, data: Any, indent: int = 2) -> bool:
    """
    Save JSON atomically and keep a .bak of the previous version.
    """
    filepath = str(filepath)
    backup_path = filepath + '.bak'

    # Back up existing file
    if os.path.exists(filepath):
        try:
            os.replace(filepath, backup_path)
        except Exception:
            pass  # Non-critical

    return atomic_json_write(filepath, data, indent=indent)


# ── Chunked file hashing ────────────────────────────────────────────────────

def chunked_file_hash(path: str, algorithm: str = 'sha256',
                      chunk_size: int = 8192) -> Optional[str]:
    """
    Calculate file hash using chunked reads (memory-safe for large files).

    Args:
        path: Path to the file.
        algorithm: Hash algorithm name (default sha256).
        chunk_size: Read chunk size in bytes.

    Returns:
        Hex digest string, or None on error.
    """
    try:
        hasher = hashlib.new(algorithm)
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(chunk_size), b''):
                hasher.update(chunk)
        return hasher.hexdigest()
    except Exception as e:
        print(f"⚠️ Error hashing {path}: {e}")
        return None


# ── Session health check ────────────────────────────────────────────────────

async def detect_session_health(client: Any, account_name: str = "unknown") -> Dict[str, Any]:
    """
    Check if a Telethon client session is healthy.

    Returns dict with keys: healthy (bool), account_name, error (str or None).
    """
    result: Dict[str, Any] = {'healthy': False, 'account_name': account_name, 'error': None}
    try:
        me = await asyncio.wait_for(client.get_me(), timeout=15)
        if me:
            result['healthy'] = True
            result['user_id'] = me.id
            result['username'] = getattr(me, 'username', None)
        else:
            result['error'] = 'get_me() returned None'
    except asyncio.TimeoutError:
        result['error'] = 'Timed out after 15s'
    except Exception as e:
        result['error'] = str(e)
    return result


# ── Internet availability check ─────────────────────────────────────────────

def _is_internet_available(host: str = "8.8.8.8", port: int = 53, timeout: float = 3.0) -> bool:
    """Fast internet check via DNS socket (no HTTP overhead)."""
    try:
        socket.setdefaulttimeout(timeout)
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect((host, port))
        return True
    except (socket.error, OSError):
        return False


def wait_for_internet(shutdown: "GracefulShutdown | None" = None, check_interval: float = 5.0) -> bool:
    """
    Block until internet is available or shutdown is requested.
    Returns True if internet came back, False if shutdown was requested.
    """
    if _is_internet_available():
        return True
    print("[OFFLINE] No internet. Waiting for connection...")
    while not _is_internet_available():
        if shutdown and shutdown.requested:
            print("[STOPPED] Shutdown requested while waiting for internet.")
            return False
        time.sleep(check_interval)
    print("[ONLINE] Connection restored.")
    return True


# ── Graceful shutdown context manager ────────────────────────────────────────

class GracefulShutdown:
    """
    Context manager that catches Ctrl+C and sets a flag instead of crashing.

    Usage:
        shutdown = GracefulShutdown()
        with shutdown:
            while not shutdown.requested:
                do_work()
    """
    def __init__(self):
        self.requested = False
        self._original_handler = None

    def _handler(self, signum: int, frame: Any) -> None:
        print("\n🛑 Shutdown requested. Finishing current operation...")
        self.requested = True

    def __enter__(self) -> 'GracefulShutdown':
        self._original_handler = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, self._handler)
        return self

    def __exit__(self, *args: Any) -> None:
        signal.signal(signal.SIGINT, self._original_handler or signal.SIG_DFL)


# ── Safe text file append ────────────────────────────────────────────────────

def safe_append_line(filepath: str, line: str) -> bool:
    """Append a line to a text file, creating parent dirs if needed."""
    try:
        os.makedirs(os.path.dirname(filepath) or '.', exist_ok=True)
        with open(filepath, 'a', encoding='utf-8') as f:
            f.write(line.rstrip('\n') + '\n')
        return True
    except Exception as e:
        print(f"⚠️ Failed to append to {filepath}: {e}")
        return False
