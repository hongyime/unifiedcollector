import asyncio
import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path

import httpx

from src.core.base_collector import BaseCollector

logger = logging.getLogger(__name__)

REQUIRED_COOKIES = {"sessionid", "tt_csrf_token", "ttwid", "msToken", "tt_chain_token", "sid_guard", "passport_csrf_token"}
RECOMMENDED_COOKIES = {"s_v_web_id", "odin_tt", "cmpl_token"}


def validate_cookies(cookies_file: str) -> dict:
    """Validate a Netscape-format cookies file for required TikTok cookies."""
    result = {"valid": False, "total": 0, "present": set(), "missing": set(), "expired": set(), "warnings": []}
    if not cookies_file or not os.path.isfile(cookies_file):
        result["warnings"].append("Cookies file not found")
        return result

    import time
    now = int(time.time())

    try:
        with open(cookies_file) as f:
            for line in f:
                line = line.strip()
                if line.startswith("#") or not line:
                    continue
                parts = line.split("\t")
                if len(parts) >= 7:
                    name, value = parts[5], parts[6]
                    expiry = int(parts[4]) if parts[4].isdigit() else 0
                    result["total"] += 1
                    result["present"].add(name)
                    if expiry > 0 and expiry < now:
                        result["expired"].add(name)
    except Exception as e:
        result["warnings"].append(f"Parse error: {e}")
        return result

    result["missing"] = REQUIRED_COOKIES - result["present"]
    expired_required = result["expired"] & REQUIRED_COOKIES
    if expired_required:
        result["warnings"].append(f"Expired required cookies: {expired_required}")

    result["valid"] = len(result["missing"]) == 0 and len(expired_required) == 0
    if not result["valid"]:
        missing_recommended = RECOMMENDED_COOKIES - result["present"]
        if missing_recommended:
            result["warnings"].append(f"Missing recommended: {missing_recommended}")

    return result


