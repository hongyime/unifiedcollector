"""Profile photo change detection using URL + perceptual hash (pHash).

Two-stage detection:
1. Compare URL for quick changed/unchanged verdict
2. If URL changed, compute pHash of downloaded image to confirm visual change
"""

from __future__ import annotations

import logging
import sqlite3
import hashlib
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, Tuple, List
from datetime import datetime, timezone

logger = logging.getLogger("uttk.profile_photo")


@dataclass
class PhotoRecord:
    username: str
    url: str
    phash: str
    file_path: Optional[str] = None


def compute_phash(image_path: Path, hash_size: int = 8) -> Optional[str]:
    """Compute perceptual hash (pHash) of an image.

    Falls back to simple average hash if image processing unavailable.
    Returns hex string or None on error.
    """
    try:
        try:
            from PIL import Image
        except ImportError:
            logger.debug("PIL not available; cannot compute pHash")
            return None

        with Image.open(image_path) as img:
            if img.mode != 'L':
                img = img.convert('L')
            img = img.resize((hash_size, hash_size), Image.Resampling.LANCZOS)
            pixels = list(img.getdata())
            avg = sum(pixels) / len(pixels)
            bits = ''.join('1' if p >= avg else '0' for p in pixels)
            hex_hash = hex(int(bits, 2))[2:].rjust(hash_size * hash_size // 4, '0')
            return hex_hash
    except Exception as e:
        logger.debug(f"pHash compute failed for {image_path}: {e}")
        return None


def hamming_distance(hash1: str, hash2: str) -> int:
    """Compute Hamming distance between two hex hashes."""
    try:
        b1 = int(hash1, 16)
        b2 = int(hash2, 16)
        x = b1 ^ b2
        return bin(x).count('1')
    except Exception:
        return 999


class ProfilePhotoTracker:
    """Tracks profile photo changes per user."""

    PHASH_THRESHOLD = 5  # Hamming distance below this = same photo

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(self.db_path)) as conn:
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

    def get_current(self, username: str) -> Optional[PhotoRecord]:
        """Get current known photo for user."""
        with sqlite3.connect(str(self.db_path)) as conn:
            cur = conn.execute(
                "SELECT username, profile_pic_url, profile_pic_phash FROM profiles WHERE username=?",
                (username,)
            )
            row = cur.fetchone()
        if row:
            return PhotoRecord(username=row[0], url=row[1] or '', phash=row[2] or '')
        return None

    def get_latest_history(self, username: str) -> Optional[PhotoRecord]:
        """Get most recent history entry for user."""
        with sqlite3.connect(str(self.db_path)) as conn:
            cur = conn.execute(
                """
                SELECT username, photo_url, photo_phash, file_path
                FROM profile_photo_history
                WHERE username=? ORDER BY detected_at DESC LIMIT 1
                """,
                (username,)
            )
            row = cur.fetchone()
        if row:
            return PhotoRecord(username=row[0], url=row[1], phash=row[2], file_path=row[3])
        return None

    def is_changed(self, username: str, new_url: str) -> bool:
        """Quick check: URL changed?"""
        current = self.get_current(username)
        if current is None:
            return True
        return current.url != new_url

    def confirm_change_by_phash(self, username: str, image_path: Path) -> Tuple[bool, Optional[str]]:
        """Compare pHash of downloaded image to last known; return (changed, phash)."""
        phash = compute_phash(image_path)
        if phash is None:
            return True, None  # cannot verify, assume changed
        prev = self.get_latest_history(username)
        if prev is None or not prev.phash:
            return True, phash
        dist = hamming_distance(phash, prev.phash)
        changed = dist > self.PHASH_THRESHOLD
        return changed, phash

    def record(
        self,
        username: str,
        url: str,
        phash: str,
        file_path: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> None:
        """Record detected photo to history and update profiles."""
        now = datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("""
                INSERT INTO profile_photo_history (username, user_id, photo_url, photo_phash, file_path, detected_at)
                VALUES (?,?,?,?,?,?)
            """, (username, user_id, url, phash, file_path, now))
            conn.execute("""
                INSERT INTO profiles (username, user_id, profile_pic_url, profile_pic_phash, last_scraped_ts)
                VALUES (?,?,?,?,?)
                ON CONFLICT(username) DO UPDATE SET
                    user_id=COALESCE(excluded.user_id, user_id),
                    profile_pic_url=excluded.profile_pic_url,
                    profile_pic_phash=excluded.profile_pic_phash,
                    last_scraped_ts=excluded.last_scraped_ts
            """, (username, user_id, url, phash, now))
            conn.commit()

    def get_change_candidates(self, limit: int = 100) -> List[str]:
        """Return usernames whose stored URL differs from last history (or no history)."""
        candidates = []
        with sqlite3.connect(str(self.db_path)) as conn:
            cur = conn.execute(
                """
                SELECT p.username FROM profiles p
                LEFT JOIN (
                    SELECT username, photo_url, MAX(detected_at) AS max_ts
                    FROM profile_photo_history GROUP BY username
                ) h ON p.username = h.username
                WHERE p.profile_pic_url IS NOT NULL
                  AND (h.photo_url IS NULL OR h.photo_url != p.profile_pic_url)
                LIMIT ?
                """,
                (limit,)
            )
            for (username,) in cur.fetchall():
                candidates.append(username)
        return candidates
