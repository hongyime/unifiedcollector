"""Utility functions for the toolkit."""

import re
import time
import random
import shutil
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, List, Union


# Regex patterns (reduced scope – only username/video if ever needed internally)
VIDEO_ID_RE = re.compile(r'(?:/video/)(\d+)')
USERNAME_RE = re.compile(r'tiktok\.com/@([\w\.-]+)')


def extract_video_id(url: str) -> Optional[str]:
    """Extract video ID from TikTok URL."""
    match = VIDEO_ID_RE.search(url)
    return match.group(1) if match else None


def extract_username_from_url(url: str) -> Optional[str]:
    """Extract username from TikTok URL.
    
    Args:
        url: TikTok URL (e.g., https://www.tiktok.com/@username/video/123)
        
    Returns:
        Username without @ symbol, or None if not found
    """
    # Remove any trailing slashes and normalize
    url = url.rstrip('/')
    
    # Pattern for standard TikTok URLs: https://www.tiktok.com/@username/...
    pattern = r'(?:https?://)?(?:www\.)?tiktok\.com/@([^/\s]+)'
    match = re.search(pattern, url)
    
    if match:
        return match.group(1)
    
    # Pattern for short URLs like vm.tiktok.com - these need to be resolved
    # For now, return None since we'd need to follow redirects
    vm_pattern = r'(?:https?://)?vm\.tiktok\.com/'
    if re.search(vm_pattern, url):
        return None  # Cannot extract username from short URLs without following redirects
    
    return None


def safe_filename(name: str) -> str:
    """Convert a string to a safe filename."""
    return re.sub(r'[^A-Za-z0-9_.-]+', '_', name)[:120]


def build_profile_pic_filename(username: str, ext: str = 'jpg') -> str:
    """Build profile picture filename with download date.
    
    Format: username_profile_YYYY-MM-DD_HHMMSS.ext
    """
    download_date = datetime.now(timezone.utc).strftime('%Y-%m-%d_%H%M%S')
    return f"{safe_filename(username)}_profile_{download_date}.{ext}"


def sanitize_filename(filename: str) -> str:
    """Sanitize filename for safe filesystem usage."""
    # Remove or replace invalid characters
    invalid_chars = r'<>:"/\\|?*'
    for char in invalid_chars:
        filename = filename.replace(char, '_')
    
    # Remove excessive whitespace
    filename = re.sub(r'\s+', ' ', filename).strip()
    
    # Ensure reasonable length
    if len(filename) > 200:
        filename = filename[:200] + '...'
    
    return filename


def build_output_path(root: Path, video_id: str, ext: str, existing_path: Optional[Path] = None,
                       username: Optional[str] = None, post_date: Optional[str] = None) -> Path:
    """Build output path for downloaded files — flat layout, no date subfolders.

    Creates files directly in root/videoid.ext.
    If a file with the same video_id already exists anywhere under root,
    returns that existing path so callers skip re-downloading.

    Args:
        root: target directory (already username-specific)
        video_id: the video ID
        ext: file extension
        existing_path: if provided, avoid overwriting this exact path
        username: TikTok username (optional, unused — kept for API compatibility)
        post_date: ignored — kept for API compatibility
    """
    safe_vid = safe_filename(video_id)
    root = root.resolve()  # always work with absolute paths

    # Check if this video_id already exists anywhere under root (any subfolder)
    if root.exists():
        for existing in root.rglob(f"{safe_vid}.*"):
            if existing.is_file() and (existing_path is None or existing.resolve() != existing_path.resolve()):
                return existing

    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{safe_vid}.{ext}"

    # Only increment counter if exact path already exists and isn't our own file
    counter = 1
    while path.exists() and (existing_path is None or path.resolve() != existing_path.resolve()):
        path = root / f"{safe_vid}_{counter}.{ext}"
        counter += 1

    return path


