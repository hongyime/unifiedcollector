"""
Username Database - Structured storage for usernames with source account tracking.

This module provides the UsernameDatabase class for managing Instagram usernames
with metadata about which account scraped which users, enabling targeted re-scraping
and intelligent account selection.

Persistence is delegated to UsernameRepository (SQLite/PostgreSQL).
"""

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional

_LIB_DIR = os.path.dirname(os.path.abspath(__file__))
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from src.config import INSTAGRAM_ACCOUNTS, DATA_DIR


# Instagram username validation pattern
INSTAGRAM_USERNAME_PATTERN = re.compile(r'^[a-zA-Z0-9._]+$')


def is_valid_instagram_username(username: str) -> bool:
    """Validate Instagram username format."""
    if not username or not isinstance(username, str):
        return False
    return bool(INSTAGRAM_USERNAME_PATTERN.match(username))


@dataclass
class UsernameRecord:
    """Data model for a username with source account tracking and metadata."""
    username: str
    source_account: str
    added_timestamp: float
    added_datetime: str
    last_accessed: Optional[float] = None
    metadata: dict = field(default_factory=dict)
    following_status: dict = field(default_factory=dict)

    def __post_init__(self):
        if not is_valid_instagram_username(self.username):
            raise ValueError(f"Invalid Instagram username format: {self.username}")
        account_names = [acc['name'] for acc in INSTAGRAM_ACCOUNTS]
        if self.source_account not in account_names and self.source_account not in ("migrated", "collected", "unknown"):
            raise ValueError(
                f"Invalid source account: {self.source_account}. "
                f"Available accounts: {', '.join(account_names)}"
            )
        if self.added_timestamp <= 0:
            raise ValueError(f"Invalid timestamp: {self.added_timestamp}")
        if not isinstance(self.metadata, dict):
            raise ValueError("Metadata must be a dictionary")
        if not isinstance(self.following_status, dict):
            raise ValueError("Following status must be a dictionary")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> 'UsernameRecord':
        return cls(**data)


def _get_db():
    """Return a module-level DatabaseManager singleton."""
    import os as _os
    from db.manager import DatabaseManager
    if not hasattr(_get_db, "_instance") or _get_db._instance is None:
        _get_db._instance = DatabaseManager(_os.environ.get("DATABASE_URL", ""))
    return _get_db._instance


_get_db._instance = None


