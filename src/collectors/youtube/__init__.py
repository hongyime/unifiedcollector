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
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx

from src.core.base_collector import BaseCollector
from src.collectors.youtube.parse import (
    vtt_to_text as _parse_vtt_to_text,
    parse_relative_timestamp as _parse_rel_ts,
)
from src.core.discovered_links import persist_discovered_links
from src.core.link_extractor import extract_all_links
from src.core.file_naming import sanitize_name
from src.core.vault import VAULT_ROOT, write_atomic_artifact, write_atomic_artifact_from_path
from src.core.subprocess_downloader import check_tool
from src.core.user_change_tracker import (
    UserChangeTracker,
    YOUTUBE_TRACKED_FIELDS,
)
from src.core.rate_limit_events import record_rate_limit_event

logger = logging.getLogger(__name__)

YT_API_BASE = "https://www.googleapis.com/youtube/v3"
LIKED_VIDEOS_PLAYLIST_ID = "LL"
_SECRET_QUERY_PARAM_RE = re.compile(
    r"([?&](?:key|api_key|access_token|token|client_secret|oauth_token)=)[^&\s'\"<>]+",
    re.IGNORECASE,
)
_YOUTUBE_HANDLE_RE = re.compile(r"(?<![\w@])@([A-Za-z0-9._-]{2,80})")
_YOUTUBE_CHANNEL_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:m\.)?youtube\.com/(?:channel/)?(UC[\w-]{20,})",
    re.IGNORECASE,
)
_YOUTUBE_HANDLE_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:m\.)?youtube\.com/@([A-Za-z0-9._-]{2,80})",
    re.IGNORECASE,
)
_YOUTUBE_PROGRESSIVE_FORMAT = (
    "best[ext=mp4][vcodec!=none][acodec!=none]/"
    "best[ext=webm][vcodec!=none][acodec!=none]/"
    "best[vcodec!=none][acodec!=none]/best"
)
_YOUTUBE_EXPECTED_YTDLP_STATES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\blive event will begin\b", re.IGNORECASE), "upcoming_live"),
    (re.compile(r"\bpremiere will begin\b", re.IGNORECASE), "upcoming_live"),
    (re.compile(r"\boffline\b", re.IGNORECASE), "live_offline"),
    (re.compile(r"\bprivate video\b", re.IGNORECASE), "unavailable"),
    (re.compile(r"\bvideo unavailable\b", re.IGNORECASE), "unavailable"),
    (re.compile(r"\bthis video is unavailable\b", re.IGNORECASE), "unavailable"),
    (re.compile(r"\bmembers-only\b", re.IGNORECASE), "restricted"),
    (re.compile(r"\bsign in to confirm your age\b", re.IGNORECASE), "restricted"),
)


def _safe_log_text(value) -> str:
    """Redact URL query secrets from exception text before logging/DLQ."""
    text = str(value).strip()
    if not text and isinstance(value, BaseException):
        text = type(value).__name__
    return _SECRET_QUERY_PARAM_RE.sub(r"\1<redacted>", text)


