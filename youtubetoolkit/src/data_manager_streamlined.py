#!/usr/bin/env python3
"""
Streamlined Database Management System for YouTube Toolkit
=========================================================
Core database operations for the YouTube video downloading toolkit.

Features:
- SQLite database for video metadata and download status tracking
- Session management for download operations
- Comprehensive video status tracking (pending, downloading, completed, failed)
- Channel-based filtering and organization
- Duplicate prevention and file path management
- Statistics and reporting functionality

This module handles all persistent data storage and retrieval for the toolkit,
providing a clean interface for video metadata and download status management.
"""
import atexit
import os
import sqlite3
import json
import time
import random
import re
from datetime import datetime
from typing import List, Dict, Tuple, Optional, Any, Callable
from urllib.parse import urlparse, parse_qs

from app_paths import DATABASE_FILE

# Import resilience utilities
try:
    from resilience import _SHUTDOWN, _interruptible_sleep
except ImportError:
    # Fallback if resilience module not available
    import threading
    _SHUTDOWN = threading.Event()
    
    def _interruptible_sleep(seconds: float, check_interval: float = 0.2) -> None:
        """Sleep in short slices so Ctrl+C is observed quickly."""
        if seconds <= 0:
            return
        end_time = time.time() + seconds
        while True:
            remaining = end_time - time.time()
            if remaining <= 0:
                return
            time.sleep(min(check_interval, remaining))


def _strip_ansi_codes(text: Optional[str]) -> Optional[str]:
    """Remove ANSI escape codes from text.
    
    Args:
        text: Text potentially containing ANSI escape codes
        
    Returns:
        Text with ANSI codes removed, or None if input was None
    """
    if not text:
        return text
    return re.sub(r'\x1b\[[0-9;]*m', '', text)


def with_exponential_backoff(max_retries: int = 5, base_delay: float = 1.0):
    """Decorator for exponential backoff on database operations"""
    def decorator(func: Callable):
        def wrapper(*args, **kwargs):
            retries = 0
            while retries < max_retries:
                try:
                    return func(*args, **kwargs)
                except sqlite3.OperationalError as e:
                    if "database is locked" in str(e).lower() or "busy" in str(e).lower():
                        retries += 1
                        if retries == max_retries:
                            raise
                        delay = base_delay * (2 ** (retries - 1)) + random.uniform(0, 0.1)
                        print(f"⚠️  Database busy, retrying in {delay:.2f}s... ({retries}/{max_retries})")
                        _interruptible_sleep(delay)
                    else:
                        raise
                except Exception:
                    raise
            return func(*args, **kwargs)
        return wrapper
    return decorator

