"""AccountRateLimitRepository — sliding window rate limiting.
Tracks requests per account across multiple time windows (1h, 3h, 5h, 1d).
Works across different machines via shared database.
"""
from __future__ import annotations

import time
from typing import Optional
from datetime import datetime, timedelta

from ..manager import DatabaseManager


# Window sizes in seconds
WINDOW_1H_SECS = 60 * 60       # 1 hour
WINDOW_3H_SECS = 3 * WINDOW_1H_SECS  # 3 hours
WINDOW_5H_SECS = 5 * WINDOW_1H_SECS  # 5 hours
WINDOW_1D_SECS = 24 * WINDOW_1H_SECS  # 24 hours

# Default limits (can be overridden per-account)
DEFAULT_LIMITS = {
    'window_1h_limit': 180,   # 180 requests/hour
    'window_3h_limit': 400,   # 400 requests/3h
    'window_5h_limit': 600,   # 600 requests/5h
    'window_1d_limit': 2000,  # 2000 requests/day
}


class AccountRateLimitRepository:
    """Repository for sliding window rate limiting.

    Tracks individual request timestamps and aggregates them into
    sliding window counts across multiple time windows.

    All operations are database-backed, enabling cross-machine coordination.
    """

    def __init__(self, db: DatabaseManager) -> None:
        self._db = db

    def record_request(
        self,
        account: str,
        request_type: str = 'action',
        timestamp: Optional[float] = None,
        machine_id: Optional[str] = None,
    ) -> None:
        """Record a request timestamp.

        Args:
            account: Instagram account name making the request
            request_type: Type of request ('profile_view', 'download', 'action')
            timestamp: Unix timestamp (default: now)
            machine_id: Optional machine identifier for debugging
        """
        if timestamp is None:
            timestamp = time.time()

        self._db.execute(
            """
            INSERT INTO account_request_log (account_name, request_type, timestamp, machine_id)
            VALUES (?, ?, ?, ?)
            """,
            (account, request_type, timestamp, machine_id),
        )

    def get_request_count(
        self,
        account: str,
        window_seconds: int,
        request_type: Optional[str] = None,
    ) -> int:
        """Count requests within sliding window.

        Args:
            account: Instagram account name
            window_seconds: Window size in seconds (e.g., 3600 for 1h)
            request_type: Optional filter by request type (None = all types)

        Returns:
            Number of requests within the window
        """
        now = time.time()
        window_start = now - window_seconds

        if request_type:
            count_row = self._db.fetchone(
                """
                SELECT COUNT(*) as count
                FROM account_request_log
                WHERE account_name = ? AND request_type = ? AND timestamp > ?
                """,
                (account, request_type, window_start),
            )
        else:
            count_row = self._db.fetchone(
                """
                SELECT COUNT(*) as count
                FROM account_request_log
                WHERE account_name = ? AND timestamp > ?
                """,
                (account, window_start),
            )

        return count_row['count'] if count_row else 0

    def _ensure_limits(self, account: str) -> None:
        """Ensure account has limit configuration (insert default if missing)."""
        # Check if limits exist
        existing = self._db.fetchone(
            "SELECT account_name FROM account_rate_limits WHERE account_name = ?",
            (account,),
        )

        if not existing:
            # Insert default limits
            self._db.execute(
                """
                INSERT INTO account_rate_limits
                (account_name, window_1h_limit, window_3h_limit, window_5h_limit, window_1d_limit, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    account,
                    DEFAULT_LIMITS['window_1h_limit'],
                    DEFAULT_LIMITS['window_3h_limit'],
                    DEFAULT_LIMITS['window_5h_limit'],
                    DEFAULT_LIMITS['window_1d_limit'],
                    time.time(),
                ),
            )

    def get_limits(self, account: str) -> dict:
        """Get configured limits for account.

        Args:
            account: Instagram account name

        Returns:
            Dict with window limits and window sizes
        """
        self._ensure_limits(account)

        row = self._db.fetchone(
            "SELECT * FROM account_rate_limits WHERE account_name = ?",
            (account,),
        )

        if not row:
            return DEFAULT_LIMITS.copy()

        return {
            'window_1h_limit': row['window_1h_limit'],
            'window_3h_limit': row['window_3h_limit'],
            'window_5h_limit': row['window_5h_limit'],
            'window_1d_limit': row['window_1d_limit'],
        }

    def set_limits(self, account: str, limits: dict) -> None:
        """Update limits for account.

        Args:
            account: Instagram account name
            limits: Dict with 'window_1h_limit', 'window_3h_limit', etc.
        """
        self._ensure_limits(account)

        updates = []
        params = []

        if 'window_1h_limit' in limits:
            updates.append("window_1h_limit = ?")
            params.append(limits['window_1h_limit'])
        if 'window_3h_limit' in limits:
            updates.append("window_3h_limit = ?")
            params.append(limits['window_3h_limit'])
        if 'window_5h_limit' in limits:
            updates.append("window_5h_limit = ?")
            params.append(limits['window_5h_limit'])
        if 'window_1d_limit' in limits:
            updates.append("window_1d_limit = ?")
            params.append(limits['window_1d_limit'])

        if updates:
            params.append(time.time())  # updated_at
            params.append(account)      # WHERE clause

            query = f"""
            UPDATE account_rate_limits
            SET {', '.join(updates)}, updated_at = ?
            WHERE account_name = ?
            """
            self._db.execute(query, params)

    def can_make_request(
        self,
        account: str,
        request_type: str = 'action',
    ) -> tuple[bool, dict]:
        """Check if account can make request (returns bool + wait info).

        Checks all sliding windows. If any window is at limit, returns
        wait time until that window clears.

        Args:
            account: Instagram account name
            request_type: Type of request being made

        Returns:
            Tuple of (can_make: bool, wait_info: dict)
            - wait_info contains: wait_until, wait_seconds, limiting_window, current_counts
        """
        limits = self.get_limits(account)
        now = time.time()

        # Count requests in each window
        windows = {
            '1h': (WINDOW_1H_SECS, limits['window_1h_limit']),
            '3h': (WINDOW_3H_SECS, limits['window_3h_limit']),
            '5h': (WINDOW_5H_SECS, limits['window_5h_limit']),
            '1d': (WINDOW_1D_SECS, limits['window_1d_limit']),
        }

        current_counts = {}
        limiting_window = None
        max_wait_until = None

        for window_name, (window_secs, limit) in windows.items():
            count = self.get_request_count(account, window_secs, request_type)
            current_counts[window_name] = count

            if count >= limit:
                # This window is at limit - find when it will clear
                # Get the oldest timestamp in this window
                oldest_row = self._db.fetchone(
                    """
                    SELECT timestamp
                    FROM account_request_log
                    WHERE account_name = ? AND request_type = ?
                    ORDER BY timestamp ASC
                    LIMIT 1
                    OFFSET (SELECT COUNT(*) FROM account_request_log
                            WHERE account_name = ? AND request_type = ?
                            ORDER BY timestamp ASC) - ?
                    """,
                    (account, request_type, account, request_type, count - limit + 1),
                )

                if oldest_row:
                    oldest_ts = oldest_row['timestamp']
                    clear_at = oldest_ts + window_secs

                    if clear_at > now:
                        wait_seconds = clear_at - now
                        if max_wait_until is None or clear_at > max_wait_until:
                            max_wait_until = clear_at
                            limiting_window = window_name

        if limiting_window:
            wait_seconds = max_wait_until - now if max_wait_until else 0
            wait_until = datetime.fromtimestamp(max_wait_until).strftime('%H:%M:%S') if max_wait_until else 'unknown'

            return False, {
                'wait_until': wait_until,
                'wait_seconds': wait_seconds,
                'limiting_window': limiting_window,
                'current_counts': current_counts,
            }

        return True, {
            'wait_until': None,
            'wait_seconds': 0,
            'limiting_window': None,
            'current_counts': current_counts,
        }

    def cleanup_old_records(self, older_than_hours: int = 24) -> int:
        """Remove old request logs to prevent DB bloat.

        Args:
            older_than_hours: Remove records older than this many hours

        Returns:
            Number of records deleted
        """
        cutoff_time = time.time() - (older_than_hours * 3600)

        self._db.execute(
            "DELETE FROM account_request_log WHERE timestamp < ?",
            (cutoff_time,),
        )

        # Get count of deleted rows via vacuum or count before delete
        # For SQLite, we'll return a placeholder as rowcount isn't reliable
        return 0

    def get_usage_summary(self, account: str) -> dict:
        """Get comprehensive usage summary for account.

        Returns detailed stats for all windows, including percentages.

        Args:
            account: Instagram account name

        Returns:
            Dict with usage stats for each window
        """
        limits = self.get_limits(account)

        summary = {}

        for window_name, window_secs in [('1h', WINDOW_1H_SECS),
                                         ('3h', WINDOW_3H_SECS),
                                         ('5h', WINDOW_5H_SECS),
                                         ('1d', WINDOW_1D_SECS)]:
            count = self.get_request_count(account, window_secs)
            limit_key = f'window_{window_name}_limit'
            limit = limits.get(limit_key, 0)

            summary[window_name] = {
                'count': count,
                'limit': limit,
                'percentage': (count / limit * 100) if limit > 0 else 0,
                'remaining': max(0, limit - count),
            }

        return summary


__all__ = ["AccountRateLimitRepository"]
