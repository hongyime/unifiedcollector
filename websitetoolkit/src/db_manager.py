import os
import json
import sqlite3
import logging
import math
import atexit
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional, Tuple

logger = logging.getLogger(__name__)


def _connect(db_path: str) -> sqlite3.Connection:
    """Open a WAL-aware connection with a 5-second busy retry window."""
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.execute("PRAGMA busy_timeout=5000")
    return conn

class DatabaseManager:
    """Manages SQLite database with automated JSON backup/sync and advanced analytics"""
    
    def __init__(self, db_path: str, backup_dir: str):
        self.db_path = db_path
        self.backup_dir = backup_dir
        os.makedirs(self.backup_dir, exist_ok=True)
        self.init_db()

    def init_db(self):
        with _connect(self.db_path) as conn:
            conn.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS file_hashes (
                    hash_id TEXT PRIMARY KEY,
                    hash_type TEXT,
                    timestamp TEXT
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value_json TEXT
                );
                CREATE TABLE IF NOT EXISTS websites_config (
                    name TEXT PRIMARY KEY,
                    config_json TEXT
                );
                CREATE TABLE IF NOT EXISTS websites (
                    id INTEGER PRIMARY KEY,
                    name TEXT UNIQUE,
                    url TEXT,
                    enabled BOOLEAN,
                    added_date TEXT,
                    last_crawled TEXT,
                    last_scraped TEXT,
                    total_links_found INTEGER DEFAULT 0,
                    total_photos_downloaded INTEGER DEFAULT 0,
                    discovery_source TEXT
                );
                CREATE TABLE IF NOT EXISTS links (
                    id INTEGER PRIMARY KEY,
                    website_id INTEGER,
                    url TEXT,
                    link_type TEXT,
                    discovered_date TEXT,
                    status TEXT DEFAULT 'pending',
                    FOREIGN KEY (website_id) REFERENCES websites (id)
                );
                CREATE TABLE IF NOT EXISTS search_terms (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    term TEXT UNIQUE NOT NULL,
                    search_count INTEGER DEFAULT 1,
                    last_searched TEXT,
                    results_found INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS media_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    website_id INTEGER,
                    url TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    phash TEXT,
                    media_type TEXT,
                    file_size INTEGER,
                    downloaded_at TEXT,
                    FOREIGN KEY (website_id) REFERENCES websites (id),
                    UNIQUE(sha256)
                );
                CREATE TABLE IF NOT EXISTS cycles (
                    id INTEGER PRIMARY KEY,
                    cycle_id TEXT UNIQUE,
                    start_time TEXT,
                    end_time TEXT,
                    websites_processed INTEGER,
                    links_discovered INTEGER,
                    photos_downloaded INTEGER,
                    new_websites_added INTEGER,
                    status TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_websites_enabled ON websites(enabled);
                CREATE INDEX IF NOT EXISTS idx_links_website ON links(website_id);
                CREATE INDEX IF NOT EXISTS idx_links_status ON links(status);
                CREATE INDEX IF NOT EXISTS idx_cycles_date ON cycles(start_time);
            """)

    def sync_from_backup(self):
        """Load from JSON backups if DB is empty or missing data"""
        hashes_backup = os.path.join(self.backup_dir, 'file_hashes_backup.json')
        config_backup = os.path.join(self.backup_dir, 'websites_config_backup.json')
        
        with _connect(self.db_path) as conn:
            # Sync hashes
            if os.path.exists(hashes_backup):
                try:
                    with open(hashes_backup, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        for item in data:
                            conn.execute(
                                "INSERT OR IGNORE INTO file_hashes (hash_id, hash_type, timestamp) VALUES (?, ?, ?)",
                                (item['hash_id'], item['hash_type'], item['timestamp'])
                            )
                except Exception as e:
                    logger.error(f"Error syncing hashes from backup: {e}")

            # Sync config
            if os.path.exists(config_backup):
                try:
                    with open(config_backup, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        
                        # Sync settings
                        if 'settings' in data:
                            for k, v in data['settings'].items():
                                conn.execute(
                                    "INSERT OR IGNORE INTO settings (key, value_json) VALUES (?, ?)",
                                    (k, json.dumps(v))
                                )
                        
                        # Sync websites
                        if 'websites' in data:
                            for w in data['websites']:
                                if isinstance(w, dict):
                                    name = w.get('name', '')
                                    if name:
                                        conn.execute(
                                            "INSERT OR IGNORE INTO websites_config (name, config_json) VALUES (?, ?)",
                                            (name, json.dumps(w))
                                        )
                                elif isinstance(w, str):
                                    # string based URL
                                    name = w.split('://')[-1].split('/')[0]
                                    if name.startswith('www.'):
                                        name = name[4:]
                                    w_dict = {'name': name, 'url': w, 'enabled': True}
                                    conn.execute(
                                        "INSERT OR IGNORE INTO websites_config (name, config_json) VALUES (?, ?)",
                                        (name, json.dumps(w_dict))
                                    )
                except Exception as e:
                    logger.error(f"Error syncing config from backup: {e}")
        self.sync_config_to_websites()

    def sync_config_to_websites(self) -> int:
        """Sync websites_config to websites table, preserving per-site stats."""
        synced = 0
        with _connect(self.db_path) as conn:
            rows = conn.execute("SELECT config_json FROM websites_config").fetchall()
            for row in rows:
                try:
                    website = json.loads(row[0])
                    name = website.get('name', '')
                    url = website.get('url', '')
                    enabled = website.get('enabled', True)
                    added_date = website.get('created_at', datetime.now().isoformat())
                    conn.execute("""
                        INSERT INTO websites (name, url, enabled, added_date)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(name) DO UPDATE SET
                            url=excluded.url,
                            enabled=excluded.enabled
                    """, (name, url, enabled, added_date))
                    synced += 1
                except Exception as e:
                    logger.error(f"WARNING: Failed to sync website {row[0]}: {e}")
        return synced

    def update_backup(self):
        """Export current DB state to JSON backups with atomic writes"""
        hashes_backup = os.path.join(self.backup_dir, 'file_hashes_backup.json')
        config_backup = os.path.join(self.backup_dir, 'websites_config_backup.json')
        
        with _connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            
            # Backup hashes - atomic write
            hashes = [dict(row) for row in conn.execute("SELECT * FROM file_hashes")]
            tmp_hashes = hashes_backup + '.tmp'
            with open(tmp_hashes, 'w', encoding='utf-8') as f:
                json.dump(hashes, f, indent=2)
            os.replace(tmp_hashes, hashes_backup)  # atomic
                
            # Backup config - atomic write
            settings_rows = conn.execute("SELECT * FROM settings").fetchall()
            settings = {row['key']: json.loads(row['value_json']) for row in settings_rows}
            
            websites_rows = conn.execute("SELECT * FROM websites_config").fetchall()
            websites = [json.loads(row['config_json']) for row in websites_rows]
            
            config_data = {
                'settings': settings,
                'websites': websites
            }
            tmp_config = config_backup + '.tmp'
            with open(tmp_config, 'w', encoding='utf-8') as f:
                json.dump(config_data, f, indent=2)
            os.replace(tmp_config, config_backup)  # atomic

    # Analytics and Readability Methods (migrated from DataReadabilityManager)
    def get_paginated_websites(self, page: int = 1, per_page: int = 20, 
                              filter_enabled: Optional[bool] = None) -> Dict[str, Any]:
        """Get paginated website list for better readability"""
        offset = (page - 1) * per_page
        
        base_query = "SELECT * FROM websites"
        count_query = "SELECT COUNT(*) FROM websites"
        params = []
        
        if filter_enabled is not None:
            base_query += " WHERE enabled = ?"
            count_query += " WHERE enabled = ?"
            params.append(filter_enabled)
        
        base_query += " ORDER BY last_crawled DESC LIMIT ? OFFSET ?"
        params.extend([per_page, offset])
        
        with _connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            
            # Get total count
            total_count = conn.execute(count_query, params[:-2] if filter_enabled is not None else []).fetchone()[0]
            
            # Get paginated results
            websites = [dict(row) for row in conn.execute(base_query, params)]
            
            return {
                'websites': websites,
                'pagination': {
                    'current_page': page,
                    'per_page': per_page,
                    'total_count': total_count,
                    'total_pages': math.ceil(total_count / per_page),
                    'has_next': page * per_page < total_count,
                    'has_prev': page > 1
                }
            }

    def get_system_metrics(self) -> Dict[str, Any]:
        """Get comprehensive system metrics"""
        with _connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            
            # Website counts
            total_websites = conn.execute("SELECT COUNT(*) FROM websites").fetchone()[0]
            enabled_websites = conn.execute("SELECT COUNT(*) FROM websites WHERE enabled = 1").fetchone()[0]
            
            # Link counts
            total_links = conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]
            
            # Photo counts from cycles
            total_photos = conn.execute("""
                SELECT COALESCE(SUM(photos_downloaded), 0) FROM cycles
            """).fetchone()[0]
            
            # Storage usage
            storage_mb = self._calculate_storage_usage()
            
            # Health score (0-100)
            health_score = self._calculate_health_score(total_websites, enabled_websites, total_links)
            
            return {
                'total_websites': total_websites,
                'enabled_websites': enabled_websites,
                'total_links_stored': total_links,
                'total_photos_downloaded': total_photos,
                'storage_used_mb': storage_mb,
                'last_update': datetime.now().isoformat(),
                'data_health_score': health_score
            }

    def _calculate_storage_usage(self) -> float:
        """Calculate total storage usage in MB"""
        total_size = 0
        data_dir = os.path.dirname(self.db_path)
        for root, dirs, files in os.walk(data_dir):
            for file in files:
                file_path = os.path.join(root, file)
                try:
                    total_size += os.path.getsize(file_path)
                except (OSError, FileNotFoundError):
                    continue
        return total_size / (1024 * 1024)

    def _calculate_health_score(self, total_sites: int, enabled_sites: int, total_links: int) -> float:
        if total_sites == 0: return 0.0
        enabled_ratio = enabled_sites / total_sites
        links_per_site = total_links / total_sites
        enabled_score = enabled_ratio * 100
        activity_score = min(links_per_site * 2, 100)
        return round((enabled_score * 0.4) + (activity_score * 0.6), 1)

    def get_advanced_statistics(self) -> Dict[str, Any]:
        """Calculate advanced statistics for analytics and reporting"""
        with _connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row

            total_websites = conn.execute("SELECT COUNT(*) FROM websites").fetchone()[0]
            enabled_websites = conn.execute("SELECT COUNT(*) FROM websites WHERE enabled = 1").fetchone()[0]
            avg_links_per_site = conn.execute("SELECT AVG(total_links_found) FROM websites").fetchone()[0] or 0
            avg_photos_per_site = conn.execute("SELECT AVG(total_photos_downloaded) FROM websites").fetchone()[0] or 0

            top_sites = conn.execute(
                "SELECT name, total_links_found, total_photos_downloaded FROM websites ORDER BY total_links_found DESC LIMIT 5"
            ).fetchall()

            recent_cycles = conn.execute(
                "SELECT cycle_id, start_time, websites_processed, photos_downloaded, new_websites_added FROM cycles ORDER BY start_time DESC LIMIT 5"
            ).fetchall()

            return {
                "total_websites": total_websites,
                "enabled_websites": enabled_websites,
                "avg_links_per_site": avg_links_per_site,
                "avg_photos_per_site": avg_photos_per_site,
                "top_sites": [dict(row) for row in top_sites],
                "recent_cycles": [dict(row) for row in recent_cycles],
            }

    # Hash operations
    def add_hash(self, hash_id: str, hash_type: str, timestamp: str):
        with _connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO file_hashes (hash_id, hash_type, timestamp) VALUES (?, ?, ?)",
                (hash_id, hash_type, timestamp)
            )
        # Backup is intentionally not flushed here — doing so per-hash causes a full JSON rewrite
        # for every downloaded image. Backup is refreshed by save_settings() / save_websites().

    def claim_hash_atomic(self, hash_id: str, hash_type: str) -> bool:
        """Atomically claim a hash slot. Returns True if we own it (proceed), False if already taken."""
        with _connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO file_hashes (hash_id, hash_type, timestamp) VALUES (?, ?, ?)",
                (hash_id, hash_type, datetime.now().isoformat())
            )
            return conn.execute("SELECT changes()").fetchone()[0] > 0

    def has_hash(self, hash_id: str) -> bool:
        with _connect(self.db_path) as conn:
            cursor = conn.execute("SELECT 1 FROM file_hashes WHERE hash_id = ?", (hash_id,))
            return cursor.fetchone() is not None

    def get_all_hashes(self, hash_type: Optional[str] = None) -> List[str]:
        with _connect(self.db_path) as conn:
            if hash_type:
                cursor = conn.execute("SELECT hash_id FROM file_hashes WHERE hash_type = ?", (hash_type,))
            else:
                cursor = conn.execute("SELECT hash_id FROM file_hashes")
            return [row[0] for row in cursor.fetchall()]

    # Config operations
    def get_settings(self) -> Dict[str, Any]:
        with _connect(self.db_path) as conn:
            rows = conn.execute("SELECT key, value_json FROM settings").fetchall()
            return {row[0]: json.loads(row[1]) for row in rows}

    def save_settings(self, settings: Dict[str, Any]):
        with _connect(self.db_path) as conn:
            for k, v in settings.items():
                conn.execute(
                    "INSERT OR REPLACE INTO settings (key, value_json) VALUES (?, ?)",
                    (k, json.dumps(v))
                )
        self.update_backup()

    def save_cycle(self, cycle_id: str, start_time: str, end_time: Optional[str],
                   websites_processed: int, links_discovered: int,
                   photos_downloaded: int, new_websites_added: int, status: str):
        with _connect(self.db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO cycles
                (cycle_id, start_time, end_time, websites_processed, links_discovered,
                 photos_downloaded, new_websites_added, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (cycle_id, start_time, end_time, websites_processed,
                  links_discovered, photos_downloaded, new_websites_added, status))

    def get_websites(self) -> List[Dict[str, Any]]:
        with _connect(self.db_path) as conn:
            rows = conn.execute("SELECT config_json FROM websites_config").fetchall()
            return [json.loads(row[0]) for row in rows]

    def save_websites(self, websites: List[Dict[str, Any]]):
        with _connect(self.db_path) as conn:
            conn.execute("DELETE FROM websites_config")
            for w in websites:
                if isinstance(w, dict) and 'name' in w:
                    conn.execute(
                        "INSERT INTO websites_config (name, config_json) VALUES (?, ?)",
                        (w['name'], json.dumps(w))
                    )
        self.sync_config_to_websites()
        self.update_backup()

# Global instance initialization
_db_manager = None


def _atexit_checkpoint(db_path: str) -> None:
    """Merge WAL into the main DB file on clean exit so the next open is fast."""
    try:
        conn = sqlite3.connect(db_path, timeout=2.0)
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        conn.close()
    except Exception:
        pass


def get_db_manager() -> DatabaseManager:
    global _db_manager
    if _db_manager is None:
        from config import DATA_DIR
        db_path = os.path.join(DATA_DIR, 'toolkit.db')
        _db_manager = DatabaseManager(db_path, DATA_DIR)
        _db_manager.sync_from_backup()
        atexit.register(_atexit_checkpoint, db_path)
    return _db_manager