class UsernameDatabase:
    """Structured storage for Instagram usernames with source account tracking.

    All persistence is delegated to UsernameRepository.
    The UsernameRecord dataclass and all public method signatures are preserved.
    """

    def __init__(self, db_path: Optional[str] = None):
        """Initialize username database.

        Args:
            db_path: Ignored (kept for backward compat). DB path is controlled by DATABASE_URL.
        """
        self.db_path = db_path or os.path.join(DATA_DIR, "username_database.json")
        from db.repositories.username_repository import UsernameRepository
        self._repo = UsernameRepository(_get_db())

        # In-memory caches for backward compat
        self._usernames: dict[str, UsernameRecord] = {}
        self._source_account_index: dict[str, list[str]] = {}
        self._loaded = False

    def _ensure_loaded(self):
        """Lazily populate in-memory caches from DB."""
        if self._loaded:
            return
        self._loaded = True
        rows = self._repo.get_all()
        for row in rows:
            try:
                record = self._row_to_record(row)
                self._usernames[record.username] = record
                src = record.source_account
                self._source_account_index.setdefault(src, []).append(record.username)
            except Exception:
                pass

    def _row_to_record(self, row: dict) -> UsernameRecord:
        """Convert a DB row dict to a UsernameRecord."""
        meta = json.loads(row.get("metadata_json") or "{}")
        # Fetch following status
        following_status: dict[str, bool] = {}
        try:
            fs_rows = _get_db().fetchall(
                "SELECT account_name, is_following FROM username_following_status WHERE username=?",
                (row["username"],),
            )
            for fs in fs_rows:
                following_status[fs["account_name"]] = bool(fs["is_following"])
        except Exception:
            pass

        # Determine source_account — fall back to "migrated" if not in known accounts
        source = row.get("source_account", "migrated")
        account_names = [acc['name'] for acc in INSTAGRAM_ACCOUNTS]
        if source not in account_names:
            source = "migrated"

        return UsernameRecord(
            username=row["username"],
            source_account=source,
            added_timestamp=float(row.get("added_ts") or time.time()),
            added_datetime=datetime.fromtimestamp(float(row.get("added_ts") or time.time())).isoformat(),
            last_accessed=row.get("last_accessed_ts"),
            metadata=meta,
            following_status=following_status,
        )

    # ── Public API (unchanged signatures) ─────────────────────────────────

    def add_username(
        self,
        username: str,
        source_account: str,
        metadata: Optional[dict] = None,
    ) -> bool:
        """Add a username to the database. Returns True if added, False if duplicate."""
        if not is_valid_instagram_username(username):
            print(f"[ERROR] Failed to add username {username}: Invalid Instagram username format: {username}")
            return False

        # Validate source account
        account_names = [acc['name'] for acc in INSTAGRAM_ACCOUNTS]
        if source_account not in account_names and source_account not in ("migrated", "collected", "unknown"):
            print(f"[ERROR] Failed to add username {username}: Invalid source account: {source_account}")
            return False

        added = self._repo.add_username(username, source_account, metadata)
        if added:
            # Update in-memory cache
            current_time = time.time()
            try:
                record = UsernameRecord(
                    username=username,
                    source_account=source_account,
                    added_timestamp=current_time,
                    added_datetime=datetime.now().isoformat(),
                    metadata=metadata or {},
                )
                self._usernames[username] = record
                self._source_account_index.setdefault(source_account, []).append(username)
            except Exception:
                pass
        return added

    def get_usernames_by_source(self, source_account: str) -> list:
        """Retrieve all usernames scraped by a specific account."""
        rows = self._repo.get_by_source(source_account)
        records = []
        for row in rows:
            try:
                records.append(self._row_to_record(row))
            except Exception:
                pass
        records.sort(key=lambda r: r.added_timestamp)
        return records

    def get_all_usernames(self) -> list:
        """Retrieve all username records."""
        rows = self._repo.get_all()
        records = []
        for row in rows:
            try:
                records.append(self._row_to_record(row))
            except Exception:
                pass
        records.sort(key=lambda r: r.added_timestamp)
        return records

    def get_username_record(self, username: str) -> Optional[UsernameRecord]:
        """Get a single username record."""
        row = _get_db().fetchone("SELECT * FROM usernames WHERE username=?", (username,))
        if row is None:
            return None
        try:
            return self._row_to_record(row)
        except Exception:
            return None

    def update_metadata(self, username: str, metadata: dict) -> bool:
        """Update metadata for a username (merges with existing metadata)."""
        try:
            json.dumps(metadata)
        except (TypeError, ValueError) as e:
            print(f"[ERROR] Metadata is not JSON-serializable for {username}: {e}")
            return False
        return self._repo.update_metadata(username, metadata)

    def update_last_accessed(self, username: str, timestamp: Optional[float] = None) -> bool:
        """Update the last_accessed timestamp for a username."""
        if timestamp is not None:
            _get_db().execute(
                "UPDATE usernames SET last_accessed_ts=? WHERE username=?",
                (timestamp, username),
            )
            row = _get_db().fetchone("SELECT 1 FROM usernames WHERE username=?", (username,))
            return row is not None
        return self._repo.update_last_accessed(username)

    def update_following_status(self, username: str, account_name: str, is_following: bool) -> bool:
        """Update the following status for a specific account-username pair."""
        return self._repo.update_following_status(username, account_name, is_following)

    def remove_username(self, username: str) -> bool:
        """Remove a username from the database."""
        self.create_backup()
        return self._repo.remove(username)

    def save(self, max_retries: int = 3) -> bool:
        """No-op: data is persisted immediately via the repository."""
        return True

    def load(self) -> bool:
        """No-op: data is loaded from DB on demand."""
        self._loaded = False
        return True

    def _load_from_backup(self) -> bool:
        return False

    def _rebuild_index(self):
        self._source_account_index = {}
        for username, record in self._usernames.items():
            src = record.source_account
            self._source_account_index.setdefault(src, []).append(username)

    def create_backup(self) -> bool:
        """No-op in DB mode."""
        return True

    def migrate_from_flat_file(self, filepath: str, default_source: str) -> dict:
        """Migrate usernames from flat file to structured database."""
        import shutil
        backup_path = f"{filepath}.backup.{int(time.time())}"
        try:
            shutil.copy2(filepath, backup_path)
        except Exception as e:
            return {"error": str(e)}

        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                lines = f.readlines()
        except Exception as e:
            return {"error": str(e)}

        stats = {"added": 0, "skipped": 0, "duplicates": 0, "invalid": 0}
        for line in lines:
            username = line.strip()
            if not username:
                stats["skipped"] += 1
                continue
            if not is_valid_instagram_username(username):
                stats["invalid"] += 1
                continue
            success = self.add_username(
                username=username,
                source_account=default_source,
                metadata={"migrated_from_flat_file": True, "migration_timestamp": time.time()},
            )
            if success:
                stats["added"] += 1
            else:
                stats["duplicates"] += 1

        return {
            "total_lines": len(lines),
            "backup_path": backup_path,
            "statistics": stats,
            "migration_timestamp": datetime.now().isoformat(),
        }

    def export_to_flat_file(self, filepath: str) -> int:
        """Export all usernames to flat file format (one per line)."""
        try:
            records = self.get_all_usernames()
            with open(filepath, 'w', encoding='utf-8') as f:
                for record in records:
                    f.write(f"{record.username}\n")
            return len(records)
        except Exception as e:
            print(f"[ERROR] Failed to export to flat file: {e}")
            return 0


