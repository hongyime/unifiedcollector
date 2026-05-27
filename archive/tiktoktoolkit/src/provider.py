"""Gallery-dl provider implementation.

Enhancements:
 - Optional JSON tracker (DownloadTracker) to avoid re-downloading media even if
     files are moved/archived from the output folder.
 - Pre-check using --list-urls (when supported) to short-circuit work when all
     recent videos are already tracked.
"""

import logging
import json
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import DownloadResult
from .errors import ProviderError
from .utils import build_output_path, create_folder_structure, remove_empty_dirs, extract_username_from_url, extract_video_id_from_filename
from .tracker import create_tracker
from . import resilience

logger = logging.getLogger('uttk.provider')


# Use shared resilience interruptible sleep
def _interruptible_sleep(seconds: float, check_interval: float = 0.2) -> None:
    """Sleep in short slices so Ctrl+C interrupts long backoff quickly.
    Delegates to resilience.interruptible_sleep for consistent shutdown handling.
    """
    resilience.interruptible_sleep(seconds, check_interval)


class GalleryDLProvider:
    """TikTok download provider using gallery-dl."""
    
    name = 'gallerydl'
    # Legacy regex kept for backward compatibility (now using extract_video_id_from_filename)
    VIDEO_ID_RE = re.compile(r'(\d{6,})')
    
    def __init__(self, config: Dict[str, Any]):
        # Store raw config
        self.config = config

        # Verify gallery-dl installed and cache version
        self.version = self._check_gallery_dl_installation()

        # Nested provider config
        self.gd_config = config.get('gallerydl', {}) or {}

        # Core operational settings
        self.retries = int(self.gd_config.get('retries', 3))
        self.sleep = int(self.gd_config.get('sleep', 1))
        # Timeout in seconds (config uses timeout_seconds, legacy 'timeout' assumed to be minutes)
        timeout_config = self.gd_config.get('timeout_seconds') or self.gd_config.get('timeout', 30)
        self.timeout_seconds = int(timeout_config) if self.gd_config.get('timeout_seconds') else int(timeout_config) * 60
        self.user_agent = self.gd_config.get('user_agent', "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
        self.proxy = self.gd_config.get('proxy')
        self.skip_existing = bool(self.gd_config.get('skip_existing', True))

        # Browser / cookies
        self.browser_enabled = bool(self.gd_config.get('browser_enabled', False))
        self.browser_headless = bool(self.gd_config.get('browser_headless', False))
        self.browser_executable = self.gd_config.get('browser_executable', 'webkit')
        
        # Browser automation fallback (anti-bot mitigation)
        self.browser_fallback_enabled = bool(self.gd_config.get('browser_fallback_enabled', True))
        self.browser_fallback_headless = bool(self.gd_config.get('browser_fallback_headless', True))
        self.browser_fallback_timeout = int(self.gd_config.get('browser_fallback_timeout', 60))

        # yt-dlp fallback settings
        self.ytdlp_fallback_enabled = bool(self.gd_config.get('ytdlp_fallback_enabled', True))
        self.ytdlp_fallback_timeout = int(self.gd_config.get('ytdlp_fallback_timeout', 120))
        
        self.cookies_file = self.gd_config.get('cookies_file') or config.get('cookies_file')
        self.cookies_browser = self.gd_config.get('cookies_browser') or config.get('cookies_browser')
        if self.cookies_file and not Path(self.cookies_file).exists():
            logger.warning(f"Configured cookies file not found: {self.cookies_file}")

        # Tracker
        tracker_path = self.gd_config.get('tracker_db') or self.gd_config.get('tracker_file') or config.get('tracker_db')
        json_backup = self.gd_config.get('tracker_json_backup') or self.gd_config.get('tracker_file')
        tracker_required = bool(self.gd_config.get('tracker_required', True))
        if not tracker_path:
            tracker_path = 'data/tiktok_toolkit.db'
        if not str(tracker_path).endswith('.sqlite') and not str(tracker_path).endswith('.db'):
            json_backup = tracker_path
            tracker_path = 'data/tiktok_toolkit.db'
        try:
            compute_hash = bool(self.gd_config.get('tracker_hash', False))
            hash_algo = self.gd_config.get('tracker_hash_algorithm', 'sha256')
            self.tracker = create_tracker(
                Path(tracker_path),
                Path(json_backup) if json_backup else Path('configs/download_tracker.json'),
                compute_hash=compute_hash,
                hash_algorithm=hash_algo
            )
            logger.debug(f"Download tracker (sqlite) initialized at {tracker_path} (backup: {json_backup})")
        except Exception as e:
            if tracker_required:
                raise ProviderError(f"Failed to initialize download tracker: {e}") from e
            self.tracker = None
            logger.warning(f"Tracker disabled because initialization failed and tracker_required=false: {e}")

        # Disable browser automation for older versions (<1.31)
        try:
            major_minor = self.version.split('.')[:2]
            ver_tuple = tuple(int(p) for p in major_minor)
            if len(ver_tuple) == 2 and (ver_tuple[0] == 1 and ver_tuple[1] < 31):
                if self.browser_enabled:
                    logger.info(f"Disabling browser automation (unsupported in gallery-dl {self.version}).")
                self.browser_enabled = False
        except Exception:
            pass

        # Detect support for --list-urls (introduced in later gallery-dl versions)
        # Test with actual command instead of just checking help text
        self.supports_list_urls = False
        try:
            # Try to use --list-urls with a test URL to see if it's actually supported
            test_result = subprocess.run(
                ['gallery-dl', '--list-urls', '--range', '1', 'https://www.tiktok.com/@tiktok'],
                capture_output=True,
                text=True,
                timeout=10
            )
            # If command succeeds or fails with extraction error (not argument error), feature is supported
            if test_result.returncode == 0 or 'unrecognized arguments' not in test_result.stderr:
                self.supports_list_urls = True
                logger.debug(f'gallery-dl {self.version} supports --list-urls')
            else:
                logger.debug(f'gallery-dl {self.version} does not support --list-urls (will use --simulate fallback)')
        except subprocess.TimeoutExpired:
            # Timeout might mean it's working but slow, assume supported
            self.supports_list_urls = True
            logger.debug('gallery-dl --list-urls test timed out (assuming supported)')
        except Exception as e:
            logger.debug(f'Could not determine --list-urls support: {e} (assuming not supported)')
            self.supports_list_urls = False
    
    def _check_gallery_dl_installation(self) -> str:
        """Check if gallery-dl is installed and return its version string."""
        try:
            result = subprocess.run(['gallery-dl', '--version'],
                                    capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                version = result.stdout.strip()
                logger.info(f"Found gallery-dl: {version}")
                return version
            else:
                raise ProviderError("Gallery-dl is not working properly")
        except FileNotFoundError:
            raise ProviderError(
                "Gallery-dl is not installed. Please install it with: pip install gallery-dl"
            )
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"Gallery-dl check failed: {e}")

    def _get_gallery_dl_version(self) -> str:
        """Get gallery-dl version string (uses cached self.version when available)."""
        return getattr(self, 'version', 'unknown')
    
    def check_cookies_validity(self, test_url: str = "https://www.tiktok.com/@tiktok") -> Dict[str, Any]:
        """Check if cookies are valid and working."""
        result = {
            'cookies_file_exists': False,
            'cookies_file_path': None,
            'cookies_valid': False,
            'can_access_content': False,
            'urls_found': 0,
            'error': None
        }
        
        # Check if cookies file exists
        if self.cookies_file:
            cookies_path = Path(self.cookies_file)
            if cookies_path.exists():
                result['cookies_file_exists'] = True
                result['cookies_file_path'] = str(cookies_path)
                
                # Check file size
                if cookies_path.stat().st_size == 0:
                    result['error'] = "Cookies file is empty"
                    return result
                
                result['cookies_valid'] = True
        else:
            result['error'] = "No cookies file configured"
            return result
        
        # Test with gallery-dl (use --list-urls if supported, else --simulate)
        try:
            # Build base args — include the gallery-dl.json config so that extractor
            # settings (filename template, directory layout, etc.) are consistent with
            # the rest of the toolkit.  The "cookies" key has been removed from that
            # file, so passing --config here no longer creates a duplicate-cookie
            # conflict with the --cookies flag below.
            gdl_json = Path('configs') / 'gallery-dl.json'
            base_args = ['gallery-dl']
            if gdl_json.exists():
                base_args.extend(['--config', str(gdl_json.resolve())])
            base_args.extend(['--cookies', str(self.cookies_file)])

            if self.supports_list_urls:
                test_args = base_args + ['--list-urls', '--range', '1-3', test_url]
            else:
                test_args = base_args + ['--simulate', '--range', '1-3', test_url]
            
            test_result = subprocess.run(
                test_args,
                capture_output=True,
                text=True,
                timeout=45
            )
            
            if test_result.returncode == 0:
                urls = [line.strip() for line in test_result.stdout.split('\n') 
                       if line.strip() and 'tiktok.com' in line]
                result['can_access_content'] = True
                result['urls_found'] = len(urls)
            else:
                result['error'] = f"Gallery-dl failed: {test_result.stderr}"
                
        except subprocess.TimeoutExpired:
            result['error'] = "Cookie test timed out"
        except Exception as e:
            result['error'] = f"Cookie test failed: {e}"
        
        return result
    
    def _build_gallery_dl_args(self, url: str, target_dir: Path, limit: Optional[int] = None, use_cookies: bool = True, download_type: str = 'videos') -> List[str]:
        """Build gallery-dl command arguments."""
        # Always use absolute path for destination to avoid nesting like downloads/username/ downloads/username/...
        abs_target = target_dir.resolve()
        args = [
            'gallery-dl',
            '--dest', str(abs_target),
            '--retries', str(self.retries),
        ]

        # Handle different download types
        if download_type == 'profile_pictures':
            args.extend(['--filter', "extension in ('jpg', 'jpeg', 'png', 'webp')"])
        elif download_type == 'videos':
            args.extend(['--filter', "extension in ('mp4', 'mov', 'avi', 'mkv', 'webm')"])

        # Load gallery-dl.json config if it exists (provides TikTok-specific
        # extractor settings like directory layout, filename template, etc.)
        gdl_json = Path('configs') / 'gallery-dl.json'
        if gdl_json.exists():
            args.extend(['--config', str(gdl_json.resolve())])

        # Verbose output when toolkit log level is DEBUG
        if logger.isEnabledFor(logging.DEBUG):
            args.append('--verbose')

        # Add sleep if specified
        if self.sleep and self.sleep > 0:
            args.extend(['--sleep', str(self.sleep)])
        
        # Add user agent
        if self.user_agent:
            args.extend(['--user-agent', self.user_agent])
        
        # Add range limit if specified
        if limit and limit > 0:
            args.extend(['--range', f'1-{limit}'])
        
        # Add proxy if specified
        if self.proxy:
            args.extend(['--proxy', self.proxy])
        
        # Duplicate detection: gallery-dl handles file-level skips internally,
        # and the SQLite tracker provides a second dedup layer at the video-id level.
        
        # Cookie settings - only add if use_cookies is True
        if use_cookies:
            if self.cookies_file and Path(self.cookies_file).exists():
                args.extend(['--cookies', str(self.cookies_file)])
            elif self.cookies_browser:
                args.extend(['--cookies-from-browser', self.cookies_browser])
        
        args.append(url)
        return args

    # ---------------- Tracker-aware helpers ---------------- #
    def _list_user_video_urls(self, username: str, max_expected: int) -> List[str]:
        """List video URLs for a user using gallery-dl metadata mode.

        Returns up to max_expected URLs (order as provided by gallery-dl).
        Only implemented when --list-urls is supported; otherwise returns [].
        """
        if not self.supports_list_urls:
            return []
        user_url = f"https://www.tiktok.com/@{username}"
        args = ['gallery-dl']
        if self.cookies_file and Path(self.cookies_file).exists():
            args.extend(['--cookies', str(self.cookies_file)])
        elif self.cookies_browser:
            args.extend(['--cookies-from-browser', self.cookies_browser])
        args.extend(['--list-urls', user_url])

        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=60)
            if result.returncode != 0:
                logger.debug(f"list-urls pre-check failed (code {result.returncode}): {result.stderr[:200]}")
                return []
            urls = [ln.strip() for ln in result.stdout.split('\n') if ln.strip() and 'tiktok.com' in ln]
            return urls[:max_expected]
        except Exception as e:
            logger.debug(f"list-urls pre-check exception: {e}")
            return []

    def _extract_video_id_from_url(self, url: str) -> Optional[str]:
        match = re.search(r'(\d{6,})', url)
        return match.group(1) if match else None

    def _tracker_precheck(self, username: str, desired_limit: int, target_dir: Path) -> Optional[int]:
        """Determine how many *new* videos we should attempt to download.
        
        Uses tracker database to check which videos are already downloaded,
        avoiding unnecessary API calls and downloads.

        Returns:
          - int: number of new videos desired (may be 0) if tracker usable
          - None: if pre-check not possible (fall back to original limit)
        """
        if not getattr(self, 'tracker', None):
            return None
        
        # Scan more videos than requested to account for already-downloaded ones
        # Formula: max(desired_limit, min(desired_limit * 3, desired_limit + 50))
        # - Multiply by 3: Assumes ~66% of recent videos might already be downloaded
        # - Add 50: Ensures we scan at least 50 extra videos for small limits
        # - Max with desired_limit: Ensures we always scan at least the requested amount
        scan_count = max(desired_limit, min(desired_limit * 3, desired_limit + 50))
        
        urls = self._list_user_video_urls(username, scan_count)
        if not urls:
            return None
        new_ids = []
        for u in urls:
            vid = self._extract_video_id_from_url(u)
            if not vid:
                continue
            # Check if downloaded AND in this specific target directory
            if not self.tracker.is_downloaded_in_folder(username, vid, str(target_dir)):
                new_ids.append(vid)
            if len(new_ids) >= desired_limit:
                break
        logger.debug(f"Tracker pre-check for @{username} in {target_dir}: desired {desired_limit}, new {len(new_ids)}, scanned {len(urls)}")
        return len(new_ids)

    def _is_media_file(self, path: Path) -> bool:
        return path.is_file() and path.suffix.lower() in ['.mp4', '.mov', '.avi', '.webm', '.mkv', '.jpg', '.jpeg', '.png', '.webp']

    def _collect_media_files(self, target_dir: Path) -> List[Path]:
        if not target_dir.exists():
            return []
        return [path for path in target_dir.rglob('*') if self._is_media_file(path)]

    def _normalize_downloaded_files(self, target_dir: Path, downloaded_files: List[Path], username: str = None) -> List[Path]:
        normalized_files: List[Path] = []
        seen_paths = set()
        seen_destinations = set()

        for file_path in downloaded_files:
            if not self._is_media_file(file_path):
                continue

            source_path = file_path.resolve()
            if str(source_path) in seen_paths:
                continue
            seen_paths.add(str(source_path))

            # Handle profile pictures (images)
            if source_path.suffix.lower() in ['.jpg', '.jpeg', '.png', '.webp']:
                # Save to root with download date
                from .utils import build_profile_pic_filename
                
                pic_filename = build_profile_pic_filename(username or 'profile', source_path.suffix.lower().lstrip('.'))
                destination = target_dir / pic_filename
                
                if str(destination) in seen_destinations:
                    continue
                seen_destinations.add(str(destination))

                try:
                    if source_path != destination:
                        source_path.replace(destination)
                    logger.info(f"Profile picture: {destination.name}")
                    normalized_files.append(destination)
                except Exception as e:
                    logger.error(f"Failed to move profile picture: {e}")
                continue

            # Handle video files
            video_id = extract_video_id_from_filename(source_path.name)
            if not video_id:
                # Fallback to stem if no ID found
                video_id = source_path.stem
            
            # Try to use today's date as post date (would be improved with actual metadata)
            post_date = datetime.now(timezone.utc).strftime('%Y-%m-%d')
            
            destination = build_output_path(target_dir, video_id, source_path.suffix.lstrip('.'), 
                                          existing_path=source_path, username=username, post_date=post_date)

            if str(destination) in seen_destinations:
                continue
            seen_destinations.add(str(destination))

            if source_path.parent == destination.parent and source_path.name == destination.name:
                normalized_files.append(source_path)
                continue

            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                if source_path != destination:
                    if destination.exists():
                        destination.unlink()
                    source_path.replace(destination)
                logger.debug(f"Video: {destination.name}")
                normalized_files.append(destination)
            except Exception as e:
                logger.error(f"Failed to move video {source_path}: {e}")

        # Final pass to deduplicate
        final_files = []
        for nf in normalized_files:
            if str(nf) not in [str(f) for f in final_files]:
                final_files.append(nf)
                
        return final_files

    def _run_gallery_dl(self, url: str, target_dir: Path, limit: Optional[int] = None, download_type: str = 'videos') -> tuple[List[Path], int]:
        """Run gallery-dl command and return (downloaded files, skipped count)."""
        # Strategy: Try WITH cookies first if available (since you just added them).
        # Only fall back to no-cookies for public content if that fails.
        
        # Determine if we have cookies available
        has_cookies = bool((self.cookies_file and Path(self.cookies_file).exists()) or self.cookies_browser)
        
        # Start WITH cookies if available, otherwise without
        use_cookies = has_cookies
        
        # Exponential backoff parameters for retries
        # Formula: delay = base_delay * (2 ** attempt)
        # Attempt 0: 2.0 * (2^0) = 2.0 seconds
        # Attempt 1: 2.0 * (2^1) = 4.0 seconds
        # Attempt 2: 2.0 * (2^2) = 8.0 seconds
        # This prevents overwhelming the server while giving transient errors time to resolve
        max_retries = self.retries
        base_delay = 2.0  # Initial delay in seconds
        
        for attempt in range(max_retries + 1):
            args = self._build_gallery_dl_args(url, target_dir, limit, use_cookies=use_cookies, download_type=download_type)

            logger.info(f"Downloading {download_type} (Attempt {attempt+1}/{max_retries+1}, Cookies: {use_cookies}): {url}")

            try:
                # Run gallery-dl command
                result = subprocess.run(
                    args,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,  # Timeout in seconds
                )
                
                logger.debug(f"Gallery-dl return code: {result.returncode}")
                
                # Parse output for actual results (only show on debug or error)
                if result.stderr and logger.isEnabledFor(logging.DEBUG):
                    logger.debug(f"Gallery-dl STDERR: {result.stderr[:500]}")
                
                # Check for various failure conditions
                stdout_lower = result.stdout.lower() if result.stdout else ""
                stderr_lower = result.stderr.lower() if result.stderr else ""
                
                needs_cookie_fallback = False
                needs_retry = False
                error_msg = result.stderr.strip() or result.stdout.strip()
                
                if result.returncode != 0:
                    logger.debug(f"Gallery-dl failed (code {result.returncode})")
                    
                    if "unrecognized arguments" in error_msg:
                        raise ProviderError(f"Gallery-dl version incompatibility. Please update: pip install --upgrade gallery-dl")
                    elif "429" in error_msg or "rate limit" in error_msg.lower():
                        logger.info("Rate-limited (429). Retrying...")
                        needs_retry = True
                    elif "403" in error_msg or "forbidden" in error_msg.lower():
                        if use_cookies:
                            # 403 on individual videos is common — only abort if it's
                            # a profile-level block (no items retrieved at all)
                            if 'retrieving creator/item_list' in result.stderr.lower() or 'retrieving creator/item_list' in result.stdout.lower():
                                # Got some items but 403'd on downloads — treat as anti-bot
                                raise ProviderError(error_msg)
                            else:
                                logger.error("Access denied (403) even with cookies. Private account or cookie issue.")
                                raise ProviderError(f"403 Forbidden: {error_msg}")
                        else:
                            logger.info("Access denied (403). Retrying with cookies...")
                            needs_cookie_fallback = True
                    elif "404" in error_msg or "not found" in error_msg.lower():
                        raise ProviderError(f"Content not found (404). Verify username/URL.")
                    elif "no videos found" in error_msg.lower() or "no entries" in error_msg.lower():
                        raise ProviderError(f"No {download_type} available. Account may be private or empty.")
                    elif any(p in error_msg.lower() for p in [
                        'could not extract rehydration data',
                        'extraction error',
                        'javascript challenge',
                        'solving javascript',
                    ]):
                        # Anti-bot pattern — raise immediately with original message so
                        # download_user can detect it and trigger browser fallback.
                        raise ProviderError(error_msg)
                    else:
                        needs_retry = True
                
                # Parse for successes (look for downloaded files in output)
                downloaded_files = []
                skipped_count = 0
                
                for line in result.stdout.split('\n'):
                    line_lower = line.lower()
                    stripped = line.strip()
                    
                    # Files are prefixed with "# " (comments in gallery-dl output)
                    if stripped.startswith('# '):
                        file_path = Path(stripped[2:].strip())
                        if self._is_media_file(file_path):
                            downloaded_files.append(file_path)
                    
                    # Count skipped items
                    if 'skipping' in line_lower or 'already exists' in line_lower:
                        skipped_count += 1
                
                # If no files found in output, check disk
                if not downloaded_files and result.returncode == 0:
                    found = self._collect_media_files(target_dir)
                    downloaded_files = found
                
                # Handle retries/fallbacks
                if needs_cookie_fallback and not use_cookies:
                    use_cookies = True
                    logger.info("Retrying with cookies...")
                    continue
                    
                if needs_retry and attempt < max_retries:
                    delay = base_delay * (2 ** attempt)
                    logger.info(f"Retrying in {delay}s...")
                    _interruptible_sleep(delay)
                    continue
                elif needs_retry and attempt >= max_retries:
                    raise ProviderError(f"Download failed after {max_retries} retries")
                
                # Successful result
                if result.returncode != 0 and not needs_retry and not needs_cookie_fallback:
                    raise ProviderError(f"Gallery-dl failed: {error_msg}")
                
                username = extract_username_from_url(url)
                downloaded_files = self._normalize_downloaded_files(target_dir, downloaded_files, username)
                logger.info(f"Downloaded {len(downloaded_files)} files")
                return downloaded_files, skipped_count
                
            except subprocess.TimeoutExpired:
                timeout_minutes = self.timeout_seconds / 60
                logger.error(f"Gallery-dl timeout after {timeout_minutes:.1f} minutes ({self.timeout_seconds} seconds)")
                if attempt < max_retries:
                    delay = base_delay * (2 ** attempt)
                    logger.info(f"Retrying in {delay}s...")
                    _interruptible_sleep(delay)
                    continue
                raise ProviderError(f"Download timeout after {timeout_minutes:.1f} minutes")
            except ProviderError:
                raise
            except Exception as e:
                logger.error(f"Gallery-dl execution failed: {e}")
                if attempt < max_retries:
                    delay = base_delay * (2 ** attempt)
                    logger.info(f"Retrying in {delay}s...")
                    _interruptible_sleep(delay)
                    continue
                raise ProviderError(f"Gallery-dl execution failed: {e}")
                
        return [], 0
    
    def _create_result_from_path(self, file_path: Path, url: str) -> DownloadResult:
        """Create DownloadResult from file path."""
        if file_path.exists():
            # Extract video ID from filename using improved extraction
            video_id = extract_video_id_from_filename(file_path.name)
            if not video_id:
                # Fallback to stem if no ID found
                video_id = file_path.stem
            
            return DownloadResult(
                ok=True,
                url=url,
                status='downloaded',
                filepath=file_path,
                meta={'video_id': video_id}
            )
        else:
            return DownloadResult(
                ok=False,
                url=url,
                status='failed',
                filepath=None,
                reason=f"File not found: {file_path}"
            )
    
    def download_user(self, username: str, limit: int, out_dir: Path, download_type: str = 'videos') -> List[DownloadResult]:
        """Download content from a user profile with automatic browser fallback.
        
        This method first attempts to use gallery-dl (fast). If anti-bot protection
        is detected, it automatically falls back to browser automation (slower but reliable).
        
        Args:
            username: The TikTok username
            limit: Maximum number of items to download
            out_dir: Output directory
            download_type: Type of content to download ('videos' or 'profile_pictures')
        """
        try:
            user_url = f"https://www.tiktok.com/@{username}"
            target_dir = create_folder_structure(out_dir, 'username', username)
            
            logger.info(f"Attempting to download {limit} {download_type} from user @{username}")
            logger.debug(f"User URL: {user_url}")
            logger.debug(f"Target directory: {target_dir}")

            if resilience.is_shutdown():
                logger.info(f"Shutdown requested before starting @{username}")
                return [DownloadResult(
                    ok=False,
                    url=user_url,
                    status='failed',
                    filepath=None,
                    reason='Shutdown requested'
                )]
            
            adjusted_limit = limit
            if download_type == 'videos':
                precheck_new_count = self._tracker_precheck(username, limit, target_dir)
                if precheck_new_count is not None:
                    if precheck_new_count == 0:
                        logger.info(f"All recent videos for @{username} already tracked in {target_dir}; skipping download")
                        return [DownloadResult(
                            ok=True,
                            url=user_url,
                            status='skipped',
                            filepath=None,
                            reason=f"All {self.tracker.count_for_user(username)} known videos already downloaded in this folder (tracker)"
                        )]
                    adjusted_limit = precheck_new_count
                    logger.debug(f"Adjusted download limit for @{username}: {adjusted_limit} (was {limit})")
            elif download_type == 'profile_pictures':
                # Force limit to 1 for profile pictures to save time
                adjusted_limit = 1

            # Try gallery-dl first (fast path)
            try:
                downloaded_files, skipped_count = self._run_gallery_dl(user_url, target_dir, adjusted_limit, download_type=download_type)
            except ProviderError as e:
                error_msg = str(e).lower()
                
                # Check if anti-bot protection detected OR timeout occurred
                # Timeouts often indicate anti-bot challenges that gallery-dl can't solve
                if any(pattern in error_msg for pattern in [
                    'could not extract rehydration data',
                    'extraction error',
                    'user account could not be found',
                    'javascript challenge',
                    '403',
                    'forbidden',
                ]):  # Note: timeouts are handled separately — they retry, not browser fallback
                    logger.warning(f"Anti-bot protection or timeout detected for @{username}: {e}")
                    logger.info("Attempting yt-dlp fallback...")

                    if resilience.is_shutdown():
                        logger.info(f"Shutdown requested before fallback for @{username}")
                        return [DownloadResult(
                            ok=False,
                            url=user_url,
                            status='failed',
                            filepath=None,
                            reason='Shutdown requested'
                        )]

                    # Try yt-dlp/curl-cffi first (faster, handles TLS fingerprinting)
                    if self.ytdlp_fallback_enabled and download_type == 'videos':
                        ytdlp_results = self._download_with_ytdlp_fallback(username, limit, target_dir)
                        if any(r.ok for r in ytdlp_results):
                            return ytdlp_results
                        logger.warning("yt-dlp fallback also failed; trying Playwright browser automation...")

                    if resilience.is_shutdown():
                        logger.info(f"Shutdown requested before browser fallback for @{username}")
                        return [DownloadResult(
                            ok=False,
                            url=user_url,
                            status='failed',
                            filepath=None,
                            reason='Shutdown requested'
                        )]

                    # Last resort: Playwright browser automation
                    logger.info("Attempting browser automation fallback...")
                    return self._download_with_browser_fallback(username, limit, target_dir, download_type)
                else:
                    # Different error, propagate
                    raise
            
            results = []
            for file_path in downloaded_files:
                dr = self._create_result_from_path(file_path, user_url)
                results.append(dr)
                # Only track videos in the database, not profile pictures
                if dr.ok and getattr(self, 'tracker', None) and dr.meta and 'video_id' in dr.meta and dr.filepath and download_type == 'videos':
                    try:
                        size = dr.filepath.stat().st_size if dr.filepath.exists() else None
                        self.tracker.mark_downloaded(username, dr.meta['video_id'], str(dr.filepath), size)
                    except Exception as e_track:
                        logger.debug(f"Failed to record tracker entry for {dr.filepath}: {e_track}")
            
            # Enhanced failure/skip analysis
            if not results:
                if skipped_count > 0:
                    logger.info(f"No new {download_type} downloaded for @{username} ({skipped_count} skipped by gallery-dl)")
                    results.append(DownloadResult(
                        ok=True,
                        url=user_url,
                        status='skipped',
                        filepath=None,
                        reason=f"No new {download_type} found; {skipped_count} items already exist in output directory."
                    ))
                else:
                    logger.warning(f"No {download_type} downloaded for user @{username}")
                    
                    # Check if target directory has any files (maybe from previous runs)
                    existing_files = []
                    if target_dir.exists():
                        extensions = ['.mp4', '.mov', '.avi', '.mkv'] if download_type == 'videos' else ['.jpg', '.jpeg', '.png', '.webp']
                        for file in target_dir.rglob('*'):
                            if file.is_file() and file.suffix.lower() in extensions:
                                existing_files.append(file)
                    
                    if existing_files:
                        logger.info(f"Found {len(existing_files)} existing files in directory")
                        results.append(DownloadResult(
                            ok=True,
                            url=user_url,
                            status='skipped',
                            filepath=None,
                            reason=f"No new {download_type} downloaded (found {len(existing_files)} existing files)."
                        ))
                    else:
                        logger.warning(f"No files found in target directory - may need browser fallback")
                        if resilience.is_shutdown():
                            logger.info(f"Shutdown requested instead of browser fallback for @{username}")
                            return [DownloadResult(
                                ok=False,
                                url=user_url,
                                status='failed',
                                filepath=None,
                                reason='Shutdown requested'
                            )]
                        # Try browser fallback as last resort
                        logger.info("Attempting browser automation fallback...")
                        return self._download_with_browser_fallback(username, limit, target_dir, download_type)
            else:
                logger.info(f"Successfully downloaded {len(results)} {download_type} from user @{username}")
            
            # Clean up any empty directories left behind by gallery-dl
            if target_dir.exists():
                try:
                    remove_empty_dirs(target_dir)
                except Exception as e_clean:
                    logger.debug(f"Failed to clean up empty directories in {target_dir}: {e_clean}")
            
            return results
            
        except Exception as e:
            logger.error(f"Failed to download from user @{username}: {e}")
            return [DownloadResult(
                ok=False,
                url=f"https://www.tiktok.com/@{username}",
                status='failed',
                filepath=None,
                reason=str(e)
            )]
    
    def _download_with_ytdlp_fallback(self, username: str, limit: int, target_dir: Path) -> List[DownloadResult]:
        """Attempt download via yt-dlp/curl-cffi before falling back to Playwright.

        Args:
            username: TikTok username
            limit: Maximum videos to download
            target_dir: Output directory

        Returns:
            List of DownloadResult objects. Caller checks if any are ok.
        """
        from .ytdlp_downloader import YtDlpDownloader

        if resilience.is_shutdown():
            return [DownloadResult(
                ok=False,
                url=f"https://www.tiktok.com/@{username}",
                status='failed',
                filepath=None,
                reason='Shutdown requested'
            )]

        if not hasattr(self, '_ytdlp_downloader'):
            cookies_file = Path(self.cookies_file) if self.cookies_file else None
            self._ytdlp_downloader = YtDlpDownloader(
                cookies_file=cookies_file,
                timeout=self.ytdlp_fallback_timeout
            )

        if not self._ytdlp_downloader.is_available():
            logger.warning("yt-dlp/curl-cffi not available — skipping yt-dlp fallback")
            return [DownloadResult(
                ok=False,
                url=f"https://www.tiktok.com/@{username}",
                status='failed',
                filepath=None,
                reason='yt-dlp not available (install with: pip install yt-dlp curl-cffi)'
            )]

        try:
            results = self._ytdlp_downloader.download_user_videos(username, limit, target_dir)

            # Track successful downloads
            if getattr(self, 'tracker', None):
                for result in results:
                    if result.ok and result.meta and 'video_id' in result.meta and result.filepath:
                        try:
                            size = result.meta.get('size') or result.filepath.stat().st_size
                            self.tracker.mark_downloaded(
                                username, result.meta['video_id'],
                                str(result.filepath), size, source='ytdlp'
                            )
                        except Exception as e_track:
                            logger.debug(f"Failed to track yt-dlp download: {e_track}")

            return results

        except Exception as e:
            logger.error(f"yt-dlp fallback error for @{username}: {e}")
            return [DownloadResult(
                ok=False,
                url=f"https://www.tiktok.com/@{username}",
                status='failed',
                filepath=None,
                reason=f'yt-dlp fallback error: {e}'
            )]

    def _download_with_browser_fallback(self, username: str, limit: int, target_dir: Path, download_type: str) -> List[DownloadResult]:
        """Fallback to browser automation when gallery-dl fails.
        
        Args:
            username: TikTok username
            limit: Maximum items to download
            target_dir: Output directory
            download_type: Type of content ('videos' or 'profile_pictures')
            
        Returns:
            List of DownloadResult objects
        """
        if resilience.is_shutdown():
            return [DownloadResult(
                ok=False,
                url=f"https://www.tiktok.com/@{username}",
                status='failed',
                filepath=None,
                reason='Shutdown requested'
            )]

        # Check if browser fallback is enabled
        if not self.browser_fallback_enabled:
            logger.warning("Browser automation fallback is disabled in configuration")
            return [DownloadResult(
                ok=False,
                url=f"https://www.tiktok.com/@{username}",
                status='failed',
                filepath=None,
                reason="Anti-bot protection detected but browser fallback is disabled. Enable in configs/providers.yaml"
            )]
        
        try:
            from .browser_downloader import BrowserDownloader, PLAYWRIGHT_AVAILABLE
            
            if not PLAYWRIGHT_AVAILABLE:
                logger.error("Browser automation fallback unavailable: Playwright not installed")
                return [DownloadResult(
                    ok=False,
                    url=f"https://www.tiktok.com/@{username}",
                    status='failed',
                    filepath=None,
                    reason="Anti-bot protection detected but Playwright not installed. Install with: pip install playwright && playwright install chromium"
                )]
            
            # Initialize browser downloader with configured settings
            if not hasattr(self, '_browser_downloader'):
                self._browser_downloader = BrowserDownloader(
                    headless=self.browser_fallback_headless,
                    timeout=self.browser_fallback_timeout
                )
            
            # Only download videos with browser (profile pictures not supported yet)
            if download_type != 'videos':
                logger.warning(f"Browser fallback only supports videos, not {download_type}")
                return [DownloadResult(
                    ok=False,
                    url=f"https://www.tiktok.com/@{username}",
                    status='failed',
                    filepath=None,
                    reason=f"Browser fallback does not support {download_type}"
                )]
            
            # Download with browser
            cookies_file = Path(self.cookies_file) if self.cookies_file else None
            results = self._browser_downloader.download_user_with_browser(
                username=username,
                limit=limit,
                output_dir=target_dir,
                cookies_file=cookies_file
                # chrome_user_data_dir auto-detected inside download_user_with_browser
            )
            
            # Track downloaded videos
            if getattr(self, 'tracker', None):
                for result in results:
                    if result.ok and result.meta and 'video_id' in result.meta and result.filepath:
                        try:
                            size = result.meta.get('size') or result.filepath.stat().st_size
                            self.tracker.mark_downloaded(username, result.meta['video_id'], str(result.filepath), size)
                        except Exception as e:
                            logger.debug(f"Failed to track browser download: {e}")
            
            return results
            
        except ImportError as e:
            logger.error(f"Browser automation import failed: {e}")
            return [DownloadResult(
                ok=False,
                url=f"https://www.tiktok.com/@{username}",
                status='failed',
                filepath=None,
                reason="Browser automation unavailable (Playwright not installed)"
            )]
        except Exception as e:
            logger.error(f"Browser automation fallback failed: {e}")
            return [DownloadResult(
                ok=False,
                url=f"https://www.tiktok.com/@{username}",
                status='failed',
                filepath=None,
                reason=f"Browser automation error: {str(e)}"
            )]
    
    def setup_browser_cookies(self, browser_name: str = "chrome") -> Path:
        """Extract cookies from browser using gallery-dl or browser-cookie3 fallback."""
        from .cookie_manager import TikTokCookieManager
        from .utils import secure_file_permissions
        
        # Use configured cookies_file path if available, otherwise default to configs/tiktok_cookies.txt
        cookies_file_path = Path(self.cookies_file or "configs/tiktok_cookies.txt")
        self.cookies_file = str(cookies_file_path)
        
        # Ensure parent directory exists
        cookies_file_path.parent.mkdir(exist_ok=True, parents=True)
        
        # Try Method 1: gallery-dl with shorter timeout
        try:
            logger.info(f"Attempting to extract cookies from {browser_name} using gallery-dl...")
            
            # NOTE:
            # gallery-dl requires an extractor-supported TikTok URL here.
            # The TikTok homepage (https://www.tiktok.com/) is rejected as
            # "Unsupported URL" in newer versions, which breaks cookie export.
            probe_url = "https://www.tiktok.com/@tiktok"

            # Use gallery-dl to extract cookies with shorter timeout
            cmd = [
                'gallery-dl',
                '--cookies-from-browser', browser_name,
                '--cookies-export', str(cookies_file_path),
                '--no-download',
                probe_url,
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            
            if result.returncode == 0 and cookies_file_path.exists():
                logger.info(f"✓ Successfully extracted cookies from {browser_name} using gallery-dl")
                
                # Set restrictive permissions on cookie file (Unix only)
                if secure_file_permissions(cookies_file_path, mode=0o600):
                    logger.debug("Set restrictive permissions on cookie file")
                else:
                    logger.warning("Could not set restrictive permissions on cookie file")
                
                # Validate extracted cookies
                cookie_mgr = TikTokCookieManager()
                validation = cookie_mgr.validate_cookies(cookies_file_path)
                
                if validation['warnings']:
                    for warning in validation['warnings']:
                        logger.warning(f"Cookie validation: {warning}")
                
                if not validation['valid']:
                    logger.warning(
                        "Extracted cookies may be incomplete. "
                        "This could cause issues with private account access."
                    )
                else:
                    logger.info("Cookie validation: All required cookies present")
                
                return cookies_file_path
            else:
                gallery_stderr = result.stderr
                logger.warning(f"Gallery-dl cookie extraction failed: {gallery_stderr}")
                raise Exception(f"Gallery-dl method failed: {gallery_stderr}")
                
        except subprocess.TimeoutExpired:
            logger.warning("Gallery-dl cookie extraction timed out (likely anti-bot protection)")
            gallery_error = "Gallery-dl timed out"
        except Exception as e:
            logger.warning(f"Gallery-dl cookie extraction failed: {e}")
            gallery_error = str(e)
        
        # Try Method 2: rookiepy (supports Chrome App-Bound Encryption on Windows)
        try:
            logger.info(f"Attempting to extract cookies from {browser_name} using rookiepy...")
            import rookiepy

            # Map browser names to rookiepy functions
            browser_map = {
                'chrome': rookiepy.chrome,
                'chromium': rookiepy.chromium,
                'firefox': rookiepy.firefox,
                'edge': rookiepy.edge,
                'opera': rookiepy.opera,
                'brave': rookiepy.brave,
                'vivaldi': rookiepy.vivaldi,
            }

            browser_key = browser_name.lower()
            if browser_key not in browser_map:
                raise ProviderError(f"Unsupported browser: {browser_name}. Supported: {', '.join(browser_map.keys())}")

            # rookiepy returns a list of dicts; filter to tiktok.com cookies
            browser_func = browser_map[browser_key]
            cookies = browser_func(['tiktok.com'])

            # Use rookiepy's built-in Netscape converter
            netscape_content = rookiepy.to_netscape(cookies)
            cookies_file_path.write_text(netscape_content, encoding='utf-8')

            logger.info(f"✓ Successfully extracted cookies from {browser_name} using rookiepy")

            # Set restrictive permissions
            if secure_file_permissions(cookies_file_path, mode=0o600):
                logger.debug("Set restrictive permissions on cookie file")

            # Validate extracted cookies
            cookie_mgr = TikTokCookieManager()
            validation = cookie_mgr.validate_cookies(cookies_file_path)

            if validation['warnings']:
                for warning in validation['warnings']:
                    logger.warning(f"Cookie validation: {warning}")

            if not validation['valid']:
                logger.warning(
                    "Extracted cookies may be incomplete. "
                    "This could cause issues with private account access."
                )
            else:
                logger.info("Cookie validation: All required cookies present")

            return cookies_file_path

        except ImportError:
            logger.error("rookiepy not installed. Install with: pip install rookiepy")
            raise ProviderError(
                f"Cookie extraction failed. gallery-dl: {gallery_error}; rookiepy not installed.\n"
                "Install rookiepy: pip install rookiepy"
            )
        except Exception as e:
            logger.error(f"rookiepy extraction failed: {e}")
            raise ProviderError(f"Cookie extraction failed: {e}")
