"""B6: BrowserDownloader — orchestrates B1-B5 for one or many targets.

Usage:
    dl = BrowserDownloader(account_name='b')
    dl.download('someuser', post_limit=50)
    dl.close()

Ctrl+C safe: _SHUTDOWN flag from resilience.py stops the loop after
the current file finishes. DB is checkpointed on exit via atexit (db manager).
"""
from __future__ import annotations

import os
import random
import time
from pathlib import Path
from typing import Optional

from src.resilience import _SHUTDOWN
from src.config import DATA_DIR
from src.browser_session_manager import get_browser_manager
from src.browser_profile_scraper import get_post_shortcodes
from src.browser_post_extractor import extract_post_data
from src.browser_media_downloader import download_media_item, make_requests_session
from src.browser_db_recorder import (
    username_completed, shortcode_completed,
    record_media_item, mark_shortcode_failed,
    mark_username_completed, mark_username_failed,
)


def _get_db():
    import os as _os
    from src.db.manager import DatabaseManager
    if not hasattr(_get_db, "_instance") or _get_db._instance is None:
        _get_db._instance = DatabaseManager(_os.environ.get("DATABASE_URL", ""))
    return _get_db._instance


_get_db._instance = None


class BrowserDownloader:
    """Stealth media downloader using a real browser session."""

    def __init__(self, account_name: str, downloads_dir: Optional[str] = None):
        self._account_name = account_name
        self._downloads_root = Path(downloads_dir or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "downloads",
        ))
        self._manager = get_browser_manager()
        self._page = None
        self._requests_session = None

    def _ensure_page(self) -> bool:
        """Lazily open authenticated browser page."""
        if self._page is not None:
            return True
        print(f"[BROWSER] Authenticating {self._account_name}…")
        self._page = self._manager.get_page(self._account_name)
        if self._page is None:
            return False
        self._requests_session = make_requests_session(self._page)
        return True

    def download(self, username: str, post_limit: int = 0) -> dict:
        """Download all (or up to post_limit) posts for username.

        Returns summary dict: {downloaded, skipped, failed, status}
        """
        db = _get_db()
        summary = {"downloaded": 0, "skipped": 0, "failed": 0, "status": "ok"}

        # Level-1 dedup: entire username already done?
        if username_completed(db, username):
            print(f"[BROWSER] {username}: already completed — skip")
            summary["status"] = "already_done"
            return summary

        if not self._ensure_page():
            summary["status"] = "auth_failed"
            return summary

        # Collect shortcodes by scrolling profile
        shortcodes, profile_status = get_post_shortcodes(
            self._page, username, limit=post_limit
        )

        if profile_status == "not_found":
            mark_username_failed(db, username, "not_found")
            summary["status"] = "not_found"
            return summary

        if profile_status == "private":
            mark_username_failed(db, username, "private_inaccessible")
            summary["status"] = "private"
            return summary

        if not shortcodes:
            mark_username_completed(db, username)
            summary["status"] = "empty"
            return summary

        dest_dir = self._downloads_root / username
        print(f"[BROWSER] {username}: downloading {len(shortcodes)} posts → {dest_dir}")

        for i, shortcode in enumerate(shortcodes, 1):
            if _SHUTDOWN.is_set():
                print(f"[BROWSER] Shutdown — stopping after {i-1}/{len(shortcodes)} posts")
                summary["status"] = "interrupted"
                break

            # Level-2 dedup: this post already downloaded?
            if shortcode_completed(db, shortcode):
                summary["skipped"] += 1
                continue

            # Extract post metadata + media URLs
            post_data = extract_post_data(self._page, shortcode)
            if post_data is None:
                mark_shortcode_failed(db, shortcode, username)
                summary["failed"] += 1
                continue

            # Download each media item in the post
            post_ok = True
            for item in post_data["media"]:
                if _SHUTDOWN.is_set():
                    break
                file_info = download_media_item(
                    item, post_data, dest_dir, self._requests_session
                )
                if file_info:
                    record_media_item(db, post_data, file_info)
                    summary["downloaded"] += 1
                else:
                    post_ok = False
                    summary["failed"] += 1

            if not post_ok:
                mark_shortcode_failed(db, shortcode, username)

            # Throttle between posts — human reading rhythm
            if i < len(shortcodes):
                pause = random.uniform(2.0, 5.0)
                if i % 15 == 0:
                    pause = random.uniform(20.0, 40.0)
                    print(f"[BROWSER] {i}/{len(shortcodes)} — longer pause ({pause:.0f}s)")
                time.sleep(pause)

        # Mark username done only if not interrupted and no failures
        if summary["status"] == "ok" and summary["failed"] == 0:
            mark_username_completed(db, username)

        print(
            f"[BROWSER] {username} done — "
            f"downloaded={summary['downloaded']} "
            f"skipped={summary['skipped']} "
            f"failed={summary['failed']}"
        )
        return summary

    def download_batch(self, usernames: list[str], post_limit: int = 0) -> None:
        """Download media for a list of usernames sequentially."""
        total = len(usernames)
        for i, username in enumerate(usernames, 1):
            if _SHUTDOWN.is_set():
                print("[BROWSER] Shutdown requested — stopping batch")
                break
            print(f"\n[BROWSER] [{i}/{total}] {username}")
            self.download(username, post_limit=post_limit)
            if i < total and not _SHUTDOWN.is_set():
                time.sleep(random.uniform(3.0, 8.0))

    def close(self) -> None:
        if self._requests_session:
            try:
                self._requests_session.close()
            except Exception:
                pass
        # Browser manager owns the browser lifecycle; it closes on atexit
