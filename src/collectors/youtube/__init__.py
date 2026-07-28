"""YouTube collector — Wave 2 Batch A port of youtubetoolkit/.

Absorbed from youtubetoolkit/:
    - main.py loop verbs → public collect_*() methods (collect_subscriptions,
      collect_liked_videos, collect_custom_playlist, collect_target_channel,
      batch_download).
    - scripts/subscription_processor.py → collect_subscriptions() w/
      since_last_scrape (smart mode), per-channel video extraction via
      uploads playlist (1 quota unit/page), batched videos.list enrichment.
    - scripts/scrape_liked_videos_enhanced.py → collect_liked_videos() using
      the Data API "LL" playlist (OAuth-only).
    - scripts/scrape_custom_playlist.py → collect_custom_playlist() via
      yt-dlp --flat-playlist (no API quota cost) for arbitrary playlists or
      channel URLs.
    - scripts/scrape_targets.py → collect_target_channels() reads operator
      target_channels.txt and dispatches to _collect_channel().
    - scripts/batch_downloader.py → batch_download() over a target list,
      with photos_only and days/duration filters mapped to existing yt-dlp
      flow.
    - src/video_processor.py download flow → already covered by
      _download_videos_via_yt_dlp / _collect_thumbnails_via_yt_dlp.
    - src/auth_cache.py → JSON-credentials path inside _load_oauth_credentials()
      (unified collector replaces pickle with JSON; legacy pickle migration
      already in place).
    - src/channel_photo_tracker.py (pHash logic for CDN-rotation vs genuine
      change) → handled at the platform-agnostic level by
      src.core.profile_photo_tracker; collector emits the URL change.
    - src/data_manager_streamlined.parse_duration() → _parse_iso8601_duration().

Dropped (per Wave 2 drop rules):
    - main.py interactive Rich/questionary menus → operator UI, not collector
      (toolkit's main.py runs as standalone script).
    - SQLite database.py / migrations → unified Postgres schema is canonical.
    - upload/comment/like-write/subscribe-write endpoints → read-only ingest.
    - setup.bat / start_toolkit.bat / standalone CLI → no equivalent here.
    - scripts/youtube_oauth_bootstrap.py / scripts/logout_account.py / 
      scripts/validate_installation.py → user setup scripts. We reference
      youtube_oauth_bootstrap (lives at scripts/) for OAuth onboarding but
      DO NOT absorb its interactive flow.
    - download_path_manager.prompt_for_download_path → prompts the user;
      collectors write to a DRIVE_PATH baked into BaseCollector.
    - download_structurer.py heuristics → file_naming.py already handles
      our atomic + sanitised path scheme.

Deferred:
    - rate_limiter.py per-channel adaptive multipliers → would be nice on
      top of src.core.adaptive_rate; current implementation uses
      _api_delay/_download_delay hard sleeps. Tracked as TODO.
    - resilience.py token-bucket → src.core.adaptive_rate covers AIMD; the
      few `subprocess.run(..., timeout=...)` wrappers should migrate to
      src.core.subprocess_downloader (already used for video download).
"""
import asyncio
import json
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
from datetime import datetime, timedelta, timezone

import httpx

from src.core.base_collector import BaseCollector
from src.collectors.youtube.parse import (
    vtt_to_text as _parse_vtt_to_text,
    parse_relative_timestamp as _parse_rel_ts,
)
from src.core.file_naming import sanitize_name
from src.core.vault import VAULT_ROOT, write_atomic_artifact
from src.core.user_change_tracker import (
    UserChangeTracker,
    YOUTUBE_TRACKED_FIELDS,
)

logger = logging.getLogger(__name__)

YT_API_BASE = "https://www.googleapis.com/youtube/v3"
LIKED_VIDEOS_PLAYLIST_ID = "LL"


