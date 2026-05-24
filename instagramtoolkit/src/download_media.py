# Media downloader using Instaloader - posts, stories, highlights, and profile photos
import glob
import hashlib
import os
import sys
import time
import instaloader
from pathlib import Path
from src.account_manager import InstagramAccountManager
from src.config import (
    get_downloads_directory,
    DOWNLOAD_PAUSE_EVERY, DOWNLOAD_PAUSE_SECONDS,
    MAX_RETRIES, RETRY_BASE_DELAY, RETRY_MAX_DELAY,
)
from src.media_utils import get_profile, summarize_profile, profile_access_blocked
from src.io_utils import retry_with_backoff
from src.rate_limiter import RateLimiter
from src.resilience import _SHUTDOWN, _interruptible_sleep, with_internet_retry


def _get_db():
    import os as _os
    from db.manager import DatabaseManager
    if not hasattr(_get_db, "_instance") or _get_db._instance is None:
        _get_db._instance = DatabaseManager(_os.environ.get("DATABASE_URL", ""))
    return _get_db._instance

_get_db._instance = None


def _sha256_file(file_path: str) -> str:
    """Compute SHA-256 of a file using chunked reads (never loads full file into RAM)."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(8192)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _record_media_item(username: str, user_id: str | None, shortcode: str, media_type: str,
                       file_path: str | None, taken_at: float | None) -> None:
    """Insert a row into media_items after a successful download."""
    try:
        db = _get_db()
        file_hash = _sha256_file(file_path) if file_path and os.path.exists(file_path) else None
        file_size = os.path.getsize(file_path) if file_path and os.path.exists(file_path) else None
        db.execute(
            """INSERT OR IGNORE INTO media_items
               (username, user_id, shortcode, media_type, file_path, file_hash,
                file_size, taken_at, downloaded_at, download_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'downloaded')""",
            (username, user_id, shortcode, media_type, file_path, file_hash,
             file_size, taken_at, time.time()),
        )
    except Exception as e:
        print(f"[WARNING] Could not record media_item for {shortcode}: {e}", flush=True)

# Media file extensions we consider a successful download
_MEDIA_EXTENSIONS = ("*.jpg", "*.jpeg", "*.mp4", "*.png", "*.webp")

class MediaDownloader:
    def __init__(self, account_name=None):
        self.manager = InstagramAccountManager()
        self.loader = self.manager.get_authenticated_loader(account_name)
        
        if not self.loader:
            raise RuntimeError("Failed to authenticate account")

        self.downloads_dir = None
        self.rate = RateLimiter(label="media")

    def cleanup(self):
        """Cleanup resources"""
        if self.manager:
            self.manager.logout()

    def _get_downloads_dir(self):
        """Get downloads directory, prompting user if not set"""
        if self.downloads_dir is None:
            self.downloads_dir = get_downloads_directory()
        return self.downloads_dir

    def _setup_target_directory(self, username):
        """Setup target directory for downloads"""
        downloads_dir = self._get_downloads_dir()
        target_dir = os.path.join(downloads_dir, f"user_{username}")
        os.makedirs(target_dir, exist_ok=True)
        
        # Configure instaloader to use this directory
        self.loader.dirname_pattern = target_dir
        return target_dir

    def verify_download(self, username: str, category: str, target_dir: str) -> bool:
        """Check that at least one media file exists in target_dir after a download.

        Scans for *.jpg, *.jpeg, *.mp4, *.png, *.webp recursively under target_dir.
        Returns True if any media file is found, False otherwise.
        """
        for pattern in _MEDIA_EXTENSIONS:
            matches = glob.glob(os.path.join(target_dir, "**", pattern), recursive=True)
            if matches:
                return True
        print(f"[VERIFY] No media files found for {username}/{category} in {target_dir}")
        return False

    def _download_with_verify(self, username: str, category: str, download_fn, max_retries: int = 2):
        """Run download_fn(), then verify files exist. Retry up to max_retries times if empty.

        Returns (success: bool, downloaded_count: int).
        download_fn must return (success: bool, count: int).
        """
        target_dir = self._setup_target_directory(username)
        for attempt in range(max_retries + 1):
            success, count = download_fn(target_dir)
            if not success:
                return False, 0
            # Stories/highlights may legitimately have 0 items — treat as success
            if count == 0:
                return True, 0
            if self.verify_download(username, category, target_dir):
                return True, count
            if attempt < max_retries:
                print(f"[VERIFY] Retry {attempt + 1}/{max_retries} — re-downloading {category} for {username}")
            else:
                print(f"[VERIFY] ❌ Files missing after {max_retries} retries for {username}/{category}")
                return False, count
        return False, 0

    def download_profile_photo(self, username):
        """Download profile photo for a user, with post-download file verification."""
        if not username or username.strip() == '':
            print("❌ Invalid username provided")
            return False

        print(f"📸 Downloading profile photo for {username}")

        def _do_download(target_dir):
            try:
                profile = retry_with_backoff(
                    get_profile, self.loader, username,
                    max_retries=MAX_RETRIES,
                    base_delay=RETRY_BASE_DELAY,
                    max_delay=RETRY_MAX_DELAY,
                    label=f"pfp:{username}",
                )
                if not profile:
                    print(f"❌ Could not load profile {username}")
                    return False, 0
                result = retry_with_backoff(
                    self.loader.download_pic,
                    filename=os.path.join(target_dir, f"{username}_profile"),
                    url=profile.profile_pic_url,
                    mtime=None,
                    max_retries=2,
                    base_delay=RETRY_BASE_DELAY,
                    max_delay=RETRY_MAX_DELAY,
                    label=f"pfp_dl:{username}",
                )
                if result is None:
                    return False, 0
                return True, 1
            except instaloader.exceptions.ProfileNotExistsException:
                print(f"❌ Profile {username} does not exist")
                return False, 0
            except Exception as e:
                print(f"❌ Error downloading profile photo for {username}: {e}")
                return False, 0

        success, _ = self._download_with_verify(username, "profile_photo", _do_download)
        if success:
            print(f"✅ Profile photo downloaded for {username}")
        return success

    def download_posts(self, username, limit=None):
        """Download posts for a user, with post-download file verification."""
        if not username or username.strip() == '':
            print("❌ Invalid username provided")
            return False

        print(f"📱 Downloading posts for {username}" + (f" (limit: {limit})" if limit else ""))

        def _do_download(target_dir):
            try:
                profile = retry_with_backoff(
                    get_profile, self.loader, username,
                    max_retries=MAX_RETRIES,
                    base_delay=RETRY_BASE_DELAY,
                    max_delay=RETRY_MAX_DELAY,
                    label=f"profile:{username}",
                )
                if not profile:
                    print(f"❌ Could not load profile {username}")
                    return False, 0
                print(f"🔍 Profile Debug: {summarize_profile(profile)}")
                if profile_access_blocked(profile):
                    print(f"🔒 Profile {username} not accessible (private & not followed)")
                    return False, 0

                downloaded = 0
                failed_posts = 0
                user_id = str(getattr(profile, 'userid', '') or '')
                try:
                    for post in profile.get_posts():
                        if _SHUTDOWN.is_set():
                            print(f"[STOPPED] Shutdown requested — stopping post downloads for {username}", flush=True)
                            break
                        if limit and downloaded >= limit:
                            break
                        result = retry_with_backoff(
                            self.loader.download_post, post, username,
                            max_retries=2,
                            base_delay=RETRY_BASE_DELAY,
                            max_delay=RETRY_MAX_DELAY,
                            label=f"post:{username}",
                        )
                        if result is None:
                            failed_posts += 1
                            print(f"❌ Failed to download post after retries ({failed_posts} failures)")
                            if failed_posts >= 3:
                                print(f"[ERROR] Too many post download failures, aborting")
                                return False, downloaded
                            continue
                        downloaded += 1
                        failed_posts = 0
                        print(f"📥 Downloaded post {downloaded}" + (f"/{limit}" if limit else ""), flush=True)
                        sys.stdout.flush()
                        # Record in media_items
                        taken_at = getattr(post, 'date_utc', None)
                        taken_ts = taken_at.timestamp() if taken_at else None
                        _record_media_item(
                            username, user_id or None,
                            str(getattr(post, 'shortcode', '')),
                            'post', None, taken_ts,
                        )
                        self.rate.periodic(downloaded, every=DOWNLOAD_PAUSE_EVERY, seconds=DOWNLOAD_PAUSE_SECONDS)

                    if failed_posts > 0:
                        print(f"[WARNING] Downloaded {downloaded} posts with {failed_posts} failures for {username}")
                    else:
                        print(f"✅ Downloaded {downloaded} posts for {username}")
                    return True, downloaded

                except instaloader.exceptions.PrivateProfileNotFollowedException:
                    print(f"🔒 Cannot access posts of private profile {username}")
                    return False, 0

            except instaloader.exceptions.ProfileNotExistsException:
                print(f"❌ Profile {username} does not exist")
                return False, 0
            except Exception as e:
                print(f"❌ Error downloading posts for {username}: {e}")
                return False, 0

        success, _ = self._download_with_verify(username, "posts", _do_download)
        return success

    def download_stories(self, username):
        """Download active stories for a user, with post-download file verification."""
        if not username or username.strip() == '':
            print("❌ Invalid username provided")
            return False

        print(f"📚 Downloading stories for {username}")

        def _do_download(target_dir):
            try:
                profile = retry_with_backoff(
                    get_profile, self.loader, username,
                    max_retries=MAX_RETRIES,
                    base_delay=RETRY_BASE_DELAY,
                    max_delay=RETRY_MAX_DELAY,
                    label=f"stories:{username}",
                )
                if not profile:
                    print(f"❌ Could not load profile {username}")
                    return False, 0
                print(f"🔍 Stories Debug: {summarize_profile(profile)}")
                if profile_access_blocked(profile):
                    print(f"🔒 Profile {username} not accessible (private & not followed)")
                    return False, 0

                downloaded = 0
                try:
                    for story in self.loader.get_stories(userids=[profile.userid]):
                        for item in story.get_items():
                            result = retry_with_backoff(
                                self.loader.download_storyitem, item, username,
                                max_retries=2,
                                base_delay=RETRY_BASE_DELAY,
                                max_delay=RETRY_MAX_DELAY,
                                label=f"story:{username}",
                            )
                            if result is None:
                                print(f"❌ Skipping story item after retries")
                                continue
                            downloaded += 1
                            print(f"📥 Downloaded story item {downloaded}")

                    if downloaded == 0:
                        print(f"📝 No active stories found for {username}")
                    else:
                        print(f"✅ Downloaded {downloaded} story items for {username}")
                    return True, downloaded

                except instaloader.exceptions.PrivateProfileNotFollowedException:
                    print(f"🔒 Cannot access stories of private profile {username}")
                    return False, 0

            except instaloader.exceptions.ProfileNotExistsException:
                print(f"❌ Profile {username} does not exist")
                return False, 0
            except Exception as e:
                print(f"❌ Error downloading stories for {username}: {e}")
                return False, 0

        success, _ = self._download_with_verify(username, "stories", _do_download)
        return success

    def download_highlights(self, username):
        """Download highlight reels for a user, with post-download file verification."""
        if not username or username.strip() == '':
            print("❌ Invalid username provided")
            return False

        print(f"⭐ Downloading highlights for {username}")

        def _do_download(target_dir):
            try:
                profile = retry_with_backoff(
                    get_profile, self.loader, username,
                    max_retries=MAX_RETRIES,
                    base_delay=RETRY_BASE_DELAY,
                    max_delay=RETRY_MAX_DELAY,
                    label=f"highlights:{username}",
                )
                if not profile:
                    print(f"❌ Could not load profile {username}")
                    return False, 0
                print(f"🔍 Highlights Debug: {summarize_profile(profile)}")
                if profile_access_blocked(profile):
                    print(f"🔒 Profile {username} not accessible (private & not followed)")
                    return False, 0

                downloaded = 0
                try:
                    for highlight in self.loader.get_highlights(profile):
                        highlight_name = highlight.title or f"highlight_{highlight.unique_id}"
                        print(f"📥 Downloading highlight: {highlight_name}")
                        for item in highlight.get_items():
                            result = retry_with_backoff(
                                self.loader.download_storyitem, item, f"{username}_highlights_{highlight_name}",
                                max_retries=2,
                                base_delay=RETRY_BASE_DELAY,
                                max_delay=RETRY_MAX_DELAY,
                                label=f"highlight:{username}",
                            )
                            if result is None:
                                print(f"❌ Skipping highlight item after retries")
                                continue
                            downloaded += 1

                    if downloaded == 0:
                        print(f"📝 No highlights found for {username}")
                    else:
                        print(f"✅ Downloaded {downloaded} highlight items for {username}")
                    return True, downloaded

                except instaloader.exceptions.PrivateProfileNotFollowedException:
                    print(f"🔒 Cannot access highlights of private profile {username}")
                    return False, 0

            except instaloader.exceptions.ProfileNotExistsException:
                print(f"❌ Profile {username} does not exist")
                return False, 0
            except Exception as e:
                print(f"❌ Error downloading highlights for {username}: {e}")
                return False, 0

        success, _ = self._download_with_verify(username, "highlights", _do_download)
        return success

    def download_all(self, username, post_limit=None):
        """Download all media categories sequentially for the given username.

        Returns dict with success/partial_success keys - callers must check dict structure, not truthiness.
        
        Return structure:
        {
            'success': bool,           # True if all categories succeeded
            'partial_success': bool,   # True if some (but not all) categories succeeded
            'success_count': int,      # Number of successful categories
            'total_count': int,        # Total number of categories attempted
            'results': dict            # Per-category success/failure results
        }
        """
        print(f"🎯 Starting complete download for {username}")

        category_results = {
            'profile_photo': self.download_profile_photo(username),
            'posts': self.download_posts(username, post_limit),
            'stories': self.download_stories(username),
            'highlights': self.download_highlights(username),
        }
        success_count = sum(1 for v in category_results.values() if v)
        total_count = len(category_results)
        success = success_count == total_count
        partial_success = success_count > 0 and not success

        print(f"📊 Download summary for {username}: {success_count}/{total_count} categories successful")
        return {
            'success': success,
            'partial_success': partial_success,
            'success_count': success_count,
            'total_count': total_count,
            'results': category_results,
        }







