#!/usr/bin/env python3
"""
YouTube Video Processor - Core Download Engine
==============================================
Handles the actual downloading of YouTube videos using yt-dlp.

Features:
- High-quality video downloading with format selection
- Automatic thumbnail and metadata extraction
- Cookie-based authentication support for private videos
- Comprehensive error handling and retry logic
- Progress tracking and logging
- File organization and naming conventions

This module serves as the core downloading engine used by the batch downloader.
"""

import os
import yt_dlp
import time
import glob
import re
import sqlite3
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from typing import List, Dict, Optional, Any, Tuple, Union

from app_paths import DEFAULT_DOWNLOADS_DIR
from download_path_manager import prompt_for_download_path

# Import new modules
# Check imports separately to isolate rate limiter issues
try:
    from rate_limiter import get_rate_limiter
    RATE_LIMITER_AVAILABLE = True
except ImportError:
    RATE_LIMITER_AVAILABLE = False
    print("⚠️  rate_limiter.py not found - rate limiting disabled")

try:
    from download_structurer import DownloadStructurer
except ImportError:
    DownloadStructurer = None
    print("⚠️  download_structurer.py not found - using basic download mode")

# Dynamic path resolution
TOOLKIT_ROOT = Path(__file__).resolve().parent
DEFAULT_DOWNLOADS = DEFAULT_DOWNLOADS_DIR


def extract_video_id(url: str) -> Optional[str]:
    """Extract YouTube video ID from URL"""
    try:
        parsed = urlparse(url)
        if 'youtube.com' in parsed.netloc:
            return parse_qs(parsed.query).get('v', [None])[0]
        elif 'youtu.be' in parsed.netloc:
            return parsed.path.lstrip('/')
    except Exception:
        pass
    return None

def check_existing_download_file(url: str, path: str) -> Optional[str]:
    """Check if video file already exists by checking file patterns"""
    video_id = extract_video_id(url)
    if not video_id:
        return None
    
    # Check for existing files with this video ID
    patterns = [
        f"*{video_id}*.mp4",
        f"*{video_id}*.webm", 
        f"*{video_id}*.mkv",
        f"*{video_id}*.avi"
    ]
    
    for pattern in patterns:
        matches = glob.glob(os.path.join(path, pattern))
        if matches:
            return matches[0]  # Return first match
    
    return None

def cleanup_partial_files(url: str, path: str) -> None:
    """Clean up partial download files"""
    video_id = extract_video_id(url)
    if not video_id:
        return
    
    # Look for partial files
    partial_patterns = [
        f"*{video_id}*.part",
        f"*{video_id}*.temp",
        f"*{video_id}*.tmp"
    ]
    
    for pattern in partial_patterns:
        matches = glob.glob(os.path.join(path, pattern))
        for partial_file in matches:
            try:
                os.remove(partial_file)
                print(f"🗑️  Cleaned up partial file: {os.path.basename(partial_file)}")
            except Exception as e:
                print(f"⚠️  Could not remove partial file {partial_file}: {e}")

