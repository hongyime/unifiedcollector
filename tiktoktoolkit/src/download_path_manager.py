#!/usr/bin/env python3
"""
Unified Download Path Manager
==============================
Handles download path configuration across all toolkits with session-based caching.
"""

import atexit
import signal
import sys
from pathlib import Path
from typing import Optional

# Global session state (not thread-safe, designed for CLI use)
_SESSION_DOWNLOAD_PATH: Optional[str] = None
_SESSION_ACTIVE: bool = False


def _cleanup_session():
    """Clear session state (called on exit, signals, or manual clear)."""
    global _SESSION_DOWNLOAD_PATH, _SESSION_ACTIVE
    _SESSION_DOWNLOAD_PATH = None
    _SESSION_ACTIVE = False


def _handle_sigint(signum, frame):
    _cleanup_session()
    signal.default_int_handler(signum, frame)


def _handle_sigterm(signum, frame):
    _cleanup_session()
    raise SystemExit(143)


# Register cleanup handlers
atexit.register(_cleanup_session)
try:
    signal.signal(signal.SIGINT, _handle_sigint)
    signal.signal(signal.SIGTERM, _handle_sigterm)
except (ValueError, OSError):
    pass


def prompt_for_download_path(
    context: str = "files",
    allow_session_reuse: bool = True,
    out_path: Optional[str] = None,
    default_path: Optional[str] = None
) -> str:
    """Prompt user for download path with session caching and validation."""
    global _SESSION_DOWNLOAD_PATH, _SESSION_ACTIVE

    if out_path:
        validated = _validate_and_create_path(out_path)
        _SESSION_DOWNLOAD_PATH = validated
        _SESSION_ACTIVE = True
        return validated

    if allow_session_reuse and _SESSION_ACTIVE and _SESSION_DOWNLOAD_PATH:
        print(f"\n{'=' * 70}")
        print(f"[SESSION] CURRENT PATH: {_SESSION_DOWNLOAD_PATH}")
        print(f"{'=' * 70}")
        reuse = input("Use same path for this download? (y/n): ").strip().lower()
        if reuse == 'y':
            print(f"[OK] Reusing session path: {_SESSION_DOWNLOAD_PATH}\n")
            return _SESSION_DOWNLOAD_PATH
        print("Prompting for new path...\n")

    print(f"\n{'=' * 70}")
    print(f"[DIR] DOWNLOAD LOCATION REQUIRED FOR: {context.upper()}")
    print(f"{'=' * 70}")
    if default_path:
        print(f"[TIP] Default path: {default_path}")
        print("      (Press Enter to use default)")
    print("[NOTE] Path is NOT saved between script runs.")
    print(f"{'=' * 70}")
    print("\n[EXAMPLES]")
    print(f"   Windows:   C:\\Users\\YourName\\Downloads\\tiktok")
    print(f"   Network:   Z:\\media\\tiktok")
    print(f"   Relative:  ./downloads")
    print("\n[TIPS]")
    print("   - Use absolute paths for clarity")
    print("   - Directory will be created if it doesn't exist")
    print("   - Type 'exit' or 'q' to cancel")
    print(f"{'=' * 70}\n")

    while True:
        prompt_text = "[DIR] Enter download directory path"
        if default_path:
            prompt_text += f" [default: {default_path}]"
        prompt_text += ": "

        download_path = input(prompt_text).strip()

        if download_path.lower() in ['exit', 'q', 'quit']:
            print("\n[CANCELLED] Operation cancelled by user.")
            _cleanup_session()
            sys.exit(0)

        if not download_path:
            if default_path:
                download_path = default_path
            else:
                print("[ERROR] Path cannot be empty. Please enter a valid path.\n")
                continue

        try:
            validated_path = _validate_and_create_path(download_path)
            _SESSION_DOWNLOAD_PATH = validated_path
            _SESSION_ACTIVE = True
            print(f"\n{'=' * 70}")
            print(f"[OK] SESSION PATH SET: {validated_path}")
            print("[NOTE] Valid for this session only - will clear on exit!")
            print(f"{'=' * 70}\n")
            return validated_path
        except PermissionError:
            print(f"[ERROR] Permission denied: {download_path}")
            print("        Please choose a location with write permissions.\n")
        except FileExistsError as exc:
            print(f"[ERROR] {exc}")
            print("        Please choose a different path.\n")
        except Exception as exc:
            print(f"[ERROR] Invalid path: {download_path}")
            print(f"        Error: {exc}\n")


def _validate_and_create_path(path_str: str) -> str:
    """Validate path and create directory if needed (with user confirmation)."""
    path = Path(path_str).expanduser().resolve()

    if path.exists() and not path.is_dir():
        raise FileExistsError(f"Path exists but is a file, not a directory: {path}")

    if not path.exists():
        print(f"\n[DIR] Directory does not exist: {path}")
        confirm = input("      Create this directory? (y/n): ").strip().lower()
        if confirm != 'y':
            raise ValueError("Directory creation declined by user")
        try:
            path.mkdir(parents=True, exist_ok=True)
            print(f"[OK] Created directory: {path}")
        except PermissionError as exc:
            raise PermissionError(f"Cannot create directory (permission denied): {path}") from exc
        except Exception as exc:
            raise Exception(f"Failed to create directory: {exc}") from exc

    try:
        test_file = path / '.write_test_temp'
        test_file.write_text('test')
        test_file.unlink(missing_ok=True)
    except PermissionError as exc:
        raise PermissionError(f"No write permission for directory: {path}") from exc
    except Exception as exc:
        raise Exception(f"Cannot write to directory: {exc}") from exc

    return str(path)


def get_session_path() -> Optional[str]:
    """Get the currently cached session path, if any."""
    return _SESSION_DOWNLOAD_PATH if _SESSION_ACTIVE else None


def clear_session_cache():
    """Manually clear the session path cache."""
    _cleanup_session()
    print("[OK] Session download path cache cleared")