class TiktokCollector(BaseCollector):
    SOURCE_NAME = "tiktok"

    def __init__(self):
        super().__init__()
        self._cookies_file = os.getenv("TIKTOK_COOKIES_FILE", "")
        self._session_id = os.getenv("TIKTOK_SESSION_ID", "")
        self._min_sleep = float(os.getenv("TIKTOK_MIN_SLEEP", "0.5"))
        self._max_sleep = float(os.getenv("TIKTOK_MAX_SLEEP", "2.0"))
        self._retries = int(os.getenv("TIKTOK_RETRIES", "2"))
        self._timeout = int(os.getenv("TIKTOK_TIMEOUT_SECONDS", "300"))
        self._browser_fallback = os.getenv("TIKTOK_BROWSER_FALLBACK_ENABLED", "true").lower() == "true"
        self._ytdlp_fallback = os.getenv("TIKTOK_YTDLP_FALLBACK_ENABLED", "true").lower() == "true"
        self._use_gallery_dl = self._check_tool("gallery-dl")
        self._use_yt_dlp = self._check_tool("yt-dlp")
        self._sem = asyncio.Semaphore(2)
        self._cookies_valid = False
        self._tracker_file = Path(os.getenv("TIKTOK_TRACKER_FILE", "data/tiktok_tracker.json"))
        self._tracked_ids: set[str] = set()

        if self._cookies_file:
            result = validate_cookies(self._cookies_file)
            self._cookies_valid = result["valid"]
            if not result["valid"]:
                for w in result.get("warnings", []):
                    logger.warning("TikTok cookie issue: %s", w)
                if result["missing"]:
                    logger.warning("Missing required cookies: %s", result["missing"])
            else:
                logger.info("TikTok cookies validated: %d cookies, all required present", result["total"])

    @staticmethod
    def _check_tool(name: str) -> bool:
        try:
            subprocess.run([name, "--version"], capture_output=True, check=True)
            return True
        except (FileNotFoundError, subprocess.CalledProcessError):
            return False

    async def collect(self, targets: list[str]):
        await self._load_tracker_state()
        for username in targets:
            if self._stop.is_set():
                break
            username = username.lstrip("@")

            if self._is_invalid_username(username):
                logger.warning("Invalid TikTok username: %s", username)
                continue

            logger.info("Collecting tiktok/%s", username)
            try:
                await self._collect_user(username)
                await self.checkpoint.save_progress(username)
            except Exception as e:
                logger.error("Failed tiktok/%s: %s", username, e)
                await self.send_to_dlq(username, username, str(e))

    @staticmethod
    def _is_invalid_username(username: str) -> bool:
        if len(username) < 2 or len(username) > 24:
            return True
        if not username.replace("_", "").replace(".", "").isalnum():
            return True
        return False

    async def _collect_user(self, username: str):
        profile_url = f"https://www.tiktok.com/@{username}"

        if self._use_gallery_dl:
            success = await self._collect_via_gallery_dl(username, profile_url)
            if success:
                return

        if self._use_yt_dlp and self._ytdlp_fallback:
            success = await self._collect_via_yt_dlp(username, profile_url)
            if success:
                return

        if self._browser_fallback:
            success = await self._collect_via_playwright(username)
            if success:
                return

        await self._collect_via_api(username)

    async def _collect_via_gallery_dl(self, username: str, profile_url: str) -> bool:
        with tempfile.TemporaryDirectory() as tmpdir:
            cmd = ["gallery-dl", "--dest", tmpdir, "--no-mtime"]
            if self._cookies_file:
                cmd.extend(["--cookies", self._cookies_file])
            cmd.append(profile_url)

            loop = asyncio.get_event_loop()
            proc = await loop.run_in_executor(
                None,
                lambda: subprocess.run(cmd, capture_output=True, text=True, timeout=self._timeout),
            )

            if proc.returncode != 0:
                logger.warning("gallery-dl failed for %s: %s", username, proc.stderr[:200])
                return False

            await self._ingest_tmpdir(tmpdir, username)
            return True

    async def _collect_via_yt_dlp(self, username: str, profile_url: str) -> bool:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_tmpl = os.path.join(tmpdir, "%(id)s.%(ext)s")
            cmd = [
                "yt-dlp",
                "--impersonate", "chrome",
                "--write-thumbnail",
                "--no-overwrites",
                "-o", output_tmpl,
                "--max-downloads", "50",
                "--retries", str(self._retries),
                "--socket-timeout", "30",
            ]
            if self._cookies_file:
                cmd.extend(["--cookies", self._cookies_file])
            cmd.append(profile_url)

            loop = asyncio.get_event_loop()
            proc = await loop.run_in_executor(
                None,
                lambda: subprocess.run(cmd, capture_output=True, text=True, timeout=self._timeout),
            )

            if proc.returncode not in (0, 101):
                logger.warning("yt-dlp failed for %s: %s", username, proc.stderr[:200])
                return False

            await self._ingest_tmpdir(tmpdir, username)
            return True

    async def _ingest_tmpdir(self, tmpdir: str, username: str):
        import random
        for f in Path(tmpdir).rglob("*"):
            if self._stop.is_set():
                break
            if not f.is_file():
                continue
            ext = f.suffix.lstrip(".").lower()
            if ext not in ("jpg", "jpeg", "png", "mp4", "webm", "gif", "webp"):
                continue

            cid = f.stem
            if self.is_known(cid):
                continue

            data = f.read_bytes()
            content_type = "video" if ext in ("mp4", "webm") else "post"

            await self.download_media({
                "entity_id": username,
                "entity_name": username,
                "content_type": content_type,
                "content_id": cid,
                "data": data,
                "extension": ext if ext != "jpeg" else "jpg",
            })

            await asyncio.sleep(random.uniform(self._min_sleep, self._max_sleep))

    async def _collect_via_api(self, username: str):
        await self.wait_rate_limit("tiktok.com")
        cookies = {}
        if self._session_id:
            cookies["sessionid"] = self._session_id

        try:
            async with httpx.AsyncClient(
                timeout=30, cookies=cookies, follow_redirects=True,
            ) as client:
                resp = await client.get(
                    f"https://www.tiktok.com/@{username}",
                    headers={"User-Agent": self.user_agents.get_for_domain("tiktok.com")},
                )
                resp.raise_for_status()

                html = resp.text
                marker = '"ItemModule":'
                start = html.find(marker)
                if start == -1:
                    logger.warning("No ItemModule found for %s", username)
                    return

                bracket_start = html.find("{", start + len(marker))
                depth = 0
                end = bracket_start
                for i, ch in enumerate(html[bracket_start:], bracket_start):
                    if ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            end = i + 1
                            break

                items_json = html[bracket_start:end]
                items = json.loads(items_json)

                for video_id, video_data in items.items():
                    if self._stop.is_set():
                        break
                    if self.is_known(video_id):
                        continue
                    cover = video_data.get("video", {}).get("cover")
                    if cover:
                        await self.download_media({
                            "entity_id": username,
                            "entity_name": username,
                            "content_type": "thumbnail",
                            "content_id": video_id,
                            "url": cover,
                            "extension": "jpg",
                        })
        except Exception as e:
            logger.error("API fallback failed for %s: %s", username, e)
            raise

    _STEALTH_SCRIPT = (
        "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        "Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });"
        "Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });"
    )

    _CHROMIUM_ARGS = [
        "--disable-blink-features=AutomationControlled",
        "--disable-dev-shm-usage",
        "--no-sandbox",
    ]

    async def _collect_via_playwright(self, username: str) -> bool:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.debug("Playwright not installed, skipping browser fallback")
            return False

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True, args=self._CHROMIUM_ARGS,
                )
                context = await browser.new_context(
                    viewport={"width": 1920, "height": 1080},
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                    ),
                    locale="en-US",
                    timezone_id="America/New_York",
                    extra_http_headers={
                        "Accept-Language": "en-US,en;q=0.9",
                        "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
                        "sec-ch-ua-mobile": "?0",
                        "sec-ch-ua-platform": '"Windows"',
                    },
                )
                if self._cookies_file and os.path.isfile(self._cookies_file):
                    cookies = self._parse_netscape_cookies(self._cookies_file)
                    if cookies:
                        await context.add_cookies(cookies)

                page = await context.new_page()
                await page.add_init_script(self._STEALTH_SCRIPT)

                await page.goto(f"https://www.tiktok.com/@{username}", wait_until="domcontentloaded")
                try:
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                await page.wait_for_timeout(3000)

                for _ in range(3):
                    await page.evaluate("window.scrollBy(0, 800)")
                    await page.wait_for_timeout(500)

                video_links = await page.eval_on_selector_all(
                    'a[href*="/video/"]',
                    "els => els.map(e => e.href)",
                )

                video_ids = set()
                for link in video_links:
                    parts = link.rstrip("/").split("/")
                    if parts:
                        vid = parts[-1]
                        if vid.isdigit() and vid not in self._tracked_ids:
                            video_ids.add(vid)

                downloaded = 0
                for vid in list(video_ids)[:50]:
                    if self._stop.is_set():
                        break
                    if self.is_known(vid):
                        continue

                    cdn_url = await self._intercept_video_cdn(
                        page, context, username, vid,
                    )
                    if cdn_url:
                        try:
                            api_resp = await page.request.get(
                                cdn_url,
                                headers={
                                    "Referer": "https://www.tiktok.com/",
                                    "Origin": "https://www.tiktok.com",
                                },
                                timeout=120000,
                            )
                            if api_resp.status in (200, 206):
                                data = await api_resp.body()
                                if len(data) > 10000:
                                    await self.download_media({
                                        "entity_id": username,
                                        "entity_name": username,
                                        "content_type": "video",
                                        "content_id": vid,
                                        "data": data,
                                        "extension": "mp4",
                                    })
                                    downloaded += 1
                                    continue
                        except Exception as e:
                            logger.debug("CDN download %s failed: %s", vid, e)

                    poster = await page.query_selector("video")
                    if poster:
                        src = await poster.get_attribute("poster")
                        if src:
                            await self.download_media({
                                "entity_id": username,
                                "entity_name": username,
                                "content_type": "thumbnail",
                                "content_id": vid,
                                "url": src,
                                "extension": "jpg",
                            })

                await browser.close()
            logger.info("Playwright: %d videos, %d CDN downloads for @%s",
                        len(video_ids), downloaded, username)
            return bool(video_ids)
        except Exception as e:
            logger.warning("Playwright fallback failed for %s: %s", username, e)
            return False

    async def _intercept_video_cdn(self, page, context, username: str, vid: str) -> str | None:
        captured = []

        def on_request(request):
            url = request.url
            if (
                not captured
                and ("mime_type=video" in url or url.endswith(".mp4"))
                and ("tiktok.com" in url or "tiktokcdn.com" in url or "tiktokv.com" in url)
                and not url.startswith("blob:")
            ):
                captured.append(url)

        page.on("request", on_request)
        try:
            await page.goto(
                f"https://www.tiktok.com/@{username}/video/{vid}",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            try:
                await page.wait_for_selector("video", timeout=10000)
                await page.evaluate(
                    "const v = document.querySelector('video');"
                    "if (v) { v.muted = true; v.play(); }"
                )
            except Exception:
                pass

            deadline = asyncio.get_event_loop().time() + 10
            while not captured and asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(0.2)
        finally:
            page.remove_listener("request", on_request)

        if captured:
            return captured[0]

        video_el = await page.query_selector("video")
        if video_el:
            src = await video_el.get_attribute("src")
            if src and not src.startswith("blob:"):
                return src
        return None

    @staticmethod
    def _parse_netscape_cookies(cookies_file: str) -> list[dict]:
        cookies = []
        try:
            with open(cookies_file) as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split("\t")
                    if len(parts) < 7:
                        continue
                    domain = parts[0].strip()
                    if not domain:
                        continue
                    if domain.startswith(("http://", "https://")):
                        domain = domain.split("://", 1)[1]
                    expires = int(parts[4]) if parts[4] not in ("0", "") else -1
                    cookie = {
                        "name": parts[5],
                        "value": parts[6],
                        "domain": domain,
                        "path": parts[2] or "/",
                        "secure": parts[3].lower() == "true",
                    }
                    if expires > 0:
                        cookie["expires"] = expires
                    cookies.append(cookie)
        except Exception as e:
            logger.debug("Failed to parse cookies file: %s", e)
        return cookies

    async def _load_tracker_state(self):
        if self._pool:
            try:
                async with self._pool.acquire() as conn:
                    rows = await conn.fetch(
                        "SELECT video_id FROM tiktok_download_tracker WHERE status = 'complete'"
                    )
                    self._tracked_ids = {r["video_id"] for r in rows}
            except Exception:
                pass

        if not self._tracked_ids and self._tracker_file.exists():
            try:
                data = json.loads(self._tracker_file.read_text())
                self._tracked_ids = set(data.get("completed", []))
            except Exception:
                pass
        logger.info("TikTok tracker loaded %d known video IDs", len(self._tracked_ids))

    async def _record_download(self, username: str, video_id: str, file_path: str):
        self._tracked_ids.add(video_id)

        if self._pool:
            try:
                async with self._pool.acquire() as conn:
                    await conn.execute(
                        """
                        INSERT INTO tiktok_download_tracker (username, video_id, file_path, status)
                        VALUES ($1, $2, $3, 'complete')
                        ON CONFLICT (video_id) DO NOTHING
                        """,
                        username, video_id, file_path,
                    )
            except Exception as e:
                logger.debug("Tracker DB write failed: %s", e)

        try:
            self._tracker_file.parent.mkdir(parents=True, exist_ok=True)
            self._tracker_file.write_text(json.dumps({
                "completed": list(self._tracked_ids),
            }))
        except Exception:
            pass

    async def download_media(self, item: dict):
        cid = item["content_id"]
        if self.is_known(cid):
            return

        filename = self.build_filename(
            entity_id=item["entity_id"],
            entity_name=item["entity_name"],
            content_type=item["content_type"],
            content_id=cid,
            extension=item.get("extension", "mp4"),
        )

        dest = self.media_dir / filename
        if dest.exists():
            return

        try:
            if "data" in item:
                data = item["data"]
            elif "url" in item:
                await self.wait_rate_limit("tiktok.com")
                async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                    resp = await client.get(
                        item["url"],
                        headers={"User-Agent": self.user_agents.get_for_domain("tiktok.com")},
                    )
                    resp.raise_for_status()
                    data = resp.content
            else:
                return

            sha = self.sha256_bytes(data)
            self.save_file(data, filename)
            self.rate_limiter.record_success("tiktok.com")
            self.circuit_breaker.record_success()

            await self.insert_media_item(
                entity_id=item["entity_id"],
                entity_name=item["entity_name"],
                content_type=item["content_type"],
                content_id=cid,
                filename=filename,
                file_path=str(dest),
                file_size=len(data),
                sha256=sha,
                source_url=item.get("url"),
            )
            await self._record_download(item["entity_id"], cid, str(dest))
        except Exception as e:
            self.rate_limiter.record_failure("tiktok.com")
            self.circuit_breaker.record_failure()
            logger.error("Download failed %s: %s", cid, e)
            await self.send_to_dlq(item["entity_id"], cid, str(e))
