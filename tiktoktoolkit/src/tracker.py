"""Download tracking backends (JSON + SQLite) with file locking.

Primary storage: SQLite database for scalability.
Secondary backup: JSON snapshot (optional) for easier manual inspection.

Provides duplicate prevention even if media files are moved outside the
original output directory. Supports importing an existing directory tree.
"""

from __future__ import annotations

import atexit
import json
import sqlite3
import hashlib
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, Optional, Any, Iterable, Tuple, List, Protocol
from datetime import datetime, timezone
import logging
import re
import shutil

logger = logging.getLogger("uttk.tracker")

VIDEO_ID_RE = re.compile(r'(\d{6,})')  # broad numeric id capture
# Migration removed - DB already migrated to data/tiktok_toolkit.db
DEFAULT_DB_PATH = Path(os.environ.get('TIKTOK_DB_PATH', 'data/tiktok_toolkit.db'))
DEFAULT_JSON_BACKUP_PATH = Path(os.environ.get('TIKTOK_TRACKER_JSON_BACKUP', 'configs/download_tracker.json.backup'))


def _pid_alive(pid: int) -> bool:
    """Return True if the given PID is still running (cross-platform)."""
    if sys.platform == 'win32':
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except Exception:
            return True  # unknown — assume alive to be safe


# Migration removed - DB already migrated to data/tiktok_toolkit.db


# ---------------- File Lock (cross-process) ---------------- #
class FileLock:
    """Simple cross-process lock using lockfile creation with stale-lock detection."""
    def __init__(self, lock_path: Path, timeout: float = 30.0, poll: float = 0.1):
        self.lock_path = lock_path
        self.timeout = timeout
        self.poll = poll
        self._fd: Optional[int] = None

    def acquire(self):
        start = time.time()
        while True:
            try:
                # O_CREAT|O_EXCL ensures exclusive create or raises FileExistsError
                self._fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
                os.write(self._fd, str(os.getpid()).encode())
                return
            except FileExistsError:
                # Check for stale lock left by a crashed process
                try:
                    raw = self.lock_path.read_text(encoding='utf-8').strip()
                    pid = int(raw)
                    if not _pid_alive(pid):
                        logger.debug(f"Removing stale lock from dead PID {pid}: {self.lock_path}")
                        self.lock_path.unlink(missing_ok=True)
                        continue  # retry immediately
                except Exception:
                    pass  # can't read/parse PID — fall through to normal timeout
                if time.time() - start > self.timeout:
                    raise TimeoutError(f"Timeout acquiring lock {self.lock_path}")
                time.sleep(self.poll)
            except Exception as e:
                logger.debug(f"Lock acquire transient error: {e}")
                time.sleep(self.poll)

    def release(self):
        if self._fd is not None:
            try:
                os.close(self._fd)
            finally:
                self._fd = None
        try:
            if self.lock_path.exists():
                self.lock_path.unlink()
        except Exception:
            pass

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()


# ---------------- Tracker Protocol ---------------- #
class TrackerBackend(Protocol):
    def is_downloaded(self, username: str, video_id: str) -> bool: ...
    def is_downloaded_in_folder(self, username: str, video_id: str, target_dir: str) -> bool: ...
    def mark_downloaded(self, username: str, video_id: str, filepath: Optional[str] = None,
                        size: Optional[int] = None, source: str = "download", extra: Optional[Dict[str, Any]] = None) -> None: ...
    def count_for_user(self, username: str) -> int: ...
    def import_directory(self, root: Path, assume_username: Optional[str] = None, source: str = "import") -> int: ...
    def close(self) -> None: ...