def get_available_browsers_and_profiles() -> List[Tuple[str, Optional[str], str]]:
    """Detect all available browsers and Chrome/Edge profiles"""
    available_options = []
    
    # 1. Check Chrome profiles (Windows paths)
    chrome_profiles = []
    chrome_base_path = os.path.expanduser("~\\AppData\\Local\\Google\\Chrome\\User Data")
    
    if os.path.exists(chrome_base_path):
        # Check default profile
        default_profile = os.path.join(chrome_base_path, "Default")
        if os.path.exists(default_profile):
            chrome_profiles.append(("chrome", None, "Chrome (Default Profile)"))
        
        # Check numbered profiles (Profile 1, Profile 2, etc.)
        for i in range(1, 20):  # Check up to 20 profiles
            profile_path = os.path.join(chrome_base_path, f"Profile {i}")
            if os.path.exists(profile_path):
                chrome_profiles.append(("chrome", f"Profile {i}", f"Chrome (Profile {i})"))
        
        # Check named profiles by scanning directories
        try:
            for item in os.listdir(chrome_base_path):
                item_path = os.path.join(chrome_base_path, item)
                if os.path.isdir(item_path) and item.startswith("Profile") and item not in [f"Profile {i}" for i in range(1, 20)]:
                    chrome_profiles.append(("chrome", item, f"Chrome ({item})"))
        except Exception:
            pass
    
    # Add Chrome profiles to available options
    available_options.extend(chrome_profiles)
    
    # 2. Check Microsoft Edge profiles (Windows paths)
    edge_profiles = []
    edge_base_path = os.path.expanduser("~\\AppData\\Local\\Microsoft\\Edge\\User Data")
    
    if os.path.exists(edge_base_path):
        # Check default profile
        default_profile = os.path.join(edge_base_path, "Default")
        if os.path.exists(default_profile):
            edge_profiles.append(("edge", None, "Edge (Default Profile)"))
        
        # Check numbered profiles (Profile 1, Profile 2, etc.)
        for i in range(1, 20):  # Check up to 20 profiles
            profile_path = os.path.join(edge_base_path, f"Profile {i}")
            if os.path.exists(profile_path):
                edge_profiles.append(("edge", f"Profile {i}", f"Edge (Profile {i})"))
        
        # Check named profiles by scanning directories
        try:
            for item in os.listdir(edge_base_path):
                item_path = os.path.join(edge_base_path, item)
                if os.path.isdir(item_path) and item.startswith("Profile") and item not in [f"Profile {i}" for i in range(1, 20)]:
                    edge_profiles.append(("edge", item, f"Edge ({item})"))
        except Exception:
            pass
    
    # Add Edge profiles to available options
    available_options.extend(edge_profiles)
    
    # 3. Check other browsers
    other_browsers = [
        ("firefox", None, "Firefox"),
        ("safari", None, "Safari"),
        ("opera", None, "Opera"),
        ("brave", None, "Brave"),
        ("chromium", None, "Chromium")
    ]
    
    available_options.extend(other_browsers)
    
    return available_options

def test_browser_cookies(browser: str, profile: Optional[str] = None) -> bool:
    """Test if cookies can be extracted from a specific browser/profile"""
    try:
        test_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': True,
            'skip_download': True
        }
        
        if profile:
            test_opts['cookiesfrombrowser'] = (browser, None, None, profile)
        else:
            test_opts['cookiesfrombrowser'] = (browser,)
        
        # Test with a simple YouTube URL
        with yt_dlp.YoutubeDL(test_opts) as test_ydl:
            # Try to extract cookies without actually downloading
            test_ydl.cookiejar  # This will trigger cookie loading
            return True
            
    except Exception as e:
        return False

def get_best_cookie_option() -> Optional[Tuple[str, Optional[str], str]]:
    """Find the best available cookie option, prioritizing local files to avoid browser scan noise."""
    # 1. First priority: Check for local cookie files in common locations
    print("🔍 Looking for local cookie files...")
    data_dir = Path(__file__).resolve().parent.parent / "data"
    cookie_search_paths = [
        data_dir / "cookies.txt",
        data_dir / "youtube_cookies.txt",
        Path("cookies.txt"),
        Path("youtube_cookies.txt"),
        Path("yt_cookies.txt"),
    ]
    for cookie_path in cookie_search_paths:
        if cookie_path.exists():
            print(f"✅ Found cookie file: {cookie_path}")
            # Verify the file is not empty or basic sanity check
            if os.path.getsize(cookie_path) > 100:
                return ("file", str(cookie_path), f"Cookie file: {cookie_path.name}")

    # 2. Second priority: Scan browser profiles only if no local file is found
    print("🔍 No local cookies found. Scanning for browser cookies (this may be noisy)...")
    
    available_browsers = get_available_browsers_and_profiles()
    working_options = []
    
    for browser, profile, display_name in available_browsers:
        # Don't print "Testing..." unless we are actually going to try it, to reduce noise
        if test_browser_cookies(browser, profile):
            working_options.append((browser, profile, display_name))
            print(f"  ✅ Working browser: {display_name}")
    
    if working_options:
        # Prefer Chrome default profile, then other Chrome profiles, then Edge profiles, then other browsers
        chrome_default = [(b, p, d) for b, p, d in working_options if b == "chrome" and p is None]
        chrome_others = [(b, p, d) for b, p, d in working_options if b == "chrome" and p is not None]
        edge_default = [(b, p, d) for b, p, d in working_options if b == "edge" and p is None]
        edge_others = [(b, p, d) for b, p, d in working_options if b == "edge" and p is not None]
        other_browsers = [(b, p, d) for b, p, d in working_options if b not in ["chrome", "edge"]]
        
        # Return the best option
        if chrome_default:
            return chrome_default[0]
        elif chrome_others:
            return chrome_others[0]
        elif edge_default:
            return edge_default[0]
        elif edge_others:
            return edge_others[0]
        elif other_browsers:
            return other_browsers[0]

    print("❌ No usable cookies found")
    print("💡 To fix: export cookies from your browser using the 'Get cookies.txt LOCALLY' Chrome extension")
    print(f"   then save as: {data_dir / 'cookies.txt'}")
    return None

