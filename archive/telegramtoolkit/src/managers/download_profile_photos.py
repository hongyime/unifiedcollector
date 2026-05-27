"""
Profile Photo Downloader
Downloads all visible profile photos from users stored in SQLite.

Tracking is DB-only (profile_photo_tracking table + users summary columns).
No JSON or text-file tracking files are written.

Features:
- Round-robin load balancing across all accounts
- Downloads all available profile photos per user
- Organizes photos by user in separate folders
- Smart filename generation with user ID and timestamp
- Comprehensive error handling for missing users/photos
- Account rotation to prevent rate limiting
- Resume capability — skips already-downloaded photos via DB
- File-existence verification to detect missing files and re-queue them
- Graceful Ctrl+C with DB flush and remaining-count display
- Progress reset (full or scoped to specific user IDs)
"""
import asyncio
import hashlib
import os
import re
import signal
import sys
from datetime import datetime
from pathlib import Path
from telethon import TelegramClient
from telethon.errors import UserPrivacyRestrictedError, FloodWaitError
from src.core.parallel_processor import TelegramParallelProcessor
from src.core.dynamic_config import get_accounts, get_config_value
from src.core.state_manager import get_state_manager


class ProfilePhotoDownloader:
    def __init__(self, save_path, parallel_processor=None, verify_files=False):
        """
        DB-first profile photo downloader.

        Args:
            save_path: Directory where photos are saved.
            parallel_processor: Optional TelegramParallelProcessor instance.
            verify_files: If True, check that every DB-tracked photo still exists
                          on disk and reset missing ones so they re-download.
        """
        self.save_path = save_path
        if not self.save_path:
            raise ValueError("save_path is required")

        self.parallel_processor = parallel_processor
        self.state = get_state_manager()

        # In-memory caches (populated from DB on init)
        self.downloaded_photos: set = set()   # "<user_id>_<photo_id>" strings
        self.downloaded_hashes: set = set()   # SHA-256 hex strings

        self.error_log_file = os.path.join(self.save_path, "profile_photo_errors.log")

        # Round-robin state
        self.account_round_robin_index = 0
        self.account_stats: dict = {}
        self.account_last_used: dict = {}
        self.failed_accounts: set = set()

        # Folder index (built from os.scandir — fast)
        self.user_folder_index: dict = {}
        self.user_folder_index_ready = False
        self.folder_photo_index: dict = {}

        self._profile_file_pattern = re.compile(r"^profile_(\d+)_(\d+)_(.+)$")
        self._valid_profile_extensions = ('.jpg', '.jpeg', '.png', '.webp')

        os.makedirs(self.save_path, exist_ok=True)

        # Load tracking from DB
        self.load_downloaded_photos()
        self.downloaded_hashes = set(self.state.get_all_hashes())
        self._build_user_folder_index()

        if verify_files:
            result = self.verify_files_on_disk()
            if result['missing']:
                print(f"🔍 File check: {result['checked']} users checked, "
                      f"{result['missing']} missing → reset for re-download")

        self._setup_signal_handlers()

    @staticmethod
    def _parse_photo_identifier(photo_identifier):
        """Parse <user_id>_<photo_id> identifiers used by tracking helpers."""
        raw = str(photo_identifier or '').strip()
        if '_' not in raw:
            return None, None
        user_part, photo_part = raw.split('_', 1)
        if not user_part.isdigit() or not photo_part:
            return None, None
        return int(user_part), photo_part

    def _setup_signal_handlers(self):
        """Graceful Ctrl+C: flush DB buffers and show remaining count."""
        def graceful_shutdown(signum, frame):
            print("\n⚠️  Ctrl+C — saving state...")
            self.state.flush_all_buffers()
            remaining = self._count_remaining_users()
            print(f"✅ State saved. {remaining} users still pending.")
            print("💡 Run again to resume from where you left off.")
            sys.exit(0)

        signal.signal(signal.SIGINT, graceful_shutdown)

    def _count_remaining_users(self) -> int:
        """Count users that still need profile photos downloaded."""
        try:
            row = self.state.conn.execute(
                "SELECT COUNT(*) as c FROM users "
                "WHERE COALESCE(is_bot, 0) = 0 AND COALESCE(profile_photo_downloaded, 0) = 0"
            ).fetchone()
            return row['c'] if row else 0
        except Exception:
            return 0

    def _build_user_folder_index(self):
        self.user_folder_index = {}
        self.folder_photo_index = {}
        if not os.path.exists(self.save_path):
            self.user_folder_index_ready = True
            return
        try:
            with os.scandir(self.save_path) as entries:
                for entry in entries:
                    if not entry.is_dir():
                        continue
                    name = entry.name
                    if not name.startswith("user_"):
                        continue
                    parts = name.split('_', 2)
                    if len(parts) < 2:
                        continue
                    user_id = parts[1]
                    if user_id and user_id not in self.user_folder_index:
                        self.user_folder_index[user_id] = name
            self.user_folder_index_ready = True
        except Exception as e:
            print(f"⚠️ Error building user folder index: {e}")
            self.user_folder_index_ready = True

    def _get_photo_filename_map(self, user_folder_path):
        cached = self.folder_photo_index.get(user_folder_path)
        if cached is not None:
            return cached
        photo_map = {}
        if not os.path.exists(user_folder_path):
            self.folder_photo_index[user_folder_path] = photo_map
            return photo_map
        try:
            with os.scandir(user_folder_path) as entries:
                for entry in entries:
                    if not entry.is_file():
                        continue
                    filename = entry.name
                    if not filename.lower().endswith(self._valid_profile_extensions):
                        continue
                    if not filename.startswith("profile_"):
                        continue
                    stem, _sep, _ext = filename.rpartition('.')
                    match = self._profile_file_pattern.match(stem)
                    if not match:
                        continue
                    _user_id, photo_token, date_token = match.groups()
                    if photo_token:
                        photo_map[f"id:{photo_token}"] = filename
                    if date_token and date_token != "unknown_date":
                        photo_map[f"date:{date_token}"] = filename
        except Exception as e:
            print(f"⚠️ Error indexing photos in {user_folder_path}: {e}")
        self.folder_photo_index[user_folder_path] = photo_map
        return photo_map

    def verify_files_on_disk(self) -> dict:
        """
        Fast file-existence check using the in-memory folder index.

        For every user with profile_photo_downloaded=1, checks whether at least
        one profile_* file exists in their folder on disk.
        Missing users are reset to downloaded=0 so they re-download next run.

        Returns:
            {"checked": N, "missing": M}
        """
        checked = 0
        missing = 0
        try:
            cursor = self.state.conn.execute(
                "SELECT user_id FROM users "
                "WHERE COALESCE(is_bot, 0) = 0 AND COALESCE(profile_photo_downloaded, 0) = 1"
            )
            rows = cursor.fetchall()
        except Exception as e:
            print(f"⚠️ verify_files_on_disk: DB query failed: {e}")
            return {"checked": 0, "missing": 0}

        for row in rows:
            user_id = row['user_id']
            checked += 1
            folder_name = self.user_folder_index.get(str(user_id))
            has_file = False
            if folder_name:
                folder_path = os.path.join(self.save_path, folder_name)
                photo_map = self._get_photo_filename_map(folder_path)
                has_file = bool(photo_map)
                if not has_file:
                    try:
                        has_file = any(
                            f.lower().endswith(self._valid_profile_extensions)
                            for f in os.listdir(folder_path)
                            if f.lower().startswith("profile_")
                        )
                    except OSError:
                        pass

            if not has_file:
                missing += 1
                try:
                    self.state.conn.execute(
                        "UPDATE users SET profile_photo_downloaded = 0, "
                        "profile_photo_count = 0 WHERE user_id = ?",
                        (user_id,)
                    )
                    self.state.conn.execute(
                        "DELETE FROM profile_photo_tracking WHERE user_id = ?",
                        (user_id,)
                    )
                    self.downloaded_photos = {
                        p for p in self.downloaded_photos
                        if not p.startswith(f"{user_id}_")
                    }
                except Exception as e:
                    print(f"⚠️ verify_files_on_disk: reset failed for user {user_id}: {e}")

        if missing:
            try:
                self.state.conn.commit()
            except Exception:
                pass

        return {"checked": checked, "missing": missing}

    def reset_profile_download_progress(self, user_ids: list = None) -> int:
        """
        Reset download tracking so photos will be re-downloaded.

        Args:
            user_ids: List of int user IDs to reset. None = reset all users.

        Returns:
            Number of users reset.
        """
        try:
            if user_ids is None:
                self.state.conn.execute(
                    "UPDATE users SET profile_photo_downloaded = 0, "
                    "profile_photo_count = 0, profile_photo_last_checked = NULL "
                    "WHERE COALESCE(is_bot, 0) = 0"
                )
                self.state.conn.execute("DELETE FROM profile_photo_tracking")
                row = self.state.conn.execute("SELECT changes() as c").fetchone()
                count = row['c'] if row else 0
                self.downloaded_photos.clear()
            else:
                count = 0
                for uid in user_ids:
                    self.state.conn.execute(
                        "UPDATE users SET profile_photo_downloaded = 0, "
                        "profile_photo_count = 0, profile_photo_last_checked = NULL "
                        "WHERE user_id = ?",
                        (int(uid),)
                    )
                    self.state.conn.execute(
                        "DELETE FROM profile_photo_tracking WHERE user_id = ?",
                        (int(uid),)
                    )
                    self.downloaded_photos = {
                        p for p in self.downloaded_photos
                        if not p.startswith(f"{uid}_")
                    }
                    count += 1

            self.state.conn.commit()
            print(f"✅ Reset profile download progress for {count} user(s).")
            return count
        except Exception as e:
            print(f"❌ reset_profile_download_progress failed: {e}")
            return 0
    
    def file_hash(self, path):
        """Calculate SHA256 hash of file (chunked to avoid memory issues)"""
        try:
            hasher = hashlib.sha256()
            with open(path, 'rb') as f:
                for chunk in iter(lambda: f.read(8192), b''):
                    hasher.update(chunk)
            return hasher.hexdigest()
        except Exception as e:
            print(f"⚠️ Error calculating file hash for {path}: {e}")
            return None
    
    def save_hash(self, file_hash):
        """Save hash to DB and local cache."""
        if not file_hash:
            return
        self.downloaded_hashes.add(file_hash)
        self.state.save_hash(file_hash)
    
    def is_file_already_downloaded(self, filepath):
        """Check if file already exists and is tracked by hash"""
        if not os.path.exists(filepath):
            return False
        
        # Calculate hash and check if already downloaded
        file_hash = self.file_hash(filepath)
        if file_hash and file_hash in self.downloaded_hashes:
            print(f"⏭️ File already downloaded (hash match): {os.path.basename(filepath)}")
            return True
        
        return False
    
    def get_user_folder_name(self, user):
        """Generate safe folder name for user"""
        user_id = user['user_id']
        username = user['username']
        first_name = user['first_name']
        last_name = user['last_name']
        
        # Create display name
        if username:
            display_name = username
        elif first_name or last_name:
            display_name = f"{first_name}_{last_name}".strip('_')
        else:
            display_name = "unknown"
        
        # Clean for folder name
        safe_name = "".join(c for c in display_name if c.isalnum() or c in ('_', '-')).strip()
        if not safe_name:
            safe_name = "unknown"
        
        return f"user_{user_id}_{safe_name}"
    
    def load_downloaded_photos(self):
        """Load already-downloaded profile photo identifiers from DB."""
        try:
            self.downloaded_photos = set()
            cursor = self.state.conn.execute(
                "SELECT user_id, photo_id FROM profile_photo_tracking WHERE downloaded = 1"
            )
            for row in cursor:
                self.downloaded_photos.add(f"{row['user_id']}_{row['photo_id']}")
            if self.downloaded_photos:
                print(f"📂 Loaded {len(self.downloaded_photos)} existing profile photo records from database")
        except Exception as e:
            print(f"⚠️ Error loading profile photos from database: {e}")
            self.downloaded_photos = set()

    def save_downloaded_photo(self, photo_identifier):
        """Save a photo identifier to DB tracking and local cache."""
        normalized = str(photo_identifier or '').strip()
        if not normalized or normalized in self.downloaded_photos:
            return
        self.downloaded_photos.add(normalized)
        user_id, photo_id = self._parse_photo_identifier(normalized)
        if user_id is not None and photo_id is not None:
            self.state.save_profile_photo(user_id, photo_id, downloaded=True)

    def is_photo_already_processed(self, filepath, photo_identifier):
        """
        Check whether a photo has already been downloaded.
        Returns: (is_processed: bool, reason: str)
        """
        user_id, photo_id = self._parse_photo_identifier(photo_identifier)

        # Fast path: in-memory cache
        if photo_identifier in self.downloaded_photos:
            if os.path.exists(filepath):
                return True, "tracking cache + file exists"
            # Stale cache entry — file is gone
            self.downloaded_photos.discard(photo_identifier)

        # DB check
        if user_id is not None and photo_id is not None and self.state.is_profile_photo_downloaded(user_id, photo_id):
            self.downloaded_photos.add(photo_identifier)
            if os.path.exists(filepath):
                return True, "DB tracking + file exists"
            existing_filename = self.check_photo_exists_in_folder(os.path.dirname(filepath), photo_id)
            if existing_filename and os.path.exists(os.path.join(os.path.dirname(filepath), existing_filename)):
                return True, "DB tracking + folder match"
            return False, "DB tracking exists but file missing"

        # File exists — check hash
        if os.path.exists(filepath):
            if os.path.getsize(filepath) <= 0:
                return False, "File exists but is empty"
            file_hash = self.file_hash(filepath)
            if file_hash and file_hash in self.downloaded_hashes:
                print(f"📝 Backfilling tracking from existing file: {photo_identifier}")
                self.save_downloaded_photo(photo_identifier)
                return True, "Hash tracking + file exists"
            elif file_hash:
                self.save_hash(file_hash)
                self.save_downloaded_photo(photo_identifier)
                return True, "Existing file auto-registered"
            else:
                print(f"⚠️ Cannot calculate hash for existing file: {filepath}")
                return False, "File exists but hash calculation failed"

        return False, "Not processed"

    def log_error(self, message):
        """Log errors to file with timestamp"""
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with open(self.error_log_file, 'a', encoding='utf-8') as f:
            f.write(f"[{timestamp}] {message}\n")
    
    def load_users_from_db(self):
        """Load non-bot users from SQLite."""
        users = []
        try:
            for row in self.state.iter_users_for_profile_download() or []:
                users.append(row)
        except Exception as e:
            print(f"⚠️ Error loading users from database: {e}")
        if users:
            print(f"📊 Loaded {len(users)} non-bot users from database")
        return users

    def load_users_for_download(self):
        """Load users from DB."""
        return self.load_users_from_db()
    
    def get_next_available_account(self, accounts):
        """Get the next account using round-robin with failover"""
        if not accounts:
            return None
            
        # Filter out failed accounts
        available_accounts = [acc for acc in accounts if acc['name'] not in self.failed_accounts]
        
        if not available_accounts:
            # If all accounts failed, reset and try again
            print("⚠️ All accounts failed, resetting failed accounts list")
            self.failed_accounts.clear()
            available_accounts = accounts
        
        # Use round-robin to select next account
        account = available_accounts[self.account_round_robin_index % len(available_accounts)]
        self.account_round_robin_index = (self.account_round_robin_index + 1) % len(available_accounts)
        
        return account
    
    def update_account_stats(self, account_name, downloads_count):
        """Update statistics for an account"""
        if account_name not in self.account_stats:
            self.account_stats[account_name] = {'downloads': 0, 'errors': 0}
        
        self.account_stats[account_name]['downloads'] += downloads_count
        self.account_last_used[account_name] = datetime.now()
    
    def mark_account_failed(self, account_name):
        """Mark an account as temporarily failed"""
        self.failed_accounts.add(account_name)
        if account_name not in self.account_stats:
            self.account_stats[account_name] = {'downloads': 0, 'errors': 0}
        self.account_stats[account_name]['errors'] += 1
        
        print(f"⚠️ Account {account_name} marked as failed (will retry later)")
    
    def print_account_stats(self):
        """Print current account usage statistics"""
        if not self.account_stats:
            return
            
        print("\n📊 Account Usage Statistics:")
        print("-" * 50)
        for account_name, stats in self.account_stats.items():
            status = "❌ Failed" if account_name in self.failed_accounts else "✅ Active"
            last_used = self.account_last_used.get(account_name, "Never")
            if isinstance(last_used, datetime):
                last_used = last_used.strftime("%H:%M:%S")
            print(f"  {account_name}: {stats['downloads']} downloads, {stats['errors']} errors, Last used: {last_used} {status}")
        print("-" * 50)
    
    def find_existing_user_folder(self, user_id):
        """
        Find existing user folder by searching for folders containing the user_id
        """
        user_id_str = str(user_id)
        if not self.user_folder_index_ready:
            self._build_user_folder_index()
        cached = self.user_folder_index.get(user_id_str)
        if cached:
            return cached
        
        try:
            if not os.path.exists(self.save_path):
                return None
                
            all_folders = [f for f in os.listdir(self.save_path) 
                          if os.path.isdir(os.path.join(self.save_path, f))]
            
            # Look for folders that contain this user_id
            matching_folders = []
            for folder in all_folders:
                # Check if user_id appears in the folder name
                if f"user_{user_id_str}_" in folder or folder.endswith(f"_{user_id_str}"):
                    matching_folders.append(folder)
            
            if len(matching_folders) == 1:
                self.user_folder_index[user_id_str] = matching_folders[0]
                return matching_folders[0]
            elif len(matching_folders) > 1:
                # Multiple matches - use the first one and log warning
                print(f"⚠️ Multiple folders found for user {user_id}, using: {matching_folders[0]}")
                self.user_folder_index[user_id_str] = matching_folders[0]
                return matching_folders[0]
            else:
                return None
                
        except Exception as e:
            print(f"⚠️ Error searching for existing user folder for {user_id}: {e}")
            return None

    def check_photo_exists_in_folder(self, user_folder_path, photo_id, upload_date=None):
        """
        Check if a photo with the same photo_id already exists in the user folder
        Returns the existing filename if found, None otherwise
        """
        try:
            if not os.path.exists(user_folder_path):
                return None
            photo_id_str = str(photo_id)
            photo_map = self._get_photo_filename_map(user_folder_path)
            by_id = photo_map.get(f"id:{photo_id_str}")
            if by_id:
                return by_id

            if upload_date:
                date_key = upload_date.strftime('%Y%m%d_%H%M%S')
                by_date = photo_map.get(f"date:{date_key}")
                if by_date:
                    return by_date

            return None
            
        except Exception as e:
            print(f"⚠️ Error checking existing photos in folder: {e}")
            return None

    def get_or_create_user_folder(self, user):
        """
        Get existing user folder or create new one, ensuring proper naming
        """
        user_id = user['user_id']
        
        # First, try to find existing folder
        existing_folder = self.find_existing_user_folder(user_id)
        
        if existing_folder:
            print(f"📁 Found existing folder for user {user_id}: {existing_folder}")
            return existing_folder
        else:
            # Create new folder with current naming format
            new_folder = self.get_user_folder_name(user)
            self.user_folder_index[str(user_id)] = new_folder
            print(f"📁 Creating new folder for user {user_id}: {new_folder}")
            return new_folder
    
    def generate_photo_filename(self, user_id, photo_token, upload_date=None):
        """Generate filename for profile photo"""
        token = str(photo_token)
        if upload_date:
            date_str = upload_date.strftime('%Y%m%d_%H%M%S')
            return f"profile_{user_id}_{token}_{date_str}.jpg"
        else:
            return f"profile_{user_id}_{token}_unknown_date.jpg"
    
    async def download_user_profile_photos(self, client, user, account_name):
        """Download all profile photos for a specific user"""
        user_id = user['user_id']
        
        # Find existing folder or create new one
        user_folder = self.get_or_create_user_folder(user)
        user_dir = os.path.join(self.save_path, user_folder)
        os.makedirs(user_dir, exist_ok=True)
        
        downloaded_count = 0
        
        try:
            # Get user entity
            try:
                user_entity = await client.get_entity(user_id)
            except Exception as e:
                error_msg = f"[{account_name}] User {user_id} not found or inaccessible: {e}"
                self.log_error(error_msg)
                print(f"⚠️ {error_msg}")
                return 0
            
            # Check if user has profile photos
            if not hasattr(user_entity, 'photo') or not user_entity.photo:
                print(f"📷 [{account_name}] User {user_id} has no profile photo")
                return 0
            
            # Get all profile photos
            try:
                photos = await client.get_profile_photos(user_entity)
                
                if not photos:
                    print(f"📷 [{account_name}] User {user_id} has no accessible profile photos")
                    return 0
                
                print(f"📷 [{account_name}] Found {len(photos)} profile photos for user {user_id}")
                
                # Download each photo
                for index, photo in enumerate(photos):
                    try:
                        # Create photo identifier for tracking
                        photo_id = f"{user_id}_{photo.id}"
                        
                        # Check if photo already exists in folder (additional safety check)
                        upload_date = getattr(photo, 'date', None)
                        existing_file = self.check_photo_exists_in_folder(user_dir, photo.id, upload_date)
                        if existing_file:
                            filepath = os.path.join(user_dir, existing_file)
                            is_processed, reason = self.is_photo_already_processed(filepath, photo_id)
                            if is_processed:
                                print(f"⏭️ [{account_name}] Photo already processed: {existing_file} ({reason})")
                                continue
                            else:
                                print(f"⚠️ [{account_name}] Photo exists but not tracked: {existing_file}")
                                # File exists but not tracked - add to tracking
                                file_hash = self.file_hash(filepath)
                                if file_hash:
                                    self.save_hash(file_hash)
                                self.save_downloaded_photo(photo_id)
                                continue
                        
                        # Generate filename (tokenized by Telegram photo id for stable dedupe)
                        filename = self.generate_photo_filename(user_id, photo.id, upload_date)
                        filepath = os.path.join(user_dir, filename)
                        
                        # Robust checking using both JSON and hash tracking
                        is_processed, reason = self.is_photo_already_processed(filepath, photo_id)
                        if is_processed:
                            print(f"⏭️ [{account_name}] Already processed: {filename} ({reason})")
                            continue
                        
                        # Download photo
                        await client.download_media(photo, filepath)
                        
                        # Verify download and add to tracking systems
                        if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
                            downloaded_count += 1
                            
                            # Add to unified hash system
                            file_hash = self.file_hash(filepath)
                            if file_hash:
                                self.save_hash(file_hash)
                            
                            # Add to centralized JSON tracking
                            self.save_downloaded_photo(photo_id)
                            photo_map = self._get_photo_filename_map(user_dir)
                            photo_map[f"id:{photo.id}"] = filename
                            if upload_date:
                                photo_map[f"date:{upload_date.strftime('%Y%m%d_%H%M%S')}"] = filename
                            
                            upload_info = f" (uploaded {upload_date.strftime('%Y-%m-%d %H:%M:%S')})" if upload_date else " (upload date unknown)"
                            print(f"✅ [{account_name}] Downloaded: {filename}{upload_info}")
                        else:
                            error_msg = f"[{account_name}] Failed to download photo {index+1} for user {user_id}"
                            self.log_error(error_msg)
                            print(f"❌ {error_msg}")
                            if os.path.exists(filepath):
                                os.remove(filepath)  # Remove empty file
                        
                    except FloodWaitError as e:
                        wait_time = e.seconds
                        print(f"⏰ [{account_name}] Rate limited, waiting {wait_time} seconds...")
                        await asyncio.sleep(wait_time)
                        continue
                    except Exception as e:
                        error_msg = f"[{account_name}] Error downloading photo {index+1} for user {user_id}: {e}"
                        self.log_error(error_msg)
                        print(f"❌ {error_msg}")
                        continue
                
            except UserPrivacyRestrictedError:
                error_msg = f"[{account_name}] User {user_id} has privacy restrictions - cannot access profile photos"
                self.log_error(error_msg)
                print(f"🔒 {error_msg}")
                return 0
            except Exception as e:
                error_msg = f"[{account_name}] Error accessing profile photos for user {user_id}: {e}"
                self.log_error(error_msg)
                print(f"❌ {error_msg}")
                return 0
                
        except Exception as e:
            error_msg = f"[{account_name}] Unexpected error for user {user_id}: {e}"
            self.log_error(error_msg)
            print(f"❌ {error_msg}")
            return 0
        
        if downloaded_count > 0:
            print(f"📊 [{account_name}] Successfully downloaded {downloaded_count} photos for user {user_id}")
        
        return downloaded_count
    
    async def download_from_account_legacy(self, account, users):
        """Legacy method - Download profile photos using a specific account (kept for compatibility)"""
        print(f"\n📥 Starting profile photo download with account: {account['name']}")
        
        # Initialize client
        client = TelegramClient(account['session_file'], account['api_id'], account['api_hash'])
        await client.start(account['phone'])
        
        total_downloaded = 0
        processed_users = 0
        successful_users = 0
        
        for user in users:
            try:
                downloaded_count = await self.download_user_profile_photos(client, user, account['name'])
                total_downloaded += downloaded_count
                processed_users += 1
                
                if downloaded_count > 0:
                    successful_users += 1
                
                # Progress update every 50 users
                if processed_users % 50 == 0:
                    print(f"📊 [{account['name']}] Progress: {processed_users}/{len(users)} users processed, {total_downloaded} photos downloaded")
                
                # Small delay to avoid rate limiting
                await asyncio.sleep(0.1)
                
            except Exception as e:
                error_msg = f"[{account['name']}] Critical error processing user {user['user_id']}: {e}"
                self.log_error(error_msg)
                print(f"💥 {error_msg}")
                continue
        
        await client.disconnect()
        
        print(f"✅ [{account['name']}] Download completed:")
        print(f"   📊 Users processed: {processed_users}/{len(users)}")
        print(f"   ✅ Users with downloaded photos: {successful_users}")
        print(f"   📸 Total photos downloaded: {total_downloaded}")
        
        return total_downloaded
    
    async def download_all_profile_photos_with_account_rotation(self):
        """Download profile photos with per-user account rotation for maximum success rate"""
        print(f"🚀 Starting enhanced profile photo download with per-user account rotation")
        print(f"📁 Photos will be saved to: {os.path.abspath(self.save_path)}")

        # Load users from database
        users = self.load_users_for_download()
        if not users:
            print("❌ No users loaded from database. Run user analysis first.")
            return

        # Get accounts
        accounts = get_accounts()
        if not accounts:
            print("❌ No accounts configured. Please add accounts first.")
            return

        print(f"📊 Found {len(users)} users to process with {len(accounts)} accounts")
        print(f"📂 Hash deduplication: {len(self.downloaded_hashes)} files tracked")
        print(f"📂 Photo tracking: {len(self.downloaded_photos)} photos tracked")
        print(f"🔄 Using per-user account rotation for maximum success rate")
        
        total_downloaded = 0
        processed_users = 0
        successful_users = 0
        failed_users = []
        
        for i, user in enumerate(users, 1):
            user_id = user['user_id']
            print(f"\n[{i}/{len(users)}] Processing user {user_id}...")
            
            downloaded_count = await self.try_download_with_rotation(user, accounts)
            
            total_downloaded += downloaded_count
            processed_users += 1
            
            if downloaded_count > 0:
                successful_users += 1
                print(f"✅ Successfully downloaded {downloaded_count} photos for user {user_id}")
            else:
                failed_users.append(user_id)
                print(f"❌ Failed to download photos for user {user_id} (tried all accounts)")
            
            # Progress update every 10 users
            if processed_users % 10 == 0:
                success_rate = (successful_users / processed_users) * 100
                print(f"📊 Progress: {processed_users}/{len(users)} users processed")
                print(f"   ✅ Success rate: {success_rate:.1f}% ({successful_users}/{processed_users})")
                print(f"   📸 Total photos downloaded: {total_downloaded}")
            
            # Small delay to avoid rate limiting
            await asyncio.sleep(1)
        
        print(f"\n🎉 Enhanced profile photo download complete!")
        print(f"📊 Users processed: {processed_users}/{len(users)}")
        print(f"✅ Users with downloaded photos: {successful_users}")
        print(f"❌ Failed users: {len(failed_users)}")
        print(f"📸 Total photos downloaded: {total_downloaded}")
        print(f"📁 Photos saved to: {os.path.abspath(self.save_path)}")

        if failed_users:
            print(f"\n❌ Failed user IDs ({len(failed_users)} users):")
            for user_id in failed_users[:10]:  # Show first 10
                print(f"   {user_id}")
            if len(failed_users) > 10:
                print(f"   ... and {len(failed_users) - 10} more users")
        
        # Print final account statistics
        self.print_account_stats()
        self.state.flush_all_buffers()

        return total_downloaded

    async def try_download_with_rotation(self, user, accounts):
        """Try downloading profile photo using account rotation for a single user"""
        user_id = user['user_id']
        
        for i, account in enumerate(accounts, 1):
            try:
                print(f"🔄 [{i}/{len(accounts)}] Trying account: {account['name']} for user {user_id}")
                
                # Create client for this account
                client = TelegramClient(account['session_file'], account['api_id'], account['api_hash'])
                await client.start(account['phone'])
                
                try:
                    # Try to download profile photos
                    downloaded_count = await self.download_user_profile_photos(client, user, account['name'])
                    
                    if downloaded_count > 0:
                        print(f"✅ Downloaded {downloaded_count} photos for user {user_id} using {account['name']}")
                        await client.disconnect()
                        return downloaded_count
                    else:
                        print(f"📷 No photos available for user {user_id} via {account['name']}")
                        
                except (UserPrivacyRestrictedError, Exception) as e:
                    print(f"❌ Account {account['name']} failed for user {user_id}: {e}")
                finally:
                    await client.disconnect()
                    
            except Exception as e:
                print(f"❌ Account {account['name']} connection failed: {e}")
                continue
        
        # If all accounts failed
        print(f"❌ All {len(accounts)} accounts failed for user {user_id}")
        return 0

    async def download_all_profile_photos(self):
        """Download profile photos using round-robin account rotation"""
        print("🚀 Starting round-robin profile photo download from database")
        print(f"📁 Photos will be saved to: {os.path.abspath(self.save_path)}")
        
        # Load users from database
        users = self.load_users_for_download()
        if not users:
            print("❌ No users loaded from database. Run user analysis first.")
            return
        
        # Get accounts
        accounts = get_accounts()
        if not accounts:
            print("❌ No accounts configured. Please add accounts first.")
            return
        
        print(f"📊 Found {len(users)} users to process with {len(accounts)} accounts")
        print(f"📂 Already downloaded photos: {len(self.downloaded_photos)}")
        print(f"🔄 Using round-robin rotation across {len(accounts)} accounts")
        
        # Initialize all clients
        clients = {}
        active_accounts = []
        
        for account in accounts:
            try:
                print(f"🔗 Connecting to account: {account['name']}")
                client = TelegramClient(account['session_file'], account['api_id'], account['api_hash'])
                await client.start(account['phone'])
                clients[account['name']] = client
                active_accounts.append(account)
                print(f"✅ Connected to {account['name']}")
            except Exception as e:
                error_msg = f"Failed to connect to account {account['name']}: {e}"
                self.log_error(error_msg)
                print(f"❌ {error_msg}")
                self.mark_account_failed(account['name'])
        
        if not active_accounts:
            print("❌ No accounts could be connected. Aborting.")
            return
        
        print(f"🚀 Starting download with {len(active_accounts)} active accounts")
        
        total_downloaded = 0
        processed_users = 0
        successful_users = 0
        
        try:
            # Process users one by one using round-robin account selection
            for user in users:
                if not active_accounts:
                    print("❌ No active accounts remaining. Stopping.")
                    break
                
                # Get next account using round-robin
                account = self.get_next_available_account(active_accounts)
                if not account:
                    print("❌ No available accounts. Stopping.")
                    break
                
                client = clients.get(account['name'])
                if not client:
                    print(f"❌ Client not found for account {account['name']}")
                    self.mark_account_failed(account['name'])
                    continue
                
                try:
                    downloaded_count = await self.download_user_profile_photos(client, user, account['name'])
                    total_downloaded += downloaded_count
                    processed_users += 1
                    
                    if downloaded_count > 0:
                        successful_users += 1
                    
                    # Update account stats
                    self.update_account_stats(account['name'], downloaded_count)
                    
                    # Progress update every 20 users with account statistics
                    if processed_users % 20 == 0:
                        print(f"📊 Progress: {processed_users}/{len(users)} users processed, {total_downloaded} photos downloaded")
                        self.print_account_stats()
                    
                    # Small delay to avoid rate limiting
                    await asyncio.sleep(0.1)
                    
                except FloodWaitError as e:
                    wait_time = e.seconds
                    print(f"⏰ [{account['name']}] Rate limited, waiting {wait_time} seconds...")
                    await asyncio.sleep(wait_time)
                    # Don't mark as failed for rate limits, just continue
                    continue
                    
                except Exception as e:
                    error_msg = f"[{account['name']}] Critical error processing user {user['user_id']}: {e}"
                    self.log_error(error_msg)
                    print(f"💥 {error_msg}")
                    
                    # If this account is having issues, mark it as failed temporarily
                    if "connection" in str(e).lower() or "timeout" in str(e).lower():
                        self.mark_account_failed(account['name'])
                        # Remove from active accounts list
                        active_accounts = [acc for acc in active_accounts if acc['name'] != account['name']]
                    
                    continue
        
        finally:
            # Disconnect all clients
            for account_name, client in clients.items():
                try:
                    await client.disconnect()
                    print(f"🔌 Disconnected from {account_name}")
                except:
                    pass  # Ignore disconnect errors

            self.state.flush_all_buffers()

        print(f"\n🎉 Round-robin profile photo download complete!")
        print(f"📊 Users processed: {processed_users}/{len(users)}")
        print(f"✅ Users with downloaded photos: {successful_users}")
        print(f"📸 Total photos downloaded: {total_downloaded}")
        print(f"📁 Photos saved to: {os.path.abspath(self.save_path)}")
        print(f"📝 Error log: {os.path.abspath(self.error_log_file)}")
        print(f"📋 Downloaded photos log: {os.path.abspath(self.profile_photos_file)}")
        
        # Print final account statistics
        self.print_account_stats()

    async def download_all_profile_photos_parallel_round_robin(self):
        """Alternative: Download profile photos using parallel processing with round-robin distribution"""
        print("🚀 Starting parallel round-robin profile photo download from database")
        print(f"📁 Photos will be saved to: {os.path.abspath(self.save_path)}")
        
        # Load users from database
        users = self.load_users_for_download()
        if not users:
            print("❌ No users loaded from database. Run user analysis first.")
            return
        
        # Get accounts
        accounts = get_accounts()
        if not accounts:
            print("❌ No accounts configured. Please add accounts first.")
            return
        
        print(f"📊 Found {len(users)} users to process with {len(accounts)} accounts")
        print(f"📂 Already downloaded photos: {len(self.downloaded_photos)}")
        print(f"🔄 Using enhanced round-robin distribution across {len(accounts)} accounts")
        
        # Distribute users using round-robin but with better balancing
        user_chunks = self.distribute_users_round_robin(users, accounts)
        
        # Create parallel tasks for all accounts with semaphore
        semaphore = asyncio.Semaphore(min(3, len(accounts)))  # Limit concurrent connections
        
        async def download_with_semaphore(account, user_chunk):
            async with semaphore:
                return await self.download_from_account_chunk(account, user_chunk)
        
        # Create tasks for each account with their assigned users
        tasks = []
        for i, account in enumerate(accounts):
            user_chunk = user_chunks[i]
            if user_chunk:  # Only create task if account has users assigned
                task = download_with_semaphore(account, user_chunk)
                tasks.append((account, task))
        
        print(f"🚀 Starting {len(tasks)} parallel download tasks...")
        
        # Wait for all tasks to complete with timeout
        try:
            task_list = [task for _, task in tasks]
            results = await asyncio.wait_for(
                asyncio.gather(*task_list, return_exceptions=True),
                timeout=1800  # 30 minute timeout for all downloads
            )
        except asyncio.TimeoutError:
            print("⚠️ Download timed out after 30 minutes")
            results = [0] * len(tasks)
        
        # Process results
        total_downloaded = 0
        successful_accounts = []
        
        for i, result in enumerate(results):
            account_name = accounts[i]['name'] if i < len(accounts) else "Unknown"
            if isinstance(result, Exception):
                print(f"❌ [{account_name}] Error: {result}")
            else:
                downloaded_count = result if isinstance(result, int) else 0
                total_downloaded += downloaded_count
                if downloaded_count > 0:
                    successful_accounts.append(account_name)
                print(f"✅ [{account_name}] Completed: {downloaded_count} photos downloaded")
        
        print(f"\n🎉 Parallel round-robin profile photo download complete!")
        print(f"📊 Successfully processed {len(successful_accounts)}/{len(accounts)} accounts")
        print(f"📸 Total photos downloaded: {total_downloaded}")
        print(f"📁 Photos saved to: {os.path.abspath(self.save_path)}")
        self.state.flush_all_buffers()

    def distribute_users_round_robin(self, users, accounts):
        """Enhanced round-robin distribution with better load balancing"""
        num_accounts = len(accounts)
        user_chunks = [[] for _ in range(num_accounts)]
        
        # Distribute users in round-robin fashion
        for i, user in enumerate(users):
            account_index = i % num_accounts
            user_chunks[account_index].append(user)
        
        # Log distribution with account names
        total_distributed = 0
        for i, chunk in enumerate(user_chunks):
            if chunk:
                account_name = accounts[i]['name']
                print(f"📋 [{account_name}] Assigned {len(chunk)} users (round-robin)")
                total_distributed += len(chunk)
        
        print(f"📊 Round-robin distribution complete: {total_distributed} users across {num_accounts} accounts")
        return user_chunks

    def distribute_users_across_accounts(self, users, accounts):
        """Legacy method: Distribute users evenly across accounts for load balancing"""
        return self.distribute_users_round_robin(users, accounts)

    async def download_from_account_chunk(self, account, users):
        """Download profile photos using a specific account for assigned users"""
        if not users:
            return 0
            
        print(f"\n📥 Starting profile photo download with account: {account['name']} ({len(users)} users)")
        
        # Initialize client
        try:
            client = TelegramClient(account['session_file'], account['api_id'], account['api_hash'])
            await client.start(account['phone'])
        except Exception as e:
            error_msg = f"[{account['name']}] Failed to initialize client: {e}"
            self.log_error(error_msg)
            print(f"❌ {error_msg}")
            return 0
        
        total_downloaded = 0
        processed_users = 0
        successful_users = 0
        
        try:
            for user in users:
                try:
                    downloaded_count = await self.download_user_profile_photos(client, user, account['name'])
                    total_downloaded += downloaded_count
                    processed_users += 1
                    
                    if downloaded_count > 0:
                        successful_users += 1
                    
                    # Progress update every 25 users for better feedback
                    if processed_users % 25 == 0:
                        print(f"📊 [{account['name']}] Progress: {processed_users}/{len(users)} users processed, {total_downloaded} photos downloaded")
                    
                    # Small delay to avoid rate limiting
                    await asyncio.sleep(0.2)
                    
                except Exception as e:
                    error_msg = f"[{account['name']}] Critical error processing user {user['user_id']}: {e}"
                    self.log_error(error_msg)
                    print(f"💥 {error_msg}")
                    continue
        
        finally:
            try:
                await client.disconnect()
            except:
                pass  # Ignore disconnect errors
        
        print(f"✅ [{account['name']}] Download completed:")
        print(f"   📊 Users processed: {processed_users}/{len(users)}")
        print(f"   ✅ Users with downloaded photos: {successful_users}")
        print(f"   📸 Total photos downloaded: {total_downloaded}")
        
        return total_downloaded

    async def download_all_profile_photos_parallel(self, accounts: list):
        """Download profile photos using parallel processing across multiple accounts"""
        if not self.parallel_processor:
            print("❌ No parallel processor available, falling back to round-robin mode")
            return await self.download_all_profile_photos_parallel_round_robin()
        
        print(f"🚀 Starting parallel profile photo download with {len(accounts)} accounts")
        
        # Load users from database
        users = self.load_users_for_download()
        if not users:
            print("❌ No users found in database. Run user analysis first!")
            return
        
        print(f"👥 Found {len(users)} users to process")
        
        # Process users in parallel across accounts
        await self.parallel_processor.process_with_multiple_accounts(
            accounts=accounts,
            targets=users,
            operation_func=self.download_single_user_photos
        )

        self.state.flush_all_buffers()

        print(f"\n🎉 Parallel profile photo download complete!")
        print(f"👥 Total users processed: {len(users)}")
        print(f"📁 Photos saved to: {os.path.abspath(self.save_path)}")
        
    async def download_single_user_photos(self, client, user: dict, account_name: str) -> dict:
        """Download profile photos for a single user - designed for parallel processing"""
        try:
            user_id = user['user_id']
            
            print(f"📸 [{account_name}] Downloading photos for user: {user_id}")
            
            # Create user folder
            user_folder = self.get_user_folder_name(user)
            user_path = os.path.join(self.save_path, user_folder)
            os.makedirs(user_path, exist_ok=True)
            
            photos_downloaded = 0
            
            try:
                # Get user entity
                try:
                    user_entity = await client.get_entity(int(user_id))
                except Exception as e:
                    print(f"⚠️ [{account_name}] Could not get user entity for {user_id}: {e}")
                    return {'status': 'user_not_found', 'user_id': user_id}
                
                # Check if user has profile photos
                if not hasattr(user_entity, 'photo') or not user_entity.photo:
                    print(f"📷 [{account_name}] User {user_id} has no profile photos")
                    return {'status': 'no_photos', 'user_id': user_id}
                
                # Download all profile photos
                try:
                    async for photo in client.iter_profile_photos(user_entity):
                        try:
                            upload_date = getattr(photo, 'date', None)
                            photo_token = getattr(photo, 'id', photos_downloaded + 1)

                            # Resolve existing file in user folder by id/date before generating new path
                            existing_file = self.check_photo_exists_in_folder(user_path, photo_token, upload_date)
                            photo_identifier = f"{user_id}_{photo_token}"
                            if existing_file:
                                existing_path = os.path.join(user_path, existing_file)
                                is_processed, reason = self.is_photo_already_processed(existing_path, photo_identifier)
                                if is_processed:
                                    print(f"⏭️ [{account_name}] Already processed: {existing_file} ({reason})")
                                    continue

                            # Generate unique filename
                            photo_filename = self.generate_photo_filename(
                                user_id, 
                                photo_token,
                                upload_date
                            )
                            
                            photo_path = os.path.join(user_path, photo_filename)
                            
                            # Robust checking using both JSON and hash tracking
                            is_processed, reason = self.is_photo_already_processed(photo_path, photo_identifier)
                            if is_processed:
                                print(f"⏭️ [{account_name}] Already processed: {photo_filename} ({reason})")
                                continue
                            
                            # Download the photo
                            await client.download_profile_photo(user_entity, photo_path, download_big=True)
                            
                            # Verify download and add to tracking systems
                            if os.path.exists(photo_path) and os.path.getsize(photo_path) > 0:
                                # Add to unified hash system
                                file_hash = self.file_hash(photo_path)
                                if file_hash:
                                    self.save_hash(file_hash)
                                
                                # Add to centralized JSON tracking
                                self.save_downloaded_photo(photo_identifier)
                                photos_downloaded += 1
                                
                                print(f"✅ [{account_name}] Downloaded: {photo_filename}")
                            else:
                                error_msg = f"[{account_name}] Failed to download photo for user {user_id}: {photo_filename}"
                                self.log_error(error_msg)
                                print(f"❌ {error_msg}")
                                if os.path.exists(photo_path):
                                    os.remove(photo_path)  # Remove empty file
                            
                        except Exception as e:
                            error_msg = f"[{account_name}] Error downloading photo for user {user_id}: {e}"
                            self.log_error(error_msg)
                            print(f"❌ {error_msg}")
                            continue
                
                except Exception as e:
                    if "FloodWaitError" in str(e):
                        print(f"⏳ [{account_name}] Rate limit hit for user {user_id}")
                        return {'status': 'rate_limited', 'user_id': user_id}
                    else:
                        print(f"❌ [{account_name}] Error iterating photos for user {user_id}: {e}")
                        return {'status': 'error', 'error': str(e)}
                
                print(f"✅ [{account_name}] User {user_id}: Downloaded {photos_downloaded} photos")
                self.state.mark_profile_photo_summary(int(user_id), photos_downloaded)
                
                return {
                    'status': 'success',
                    'account': account_name,
                    'user_id': user_id,
                    'photos_downloaded': photos_downloaded
                }
                
            except UserPrivacyRestrictedError:
                print(f"🔒 [{account_name}] User {user_id} has privacy restrictions")
                return {'status': 'privacy_restricted', 'user_id': user_id}
            except Exception as e:
                error_msg = f"[{account_name}] Error processing user {user_id}: {e}"
                self.log_error(error_msg)
                print(f"❌ {error_msg}")
                return {'status': 'error', 'error': str(e)}
                
        except Exception as e:
            print(f"❌ [{account_name}] Failed to process user {user.get('user_id', 'Unknown')}: {e}")
            return {'status': 'failed', 'error': str(e)}

