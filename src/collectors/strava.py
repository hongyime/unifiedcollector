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
                    await self._collect_via_cookies()
                elif target.lower() == "feed" and self._use_web: await self._collect_feed()
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

        async with httpx.AsyncClient(timeout=30, cookies=jar, follow_redirects=True,
                                     headers={"User-Agent": ua}) as client:
            # 1) Resolve current athlete via /api/v3/athlete (web cookie works here).
            athlete_id = None
            athlete_name = "me"
            try:
                resp = await client.get(f"{STRAVA_API}/athlete", headers=base_headers)
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

            # Fallback: pull athlete id from dashboard HTML hydration if needed.
            if not athlete_id:
                try:
                    dash = await client.get(f"{STRAVA_WEB}/dashboard",
                                            headers={"User-Agent": ua,
                                                     "Accept": "text/html,application/xhtml+xml"})
                    if dash.status_code == 200:
                        m = re.search(r'"athlete_id"\s*:\s*(\d+)', dash.text) or \
                            re.search(r'/athletes/(\d+)', dash.text)
                        if m:
                            athlete_id = m.group(1)
                            logger.info("strava: resolved athlete_id=%s via dashboard hydration", athlete_id)
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
                    resp = await client.get(
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
                        },
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

    @staticmethod
    def _normalize_training_activity(raw: dict) -> dict:
        """Map Strava /athlete/training_activities payload fields to the
        shape _upsert_activity expects (compatible with /api/v3/athlete/activities)."""
        # raw fields seen: id, name, type, distance ("9.99mi" or meters), moving_time
        # ("1h 2m" or seconds), elapsed_time, start_date_local, total_elevation_gain,
        # start_date_local_raw (epoch). Numeric fields are sometimes strings with units.
        def _num(v):
            if v is None: return None
            if isinstance(v, (int, float)): return v
            if isinstance(v, str):
                m = re.match(r"^\s*([\d.]+)", v)
                if m:
                    try: return float(m.group(1))
                    except Exception: return None
            return None

        def _seconds(v):
            if v is None: return None
            if isinstance(v, (int, float)): return int(v)
            if isinstance(v, str):
                # "1h 2m 3s" or "62:30" or "3600"
                if v.isdigit(): return int(v)
                total = 0
                for n, unit in re.findall(r"(\d+)\s*([hms])", v):
                    n = int(n)
                    total += n * (3600 if unit == "h" else 60 if unit == "m" else 1)
                if total: return total
                # mm:ss or hh:mm:ss
                parts = v.split(":")
                if all(p.strip().isdigit() for p in parts):
                    parts = [int(p) for p in parts]
                    if len(parts) == 2: return parts[0] * 60 + parts[1]
                    if len(parts) == 3: return parts[0] * 3600 + parts[1] * 60 + parts[2]
            return None

        start = raw.get("start_date_local") or raw.get("start_date")
        # Convert epoch raw if that's all we have, or parse human format like "Wed, 4/15/2026".
        epoch_raw = raw.get("start_date_local_raw") or raw.get("start_date_raw")
        if epoch_raw:
            try:
                start = datetime.fromtimestamp(int(epoch_raw), tz=timezone.utc).isoformat()
            except Exception:
                pass
        if isinstance(start, str) and start and not re.match(r"^\d{4}-\d{2}-\d{2}", start):
            # Strava web returns strings like "Wed, 4/15/2026" — convert to ISO date.
            try:
                # strip leading weekday + comma
                s = re.sub(r"^[A-Za-z]+,\s*", "", start).strip()
                dt = datetime.strptime(s, "%m/%d/%Y")
                start = dt.replace(tzinfo=timezone.utc).isoformat()
            except Exception:
                start = None
        # Ensure ISO with no trailing Z handler issues.
        if isinstance(start, str) and start.endswith("Z"):
            start = start  # _upsert_activity strips Z itself

        return {
            "id": raw.get("id"),
            "name": raw.get("name"),
            "type": raw.get("type") or raw.get("activity_type"),
            "sport_type": raw.get("sport_type") or raw.get("type"),
            "distance": _num(raw.get("distance")) or _num(raw.get("distance_raw")),
            "moving_time": _seconds(raw.get("moving_time")) or _seconds(raw.get("moving_time_raw")),
            "elapsed_time": _seconds(raw.get("elapsed_time")) or _seconds(raw.get("elapsed_time_raw")),
            "total_elevation_gain": _num(raw.get("elevation_gain")) or _num(raw.get("elevation_gain_raw"))
                or _num(raw.get("total_elevation_gain")),
            "average_speed": _num(raw.get("average_speed")),
            "max_speed": _num(raw.get("max_speed")),
            "average_heartrate": _num(raw.get("average_heartrate")),
            "calories": _num(raw.get("calories")),
            "start_date": start,
        }

    async def _collect_authenticated_athlete(self):
        async with httpx.AsyncClient(timeout=30) as client:
            await self._delay()
            resp = await client.get(f"{STRAVA_API}/athlete", headers={"Authorization": f"Bearer {self._access_token}"})
            resp.raise_for_status()
            athlete = resp.json()
            await self._upsert_athlete(athlete)
            aid, aname = str(athlete["id"]), athlete.get("username", str(athlete["id"]))
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
                    username = EXCLUDED.username, profile = EXCLUDED.profile,
                    follower_count = EXCLUDED.follower_count, updated_at = NOW()
            """, athlete.get("id"), athlete.get("username"), athlete.get("firstname"), athlete.get("lastname"), athlete.get("profile"), athlete.get("city"), athlete.get("state"), athlete.get("country"), athlete.get("sex"), athlete.get("follower_count", 0), athlete.get("friend_count", 0))

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
                await self._collect_activity_photos(client, activity, aid, aname)
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

    async def cleanup(self):
        pass
