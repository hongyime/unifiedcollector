import asyncio
import json
import logging
import math
import os
import random
import time
import uuid
from datetime import datetime
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
        self._account_browser_cookies: dict[str, str] = {}
        for i in range(1, 20):
            name = os.getenv(f"INSTA_ACCOUNT_{i}_NAME", "")
            px = os.getenv(f"INSTA_ACCOUNT_{i}_PROXY", "")
            browser = os.getenv(f"INSTA_ACCOUNT_{i}_BROWSER", "")
            if name and px:
                self._account_proxies[name] = px.strip()
            if name and browser:
                self._account_browser_cookies[name] = browser.strip()

        self._loader = None
        self._current_account = None

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
        if not username or not password:
            return False

        # Try browser cookie import first
        if account.name in self._account_browser_cookies:
            cookie_path = self._account_browser_cookies[account.name]
            if self._login_from_cookies(username, cookie_path):
                return True
            logger.info("Cookie login failed for %s, falling back to session/password", username)

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
            logger.error("Login failed for %s: %s", username, e)
            self.account_pool.record_error(account.name)
            return False

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
        hour = datetime.now().hour
        if hour in NIGHT_HOURS:
            return random.uniform(2.5, 4.0)
        if hour in RISKY_HOURS:
            return 1.5
        return 1.0

    def _check_daily_quota(self, account_name: str) -> bool:
        today = datetime.now().strftime("%Y-%m-%d")
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
        today = datetime.now().strftime("%Y-%m-%d")
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
            for username in targets:
                if self._stop.is_set():
                    break

                if self._current_account and not self._check_daily_quota(self._current_account.name):
                    next_acct = self.account_pool.get_next(exclude=self._current_account.name)
                    if next_acct and self._check_daily_quota(next_acct.name):
                        logger.info("Switching to %s (daily quota exceeded on %s)",
                                    next_acct.name, self._current_account.name)
                        await asyncio.sleep(random.uniform(ACCOUNT_SWITCH_DELAY_MIN, ACCOUNT_SWITCH_DELAY_MAX))
                        self._current_account = next_acct
                        await asyncio.get_event_loop().run_in_executor(
                            None, self._login_account, next_acct,
                        )
                    else:
                        logger.error("All accounts hit daily quota, stopping")
                        break

                if SLIDING_WINDOW_ENABLED and not self._sliding_limiter.check("instagram.com"):
                    wait = self._sliding_limiter.time_until_allowed("instagram.com")
                    logger.warning("Sliding window limit hit, waiting %.0fs", wait)
                    await asyncio.sleep(min(wait, 600))
                    if not self._sliding_limiter.check("instagram.com"):
                        logger.error("Still rate-limited after wait, stopping")
                        break

                tod_mult = self._time_of_day_multiplier()
                if tod_mult > 1.0:
                    extra = self.rate_limiter.base_delay * (tod_mult - 1.0)
                    logger.debug("Time-of-day multiplier %.1fx, extra delay %.1fs", tod_mult, extra)
                    await asyncio.sleep(extra)

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

    async def _handle_rate_limit(self, error):
        if isinstance(self.rate_limiter, HumanLikeRateLimiter):
            self.rate_limiter.trigger_emergency_cooldown("instagram.com")
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
        await self.rate_limiter.async_wait("instagram.com", OperationType.PROFILE_VIEW)

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

        follower_count = user_data.get("edge_followed_by", {}).get("count", 0)
        if self._max_followers and follower_count > self._max_followers:
            logger.info("Skipping %s: %d followers > max %d", username, follower_count, self._max_followers)
            return

        self.rate_limiter.record_success("instagram.com")

        profile_pic = user_data.get("profile_pic_url_hd") or user_data.get("profile_pic_url")
        if profile_pic:
            changed, path = await self._photo_tracker.check_and_download(
                profile_pic, uid, "instagram", self.media_dir / "profiles",
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
                )
            elif not changed:
                cid = f"profile_{uid}"
                if not self.is_known(cid):
                    await self.download_media({
                        "entity_id": uid,
                        "entity_name": entity_name,
                        "content_type": "profile_photo",
                        "content_id": cid,
                        "url": profile_pic,
                        "extension": "jpg",
                    })

        await self._collect_posts(client, uid, entity_name)
        await self._collect_stories(uid, entity_name)
        await self._collect_highlights(client, uid, entity_name)

    async def _collect_posts(self, client: httpx.AsyncClient, uid: str, entity_name: str):
        end_cursor = ""
        has_next = True
        page_depth = 0

        while has_next and not self._stop.is_set():
            await self.rate_limiter.async_wait(
                "instagram.com", OperationType.PAGINATION, pagination_depth=page_depth,
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
                    if resp.status_code == 429:
                        await self._handle_rate_limit(Exception("429"))
                        break
                    resp.raise_for_status()
                except Exception as e:
                    self.rate_limiter.record_failure("instagram.com")
                    self.circuit_breaker.record_failure()
                    logger.error("GraphQL request failed: %s", e)
                    break

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

            for edge in edges:
                if self._stop.is_set():
                    break
                node = edge.get("node", {})
                await self._process_post(node, uid, entity_name)

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
                    })
        except Exception as e:
            logger.debug("Highlights collection failed for %s: %s", entity_name, e)

    async def _process_post(self, node: dict, uid: str, entity_name: str):
        shortcode = node.get("shortcode", "")
        typename = node.get("__typename", "")

        if typename == "GraphSidecar":
            sidecar_edges = (node.get("edge_sidecar_to_children", {})
                             .get("edges", []))
            for i, se in enumerate(sidecar_edges):
                child = se.get("node", {})
                cid = f"{shortcode}_{i}"
                if not self.is_known(cid):
                    await self._download_node(child, uid, entity_name, cid)
        else:
            if not self.is_known(shortcode):
                await self._download_node(node, uid, entity_name, shortcode)

    async def _download_node(self, node: dict, uid: str, entity_name: str, content_id: str):
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
        except Exception:
            pass
        return True

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

        dest = self.media_dir / filename
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
            self.save_file(data, filename)
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
            )
        except Exception as e:
            self.rate_limiter.record_failure("instagram.com")
            logger.error("Download failed %s: %s", cid, e)
            await self.send_to_dlq(item["entity_id"], cid, str(e))