async def main():
    """Main function for standalone execution"""
    print("📸 Profile Photo Downloader")
    print("="*50)
    
    # Get download directory from user
    print("\n📁 Please specify where to save profile photos:")
    download_path = input("Enter download directory path: ").strip()
    
    if not download_path:
        print("❌ Error: Download directory is required!")
        return
    
    # Expand user path and make absolute
    download_path = os.path.abspath(os.path.expanduser(download_path))
    
    # Create directory if it doesn't exist
    try:
        os.makedirs(download_path, exist_ok=True)
        print(f"✅ Download directory set to: {download_path}")
    except Exception as e:
        print(f"❌ Error creating directory: {e}")
        return
    
    # Check if we have any accounts configured
    accounts = get_accounts()
    if not accounts:
        print("❌ Error: No accounts configured!")
        print("💡 Please add accounts using the Account Manager in the main menu.")
        return
    
    print(f"✅ Found {len(accounts)} configured accounts")
    
    # Ask user for parallel processing options
    print(f"\n⚙️ Parallel Processing Options ({len(accounts)} accounts available):")
    print("1. True Parallel Processing - All accounts simultaneously (fastest)")
    print("2. Enhanced Account Rotation - Try all accounts per user (highest success)")
    print("3. Balanced Parallel - Optimal speed/success balance (recommended)")
    print("4. Conservative Mode - Fewer concurrent operations (most stable)")
    
    choice = input("Choose option (1-4): ").strip()
    
    # Convert to dicts for processor
    available_accounts_list = [acc['name'] for acc in accounts]
    account_dicts = ParallelAccountManager.get_accounts_by_names(available_accounts_list)
    
    # Initialize processor
    parallel_processor = TelegramParallelProcessor()
    
    downloader = ProfilePhotoDownloader(
        download_path,
        parallel_processor=parallel_processor
    )
    
    if choice == "1":
        print("⚡ Using true parallel processing method...")
        await downloader.download_all_profile_photos_parallel(account_dicts)
    elif choice == "2":
        print("🔄 Using enhanced per-user account rotation method...")
        await downloader.download_all_profile_photos_parallel(account_dicts)
    elif choice == "4":
        print("🛡️ Using conservative parallel mode...")
        # Use fewer accounts and lower concurrency
        conservative_accounts = account_dicts[:min(3, len(account_dicts))]
        await downloader.download_all_profile_photos_parallel(conservative_accounts)
    else:
        print("⚖️ Using balanced parallel method...")
        await downloader.download_all_profile_photos_parallel(account_dicts)

if __name__ == "__main__":
    asyncio.run(main())