def create_folder_structure(base_dir: Path, download_type: str, identifier: str) -> Path:
    """Create standardized folder structure (username-only focus).

    The previous implementation supported hashtags and raw URLs; the
    toolkit is now simplified to only download from usernames.
    Non-username types default to a generic folder.
    """
    if download_type == 'username':
        folder_name = f"username_{identifier}"
    else:
        folder_name = "misc"

    target_dir = base_dir / folder_name
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir


def read_usernames_from_file(file_path: Union[str, Path]) -> List[str]:
    """Read usernames from a text file.
    
    Args:
        file_path: Path to text file containing usernames (one per line)
        
    Returns:
        List of usernames (cleaned and validated)
    """
    usernames = []
    file_path = Path(file_path)
    
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")
    
    with open(file_path, 'r', encoding='utf-8') as f:
        for line in f:
            username = line.strip()
            if username and not username.startswith('#'):  # Skip empty lines and comments
                # Remove @ symbol if present
                if username.startswith('@'):
                    username = username[1:]
                
                # Basic validation
                if re.match(r'^[a-zA-Z0-9._-]+$', username):
                    usernames.append(username)
    
    return usernames


def sleep_range(min_s: float, max_s: float) -> None:
    """Sleep for a random duration between min_s and max_s seconds."""
    time.sleep(random.uniform(min_s, max_s))


def remove_empty_dirs(path: Path) -> None:
    """Recursively remove empty directories within the given path."""
    import stat
    import os
    
    if not path.is_dir():
        return
    
    # Process children first (bottom-up)
    for child in path.iterdir():
        if child.is_dir():
            remove_empty_dirs(child)
            
    # Try to remove the directory if it's empty
    try:
        if not any(path.iterdir()):
            try:
                path.rmdir()
            except PermissionError:
                # Might be read-only (common with gallery-dl created folders on Windows)
                os.chmod(path, stat.S_IWRITE)
                path.rmdir()
    except OSError:
        # Directory might not be empty or we might not have permissions
        pass