def create_ydl_options_with_specific_cookie(path: str, cookie_choice: Optional[Union[str, Tuple[str, Optional[str], str]]] = None, progress_hook: Optional[Any] = None) -> Dict[str, Any]:
    """Create yt-dlp options with specific cookie choice."""
    
    # Check config for download preferences
    try:
        from config import config
        max_res = config.get('download.max_resolution', '720')
        audio_only = config.get('download.audio_only', False)
        
        if audio_only:
            format_str = 'bestaudio/best'
        elif str(max_res).lower() == 'best' or str(max_res) == '0':
            format_str = 'bestvideo+bestaudio/best'
        else:
            format_str = f'best[height<={max_res}]/best'
    except Exception:
        format_str = 'best[height<=720]/best'
        
    ydl_opts = {
        'outtmpl': os.path.join(path, '%(title)s.%(ext)s'),
        'format': format_str,
        'writesubtitles': False,
        'writeautomaticsub': False,
        'ignoreerrors': False,
        'no_warnings': False,
        'retries': 3,
        'fragment_retries': 3,
        # Enable remote components to solve YouTube JS challenges (n-parameter, etc.)
        'remote_components': ['ejs:github'],
        # yt-dlp defaults to deno-only; node is installed (v26) so enable it explicitly.
        # Python API takes {runtime: {config}} dict, not a list like the CLI does.
        'js_runtimes': {'node': {}},
        # Try android client first — it bypasses the n-challenge entirely, then fall
        # back to web clients which need the JS solver.
        'extractor_args': {'youtube': {'player_client': ['android', 'web', 'mweb']}},
    }
    
    if progress_hook:
        ydl_opts['progress_hooks'] = [progress_hook]
    
    if cookie_choice and cookie_choice != "auto":
        browser, profile_or_file, display_name = cookie_choice
        
        if browser == "file":
            # Use cookie file
            ydl_opts['cookiefile'] = profile_or_file
            print(f"🍪 Using {display_name}")
        else:
            # Use browser cookies
            if profile_or_file:
                # Chrome profile specified
                ydl_opts['cookiesfrombrowser'] = (browser, None, None, profile_or_file)
            else:
                # Default browser profile
                ydl_opts['cookiesfrombrowser'] = (browser,)
            
            print(f"🍪 Using cookies from {display_name}")
    elif cookie_choice == "auto":
        # Auto-detect best option
        best_option = get_best_cookie_option()
        if best_option:
            browser, profile_or_file, display_name = best_option
            
            if browser == "file":
                ydl_opts['cookiefile'] = profile_or_file
            else:
                if profile_or_file:
                    ydl_opts['cookiesfrombrowser'] = (browser, None, None, profile_or_file)
                else:
                    ydl_opts['cookiesfrombrowser'] = (browser,)
            
            print(f"🍪 Auto-selected: {display_name}")
    
    return ydl_opts

