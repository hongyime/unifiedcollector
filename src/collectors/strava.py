import asyncio
import json
import logging
import os
import random
import re
from datetime import date, datetime, timedelta

import httpx

from src.core.base_collector import BaseCollector
from src.core.profile_photo_tracker import ProfilePhotoTracker

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
        self._access_token = ""
        self._sem = asyncio.Semaphore(2)

        self._api_delay_min = float(os.getenv("STRAVA_API_DELAY_MIN", "5.0"))
        self._api_delay_max = float(os.getenv("STRAVA_API_DELAY_MAX", "10.0"))
        self._feed_delay_min = float(os.getenv("STRAVA_FEED_DELAY_MIN", "5.0"))
        self._feed_delay_max = float(os.getenv("STRAVA_FEED_DELAY_MAX", "12.0"))
        self._backfill_steps = int(os.getenv("STRAVA_BACKFILL_STEPS", "25"))
        self._backfill_parallelism = int(os.getenv("STRAVA_BACKFILL_PARALLELISM", "3"))

        self._use_api = bool(self._client_id and self._client_secret and self._refresh_token)
        self._use_web = bool(self._session_cookie)
        self._photo_tracker = ProfilePhotoTracker()
        self._gps_enabled = os.getenv("STRAVA_GPS_ENABLED", "false").lower() == "true"
        self._follow_scrape_enabled = os.getenv("STRAVA_FOLLOW_SCRAPE_ENABLED", "false").lower() == "true"

    def set_pool(self, pool):
        super().set_pool(pool)
        self._photo_tracker.set_pool(pool)

    async def _delay(self, min_s: float | None = None, max_s: float | None = None):
        lo = min_s or self._api_delay_min
        hi = max_s or self._api_delay_max
        await asyncio.sleep(random.uniform(lo, hi))

    async def _ensure_token(self):
        if self._access_token:
            return
        if not self._use_api:
            return

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(TOKEN_URL, data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": self._refresh_token,
                "grant_type": "refresh_token",
            })
            resp.raise_for_status()
            data = resp.json()
            self._access_token = data["access_token"]
            if data.get("refresh_token"):
                self._refresh_token = data["refresh_token"]
            logger.info("Strava token refreshed")

    def _api_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token}"}

    def _web_headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.user_agents.get_for_domain("strava.com"),
            "Cookie": f"_strava4_session={self._session_cookie}",
            "Accept": "text/html,application/xhtml+xml",
        }

    async def collect(self, targets: list[str]):
        if self._use_api:
            await self._ensure_token()

        for target in targets:
            if self._stop.is_set():
                break
            logger.info("Collecting strava/%s", target)
            try:
                if target.lower() == "me" and self._use_api:
                    await self._collect_authenticated_athlete()
                elif target.lower() == "feed" and self._use_web:
                    await self._collect_feed()
                elif self._use_api:
                    await self._collect_athlete(target)
                elif self._use_web:
                    await self._collect_athlete_web(target)
                else:
                    logger.error("No auth configured for Strava (need OAuth or session cookie)")
                    return
                await self.checkpoint.save_progress(target)
            except Exception as e:
                logger.error("Failed strava/%s: %s", target, e)
                await self.send_to_dlq(target, target, str(e))

    async def _collect_authenticated_athlete(self):
        async with httpx.AsyncClient(timeout=30) as client:
            await self._delay()
            resp = await client.get(f"{STRAVA_API}/athlete", headers=self._api_headers())
            resp.raise_for_status()
            athlete = resp.json()
            self.rate_limiter.record_success("strava.com")

            aid = str(athlete["id"])
            aname = athlete.get("username") or f"{athlete.get('firstname', '')}_{athlete.get('lastname', '')}".strip("_")

            if athlete.get("profile"):
                changed, path = await self._photo_tracker.check_and_download(
                    athlete["profile"], aid, "strava", self.media_dir / "profiles",
                )
                if changed and path:
                    data = path.read_bytes()
                    await self.insert_media_item(
                        entity_id=aid, entity_name=aname,
                        content_type="profile_photo", content_id=f"profile_{aid}",
                        filename=path.name, file_path=str(path),
                        file_size=len(data), sha256=self.sha256_bytes(data),
                    )
                elif not changed:
                    cid = f"profile_{aid}"
                    if not self.is_known(cid):
                        await self.download_media({
                            "entity_id": aid, "entity_name": aname,
                            "content_type": "profile_photo", "content_id": cid,
                            "url": athlete["profile"], "extension": "jpg",
                        })

            await self._collect_activities_api(client, aid, aname)

            if self._follow_scrape_enabled and self._use_web:
                await self._scrape_following(client, aid)

    async def _collect_athlete(self, athlete_id: str):
        async with httpx.AsyncClient(timeout=30) as client:
            await self._delay()
            resp = await client.get(
                f"{STRAVA_API}/athletes/{athlete_id}/stats",
                headers=self._api_headers(),
            )
            if resp.status_code == 200:
                self.rate_limiter.record_success("strava.com")
            await self._collect_activities_api(client, athlete_id, athlete_id)

    async def _collect_activities_api(self, client: httpx.AsyncClient, aid: str, aname: str):
        page = 1
        per_page = 50

        while not self._stop.is_set():
            await self._delay()

            async with self._sem:
                resp = await client.get(
                    f"{STRAVA_API}/athlete/activities",
                    headers=self._api_headers(),
                    params={"page": page, "per_page": per_page},
                )

            if resp.status_code == 429:
                logger.warning("Strava rate limit hit")
                self.rate_limiter.record_failure("strava.com")
                await asyncio.sleep(60)
                continue

            resp.raise_for_status()
            activities = resp.json()
            self.rate_limiter.record_success("strava.com")

            if not activities:
                break

            for activity in activities:
                if self._stop.is_set():
                    break
                await self._collect_activity_photos(client, activity, aid, aname)
                if self._gps_enabled:
                    await self._collect_gps_streams(client, activity, aid)

            page += 1

    async def _collect_activity_photos(self, client: httpx.AsyncClient,
                                        activity: dict, aid: str, aname: str):
        activity_id = str(activity["id"])
        photos = activity.get("total_photo_count", 0)
        if not photos:
            return

        await self._delay()
        photo_resp = await client.get(
            f"{STRAVA_API}/activities/{activity_id}/photos",
            headers=self._api_headers(),
            params={"size": 2048},
        )
        if photo_resp.status_code != 200:
            return

        photo_list = photo_resp.json()
        for i, photo in enumerate(photo_list):
            urls = photo.get("urls", {})
            url = urls.get("2048") or urls.get("600") or urls.get("100")
            if not url:
                continue

            cid = f"{activity_id}_{i}"
            if self.is_known(cid):
                continue

            await self.download_media({
                "entity_id": aid,
                "entity_name": aname,
                "content_type": "activity",
                "content_id": cid,
                "url": url,
                "extension": "jpg",
                "source_url": f"https://www.strava.com/activities/{activity_id}",
                "metadata": json.dumps({
                    "activity_name": activity.get("name"),
                    "activity_type": activity.get("type"),
                }),
            })

    async def _collect_feed(self):
        """Web scraping mode: collect from the logged-in athlete's feed."""
        if not self._use_web:
            return

        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            await self._delay(self._feed_delay_min, self._feed_delay_max)
            resp = await client.get(
                f"{STRAVA_WEB}/dashboard",
                headers=self._web_headers(),
            )
            if resp.status_code != 200:
                logger.warning("Feed fetch failed: %s", resp.status_code)
                return

            html = resp.text
            activity_ids = re.findall(r'/activities/(\d+)', html)
            activity_ids = list(dict.fromkeys(activity_ids))

            logger.info("Feed: found %d activities", len(activity_ids))

            for act_id in activity_ids:
                if self._stop.is_set():
                    break
                await self._scrape_activity(client, act_id)

    async def _scrape_activity(self, client: httpx.AsyncClient, activity_id: str):
        await self._delay(self._feed_delay_min, self._feed_delay_max)
        resp = await client.get(
            f"{STRAVA_WEB}/activities/{activity_id}",
            headers=self._web_headers(),
        )
        if resp.status_code != 200:
            return

        html = resp.text
        athlete_match = re.search(r'/athletes/(\d+)', html)
        aid = athlete_match.group(1) if athlete_match else "unknown"
        name_match = re.search(r'class="athlete-name"[^>]*>([^<]+)', html)
        aname = name_match.group(1).strip() if name_match else aid

        photo_urls = re.findall(r'"(https://dgtzuqphqg23d\.cloudfront\.net/[^"]+)"', html)
        photo_urls += re.findall(r'"(https://d3nn82uaxijpm6\.cloudfront\.net/[^"]+)"', html)
        photo_urls = list(dict.fromkeys(photo_urls))

        for i, url in enumerate(photo_urls):
            cid = f"{activity_id}_{i}"
            if self.is_known(cid):
                continue
            await self.download_media({
                "entity_id": aid,
                "entity_name": aname,
                "content_type": "activity",
                "content_id": cid,
                "url": url,
                "extension": "jpg",
                "source_url": f"https://www.strava.com/activities/{activity_id}",
            })

    async def _collect_athlete_web(self, athlete_id: str):
        """Web scraping fallback for collecting a specific athlete's activities."""
        if not self._use_web:
            return

        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            await self._delay(self._feed_delay_min, self._feed_delay_max)
            resp = await client.get(
                f"{STRAVA_WEB}/athletes/{athlete_id}",
                headers=self._web_headers(),
            )
            if resp.status_code != 200:
                return

            html = resp.text
            activity_ids = re.findall(r'/activities/(\d+)', html)
            activity_ids = list(dict.fromkeys(activity_ids))

            for act_id in activity_ids[:self._backfill_steps]:
                if self._stop.is_set():
                    break
                await self._scrape_activity(client, act_id)

    async def _scrape_following(self, client: httpx.AsyncClient, athlete_id: str):
        page = 1
        discovered = []
        while not self._stop.is_set():
            await self._delay(self._feed_delay_min, self._feed_delay_max)
            try:
                resp = await client.get(
                    f"{STRAVA_WEB}/athletes/{athlete_id}/follows",
                    params={"page": page, "type": "following"},
                    headers=self._web_headers(),
                )
                if resp.status_code != 200:
                    break
                html = resp.text
                if not html.strip():
                    break

                found = set()
                for pattern in [
                    re.compile(r'class="following[^"]*"[^>]*href="/athletes/(\d+)"'),
                    re.compile(r'"athleteId"\s*:\s*(\d+)'),
                    re.compile(r'href="/athletes/(\d+)"'),
                ]:
                    for m in pattern.finditer(html):
                        found.add(m.group(1))

                if not found:
                    try:
                        marker = '__NEXT_DATA__'
                        idx = html.find(marker)
                        if idx != -1:
                            script_start = html.find('>', idx) + 1
                            script_end = html.find('</script>', script_start)
                            data = json.loads(html[script_start:script_end])
                            athletes = data.get("props", {}).get("pageProps", {}).get("athletes", [])
                            for a in athletes:
                                aid = str(a.get("athleteId", a.get("id", "")))
                                if aid:
                                    found.add(aid)
                    except Exception:
                        pass

                if not found:
                    break

                discovered.extend(found)
                page += 1
                self.rate_limiter.record_success("strava.com")

            except Exception as e:
                logger.debug("Following page %d failed: %s", page, e)
                break

        logger.info("Discovered %d athletes from following list of %s", len(discovered), athlete_id)

        for aid in discovered[:50]:
            if self._stop.is_set():
                break
            try:
                await self._collect_athlete_web(aid)
            except Exception as e:
                logger.debug("Discovered athlete %s failed: %s", aid, e)

    async def _collect_gps_streams(self, client: httpx.AsyncClient, activity: dict, aid: str):
        activity_id = str(activity["id"])
        activity_date = activity.get("start_date_local", "")[:10]

        if self._pool and activity_date:
            try:
                async with self._pool.acquire() as conn:
                    exists = await conn.fetchval(
                        "SELECT 1 FROM strava_gps_streams WHERE activity_id = $1",
                        activity_id,
                    )
                    if exists:
                        return
            except Exception:
                pass

        try:
            await self._delay()
            resp = await client.get(
                f"{STRAVA_API}/activities/{activity_id}/streams",
                headers=self._api_headers(),
                params={"keys": "latlng,time,altitude", "key_by_type": "true"},
            )
            if resp.status_code != 200:
                return

            streams = resp.json()
            self.rate_limiter.record_success("strava.com")

            if not self._pool:
                return

            async with self._pool.acquire() as conn:
                for stream_type in ("latlng", "time", "altitude"):
                    if stream_type in streams:
                        await conn.execute(
                            """
                            INSERT INTO strava_gps_streams
                                (athlete_id, activity_id, stream_type, data)
                            VALUES ($1, $2, $3, $4)
                            ON CONFLICT DO NOTHING
                            """,
                            aid, activity_id, stream_type,
                            json.dumps(streams[stream_type].get("data", [])),
                        )

                if activity_date:
                    await conn.execute(
                        """
                        INSERT INTO strava_day_coverage (athlete_id, date, has_data)
                        VALUES ($1, $2, true)
                        ON CONFLICT (athlete_id, date) DO UPDATE SET has_data = true
                        """,
                        aid, date.fromisoformat(activity_date),
                    )

        except Exception as e:
            logger.debug("GPS stream fetch failed for activity %s: %s", activity_id, e)

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
            await self._delay()
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                resp = await client.get(item["url"])
                resp.raise_for_status()
                data = resp.content

            sha = self.sha256_bytes(data)
            self.save_file(data, filename)
            self.rate_limiter.record_success("strava.com")
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
                metadata=item.get("metadata"),
            )
        except Exception as e:
            self.rate_limiter.record_failure("strava.com")
            self.circuit_breaker.record_failure()
            logger.error("Download failed %s: %s", cid, e)
            await self.send_to_dlq(item["entity_id"], cid, str(e))