def secure_file_permissions(filepath: Path, mode: int = 0o600) -> bool:
    """Set restrictive permissions on a file.
    
    On Unix systems, sets file permissions to the specified mode (default: 0o600 = rw-------).
    On Windows, applies a restrictive ACL using `icacls` for the current user, SYSTEM,
    and Administrators.
    
    Args:
        filepath: Path to the file
        mode: Permission mode (default: 0o600 for owner read/write only)
        
    Returns:
        True if permissions were set successfully, False otherwise
    """
    import os
    import stat
    import logging
    import subprocess
    import getpass
    
    logger = logging.getLogger('uttk.utils')
    
    if not filepath.exists():
        logger.warning(f"Cannot set permissions on non-existent file: {filepath}")
        return False
    
    if os.name == 'nt':
        try:
            current_user = getpass.getuser()
            subprocess.run(
                [
                    'icacls',
                    str(filepath),
                    '/inheritance:r',
                    '/grant:r',
                    f'{current_user}:(R,W)',
                    '/grant:r',
                    'SYSTEM:(F)',
                    '/grant:r',
                    'Administrators:(F)',
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            logger.debug(f"Set restrictive Windows ACL on {filepath}")
            return True
        except (OSError, subprocess.SubprocessError) as e:
            logger.warning(f"Failed to set Windows ACL on {filepath}: {e}")
            return False
    
    try:
        os.chmod(filepath, mode)
        logger.debug(f"Set permissions {oct(mode)} on {filepath}")
        return True
    except (OSError, PermissionError) as e:
        logger.warning(f"Failed to set permissions on {filepath}: {e}")
        return False


def check_file_permissions(filepath: Path, max_mode: int = 0o600) -> bool:
    """Check if file permissions are restrictive enough.
    
    On Unix systems, checks if file permissions are at most as permissive as max_mode.
    On Windows, verifies that broad principals like Everyone or Users do not appear in
    the ACL output.
    
    Args:
        filepath: Path to the file
        max_mode: Maximum allowed permission mode (default: 0o600)
        
    Returns:
        True if permissions are acceptable, False if too permissive
    """
    import os
    import stat
    import logging
    import subprocess
    
    logger = logging.getLogger('uttk.utils')
    
    if not filepath.exists():
        logger.warning(f"Cannot check permissions on non-existent file: {filepath}")
        return False
    
    if os.name == 'nt':
        try:
            result = subprocess.run(
                ['icacls', str(filepath)],
                capture_output=True,
                text=True,
                check=True,
            )
            acl_text = result.stdout.lower()
            broad_principals = ('everyone:', 'builtin\\users:', ' authenticated users:')
            if any(principal in acl_text for principal in broad_principals):
                logger.warning(f"File {filepath} has broad Windows ACL entries")
                return False
            return True
        except (OSError, subprocess.SubprocessError) as e:
            logger.warning(f"Failed to check Windows ACL on {filepath}: {e}")
            return False
    
    try:
        file_stat = filepath.stat()
        file_mode = stat.S_IMODE(file_stat.st_mode)
        if file_mode & ~max_mode:
            logger.warning(
                f"File {filepath} has overly permissive permissions: {oct(file_mode)} "
                f"(should be at most {oct(max_mode)})"
            )
            return False
        
        return True
    except (OSError, PermissionError) as e:
        logger.warning(f"Failed to check permissions on {filepath}: {e}")
        return False


def extract_video_id_from_filename(filename: str) -> Optional[str]:
    """Extract TikTok video ID from filename using improved pattern.
    
    TikTok video IDs are typically 19 digits. This function tries multiple patterns:
    1. 19-digit ID at start of filename (most reliable)
    2. Any 6+ digit sequence (fallback for backward compatibility)
    
    Args:
        filename: The filename to extract ID from
        
    Returns:
        Video ID string if found, None otherwise
        
    Examples:
        >>> extract_video_id_from_filename("7123456789012345678.mp4")
        '7123456789012345678'
        >>> extract_video_id_from_filename("username_2024-01-01_7123456789012345678.mp4")
        '7123456789012345678'
        >>> extract_video_id_from_filename("123456.mp4")
        '123456'
    """
    # Pattern 1: 19-digit ID at start of filename (most common TikTok format)
    match = re.match(r'^(\d{19})', filename)
    if match:
        return match.group(1)
    
    # Pattern 2: 19-digit ID anywhere in filename
    match = re.search(r'(\d{19})', filename)
    if match:
        return match.group(1)
    
    # Pattern 3: Fallback to any 6+ digit sequence (backward compatibility)
    match = re.search(r'(\d{6,})', filename)
    if match:
        return match.group(1)
    
    return None


def find_duplicate_videos(root: Path) -> dict:
    """Scan a directory tree for duplicate video files (same video ID, _1/_2 suffixes).

    Returns a dict mapping video_id -> list of Path objects (sorted, original first).
    Only entries with 2+ files are included.
    """
    from collections import defaultdict
    id_map = defaultdict(list)

    for path in root.rglob('*'):
        if not path.is_file():
            continue
        if path.suffix.lower() not in {'.mp4', '.mov', '.avi', '.webm', '.mkv'}:
            continue
        vid = extract_video_id_from_filename(path.stem)
        if vid:
            id_map[vid].append(path)

    # Keep only IDs with duplicates, sort so original (no suffix) comes first
    dupes = {}
    for vid, paths in id_map.items():
        if len(paths) > 1:
            dupes[vid] = sorted(paths, key=lambda p: (
                # Files ending in _N are duplicates — sort them after the original
                1 if re.search(r'_\d+$', p.stem) else 0,
                p.name
            ))
    return dupes


def create_backup(files: List[Path], backup_dir: Optional[Path] = None) -> Path:
    """Create timestamped backup of files before destructive operations.
    
    Args:
        files: List of files to backup
        backup_dir: Directory to store backup (default: configs/backup_YYYYMMDD_HHMMSS)
        
    Returns:
        Path to backup directory
    """
    if backup_dir is None:
        timestamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
        backup_dir = Path('configs') / f'backup_{timestamp}'
    
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    
    for src in files:
        if src.exists():
            dst = backup_dir / src.name
            shutil.copy2(src, dst)
    
    return backup_dir
