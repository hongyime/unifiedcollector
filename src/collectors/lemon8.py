import asyncio
import hashlib
import io
import json
import logging
import os
import re

import httpx

from src.core.base_collector import BaseCollector
from src.core.human_rate_limiter import OperationType

logger = logging.getLogger(__name__)

API_BASE = "https://api.lemon8-app.com"


def _enhance_image_url(url: str, target_width: int = 2160) -> str:
    """Remove CDN shrink/thumbnail params to get highest-quality image."""
    url = re.sub(r"[?&]w=\d+", "", url)
    url = re.sub(r"[?&]h=\d+", "", url)
    url = re.sub(r"[?&]q=\d+", "", url)
    url = re.sub(r"/w:\d+/", "/", url)
    url = re.sub(r"/h:\d+/", "/", url)
    url = re.sub(r"/thumb/\d+x\d+/", "/", url)
    url = re.sub(r"~tplv-[a-z0-9]+-[a-z0-9_]+\.[a-z]+", "", url)
    return url


class Lemon8Collector(BaseCollector):
    SOURCE_NAME = "lemon8"
    USE_HUMAN_RATE_LIMITER = True

    def __init__(self):
        super().__init__()
        self._cookies_file = os.getenv("LEMON8_COOKIES_FILE", "")
        self._cookies: dict[str, str] = {}
        if self._cookies_file and os.path.isfile(self._cookies_file):
            self._cookies = self._parse_cookies(self._cookies_file)
        self._sem = asyncio.Semaphore(2)

        self._min_width = int(os.getenv("LEMON8_MIN_WIDTH", "320"))
        self._min_height = int(os.getenv("LEMON8_MIN_HEIGHT", "320"))
        self._min_file_size = int(os.getenv("LEMON8_MIN_FILE_SIZE", "8192"))
        self._hq_width = int(os.getenv("LEMON8_HIGH_QUALITY_WIDTH", "2160"))
        self._enhance_urls = os.getenv("LEMON8_IMAGE_ENHANCEMENT", "true").lower() == "true"
        self._profile_photos = os.getenv("LEMON8_PROFILE_PHOTO_ENABLED", "true").lower() == "true"
        self._feed_enabled = os.getenv("LEMON8_FEED_ENABLED", "false").lower() == "true"
        self._tag_pages = int(os.getenv("LEMON8_TAG_PAGES", "10"))
        self._discovered_users: set[str] = set()
        self._discovered_tags: set[str] = set()

    @staticmethod
    def _parse_cookies(path: str) -> dict[str, str]:
        cookies: dict[str, str] = {}
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("#") or not line:
                        continue
                    parts = line.split("\t")
                    if len(parts) >= 7:
                        cookies[parts[5]] = parts[6]
        except Exception:
            pass
        return cookies

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.user_agents.get_for_domain("lemon8-app.com"),
            "Accept": "application/json",
            "Referer": "https://www.lemon8-app.com/",
        }

    async def collect(self, targets: list[str]):
        async with httpx.AsyncClient(
            timeout=30, cookies=self._cookies, headers=self._headers(), follow_redirects=True,
        ) as client:
            if self._feed_enabled:
                await self._collect_feed(client)

            for username in targets:
                if self._stop.is_set():
                    break
                if username.startswith("#"):
                    logger.info("Collecting lemon8/tag/%s", username)
                    try:
                        await self._collect_tag(client, username.lstrip("#"))
                    except Exception as e:
                        logger.error("Failed lemon8/tag/%s: %s", username, e)
                    continue

                logger.info("Collecting lemon8/%s", username)
                try:
                    await self._collect_user(client, username)
                    await self.checkpoint.save_progress(username)
                except Exception as e:
                    logger.error("Failed lemon8/%s: %s", username, e)
                    await self.send_to_dlq(username, username, str(e))

            for discovered in list(self._discovered_users)[:20]:
                if self._stop.is_set():
                    break
                if discovered not in targets:
                    try:
                        await self._collect_user(client, discovered)
                    except Exception:
                        pass

    async def _collect_user(self, client: httpx.AsyncClient, username: str):
        await self.rate_limiter.async_wait("lemon8-app.com", OperationType.PROFILE_VIEW)
        profile_url = f"https://www.lemon8-app.com/{username}"
        resp = await client.get(profile_url)
        resp.raise_for_status()
        html = resp.text

        user_id = username
        marker = '"user_id":"'
        idx = html.find(marker)
        if idx != -1:
            end = html.find('"', idx + len(marker))
            user_id = html[idx + len(marker):end]

        self.rate_limiter.record_success("lemon8-app.com")

        if self._profile_photos:
            avatar_url = self._extract_avatar(html)
            if avatar_url:
                await self.download_media({
                    "entity_id": user_id,
                    "entity_name": username,
                    "content_type": "profile_photo",
                    "content_id": f"profile_{user_id}",
                    "url": avatar_url,
                    "extension": "jpg",
                })

        posts = self._extract_posts(html, user_id, username)
        for post in posts:
            if self._stop.is_set():
                break
            for media_item in post.get("media", []):
                if not self.is_known(media_item["content_id"]):
                    await self.download_media(media_item)

    def _extract_avatar(self, html: str) -> str | None:
        for marker in ['"avatar_url":"', '"avatarUrl":"', '"profile_image":"']:
            idx = html.find(marker)
            if idx != -1:
                end = html.find('"', idx + len(marker))
                url = html[idx + len(marker):end].replace("\\u002F", "/")
                if url and url.startswith("http"):
                    return _enhance_image_url(url) if self._enhance_urls else url
        return None

    def _extract_posts(self, html: str, user_id: str, username: str) -> list[dict]:
        posts: list[dict] = []

        marker = '"itemList":'
        idx = html.find(marker)
        if idx == -1:
            marker = '"postList":'
            idx = html.find(marker)
        if idx == -1:
            return self._extract_images_from_html(html, user_id, username)

        try:
            bracket = html.find("[", idx)
            depth = 0
            end = bracket
            for i, ch in enumerate(html[bracket:], bracket):
                if ch == "[":
                    depth += 1
                elif ch == "]":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            items = json.loads(html[bracket:end])

            for item in items:
                post_id = str(item.get("id", item.get("post_id", "")))
                media_items = []

                image_list = item.get("image_list", item.get("images", []))
                for i, img in enumerate(image_list):
                    url = ""
                    if isinstance(img, dict):
                        url = (img.get("url_list", [None])[0]
                               if img.get("url_list")
                               else img.get("url", ""))
                    elif isinstance(img, str):
                        url = img
                    if url:
                        if self._enhance_urls:
                            url = _enhance_image_url(url, self._hq_width)
                        media_items.append({
                            "entity_id": user_id,
                            "entity_name": username,
                            "content_type": "post",
                            "content_id": f"{post_id}_{i}",
                            "url": url,
                            "extension": "jpg",
                        })

                video_list = item.get("video_list", item.get("videos", []))
                for i, vid in enumerate(video_list):
                    url = ""
                    if isinstance(vid, dict):
                        url = (vid.get("url_list", [None])[0]
                               if vid.get("url_list")
                               else vid.get("play_addr", {}).get("url_list", [None])[0]
                               if isinstance(vid.get("play_addr"), dict)
                               else vid.get("url", ""))
                    elif isinstance(vid, str):
                        url = vid
                    if url:
                        media_items.append({
                            "entity_id": user_id,
                            "entity_name": username,
                            "content_type": "video",
                            "content_id": f"{post_id}_v{i}",
                            "url": url,
                            "extension": "mp4",
                        })

                posts.append({"post_id": post_id, "media": media_items})
        except (json.JSONDecodeError, IndexError):
            return self._extract_images_from_html(html, user_id, username)

        return posts

    def _extract_images_from_html(self, html: str, user_id: str, username: str) -> list[dict]:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        media: list[dict] = []

        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src")
            if not src or "avatar" in src.lower() or "icon" in src.lower():
                continue
            if self._enhance_urls:
                src = _enhance_image_url(src, self._hq_width)
            content_id = hashlib.sha256(src.encode()).hexdigest()[:16]
            media.append({
                "entity_id": user_id,
                "entity_name": username,
                "content_type": "post",
                "content_id": content_id,
                "url": src,
                "extension": "jpg",
            })

        for vid in soup.find_all("video"):
            src = vid.get("src") or vid.find("source", src=True)
            if isinstance(src, str) and src:
                content_id = hashlib.sha256(src.encode()).hexdigest()[:16]
                media.append({
                    "entity_id": user_id,
                    "entity_name": username,
                    "content_type": "video",
                    "content_id": content_id,
                    "url": src,
                    "extension": "mp4",
                })

        if media:
            return [{"post_id": "html_extract", "media": media}]
        return []

    async def _collect_feed(self, client: httpx.AsyncClient):
        try:
            await self.rate_limiter.async_wait("lemon8-app.com", OperationType.PROFILE_VIEW)
            resp = await client.get(f"{API_BASE}/api/feed/recommend/")
            if resp.status_code != 200:
                logger.debug("Feed API returned %d", resp.status_code)
                return
            data = resp.json()
            items = data.get("data", {}).get("items", [])
            for item in items:
                if self._stop.is_set():
                    break
                self._extract_discoveries(item)
            logger.info("Feed discovery: %d users, %d tags",
                        len(self._discovered_users), len(self._discovered_tags))
        except Exception as e:
            logger.debug("Feed collection failed: %s", e)

    async def _collect_tag(self, client: httpx.AsyncClient, tag: str):
        cursor = ""
        for page in range(self._tag_pages):
            if self._stop.is_set():
                break
            await self.rate_limiter.async_wait("lemon8-app.com", OperationType.PAGINATION)
            try:
                params = {"keyword": tag, "count": 20}
                if cursor:
                    params["cursor"] = cursor
                resp = await client.get(f"{API_BASE}/api/feed/search/", params=params)
                if resp.status_code != 200:
                    break
                data = resp.json()
                items = data.get("data", {}).get("items", [])
                if not items:
                    break

                for item in items:
                    self._extract_discoveries(item)
                    author = item.get("author", {})
                    uid = str(author.get("user_id", ""))
                    uname = author.get("unique_id", "") or author.get("nickname", "")
                    if not uid or not uname:
                        continue

                    images = item.get("image_list", item.get("images", []))
                    for i, img in enumerate(images):
                        if self._stop.is_set():
                            break
                        url = ""
                        if isinstance(img, dict):
                            url = (img.get("url_list", [None])[0]
                                   if img.get("url_list")
                                   else img.get("url", ""))
                        elif isinstance(img, str):
                            url = img
                        if url:
                            if self._enhance_urls:
                                url = _enhance_image_url(url, self._hq_width)
                            post_id = str(item.get("id", item.get("post_id", "")))
                            cid = f"{post_id}_{i}"
                            if not self.is_known(cid):
                                await self.download_media({
                                    "entity_id": uid,
                                    "entity_name": uname,
                                    "content_type": "post",
                                    "content_id": cid,
                                    "url": url,
                                    "extension": "jpg",
                                })

                cursor = data.get("data", {}).get("cursor", "")
                if not cursor or not data.get("data", {}).get("has_more", False):
                    break
                self.rate_limiter.record_success("lemon8-app.com")
            except Exception as e:
                logger.debug("Tag page %d for '%s' failed: %s", page, tag, e)
                break

    def _extract_discoveries(self, item: dict):
        author = item.get("author", {})
        uid = author.get("unique_id", "") or author.get("nickname", "")
        if uid:
            self._discovered_users.add(uid)

        for tag in item.get("hashtags", item.get("text_extra", [])):
            if isinstance(tag, dict):
                name = tag.get("hashtag_name", tag.get("name", ""))
            elif isinstance(tag, str):
                name = tag
            else:
                continue
            if name:
                self._discovered_tags.add(name)

    async def _persist_discoveries(self):
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                for uid in self._discovered_users:
                    await conn.execute(
                        """
                        INSERT INTO lemon8_discovered (entity_type, entity_id, entity_name, source)
                        VALUES ('user', $1, $1, 'feed')
                        ON CONFLICT (entity_id) DO NOTHING
                        """,
                        uid,
                    )
                for tag in self._discovered_tags:
                    await conn.execute(
                        """
                        INSERT INTO lemon8_discovered (entity_type, entity_id, entity_name, source)
                        VALUES ('tag', $1, $1, 'feed')
                        ON CONFLICT (entity_id) DO NOTHING
                        """,
                        tag,
                    )
        except Exception as e:
            logger.debug("Discovery persist failed: %s", e)

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
            await self.rate_limiter.async_wait("lemon8-app.com", OperationType.MEDIA_DOWNLOAD)
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                resp = await client.get(item["url"])
                resp.raise_for_status()
                data = resp.content

            if len(data) < self._min_file_size:
                return

            is_video = item.get("extension") in ("mp4", "webm")
            width, height = None, None

            if not is_video:
                try:
                    from PIL import Image
                    img = Image.open(io.BytesIO(data))
                    width, height = img.size
                    if width < self._min_width or height < self._min_height:
                        return
                except Exception:
                    pass

            sha = self.sha256_bytes(data)
            self.save_file(data, filename)
            self.rate_limiter.record_success("lemon8-app.com")
            self.circuit_breaker.record_success()

            await self.insert_media_item(
                entity_id=item["entity_id"],
                entity_name=item["entity_name"],
                content_type=item["content_type"],
                content_id=cid,
                filename=filename,
                file_path=str(dest),
                file_size=len(data),
                width=width,
                height=height,
                sha256=sha,
                source_url=item.get("url"),
            )
        except Exception as e:
            self.rate_limiter.record_failure("lemon8-app.com")
            self.circuit_breaker.record_failure()
            logger.error("Download failed %s: %s", cid, e)
            await self.send_to_dlq(item["entity_id"], cid, str(e))
