import asyncio
import json
import logging
import os
import pickle
import subprocess
import tempfile
from pathlib import Path

import httpx

from src.core.base_collector import BaseCollector

logger = logging.getLogger(__name__)

YT_API_BASE = "https://www.googleapis.com/youtube/v3"


class YoutubeCollector(BaseCollector):
    SOURCE_NAME = "youtube"

    def __init__(self):
        super().__init__()
        self._api_key = os.getenv("YOUTUBE_API_KEY", "")
        self._cookie_browser = os.getenv("YOUTUBE_COOKIE_BROWSER", "")
        self._max_duration = int(os.getenv("YOUTUBE_MAX_VIDEO_DURATION_MINUTES", "0"))
        self._ytdlp_format = os.getenv("YOUTUBE_YTDLP_FORMAT", "bestvideo+bestaudio/best")
        self._merge_format = os.getenv("YOUTUBE_MERGE_FORMAT", "mp4")
        self._download_delay = float(os.getenv("YOUTUBE_DOWNLOAD_DELAY", "5.0"))
        self._api_delay = float(os.getenv("YOUTUBE_API_DELAY", "3.0"))
        self._max_concurrent = int(os.getenv("YOUTUBE_MAX_CONCURRENT_DOWNLOADS", "3"))
        self._use_yt_dlp = self._check_yt_dlp()
        self._sem = asyncio.Semaphore(self._max_concurrent)
        self._download_videos = os.getenv("YOUTUBE_DOWNLOAD_VIDEOS", "true").lower() == "true"
        self._oauth_pickle = Path(os.getenv("YOUTUBE_OAUTH_PICKLE", "data/youtube_oauth.pickle"))
        self._oauth_credentials = None

    @staticmethod
    def _check_yt_dlp() -> bool:
        try:
            subprocess.run(["yt-dlp", "--version"], capture_output=True, check=True)
            return True
        except (FileNotFoundError, subprocess.CalledProcessError):
            return False

    def _load_oauth_credentials(self) -> str | None:
        if self._oauth_credentials:
            return self._oauth_credentials

        if self._oauth_pickle.exists():
            try:
                with open(self._oauth_pickle, "rb") as f:
                    creds = pickle.load(f)

                try:
                    from google.auth.transport.requests import Request
                    if hasattr(creds, "expired") and creds.expired and creds.refresh_token:
                        creds.refresh(Request())
                        with open(self._oauth_pickle, "wb") as f:
                            pickle.dump(creds, f)
                        logger.info("YouTube OAuth token refreshed")
                except ImportError:
                    logger.debug("google-auth not installed, using cached token as-is")
                except Exception as e:
                    logger.warning("OAuth token refresh failed: %s", e)

                if hasattr(creds, "token"):
                    self._oauth_credentials = creds.token
                    return self._oauth_credentials
            except Exception as e:
                logger.debug("OAuth pickle load failed: %s", e)

        return None

    async def collect(self, targets: list[str]):
        if not self._api_key:
            oauth_token = self._load_oauth_credentials()
            if oauth_token:
                logger.info("Using OAuth credentials for YouTube API")

        for target in targets:
            if self._stop.is_set():
                break
            logger.info("Collecting youtube/%s", target)
            try:
                await self._collect_channel(target)
                await self.checkpoint.save_progress(target)
            except Exception as e:
                logger.error("Failed youtube/%s: %s", target, e)
                await self.send_to_dlq(target, target, str(e))

    async def _collect_channel(self, channel_input: str):
        channel_id, channel_name = await self._resolve_channel(channel_input)
        if not channel_id:
            logger.warning("Could not resolve channel: %s", channel_input)
            return

        if self._api_key:
            video_ids = await self._collect_video_list_via_api(channel_id, channel_name)
        else:
            video_ids = []

        if self._use_yt_dlp:
            if self._download_videos:
                await self._download_videos_via_yt_dlp(channel_id, channel_name, video_ids)
            else:
                await self._collect_thumbnails_via_yt_dlp(channel_id, channel_name)

    def _api_params(self, extra: dict) -> dict:
        params = dict(extra)
        if self._api_key:
            params["key"] = self._api_key
        return params

    def _api_headers(self) -> dict[str, str]:
        if not self._api_key and self._oauth_credentials:
            return {"Authorization": f"Bearer {self._oauth_credentials}"}
        return {}

    async def _resolve_channel(self, channel_input: str) -> tuple[str, str]:
        if not self._api_key and not self._oauth_credentials:
            return channel_input, channel_input

        headers = self._api_headers()
        async with httpx.AsyncClient(timeout=30, headers=headers) as client:
            if channel_input.startswith("UC"):
                resp = await client.get(
                    f"{YT_API_BASE}/channels",
                    params={"part": "snippet", "id": channel_input, "key": self._api_key},
                )
            elif channel_input.startswith("@"):
                resp = await client.get(
                    f"{YT_API_BASE}/channels",
                    params={"part": "snippet", "forHandle": channel_input, "key": self._api_key},
                )
            else:
                resp = await client.get(
                    f"{YT_API_BASE}/search",
                    params={
                        "part": "snippet", "q": channel_input,
                        "type": "channel", "maxResults": 1, "key": self._api_key,
                    },
                )

            if resp.status_code != 200:
                return channel_input, channel_input

            data = resp.json()
            items = data.get("items", [])
            if not items:
                return channel_input, channel_input

            item = items[0]
            snippet = item.get("snippet", {})
            cid = item.get("id", {})
            if isinstance(cid, dict):
                cid = cid.get("channelId", channel_input)

            return cid, snippet.get("title", channel_input)

    async def _collect_video_list_via_api(self, channel_id: str, channel_name: str) -> list[str]:
        video_ids = []
        page_token = ""

        async with httpx.AsyncClient(timeout=30) as client:
            while not self._stop.is_set():
                await asyncio.sleep(self._api_delay)

                params = {
                    "part": "snippet",
                    "channelId": channel_id,
                    "maxResults": 50,
                    "order": "date",
                    "type": "video",
                    "key": self._api_key,
                }
                if page_token:
                    params["pageToken"] = page_token

                async with self._sem:
                    resp = await client.get(f"{YT_API_BASE}/search", params=params)

                if resp.status_code == 403:
                    logger.warning("YouTube API quota exceeded")
                    self.rate_limiter.record_failure("googleapis.com")
                    break

                resp.raise_for_status()
                data = resp.json()
                self.rate_limiter.record_success("googleapis.com")

                for item in data.get("items", []):
                    if self._stop.is_set():
                        break
                    video_id = item.get("id", {}).get("videoId")
                    if not video_id:
                        continue
                    video_ids.append(video_id)

                    thumbs = item.get("snippet", {}).get("thumbnails", {})
                    best = thumbs.get("maxres") or thumbs.get("high") or thumbs.get("medium")
                    if best and best.get("url"):
                        if not self.is_known(video_id):
                            await self.download_media({
                                "entity_id": channel_id,
                                "entity_name": channel_name,
                                "content_type": "thumbnail",
                                "content_id": video_id,
                                "url": best["url"],
                                "extension": "jpg",
                                "source_url": f"https://www.youtube.com/watch?v={video_id}",
                            })

                page_token = data.get("nextPageToken", "")
                if not page_token:
                    break

        return video_ids

    async def _download_videos_via_yt_dlp(self, channel_id: str, channel_name: str,
                                           video_ids: list[str]):
        urls = [f"https://www.youtube.com/watch?v={vid}" for vid in video_ids if not self.is_known(f"video_{vid}")]
        if not urls:
            channel_url = f"https://www.youtube.com/channel/{channel_id}/videos"
            urls = [channel_url]

        for url in urls:
            if self._stop.is_set():
                break
            await asyncio.sleep(self._download_delay)

            with tempfile.TemporaryDirectory() as tmpdir:
                output_tmpl = os.path.join(tmpdir, "%(id)s.%(ext)s")
                cmd = [
                    "yt-dlp",
                    "--impersonate", "chrome",
                    "-f", self._ytdlp_format,
                    "--merge-output-format", self._merge_format,
                    "--write-thumbnail",
                    "--no-overwrites",
                    "-o", output_tmpl,
                    "--retries", "3",
                    "--socket-timeout", "30",
                ]

                if self._max_duration:
                    cmd.extend(["--match-filter", f"duration<={self._max_duration * 60}"])

                if self._cookie_browser:
                    cmd.extend(["--cookies-from-browser", self._cookie_browser])

                if "watch?v=" not in url:
                    cmd.extend(["--playlist-end", "50"])

                cmd.append(url)

                loop = asyncio.get_event_loop()
                proc = await loop.run_in_executor(
                    None,
                    lambda: subprocess.run(cmd, capture_output=True, text=True, timeout=600),
                )

                if proc.returncode not in (0, 101):
                    logger.warning("yt-dlp failed for %s: %s", url, proc.stderr[:300])
                    continue

                for f in Path(tmpdir).rglob("*"):
                    if self._stop.is_set():
                        break
                    if not f.is_file():
                        continue
                    ext = f.suffix.lstrip(".").lower()
                    if ext not in ("jpg", "jpeg", "png", "webp", "mp4", "webm", "mkv"):
                        continue

                    cid = f.stem
                    is_video = ext in ("mp4", "webm", "mkv")
                    if is_video:
                        cid = f"video_{cid}"

                    if self.is_known(cid):
                        continue

                    data = f.read_bytes()
                    content_type = "video" if is_video else "thumbnail"

                    await self.download_media({
                        "entity_id": channel_id,
                        "entity_name": channel_name,
                        "content_type": content_type,
                        "content_id": cid,
                        "data": data,
                        "extension": ext if ext != "jpeg" else "jpg",
                        "source_url": url if "watch?v=" in url else f"https://www.youtube.com/watch?v={f.stem}",
                    })

    async def _collect_thumbnails_via_yt_dlp(self, channel_id: str, channel_name: str):
        channel_url = f"https://www.youtube.com/channel/{channel_id}/videos"

        with tempfile.TemporaryDirectory() as tmpdir:
            output_tmpl = os.path.join(tmpdir, "%(id)s.%(ext)s")
            cmd = [
                "yt-dlp",
                "--impersonate", "chrome",
                "--write-thumbnail", "--skip-download",
                "--no-overwrites",
                "-o", output_tmpl,
                "--playlist-end", "50",
            ]
            if self._cookie_browser:
                cmd.extend(["--cookies-from-browser", self._cookie_browser])
            cmd.append(channel_url)

            loop = asyncio.get_event_loop()
            proc = await loop.run_in_executor(
                None,
                lambda: subprocess.run(cmd, capture_output=True, text=True, timeout=600),
            )

            if proc.returncode not in (0, 101):
                logger.warning("yt-dlp thumbnail failed for %s: %s", channel_name, proc.stderr[:200])
                return

            for f in Path(tmpdir).rglob("*"):
                if self._stop.is_set():
                    break
                if not f.is_file():
                    continue
                ext = f.suffix.lstrip(".").lower()
                if ext not in ("jpg", "jpeg", "png", "webp"):
                    continue

                cid = f.stem
                if self.is_known(cid):
                    continue

                data = f.read_bytes()
                await self.download_media({
                    "entity_id": channel_id,
                    "entity_name": channel_name,
                    "content_type": "thumbnail",
                    "content_id": cid,
                    "data": data,
                    "extension": ext if ext != "jpeg" else "jpg",
                    "source_url": f"https://www.youtube.com/watch?v={cid}",
                })

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
            if "data" in item:
                data = item["data"]
            elif "url" in item:
                await self.wait_rate_limit("googleapis.com")
                async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                    resp = await client.get(item["url"])
                    resp.raise_for_status()
                    data = resp.content
            else:
                return

            sha = self.sha256_bytes(data)
            self.save_file(data, filename)
            self.rate_limiter.record_success("googleapis.com")
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
                source_url=item.get("source_url"),
            )
        except Exception as e:
            self.rate_limiter.record_failure("googleapis.com")
            self.circuit_breaker.record_failure()
            logger.error("Download failed %s: %s", cid, e)
            await self.send_to_dlq(item["entity_id"], cid, str(e))