# ---------------- JSON Backup Backend ---------------- #
class JSONBackup:
    CURRENT_VERSION = 1

    def __init__(self, path: Path):
        self.path = path
        self.data: Dict[str, Any] = {"version": self.CURRENT_VERSION, "users": {}}
        self._load()

    def _load(self):
        try:
            if self.path.exists():
                raw = json.loads(self.path.read_text(encoding='utf-8'))
                if isinstance(raw, dict) and 'users' in raw:
                    self.data = raw
        except Exception as e:
            logger.warning(f"JSON backup load failed: {e}")

    def update_entry(self, username: str, video_id: str, filepath: Optional[str], size: Optional[int]):
        users = self.data.setdefault('users', {})
        user_map = users.setdefault(username, {})
        now = datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
        entry = user_map.get(video_id)
        if entry:
            entry['last_seen'] = now
            if filepath:
                entry['filepath'] = filepath
            if size is not None:
                entry['size'] = size
        else:
            user_map[video_id] = {
                'first_downloaded': now,
                'last_seen': now,
                'filepath': filepath,
                'size': size
            }

    def flush(self):
        """Flush in-memory data to JSON file with atomic write.

        Uses a temporary file and atomic replace to prevent corruption.
        Ensures temp files are always cleaned up even on failure.
        """
        tmp_path = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(prefix='tracker_json_', dir=str(self.path.parent))
            os.close(fd)
            tmp_path = Path(tmp_name)

            try:
                with tmp_path.open('w', encoding='utf-8') as f:
                    json.dump(self.data, f, ensure_ascii=False, indent=2)

                if self.path.exists():
                    try:
                        self.path.unlink()
                    except PermissionError:
                        time.sleep(0.1)
                        self.path.unlink()

                tmp_path.replace(self.path)
                tmp_path = None

            finally:
                if tmp_path is not None and tmp_path.exists():
                    try:
                        tmp_path.unlink()
                    except Exception as cleanup_error:
                        logger.debug(f"Could not clean up temp file {tmp_path}: {cleanup_error}")

        except Exception as e:
            logger.warning(f"Failed to write JSON backup: {e}")


