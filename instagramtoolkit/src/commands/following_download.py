"""
Following download command - Download media from accounts you follow.
"""
from src.commands.base import BaseCommand
import argparse


class FollowingDownloadCommand(BaseCommand):
    """Download media only from accounts in your following list."""
    
    name = "following-download"
    description = "Download media from followed accounts"
    help_text = "Download media only from accounts you follow"
    
    def _add_arguments(self):
        """Add following-download-specific arguments."""
        self.parser.add_argument(
            '--profile-only',
            action='store_true',
            help='Only download profile pictures'
        )
        self.parser.add_argument(
            '--posts-only',
            action='store_true',
            help='Only download posts'
        )
    
    def execute(self, args: argparse.Namespace) -> int:
        """Execute following media download."""
        try:
            from src.following_media_downloader import FollowingMediaDownloader
            
            # Create downloader
            downloader = FollowingMediaDownloader()
            
            # Select account
            if not downloader.select_account():
                return 1
            
            # Set download directory
            downloader._get_downloads_dir()
            
            # Get following list
            downloader.get_following_list()
            
            self.print_info(f"Processing {len(downloader.following_list)} followed accounts...")
            
            # Download from all followed accounts
            for username in downloader.following_list:
                downloader.download_account_media(username)
            
            # Cleanup
            downloader.cleanup()
            return 0
            
        except Exception as e:
            self.print_error(f"Following download failed: {e}")
            return 1


__all__ = ["FollowingDownloadCommand"]


