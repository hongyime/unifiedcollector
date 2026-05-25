import asyncio
import json
import logging
import math
import os
import random
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

from src.core.base_collector import BaseCollector
from src.core.account_pool import AccountPool
from src.core.human_rate_limiter import HumanLikeRateLimiter, OperationType
from src.core.sliding_window_limiter import SlidingWindowRateLimiter, WindowConfig
from src.core.profile_photo_tracker import ProfilePhotoTracker

logger = logging.getLogger(__name__)

GRAPH_API = "https://www.instagram.com/api/v1"

NIGHT_HOURS = {23, 0, 1, 2, 3, 4, 5, 6}
RISKY_HOURS = {9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19}

MICRO_PAUSE_PROBABILITY = 0.7
MICRO_PAUSE_MIN = 0.5
MICRO_PAUSE_MAX = 3.0

ACCOUNT_SWITCH_DELAY_MIN = 180
ACCOUNT_SWITCH_DELAY_MAX = 300

DAILY_QUOTA_PROFILE_VIEWS = int(os.getenv("INSTA_DAILY_QUOTA_PROFILE_VIEWS", "180"))
DAILY_QUOTA_ACTIONS = int(os.getenv("INSTA_DAILY_QUOTA_ACTIONS", "6000"))

SLIDING_WINDOW_ENABLED = os.getenv("SLIDING_WINDOW_ENABLED", "true").lower() == "true"
CONTENT_AWARE_ENABLED = os.getenv("CONTENT_AWARE_ENABLED", "true").lower() == "true"

CONTENT_DELAYS = {
    "post": 3.0,
    "video": 6.0,
    "carousel": 4.0,
    "story": 2.0,
    "story_video": 2.5,
    "reel": 7.0,
    "highlight": 2.0,
    "highlight_video": 3.0,
    "profile_photo": 1.5,
}

# ---------------------------------------------------------------------------
# Playwright (Mode β) — hybrid fallback when instaloader/Graph endpoints fail.
#
# *** STRICT 1-AT-A-TIME GLOBAL SEMAPHORE ***
# WSL on this host is capped at 6GB and Docker has only 5.79GiB.  Each
# Chromium instance, even with --single-process, eats ~250-400MB.  We MUST NOT
# launch parallel browsers across account workers — doing so will OOM-kill
# the collector container.  Increase only if RAM is bumped.
# ---------------------------------------------------------------------------
PLAYWRIGHT_SEMAPHORE = asyncio.Semaphore(1)
PLAYWRIGHT_LAUNCH_ARGS = [
    "--single-process",       # CRITICAL for low-RAM hosts
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--no-zygote",
]


