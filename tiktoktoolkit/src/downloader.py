"""Unified downloader module for TikTok user content."""

import logging
from pathlib import Path
from typing import List, Dict

from . import resilience
from .models import DownloadResult
from .provider import GalleryDLProvider
from .utils import read_usernames_from_file

logger = logging.getLogger('uttk.downloader')


class TikTokDownloader:
    """TikTok downloader focused on user profiles and posts."""
    
    def __init__(self, provider: GalleryDLProvider):
        self.provider = provider
    
    def download_user(self, username: str, limit: int, output_dir: Path, download_type: str = 'videos') -> List[DownloadResult]:
        """Download videos or profile pictures from a user profile."""
        logger.info(f"Downloading {limit} {download_type} from user @{username}")
        
        try:
            results = self.provider.download_user(username, limit, output_dir, download_type=download_type)
            successful = len([r for r in results if r.ok])
            logger.info(f"Downloaded {successful}/{len(results)} {download_type} from user @{username}")
            return results
        except Exception as e:
            logger.error(f"Failed to download from user @{username}: {e}")
            raise
    
    def download_users_bulk(
        self, 
        usernames: List[str], 
        limit_per_user: int, 
        output_dir: Path,
        download_type: str = 'videos'
    ) -> Dict[str, List[DownloadResult]]:
        """Download videos or profile pictures from multiple users."""
        results = {}
        total_downloaded = 0
        
        logger.info(f"Starting bulk download for {len(usernames)} users ({download_type})")
        
        for i, username in enumerate(usernames, 1):
            if resilience.is_shutdown():
                logger.info("Shutdown requested; stopping bulk download before processing next user")
                break

            logger.info(f"Processing user {i}/{len(usernames)}: @{username}")
            
            try:
                user_results = self.download_user(username, limit_per_user, output_dir, download_type=download_type)
                results[username] = user_results
                successful_downloads = len([r for r in user_results if r.ok])
                total_downloaded += successful_downloads
                
                logger.info(f"User @{username}: {successful_downloads}/{len(user_results)} successful downloads")

                if resilience.is_shutdown():
                    logger.info("Shutdown requested; stopping bulk download after current user")
                    break
                
            except Exception as e:
                logger.error(f"Failed to download from user @{username}: {e}")
                results[username] = []
                if resilience.is_shutdown():
                    logger.info("Shutdown requested after user failure; aborting remaining bulk download")
                    break
        
        logger.info(f"Bulk download completed: {total_downloaded} total {download_type} from {len(usernames)} users")
        return results
    
    def download_users_from_file(
        self,
        file_path: str,
        limit_per_user: int,
        output_dir: Path,
        download_type: str = 'videos'
    ) -> Dict[str, List[DownloadResult]]:
        """Download videos or profile pictures from users listed in a text file."""
        try:
            usernames = read_usernames_from_file(file_path)
            logger.info(f"Read {len(usernames)} usernames from {file_path}")
            
            if not usernames:
                logger.warning("No valid usernames found in file")
                return {}
            
            return self.download_users_bulk(usernames, limit_per_user, output_dir, download_type=download_type)
            
        except FileNotFoundError as e:
            logger.error(f"Username file not found: {e}")
            raise
        except Exception as e:
            logger.error(f"Error reading usernames from file: {e}")
            raise
