#!/usr/bin/env python3
"""
Unified State Manager for Telegram Toolkit
Provides SQLite-backed state management with JSON backup synchronization.

Key Features:
- SQLite as primary storage (fast, concurrent, ACID)
- JSON backup for disaster recovery
- Automatic DB recreation from backup if deleted
- Buffering and batch writes for performance
- Graceful shutdown with data flush

Usage:
    from toolkit.core.state_manager import get_state_manager
    
    state = get_state_manager()
    state.save_scan_progress('account1', 'chat123', 100, {'scanned': 50, 'links': 2})
    state.save_link('https://t.me/example', 'telegram', 'chat123', 'account1')
    state.save_hash('abc123def456...', '/path/to/file.mp4', 1024000)
"""

import atexit
import asyncio
import json
import os
import signal
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from src.core.resilience import atomic_json_write, safe_json_load
from src.core.sqlite_utils import (
    connect_sqlite,
    describe_database_lock,
    is_database_lock_error,
)


class StateManager:
    """Unified state management with SQLite primary + JSON backup sync"""
    
    _instance = None
    _lock = None
    
    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self, db_path: str = "data/users_analysis.db"):
        if hasattr(self, '_initialized') and self._initialized:
            return
            
        self.db_path = Path(db_path)
        self.json_backup_dir = Path("data/backups")
        self.json_backup_dir.mkdir(parents=True, exist_ok=True)
        
        # Buffering for batch writes
        self.scan_progress_buffer: List[Tuple] = []
        self.link_buffer: List[Tuple] = []
        self.hash_buffer: List[Tuple] = []
        self.photo_progress_buffer: List[Tuple] = []
        self.feature_progress_buffer: Dict[Tuple[str, str, str], Tuple] = {}
        self.feature_progress_update_count = 0
        self.buffer_size = 100
        
        # Per-thread connection storage — each thread gets its own sqlite3.Connection
        self._tls = threading.local()
        self._all_connections: List[sqlite3.Connection] = []
        self._conns_lock = threading.Lock()
        self._shutdown = False

        # Initialize schema on the calling (main) thread's connection
        _conn = self.conn
        self._create_tables(_conn)
        self._ensure_schema_compatibility(_conn)
        _conn.execute("UPDATE ingestion_queue SET status='pending' WHERE status='in_progress'")
        _conn.commit()

        self._initialized = True
        atexit.register(self._atexit_handler)

        # Start background JSON sync task
        self._json_sync_task = None
        self._start_json_sync_task()
    
    @property
    def conn(self) -> sqlite3.Connection:
        """Thread-local SQLite connection — each thread opens its own handle."""
        if not hasattr(self._tls, 'conn') or self._tls.conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            c = connect_sqlite(
                self.db_path,
                timeout=30.0,
                check_same_thread=True,
                wal=True,
                cache_size=10000,
                wal_autocheckpoint=1000,
            )
            self._tls.conn = c
            with self._conns_lock:
                self._all_connections.append(c)
        return self._tls.conn

    def _atexit_handler(self) -> None:
        """Flush all buffers and checkpoint WAL on any process exit."""
        if self._shutdown:
            return
        self._shutdown = True
        try:
            self.flush_all_buffers()
        except Exception:
            pass
        with self._conns_lock:
            for c in list(self._all_connections):
                try:
                    c.execute("PRAGMA wal_checkpoint(PASSIVE)")
                    c.close()
                except Exception:
                    pass
            self._all_connections.clear()
    
    def _create_tables(self, conn: sqlite3.Connection):
        """Create all state management tables"""
        cursor = conn.cursor()
        
        # Scan progress tracking (unified across all features)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS scan_progress (
                account_name TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                last_message_id INTEGER DEFAULT 0,
                messages_scanned INTEGER DEFAULT 0,
                links_found INTEGER DEFAULT 0,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (account_name, chat_id)
            ) WITHOUT ROWID
        ''')
        
        # Link collection
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS link_collection (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL,
                platform TEXT NOT NULL DEFAULT 'telegram',
                source_chat TEXT,
                account_name TEXT,
                discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(url, platform)
            )
        ''')
        
        # Download hashes
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS download_hashes (
                hash TEXT PRIMARY KEY,
                file_path TEXT,
                file_size INTEGER,
                downloaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Photo send progress
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS photo_send_progress (
                account_name TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                last_message_id INTEGER DEFAULT 0,
                photos_sent INTEGER DEFAULT 0,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (account_name, chat_id)
            ) WITHOUT ROWID
        ''')
        
        # Profile photo tracking
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS profile_photo_tracking (
                user_id INTEGER NOT NULL,
                photo_id TEXT NOT NULL,
                downloaded INTEGER DEFAULT 0,
                downloaded_at TIMESTAMP,
                PRIMARY KEY (user_id, photo_id)
            ) WITHOUT ROWID
        ''')
        
        # Schema versioning
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('INSERT OR IGNORE INTO schema_version (version) VALUES (1)')
        
        # Feature progress tracking (for unified scanner)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS feature_progress (
                account_name TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                feature_name TEXT NOT NULL,
                last_message_id INTEGER DEFAULT 0,
                items_processed INTEGER DEFAULT 0,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (account_name, chat_id, feature_name)
            ) WITHOUT ROWID
        ''')
        
        # ── Analytics Tables (Phase 2 Migration) ──
        
        # Users table (replaces Users.csv)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                phone TEXT,
                is_bot INTEGER DEFAULT 0,
                is_premium INTEGER DEFAULT 0,
                is_verified INTEGER DEFAULT 0,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Memberships table (replaces Memberships.csv)
        # Note: Using existing schema with group_id instead of chat_id
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS memberships (
                user_id INTEGER NOT NULL,
                group_id TEXT NOT NULL,
                group_name TEXT,
                discovered_at TIMESTAMP,
                PRIMARY KEY (user_id, group_id)
            )
        ''')
        
        # User history table (replaces user_history.json)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                chat_id TEXT,
                event_data TEXT,
                occurred_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        ''')
        
        # Dashboard stats (pre-computed aggregates)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS dashboard_stats (
                stat_key TEXT PRIMARY KEY,
                stat_value TEXT,
                computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # User changes tracking (replaces user_changes.csv/json)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_changes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                change_type TEXT NOT NULL,
                old_value TEXT,
                new_value TEXT,
                changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS failed_lookups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reference TEXT NOT NULL,
                reference_type TEXT NOT NULL,
                error_type TEXT DEFAULT 'unknown',
                failed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                retry_after TIMESTAMP,
                username TEXT,
                UNIQUE(reference, reference_type)
            )
        ''')
        
        # Entity cache table (for shared entity resolution across accounts)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS entity_cache (
                cache_key TEXT PRIMARY KEY,
                entity_data TEXT,
                entity_type TEXT,
                cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Profile photo change history (two-stage detection)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS profile_photo_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_id INTEGER NOT NULL,
                entity_type TEXT NOT NULL,
                file_id TEXT NOT NULL,
                photo_phash TEXT NOT NULL,
                photo_blob BLOB,
                file_path TEXT,
                detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Ingestion queue for prioritised operation scheduling
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ingestion_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_id INTEGER NOT NULL,
                entity_type TEXT NOT NULL,
                operation_type TEXT NOT NULL,
                priority INTEGER DEFAULT 5,
                status TEXT DEFAULT 'pending',
                attempts INTEGER DEFAULT 0,
                last_error TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(entity_id, operation_type)
            )
        ''')

        # Per-account flood-wait cooldowns (persisted across restarts)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS account_cooldowns (
                account_name TEXT PRIMARY KEY,
                until_ts REAL NOT NULL,
                reason TEXT DEFAULT 'flood-wait',
                flood_wait_seconds INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Media item download tracking
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS media_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_id INTEGER NOT NULL,
                entity_type TEXT NOT NULL,
                message_id INTEGER,
                media_type TEXT NOT NULL,
                file_id TEXT UNIQUE,
                file_path TEXT,
                file_hash TEXT,
                file_size INTEGER,
                download_status TEXT DEFAULT 'pending'
            )
        ''')

        # Create indexes for analytics tables
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_memberships_user ON memberships(user_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_memberships_group ON memberships(group_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_user_history_user ON user_history(user_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_user_history_event ON user_history(event_type)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_user_changes_user ON user_changes(user_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_user_changes_time ON user_changes(changed_at)')
        # These indexes depend on columns added in schema v4/v5 migration.
        # Silently skip if the old schema is still present — _ensure_schema_compatibility
        # will create them after migrating the table.
        try:
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_failed_lookups_ref ON failed_lookups(reference)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_failed_lookups_type ON failed_lookups(reference_type, error_type)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_failed_lookups_username ON failed_lookups(username)')
        except sqlite3.OperationalError:
            pass
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_entity_cache_key ON entity_cache(cache_key)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_profile_photo_history_entity ON profile_photo_history(entity_id, entity_type)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_media_items_entity ON media_items(entity_id, entity_type)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_media_items_status ON media_items(download_status)')

        conn.commit()

    def _ensure_schema_compatibility(self, conn: sqlite3.Connection):
        """Ensure required columns exist for processor compatibility"""
        try:
            # Check current schema version to skip work if already up-to-date
            try:
                row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
                current_version = row['v'] if row else 0
            except sqlite3.OperationalError:
                current_version = 0

            if current_version >= 5:
                return

            # Version 1-3: Legacy schema updates
            if current_version < 3:
                users_columns = {
                    row['name']
                    for row in conn.execute("PRAGMA table_info(users)")
                }
                altered = False
                if 'updated_at' not in users_columns:
                    conn.execute("ALTER TABLE users ADD COLUMN updated_at TIMESTAMP")
                    users_columns.add('updated_at')
                    altered = True
                if 'last_updated' not in users_columns:
                    conn.execute("ALTER TABLE users ADD COLUMN last_updated TIMESTAMP")
                    users_columns.add('last_updated')
                    altered = True
                if 'is_verified' not in users_columns:
                    conn.execute("ALTER TABLE users ADD COLUMN is_verified INTEGER DEFAULT 0")
                    altered = True
                if 'last_seen' not in users_columns:
                    conn.execute("ALTER TABLE users ADD COLUMN last_seen TIMESTAMP")
                    altered = True
                if 'profile_photo_downloaded' not in users_columns:
                    conn.execute("ALTER TABLE users ADD COLUMN profile_photo_downloaded INTEGER DEFAULT 0")
                    altered = True
                if 'profile_photo_last_checked' not in users_columns:
                    conn.execute("ALTER TABLE users ADD COLUMN profile_photo_last_checked TIMESTAMP")
                    altered = True
                if 'profile_photo_count' not in users_columns:
                    conn.execute("ALTER TABLE users ADD COLUMN profile_photo_count INTEGER DEFAULT 0")
                    altered = True
                # Only run the backfill once when columns were just added
                if altered and {'updated_at', 'last_updated'}.issubset(users_columns):
                    conn.execute('''
                        UPDATE users
                        SET
                            updated_at = COALESCE(updated_at, last_updated, CURRENT_TIMESTAMP),
                            last_updated = COALESCE(last_updated, updated_at, CURRENT_TIMESTAMP)
                        WHERE updated_at IS NULL OR last_updated IS NULL
                    ''')
                
                failed_lookup_columns = {
                    row['name']
                    for row in conn.execute("PRAGMA table_info(failed_lookups)")
                }
                if failed_lookup_columns and 'failed_at' not in failed_lookup_columns:
                    conn.execute("ALTER TABLE failed_lookups ADD COLUMN failed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")

                conn.execute("INSERT OR REPLACE INTO schema_version (version) VALUES (3)")
                conn.commit()

            # Version 4: Enhanced failed_lookups table to support username references
            if current_version < 4:
                print("📦 [StateManager] Migrating to schema version 4...")
                # Check if the new schema already exists (possibly from a partial migration)
                table_info = conn.execute("PRAGMA table_info(failed_lookups)").fetchall()
                has_required_columns = any(col[1] == 'reference' for col in table_info)

                if not has_required_columns:
                    # Backup existing failed_lookups table
                    conn.execute("CREATE TABLE IF NOT EXISTS failed_lookups_v3 AS SELECT * FROM failed_lookups")
                    conn.execute("DROP INDEX IF EXISTS idx_failed_lookups_error_type")
                    conn.execute("DROP TABLE IF EXISTS failed_lookups")

                    # Create new failed_lookups table with enhanced schema
                    conn.execute('''
                        CREATE TABLE failed_lookups (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            reference TEXT NOT NULL,
                            reference_type TEXT NOT NULL,
                            error_type TEXT DEFAULT 'unknown',
                            failed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            retry_after TIMESTAMP,
                            UNIQUE(reference, reference_type)
                        )
                    ''')

                    # Create indexes
                    conn.execute("CREATE INDEX idx_failed_lookups_ref ON failed_lookups(reference)")
                    conn.execute("CREATE INDEX idx_failed_lookups_type ON failed_lookups(reference_type, error_type)")

                    # Migrate existing data (user_id references)
                    conn.execute('''
                        INSERT OR IGNORE INTO failed_lookups (reference, reference_type, error_type, failed_at)
                        SELECT CAST(user_id AS TEXT), 'user_id', error_type, failed_at
                        FROM failed_lookups_v3
                    ''')

                    # Drop backup table
                    conn.execute("DROP TABLE IF EXISTS failed_lookups_v3")

                conn.execute("INSERT OR REPLACE INTO schema_version (version) VALUES (4)")
                conn.commit()
                print("✅ [StateManager] Schema version 4 migration complete")

            # Version 5: Add username field to failed_lookups for easier querying
            if current_version < 5:
                print("📦 [StateManager] Migrating to schema version 5...")
                check_columns = {
                    row['name']
                    for row in conn.execute("PRAGMA table_info(failed_lookups)")
                }

                if 'username' not in check_columns:
                    conn.execute("ALTER TABLE failed_lookups ADD COLUMN username TEXT")
                    # Populate for username-type references
                    conn.execute('''
                        UPDATE failed_lookups
                        SET username = reference
                        WHERE reference_type = 'username'
                    ''')
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_failed_lookups_username ON failed_lookups(username)")

                conn.execute("INSERT OR REPLACE INTO schema_version (version) VALUES (5)")
                conn.commit()
                print("✅ [StateManager] Schema version 5 migration complete")

        except sqlite3.OperationalError as e:
            if is_database_lock_error(e):
                print(f"WARNING: {describe_database_lock('ensuring schema compatibility', self.db_path)}")
            else:
                print(f"❌ Error ensuring schema compatibility: {e}")
        except Exception as e:
            print(f"❌ Error ensuring schema compatibility: {e}")
    
    def _start_json_sync_task(self):
        """Kick off an initial JSON backup without relying on deprecated loop APIs."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._sync_to_json_backup_impl()
            return

        self._json_sync_task = loop.create_task(self.sync_to_json_backup())

    def _run_db_write(self, action, context: str, retries: int = 3) -> bool:
        """Run a write transaction with bounded retries for transient lock contention."""
        for attempt in range(retries):
            try:
                action()
                self.conn.commit()
                return True
            except sqlite3.OperationalError as e:
                if not is_database_lock_error(e):
                    print(f"❌ Error {context}: {e}")
                    return False
                if attempt < retries - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                print(f"WARNING: {describe_database_lock(context, self.db_path)}")
                return False
            except Exception as e:
                print(f"❌ Error {context}: {e}")
                return False

        return False
    
    # ── Scan Progress Methods ──
    
    def save_scan_progress(self, account: str, chat_id: str, message_id: int, stats: Dict):
        """Save scan progress to database"""
        self.scan_progress_buffer.append((
            account, chat_id, message_id,
            stats.get('scanned') or 0,
            stats.get('links') or 0
        ))
        
        if len(self.scan_progress_buffer) >= self.buffer_size:
            self._flush_scan_progress()
    
    def _flush_scan_progress(self):
        """Flush scan progress buffer to database"""
        if not self.scan_progress_buffer:
            return
        
        pending = list(self.scan_progress_buffer)

        def action():
            self.conn.executemany('''
                INSERT OR REPLACE INTO scan_progress 
                (account_name, chat_id, last_message_id, messages_scanned, links_found, last_updated)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ''', pending)

        if self._run_db_write(action, "flushing scan progress"):
            self.scan_progress_buffer.clear()
    
    def load_scan_progress(self) -> Dict[str, Dict]:
        """Load scan progress from database"""
        try:
            cursor = self.conn.execute("SELECT * FROM scan_progress")
            result = {}
            for row in cursor:
                key = f"{row['account_name']}::{row['chat_id']}"
                result[key] = {
                    'last_message_id': row['last_message_id'],
                    'messages_scanned': row['messages_scanned'],
                    'links_found': row['links_found'],
                    'last_updated': row['last_updated']
                }
            return result
        except sqlite3.OperationalError:
            return {}
    
    def get_chat_progress(self, account: str, chat_id: str) -> Optional[int]:
        """Get last scanned message ID for a specific chat"""
        try:
            cursor = self.conn.execute(
                "SELECT last_message_id FROM scan_progress WHERE account_name = ? AND chat_id = ?",
                (account, chat_id)
            )
            row = cursor.fetchone()
            return row['last_message_id'] if row else None
        except sqlite3.OperationalError:
            return None

    def update_scan_progress(self, account: str, chat_id: str, message_id: int) -> None:
        """Compatibility helper for legacy callers that only track message offsets."""
        try:
            cursor = self.conn.execute(
                "SELECT messages_scanned, links_found FROM scan_progress WHERE account_name = ? AND chat_id = ?",
                (account, chat_id)
            )
            row = cursor.fetchone()
            stats = {
                'scanned': (row['messages_scanned'] if row else 0) + 1,
                'links': row['links_found'] if row else 0,
            }
        except sqlite3.OperationalError:
            stats = {'scanned': 1, 'links': 0}

        self.save_scan_progress(account, chat_id, message_id, stats)
    
    # ── Link Collection Methods ──
    
    def save_link(self, url: str, platform: str, source_chat: str = None, account: str = None):
        """Save a collected link"""
        self.link_buffer.append((url, platform, source_chat, account))
        
        if len(self.link_buffer) >= self.buffer_size:
            self._flush_links()
    
    def _flush_links(self):
        """Flush link buffer to database"""
        if not self.link_buffer:
            return
        
        pending = list(self.link_buffer)

        def action():
            self.conn.executemany('''
                INSERT OR IGNORE INTO link_collection 
                (url, platform, source_chat, account_name)
                VALUES (?, ?, ?, ?)
            ''', pending)

        if self._run_db_write(action, "flushing collected links"):
            self.link_buffer.clear()
    
    def load_existing_links(self, platform: str = 'telegram') -> Set[str]:
        """Load existing links for deduplication"""
        try:
            cursor = self.conn.execute(
                "SELECT url FROM link_collection WHERE platform = ?", 
                (platform,)
            )
            return {row['url'] for row in cursor}
        except sqlite3.OperationalError:
            return set()
    
    def get_all_links(self, platform: str = None) -> List[str]:
        """Get all collected links"""
        try:
            if platform:
                cursor = self.conn.execute(
                    "SELECT url FROM link_collection WHERE platform = ? ORDER BY id",
                    (platform,)
                )
            else:
                cursor = self.conn.execute("SELECT url FROM link_collection ORDER BY id")
            return [row['url'] for row in cursor]
        except sqlite3.OperationalError:
            return []
    
    def get_link_count(self, platform: str = None) -> int:
        """Get total link count"""
        try:
            if platform:
                cursor = self.conn.execute(
                    "SELECT COUNT(*) as count FROM link_collection WHERE platform = ?",
                    (platform,)
                )
            else:
                cursor = self.conn.execute("SELECT COUNT(*) as count FROM link_collection")
            return cursor.fetchone()['count']
        except sqlite3.OperationalError:
            return 0
    
    # ── Download Hash Methods ──
    
    def save_hash(self, file_hash: str, file_path: str = None, file_size: int = None):
        """Save a download hash"""
        self.hash_buffer.append((file_hash, file_path, file_size))
        
        if len(self.hash_buffer) >= self.buffer_size:
            self._flush_hashes()
    
    def _flush_hashes(self):
        """Flush hash buffer to database"""
        if not self.hash_buffer:
            return
        
        pending = list(self.hash_buffer)

        def action():
            self.conn.executemany('''
                INSERT OR IGNORE INTO download_hashes 
                (hash, file_path, file_size)
                VALUES (?, ?, ?)
            ''', pending)

        if self._run_db_write(action, "flushing download hashes"):
            self.hash_buffer.clear()
    
    def hash_exists(self, file_hash: str) -> bool:
        """Check if hash exists"""
        try:
            cursor = self.conn.execute(
                "SELECT 1 FROM download_hashes WHERE hash = ?", 
                (file_hash,)
            )
            return cursor.fetchone() is not None
        except sqlite3.OperationalError:
            return False
    
    def get_all_hashes(self) -> List[str]:
        """Get all stored hashes"""
        try:
            cursor = self.conn.execute("SELECT hash FROM download_hashes ORDER BY rowid")
            return [row['hash'] for row in cursor]
        except sqlite3.OperationalError:
            return []
    
    def get_hash_count(self) -> int:
        """Get total hash count"""
        try:
            cursor = self.conn.execute("SELECT COUNT(*) as count FROM download_hashes")
            return cursor.fetchone()['count']
        except sqlite3.OperationalError:
            return 0
    
    # ── Photo Send Progress Methods ──
    
    def save_photo_send_progress(self, account: str, chat_id: str, message_id: int, photos_sent: int):
        """Save photo send progress"""
        self.photo_progress_buffer.append((account, chat_id, message_id, photos_sent))
        
        if len(self.photo_progress_buffer) >= self.buffer_size:
            self._flush_photo_progress()
    
    def _flush_photo_progress(self):
        """Flush photo progress buffer"""
        if not self.photo_progress_buffer:
            return
        
        pending = list(self.photo_progress_buffer)

        def action():
            self.conn.executemany('''
                INSERT OR REPLACE INTO photo_send_progress 
                (account_name, chat_id, last_message_id, photos_sent, last_updated)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ''', pending)

        if self._run_db_write(action, "flushing photo send progress"):
            self.photo_progress_buffer.clear()
    
    def load_photo_send_progress(self) -> Dict[str, Dict]:
        """Load photo send progress"""
        try:
            cursor = self.conn.execute("SELECT * FROM photo_send_progress")
            result = {}
            for row in cursor:
                key = f"{row['account_name']}::{row['chat_id']}"
                result[key] = {
                    'last_message_id': row['last_message_id'],
                    'photos_sent': row['photos_sent'],
                    'last_updated': row['last_updated']
                }
            return result
        except sqlite3.OperationalError:
            return {}
    
    # ── Profile Photo Tracking Methods ──
    
    def save_profile_photo(self, user_id: int, photo_id: str, downloaded: bool = True):
        """Save profile photo tracking"""
        def action():
            self.conn.execute('''
                INSERT OR REPLACE INTO profile_photo_tracking 
                (user_id, photo_id, downloaded, downloaded_at)
                VALUES (?, ?, ?, ?)
            ''', (user_id, photo_id, 1 if downloaded else 0, 
                  datetime.now().isoformat() if downloaded else None))
        self._run_db_write(action, "saving profile photo tracking")
    
    def is_profile_photo_downloaded(self, user_id: int, photo_id: str) -> bool:
        """Check if profile photo was downloaded"""
        try:
            cursor = self.conn.execute(
                "SELECT downloaded FROM profile_photo_tracking WHERE user_id = ? AND photo_id = ?",
                (user_id, photo_id)
            )
            row = cursor.fetchone()
            return row['downloaded'] == 1 if row else False
        except sqlite3.OperationalError:
            return False

    def iter_users_for_profile_download(self):
        """Yield non-bot users from the users table for profile-photo download."""
        try:
            cursor = self.conn.execute(
                '''
                SELECT user_id, username, first_name, last_name
                FROM users
                WHERE COALESCE(is_bot, 0) = 0
                ORDER BY user_id
                '''
            )
            for row in cursor:
                yield {
                    'user_id': int(row['user_id']),
                    'username': row['username'] or '',
                    'first_name': row['first_name'] or '',
                    'last_name': row['last_name'] or '',
                }
        except sqlite3.OperationalError:
            return

    def mark_profile_photo_summary(self, user_id: int, photos_downloaded: int = 0):
        """Update per-user profile-photo summary fields for analytics/resume."""
        normalized_count = max(0, int(photos_downloaded or 0))

        def action():
            self.conn.execute(
                '''
                UPDATE users
                SET
                    profile_photo_downloaded = ?,
                    profile_photo_count = ?,
                    profile_photo_last_checked = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP,
                    last_updated = CURRENT_TIMESTAMP
                WHERE user_id = ?
                ''',
                (1 if normalized_count > 0 else 0, normalized_count, int(user_id)),
            )

        self._run_db_write(action, "updating profile photo summary")
    
    # ── Analytics Methods (Phase 2 Migration) ──
    
    def save_user(self, user_id: int, username: str = None, first_name: str = None,
                  last_name: str = None, phone: str = None, is_bot: bool = False,
                  is_premium: bool = False):
        """Save or update user information, preserving added_at on updates"""
        def action():
            self.conn.execute('''
                INSERT INTO users
                (
                    user_id, username, first_name, last_name, phone,
                    is_bot, is_premium, updated_at, last_updated
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = COALESCE(NULLIF(excluded.username, ''), users.username),
                    first_name = COALESCE(NULLIF(excluded.first_name, ''), users.first_name),
                    last_name = COALESCE(NULLIF(excluded.last_name, ''), users.last_name),
                    phone = COALESCE(NULLIF(excluded.phone, ''), users.phone),
                    is_bot = excluded.is_bot,
                    is_premium = excluded.is_premium,
                    updated_at = CURRENT_TIMESTAMP,
                    last_updated = CURRENT_TIMESTAMP
            ''', (user_id, username, first_name, last_name, phone,
                  1 if is_bot else 0, 1 if is_premium else 0))
        self._run_db_write(action, "saving user data")

    def save_user_batch(self, users: List[Dict]):
        """Save multiple users in batch, preserving added_at on updates"""
        batch = [(u['user_id'], u.get('username'), u.get('first_name'),
                 u.get('last_name'), u.get('phone'),
                 1 if u.get('is_bot', False) else 0,
                 1 if u.get('is_premium', False) else 0) for u in users]

        def action():
            self.conn.executemany('''
                INSERT INTO users
                (
                    user_id, username, first_name, last_name, phone,
                    is_bot, is_premium, updated_at, last_updated
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = COALESCE(NULLIF(excluded.username, ''), users.username),
                    first_name = COALESCE(NULLIF(excluded.first_name, ''), users.first_name),
                    last_name = COALESCE(NULLIF(excluded.last_name, ''), users.last_name),
                    phone = COALESCE(NULLIF(excluded.phone, ''), users.phone),
                    is_bot = excluded.is_bot,
                    is_premium = excluded.is_premium,
                    updated_at = CURRENT_TIMESTAMP,
                    last_updated = CURRENT_TIMESTAMP
            ''', batch)
        self._run_db_write(action, "saving a user batch")
    
    def save_membership(self, user_id: int, group_id: str, group_name: str = None, 
                       discovered_at: str = None):
        """Save or update membership information"""
        def action():
            self.conn.execute('''
                INSERT OR REPLACE INTO memberships 
                (user_id, group_id, group_name, discovered_at)
                VALUES (?, ?, ?, ?)
            ''', (user_id, group_id, group_name, discovered_at))
        self._run_db_write(action, "saving membership data")
    
    def save_membership_batch(self, memberships: List[Dict]):
        """Save multiple memberships in batch"""
        batch = [(m['user_id'], m['group_id'], m.get('group_name'), 
                 m.get('discovered_at')) for m in memberships]

        def action():
            self.conn.executemany('''
                INSERT OR REPLACE INTO memberships 
                (user_id, group_id, group_name, discovered_at)
                VALUES (?, ?, ?, ?)
            ''', batch)
        self._run_db_write(action, "saving a membership batch")

    def _sync_upsert_user(self, user_data: Dict[str, Any]) -> None:
        """Synchronously insert or update a user record with username change tracking"""
        user_id = user_data.get('id', user_data.get('user_id'))
        if user_id is None:
            return
        
        new_username = user_data.get('username', '')
        new_username = new_username.strip() if isinstance(new_username, str) else ''
        
        def action():
            # Check if this user exists and what their current username is
            existing = self.conn.execute(
                "SELECT username FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            
            old_username = existing['username'] if existing else None
            
            # Update user record
            self.conn.execute('''
                INSERT INTO users
                (
                    user_id, username, first_name, last_name, phone,
                    is_bot, is_verified, is_premium, last_seen, updated_at, last_updated
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = COALESCE(NULLIF(excluded.username, ''), users.username),
                    first_name = COALESCE(NULLIF(excluded.first_name, ''), users.first_name),
                    last_name = COALESCE(NULLIF(excluded.last_name, ''), users.last_name),
                    phone = COALESCE(NULLIF(excluded.phone, ''), users.phone),
                    is_bot = excluded.is_bot,
                    is_verified = excluded.is_verified,
                    is_premium = excluded.is_premium,
                    last_seen = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP,
                    last_updated = CURRENT_TIMESTAMP
            ''', (
                user_id,
                new_username,
                user_data.get('first_name', ''),
                user_data.get('last_name', ''),
                user_data.get('phone', ''),
                1 if user_data.get('is_bot', False) else 0,
                1 if user_data.get('is_verified', False) else 0,
                1 if user_data.get('is_premium', False) else 0
            ))
            
            # Record username change if different (and both are non-empty)
            if old_username and new_username and old_username != new_username:
                self.conn.execute('''
                    INSERT INTO user_changes 
                    (user_id, change_type, old_value, new_value, changed_at)
                    VALUES (?, 'username_change', ?, ?, CURRENT_TIMESTAMP)
                ''', (user_id, old_username, new_username))
            elif old_username and not new_username:
                # User cleared their username
                self.conn.execute('''
                    INSERT INTO user_changes 
                    (user_id, change_type, old_value, new_value, changed_at)
                    VALUES (?, 'username_cleared', ?, NULL, CURRENT_TIMESTAMP)
                ''', (user_id, old_username))
            elif not old_username and new_username:
                # User set their first username
                self.conn.execute('''
                    INSERT INTO user_changes 
                    (user_id, change_type, old_value, new_value, changed_at)
                    VALUES (?, 'username_set', NULL, ?, CURRENT_TIMESTAMP)
                ''', (user_id, new_username))
        
        self._run_db_write(action, "upserting user data with change tracking")

    async def upsert_user(self, user_data: Dict[str, Any]) -> None:
        """Async wrapper for processor compatibility"""
        self._sync_upsert_user(user_data)

    def _sync_add_membership(self, user_id: int, group_id: str, group_name: str = None) -> None:
        """Synchronously add a membership record"""
        def action():
            self.conn.execute('''
                INSERT OR IGNORE INTO memberships
                (user_id, group_id, group_name, discovered_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ''', (user_id, str(group_id), group_name))
        self._run_db_write(action, "adding membership data")

    async def add_membership(self, user_id: int, group_id: str, group_name: str = None) -> None:
        """Async wrapper for processor compatibility"""
        self._sync_add_membership(user_id, group_id, group_name)

    def is_failed_lookup(self, reference: Any, reference_type: str = None) -> bool:
        """
        Check whether a reference was previously marked as a failed lookup.
        
        Args:
            reference: Can be user_id (int) or username (str)
            reference_type: Optional, auto-detected if not provided ('user_id' or 'username')
        
        Returns:
            True if the reference is in the failed_lookups table, False otherwise
        """
        if reference is None:
            return False
        
        # Auto-detect reference type if not provided
        if reference_type is None:
            reference_type = 'username' if isinstance(reference, str) else 'user_id'
        
        try:
            # Return True (skip this lookup) when permanently blocked (NULL) or block hasn't expired yet (> now)
            cursor = self.conn.execute(
                """SELECT 1 FROM failed_lookups
                   WHERE reference = ? AND reference_type = ?
                   AND (retry_after IS NULL OR retry_after > CURRENT_TIMESTAMP)""",
                (str(reference), reference_type)
            )
            return cursor.fetchone() is not None
        except sqlite3.OperationalError:
            # Fallback for old schema during migration
            try:
                cursor = self.conn.execute(
                    "SELECT 1 FROM failed_lookups WHERE user_id = ?",
                    (int(reference),)
                )
                return cursor.fetchone() is not None
            except (ValueError, TypeError, sqlite3.OperationalError):
                return False

    def _sync_add_failed_lookup(
        self, 
        reference: Any, 
        error_type: str = 'unknown',
        reference_type: str = None,
        retry_after_days: int = 7
    ) -> None:
        """
        Synchronously store a failed lookup with enhanced tracking.
        
        Args:
            reference: Can be user_id (int) or username (str)
            error_type: Type of error that occurred
            reference_type: Optional, auto-detected if not provided ('user_id' or 'username')
            retry_after_days: Days to wait before retrying this lookup (None for permanent)
        """
        if reference is None:
            return
        
        # Auto-detect reference type if not provided
        if reference_type is None:
            reference_type = 'username' if isinstance(reference, str) else 'user_id'
        
        # Extract username for easier querying if it's a username reference
        username = reference if reference_type == 'username' else None
        
        def action():
            # Build retry_after timestamp if specified
            retry_after_val = None
            if retry_after_days is not None:
                cursor = self.conn.execute(f"SELECT datetime('now', '+{retry_after_days} days')")
                retry_after_val = cursor.fetchone()[0]
            
            self.conn.execute('''
                INSERT OR IGNORE INTO failed_lookups 
                (reference, reference_type, error_type, failed_at, retry_after, username)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?, ?)
            ''', (str(reference), reference_type, error_type, retry_after_val, username))
        self._run_db_write(action, "recording a failed lookup")

    async def add_failed_lookup(
        self, 
        reference: Any, 
        error_type: str = 'unknown',
        reference_type: str = None,
        retry_after_days: int = 7
    ) -> None:
        """
        Async wrapper for processor compatibility with enhanced tracking.
        
        Args:
            reference: Can be user_id (int) or username (str)
            error_type: Type of error that occurred
            reference_type: Optional, auto-detected if not provided ('user_id' or 'username')
            retry_after_days: Days to wait before retrying this lookup (None for permanent)
        """
        self._sync_add_failed_lookup(reference, error_type, reference_type, retry_after_days)
    
    # ── Entity Cache Methods ──
    
    def get_cached_entity(self, cache_key: str) -> Optional[Dict]:
        """
        Query entity_cache table by cache_key.
        Returns deserialized entity data if found, None otherwise.
        Handles database errors gracefully.
        """
        try:
            cursor = self.conn.execute(
                "SELECT entity_data, entity_type FROM entity_cache WHERE cache_key = ?",
                (cache_key,)
            )
            row = cursor.fetchone()
            if row:
                return self._deserialize_entity(row['entity_data'], row['entity_type'])
            return None
        except sqlite3.OperationalError as e:
            if is_database_lock_error(e):
                print(f"WARNING: {describe_database_lock('retrieving cached entity', self.db_path)}")
            else:
                print(f"❌ Error retrieving cached entity: {e}")
            return None
        except Exception as e:
            print(f"❌ Error retrieving cached entity: {e}")
            return None
    
    def save_cached_entity(self, cache_key: str, entity: Any, entity_type: str) -> None:
        """
        Serialize entity to JSON using _serialize_entity.
        Insert or replace into entity_cache table.
        Handles database errors gracefully.
        """
        try:
            entity_data = self._serialize_entity(entity)
            
            def action():
                self.conn.execute('''
                    INSERT OR REPLACE INTO entity_cache 
                    (cache_key, entity_data, entity_type, cached_at)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ''', (cache_key, entity_data, entity_type))
            
            self._run_db_write(action, "saving cached entity")
        except Exception as e:
            print(f"❌ Error saving cached entity: {e}")
    
    def _serialize_entity(self, entity: Any) -> str:
        """
        Extract relevant entity attributes and convert to JSON string.
        Handles serialization errors gracefully.
        """
        try:
            # Extract relevant attributes from entity object
            entity_dict = {}
            
            # Common attributes for User/Channel entities
            if hasattr(entity, 'id'):
                entity_dict['id'] = entity.id
            if hasattr(entity, 'username'):
                entity_dict['username'] = entity.username
            if hasattr(entity, 'first_name'):
                entity_dict['first_name'] = entity.first_name
            if hasattr(entity, 'last_name'):
                entity_dict['last_name'] = entity.last_name
            if hasattr(entity, 'phone'):
                entity_dict['phone'] = entity.phone
            if hasattr(entity, 'is_bot'):
                entity_dict['is_bot'] = entity.is_bot
            if hasattr(entity, 'is_premium'):
                entity_dict['is_premium'] = entity.is_premium
            if hasattr(entity, 'is_verified'):
                entity_dict['is_verified'] = entity.is_verified
            if hasattr(entity, 'title'):
                entity_dict['title'] = entity.title
            if hasattr(entity, 'access_hash'):
                entity_dict['access_hash'] = entity.access_hash
            
            return json.dumps(entity_dict, ensure_ascii=False)
        except Exception as e:
            print(f"❌ Error serializing entity: {e}")
            return "{}"
    
    def _deserialize_entity(self, entity_data: str, entity_type: str) -> Dict:
        """
        Parse JSON string to dictionary.
        Returns entity data dictionary.
        Handles deserialization errors gracefully.
        """
        try:
            return json.loads(entity_data)
        except Exception as e:
            print(f"❌ Error deserializing entity: {e}")
            return {}
    
    def save_user_history_event(self, user_id: int, event_type: str, 
                                chat_id: str = None, event_data: Dict = None):
        """Save a user history event"""
        def action():
            self.conn.execute('''
                INSERT INTO user_history 
                (user_id, event_type, chat_id, event_data, occurred_at)
                VALUES (?, ?, ?, ?, ?)
            ''', (user_id, event_type, chat_id, 
                  json.dumps(event_data) if event_data else None, 
                  datetime.now().isoformat()))
        self._run_db_write(action, "saving a user history event")
    
    def save_user_change(self, user_id: int, change_type: str, 
                        old_value: str = None, new_value: str = None):
        """Save a user change event"""
        def action():
            self.conn.execute('''
                INSERT INTO user_changes 
                (user_id, change_type, old_value, new_value, changed_at)
                VALUES (?, ?, ?, ?, ?)
            ''', (user_id, change_type, old_value, new_value, datetime.now().isoformat()))
        self._run_db_write(action, "saving a user change")
    
    def save_dashboard_stat(self, stat_key: str, stat_value: Dict):
        """Save pre-computed dashboard statistic"""
        def action():
            self.conn.execute('''
                INSERT OR REPLACE INTO dashboard_stats 
                (stat_key, stat_value, computed_at)
                VALUES (?, ?, ?)
            ''', (stat_key, json.dumps(stat_value), datetime.now().isoformat()))
        self._run_db_write(action, "saving dashboard statistics")
    
    def get_user(self, user_id: int) -> Optional[Dict]:
        """Get user by ID"""
        try:
            cursor = self.conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
        except sqlite3.OperationalError:
            return None
    
    def get_user_count(self) -> int:
        """Get total user count"""
        try:
            cursor = self.conn.execute("SELECT COUNT(*) as count FROM users")
            return cursor.fetchone()['count']
        except sqlite3.OperationalError:
            return 0
    
    def get_membership_count(self) -> int:
        """Get total membership count"""
        try:
            cursor = self.conn.execute("SELECT COUNT(*) as count FROM memberships")
            return cursor.fetchone()['count']
        except sqlite3.OperationalError:
            return 0
    
    def get_dashboard_stats(self) -> Dict:
        """Get all dashboard statistics"""
        try:
            cursor = self.conn.execute("SELECT * FROM dashboard_stats")
            result = {}
            for row in cursor:
                result[row['stat_key']] = json.loads(row['stat_value']) if row['stat_value'] else {}
            return result
        except sqlite3.OperationalError:
            return {}
    
    # ── Feature Progress Tracking (for Unified Scanner) ──
    
    def save_feature_progress(self, account_name: str, chat_id: str, feature_name: str, 
                             last_message_id: int, items_processed: int) -> None:
        """Save progress for a specific feature"""
        key = (account_name, chat_id, feature_name)
        self.feature_progress_buffer[key] = (
            account_name,
            chat_id,
            feature_name,
            last_message_id,
            items_processed,
        )
        self.feature_progress_update_count += 1

        if self.feature_progress_update_count >= self.buffer_size:
            self._flush_feature_progress()

    def _flush_feature_progress(self) -> None:
        """Flush buffered feature progress updates to database."""
        if not self.feature_progress_buffer:
            return

        pending = list(self.feature_progress_buffer.values())

        def action():
            self.conn.executemany('''
                INSERT OR REPLACE INTO feature_progress 
                (account_name, chat_id, feature_name, last_message_id, items_processed, last_updated)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ''', pending)

        if self._run_db_write(action, "saving feature progress"):
            self.feature_progress_buffer.clear()
            self.feature_progress_update_count = 0
    
    def get_feature_progress(self, account_name: str, chat_id: str, feature_name: str) -> Optional[int]:
        """Get last_message_id progress for a specific feature"""
        buffered = self.feature_progress_buffer.get((account_name, chat_id, feature_name))
        if buffered:
            return buffered[3]

        try:
            cursor = self.conn.execute(
                "SELECT last_message_id FROM feature_progress WHERE account_name = ? AND chat_id = ? AND feature_name = ?",
                (account_name, chat_id, feature_name)
            )
            row = cursor.fetchone()
            return row['last_message_id'] if row else None
        except sqlite3.OperationalError:
            return None
    
    def get_feature_progress_all(self, account_name: str, chat_id: str) -> Dict[str, int]:
        """Get progress for all features for a specific account/chat"""
        try:
            cursor = self.conn.execute(
                "SELECT feature_name, last_message_id, items_processed FROM feature_progress WHERE account_name = ? AND chat_id = ?",
                (account_name, chat_id)
            )
            result = {}
            for row in cursor:
                result[row['feature_name']] = row['last_message_id']
            for (
                buffered_account,
                buffered_chat,
                buffered_feature,
            ), buffered_value in self.feature_progress_buffer.items():
                if buffered_account == account_name and buffered_chat == chat_id:
                    result[buffered_feature] = buffered_value[3]
            return result
        except sqlite3.OperationalError:
            return {}
    
    def reset_feature_progress(self, account_name: str, chat_id: str, feature_name: str = None) -> None:
        """Reset progress for a feature (or all features if feature_name is None)"""
        buffered_keys = [
            key for key in self.feature_progress_buffer
            if key[0] == account_name and key[1] == chat_id and (feature_name is None or key[2] == feature_name)
        ]
        for key in buffered_keys:
            self.feature_progress_buffer.pop(key, None)

        def action():
            if feature_name:
                self.conn.execute(
                    "DELETE FROM feature_progress WHERE account_name = ? AND chat_id = ? AND feature_name = ?",
                    (account_name, chat_id, feature_name)
                )
            else:
                self.conn.execute(
                    "DELETE FROM feature_progress WHERE account_name = ? AND chat_id = ?",
                    (account_name, chat_id)
                )
        self._run_db_write(action, "resetting feature progress")

    def flush_all_buffers(self) -> None:
        """Persist all in-memory tracking buffers before inspection or reset operations."""
        self._flush_scan_progress()
        self._flush_links()
        self._flush_hashes()
        self._flush_photo_progress()
        self._flush_feature_progress()

    def reset_scan_progress(
        self,
        account_name: str = None,
        chat_id: str = None,
    ) -> None:
        """Reset unified scan checkpoints globally or for a specific account/chat."""
        pending = []
        for row in self.scan_progress_buffer:
            buffered_account, buffered_chat = row[0], row[1]
            if account_name and buffered_account != account_name:
                pending.append(row)
                continue
            if chat_id and buffered_chat != chat_id:
                pending.append(row)
                continue
        self.scan_progress_buffer = pending

        def action():
            query = "DELETE FROM scan_progress"
            params = []
            clauses = []
            if account_name:
                clauses.append("account_name = ?")
                params.append(account_name)
            if chat_id:
                clauses.append("chat_id = ?")
                params.append(chat_id)
            if clauses:
                query += " WHERE " + " AND ".join(clauses)
            self.conn.execute(query, tuple(params))

        self._run_db_write(action, "resetting scan progress")

    def reset_feature_progress_scope(
        self,
        account_name: str = None,
        chat_id: str = None,
        feature_name: str = None,
    ) -> None:
        """Reset feature checkpoints globally or for a scoped account/chat/feature."""
        buffered_keys = []
        for key in self.feature_progress_buffer:
            buffered_account, buffered_chat, buffered_feature = key
            if account_name and buffered_account != account_name:
                continue
            if chat_id and buffered_chat != chat_id:
                continue
            if feature_name and buffered_feature != feature_name:
                continue
            buffered_keys.append(key)

        for key in buffered_keys:
            self.feature_progress_buffer.pop(key, None)
        if buffered_keys:
            self.feature_progress_update_count = max(
                0,
                self.feature_progress_update_count - len(buffered_keys),
            )

        def action():
            query = "DELETE FROM feature_progress"
            params = []
            clauses = []
            if account_name:
                clauses.append("account_name = ?")
                params.append(account_name)
            if chat_id:
                clauses.append("chat_id = ?")
                params.append(chat_id)
            if feature_name:
                clauses.append("feature_name = ?")
                params.append(feature_name)
            if clauses:
                query += " WHERE " + " AND ".join(clauses)
            self.conn.execute(query, tuple(params))

        self._run_db_write(action, "resetting scoped feature progress")

    def reset_photo_send_progress(
        self,
        account_name: str = None,
        chat_id: str = None,
    ) -> None:
        """Reset photo-send checkpoints globally or for a scoped account/chat."""
        pending = []
        for row in self.photo_progress_buffer:
            buffered_account, buffered_chat = row[0], row[1]
            if account_name and buffered_account != account_name:
                pending.append(row)
                continue
            if chat_id and buffered_chat != chat_id:
                pending.append(row)
                continue
        self.photo_progress_buffer = pending

        def action():
            query = "DELETE FROM photo_send_progress"
            params = []
            clauses = []
            if account_name:
                clauses.append("account_name = ?")
                params.append(account_name)
            if chat_id:
                clauses.append("chat_id = ?")
                params.append(chat_id)
            if clauses:
                query += " WHERE " + " AND ".join(clauses)
            self.conn.execute(query, tuple(params))

        self._run_db_write(action, "resetting photo send progress")

    def reset_link_collection(self, platform: str = None, account_name: str = None) -> None:
        """Clear collected-link dedupe/history, optionally scoped by platform or account."""
        if account_name:
            self.link_buffer = [
                row for row in self.link_buffer
                if row[3] != account_name  # index 3 is account_name in buffer tuple
            ]
        elif platform:
            self.link_buffer = [
                row for row in self.link_buffer
                if row[1] != platform
            ]
        else:
            self.link_buffer = []

        def action():
            clauses = []
            params = []
            if platform:
                clauses.append("platform = ?")
                params.append(platform)
            if account_name:
                clauses.append("account_name = ?")
                params.append(account_name)
            query = "DELETE FROM link_collection"
            if clauses:
                query += " WHERE " + " AND ".join(clauses)
            self.conn.execute(query, tuple(params))

        self._run_db_write(action, "resetting collected links")

    def reset_download_hashes(self) -> None:
        """Clear shared media/profile hash dedupe state."""
        self.hash_buffer.clear()

        def action():
            self.conn.execute("DELETE FROM download_hashes")

        self._run_db_write(action, "resetting download hashes")

    def reset_failed_lookups(self) -> None:
        """Clear cached failed user/entity lookups."""
        def action():
            self.conn.execute("DELETE FROM failed_lookups")

        self._run_db_write(action, "resetting failed lookups")

    def get_failed_lookups_report(
        self, 
        error_type: str = None, 
        reference_type: str = None,
        limit: int = 1000
    ) -> List[Dict]:
        """
        Get report of failed lookups, optionally filtered by error type.
        
        Args:
            error_type: Filter by specific error type (e.g., 'username_not_occupied')
            reference_type: Filter by reference type ('user_id' or 'username')
            limit: Maximum number of records to return
        
        Returns:
            List of failed lookup dictionaries
        """
        conditions = []
        params = []
        
        if error_type:
            conditions.append("error_type = ?")
            params.append(error_type)
        
        if reference_type:
            conditions.append("reference_type = ?")
            params.append(reference_type)
        
        where_clause = " AND ".join(conditions) if conditions else "1=1"
        limit_param = params + [limit]
        
        try:
            cursor = self.conn.execute(
                f"SELECT * FROM failed_lookups WHERE {where_clause} ORDER BY failed_at DESC LIMIT ?",
                limit_param
            )
            return [dict(row) for row in cursor]
        except sqlite3.OperationalError as e:
            print(f"❌ Error getting failed lookups report: {e}")
            return []

    def get_failed_lookups_summary(self) -> Dict[str, int]:
        """Get summary statistics of failed lookups by error type."""
        try:
            cursor = self.conn.execute('''
                SELECT 
                    error_type,
                    reference_type,
                    COUNT(*) as count,
                    MIN(failed_at) as first_seen,
                    MAX(failed_at) as last_seen
                FROM failed_lookups
                GROUP BY error_type, reference_type
                ORDER BY count DESC
            ''')
            return {f"{row['error_type']} ({row['reference_type']})": int(row['count']) for row in cursor}
        except sqlite3.OperationalError as e:
            print(f"❌ Error getting failed lookups summary: {e}")
            return {}

    def clear_failed_lookups(
        self, 
        error_type: str = None, 
        reference_type: str = None,
        older_than_days: int = None
    ) -> int:
        """
        Clear failed lookups, optionally filtered by type and age.
        
        Args:
            error_type: Only clear lookups of this error type
            reference_type: Only clear lookups of this reference type
            older_than_days: Only clear lookups older than this many days
        
        Returns:
            Number of rows deleted
        """
        conditions = []
        params = []
        
        if error_type:
            conditions.append("error_type = ?")
            params.append(error_type)
        
        if reference_type:
            conditions.append("reference_type = ?")
            params.append(reference_type)
        
        if older_than_days is not None:
            conditions.append("failed_at < datetime('now', '-' || ? || ' days')")
            params.append(older_than_days)
        
        where_clause = " AND ".join(conditions) if conditions else "1=1"
        
        try:
            cursor = self.conn.execute(
                f"DELETE FROM failed_lookups WHERE {where_clause}",
                params
            )
            self.conn.commit()
            return cursor.rowcount
        except Exception as e:
            print(f"❌ Error clearing failed lookups: {e}")
            return 0

    def retry_failed_lookups(
        self, 
        error_types: List[str] = None,
        usernames_only: bool = False
    ) -> int:
        """
        Mark failed lookups for retry by deleting them from cache.
        
        Args:
            error_types: List of error types to retry (e.g., ['flood_wait'])
            usernames_only: Only retry username references, not user_id references
        
        Returns:
            Number of rows deleted
        """
        conditions = []
        params = []
        
        if error_types:
            placeholders = ",".join("?" * len(error_types))
            conditions.append(f"error_type IN ({placeholders})")
            params.extend(error_types)
        
        if usernames_only:
            conditions.append("reference_type = 'username'")
        
        where_clause = " AND ".join(conditions) if conditions else "1=1"
        
        try:
            cursor = self.conn.execute(
                f"DELETE FROM failed_lookups WHERE {where_clause}",
                params
            )
            self.conn.commit()
            return cursor.rowcount
        except Exception as e:
            print(f"❌ Error retrying failed lookups: {e}")
            return 0

    def reset_profile_photo_tracking(self, user_id: int = None) -> None:
        """Clear profile-photo processing markers globally or for one user."""
        def action():
            if user_id is None:
                self.conn.execute("DELETE FROM profile_photo_tracking")
            else:
                self.conn.execute(
                    "DELETE FROM profile_photo_tracking WHERE user_id = ?",
                    (user_id,),
                )

        self._run_db_write(action, "resetting profile photo tracking")

    def get_tracking_summary(self) -> Dict[str, int]:
        """Return counts for tracking-oriented tables without touching analytics data."""
        self.flush_all_buffers()

        def _count(query: str, params: Tuple[Any, ...] = ()) -> int:
            try:
                cursor = self.conn.execute(query, params)
                return int(cursor.fetchone()["count"])
            except sqlite3.OperationalError:
                return 0

        return {
            'scan_progress': _count("SELECT COUNT(*) AS count FROM scan_progress"),
            'feature_progress': _count("SELECT COUNT(*) AS count FROM feature_progress"),
            'photo_send_progress': _count("SELECT COUNT(*) AS count FROM photo_send_progress"),
            'failed_lookups': _count("SELECT COUNT(*) AS count FROM failed_lookups"),
            'download_hashes': _count("SELECT COUNT(*) AS count FROM download_hashes"),
            'profile_photo_tracking': _count("SELECT COUNT(*) AS count FROM profile_photo_tracking"),
            'link_collection': _count("SELECT COUNT(*) AS count FROM link_collection"),
        }
    
    def get_user_history(self, user_id: int, limit: int = 100) -> List[Dict]:
        """Get user history events"""
        try:
            cursor = self.conn.execute(
                "SELECT * FROM user_history WHERE user_id = ? ORDER BY occurred_at DESC LIMIT ?",
                (user_id, limit))
            return [dict(row) for row in cursor]
        except sqlite3.OperationalError:
            return []
    
    def get_user_changes(self, limit: int = 100) -> List[Dict]:
        """Get recent user changes"""
        try:
            cursor = self.conn.execute(
                "SELECT * FROM user_changes ORDER BY changed_at DESC LIMIT ?", (limit,))
            return [dict(row) for row in cursor]
        except sqlite3.OperationalError:
            return []
    
    # ── JSON Backup Sync Methods ──
    
    def _sync_to_json_backup_impl(self):
        """Sync current database state to JSON backup files."""
        if self._shutdown:
            return

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        
        try:
            # Flush all buffers first
            self._flush_all_buffers()
            
            # Export scan progress
            scan_progress = self.load_scan_progress()
            atomic_json_write(
                str(self.json_backup_dir / f"scan_progress_{timestamp}.json"),
                scan_progress
            )
            
            # Export links
            links = self.get_all_links()
            with open(self.json_backup_dir / f"links_{timestamp}.txt", 'w', encoding='utf-8') as f:
                f.writelines(f"{link}\n" for link in links)
            
            # Export hashes
            hashes = self.get_all_hashes()
            with open(self.json_backup_dir / f"hashes_{timestamp}.txt", 'w', encoding='utf-8') as f:
                f.writelines(f"{hash}\n" for hash in hashes)
            
            # Export photo send progress
            photo_progress = self.load_photo_send_progress()
            atomic_json_write(
                str(self.json_backup_dir / f"photo_send_progress_{timestamp}.json"),
                photo_progress
            )
            
            # Keep only last 3 backups
            self._cleanup_old_backups()
            
        except Exception as e:
            print(f"WARNING: JSON backup sync failed: {e}")

    async def sync_to_json_backup(self):
        """Async wrapper for compatibility with callers that schedule backup tasks."""
        self._sync_to_json_backup_impl()
    
    def _flush_all_buffers(self):
        """Flush all pending buffers to database"""
        self.flush_all_buffers()
    
    def _cleanup_old_backups(self, keep: int = 3):
        """Keep only the most recent N backups"""
        patterns = [
            'scan_progress_*.json',
            'links_*.txt',
            'hashes_*.txt',
            'photo_send_progress_*.json'
        ]
        
        for pattern in patterns:
            backups = sorted(self.json_backup_dir.glob(pattern))
            for old_backup in backups[:-keep]:
                try:
                    old_backup.unlink()
                except Exception:
                    pass
    
    # ── Recovery Methods ──
    
    async def recover_from_json_backup(self):
        """Recover database from latest JSON backup if DB is missing or corrupted"""
        print("🔄 Attempting to recover from JSON backup...")
        
        try:
            # Find latest scan progress backup
            scan_backups = sorted(self.json_backup_dir.glob("scan_progress_*.json"))
            if scan_backups:
                latest_backup = scan_backups[-1]
                print(f"📂 Found scan progress backup: {latest_backup.name}")
                
                try:
                    data = safe_json_load(str(latest_backup))
                    if isinstance(data, dict):
                        for key, value in data.items():
                            if '::' in key:
                                parts = key.split('::', 1)
                            elif '_' in key:
                                parts = key.split('_', 1)
                            else:
                                continue
                            if len(parts) == 2:
                                    account, chat_id = parts
                                    self.save_scan_progress(
                                        account,
                                    chat_id,
                                    value.get('last_message_id') or 0,
                                    {
                                        'scanned': value.get('messages_scanned') or 0,
                                        'links': value.get('links_found') or 0
                                    }
                                    )
                        self._flush_scan_progress()
                        print("✅ Scan progress recovered")
                except Exception as e:
                    print(f"❌ Failed to recover scan progress: {e}")
            
            # Find latest links backup
            link_backups = sorted(self.json_backup_dir.glob("links_*.txt"))
            if link_backups:
                latest_backup = link_backups[-1]
                print(f"📂 Found links backup: {latest_backup.name}")
                try:
                    with open(latest_backup, 'r', encoding='utf-8') as f:
                        count = 0
                        for line in f:
                            url = line.strip()
                            if url:
                                self.save_link(url, 'telegram')
                                count += 1
                                if count % 1000 == 0:
                                    self._flush_links()
                        self._flush_links()
                    print(f"✅ Links recovered ({count} links)")
                except Exception as e:
                    print(f"❌ Failed to recover links: {e}")
            
            # Find latest hashes backup
            hash_backups = sorted(self.json_backup_dir.glob("hashes_*.txt"))
            if hash_backups:
                latest_backup = hash_backups[-1]
                print(f"📂 Found hashes backup: {latest_backup.name}")
                try:
                    with open(latest_backup, 'r', encoding='utf-8') as f:
                        count = 0
                        for line in f:
                            file_hash = line.strip()
                            if file_hash:
                                self.save_hash(file_hash)
                                count += 1
                                if count % 1000 == 0:
                                    self._flush_hashes()
                        self._flush_hashes()
                    print(f"✅ Hashes recovered ({count} hashes)")
                except Exception as e:
                    print(f"❌ Failed to recover hashes: {e}")
            
            # Find latest photo send progress backup
            photo_backups = sorted(self.json_backup_dir.glob("photo_send_progress_*.json"))
            if photo_backups:
                latest_backup = photo_backups[-1]
                print(f"📂 Found photo send progress backup: {latest_backup.name}")
                try:
                    data = safe_json_load(str(latest_backup))
                    if isinstance(data, dict):
                        for key, value in data.items():
                            if '::' in key:
                                parts = key.split('::', 1)
                            elif '_' in key:
                                parts = key.split('_', 1)
                            else:
                                continue
                            if len(parts) == 2:
                                    account, chat_id = parts
                                    self.save_photo_send_progress(
                                        account,
                                        chat_id,
                                        value.get('last_message_id') or 0,
                                        value.get('photos_sent') or 0
                                    )
                        self._flush_photo_progress()
                        print("✅ Photo send progress recovered")
                except Exception as e:
                    print(f"❌ Failed to recover photo send progress: {e}")
            
        except Exception as e:
            print(f"❌ Recovery failed: {e}")
    
    def close(self):
        """Clean shutdown - flush buffers, sync to backup, and close all connections."""
        if self._shutdown:
            return

        self._shutdown = True

        if self._json_sync_task and not self._json_sync_task.done():
            self._json_sync_task.cancel()

        try:
            self.flush_all_buffers()
        except Exception:
            pass

        try:
            self._sync_to_json_backup_impl()
        except Exception as e:
            print(f"WARNING: Final backup sync failed: {e}")

        with self._conns_lock:
            for c in list(self._all_connections):
                try:
                    c.execute("PRAGMA wal_checkpoint(PASSIVE)")
                    c.close()
                except Exception:
                    pass
            self._all_connections.clear()
        if hasattr(self._tls, 'conn'):
            self._tls.conn = None
    
    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
    
    # ── Export Functions (On-Demand CSV/JSON Generation) ──
    
    def export_users_to_csv(self, output_file: str = "data/Users.csv") -> int:
        """Export users table to CSV file"""
        import csv
        try:
            cursor = self.conn.execute("SELECT * FROM users ORDER BY user_id")
            columns = [description[0] for description in cursor.description]
            
            count = 0
            with open(output_file, 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=columns)
                writer.writeheader()
                for row in cursor:
                    writer.writerow(dict(row))
                    count += 1
            
            print(f"✅ Exported {count} users to {output_file}")
            return count
        except Exception as e:
            print(f"❌ Error exporting users: {e}")
            return 0
    
    def export_memberships_to_csv(self, output_file: str = "data/Memberships.csv") -> int:
        """Export memberships table to CSV file"""
        import csv
        try:
            cursor = self.conn.execute("SELECT * FROM memberships ORDER BY user_id, group_id")
            columns = [description[0] for description in cursor.description]
            
            count = 0
            with open(output_file, 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=columns)
                writer.writeheader()
                for row in cursor:
                    writer.writerow(dict(row))
                    count += 1
            
            print(f"✅ Exported {count} memberships to {output_file}")
            return count
        except Exception as e:
            print(f"❌ Error exporting memberships: {e}")
            return 0
    
    def export_users_to_json(self, output_file: str = "data/users.json") -> int:
        """Export users table to JSON file"""
        try:
            cursor = self.conn.execute("SELECT * FROM users ORDER BY user_id")
            users = [dict(row) for row in cursor]
            
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(users, f, indent=2, ensure_ascii=False)
            
            print(f"✅ Exported {len(users)} users to {output_file}")
            return len(users)
        except Exception as e:
            print(f"❌ Error exporting users to JSON: {e}")
            return 0
    
    def export_memberships_to_json(self, output_file: str = "data/memberships.json") -> int:
        """Export memberships table to JSON file"""
        try:
            cursor = self.conn.execute("SELECT * FROM memberships ORDER BY user_id, group_id")
            memberships = [dict(row) for row in cursor]
            
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(memberships, f, indent=2, ensure_ascii=False)
            
            print(f"✅ Exported {len(memberships)} memberships to {output_file}")
            return len(memberships)
        except Exception as e:
            print(f"❌ Error exporting memberships to JSON: {e}")
            return 0
    
    def export_all_to_csv(self, output_dir: str = "data") -> Dict[str, int]:
        """Export all tables to CSV files"""
        results = {
            'users': self.export_users_to_csv(str(Path(output_dir) / "Users.csv")),
            'memberships': self.export_memberships_to_csv(str(Path(output_dir) / "Memberships.csv")),
        }
        return results
    
    def export_all_to_json(self, output_dir: str = "data") -> Dict[str, int]:
        """Export all tables to JSON files"""
        results = {
            'users': self.export_users_to_json(str(Path(output_dir) / "users.json")),
            'memberships': self.export_memberships_to_json(str(Path(output_dir) / "memberships.json")),
        }
        return results


# ── Global State Manager Interface ──

_state_manager: Optional[StateManager] = None
_lock: Optional[asyncio.Lock] = None


async def get_state_manager_async() -> StateManager:
    """Get or create global state manager instance (async-safe)"""
    global _state_manager, _lock
    if _lock is None:
        _lock = asyncio.Lock()
    if _state_manager is None:
        async with _lock:
            if _state_manager is None:
                _state_manager = StateManager()
    return _state_manager


def get_state_manager() -> StateManager:
    """Get or create global state manager instance (sync)."""
    global _state_manager
    if _state_manager is None or getattr(_state_manager, 'conn', None) is None:
        StateManager._instance = None
        _state_manager = StateManager()
    return _state_manager


def shutdown_state_manager():
    """Clean shutdown of state manager"""
    global _state_manager
    if _state_manager:
        _state_manager.close()
        _state_manager = None
    StateManager._instance = None


# ── Convenience Functions ──

async def ensure_database_exists():
    """Ensure database exists, recover from backup if needed"""
    db_path = Path("data/users_analysis.db")
    
    if not db_path.exists():
        print("WARNING: Database not found! Attempting to recreate from backup...")
        state = get_state_manager()
        await state.recover_from_json_backup()
        print("✅ Database recreated from backup")
    else:
        # Verify database integrity
        try:
            conn = connect_sqlite(db_path)
            conn.execute("SELECT 1 FROM schema_version LIMIT 1")
            conn.close()
        except sqlite3.OperationalError as e:
            if is_database_lock_error(e):
                print(f"WARNING: {describe_database_lock('verifying database integrity', db_path)}")
                return
            raise
        except Exception as e:
            print("WARNING: Database corrupted! Recreating from backup...")
            # Backup corrupted DB
            corrupted_path = db_path.with_suffix('.db.corrupted')
            try:
                db_path.rename(corrupted_path)
            except Exception:
                pass
            # Recover
            state = get_state_manager()
            await state.recover_from_json_backup()
            print("✅ Database recreated from backup")
