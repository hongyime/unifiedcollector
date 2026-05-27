"""
state_manager.py — SQLite + JSON state persistence for SearchToolkit.

Provides persistent tracking of download progress, query history, and API usage.
Supports JSON backup for portability and automatic sync on startup.

Design inspired by FORGE's state_store.py but simplified for SearchToolkit needs.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set


@dataclass
class DownloadRecord:
    """Represents a downloaded file record."""
    url: str
    content_hash: str
    file_path: str
    query: str
    status: str  # pending, completed, failed, skipped
    created_at: str
    completed_at: Optional[str] = None


@dataclass
class QueryProgress:
    """Represents progress for a specific query/dork combination."""
    query: str
    dork: str
    status: str  # pending, in_progress, completed, failed
    total_urls: int
    downloaded: int
    failed: int
    started_at: str
    completed_at: Optional[str] = None


@dataclass
class ApiUsage:
    """Represents API usage statistics."""
    engine: str  # duckduckgo, bing, serper
    query: str
    results_count: int
    cost_credits: float
    created_at: str


class StateManager:
    """Manages persistent state for SearchToolkit using SQLite + JSON backup."""

    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        
        self.db_path = state_dir / "state.db"
        self.json_backup_path = state_dir / "state_backup.json"
        
        self._lock = threading.Lock()
        self._init_db()
        self._sync_from_json()  # Sync from JSON backup on startup

    def _init_db(self) -> None:
        """Initialize SQLite database with required schema."""
        with self._lock:
            conn = self._get_connection()
            try:
                conn.executescript("""
                    -- Download records table
                    CREATE TABLE IF NOT EXISTS downloads (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        url TEXT UNIQUE NOT NULL,
                        content_hash TEXT NOT NULL,
                        file_path TEXT,
                        query TEXT NOT NULL,
                        status TEXT DEFAULT 'pending',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        completed_at TIMESTAMP
                    );

                    -- Query progress table
                    CREATE TABLE IF NOT EXISTS query_progress (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        query TEXT NOT NULL,
                        dork TEXT NOT NULL,
                        status TEXT DEFAULT 'pending',
                        total_urls INTEGER DEFAULT 0,
                        downloaded INTEGER DEFAULT 0,
                        failed INTEGER DEFAULT 0,
                        started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        completed_at TIMESTAMP,
                        UNIQUE(query, dork)
                    );

                    -- API usage table
                    CREATE TABLE IF NOT EXISTS api_usage (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        engine TEXT NOT NULL,
                        query TEXT NOT NULL,
                        results_count INTEGER DEFAULT 0,
                        cost_credits REAL DEFAULT 0,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );

                    -- Indexes for performance
                    CREATE INDEX IF NOT EXISTS idx_downloads_url ON downloads(url);
                    CREATE INDEX IF NOT EXISTS idx_downloads_hash ON downloads(content_hash);
                    CREATE INDEX IF NOT EXISTS idx_downloads_query ON downloads(query);
                    CREATE INDEX IF NOT EXISTS idx_downloads_status ON downloads(status);
                    CREATE INDEX IF NOT EXISTS idx_query_progress_query ON query_progress(query);
                """)
                conn.commit()
            finally:
                conn.close()

    def _get_connection(self) -> sqlite3.Connection:
        """Get a database connection with WAL mode enabled."""
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.row_factory = sqlite3.Row
        return conn

    def _sync_from_json(self) -> None:
        """Sync from JSON backup if database is empty."""
        # Check if database is empty
        conn = self._get_connection()
        try:
            row = conn.execute("SELECT COUNT(*) as count FROM downloads").fetchone()
            if row["count"] > 0:
                return  # Database already has data, skip sync
        finally:
            conn.close()

        # Load from JSON backup if exists
        if not self.json_backup_path.exists():
            return

        try:
            with open(self.json_backup_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError):
            return

        conn = self._get_connection()
        try:
            # Insert downloads
            for record in data.get("downloads", []):
                try:
                    conn.execute(
                        """INSERT OR IGNORE INTO downloads 
                           (url, content_hash, file_path, query, status, created_at, completed_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (
                            record["url"],
                            record["content_hash"],
                            record.get("file_path"),
                            record["query"],
                            record["status"],
                            record["created_at"],
                            record.get("completed_at")
                        )
                    )
                except sqlite3.IntegrityError:
                    pass

            # Insert query progress
            for record in data.get("query_progress", []):
                try:
                    conn.execute(
                        """INSERT OR IGNORE INTO query_progress 
                           (query, dork, status, total_urls, downloaded, failed, started_at, completed_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            record["query"],
                            record["dork"],
                            record["status"],
                            record["total_urls"],
                            record["downloaded"],
                            record["failed"],
                            record["started_at"],
                            record.get("completed_at")
                        )
                    )
                except sqlite3.IntegrityError:
                    pass

            # Insert API usage
            for record in data.get("api_usage", []):
                conn.execute(
                    """INSERT INTO api_usage (engine, query, results_count, cost_credits, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        record["engine"],
                        record["query"],
                        record["results_count"],
                        record["cost_credits"],
                        record["created_at"]
                    )
                )

            conn.commit()
        finally:
            conn.close()

    def backup_to_json(self) -> None:
        """Backup all state to JSON file."""
        conn = self._get_connection()
        try:
            data = {
                "version": 1,
                "last_sync": datetime.now(timezone.utc).isoformat(),
                "downloads": [],
                "query_progress": [],
                "api_usage": []
            }

            # Export downloads
            for row in conn.execute("SELECT * FROM downloads"):
                data["downloads"].append(dict(row))

            # Export query progress
            for row in conn.execute("SELECT * FROM query_progress"):
                data["query_progress"].append(dict(row))

            # Export API usage
            for row in conn.execute("SELECT * FROM api_usage"):
                data["api_usage"].append(dict(row))

            # Atomic write
            temp_path = self.json_backup_path.with_suffix('.tmp')
            with open(temp_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
            temp_path.replace(self.json_backup_path)
        finally:
            conn.close()

    # Download tracking methods

    def is_downloaded(self, url: str) -> bool:
        """Check if a URL has already been downloaded."""
        conn = self._get_connection()
        try:
            row = conn.execute(
                "SELECT 1 FROM downloads WHERE url = ? AND status = 'completed'",
                (url,)
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    def mark_download_pending(self, url: str, query: str) -> None:
        """Mark a download as pending."""
        conn = self._get_connection()
        try:
            conn.execute(
                """INSERT OR IGNORE INTO downloads (url, content_hash, query, status, created_at)
                   VALUES (?, '', ?, 'pending', ?)""",
                (url, query, datetime.now(timezone.utc).isoformat())
            )
            conn.commit()
        finally:
            conn.close()

    def mark_download_complete(self, url: str, file_path: str, content_hash: str) -> None:
        """Mark a download as completed."""
        conn = self._get_connection()
        try:
            conn.execute(
                """UPDATE downloads 
                   SET status = 'completed', file_path = ?, content_hash = ?, completed_at = ?
                   WHERE url = ?""",
                (file_path, content_hash, datetime.now(timezone.utc).isoformat(), url)
            )
            conn.commit()
        finally:
            conn.close()

    def mark_download_failed(self, url: str) -> None:
        """Mark a download as failed."""
        conn = self._get_connection()
        try:
            conn.execute(
                "UPDATE downloads SET status = 'failed' WHERE url = ?",
                (url,)
            )
            conn.commit()
        finally:
            conn.close()

    def mark_download_skipped(self, url: str) -> None:
        """Mark a download as skipped."""
        conn = self._get_connection()
        try:
            conn.execute(
                "UPDATE downloads SET status = 'skipped' WHERE url = ?",
                (url,)
            )
            conn.commit()
        finally:
            conn.close()

    def get_pending_downloads(self) -> List[Dict[str, Any]]:
        """Get list of pending or failed downloads for resume."""
        conn = self._get_connection()
        try:
            rows = conn.execute(
                """SELECT url, query, status FROM downloads 
                   WHERE status IN ('pending', 'failed')
                   ORDER BY created_at"""
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    # Query progress methods

    def start_query(self, query: str, dork: str) -> None:
        """Mark a query as in_progress."""
        conn = self._get_connection()
        try:
            conn.execute(
                """INSERT OR REPLACE INTO query_progress 
                   (query, dork, status, total_urls, downloaded, failed, started_at)
                   VALUES (?, ?, 'in_progress', 0, 0, 0, ?)""",
                (query, dork, datetime.now(timezone.utc).isoformat())
            )
            conn.commit()
        finally:
            conn.close()

    def update_query_progress(self, query: str, dork: str, total: int, downloaded: int, failed: int) -> None:
        """Update query progress counters."""
        conn = self._get_connection()
        try:
            conn.execute(
                """UPDATE query_progress 
                   SET total_urls = ?, downloaded = ?, failed = ?
                   WHERE query = ? AND dork = ?""",
                (total, downloaded, failed, query, dork)
            )
            conn.commit()
        finally:
            conn.close()

    def complete_query(self, query: str, dork: str) -> None:
        """Mark a query as completed."""
        conn = self._get_connection()
        try:
            conn.execute(
                """UPDATE query_progress 
                   SET status = 'completed', completed_at = ?
                   WHERE query = ? AND dork = ?""",
                (datetime.now(timezone.utc).isoformat(), query, dork)
            )
            conn.commit()
        finally:
            conn.close()

    # API usage methods

    def record_api_usage(self, engine: str, query: str, results_count: int, cost_credits: float = 0.0) -> None:
        """Record API usage."""
        conn = self._get_connection()
        try:
            conn.execute(
                """INSERT INTO api_usage (engine, query, results_count, cost_credits, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (engine, query, results_count, cost_credits, datetime.now(timezone.utc).isoformat())
            )
            conn.commit()
        finally:
            conn.close()

    def get_api_usage_summary(self) -> Dict[str, Any]:
        """Get summary of API usage."""
        conn = self._get_connection()
        try:
            summary = {}
            for row in conn.execute(
                """SELECT engine, COUNT(*) as calls, SUM(results_count) as total_results, 
                          SUM(cost_credits) as total_cost
                   FROM api_usage
                   GROUP BY engine"""
            ):
                summary[row["engine"]] = {
                    "calls": row["calls"],
                    "total_results": row["total_results"],
                    "total_cost": row["total_cost"]
                }
            return summary
        finally:
            conn.close()

    # Statistics methods

    def get_stats(self) -> Dict[str, Any]:
        """Get overall statistics."""
        conn = self._get_connection()
        try:
            stats = {}
            
            # Download stats
            stats["downloads"] = {}
            for status in ['completed', 'failed', 'skipped', 'pending']:
                row = conn.execute(
                    "SELECT COUNT(*) as count FROM downloads WHERE status = ?",
                    (status,)
                ).fetchone()
                stats["downloads"][status] = row["count"]
            
            # Query stats
            stats["queries"] = {}
            for status in ['completed', 'failed', 'in_progress', 'pending']:
                row = conn.execute(
                    "SELECT COUNT(*) as count FROM query_progress WHERE status = ?",
                    (status,)
                ).fetchone()
                stats["queries"][status] = row["count"]
            
            return stats
        finally:
            conn.close()

    def clear_all(self) -> None:
        """Clear all state (use with caution)."""
        conn = self._get_connection()
        try:
            conn.execute("DELETE FROM downloads")
            conn.execute("DELETE FROM query_progress")
            conn.execute("DELETE FROM api_usage")
            conn.commit()
        finally:
            conn.close()