def resolve_cookie_choice(
    use_cookies: bool,
    cookie_choice: Optional[Union[str, Tuple[str, Optional[str], str]]],
) -> Optional[Union[str, Tuple[str, Optional[str], str]]]:
    """Resolve the caller's authentication preference into a concrete cookie mode."""
    if not use_cookies:
        return None
    return cookie_choice or "auto"

def download_youtube_video(url: str, path: Optional[str] = None, use_cookies: bool = False, cookie_choice: Optional[Union[str, Tuple[str, Optional[str], str]]] = None, download_profile_photo: bool = False) -> Union[str, Dict[str, Any], None]:
    """Download a YouTube video or profile photo using yt-dlp with enhanced duplicate prevention and cookie support."""
    # Resolve download path with mandatory prompting
    if path is None:
        default_path = str(DEFAULT_DOWNLOADS_DIR)
        path = prompt_for_download_path(
            context="YouTube profile photos" if download_profile_photo else "YouTube video",
            out_path=None,
            default_path=default_path
        )
    else:
        path = str(Path(path).resolve())
    
    # Ensure profile photo directory
    if download_profile_photo:
        path = os.path.join(path, "profile_photos")
        os.makedirs(path, exist_ok=True)

    operation_start = None
    progress_ui = None
    
    try:
        # Check database for existing download (skip for profile photos for now)
        if not download_profile_photo:
            existing_file = check_existing_download_file(url, path)
            if existing_file:
                print(f"✅ Video already downloaded: {existing_file}")
                return existing_file
        
        cleanup_partial_files(url, path)
        
        # Create download directory if it doesn't exist
        if not os.path.exists(path):
            os.makedirs(path, exist_ok=True)
        
        effective_cookie_choice = resolve_cookie_choice(use_cookies, cookie_choice)
        ydl_opts = create_ydl_options_with_specific_cookie(path, cookie_choice=effective_cookie_choice)
        
        if download_profile_photo:
            ydl_opts.update({
                'skip_download': True,
                'writethumbnail': True,
                'outtmpl': os.path.join(path, '%(uploader)s_%(uploader_id)s.%(ext)s'),
            })

        # Initial attempt without cookies for public videos
        current_ydl_opts = ydl_opts.copy()
        
        # If we have cookies configured, temporarily remove them for the first attempt 
        # unless explicitly requested for age-gated/private content.
        # Most "n" parameter and format issues are solved by remote_components, not cookies.
        had_cookies = False
        if 'cookiefile' in current_ydl_opts or 'cookiesfrombrowser' in current_ydl_opts:
            had_cookies = True
            cookie_backup = {
                'cookiefile': current_ydl_opts.pop('cookiefile', None),
                'cookiesfrombrowser': current_ydl_opts.pop('cookiesfrombrowser', None)
            }
            print("🔍 Attempting download without cookies (preferred for public videos)...")
        
        try:
            with yt_dlp.YoutubeDL(current_ydl_opts) as ydl:
                print(f"🔄 Extracting info from: {url}")
                info = ydl.extract_info(url, download=not download_profile_photo)
                # Success! Proceed as normal
        except Exception as e:
            # If it failed and we had cookies available, try one more time WITH cookies
            if had_cookies:
                print(f"⚠️  Download without cookies failed or restricted. Retrying with cookies...")
                current_ydl_opts.update({k: v for k, v in cookie_backup.items() if v is not None})
                with yt_dlp.YoutubeDL(current_ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=not download_profile_photo)
            else:
                # No cookies were available to fall back to, or it's a different error
                raise e

        if download_profile_photo:
            uploader = info.get('uploader', 'Unknown')
            uploader_id = info.get('uploader_id', 'Unknown')
            photo_matches = glob.glob(os.path.join(path, f"{uploader}_{uploader_id}.*"))
            if photo_matches:
                print(f"✅ Profile photo saved: {photo_matches[0]}")
                return photo_matches[0]
            return None

        # Video download logic
        video_title = info.get('title', 'Unknown')
        # Re-initialize ydl for filename preparation since the context managers are closed
        with yt_dlp.YoutubeDL(current_ydl_opts) as ydl:
            expected_filename = ydl.prepare_filename(info)
        
        base_name = os.path.splitext(expected_filename)[0]
        downloaded_file = None
        
        # Try hardcoded extensions first (fast path)
        for ext in ['.mp4', '.webm', '.mkv', '.avi']:
            if os.path.exists(base_name + ext):
                downloaded_file = base_name + ext
                break
        
        # Fallback 1: Check yt-dlp's reported downloaded files
        if not downloaded_file and info and 'requested_downloads' in info:
            for download in info['requested_downloads']:
                if 'filepath' in download and os.path.exists(download['filepath']):
                    downloaded_file = download['filepath']
                    break
        
        # Fallback 2: Glob for any file with video_id in the download directory
        if not downloaded_file:
            video_id = extract_video_id(url)
            if video_id:
                matches = glob.glob(os.path.join(path, f"*{video_id}*"))
                # Filter out partial files
                matches = [m for m in matches if not m.endswith(('.part', '.temp', '.tmp'))]
                if matches:
                    downloaded_file = matches[0]
        
        if downloaded_file:
                file_size = os.path.getsize(downloaded_file)
                
                # Scrape links from description
                description = info.get('description', '')
                if description:
                    links = re.findall(r'(https?://[^\s]+)', description)
                    if links:
                        # Add common path for db_manager if needed, but we should use the existing one
                        from data_manager_streamlined import DatabaseManager
                        dm = DatabaseManager()
                        dm.save_scraped_links(url, links)
                        print(f"🔗 Scraped {len(set(links))} unique links from description")

                return downloaded_file

    except Exception as e:
        raise

