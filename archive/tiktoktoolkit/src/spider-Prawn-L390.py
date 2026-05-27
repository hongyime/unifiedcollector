"""Spider module for incremental profile discovery and batch processing.

Features:
- Chunked processing (max 500 items per batch)
- Checks _SHUTDOWN flag between batches
- Uses wait_for_internet() / with_internet_retry() for network resilience
- Integrates with AccountManager and RateLimiter
- Fetches real profile stats (following/followers/video counts) via Playwright
- Skips following-list fetch when following OR followers > configured threshold
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from dataclasses import dataclass
from typing import List, Optional, Dict, Any

from . import resilience
from .account_manager import AccountManager
from .rate_limiter import AdaptiveRateLimiter

logger = logging.getLogger("uttk.spider")


@dataclass
class SpiderTask:
    username: str
    url: Optional[str] = None
    status: str = 'pending'
    priority: int = 0


class Spider:
    """Incremental profile spider with batch limits and shutdown handling."""

    def __init__(
        self,
        db_path: Path,
        account_manager: Optional[AccountManager] = None,
        rate_limiter: Optional[AdaptiveRateLimiter] = None,
        batch_size: int = 500,
        base_delay: float = 1.0,
        cookies_file: Optional[Path] = None,
        max_following: int = 500,
        max_followers: int = 500,
        headless: bool = True,
    ):
        self.db_path = Path(db_path)
        self.account_manager = account_manager
        self.rate_limiter = rate_limiter or AdaptiveRateLimiter(
            base_delay=base_delay, max_delay=30.0, min_delay=0.5, jitter=0.5
        )
        self.batch_size = max(1, min(batch_size, 500))
        self.cookies_file = cookies_file
        self.max_following = max_following
        self.max_followers = max_followers
        self.headless = headless
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
            conn.execute("CREATE INDEX IF NOT EXISTS idx_profiles_spider ON profiles(spider_status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_profiles_download ON profiles(download_status)")

    def enqueue(self, usernames: List[str], priority: int = 0) -> int:
        now = time.time()
        added = 0
        with sqlite3.connect(str(self.db_path)) as conn:
            for u in usernames:
                try:
                    conn.execute("""
                        INSERT INTO profiles (username, spider_status, last_scraped_ts)
                        VALUES (?,?,?)
                        ON CONFLICT(username) DO NOTHING
                    """, (u, 'pending', now))
                    if conn.execute("SELECT changes()").fetchone()[0]:
                        added += 1
                except Exception as exc:
                    logger.debug(f"Failed to enqueue {u}: {exc}")
            conn.commit()
        logger.info(f"Enqueued {added} new profiles ({len(usernames)} submitted)")
        return added

    def fetch_pending(self, limit: int = 500) -> List[SpiderTask]:
        tasks = []
        with sqlite3.connect(str(self.db_path)) as conn:
            cur = conn.execute("""
                SELECT username FROM profiles
                WHERE spider_status='pending'
                ORDER BY last_scraped_ts ASC NULLS FIRST
                LIMIT ?
            """, (limit,))
            rows = cur.fetchall()
        for (username,) in rows:
            tasks.append(SpiderTask(username=username, status='pending'))
        return tasks

    def update_spider_status(self, username: str, status: str, filter_reason: Optional[str] = None) -> None:
        with sqlite3.connect(str(self.db_path)) as conn:
            if filter_reason:
                conn.execute(
                    "UPDATE profiles SET spider_status=?, filter_reason=?, last_scraped_ts=? WHERE username=?",
                    (status, filter_reason, time.time(), username)
                )
            else:
                conn.execute(
                    "UPDATE profiles SET spider_status=?, last_scraped_ts=? WHERE username=?",
                    (status, time.time(), username)
                )
            conn.commit()

    @resilience.with_internet_retry(max_retries=3, backoff=2.0, max_backoff=30.0)
    def _fetch_profile_data(self, username: str) -> Dict[str, Any]:
        """Fetch real TikTok profile counts via Playwright."""
        from .browser_downloader import fetch_profile_stats

        url = f"https://www.tiktok.com/@{username}"
        if self.rate_limiter:
            self.rate_limiter.wait(url)
        if resilience.is_shutdown():
            raise RuntimeError("Shutdown requested during fetch")
        resilience.wait_for_internet(poll=3.0)

        return fetch_profile_stats(username, self.cookies_file, self.headless)

    def _get_following_list(self, username: str) -> List[str]:
        """Fetch following list via Playwright. Caller has already verified counts ≤ threshold."""
        from .browser_downloader import fetch_following_list

        url = f"https://www.tiktok.com/@{username}"
        if self.rate_limiter:
            self.rate_limiter.wait(url)
        if resilience.is_shutdown():
            return []

        return fetch_following_list(username, self.cookies_file, self.headless)

    def _process_one(self, task: SpiderTask) -> bool:
        try:
            self.update_spider_status(task.username, 'processing')
            data = self._fetch_profile_data(task.username)

            following = int(data.get('following_count') or 0)
            followers = int(data.get('followers_count') or 0)
            video_count = int(data.get('video_count') or 0)
            now = time.time()

            # Always store counts regardless of threshold
            with sqlite3.connect(str(self.db_path)) as conn:
                conn.execute("""
                    UPDATE profiles SET
                        user_id=COALESCE(NULLIF(?, ''), user_id),
                        followers_count=?,
                        following_count=?,
                        video_count=?,
                        spider_status='done',
                        last_scraped_ts=?
                    WHERE username=?
                """, (data.get('user_id'), followers, following, video_count, now, task.username))
                conn.commit()

            # Threshold check: skip following-list fetch if either count exceeds limit
            over_threshold = (following > self.max_following) or (followers > self.max_followers)

            if over_threshold:
                reason = (
                    f"following={following} (max {self.max_following})"
                    if following > self.max_following
                    else f"followers={followers} (max {self.max_followers})"
                )
                logger.info(f"@{task.username}: skipping following list — {reason}")
            elif not resilience.is_shutdown():
                following_names = self._get_following_list(task.username)
                if following_names:
                    added = self.enqueue(following_names)
                    logger.info(
                        f"@{task.username}: found {len(following_names)} following, "
                        f"enqueued {added} new for spidering"
                    )
                else:
                    logger.debug(f"@{task.username}: following list empty or unavailable")

            if self.rate_limiter:
                self.rate_limiter.record_success(f"https://www.tiktok.com/@{task.username}")
            logger.debug(f"Spider processed: @{task.username} "
                         f"(following={following}, followers={followers}, videos={video_count})")
            return True

        except Exception as exc:
            logger.warning(f"Spider failed for @{task.username}: {exc}")
            self.update_spider_status(task.username, 'failed', filter_reason=str(exc)[:200])
            if self.rate_limiter:
                self.rate_limiter.record_failure(f"https://www.tiktok.com/@{task.username}", status_code=0)
            return False

    def run_batch(self, max_items: Optional[int] = None) -> int:
        limit = max(1, min(max_items if max_items is not None else self.batch_size, 500))
        tasks = self.fetch_pending(limit)
        if not tasks:
            return 0
        processed = 0
        for task in tasks:
            if resilience.is_shutdown():
                logger.info("Spider batch interrupted by shutdown signal")
                break
            if self._process_one(task):
                processed += 1
        return processed

    def run_until_done(self, check_interval: float = 1.0) -> int:
        total = 0
        while True:
            if resilience.is_shutdown():
                break
            n = self.run_batch()
            if n == 0:
                break
            total += n
            resilience.interruptible_sleep(check_interval)
        return total
