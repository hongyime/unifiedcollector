"""B1: Browser session manager.

One Playwright browser per process, one context (tab) per account.
Sessions stored as JSON cookies in sessions/{username}_browser.json.

First run per account: headed browser opens → you log in visually → cookies saved.
Subsequent runs: headless, cookies loaded, ~3s startup.
"""
from __future__ import annotations

import json
import os
import random
import time
from pathlib import Path
from typing import Optional

from src.config import SESSIONS_DIR, INSTAGRAM_ACCOUNTS
from src.account_manager import _get_account_fingerprint

_BROWSER_SUFFIX = "_browser.json"
_IG_HOME = "https://www.instagram.com/"
_IG_LOGIN = "https://www.instagram.com/accounts/login/"


def _session_path(username: str) -> Path:
    return Path(SESSIONS_DIR) / f"{username}{_BROWSER_SUFFIX}"


def _stealth_init(page) -> None:
    """Patch common headless-detection vectors before any navigation."""
    page.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
        Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});
        window.chrome = {runtime: {}};
        const origQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (p) =>
            p.name === 'notifications'
            ? Promise.resolve({state: Notification.permission})
            : origQuery(p);
    """)


class BrowserSessionManager:
    """Manages one Playwright browser + per-account cookie sessions."""

    def __init__(self):
        self._playwright = None
        self._browser = None

    def _launch(self, headless: bool = True):
        from playwright.sync_api import sync_playwright
        if self._playwright is None:
            self._playwright = sync_playwright().start()
        if self._browser is None or not self._browser.is_connected():
            self._browser = self._playwright.chromium.launch(
                headless=headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                ],
            )
        return self._browser

    def get_page(self, account_name: str, headless: bool = True):
        """Return an authenticated Playwright page for account_name.

        First call: headed browser if no saved session, else headless.
        Saves cookies to sessions/{username}_browser.json after login.
        """
        account = next((a for a in INSTAGRAM_ACCOUNTS if a['name'] == account_name), None)
        if not account:
            raise ValueError(f"Account '{account_name}' not found in config")

        username = account['username']
        fp = _get_account_fingerprint(account_name)
        session_file = _session_path(username)
        has_session = session_file.exists()

        # Headed for first-time login, headless when reusing saved session
        browser = self._launch(headless=headless and has_session)

        context = browser.new_context(
            viewport={"width": random.randint(1260, 1400), "height": random.randint(880, 960)},
            user_agent=fp['ua'],
            locale=fp['accept_language'].split(',')[0].strip(),
            timezone_id=fp['timezone'],
            java_script_enabled=True,
        )

        # Load saved cookies if available
        if has_session:
            try:
                cookies = json.loads(session_file.read_text())
                context.add_cookies(cookies)
                print(f"[BROWSER] {account_name}: loaded saved session")
            except Exception as e:
                print(f"[BROWSER] {account_name}: cookie load failed ({e}) — will re-login")
                has_session = False

        page = context.new_page()
        _stealth_init(page)

        # Verify or perform login
        if not self._verify_logged_in(page, account, context, session_file, has_session):
            page.close()
            context.close()
            return None

        return page

    def _verify_logged_in(self, page, account, context, session_file, has_session: bool) -> bool:
        """Check login state; perform interactive login if needed."""
        page.goto(_IG_HOME, wait_until="domcontentloaded", timeout=30_000)
        time.sleep(random.uniform(1.5, 2.5))

        if self._is_logged_in(page):
            print(f"[BROWSER] {account['username']}: session valid ✓")
            return True

        if has_session:
            print(f"[BROWSER] {account['username']}: session expired — re-logging in")

        # Headed login flow
        print(f"[BROWSER] Opening login page for {account['username']} — complete login in browser...")
        page.goto(_IG_LOGIN, wait_until="domcontentloaded", timeout=30_000)
        time.sleep(random.uniform(1.0, 2.0))

        # Fill credentials
        try:
            page.fill('input[name="username"]', account['username'])
            time.sleep(random.uniform(0.5, 1.2))
            page.fill('input[name="password"]', account['password'])
            time.sleep(random.uniform(0.8, 1.5))
            page.click('button[type="submit"]')
            # Wait up to 60s for user to complete 2FA if required
            page.wait_for_url("**/instagram.com/**", timeout=60_000)
            time.sleep(random.uniform(2.0, 3.0))
        except Exception as e:
            print(f"[BROWSER] Login interaction failed: {e}")

        # If still on login/challenge page, wait for user action
        for _ in range(30):
            if self._is_logged_in(page):
                break
            print(f"[BROWSER] Waiting for login completion (2FA?)… ({_}/30)")
            time.sleep(2)
        else:
            print(f"[BROWSER] {account['username']}: login failed after 60s")
            return False

        # Save cookies
        try:
            cookies = context.cookies()
            session_file.parent.mkdir(parents=True, exist_ok=True)
            session_file.write_text(json.dumps(cookies, indent=2))
            print(f"[BROWSER] {account['username']}: session saved → {session_file}")
        except Exception as e:
            print(f"[BROWSER] Could not save session: {e}")

        return True

    @staticmethod
    def _is_logged_in(page) -> bool:
        """Heuristic: logged-in users see the feed or profile, not the login gate."""
        url = page.url
        return (
            "instagram.com" in url
            and "/accounts/login" not in url
            and "/challenge" not in url
        )

    def close(self) -> None:
        """Close browser and stop Playwright."""
        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._playwright:
                self._playwright.stop()
        except Exception:
            pass
        self._browser = None
        self._playwright = None


# Module-level singleton — one browser process per Python process
_manager: Optional[BrowserSessionManager] = None


def get_browser_manager() -> BrowserSessionManager:
    global _manager
    if _manager is None:
        _manager = BrowserSessionManager()
    return _manager


import atexit
atexit.register(lambda: _manager.close() if _manager else None)
