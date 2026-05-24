"""B2: Profile grid scraper — collect post shortcodes by scrolling."""
from __future__ import annotations

import random
import time
from typing import Optional

from src.resilience import _SHUTDOWN

_IG_BASE = "https://www.instagram.com"


def get_post_shortcodes(
    page,
    username: str,
    limit: int = 0,
) -> tuple[list[str], str]:
    """Scroll the profile grid and return (shortcodes, status).

    status: 'ok' | 'private' | 'not_found' | 'empty'
    shortcodes ordered most-recent-first (Instagram grid default).
    limit=0 → collect all posts.
    """
    url = f"{_IG_BASE}/{username}/"
    page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    time.sleep(random.uniform(1.5, 2.5))

    # Check for not-found / private
    if _is_not_found(page):
        print(f"[BROWSER] {username}: profile not found")
        return [], "not_found"

    if _is_private(page, username):
        print(f"[BROWSER] {username}: private profile")
        return [], "private"

    shortcodes: list[str] = []
    seen: set[str] = set()
    no_new_streak = 0

    print(f"[BROWSER] Scrolling {username} grid (limit={limit or 'all'})…")

    while True:
        if _SHUTDOWN.is_set():
            print("[BROWSER] Shutdown — stopping scroll")
            break

        # Extract shortcodes from visible grid links
        links = page.query_selector_all('a[href*="/p/"]')
        new_found = 0
        for link in links:
            href = link.get_attribute("href") or ""
            if "/p/" in href:
                sc = href.split("/p/")[1].rstrip("/")
                if sc and sc not in seen:
                    seen.add(sc)
                    shortcodes.append(sc)
                    new_found += 1

        if limit > 0 and len(shortcodes) >= limit:
            shortcodes = shortcodes[:limit]
            break

        if new_found == 0:
            no_new_streak += 1
            if no_new_streak >= 3:
                break  # no more posts to load
        else:
            no_new_streak = 0

        # Human-like scroll
        page.evaluate(f"window.scrollBy(0, {random.randint(600, 950)})")
        time.sleep(random.uniform(0.8, 2.2))

    status = "ok" if shortcodes else "empty"
    print(f"[BROWSER] {username}: found {len(shortcodes)} posts")
    return shortcodes, status


def _is_not_found(page) -> bool:
    return (
        page.query_selector('h2[class*="error"]') is not None
        or "Page Not Found" in (page.title() or "")
        or page.url.endswith("/404/")
    )


def _is_private(page, username: str) -> bool:
    try:
        # Private account shows a lock icon / "This Account is Private" text
        content = page.inner_text("body") or ""
        return (
            "This Account is Private" in content
            or "account is private" in content.lower()
        )
    except Exception:
        return False