def resume_interrupted_downloads(download_folder=None):
    """Resume downloads that were interrupted"""
    # Import here to avoid circular imports
    from data_manager_streamlined import DatabaseManager
    dm = DatabaseManager()
    
    # We'll use a simplified version for the streamlined toolkit
    # Find videos marked as 'downloading'
    with sqlite3.connect(dm.db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM videos WHERE download_status = 'downloading'")
        interrupted = [dict(row) for row in cursor.fetchall()]
    
    if not interrupted:
        print("✅ No interrupted downloads found")
        return 0
    
    print(f"🔄 Found {len(interrupted)} interrupted downloads")
    
    # Ask for download folder if not provided
    if download_folder is None:
        default_path = str(DEFAULT_DOWNLOADS_DIR)
        download_folder = prompt_for_download_path(
            context="interrupted downloads",
            default_path=default_path
        )
    
    resumed_count = 0
    for video in interrupted:
        print(f"📥 Resuming: {video.get('title', 'Unknown')} - {video['url']}")
        cleanup_partial_files(video['url'], download_folder)
        dm.update_download_status(video['url'], 'pending')
        
        try:
            result = download_youtube_video(video['url'], path=download_folder, use_cookies=True)
            if result:
                file_size = os.path.getsize(result) if isinstance(result, str) and os.path.exists(result) else 0
                dm.update_download_status(video['url'], 'completed', result if isinstance(result, str) else None, file_size)
                resumed_count += 1
        except Exception as e:
            dm.update_download_status(video['url'], 'failed', error_message=str(e))
            print(f"❌ Failed to resume {video['url']}: {e}")
    
    print(f"📊 Resume summary: {resumed_count}/{len(interrupted)} downloads completed")
    return resumed_count


def download_with_rate_limiting(
    url: str,
    path: Optional[str] = None,
    use_cookies: bool = False,
    cookie_choice: Optional[Union[str, Tuple[str, Optional[str], str]]] = None,
    download_profile_photo: bool = False,
    channel_id: Optional[str] = None,
    channel_name: Optional[str] = None,
    max_retries: int = 5
) -> Union[str, Dict[str, Any], None]:
    """
    Enhanced download function with automatic rate limiting and channel-based structure.
    
    This is a wrapper around download_youtube_video that adds:
    - Human-like rate limiting with jitter
    - Exponential backoff on 429 errors
    - Channel-based download organization
    
    Args:
        url: YouTube video/channel URL
        path: Download directory (optional)
        use_cookies: Whether to use browser cookies
        cookie_choice: Specific cookie choice
        download_profile_photo: Download profile photo instead of video
        channel_id: YouTube channel ID for organization
        channel_name: Channel name for folder structure
        max_retries: Max retries on 429 errors
        
    Returns:
        File path on success, None on failure
    """
    # Get rate limiter instance
    rate_limiter = get_rate_limiter() if RATE_LIMITER_AVAILABLE else None
    
    # Retry loop with exponential backoff
    for attempt in range(max_retries + 1):
        # Rate limit before each attempt (except first attempt which has its own delay)
        if rate_limiter and attempt > 0:
            if not rate_limiter.wait_with_backoff(url, attempt - 1):
                print("⚠️  Shutdown requested during backoff")
                return None
        elif rate_limiter and attempt == 0:
            if not rate_limiter.can_proceed(url):
                print("⚠️  Shutdown requested, aborting download")
                return None
            delay = rate_limiter.wait_for_slot(url)
            if delay > 0:
                print(f"⏱️  Rate limiting: waited {delay:.2f}s before request")
        
        try:
            result = download_youtube_video(
                url=url,
                path=path,
                use_cookies=use_cookies,
                cookie_choice=cookie_choice,
                download_profile_photo=download_profile_photo,
            )
            
            # Reset backoff on success
            if rate_limiter:
                rate_limiter.record_request(url)
                rate_limiter.reset_backoff(url)
            
            return result
            
        except Exception as e:
            # Check for 429 errors and authentication issues
            error_str = str(e).lower()
            if ('429' in error_str or 
                'too many requests' in error_str or 
                'sign in to confirm' in error_str or
                'unusual traffic' in error_str or
                'concurrent requests' in error_str):
                if rate_limiter:
                    rate_limiter.record_error(url, 429)
                
                if attempt < max_retries:
                    print(f"⚠️  Rate limit hit (attempt {attempt + 1}/{max_retries + 1}): {str(e)[:100]}")
                    
                    # Calculate backoff delay
                    backoff_delay = (2 ** attempt) if not rate_limiter else rate_limiter.get_backoff_delay(url, attempt)
                    print(f"⏳ Backing off for {backoff_delay:.1f}s...")
                    import time
                    time.sleep(backoff_delay)
                    continue  # Retry
                else:
                    print(f"❌ Max retries ({max_retries}) exceeded for: {str(e)[:100]}")
                    return None
            else:
                # Non-retryable error, raise immediately
                print(f"❌ Non-retryable error: {e}")
                return None
    
    return None


def main():
    """Main function for downloading YouTube videos"""
    
    print("\n" + "="*80)
    print("🎬 YOUTUBE TOOLKIT - DOWNLOADER")
    print("="*80)
    
    # Menu for clear options
    print("1. Download Video")
    print("2. Download Channel Profile Photo")
    print("3. Resume Interrupted Downloads")
    print("4. Exit")
    
    choice = input("\nSelect an option (1-4): ").strip()
    
    if choice == '4':
        return
    elif choice == '3':
        resume_interrupted_downloads()
        return

    # Get video URL
    while True:
        youtube_url = input("\n📝 Enter the YouTube video/channel URL: ").strip()
        if youtube_url: break
        print("❌ URL cannot be empty")

    if choice == '2':
        download_youtube_video(youtube_url, download_profile_photo=True)
        return

    # Option 1: Download Video
    # Cookie authentication options
    print(f"\n🍪 AUTHENTICATION OPTIONS:")
    print(f"1. Use browser cookies (recommended)")
    print(f"2. No authentication")
    
    use_cookies = input("\nChoose (1/2, default=1): ").strip() != '2'
    
    try:
        video_path = download_youtube_video(youtube_url, use_cookies=use_cookies)
        if video_path:
            print(f"\n✅ Success! File saved at: {video_path}")
    except Exception as e:
        print(f"\n❌ Error: {e}")

if __name__ == '__main__':
    main()