# ---------------- SQLite Backend ---------------- #
class SQLiteDownloadTracker(TrackerBackend):
    # Class-level connection tracking for atexit cleanup across all instances/threads
    _all_connections: list = []
    _conns_lock = threading.Lock()
    _atexit_registered = False
    _atexit_registered_lock = threading.Lock()

    def __init__(self, db_path: Path, json_backup: Optional[JSONBackup] = None,
                 compute_hash: bool = False, hash_algorithm: str = 'sha256'):
        self.db_path = db_path
        self.json_backup = json_backup
        self._backup_lock = threading.Lock()
        self.compute_hash = compute_hash
        self.hash_algorithm = hash_algorithm.lower()
        self._tls = threading.local()
        self._shutdown = False

        with SQLiteDownloadTracker._atexit_registered_lock:
            if not SQLiteDownloadTracker._atexit_registered:
                atexit.register(SQLiteDownloadTracker._atexit_handler)
                SQLiteDownloadTracker._atexit_registered = True

        self._ensure_schema()

    @classmethod
    def _atexit_handler(cls) -> None:
        """Checkpoint WAL and close all connections on process exit."""
        with cls._conns_lock:
            conns = list(cls._all_connections)
            cls._all_connections.clear()
        for conn in conns:
            try:
                conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                conn.close()
            except Exception:
                pass

    def _get_conn(self) -> sqlite3.Connection:
        """Return this thread's cached SQLite connection, creating it if needed."""
        conn = getattr(self._tls, 'conn', None)
        if conn is not None:
            return conn
        conn = sqlite3.connect(str(self.db_path), timeout=5.0, check_same_thread=True)
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        self._tls.conn = conn
        with SQLiteDownloadTracker._conns_lock:
            SQLiteDownloadTracker._all_connections.append(conn)
        return conn

    def _ensure_schema(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._get_conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS videos (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  username TEXT NOT NULL,
                  video_id TEXT NOT NULL,
                  first_downloaded TEXT NOT NULL,
                  last_seen TEXT NOT NULL,
                  filepath TEXT,
                  size INTEGER,
                  hash TEXT,
                  source TEXT,
                  extra JSON,
                  UNIQUE(username, video_id)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_videos_user ON videos(username);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_videos_vid ON videos(video_id);")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS profiles (
                    username TEXT PRIMARY KEY,
                    user_id TEXT UNIQUE,
                    display_name TEXT,
                    profile_pic_url TEXT,
                    profile_pic_phash TEXT,
                    followers_count INTEGER DEFAULT 0,
                    following_count INTEGER DEFAULT 0,
                    video_count INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'active',
                    spider_status TEXT DEFAULT 'pending',
                    download_status TEXT DEFAULT 'pending',
                    filter_reason TEXT,
                    last_scraped_ts REAL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_profiles_spider ON profiles(spider_status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_profiles_download ON profiles(download_status)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS profile_photo_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL,
                    user_id TEXT,
                    photo_url TEXT NOT NULL,
                    photo_phash TEXT NOT NULL,
                    photo_blob BLOB,
                    file_path TEXT,
                    detected_at REAL DEFAULT (unixepoch())
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_photo_history_user ON profile_photo_history(username, detected_at DESC)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS account_cooldowns (
                    account_name TEXT PRIMARY KEY,
                    until_ts REAL NOT NULL,
                    reason TEXT DEFAULT 'rate-limit',
                    created_at REAL DEFAULT (unixepoch())
                )
            """)

        self._sync_from_backup_if_needed()

    def _sync_from_backup_if_needed(self):
        """Restore database from JSON backup if DB is empty but JSON has data."""
        if not self.json_backup or not self.json_backup.path.exists():
            return

        try:
            with self._get_conn() as conn:
                cur = conn.execute("SELECT COUNT(*) FROM videos")
                count = cur.fetchone()[0]

                if count == 0 and self.json_backup.data.get('users'):
                    logger.info("SQLite database is empty. Restoring from JSON backup...")
                    users = self.json_backup.data.get('users', {})
                    restored = 0

                    for username, videos in users.items():
                        for video_id, data in videos.items():
                            conn.execute("""
                                INSERT INTO videos (username, video_id, first_downloaded, last_seen, filepath, size, source)
                                VALUES (?,?,?,?,?,?,?)
                            """, (
                                username,
                                video_id,
                                data.get('first_downloaded', datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')),
                                data.get('last_seen', datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')),
                                data.get('filepath'),
                                data.get('size'),
                                'json_restore'
                            ))
                            restored += 1

                    conn.commit()
                    logger.info(f"Successfully restored {restored} records from JSON backup.")
        except Exception as e:
            logger.error(f"Failed to sync from JSON backup: {e}")

    # ------------- Public API ------------- #
    def is_downloaded(self, username: str, video_id: str) -> bool:
        with self._get_conn() as conn:
            cur = conn.execute("SELECT 1 FROM videos WHERE username=? AND video_id=? LIMIT 1", (username, video_id))
            return cur.fetchone() is not None

    def is_downloaded_in_folder(self, username: str, video_id: str, target_dir: str) -> bool:
        """Check if a video was downloaded AND its recorded filepath is within the target_dir."""
        with self._get_conn() as conn:
            cur = conn.execute("SELECT filepath FROM videos WHERE username=? AND video_id=? LIMIT 1", (username, video_id))
            row = cur.fetchone()
            if not row or not row[0]:
                return False

            try:
                stored_path = Path(row[0]).resolve()
                target_path = Path(target_dir).resolve()
                return stored_path == target_path or target_path in stored_path.parents
            except Exception:
                stored = str(row[0]).replace('\\', '/').rstrip('/') + '/'
                target = str(target_dir).replace('\\', '/').rstrip('/') + '/'
                return stored.startswith(target)

    def count_for_user(self, username: str) -> int:
        with self._get_conn() as conn:
            cur = conn.execute("SELECT COUNT(*) FROM videos WHERE username=?", (username,))
            row = cur.fetchone()
            return int(row[0]) if row else 0

    def vacuum(self) -> None:
        """Perform database maintenance (VACUUM and ANALYZE)."""
        with self._get_conn() as conn:
            conn.execute("VACUUM;")
            conn.execute("ANALYZE;")
        logger.info("SQLite tracker vacuumed and analyzed.")

    def mark_downloaded(self, username: str, video_id: str, filepath: Optional[str] = None,
                        size: Optional[int] = None, source: str = "download", extra: Optional[Dict[str, Any]] = None) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
        file_hash = None
        if self.compute_hash and filepath:
            file_hash = self._maybe_hash(Path(filepath))
        with self._get_conn() as conn:
            # Atomic upsert — no TOCTOU race between processes.
            # ON CONFLICT preserves first_downloaded, source, extra; merges filepath/size/hash.
            conn.execute("""
                INSERT INTO videos (username, video_id, first_downloaded, last_seen, filepath, size, hash, source, extra)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(username, video_id) DO UPDATE SET
                    last_seen=excluded.last_seen,
                    filepath=COALESCE(excluded.filepath, videos.filepath),
                    size=COALESCE(excluded.size, videos.size),
                    hash=COALESCE(excluded.hash, videos.hash)
            """, (username, video_id, now, now, filepath, size, file_hash, source, json.dumps(extra) if extra else None))
        if self.json_backup:
            with self._backup_lock:
                self.json_backup.update_entry(username, video_id, filepath, size)
                self.json_backup.flush()

    def import_directory(self, root: Path, assume_username: Optional[str] = None, source: str = "import") -> int:
        """Scan a directory tree for video files and register them."""
        root = root.resolve()
        if not root.exists():
            logger.warning(f"Import root does not exist: {root}")
            return 0
        additions = 0
        for path in root.rglob('*'):
            if not path.is_file():
                continue
            if path.suffix.lower() not in {'.mp4', '.mov', '.avi', '.webm', '.mkv'}:
                continue
            username = assume_username
            if not username:
                for parent in path.parents:
                    if parent.name.startswith('username_'):
                        username = parent.name[len('username_'):]
                        break
            if not username:
                continue
            match = VIDEO_ID_RE.search(path.name)
            if not match:
                continue
            vid = match.group(1)
            if not self.is_downloaded(username, vid):
                size = None
                try:
                    size = path.stat().st_size
                except Exception:
                    pass
                self.mark_downloaded(username, vid, str(path), size, source=source)
                additions += 1
        logger.info(f"Imported {additions} existing files from {root}")
        return additions

    def close(self) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        conn = getattr(self._tls, 'conn', None)
        if conn is not None:
            try:
                conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                conn.close()
            except Exception:
                pass
            self._tls.conn = None
            with SQLiteDownloadTracker._conns_lock:
                try:
                    SQLiteDownloadTracker._all_connections.remove(conn)
                except ValueError:
                    pass

    def _maybe_hash(self, path: Path) -> Optional[str]:
        try:
            if not path.exists() or not path.is_file():
                return None
            h = hashlib.new(self.hash_algorithm)
            with path.open('rb') as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b''):
                    h.update(chunk)
            return h.hexdigest()
        except Exception as e:
            logger.debug(f"Hash compute failed for {path}: {e}")
            return None

    def backfill_hashes(self, limit: Optional[int] = None) -> int:
        """Compute hashes for rows missing them. Returns number updated."""
        updated = 0
        if not self.compute_hash:
            logger.info("Hash computation disabled; enable compute_hash to backfill")
            return 0
        with self._get_conn() as conn:
            q = "SELECT username, video_id, filepath FROM videos WHERE hash IS NULL AND filepath IS NOT NULL"
            if limit:
                q += f" LIMIT {int(limit)}"
            rows = conn.execute(q).fetchall()
            for username, video_id, filepath in rows:
                hp = self._maybe_hash(Path(filepath))
                if hp:
                    conn.execute("UPDATE videos SET hash=?, last_seen=? WHERE username=? AND video_id=?", (
                        hp, datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z'), username, video_id
                    ))
                    updated += 1
        logger.info(f"Backfilled {updated} hashes")
        return updated


# ---------------- Composite Wrapper (preferred external interface) ---------------- #
class CompositeTracker:
    def __init__(self, primary: TrackerBackend):
        self.primary = primary

    def is_downloaded(self, username: str, video_id: str) -> bool:
        return self.primary.is_downloaded(username, video_id)

    def is_downloaded_in_folder(self, username: str, video_id: str, target_dir: str) -> bool:
        return self.primary.is_downloaded_in_folder(username, video_id, target_dir)

    def count_for_user(self, username: str) -> int:
        return self.primary.count_for_user(username)

    def mark_downloaded(self, username: str, video_id: str, filepath: Optional[str] = None,
                        size: Optional[int] = None, source: str = "download", extra: Optional[Dict[str, Any]] = None):
        self.primary.mark_downloaded(username, video_id, filepath, size, source, extra)

    def import_directory(self, root: Path, assume_username: Optional[str] = None, source: str = "import") -> int:
        return self.primary.import_directory(root, assume_username, source)

    def vacuum(self) -> None:
        if hasattr(self.primary, 'vacuum'):
            self.primary.vacuum()

    def close(self):
        self.primary.close()


DownloadTracker = CompositeTracker


def create_tracker(sqlite_path: Path = DEFAULT_DB_PATH, json_backup_path: Optional[Path] = DEFAULT_JSON_BACKUP_PATH,
                   compute_hash: bool = False, hash_algorithm: str = 'sha256') -> DownloadTracker:
    backup = JSONBackup(json_backup_path) if json_backup_path else None
    primary = SQLiteDownloadTracker(sqlite_path, json_backup=backup, compute_hash=compute_hash, hash_algorithm=hash_algorithm)
    return CompositeTracker(primary)