class InstagramCollector(BaseCollector):
    SOURCE_NAME = "instagram"
    USE_HUMAN_RATE_LIMITER = True

    def __init__(self):
        super().__init__()
        self._max_followers = int(os.getenv("FILTER_MAX_FOLLOWERS", "0"))
        self._sem = asyncio.Semaphore(2)

        self.account_pool = AccountPool(
            default_cooldown=900.0,
            error_cooldown=1800.0,
            max_consecutive_errors=3,
        )
        self.account_pool.load_from_env("INSTA", ["NAME", "USER", "PASS"])

        self._session_dir = Path("sessions") / "instagram"
        self._session_dir.mkdir(parents=True, exist_ok=True)

        self._sliding_limiter = SlidingWindowRateLimiter(windows=[
            WindowConfig("1h", 3600, int(os.getenv("INSTA_WINDOW_1H", "180"))),
            WindowConfig("3h", 10800, int(os.getenv("INSTA_WINDOW_3H", "400"))),
            WindowConfig("5h", 18000, int(os.getenv("INSTA_WINDOW_5H", "600"))),
            WindowConfig("1d", 86400, int(os.getenv("INSTA_WINDOW_1D", "2000"))),
        ])
        self._photo_tracker = ProfilePhotoTracker()
        self._session_max_age_days = int(os.getenv("INSTA_SESSION_MAX_AGE_DAYS", "7"))
        self._warmed_up = False
        self._daily_views: dict[str, int] = {}
        self._daily_actions: dict[str, int] = {}

        proxy_url = os.getenv("PROXY_URL", "")
        self._global_proxy = proxy_url.strip() if proxy_url else None
        self._account_proxies: dict[str, str] = {}
        self._account_browser_cookies: dict[str, str] = self._auto_discover_cookies()
        self._account_priorities: dict[str, str] = {}
        for i in range(1, 20):
            name = os.getenv(f"INSTA_ACCOUNT_{i}_NAME", "")
            px = os.getenv(f"INSTA_ACCOUNT_{i}_PROXY", "")
            browser = os.getenv(f"INSTA_ACCOUNT_{i}_BROWSER", "")
            priority = os.getenv(f"INSTA_ACCOUNT_{i}_PRIORITY", os.getenv("INSTA_LOGIN_PRIORITY", "cookie"))
            if name:
                self._account_priorities[name] = priority
                if px:
                    self._account_proxies[name] = px.strip()
                if browser:
                    self._account_browser_cookies[name] = browser.strip()

        self._loader = None
        self._current_account = None

    def _auto_discover_cookies(self) -> dict:
        """Auto-discover cookie files for all accounts.

        Searches both credentials/instagram/cookies/ (legacy) and
        credentials/instagram/ (current layout). Files are mapped by:
          1. exact account NAME match (e.g. shotsbyseah234.txt -> shotsbyseah234)
          2. account_<N> pattern -> ACCOUNT_<N>
          3. fallback: stem of filename
        """
        import re
        discovered = {}
        cookie_dirs = [
            "credentials/instagram/cookies/",
            "credentials/instagram/",
        ]
        for cookie_dir in cookie_dirs:
            if not os.path.exists(cookie_dir):
                continue
            for filename in os.listdir(cookie_dir):
                full = os.path.join(cookie_dir, filename)
                if not os.path.isfile(full):
                    continue
                if not (filename.endswith('.txt') or filename.endswith('.json')):
                    continue
                match = re.search(r'account_(\d+)', filename)
                if match:
                    account_name = f"ACCOUNT_{match.group(1)}"
                else:
                    account_name = filename.rsplit('.', 1)[0]
                # Don't overwrite if already discovered from the more-specific cookies/ dir
                if account_name in discovered:
                    continue
                discovered[account_name] = full
                logger.info("Auto-discovered cookie file for %s: %s", account_name, full)
        return discovered

    def set_pool(self, pool):
        super().set_pool(pool)
        self._photo_tracker.set_pool(pool)

    def _init_loader(self):
        try:
            import instaloader
            self._loader = instaloader.Instaloader(
                download_pictures=False,
                download_videos=False,
                download_video_thumbnails=False,
                download_geotags=False,
                download_comments=False,
                save_metadata=False,
                compress_json=False,
                quiet=True,
            )
        except ImportError:
            logger.warning("instaloader not installed — falling back to session cookie mode")
            self._loader = None

    def _login_account(self, account) -> bool:
        if not self._loader:
            self._init_loader()
        if not self._loader:
            return False

        username = account.credentials.get("user", "")
        password = account.credentials.get("pass", "")
        if not username:
            return False

        priority = self._account_priorities.get(account.name, os.getenv("INSTA_LOGIN_PRIORITY", "cookie"))

        if priority == "cookie":
            if self._try_cookie_login(account, username):
                return True
            if password:
                return self._password_login(account, username, password)
            return False
        else:
            if password:
                if self._password_login(account, username, password):
                    return True
            if self._try_cookie_login(account, username):
                return True
            return False

    def _try_cookie_login(self, account, username: str) -> bool:
        if account.name in self._account_browser_cookies:
            cookie_path = self._account_browser_cookies[account.name]
            if self._login_from_cookies(username, cookie_path):
                return True
            logger.info("Cookie login failed for %s", username)
        return False

    def _password_login(self, account, username: str, password: str) -> bool:
        session_file = self._session_dir / f"{username}.session"
        try:
            if session_file.exists() and self._check_session_age(username):
                import instaloader
                self._loader.load_session_from_file(username, str(session_file))
                self._loader.test_login()
                logger.info("Resumed session for %s", username)
                return True
        except Exception:
            logger.debug("Stale session for %s, re-logging in", username)

        try:
            self._loader.login(username, password)
            self._loader.save_session_to_file(str(session_file))
            self._save_session_meta(username)
            logger.info("Logged in as %s", username)
            return True
        except Exception as e:
            # Detect 2FA challenge from instaloader.
            err_text = str(e).lower()
            cls_name = type(e).__name__
            needs_2fa = (
                cls_name == "TwoFactorAuthRequiredException"
                or "two-factor" in err_text
                or "two_factor" in err_text
                or "2fa" in err_text
            )
            if needs_2fa:
                code = self._resolve_2fa_code(username)
                if code:
                    try:
                        self._loader.two_factor_login(code)
                        self._loader.save_session_to_file(str(session_file))
                        self._save_session_meta(username)
                        logger.info("Logged in (2FA) as %s", username)
                        # Consume the drop-file once used so a stale code can't be reused.
                        self._consume_2fa_dropfile(username)
                        return True
                    except Exception as e2:
                        logger.error("2FA login failed for %s: %s", username, e2)
                        self.account_pool.record_error(account.name)
                        return False
                else:
                    logger.error(
                        "2FA required for %s but no code available. "
                        "Set INSTA_ACCOUNT_<N>_TOTP_SECRET in .env, "
                        "or drop a 6-digit code into credentials/instagram/2fa/%s.code",
                        username, username,
                    )
                    self.account_pool.record_error(account.name)
                    return False
            logger.error("Login failed for %s: %s", username, e)
            self.account_pool.record_error(account.name)
            return False

    def _resolve_2fa_code(self, username: str) -> str:
        """Resolve a 2FA code for `username` from (in order):
          1. INSTA_ACCOUNT_<N>_TOTP_SECRET env (per matching account index)
          2. credentials/instagram/2fa/<username>.code drop-file (one-shot)
        Returns empty string if none available.
        """
        # 1. TOTP env var
        for i in range(1, 20):
            name = os.getenv(f"INSTA_ACCOUNT_{i}_NAME", "")
            if name and name.lower() == username.lower():
                secret = os.getenv(f"INSTA_ACCOUNT_{i}_TOTP_SECRET", "").strip()
                if secret:
                    try:
                        import pyotp
                        return pyotp.TOTP(secret).now()
                    except ImportError:
                        logger.warning("pyotp not installed; cannot use TOTP secret for %s", username)
                    except Exception as e:
                        logger.warning("TOTP generation failed for %s: %s", username, e)
                break
        # 2. Drop-file
        drop = Path("credentials/instagram/2fa") / f"{username}.code"
        if drop.exists():
            try:
                code = drop.read_text(encoding="utf-8").strip()
                # Allow either bare 6 digits or first whitespace-separated token
                code = code.split()[0] if code else ""
                if code.isdigit() and 6 <= len(code) <= 8:
                    logger.info("Using 2FA drop-file code for %s", username)
                    return code
            except Exception as e:
                logger.warning("Failed to read 2FA drop-file for %s: %s", username, e)
        return ""

    def _consume_2fa_dropfile(self, username: str) -> None:
        drop = Path("credentials/instagram/2fa") / f"{username}.code"
        try:
            if drop.exists():
                drop.unlink()
                logger.info("Consumed 2FA drop-file for %s", username)
        except Exception:
            pass

    def _login_from_cookies(self, username: str, cookie_path: str) -> bool:
        if not os.path.exists(cookie_path):
            logger.warning("Cookie file not found: %s", cookie_path)
            return False
        try:
            cookies = self._parse_browser_cookies(cookie_path)
            if not cookies:
                logger.warning("No cookies parsed from %s", cookie_path)
                return False

            required = {"sessionid", "csrftoken"}
            found = required & set(cookies.keys())
            if not found:
                logger.warning("Cookie file missing required keys (sessionid/csrftoken)")
                return False

            import requests
            session = self._loader.context._session
            for name, value in cookies.items():
                session.cookies.set(name, value, domain=".instagram.com")

            self._loader.test_login()
            self._save_session_meta(username)
            logger.info("Logged in via browser cookies for %s (%d cookies loaded)",
                        username, len(cookies))
            return True
        except Exception as e:
            logger.debug("Browser cookie login failed for %s: %s", username, e)
            return False

    @staticmethod
    def _parse_browser_cookies(filepath: str) -> dict[str, str]:
        cookies = {}
        with open(filepath, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) >= 7:
                    cookies[parts[5]] = parts[6]
        return cookies

    def _get_session_cookies(self) -> dict[str, str]:
        if self._loader and self._loader.context._session:
            jar = self._loader.context._session.cookies
            return {c.name: c.value for c in jar}
        return {}

    def _headers(self, account=None) -> dict[str, str]:
        ua = self.user_agents.get_for_domain("instagram.com")
        if account and account.fingerprint.get("user_agent"):
            ua = account.fingerprint["user_agent"]
        cookies = self._get_session_cookies()

        device_id = str(uuid.uuid4())
        if account and account.fingerprint.get("device_id"):
            device_id = account.fingerprint["device_id"]

        return {
            "User-Agent": ua,
            "X-IG-App-ID": "936619743392459",
            "X-IG-Device-ID": device_id,
            "X-IG-Android-ID": device_id.replace("-", "")[:16],
            "X-IG-Connection-Speed": f"{random.randint(1200, 8000)}kbps",
            "X-IG-Connection-Type": "WIFI",
            "X-IG-Capabilities": "3brTv10=",
            "X-Instagram-AJAX": "1",
            "X-Requested-With": "XMLHttpRequest",
            "X-CSRFToken": cookies.get("csrftoken", ""),
            "Accept": "*/*",
            "Origin": "https://www.instagram.com",
            "Referer": "https://www.instagram.com/",
        }

    def _get_proxy(self, account=None) -> str | None:
        if account and account.name in self._account_proxies:
            return self._account_proxies[account.name]
        return self._global_proxy

    def _time_of_day_multiplier(self) -> float:
        hour = datetime.now(timezone.utc).hour
        if hour in NIGHT_HOURS:
            return random.uniform(2.5, 4.0)
        if hour in RISKY_HOURS:
            return 1.5
        return 1.0

    def _check_daily_quota(self, account_name: str) -> bool:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        key = f"{account_name}:{today}"
        views = self._daily_views.get(key, 0)
        actions = self._daily_actions.get(key, 0)
        if DAILY_QUOTA_PROFILE_VIEWS and views >= DAILY_QUOTA_PROFILE_VIEWS:
            logger.warning("Daily profile view quota (%d) hit for %s", DAILY_QUOTA_PROFILE_VIEWS, account_name)
            return False
        if DAILY_QUOTA_ACTIONS and actions >= DAILY_QUOTA_ACTIONS:
            logger.warning("Daily action quota (%d) hit for %s", DAILY_QUOTA_ACTIONS, account_name)
            return False
        return True

    def _record_daily_action(self, account_name: str, views: int = 0, actions: int = 1):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        key = f"{account_name}:{today}"
        self._daily_views[key] = self._daily_views.get(key, 0) + views
        self._daily_actions[key] = self._daily_actions.get(key, 0) + actions

    async def _micro_pause(self):
        if random.random() < MICRO_PAUSE_PROBABILITY:
            u = max(random.random(), 1e-10)
            mean = (MICRO_PAUSE_MIN + MICRO_PAUSE_MAX) / 2
            pause = max(MICRO_PAUSE_MIN, min(-mean * math.log(u), MICRO_PAUSE_MAX))
            await asyncio.sleep(pause)

    async def _content_aware_delay(self, content_type: str):
        if not CONTENT_AWARE_ENABLED:
            return
        base = CONTENT_DELAYS.get(content_type, 3.0)
        base *= self._time_of_day_multiplier()
        jitter = random.uniform(0.8, 1.3)
        delay = base * jitter
        await asyncio.sleep(delay)

    @property
    def account_media_dir(self) -> Path:
        if self._current_account:
            acc_name = sanitize_name(self._current_account.name)
            path = self.media_dir / f"account_{acc_name}"
        else:
            path = self.media_dir / "default"
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def collect(self, targets: list[str]):
        account = self.account_pool.get_next()
        if account:
            self._current_account = account
            logged_in = await asyncio.get_event_loop().run_in_executor(
                None, self._login_account, account
            )
            if not logged_in:
                logger.error("Could not log in to any Instagram account")
                return
        else:
            logger.warning("No Instagram accounts configured — limited functionality")

        cookies = self._get_session_cookies()
        proxy = self._get_proxy(account)
        client_kwargs = dict(
            timeout=30, cookies=cookies, headers=self._headers(account), follow_redirects=True,
        )
        if proxy:
            client_kwargs["proxy"] = proxy
            logger.info("Using proxy for Instagram: %s", proxy.split("@")[-1] if "@" in proxy else "configured")

        async with httpx.AsyncClient(**client_kwargs) as client:
            await self._warmup(client)
            
            # Process manual targets first
            for username in targets:
                if self._stop.is_set(): break
                await self._process_target(client, username)

            # Then process spider queue if enabled
            if os.getenv("INSTA_SPIDER_ENABLED", "true").lower() == "true":
                await self._process_spider_queue(client)

    async def _process_target(self, client: httpx.AsyncClient, username: str):
        if self._current_account and not self._check_daily_quota(self._current_account.name):
            return

        # §21 Hard gate: respect per-account emergency cooldown set by 429 responses.
        # Without this the worker re-enters every 5min and re-triggers the cooldown.
        # Now per-account isolated — only THIS account's cooldown blocks it.
        if isinstance(self.rate_limiter, HumanLikeRateLimiter):
            acct_name = self._current_account.name if self._current_account else None
            remaining = self.rate_limiter.cooldown_remaining_seconds(
                "instagram.com", account=acct_name,
            )
            if remaining > 30.0:
                logger.warning(
                    "Skipping instagram/%s — per-account cooldown active for %s (%.0fs remaining)",
                    username, acct_name or "global", remaining,
                )
                return

        if SLIDING_WINDOW_ENABLED and not self._sliding_limiter.check("instagram.com"):
            wait = self._sliding_limiter.time_until_allowed("instagram.com")
            logger.warning("Sliding window limit hit, waiting %.0fs", wait)
            await asyncio.sleep(min(wait, 600))

        logger.info("Collecting instagram/%s", username)
        try:
            await self._collect_user(client, username)
            self._sliding_limiter.record("instagram.com")
            await self.checkpoint.save_progress(username)
            if self._current_account:
                self.account_pool.record_success(self._current_account.name)
                self._record_daily_action(self._current_account.name, views=1)
            await self._micro_pause()
        except Exception as e:
            if "429" in str(e) or "rate" in str(e).lower():
                await self._handle_rate_limit(e)
            else:
                logger.error("Failed instagram/%s: %s", username, e)
                await self.send_to_dlq(username, username, str(e))

    async def _process_spider_queue(self, client: httpx.AsyncClient):
        """Claim and process jobs from the spider queue."""
        while not self._stop.is_set():
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow("""
                    UPDATE instagram_spider_queue
                    SET status = 'processing',
                        last_attempt = NOW(),
                        attempts = attempts + 1
                    WHERE id = (
                        SELECT id FROM instagram_spider_queue
                        WHERE status = 'pending' AND attempts < 3
                        ORDER BY priority ASC, collected_at ASC
                        LIMIT 1
                    )
                    RETURNING platform_user_id, username
                """)
            
            if not row:
                break
            
            target = row['username'] or row['platform_user_id']
            try:
                await self._process_target(client, target)
                async with self.pool.acquire() as conn:
                    await conn.execute("UPDATE instagram_spider_queue SET status = 'completed' WHERE platform_user_id = $1", row['platform_user_id'])
            except Exception as e:
                logger.error("Spider job failed for %s: %s", target, e)
                async with self.pool.acquire() as conn:
                    await conn.execute("UPDATE instagram_spider_queue SET status = 'failed', error_message = $1 WHERE platform_user_id = $2", str(e), row['platform_user_id'])

    async def _handle_rate_limit(self, error):
        # Per-account cooldown (§22): isolate this account's 429 from siblings.
        acct_name = self._current_account.name if self._current_account else None
        if isinstance(self.rate_limiter, HumanLikeRateLimiter):
            self.rate_limiter.trigger_emergency_cooldown(
                "instagram.com", account=acct_name,
            )
        if self._current_account:
            self.account_pool.cooldown(self._current_account.name, 900.0)
            next_acct = self.account_pool.get_next(exclude=self._current_account.name)
            if next_acct:
                logger.info("Switching to account %s after rate limit", next_acct.name)
                self._current_account = next_acct
                await asyncio.get_event_loop().run_in_executor(
                    None, self._login_account, next_acct
                )

    async def _collect_user(self, client: httpx.AsyncClient, username: str):
        acct_name = self._current_account.name if self._current_account else None
        await self.rate_limiter.async_wait(
            "instagram.com", OperationType.PROFILE_VIEW, account=acct_name,
        )

        resp = await client.get(
            f"{GRAPH_API}/users/web_profile_info/",
            params={"username": username},
        )
        if resp.status_code == 404:
            logger.warning("User not found: %s", username)
            return
        if resp.status_code == 429:
            await self._handle_rate_limit(Exception("429"))
            return
        resp.raise_for_status()
        user_data = resp.json().get("data", {}).get("user", {})
        if not user_data:
            logger.warning("Empty profile data for %s", username)
            return

        uid = user_data.get("id", username)
        entity_name = user_data.get("username", username)

        # 1. Save Profile to Database
        await self._upsert_profile(user_data)

        follower_count = user_data.get("edge_followed_by", {}).get("count", 0)
        if self._max_followers and follower_count > self._max_followers:
            logger.info("Skipping %s: %d followers > max %d", username, follower_count, self._max_followers)
            return

        self.rate_limiter.record_success("instagram.com")

        # 2. Handle Profile Photo
        profile_pic = user_data.get("profile_pic_url_hd") or user_data.get("profile_pic_url")
        if profile_pic:
            dest_dir = self.account_media_dir / "profiles"
            dest_dir.mkdir(parents=True, exist_ok=True)
            changed, path = await self._photo_tracker.check_and_download(
                profile_pic, uid, "instagram", dest_dir,
            )
            if changed and path:
                data = path.read_bytes()
                await self.insert_media_item(
                    entity_id=uid,
                    entity_name=entity_name,
                    content_type="profile_photo",
                    content_id=f"profile_{uid}",
                    filename=path.name,
                    file_path=str(path),
                    file_size=len(data),
                    sha256=self.sha256_bytes(data),
                    metadata={"raw": user_data}
                )

        # 3. Spidering (if enabled)
        if os.getenv("INSTA_SPIDER_FOLLOWERS", "false").lower() == "true":
            await self._spider_followers(client, uid, entity_name)

        # 4. Collect Content
        # Probe instaloader/Graph first; if post enumeration explicitly fails
        # (401/429/empty), fall back to Playwright (Mode β, §22 hybrid).
        posts_ok = False
        try:
            posts_ok = await self._collect_posts(client, uid, entity_name)
        except Exception as e:
            logger.warning(
                "instagram/%s: Graph post enumeration raised %s — Playwright fallback",
                entity_name, type(e).__name__,
            )
            posts_ok = False

        if not posts_ok:
            try:
                await self._collect_posts_playwright(uid, entity_name)
            except Exception as e:
                logger.warning(
                    "instagram/%s: Playwright fallback failed: %s", entity_name, e,
                )

        await self._collect_stories(uid, entity_name)
        await self._collect_highlights(client, uid, entity_name)

    async def _upsert_profile(self, data: dict):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO instagram_profiles (
                    platform_user_id, username, full_name, bio,
                    followers_count, following_count, posts_count,
                    is_verified, is_private, profile_pic_url,
                    external_url, updated_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, NOW())
                ON CONFLICT (platform_user_id) DO UPDATE SET
                    username = EXCLUDED.username,
                    full_name = EXCLUDED.full_name,
                    bio = EXCLUDED.bio,
                    followers_count = EXCLUDED.followers_count,
                    following_count = EXCLUDED.following_count,
                    posts_count = EXCLUDED.posts_count,
                    is_verified = EXCLUDED.is_verified,
                    is_private = EXCLUDED.is_private,
                    profile_pic_url = EXCLUDED.profile_pic_url,
                    external_url = EXCLUDED.external_url,
                    updated_at = NOW()
            """, 
            data.get("id"), data.get("username"), data.get("full_name"), data.get("biography"),
            data.get("edge_followed_by", {}).get("count", 0),
            data.get("edge_follow", {}).get("count", 0),
            data.get("edge_owner_to_timeline_media", {}).get("count", 0),
            data.get("is_verified", False), data.get("is_private", False),
            data.get("profile_pic_url_hd"), data.get("external_url")
            )

    async def _spider_followers(self, client: httpx.AsyncClient, uid: str, username: str):
        # Implementation for follower spidering would go here (requires GraphQL or Instaloader)
        logger.debug("Spidering followers for %s (not fully implemented in this step)", username)

    async def _collect_posts(self, client: httpx.AsyncClient, uid: str, entity_name: str) -> bool:
        """Try to enumerate posts via the GraphQL endpoint.

        Returns True on success (at least one page processed cleanly), False if
        the endpoint signals auth/rate failure (401/429) or returns empty —
        signal to caller to invoke the Playwright fallback.
        """
        end_cursor = ""
        has_next = True
        page_depth = 0
        any_success = False

        while has_next and not self._stop.is_set():
            await self.rate_limiter.async_wait(
                "instagram.com", OperationType.PAGINATION, pagination_depth=page_depth,
                account=(self._current_account.name if self._current_account else None),
            )

            params = {
                "query_hash": "472f257a40c653c64c666ce877d59d2b",
                "variables": json.dumps({
                    "id": uid,
                    "first": 12,
                    "after": end_cursor,
                }),
            }

            async with self._sem:
                try:
                    resp = await client.get(
                        "https://www.instagram.com/graphql/query/",
                        params=params,
                    )
                    if resp.status_code in (401, 403):
                        logger.info(
                            "instagram/%s: GraphQL %s — signalling Playwright fallback",
                            entity_name, resp.status_code,
                        )
                        return False
                    if resp.status_code == 429:
                        await self._handle_rate_limit(Exception("429"))
                        return False
                    resp.raise_for_status()
                except Exception as e:
                    self.rate_limiter.record_failure("instagram.com")
                    self.circuit_breaker.record_failure()
                    logger.error("GraphQL request failed: %s", e)
                    return any_success

            data = resp.json()
            media_data = (data.get("data", {})
                          .get("user", {})
                          .get("edge_owner_to_timeline_media", {}))
            edges = media_data.get("edges", [])
            page_info = media_data.get("page_info", {})
            has_next = page_info.get("has_next_page", False)
            end_cursor = page_info.get("end_cursor", "")

            self.rate_limiter.record_success("instagram.com")
            self.circuit_breaker.record_success()
            page_depth += 1

            if edges:
                any_success = True

            for edge in edges:
                if self._stop.is_set():
                    break
                node = edge.get("node", {})
                await self._process_post(node, uid, entity_name)

        return any_success

    # ------------------------------------------------------------------
    # Playwright (Mode β) hybrid fallback
    # ------------------------------------------------------------------
    def _build_playwright_storage_state(self, account_name: str) -> dict | None:
        """Convert the per-account Netscape cookie file to Playwright storage_state.

        Returns None if no usable cookie file exists.
        """
        cookie_path = self._account_browser_cookies.get(account_name)
        if not cookie_path or not os.path.exists(cookie_path):
            return None

        cookies: list[dict] = []
        try:
            # Support both Netscape .txt and JSON format
            if cookie_path.endswith(".json"):
                try:
                    raw = json.loads(Path(cookie_path).read_text(encoding="utf-8"))
                except Exception:
                    return None
                if isinstance(raw, list):
                    for c in raw:
                        if isinstance(c, dict) and "name" in c and "value" in c:
                            cookies.append({
                                "name": c["name"],
                                "value": c["value"],
                                "domain": c.get("domain", ".instagram.com"),
                                "path": c.get("path", "/"),
                                "expires": float(c.get("expirationDate", c.get("expires", -1))),
                                "httpOnly": bool(c.get("httpOnly", False)),
                                "secure": bool(c.get("secure", True)),
                                "sameSite": c.get("sameSite", "Lax"),
                            })
            else:
                with open(cookie_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        parts = line.split("\t")
                        if len(parts) < 7:
                            continue
                        domain, _flag, path, secure, expires, name, value = parts[:7]
                        try:
                            expires_f = float(expires)
                        except ValueError:
                            expires_f = -1
                        cookies.append({
                            "name": name,
                            "value": value,
                            "domain": domain if domain.startswith(".") else f".{domain}",
                            "path": path or "/",
                            "expires": expires_f,
                            "httpOnly": False,
                            "secure": secure.upper() == "TRUE",
                            "sameSite": "Lax",
                        })
        except Exception as e:
            logger.warning("Failed to parse cookies for Playwright (%s): %s", account_name, e)
            return None

        if not cookies:
            return None
        return {"cookies": cookies, "origins": []}

    async def _collect_posts_playwright(self, uid: str, entity_name: str):
        """Mode β: spin up a single-process headless Chromium, navigate to the
        profile, scrape ``window._sharedData`` / ``window.__additionalDataLoaded``,
        and upsert any post nodes we find.

        Concurrency: STRICT 1-at-a-time via the module-level ``PLAYWRIGHT_SEMAPHORE``.
        Do NOT raise this without bumping host RAM (see comment near the semaphore).
        """
        try:
            from playwright.async_api import async_playwright  # type: ignore
        except ImportError:
            logger.warning(
                "Playwright not installed — cannot run Mode β fallback for %s",
                entity_name,
            )
            return

        acct_name = self._current_account.name if self._current_account else None
        storage_state = self._build_playwright_storage_state(acct_name) if acct_name else None
        ua = self.user_agents.get_for_domain("instagram.com")
        if self._current_account and self._current_account.fingerprint.get("user_agent"):
            ua = self._current_account.fingerprint["user_agent"]

        url = f"https://www.instagram.com/{entity_name}/"

        async with PLAYWRIGHT_SEMAPHORE:
            logger.info(
                "Playwright fallback launching Chromium for instagram/%s (account=%s)",
                entity_name, acct_name or "anonymous",
            )
            playwright_ctx = await async_playwright().start()
            browser = None
            try:
                browser = await playwright_ctx.chromium.launch(
                    headless=True,
                    args=PLAYWRIGHT_LAUNCH_ARGS,
                )
                context_kwargs: dict = {
                    "user_agent": ua,
                    "viewport": {"width": 1280, "height": 800},
                    "locale": "en-US",
                }
                if storage_state:
                    context_kwargs["storage_state"] = storage_state
                context = await browser.new_context(**context_kwargs)
                page = await context.new_page()

                try:
                    await page.goto(url, wait_until="networkidle", timeout=45000)
                except Exception as e:
                    logger.warning("Playwright goto failed for %s: %s", url, e)
                    return

                # IG layouts vary; try several extraction strategies.
                payload = await page.evaluate(
                    """() => {
                        const out = {};
                        try { out.shared = window._sharedData || null; } catch(e) {}
                        try {
                            const all = [];
                            for (const k of Object.keys(window)) {
                                if (k.startsWith('__additionalData')) all.push(window[k]);
                            }
                            out.additional = all;
                        } catch(e) {}
                        return out;
                    }"""
                )

                edges = self._extract_post_edges_from_payload(payload)
                if not edges:
                    logger.info(
                        "Playwright fallback: no post edges parsed for %s "
                        "(IG layout may have changed)", entity_name,
                    )
                    return

                logger.info(
                    "Playwright fallback: extracted %d post nodes for %s",
                    len(edges), entity_name,
                )
                for edge in edges:
                    if self._stop.is_set():
                        break
                    node = edge.get("node", edge) if isinstance(edge, dict) else {}
                    if node:
                        try:
                            await self._process_post(node, uid, entity_name)
                        except Exception as e:
                            logger.debug("process_post failed: %s", e)
            finally:
                # ALWAYS close the browser — leaks here will OOM the WSL host.
                try:
                    if browser is not None:
                        await browser.close()
                except Exception:
                    pass
                try:
                    await playwright_ctx.stop()
                except Exception:
                    pass

    @staticmethod
    def _extract_post_edges_from_payload(payload: dict) -> list:
        """Best-effort traversal of IG's nested JSON shapes to find post edges."""
        edges: list = []
        if not isinstance(payload, dict):
            return edges

        def walk(obj):
            if isinstance(obj, dict):
                # Common IG shape
                etmm = obj.get("edge_owner_to_timeline_media")
                if isinstance(etmm, dict):
                    e = etmm.get("edges")
                    if isinstance(e, list):
                        edges.extend(e)
                for v in obj.values():
                    walk(v)
            elif isinstance(obj, list):
                for v in obj:
                    walk(v)

        walk(payload)
        return edges

    async def _collect_stories(self, uid: str, entity_name: str):
        if not self._loader:
            return
        try:
            import instaloader
            profile = instaloader.Profile.from_id(self._loader.context, int(uid))
            for story in self._loader.get_stories(userids=[profile.userid]):
                for item in story.get_items():
                    if self._stop.is_set():
                        return
                    await self.rate_limiter.async_wait("instagram.com", OperationType.MEDIA_DOWNLOAD)

                    url = item.video_url if item.is_video else item.url
                    ext = "mp4" if item.is_video else "jpg"
                    content_type = "story_video" if item.is_video else "story"

                    if self.is_known(f"story_{item.mediaid}"):
                        continue

                    await self.download_media({
                        "entity_id": uid,
                        "entity_name": entity_name,
                        "content_type": content_type,
                        "content_id": f"story_{item.mediaid}",
                        "url": url,
                        "extension": ext,
                        "raw": item._asdict() if hasattr(item, "_asdict") else {}
                    })
        except Exception as e:
            logger.debug("Stories collection failed for %s: %s", entity_name, e)

    async def _collect_highlights(self, client: httpx.AsyncClient, uid: str, entity_name: str):
        if not self._loader:
            return
        try:
            import instaloader
            profile = instaloader.Profile.from_id(self._loader.context, int(uid))
            for highlight in self._loader.get_highlights(profile):
                if self._stop.is_set():
                    return
                for item in highlight.get_items():
                    if self._stop.is_set():
                        return
                    await self.rate_limiter.async_wait("instagram.com", OperationType.MEDIA_DOWNLOAD)

                    url = item.video_url if item.is_video else item.url
                    ext = "mp4" if item.is_video else "jpg"
                    content_type = "highlight_video" if item.is_video else "highlight"
                    cid = f"highlight_{highlight.unique_id}_{item.mediaid}"

                    if self.is_known(cid):
                        continue

                    await self.download_media({
                        "entity_id": uid,
                        "entity_name": entity_name,
                        "content_type": content_type,
                        "content_id": cid,
                        "url": url,
                        "extension": ext,
                        "raw": item._asdict() if hasattr(item, "_asdict") else {}
                    })
        except Exception as e:
            logger.debug("Highlights collection failed for %s: %s", entity_name, e)

    async def _process_post(self, node: dict, uid: str, entity_name: str):
        shortcode = node.get("shortcode", "")
        typename = node.get("__typename", "")

        # Save post metadata to database
        await self._upsert_post(node, uid)

        if typename == "GraphSidecar":
            sidecar_edges = (node.get("edge_sidecar_to_children", {})
                             .get("edges", []))
            for i, se in enumerate(sidecar_edges):
                child = se.get("node", {})
                cid = f"{shortcode}_{i}"
                if not self.is_known(cid):
                    await self._download_node(child, uid, entity_name, cid, parent_node=node)
        else:
            if not self.is_known(shortcode):
                await self._download_node(node, uid, entity_name, shortcode)

    async def _upsert_post(self, node: dict, uid: str):
        caption = ""
        edges = node.get("edge_media_to_caption", {}).get("edges", [])
        if edges:
            caption = edges[0].get("node", {}).get("text", "")

        async with self.pool.acquire() as conn:
            # First ensure profile exists (might be missing if we are spidering from a post)
            profile_row = await conn.fetchrow("SELECT id FROM instagram_profiles WHERE platform_user_id = $1", uid)
            profile_uuid = profile_row['id'] if profile_row else None

            await conn.execute("""
                INSERT INTO instagram_posts (
                    platform_post_id, profile_id, media_type, caption,
                    likes_count, comments_count, platform_created_at,
                    collected_at, metadata
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, NOW(), $8)
                ON CONFLICT (platform_post_id) DO UPDATE SET
                    likes_count = EXCLUDED.likes_count,
                    comments_count = EXCLUDED.comments_count,
                    caption = EXCLUDED.caption,
                    metadata = EXCLUDED.metadata
            """,
            node.get("shortcode"), profile_uuid, node.get("__typename"), caption,
            node.get("edge_media_preview_like", {}).get("count", 0),
            node.get("edge_media_to_comment", {}).get("count", 0),
            datetime.fromtimestamp(node.get("taken_at_timestamp", time.time())),
            node
            )

    async def _download_node(self, node: dict, uid: str, entity_name: str, content_id: str, parent_node: dict | None = None):
        is_video = node.get("is_video", False)

        if is_video:
            url = node.get("video_url")
            ext = "mp4"
            content_type = "video"
        else:
            url = node.get("display_url")
            ext = "jpg"
            content_type = "post"

        if not url:
            return

        await self._content_aware_delay(content_type)
        await self.download_media({
            "entity_id": uid,
            "entity_name": entity_name,
            "content_type": content_type,
            "content_id": content_id,
            "url": url,
            "extension": ext,
            "source_url": f"https://www.instagram.com/p/{content_id.split('_')[0]}/",
            "raw": node if not parent_node else {"node": node, "parent": parent_node}
        })

    async def _warmup(self, client: httpx.AsyncClient):
        if self._warmed_up:
            return
        warmup_enabled = os.getenv("INSTA_WARMUP_ENABLED", "true").lower() == "true"
        if not warmup_enabled:
            self._warmed_up = True
            return
        try:
            logger.info("Warmup: initial pause (simulating app open)...")
            await asyncio.sleep(random.uniform(30, 60))
            await client.get(
                "https://www.instagram.com/",
                headers=self._headers(self._current_account),
            )
            logger.info("Warmup: browsing pause...")
            await asyncio.sleep(random.uniform(30, 60))
            logger.info("Warmup: pre-operation pause...")
            await asyncio.sleep(random.uniform(30, 60))
            self._warmed_up = True
            logger.info("Warmup sequence complete")
        except Exception as e:
            logger.debug("Warmup failed (non-fatal): %s", e)
            self._warmed_up = True

    def _check_session_age(self, username: str) -> bool:
        meta_file = self._session_dir / f"{username}.meta"
        if not meta_file.exists():
            self._save_session_meta(username)
            return True
        try:
            meta = json.loads(meta_file.read_text())
            created_at = meta.get("created_at", 0)
            jitter = random.randint(-3, 3) * 86400
            max_age = (self._session_max_age_days * 86400) + jitter
            if time.time() - created_at > max_age:
                logger.info("Session for %s expired (age %dd), forcing re-auth",
                            username, int((time.time() - created_at) // 86400))
                return False
            return True
        except Exception as e:
            # Corrupt or unreadable session meta means we don't know how old
            # the session is. Treat as expired and force re-auth — using a
            # potentially-old session is ban-bait.
            logger.warning(
                "Session meta for %s unreadable (%s), forcing re-auth",
                username, e,
            )
            return False

    def _save_session_meta(self, username: str):
        meta_file = self._session_dir / f"{username}.meta"
        meta_file.write_text(json.dumps({
            "created_at": time.time(),
            "username": username,
        }))

    async def download_media(self, item: dict):
        cid = item["content_id"]
        if self.is_known(cid):
            return

        filename = self.build_filename(
            entity_id=item["entity_id"],
            entity_name=item["entity_name"],
            content_type=item["content_type"],
            content_id=cid,
            extension=item.get("extension", "jpg"),
        )

        # Per-account subdirectory isolation
        dest_dir = self.account_media_dir / item["content_type"]
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / filename

        if dest.exists():
            return

        try:
            await self.rate_limiter.async_wait("instagram.com", OperationType.MEDIA_DOWNLOAD)
            cookies = self._get_session_cookies()
            async with httpx.AsyncClient(
                timeout=60, cookies=cookies, follow_redirects=True,
            ) as client:
                resp = await client.get(item["url"])
                resp.raise_for_status()
                data = resp.content

            sha = self.sha256_bytes(data)
            
            # Use updated save_file to also save metadata JSON
            metadata = {
                "entity_id": item["entity_id"],
                "entity_name": item["entity_name"],
                "content_type": item["content_type"],
                "content_id": cid,
                "source_url": item.get("source_url"),
                "collected_at": datetime.now(timezone.utc).isoformat(),
                "raw": item.get("raw", {})
            }
            
            # Temporary override media_dir to use the account-specific one for save_file
            old_media_dir = self.media_dir
            try:
                self.__dict__["media_dir_override"] = dest_dir
                # Note: save_file uses self.media_dir internally. 
                # We need a cleaner way or just use the Path directly.
                # Let's use save_json and manual write for now to be safe.
                
                # Atomic write
                fd, tmp_path = tempfile.mkstemp(dir=dest_dir, suffix=".tmp")
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, dest)
                
                # Save metadata
                self.save_json(metadata, dest_dir / f"{Path(filename).stem}_metadata.json")
                if "raw" in metadata:
                    self.save_json(metadata["raw"], dest_dir / f"{Path(filename).stem}_raw.json")
                
                self._known_ids.add(cid)
            finally:
                if "media_dir_override" in self.__dict__: del self.__dict__["media_dir_override"]

            self.rate_limiter.record_success("instagram.com")

            await self.insert_media_item(
                entity_id=item["entity_id"],
                entity_name=item["entity_name"],
                content_type=item["content_type"],
                content_id=cid,
                filename=filename,
                file_path=str(dest),
                file_size=len(data),
                sha256=sha,
                source_url=item.get("source_url"),
                metadata=metadata
            )
        except Exception as e:
            self.rate_limiter.record_failure("instagram.com")
            logger.error("Download failed %s: %s", cid, e)
            await self.send_to_dlq(item["entity_id"], cid, str(e))
