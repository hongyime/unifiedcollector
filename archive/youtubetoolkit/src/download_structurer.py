#!/usr/bin/env python3
"""
Download Path Structurer with Channel Organization
================================================
Handles channel-based directory structure and file naming.

Directory Structure:
    downloads/
    ├── UC_channel_id_@username/
    │   ├── profile_photo.jpg
    │   ├── UC_channel_id_@username_video1_title.mp4
    │   └── UC_channel_id_@username_video2_title.mp4
    └── ...

Features:
- Sanitized file names for cross-platform compatibility
- Channel-based subfolders
- Profile photo deduplication
- Safe filename generation
"""

import os
import re
import unicodedata
from pathlib import Path
from typing import Optional, Tuple


class DownloadStructurer:
    """
    Manages download path organization with channel-based structure.
    """
    
    # Invalid filename characters (platform-dependent)
    INVALID_CHARS = r'[<>:"/\\|?*\x00-\x1f]'
    # Reserved filenames on Windows
    RESERVED_NAMES = {
        'CON', 'PRN', 'AUX', 'NUL',
        'COM1', 'COM2', 'COM3', 'COM4', 'COM5', 'COM6', 'COM7', 'COM8', 'COM9',
        'LPT1', 'LPT2', 'LPT3', 'LPT4', 'LPT5', 'LPT6', 'LPT7', 'LPT8', 'LPT9'
    }
    
    def __init__(self, base_path: str):
        """
        Initialize download structurer.
        
        Args:
            base_path: Base downloads directory
        """
        self.base_path = Path(base_path).resolve()
        self.base_path.mkdir(parents=True, exist_ok=True)
    
    @staticmethod
    def sanitize_filename(name: str, max_length: int = 100, replace_char: str = '_') -> str:
        """
        Sanitize a filename for cross-platform compatibility.
        
        Args:
            name: Original filename or string
            max_length: Maximum length of filename (excluding extension)
            replace_char: Character to replace invalid chars with
            
        Returns:
            str: Sanitized filename
        """
        # Normalize unicode characters (convert accented chars to ASCII equivalents)
        normalized = unicodedata.normalize('NFKD', name)
        ascii_only = ''.join(c for c in normalized if unicodedata.category(c) != 'Mn')
        
        # Remove invalid characters
        sanitized = re.sub(DownloadStructurer.INVALID_CHARS, replace_char, ascii_only)
        
        # Remove leading/trailing dots and spaces
        sanitized = sanitized.strip('. ')
        
        # Check for reserved filenames (Windows)
        base_name = os.path.splitext(sanitized)[0].upper()
        if base_name in DownloadStructurer.RESERVED_NAMES:
            sanitized = f"{sanitized}_{replace_char}file"
        
        # Truncate if too long (keep extension if present)
        if len(sanitized) > max_length + 20:  # +20 for extension
            ext = os.path.splitext(sanitized)[1]
            base = sanitized[:max_length]
            sanitized = base + ext
        
        # Don't return empty string
        if not sanitized:
            sanitized = "unnamed_file"
        
        return sanitized
    
    def get_channel_folder_name(self, channel_id: Optional[str], channel_name: Optional[str]) -> str:
        """
        Generate sanitized channel folder name.
        
        Format: {channel_id}_{@username}
        
        Args:
            channel_id: YouTube channel ID (e.g., UCxxxxxxxxxxxxxxxx)
            channel_name: Channel name or handle (e.g., @username or actual name)
            
        Returns:
            str: Sanitized folder name
        """
        if not channel_id and not channel_name:
            return "unknown_channel"
        
        # Sanitize both parts
        safe_id = self.sanitize_filename(channel_id or "unknown", max_length=50)
        safe_name = self.sanitize_filename(channel_name or "unknown", max_length=50)
        
        # Format: channel_id/channel_name
        folder_name = f"{safe_id}_{safe_name}"
        
        # Ensure length limit (filesystem dependent, usually 255 chars)
        if len(folder_name) > 200:
            # Truncate the channel name part
            folder_name = f"{safe_id[:50]}_{safe_name[:50]}"
        
        return folder_name
    
    def get_channel_path(self, channel_id: Optional[str], channel_name: Optional[str]) -> Path:
        """
        Get path to channel folder.
        
        Args:
            channel_id: YouTube channel ID
            channel_name: Channel name
            
        Returns:
            Path: Path to channel folder (created if needed)
        """
        folder_name = self.get_channel_folder_name(channel_id, channel_name)
        channel_path = self.base_path / folder_name
        channel_path.mkdir(parents=True, exist_ok=True)
        return channel_path
    
    def get_video_filename(
        self,
        channel_id: Optional[str],
        channel_name: Optional[str],
        video_title: str,
        video_id: Optional[str] = None,
        extension: str = "mp4"
    ) -> str:
        """
        Generate video filename with channel prefix.
        
        Format: {channel_id}_{channel_name}_{title}.{ext}
        
        Args:
            channel_id: YouTube channel ID
            channel_name: Channel name
            video_title: Video title
            video_id: Video ID (for uniqueness, optional)
            extension: File extension
            
        Returns:
            str: Sanitized filename
        """
        # Sanitize parts
        safe_id = self.sanitize_filename(channel_id or "unknown", max_length=40)
        safe_name = self.sanitize_filename(channel_name or "unknown", max_length=40)
        safe_title = self.sanitize_filename(video_title, max_length=100)
        
        # If title is empty after sanitization, use video ID
        if not safe_title:
            safe_title = f"video_{video_id or 'unknown'}" if video_id else "video_unknown"
        
        # Format and truncate if needed
        if video_id:
            # Include video ID for uniqueness
            safe_video_id = self.sanitize_filename(video_id, max_length=20)
            filename = f"{safe_id}_{safe_name}_{safe_title}_[{safe_video_id}].{extension}"
        else:
            filename = f"{safe_id}_{safe_name}_{safe_title}.{extension}"
        
        # Max filename limit
        max_len = 250
        if len(filename) > max_len:
            # Keep ID, name, and extension, truncate title
            ext_len = len(extension) + 1  # +1 for dot
            prefix_len = len(f"{safe_id}_{safe_name}_")
            title_max = max_len - prefix_len - ext_len
            safe_title = safe_title[:title_max]
            filename = f"{safe_id}_{safe_name}_{safe_title}.{extension}"
        
        return filename
    
    def get_profile_photo_filename(self, channel_id: Optional[str], channel_name: Optional[str]) -> str:
        """
        Generate profile photo filename.
        
        Format: {channel_id}_{channel_name}.jpg
        
        Args:
            channel_id: YouTube channel ID
            channel_name: Channel name
            
        Returns:
            str: Profile photo filename
        """
        safe_id = self.sanitize_filename(channel_id or "unknown", max_length=50)
        safe_name = self.sanitize_filename(channel_name or "unknown", max_length=50)
        return f"{safe_id}_{safe_name}.jpg"
    
    def get_video_path(
        self,
        channel_id: Optional[str],
        channel_name: Optional[str],
        video_title: str,
        video_id: Optional[str] = None,
        extension: str = "mp4"
    ) -> Path:
        """
        Get full path for video file.
        
        Args:
            channel_id: YouTube channel ID
            channel_name: Channel name
            video_title: Video title
            video_id: Video ID
            extension: File extension
            
        Returns:
            Path: Full path to video (channel folder created if needed)
        """
        channel_path = self.get_channel_path(channel_id, channel_name)
        filename = self.get_video_filename(channel_id, channel_name, video_title, video_id, extension)
        return channel_path / filename
    
    def get_profile_photo_path(self, channel_id: Optional[str], channel_name: Optional[str]) -> Path:
        """
        Get full path for profile photo.
        
        Args:
            channel_id: YouTube channel ID
            channel_name: Channel name
            
        Returns:
            Path: Full path to profile photo (channel folder created if needed)
        """
        channel_path = self.get_channel_path(channel_id, channel_name)
        filename = self.get_profile_photo_filename(channel_id, channel_name)
        return channel_path / filename
    
    def profile_photo_exists(self, channel_id: Optional[str], channel_name: Optional[str]) -> bool:
        """
        Check if profile photo already exists.
        
        Args:
            channel_id: YouTube channel ID
            channel_name: Channel name
            
        Returns:
            bool: True if photo exists
        """
        photo_path = self.get_profile_photo_path(channel_id, channel_name)
        return photo_path.exists() and photo_path.is_file()
    
    def find_existing_video(
        self,
        channel_id: Optional[str],
        channel_name: Optional[str],
        video_id: Optional[str] = None
    ) -> Optional[Path]:
        """
        Find existing video file by video ID.
        
        Args:
            channel_id: YouTube channel ID
            channel_name: Channel name
            video_id: Video ID to search for
            
        Returns:
            Optional[Path]: Path to existing video or None
        """
        if not video_id:
            return None
        
        channel_path = self.get_channel_path(channel_id, channel_name)
        
        # Search for files with video ID in name
        for ext in ['mp4', 'webm', 'mkv', 'avi']:
            # Pattern: *channel_id*video_id*.*
            pattern = f"*{video_id}*.{ext}"
            matches = list(channel_path.glob(pattern))
            
            # Filter out partial files
            matches = [
                m for m in matches
                if not m.suffix.lower().endswith(('.part', '.temp', '.tmp'))
            ]
            
            if matches:
                return matches[0]
        
        return None


def get_download_structurer(base_path: str) -> DownloadStructurer:
    """
    Get or create a download structurer instance.
    
    Args:
        base_path: Base downloads directory
        
    Returns:
        DownloadStructurer: Configured structurer instance
    """
    return DownloadStructurer(base_path)
