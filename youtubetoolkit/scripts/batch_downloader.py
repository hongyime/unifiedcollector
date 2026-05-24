#!/usr/bin/env python3
"""
Streamlined Batch Downloader
============================
Core video downloading functionality for the YouTube toolkit.

Features:
- Downloads all pending videos from SQLite database
- Real-time progress tracking with ETA calculations  
- Optional channel-based filtering via target_channels.txt
- Automatic duplicate detection and skipping
- Comprehensive error handling and recovery
- Database status updates for each download

This module handles the actual video downloading after URLs have been
scraped and stored in the database by other modules.
"""

import os
import time
from datetime import datetime
from typing import List, Optional, Tuple, Dict, Any, Union

from app_paths import DEFAULT_DOWNLOADS_DIR
from download_path_manager import prompt_for_download_path
from data_manager_streamlined import DatabaseManager

# Import video processor for downloads
try:
    from video_processor import download_youtube_video, download_with_rate_limiting
    VIDEO_PROCESSOR_AVAILABLE = True
except ImportError:
    print("⚠️  video_processor.py not found, using basic download")
    VIDEO_PROCESSOR_AVAILABLE = False
    download_youtube_video = None  # Make it patchable even when not available
    download_with_rate_limiting = None

class BatchDownloader:
    """
    Handles batch downloading of YouTube videos from database.
    
    Supports both full database downloads and channel-filtered downloads
    using a target channels file for selective processing.
    """
    def __init__(self, download_folder: Optional[str] = None, target_channels_file: Optional[str] = None, days: Optional[int] = None, photos_only: bool = False):
        self.db = DatabaseManager()
        self.days = days
        self.photos_only = photos_only
        
        # Mandatory path prompting
        if not download_folder:
            default_path = str(DEFAULT_DOWNLOADS_DIR)
            download_folder = prompt_for_download_path(
                context="YouTube profile photos" if self.photos_only else "YouTube videos",
                out_path=download_folder,
                default_path=default_path
            )
        
        self.download_folder = download_folder
        self.target_channels_file = target_channels_file
        self.target_channels: Optional[List[str]] = self.load_target_channels() if target_channels_file else None
        self.stats: Dict[str, Any] = {
            'total': 0,
            'successful': 0,
            'failed': 0,
            'skipped': 0,
            'start_time': None
        }
        
    def load_target_channels(self) -> Optional[List[str]]:
        """
        Load target channel IDs from file for filtered downloads.
        
        Returns:
            List of channel IDs or None if no file/no valid channels
        """
        if not self.target_channels_file or not os.path.exists(self.target_channels_file):
            return None
            
        channels: set[str] = set()
        try:
            with open(self.target_channels_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#'):
                        channels.add(line)
        except Exception as e:
            print(f"❌ Error reading target channels: {e}")
            
        return list(channels) if channels else None
        
    def ensure_download_folder(self):
        """Create download folder if it doesn't exist"""
        os.makedirs(self.download_folder, exist_ok=True)
        
    def get_pending_videos(self) -> List[Tuple[Any, ...]]:
        """Get all videos pending download, optionally filtered by target channels and duration"""
        from config import config
        
        try:
            # CRITICAL FIX: For photos_only, use the optimized unique channels query
            # This reduces downloads from N videos to N unique channels
            if self.photos_only:
                print("📸 Using optimized query for profile photos - 1 per unique channel")
                videos = self.db.get_pending_channels_with_photos(limit=9999, days=self.days)
                print(f"📺 Found {len(videos)} unique channels needing profile photos")
            elif self.target_channels:
                # Use channel filtering
                videos = self.db.get_unprocessed_videos_by_channels(self.target_channels, limit=9999, days=self.days)
                print(f"📺 Found {len(videos)} pending videos from {len(self.target_channels)} target channels")
            else:
                # Get all videos
                videos = self.db.get_unprocessed_videos(limit=9999, days=self.days)  # Get all
                print(f"📺 Found {len(videos)} pending videos from all channels")
            
            # Apply duration filter if configured
            max_duration_minutes = config.get('download.max_video_duration_minutes', 0)
            if max_duration_minutes > 0:
                max_duration_seconds = max_duration_minutes * 60
                original_count = len(videos)
                # Filter: video[5] is duration in seconds (id, url, title, channel, channel_id, duration)
                videos = [v for v in videos if len(v) <= 5 or not v[5] or v[5] <= max_duration_seconds]
                filtered_count = original_count - len(videos)
                if filtered_count > 0:
                    print(f"⏱️  Filtered out {filtered_count} videos longer than {max_duration_minutes} minutes")
                    print(f"📺 {len(videos)} videos remaining after duration filter")
            
            return videos
        except Exception as e:
            print(f"❌ Error getting pending videos: {e}")
            return []
    
    def get_failed_videos(self) -> List[Dict[str, Any]]:
        """Get all videos that failed to download"""
        try:
            return self.db.get_failed_downloads()
        except Exception as e:
            print(f"❌ Error getting failed videos: {e}")
            return []
    
    def download_single_video(self, video_data: Union[Tuple, List]) -> bool:
        """Download a single video or profile photo"""
        video_id = video_data[0]
        url = video_data[1]
        title = video_data[2] if len(video_data) > 2 else "Unknown"
        channel_id = video_data[4] if len(video_data) > 4 else None
        
        # Import config for settings
        from config import config
        
        # Determine if we should use cookies and rate limiting
        use_cookies = config.get('cookies.use_cookies', False)
        cookie_browser = config.get('cookies.browser', 'auto')
        delay_between = config.get('download.delay_seconds', 5.0)
        
        if self.photos_only:
            print(f"\n📸 Downloading Profile Photo for channel associated with: {title or url}")
            try:
                if VIDEO_PROCESSOR_AVAILABLE and download_with_rate_limiting:
                    result = download_with_rate_limiting(
                        url=url,
                        path=self.download_folder,
                        use_cookies=use_cookies,
                        cookie_choice=cookie_browser,
                        download_profile_photo=True,
                        max_retries=5
                    )
                    if result:
                        self.stats['successful'] += 1
                        print(f"   ✅ Photo download completed")
                        return True
                    else:
                        print(f"   ⏭️  Photo already exists or skipped")
                        self.stats['skipped'] += 1
                        return True
                elif VIDEO_PROCESSOR_AVAILABLE and download_youtube_video:
                    result = download_youtube_video(url, self.download_folder, download_profile_photo=True)
                    if result:
                        self.stats['successful'] += 1
                        print(f"   ✅ Photo download completed")
                        return True
                    else:
                        print(f"   ⏭️  Photo already exists or skipped")
                        self.stats['skipped'] += 1
                        return True
                else:
                    raise Exception("video_processor.py required for profile photos")
            except Exception as e:
                print(f"   ❌ Photo download failed: {e}")
                self.stats['failed'] += 1
                return False

        print(f"\n📥 Downloading: {title or url}")
        
        # Check if already downloaded
        existing_file = self.db.check_existing_download(url)
        if existing_file:
            print(f"   ✅ Already downloaded: {existing_file}")
            self.stats['skipped'] += 1
            return True
            
        # Add delay between downloads (after first one)
        if self.stats['successful'] > 0 or self.stats['failed'] > 0:
            print(f"   ⏱️  Waiting {delay_between}s before next download to avoid rate limits...")
            import time
            time.sleep(delay_between)
            
        try:
            # Atomic claim: skip if another process already claimed this URL
            if not self.db.update_download_status(url, 'downloading'):
                print(f"   ⏭️  Already claimed by another process, skipping")
                self.stats['skipped'] += 1
                return True
            
            if VIDEO_PROCESSOR_AVAILABLE and download_with_rate_limiting:
                # Use the rate-limited download function with cookies
                result = download_with_rate_limiting(
                    url=url,
                    path=self.download_folder,
                    use_cookies=use_cookies,
                    cookie_choice=cookie_browser,
                    max_retries=5
                )
                if result:
                    # Update database with success
                    # Only calculate file size if result is a valid path and file exists
                    if isinstance(result, str) and os.path.exists(result):
                        file_path = result
                        file_size = os.path.getsize(file_path)
                    else:
                        # Result is not a valid path or file doesn't exist
                        file_path = None
                        file_size = 0
                    
                    self.db.update_download_status(url, 'completed', file_path, file_size)
                    self.stats['successful'] += 1
                    print(f"   ✅ Download completed")
                    return True
                else:
                    raise Exception("Download failed")
            elif VIDEO_PROCESSOR_AVAILABLE and download_youtube_video:
                # Fallback to non-rate-limited if rate limiting failed
                result = download_youtube_video(
                    url, 
                    self.download_folder, 
                    use_cookies=use_cookies, 
                    cookie_choice=cookie_browser
                )
                if result:
                    # Update database with success
                    if isinstance(result, str) and os.path.exists(result):
                        file_path = result
                        file_size = os.path.getsize(file_path)
                    else:
                        file_path = None
                        file_size = 0
                    
                    self.db.update_download_status(url, 'completed', file_path, file_size)
                    self.stats['successful'] += 1
                    print(f"   ✅ Download completed (rate limiting unavailable)")
                    return True
                else:
                    raise Exception("Download failed")
            else:
                # Basic fallback using yt-dlp directly
                import subprocess
                cmd = [
                    'yt-dlp',
                    '--remote-components', 'ejs:github',
                    '-o', f'{self.download_folder}/%(title)s.%(ext)s',
                    url
                ]
                result = subprocess.run(cmd, capture_output=True, text=True)
                if result.returncode == 0:
                    self.db.update_download_status(url, 'completed')
                    self.stats['successful'] += 1
                    print(f"   ✅ Download completed (basic mode)")
                    return True
                else:
                    raise Exception(f"yt-dlp failed: {result.stderr}")
                    
        except Exception as e:
            print(f"   ❌ Download failed: {e}")
            self.db.update_download_status(url, 'failed', error_message=str(e))
            self.stats['failed'] += 1
            return False
    
    def download_all(self):
        """Download all pending videos or photos"""
        print("🚀 BATCH DOWNLOADER")
        print("=" * 50)
        
        # Setup
        self.ensure_download_folder()
        videos = self.get_pending_videos()
        
        if not videos:
            print("✅ No items pending download")
            return

        self.stats['total'] = len(videos)
        self.stats['start_time'] = time.time()
        
        print(f"📊 Found {len(videos)} items to process")
        print(f"📁 Download folder: {os.path.abspath(self.download_folder)}")
        
        proceed = input(f"\n🎯 Start processing {len(videos)} items? (y/N): ").strip().lower()
        if proceed != 'y':
            print("❌ Download cancelled")
            return
            
        print(f"\n🚀 Starting downloads...")
        
        # Process each video
        for i, video in enumerate(videos, 1):
            print(f"\n📊 Progress: {i}/{len(videos)} ({(i/len(videos)*100):.1f}%)")
            
            self.download_single_video(video)
            
            # Show running stats every 10 videos
            if i % 10 == 0:
                self.show_progress_stats()
                
        # Final summary
        self.show_final_summary()
        
    def retry_failed(self):
        """Retry all failed downloads"""
        print("🔄 RETRY FAILED DOWNLOADS")
        print("=" * 50)
        
        self.ensure_download_folder()
        videos_dict = self.get_failed_videos()
        
        if not videos_dict:
            print("✅ No failed videos found to retry")
            return
            
        # Convert dict to tuple format expected by download_single_video
        videos = [(v['id'], v['url'], v['title']) for v in videos_dict]
        
        self.stats['total'] = len(videos)
        self.stats['start_time'] = time.time()
        
        print(f"📊 Found {len(videos)} failed videos to retry")
        print(f"📁 Download folder: {os.path.abspath(self.download_folder)}")
        
        proceed = input(f"\n🎯 Start retrying {len(videos)} videos? (y/N): ").strip().lower()
        if proceed != 'y':
            print("❌ Retry cancelled")
            return
            
        print(f"\n🚀 Starting retries...")
        
        # Process each video
        for i, video in enumerate(videos, 1):
            print(f"\n📊 Progress: {i}/{len(videos)} ({(i/len(videos)*100):.1f}%)")
            
            # Reset database status to pending before retrying
            url = video[1]
            self.db.update_download_status(url, 'pending')
            
            self.download_single_video(video)
            
            # Show running stats every 10 videos
            if i % 10 == 0:
                self.show_progress_stats()
                
        # Final summary
        self.show_final_summary()
    
    def show_progress_stats(self):
        """Show current progress statistics"""
        elapsed = time.time() - self.stats['start_time']
        processed = self.stats['successful'] + self.stats['failed'] + self.stats['skipped']
        remaining = self.stats['total'] - processed
        
        print(f"\n📈 PROGRESS UPDATE:")
        print(f"   ✅ Successful: {self.stats['successful']}")
        print(f"   ❌ Failed: {self.stats['failed']}")
        print(f"   ⏭️  Skipped: {self.stats['skipped']}")
        print(f"   ⏱️  Elapsed: {elapsed/60:.1f} minutes")
        if processed > 0:
            avg_time = elapsed / processed
            eta = (avg_time * remaining) / 60
            print(f"   ⏰ ETA: {eta:.1f} minutes")
    
    def show_final_summary(self):
        """Show final download summary"""
        elapsed = time.time() - self.stats['start_time']
        
        print(f"\n🎉 DOWNLOAD COMPLETE!")
        print("=" * 50)
        print(f"📊 FINAL STATISTICS:")
        print(f"   📥 Total videos: {self.stats['total']}")
        print(f"   ✅ Successful: {self.stats['successful']}")
        print(f"   ❌ Failed: {self.stats['failed']}")
        print(f"   ⏭️  Skipped (already downloaded): {self.stats['skipped']}")
        print(f"   ⏱️  Total time: {elapsed/60:.1f} minutes")
        
        success_rate = (self.stats['successful'] / self.stats['total'] * 100) if self.stats['total'] > 0 else 0
        print(f"   📈 Success rate: {success_rate:.1f}%")
        
        print(f"\n📁 Downloads saved to: {os.path.abspath(self.download_folder)}")
        
        if self.stats['failed'] > 0:
            print(f"\n⚠️  {self.stats['failed']} videos failed to download")
            print("   Check the database for error details")

def main():
    """Main execution"""
    import argparse
    
    parser = argparse.ArgumentParser(description='Batch download YouTube videos')
    parser.add_argument('--download-folder', default=None,
                       help='Folder to save downloaded videos')
    parser.add_argument('--channels-file', 
                       help='Path to file containing target channel IDs (optional)')
    parser.add_argument('--interactive', action='store_true',
                       help='Use interactive mode for download folder selection')
    parser.add_argument('--retry-failed', action='store_true',
                       help='Retry downloading previously failed videos')
    parser.add_argument('--days', type=int,
                       help='Restrict downloads to videos added in the last N days')
    parser.add_argument('--photos-only', action='store_true',
                       help='Download only profile photos for the pending channels')
    
    args = parser.parse_args()
    
    print("📥 STREAMLINED BATCH DOWNLOADER")
    print("Downloads items from database")
    print()
    
    # Initialize downloader (will prompt for folder if needed)
    downloader = BatchDownloader(
        download_folder=args.download_folder,
        target_channels_file=args.channels_file,
        days=args.days,
        photos_only=args.photos_only
    )
    
    if args.retry_failed:
        print("🔄 Retrying failed downloads only")
        print()
        downloader.retry_failed()
    else:
        if args.channels_file:
            print(f"🎯 Using channel filter: {args.channels_file}")
        else:
            print("📺 Downloading from all channels")
        
        print()
        downloader.download_all()

if __name__ == '__main__':
    main()
