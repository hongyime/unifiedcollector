import asyncio
import http.cookiejar
import json
import logging
import os
import random
import re
import tempfile
from pathlib import Path
from datetime import date, datetime, timedelta, timezone

import httpx

from src.core.base_collector import BaseCollector
from src.collectors.strava.parse import (
    normalize_training_activity as _parse_normalize_training_activity,
    normalize_feed_activity as _parse_normalize_feed_activity,
)
from src.core.profile_photo_tracker import ProfilePhotoTracker
from src.core.file_naming import sanitize_name

logger = logging.getLogger(__name__)

STRAVA_API = "https://www.strava.com/api/v3"
TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_WEB = "https://www.strava.com"


class StravaCollector(BaseCollector):
    SOURCE_NAME = "strava"

    def __init__(self):
        super().__init__()
        self._client_id = os.getenv("STRAVA_CLIENT_ID", "")
        self._client_secret = os.getenv("STRAVA_CLIENT_SECRET", "")
        self._refresh_token = os.getenv("STRAVA_REFRESH_TOKEN", "")
        self._session_cookie = os.getenv("STRAVA_SESSION_COOKIE", "")
        self._cookies_file = os.getenv("STRAVA_COOKIES_FILE", "credentials/strava/strava_cookies.txt")
        # Fallback: read session cookie from a Netscape cookie jar.
        # User can drop credentials/strava/strava_cookies.txt and we'll extract
        # the `_strava4_session` cookie automatically.
        if not self._session_cookie:
            self._session_cookie = self._load_session_cookie_from_file(self._cookies_file)
        self._access_token = ""
        self._sem = asyncio.Semaphore(2)

        self._api_delay_min = float(os.getenv("STRAVA_API_DELAY_MIN", "5.0"))
        self._api_delay_max = float(os.getenv("STRAVA_API_DELAY_MAX", "10.0"))
        self._feed_delay_min = float(os.getenv("STRAVA_FEED_DELAY_MIN", "5.0"))
        self._feed_delay_max = float(os.getenv("STRAVA_FEED_DELAY_MAX", "12.0"))
        self._backfill_steps = int(os.getenv("STRAVA_BACKFILL_STEPS", "25"))

        self._use_api = bool(self._client_id and self._client_secret and self._refresh_token)
        self._use_web = bool(self._session_cookie)
        self._photo_tracker = ProfilePhotoTracker()
        self._gps_enabled = os.getenv("STRAVA_GPS_ENABLED", "true").lower() == "true"
        self._follow_scrape_enabled = os.getenv("STRAVA_FOLLOW_SCRAPE_ENABLED", "true").lower() == "true"
        # Auto-seed roster expansion from the authenticated athlete (Bryan: spider
        # out from my own following/followers). Set after we fetch /athlete.
        self._my_athlete_id: str | None = None
        # Only ingest activities that carry media (photos) or a map/GPS polyline.
        self._require_media_or_map = os.getenv("STRAVA_REQUIRE_MEDIA_OR_MAP", "false").lower() == "true"

    def set_pool(self, pool):
        super().set_pool(pool)
        self._photo_tracker.set_pool(pool)

    @staticmethod
    def _load_session_cookie_from_file(path: str) -> str:
        """Parse a Netscape-format cookie jar and return the value of
        `_strava4_session`. Returns empty string if file missing or cookie not
        found. Does NOT raise — falls through to graceful skip."""
        try:
            if not os.path.exists(path):
                return ""
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split("\t")
                    if len(parts) >= 7 and parts[5] == "_strava4_session":
                        logger.info("Loaded Strava session cookie from %s", path)
                        return parts[6]
            logger.warning("Strava cookie file %s exists but no _strava4_session entry", path)
        except Exception as e:
            logger.warning("Failed to read Strava cookie file %s: %s", path, e)
        return ""

    @property
    def account_media_dir(self) -> Path:
        acc_name = sanitize_name(self._client_id[:8]) if self._client_id else "web"
        path = self.media_dir / f"account_{acc_name}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def _delay(self, min_s: float | None = None, max_s: float | None = None):
        lo = min_s or self._api_delay_min
        hi = max_s or self._api_delay_max
        await asyncio.sleep(random.uniform(lo, hi))

    async def _ensure_token(self):
        if self._access_token: return
        if not self._use_api: return
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(TOKEN_URL, data={"client_id": self._client_id, "client_secret": self._client_secret, "refresh_token": self._refresh_token, "grant_type": "refresh_token"})
            resp.raise_for_status()
            data = resp.json()
            self._access_token = data["access_token"]
            if data.get("refresh_token"): self._refresh_token = data["refresh_token"]
            logger.info("Strava token refreshed")

    async def collect(self, targets: list[str]):
        from src.core.env import env_bool
        if not env_bool("STRAVA_COLLECTOR_ENABLED", default=True):
            logger.info("strava: disabled via STRAVA_COLLECTOR_ENABLED=false, skipping")
            return
        # Previously disabled due to httpx → Z:/C: NTFS kernel D-state.
        # Root cause fixed: all mounts now on WSL2 ext4 named volumes.
        if self._use_api: await self._ensure_token()
        for target in targets:
            if self._stop.is_set(): break
            logger.info("Collecting strava/%s", target)
            try:
                if target.lower() == "me" and self._use_api:
                    await self._collect_authenticated_athlete()
                elif target.lower() == "me":
                    # No API creds: try cookie-based scrape of authenticated user's
                    # training_activities feed. Falls through to graceful skip
                    # if cookies are missing/invalid.
                    await asyncio.wait_for(self._collect_via_cookies(), timeout=300.0)
                elif target.lower() == "feed" and self._use_web:
                    # Cookie-based scrape of the authenticated dashboard/activity
                    # feed. (_collect_feed never existed; _collect_via_cookies is
                    # the real feed-scrape entrypoint.)
                    await asyncio.wait_for(self._collect_via_cookies(), timeout=300.0)
                elif self._use_api: await self._collect_athlete(target)
                elif self._use_web: await self._collect_athlete_web(target)
                else:
                    logger.warning("strava/%s: no auth available (no API creds, no session cookie); skipping", target)
                await self.checkpoint.save_progress(target)
            except Exception as e:
                logger.error("Failed strava/%s: %s", target, e)
                await self.send_to_dlq(target, target, str(e))

        if os.getenv("STRAVA_SPIDER_ENABLED", "true").lower() == "true":
            await self._process_spider_queue()

        # Optional follow-roster expansion: when STRAVA_ROSTER_SEED_TARGETS
        # is set (comma-separated athlete IDs) we scrape /follows pages and
        # enqueue every discovered athlete into strava_spider_queue. Off by
        # default to avoid surprise BFS expansion. Requires cookie auth.
        if self._follow_scrape_enabled:
            roster_seeds = os.getenv("STRAVA_ROSTER_SEED_TARGETS", "").strip()
            seed_ids = [s.strip() for s in roster_seeds.split(",") if s.strip()] if roster_seeds else []
            # Auto-seed from the authenticated athlete so we spider out from MY
            # own following/followers without needing a manual ID list (Bryan).
            if self._my_athlete_id and self._my_athlete_id not in seed_ids:
                seed_ids.insert(0, self._my_athlete_id)
            if seed_ids and self._use_web:
                for sid in seed_ids:
                    if not sid or self._stop.is_set():
                        continue
                    try:
                        await self.collect_following_roster(sid)
                    except Exception as e:
                        logger.warning("strava: roster expansion for %s failed: %s", sid, e)
            elif seed_ids and not self._use_web:
                logger.info("strava: roster expansion skipped (no session cookie / web auth); "
                            "set STRAVA_SESSION_COOKIE or cookies file to enable following/follower spider")

    async def _process_spider_queue(self):
        while not self._stop.is_set():
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow("""
                    UPDATE strava_spider_queue
                    SET status = 'processing'
                    WHERE id = (
                        SELECT id FROM strava_spider_queue
                        WHERE status = 'pending'
                        ORDER BY priority ASC, collected_at ASC
                        LIMIT 1
                    )
                    RETURNING platform_athlete_id
                """)
            if not row: break
            try:
                if self._use_api: await self._collect_athlete(str(row['platform_athlete_id']))
                elif self._use_web: await self._collect_athlete_web(str(row['platform_athlete_id']))
                async with self.pool.acquire() as conn:
                    await conn.execute("UPDATE strava_spider_queue SET status = 'completed' WHERE platform_athlete_id = $1", row['platform_athlete_id'])
            except Exception:
                async with self.pool.acquire() as conn:
                    await conn.execute("UPDATE strava_spider_queue SET status = 'failed' WHERE platform_athlete_id = $1", row['platform_athlete_id'])

    async def _collect_via_cookies(self):
        """Cookie-authenticated scrape of the logged-in user's training_activities.

        Strategy:
          1. Load cookies from Netscape jar (or fall back to STRAVA_SESSION_COOKIE
             value as a single _strava4_session cookie).
          2. Hit /athlete/training_activities?page=N with XHR header — Strava
             returns a JSON array of activity dicts.
          3. Resolve athlete identity from /api/v3/athlete (works with web cookie)
             or fall back to the dashboard HTML hydration JSON.
          4. Upsert athlete + each activity. Pagination stops on empty page.

        On any failure (no cookies, stale cookies, HTML-only response) this
        method logs a warning and returns cleanly — never raises.
        """
        if not os.path.exists(self._cookies_file) and not self._session_cookie:
            logger.warning("strava: no cookies available (file %s missing, no STRAVA_SESSION_COOKIE); skipping",
                           self._cookies_file)
            return

        # Build a cookie jar that httpx can consume.
        jar = httpx.Cookies()
        cookies_loaded = 0
        if os.path.exists(self._cookies_file):
            try:
                mj = http.cookiejar.MozillaCookieJar()
                mj.load(self._cookies_file, ignore_discard=True, ignore_expires=True)
                for c in mj:
                    jar.set(c.name, c.value, domain=c.domain or ".strava.com", path=c.path or "/")
                    cookies_loaded += 1
            except Exception as e:
                logger.warning("strava: failed to load cookie jar %s: %s", self._cookies_file, e)
        if cookies_loaded == 0 and self._session_cookie:
            jar.set("_strava4_session", self._session_cookie, domain=".strava.com", path="/")
            cookies_loaded = 1
        if cookies_loaded == 0:
            logger.warning("strava: no usable cookies after parse; skipping")
            return
        logger.info("strava: cookie scrape starting (%d cookies loaded)", cookies_loaded)

        ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        base_headers = {
            "User-Agent": ua,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "https://www.strava.com/dashboard",
        }

        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=25.0, write=10.0, pool=5.0),
                                     cookies=jar, follow_redirects=True,
                                     headers={**base_headers, "User-Agent": ua}) as client:
            # 1) Resolve current athlete via /api/v3/athlete (web cookie works here).
            athlete_id = None
            athlete_name = "me"
            try:
                resp = await asyncio.wait_for(
                    client.get(f"{STRAVA_API}/athlete", headers=base_headers), timeout=30.0)
                if resp.status_code == 200 and resp.headers.get("content-type", "").startswith("application/json"):
                    a = resp.json()
                    athlete_id = str(a.get("id") or "")
                    athlete_name = a.get("username") or athlete_name
                    if athlete_id:
                        try:
                            await self._upsert_athlete(a)
                        except Exception as e:
                            logger.warning("strava: athlete upsert failed: %s", e)
                else:
                    logger.info("strava: /api/v3/athlete returned HTTP %d (cookie may lack API scope); "
                                "falling back to dashboard hydration", resp.status_code)
            except Exception as e:
                logger.info("strava: /api/v3/athlete probe error: %s", e)

            # Fallback: pull athlete id AND profile data from dashboard HTML hydration.
            if not athlete_id:
                try:
                    dash = await asyncio.wait_for(
                        client.get(f"{STRAVA_WEB}/dashboard",
                                   headers={"User-Agent": ua, "Accept": "text/html,application/xhtml+xml"}),
                        timeout=30.0)
                    if dash.status_code == 200:
                        m = re.search(r'"athlete_id"\s*:\s*(\d+)', dash.text) or \
                            re.search(r'/athletes/(\d+)', dash.text)
                        if m:
                            athlete_id = m.group(1)
                            logger.info("strava: resolved athlete_id=%s via dashboard hydration", athlete_id)
                        # Also try to extract full profile from embedded JSON blob
                        # Strava embeds {"currentAthlete":{...}} in the page JS
                        profile_m = re.search(r'"currentAthlete"\s*:\s*(\{[^}]{20,500}\})', dash.text)
                        if profile_m:
                            try:
                                profile_data = json.loads(profile_m.group(1))
                                if profile_data.get("id"):
                                    await self._upsert_athlete(profile_data)
                                    logger.info("strava: upserted athlete profile from dashboard JSON")
                            except Exception:
                                pass
                except Exception as e:
                    logger.warning("strava: dashboard hydration probe failed: %s", e)

            if not athlete_id:
                logger.warning("strava: could not resolve current athlete id from cookies; skipping")
                return

            # Make sure athlete row exists so FK resolves.
            try:
                async with self.pool.acquire() as conn:
                    await conn.execute(
                        """INSERT INTO strava_athletes (platform_athlete_id, username, updated_at)
                           VALUES ($1, $2, NOW())
                           ON CONFLICT (platform_athlete_id) DO NOTHING""",
                        int(athlete_id), athlete_name,
                    )
            except Exception as e:
                logger.warning("strava: minimal athlete insert failed: %s", e)

            # 2) Paginate /athlete/training_activities (returns JSON for XHR).
            total = 0
            page = 1
            empty_streak = 0
            while not self._stop.is_set() and page <= 200:
                await self._delay(self._feed_delay_min, self._feed_delay_max)
                try:
                    resp = await asyncio.wait_for(
                        client.get(
                            f"{STRAVA_WEB}/athlete/training_activities",
                            headers=base_headers,
                            params={
                            "keywords": "",
                            "activity_type": "",
                            "workout_type": "",
                            "commute": "",
                            "private_activities": "",
                            "start_date": "",
                            "end_date": "",
                            "page": str(page),
                        }),
                        timeout=30.0,
                    )
                except Exception as e:
                    logger.warning("strava: training_activities page %d fetch error: %s", page, e)
                    break
                if resp.status_code == 429:
                    logger.warning("strava: rate-limited on page %d, sleeping 60s", page)
                    await asyncio.sleep(60)
                    continue
                if resp.status_code != 200:
                    logger.warning("strava: training_activities page %d HTTP %d (cookie likely stale); stopping",
                                   page, resp.status_code)
                    break
                ctype = resp.headers.get("content-type", "")
                if not ctype.startswith("application/json"):
                    logger.warning("strava: training_activities returned non-JSON (%s) on page %d; stopping",
                                   ctype, page)
                    break
                try:
                    payload = resp.json()
                except Exception as e:
                    logger.warning("strava: JSON decode failed on page %d: %s", page, e)
                    break
                # Strava returns either a bare list or {"models":[...], "total":N}
                if isinstance(payload, dict):
                    items = payload.get("models") or payload.get("activities") or []
                else:
                    items = payload
                if not items:
                    empty_streak += 1
                    if empty_streak >= 2:
                        break
                    page += 1
                    continue
                empty_streak = 0
                for raw in items:
                    if self._stop.is_set():
                        break
                    norm = self._normalize_training_activity(raw)
                    if not norm.get("id"):
                        continue
                    try:
                        await self._upsert_activity(norm, athlete_id)
                        total += 1
                    except Exception as e:
                        logger.warning("strava: upsert activity %s failed: %s", norm.get("id"), e)
                logger.info("strava: page %d ingested %d activities (running total %d)",
                            page, len(items), total)
                page += 1

            logger.info("strava: cookie scrape complete — %d activities upserted for athlete %s",
                        total, athlete_id)
            # Try to enrich athlete profile by scraping their public profile page
            if athlete_id:
                try:
                    profile_page = await client.get(
                        f"{STRAVA_WEB}/athletes/{athlete_id}",
                        headers={"User-Agent": ua, "Accept": "text/html,application/xhtml+xml"},
                    )
                    if profile_page.status_code == 200:
                        html = profile_page.text
                        # Extract name, location, profile photo from the page
                        athlete_patch: dict = {"id": int(athlete_id)}
                        name_m = re.search(r'<h1[^>]*class="[^"]*athlete-name[^"]*"[^>]*>([^<]+)<', html)
                        if not name_m:
                            name_m = re.search(r'"name"\s*:\s*"([^"]{2,60})"', html)
                        if name_m:
                            parts = name_m.group(1).strip().split(None, 1)
                            athlete_patch["firstname"] = parts[0]
                            if len(parts) > 1:
                                athlete_patch["lastname"] = parts[1]
                        loc_m = re.search(r'"location"\s*:\s*"([^"]{2,80})"', html)
                        if loc_m:
                            athlete_patch["city"] = loc_m.group(1)
                        photo_m = re.search(r'"profile"\s*:\s*"(https?://[^"]+\.(jpg|png|jpeg)[^"]*)"', html)
                        if not photo_m:
                            photo_m = re.search(r'<img[^>]+class="[^"]*avatar[^"]*"[^>]+src="([^"]+)"', html)
                        if photo_m:
                            athlete_patch["profile"] = photo_m.group(1)
                        if len(athlete_patch) > 1:
                            await self._upsert_athlete(athlete_patch)
                            logger.info("strava: enriched athlete %s profile from profile page", athlete_id)
                except Exception as e:
                    logger.debug("strava: profile page scrape failed: %s", e)

    @staticmethod
    def _normalize_training_activity(raw: dict) -> dict:
        """Map Strava /athlete/training_activities payload fields to the
        shape _upsert_activity expects (compatible with /api/v3/athlete/activities)."""
        return _parse_normalize_training_activity(raw)

    async def _collect_authenticated_athlete(self):
        async with httpx.AsyncClient(timeout=30) as client:
            await self._delay()
            resp = await client.get(f"{STRAVA_API}/athlete", headers={"Authorization": f"Bearer {self._access_token}"})
            resp.raise_for_status()
            athlete = resp.json()
            await self._upsert_athlete(athlete)
            aid, aname = str(athlete["id"]), athlete.get("username", str(athlete["id"]))
            self._my_athlete_id = aid
            if athlete.get("profile"):
                dest_dir = self.account_media_dir / "profiles"
                dest_dir.mkdir(parents=True, exist_ok=True)
                changed, path = await self._photo_tracker.check_and_download(athlete["profile"], aid, "strava", dest_dir)
                if changed and path:
                    await self.insert_media_item(entity_id=aid, entity_name=aname, content_type="profile_photo", content_id=f"profile_{aid}", filename=path.name, file_path=str(path), file_size=path.stat().st_size, sha256=self.sha256_bytes(path.read_bytes()), metadata={"raw": athlete})
            await self._collect_activities_api(client, aid, aname)

    async def _upsert_athlete(self, athlete: dict):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO strava_athletes (
                    platform_athlete_id, username, firstname, lastname, profile,
                    city, state, country, sex, follower_count, following_count,
                    updated_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, NOW())
                ON CONFLICT (platform_athlete_id) DO UPDATE SET
                    username = COALESCE(EXCLUDED.username, strava_athletes.username),
                    firstname = COALESCE(EXCLUDED.firstname, strava_athletes.firstname),
                    lastname = COALESCE(EXCLUDED.lastname, strava_athletes.lastname),
                    profile = COALESCE(EXCLUDED.profile, strava_athletes.profile),
                    city = COALESCE(EXCLUDED.city, strava_athletes.city),
                    state = COALESCE(EXCLUDED.state, strava_athletes.state),
                    country = COALESCE(EXCLUDED.country, strava_athletes.country),
                    follower_count = COALESCE(EXCLUDED.follower_count, strava_athletes.follower_count),
                    following_count = COALESCE(EXCLUDED.following_count, strava_athletes.following_count),
                    updated_at = NOW()
            """, athlete.get("id"), athlete.get("username"), athlete.get("firstname"),
                athlete.get("lastname"), athlete.get("profile"), athlete.get("city"),
                athlete.get("state"), athlete.get("country"), athlete.get("sex"),
                athlete.get("follower_count", 0) or athlete.get("friends", 0) or 0,
                athlete.get("friend_count", 0) or athlete.get("following_count", 0) or 0)

    async def _upsert_activity(self, activity: dict, athlete_id: str):
        async with self.pool.acquire() as conn:
            athlete_row = await conn.fetchrow("SELECT id FROM strava_athletes WHERE platform_athlete_id = $1", int(athlete_id))
            athlete_uuid = athlete_row['id'] if athlete_row else None
            metadata_json = json.dumps(activity, default=str)
            await conn.execute("""
                INSERT INTO strava_activities (
                    platform_activity_id, athlete_id, name, type, sport_type,
                    distance, moving_time, elapsed_time, total_elevation_gain,
                    average_speed, max_speed, average_heartrate, calories,
                    start_date, metadata
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15::jsonb)
                ON CONFLICT (platform_activity_id) DO UPDATE SET
                    name = EXCLUDED.name, metadata = EXCLUDED.metadata
            """, activity.get("id"), athlete_uuid, activity.get("name"), activity.get("type"), activity.get("sport_type"), activity.get("distance"), activity.get("moving_time"), activity.get("elapsed_time"), activity.get("total_elevation_gain"), activity.get("average_speed"), activity.get("max_speed"), activity.get("average_heartrate"), activity.get("calories"), datetime.fromisoformat(activity.get("start_date").replace("Z", "")) if activity.get("start_date") else None, metadata_json)
        # Count the activity as progress so the worker zero-progress watchdog
        # doesn't falsely flag strava (whose primary output is activity rows in
        # strava_activities, not media_items) as wedged.
        self._progress_count += 1

    async def _collect_athlete(self, athlete_id: str):
        async with httpx.AsyncClient(timeout=30) as client:
            await self._delay()
            resp = await client.get(f"{STRAVA_API}/athletes/{athlete_id}/stats", headers={"Authorization": f"Bearer {self._access_token}"})
            if resp.status_code == 200: await self._collect_activities_api(client, athlete_id, athlete_id)

    async def _collect_athlete_web(self, athlete_id: str):
        """Cookie-based web scrape fallback when API credentials are unavailable.

        Strava's web UI requires an authenticated session cookie. With a valid
        STRAVA_SESSION_COOKIE we fetch the public profile page and extract
        basic athlete info; activity pagination via web is not supported here
        (would require parsing the activities feed). Logs and exits cleanly
        when no usable auth path exists.
        """
        if not self._session_cookie:
            logger.warning("strava web scrape requested for %s but no STRAVA_SESSION_COOKIE set; skipping", athlete_id)
            return
        if athlete_id.lower() == "me":
            logger.warning("strava target 'me' requires API auth (STRAVA_CLIENT_ID/SECRET/REFRESH_TOKEN); web cookie alone cannot resolve self; skipping")
            return
        url = f"{STRAVA_WEB}/athletes/{athlete_id}"
        headers = {
            "Cookie": self._session_cookie,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml",
        }
        try:
            await self._delay(self._feed_delay_min, self._feed_delay_max)
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                logger.warning("strava web fetch %s returned HTTP %d; cookie may be stale", athlete_id, resp.status_code)
                return
            html = resp.text
            name_match = re.search(r'<title>([^<]+)</title>', html)
            athlete_name = name_match.group(1).strip() if name_match else athlete_id
            athlete = {
                "id": athlete_id,
                "username": athlete_name,
                "firstname": athlete_name.split()[0] if athlete_name else None,
                "lastname": " ".join(athlete_name.split()[1:]) if len(athlete_name.split()) > 1 else None,
                "profile": None,
                "follower_count": 0,
                "friend_count": 0,
            }
            try:
                await self._upsert_athlete(athlete)
            except Exception as e:
                logger.warning("strava web upsert failed for %s: %s", athlete_id, e)
            logger.info("strava web scrape captured profile for %s (activities require API)", athlete_id)
        except Exception as e:
            logger.error("strava web scrape failed for %s: %s", athlete_id, e)
            await self.send_to_dlq(athlete_id, athlete_id, f"web_scrape:{e}")

    async def _collect_activities_api(self, client: httpx.AsyncClient, aid: str, aname: str):
        page, per_page = 1, 50
        while not self._stop.is_set():
            await self._delay()
            async with self._sem:
                resp = await client.get(f"{STRAVA_API}/athlete/activities", headers={"Authorization": f"Bearer {self._access_token}"}, params={"page": page, "per_page": per_page})
            if resp.status_code == 429: await asyncio.sleep(60); continue
            resp.raise_for_status()
            activities = resp.json()
            if not activities: break
            for activity in activities:
                if self._stop.is_set(): break
                await self._upsert_activity(activity, aid)
                # MEDIA/MAP FILTER (Bryan): only spider/collect media from activities
                # that carry photos or a map/GPS polyline. The activity row is still
                # upserted above (metadata); we just skip the media-collection work.
                if self._require_media_or_map:
                    has_photos = bool(activity.get("total_photo_count", 0))
                    mp = activity.get("map") or {}
                    has_map = bool(mp.get("summary_polyline") or mp.get("polyline")
                                   or activity.get("summary_polyline"))
                    if not has_photos and not has_map:
                        continue
                await self._collect_activity_photos(client, activity, aid, aname)
                # Wave 0 leverage: persist route polyline for media_download
                # consumers. No-op when activity has no map data.
                try:
                    await self.download_route_maps(activity, athlete_id=aid)
                except Exception as e:
                    logger.debug("strava: route_map persist skipped for %s: %s",
                                 activity.get("id"), e)
                if self._gps_enabled: await self._collect_gps_streams(client, activity, aid)
            page += 1

    async def _collect_activity_photos(self, client: httpx.AsyncClient, activity: dict, aid: str, aname: str):
        activity_id = str(activity["id"])
        if not activity.get("total_photo_count", 0): return
        await self._delay()
        photo_resp = await client.get(f"{STRAVA_API}/activities/{activity_id}/photos", headers={"Authorization": f"Bearer {self._access_token}"}, params={"size": 2048})
        if photo_resp.status_code != 200: return
        for i, photo in enumerate(photo_resp.json()):
            urls = photo.get("urls", {})
            url = urls.get("2048") or urls.get("600") or urls.get("100")
            if not url: continue
            if not self.is_known(f"{activity_id}_{i}"):
                await self.download_media({"entity_id": aid, "entity_name": aname, "content_type": "activity", "content_id": f"{activity_id}_{i}", "url": url, "extension": "jpg", "source_url": f"https://www.strava.com/activities/{activity_id}", "raw": photo})

    async def _collect_gps_streams(self, client: httpx.AsyncClient, activity: dict, aid: str):
        activity_id = str(activity["id"])
        async with self.pool.acquire() as conn:
            exists = await conn.fetchval("SELECT 1 FROM strava_gps_streams WHERE activity_id = (SELECT id FROM strava_activities WHERE platform_activity_id = $1)", int(activity_id))
            if exists: return
        try:
            await self._delay()
            resp = await client.get(f"{STRAVA_API}/activities/{activity_id}/streams", headers={"Authorization": f"Bearer {self._access_token}"}, params={"keys": "latlng,time,altitude", "key_by_type": "true"})
            if resp.status_code == 200:
                streams = resp.json()
                async with self.pool.acquire() as conn:
                    act_row = await conn.fetchrow("SELECT id FROM strava_activities WHERE platform_activity_id = $1", int(activity_id))
                    if act_row:
                        await conn.execute("INSERT INTO strava_gps_streams (activity_id, latlng, time, altitude) VALUES ($1, $2, $3, $4)", act_row['id'], json.dumps(streams.get("latlng", {}).get("data", [])), json.dumps(streams.get("time", {}).get("data", [])), json.dumps(streams.get("altitude", {}).get("data", [])))
        except Exception: pass

    async def download_media(self, item: dict):
        cid = item["content_id"]
        if self.is_known(cid): return
        filename = self.build_filename(item["entity_id"], item["entity_name"], item["content_type"], cid, extension=item.get("extension", "jpg"))
        dest_dir = self.account_media_dir / item["content_type"]
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / filename
        try:
            await self._delay()
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                resp = await client.get(item["url"])
                resp.raise_for_status()
                data = resp.content
            sha = self.sha256_bytes(data)
            fd, tmp_path = tempfile.mkstemp(dir=dest_dir, suffix=".tmp")
            with os.fdopen(fd, "wb") as f:
                f.write(data); f.flush(); os.fsync(f.fileno())
            os.replace(tmp_path, dest)
            metadata = {"entity_id": item["entity_id"], "entity_name": item["entity_name"], "content_type": item["content_type"], "content_id": cid, "collected_at": datetime.now(timezone.utc).isoformat(), "raw": item.get("raw", {})}
            self.save_json(metadata, dest_dir / f"{Path(filename).stem}_metadata.json")
            await self.insert_media_item(entity_id=item["entity_id"], entity_name=item["entity_name"], content_type=item["content_type"], content_id=cid, filename=filename, file_path=str(dest), file_size=len(data), sha256=sha, metadata=metadata)
            self._known_ids.add(cid)
        except Exception as e:
            logger.error("Download failed %s: %s", cid, e)
            await self.send_to_dlq(item["entity_id"], cid, str(e))

    # ------------------------------------------------------------------ #
    # Following-feed playback (ported from stravatoolkit FollowingFeedScraper)
    # ------------------------------------------------------------------ #
    # The /dashboard/feed endpoint is cookie-only and lets us page back through
    # the authenticated athlete's following feed by `before` timestamp + cursor.
    # We use it to discover historical activities that the API view of an
    # athlete does NOT expose (e.g., friend activities). All discovered
    # activities upsert by platform_activity_id into strava_activities, which
    # is the same table the API path writes — so feed-discovered rows merge
    # cleanly with API-discovered rows under the existing UNIQUE constraint.

    @staticmethod
    def _day_bounds(date_string: str) -> tuple[int, int]:
        """Return (epoch_start, epoch_end) for a YYYY-MM-DD date in UTC."""
        d = datetime.fromisoformat(date_string).replace(tzinfo=timezone.utc)
        start = int(d.timestamp())
        end = start + 86400 - 1
        return start, end

    def _build_cookie_jar(self) -> httpx.Cookies | None:
        """Construct an httpx.Cookies jar from STRAVA_COOKIES_FILE or
        STRAVA_SESSION_COOKIE. Returns None when neither is usable."""
        jar = httpx.Cookies()
        loaded = 0
        if os.path.exists(self._cookies_file):
            try:
                mj = http.cookiejar.MozillaCookieJar()
                mj.load(self._cookies_file, ignore_discard=True, ignore_expires=True)
                for c in mj:
                    jar.set(c.name, c.value, domain=c.domain or ".strava.com", path=c.path or "/")
                    loaded += 1
            except Exception as e:
                logger.warning("strava feed: cookie jar load failed: %s", e)
        if loaded == 0 and self._session_cookie:
            jar.set("_strava4_session", self._session_cookie, domain=".strava.com", path="/")
            loaded = 1
        return jar if loaded > 0 else None

    @staticmethod
    def _normalize_feed_activity(raw_item: dict) -> dict | None:
        """Map a /dashboard/feed entry to our strava_activities upsert shape.

        The feed payload nests the activity under either `activity`, `row`, or
        the raw item itself. We only need the fields _upsert_activity stores
        (id, name, type, start_date, distance, elapsed_time, etc.).
        """
        return _parse_normalize_feed_activity(raw_item)

    def _extract_feed_page(self, payload) -> tuple[list[dict], int | None]:
        """Pull (items, next_cursor) out of a /dashboard/feed JSON payload."""
        items: list[dict] = []
        if isinstance(payload, list):
            items = [x for x in payload if isinstance(x, dict)]
            return items, None
        if isinstance(payload, dict):
            for key in ("entries", "items", "feed", "results"):
                v = payload.get(key)
                if isinstance(v, list):
                    items = [x for x in v if isinstance(x, dict)]
                    break
            cursor = None
            pagination = payload.get("pagination") if isinstance(payload.get("pagination"), dict) else {}
            for v in (payload.get("cursor"), pagination.get("cursor"), pagination.get("next_cursor")):
                if v:
                    cursor = v
                    break
            if cursor is None and items:
                last = items[-1]
                cd = last.get("cursorData") if isinstance(last.get("cursorData"), dict) else {}
                cursor = cd.get("updated_at") or cd.get("rank")
            try:
                cursor = int(cursor) if cursor is not None else None
            except (TypeError, ValueError):
                cursor = None
            return items, cursor
        return [], None

    async def fetch_feed_for_date(self, athlete_id, date_string: str) -> list[dict]:
        """Fetch following-feed activities visible to `athlete_id` whose
        start_date falls within UTC day `date_string` (YYYY-MM-DD).

        Cookie-only. Returns [] when no auth, when the feed responds non-200,
        or when no items match the day window. Persists every matched activity
        into strava_activities (UPSERT on platform_activity_id, so feed-only
        rows merge with API-discovered ones).
        """
        if not self._use_web:
            logger.info("strava feed: cookie auth required; skipping athlete=%s", athlete_id)
            return []
        jar = self._build_cookie_jar()
        if jar is None:
            logger.warning("strava feed: no cookies available; skipping")
            return []
        try:
            day_start, day_end = self._day_bounds(date_string)
        except Exception as e:
            logger.warning("strava feed: bad date %r: %s", date_string, e)
            return []

        ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        headers = {
            "User-Agent": ua,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "https://www.strava.com/dashboard",
        }

        results: list[dict] = []
        seen: set[int] = set()
        before = day_end
        cursor = None
        page = 1
        max_pages = int(os.getenv("STRAVA_FEED_MAX_PAGES", "20"))

        async with httpx.AsyncClient(timeout=30, cookies=jar, follow_redirects=True) as client:
            while not self._stop.is_set() and page <= max_pages:
                await self._delay(self._feed_delay_min, self._feed_delay_max)
                params = {
                    "feed_type": "following",
                    "athlete_id": athlete_id,
                    "before": before,
                }
                if cursor is not None:
                    params["cursor"] = cursor
                try:
                    resp = await client.get(f"{STRAVA_WEB}/dashboard/feed", headers=headers, params=params)
                except Exception as e:
                    logger.warning("strava feed: page %d fetch error: %s", page, e)
                    break
                if resp.status_code == 429:
                    logger.warning("strava feed: rate-limited on page %d, sleeping 60s", page)
                    await asyncio.sleep(60)
                    continue
                if resp.status_code != 200:
                    logger.warning("strava feed: stopped at page %d HTTP %d", page, resp.status_code)
                    break
                try:
                    payload = resp.json()
                except Exception as e:
                    logger.warning("strava feed: JSON decode failed page %d: %s", page, e)
                    break
                items, next_cursor = self._extract_feed_page(payload)
                if not items:
                    logger.info("strava feed: page %d empty; stopping", page)
                    break

                reached_older = False
                for raw in items:
                    norm = self._normalize_feed_activity(raw)
                    if not norm:
                        continue
                    aid = norm["id"]
                    if aid in seen:
                        continue
                    # Filter by day window using start_date.
                    sd = norm.get("start_date") or ""
                    try:
                        ts = int(datetime.fromisoformat(sd.replace("Z", "+00:00")).timestamp())
                    except Exception:
                        continue
                    if ts < day_start:
                        reached_older = True
                        continue
                    if ts > day_end:
                        continue
                    seen.add(aid)
                    results.append(norm)
                    # Persist immediately so partial failures still capture progress.
                    aoid = norm.get("_athlete_id") or athlete_id
                    try:
                        # Make sure athlete row exists for FK resolution.
                        async with self.pool.acquire() as conn:
                            await conn.execute(
                                """INSERT INTO strava_athletes (platform_athlete_id, username, updated_at)
                                   VALUES ($1, $2, NOW())
                                   ON CONFLICT (platform_athlete_id) DO NOTHING""",
                                int(aoid), norm.get("_athlete_name") or str(aoid),
                            )
                        await self._upsert_activity(norm, str(aoid))
                    except Exception as e:
                        logger.warning("strava feed: upsert activity %s failed: %s", aid, e)

                logger.info("strava feed: page %d kept %d total=%d (next_cursor=%s)",
                            page, len(results), len(results), next_cursor)
                if reached_older or next_cursor is None:
                    break
                cursor = next_cursor
                before = next_cursor
                page += 1

        logger.info("strava feed: %s on %s -> %d activities", athlete_id, date_string, len(results))
        return results

    async def backfill_feed_history(self, athlete_id, days_back: int = 30) -> int:
        """Walk N days backwards from yesterday and call fetch_feed_for_date
        for each. Returns total activities discovered across all days.

        Records day coverage in strava_day_coverage so future runs can
        skip already-walked days. Halts early on consecutive failures.
        """
        if days_back <= 0:
            return 0
        if not self._use_web:
            logger.info("strava feed backfill: cookie auth required; skipping")
            return 0
        total = 0
        today = datetime.now(timezone.utc).date()
        consecutive_failures = 0
        for i in range(1, days_back + 1):
            if self._stop.is_set():
                break
            day = today - timedelta(days=i)
            date_string = day.isoformat()
            # Skip days we've already covered.
            try:
                async with self.pool.acquire() as conn:
                    has = await conn.fetchval(
                        "SELECT has_data FROM strava_day_coverage WHERE athlete_id = $1 AND date = $2",
                        int(athlete_id), day,
                    )
            except Exception:
                has = None
            if has is True:
                logger.info("strava feed backfill: %s already covered, skipping", date_string)
                continue
            try:
                acts = await self.fetch_feed_for_date(athlete_id, date_string)
                total += len(acts)
                consecutive_failures = 0
                try:
                    async with self.pool.acquire() as conn:
                        await conn.execute(
                            """INSERT INTO strava_day_coverage (athlete_id, date, has_data)
                               VALUES ($1, $2, $3)
                               ON CONFLICT (athlete_id, date) DO UPDATE
                               SET has_data = EXCLUDED.has_data OR strava_day_coverage.has_data""",
                            int(athlete_id), day, len(acts) > 0,
                        )
                except Exception as e:
                    logger.debug("strava feed backfill: coverage upsert failed for %s: %s", date_string, e)
            except Exception as e:
                logger.warning("strava feed backfill: %s failed: %s", date_string, e)
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    logger.warning("strava feed backfill: 3 consecutive failures; stopping")
                    break
        logger.info("strava feed backfill: athlete=%s days=%d total_activities=%d",
                    athlete_id, days_back, total)
        return total

    async def cleanup(self):
        pass

    # ------------------------------------------------------------------ #
    # Public method surface (parity with stravatoolkit task spec)
    # ------------------------------------------------------------------ #
    # The task contract asks for these named coroutines on the collector:
    #   collect_athlete_profile, collect_activities, collect_clubs,
    #   collect_segments_starred, download_route_maps, run().
    # Internally we re-use the existing _collect_athlete*/_collect_activities_api
    # plumbing so behavior stays consistent with the original toolkit.

    async def collect_athlete_profile(self, athlete_id: str):
        """Fetch + upsert profile for one athlete.

        Tries API first (when STRAVA_CLIENT_ID/SECRET/REFRESH_TOKEN are
        configured), falls back to cookie-authenticated web scrape, and
        finally degrades to a warning when no auth is available.
        """
        athlete_id = str(athlete_id)
        if self._use_api:
            await self._ensure_token()
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    await self._delay()
                    if athlete_id.lower() == "me":
                        resp = await client.get(
                            f"{STRAVA_API}/athlete",
                            headers={"Authorization": f"Bearer {self._access_token}"},
                        )
                    else:
                        # /athletes/{id} returns limited public fields.
                        resp = await client.get(
                            f"{STRAVA_API}/athletes/{athlete_id}",
                            headers={"Authorization": f"Bearer {self._access_token}"},
                        )
                    if resp.status_code == 200:
                        await self._upsert_athlete(resp.json())
                        return
                    logger.warning("strava: profile API for %s returned HTTP %d", athlete_id, resp.status_code)
            except Exception as e:
                logger.warning("strava: profile API for %s failed: %s", athlete_id, e)
        if self._use_web:
            await self._collect_athlete_web(athlete_id)
            return
        logger.warning("strava: no auth path available for profile %s", athlete_id)

    async def collect_activities(self, athlete_id: str):
        """Page-walk an athlete's activities.

        For 'me' (or when API auth is present and we own the token) this uses
        /api/v3/athlete/activities. For other athletes the public API only
        exposes stats (distance/segment counts) — no activity list — so we
        fall back to the cookie-driven /athlete/training_activities path
        when the target is the authenticated user.
        """
        athlete_id = str(athlete_id)
        if self._use_api:
            await self._ensure_token()
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    if athlete_id.lower() == "me":
                        resp = await client.get(
                            f"{STRAVA_API}/athlete",
                            headers={"Authorization": f"Bearer {self._access_token}"},
                        )
                        if resp.status_code == 200:
                            a = resp.json()
                            aid = str(a.get("id") or athlete_id)
                            aname = a.get("username") or aid
                        else:
                            aid, aname = athlete_id, athlete_id
                    else:
                        aid, aname = athlete_id, athlete_id
                    await self._collect_activities_api(client, aid, aname)
                return
            except Exception as e:
                logger.warning("strava: activities API for %s failed: %s", athlete_id, e)
        if self._use_web and athlete_id.lower() == "me":
            await self._collect_via_cookies()
            return
        logger.info("strava: no activity path for %s (public profiles need API auth)", athlete_id)

    async def collect_clubs(self, athlete_id: str):
        """Collect club memberships for the authenticated athlete.

        Strava only exposes /athlete/clubs for the *current* token holder, so
        this is a no-op for arbitrary athlete IDs. Persists raw club list
        into the strava_athletes.metadata JSON via update — schema does not
        ship a dedicated strava_clubs table, so we store on the athlete row.
        """
        if not self._use_api:
            logger.info("strava: clubs requires API auth; skipping for %s", athlete_id)
            return
        await self._ensure_token()
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                await self._delay()
                resp = await client.get(
                    f"{STRAVA_API}/athlete/clubs",
                    headers={"Authorization": f"Bearer {self._access_token}"},
                )
            if resp.status_code != 200:
                logger.info("strava: /athlete/clubs HTTP %d", resp.status_code)
                return
            clubs = resp.json() or []
            if not clubs:
                return
            # Save sidecar JSON under media_dir/clubs/ so we don't lose
            # data when the schema lacks a clubs table. This mirrors
            # the approach the toolkit took for ad-hoc payloads.
            dest_dir = self.account_media_dir / "clubs"
            dest_dir.mkdir(parents=True, exist_ok=True)
            self.save_json(
                {"athlete_id": athlete_id, "clubs": clubs,
                 "collected_at": datetime.now(timezone.utc).isoformat()},
                dest_dir / f"clubs_{athlete_id}.json",
            )
            logger.info("strava: captured %d clubs for athlete %s", len(clubs), athlete_id)
        except Exception as e:
            logger.warning("strava: clubs fetch failed for %s: %s", athlete_id, e)

    async def collect_segments_starred(self, athlete_id: str):
        """Collect the authenticated athlete's starred segments.

        Persists into strava_segments. Only the API-authenticated current
        athlete can list their stars; for other athlete IDs this is a no-op.
        """
        if not self._use_api:
            logger.info("strava: starred segments requires API auth; skipping for %s", athlete_id)
            return
        await self._ensure_token()
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                page, per_page = 1, 50
                while not self._stop.is_set() and page <= 20:
                    await self._delay()
                    resp = await client.get(
                        f"{STRAVA_API}/segments/starred",
                        headers={"Authorization": f"Bearer {self._access_token}"},
                        params={"page": page, "per_page": per_page},
                    )
                    if resp.status_code == 429:
                        await asyncio.sleep(60)
                        continue
                    if resp.status_code != 200:
                        logger.info("strava: starred segments HTTP %d page %d", resp.status_code, page)
                        return
                    segs = resp.json() or []
                    if not segs:
                        return
                    for seg in segs:
                        try:
                            await self._upsert_segment(seg)
                        except Exception as e:
                            logger.warning("strava: segment upsert failed: %s", e)
                    if len(segs) < per_page:
                        return
                    page += 1
        except Exception as e:
            logger.warning("strava: starred segments failed for %s: %s", athlete_id, e)

    async def _upsert_segment(self, seg: dict):
        sll = seg.get("start_latlng") or []
        ell = seg.get("end_latlng") or []
        sll_s = ",".join(str(x) for x in sll) if isinstance(sll, list) else str(sll)
        ell_s = ",".join(str(x) for x in ell) if isinstance(ell, list) else str(ell)
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO strava_segments (
                    platform_segment_id, name, activity_type, distance,
                    average_grade, maximum_grade, elevation_high, elevation_low,
                    start_latlng, end_latlng, climb_category
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                ON CONFLICT (platform_segment_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    distance = EXCLUDED.distance,
                    average_grade = EXCLUDED.average_grade
                """,
                seg.get("id"),
                seg.get("name"),
                seg.get("activity_type"),
                seg.get("distance"),
                seg.get("average_grade"),
                seg.get("maximum_grade"),
                seg.get("elevation_high"),
                seg.get("elevation_low"),
                sll_s,
                ell_s,
                seg.get("climb_category"),
            )

    async def download_route_maps(self, activity: dict, athlete_id: str | None = None):
        """Persist route polyline data for an activity.

        The Strava-issued summary_polyline is sufficient to reconstruct the
        route. We do not call out to a paid map tile provider — instead we
        save the polyline + bounds as a JSON sidecar under
        media_dir/routes/ and register the file in collected_media so the
        unified store can serve it. Render-time conversion to PNG is
        deferred to consumers.
        """
        try:
            activity_id = str(activity.get("id") or activity.get("activity_id") or "")
            if not activity_id:
                return
            if self.is_known(f"route_{activity_id}"):
                return
            mp = activity.get("map") or {}
            polyline = (mp.get("summary_polyline")
                        or mp.get("polyline")
                        or activity.get("summary_polyline"))
            if not polyline:
                return
            payload = {
                "activity_id": activity_id,
                "athlete_id": athlete_id,
                "polyline": polyline,
                "start_latlng": activity.get("start_latlng"),
                "end_latlng": activity.get("end_latlng"),
                "bounds": mp.get("bounds"),
                "name": activity.get("name"),
                "distance": activity.get("distance"),
                "collected_at": datetime.now(timezone.utc).isoformat(),
            }
            dest_dir = self.account_media_dir / "routes"
            dest_dir.mkdir(parents=True, exist_ok=True)
            filename = self.build_filename(
                str(athlete_id or "unknown"),
                str(athlete_id or "unknown"),
                "route_map",
                activity_id,
                extension="json",
            )
            dest = dest_dir / filename
            data = json.dumps(payload, indent=2).encode("utf-8")
            fd, tmp_path = tempfile.mkstemp(dir=dest_dir, suffix=".tmp")
            with os.fdopen(fd, "wb") as f:
                f.write(data); f.flush(); os.fsync(f.fileno())
            os.replace(tmp_path, dest)
            await self.insert_media_item(
                entity_id=str(athlete_id or "unknown"),
                entity_name=str(athlete_id or "unknown"),
                content_type="route_map",
                content_id=activity_id,
                filename=filename,
                file_path=str(dest),
                file_size=len(data),
                sha256=self.sha256_bytes(data),
                metadata=payload,
            )
            self._known_ids.add(f"route_{activity_id}")
        except Exception as e:
            logger.warning("strava: route map persist failed for %s: %s",
                           activity.get("id"), e)

    async def collect_following_roster(self, athlete_id: str, max_pages: int = 50):
        """Cookie-only HTML scrape of /athletes/{id}/follows?type=following.

        Parses athlete cards using the same regexes as the original toolkit
        and seeds the spider queue (BFS expansion). Stops on empty page
        or non-200. Returns the number of seeds enqueued.
        """
        if not self._use_web:
            logger.info("strava: follow roster needs cookie auth; skipping %s", athlete_id)
            return 0
        from html import unescape
        # Local copies of the toolkit's regexes so we don't take a runtime dep
        # on stravatoolkit/. Kept verbatim modulo Python triple-string quoting.
        ATHLETE_CARD_RE = re.compile(
            r'data-athlete-id="(?P<id>\d+)".*?(?:src|data-src)="(?P<avatar>[^"]*)".*?'
            r'(?:text-headline|athlete-name)[^>]*>(?P<name>.*?)<',
            re.DOTALL,
        )
        ATHLETE_LIST_ITEM_RE = re.compile(
            r"<li[^>]*data-athlete-id=['\"](?P<id>\d+)['\"][^>]*>(?P<body>.*?)</li>",
            re.DOTALL,
        )
        ATHLETE_LINK_RE = re.compile(
            r"<a[^>]+href=['\"]/athletes/\d+['\"][^>]*>(?P<name>.*?)</a>",
            re.DOTALL,
        )

        jar = httpx.Cookies()
        if os.path.exists(self._cookies_file):
            try:
                mj = http.cookiejar.MozillaCookieJar()
                mj.load(self._cookies_file, ignore_discard=True, ignore_expires=True)
                for c in mj:
                    jar.set(c.name, c.value, domain=c.domain or ".strava.com", path=c.path or "/")
            except Exception as e:
                logger.warning("strava roster: cookie jar load failed: %s", e)
        if not jar and self._session_cookie:
            jar.set("_strava4_session", self._session_cookie, domain=".strava.com", path="/")
        if not jar:
            logger.warning("strava roster: no cookies available; skipping")
            return 0

        ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        headers = {"User-Agent": ua, "Accept": "text/html,application/xhtml+xml"}
        seeded = 0
        async with httpx.AsyncClient(timeout=30, cookies=jar, follow_redirects=True) as client:
            page = 1
            seen: set[int] = set()
            while not self._stop.is_set() and page <= max_pages:
                await self._delay(self._feed_delay_min, self._feed_delay_max)
                try:
                    resp = await client.get(
                        f"{STRAVA_WEB}/athletes/{athlete_id}/follows",
                        params={"type": "following", "page": page},
                        headers=headers,
                    )
                except Exception as e:
                    logger.warning("strava roster: page %d fetch error: %s", page, e)
                    break
                if resp.status_code == 429:
                    await asyncio.sleep(60); continue
                if resp.status_code != 200 or not resp.text.strip():
                    break
                html = resp.text
                discovered: list[dict] = []
                for m in ATHLETE_CARD_RE.finditer(html):
                    try:
                        aid = int(m.group("id"))
                    except Exception:
                        continue
                    discovered.append({
                        "athlete_id": aid,
                        "name": " ".join(unescape(m.group("name")).split()),
                        "avatar_url": unescape(m.group("avatar")) or None,
                    })
                if not discovered:
                    for m in ATHLETE_LIST_ITEM_RE.finditer(html):
                        try:
                            aid = int(m.group("id"))
                        except Exception:
                            continue
                        body = m.group("body")
                        nm = ATHLETE_LINK_RE.search(body)
                        if not nm:
                            continue
                        discovered.append({
                            "athlete_id": aid,
                            "name": " ".join(unescape(nm.group("name")).split()),
                            "avatar_url": None,
                        })
                if not discovered:
                    break
                # Dedup + seed spider queue.
                for entry in discovered:
                    if entry["athlete_id"] in seen:
                        continue
                    seen.add(entry["athlete_id"])
                    try:
                        async with self.pool.acquire() as conn:
                            await conn.execute(
                                """
                                INSERT INTO strava_spider_queue (
                                    platform_athlete_id, source, priority, status
                                ) VALUES ($1, 'following', 5, 'pending')
                                ON CONFLICT (platform_athlete_id) DO NOTHING
                                """,
                                entry["athlete_id"],
                            )
                            # Stub athlete row so FKs resolve later.
                            await conn.execute(
                                """
                                INSERT INTO strava_athletes (
                                    platform_athlete_id, username, updated_at
                                ) VALUES ($1, $2, NOW())
                                ON CONFLICT (platform_athlete_id) DO NOTHING
                                """,
                                entry["athlete_id"],
                                entry["name"][:255] if entry.get("name") else None,
                            )
                        seeded += 1
                    except Exception as e:
                        logger.warning("strava roster: seed %s failed: %s",
                                       entry["athlete_id"], e)
                logger.info("strava roster: page %d -> %d athletes (running seeded=%d)",
                            page, len(discovered), seeded)
                page += 1
        logger.info("strava roster: seeded %d new spider entries from %s", seeded, athlete_id)
        return seeded