class DatabaseManager:
    def __init__(self, db_path: str = str(DATABASE_FILE)) -> None:
        self.db_path = db_path
        self.backup_path = self.db_path + '.json'
        self.ensure_database()
        self.sync_with_backup()

        # Clean up any interrupted downloads on startup
        cleaned = self.cleanup_interrupted_downloads()
        if cleaned > 0:
            print(f"🔄 Reset {cleaned} interrupted downloads to pending")

        atexit.register(self._atexit_checkpoint)

    def _connect(self) -> sqlite3.Connection:
        """Open a connection with retry timeout and C-layer busy retry."""
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.execute('PRAGMA busy_timeout=5000')
        return conn

    def _atexit_checkpoint(self) -> None:
        """Merge WAL into main DB file on clean exit so no WAL is left open."""
        try:
            conn = sqlite3.connect(self.db_path, timeout=2.0)
            conn.execute('PRAGMA wal_checkpoint(PASSIVE)')
            conn.close()
        except Exception:
            pass
    
    def ensure_database(self) -> None:
        """Create database and tables if they don't exist (streamlined)"""
        db_dir = os.path.dirname(self.db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        
        # If DB file doesn't exist, it will be recreated by sqlite3.connect
        # We don't need to explicitly check, but we ensure tables exist
        with self._connect() as conn:
            # Enable WAL mode and foreign keys
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA foreign_keys=ON')
            
            # Create channels table first (referenced by videos)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS channels (
                    channel_id          TEXT PRIMARY KEY,
                    channel_name        TEXT NOT NULL,
                    handle              TEXT,
                    description         TEXT,
                    subscriber_count    INTEGER DEFAULT 0,
                    video_count         INTEGER DEFAULT 0,
                    profile_pic_url     TEXT,
                    profile_pic_phash   TEXT,
                    is_selected         INTEGER DEFAULT 1,
                    is_active           INTEGER DEFAULT 1,
                    status              TEXT DEFAULT 'active',
                    first_seen_ts       TEXT DEFAULT (datetime('now')),
                    last_scraped_ts     TEXT,
                    discovery_source    TEXT
                )
            """)
            
            conn.execute('CREATE INDEX IF NOT EXISTS idx_channels_selected ON channels(is_selected)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_channels_subscribers ON channels(subscriber_count)')
            
            # Create channel_snapshots table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS channel_snapshots (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id          TEXT NOT NULL,
                    subscriber_count    INTEGER DEFAULT 0,
                    video_count         INTEGER DEFAULT 0,
                    snapshot_ts         TEXT DEFAULT (datetime('now')),
                    FOREIGN KEY (channel_id) REFERENCES channels(channel_id) ON DELETE RESTRICT
                )
            """)
            
            conn.execute('CREATE INDEX IF NOT EXISTS idx_channel_snapshots ON channel_snapshots(channel_id, snapshot_ts DESC)')
            
            # Create channel_photo_history table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS channel_photo_history (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id          TEXT NOT NULL,
                    photo_url           TEXT NOT NULL,
                    photo_phash         TEXT NOT NULL,
                    photo_blob          BLOB,
                    file_path           TEXT,
                    detected_at         TEXT DEFAULT (datetime('now')),
                    FOREIGN KEY (channel_id) REFERENCES channels(channel_id) ON DELETE RESTRICT
                )
            """)
            
            conn.execute('CREATE INDEX IF NOT EXISTS idx_photo_history_channel ON channel_photo_history(channel_id, detected_at DESC)')
            
            # Create api_quota_usage table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS api_quota_usage (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    date                TEXT NOT NULL,
                    operation           TEXT NOT NULL,
                    units_used          INTEGER DEFAULT 0,
                    recorded_at         TEXT DEFAULT (datetime('now'))
                )
            """)
            
            conn.execute('CREATE INDEX IF NOT EXISTS idx_quota_date ON api_quota_usage(date)')
            
            # Create videos table (without media_type initially for backward compatibility)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS videos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT UNIQUE NOT NULL,
                    video_id TEXT,
                    title TEXT,
                    channel TEXT,
                    channel_id TEXT,
                    duration INTEGER,
                    status TEXT DEFAULT 'pending',
                    download_status TEXT DEFAULT 'pending',
                    file_path TEXT,
                    file_size INTEGER,
                    download_started TIMESTAMP,
                    download_completed TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    processed_at TIMESTAMP,
                    error_message TEXT,
                    metadata TEXT,
                    scraped_links TEXT
                )
            """)
            
            # Add media_type column if it doesn't exist (migration)
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(videos)")
            columns = [row[1] for row in cursor.fetchall()]
            
            if 'media_type' not in columns:
                conn.execute("ALTER TABLE videos ADD COLUMN media_type TEXT DEFAULT 'video'")
            
            # Create indexes
            conn.execute('CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_videos_download_status ON videos(download_status)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_videos_url ON videos(url)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_videos_video_id ON videos(video_id)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_videos_channel_id ON videos(channel_id)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_videos_media_type ON videos(media_type)')
            
            conn.commit()

    def sync_with_backup(self) -> None:
        """Sync JSON backup with database on startup"""
        if not os.path.exists(self.backup_path):
            self.create_backup()
            return

        try:
            print(f"🔄 Syncing database with backup: {self.backup_path}")
            try:
                with open(self.backup_path, 'r', encoding='utf-8') as f:
                    backup_data = json.load(f)
            except (json.JSONDecodeError, ValueError) as json_err:
                print(f"⚠️  Backup JSON is corrupted ({json_err}). Regenerating from database...")
                self.create_backup()
                print(f"✅ Backup regenerated from database.")
                return

            if not backup_data:
                return

            # Complete restore logic: Add any missing videos with full data
            with self._connect() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT url FROM videos")
                existing_urls = {row[0] for row in cursor.fetchall()}

                records_to_insert = []
                for v in backup_data:
                    url = v.get('url')
                    if url and url not in existing_urls:
                        records_to_insert.append((
                            url, v.get('video_id'), v.get('title'), v.get('channel'),
                            v.get('channel_id'), v.get('duration'), v.get('status', 'pending'),
                            v.get('download_status', 'pending'), v.get('file_path'),
                            v.get('file_size'), v.get('download_started'), v.get('download_completed'),
                            v.get('created_at'), v.get('processed_at'), v.get('error_message'),
                            v.get('metadata'), v.get('scraped_links')
                        ))

                if records_to_insert:
                    cursor.executemany("""
                        INSERT OR IGNORE INTO videos (
                            url, video_id, title, channel, channel_id, duration, status,
                            download_status, file_path, file_size, download_started, download_completed,
                            created_at, processed_at, error_message, metadata, scraped_links
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, records_to_insert)
                    print(f"✅ Restored {len(records_to_insert)} records from backup.")
            print(f"✅ Sync complete.")
        except Exception as e:
            print(f"⚠️  Sync failed: {e}")

    def create_backup(self) -> None:
        """Create a JSON backup of the videos table (atomic write to prevent corruption)"""
        tmp_path = self.backup_path + '.tmp'
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM videos")
                rows = [dict(row) for row in cursor.fetchall()]

            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(rows, f, indent=2, ensure_ascii=False)

            # Atomic replace: only swap in the new file after it is fully written
            if os.path.exists(self.backup_path):
                os.replace(tmp_path, self.backup_path)
            else:
                os.rename(tmp_path, self.backup_path)
        except Exception as e:
            print(f"⚠️  Backup failed: {e}")
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass

    def extract_video_id(self, url: str) -> Optional[str]:
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

    @with_exponential_backoff()
    def add_video(self, url: str, title: Optional[str] = None, channel: Optional[str] = None, 
                  channel_id: Optional[str] = None, duration: Optional[int] = None, metadata: Optional[Dict] = None) -> Optional[int]:
        """Add a video to the database"""
        try:
            video_id = self.extract_video_id(url)
            metadata_json = json.dumps(metadata) if metadata else None
            res_id = None
            
            with self._connect() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT OR IGNORE INTO videos 
                    (url, video_id, title, channel, channel_id, duration, metadata)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (url, video_id, title, channel, channel_id, duration, metadata_json))
                
                if cursor.rowcount > 0:
                    res_id = cursor.lastrowid
                else:
                    # Get existing video ID
                    cursor.execute("SELECT id FROM videos WHERE url = ?", (url,))
                    result = cursor.fetchone()
                    res_id = result[0] if result else None

            if res_id:
                self.create_backup()
            return res_id
        
        except Exception as e:
            print(f"❌ Error adding video: {e}")
            return None

    @with_exponential_backoff()
    def batch_add_videos(self, videos: List[Dict]) -> int:
        """Batch add multiple videos to the database efficiently"""
        if not videos:
            return 0
            
        added_count = 0
        try:
            with self._connect() as conn:
                cursor = conn.cursor()
                
                # Fetch existing URLs to avoid duplicate inserts
                urls = [v['url'] for v in videos if 'url' in v]
                if not urls: return 0
                
                # Handle large batches for SQL IN clause
                existing_urls = set()
                for i in range(0, len(urls), 999):
                    batch_urls = urls[i:i+999]
                    placeholders = ','.join('?' for _ in batch_urls)
                    cursor.execute(f"SELECT url FROM videos WHERE url IN ({placeholders})", batch_urls)
                    existing_urls.update(row[0] for row in cursor.fetchall())
                
                # Prepare data for new insertions
                records_to_insert = []
                for v in videos:
                    url = v.get('url')
                    if url and url not in existing_urls:
                        video_id = self.extract_video_id(url)
                        metadata_json = json.dumps(v.get('metadata')) if v.get('metadata') else None
                        records_to_insert.append((
                            url, 
                            video_id, 
                            v.get('title'), 
                            v.get('channel'), 
                            v.get('channel_id'), 
                            v.get('duration'), 
                            metadata_json
                        ))
                
                if records_to_insert:
                    cursor.executemany("""
                        INSERT OR IGNORE INTO videos 
                        (url, video_id, title, channel, channel_id, duration, metadata)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, records_to_insert)
                    added_count = cursor.rowcount

            if added_count > 0:
                self.create_backup()
            return added_count
            
        except Exception as e:
            print(f"❌ Error in batch adding videos: {e}")
            return 0

    @with_exponential_backoff()
    def update_video_status(self, video_id: int, status: str, 
                           error_message: Optional[str] = None, metadata: Optional[Dict] = None):
        """Update video processing status"""
        try:
            metadata_json = json.dumps(metadata) if metadata else None
            
            with self._connect() as conn:
                conn.execute("""
                    UPDATE videos 
                    SET status = ?, processed_at = CURRENT_TIMESTAMP, 
                        error_message = ?, metadata = ?
                    WHERE id = ?
                """, (status, error_message, metadata_json, video_id))
            self.create_backup()
        
        except Exception as e:
            print(f"❌ Error updating video status: {e}")

    @with_exponential_backoff()
    def update_download_status(self, url: str, status: str, file_path: Optional[str] = None, 
                              file_size: Optional[int] = None, error_message: Optional[str] = None):
        """Update video download status"""
        try:
            # Strip ANSI escape codes from error messages before storing
            if error_message:
                error_message = _strip_ansi_codes(error_message)

            claimed = None
            with self._connect() as conn:
                if status == 'downloading':
                    # Atomic claim: only succeeds if still pending (prevents double-download races)
                    cursor = conn.execute("""
                        UPDATE videos
                        SET download_status = ?, download_started = CURRENT_TIMESTAMP
                        WHERE url = ? AND download_status = 'pending'
                    """, (status, url))
                    claimed = cursor.rowcount == 1
                elif status == 'completed':
                    conn.execute("""
                        UPDATE videos
                        SET download_status = ?, download_completed = CURRENT_TIMESTAMP,
                            file_path = ?, file_size = ?, status = 'completed'
                        WHERE url = ?
                    """, (status, file_path, file_size, url))
                elif status == 'failed':
                    conn.execute("""
                        UPDATE videos
                        SET download_status = ?, error_message = ?, status = 'failed'
                        WHERE url = ?
                    """, (status, error_message, url))
                else:
                    conn.execute("""
                        UPDATE videos
                        SET download_status = ?
                        WHERE url = ?
                    """, (status, url))

            if claimed is not None:
                return claimed
            self.create_backup()

        except Exception as e:
            print(f"❌ Error updating download status: {e}")
    
    def get_video_by_url(self, url: str) -> Optional[Dict]:
        """Get video record by URL"""
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT * FROM videos WHERE url = ?
                """, (url,))
                result = cursor.fetchone()
                return dict(result) if result else None
        
        except Exception as e:
            print(f"❌ Error getting video by URL: {e}")
            return None
    
    def check_existing_download(self, url: str) -> Optional[str]:
        """Check if video is already downloaded and file exists"""
        try:
            video = self.get_video_by_url(url)
            if video and video['download_status'] == 'completed' and video['file_path']:
                if os.path.exists(video['file_path']):
                    return video['file_path']
                else:
                    # File was deleted, reset download status
                    self.update_download_status(url, 'pending')
                    return None
            return None
        
        except Exception as e:
            print(f"❌ Error checking existing download: {e}")
            return None
    
    def get_unprocessed_videos(self, limit: int = 10, days: Optional[int] = None) -> List[Tuple]:
        """Get videos that haven't been downloaded yet"""
        try:
            with self._connect() as conn:
                cursor = conn.cursor()
                if days:
                    cursor.execute("""
                        SELECT id, url, title, channel, channel_id, duration FROM videos 
                        WHERE download_status = 'pending' 
                        AND created_at >= date('now', ?)
                        ORDER BY created_at ASC 
                        LIMIT ?
                    """, (f'-{days} days', limit))
                else:
                    cursor.execute("""
                        SELECT id, url, title, channel, channel_id, duration FROM videos 
                        WHERE download_status = 'pending' 
                        ORDER BY created_at ASC 
                        LIMIT ?
                    """, (limit,))
                return cursor.fetchall()
        
        except Exception as e:
            print(f"❌ Error getting unprocessed videos: {e}")
            return []
    
    def get_unprocessed_videos_by_channels(self, channel_ids: List[str], limit: int = 10, days: Optional[int] = None) -> List[Tuple]:
        """Get videos that haven't been downloaded yet from specific channels"""
        try:
            if not channel_ids:
                return self.get_unprocessed_videos(limit, days)
                
            with self._connect() as conn:
                cursor = conn.cursor()
                placeholders = ','.join(['?' for _ in channel_ids])
                
                if days:
                    query = f"""
                        SELECT id, url, title, channel, channel_id, duration FROM videos 
                        WHERE download_status = 'pending' AND channel_id IN ({placeholders})
                        AND created_at >= date('now', ?)
                        ORDER BY created_at ASC 
                        LIMIT ?
                    """
                    params = channel_ids + [f'-{days} days', limit]
                else:
                    query = f"""
                        SELECT id, url, title, channel, channel_id, duration FROM videos 
                        WHERE download_status = 'pending' AND channel_id IN ({placeholders})
                        ORDER BY created_at ASC 
                        LIMIT ?
                    """
                    params = channel_ids + [limit]
                    
                cursor.execute(query, params)
                return cursor.fetchall()
        
        except Exception as e:
            print(f"❌ Error getting unprocessed videos by channels: {e}")
            return []
    
    @with_exponential_backoff()
    def get_pending_channels_with_photos(self, limit: int = 9999, days: Optional[int] = None) -> List[Tuple]:
        """
        Get unique channels that need profile photos downloaded.
        
        Returns ONLY ONE video per channel (the earliest one) to avoid
        downloading the same profile photo multiple times.
        
        This is critical for reducing 429 errors: instead of downloading 
        profile photos for every video (e.g., 7845 videos), we only download
        once per unique channel (e.g., 491 channels).
        
        Args:
            limit: Maximum number of channels to return
            days: Only include videos added within last N days (None = all)
            
        Returns:
            List[Tuple]: Each tuple contains (id, url, title, channel, channel_id, duration)
        """
        try:
            with self._connect() as conn:
                cursor = conn.cursor()
                
                # Group by channel and get the earliest video for each channel
                # This ensures we get exactly one representative video per channel
                if days:
                    query = """
                        SELECT MIN(id) as first_video_id, 
                               url, 
                               channel, 
                               channel_id
                        FROM videos 
                        WHERE download_status = 'pending'
                        AND channel_id IS NOT NULL
                        AND created_at >= date('now', ?)
                        GROUP BY channel_id
                        ORDER BY first_video_id ASC
                        LIMIT ?
                    """
                    params = [f'-{days} days', limit]
                else:
                    query = """
                        SELECT MIN(id) as first_video_id, 
                               url, 
                               channel, 
                               channel_id
                        FROM videos 
                        WHERE download_status = 'pending'
                        AND channel_id IS NOT NULL
                        GROUP BY channel_id
                        ORDER BY first_video_id ASC
                        LIMIT ?
                    """
                    params = [limit]
                
                cursor.execute(query, params)
                
                # Format the result to match other methods
                # Returns: (id, url, title, channel, channel_id, duration)
                results = []
                for row in cursor.fetchall():
                    # Get the full video record for the first video
                    video_id = row[0]
                    cursor2 = conn.cursor()
                    cursor2.execute("""
                        SELECT id, url, title, channel, channel_id, duration
                        FROM videos 
                        WHERE id = ?
                    """, (video_id,))
                    video_record = cursor2.fetchone()
                    if video_record:
                        results.append(video_record)
                
                return results
        
        except Exception as e:
            print(f"❌ Error getting pending channels: {e}")
            return []
    
    def get_video_statistics(self) -> Dict[str, Any]:
        """Get comprehensive statistics about videos"""
        try:
            with self._connect() as conn:
                cursor = conn.cursor()
                
                # Video counts by download status
                cursor.execute("""
                    SELECT download_status, COUNT(*) FROM videos GROUP BY download_status
                """)
                videos_by_status = dict(cursor.fetchall())
                
                # Video counts by processing status
                cursor.execute("""
                    SELECT status, COUNT(*) FROM videos GROUP BY status
                """)
                videos_by_processing_status = dict(cursor.fetchall())
                
                # Channel statistics
                cursor.execute("""
                    SELECT channel, COUNT(*) as video_count
                    FROM videos
                    WHERE channel IS NOT NULL
                    GROUP BY channel
                    ORDER BY video_count DESC
                    LIMIT 10
                """)
                top_channels = cursor.fetchall()
                
                # Recent activity
                cursor.execute("""
                    SELECT COUNT(*) FROM videos 
                    WHERE created_at > datetime('now', '-24 hours')
                """)
                recent_additions = cursor.fetchone()[0]
                
                return {
                    'videos_by_status': videos_by_status,
                    'videos_by_processing_status': videos_by_processing_status,
                    'top_channels': top_channels,
                    'recent_additions': recent_additions
                }
        
        except Exception as e:
            print(f"❌ Error getting statistics: {e}")
            return {}

    @with_exponential_backoff()
    def save_scraped_links(self, video_url: str, links: List[str]) -> None:
        """Save unique links to a single text file, one link per line"""
        if not links: return
        
        try:
            db_dir = os.path.dirname(self.db_path)
            links_file = os.path.join(db_dir, 'all_scraped_links.txt')
            
            existing_links = set()
            if os.path.exists(links_file):
                with open(links_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        cleaned = line.strip()
                        if cleaned:
                            existing_links.add(cleaned)
            
            # Add new links
            for link in links:
                existing_links.add(link.strip())
                
            # Write back sorted
            with open(links_file, 'w', encoding='utf-8') as f:
                for link in sorted(list(existing_links)):
                    f.write(f"{link}\n")
                    
        except Exception as e:
            print(f"❌ Error saving scraped links: {e}")

    def get_failed_downloads(self) -> List[Dict]:
        """Get videos that failed to download"""
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT * FROM videos 
                    WHERE download_status = 'failed'
                    ORDER BY processed_at DESC
                """)
                return [dict(row) for row in cursor.fetchall()]
        
        except Exception as e:
            print(f"❌ Error getting failed downloads: {e}")
            return []
    
    def count_videos_by_status(self, status: str) -> int:
        """Count videos by download status"""
        try:
            with self._connect() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT COUNT(*) FROM videos WHERE download_status = ?
                """, (status,))
                return cursor.fetchone()[0]
        except Exception as e:
            print(f"❌ Error counting videos: {e}")
            return 0
    
    def cleanup_interrupted_downloads(self):
        """Reset interrupted downloads to pending status"""
        try:
            with self._connect() as conn:
                cursor = conn.cursor()
                # Find downloads that were started more than 1 hour ago and still "downloading"
                cursor.execute("""
                    UPDATE videos 
                    SET download_status = 'pending', error_message = 'Reset: Interrupted download'
                    WHERE download_status = 'downloading' 
                    AND download_started < datetime('now', '-1 hour')
                """)
                affected = cursor.rowcount
                return affected
        except Exception as e:
            print(f"❌ Error cleaning up interrupted downloads: {e}")
            return 0
    
    def migrate_channels_from_videos(self):
        """One-time migration: extract unique channels from videos table."""
        try:
            with self._connect() as conn:
                cursor = conn.cursor()
                
                # Get unique channels from videos
                cursor.execute("""
                    SELECT DISTINCT channel, channel_id 
                    FROM videos 
                    WHERE channel IS NOT NULL AND channel_id IS NOT NULL
                """)
                channels = cursor.fetchall()
                
                migrated = 0
                for channel_name, channel_id in channels:
                    cursor.execute("""
                        INSERT OR IGNORE INTO channels (channel_id, channel_name, discovery_source)
                        VALUES (?, ?, 'migration')
                    """, (channel_id, channel_name))
                    if cursor.rowcount > 0:
                        migrated += 1
                
                conn.commit()
                return migrated
        except Exception as e:
            print(f"❌ Error migrating channels: {e}")
            return 0

# Create global instance
db_manager = DatabaseManager()