def _classify_ytdlp_media_failure(summary: str) -> tuple[str, str, int]:
    """Return media_status, log_level, retry_delay_hours for a yt-dlp failure."""
    text = summary or ""
    for pattern, status in _YOUTUBE_EXPECTED_YTDLP_STATES:
        if pattern.search(text):
            if status == "upcoming_live":
                return status, "info", 6
            if status == "live_offline":
                return status, "info", 24
            return status, "info", 168
    if re.search(r"\b(?:curl:\s*\(28\)|timed?\s*out|connection\s+timed\s+out)\b", text, re.IGNORECASE):
        return "transient_network", "warning", 2
    return "failed", "warning", 0


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
        self._usable_cookie_file_cache: str | None = None
        self._max_duration = int(os.getenv("YOUTUBE_MAX_VIDEO_DURATION_MINUTES", "0"))
        self._ytdlp_format = os.getenv("YOUTUBE_YTDLP_FORMAT", "auto")
        self._ytdlp_max_filesize = os.getenv("YOUTUBE_MAX_FILESIZE", "").strip()
        self._merge_format = os.getenv("YOUTUBE_MERGE_FORMAT", "mp4")
        self._download_delay = float(os.getenv("YOUTUBE_DOWNLOAD_DELAY", "5.0"))
        self._api_delay = float(os.getenv("YOUTUBE_API_DELAY", "3.0"))
        self._api_403_cooldown_seconds = int(os.getenv("YOUTUBE_API_403_COOLDOWN_SECONDS", "21600"))
        self._api_429_cooldown_seconds = int(os.getenv("YOUTUBE_API_429_COOLDOWN_SECONDS", "1800"))
        self._api_cooldown_until = 0.0
        self._api_cooldown_restored = False
        self._max_concurrent = int(os.getenv("YOUTUBE_MAX_CONCURRENT_DOWNLOADS", "3"))
        self._use_yt_dlp = self._check_yt_dlp()
        self._ffmpeg_available = check_tool("ffmpeg")
        self._ffprobe_available = check_tool("ffprobe")
        logger.info(
            "youtube tool availability: yt-dlp=%s ffmpeg=%s ffprobe=%s",
            self._use_yt_dlp,
            self._ffmpeg_available,
            self._ffprobe_available,
        )
        self._sem = asyncio.Semaphore(self._max_concurrent)
        self._download_sem = asyncio.Semaphore(self._max_concurrent)
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
        self._video_backfill_max_passes = max(1, int(os.getenv("YOUTUBE_VIDEO_BACKFILL_MAX_PASSES", "1")))
        self._video_backfill_failed_retry_hours = max(
            1,
            int(os.getenv("YOUTUBE_VIDEO_BACKFILL_FAILED_RETRY_HOURS", "24")),
        )
        self._video_backfill_scan_limit = int(os.getenv("YOUTUBE_VIDEO_BACKFILL_SCAN_LIMIT", "5000"))
        self._video_download_timeout = max(
            30,
            int(os.getenv("YOUTUBE_VIDEO_DOWNLOAD_TIMEOUT", "600")),
        )
        self._prefill_media_backlog = os.getenv("YOUTUBE_PREFILL_MEDIA_BACKLOG", "true").lower() == "true"
        self._profile_queue_enabled = os.getenv("YOUTUBE_PROFILE_QUEUE_ENABLED", "true").lower() == "true"
        self._spider_autotarget = os.getenv("YOUTUBE_SPIDER_AUTOTARGET", "true").lower() == "true"
        self._profile_queue_batch = int(os.getenv("YOUTUBE_PROFILE_QUEUE_BATCH", "20"))
        self._profile_queue_max_attempts = int(os.getenv("YOUTUBE_PROFILE_QUEUE_MAX_ATTEMPTS", "5"))
        self._mention_backfill_batch = int(os.getenv("YOUTUBE_MENTION_BACKFILL_BATCH", "200"))
        self._max_refs_per_record = int(os.getenv("YOUTUBE_MAX_REFS_PER_RECORD", "8"))
        self._max_comment_author_enqueues = int(os.getenv("YOUTUBE_MAX_COMMENT_AUTHOR_ENQUEUES", "25"))
        self._spider_queue_batch = int(os.getenv("YOUTUBE_SPIDER_QUEUE_BATCH", "5"))
        self._discovered_target_priority = int(os.getenv("YOUTUBE_DISCOVERED_TARGET_PRIORITY", "1"))
        self._skip_channel_fallback_after_api_empty: set[str] = set()
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
                result = await self._collect_channel(target) or {}
                status = "completed"
                reason = result.get("reason") or "collected"
                await self._mark_youtube_target(
                    target,
                    status=status,
                    reason=reason,
                    metadata={"youtube_result": result},
                )
                await self.checkpoint.save_progress(target)
            except Exception as e:
                safe_error = _safe_log_text(e)
                logger.error("Failed youtube/%s: %s", target, safe_error)
                await self._mark_youtube_target(
                    target,
                    status="error",
                    reason=safe_error[:1000],
                    metadata={"error_type": type(e).__name__},
                )
                await self._record_channel_error(target, safe_error)
                await self.send_to_dlq(target, target, safe_error)

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

        if self._profile_queue_enabled and not self._stop.is_set():
            try:
                await self._backfill_youtube_mentions(limit=self._mention_backfill_batch)
                await self._process_profile_queue(limit=self._profile_queue_batch)
            except Exception as e:
                logger.debug("youtube profile queue pass failed: %s", e)

        # Spider queue processing
        if os.getenv("YOUTUBE_SPIDER_ENABLED", "true").lower() == "true":
            await self._process_spider_queue(limit=self._spider_queue_batch)

    async def _process_spider_queue(self, limit: int | None = None):
        processed = 0
        max_items = max(1, int(limit if limit is not None else self._spider_queue_batch))
        while not self._stop.is_set() and processed < max_items:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow("""
                    UPDATE youtube_spider_queue
                    SET status = 'processing'
                    WHERE id = (
                        SELECT id FROM youtube_spider_queue
                        WHERE status = 'pending'
                        ORDER BY priority ASC, collected_at ASC
                        LIMIT 1
                        FOR UPDATE SKIP LOCKED
                    )
                    RETURNING platform_channel_id
                """)
            if not row: break
            processed += 1
            try:
                await self._collect_channel(row['platform_channel_id'])
                async with self.pool.acquire() as conn:
                    await conn.execute(
                        """
                        UPDATE youtube_spider_queue
                        SET status = 'completed', collected_at = NOW()
                        WHERE platform_channel_id = $1
                        """,
                        row['platform_channel_id'],
                    )
            except Exception as exc:
                async with self.pool.acquire() as conn:
                    await conn.execute(
                        """
                        UPDATE youtube_spider_queue
                        SET status = 'failed', collected_at = NOW()
                        WHERE platform_channel_id = $1
                        """,
                        row['platform_channel_id'],
                    )
                logger.debug("youtube spider queue failed for %s: %s", row["platform_channel_id"], exc)
        return processed

    async def _collect_channel(self, channel_input: str):
        api_cooldown_active = await self._youtube_api_cooldown_active("collect_channel", channel_input)
        if api_cooldown_active:
            channel_id, channel_name = channel_input, channel_input
        else:
            channel_id, channel_name = await self._resolve_channel(channel_input)
        if not channel_id:
            logger.warning("Could not resolve channel: %s", channel_input)
            return {"status": "error", "reason": "resolve_failed", "input": channel_input}

        # 1. Upsert Channel Info (returns uploads playlist ID + subscriber count)
        uploads_playlist, sub_count = await self._upsert_channel(
            channel_id,
            channel_name,
            allow_api=not api_cooldown_active,
        )

        # FAMOUS-FILTER (Bryan): skip channels at/above the subscriber cap, even if
        # subscribed. The channel row is still upserted above (so we know it + its
        # sub count) but we collect NO videos from it. Overrides the seed.
        if self._famous_sub_cap and sub_count >= self._famous_sub_cap:
            logger.info(
                "youtube: skipping famous channel %s (%s subs >= cap %d)",
                channel_name or channel_id, sub_count, self._famous_sub_cap,
            )
            await self._mark_channel_skip(channel_id, "subscriber_cap", {"subscriber_count": sub_count})
            return {
                "status": "completed",
                "reason": "subscriber_cap",
                "channel_id": channel_id,
                "subscriber_count": sub_count,
                "subscriber_cap": self._famous_sub_cap,
            }

        # When we have API auth and the channels.list lookup returned no item,
        # the channel is confirmed-missing — skip the expensive yt-dlp download
        # (running yt-dlp on a dead /channel/<id>/videos URL otherwise burns the
        # full download timeout per cycle for nothing).
        if self._has_auth and uploads_playlist is None and not api_cooldown_active:
            logger.info("youtube: skipping yt-dlp for confirmed-missing channel %s", channel_id)
            await self._mark_channel_skip(channel_id, "no_uploads_playlist", {"has_auth": True})
            return {"status": "completed", "reason": "no_uploads_playlist", "channel_id": channel_id}

        if self._has_auth and uploads_playlist and not api_cooldown_active:
            video_ids = await self._collect_video_list_via_api(channel_id, channel_name, uploads_playlist)
        else:
            video_ids = []

        if self._use_yt_dlp:
            if self._download_videos:
                download_video_ids = video_ids
                if self._video_downloads_per_target > 0 and len(download_video_ids) > self._video_downloads_per_target:
                    original_count = len(download_video_ids)
                    download_video_ids, skipped_duration, skipped_db = await self._select_live_download_video_ids(
                        download_video_ids,
                        self._video_downloads_per_target,
                    )
                    logger.info(
                        "youtube: limiting live video downloads for %s to %d/%d selected from %d this cycle "
                        "(skipped_archived=%d skipped_duration=%d)",
                        channel_id,
                        len(download_video_ids),
                        self._video_downloads_per_target,
                        original_count,
                        skipped_db,
                        skipped_duration,
                    )
                if download_video_ids or not video_ids:
                    if not download_video_ids and channel_id in self._skip_channel_fallback_after_api_empty:
                        logger.info(
                            "youtube: skipping yt-dlp channel fallback for %s; API uploads playlist returned no public videos",
                            channel_id,
                        )
                    else:
                        await self._download_videos_via_yt_dlp(channel_id, channel_name, download_video_ids)
                else:
                    logger.info("youtube: no live video download candidates remain for %s after archive/duration filtering", channel_id)
            else:
                await self._collect_thumbnails_via_yt_dlp(channel_id, channel_name)
        return {
            "status": "completed",
            "reason": "collected",
            "channel_id": channel_id,
            "video_ids": len(video_ids),
            "subscriber_count": sub_count,
        }

    def _yt_auth(self, params: dict | None = None) -> tuple[dict, dict]:
        """Return (headers, params) populated with whichever auth is available."""
        params = dict(params or {})
        headers: dict[str, str] = {}
        if self._oauth_credentials:
            headers["Authorization"] = f"Bearer {self._oauth_credentials}"
        elif self._api_key:
            params["key"] = self._api_key
        return headers, params

    def _youtube_quota_account(self) -> str:
        if self._api_key:
            return f"api_key:{sanitize_name(self._api_key[:8])}"
        if self._oauth_credentials:
            return "oauth"
        return "anonymous"

    def _youtube_api_cooldown_remaining(self) -> float:
        return max(0.0, self._api_cooldown_until - time.time())

    async def _restore_youtube_api_cooldown(self) -> None:
        if self._api_cooldown_restored or self.pool is None:
            return
        self._api_cooldown_restored = True
        account = self._youtube_quota_account()
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT MAX(created_at + (COALESCE(cooldown_seconds, 0) * INTERVAL '1 second')) AS cooldown_until
                    FROM rate_limit_events
                    WHERE source = 'youtube'
                      AND account = $1
                      AND status_code IN (403, 429)
                      AND COALESCE(cooldown_seconds, 0) > 0
                      AND created_at + (COALESCE(cooldown_seconds, 0) * INTERVAL '1 second') > NOW()
                    """,
                    account,
                )
        except Exception:
            logger.debug("youtube api cooldown restore failed", exc_info=True)
            return
        cooldown_until = row["cooldown_until"] if row else None
        if not cooldown_until:
            return
        if cooldown_until.tzinfo is None:
            cooldown_until = cooldown_until.replace(tzinfo=timezone.utc)
        remaining = (cooldown_until - datetime.now(timezone.utc)).total_seconds()
        if remaining > 0:
            self._api_cooldown_until = max(self._api_cooldown_until, time.time() + remaining)
            logger.info("youtube: restored Data API cooldown for %ds on %s", int(remaining), account)

    async def _youtube_api_cooldown_active(self, scope: str, target: str | None = None) -> bool:
        await self._restore_youtube_api_cooldown()
        remaining = self._youtube_api_cooldown_remaining()
        if remaining <= 0:
            return False
        detail = f" for {target}" if target else ""
        logger.info(
            "youtube: skipping Data API %s%s while %s is cooling down for %ds",
            scope,
            detail,
            self._youtube_quota_account(),
            int(remaining),
        )
        return True

    def _set_youtube_api_cooldown(self, seconds: int | None) -> None:
        if not seconds or seconds <= 0:
            return
        self._api_cooldown_until = max(self._api_cooldown_until, time.time() + int(seconds))

    async def _record_api_request(
        self,
        endpoint: str,
        *,
        status_code: int | None = None,
        weight: int = 1,
        metadata: dict | None = None,
    ) -> None:
        if self.pool is None:
            return
        account = self._youtube_quota_account()
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO account_quota_usage
                        (platform, account, day, requests_today, week_iso,
                         requests_week, hour_bucket, requests_hour)
                    VALUES (
                        'youtube',
                        $1,
                        (NOW() AT TIME ZONE 'Asia/Singapore')::date,
                        $2,
                        to_char((NOW() AT TIME ZONE 'Asia/Singapore')::date, 'IYYY-"W"IW'),
                        $2,
                        to_char(NOW() AT TIME ZONE 'Asia/Singapore', 'YYYY-MM-DD HH24:00'),
                        $2
                    )
                    ON CONFLICT (platform, account, day) DO UPDATE SET
                        requests_today = account_quota_usage.requests_today + EXCLUDED.requests_today,
                        week_iso = EXCLUDED.week_iso,
                        requests_week = account_quota_usage.requests_week + EXCLUDED.requests_today,
                        requests_hour = CASE
                            WHEN account_quota_usage.hour_bucket = EXCLUDED.hour_bucket
                                THEN account_quota_usage.requests_hour + EXCLUDED.requests_hour
                            ELSE EXCLUDED.requests_hour
                        END,
                        hour_bucket = EXCLUDED.hour_bucket,
                        updated_at = NOW()
                    """,
                    account,
                    int(max(weight, 1)),
                )
        except Exception:
            logger.debug("youtube quota usage update failed", exc_info=True)

        if status_code in (403, 429):
            cooldown_seconds = (
                self._api_403_cooldown_seconds if status_code == 403
                else self._api_429_cooldown_seconds
            )
            self._set_youtube_api_cooldown(cooldown_seconds)
            await record_rate_limit_event(
                self.pool,
                source="youtube",
                account=account,
                scope=endpoint,
                status_code=status_code,
                cooldown_seconds=cooldown_seconds,
                reason="youtube_api_quota_or_access",
                metadata=metadata or {},
            )

    async def _mark_youtube_target(self, target: str, *, status: str, reason: str | None = None,
                                   metadata: dict | None = None) -> None:
        if self.pool is None:
            return
        payload = json.dumps(metadata or {}, default=str)
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE collection_targets
                    SET collection_count = collection_count + 1,
                        last_collection_at = NOW(),
                        status = $3,
                        error_message = $4,
                        metadata = COALESCE(metadata, '{}'::jsonb)
                                   || jsonb_build_object(
                                        'youtube_last_status', $3::text,
                                        'youtube_last_reason', $4::text,
                                        'youtube_last_result_at', NOW()
                                      )
                                   || $5::jsonb
                    WHERE source = 'youtube' AND target_id = $1
                    """,
                    target,
                    self.SOURCE_NAME,
                    status,
                    reason,
                    payload,
                )
        except Exception:
            logger.debug("youtube target status update failed for %s", target, exc_info=True)

    @staticmethod
    def _normalize_handle(handle: str | None) -> str | None:
        if not handle:
            return None
        value = str(handle).strip().lstrip("@").strip("/")
        if not value or len(value) > 100:
            return None
        if not re.match(r"^[A-Za-z0-9._-]+$", value):
            return None
        return value

    @staticmethod
    def _extract_youtube_refs(text: str | None) -> list[dict]:
        if not text:
            return []
        refs: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for m in _YOUTUBE_CHANNEL_URL_RE.finditer(text):
            cid = m.group(1)
            key = ("channel", cid)
            if key not in seen:
                seen.add(key)
                refs.append({"key_type": "channel", "platform_channel_id": cid, "profile_key": cid})
        for m in _YOUTUBE_HANDLE_URL_RE.finditer(text):
            handle = YoutubeCollector._normalize_handle(m.group(1))
            if not handle:
                continue
            key = ("handle", handle.lower())
            if key not in seen:
                seen.add(key)
                refs.append({"key_type": "handle", "handle": handle, "profile_key": f"@{handle.lower()}"})
        for m in _YOUTUBE_HANDLE_RE.finditer(text):
            handle = YoutubeCollector._normalize_handle(m.group(1))
            if not handle:
                continue
            key = ("handle", handle.lower())
            if key not in seen:
                seen.add(key)
                refs.append({"key_type": "handle", "handle": handle, "profile_key": f"@{handle.lower()}"})
        return refs

    async def _queue_youtube_profile(
        self,
        conn,
        ref: dict,
        *,
        source: str,
        priority: int,
        discovered_from: str,
        metadata: dict | None = None,
    ) -> None:
        if not self._profile_queue_enabled:
            return
        profile_key = ref.get("profile_key")
        if not profile_key:
            return
        key_type = ref.get("key_type") or ("channel" if ref.get("platform_channel_id") else "handle")
        channel_id = ref.get("platform_channel_id")
        handle = ref.get("handle")
        meta = json.dumps(metadata or {}, default=str)
        await conn.execute(
            """
            INSERT INTO youtube_profile_queue
                (profile_key, key_type, platform_channel_id, handle, source,
                 priority, status, evidence_count, discovered_from, metadata)
            VALUES ($1,$2,$3,$4,$5,$6,'pending',1,$7,$8::jsonb)
            ON CONFLICT (profile_key) DO UPDATE SET
                platform_channel_id = COALESCE(youtube_profile_queue.platform_channel_id, EXCLUDED.platform_channel_id),
                handle = COALESCE(youtube_profile_queue.handle, EXCLUDED.handle),
                priority = LEAST(youtube_profile_queue.priority, EXCLUDED.priority),
                evidence_count = youtube_profile_queue.evidence_count + 1,
                status = CASE
                    WHEN youtube_profile_queue.status IN ('failed', 'resolved') THEN 'pending'
                    ELSE youtube_profile_queue.status
                END,
                last_seen = NOW(),
                metadata = COALESCE(youtube_profile_queue.metadata, '{}'::jsonb) || EXCLUDED.metadata
            """,
            profile_key,
            key_type,
            channel_id,
            handle,
            source,
            priority,
            discovered_from,
            meta,
        )
        if self._spider_autotarget and channel_id:
            await self._enqueue_discovered_channel(
                conn,
                channel_id,
                source=source,
                priority=priority,
                discovered_from=discovered_from,
                metadata=metadata,
            )

    async def _enqueue_discovered_channel(
        self,
        conn,
        channel_id: str,
        *,
        source: str,
        priority: int,
        discovered_from: str,
        metadata: dict | None = None,
    ) -> None:
        if not channel_id or not channel_id.startswith("UC"):
            return
        meta = json.dumps(
            {
                "source": source,
                "discovered_from": discovered_from,
                "auto_discovered": True,
                "preserve_on_source_config_sync": True,
                **(metadata or {}),
            },
            default=str,
        )
        await conn.execute(
            """
            INSERT INTO collection_targets (source, target_id, target_type, status, priority, metadata)
            VALUES ('youtube', $1, 'user', 'pending', $2, $3::jsonb)
            ON CONFLICT (source, target_id) DO UPDATE SET
                priority = GREATEST(collection_targets.priority, EXCLUDED.priority),
                metadata = COALESCE(collection_targets.metadata, '{}'::jsonb) || EXCLUDED.metadata
            """,
            channel_id,
            priority,
            meta,
        )
        await conn.execute(
            """
            INSERT INTO youtube_spider_queue (platform_channel_id, source, priority, status)
            VALUES ($1, $2, $3, 'pending')
            ON CONFLICT (platform_channel_id) DO UPDATE SET
                priority = LEAST(youtube_spider_queue.priority, EXCLUDED.priority),
                status = CASE
                    WHEN youtube_spider_queue.status = 'failed'
                         AND youtube_spider_queue.collected_at < NOW() - make_interval(hours => $4::int)
                        THEN 'pending'
                    WHEN youtube_spider_queue.status = 'completed'
                         AND youtube_spider_queue.collected_at < NOW() - make_interval(hours => $5::int)
                        THEN 'pending'
                    ELSE youtube_spider_queue.status
                END,
                collected_at = NOW()
            """,
            channel_id,
            source,
            priority,
            int(os.getenv("YOUTUBE_SPIDER_RETRY_FAILED_AFTER_HOURS", "24")),
            int(os.getenv("YOUTUBE_SPIDER_REFRESH_AFTER_HOURS", "168")),
        )

    async def _record_youtube_edge(
        self,
        conn,
        *,
        edge_type: str,
        source_record_id: str,
        source_table: str,
        source_channel_id: str | None = None,
        target_channel_id: str | None = None,
        target_handle: str | None = None,
        source_video_id: str | None = None,
        source_comment_id: str | None = None,
        source_post_id: str | None = None,
        strength: int = 50,
        evidence_text: str | None = None,
        evidence_url: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        await conn.execute(
            """
            INSERT INTO youtube_edges (
                source_channel_id, target_channel_id, target_handle,
                source_video_id, source_comment_id, source_post_id, edge_type,
                strength, evidence_text, evidence_url, source_table,
                source_record_id, metadata
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::jsonb)
            ON CONFLICT DO NOTHING
            """,
            source_channel_id,
            target_channel_id,
            target_handle,
            source_video_id,
            source_comment_id,
            source_post_id,
            edge_type,
            int(strength),
            evidence_text,
            evidence_url,
            source_table,
            source_record_id,
            json.dumps(metadata or {}, default=str),
        )

    async def _record_refs_from_text(
        self,
        conn,
        text: str | None,
        *,
        source_table: str,
        source_record_id: str,
        source_channel_id: str | None = None,
        source_video_id: str | None = None,
        source_comment_id: str | None = None,
        source_post_id: str | None = None,
        evidence_url: str | None = None,
        priority: int | None = None,
    ) -> int:
        refs = self._extract_youtube_refs(text)
        if not refs:
            return 0
        if self._max_refs_per_record > 0 and len(refs) > self._max_refs_per_record:
            refs = refs[: self._max_refs_per_record]
        written = 0
        for ref in refs:
            target_channel_id = ref.get("platform_channel_id")
            target_handle = ref.get("handle")
            await self._queue_youtube_profile(
                conn,
                ref,
                source="mention",
                priority=priority if priority is not None else self._discovered_target_priority,
                discovered_from=f"{source_table}:{source_record_id}",
                metadata={
                    "source_table": source_table,
                    "source_record_id": source_record_id,
                    "source_channel_id": source_channel_id,
                    "source_video_id": source_video_id,
                },
            )
            await self._record_youtube_edge(
                conn,
                edge_type="mentioned",
                source_table=source_table,
                source_record_id=source_record_id,
                source_channel_id=source_channel_id,
                target_channel_id=target_channel_id,
                target_handle=target_handle,
                source_video_id=source_video_id,
                source_comment_id=source_comment_id,
                source_post_id=source_post_id,
                strength=55,
                evidence_text=(text or "")[:1000],
                evidence_url=evidence_url,
                metadata=ref,
            )
            written += 1
        return written

    async def _process_profile_queue(self, limit: int = 20) -> int:
        """Resolve discovered YouTube handles/channels and enqueue real UC IDs.

        Mentions often arrive as @handles from comments/descriptions. We keep
        them in youtube_profile_queue first, then resolve a bounded batch to
        channel IDs before spidering. This keeps discovery auditable and avoids
        turning every bare handle into a blind channel scrape.
        """
        if not self.pool or not self._profile_queue_enabled or limit <= 0:
            return 0
        await self._ensure_auth()
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                UPDATE youtube_profile_queue
                SET status = 'processing',
                    attempts = attempts + 1,
                    last_attempt_at = NOW()
                WHERE profile_key IN (
                    SELECT profile_key
                    FROM youtube_profile_queue
                    WHERE status = 'pending'
                      AND (next_attempt_at IS NULL OR next_attempt_at <= NOW())
                      AND attempts < $1
                    ORDER BY priority ASC, evidence_count DESC, last_seen DESC
                    LIMIT $2
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING profile_key, key_type, platform_channel_id, handle,
                          source, priority, evidence_count, discovered_from,
                          attempts, metadata
                """,
                self._profile_queue_max_attempts,
                limit,
            )
        resolved = 0
        for raw in rows:
            if self._stop.is_set():
                break
            row = dict(raw)
            profile_key = str(row.get("profile_key") or "")
            channel_id = str(row.get("platform_channel_id") or "")
            handle = self._normalize_handle(row.get("handle") or profile_key)
            resolved_id = channel_id if channel_id.startswith("UC") else None
            resolved_name = resolved_id or handle or profile_key

            if resolved_id is None and handle and await self._ensure_auth():
                candidate, name = await self._resolve_channel(f"@{handle}")
                if candidate and candidate.startswith("UC"):
                    resolved_id = candidate
                    resolved_name = name or candidate

            try:
                async with self.pool.acquire() as conn:
                    if resolved_id:
                        await conn.execute(
                            """
                            UPDATE youtube_profile_queue
                            SET status = 'resolved',
                                platform_channel_id = $2,
                                handle = COALESCE(handle, $3),
                                resolved_at = NOW(),
                                last_error = NULL,
                                next_attempt_at = NULL,
                                last_seen = NOW()
                            WHERE profile_key = $1
                            """,
                            profile_key,
                            resolved_id,
                            handle,
                        )
                        await self._enqueue_discovered_channel(
                            conn,
                            resolved_id,
                            source=row.get("source") or "profile_queue",
                            priority=int(row.get("priority") or self._discovered_target_priority),
                            discovered_from=row.get("discovered_from") or f"profile_queue:{profile_key}",
                            metadata={
                                "profile_key": profile_key,
                                "handle": handle,
                                "resolved_name": resolved_name,
                                "evidence_count": int(row.get("evidence_count") or 0),
                            },
                        )
                        resolved += 1
                    else:
                        attempts = int(row.get("attempts") or 0)
                        terminal = attempts >= self._profile_queue_max_attempts
                        await conn.execute(
                            """
                            UPDATE youtube_profile_queue
                            SET status = CASE WHEN $2 THEN 'failed' ELSE 'pending' END,
                                next_attempt_at = CASE
                                    WHEN $2 THEN NULL
                                    ELSE NOW() + make_interval(mins => $3::int)
                                END,
                                last_error = $4,
                                last_seen = NOW()
                            WHERE profile_key = $1
                            """,
                            profile_key,
                            terminal,
                            min(1440, max(15, attempts * 15)),
                            "unresolved_handle_or_channel",
                        )
            except Exception as exc:
                logger.debug("youtube profile queue update failed for %s: %s", profile_key, exc, exc_info=True)
        if rows:
            logger.info("youtube profile queue: resolved %d/%d discovered profile(s)", resolved, len(rows))
        return resolved

    async def _backfill_youtube_mentions(self, limit: int = 200) -> int:
        """Bounded historical pass for @handle/channel mentions in old rows."""
        if not self.pool or limit <= 0:
            return 0
        processed = 0
        processed += await self._backfill_video_mentions(limit=max(1, limit // 2))
        if processed < limit:
            processed += await self._backfill_comment_mentions(limit=limit - processed)
        return processed

    async def _backfill_video_mentions(self, limit: int) -> int:
        service = "youtube_mentions_videos"
        async with self.pool.acquire() as conn:
            cursor = await conn.fetchrow(
                "SELECT last_processed_at FROM service_cursors WHERE service=$1",
                service,
            )
            since = cursor["last_processed_at"] if cursor else None
            rows = await conn.fetch(
                """
                SELECT v.platform_video_id,
                       COALESCE(c.platform_channel_id, '') AS source_channel_id,
                       concat_ws(' ', v.title, v.description) AS text,
                       v.collected_at
                FROM youtube_videos v
                LEFT JOIN youtube_channels c ON c.id = v.channel_id
                WHERE ($1::timestamp IS NULL OR v.collected_at > $1::timestamp)
                  AND (v.title ILIKE '%@%' OR v.description ILIKE '%@%'
                       OR v.title ILIKE '%youtube.com/%' OR v.description ILIKE '%youtube.com/%')
                ORDER BY v.collected_at ASC, v.platform_video_id ASC
                LIMIT $2
                """,
                since,
                limit,
            )
            newest = None
            for row in rows:
                newest = row["collected_at"] or newest
                await self._record_refs_from_text(
                    conn,
                    row["text"],
                    source_table="youtube_videos",
                    source_record_id=row["platform_video_id"],
                    source_channel_id=row["source_channel_id"] or None,
                    source_video_id=row["platform_video_id"],
                    evidence_url=f"https://www.youtube.com/watch?v={row['platform_video_id']}",
                    priority=self._discovered_target_priority,
                )
            if newest:
                await conn.execute(
                    """
                    INSERT INTO service_cursors (service, last_processed_id, last_processed_at, status)
                    VALUES ($1, $2, $3, 'idle')
                    ON CONFLICT (service) DO UPDATE SET
                        last_processed_id = EXCLUDED.last_processed_id,
                        last_processed_at = EXCLUDED.last_processed_at,
                        status = 'idle'
                    """,
                    service,
                    str(rows[-1]["platform_video_id"]),
                    newest,
                )
        return len(rows)

    async def _backfill_comment_mentions(self, limit: int) -> int:
        service = "youtube_mentions_comments"
        async with self.pool.acquire() as conn:
            cursor = await conn.fetchrow(
                "SELECT last_processed_at FROM service_cursors WHERE service=$1",
                service,
            )
            since = cursor["last_processed_at"] if cursor else None
            rows = await conn.fetch(
                """
                SELECT yc.platform_comment_id,
                       yc.author_channel_id,
                       yc.text_original,
                       yc.collected_at,
                       v.platform_video_id,
                       COALESCE(ch.platform_channel_id, '') AS owner_channel_id
                FROM youtube_comments yc
                LEFT JOIN youtube_videos v ON v.id = yc.video_id
                LEFT JOIN youtube_channels ch ON ch.id = v.channel_id
                WHERE ($1::timestamp IS NULL OR yc.collected_at > $1::timestamp)
                  AND (yc.text_original ILIKE '%@%' OR yc.text_original ILIKE '%youtube.com/%')
                ORDER BY yc.collected_at ASC, yc.platform_comment_id ASC
                LIMIT $2
                """,
                since,
                limit,
            )
            newest = None
            for row in rows:
                newest = row["collected_at"] or newest
                source_channel = row["author_channel_id"] or row["owner_channel_id"] or None
                await self._record_refs_from_text(
                    conn,
                    row["text_original"],
                    source_table="youtube_comments",
                    source_record_id=row["platform_comment_id"],
                    source_channel_id=source_channel,
                    source_video_id=row["platform_video_id"],
                    source_comment_id=row["platform_comment_id"],
                    evidence_url=f"https://www.youtube.com/watch?v={row['platform_video_id']}" if row["platform_video_id"] else None,
                    priority=self._discovered_target_priority,
                )
            if newest:
                await conn.execute(
                    """
                    INSERT INTO service_cursors (service, last_processed_id, last_processed_at, status)
                    VALUES ($1, $2, $3, 'idle')
                    ON CONFLICT (service) DO UPDATE SET
                        last_processed_id = EXCLUDED.last_processed_id,
                        last_processed_at = EXCLUDED.last_processed_at,
                        status = 'idle'
                    """,
                    service,
                    str(rows[-1]["platform_comment_id"]),
                    newest,
                )
        return len(rows)

    async def _record_channel_error(self, channel_id: str, error: str) -> None:
        if not self.pool:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE youtube_channels
                    SET last_error = $2, last_error_at = NOW()
                    WHERE platform_channel_id = $1
                    """,
                    channel_id,
                    error[:1000],
                )
        except Exception:
            logger.debug("youtube channel error update failed for %s", channel_id, exc_info=True)

    async def _mark_channel_skip(self, channel_id: str, reason: str, metadata: dict | None = None) -> None:
        if not self.pool:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE youtube_channels
                    SET last_skip_reason = $2,
                        last_skip_at = NOW()
                    WHERE platform_channel_id = $1
                    """,
                    channel_id,
                    reason,
                )
        except Exception:
            logger.debug("youtube channel skip update failed for %s", channel_id, exc_info=True)

    async def _mark_video_media_attempt(self, platform_video_id: str, *, status: str,
                                        reason: str | None = None) -> None:
        if not self.pool or not platform_video_id:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE youtube_videos
                    SET media_status=$2,
                        media_skip_reason=$3,
                        last_media_attempt_at=NOW()
                    WHERE platform_video_id=$1
                    """,
                    platform_video_id,
                    status,
                    reason[:1000] if reason else None,
                )
        except Exception:
            logger.debug("youtube video media status update failed for %s", platform_video_id, exc_info=True)

    async def _mark_transcript_attempt(self, video_uuid, *, status: str,
                                       error: str | None = None) -> None:
        if not self.pool:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE youtube_videos
                    SET transcript_status=$2,
                        transcript_error=$3,
                        last_transcript_attempt_at=NOW()
                    WHERE id=$1
                    """,
                    video_uuid,
                    status,
                    error[:1000] if error else None,
                )
        except Exception:
            logger.debug("youtube transcript status update failed", exc_info=True)

    async def _mark_comments_attempt(self, video_uuid, *, status: str,
                                     error: str | None = None) -> None:
        if not self.pool:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE youtube_videos
                    SET comments_status=$2,
                        comments_error=$3,
                        last_comments_attempt_at=NOW()
                    WHERE id=$1
                    """,
                    video_uuid,
                    status,
                    error[:1000] if error else None,
                )
        except Exception:
            logger.debug("youtube comments status update failed", exc_info=True)

    async def _upsert_channel(
        self,
        channel_id: str,
        channel_name: str,
        *,
        allow_api: bool = True,
    ) -> tuple[str | None, int]:
        """Upsert channel row. Returns (uploads playlist ID or None, subscriber_count)."""
        snippet = {}
        statistics = {}
        uploads_playlist = None
        if self._has_auth and allow_api and not await self._youtube_api_cooldown_active("channels.list", channel_id):
            try:
                headers, params = self._yt_auth({"part": "snippet,statistics,contentDetails", "id": channel_id})
                async with httpx.AsyncClient(timeout=30, headers=headers) as client:
                    resp = await client.get(f"{YT_API_BASE}/channels", params=params)
                    await self._record_api_request(
                        "channels.list",
                        status_code=resp.status_code,
                        metadata={"channel_id": channel_id, "part": "snippet,statistics,contentDetails"},
                    )
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
                logger.warning("YouTube _upsert_channel meta fetch failed for %s: %s", channel_id, _safe_log_text(e))

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

        thumbnail_url = snippet.get("thumbnails", {}).get("high", {}).get("url")
        try:
            external_links = [
                {"url": url, "type": link_type, "domain": urlparse(url).netloc.lower() or None}
                for url, link_type in extract_all_links(snippet.get("description") or "")
            ]
        except Exception:
            external_links = []

        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO youtube_channels (
                    platform_channel_id, title, description, custom_url,
                    published_at, thumbnail_url, view_count, subscriber_count,
                    video_count, external_links, updated_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, NOW())
                ON CONFLICT (platform_channel_id) DO UPDATE SET
                    title = EXCLUDED.title,
                    description = EXCLUDED.description,
                    custom_url = EXCLUDED.custom_url,
                    thumbnail_url = EXCLUDED.thumbnail_url,
                    view_count = EXCLUDED.view_count,
                    subscriber_count = EXCLUDED.subscriber_count,
                    video_count = EXCLUDED.video_count,
                    external_links = EXCLUDED.external_links,
                    last_error = NULL,
                    last_error_at = NULL,
                    updated_at = NOW()
            """,
            channel_id, channel_name, snippet.get("description"), snippet.get("customUrl"),
            datetime.fromisoformat(snippet.get("publishedAt").replace("Z", "")) if snippet.get("publishedAt") else None,
            thumbnail_url,
            int(statistics.get("viewCount", 0) or 0),
            int(statistics.get("subscriberCount", 0) or 0),
            int(statistics.get("videoCount", 0) or 0),
            json.dumps(external_links, default=str),
            )
            await persist_discovered_links(
                conn,
                source="youtube",
                source_table="youtube_channels",
                source_record_id=channel_id,
                context_id=channel_id,
                entity_id=channel_id,
                text=snippet.get("description"),
                metadata={"platform_channel_id": channel_id, "title": channel_name},
            )
            await self._record_refs_from_text(
                conn,
                snippet.get("description"),
                source_table="youtube_channels",
                source_record_id=channel_id,
                source_channel_id=channel_id,
                evidence_url=f"https://www.youtube.com/channel/{channel_id}",
                priority=max(self._discovered_target_priority, 2),
            )

        if thumbnail_url and not self.is_known(f"profile_{channel_id}"):
            try:
                inserted = await self.download_media({
                    "entity_id": channel_id,
                    "entity_name": channel_name or channel_id,
                    "content_type": "profile_photo",
                    "content_id": f"profile_{channel_id}",
                    "url": thumbnail_url,
                    "extension": "jpg",
                    "source_url": f"https://www.youtube.com/channel/{channel_id}",
                    "raw": {"platform_channel_id": channel_id, "thumbnail_url": thumbnail_url},
                })
                if inserted:
                    async with self.pool.acquire() as conn:
                        media_id = await conn.fetchval(
                            "SELECT id FROM media_items WHERE source='youtube' AND content_id=$1 ORDER BY collected_at DESC LIMIT 1",
                            f"profile_{channel_id}",
                        )
                        if media_id:
                            await conn.execute(
                                "UPDATE youtube_channels SET profile_photo_media_id=$2 WHERE platform_channel_id=$1",
                                channel_id,
                                media_id,
                            )
            except Exception:
                logger.debug("youtube profile photo archive failed for %s", channel_id, exc_info=True)

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
                          (platform_post_id, channel_id, text, likes_count, comments_count,
                           has_image, image_url, collected_at, raw)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,NOW(),$8::jsonb)
                        ON CONFLICT (platform_post_id) DO UPDATE SET
                          text=EXCLUDED.text, likes_count=EXCLUDED.likes_count,
                          comments_count=EXCLUDED.comments_count,
                          has_image=EXCLUDED.has_image, image_url=EXCLUDED.image_url,
                          raw=EXCLUDED.raw
                        """,
                        pid, channel_id, text or None, votes, comments, bool(image_url), image_url, pj,
                    )
                    await self._record_refs_from_text(
                        conn,
                        text,
                        source_table="youtube_community_posts",
                        source_record_id=pid,
                        source_channel_id=channel_id,
                        source_post_id=pid,
                        evidence_url=f"https://www.youtube.com/channel/{channel_id}/community",
                    )
                    saved += 1
                except Exception:
                    logger.debug("community upsert failed %s", pid, exc_info=True)
                if image_url and not self.is_known(f"community_{pid}"):
                    try:
                        inserted = await self.download_media({
                            "entity_id": channel_id,
                            "entity_name": channel_id,
                            "content_type": "community_image",
                            "content_id": f"community_{pid}",
                            "url": image_url,
                            "extension": "jpg",
                            "source_url": f"https://www.youtube.com/channel/{channel_id}/community",
                            "raw": {"platform_post_id": pid, "image_url": image_url},
                        })
                        if inserted:
                            media_row = await conn.fetchrow(
                                "SELECT id FROM media_items WHERE source='youtube' AND content_id=$1 ORDER BY collected_at DESC LIMIT 1",
                                f"community_{pid}",
                            )
                            await conn.execute(
                                """
                                UPDATE youtube_community_posts
                                SET media_status='stored',
                                    media_item_id=$2,
                                    last_media_attempt_at=NOW(),
                                    media_error=NULL
                                WHERE platform_post_id=$1
                                """,
                                pid,
                                media_row["id"] if media_row else None,
                            )
                    except Exception as exc:
                        await conn.execute(
                            """
                            UPDATE youtube_community_posts
                            SET media_status='failed',
                                media_error=$2,
                                last_media_attempt_at=NOW()
                            WHERE platform_post_id=$1
                            """,
                            pid,
                            _safe_log_text(exc)[:1000],
                        )
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
            cid = r["platform_channel_id"]
            total += await self._collect_community_posts(cid)
            try:
                async with self.pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE youtube_channels SET last_community_scan_at=NOW() WHERE platform_channel_id=$1",
                        cid,
                    )
            except Exception:
                logger.debug("youtube: community scan timestamp update failed", exc_info=True)
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
            await persist_discovered_links(
                conn,
                source="youtube",
                source_table="youtube_videos",
                source_record_id=video_id,
                context_id=channel_id,
                entity_id=channel_id,
                text=snippet.get("description"),
                metadata={
                    "platform_video_id": video_id,
                    "platform_channel_id": channel_id,
                    "title": snippet.get("title"),
                },
            )
            await self._record_refs_from_text(
                conn,
                " ".join(v for v in (snippet.get("title"), snippet.get("description")) if v),
                source_table="youtube_videos",
                source_record_id=video_id,
                source_channel_id=channel_id,
                source_video_id=video_id,
                evidence_url=f"https://www.youtube.com/watch?v={video_id}",
                priority=self._discovered_target_priority,
            )

    async def _resolve_channel(self, channel_input: str) -> tuple[str, str]:
        if not self._has_auth:
            return channel_input, channel_input
        if await self._youtube_api_cooldown_active("resolve", channel_input):
            return channel_input, channel_input

        async with httpx.AsyncClient(timeout=30) as client:
            if channel_input.startswith("UC"):
                headers, params = self._yt_auth({"part": "snippet", "id": channel_input})
                resp = await client.get(f"{YT_API_BASE}/channels", params=params, headers=headers)
                await self._record_api_request("channels.list", status_code=resp.status_code, metadata={"resolve": channel_input})
            elif channel_input.startswith("@"):
                headers, params = self._yt_auth({"part": "snippet", "forHandle": channel_input})
                resp = await client.get(f"{YT_API_BASE}/channels", params=params, headers=headers)
                await self._record_api_request("channels.list", status_code=resp.status_code, metadata={"resolve": channel_input})
            else:
                headers, params = self._yt_auth({"part": "snippet", "q": channel_input, "type": "channel", "maxResults": 1})
                resp = await client.get(f"{YT_API_BASE}/search", params=params, headers=headers)
                await self._record_api_request("search.list", status_code=resp.status_code, metadata={"resolve": channel_input})

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
        if await self._youtube_api_cooldown_active("playlistItems.list", channel_id):
            return []
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
                await self._record_api_request(
                    "playlistItems.list",
                    status_code=resp.status_code,
                    metadata={"channel_id": channel_id, "playlist_id": uploads_playlist},
                )
                if resp.status_code == 403:
                    logger.warning("YouTube playlistItems 403 (quota or permission) for channel %s", channel_id)
                    break
                if resp.status_code == 404:
                    logger.warning("YouTube uploads playlist 404 for channel %s (%s)", channel_id, uploads_playlist)
                    await self._mark_channel_skip(channel_id, "uploads_playlist_404", {"playlist_id": uploads_playlist})
                    self._skip_channel_fallback_after_api_empty.add(channel_id)
                    break
                resp.raise_for_status()
                data = resp.json()
                items = data.get("items", [])
                if not items:
                    logger.info("YouTube uploads playlist %s returned 0 items (channel may have no public videos)", uploads_playlist)
                    self._skip_channel_fallback_after_api_empty.add(channel_id)
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
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    "UPDATE youtube_channels SET last_video_scan_at=NOW() WHERE platform_channel_id=$1",
                    channel_id,
                )
        except Exception:
            logger.debug("youtube: video scan timestamp update failed", exc_info=True)
        if video_ids:
            try:
                await self._enrich_video_stats(video_ids)
            except Exception as e:
                logger.error("YouTube batch enrichment failed for channel %s: %s", channel_id, _safe_log_text(e))
        return video_ids

    async def _enrich_video_stats(self, video_ids: list[str]):
        """Batch-fetch statistics+contentDetails via videos.list (50 IDs/req) and UPDATE youtube_videos."""
        if await self._youtube_api_cooldown_active("videos.list"):
            return
        async with httpx.AsyncClient(timeout=30) as client:
            for i in range(0, len(video_ids), 50):
                if self._stop.is_set(): break
                chunk = video_ids[i:i + 50]
                await asyncio.sleep(self._api_delay)
                try:
                    headers, params = self._yt_auth({"part": "statistics,contentDetails", "id": ",".join(chunk)})
                    async with self._sem:
                        resp = await client.get(f"{YT_API_BASE}/videos", params=params, headers=headers)
                    await self._record_api_request(
                        "videos.list",
                        status_code=resp.status_code,
                        metadata={"ids": len(chunk), "first_id": chunk[0] if chunk else None},
                    )
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
                    logger.error(
                        "YouTube videos.list batch failed (chunk starting %s): %s",
                        chunk[0] if chunk else "?",
                        _safe_log_text(e),
                    )

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
        candidates, skipped_db = await self._filter_video_ids_already_archived(candidates)
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
            "youtube yt-dlp starting video downloads for %s: urls=%d candidates=%d skipped_duration=%d skipped_db=%d",
            channel_id,
            len(urls),
            len(candidates),
            skipped_duration,
            skipped_db,
        )
        async def _download_one(url: str) -> int:
            if self._stop.is_set():
                return 0
            async with self._download_sem:
                if self._stop.is_set():
                    return 0
                await asyncio.sleep(self._download_delay)
                async with managed_tempdir("yt_") as tmpdir:
                    logger.info("youtube yt-dlp starting %s", url)
                    extra = self._yt_dlp_extra_args()
                    if self._max_duration:
                        extra.extend(["--match-filter", f"duration<={self._max_duration * 60}"])
                    if self._cookie_browser:
                        extra.extend(["--cookies-from-browser", self._cookie_browser])
                    if "watch?v=" not in url:
                        extra.extend(["--playlist-end", "50"])
                    result = await yt_dlp_download(
                        url,
                        cookies_file=self._usable_cookie_file() or None,
                        output_template=os.path.join(tmpdir, "%(id)s.%(ext)s"),
                        max_downloads=None,  # respect playlist-end / per-video
                        retries=3,
                        impersonate="chrome",
                        write_thumbnail=True,
                        no_overwrites=True,
                        extra_args=extra,
                        timeout=self._video_download_timeout,
                        tempdir=tmpdir,
                        stop_event=self._stop if hasattr(self._stop, "wait") else None,
                    )
                    if not result.ok and not result.cancelled:
                        reason = result.output_summary(400)
                        status, log_level, retry_delay_hours = _classify_ytdlp_media_failure(reason)
                        log = logger.info if log_level == "info" else logger.warning
                        log(
                            "youtube yt-dlp video download %s for %s: rc=%s timed_out=%s retry_delay_hours=%s output_tail=%s",
                            status, url, result.returncode, result.timed_out, retry_delay_hours, reason,
                        )
                        m = re.search(r"watch\?v=([\w-]+)", url)
                        if m:
                            await self._mark_video_media_attempt(
                                m.group(1),
                                status=status,
                                reason=reason or f"yt-dlp rc={result.returncode}",
                            )
                        self._progress_count += 1  # tick watchdog so metadata-only runs don't look hung
                    eligible_files = 0
                    stored_files = 0
                    skipped_known = 0
                    skipped_extension = 0
                    for f in result.files:
                        if self._stop.is_set():
                            break
                        ext = f.suffix.lstrip(".").lower()
                        if ext not in ("jpg", "jpeg", "png", "webp", "mp4", "webm", "mkv"):
                            skipped_extension += 1
                            continue
                        eligible_files += 1
                        cid = f.stem
                        is_video = ext in ("mp4", "webm", "mkv")
                        if is_video:
                            cid = f"video_{cid}"
                        if self.is_known(cid):
                            skipped_known += 1
                            continue
                        inserted = await self.download_media({"entity_id": channel_id, "entity_name": channel_name, "content_type": "video" if is_video else "thumbnail", "content_id": cid, "source_path": str(f), "extension": ext if ext != "jpeg" else "jpg", "source_url": url if "watch?v=" in url else f"https://www.youtube.com/watch?v={f.stem}"})
                        if inserted:
                            stored_files += 1
                            if is_video:
                                await self._mark_video_media_attempt(f.stem, status="stored")
                    logger.info(
                        "youtube yt-dlp media ingest for %s: result_files=%d eligible=%d stored=%d skipped_known=%d skipped_ext=%d",
                        url,
                        len(result.files),
                        eligible_files,
                        stored_files,
                        skipped_known,
                        skipped_extension,
                    )
                    return stored_files

        results = await asyncio.gather(*(_download_one(url) for url in urls))
        return sum(results)

    def _yt_dlp_extra_args(self) -> list[str]:
        """Build yt-dlp args.

        Without ffmpeg, yt-dlp cannot merge separate video/audio streams. In
        that runtime, prefer single-file progressive formats so archival still
        produces playable media instead of partial/error-prone downloads.
        """
        extra: list[str] = []
        fmt = (self._ytdlp_format or "").strip()
        auto_format = not fmt or fmt.lower() in {"auto", "default", "best"}
        if auto_format and not self._ffmpeg_available:
            extra.extend(["-f", _YOUTUBE_PROGRESSIVE_FORMAT])
        elif not auto_format:
            extra.extend(["-f", fmt])
        if self._ytdlp_max_filesize:
            extra.extend(["--max-filesize", self._ytdlp_max_filesize])
        if self._merge_format and self._ffmpeg_available:
            extra.extend(["--merge-output-format", self._merge_format])
        return extra

    def _usable_cookie_file(self) -> str:
        """Return a valid Netscape cookies.txt path, or blank if unusable."""
        if self._usable_cookie_file_cache is not None:
            return self._usable_cookie_file_cache
        path = (self._cookie_file or "").strip()
        if not path:
            self._usable_cookie_file_cache = ""
            return ""
        p = Path(path)
        if not p.exists() or not p.is_file():
            logger.warning("youtube: ignoring missing cookie file %s", path)
            self._usable_cookie_file_cache = ""
            return ""
        try:
            if p.stat().st_size <= 0:
                logger.warning("youtube: ignoring empty cookie file %s", path)
                self._usable_cookie_file_cache = ""
                return ""
        except Exception as exc:
            logger.warning("youtube: ignoring unreadable cookie file %s: %s", path, _safe_log_text(exc))
            self._usable_cookie_file_cache = ""
            return ""
        try:
            lines: list[str] = []
            with p.open("r", encoding="utf-8", errors="replace") as fh:
                for _ in range(20):
                    line = fh.readline()
                    if not line:
                        break
                    lines.append(line.rstrip("\n"))
        except Exception as exc:
            logger.warning("youtube: ignoring unreadable cookie file %s: %s", path, _safe_log_text(exc))
            self._usable_cookie_file_cache = ""
            return ""
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            if "Netscape HTTP Cookie File" in stripped:
                self._usable_cookie_file_cache = path
                return path
            if stripped.startswith("#"):
                continue
            if len(stripped.split("\t")) >= 7:
                self._usable_cookie_file_cache = path
                return path
        logger.warning("youtube: ignoring cookie file %s because it is not Netscape cookies.txt format", path)
        self._usable_cookie_file_cache = ""
        return ""

    async def _filter_video_ids_already_archived(self, video_ids: list[str]) -> tuple[list[str], int]:
        """Drop explicit video IDs that Postgres already has archived files for.

        The in-memory known-id cache is a speed hint, not the source of truth.
        Long-running YouTube cycles can miss older DB rows and repeatedly invoke
        yt-dlp for already-archived videos; this DB check prevents that wasted
        bandwidth before subprocess work starts.
        """
        if not video_ids or self.pool is None:
            return list(video_ids or []), 0
        content_ids = [f"video_{vid}" for vid in video_ids if vid]
        if not content_ids:
            return [], 0
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT content_id
                    FROM media_items
                    WHERE source = 'youtube'
                      AND content_id = ANY($1::text[])
                    """,
                    content_ids,
                    timeout=10,
                )
        except Exception:
            logger.debug("youtube: DB archived-video filter failed", exc_info=True)
            return list(video_ids), 0

        archived = {str(row["content_id"]) for row in rows}
        if not archived:
            return list(video_ids), 0
        self._known_ids.update(archived)
        kept = [vid for vid in video_ids if f"video_{vid}" not in archived]
        return kept, len(video_ids) - len(kept)

    async def _select_live_download_video_ids(self, video_ids: list[str], limit: int) -> tuple[list[str], int, int]:
        """Choose the first missing downloadable IDs, then apply the live-cycle cap."""
        if limit <= 0:
            return list(video_ids or []), 0, 0
        candidates, skipped_duration = await self._filter_video_ids_for_download(list(video_ids or []))
        candidates, skipped_db = await self._filter_video_ids_already_archived(candidates)
        return candidates[:limit], skipped_duration, skipped_db

    async def _collect_thumbnails_via_yt_dlp(self, channel_id: str, channel_name: str):
        from src.core.subprocess_downloader import yt_dlp_download, managed_tempdir
        channel_url = f"https://www.youtube.com/channel/{channel_id}/videos"
        async with managed_tempdir("yt_thumb_") as tmpdir:
            extra: list[str] = ["--skip-download", "--playlist-end", "50"]
            if self._cookie_browser:
                extra.extend(["--cookies-from-browser", self._cookie_browser])
            result = await yt_dlp_download(
                channel_url,
                cookies_file=self._usable_cookie_file() or None,
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
                await self.download_media({"entity_id": channel_id, "entity_name": channel_name, "content_type": "thumbnail", "content_id": f.stem, "source_path": str(f), "extension": ext if ext != "jpeg" else "jpg", "source_url": f"https://www.youtube.com/watch?v={f.stem}"})

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
                    await self._mark_transcript_attempt(row["id"], status="failed", error=_safe_log_text(e))
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
                    await self._mark_comments_attempt(row["id"], status="failed", error=_safe_log_text(e))
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
        await self._mark_transcript_attempt(video_uuid, status="processing")
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
            cookie_file = self._usable_cookie_file()
            if cookie_file:
                cmd.extend(["--cookies", cookie_file])
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
                await self._mark_transcript_attempt(
                    video_uuid,
                    status="unavailable" if proc.returncode == 0 else "failed",
                    error="no_vtt_returned",
                )
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
                await self._mark_transcript_attempt(video_uuid, status="failed", error=_safe_log_text(e))
                return
            content = self._vtt_to_text(vtt_text)
            if not content:
                logger.info("YouTube transcript: empty after parse for %s", platform_video_id)
                await self._mark_transcript_attempt(video_uuid, status="empty", error="empty_after_parse")
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
            await self._mark_transcript_attempt(video_uuid, status="stored")
            logger.info("YouTube transcript saved for %s (%s, %d chars, generated=%s)", platform_video_id, lang, len(content), is_generated)

    @staticmethod
    def _parse_relative_timestamp(text: str) -> datetime | None:
        """Best-effort parse for YouTube relative timestamps like '3 days ago' or '1 month ago (edited)'."""
        return _parse_rel_ts(text)

    async def _fetch_comments(self, video_uuid, platform_video_id: str):
        """Run yt-dlp --write-comments to dump comments into the .info.json,
        then parse and bulk-INSERT into youtube_comments."""
        await self._mark_comments_attempt(video_uuid, status="processing")
        owner_channel_id = None
        try:
            async with self.pool.acquire() as conn:
                owner_row = await conn.fetchrow(
                    """
                    SELECT c.platform_channel_id
                    FROM youtube_videos v
                    LEFT JOIN youtube_channels c ON c.id = v.channel_id
                    WHERE v.id = $1
                    """,
                    video_uuid,
                )
                if owner_row:
                    owner_channel_id = owner_row["platform_channel_id"]
        except Exception:
            logger.debug("YouTube comments: owner channel lookup failed for %s", platform_video_id, exc_info=True)

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
            cookie_file = self._usable_cookie_file()
            if cookie_file:
                cmd.extend(["--cookies", cookie_file])
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
                await self._mark_comments_attempt(
                    video_uuid,
                    status="unavailable" if proc.returncode == 0 else "failed",
                    error="no_info_json_returned",
                )
                return
            try:
                info = json.loads(info_files[0].read_text(encoding="utf-8", errors="replace"))
            except Exception as e:
                logger.warning("YouTube comments: bad info.json for %s: %s", platform_video_id, e)
                await self._mark_comments_attempt(video_uuid, status="failed", error=_safe_log_text(e))
                return
            comments = info.get("comments") or []
            if not comments:
                logger.info("YouTube comments: 0 comments returned for %s", platform_video_id)
                await self._mark_comments_attempt(video_uuid, status="empty")
                return
            inserted = 0
            author_enqueues = 0
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
                        author_channel_id = c.get("author_id") or c.get("channel_id")
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
                            author_channel_id,
                            c.get("author_thumbnail"),
                            c.get("text"),
                            int(c.get("like_count") or 0),
                            parent if is_reply else None,
                            is_reply,
                            published_at,
                            datetime.now(timezone.utc),
                        )
                        if author_channel_id and str(author_channel_id).startswith("UC"):
                            if author_enqueues < self._max_comment_author_enqueues:
                                await self._queue_youtube_profile(
                                    conn,
                                    {
                                        "key_type": "channel",
                                        "platform_channel_id": author_channel_id,
                                        "profile_key": author_channel_id,
                                    },
                                    source="comment_author",
                                    priority=self._discovered_target_priority,
                                    discovered_from=f"youtube_comments:{cid}",
                                    metadata={
                                        "platform_video_id": platform_video_id,
                                        "owner_channel_id": owner_channel_id,
                                        "author_name": c.get("author"),
                                    },
                                )
                                author_enqueues += 1
                            await self._record_youtube_edge(
                                conn,
                                edge_type="commented_on_video",
                                source_table="youtube_comments",
                                source_record_id=cid,
                                source_channel_id=author_channel_id,
                                target_channel_id=owner_channel_id,
                                source_video_id=platform_video_id,
                                source_comment_id=cid,
                                strength=70,
                                evidence_text=(c.get("text") or "")[:1000],
                                evidence_url=url,
                                metadata={"author_name": c.get("author")},
                            )
                        await self._record_refs_from_text(
                            conn,
                            c.get("text"),
                            source_table="youtube_comments",
                            source_record_id=cid,
                            source_channel_id=author_channel_id or owner_channel_id,
                            source_video_id=platform_video_id,
                            source_comment_id=cid,
                            evidence_url=url,
                            priority=self._discovered_target_priority,
                        )
                        inserted += 1
                    except Exception as e:
                        logger.debug("YouTube comment insert failed (%s): %s", c.get("id"), e)
            await self._mark_comments_attempt(video_uuid, status="stored" if inserted else "empty")
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
                       v.channel_id,
                       v.last_media_attempt_at,
                       v.platform_published_at,
                       v.collected_at
                FROM youtube_videos v
                LEFT JOIN media_items mi
                  ON mi.source = 'youtube'
                 AND mi.content_id = 'video_' || v.platform_video_id
                WHERE mi.id IS NULL
                  AND NOT (
                    v.media_status = 'failed'
                    AND v.last_media_attempt_at IS NOT NULL
                    AND v.last_media_attempt_at > NOW() - ($2::int * INTERVAL '1 hour')
                  )
                  AND NOT (
                    v.media_status = 'skipped'
                    AND v.media_skip_reason = 'live_or_scheduled_placeholder'
                  )
                  AND NOT (
                    v.media_status = 'skipped'
                    AND v.media_skip_reason = 'over_duration_cap'
                    AND $3::int > 0
                  )
                ORDER BY
                  (v.last_media_attempt_at IS NULL) DESC,
                  CASE WHEN v.media_status = 'failed' THEN 1 ELSE 0 END,
                  COALESCE(v.last_media_attempt_at, TIMESTAMPTZ 'epoch') ASC,
                  COALESCE(v.platform_published_at, v.collected_at) ASC NULLS LAST,
                  v.platform_video_id ASC
                LIMIT $1
                """,
                scan_limit,
                self._video_backfill_failed_retry_hours,
                self._max_duration,
                timeout=20,
            )

        def row_value(row, key: str, default=None):
            try:
                return row[key]
            except Exception:
                return default

        limit_seconds = self._max_duration * 60 if self._max_duration else 0
        selected_rows: list = []
        selected = 0
        skipped_duration = 0
        skipped_live_placeholder = 0
        skipped_duration_ids: list[str] = []
        skipped_live_ids: list[str] = []
        for row in rows:
            if selected >= batch_size:
                break
            duration = row["duration"] or ""
            if duration.upper() in {"P0D", "PT0S"}:
                skipped_live_placeholder += 1
                skipped_live_ids.append(row["platform_video_id"])
                continue
            duration_seconds = parse_iso8601_duration(duration)
            if limit_seconds and duration_seconds and duration_seconds > limit_seconds:
                skipped_duration += 1
                skipped_duration_ids.append(row["platform_video_id"])
                continue
            selected_rows.append(row)
            selected += 1

        channel_ids = sorted(
            {
                str(channel_id)
                for channel_id in (row_value(row, "channel_id") for row in selected_rows)
                if channel_id
            }
        )
        channel_by_id: dict[str, dict] = {}
        if channel_ids:
            try:
                async with self.pool.acquire() as conn:
                    channel_rows = await conn.fetch(
                        """
                        SELECT id, platform_channel_id, title
                        FROM youtube_channels
                        WHERE id = ANY($1::uuid[])
                        """,
                        channel_ids,
                        timeout=10,
                    )
                channel_by_id = {str(row["id"]): dict(row) for row in channel_rows}
            except Exception:
                logger.debug("youtube: channel lookup for video backfill failed", exc_info=True)

        groups: dict[tuple[str, str], list[str]] = {}
        for row in selected_rows:
            channel_id = row_value(row, "channel_id")
            channel = channel_by_id.get(str(channel_id)) if channel_id else None
            platform_channel_id = (
                row_value(row, "platform_channel_id")
                or (channel or {}).get("platform_channel_id")
                or "unknown"
            )
            channel_name = (
                row_value(row, "channel_name")
                or (channel or {}).get("title")
                or "unknown"
            )
            key = (platform_channel_id, channel_name)
            groups.setdefault(key, []).append(row["platform_video_id"])
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
        if skipped_live_ids or skipped_duration_ids:
            try:
                async with self.pool.acquire() as conn:
                    if skipped_live_ids:
                        await conn.execute(
                            """
                            UPDATE youtube_videos
                            SET media_status = 'skipped',
                                media_skip_reason = 'live_or_scheduled_placeholder',
                                last_media_attempt_at = NOW()
                            WHERE platform_video_id = ANY($1::text[])
                            """,
                            skipped_live_ids,
                        )
                    if skipped_duration_ids:
                        await conn.execute(
                            """
                            UPDATE youtube_videos
                            SET media_status = 'skipped',
                                media_skip_reason = 'over_duration_cap',
                                last_media_attempt_at = NOW()
                            WHERE platform_video_id = ANY($1::text[])
                            """,
                            skipped_duration_ids,
                        )
            except Exception:
                logger.debug("youtube: media skip status update failed", exc_info=True)
        return groups

    async def _restore_over_duration_candidates(self) -> int:
        """Re-queue old rows skipped only because a duration cap used to exist.

        With ``YOUTUBE_MAX_VIDEO_DURATION_MINUTES=0`` the operator intent is
        "archive all videos". Historical rows can still carry
        ``media_skip_reason='over_duration_cap'`` from earlier capped runs; clear
        that soft skip so normal media backfill can pick them up again.
        """
        if self._max_duration or not self.pool:
            return 0
        try:
            async with self.pool.acquire() as conn:
                restored = await conn.fetchval(
                    """
                    WITH selected AS (
                        SELECT platform_video_id
                        FROM youtube_videos
                        WHERE media_status = 'skipped'
                          AND media_skip_reason = 'over_duration_cap'
                        ORDER BY last_media_attempt_at ASC NULLS FIRST,
                                 collected_at ASC NULLS LAST,
                                 platform_video_id ASC
                        LIMIT $1
                    ),
                    updated AS (
                        UPDATE youtube_videos v
                        SET media_status = 'pending',
                            media_skip_reason = NULL,
                            last_media_attempt_at = NULL
                        FROM selected
                        WHERE v.platform_video_id = selected.platform_video_id
                        RETURNING 1
                    )
                    SELECT count(*)::int FROM updated
                    """,
                    max(1, self._video_backfill_scan_limit),
                    timeout=20,
                )
            return int(restored or 0)
        except Exception:
            logger.debug("youtube: restore over-duration candidates failed", exc_info=True)
            return 0

    async def run_backfill(self):
        thumbnail_count = await super().run_backfill()
        if not self._download_videos or not self._use_yt_dlp or self._video_backfill_batch_size <= 0:
            return thumbnail_count
        restored = await self._restore_over_duration_candidates()
        if restored:
            logger.info("youtube: restored %d old over-duration video candidate(s)", restored)
        stored = 0
        attempted_total = 0
        group_total = 0
        for pass_no in range(1, self._video_backfill_max_passes + 1):
            if self._stop.is_set():
                break
            groups = await self._get_video_backfill_groups(self._video_backfill_batch_size)
            if not groups:
                break

            attempted = sum(len(v) for v in groups.values())
            attempted_total += attempted
            group_total += len(groups)
            pass_stored = 0
            for (channel_id, channel_name), video_ids in groups.items():
                if self._stop.is_set():
                    break
                pass_stored += await self._download_videos_via_yt_dlp(
                    channel_id,
                    channel_name,
                    video_ids,
                    allow_channel_fallback=False,
                )
            stored += pass_stored
            logger.info(
                "youtube: video backfill pass %d/%d attempted %d candidate(s) across %d channel(s), stored %d media item(s)",
                pass_no,
                self._video_backfill_max_passes,
                attempted,
                len(groups),
                pass_stored,
            )
            if pass_stored <= 0:
                break
        logger.info(
            "youtube: video backfill attempted %d candidate(s) across %d group-pass(es), stored %d media item(s)",
            attempted_total,
            group_total,
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
        if ctype == "community_image":
            channel = item.get("entity_id")
            if channel:
                return f"https://www.youtube.com/channel/{channel}/community"
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
            source_path = item.get("source_path")
            data = None
            if "data" in item:
                data = item["data"]
            elif source_path:
                request_url = item.get("url")
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
            source_url = item.get("source_url") or self._build_youtube_source_url(item)
            metadata = {
                "entity_id": item["entity_id"],
                "entity_name": item["entity_name"],
                "content_type": item["content_type"],
                "content_id": cid,
                "collected_at": datetime.now(timezone.utc).isoformat(),
                "raw": item.get("raw", {}),
                "rebuild_target_tables": ["media_items", "youtube_videos", "youtube_channels"],
            }
            artifact_metadata = {
                **metadata,
                "filename": filename,
                "source_url": source_url,
                "request_url": request_url if "url" in item else item.get("url"),
            }
            if source_path:
                artifact = write_atomic_artifact_from_path(
                    source=self.SOURCE_NAME,
                    artifact_id=cid,
                    artifact_kind="media_blob",
                    source_path=source_path,
                    extension=item.get("extension", "jpg"),
                    metadata=artifact_metadata,
                    root=VAULT_ROOT,
                    delete_source=True,
                )
            else:
                artifact = write_atomic_artifact(
                    source=self.SOURCE_NAME,
                    artifact_id=cid,
                    artifact_kind="media_blob",
                    data=data,
                    extension=item.get("extension", "jpg"),
                    metadata=artifact_metadata,
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
        except httpx.HTTPStatusError as e:
            safe_error = _safe_log_text(e)
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status == 404 and item.get("content_type") == "thumbnail":
                logger.warning(
                    "YouTube thumbnail unavailable %s: %s",
                    cid,
                    request_url or safe_error,
                )
                await self.send_to_dlq(item["entity_id"], cid, f"thumbnail_not_found: {safe_error}")
                return False
            logger.error("Download failed %s: %s", cid, safe_error)
            await self.send_to_dlq(item["entity_id"], cid, safe_error)
            return False
        except Exception as e:
            safe_error = _safe_log_text(e)
            logger.error("Download failed %s: %s", cid, safe_error)
            await self.send_to_dlq(item["entity_id"], cid, safe_error)
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
        if await self._youtube_api_cooldown_active(path, params.get("id") or params.get("playlistId")):
            return {}
        headers, qparams = self._yt_auth(params)
        own_client = client is None
        if own_client:
            client = httpx.AsyncClient(timeout=30, headers=headers)
        try:
            resp = await client.get(f"{YT_API_BASE}/{path}", params=qparams, headers=headers if not own_client else None)
            await self._record_api_request(path, status_code=resp.status_code, metadata={"path": path})
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
        cookie_file = self._usable_cookie_file()
        if cookie_file:
            cmd.extend(["--cookies", cookie_file])
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
                safe_error = _safe_log_text(e)
                logger.error("collect_target_channel failed for %s: %s", ch, safe_error)
                await self.send_to_dlq(ch, ch, safe_error)
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
                logger.error("batch_download failure for channel %s: %s", ch, _safe_log_text(e))
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