def parse_iso8601_duration(duration_str: str) -> int:
    """Parse ISO 8601 duration (e.g. PT1H23M45S) to seconds. Returns 0 on parse failure.

    Ported from youtubetoolkit/scripts/subscription_processor.parse_duration.
    """
    if not duration_str:
        return 0
    try:
        m = re.search(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", duration_str)
        if m:
            h = int(m.group(1) or 0)
            mn = int(m.group(2) or 0)
            s = int(m.group(3) or 0)
            return h * 3600 + mn * 60 + s
    except Exception:
        pass
    return 0


class YoutubeCollector(BaseCollector):
    SOURCE_NAME = "youtube"

    def __init__(self):
        super().__init__()
        self._api_key = os.getenv("YOUTUBE_API_KEY", "")
        self._cookie_browser = os.getenv("YOUTUBE_COOKIE_BROWSER", "")
        self._cookie_file = os.getenv("YOUTUBE_COOKIE_FILE", "")
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
        self._fetch_transcripts = os.getenv("YOUTUBE_FETCH_TRANSCRIPTS", "true").lower() == "true"
        self._fetch_comments_enabled = os.getenv("YOUTUBE_FETCH_COMMENTS", "true").lower() == "true"
        self._transcript_lang = os.getenv("YOUTUBE_TRANSCRIPT_LANG", "en")
        self._max_comments = int(os.getenv("YOUTUBE_MAX_COMMENTS", "200"))
        self._enrich_batch_limit = int(os.getenv("YOUTUBE_ENRICH_BATCH_LIMIT", "10"))
        # Wave 2 absorbed-from-toolkit settings
        self._subscription_cache_file = Path(
            os.getenv("YOUTUBE_SUBSCRIPTION_CACHE", "data/youtube_subscriptions.json")
        )
        self._target_channels_file = Path(
            os.getenv("YOUTUBE_TARGET_CHANNELS_FILE", "data/youtube_target_channels.txt")
        )
        self._max_liked_videos = int(os.getenv("YOUTUBE_MAX_LIKED_VIDEOS", "1000"))
        self._max_subscriptions = int(os.getenv("YOUTUBE_MAX_SUBSCRIPTIONS", "999"))
        self._max_videos_per_channel = int(os.getenv("YOUTUBE_MAX_VIDEOS_PER_CHANNEL", "0"))
        self._video_downloads_per_target = int(os.getenv("YOUTUBE_VIDEO_DOWNLOADS_PER_TARGET", "5"))
        self._video_backfill_batch_size = int(
            os.getenv("YOUTUBE_VIDEO_BACKFILL_BATCH_SIZE", os.getenv("BACKFILL_BATCH_SIZE", "100"))
        )
        self._video_backfill_scan_limit = int(os.getenv("YOUTUBE_VIDEO_BACKFILL_SCAN_LIMIT", "5000"))
        self._prefill_media_backlog = os.getenv("YOUTUBE_PREFILL_MEDIA_BACKLOG", "true").lower() == "true"
        # FAMOUS-FILTER (Bryan): skip channels at or above this subscriber count,
        # even if subscribed. 0 disables. Overrides the subscription seed.
        self._famous_sub_cap = int(os.getenv("YOUTUBE_FAMOUS_SUB_CAP", "0") or "0")

    @staticmethod
    def _check_yt_dlp() -> bool:
        try:
            subprocess.run(["yt-dlp", "--version"], capture_output=True, check=True)
            return True
        except (FileNotFoundError, subprocess.CalledProcessError):
            return False

    def _load_oauth_credentials(self) -> str | None:
        """Load Google OAuth credentials.

        Storage format is JSON (`Credentials.to_json()`). For backward
        compatibility we one-shot migrate any pre-existing pickle file to
        JSON, then refuse to read pickle on subsequent runs. Pickle is an
        RCE vector if the creds file is ever attacker-writable, so we
        treat it as transition-only and always rewrite to JSON.
        """
        if self._oauth_credentials:
            return self._oauth_credentials

        json_path = self._oauth_pickle.with_suffix(".json")
        creds = None

        # Preferred: JSON.
        if json_path.exists():
            try:
                from google.oauth2.credentials import Credentials
                import json as _json
                with open(json_path, "r", encoding="utf-8") as f:
                    info = _json.load(f)
                creds = Credentials.from_authorized_user_info(info)
            except Exception as e:
                logger.warning("Failed to load YouTube OAuth JSON creds: %s", e)
                creds = None

        # Migration: if JSON missing but legacy pickle present, migrate once
        # then disable further pickle reads.
        if creds is None and self._oauth_pickle.exists():
            logger.warning(
                "Migrating legacy YouTube OAuth pickle %s -> %s. "
                "Pickle is an RCE vector and will not be loaded again.",
                self._oauth_pickle, json_path,
            )
            try:
                # Restricted unpickler — only the google credential class is
                # whitelisted. Anything else raises and the file is quarantined.
                import pickle as _pickle
                import io as _io

                class _SafeUnpickler(_pickle.Unpickler):
                    def find_class(self, module, name):
                        if module == "google.oauth2.credentials" and name == "Credentials":
                            from google.oauth2.credentials import Credentials as _C
                            return _C
                        raise _pickle.UnpicklingError(
                            f"Refusing to load {module}.{name} from legacy pickle"
                        )

                with open(self._oauth_pickle, "rb") as f:
                    creds = _SafeUnpickler(_io.BytesIO(f.read())).load()
                # Persist as JSON.
                with open(json_path, "w", encoding="utf-8") as f:
                    f.write(creds.to_json())
                # Quarantine the pickle so it can't be re-loaded later.
                quarantine = self._oauth_pickle.with_suffix(".pickle.migrated")
                try:
                    self._oauth_pickle.replace(quarantine)
                except OSError:
                    pass
            except Exception as e:
                logger.error("Could not migrate legacy YouTube OAuth pickle: %s", e)
                creds = None

        if creds is None:
            return None

        # Refresh if expired.
        try:
            from google.auth.transport.requests import Request
            if getattr(creds, "expired", False) and getattr(creds, "refresh_token", None):
                creds.refresh(Request())
                with open(json_path, "w", encoding="utf-8") as f:
                    f.write(creds.to_json())
                logger.info("YouTube OAuth token refreshed")
        except Exception as e:
            logger.warning("YouTube OAuth refresh failed: %s", e)

        token = getattr(creds, "token", None)
        if token:
            self._oauth_credentials = token
            return token
        return None

    @property
    def account_media_dir(self) -> Path:
        # Use first 8 chars of API key for isolation
        key_hash = sanitize_name(self._api_key[:8]) if self._api_key else "oauth"
        path = self.media_dir / f"api_{key_hash}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def collect(self, targets: list[str]):
        # OAuth bootstrap: try to load pickle so subsequent API calls have auth.
        if not self._api_key:
            oauth_token = self._load_oauth_credentials()
            if oauth_token:
                logger.info("Using OAuth credentials for YouTube API")
            else:
                logger.warning("YouTube collector has neither YOUTUBE_API_KEY nor OAuth pickle — calls will fail")
        self._has_auth = bool(self._api_key or self._oauth_credentials)

        if (
            self._prefill_media_backlog
            and self._use_yt_dlp
            and self._download_videos
            and not self._stop.is_set()
        ):
            try:
                stored = await self.run_backfill()
                logger.info("youtube: pre-cycle media backlog pass stored %d item(s)", stored)
            except Exception as e:
                logger.warning("youtube: pre-cycle media backlog pass failed: %s", e)

        for target in targets:
            if self._stop.is_set(): break
            logger.info("Collecting youtube/%s", target)
            try:
                await self._collect_channel(target)
                await self.checkpoint.save_progress(target)
            except Exception as e:
                logger.error("Failed youtube/%s: %s", target, e)
                await self.send_to_dlq(target, target, str(e))

        # Rich enrichment runs after explicit channel targets so the source obeys
        # the shared priority policy instead of competing with freshness work.
        if self._use_yt_dlp and (self._fetch_transcripts or self._fetch_comments_enabled) and not self._stop.is_set():
            try:
                logger.info("YouTube: running enrichment phase (limit=%d)", self._enrich_batch_limit)
                await self._enrich_transcripts_and_comments(limit=self._enrich_batch_limit)
            except Exception as e:
                logger.error("YouTube: enrichment phase failed: %s", e, exc_info=True)

        # Community posts — BOUNDED sweep (15 channels/cycle, oldest first), not
        # per-channel (that was 492 fetches/cycle -> 50% CPU). Covers all over time.
        if os.getenv("YOUTUBE_COMMUNITY_ENABLED", "true").lower() == "true":
            try:
                await self._community_pass(batch_size=int(os.getenv("YOUTUBE_COMMUNITY_BATCH", "15")))
            except Exception as e:
                logger.debug("youtube community pass failed: %s", e)

        # Spider queue processing
        if os.getenv("YOUTUBE_SPIDER_ENABLED", "true").lower() == "true":
            await self._process_spider_queue()

    async def _process_spider_queue(self):
        while not self._stop.is_set():
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow("""
                    UPDATE youtube_spider_queue
                    SET status = 'processing'
                    WHERE id = (
                        SELECT id FROM youtube_spider_queue
                        WHERE status = 'pending'
                        ORDER BY priority ASC, collected_at ASC
                        LIMIT 1
                    )
                    RETURNING platform_channel_id
                """)
            if not row: break
            try:
                await self._collect_channel(row['platform_channel_id'])
                async with self.pool.acquire() as conn:
                    await conn.execute("UPDATE youtube_spider_queue SET status = 'completed' WHERE platform_channel_id = $1", row['platform_channel_id'])
            except Exception:
                async with self.pool.acquire() as conn:
                    await conn.execute("UPDATE youtube_spider_queue SET status = 'failed' WHERE platform_channel_id = $1", row['platform_channel_id'])

    async def _collect_channel(self, channel_input: str):
        channel_id, channel_name = await self._resolve_channel(channel_input)
        if not channel_id:
            logger.warning("Could not resolve channel: %s", channel_input)
            return

        # 1. Upsert Channel Info (returns uploads playlist ID + subscriber count)
        uploads_playlist, sub_count = await self._upsert_channel(channel_id, channel_name)

        # FAMOUS-FILTER (Bryan): skip channels at/above the subscriber cap, even if
        # subscribed. The channel row is still upserted above (so we know it + its
        # sub count) but we collect NO videos from it. Overrides the seed.
        if self._famous_sub_cap and sub_count >= self._famous_sub_cap:
            logger.info(
                "youtube: skipping famous channel %s (%s subs >= cap %d)",
                channel_name or channel_id, sub_count, self._famous_sub_cap,
            )
            return

        # When we have API auth and the channels.list lookup returned no item,
        # the channel is confirmed-missing — skip the expensive yt-dlp download
        # (running yt-dlp on a dead /channel/<id>/videos URL otherwise burns the
        # full download timeout per cycle for nothing).
        if self._has_auth and uploads_playlist is None:
            logger.info("youtube: skipping yt-dlp for confirmed-missing channel %s", channel_id)
            return

        if self._has_auth and uploads_playlist:
            video_ids = await self._collect_video_list_via_api(channel_id, channel_name, uploads_playlist)
        else:
            video_ids = []

        if self._use_yt_dlp:
            if self._download_videos:
                download_video_ids = video_ids
                if self._video_downloads_per_target > 0 and len(download_video_ids) > self._video_downloads_per_target:
                    logger.info(
                        "youtube: limiting live video downloads for %s to %d/%d this cycle",
                        channel_id,
                        self._video_downloads_per_target,
                        len(download_video_ids),
                    )
                    download_video_ids = download_video_ids[: self._video_downloads_per_target]
                await self._download_videos_via_yt_dlp(channel_id, channel_name, download_video_ids)
            else:
                await self._collect_thumbnails_via_yt_dlp(channel_id, channel_name)

    def _yt_auth(self, params: dict | None = None) -> tuple[dict, dict]:
        """Return (headers, params) populated with whichever auth is available."""
        params = dict(params or {})
        headers: dict[str, str] = {}
        if self._oauth_credentials:
            headers["Authorization"] = f"Bearer {self._oauth_credentials}"
        elif self._api_key:
            params["key"] = self._api_key
        return headers, params

    async def _upsert_channel(self, channel_id: str, channel_name: str) -> tuple[str | None, int]:
        """Upsert channel row. Returns (uploads playlist ID or None, subscriber_count)."""
        snippet = {}
        statistics = {}
        uploads_playlist = None
        if self._has_auth:
            try:
                headers, params = self._yt_auth({"part": "snippet,statistics,contentDetails", "id": channel_id})
                async with httpx.AsyncClient(timeout=30, headers=headers) as client:
                    resp = await client.get(f"{YT_API_BASE}/channels", params=params)
                    if resp.status_code == 200:
                        items = resp.json().get("items", [])
                        if not items:
                            logger.warning("YouTube channel not found: %s (channels.list returned 0 items)", channel_id)
                            return None, 0
                        item = items[0]
                        snippet = item.get("snippet", {})
                        statistics = item.get("statistics", {})
                        uploads_playlist = item.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
            except Exception as e:
                logger.warning("YouTube _upsert_channel meta fetch failed for %s: %s", channel_id, e)

        # ── User-intelligence diff (Tier 4): snapshot the row BEFORE upserting
        # so UserChangeTracker can compare old → new and emit one row per
        # changed field into youtube_user_changes. Wrapped in try/except so any
        # failure (DB, schema drift, etc.) is non-fatal to ingestion.
        prev_row = None
        try:
            async with self.pool.acquire() as conn:
                prev_row = await conn.fetchrow(
                    "SELECT title, description, view_count, subscriber_count, "
                    "video_count "
                    "FROM youtube_channels WHERE platform_channel_id = $1",
                    channel_id,
                )
        except Exception as exc:
            logger.debug("user_change_tracker[youtube]: prev-row fetch failed: %s", exc)

        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO youtube_channels (
                    platform_channel_id, title, description, custom_url,
                    published_at, thumbnail_url, view_count, subscriber_count,
                    video_count, updated_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, NOW())
                ON CONFLICT (platform_channel_id) DO UPDATE SET
                    title = EXCLUDED.title,
                    description = EXCLUDED.description,
                    view_count = EXCLUDED.view_count,
                    subscriber_count = EXCLUDED.subscriber_count,
                    video_count = EXCLUDED.video_count,
                    updated_at = NOW()
            """,
            channel_id, channel_name, snippet.get("description"), snippet.get("customUrl"),
            datetime.fromisoformat(snippet.get("publishedAt").replace("Z", "")) if snippet.get("publishedAt") else None,
            snippet.get("thumbnails", {}).get("high", {}).get("url"),
            int(statistics.get("viewCount", 0) or 0),
            int(statistics.get("subscriberCount", 0) or 0),
            int(statistics.get("videoCount", 0) or 0)
            )

        # ── Change-log write (non-fatal). Field names match youtube_channels
        # column names, so prev_row passes through unmodified. Count fields
        # are snapshotted as None when channels.list returned no statistics
        # (unauthenticated path), so an auth-less run can't log "N → 0" drops.
        try:
            tracker = UserChangeTracker(self.pool)
            new_snapshot = {
                "title":            channel_name,
                "description":      snippet.get("description"),
                "view_count":       int(statistics.get("viewCount", 0) or 0) if statistics else None,
                "subscriber_count": int(statistics.get("subscriberCount", 0) or 0) if statistics else None,
                "video_count":      int(statistics.get("videoCount", 0) or 0) if statistics else None,
            }
            await tracker.detect_and_log(
                table="youtube_user_changes",
                pk_col="channel_id",
                pk_val=str(channel_id),
                current_row=dict(prev_row) if prev_row is not None else None,
                new_row=new_snapshot,
                fields=YOUTUBE_TRACKED_FIELDS,
            )
        except Exception as exc:
            logger.debug("user_change_tracker[youtube]: detect_and_log failed: %s", exc)

        return uploads_playlist, int(statistics.get("subscriberCount", 0) or 0)

    @staticmethod
    def _parse_count(s):
        """Parse YouTube short counts: '1.2K' -> 1200, '3.4M likes' -> 3400000."""
        if not s:
            return None
        m = re.search(r"([\d.,]+)\s*([KMB]?)", str(s).replace(",", ""))
        if not m:
            return None
        try:
            n = float(m.group(1))
        except ValueError:
            return None
        return int(n * {"K": 1e3, "M": 1e6, "B": 1e9}.get(m.group(2).upper(), 1))

    async def _collect_community_posts(self, channel_id: str, max_posts: int = 40) -> int:
        """Collect a channel's Community tab (text/poll/image posts) via YouTube's
        InnerTube browse API (the tab lazy-loads; not in the initial HTML). The
        renderer is `postRenderer` (formerly backstagePostRenderer). No API quota."""
        headers = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
                   "Accept-Language": "en-US,en;q=0.9", "Cookie": "SOCS=CAI"}
        try:
            async with httpx.AsyncClient(timeout=30, headers=headers, follow_redirects=True) as client:
                page = await client.get(f"https://www.youtube.com/channel/{channel_id}/community?hl=en&gl=US")
                km = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', page.text)
                vm = re.search(r'"INNERTUBE_CONTEXT_CLIENT_VERSION":"([^"]+)"', page.text) or re.search(r'"clientVersion":"([0-9.]+)"', page.text)
                if not km:
                    return 0
                body = {"context": {"client": {"clientName": "WEB", "clientVersion": vm.group(1) if vm else "2.20240101.00.00", "hl": "en", "gl": "US"}},
                        "browseId": channel_id, "params": "Egljb21tdW5pdHk="}  # community tab
                resp = await client.post(f"https://www.youtube.com/youtubei/v1/browse?key={km.group(1)}&prettyPrint=false", json=body)
            if resp.status_code != 200:
                return 0
            data = resp.json()
        except Exception as e:
            logger.debug("community fetch/parse failed %s: %s", channel_id, e)
            return 0

        posts = []
        def walk(o):
            if isinstance(o, dict):
                for rk in ("postRenderer", "sharedPostRenderer", "backstagePostRenderer"):
                    if rk in o:
                        posts.append(o[rk])
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)
        walk(data)

        saved = 0
        async with self.pool.acquire() as conn:
            for p in posts[:max_posts]:
                pid = p.get("postId")
                if not pid:
                    continue
                text = "".join(r.get("text", "") for r in (p.get("contentText", {}).get("runs") or []))
                votes = self._parse_count((p.get("voteCount") or {}).get("simpleText"))
                pj = json.dumps(p)
                im = re.search(r'(https://[^"\\]+(?:ggpht|ytimg)[^"\\]+)', pj)
                image_url = im.group(1) if im else None
                # NB: pj = json.dumps adds a space after ':' -> tolerate \s*.
                cm = re.search(r'"replyButton".{0,800}?"simpleText":\s*"([^"]+)"', pj)
                comments = self._parse_count(cm.group(1)) if cm else None
                try:
                    await conn.execute(
                        """
                        INSERT INTO youtube_community_posts
                          (platform_post_id, channel_id, text, likes_count, comments_count, has_image, image_url, collected_at)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,NOW())
                        ON CONFLICT (platform_post_id) DO UPDATE SET
                          text=EXCLUDED.text, likes_count=EXCLUDED.likes_count,
                          comments_count=EXCLUDED.comments_count,
                          has_image=EXCLUDED.has_image, image_url=EXCLUDED.image_url
                        """,
                        pid, channel_id, text or None, votes, comments, bool(image_url), image_url,
                    )
                    saved += 1
                except Exception:
                    logger.debug("community upsert failed %s", pid, exc_info=True)
        if saved:
            logger.info("youtube: +%d community post(s) for channel %s", saved, channel_id)
        return saved

    async def _community_pass(self, batch_size: int = 15) -> int:
        """Per-cycle bounded sweep: refresh community posts for channels, oldest first."""
        if not self.pool:
            return 0
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT platform_channel_id FROM youtube_channels ORDER BY updated_at ASC NULLS FIRST LIMIT $1",
                batch_size,
            )
        total = 0
        for r in rows:
            if self._stop.is_set():
                break
            total += await self._collect_community_posts(r["platform_channel_id"])
        return total

    async def _upsert_video(self, channel_id: str, video_data: dict):
        async with self.pool.acquire() as conn:
            channel_row = await conn.fetchrow("SELECT id FROM youtube_channels WHERE platform_channel_id = $1", channel_id)
            channel_uuid = channel_row['id'] if channel_row else None
            
            snippet = video_data.get("snippet", {})
            stats = video_data.get("statistics", {})

            # search.list returns id as {"kind":"...","videoId":"..."}; videos.list returns id as a string.
            raw_id = video_data.get("id")
            if isinstance(raw_id, dict):
                video_id = raw_id.get("videoId")
            else:
                video_id = raw_id
            if not video_id:
                return

            await conn.execute("""
                INSERT INTO youtube_videos (
                    platform_video_id, channel_id, title, description,
                    tags, view_count, like_count, comment_count,
                    platform_published_at, metadata
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                ON CONFLICT (platform_video_id) DO UPDATE SET
                    view_count = EXCLUDED.view_count,
                    like_count = EXCLUDED.like_count,
                    comment_count = EXCLUDED.comment_count,
                    collected_at = NOW(),
                    metadata = EXCLUDED.metadata
            """,
            video_id,
            channel_uuid, snippet.get("title"), snippet.get("description"),
            snippet.get("tags"),
            int(stats.get("viewCount", 0) or 0),
            int(stats.get("likeCount", 0) or 0),
            int(stats.get("commentCount", 0) or 0),
            datetime.fromisoformat(snippet.get("publishedAt").replace("Z", "")) if snippet.get("publishedAt") else None,
            json.dumps(video_data)
            )

    async def _resolve_channel(self, channel_input: str) -> tuple[str, str]:
        if not self._has_auth:
            return channel_input, channel_input

        async with httpx.AsyncClient(timeout=30) as client:
            if channel_input.startswith("UC"):
                headers, params = self._yt_auth({"part": "snippet", "id": channel_input})
                resp = await client.get(f"{YT_API_BASE}/channels", params=params, headers=headers)
            elif channel_input.startswith("@"):
                headers, params = self._yt_auth({"part": "snippet", "forHandle": channel_input})
                resp = await client.get(f"{YT_API_BASE}/channels", params=params, headers=headers)
            else:
                headers, params = self._yt_auth({"part": "snippet", "q": channel_input, "type": "channel", "maxResults": 1})
                resp = await client.get(f"{YT_API_BASE}/search", params=params, headers=headers)

            if resp.status_code != 200: return channel_input, channel_input
            data = resp.json()
            items = data.get("items", [])
            if not items: return channel_input, channel_input
            item = items[0]
            snippet = item.get("snippet", {})
            cid = item.get("id", {})
            if isinstance(cid, dict): cid = cid.get("channelId", channel_input)
            return cid, snippet.get("title", channel_input)

    async def _collect_video_list_via_api(self, channel_id: str, channel_name: str, uploads_playlist: str) -> list[str]:
        """List videos via the channel's uploads playlist (1 quota unit/page vs search.list's 100, and reliably returns all uploads)."""
        video_ids = []
        page_token = ""
        async with httpx.AsyncClient(timeout=30) as client:
            while not self._stop.is_set():
                await asyncio.sleep(self._api_delay)
                base_params = {"part": "snippet,contentDetails", "playlistId": uploads_playlist, "maxResults": 50}
                if page_token: base_params["pageToken"] = page_token
                headers, params = self._yt_auth(base_params)
                async with self._sem:
                    resp = await client.get(f"{YT_API_BASE}/playlistItems", params=params, headers=headers)
                if resp.status_code == 403:
                    logger.warning("YouTube playlistItems 403 (quota or permission) for channel %s", channel_id)
                    break
                resp.raise_for_status()
                data = resp.json()
                items = data.get("items", [])
                if not items:
                    logger.info("YouTube uploads playlist %s returned 0 items (channel may have no public videos)", uploads_playlist)
                    break
                logger.info("YouTube fetched %d videos from playlist %s (page_token=%s)", len(items), uploads_playlist, page_token or "first")
                for item in items:
                    if self._stop.is_set(): break
                    # playlistItems uses item.contentDetails.videoId, NOT item.id (which is the playlistItem ID)
                    video_id = item.get("contentDetails", {}).get("videoId") or item.get("snippet", {}).get("resourceId", {}).get("videoId")
                    if not video_id: continue
                    video_ids.append(video_id)
                    # Re-shape so _upsert_video sees the videoId in `id` (string), and snippet/contentDetails carry through
                    video_data = {
                        "id": video_id,
                        "snippet": item.get("snippet", {}),
                        "contentDetails": item.get("contentDetails", {}),
                        "statistics": {},  # playlistItems doesn't return statistics; could batch-fetch via videos.list later
                    }
                    try:
                        await self._upsert_video(channel_id, video_data)
                        self._progress_count += 1   # tick watchdog even if yt-dlp download fails
                    except Exception as e:
                        logger.error("YouTube _upsert_video failed for %s: %s", video_id, e)
                    thumbs = item.get("snippet", {}).get("thumbnails", {})
                    best = thumbs.get("maxres") or thumbs.get("high") or thumbs.get("medium")
                    if best and best.get("url") and not self.is_known(video_id):
                        try:
                            await self.download_media({"entity_id": channel_id, "entity_name": channel_name, "content_type": "thumbnail", "content_id": video_id, "url": best["url"], "extension": "jpg", "source_url": f"https://www.youtube.com/watch?v={video_id}", "raw": video_data})
                        except Exception as e:
                            logger.warning("YouTube thumbnail download failed for %s: %s", video_id, e)
                page_token = data.get("nextPageToken", "")
                if not page_token: break
        logger.info("YouTube collected %d video IDs for channel %s", len(video_ids), channel_id)
        if video_ids:
            try:
                await self._enrich_video_stats(video_ids)
            except Exception as e:
                logger.error("YouTube batch enrichment failed for channel %s: %s", channel_id, e)
        return video_ids

    async def _enrich_video_stats(self, video_ids: list[str]):
        """Batch-fetch statistics+contentDetails via videos.list (50 IDs/req) and UPDATE youtube_videos."""
        async with httpx.AsyncClient(timeout=30) as client:
            for i in range(0, len(video_ids), 50):
                if self._stop.is_set(): break
                chunk = video_ids[i:i + 50]
                await asyncio.sleep(self._api_delay)
                try:
                    headers, params = self._yt_auth({"part": "statistics,contentDetails", "id": ",".join(chunk)})
                    async with self._sem:
                        resp = await client.get(f"{YT_API_BASE}/videos", params=params, headers=headers)
                    if resp.status_code == 403:
                        logger.warning("YouTube videos.list 403 (quota or permission); skipping enrichment for %d ids", len(chunk))
                        break
                    resp.raise_for_status()
                    data = resp.json()
                    items = data.get("items", [])
                    logger.info("YouTube videos.list enrichment fetched stats for %d/%d videos", len(items), len(chunk))
                    async with self.pool.acquire() as conn:
                        for v in items:
                            vid = v.get("id")
                            if not vid: continue
                            stats = v.get("statistics", {}) or {}
                            cd = v.get("contentDetails", {}) or {}
                            extra_meta = {"statistics": stats, "contentDetails": cd}
                            try:
                                await conn.execute(
                                    """
                                    UPDATE youtube_videos
                                    SET view_count = $1,
                                        like_count = $2,
                                        comment_count = $3,
                                        duration = $4,
                                        metadata = COALESCE(metadata, '{}'::jsonb) || $5::jsonb
                                    WHERE platform_video_id = $6
                                    """,
                                    int(stats.get("viewCount", 0) or 0),
                                    int(stats.get("likeCount", 0) or 0),
                                    int(stats.get("commentCount", 0) or 0),
                                    cd.get("duration"),
                                    json.dumps(extra_meta),
                                    vid,
                                )
                            except Exception as e:
                                logger.error("YouTube enrichment UPDATE failed for %s: %s", vid, e)
                except Exception as e:
                    logger.error("YouTube videos.list batch failed (chunk starting %s): %s", chunk[0] if chunk else "?", e)

    async def _filter_video_ids_for_download(self, video_ids: list[str]) -> tuple[list[str], int]:
        if not video_ids or not self._max_duration or not self.pool:
            return list(video_ids or []), 0
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT platform_video_id, duration
                    FROM youtube_videos
                    WHERE platform_video_id = ANY($1::text[])
                    """,
                    list(video_ids),
                    timeout=10,
                )
        except Exception:
            logger.debug("youtube: duration prefilter failed; keeping all candidates", exc_info=True)
            return list(video_ids), 0

        duration_by_id = {r["platform_video_id"]: r["duration"] for r in rows}
        limit_seconds = self._max_duration * 60
        kept: list[str] = []
        skipped = 0
        for vid in video_ids:
            seconds = parse_iso8601_duration(duration_by_id.get(vid) or "")
            if seconds and seconds > limit_seconds:
                skipped += 1
                continue
            kept.append(vid)
        return kept, skipped

    async def _download_videos_via_yt_dlp(
        self,
        channel_id: str,
        channel_name: str,
        video_ids: list[str],
        *,
        allow_channel_fallback: bool = True,
    ) -> int:
        from src.core.subprocess_downloader import yt_dlp_download, managed_tempdir
        candidates, skipped_duration = await self._filter_video_ids_for_download(video_ids)
        urls = [
            f"https://www.youtube.com/watch?v={vid}"
            for vid in candidates
            if not self.is_known(f"video_{vid}")
        ]
        if not urls:
            if video_ids:
                known_count = sum(1 for vid in candidates if self.is_known(f"video_{vid}"))
                logger.info(
                    "youtube: no video files to download for %s (%d known, %d skipped by duration cap)",
                    channel_id,
                    known_count,
                    skipped_duration,
                )
                return 0
            if not allow_channel_fallback:
                logger.info("youtube: no explicit video IDs for %s; channel fallback disabled", channel_id)
                return 0
            urls = [f"https://www.youtube.com/channel/{channel_id}/videos"]
        logger.info(
            "youtube yt-dlp starting video downloads for %s: urls=%d candidates=%d skipped_duration=%d",
            channel_id,
            len(urls),
            len(candidates),
            skipped_duration,
        )
        stored_total = 0
        for url in urls:
            if self._stop.is_set(): break
            await asyncio.sleep(self._download_delay)
            async with managed_tempdir("yt_") as tmpdir:
                logger.info("youtube yt-dlp starting %s", url)
                extra: list[str] = ["-f", self._ytdlp_format, "--merge-output-format", self._merge_format]
                if self._max_duration:
                    extra.extend(["--match-filter", f"duration<={self._max_duration * 60}"])
                if self._cookie_browser:
                    extra.extend(["--cookies-from-browser", self._cookie_browser])
                if "watch?v=" not in url:
                    extra.extend(["--playlist-end", "50"])
                result = await yt_dlp_download(
                    url,
                    cookies_file=self._cookie_file,
                    output_template=os.path.join(tmpdir, "%(id)s.%(ext)s"),
                    max_downloads=None,  # respect playlist-end / per-video
                    retries=3,
                    impersonate="chrome",
                    write_thumbnail=True,
                    no_overwrites=True,
                    extra_args=extra,
                    timeout=600,
                    tempdir=tmpdir,
                    stop_event=self._stop if hasattr(self._stop, "wait") else None,
                )
                if not result.ok and not result.cancelled:
                    logger.warning(
                        "youtube yt-dlp video download failed for %s: rc=%s timed_out=%s stderr_tail=%s",
                        url, result.returncode, result.timed_out, result.err_summary(400),
                    )
                    self._progress_count += 1  # tick watchdog so metadata-only runs don't look hung
                eligible_files = 0
                stored_files = 0
                skipped_known = 0
                skipped_extension = 0
                for f in result.files:
                    if self._stop.is_set(): break
                    ext = f.suffix.lstrip(".").lower()
                    if ext not in ("jpg", "jpeg", "png", "webp", "mp4", "webm", "mkv"):
                        skipped_extension += 1
                        continue
                    eligible_files += 1
                    cid = f.stem
                    is_video = ext in ("mp4", "webm", "mkv")
                    if is_video: cid = f"video_{cid}"
                    if self.is_known(cid):
                        skipped_known += 1
                        continue
                    inserted = await self.download_media({"entity_id": channel_id, "entity_name": channel_name, "content_type": "video" if is_video else "thumbnail", "content_id": cid, "data": f.read_bytes(), "extension": ext if ext != "jpeg" else "jpg", "source_url": url if "watch?v=" in url else f"https://www.youtube.com/watch?v={f.stem}"})
                    if inserted:
                        stored_files += 1
                        stored_total += 1
                logger.info(
                    "youtube yt-dlp media ingest for %s: result_files=%d eligible=%d stored=%d skipped_known=%d skipped_ext=%d",
                    url,
                    len(result.files),
                    eligible_files,
                    stored_files,
                    skipped_known,
                    skipped_extension,
                )
        return stored_total

    async def _collect_thumbnails_via_yt_dlp(self, channel_id: str, channel_name: str):
        from src.core.subprocess_downloader import yt_dlp_download, managed_tempdir
        channel_url = f"https://www.youtube.com/channel/{channel_id}/videos"
        async with managed_tempdir("yt_thumb_") as tmpdir:
            extra: list[str] = ["--skip-download", "--playlist-end", "50"]
            if self._cookie_browser:
                extra.extend(["--cookies-from-browser", self._cookie_browser])
            result = await yt_dlp_download(
                channel_url,
                cookies_file=self._cookie_file,
                output_template=os.path.join(tmpdir, "%(id)s.%(ext)s"),
                max_downloads=None,
                retries=3,
                impersonate="chrome",
                write_thumbnail=True,
                no_overwrites=True,
                extra_args=extra,
                timeout=600,
                tempdir=tmpdir,
                stop_event=self._stop if hasattr(self._stop, "wait") else None,
            )
            for f in result.files:
                if self._stop.is_set(): break
                ext = f.suffix.lstrip(".").lower()
                if ext not in ("jpg", "jpeg", "png", "webp"): continue
                if self.is_known(f.stem): continue
                await self.download_media({"entity_id": channel_id, "entity_name": channel_name, "content_type": "thumbnail", "content_id": f.stem, "data": f.read_bytes(), "extension": ext if ext != "jpeg" else "jpg", "source_url": f"https://www.youtube.com/watch?v={f.stem}"})

    async def _enrich_transcripts_and_comments(self, limit: int = 10):
        """Per-tick enrichment: pick up to `limit` videos missing a transcript and/or comments,
        and fetch them via yt-dlp. Each missing artifact is fetched independently so a video
        without a transcript can still get its comments scraped (and vice versa)."""
        # Fetch transcript candidates
        if self._fetch_transcripts:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT v.id, v.platform_video_id
                    FROM youtube_videos v
                    WHERE NOT EXISTS (
                        SELECT 1 FROM youtube_transcripts t WHERE t.video_id = v.id
                    )
                    ORDER BY v.platform_published_at DESC NULLS LAST
                    LIMIT $1
                    """,
                    limit,
                )
            logger.info("YouTube enrichment: %d videos missing transcripts (limit=%d)", len(rows), limit)
            for row in rows:
                if self._stop.is_set():
                    break
                try:
                    await self._fetch_transcript(row["id"], row["platform_video_id"])
                except Exception as e:
                    logger.warning("YouTube transcript fetch failed for %s: %s", row["platform_video_id"], e)
                await asyncio.sleep(self._download_delay)

        # Fetch comment candidates
        if self._fetch_comments_enabled:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT v.id, v.platform_video_id
                    FROM youtube_videos v
                    WHERE NOT EXISTS (
                        SELECT 1 FROM youtube_comments c WHERE c.video_id = v.id
                    )
                    ORDER BY v.platform_published_at DESC NULLS LAST
                    LIMIT $1
                    """,
                    limit,
                )
            logger.info("YouTube enrichment: %d videos missing comments (limit=%d)", len(rows), limit)
            for row in rows:
                if self._stop.is_set():
                    break
                try:
                    await self._fetch_comments(row["id"], row["platform_video_id"])
                except Exception as e:
                    logger.warning("YouTube comments fetch failed for %s: %s", row["platform_video_id"], e)
                await asyncio.sleep(self._download_delay)

    @staticmethod
    def _vtt_to_text(vtt: str) -> str:
        """Strip WebVTT timing/header/style blocks and de-duplicate consecutive cue lines.
        YouTube auto-captions carry a rolling overlap; the simple de-dup below drops most of it."""
        return _parse_vtt_to_text(vtt)

    async def _fetch_transcript(self, video_uuid, platform_video_id: str):
        """Run yt-dlp --write-subs/--write-auto-subs to grab a VTT subtitle file, parse to text,
        and INSERT into youtube_transcripts."""
        url = f"https://www.youtube.com/watch?v={platform_video_id}"
        with tempfile.TemporaryDirectory() as tmpdir:
            output_tmpl = os.path.join(tmpdir, "%(id)s.%(ext)s")
            cmd = [
                "yt-dlp", "--js-runtime", "node", "--impersonate", "chrome",
                "--write-subs", "--write-auto-subs",
                "--sub-lang", self._transcript_lang,
                "--sub-format", "vtt",
                "--skip-download", "--no-warnings",
                "-o", output_tmpl,
                "--retries", "2", "--socket-timeout", "30",
            ]
            if self._cookie_browser:
                cmd.extend(["--cookies-from-browser", self._cookie_browser])
            if self._cookie_file:
                cmd.extend(["--cookies", self._cookie_file])
            cmd.extend(["--", url])
            loop = asyncio.get_event_loop()
            proc = await loop.run_in_executor(
                None, lambda: subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            )
            if proc.returncode != 0:
                stderr_tail = (proc.stderr or "").strip().splitlines()[-3:]
                logger.info("yt-dlp transcript rc=%s for %s: %s", proc.returncode, platform_video_id, " | ".join(stderr_tail))
            # Find any .vtt files yt-dlp dropped
            vtt_files = sorted(Path(tmpdir).rglob("*.vtt"))
            if not vtt_files:
                logger.info("YouTube transcript: no VTT for %s (skipping)", platform_video_id)
                return
            vtt_path = vtt_files[0]
            # Heuristic: filenames look like <id>.<lang>.vtt or <id>.<lang>-orig.vtt
            name_parts = vtt_path.name.split(".")
            lang = name_parts[1] if len(name_parts) >= 3 else self._transcript_lang
            # If there is a manual sub for this video, prefer it over an auto sub
            for f in vtt_files:
                stem = f.name
                if "-orig" in stem or ".auto" in stem:
                    continue
                vtt_path = f
                lang = stem.split(".")[1] if len(stem.split(".")) >= 3 else lang
                break
            try:
                vtt_text = vtt_path.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                logger.warning("YouTube transcript: cannot read %s: %s", vtt_path, e)
                return
            content = self._vtt_to_text(vtt_text)
            if not content:
                logger.info("YouTube transcript: empty after parse for %s", platform_video_id)
                return
            is_generated = "auto" in vtt_path.name.lower() or "-orig" in vtt_path.name.lower()
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO youtube_transcripts (video_id, language, is_generated, content, collected_at)
                    VALUES ($1, $2, $3, $4, $5)
                    """,
                    video_uuid, lang[:10], is_generated, content, datetime.now(timezone.utc),
                )
            logger.info("YouTube transcript saved for %s (%s, %d chars, generated=%s)", platform_video_id, lang, len(content), is_generated)

    @staticmethod
    def _parse_relative_timestamp(text: str) -> datetime | None:
        """Best-effort parse for YouTube relative timestamps like '3 days ago' or '1 month ago (edited)'."""
        return _parse_rel_ts(text)

    async def _fetch_comments(self, video_uuid, platform_video_id: str):
        """Run yt-dlp --write-comments to dump comments into the .info.json,
        then parse and bulk-INSERT into youtube_comments."""
        url = f"https://www.youtube.com/watch?v={platform_video_id}"
        with tempfile.TemporaryDirectory() as tmpdir:
            output_tmpl = os.path.join(tmpdir, "%(id)s.%(ext)s")
            cmd = [
                "yt-dlp", "--js-runtime", "node", "--impersonate", "chrome",
                "--write-comments", "--write-info-json",
                "--skip-download", "--no-warnings",
                "--extractor-args", f"youtube:max_comments={self._max_comments},all,all,all;comment_sort=top",
                "-o", output_tmpl,
                "--retries", "2", "--socket-timeout", "60",
            ]
            if self._cookie_browser:
                cmd.extend(["--cookies-from-browser", self._cookie_browser])
            if self._cookie_file:
                cmd.extend(["--cookies", self._cookie_file])
            cmd.extend(["--", url])
            loop = asyncio.get_event_loop()
            proc = await loop.run_in_executor(
                None, lambda: subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            )
            if proc.returncode != 0:
                stderr_tail = (proc.stderr or "").strip().splitlines()[-3:]
                logger.info("yt-dlp comments rc=%s for %s: %s", proc.returncode, platform_video_id, " | ".join(stderr_tail))
            info_files = sorted(Path(tmpdir).rglob("*.info.json"))
            if not info_files:
                logger.info("YouTube comments: no info.json for %s (skipping)", platform_video_id)
                return
            try:
                info = json.loads(info_files[0].read_text(encoding="utf-8", errors="replace"))
            except Exception as e:
                logger.warning("YouTube comments: bad info.json for %s: %s", platform_video_id, e)
                return
            comments = info.get("comments") or []
            if not comments:
                logger.info("YouTube comments: 0 comments returned for %s", platform_video_id)
                return
            inserted = 0
            async with self.pool.acquire() as conn:
                for c in comments:
                    try:
                        cid = c.get("id")
                        if not cid:
                            continue
                        parent = c.get("parent")
                        is_reply = bool(parent and parent != "root")
                        ts = c.get("timestamp")
                        if ts:
                            try:
                                published_at = datetime.fromtimestamp(int(ts), tz=timezone.utc)
                            except Exception:
                                published_at = None
                        else:
                            published_at = self._parse_relative_timestamp(c.get("_time_text") or c.get("time_text") or "")
                        await conn.execute(
                            """
                            INSERT INTO youtube_comments (
                                platform_comment_id, video_id, author_name, author_channel_id,
                                author_thumbnail_url, text_original, like_count,
                                parent_comment_id, is_reply, platform_published_at, collected_at
                            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                            ON CONFLICT (platform_comment_id) DO UPDATE SET
                                like_count = EXCLUDED.like_count,
                                text_original = EXCLUDED.text_original
                            """,
                            cid,
                            video_uuid,
                            c.get("author"),
                            c.get("author_id") or c.get("channel_id"),
                            c.get("author_thumbnail"),
                            c.get("text"),
                            int(c.get("like_count") or 0),
                            parent if is_reply else None,
                            is_reply,
                            published_at,
                            datetime.now(timezone.utc),
                        )
                        inserted += 1
                    except Exception as e:
                        logger.debug("YouTube comment insert failed (%s): %s", c.get("id"), e)
            logger.info("YouTube comments saved for %s: %d / %d", platform_video_id, inserted, len(comments))

    async def get_backfill_items(self, batch_size: int) -> list[dict]:
        if not self.pool:
            return []
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT v.platform_video_id, c.platform_channel_id, c.title AS channel_name
                FROM youtube_videos v
                LEFT JOIN youtube_channels c ON v.channel_id = c.id
                LEFT JOIN media_items mi
                    ON mi.source = 'youtube'
                    AND mi.content_id = v.platform_video_id
                WHERE mi.id IS NULL
                ORDER BY v.collected_at DESC NULLS LAST
                LIMIT $1
            """, batch_size)
        return [{"entity_id": r["platform_channel_id"] or "unknown",
                 "entity_name": r["channel_name"] or "unknown",
                 "content_type": "thumbnail",
                 "content_id": r["platform_video_id"],
                 "url": f"https://i.ytimg.com/vi/{r['platform_video_id']}/maxresdefault.jpg",
                 "extension": "jpg",
                 "source_url": f"https://www.youtube.com/watch?v={r['platform_video_id']}",
                 "_video_id": r["platform_video_id"]}
                for r in rows]

    async def _get_video_backfill_groups(self, batch_size: int) -> dict[tuple[str, str], list[str]]:
        if not self.pool or batch_size <= 0:
            return {}
        scan_limit = max(batch_size, self._video_backfill_scan_limit)
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT v.platform_video_id, v.duration,
                       COALESCE(c.platform_channel_id, 'unknown') AS platform_channel_id,
                       COALESCE(c.title, 'unknown') AS channel_name
                FROM youtube_videos v
                LEFT JOIN youtube_channels c ON v.channel_id = c.id
                LEFT JOIN media_items mi
                  ON mi.source = 'youtube'
                 AND mi.content_id = 'video_' || v.platform_video_id
                WHERE mi.id IS NULL
                ORDER BY v.collected_at DESC NULLS LAST
                LIMIT $1
                """,
                scan_limit,
                timeout=20,
            )

        groups: dict[tuple[str, str], list[str]] = {}
        limit_seconds = self._max_duration * 60 if self._max_duration else 0
        selected = 0
        skipped_duration = 0
        skipped_live_placeholder = 0
        for row in rows:
            if selected >= batch_size:
                break
            duration = row["duration"] or ""
            if duration.upper() in {"P0D", "PT0S"}:
                skipped_live_placeholder += 1
                continue
            duration_seconds = parse_iso8601_duration(duration)
            if limit_seconds and duration_seconds and duration_seconds > limit_seconds:
                skipped_duration += 1
                continue
            key = (row["platform_channel_id"], row["channel_name"])
            groups.setdefault(key, []).append(row["platform_video_id"])
            selected += 1
        if skipped_live_placeholder:
            logger.info(
                "youtube: video backfill skipped %d live/scheduled placeholder candidate(s)",
                skipped_live_placeholder,
            )
        if skipped_duration:
            logger.info(
                "youtube: video backfill skipped %d over-duration candidate(s) before selecting %d from scan_limit=%d",
                skipped_duration,
                selected,
                scan_limit,
            )
        return groups

    async def run_backfill(self):
        thumbnail_count = await super().run_backfill()
        if not self._download_videos or not self._use_yt_dlp or self._video_backfill_batch_size <= 0:
            return thumbnail_count
        groups = await self._get_video_backfill_groups(self._video_backfill_batch_size)
        if not groups:
            return thumbnail_count

        attempted = sum(len(v) for v in groups.values())
        stored = 0
        for (channel_id, channel_name), video_ids in groups.items():
            if self._stop.is_set():
                break
            stored += await self._download_videos_via_yt_dlp(
                channel_id,
                channel_name,
                video_ids,
                allow_channel_fallback=False,
            )
        logger.info(
            "youtube: video backfill attempted %d candidate(s) across %d channel(s), stored %d media item(s)",
            attempted,
            len(groups),
            stored,
        )
        return thumbnail_count + stored

    @staticmethod
    def _build_youtube_source_url(item: dict) -> str | None:
        """Canonical YouTube watch URL (media_items.source_url) for the
        stored file. Content-id conventions inside this collector:
          content_type=thumbnail: content_id = <video_id>          (11-char)
          content_type=video:     content_id = "video_<video_id>"
          content_type=profile_photo: content_id = "profile_<channel_id>"
        Returns the watch URL for video/thumbnail (both point at the same
        page), the channel URL for profile_photo, or None if we can't
        extract an ID from the content_id shape."""
        ctype = item.get("content_type") or ""
        cid = item.get("content_id") or ""
        if ctype == "profile_photo":
            channel = cid[len("profile_"):] if cid.startswith("profile_") else None
            if channel:
                return f"https://www.youtube.com/channel/{channel}"
            return None
        if ctype == "video":
            vid = cid[len("video_"):] if cid.startswith("video_") else cid
        elif ctype == "thumbnail":
            vid = cid
        else:
            return None
        if not vid:
            return None
        return f"https://www.youtube.com/watch?v={vid}"

    async def download_media(self, item: dict):
        cid = item["content_id"]
        if self.is_known(cid): return False
        filename = self.build_filename(item["entity_id"], item["entity_name"], item["content_type"], cid, extension=item.get("extension", "jpg"))
        request_url = item.get("url")
        try:
            if "data" in item: data = item["data"]
            elif "url" in item:
                await self.wait_rate_limit("googleapis.com")
                request_url = item["url"]
                candidate_urls = [request_url]
                if "maxresdefault" in request_url:
                    candidate_urls.extend(
                        request_url.replace("maxresdefault", fallback)
                        for fallback in ("hqdefault", "mqdefault", "default")
                    )
                elif "hqdefault" in request_url:
                    candidate_urls.extend(
                        request_url.replace("hqdefault", fallback)
                        for fallback in ("mqdefault", "default")
                    )
                async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                    resp = None
                    for candidate_url in candidate_urls:
                        resp = await client.get(candidate_url)
                        request_url = candidate_url
                        if resp.status_code != 404:
                            break
                    assert resp is not None
                    resp.raise_for_status()
                    data = resp.content
            else: return False
            source_url = self._build_youtube_source_url(item)
            metadata = {
                "entity_id": item["entity_id"],
                "entity_name": item["entity_name"],
                "content_type": item["content_type"],
                "content_id": cid,
                "collected_at": datetime.now(timezone.utc).isoformat(),
                "raw": item.get("raw", {}),
                "rebuild_target_tables": ["media_items", "youtube_videos", "youtube_channels"],
            }
            artifact = write_atomic_artifact(
                source=self.SOURCE_NAME,
                artifact_id=cid,
                artifact_kind="media_blob",
                data=data,
                extension=item.get("extension", "jpg"),
                metadata={
                    **metadata,
                    "filename": filename,
                    "source_url": source_url,
                    "request_url": request_url if "url" in item else item.get("url"),
                },
                root=VAULT_ROOT,
            )
            if not artifact.path:
                raise RuntimeError(f"vault artifact write failed: {artifact.error}")
            metadata["vault_artifact"] = {
                "ok": artifact.ok,
                "partial": artifact.partial,
                "path": artifact.relative_path,
                "blob_path": artifact.blob_relative_path,
                "sidecar_path": artifact.sidecar.relative_path if artifact.sidecar else None,
                "duplicate_blob": artifact.duplicate_blob,
                "error": artifact.error,
            }
            inserted = await self.insert_media_item(
                entity_id=item["entity_id"],
                entity_name=item["entity_name"],
                content_type=item["content_type"],
                content_id=cid,
                filename=filename,
                file_path=str(artifact.path),
                file_size=artifact.file_size,
                sha256=artifact.sha256,
                metadata=metadata,
                source_url=source_url,
            )
            if artifact.partial:
                await self.send_to_dlq(item["entity_id"], cid, f"vault artifact partial: {artifact.error}")
            self._known_ids.add(cid)
            return inserted
        except Exception as e:
            logger.error("Download failed %s: %s", cid, e)
            await self.send_to_dlq(item["entity_id"], cid, str(e))
            return False

    # ─────────────────────────────────────────────────────────────────────
    # Wave 2: toolkit-parity public verbs.
    # Each maps to a youtubetoolkit/scripts/*.py operator command and is
    # designed to be invoked individually by the scheduler.
    # ─────────────────────────────────────────────────────────────────────

    async def _ensure_auth(self) -> bool:
        """Lazy-load OAuth credentials and set _has_auth. Returns True if any auth is present."""
        if not getattr(self, "_has_auth", False):
            if not self._api_key:
                self._load_oauth_credentials()
            self._has_auth = bool(self._api_key or self._oauth_credentials)
        return self._has_auth

    async def _api_get(self, path: str, params: dict, *, client: "httpx.AsyncClient | None" = None) -> dict:
        """Single GET against the YouTube Data API with the configured auth.

        Returns parsed JSON dict on 200, or {} on non-200 / quota / error.
        Wraps existing _yt_auth() to keep auth logic uniform across new verbs.
        """
        headers, qparams = self._yt_auth(params)
        own_client = client is None
        if own_client:
            client = httpx.AsyncClient(timeout=30, headers=headers)
        try:
            resp = await client.get(f"{YT_API_BASE}/{path}", params=qparams, headers=headers if not own_client else None)
            if resp.status_code != 200:
                logger.warning("YouTube API %s status=%s body=%s", path, resp.status_code, resp.text[:200])
                return {}
            return resp.json()
        finally:
            if own_client:
                await client.aclose()

    # ── collect_subscriptions ───────────────────────────────────────────

    async def collect_subscriptions(self, *, max_channels: int | None = None,
                                    fetch_details: bool = False,
                                    since_last_scrape: bool = False) -> list[dict]:
        """List the authenticated user's subscribed channels via Data API.

        Mirrors youtubetoolkit/scripts/subscription_processor.get_subscriptions
        + process_subscriptions(since_last_scrape=...). Requires OAuth.

        Returns a list of {channel_id, channel_name, channel_url[, bio,
        subscriber_count]} dicts. Also caches the result to
        _subscription_cache_file with last_scrape_time bookkeeping.
        """
        await self._ensure_auth()
        if not self._oauth_credentials:
            logger.warning("collect_subscriptions requires OAuth (got none)")
            return []

        cap = max_channels if max_channels is not None else self._max_subscriptions
        subscriptions: list[dict] = []
        page_token = ""

        async with httpx.AsyncClient(timeout=30) as client:
            while len(subscriptions) < cap:
                if self._stop.is_set():
                    break
                params = {"part": "snippet", "mine": "true", "maxResults": 50}
                if page_token:
                    params["pageToken"] = page_token
                data = await self._api_get("subscriptions", params, client=client)
                items = data.get("items", []) or []
                if not items:
                    break
                for item in items:
                    if len(subscriptions) >= cap:
                        break
                    snippet = item.get("snippet", {}) or {}
                    rid = snippet.get("resourceId", {}) or {}
                    cid = rid.get("channelId")
                    if not cid:
                        continue
                    subscriptions.append({
                        "channel_id": cid,
                        "channel_name": snippet.get("title", ""),
                        "channel_url": f"https://www.youtube.com/channel/{cid}",
                    })
                page_token = data.get("nextPageToken", "")
                if not page_token:
                    break
                await asyncio.sleep(self._api_delay)

        if fetch_details and subscriptions:
            subscriptions = await self._fetch_channel_details(subscriptions)

        # Persist cache + last_scrape_time.
        try:
            self._subscription_cache_file.parent.mkdir(parents=True, exist_ok=True)
            existing = {}
            if self._subscription_cache_file.exists():
                try:
                    existing = json.loads(self._subscription_cache_file.read_text(encoding="utf-8"))
                except Exception:
                    existing = {}
            cache_data = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "subscriptions": subscriptions,
                "last_scrape_time": (
                    datetime.now(timezone.utc).isoformat() if since_last_scrape
                    else existing.get("last_scrape_time")
                ),
            }
            self._subscription_cache_file.write_text(
                json.dumps(cache_data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as e:
            logger.warning("Could not cache subscriptions: %s", e)

        logger.info("YouTube collected %d subscriptions", len(subscriptions))
        return subscriptions

    async def _fetch_channel_details(self, subscriptions: list[dict]) -> list[dict]:
        """Batch-enrich subscriptions with bio + subscriber_count via channels.list (50 ids/call)."""
        out: list[dict] = []
        async with httpx.AsyncClient(timeout=30) as client:
            for i in range(0, len(subscriptions), 50):
                if self._stop.is_set():
                    break
                batch = subscriptions[i:i + 50]
                ids = ",".join(s["channel_id"] for s in batch)
                data = await self._api_get(
                    "channels", {"part": "snippet,statistics", "id": ids}, client=client,
                )
                details = {}
                for it in data.get("items", []) or []:
                    sn = it.get("snippet", {}) or {}
                    st = it.get("statistics", {}) or {}
                    details[it.get("id")] = {
                        "bio": sn.get("description", ""),
                        "subscriber_count": int(st.get("subscriberCount", 0) or 0),
                    }
                for sub in batch:
                    enriched = dict(sub)
                    d = details.get(sub["channel_id"], {})
                    enriched["bio"] = d.get("bio", "")
                    enriched["subscriber_count"] = d.get("subscriber_count", 0)
                    out.append(enriched)
                if i + 50 < len(subscriptions):
                    await asyncio.sleep(self._api_delay)
        return out

    def _get_last_scrape_time(self) -> datetime | None:
        """Read last_scrape_time from subscription cache. None if absent."""
        try:
            if not self._subscription_cache_file.exists():
                return None
            data = json.loads(self._subscription_cache_file.read_text(encoding="utf-8"))
            ts = data.get("last_scrape_time")
            if ts:
                # Tolerate naive ISO timestamps from legacy toolkit cache
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
        except Exception as e:
            logger.warning("Could not read last scrape time: %s", e)
        return None

    # ── collect_liked_videos ────────────────────────────────────────────

    async def collect_liked_videos(self, *, max_videos: int | None = None,
                                   days: int | None = None) -> list[dict]:
        """Fetch the OAuth user's liked videos (Data API "LL" playlist) and upsert.

        Mirrors youtubetoolkit/scripts/scrape_liked_videos_enhanced.get_liked_videos.
        Requires OAuth. Returns the list of collected video summaries.
        """
        await self._ensure_auth()
        if not self._oauth_credentials:
            logger.warning("collect_liked_videos requires OAuth (got none)")
            return []

        cap = max_videos if max_videos is not None else self._max_liked_videos
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=days)
            if days else None
        )
        videos: list[dict] = []
        page_token = ""

        async with httpx.AsyncClient(timeout=30) as client:
            while len(videos) < cap:
                if self._stop.is_set():
                    break
                params = {
                    "part": "snippet,contentDetails",
                    "playlistId": LIKED_VIDEOS_PLAYLIST_ID,
                    "maxResults": min(50, cap - len(videos)),
                }
                if page_token:
                    params["pageToken"] = page_token
                data = await self._api_get("playlistItems", params, client=client)
                items = data.get("items", []) or []
                if not items:
                    break
                stop_iter = False
                for it in items:
                    if len(videos) >= cap:
                        stop_iter = True
                        break
                    sn = it.get("snippet", {}) or {}
                    rid = sn.get("resourceId", {}) or {}
                    vid = rid.get("videoId")
                    if not vid:
                        continue
                    if cutoff:
                        try:
                            added = datetime.fromisoformat(
                                sn.get("publishedAt", "").replace("Z", "+00:00")
                            )
                            if added < cutoff:
                                stop_iter = True
                                break
                        except Exception:
                            pass
                    videos.append({
                        "video_id": vid,
                        "url": f"https://www.youtube.com/watch?v={vid}",
                        "title": sn.get("title", ""),
                        "channel_id": sn.get("videoOwnerChannelId") or sn.get("channelId"),
                        "channel": sn.get("videoOwnerChannelTitle") or sn.get("channelTitle", ""),
                        "published_at": sn.get("publishedAt"),
                    })
                # Persist into youtube_videos.
                if items:
                    await self._upsert_liked_batch(items)
                if stop_iter:
                    break
                page_token = data.get("nextPageToken", "")
                if not page_token:
                    break
                await asyncio.sleep(self._api_delay)

        logger.info("YouTube collected %d liked videos", len(videos))
        return videos

    async def _upsert_liked_batch(self, items: list[dict]):
        """Upsert a page of liked-playlist items into youtube_videos.

        We treat the channel referenced in each item as the canonical
        owning channel; if not yet in youtube_channels we still upsert the
        video with channel_id=NULL (FK is ON DELETE SET NULL).
        """
        for it in items:
            if self._stop.is_set():
                break
            sn = it.get("snippet", {}) or {}
            rid = sn.get("resourceId", {}) or {}
            vid = rid.get("videoId")
            if not vid:
                continue
            owner_channel = sn.get("videoOwnerChannelId") or sn.get("channelId") or ""
            video_data = {
                "id": vid,
                "snippet": sn,
                "contentDetails": it.get("contentDetails", {}),
                "statistics": {},
            }
            try:
                await self._upsert_video(owner_channel, video_data)
            except Exception as e:
                logger.warning("YouTube upsert (liked) failed for %s: %s", vid, e)

    # ── collect_custom_playlist ────────────────────────────────────────

    async def collect_custom_playlist(self, url_or_id: str, *,
                                      days: int | None = None) -> list[dict]:
        """Scrape arbitrary YouTube Playlist or Channel URL via yt-dlp --flat-playlist.

        Mirrors youtubetoolkit/scripts/scrape_custom_playlist.scrape_custom_url.
        Uses yt-dlp (no quota cost). Returns list of {video_id, url, title, ...}
        and upserts each into youtube_videos.
        """
        if not self._use_yt_dlp:
            logger.warning("collect_custom_playlist needs yt-dlp; skipping")
            return []

        url = url_or_id
        if not url.startswith("http"):
            # Bare playlist or channel id
            if url.startswith("PL") or url.startswith("LL") or url.startswith("FL"):
                url = f"https://www.youtube.com/playlist?list={url}"
            else:
                url = f"https://www.youtube.com/channel/{url}"

        cmd = [
            "yt-dlp", "--js-runtime", "node", "--flat-playlist", "--dump-single-json",
            "--quiet", "--no-warnings", "--ignore-errors",
            "--socket-timeout", "30", "--retries", "2",
        ]
        if days:
            cmd.extend(["--dateafter", f"now-{days}days"])
        if self._cookie_browser:
            cmd.extend(["--cookies-from-browser", self._cookie_browser])
        if self._cookie_file:
            cmd.extend(["--cookies", self._cookie_file])
        cmd.extend(["--", url])

        loop = asyncio.get_event_loop()
        try:
            proc = await loop.run_in_executor(
                None, lambda: subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            )
        except subprocess.TimeoutExpired:
            logger.warning("yt-dlp custom playlist timed out: %s", url)
            return []
        if proc.returncode != 0 or not proc.stdout.strip():
            stderr_tail = (proc.stderr or "").strip().splitlines()[-3:]
            logger.warning("yt-dlp custom playlist rc=%s for %s: %s",
                           proc.returncode, url, " | ".join(stderr_tail))
            return []
        try:
            info = json.loads(proc.stdout)
        except Exception as e:
            logger.warning("yt-dlp custom playlist returned bad JSON for %s: %s", url, e)
            return []

        videos: list[dict] = []
        playlist_uploader = info.get("uploader") or info.get("channel") or info.get("title")
        playlist_channel_id = info.get("channel_id") or ""

        entries = info.get("entries") or [info]
        for ent in entries:
            if not ent:
                continue
            vid = ent.get("id")
            if not vid:
                continue
            video = {
                "video_id": vid,
                "url": ent.get("url") or f"https://www.youtube.com/watch?v={vid}",
                "title": ent.get("title", ""),
                "channel": ent.get("uploader") or playlist_uploader or "",
                "channel_id": ent.get("channel_id") or playlist_channel_id or "",
                "duration": ent.get("duration") or 0,
            }
            videos.append(video)
            video_data = {
                "id": vid,
                "snippet": {
                    "title": video["title"],
                    "description": ent.get("description", ""),
                    "channelId": video["channel_id"],
                    "publishedAt": ent.get("upload_date"),
                },
                "statistics": {
                    "viewCount": ent.get("view_count", 0) or 0,
                    "likeCount": ent.get("like_count", 0) or 0,
                },
            }
            try:
                await self._upsert_video(video["channel_id"], video_data)
            except Exception as e:
                logger.warning("YouTube upsert (custom) failed for %s: %s", vid, e)

        logger.info("YouTube custom playlist %s yielded %d videos", url, len(videos))
        return videos

    # ── collect_target_channel(s) ───────────────────────────────────────

    async def collect_target_channel(self, channel_id_or_url: str) -> list[str]:
        """Scrape a single target channel: upserts metadata + recent videos.

        Thin async-public wrapper around the existing _collect_channel(); kept
        explicit so the scheduler can dispatch to a single channel.
        """
        await self._ensure_auth()
        cid = channel_id_or_url
        if cid.startswith("http"):
            # Pull the trailing /channel/UC... or @handle if present
            m = re.search(r"/channel/(UC[\w-]+)", cid)
            if m:
                cid = m.group(1)
            else:
                m = re.search(r"/(@[\w.-]+)", cid)
                if m:
                    cid = m.group(1)
        await self._collect_channel(cid)
        return [cid]

    async def collect_target_channels(self, target_file: Path | str | None = None) -> list[str]:
        """Read target_channels.txt (one channel per line, # comments OK) and process each.

        Mirrors youtubetoolkit/scripts/scrape_targets.scrape_target_channels.
        """
        path = Path(target_file) if target_file else self._target_channels_file
        if not path.exists():
            logger.info("No target_channels file at %s", path)
            return []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception as e:
            logger.warning("Failed to read target_channels file %s: %s", path, e)
            return []
        channels = [l.strip() for l in lines if l.strip() and not l.strip().startswith("#")]
        if not channels:
            return []
        processed: list[str] = []
        for ch in channels:
            if self._stop.is_set():
                break
            try:
                await self.collect_target_channel(ch)
                processed.append(ch)
            except Exception as e:
                logger.error("collect_target_channel failed for %s: %s", ch, e)
                await self.send_to_dlq(ch, ch, str(e))
            await asyncio.sleep(self._api_delay)
        return processed

    # ── batch_download ──────────────────────────────────────────────────

    async def batch_download(self, target_list: list[str | dict] | None = None,
                             *, photos_only: bool = False) -> dict:
        """Download videos / thumbnails for a list of targets.

        Mirrors youtubetoolkit/scripts/batch_downloader. Each target is either
        a string (channel_id/url or video_id) or a dict {"video_id"/"url",
        "channel_id", "channel_name"}. If target_list is None, pull pending
        videos from the DB (no-channel filter, recent first).

        photos_only: when True, hand off to _collect_thumbnails_via_yt_dlp
        per channel_id and skip the video download tier.
        """
        if not self._use_yt_dlp:
            logger.warning("batch_download needs yt-dlp; skipping")
            return {"total": 0, "successful": 0, "failed": 0, "skipped": 0}

        if target_list is None:
            target_list = await self._fetch_pending_videos()

        stats = {"total": len(target_list), "successful": 0, "failed": 0, "skipped": 0}
        if not target_list:
            return stats

        # Group by channel for bulk yt-dlp invocations.
        by_channel: dict[str, list[str]] = {}
        for t in target_list:
            if self._stop.is_set():
                break
            if isinstance(t, dict):
                ch = t.get("channel_id") or ""
                vid = t.get("video_id") or ""
            else:
                # Bare string: try to detect "watch?v=" / "UC..." / 11-char id
                if "watch?v=" in t:
                    ch = ""
                    m = re.search(r"watch\?v=([\w-]{6,})", t)
                    vid = m.group(1) if m else ""
                elif t.startswith("UC") and len(t) >= 20:
                    ch = t
                    vid = ""
                else:
                    ch = ""
                    vid = t
            if vid:
                by_channel.setdefault(ch, []).append(vid)
            elif ch:
                by_channel.setdefault(ch, [])

        for ch, vids in by_channel.items():
            if self._stop.is_set():
                break
            try:
                if photos_only:
                    await self._collect_thumbnails_via_yt_dlp(ch or "unknown", ch or "unknown")
                else:
                    await self._download_videos_via_yt_dlp(ch or "unknown", ch or "unknown", vids)
                stats["successful"] += len(vids) or 1
            except Exception as e:
                logger.error("batch_download failure for channel %s: %s", ch, e)
                stats["failed"] += len(vids) or 1

        return stats

    async def _fetch_pending_videos(self, limit: int = 100) -> list[dict]:
        """Pull recently-collected video rows from DB to drive batch_download default mode."""
        if not self.pool:
            return []
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT v.platform_video_id, c.platform_channel_id, c.title
                FROM youtube_videos v
                LEFT JOIN youtube_channels c ON v.channel_id = c.id
                ORDER BY v.collected_at DESC
                LIMIT $1
                """,
                limit,
            )
        return [
            {
                "video_id": r["platform_video_id"],
                "channel_id": r["platform_channel_id"] or "",
                "channel_name": r["title"] or "",
            }
            for r in rows
        ]
