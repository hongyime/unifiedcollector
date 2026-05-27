"""Account pool management with cooldown tracking.

Loads multiple cookie sets from sessions/ and manages rotation via account_cooldowns table.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from dataclasses import dataclass
from typing import List, Optional, Iterator

logger = logging.getLogger("uttk.accounts")


@dataclass
class Account:
    name: str
    cookies_path: Path
    is_active: bool = True
    is_cooldown: bool = False
    until_ts: float = 0.0

    def __post_init__(self):
        if isinstance(self.cookies_path, str):
            self.cookies_path = Path(self.cookies_path)


class AccountManager:
    """Manages a pool of TikTok accounts with cooldown support."""

    def __init__(
        self,
        sessions_dir: Path,
        db_path: Optional[Path] = None,
        cooldown_seconds: float = 600.0
    ):
        """
        Args:
            sessions_dir: Directory containing cookie files (e.g., sessions/account1_cookies.txt).
            db_path: SQLite database path for cooldown state (default: data/tiktok_toolkit.db).
            cooldown_seconds: Default cooldown duration in seconds.
        """
        self.sessions_dir = Path(sessions_dir) if sessions_dir else Path('sessions')
        self.db_path = Path(db_path) if db_path else Path('data/tiktok_toolkit.db')
        self.cooldown_seconds = cooldown_seconds
        self._accounts: List[Account] = []
        self._ensure_table()
        self._load_accounts()

    def _ensure_table(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS account_cooldowns (
                    account_name TEXT PRIMARY KEY,
                    until_ts REAL NOT NULL,
                    reason TEXT DEFAULT 'rate-limit',
                    created_at REAL DEFAULT (unixepoch())
                )
            """)

    def _load_accounts(self) -> None:
        """Scan sessions/ for *_cookies.txt files."""
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        accounts = []
        for f in self.sessions_dir.iterdir():
            if f.is_file() and '_cookies' in f.name and f.suffix == '.txt':
                name = f.name.split('_cookies')[0]
                accounts.append(Account(name=name, cookies_path=f))
        self._accounts = accounts
        logger.debug(f"Loaded {len(accounts)} accounts from {self.sessions_dir}")

    def list_available(self) -> List[Account]:
        """Return accounts not on cooldown."""
        now = time.time()
        available = []
        for acc in self._accounts:
            if not acc.is_active:
                continue
            if self._is_on_cooldown(acc.name, now):
                continue
            available.append(acc)
        return available

    def _is_on_cooldown(self, name: str, now: float) -> bool:
        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                cur = conn.execute("SELECT until_ts FROM account_cooldowns WHERE account_name=?", (name,))
                row = cur.fetchone()
                if row and row[0] > now:
                    return True
                if row and row[0] <= now:
                    conn.execute("DELETE FROM account_cooldowns WHERE account_name=?", (name,))
                    conn.commit()
        except Exception as e:
            logger.warning(f"Cooldown check failed for {name}: {e}")
        return False

    def set_cooldown(self, name: str, seconds: Optional[float] = None, reason: str = 'rate-limit') -> None:
        """Put account on cooldown for specified duration (default: cooldown_seconds)."""
        secs = seconds if seconds is not None else self.cooldown_seconds
        until = time.time() + secs
        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                conn.execute("""
                    INSERT INTO account_cooldowns (account_name, until_ts, reason, created_at)
                    VALUES (?,?,?,?)
                    ON CONFLICT(account_name) DO UPDATE SET until_ts=excluded.until_ts, reason=excluded.reason, created_at=excluded.created_at
                """, (name, until, reason, time.time()))
                conn.commit()
            logger.info(f"Account {name} on cooldown until {until} ({secs}s)")
        except Exception as e:
            logger.warning(f"Failed to set cooldown for {name}: {e}")

    def get_next(self) -> Optional[Account]:
        """Get next available account (round-robin with cooldown skip)."""
        available = self.list_available()
        if not available:
            return None
        return available[0]

    def mark_rate_limit(self, name: str) -> None:
        """Convenience: mark account as rate-limited (uses cooldown_seconds)."""
        self.set_cooldown(name, self.cooldown_seconds, reason='rate-limit')

    def __iter__(self) -> Iterator[Account]:
        return iter(self._accounts)

    def __len__(self) -> int:
        return len(self._accounts)


def create_account_manager_from_env(sessions_dir: Optional[Path] = None, db_path: Optional[Path] = None) -> AccountManager:
    """Factory using TIKTOK_ACCOUNT_COOLDOWN_SECONDS env."""
    import os
    cooldown = float(os.environ.get('TIKTOK_ACCOUNT_COOLDOWN_SECONDS', 600))
    sdir = sessions_dir or Path(os.environ.get('TIKTOK_SESSIONS_DIR', 'sessions'))
    db = db_path or Path(os.environ.get('TIKTOK_DB_PATH', 'data/tiktok_toolkit.db'))
    return AccountManager(sdir, db, cooldown_seconds=cooldown)
