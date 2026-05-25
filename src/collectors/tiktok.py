import asyncio
import json
import logging
import os
import random
import subprocess
import tempfile
import time
from pathlib import Path
from datetime import datetime, timezone

import httpx

from src.core.base_collector import BaseCollector
from src.core.file_naming import sanitize_name

logger = logging.getLogger(__name__)

REQUIRED_COOKIES = {"sessionid", "tt_csrf_token", "ttwid", "msToken", "tt_chain_token", "sid_guard"}
RECOMMENDED_COOKIES = {"s_v_web_id", "odin_tt", "cmpl_token", "passport_csrf_token"}


def validate_cookies(cookies_file: str) -> dict:
    """Validate a Netscape-format cookies file for required TikTok cookies."""
    result = {"valid": False, "total": 0, "present": set(), "missing": set(), "expired": set(), "warnings": []}
    if not cookies_file or not os.path.isfile(cookies_file):
        result["warnings"].append("Cookies file not found")
        return result

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
        logger.info(
            "tiktok tool availability: gallery-dl=%s yt-dlp=%s browser_fallback=%s ytdlp_fallback=%s",
            self._use_gallery_dl, self._use_yt_dlp, self._browser_fallback, self._ytdlp_fallback,
        )
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

    @property
    def account_media_dir(self) -> Path:
        if self._cookies_file:
            acc_name = Path(self._cookies_file).stem
            path = self.media_dir / f"account_{sanitize_name(acc_name)}"
        else:
            path = self.media_dir / "default"
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def collect(self, targets: list[str]):
        await self._load_tracker_state()
        for username in targets:
            if self._stop.is_set(): break
            username = username.lstrip("@")
            if self._is_invalid_username(username): continue

            logger.info("Collecting tiktok/%s", username)
            try:
                await self._collect_user(username)
                await self.checkpoint.save_progress(username)
            except Exception as e:
                logger.error("Failed tiktok/%s: %s", username, e)
                await self.send_to_dlq(username, username, str(e))

        # Spider queue processing
        if os.getenv("TIKTOK_SPIDER_ENABLED", "true").lower() == "true":
            await self._process_spider_queue()

    async def _process_spider_queue(self):
        while not self._stop.is_set():
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow("""
                    UPDATE tiktok_spider_queue
                    SET status = 'processing'
                    WHERE id = (
                        SELECT id FROM tiktok_spider_queue
                        WHERE status = 'pending'
                        ORDER BY priority ASC, collected_at ASC
                        LIMIT 1
                    )
                    RETURNING platform_user_id, username
                """)
            if not row: break
            target = row['username'] or row['platform_user_id']
            try:
                await self._collect_user(target)
                async with self.pool.acquire() as conn:
                    await conn.execute("UPDATE tiktok_spider_queue SET status = 'completed' WHERE platform_user_id = $1", row['platform_user_id'])
            except Exception:
                async with self.pool.acquire() as conn:
                    await conn.execute("UPDATE tiktok_spider_queue SET status = 'failed' WHERE platform_user_id = $1", row['platform_user_id'])

    @staticmethod
    def _is_invalid_username(username: str) -> bool:
        if len(username) < 2 or len(username) > 24:
            return True
        if not username.replace("_", "").replace(".", "").isalnum():
            return True
        return False

    async def _collect_user(self, username: str):
        profile_url = f"https://www.tiktok.com/@{username}"
        # For V2, we try to get metadata first (placeholder for now)
        await self._scrape_profile_metadata(username)

        if self._use_gallery_dl:
            if await self._collect_via_gallery_dl(username, profile_url): return
        if self._use_yt_dlp and self._ytdlp_fallback:
            if await self._collect_via_yt_dlp(username, profile_url): return
        if self._browser_fallback:
            if await self._collect_via_playwright(username): return
        await self._collect_via_api(username)

    async def _scrape_profile_metadata(self, username: str):
        """Try to fetch and save profile metadata to DB."""
        pass

    @staticmethod
    def _safe_int(value, default: int = 0) -> int:
        try:
            if value is None or value == "":
                return default
            return int(value)
        except (ValueError, TypeError):
            return default

    @staticmethod
    def _to_dt(value):
        """Coerce a unix timestamp (int or str) to aware datetime; None on failure."""
        if value is None or value == "":
            return None
        try:
            ts = int(value)
            if ts <= 0:
                return None
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (ValueError, TypeError, OSError):
            return None

    async def _upsert_profile(self, author: dict, author_stats: dict | None = None) -> str | None:
        """Upsert a tiktok_profiles row from a sidecar's author/authorStats blocks.

        Returns the profile UUID (str) on success, or None.
        """
        if not isinstance(author, dict):
            return None
        platform_user_id = author.get("id")
        if not platform_user_id:
            return None
        stats = author_stats or {}
        # Some sidecars nest stats under author itself; tolerate both.
        if not stats and isinstance(author.get("stats"), dict):
            stats = author["stats"]

        avatar = author.get("avatarLarger") or author.get("avatarMedium") or author.get("avatarThumb")
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow("""
                    INSERT INTO tiktok_profiles (
                        platform_user_id, username, nickname, avatar_url, bio,
                        following_count, followers_count, heart_count, video_count,
                        digg_count, is_verified, is_private, updated_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, NOW())
                    ON CONFLICT (platform_user_id) DO UPDATE SET
                        username = COALESCE(EXCLUDED.username, tiktok_profiles.username),
                        nickname = COALESCE(EXCLUDED.nickname, tiktok_profiles.nickname),
                        avatar_url = COALESCE(EXCLUDED.avatar_url, tiktok_profiles.avatar_url),
                        bio = COALESCE(EXCLUDED.bio, tiktok_profiles.bio),
                        following_count = EXCLUDED.following_count,
                        followers_count = EXCLUDED.followers_count,
                        heart_count = EXCLUDED.heart_count,
                        video_count = EXCLUDED.video_count,
                        digg_count = EXCLUDED.digg_count,
                        is_verified = EXCLUDED.is_verified,
                        is_private = EXCLUDED.is_private,
                        updated_at = NOW()
                    RETURNING id
                """,
                str(platform_user_id),
                author.get("uniqueId"),
                author.get("nickname"),
                avatar,
                author.get("signature"),
                self._safe_int(stats.get("followingCount")),
                self._safe_int(stats.get("followerCount")),
                self._safe_int(stats.get("heartCount") or stats.get("heart")),
                self._safe_int(stats.get("videoCount")),
                self._safe_int(stats.get("diggCount")),
                bool(author.get("verified", False)),
                bool(author.get("privateAccount", False)),
                )
                return str(row["id"]) if row else None
        except Exception as e:
            logger.warning("tiktok _upsert_profile failed for %s: %s", platform_user_id, e)
            return None

    async def _upsert_post(self, data: dict, username: str, profile_uuid: str | None = None):
        post_id = data.get("id")
        if not post_id:
            return
        video = data.get("video") if isinstance(data.get("video"), dict) else {}
        stats = data.get("stats") if isinstance(data.get("stats"), dict) else {}
        music = data.get("music") if isinstance(data.get("music"), dict) else {}

        # hashtags / mentions / challenges from textExtra and challenges[]
        hashtags: list[str] = []
        mentions: list[str] = []
        for t in (data.get("textExtra") or []):
            if not isinstance(t, dict):
                continue
            if t.get("hashtagName"):
                hashtags.append(t["hashtagName"])
            if t.get("userUniqueId"):
                mentions.append(t["userUniqueId"])
        challenges = [c.get("title") for c in (data.get("challenges") or []) if isinstance(c, dict) and c.get("title")]

        try:
            async with self.pool.acquire() as conn:
                if profile_uuid is None:
                    # Fallback: look up by username if author block was missing.
                    profile_row = await conn.fetchrow(
                        "SELECT id FROM tiktok_profiles WHERE username = $1", username
                    )
                    profile_uuid = str(profile_row["id"]) if profile_row else None

                await conn.execute("""
                    INSERT INTO tiktok_posts (
                        platform_post_id, profile_id, video_url, cover_image_url,
                        title, description, hashtags, mentions, challenges,
                        music_id, music_title, music_author, music_duration,
                        duet_enabled, stitch_enabled,
                        view_count, like_count, comment_count, share_count,
                        duration, create_time, metadata
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13,
                              $14, $15, $16, $17, $18, $19, $20, $21, $22)
                    ON CONFLICT (platform_post_id) DO UPDATE SET
                        profile_id = COALESCE(EXCLUDED.profile_id, tiktok_posts.profile_id),
                        video_url = COALESCE(EXCLUDED.video_url, tiktok_posts.video_url),
                        cover_image_url = COALESCE(EXCLUDED.cover_image_url, tiktok_posts.cover_image_url),
                        title = COALESCE(EXCLUDED.title, tiktok_posts.title),
                        description = COALESCE(EXCLUDED.description, tiktok_posts.description),
                        hashtags = EXCLUDED.hashtags,
                        mentions = EXCLUDED.mentions,
                        challenges = EXCLUDED.challenges,
                        music_id = COALESCE(EXCLUDED.music_id, tiktok_posts.music_id),
                        music_title = COALESCE(EXCLUDED.music_title, tiktok_posts.music_title),
                        music_author = COALESCE(EXCLUDED.music_author, tiktok_posts.music_author),
                        music_duration = COALESCE(EXCLUDED.music_duration, tiktok_posts.music_duration),
                        duet_enabled = EXCLUDED.duet_enabled,
                        stitch_enabled = EXCLUDED.stitch_enabled,
                        view_count = EXCLUDED.view_count,
                        like_count = EXCLUDED.like_count,
                        comment_count = EXCLUDED.comment_count,
                        share_count = EXCLUDED.share_count,
                        duration = COALESCE(EXCLUDED.duration, tiktok_posts.duration),
                        create_time = COALESCE(EXCLUDED.create_time, tiktok_posts.create_time),
                        metadata = EXCLUDED.metadata
                """,
                str(post_id),
                profile_uuid,
                video.get("downloadAddr") or video.get("playAddr"),
                video.get("cover") or video.get("originCover"),
                (data.get("desc") or "")[:500] or None,  # title (short)
                data.get("desc"),
                hashtags or None,
                mentions or None,
                challenges or None,
                str(music.get("id")) if music.get("id") is not None else None,
                music.get("title"),
                music.get("authorName"),
                self._safe_int(music.get("duration")),
                bool(data.get("duetEnabled", False)),
                bool(data.get("stitchEnabled", False)),
                self._safe_int(stats.get("playCount")),
                self._safe_int(stats.get("diggCount")),
                self._safe_int(stats.get("commentCount")),
                self._safe_int(stats.get("shareCount")),
                self._safe_int(video.get("duration")),
                self._to_dt(data.get("createTime")),
                json.dumps(data, default=str, ensure_ascii=False),
                )
        except Exception as e:
            logger.warning("tiktok _upsert_post failed for %s: %s", post_id, e)

    async def _collect_via_gallery_dl(self, username: str, profile_url: str) -> bool:
        logger.info("tiktok fallback gallery-dl: starting for %s", username)
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                cmd = ["gallery-dl", "--dest", tmpdir, "--no-mtime", "--write-metadata", "-v"]
                if self._cookies_file:
                    cmd.extend(["--cookies", self._cookies_file])
                # `--` ensures a profile_url that begins with `--` is treated as a positional.
                cmd.append("--")
                cmd.append(profile_url)

                loop = asyncio.get_event_loop()
                proc = await loop.run_in_executor(
                    None,
                    lambda: subprocess.run(cmd, capture_output=True, text=True, timeout=self._timeout),
                )

                if proc.returncode != 0:
                    logger.warning(
                        "tiktok fallback gallery-dl failed for %s: rc=%s stderr=%s stdout=%s",
                        username, proc.returncode, (proc.stderr or "")[:800], (proc.stdout or "")[:400],
                    )
                    return False

                files = list(Path(tmpdir).rglob("*"))
                file_count = sum(1 for f in files if f.is_file())
                logger.info("tiktok fallback gallery-dl: %s rc=0, downloaded %d files (stderr_tail=%s)",
                            username, file_count, (proc.stderr or "")[-300:])
                if file_count == 0:
                    return False
                await self._ingest_tmpdir(tmpdir, username)
                return True
        except Exception as e:
            logger.warning("tiktok fallback gallery-dl exception for %s: %s: %s",
                           username, type(e).__name__, e, exc_info=True)
            return False

    async def _collect_via_yt_dlp(self, username: str, profile_url: str) -> bool:
        logger.info("tiktok fallback yt-dlp: starting for %s", username)
        try:
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
                    logger.warning(
                        "tiktok fallback yt-dlp failed for %s: rc=%s stderr=%s stdout=%s",
                        username, proc.returncode, (proc.stderr or "")[:800], (proc.stdout or "")[:400],
                    )
                    return False

                files = list(Path(tmpdir).rglob("*"))
                file_count = sum(1 for f in files if f.is_file())
                logger.info("tiktok fallback yt-dlp: %s rc=%s, downloaded %d files (stderr_tail=%s)",
                            username, proc.returncode, file_count, (proc.stderr or "")[-300:])
                if file_count == 0:
                    return False
                await self._ingest_tmpdir(tmpdir, username)
                return True
        except Exception as e:
            logger.warning("tiktok fallback yt-dlp exception for %s: %s: %s",
                           username, type(e).__name__, e, exc_info=True)
            return False

    async def _ingest_tmpdir(self, tmpdir: str, username: str):
        # Pass 1: ingest gallery-dl JSON sidecars to populate tiktok_profiles
        # and tiktok_posts. Sidecars sit next to each media file as <media>.json.
        sidecar_count = 0
        post_count = 0
        profile_uuids: dict[str, str] = {}  # platform_user_id -> profile uuid
        for sc in Path(tmpdir).rglob("*.json"):
            if self._stop.is_set(): break
            if not sc.is_file(): continue
            # Skip our own metadata files if any leak in.
            if sc.name.endswith("_metadata.json"): continue
            try:
                with open(sc, "r", encoding="utf-8") as fh:
                    sidecar = json.load(fh)
            except Exception as e:
                logger.debug("tiktok sidecar parse skip %s: %s", sc.name, e)
                continue
            if not isinstance(sidecar, dict):
                continue
            sidecar_count += 1

            author = sidecar.get("author") if isinstance(sidecar.get("author"), dict) else None
            author_stats = sidecar.get("authorStats") if isinstance(sidecar.get("authorStats"), dict) else None
            profile_uuid = None
            if author:
                pid = author.get("id")
                if pid and pid in profile_uuids:
                    profile_uuid = profile_uuids[pid]
                else:
                    profile_uuid = await self._upsert_profile(author, author_stats)
                    if pid and profile_uuid:
                        profile_uuids[pid] = profile_uuid

            # Only upsert posts when sidecar looks like a post (has top-level id + desc/video).
            if sidecar.get("id") and (sidecar.get("video") or sidecar.get("desc") is not None or sidecar.get("createTime")):
                try:
                    await self._upsert_post(sidecar, username, profile_uuid)
                    post_count += 1
                except Exception as e:
                    logger.warning("tiktok sidecar upsert_post failed %s: %s", sc.name, e)

        if sidecar_count:
            logger.info(
                "tiktok ingest: parsed %d sidecars, upserted %d profile(s) and %d post(s) for %s",
                sidecar_count, len(profile_uuids), post_count, username,
            )

        # Pass 2: copy media files into our store (existing behavior).
        for f in Path(tmpdir).rglob("*"):
            if self._stop.is_set(): break
            if not f.is_file(): continue
            ext = f.suffix.lstrip(".").lower()
            if ext not in ("jpg", "jpeg", "png", "mp4", "webm", "gif", "webp"): continue

            cid = f.stem
            # gallery-dl filenames embed the title; trim to first whitespace token
            # so cid stays a stable numeric video id and fits varchar(100).
            cid = cid.split()[0][:100] if cid else cid
            if self.is_known(cid): continue

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
        cookies = {"sessionid": self._session_id} if self._session_id else {}

        try:
            async with httpx.AsyncClient(timeout=30, cookies=cookies, follow_redirects=True) as client:
                resp = await client.get(f"https://www.tiktok.com/@{username}", headers={"User-Agent": self.user_agents.get_for_domain("tiktok.com")})
                resp.raise_for_status()

                html = resp.text
                marker = '"ItemModule":'
                start = html.find(marker)
                if start == -1: return

                bracket_start = html.find("{", start + len(marker))
                depth, end = 0, bracket_start
                for i, ch in enumerate(html[bracket_start:], bracket_start):
                    if ch == "{": depth += 1
                    elif ch == "}": depth -= 1
                    if depth == 0:
                        end = i + 1
                        break

                items = json.loads(html[bracket_start:end])
                for video_id, video_data in items.items():
                    if self._stop.is_set(): break
                    await self._upsert_post(video_data, username)
                    cover = video_data.get("video", {}).get("cover")
                    if cover:
                        await self.download_media({
                            "entity_id": username, "entity_name": username,
                            "content_type": "thumbnail", "content_id": video_id,
                            "url": cover, "extension": "jpg", "raw": video_data
                        })
        except Exception as e:
            logger.error("API fallback failed for %s: %s", username, e)

    async def _collect_via_playwright(self, username: str) -> bool:
        # Implementation remains similar but simplified for V2
        logger.warning(
            "tiktok fallback playwright: NOT IMPLEMENTED (stub returns False) for %s",
            username,
        )
        return False

    async def _load_tracker_state(self):
        if self.pool:
            try:
                async with self.pool.acquire() as conn:
                    rows = await conn.fetch("SELECT platform_post_id FROM tiktok_posts")
                    self._tracked_ids = {r["platform_post_id"] for r in rows}
            except Exception: pass

    async def _record_download(self, username: str, video_id: str, file_path: str):
        self._tracked_ids.add(video_id)

    async def download_media(self, item: dict):
        cid = item["content_id"]
        if self.is_known(cid): return

        filename = self.build_filename(
            item["entity_id"], item["entity_name"],
            item["content_type"], cid, extension=item.get("extension", "mp4")
        )

        dest_dir = self.account_media_dir / item["content_type"]
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / filename

        try:
            if "data" in item:
                data = item["data"]
            elif "url" in item:
                await self.wait_rate_limit("tiktok.com")
                async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                    resp = await client.get(item["url"], headers={"User-Agent": self.user_agents.get_for_domain("tiktok.com")})
                    resp.raise_for_status()
                    data = resp.content
            else: return

            sha = self.sha256_bytes(data)
            
            # Atomic save
            fd, tmp_path = tempfile.mkstemp(dir=dest_dir, suffix=".tmp")
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, dest)
            
            metadata = {
                "entity_id": item["entity_id"],
                "entity_name": item["entity_name"],
                "content_type": item["content_type"],
                "content_id": cid,
                "collected_at": datetime.now(timezone.utc).isoformat(),
                "raw": item.get("raw", {})
            }
            self.save_json(metadata, dest_dir / f"{Path(filename).stem}_metadata.json")

            await self.insert_media_item(
                entity_id=item["entity_id"], entity_name=item["entity_name"],
                content_type=item["content_type"], content_id=cid,
                filename=filename, file_path=str(dest),
                file_size=len(data), sha256=sha, metadata=metadata
            )
            self._known_ids.add(cid)
        except Exception as e:
            logger.error("Download failed %s: %s", cid, e)
            await self.send_to_dlq(item["entity_id"], cid, str(e))
