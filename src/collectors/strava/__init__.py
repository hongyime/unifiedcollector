import asyncio
import http.cookiejar
import json
import logging
import os
import random
import re
import time
from html import unescape
from pathlib import Path
from datetime import datetime, timedelta, timezone

import httpx

from src.core.base_collector import BaseCollector
from src.collectors.strava.parse import (
    normalize_training_activity as _parse_normalize_training_activity,
    normalize_feed_activity as _parse_normalize_feed_activity,
)
from src.core.profile_photo_tracker import ProfilePhotoTracker
from src.core.file_naming import sanitize_name
from src.core.proximity import refresh_account_proximity_cache
from src.core.raw_archive import report_raw_archive_result
from src.core.rate_limit_events import record_rate_limit_event
from src.core.scrape_pacing import sleep_before_pre_cooldown_retry, sleep_rate_limit
from src.core.vault import VAULT_ROOT, write_atomic_artifact, write_raw_payload

logger = logging.getLogger(__name__)

STRAVA_API = "https://www.strava.com/api/v3"
TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_WEB = "https://www.strava.com"

# Privacy-zone truncation: distance (m) beyond which a summary start/end is
# considered to have been clipped by a Strava privacy zone vs the GPS track.
_PRIVACY_ZONE_THRESHOLD_M = 50.0
_GPS_429_EVENT_DEDUPE_SECONDS = 120.0


def _clean_strava_text(value) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", unescape(str(value))).strip()
    return text or None


def _compact_count_to_int(value) -> int | None:
    text = _clean_strava_text(value)
    if not text:
        return None
    text = text.replace(",", "")
    m = re.match(r"^(\d+(?:\.\d+)?)([kKmM])?$", text)
    if not m:
        return None
    number = float(m.group(1))
    suffix = (m.group(2) or "").lower()
    if suffix == "k":
        number *= 1_000
    elif suffix == "m":
        number *= 1_000_000
    return int(number)


def _html_meta_content(html: str, name: str) -> str | None:
    for m in re.finditer(r"<meta\b[^>]*>", html, re.IGNORECASE):
        tag = m.group(0)
        if not re.search(
            rf"(?:property|name)\s*=\s*['\"]{re.escape(name)}['\"]",
            tag,
            re.IGNORECASE,
        ):
            continue
        content = re.search(r"content\s*=\s*(['\"])(.*?)\1", tag, re.IGNORECASE | re.DOTALL)
        if content:
            return _clean_strava_text(content.group(2))
    return None


def _strip_strava_profile_title(value: str | None) -> str | None:
    text = _clean_strava_text(value)
    if not text:
        return None
    text = re.sub(r"\s*\|\s*Strava\b.*$", "", text, flags=re.IGNORECASE).strip()
    if re.match(r"^sign\s*up for free\b", text, re.IGNORECASE):
        return None
    if re.match(r"^signup for free\b", text, re.IGNORECASE):
        return None
    return text or None


def _split_display_name(value: str | None) -> tuple[str | None, str | None]:
    text = _clean_strava_text(value)
    if not text:
        return None, None
    parts = text.split(None, 1)
    return parts[0], parts[1] if len(parts) > 1 else None


def _extract_strava_profile_from_html(html: str, athlete_id: str | int) -> dict:
    """Extract basic athlete profile fields from Strava's server-rendered page."""
    profile: dict = {"id": athlete_id}
    target_start = html.find("id='athlete-profile'")
    if target_start < 0:
        target_start = html.find('id="athlete-profile"')
    target_html = html[target_start:target_start + 220000] if target_start >= 0 else html

    name = None
    m = re.search(
        r"<h1\b[^>]*class=['\"][^'\"]*athlete-name[^'\"]*['\"][^>]*>(.*?)</h1>",
        target_html,
        re.IGNORECASE | re.DOTALL,
    )
    if m:
        name = _clean_strava_text(re.sub(r"<[^>]+>", "", m.group(1)))
    if not name:
        name = _strip_strava_profile_title(_html_meta_content(html, "og:title"))
    if not name:
        title = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        name = _strip_strava_profile_title(title.group(1) if title else None)
    if name:
        first, last = _split_display_name(name)
        profile.update({"username": name, "firstname": first, "lastname": last})

    image = _html_meta_content(html, "og:image") or _html_meta_content(html, "twitter:image")
    if not image:
        avatar = re.search(
            r"<img\b[^>]*class=['\"][^'\"]*avatar-img[^'\"]*['\"][^>]*src=['\"]([^'\"]+)",
            target_html,
            re.IGNORECASE | re.DOTALL,
        )
        if avatar:
            image = _clean_strava_text(avatar.group(1))
    if not image:
        props = re.search(
            r"data-react-class=['\"]AvatarWrapper['\"][^>]*data-react-props=(['\"])(.*?)\1",
            target_html,
            re.IGNORECASE | re.DOTALL,
        )
        if props:
            try:
                payload = json.loads(unescape(props.group(2)))
                image = _clean_strava_text(payload.get("src"))
            except Exception:
                image = None
    if image:
        profile["profile"] = image

    for label, key in (("Followers", "follower_count"), ("Following", "following_count")):
        stat = re.search(
            rf"<span\b[^>]*>\s*{label}\s*</span>\s*(?:<a\b[^>]*>|<strong\b[^>]*>)\s*([^<]+)",
            target_html,
            re.IGNORECASE,
        )
        count = _compact_count_to_int(stat.group(1) if stat else None)
        if count is not None:
            profile[key] = count

    desc = _html_meta_content(html, "og:description") or _html_meta_content(html, "twitter:description")
    if desc:
        loc = re.search(r"\bis an? [^.]*? from ([^.]+?)\. Join Strava", desc, re.IGNORECASE)
        if loc:
            parts = [_clean_strava_text(p) for p in loc.group(1).split(",")]
            parts = [p for p in parts if p]
            if len(parts) >= 2:
                profile["city"] = parts[0]
                profile["country"] = parts[-1]
            elif parts:
                profile["country"] = parts[0]

    return profile


def _haversine_m(lat1, lon1, lat2, lon2) -> float:
    """Great-circle distance in metres between two lat/lon points."""
    import math
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _coerce_latlng_point(value):
    """Return a [lat, lng] list from Strava API arrays or stored 'lat,lng' strings."""
    if value is None:
        return None
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        if raw.startswith("["):
            try:
                return _coerce_latlng_point(json.loads(raw))
            except Exception:
                return None
        parts = [p.strip() for p in raw.split(",", 1)]
        if len(parts) != 2:
            return None
        try:
            return [float(parts[0]), float(parts[1])]
        except (TypeError, ValueError):
            return None
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            return [float(value[0]), float(value[1])]
        except (TypeError, ValueError):
            return None
    return None


def _format_latlng_point(value) -> str | None:
    point = _coerce_latlng_point(value)
    if not point:
        return None
    return f"{point[0]},{point[1]}"


def _coerce_latlng_stream(value) -> list[list[float]]:
    if value is None:
        return []
    if isinstance(value, str):
        raw = value.strip()
        if not raw or raw == "null":
            return []
        try:
            value = json.loads(raw)
        except Exception:
            return []
    if not isinstance(value, (list, tuple)):
        return []
    out: list[list[float]] = []
    for item in value:
        point = _coerce_latlng_point(item)
        if point:
            out.append(point)
    return out


def _encode_polyline(points, precision: int = 5) -> str:
    """Encode a list of [lat,lng] points into a Google encoded polyline string.

    The Strava /api/v3 summary_polyline is unreachable in cookie mode (401), so we
    DERIVE it from the GPS track we already scrape via the web XHR endpoint. The
    analyzer's map can then use the compact polyline OR the full gps_streams.latlng.
    Down-sampled to a manageable point count (it's an overview line, not the track).
    """
    if not points:
        return ""
    # down-sample evenly to <= MAX points (full detail stays in gps_streams.latlng)
    MAX = 600
    if len(points) > MAX:
        step = len(points) / MAX
        points = [points[int(i * step)] for i in range(MAX)]
    factor = 10 ** precision

    def _enc(v: int) -> str:
        v = v << 1 if v >= 0 else ~(v << 1)
        out = []
        while v >= 0x20:
            out.append(chr((0x20 | (v & 0x1F)) + 63))
            v >>= 5
        out.append(chr(v + 63))
        return "".join(out)

    res = []
    prev_lat = prev_lng = 0
    for pt in points:
        if not pt or len(pt) != 2:
            continue
        lat_i = int(round(pt[0] * factor))
        lng_i = int(round(pt[1] * factor))
        res.append(_enc(lat_i - prev_lat))
        res.append(_enc(lng_i - prev_lng))
        prev_lat, prev_lng = lat_i, lng_i
    return "".join(res)


def _is_truncated(summary_latlng, track_point) -> bool:
    """True if the GPS track start/end was hidden by a privacy zone.

    summary_latlng: the API summary [lat, lng] (empty/None when Strava omitted it).
    track_point:    the first/last [lat, lng] of the actual GPS stream.
    Privacy zone => either the summary was omitted entirely (but a track exists),
    or the summary point sits >threshold metres from the real track endpoint.
    """
    track = _coerce_latlng_point(track_point)
    summary = _coerce_latlng_point(summary_latlng)
    if not track:
        return False
    if not summary:
        # Summary omitted but a real track point exists => the endpoint was hidden.
        return True
    return _haversine_m(
        summary[0], summary[1], track[0], track[1]
    ) > _PRIVACY_ZONE_THRESHOLD_M


def _derive_gps_route_fields(summary_start, summary_end, latlng_data) -> dict:
    points = _coerce_latlng_stream(latlng_data)
    stream_status = "ok" if points else "truncated_empty"
    start = end = trunc_start = trunc_end = None
    privacy_start = privacy_end = False

    if points:
        start = _format_latlng_point(points[0])
        end = _format_latlng_point(points[-1])
        privacy_start = _is_truncated(summary_start, points[0])
        privacy_end = _is_truncated(summary_end, points[-1])
        trunc_start = start if privacy_start else None
        trunc_end = end if privacy_end else None
    else:
        # Fully hidden routes can still expose a privacy-safe summary start.
        summary = _format_latlng_point(summary_start)
        if summary:
            privacy_start = True
            trunc_start = summary

    return {
        "start_latlng": start,
        "end_latlng": end,
        "stream_status": stream_status,
        "privacy_zone_start": privacy_start,
        "privacy_zone_end": privacy_end,
        "truncation_point_start": trunc_start,
        "truncation_point_end": trunc_end,
        "summary_polyline": _encode_polyline(points) if points else None,
        "point_count": len(points),
    }


def _tier1_raw_archives_enabled() -> bool:
    raw = os.getenv("COLLECTOR_TIER1_RAW_PAYLOADS_ENABLED", "1")
    return raw.strip().lower() not in {"0", "false", "no", "off"}


class StravaCollector(BaseCollector):
    SOURCE_NAME = "strava"

    def __init__(self):
        super().__init__()
        self._client_id = os.getenv("STRAVA_CLIENT_ID", "")
        self._client_secret = os.getenv("STRAVA_CLIENT_SECRET", "")
        self._refresh_token = os.getenv("STRAVA_REFRESH_TOKEN", "")
        self._session_cookie = os.getenv("STRAVA_SESSION_COOKIE", "")
        self._cookies_file = os.getenv("STRAVA_COOKIES_FILE", "").strip()
        # Fallback: read session cookie from a Netscape cookie jar.
        # User can drop credentials/strava/strava_<username>.txt files (or
        # the legacy credentials/strava/strava_cookies.txt) and we auto-
        # discover them via _load_all_cookie_accounts below.
        if not self._session_cookie and self._cookies_file:
            self._session_cookie = self._load_session_cookie_from_file(self._cookies_file)
        # Multi-account: discover every credentials/strava/strava_*.txt (named by
        # username) so we can rotate across accounts for more quota AND broader
        # visibility — an activity (and its GPS) hidden from one account may be
        # visible to another that follows that athlete. [(name, session_cookie)].
        self._cookie_accounts = self._load_all_cookie_accounts()
        if not self._session_cookie and self._cookie_accounts:
            self._session_cookie = self._cookie_accounts[0][1]
        if self._cookie_accounts:
            logger.info("strava: %d cookie account(s) loaded: %s",
                        len(self._cookie_accounts),
                        ", ".join(n for n, _ in self._cookie_accounts))
        self._access_token = ""
        self._sem = asyncio.Semaphore(2)

        # Trimmed 5/10 -> 3/6: the web cookie path tolerates more than the strict
        # 100-req/15min OAuth API ceiling, and we now spread load across multiple
        # cookie accounts. Auto-backoff still kicks in on 429. Override via env.
        self._api_delay_min = float(os.getenv("STRAVA_API_DELAY_MIN", "3.0"))
        self._api_delay_max = float(os.getenv("STRAVA_API_DELAY_MAX", "6.0"))
        self._feed_delay_min = float(os.getenv("STRAVA_FEED_DELAY_MIN", "5.0"))
        self._feed_delay_max = float(os.getenv("STRAVA_FEED_DELAY_MAX", "12.0"))
        self._backfill_steps = int(os.getenv("STRAVA_BACKFILL_STEPS", "25"))
        self._ratelimit_sleep = int(os.getenv("STRAVA_RATELIMIT_SLEEP", "60"))
        self._gps_stream_cooldown_until = 0.0
        self._gps_stream_cooldown_seconds = int(
            os.getenv("STRAVA_STREAM_RATELIMIT_SLEEP", str(max(self._ratelimit_sleep, 1800)))
        )
        self._recent_gps_429s: dict[str, float] = {}

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

    def _archive_raw_payload(
        self,
        *,
        artifact_id: str,
        payload: dict | list,
        target_tables: list[str],
        metadata: dict | None = None,
    ) -> None:
        if not _tier1_raw_archives_enabled():
            return
        try:
            result = write_raw_payload(
                source=self.SOURCE_NAME,
                artifact_id=artifact_id,
                payload=payload,
                metadata=metadata or {},
                target_tables=target_tables,
                root=VAULT_ROOT,
            )
            report_raw_archive_result(
                self.pool,
                source=self.SOURCE_NAME,
                artifact_id=artifact_id,
                result=result,
                metadata=metadata,
                log=logger,
            )
        except Exception as exc:
            logger.debug("strava raw archive failed for %s: %s", artifact_id, exc)
            report_raw_archive_result(
                self.pool,
                source=self.SOURCE_NAME,
                artifact_id=artifact_id,
                result=None,
                metadata=metadata,
                log=logger,
                error=str(exc),
            )

    def _gps_stream_cooling_down(self) -> bool:
        return time.time() < self._gps_stream_cooldown_until

    async def _sync_persisted_gps_stream_cooldown(self) -> bool:
        """Hydrate GPS cooldown from durable rate_limit_events after restarts."""
        if not self.pool:
            return False
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT created_at, cooldown_seconds, reason
                    FROM rate_limit_events
                    WHERE source = 'strava'
                      AND scope IN ('gps_streams', 'browser_strava_streams')
                      AND status_code = 429
                      AND cooldown_seconds IS NOT NULL
                      AND created_at + (cooldown_seconds * INTERVAL '1 second') > now()
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                )
        except Exception as exc:
            logger.debug("strava: persisted GPS cooldown check failed: %s", exc)
            return self._gps_stream_cooling_down()
        if not row:
            return self._gps_stream_cooling_down()

        cooldown_seconds = int(row["cooldown_seconds"] or 0)
        created_at = row["created_at"]
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        cooldown_until = created_at.astimezone(timezone.utc) + timedelta(seconds=cooldown_seconds)
        remaining = (cooldown_until - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            return self._gps_stream_cooling_down()
        self._gps_stream_cooldown_until = max(
            self._gps_stream_cooldown_until,
            time.time() + remaining,
        )
        logger.info(
            "strava: GPS stream cooldown restored from rate_limit_events for %ds (%s)",
            int(remaining),
            row["reason"] or "HTTP 429",
        )
        return True

    def _set_gps_stream_cooldown(self, activity_id: str | int, context: str) -> None:
        now = time.time()
        self._gps_stream_cooldown_until = max(
            self._gps_stream_cooldown_until,
            now + self._gps_stream_cooldown_seconds,
        )
        activity_key = str(activity_id)
        recent_at = self._recent_gps_429s.get(activity_key)
        is_duplicate = recent_at is not None and now - recent_at < _GPS_429_EVENT_DEDUPE_SECONDS
        log = logger.info if is_duplicate else logger.warning
        log(
            "strava: streams 429 for %s via %s; cooling GPS backfill for %ds%s",
            activity_id,
            context,
            self._gps_stream_cooldown_seconds,
            " (duplicate suppressed)" if is_duplicate else "",
        )
        if is_duplicate:
            return
        self._recent_gps_429s[activity_key] = now
        for key, ts in list(self._recent_gps_429s.items()):
            if now - ts > _GPS_429_EVENT_DEDUPE_SECONDS * 4:
                self._recent_gps_429s.pop(key, None)
        self._note_rate_limit(
            scope="gps_streams",
            account=context.split("web:", 1)[1] if context.startswith("web:") else None,
            cooldown_seconds=self._gps_stream_cooldown_seconds,
            reason=f"streams 429 for {activity_id} via {context}",
            metadata={"activity_id": str(activity_id), "context": context},
        )

    async def _retry_gps_stream_after_429(
        self,
        fetch,
        *,
        activity_id: str | int,
        context: str,
        account: str | None = None,
    ):
        retry_delay = await sleep_before_pre_cooldown_retry(
            "strava",
            "gps_streams",
            account=account,
            status_code=429,
            reason=f"activity={activity_id} via {context}",
        )
        if retry_delay is None:
            return None
        try:
            retry_resp = await fetch()
        except Exception as exc:
            logger.debug("strava: GPS stream pre-cooldown retry failed for %s via %s: %s", activity_id, context, exc)
            return None
        retry_status = getattr(retry_resp, "status_code", None)
        if retry_status != 429:
            self._note_rate_limit(
                scope="gps_streams",
                account=account,
                cooldown_seconds=None,
                cooldown_active=False,
                reason=f"streams 429 for {activity_id} via {context}; retry returned HTTP {retry_status}",
                metadata={
                    "activity_id": str(activity_id),
                    "context": context,
                    "pre_cooldown_retry": True,
                    "retry_status_code": retry_status,
                    "retry_delay_seconds": retry_delay,
                },
            )
        return retry_resp

    def _note_rate_limit(
        self,
        *,
        scope: str,
        account: str | None = None,
        cooldown_seconds: int | None = None,
        cooldown_active: bool = True,
        reason: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        if self.pool is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(
            record_rate_limit_event(
                self.pool,
                source="strava",
                account=account,
                scope=scope,
                status_code=429,
                cooldown_seconds=(cooldown_seconds or self._ratelimit_sleep) if cooldown_active else None,
                reason=reason,
                metadata=metadata,
            )
        )

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

    def _load_all_cookie_accounts(self) -> list[tuple[str, str]]:
        """Discover all strava_*.txt cookie files (one per account, named by
        username) and load each account's _strava4_session. The explicit
        STRAVA_SESSION_COOKIE / STRAVA_COOKIES_FILE is the primary first entry.
        Deduped by cookie value. Never raises."""
        import glob
        cookie_dir = os.path.dirname(self._cookies_file) or "credentials/strava"
        accounts: list[tuple[str, str]] = []
        seen: set[str] = set()
        if self._session_cookie and self._session_cookie not in seen:
            seen.add(self._session_cookie)
            accounts.append(("primary", self._session_cookie))
        candidates: list[str] = []
        if self._cookies_file and os.path.exists(self._cookies_file):
            candidates.append(self._cookies_file)
        try:
            for p in sorted(glob.glob(os.path.join(cookie_dir, "strava_*.txt"))):
                if p not in candidates:
                    candidates.append(p)
        except Exception:
            pass
        for p in candidates:
            cookie = self._load_session_cookie_from_file(p)
            if cookie and cookie not in seen:
                seen.add(cookie)
                name = os.path.splitext(os.path.basename(p))[0]
                if name.startswith("strava_"):
                    name = name[len("strava_"):]
                accounts.append((name or "default", cookie))
        return accounts

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
        if not self._use_api:
            logger.warning(
                "strava: running in cookie-only mode (no STRAVA_CLIENT_ID/"
                "CLIENT_SECRET/REFRESH_TOKEN). The following data will be "
                "unavailable: city/country fields, "
                "athlete profile details (weight/FTP), clubs, starred segments. "
                "GPS streams are still fetched via cookie-authenticated XHR."
            )
        # Previously disabled due to httpx → Z:/C: NTFS kernel D-state.
        # Root cause fixed: all mounts now on WSL2 ext4 named volumes.
        if self._use_api: await self._ensure_token()

        owner_ids_scraped: set[str] = set()
        if self._follow_scrape_enabled and self._use_web and self._cookie_accounts:
            try:
                owner_ids_scraped = await self._collect_owner_rosters_for_cookie_accounts()
            except Exception as e:
                logger.warning("strava: owner roster capture failed: %s", e)

        # --- EDIT 2026-07-13 (GPS drain starvation fix, additive) ---
        # The tail-end GPS backfill (bottom of collect()) almost never ran:
        # worker restarts every ~0.5-2h + the 7200s no-progress watchdog chopped
        # the cycle tail, so it started only 3x in 48h while history discovery
        # (which runs EARLY every cycle) kept adding stream_status=NULL rows.
        # Run a bounded batch FIRST so the drain is deterministic every cycle;
        # the tail call still runs as a second batch when the cycle completes.
        # Disable via STRAVA_GPS_BACKFILL_FIRST=false. Same pacing/requests as
        # the tail call (3-6s STRAVA_API_DELAY per activity) — no ban-risk delta.
        try:
            repair_batch = int(os.getenv("STRAVA_GPS_ROUTE_REPAIR_BATCH", "100"))
            await self._repair_existing_gps_stream_routes(batch_size=repair_batch)
        except Exception as e:
            logger.warning(
                "strava: GPS route repair failed: %s: %s",
                type(e).__name__,
                e,
                exc_info=True,
            )
        if self._gps_enabled and self._use_web and \
                os.getenv("STRAVA_GPS_BACKFILL_FIRST", "true").lower() == "true":
            try:
                gps_batch = int(os.getenv("STRAVA_GPS_BACKFILL_BATCH", "150"))
                await self._backfill_missing_gps_streams(batch_size=gps_batch)
            except Exception as e:
                logger.warning("strava: early GPS backfill failed: %s", e)
        # --- END EDIT (GPS-first drain) ---

        # Strava photo metadata can be discovered during long page/history crawls,
        # while BaseCollector.run_backfill() only fires after collect() returns.
        # Drain a small media batch at the front so activity photos converge even
        # when the crawl tail is cut by restarts/watchdogs.
        if os.getenv("STRAVA_PHOTO_BACKFILL_FIRST", "true").lower() == "true":
            try:
                photo_batch = int(os.getenv("STRAVA_PHOTO_BACKFILL_BATCH", "25"))
                await self._backfill_missing_photo_media(batch_size=photo_batch)
            except Exception as e:
                logger.warning("strava: early photo media backfill failed: %s", e)

        for target in targets:
            if self._stop.is_set(): break
            logger.info("Collecting strava/%s", target)
            try:
                if target.lower() == "me" and self._use_api:
                    await self._collect_authenticated_athlete()
                elif target.lower() == "me":
                    await asyncio.wait_for(self._collect_via_cookies(), timeout=900.0)
                elif target.lower() == "feed" and self._use_web:
                    if self._my_athlete_id:
                        logger.info("strava/feed: already collected via 'me' target; skipping")
                    else:
                        await asyncio.wait_for(self._collect_via_cookies(), timeout=900.0)
                elif self._use_api: await self._collect_athlete(target)
                elif self._use_web: await self._collect_athlete_web(target)
                else:
                    logger.warning("strava/%s: no auth available (no API creds, no session cookie); skipping", target)
                await self.checkpoint.save_progress(target)
            except Exception as e:
                logger.error("Failed strava/%s: %s", target, e)
                await self.send_to_dlq(target, target, str(e))

        # Scrape the authenticated user's following feed (recent activities from
        # people the logged-in athlete follows). Runs only when cookie auth
        # resolved a valid self._my_athlete_id during _collect_via_cookies().
        if self._my_athlete_id and self._use_web:
            try:
                await self._collect_following_feed()
            except Exception as e:
                logger.warning("strava: _collect_following_feed failed: %s", e)

            # Backfill own activity history with polylines + photos
            try:
                n = await self._backfill_athlete_history(self._my_athlete_id, year_cap=5)
                logger.info("strava: own history backfill complete, %d activities enriched", n)
            except Exception as e:
                logger.warning("strava: own history backfill failed: %s", e)

        if os.getenv("STRAVA_SPIDER_ENABLED", "true").lower() == "true":
            # Club spider runs hourly (clubs change slowly) — enqueues club members.
            if (time.time() - getattr(self, "_last_club_spider", 0)) > 3600:
                self._last_club_spider = time.time()
                try:
                    await self._spider_clubs()
                except Exception as e:
                    logger.warning("strava: club spider failed: %s", e)
            await self._process_spider_queue()

        # Enrich stub athletes (numeric-only names) from profile pages
        try:
            profile_batch = int(os.getenv("STRAVA_PROFILE_ENRICH_BATCH", "50"))
            await self._enrich_athlete_names(batch_size=profile_batch)
        except Exception as e:
            logger.warning("strava: athlete name enrichment failed: %s", e)

        # Recurring privacy-zone backfill for historical activities (API mode only).
        if self._use_api:
            try:
                await self._backfill_privacy_zones()
            except Exception as e:
                logger.warning("strava: privacy-zone backfill failed: %s", e)

        # Scrape activity pages for photos, polylines, kudos, comments
        page_batch = int(os.getenv("STRAVA_PAGE_SCRAPE_BATCH", "200"))
        try:
            await self._sync_persisted_gps_stream_cooldown()
            await self._scrape_activity_pages(batch_size=page_batch)
        except Exception as e:
            logger.warning("strava: activity page scraping failed: %s", e)
        try:
            photo_batch = int(os.getenv("STRAVA_PHOTO_BACKFILL_BATCH", "25"))
            await self._backfill_missing_photo_media(batch_size=photo_batch)
        except Exception as e:
            logger.warning("strava: photo media backfill failed: %s", e)

        # URGENT GPS recovery: re-fetch streams for activities that never got one
        # (the old wrong-auth bug left ~95% of activities with no GPS even though
        # they have a map on strava.com). Drains a bounded batch each cycle via
        # the corrected web-XHR path. Self-terminates once all are populated.
        if self._gps_enabled and self._use_web:
            try:
                gps_batch = int(os.getenv("STRAVA_GPS_BACKFILL_BATCH", "150"))
                await self._backfill_missing_gps_streams(batch_size=gps_batch)
            except Exception as e:
                logger.warning("strava: GPS backfill failed: %s", e)

        # Optional follow-roster expansion: when STRAVA_ROSTER_SEED_TARGETS
        # is set (comma-separated athlete IDs) we scrape /follows pages and
        # enqueue every discovered athlete into strava_spider_queue. Off by
        # default to avoid surprise BFS expansion. Requires cookie auth.
        if self._follow_scrape_enabled:
            roster_seeds = os.getenv("STRAVA_ROSTER_SEED_TARGETS", "").strip()
            seed_ids = [s.strip() for s in roster_seeds.split(",") if s.strip()] if roster_seeds else []
            if self._use_web and self._cookie_accounts and not owner_ids_scraped:
                try:
                    owner_ids_scraped = await self._collect_owner_rosters_for_cookie_accounts()
                except Exception as e:
                    logger.warning("strava: owner roster capture failed: %s", e)
            # Auto-seed from the authenticated athlete so we spider out from MY
            # own following/followers without needing a manual ID list (Bryan).
            if (
                self._my_athlete_id
                and self._my_athlete_id not in seed_ids
                and self._my_athlete_id not in owner_ids_scraped
            ):
                seed_ids.insert(0, self._my_athlete_id)
            if seed_ids and self._use_web:
                for sid in seed_ids:
                    if not sid or self._stop.is_set():
                        continue
                    try:
                        # For MY own athlete, capture BOTH sides of the graph (who I
                        # follow + who follows me) into social_users; for other seeds
                        # this is just following-based discovery.
                        await self.collect_following_roster(sid, "following")
                        if (
                            self._my_athlete_id
                            and str(sid) == str(self._my_athlete_id)
                            and str(sid) not in owner_ids_scraped
                        ):
                            await self.collect_following_roster(sid, "followers")
                    except Exception as e:
                        logger.warning("strava: roster expansion for %s failed: %s", sid, e)
            elif seed_ids and not self._use_web:
                logger.info("strava: roster expansion skipped (no session cookie / web auth); "
                            "set STRAVA_SESSION_COOKIE or cookies file to enable following/follower spider")

    async def _process_spider_queue(self):
        max_per_cycle = int(os.getenv("STRAVA_SPIDER_MAX_PER_CYCLE", "10"))
        await refresh_account_proximity_cache(self.pool)
        # Prune stale pending entries to prevent unbounded queue growth.
        ttl_days = int(os.getenv("SPIDER_QUEUE_TTL_DAYS", "30"))
        try:
            async with self.pool.acquire() as conn:
                deleted = await conn.execute(
                    "DELETE FROM strava_spider_queue WHERE status = 'pending' AND collected_at < NOW() - ($1 || ' days')::INTERVAL",
                    str(ttl_days),
                )
                if deleted != "DELETE 0":
                    logger.info("strava spider: pruned %s stale queue entries (TTL %dd)", deleted, ttl_days)
        except Exception as e:
            logger.debug("strava spider: queue TTL prune failed: %s", e)
        processed = 0
        while not self._stop.is_set() and processed < max_per_cycle:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow("""
                    UPDATE strava_spider_queue
                    SET status = 'processing'
                    WHERE id = (
                        SELECT q.id
                        FROM strava_spider_queue q
                        LEFT JOIN LATERAL (
                            SELECT MIN(ap.tier) AS proximity_tier
                            FROM account_proximity_cache ap
                            WHERE ap.platform = 'strava'
                              AND ap.account_id = q.platform_athlete_id::text
                        ) prox ON TRUE
                        WHERE q.status = 'pending'
                        ORDER BY
                            CASE
                                WHEN prox.proximity_tier IN (1, 2) THEN 2
                                WHEN prox.proximity_tier = 3 THEN 1
                                ELSE 0
                            END DESC,
                            q.priority ASC,
                            q.collected_at ASC
                        LIMIT 1
                    )
                    RETURNING platform_athlete_id
                """)
            if not row: break
            try:
                if self._use_api: await self._collect_athlete(str(row['platform_athlete_id']))
                elif self._use_web:
                    await self._collect_athlete_web(str(row['platform_athlete_id']))
                    await self._backfill_athlete_history(str(row['platform_athlete_id']))
                async with self.pool.acquire() as conn:
                    await conn.execute("UPDATE strava_spider_queue SET status = 'completed' WHERE platform_athlete_id = $1", row['platform_athlete_id'])
                processed += 1
            except Exception:
                async with self.pool.acquire() as conn:
                    await conn.execute("UPDATE strava_spider_queue SET status = 'failed' WHERE platform_athlete_id = $1", row['platform_athlete_id'])
                processed += 1
        if processed > 0:
            logger.info("strava spider: processed %d athletes this cycle (%d remaining)",
                        processed, max_per_cycle - processed)

    async def _enqueue_athlete(self, athlete_id, name=None, source="spider", priority: int = 6) -> None:
        """Add a discovered athlete (kudoer/commenter/club member/follower) to the
        spider queue so we crawl out through them. Gated by STRAVA_SPIDER_ENABLED."""
        if os.getenv("STRAVA_SPIDER_ENABLED", "true").lower() != "true":
            return
        try:
            aid = int(athlete_id)
        except (TypeError, ValueError):
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO strava_spider_queue (platform_athlete_id, source, priority, status)
                       VALUES ($1, $2, $3, 'pending') ON CONFLICT (platform_athlete_id) DO NOTHING""",
                    aid, source, priority,
                )
                await conn.execute(
                    """INSERT INTO strava_athletes (platform_athlete_id, username, updated_at)
                       VALUES ($1, $2, NOW()) ON CONFLICT (platform_athlete_id) DO NOTHING""",
                    aid, (name[:255] if name else None),
                )
        except Exception as e:
            logger.debug("strava enqueue athlete %s failed: %s", athlete_id, e)

    async def _spider_clubs(self) -> int:
        """Spider athletes through CLUBS (user: "spider through ... clubs"): find the
        logged-in athlete's clubs, scrape each club's member list, enqueue every
        member. Web/cookie mode only. Idempotent."""
        if os.getenv("STRAVA_SPIDER_ENABLED", "true").lower() != "true" or not self._use_web:
            return 0
        jar = self._build_cookie_jar()
        if jar is None:
            return 0
        ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
        member_pages = int(os.getenv("STRAVA_CLUB_MEMBER_PAGES", "5"))
        enq = 0
        try:
            async with httpx.AsyncClient(timeout=30, cookies=jar, follow_redirects=True,
                                         headers={"User-Agent": ua, "Accept": "text/html"}) as client:
                resp = await client.get(f"{STRAVA_WEB}/clubs")
                club_ids = set(re.findall(r"/clubs/(\d+)", resp.text)) if resp.status_code == 200 else set()
                for cid in list(club_ids)[:25]:
                    if self._stop.is_set():
                        break
                    for page in range(1, member_pages + 1):
                        try:
                            mr = await client.get(f"{STRAVA_WEB}/clubs/{cid}/members?page={page}")
                            if mr.status_code != 200:
                                break
                            aids = set(re.findall(r"/athletes/(\d+)", mr.text))
                            if not aids:
                                break
                            for aid in aids:
                                await self._enqueue_athlete(aid, None, "club")
                                enq += 1
                            await self._delay(2.0, 4.0)
                        except Exception:
                            break
        except Exception as e:
            logger.debug("strava club spider failed: %s", e)
        if enq:
            logger.info("strava: club spider enqueued %d club member(s) across %d club(s)", enq, len(club_ids))
        return enq

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
                            # Assign to instance so follow-roster expansion can use it
                            self._my_athlete_id = athlete_id
                            logger.info("strava: resolved athlete_id=%s via dashboard hydration", athlete_id)
                        # Also try to extract full profile from embedded JSON blob.
                        # Use a brace-depth counter — the naive [^}] regex breaks on
                        # any nested object (stops at first inner closing brace).
                        profile_data = None
                        ca_idx = dash.text.find('"currentAthlete"')
                        if ca_idx != -1:
                            brace_start = dash.text.find('{', ca_idx)
                            if brace_start != -1:
                                depth, i, text = 0, brace_start, dash.text
                                while i < len(text) and i < brace_start + 8192:
                                    if text[i] == '{':
                                        depth += 1
                                    elif text[i] == '}':
                                        depth -= 1
                                        if depth == 0:
                                            try:
                                                parsed = json.loads(text[brace_start:i + 1])
                                                if parsed and parsed.get("id"):
                                                    profile_data = parsed
                                            except Exception:
                                                pass
                                            break
                                    i += 1
                        # Fallback: fetch the athlete's public profile page and
                        # parse __NEXT_DATA__.props.pageProps.athlete (works even
                        # when the dashboard embeds currentAthlete: null for cookie
                        # sessions that lack full API scope).
                        # Note: /athletes/{id} page may return a reduced page (no __NEXT_DATA__)
                        # for FollowersOnly profiles or bot-detected requests. Fall back to
                        # upserting a minimal stub from the resolved athlete_id so the FK chain works.
                        if not profile_data and athlete_id:
                            try:
                                prof_resp = await asyncio.wait_for(
                                    client.get(f"https://www.strava.com/athletes/{athlete_id}",
                                               headers={"User-Agent": ua, "Accept": "text/html"}),
                                    timeout=15.0)
                                if prof_resp.status_code == 200:
                                    profile_data = _extract_strava_profile_from_html(prof_resp.text, athlete_id)
                                    import re as _re
                                    nd_m = _re.search(
                                        r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>',
                                        prof_resp.text, _re.DOTALL)
                                    if nd_m:
                                        nd = json.loads(nd_m.group(1))
                                        ath = nd.get("props", {}).get("pageProps", {}).get("athlete")
                                        if ath and ath.get("id"):
                                            profile_data = {
                                                "id": ath.get("id"),
                                                "username": ath.get("username") or ath.get("firstName", ""),
                                                "firstname": ath.get("firstName"),
                                                "lastname": ath.get("lastName"),
                                                "profile": ath.get("profileImageUrl") or profile_data.get("profile"),
                                                "city": (ath.get("location") or {}).get("city"),
                                                "state": (ath.get("location") or {}).get("state"),
                                                "country": (ath.get("location") or {}).get("country"),
                                            }
                                    # Try extracting name from meta tags if __NEXT_DATA__ absent
                                    if not any(profile_data.get(k) for k in ("username", "firstname", "profile")):
                                        name_m = _re.search(r'<title>([^<|]+)', prof_resp.text)
                                        display_name = name_m.group(1).strip() if name_m else None
                                        profile_data = {
                                            "id": athlete_id,
                                            "username": display_name or f"athlete_{athlete_id}",
                                            "firstname": display_name,
                                        }
                                        logger.info("strava: using minimal profile stub from meta for %s (%s)", athlete_id, display_name)
                            except Exception as e:
                                logger.warning("strava: profile page fetch failed: %s", e)
                                # Last resort: upsert a bare stub so FK chain resolves
                                profile_data = {"id": athlete_id, "username": f"athlete_{athlete_id}"}
                        if profile_data:
                            try:
                                if profile_data.get("id"):
                                    await self._upsert_athlete(profile_data)
                                    logger.info("strava: upserted athlete profile (id=%s name=%s %s)",
                                                profile_data.get("id"), profile_data.get("firstname"), profile_data.get("lastname"))
                                else:
                                    logger.info("strava: currentAthlete JSON found but no 'id' field: keys=%s", list(profile_data.keys())[:10])
                            except Exception as e:
                                logger.warning("strava: _upsert_athlete failed: %s", e)
                        else:
                            logger.info("strava: no currentAthlete JSON found in dashboard (ca_idx=%s)", ca_idx)
                except Exception as e:
                    logger.warning("strava: dashboard hydration probe failed: %s", e)

            if not athlete_id:
                logger.warning("strava: could not resolve current athlete id from cookies; skipping")
                return
            self._my_athlete_id = athlete_id
            await self._backfill_owner_follow_edges_from_social_users()

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
            # History backfill captures full archives; this only needs recent activities.
            max_training_pages = int(os.getenv("STRAVA_TRAINING_MAX_PAGES", "25"))
            total = 0
            page = 1
            empty_streak = 0
            while not self._stop.is_set() and page <= max_training_pages:
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
                    logger.warning("strava: rate-limited on page %d, sleeping %ds", page, self._ratelimit_sleep)
                    self._note_rate_limit(
                        scope="training_activities",
                        cooldown_seconds=self._ratelimit_sleep,
                        reason=f"training_activities page {page} returned 429",
                        metadata={"page": page},
                    )
                    await sleep_rate_limit(self._ratelimit_sleep)
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
                        self._progress_count += 1
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
                        athlete_patch = _extract_strava_profile_from_html(html, athlete_id)
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
                    metadata = {"raw": athlete}
                    artifact_meta = self._photo_tracker.last_artifact_metadata()
                    if artifact_meta:
                        metadata["vault_artifact"] = artifact_meta
                    await self.insert_media_item(
                        entity_id=aid,
                        entity_name=aname,
                        content_type="profile_photo",
                        content_id=f"profile_{aid}",
                        filename=path.name,
                        file_path=str(path),
                        file_size=path.stat().st_size,
                        sha256=self.sha256_bytes(path.read_bytes()),
                        metadata=metadata,
                        source_url=self._build_strava_source_url({
                            "content_type": "profile_photo",
                            "content_id": f"profile_{aid}",
                        }),
                    )
            await self._collect_activities_api(client, aid, aname)

    async def _upsert_athlete(self, athlete: dict):
        # platform_athlete_id is BIGINT -- always cast to int so asyncpg doesn't
        # reject string IDs (common when athlete dict comes from web scraping).
        raw_id = athlete.get("id")
        try:
            athlete_id_int = int(raw_id)
        except (TypeError, ValueError):
            logger.warning("strava: _upsert_athlete skipped — unparseable id %r", raw_id)
            return
        # Never persist a placeholder (the bare numeric id or "athlete_<id>") in a
        # name field as if it were real — store NULL so the UI shows an honest
        # "Unknown #id" instead of a fake name.
        _placeholders = {str(athlete_id_int), f"athlete_{athlete_id_int}"}
        cleaned = {}
        for key in ("username", "firstname", "lastname", "profile", "city", "state", "country", "sex"):
            value = _clean_strava_text(athlete.get(key))
            if value in _placeholders:
                value = None
            cleaned[key] = value

        def _count_from(*keys: str) -> int | None:
            for key in keys:
                if key in athlete:
                    count = _compact_count_to_int(athlete.get(key))
                    if count is not None:
                        return count
                    try:
                        raw = athlete.get(key)
                        if raw is not None and str(raw).strip() != "":
                            return int(raw)
                    except (TypeError, ValueError):
                        pass
            return None

        follower_count = _count_from("follower_count", "followers")
        following_count = _count_from("following_count", "friend_count", "friends")
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO strava_athletes (
                    platform_athlete_id, username, firstname, lastname, profile,
                    city, state, country, sex, follower_count, following_count,
                    updated_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, NOW())
                ON CONFLICT (platform_athlete_id) DO UPDATE SET
                    username       = COALESCE(EXCLUDED.username,        strava_athletes.username),
                    firstname      = COALESCE(EXCLUDED.firstname,       strava_athletes.firstname),
                    lastname       = COALESCE(EXCLUDED.lastname,        strava_athletes.lastname),
                    profile        = COALESCE(EXCLUDED.profile,         strava_athletes.profile),
                    city           = COALESCE(EXCLUDED.city,            strava_athletes.city),
                    state          = COALESCE(EXCLUDED.state,           strava_athletes.state),
                    country        = COALESCE(EXCLUDED.country,         strava_athletes.country),
                    sex            = COALESCE(EXCLUDED.sex,             strava_athletes.sex),
                    follower_count = COALESCE(EXCLUDED.follower_count,  strava_athletes.follower_count),
                    following_count= COALESCE(EXCLUDED.following_count, strava_athletes.following_count),
                    updated_at     = NOW()
            """, athlete_id_int, cleaned["username"], cleaned["firstname"],
                cleaned["lastname"], cleaned["profile"], cleaned["city"],
                cleaned["state"], cleaned["country"], cleaned["sex"],
                follower_count, following_count)

    async def _upsert_activity(self, activity: dict, athlete_id: str):
        async with self.pool.acquire() as conn:
            athlete_row = await conn.fetchrow("SELECT id FROM strava_athletes WHERE platform_athlete_id = $1", int(athlete_id))
            athlete_uuid = athlete_row['id'] if athlete_row else None
            metadata_json = json.dumps(activity, default=str)

            # latlng comes from API as [lat, lng] list; schema is VARCHAR(50)
            def _latlng(val):
                if val and len(val) == 2:
                    return f"{val[0]},{val[1]}"
                return None

            mp = activity.get("map") or {}
            polyline = mp.get("summary_polyline") or activity.get("summary_polyline") or None
            start_latlng = _latlng(activity.get("start_latlng"))
            end_latlng = _latlng(activity.get("end_latlng"))
            utc_offset_raw = activity.get("utc_offset")
            utc_offset = int(utc_offset_raw) if utc_offset_raw is not None else None
            # watts fields are floats in API but INTEGER in schema
            def _int(v): return int(v) if v is not None else None

            start_date_str = activity.get("start_date")
            start_date = datetime.fromisoformat(start_date_str.replace("Z", "")) if start_date_str else None

            await conn.execute("""
                INSERT INTO strava_activities (
                    platform_activity_id, athlete_id, name, type, sport_type,
                    workout_type, description,
                    distance, moving_time, elapsed_time, total_elevation_gain,
                    average_speed, max_speed,
                    average_heartrate, max_heartrate,
                    average_cadence, average_temp,
                    weighted_average_watts, max_watts, kilojoules, average_watts,
                    calories,
                    start_date, start_latlng, end_latlng,
                    timezone, utc_offset,
                    summary_polyline, metadata
                ) VALUES (
                    $1, $2, $3, $4, $5,
                    $6, $7,
                    $8, $9, $10, $11,
                    $12, $13,
                    $14, $15,
                    $16, $17,
                    $18, $19, $20, $21,
                    $22,
                    $23, $24, $25,
                    $26, $27,
                    $28, $29::jsonb
                )
                ON CONFLICT (platform_activity_id) DO UPDATE SET
                    name             = EXCLUDED.name,
                    description      = COALESCE(EXCLUDED.description,      strava_activities.description),
                    start_latlng     = COALESCE(EXCLUDED.start_latlng,     strava_activities.start_latlng),
                    end_latlng       = COALESCE(EXCLUDED.end_latlng,       strava_activities.end_latlng),
                    timezone         = COALESCE(EXCLUDED.timezone,         strava_activities.timezone),
                    utc_offset       = COALESCE(EXCLUDED.utc_offset,       strava_activities.utc_offset),
                    summary_polyline = COALESCE(EXCLUDED.summary_polyline, strava_activities.summary_polyline),
                    average_heartrate= COALESCE(EXCLUDED.average_heartrate,strava_activities.average_heartrate),
                    max_heartrate    = COALESCE(EXCLUDED.max_heartrate,    strava_activities.max_heartrate),
                    calories         = COALESCE(EXCLUDED.calories,         strava_activities.calories),
                    weighted_average_watts = COALESCE(EXCLUDED.weighted_average_watts, strava_activities.weighted_average_watts),
                    kilojoules       = COALESCE(EXCLUDED.kilojoules,       strava_activities.kilojoules),
                    metadata         = EXCLUDED.metadata
            """,
            activity.get("id"), athlete_uuid, activity.get("name"), activity.get("type"), activity.get("sport_type"),
            activity.get("workout_type"), activity.get("description"),
            activity.get("distance"), activity.get("moving_time"), activity.get("elapsed_time"), activity.get("total_elevation_gain"),
            activity.get("average_speed"), activity.get("max_speed"),
            activity.get("average_heartrate"), activity.get("max_heartrate"),
            activity.get("average_cadence"), activity.get("average_temp"),
            _int(activity.get("weighted_average_watts")), _int(activity.get("max_watts")),
            activity.get("kilojoules"), _int(activity.get("average_watts")),
            activity.get("calories"),
            start_date, start_latlng, end_latlng,
            activity.get("timezone"), utc_offset,
            polyline, metadata_json)
        self._archive_raw_payload(
            artifact_id=f"activities/{activity.get('id') or 'unknown'}",
            payload=activity,
            target_tables=["strava_activities"],
            metadata={
                "payload_type": "strava_activity",
                "platform_activity_id": activity.get("id"),
                "platform_athlete_id": athlete_id,
                "ingest_path": "api_or_web",
            },
        )

    # (following-feed implementation is at _collect_following_feed below)

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
        jar = self._build_cookie_jar()
        if jar is None:
            logger.warning("strava web scrape requested for %s but no usable cookie jar; skipping", athlete_id)
            return
        url = f"{STRAVA_WEB}/athletes/{athlete_id}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml",
        }
        try:
            await self._delay(self._feed_delay_min, self._feed_delay_max)
            async with httpx.AsyncClient(timeout=30, cookies=jar, follow_redirects=True) as client:
                resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                logger.warning("strava web fetch %s returned HTTP %d; cookie may be stale", athlete_id, resp.status_code)
                return
            html = resp.text
            athlete = _extract_strava_profile_from_html(html, athlete_id)
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
            if resp.status_code == 429:
                self._note_rate_limit(
                    scope="api_athlete_activities",
                    cooldown_seconds=self._ratelimit_sleep,
                    reason=f"athlete activities page {page} returned 429",
                    metadata={"page": page, "athlete_id": aid},
                )
                await sleep_rate_limit(self._ratelimit_sleep)
                continue
            resp.raise_for_status()
            activities = resp.json()
            self._archive_raw_payload(
                artifact_id=f"api/athlete_activities/{aid}/page_{page}",
                payload={"athlete_id": aid, "page": page, "per_page": per_page, "activities": activities},
                target_tables=["strava_activities"],
                metadata={
                    "payload_type": "strava_athlete_activities_page",
                    "athlete_id": aid,
                    "page": page,
                    "per_page": per_page,
                    "ingest_path": "api",
                    "request_url": f"{STRAVA_API}/athlete/activities",
                },
            )
            if not activities: break
            for activity in activities:
                if self._stop.is_set(): break
                await self._upsert_activity(activity, aid)
                self._progress_count += 1
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
        try:
            activity_id_int = int(activity_id)
        except (TypeError, ValueError):
            return
        athlete_uuid = None
        try:
            async with self.pool.acquire() as conn:
                athlete_uuid = await conn.fetchval(
                    "SELECT id FROM strava_athletes WHERE platform_athlete_id = $1",
                    int(aid),
                )
        except Exception:
            logger.debug("strava api photos: athlete lookup failed for %s", aid, exc_info=True)
        try:
            activity_date = datetime.fromisoformat(str(activity.get("start_date")).replace("Z", "+00:00")) if activity.get("start_date") else None
        except Exception:
            activity_date = None
        for i, photo in enumerate(photo_resp.json()):
            if not isinstance(photo, dict):
                continue
            urls = photo.get("urls", {})
            url = urls.get("2048") or urls.get("600") or urls.get("100") or photo.get("url")
            if not url: continue
            photo_id = str(photo.get("unique_id") or photo.get("id") or photo.get("photo_id") or i)
            thumb = urls.get("600") or urls.get("100") or photo.get("thumbnail")
            try:
                async with self.pool.acquire() as conn:
                    await conn.execute(
                        """
                        INSERT INTO strava_activity_photos
                          (platform_photo_id, platform_activity_id, athlete_id,
                           activity_name, athlete_name, caption, media_type,
                           source_url_large, source_url_thumbnail, activity_date, source)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                        ON CONFLICT (platform_photo_id, platform_activity_id) DO UPDATE SET
                           source_url_large = COALESCE(EXCLUDED.source_url_large, strava_activity_photos.source_url_large),
                           source_url_thumbnail = COALESCE(EXCLUDED.source_url_thumbnail, strava_activity_photos.source_url_thumbnail),
                           activity_name = COALESCE(EXCLUDED.activity_name, strava_activity_photos.activity_name),
                           athlete_name = COALESCE(EXCLUDED.athlete_name, strava_activity_photos.athlete_name)
                        """,
                        photo_id, activity_id_int, athlete_uuid, activity.get("name"), aname,
                        photo.get("caption") or photo.get("caption_escaped"),
                        int(photo.get("media_type") or 1), url, thumb, activity_date, "api_activity_photos",
                    )
            except Exception:
                logger.debug("strava api photo upsert failed %s/%s", activity_id, photo_id, exc_info=True)
                continue
            content_id = f"{activity_id}_{photo_id}"
            if not self.is_known(content_id):
                await self.download_media({
                    "entity_id": aid,
                    "entity_name": aname,
                    "content_type": "activity_photo",
                    "content_id": content_id,
                    "url": url,
                    "extension": "jpg",
                    "source_url": f"https://www.strava.com/activities/{activity_id}",
                    "raw": photo,
                })

    async def _backfill_privacy_zones(self):
        """Recurring backfill: populate privacy-zone flags for historical activities
        that have a stored GPS stream but no flag yet (privacy_zone_start IS NULL).

        Re-fetches the activity summary from the API and compares start/end to the
        stored stream (same logic as _collect_gps_streams). Self-drains one batch
        per cycle (rate-limited via _delay), piggybacking the recurring per-cycle
        backfill pattern. Owned activities resolve to real flags; others' that can't
        be re-fetched (404/403) get stream_status='ok_unverifiable' so they are not
        retried forever. Batch size via STRAVA_PRIVACY_BACKFILL_BATCH (default 25).
        """
        if not self.pool or not self._access_token:
            return
        batch = int(os.getenv("STRAVA_PRIVACY_BACKFILL_BATCH", "25"))
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT a.id, a.platform_activity_id, s.latlng "
                "FROM strava_activities a JOIN strava_gps_streams s ON s.activity_id = a.id "
                "WHERE a.privacy_zone_start IS NULL AND a.stream_status = 'ok' "
                "LIMIT $1", batch,
            )
        if not rows:
            return
        done = 0
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            for r in rows:
                if self._stop.is_set():
                    break
                try:
                    await self._delay()
                    resp = await client.get(
                        f"{STRAVA_API}/activities/{r['platform_activity_id']}",
                        headers={"Authorization": f"Bearer {self._access_token}"},
                    )
                    if resp.status_code != 200:
                        # Can't re-fetch (another athlete's restricted activity etc.)
                        # — mark unverifiable so the WHERE clause skips it next cycle.
                        async with self.pool.acquire() as conn:
                            await conn.execute(
                                "UPDATE strava_activities SET stream_status='ok_unverifiable' "
                                "WHERE id=$1 AND privacy_zone_start IS NULL",
                                r["id"],
                            )
                        continue
                    act = resp.json()
                    path = r["latlng"] if isinstance(r["latlng"], list) else json.loads(r["latlng"])
                    if not path:
                        continue
                    pzs = _is_truncated(act.get("start_latlng"), path[0])
                    pze = _is_truncated(act.get("end_latlng"), path[-1])
                    tps = f"{path[0][0]},{path[0][1]}" if pzs else None
                    tpe = f"{path[-1][0]},{path[-1][1]}" if pze else None
                    async with self.pool.acquire() as conn:
                        await conn.execute(
                            "UPDATE strava_activities SET privacy_zone_start=$1, "
                            "privacy_zone_end=$2, truncation_point_start=$3, "
                            "truncation_point_end=$4 WHERE id=$5",
                            pzs, pze, tps, tpe, r["id"],
                        )
                    done += 1
                except Exception as e:
                    logger.debug("strava privacy backfill %s failed: %s",
                                 r["platform_activity_id"], e)
        if done:
            logger.info("strava: privacy-zone backfill processed %d activities this cycle", done)

    async def _backfill_missing_gps_streams(self, batch_size: int = 150) -> int:
        """Re-fetch GPS streams for activities that never got one (stream_status NULL).

        The earlier wrong-auth bug (Bearer token vs web cookie) left ~95% of
        activities with no GPS even though they have a map on strava.com. This
        drains a bounded batch per cycle via the corrected web-XHR path in
        `_collect_gps_streams`. Self-terminates once stream_status is set on all.
        """
        if not self._use_web or not self.pool:
            return 0
        await self._sync_persisted_gps_stream_cooldown()
        if self._gps_stream_cooling_down():
            left = int(self._gps_stream_cooldown_until - time.time())
            logger.info("strava: GPS backfill cooling down for %ds", max(0, left))
            return 0
        jar = self._build_cookie_jar()
        if jar is None:
            return 0
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT a.platform_activity_id, a.start_latlng, a.end_latlng
                FROM strava_activities a
                LEFT JOIN strava_athletes ath ON ath.id = a.athlete_id
                LEFT JOIN strava_gps_streams s ON s.activity_id = a.id
                LEFT JOIN LATERAL (
                    SELECT MIN(ap.tier) AS tier
                    FROM account_proximity_cache ap
                    WHERE ap.platform = 'strava'
                      AND ap.account_id = ath.platform_athlete_id::text
                ) prox ON TRUE
                LEFT JOIN LATERAL (
                    SELECT MAX(ct.priority) AS priority
                    FROM collection_targets ct
                    WHERE ct.source = 'strava'
                      AND ct.target_id = ath.platform_athlete_id::text
                ) target ON TRUE
                WHERE a.stream_status IS NULL
                  AND (s.latlng IS NULL OR s.latlng = '[]'::jsonb OR s.latlng = 'null'::jsonb)
                -- EDIT 2026-07-13: was ORDER BY a.start_date DESC. Fetch-failures
                -- (non-200 from both cookie accounts: private/deleted activities)
                -- stay stream_status=NULL and re-sorted to the queue head forever —
                -- 436 of the top 500 were previously-attempted repeats, wasting
                -- ~half of every batch. Random sampling spreads the (few hundred)
                -- permanent failures across the ~34k pool so each batch is almost
                -- all fresh work, while transient failures still get retried later.
                ORDER BY
                    CASE WHEN target.priority IS NOT NULL THEN 0 ELSE 1 END,
                    COALESCE(prox.tier, 9) ASC,
                    CASE
                        WHEN $2::text IS NOT NULL
                         AND ath.platform_athlete_id::text = $2::text
                        THEN 0 ELSE 1
                    END,
                    CASE WHEN a.page_scraped_at IS NOT NULL THEN 0 ELSE 1 END,
                    CASE
                        WHEN COALESCE(a.sport_type, a.type, '') ILIKE '%run%'
                          OR COALESCE(a.sport_type, a.type, '') ILIKE '%walk%'
                          OR COALESCE(a.sport_type, a.type, '') ILIKE '%ride%'
                          OR COALESCE(a.sport_type, a.type, '') ILIKE '%hike%'
                        THEN 0 ELSE 1
                    END,
                    random()
                LIMIT $1
                """, batch_size, self._my_athlete_id)
        if not rows:
            return 0
        ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        logger.info("strava: GPS backfill — fetching streams for %d activities", len(rows))
        done = 0
        async with httpx.AsyncClient(timeout=30, cookies=jar, follow_redirects=True,
                                     headers={"User-Agent": ua}) as client:
            for row in rows:
                if self._stop.is_set():
                    break
                activity = {"id": row["platform_activity_id"],
                            "start_latlng": row["start_latlng"],
                            "end_latlng": row["end_latlng"]}
                try:
                    await self._collect_gps_streams(client, activity, str(row["platform_activity_id"]))
                    done += 1
                    if self._gps_stream_cooling_down():
                        break
                except Exception as e:
                    logger.debug("GPS backfill failed for %s: %s",
                                 row["platform_activity_id"], e)
                # EDIT 2026-07-13: tick the watchdog on EVERY attempted activity.
                # This loop paces ~10-16s/activity (delay + up to 2 cookie-account
                # tries) and previously never advanced progress_count, so the
                # worker watchdog declared strava HUNG after 7200s and cancelled
                # the task mid-batch (observed 2026-07-13 02:56). Stream fetches
                # ARE progress — same pattern as youtube's per-item ticks.
                self._progress_count += 1
        logger.info("strava: GPS backfill processed %d activities this cycle", done)
        return done

    async def _repair_existing_gps_stream_routes(self, batch_size: int = 100) -> int:
        """Derive route metadata for old rows that already have GPS streams.

        Older page scrapes inserted strava_gps_streams.latlng but left
        strava_activities.summary_polyline/stream_status blank. The fetch path
        then skipped them because a stream existed, leaving dashboards and
        downstream analyzer rebuilds with "detail pending" despite having route
        data. This pass is local-only and bounded; it does not call Strava.
        """
        if not self.pool:
            return 0
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT a.id, a.platform_activity_id, a.start_latlng, a.end_latlng,
                       a.summary_polyline, a.stream_status, a.privacy_zone_start,
                       a.privacy_zone_end, s.latlng
                FROM strava_activities a
                JOIN strava_gps_streams s ON s.activity_id = a.id
                WHERE s.latlng IS NOT NULL
                  AND (
                        a.summary_polyline IS NULL
                     OR a.summary_polyline = ''
                     OR a.stream_status IS NULL
                     OR a.start_latlng IS NULL
                     OR a.end_latlng IS NULL
                     OR a.privacy_zone_start IS NULL
                     OR a.privacy_zone_end IS NULL
                   )
                ORDER BY a.start_date DESC NULLS LAST, a.platform_activity_id DESC
                LIMIT $1
                """,
                int(batch_size),
            )
        if not rows:
            return 0

        repaired = 0
        async with self.pool.acquire() as conn:
            for row in rows:
                fields = _derive_gps_route_fields(
                    row["start_latlng"],
                    row["end_latlng"],
                    row["latlng"],
                )
                if fields["point_count"] <= 1:
                    continue
                await conn.execute(
                    """
                    UPDATE strava_activities
                    SET start_latlng = COALESCE(start_latlng, $1),
                        end_latlng = COALESCE(end_latlng, $2),
                        stream_status = CASE
                            WHEN stream_status IS NULL
                              OR stream_status IN ('incomplete', 'truncated_empty')
                            THEN $3
                            ELSE stream_status
                        END,
                        privacy_zone_start = CASE
                            WHEN $4 THEN TRUE
                            WHEN privacy_zone_start IS NULL THEN FALSE
                            ELSE privacy_zone_start
                        END,
                        privacy_zone_end = CASE
                            WHEN $5 THEN TRUE
                            WHEN privacy_zone_end IS NULL THEN FALSE
                            ELSE privacy_zone_end
                        END,
                        truncation_point_start = COALESCE(truncation_point_start, $6),
                        truncation_point_end = COALESCE(truncation_point_end, $7),
                        summary_polyline = COALESCE(NULLIF(summary_polyline, ''), $8)
                    WHERE id = $9
                    """,
                    fields["start_latlng"],
                    fields["end_latlng"],
                    fields["stream_status"],
                    fields["privacy_zone_start"],
                    fields["privacy_zone_end"],
                    fields["truncation_point_start"],
                    fields["truncation_point_end"],
                    fields["summary_polyline"],
                    row["id"],
                )
                repaired += 1
                self._progress_count += 1
        if repaired:
            logger.info("strava: repaired %d existing GPS stream route row(s)", repaired)
        return repaired

    async def _fetch_streams(self, client: httpx.AsyncClient, activity_id: str):
        """Return (latlng, time, altitude) arrays for an activity, or (None,None,None) on failure.

        WHY THIS EXISTS: GPS was missing for activities that clearly have a map on
        strava.com. Root cause — streams were fetched from the OAuth /api/v3
        endpoint with `Authorization: Bearer {access_token}`, but in cookie mode
        the token is EMPTY (401) and the API can't read FOLLOWED athletes' streams
        at all. The web XHR endpoint (what the website's map uses) returns streams
        for ANY activity the logged-in cookie can view. Returning None on failure
        (vs an empty list) lets the caller retry instead of caching "no GPS".
        """
        if self._gps_stream_cooling_down():
            return None, None, None
        await self._delay()
        # Preferred: web XHR with the session cookie (covers followed athletes).
        # Try EACH cookie account in turn — an activity hidden from one account
        # may be visible to another that follows that athlete. First account that
        # returns a non-empty track wins. A 200-but-empty from all accounts means
        # genuinely no GPS (manual entry) -> we return [] so it isn't retried.
        if self._use_web and self._cookie_accounts:
            _ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
            saw_200_empty = False
            for idx, (name, cookie) in enumerate(self._cookie_accounts):
                jar = self._build_cookie_jar(cookie)
                if jar is None:
                    continue
                try:
                    if idx > 0:
                        await self._delay()  # space out cross-account retries
                    async with httpx.AsyncClient(
                            timeout=30, cookies=jar, follow_redirects=True,
                            headers={"User-Agent": _ua}) as c:
                        async def _get_web_streams():
                            return await c.get(
                                f"https://www.strava.com/activities/{activity_id}/streams",
                                params=[("stream_types[]", "latlng"), ("stream_types[]", "time"),
                                        ("stream_types[]", "altitude")],
                                headers={"X-Requested-With": "XMLHttpRequest",
                                         "Accept": "application/json",
                                         "Referer": f"https://www.strava.com/activities/{activity_id}"})

                        resp = await _get_web_streams()
                        if resp.status_code == 429:
                            retry_resp = await self._retry_gps_stream_after_429(
                                _get_web_streams,
                                activity_id=activity_id,
                                context=f"web:{name}",
                                account=name,
                            )
                            if retry_resp is not None:
                                resp = retry_resp
                    if resp.status_code == 200:
                        d = resp.json()
                        self._archive_raw_payload(
                            artifact_id=f"gps_streams/web/{name}/{activity_id}",
                            payload=d,
                            target_tables=["strava_gps_streams", "strava_activities"],
                            metadata={
                                "payload_type": "strava_web_gps_stream",
                                "platform_activity_id": activity_id,
                                "collection_account": name,
                                "ingest_path": "web",
                                "request_url": f"{STRAVA_WEB}/activities/{activity_id}/streams",
                            },
                        )
                        latlng = d.get("latlng") or []
                        if latlng:
                            return (latlng, d.get("time") or [], d.get("altitude") or [])
                        saw_200_empty = True
                    elif resp.status_code == 429:
                        self._set_gps_stream_cooldown(activity_id, f"web:{name}")
                        return None, None, None
                    else:
                        logger.debug("web streams %s (%s) -> HTTP %s", activity_id, name, resp.status_code)
                except Exception as e:
                    logger.debug("web streams (%s) failed for %s: %s", name, activity_id, e)
            if saw_200_empty:
                return [], [], []  # confirmed-no-GPS by at least one account
        # Fallback: OAuth API (only the token owner's own activities).
        if self._access_token:
            try:
                async def _get_api_streams():
                    return await client.get(
                        f"{STRAVA_API}/activities/{activity_id}/streams",
                        headers={"Authorization": f"Bearer {self._access_token}"},
                        params={"keys": "latlng,time,altitude", "key_by_type": "true"})

                resp = await _get_api_streams()
                if resp.status_code == 429:
                    retry_resp = await self._retry_gps_stream_after_429(
                        _get_api_streams,
                        activity_id=activity_id,
                        context="api",
                    )
                    if retry_resp is not None:
                        resp = retry_resp
                if resp.status_code == 200:
                    s = resp.json()
                    self._archive_raw_payload(
                        artifact_id=f"gps_streams/api/{activity_id}",
                        payload=s,
                        target_tables=["strava_gps_streams", "strava_activities"],
                        metadata={
                            "payload_type": "strava_api_gps_stream",
                            "platform_activity_id": activity_id,
                            "ingest_path": "api",
                            "request_url": f"{STRAVA_API}/activities/{activity_id}/streams",
                        },
                    )
                    return (s.get("latlng", {}).get("data", []),
                            s.get("time", {}).get("data", []),
                            s.get("altitude", {}).get("data", []))
                if resp.status_code == 429:
                    self._set_gps_stream_cooldown(activity_id, "api")
                    return None, None, None
            except Exception as e:
                logger.debug("api streams fetch failed for %s: %s", activity_id, e)
        return None, None, None

    async def _collect_gps_streams(self, client: httpx.AsyncClient, activity: dict, aid: str):
        activity_id = str(activity["id"])
        # Skip only if we ALREADY have a POPULATED track. The old check skipped on
        # ANY existing row, so an empty/failed fetch was cached forever; combined
        # with the wrong-auth bug above, activities with real GPS stayed blank.
        async with self.pool.acquire() as conn:
            existing = await conn.fetchval(
                "SELECT s.latlng FROM strava_activities a "
                "LEFT JOIN strava_gps_streams s ON s.activity_id = a.id "
                "WHERE a.platform_activity_id = $1", int(activity_id))
        if existing is not None and str(existing) not in ("[]", "null", ""):
            return
        try:
            latlng_data, time_data, alt_data = await self._fetch_streams(client, activity_id)
            if latlng_data is None:
                return  # fetch failed — do NOT cache empty; retry next cycle
            if True:
                async with self.pool.acquire() as conn:
                    act_row = await conn.fetchrow("SELECT id FROM strava_activities WHERE platform_activity_id = $1", int(activity_id))
                    if act_row:
                        # Replace any prior (empty) row for this activity.
                        await conn.execute("DELETE FROM strava_gps_streams WHERE activity_id = $1", act_row['id'])
                        await conn.execute("INSERT INTO strava_gps_streams (activity_id, latlng, time, altitude) VALUES ($1, $2, $3, $4)", act_row['id'], json.dumps(latlng_data), json.dumps(time_data or []), json.dumps(alt_data or []))
                        # Backfill start/end coords from the GPS track when the API
                        # summary omitted them, AND record privacy-zone/truncation
                        # metadata. The helper accepts API arrays and DB strings,
                        # so the live fetch path and local repair pass stay aligned.
                        fields = _derive_gps_route_fields(
                            activity.get("start_latlng"),
                            activity.get("end_latlng"),
                            latlng_data,
                        )
                        await conn.execute(
                            "UPDATE strava_activities SET "
                            "start_latlng           = COALESCE(start_latlng, $1), "
                            "end_latlng             = COALESCE(end_latlng,   $2), "
                            "stream_status          = $3, "
                            "privacy_zone_start     = $4, "
                            "privacy_zone_end       = $5, "
                            "truncation_point_start = $6, "
                            "truncation_point_end   = $7, "
                            "summary_polyline       = COALESCE(NULLIF(summary_polyline,''), $9) "
                            "WHERE id = $8",
                            fields["start_latlng"], fields["end_latlng"],
                            fields["stream_status"],
                            fields["privacy_zone_start"], fields["privacy_zone_end"],
                            fields["truncation_point_start"], fields["truncation_point_end"],
                            act_row['id'],
                            fields["summary_polyline"] or None,
                        )
        except Exception as e: logger.debug("GPS stream fetch failed for activity %s: %s", activity_id, e)

    async def get_backfill_items(self, batch_size: int) -> list[dict]:
        if not self.pool:
            return []
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT p.platform_photo_id, p.platform_activity_id,
                       p.source_url_large, p.athlete_name, p.activity_name
                FROM strava_activity_photos p
                LEFT JOIN media_items mi
                    ON mi.source = 'strava'
                    AND mi.content_id = p.platform_activity_id || '_' || p.platform_photo_id
                WHERE p.source_url_large IS NOT NULL
                  AND mi.id IS NULL
                ORDER BY
                  CASE WHEN p.source_url_large LIKE '%sport-image.strava.com%' THEN 1 ELSE 0 END,
                  p.collected_at DESC NULLS LAST
                LIMIT $1
            """, batch_size)
        return [{"entity_id": r["athlete_name"] or "unknown",
                 "entity_name": r["athlete_name"] or "unknown",
                 "content_type": "activity_photo",
                 "content_id": f"{r['platform_activity_id']}_{r['platform_photo_id']}",
                 "url": r["source_url_large"],
                 "extension": "png" if "sport-image.strava.com" in (r["source_url_large"] or "") else "jpg",
                 "source_url": f"https://www.strava.com/activities/{r['platform_activity_id']}"}
                for r in rows]

    async def _backfill_missing_photo_media(self, batch_size: int = 25) -> int:
        items = await self.get_backfill_items(batch_size)
        if not items:
            return 0
        downloaded = 0
        for item in items:
            if self._stop.is_set():
                break
            cid = item.get("content_id", "")
            if self.is_known(cid):
                continue
            try:
                if await self.download_media(item):
                    downloaded += 1
            except Exception as e:
                logger.warning("strava: photo media backfill failed %s: %s", cid, e)
                try:
                    self.reconciler.record_failure(cid)
                except Exception:
                    logger.debug("strava: photo media failure accounting failed", exc_info=True)
        if downloaded:
            logger.info("strava: photo media backfilled %d/%d items", downloaded, len(items))
        return downloaded

    @staticmethod
    def _build_strava_source_url(item: dict) -> str | None:
        """Canonical Strava URL for media_items.source_url. Content-id
        conventions inside this collector:
          content_type=profile_photo:  content_id = "profile_<athlete_id>"
                                       -> https://www.strava.com/athletes/<id>
          content_type=activity_photo: content_id = "<activity_id>_<uuid>"
                                       -> https://www.strava.com/activities/<activity_id>
          content_type=route_map:      content_id = "<activity_id>"
                                       -> https://www.strava.com/activities/<activity_id>
        Returns None on any unrecognised shape."""
        ctype = (item.get("content_type") or "").strip()
        cid = (item.get("content_id") or "").strip()
        if ctype == "profile_photo":
            if not cid.startswith("profile_"):
                return None
            athlete = cid[len("profile_"):]
            if not athlete.isdigit():
                return None
            return f"https://www.strava.com/athletes/{athlete}"
        if ctype == "activity_photo":
            activity_id = cid.split("_", 1)[0]
            if not activity_id.isdigit():
                return None
            return f"https://www.strava.com/activities/{activity_id}"
        if ctype == "route_map":
            if not cid.isdigit():
                return None
            return f"https://www.strava.com/activities/{cid}"
        return None

    async def download_media(self, item: dict):
        cid = item["content_id"]
        if self.is_known(cid): return False
        filename = self.build_filename(item["entity_id"], item["entity_name"], item["content_type"], cid, extension=item.get("extension", "jpg"))
        try:
            await self._delay()
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                resp = await client.get(item["url"])
                resp.raise_for_status()
                data = resp.content
            sha = self.sha256_bytes(data)
            source_url = self._build_strava_source_url(item)
            metadata = {
                "entity_id": item["entity_id"],
                "entity_name": item["entity_name"],
                "content_type": item["content_type"],
                "content_id": cid,
                "collected_at": datetime.now(timezone.utc).isoformat(),
                "raw": item.get("raw", {}),
                "rebuild_target_tables": ["media_items", "strava_activity_photos", "strava_activities"],
            }
            artifact = write_atomic_artifact(
                source=self.SOURCE_NAME,
                artifact_id=cid,
                artifact_kind="media_blob",
                data=data,
                extension=item.get("extension", "jpg"),
                expected_sha256=sha,
                metadata={
                    **metadata,
                    "filename": filename,
                    "source_url": source_url,
                    "request_url": item.get("url"),
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
            if inserted:
                self._known_ids.add(cid)
            return inserted
        except Exception as e:
            logger.error("Download failed %s: %s", cid, e)
            await self.send_to_dlq(item["entity_id"], cid, str(e))
            return False

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

    def _build_cookie_jar(self, cookie: str | None = None) -> httpx.Cookies | None:
        """Construct an httpx.Cookies jar. If `cookie` is given, build a jar for
        that specific account's _strava4_session; otherwise load
        STRAVA_COOKIES_FILE / the primary session cookie. Returns None if unusable."""
        if cookie:
            jar = httpx.Cookies()
            jar.set("_strava4_session", cookie, domain=".strava.com", path="/")
            return jar
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
                    logger.warning("strava feed: rate-limited on page %d, sleeping %ds", page, self._ratelimit_sleep)
                    self._note_rate_limit(
                        scope="feed",
                        cooldown_seconds=self._ratelimit_sleep,
                        reason=f"feed page {page} returned 429",
                        metadata={"page": page},
                    )
                    await sleep_rate_limit(self._ratelimit_sleep)
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
                        self._progress_count += 1
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

    async def _collect_following_feed(self):
        """Scrape the authenticated user's following feed and persist all
        discovered activities.

        Called from collect() after _collect_via_cookies() has resolved
        self._my_athlete_id. No date-window filtering — captures up to
        STRAVA_FEED_MAX_PAGES (default 10) pages of recent following-feed
        activities and upserts each one.  Companion to fetch_feed_for_date
        which is day-bounded; this method is the collect()-integrated path.
        """
        if not self._use_web:
            logger.info("strava following-feed: cookie auth required; skipping")
            return
        if not self._my_athlete_id:
            logger.info("strava following-feed: athlete_id not resolved; skipping")
            return
        jar = self._build_cookie_jar()
        if jar is None:
            logger.warning("strava following-feed: no cookies available; skipping")
            return

        ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        headers = {
            "User-Agent": ua,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "https://www.strava.com/dashboard",
        }

        import time as _time
        before = int(_time.time())
        cursor = None
        page = 1
        max_pages = min(int(os.getenv("STRAVA_FEED_MAX_PAGES", "10")), 10)
        total_kept = 0
        seen: set[int] = set()

        async with httpx.AsyncClient(
            timeout=30, cookies=jar, follow_redirects=True
        ) as client:
            while not self._stop.is_set() and page <= max_pages:
                await self._delay(self._feed_delay_min, self._feed_delay_max)
                params: dict = {
                    "feed_type": "following",
                    "athlete_id": self._my_athlete_id,
                    "before": before,
                }
                if cursor is not None:
                    params["cursor"] = cursor
                try:
                    resp = await client.get(
                        f"{STRAVA_WEB}/dashboard/feed",
                        headers=headers,
                        params=params,
                    )
                except Exception as e:
                    logger.warning("strava following-feed: page %d fetch error: %s", page, e)
                    break
                if resp.status_code == 429:
                    logger.warning("strava following-feed: rate-limited on page %d; sleeping %ds", page, self._ratelimit_sleep)
                    self._note_rate_limit(
                        scope="following_feed",
                        cooldown_seconds=self._ratelimit_sleep,
                        reason=f"following-feed page {page} returned 429",
                        metadata={"page": page, "athlete_id": self._my_athlete_id},
                    )
                    await sleep_rate_limit(self._ratelimit_sleep)
                    continue
                if resp.status_code != 200:
                    logger.warning(
                        "strava following-feed: stopped at page %d HTTP %d", page, resp.status_code
                    )
                    break
                try:
                    payload = resp.json()
                except Exception as e:
                    logger.warning("strava following-feed: JSON decode failed page %d: %s", page, e)
                    break

                items, next_cursor = self._extract_feed_page(payload)
                if not items:
                    logger.info("strava following-feed: page %d empty; stopping", page)
                    break

                page_kept = 0
                for raw in items:
                    norm = self._normalize_feed_activity(raw)
                    if not norm:
                        continue
                    aid = norm.get("id")
                    if not aid or aid in seen:
                        continue
                    seen.add(aid)
                    athlete_id_raw = norm.get("_athlete_id") or self._my_athlete_id
                    try:
                        athlete_id_int = int(athlete_id_raw)
                    except (TypeError, ValueError):
                        athlete_id_int = None
                    if athlete_id_int is None:
                        continue
                    athlete_name = norm.get("_athlete_name") or str(athlete_id_int)
                    try:
                        # Ensure athlete row exists (stub) so FK can resolve.
                        async with self.pool.acquire() as conn:
                            await conn.execute(
                                """INSERT INTO strava_athletes
                                       (platform_athlete_id, username, updated_at)
                                   VALUES ($1, $2, NOW())
                                   ON CONFLICT (platform_athlete_id) DO NOTHING""",
                                athlete_id_int,
                                athlete_name,
                        )
                        await self._upsert_activity(norm, str(athlete_id_int))
                        self._progress_count += 1
                        page_kept += 1
                    except Exception as e:
                        logger.warning(
                            "strava following-feed: upsert activity %s failed: %s", aid, e
                        )

                total_kept += page_kept
                logger.info(
                    "strava following-feed: page %d kept=%d total=%d (next_cursor=%s)",
                    page, page_kept, total_kept, next_cursor,
                )
                if next_cursor is None:
                    break
                cursor = next_cursor
                before = next_cursor
                page += 1

        logger.info(
            "strava following-feed: finished athlete=%s pages=%d total_activities=%d",
            self._my_athlete_id, page, total_kept,
        )

    async def _backfill_athlete_history(self, athlete_id: str, *, year_cap: int = 3) -> int:
        """Month-by-month per-athlete activity backfill.

        Ported from archive/stravatoolkit/ingestion/core/scrapers/history.py.

        Hits GET /athletes/{id}?chart_type=miles&interval_type=month&interval=YYYYMM&year_offset=0
        and extracts activities from the SSR HTML via __NEXT_DATA__, microfrontend
        data-react-props, or inline JS assignments (same 3-strategy parser from parsers.py).

        For each activity also extracts:
          - map.summary_polyline  -> stored in strava_activities.summary_polyline
          - mapAndPhotos.photoList -> upserted to strava_activity_photos

        Walks backwards month by month, stopping when:
          - consecutive empty months >= 3, OR
          - reached year_cap years back, OR
          - HTTP 403 (private/blocked profile), OR
          - self._stop is set
        """
        import re as _re
        import json as _json
        from html import unescape as _unescape
        from datetime import datetime as _dt

        if not self._session_cookie:
            return 0

        jar = self._build_cookie_jar()
        if jar is None:
            return 0

        ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        headers = {
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }

        # ---- HTML parsing helpers (ported from archive parsers.py) ----
        NEXT_DATA_RE = _re.compile(
            r'<script[^>]+id="__NEXT_DATA__"[^>]*>(?P<data>.*?)</script>', _re.DOTALL)
        MFE_RE = _re.compile(r"data-react-props='([^']+)'")
        JS_RE = _re.compile(r"=\s*(\{.*?\}|\[.*?\]);", _re.DOTALL)
        ACTIVITY_HINTS = ("id", "activity_id", "start_date", "start_date_utc",
                          "sport_type", "type", "map", "mapAndPhotos", "activityName")

        def _looks_activity(d: dict) -> bool:
            return (d.get("entity") == "Activity"
                    or isinstance(d.get("activity"), dict)
                    or sum(1 for k in ACTIVITY_HINTS if k in d) >= 2)

        def _coerce_list(val) -> list:
            if isinstance(val, list):
                return [x for x in val if isinstance(x, dict)]
            return []

        def _entries_from_dict(d: dict, assume_profile: bool = False) -> list:
            for key in ("preFetchedEntries", "activities"):
                v = _coerce_list(d.get(key))
                if v:
                    return v
            for key in ("entries",):
                v = _coerce_list(d.get(key))
                if v and (assume_profile or any(_looks_activity(e) for e in v)):
                    return v
            for key in ("items", "models", "data"):
                v = _coerce_list(d.get(key))
                if v and any(_looks_activity(e) for e in v):
                    return v
            return []

        def _extract_entries(html: str) -> list:
            # Strategy 1: microfrontend props
            for m in MFE_RE.finditer(html):
                try:
                    p = _json.loads(_unescape(m.group(1)))
                except Exception:
                    continue
                if not isinstance(p, dict):
                    continue
                ctx = p.get("appContext") or {}
                if isinstance(ctx, dict) and (ctx.get("page") == "profile" or ctx.get("feedType") == "profile"):
                    entries = _entries_from_dict(ctx, assume_profile=True)
                    if entries:
                        return entries
                entries = _entries_from_dict(p)
                if entries:
                    return entries

            # Strategy 2: __NEXT_DATA__
            m = NEXT_DATA_RE.search(html)
            if m:
                try:
                    nd = _json.loads(_unescape(m.group("data")))
                    # walk the tree
                    def _walk(obj):
                        if isinstance(obj, dict):
                            e = _entries_from_dict(obj)
                            if e:
                                return e
                            for v in obj.values():
                                r = _walk(v)
                                if r:
                                    return r
                        elif isinstance(obj, list):
                            for item in obj:
                                r = _walk(item)
                                if r:
                                    return r
                        return None
                    entries = _walk(nd)
                    if entries:
                        return entries
                except Exception:
                    pass

            # Strategy 3: inline JS assignments
            for m in JS_RE.finditer(html):
                try:
                    p = _json.loads(_unescape(m.group(1)))
                except Exception:
                    continue
                if isinstance(p, dict):
                    entries = _entries_from_dict(p)
                    if entries:
                        return entries
            return []

        def _first(*vals):
            for v in vals:
                if v not in (None, "", [], {}):
                    return v
            return None

        def _parse_activity(raw: dict, athlete_id_int: int) -> dict | None:
            # Unwrap nested structure like the archive normalizer does
            ap = raw.get("activity")
            entity = ap if isinstance(ap, dict) else (raw.get("row") or raw)
            if not isinstance(entity, dict):
                return None
            map_info = entity.get("map") if isinstance(entity.get("map"), dict) else {}
            map_photos = entity.get("mapAndPhotos") if isinstance(entity.get("mapAndPhotos"), dict) else {}
            activity_id = _first(entity.get("id"), entity.get("activity_id"), raw.get("entity_id"))
            if not activity_id:
                return None
            start_date = _first(
                entity.get("start_date"), entity.get("start_date_utc"),
                entity.get("startDate"), entity.get("start_date_local"))
            if not start_date:
                return None
            polyline = _first(
                entity.get("map_summary_polyline"),
                map_info.get("summary_polyline"),
                map_photos.get("activityMap", {}).get("polyline") if isinstance(map_photos.get("activityMap"), dict) else None,
            )
            photo_list = _first(map_photos.get("photoList"), entity.get("photoList")) or []
            return {
                "id": int(str(activity_id).strip()),
                "name": _first(entity.get("name"), entity.get("activity_name"), entity.get("activityName")),
                "type": entity.get("type"),
                "sport_type": entity.get("sport_type") or entity.get("type"),
                "start_date": start_date,
                "elapsed_time": int(entity.get("elapsed_time") or entity.get("elapsedTime") or 0),
                "distance": entity.get("distance"),
                "_polyline": polyline,
                "_photos": photo_list if isinstance(photo_list, list) else [],
            }

        # ---- month cursor helpers ----
        def _month_str(year: int, month: int) -> str:
            return f"{year}{month:02d}"

        def _prev_month(ym: str) -> str:
            y, m = int(ym[:4]), int(ym[4:])
            return _month_str(y - 1, 12) if m == 1 else _month_str(y, m - 1)

        now = _dt.utcnow()
        cursor = _month_str(now.year, now.month)
        cutoff = _month_str(now.year - year_cap, now.month)
        consecutive_empty = 0
        total_activities = 0
        total_photos = 0

        async with httpx.AsyncClient(
            timeout=30, cookies=jar, follow_redirects=True
        ) as client:
            while not self._stop.is_set() and cursor >= cutoff:
                await self._delay(2.0, 4.5)
                url = f"{STRAVA_WEB}/athletes/{athlete_id}"
                params = {
                    "chart_type": "miles",
                    "interval_type": "month",
                    "interval": cursor,
                    "year_offset": "0",
                }
                try:
                    resp = await asyncio.wait_for(
                        client.get(url, headers=headers, params=params),
                        timeout=30.0,
                    )
                except Exception as e:
                    logger.warning("strava history %s month %s: fetch error %s", athlete_id, cursor, e)
                    cursor = _prev_month(cursor)
                    continue

                if resp.status_code == 403:
                    logger.info("strava history %s: 403 forbidden, stopping backfill", athlete_id)
                    break
                if resp.status_code == 429:
                    _heavy = int(self._ratelimit_sleep * 1.5)
                    logger.warning("strava history %s: rate-limited, sleeping %ds", athlete_id, _heavy)
                    self._note_rate_limit(
                        scope="history",
                        cooldown_seconds=_heavy,
                        reason=f"history for {athlete_id} returned 429",
                        metadata={"athlete_id": str(athlete_id)},
                    )
                    await sleep_rate_limit(_heavy)
                    continue
                if resp.status_code != 200:
                    logger.warning("strava history %s month %s: HTTP %d", athlete_id, cursor, resp.status_code)
                    cursor = _prev_month(cursor)
                    consecutive_empty += 1
                    if consecutive_empty >= 3:
                        break
                    continue

                html = resp.text
                entries = _extract_entries(html)
                if not entries:
                    consecutive_empty += 1
                    logger.debug("strava history %s month %s: no entries parsed (empty=%d)",
                                 athlete_id, cursor, consecutive_empty)
                    if consecutive_empty >= 3:
                        logger.info("strava history %s: 3 consecutive empty months, stopping", athlete_id)
                        break
                    cursor = _prev_month(cursor)
                    continue

                consecutive_empty = 0
                month_count = 0

                for raw in entries:
                    if not isinstance(raw, dict):
                        continue
                    try:
                        athlete_id_int = int(athlete_id)
                    except ValueError:
                        continue
                    parsed = _parse_activity(raw, athlete_id_int)
                    if not parsed:
                        continue

                    # Upsert activity with polyline
                    try:
                        await self._upsert_activity(parsed, athlete_id)
                        self._progress_count += 1
                        # Update polyline separately if present
                        if parsed.get("_polyline"):
                            logger.info("strava history: polyline found for activity %s (%d chars)",
                                        parsed["id"], len(parsed["_polyline"]))
                            async with self.pool.acquire() as conn:
                                await conn.execute(
                                    """UPDATE strava_activities
                                       SET summary_polyline = $1
                                       WHERE platform_activity_id = $2
                                         AND (summary_polyline IS NULL OR summary_polyline = '')""",
                                    parsed["_polyline"],
                                    parsed["id"],
                                )
                        month_count += 1
                        total_activities += 1
                    except Exception as e:
                        logger.debug("strava history upsert activity %s failed: %s", parsed.get("id"), e)
                        continue

                    # Upsert photos
                    for photo in parsed.get("_photos") or []:
                        if not isinstance(photo, dict):
                            continue
                        photo_id = str(photo.get("photo_id") or photo.get("id") or "")
                        large_url = photo.get("large") or photo.get("video")
                        thumb_url = photo.get("thumbnail") or photo.get("small")
                        if not photo_id or (not large_url and not thumb_url):
                            continue
                        try:
                            async with self.pool.acquire() as conn:
                                athlete_row = await conn.fetchrow(
                                    "SELECT id FROM strava_athletes WHERE platform_athlete_id = $1",
                                    athlete_id_int,
                                )
                                athlete_uuid = athlete_row["id"] if athlete_row else None
                                await conn.execute(
                                    """INSERT INTO strava_activity_photos
                                       (platform_photo_id, platform_activity_id, athlete_id,
                                        activity_name, athlete_name, caption, media_type,
                                        source_url_large, source_url_thumbnail, activity_date, source)
                                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                                       ON CONFLICT (platform_photo_id, platform_activity_id) DO UPDATE SET
                                         source_url_large = COALESCE(EXCLUDED.source_url_large, strava_activity_photos.source_url_large),
                                         source_url_thumbnail = COALESCE(EXCLUDED.source_url_thumbnail, strava_activity_photos.source_url_thumbnail)
                                    """,
                                    photo_id, parsed["id"], athlete_uuid,
                                    parsed.get("name"), str(athlete_id_int),
                                    photo.get("caption_escaped"),
                                    int(photo.get("media_type") or 1),
                                    large_url, thumb_url,
                                    _dt.fromisoformat(parsed["start_date"].replace("Z", "+00:00")) if parsed.get("start_date") else None,
                                    "historical_backfill",
                                )
                                total_photos += 1
                                if large_url and not self.is_known(f"{parsed['id']}_{photo_id}"):
                                    await self.download_media({
                                        "entity_id": str(athlete_id_int),
                                        "entity_name": str(athlete_id_int),
                                        "content_type": "activity_photo",
                                        "content_id": f"{parsed['id']}_{photo_id}",
                                        "url": large_url,
                                        "extension": "jpg",
                                        "source_url": f"https://www.strava.com/activities/{parsed['id']}",
                                    })
                        except Exception as e:
                            logger.debug("strava history photo upsert failed: %s", e)

                logger.info("strava history %s month %s: %d activities, %d photos (total acts=%d photos=%d)",
                            athlete_id, cursor, month_count, total_photos, total_activities, total_photos)
                cursor = _prev_month(cursor)

        logger.info("strava history backfill complete: athlete=%s total_activities=%d total_photos=%d",
                    athlete_id, total_activities, total_photos)
        return total_activities

    # ------------------------------------------------------------------ #
    # Activity-page photo scraping (individual /activities/{id} pages)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _decode_polyline(encoded: str) -> list[list[float]]:
        """Decode a Google encoded polyline into [[lat, lng], ...] pairs."""
        result = []
        index = lat = lng = 0
        while index < len(encoded):
            for attr in range(2):
                shift = 0
                value = 0
                while True:
                    b = ord(encoded[index]) - 63
                    index += 1
                    value |= (b & 0x1F) << shift
                    shift += 5
                    if b < 0x20:
                        break
                delta = ~(value >> 1) if (value & 1) else (value >> 1)
                if attr == 0:
                    lat += delta
                else:
                    lng += delta
            result.append([lat / 1e5, lng / 1e5])
        return result

    async def _scrape_activity_page(self, client: httpx.AsyncClient, activity_id: int,
                                    athlete_id: str, athlete_name: str) -> dict:
        """Scrape an individual activity page for photos, polyline, kudos, and comments.

        Returns dict with counts: {"photos": N, "kudos": N, "comments": N, "polyline": bool}
        """
        from html import unescape as _unescape

        result = {"photos": 0, "kudos": 0, "comments": 0, "polyline": False, "streams": 0}
        url = f"{STRAVA_WEB}/activities/{activity_id}"
        try:
            resp = await client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml",
            })
        except Exception as e:
            logger.debug("strava scrape: fetch %s failed: %s", activity_id, e)
            return result
        if resp.status_code != 200:
            return result

        html = resp.text
        props_count = len(re.findall(r"data-react-props='", html))
        logger.info("strava scrape: activity %s status=%d size=%dKB props_blocks=%d",
                     activity_id, resp.status_code, len(html) // 1024, props_count)

        # --- Extract GPS from inline JavaScript (pageView factory pattern) ---
        # Strava embeds map bounding box and streams flag in the ViewFactory call:
        #   pageView = new Strava.Labs.Activities.RunPageView(id, type, factory)
        #     .mbr([[min_lat,min_lng],[max_lat,max_lng]])   <-- bounding box
        #     .hasStreams(true)                              <-- GPS track available
        # If hasStreams=true, fetch /activities/{id}/streams for the actual latlng array.
        has_streams_match = re.search(r'\.hasStreams\((\w+)\)', html)
        has_streams = has_streams_match and has_streams_match.group(1) == 'true'
        if has_streams and not self._gps_stream_cooling_down():
            try:
                streams_url = f"{STRAVA_WEB}/activities/{activity_id}/streams"
                async def _get_page_streams():
                    return await client.get(
                        streams_url,
                        params={"stream_types[]": ["latlng"]},
                        headers={
                            "X-Requested-With": "XMLHttpRequest",
                            "Accept": "application/json, text/javascript, */*; q=0.01",
                            "Referer": url,
                        },
                    )

                streams_resp = await _get_page_streams()
                if streams_resp.status_code == 429:
                    retry_resp = await self._retry_gps_stream_after_429(
                        _get_page_streams,
                        activity_id=activity_id,
                        context="page",
                    )
                    if retry_resp is not None:
                        streams_resp = retry_resp
                if streams_resp.status_code == 200:
                    streams_data = streams_resp.json()
                    latlng = streams_data.get("latlng", [])
                    if latlng and len(latlng) >= 2:
                        start = latlng[0]
                        end = latlng[-1]
                        sl = f"{start[0]},{start[1]}" if len(start) == 2 else None
                        el = f"{end[0]},{end[1]}" if len(end) == 2 else None
                        polyline = _encode_polyline(latlng)
                        async with self.pool.acquire() as conn:
                            await conn.execute(
                                "UPDATE strava_activities "
                                "SET start_latlng = COALESCE(start_latlng, $1), "
                                "    end_latlng   = COALESCE(end_latlng, $2), "
                                "    summary_polyline = COALESCE(NULLIF(summary_polyline, ''), $3), "
                                "    stream_status = COALESCE(stream_status, 'ok') "
                                "WHERE platform_activity_id = $4",
                                sl, el, polyline, activity_id,
                            )
                            act_row = await conn.fetchrow(
                                "SELECT id FROM strava_activities WHERE platform_activity_id = $1",
                                activity_id,
                            )
                            if act_row:
                                await conn.execute(
                                    "INSERT INTO strava_gps_streams (activity_id, latlng, collected_at) "
                                    "VALUES ($1, $2, NOW()) "
                                    "ON CONFLICT (activity_id) DO UPDATE SET "
                                    "latlng = EXCLUDED.latlng, collected_at = NOW()",
                                    act_row["id"], json.dumps(latlng),
                                )
                        result["streams"] = len(latlng)
                        logger.info("strava scrape: activity %s streams %d points, start=%s sl=%r el=%r",
                                    activity_id, len(latlng), latlng[0], sl, el)
                elif streams_resp.status_code == 429:
                    self._set_gps_stream_cooldown(activity_id, "page")
                    logger.info("strava scrape: streams 429 for %s — skipping GPS", activity_id)
            except Exception as e:
                logger.warning("strava scrape: streams fetch %s failed: %s", activity_id, e)

        # --- Extract data from data-react-props blocks ---
        # Strava uses single-quoted props with &quot; for JSON quotes
        for m in re.finditer(r"data-react-props='([^']+)'", html):
            raw = _unescape(m.group(1))
            try:
                props = json.loads(raw)
            except Exception:
                continue
            if not isinstance(props, dict):
                continue

            # Photos
            items = props.get("items") or props.get("photos") or []
            if isinstance(items, list) and items:
                for photo in items:
                    if not isinstance(photo, dict):
                        continue
                    photo_id = str(photo.get("photo_id") or photo.get("id") or "")
                    large_url = photo.get("large") or photo.get("video")
                    thumb_url = photo.get("thumbnail") or photo.get("small")
                    if not photo_id or (not large_url and not thumb_url):
                        continue
                    try:
                        athlete_id_int = int(athlete_id)
                        async with self.pool.acquire() as conn:
                            athlete_row = await conn.fetchrow(
                                "SELECT id FROM strava_athletes WHERE platform_athlete_id = $1",
                                athlete_id_int)
                            athlete_uuid = athlete_row["id"] if athlete_row else None
                            await conn.execute("""
                                INSERT INTO strava_activity_photos
                                   (platform_photo_id, platform_activity_id, athlete_id,
                                    activity_name, athlete_name, caption, media_type,
                                    source_url_large, source_url_thumbnail, activity_date, source)
                                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,NOW(),$10)
                                ON CONFLICT (platform_photo_id, platform_activity_id) DO UPDATE SET
                                  source_url_large = COALESCE(EXCLUDED.source_url_large, strava_activity_photos.source_url_large),
                                  source_url_thumbnail = COALESCE(EXCLUDED.source_url_thumbnail, strava_activity_photos.source_url_thumbnail)
                            """, photo_id, activity_id, athlete_uuid,
                                None, athlete_name,
                                photo.get("caption_escaped") or photo.get("caption"),
                                int(photo.get("media_type") or 1),
                                large_url, thumb_url, "activity_page_scrape")
                        result["photos"] += 1
                        if large_url and not self.is_known(f"{activity_id}_{photo_id}"):
                            await self.download_media({
                                "entity_id": athlete_id,
                                "entity_name": athlete_name,
                                "content_type": "activity_photo",
                                "content_id": f"{activity_id}_{photo_id}",
                                "url": large_url,
                                "extension": "jpg",
                                "source_url": f"https://www.strava.com/activities/{activity_id}",
                            })
                    except Exception as e:
                        logger.debug("strava scrape: photo %s/%s failed: %s",
                                     activity_id, photo_id, e)

            # GPS fields: start/end latlng + timezone/utc_offset.
            # Strava embeds these in the activity props block. Check both top-level
            # and nested under "activity" key (layout varies by page version).
            act_props = props.get("activity") or props
            _sll = act_props.get("start_latlng") or props.get("startLatLng")
            _ell = act_props.get("end_latlng") or props.get("endLatLng")
            _tz  = act_props.get("timezone") or props.get("timezone")
            _utc = act_props.get("utc_offset") if act_props.get("utc_offset") is not None \
                   else props.get("utc_offset")
            if _sll or _tz:
                def _ll_str(v):
                    return f"{v[0]},{v[1]}" if v and len(v) == 2 else None
                try:
                    async with self.pool.acquire() as conn:
                        await conn.execute("""
                            UPDATE strava_activities
                            SET start_latlng = COALESCE(start_latlng, $1),
                                end_latlng   = COALESCE(end_latlng,   $2),
                                timezone     = COALESCE(timezone,     $3),
                                utc_offset   = COALESCE(utc_offset,   $4)
                            WHERE platform_activity_id = $5
                        """, _ll_str(_sll), _ll_str(_ell), _tz,
                             int(_utc) if _utc is not None else None,
                             activity_id)
                except Exception as e:
                    logger.debug("strava scrape: gps fields %s failed: %s", activity_id, e)

            # Polyline from activity map props
            polyline = (props.get("polyline") or props.get("summary_polyline")
                        or (props.get("map") or {}).get("polyline")
                        or (props.get("map") or {}).get("summary_polyline")
                        or (props.get("activityMap") or {}).get("polyline"))
            if polyline and isinstance(polyline, str) and len(polyline) > 10:
                try:
                    async with self.pool.acquire() as conn:
                        await conn.execute("""
                            UPDATE strava_activities
                            SET summary_polyline = $1
                            WHERE platform_activity_id = $2
                              AND (summary_polyline IS NULL OR summary_polyline = '')
                        """, polyline, activity_id)
                        act_row = await conn.fetchrow(
                            "SELECT id FROM strava_activities WHERE platform_activity_id = $1",
                            activity_id)
                        if act_row:
                            exists = await conn.fetchval(
                                "SELECT 1 FROM strava_gps_streams WHERE activity_id = $1",
                                act_row["id"])
                            if not exists:
                                latlng = self._decode_polyline(polyline)
                                await conn.execute("""
                                    INSERT INTO strava_gps_streams (activity_id, latlng, collected_at)
                                    VALUES ($1, $2, NOW())
                                    ON CONFLICT (activity_id) DO NOTHING
                                """, act_row["id"], json.dumps(latlng))
                    result["polyline"] = True
                except Exception as e:
                    logger.debug("strava scrape: polyline %s failed: %s", activity_id, e)

            # Kudos + comments from the social interaction props block
            if "kudosCount" in props or "comments" in props:
                # Kudos: the props block has kudosCount but individual kudoers
                # are only available via the kudos modal API endpoint
                kudos_count = props.get("kudosCount", 0)

                if kudos_count and kudos_count > 0:
                    try:
                        kudos_url = f"{STRAVA_WEB}/feed/activity/{activity_id}/kudos"
                        kresp = await client.get(kudos_url, headers={
                            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                            "Accept": "application/json",
                            "X-Requested-With": "XMLHttpRequest",
                        })
                        if kresp.status_code == 200:
                            try:
                                kdata = kresp.json()
                            except Exception:
                                kdata = {}
                            kudoers = kdata.get("athletes", []) if isinstance(kdata, dict) else []
                            for kudo in kudoers:
                                if not isinstance(kudo, dict):
                                    continue
                                kid = kudo.get("id")
                                kname = kudo.get("name", "")
                                if kid:
                                    try:
                                        async with self.pool.acquire() as conn:
                                            await conn.execute("""
                                                INSERT INTO strava_activity_kudos
                                                    (platform_activity_id, platform_athlete_id, athlete_name)
                                                VALUES ($1, $2, $3)
                                                ON CONFLICT (platform_activity_id, platform_athlete_id) DO NOTHING
                                            """, activity_id, int(kid), kname)
                                        result["kudos"] += 1
                                        # SPIDER: enqueue the kudoer so we crawl
                                        # out through kudos (user request).
                                        await self._enqueue_athlete(kid, kname, "kudos")
                                    except Exception:
                                        pass
                    except Exception as e:
                        logger.debug("strava scrape: kudos fetch %s failed: %s", activity_id, e)

                # Comments from the props JSON
                comments = props.get("comments", [])
                if isinstance(comments, list):
                    for c in comments:
                        if not isinstance(c, dict):
                            continue
                        cid = c.get("athleteId") or c.get("athlete_id")
                        cname = (c.get("athleteName") or c.get("athlete_name")
                                 or f"{c.get('firstname', '')} {c.get('lastname', '')}".strip())
                        ctext = c.get("comment") or c.get("text") or c.get("body") or ""
                        if cid and ctext:
                            try:
                                ts = c.get("timestamp") or c.get("created_at")
                                from datetime import datetime as _dt
                                ts_val = None
                                if ts:
                                    if isinstance(ts, (int, float)):
                                        ts_val = _dt.utcfromtimestamp(ts)
                                    elif isinstance(ts, str):
                                        try:
                                            ts_val = _dt.fromisoformat(ts.replace("Z", "+00:00"))
                                        except Exception:
                                            pass
                                async with self.pool.acquire() as conn:
                                    await conn.execute("""
                                        INSERT INTO strava_activity_comments
                                            (platform_activity_id, platform_athlete_id,
                                             athlete_name, comment_text, platform_created_at)
                                        VALUES ($1, $2, $3, $4, $5)
                                        ON CONFLICT (platform_activity_id, platform_athlete_id, platform_created_at)
                                            DO NOTHING
                                    """, activity_id, int(cid), cname or "", ctext, ts_val)
                                result["comments"] += 1
                                # SPIDER: enqueue the commenter (crawl out via comments).
                                await self._enqueue_athlete(cid, cname, "comment")
                            except Exception:
                                pass

        # If polyline wasn't found in HTML props, try the web streams endpoint.
        # Strava removed polylines from HTML in 2024, but the web streams path
        # still returns JSON with cookie auth (the old stravatoolkit used this).
        # URL: /activities/{id}/streams?stream_types[]=latlng&stream_types[]=time
        if not result["polyline"] and self._gps_enabled and not self._gps_stream_cooling_down():
            try:
                streams_url = f"{STRAVA_WEB}/activities/{activity_id}/streams"
                async def _get_fallback_streams():
                    return await client.get(
                        streams_url,
                        headers={
                            "Accept": "application/json, text/plain, */*",
                            "X-Requested-With": "XMLHttpRequest",
                            "Referer": f"{STRAVA_WEB}/activities/{activity_id}",
                        },
                        params={"stream_types[]": ["latlng", "time"]},
                    )

                sresp = await _get_fallback_streams()
                if sresp.status_code == 429:
                    retry_resp = await self._retry_gps_stream_after_429(
                        _get_fallback_streams,
                        activity_id=activity_id,
                        context="page-fallback",
                    )
                    if retry_resp is not None:
                        sresp = retry_resp
                ctype = sresp.headers.get("content-type", "")
                if sresp.status_code == 200 and "application/json" in ctype:
                    sdata = sresp.json()
                    latlng = []
                    if isinstance(sdata, dict):
                        raw_ll = sdata.get("latlng", {})
                        latlng = raw_ll.get("data", []) if isinstance(raw_ll, dict) else raw_ll
                    elif isinstance(sdata, list):
                        for entry in sdata:
                            if isinstance(entry, dict) and entry.get("type") == "latlng":
                                latlng = entry.get("data", [])
                                break
                    if latlng:
                        start = latlng[0]
                        end = latlng[-1]
                        sl = f"{start[0]},{start[1]}" if start and len(start) == 2 else None
                        el = f"{end[0]},{end[1]}" if end and len(end) == 2 else None
                        polyline = _encode_polyline(latlng)
                        async with self.pool.acquire() as conn:
                            act_row = await conn.fetchrow(
                                "SELECT id FROM strava_activities WHERE platform_activity_id = $1",
                                activity_id)
                            if act_row:
                                await conn.execute("""
                                    INSERT INTO strava_gps_streams (activity_id, latlng, collected_at)
                                    VALUES ($1, $2, NOW())
                                    ON CONFLICT (activity_id) DO UPDATE SET
                                      latlng = EXCLUDED.latlng,
                                      collected_at = NOW()
                                """, act_row["id"], json.dumps(latlng))
                                await conn.execute("""
                                    UPDATE strava_activities
                                    SET start_latlng = COALESCE(start_latlng, $1),
                                        end_latlng = COALESCE(end_latlng, $2),
                                        summary_polyline = COALESCE(NULLIF(summary_polyline, ''), $3),
                                        stream_status = COALESCE(stream_status, 'ok')
                                    WHERE id = $4
                                """, sl, el, polyline, act_row["id"])
                        result["polyline"] = True
                        logger.info("strava scrape: web streams %s -> %d points", activity_id, len(latlng))
                elif sresp.status_code == 429:
                    self._set_gps_stream_cooldown(activity_id, "page-fallback")
            except Exception as e:
                logger.debug("strava scrape: web streams %s failed: %s", activity_id, e)

        return result

    async def _scrape_activity_pages(self, batch_size: int = 25) -> dict:
        """Scrape activity pages for photos, polylines, kudos, and comments.

        Visits individual activity pages that haven't been scraped yet and
        extracts all available data from each page in a single HTTP request.
        """
        if not self._use_web or not self.pool:
            return {"photos": 0, "kudos": 0, "comments": 0, "polylines": 0}
        jar = self._build_cookie_jar()
        if jar is None:
            return {"photos": 0, "kudos": 0, "comments": 0, "polylines": 0}

        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT a.platform_activity_id,
                       sa.platform_athlete_id,
                       COALESCE(sa.firstname, sa.username, sa.platform_athlete_id::text) as athlete_name
                FROM strava_activities a
                JOIN strava_athletes sa ON sa.id = a.athlete_id
                WHERE a.page_scraped_at IS NULL
                ORDER BY
                    CASE WHEN sa.platform_athlete_id = $2 THEN 0 ELSE 1 END,
                    a.start_date DESC NULLS LAST
                LIMIT $1
            """, batch_size, int(self._my_athlete_id) if self._my_athlete_id else 0)

        totals = {"photos": 0, "kudos": 0, "comments": 0, "polylines": 0, "streams": 0}
        if not rows:
            return totals

        ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        logger.info("strava: scraping %d activity pages (own-first)", len(rows))
        async with httpx.AsyncClient(
            timeout=30, cookies=jar, follow_redirects=True,
            headers={"User-Agent": ua}
        ) as client:
            for row in rows:
                if self._stop.is_set():
                    break
                await self._delay(2.0, 5.0)
                aid = row["platform_activity_id"]
                r = await self._scrape_activity_page(
                    client, aid,
                    str(row["platform_athlete_id"]),
                    row["athlete_name"],
                )
                totals["photos"] += r["photos"]
                totals["kudos"] += r["kudos"]
                totals["comments"] += r["comments"]
                if r["polyline"]:
                    totals["polylines"] += 1
                totals["streams"] += r.get("streams", 0)
                if self._gps_stream_cooling_down():
                    logger.info("strava: stopping page GPS scrape early due to stream cooldown")
                    break
                try:
                    async with self.pool.acquire() as conn:
                        await conn.execute(
                            "UPDATE strava_activities SET page_scraped_at = NOW() WHERE platform_activity_id = $1",
                            aid)
                except Exception:
                    pass
        logger.info("strava: scraped %d pages -> %d photos, %d kudos, %d comments, %d polylines, %d streams",
                    len(rows), totals["photos"], totals["kudos"], totals["comments"], totals["polylines"], totals["streams"])
        return totals

    # ------------------------------------------------------------------ #
    # Athlete name enrichment (scrape profile pages for stub athletes)
    # ------------------------------------------------------------------ #

    async def _enrich_athlete_names(self, batch_size: int = 20) -> int:
        """Find athletes with numeric-only usernames and enrich from profile pages.

        Also splits username into firstname/lastname for athletes that have real
        names in username but NULL firstname.
        """
        if not self._use_web or not self.pool:
            return 0
        jar = self._build_cookie_jar()
        if jar is None:
            return 0

        enriched = 0
        async with self.pool.acquire() as conn:
            # First: split existing usernames into firstname/lastname where missing
            split_rows = await conn.fetch(r"""
                SELECT platform_athlete_id, username
                FROM strava_athletes
                WHERE username IS NOT NULL
                  AND username !~ '^\d+$'
                  AND (firstname IS NULL OR firstname = '' OR firstname ~ '^\d+$')
            """)
            for r in split_rows:
                parts = r["username"].strip().split(None, 1)
                fname = parts[0] if parts else None
                lname = parts[1] if len(parts) > 1 else None
                if fname:
                    await conn.execute(r"""
                        UPDATE strava_athletes
                        SET firstname = $1, lastname = $2, updated_at = NOW()
                        WHERE platform_athlete_id = $3
                          AND (firstname IS NULL OR firstname = '' OR firstname ~ '^\d+$')
                    """, fname, lname, r["platform_athlete_id"])
                    enriched += 1

            # Second: scrape profile pages for athletes with missing/numeric names
            # OR no profile photo yet (user wants every athlete's profile photo).
            stub_rows = await conn.fetch(r"""
                SELECT platform_athlete_id
                FROM strava_athletes
                WHERE username IS NULL OR username = '' OR username ~ '^\d+$' OR profile IS NULL
                ORDER BY (profile IS NOT NULL), updated_at ASC
                LIMIT $1
            """, batch_size)

        if not stub_rows:
            if enriched:
                logger.info("strava: split %d athlete usernames into firstname/lastname", enriched)
            return enriched

        ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        async with httpx.AsyncClient(
            timeout=30, cookies=jar, follow_redirects=True,
            headers={"User-Agent": ua, "Accept": "text/html,application/xhtml+xml"}
        ) as client:
            for row in stub_rows:
                if self._stop.is_set():
                    break
                aid = row["platform_athlete_id"]
                await self._delay(2.0, 5.0)
                try:
                    resp = await client.get(f"{STRAVA_WEB}/athletes/{aid}")
                    if resp.status_code != 200:
                        continue
                    athlete_patch = _extract_strava_profile_from_html(resp.text, aid)
                    if len(athlete_patch) <= 1:
                        continue
                    await self._upsert_athlete(athlete_patch)
                    enriched += 1
                    logger.debug("strava: enriched athlete %s profile fields=%s", aid, sorted(athlete_patch.keys()))
                except Exception as e:
                    logger.debug("strava: enrich athlete %s failed: %s", aid, e)

        logger.info("strava: enriched %d athlete names", enriched)
        return enriched

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
            # Archive the raw club payload under the vault raw tree. There is no
            # dedicated clubs table yet, so the raw payload is the durable source.
            raw = write_raw_payload(
                source=self.SOURCE_NAME,
                artifact_id=f"clubs/{athlete_id}",
                payload={
                    "athlete_id": athlete_id,
                    "clubs": clubs,
                    "collected_at": datetime.now(timezone.utc).isoformat(),
                },
                metadata={
                    "athlete_id": athlete_id,
                    "payload_type": "strava_clubs",
                    "rebuild_target_tables": ["strava_athletes"],
                },
                target_tables=["strava_athletes"],
                root=VAULT_ROOT,
            )
            if not raw.ok:
                logger.warning("strava: clubs raw payload sidecar failed for %s: %s", athlete_id, raw.error)
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
                        self._note_rate_limit(
                            scope="api_activities",
                            cooldown_seconds=self._ratelimit_sleep,
                            reason=f"api activities page {page} returned 429",
                            metadata={"page": page},
                        )
                        await sleep_rate_limit(self._ratelimit_sleep)
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
        save the polyline + bounds as a vault JSON blob and register the file
        in media_items so the unified store can serve it. Render-time
        conversion to PNG is deferred to consumers.
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
                "rebuild_target_tables": ["media_items", "strava_activities", "strava_gps_streams"],
            }
            filename = self.build_filename(
                str(athlete_id or "unknown"),
                str(athlete_id or "unknown"),
                "route_map",
                activity_id,
                extension="json",
            )
            data = json.dumps(payload, indent=2).encode("utf-8")
            sha = self.sha256_bytes(data)
            source_url = self._build_strava_source_url({"content_type": "route_map", "content_id": activity_id})
            artifact = write_atomic_artifact(
                source=self.SOURCE_NAME,
                artifact_id=f"route_{activity_id}",
                artifact_kind="media_blob",
                data=data,
                extension="json",
                expected_sha256=sha,
                metadata={
                    **payload,
                    "filename": filename,
                    "source_url": source_url,
                    "request_url": source_url,
                },
                root=VAULT_ROOT,
            )
            if not artifact.path:
                raise RuntimeError(f"vault artifact write failed: {artifact.error}")
            payload["vault_artifact"] = {
                "ok": artifact.ok,
                "partial": artifact.partial,
                "path": artifact.relative_path,
                "blob_path": artifact.blob_relative_path,
                "sidecar_path": artifact.sidecar.relative_path if artifact.sidecar else None,
                "duplicate_blob": artifact.duplicate_blob,
                "error": artifact.error,
            }
            await self.insert_media_item(
                entity_id=str(athlete_id or "unknown"),
                entity_name=str(athlete_id or "unknown"),
                content_type="route_map",
                content_id=activity_id,
                filename=filename,
                file_path=str(artifact.path),
                file_size=artifact.file_size,
                sha256=artifact.sha256,
                metadata=payload,
                source_url=source_url,
            )
            if artifact.partial:
                await self.send_to_dlq(str(athlete_id or "unknown"), activity_id, f"vault artifact partial: {artifact.error}")
            self._known_ids.add(f"route_{activity_id}")
        except Exception as e:
            logger.warning("strava: route map persist failed for %s: %s",
                           activity.get("id"), e)

    async def _resolve_cookie_athlete_id(self, account_name: str, session_cookie: str) -> str | None:
        jar = httpx.Cookies()
        jar.set("_strava4_session", session_cookie, domain=".strava.com", path="/")
        ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        headers = {"User-Agent": ua, "Accept": "text/html,application/xhtml+xml"}
        try:
            async with httpx.AsyncClient(timeout=30, cookies=jar, follow_redirects=True) as client:
                resp = await client.get(f"{STRAVA_WEB}/dashboard", headers=headers)
                if resp.status_code == 429:
                    self._note_rate_limit(
                        scope="resolve_owner",
                        account=account_name,
                        cooldown_seconds=self._ratelimit_sleep,
                        reason=f"dashboard owner resolve returned 429 for {account_name}",
                    )
                    return None
                if resp.status_code != 200:
                    logger.info("strava owner graph[%s]: dashboard HTTP %d", account_name, resp.status_code)
                    return None
                m = re.search(r'"athlete_id"\s*:\s*(\d+)', resp.text) or re.search(r'/athletes/(\d+)', resp.text)
                return m.group(1) if m else None
        except Exception as e:
            logger.warning("strava owner graph[%s]: resolve failed: %s", account_name, e)
            return None

    async def _collect_owner_rosters_for_cookie_accounts(self) -> set[str]:
        owner_ids: set[str] = set()
        max_pages = int(os.getenv("STRAVA_OWNER_ROSTER_MAX_PAGES", "50"))
        for account_name, cookie in self._cookie_accounts:
            if self._stop.is_set():
                break
            owner_id = await self._resolve_cookie_athlete_id(account_name, cookie)
            if not owner_id:
                logger.info("strava owner graph[%s]: could not resolve athlete id", account_name)
                continue
            if owner_id in owner_ids:
                logger.info("strava owner graph[%s]: same athlete id %s already captured", account_name, owner_id)
                continue
            owner_ids.add(owner_id)
            logger.info("strava owner graph[%s]: capturing rosters for athlete %s", account_name, owner_id)
            await self.collect_following_roster(
                owner_id,
                "following",
                max_pages=max_pages,
                session_cookie=cookie,
                owner_account=owner_id,
                owner_label=account_name,
            )
            await self.collect_following_roster(
                owner_id,
                "followers",
                max_pages=max_pages,
                session_cookie=cookie,
                owner_account=owner_id,
                owner_label=account_name,
            )
        return owner_ids

    async def collect_following_roster(
        self,
        athlete_id: str,
        roster_type: str = "following",
        max_pages: int = 50,
        *,
        session_cookie: str | None = None,
        owner_account: str | None = None,
        owner_label: str | None = None,
    ):
        """Cookie-only HTML scrape of /athletes/{id}/follows?type=following|followers.

        Parses athlete cards using the same regexes as the original toolkit and
        seeds the spider queue (BFS expansion). When athlete_id is the AUTHENTICATED
        owner, each discovered athlete is also recorded in social_users with the
        relationship context ('follow' for following, 'follower' for followers) so
        the Targets network reflects who you follow AND who follows you. Paced by the
        same STRAVA_FEED_DELAY_* + 429 backoff as the rest of the collector (anti-ban).
        Stops on empty page or non-200. Returns the number of seeds enqueued.
        """
        roster_type = "followers" if roster_type == "followers" else "following"
        owner_id = str(owner_account) if owner_account else (str(self._my_athlete_id) if self._my_athlete_id else None)
        is_owner = owner_id is not None and str(athlete_id) == owner_id
        rel_context = "follower" if roster_type == "followers" else "follow"
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
        if session_cookie:
            jar.set("_strava4_session", session_cookie, domain=".strava.com", path="/")
        elif os.path.exists(self._cookies_file):
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
            no_growth_pages = 0
            while not self._stop.is_set() and page <= max_pages:
                await self._delay(self._feed_delay_min, self._feed_delay_max)
                try:
                    resp = await client.get(
                        f"{STRAVA_WEB}/athletes/{athlete_id}/follows",
                        params={"type": roster_type, "page": page},
                        headers=headers,
                    )
                except Exception as e:
                    logger.warning("strava roster: page %d fetch error: %s", page, e)
                    break
                if resp.status_code == 429:
                    self._note_rate_limit(
                        scope="roster",
                        cooldown_seconds=self._ratelimit_sleep,
                        reason=f"roster {roster_type} page {page} for {athlete_id} returned 429",
                        metadata={"page": page, "athlete_id": str(athlete_id), "roster_type": roster_type},
                    )
                    await sleep_rate_limit(self._ratelimit_sleep); continue
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
                new_this_page = 0
                for entry in discovered:
                    if entry["athlete_id"] in seen:
                        continue
                    seen.add(entry["athlete_id"])
                    new_this_page += 1
                    try:
                        async with self.pool.acquire() as conn:
                            await conn.execute(
                                """
                                INSERT INTO strava_spider_queue (
                                    platform_athlete_id, source, priority, status
                                ) VALUES ($1, $2, 5, 'pending')
                                ON CONFLICT (platform_athlete_id) DO NOTHING
                                """,
                                entry["athlete_id"], roster_type,
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
                            # Record MY follow-graph edge in social_users (only for the
                            # authenticated owner's own roster — otherwise this is just
                            # discovery of someone else's list).
                            if is_owner:
                                nm = entry["name"][:255] if entry.get("name") else None
                                target_uid = str(entry["athlete_id"])
                                direction = "follower" if rel_context == "follower" else "following"
                                await conn.execute(
                                    """
                                    INSERT INTO social_users
                                        (platform, uid, platform_user_id, username, display_name,
                                         profile_photo_url, contexts, first_seen, last_seen, times_seen)
                                    VALUES ('strava', $1, $1, $2, $2, $3, ARRAY[$4], now(), now(), 1)
                                    ON CONFLICT (platform, uid) DO UPDATE SET
                                        last_seen = now(),
                                        times_seen = social_users.times_seen + 1,
                                        username = COALESCE(social_users.username, EXCLUDED.username),
                                        display_name = COALESCE(social_users.display_name, EXCLUDED.display_name),
                                        profile_photo_url = COALESCE(social_users.profile_photo_url, EXCLUDED.profile_photo_url),
                                        contexts = (SELECT array(SELECT DISTINCT unnest(social_users.contexts || EXCLUDED.contexts)))
                                    """,
                                    target_uid, nm, entry.get("avatar_url"), rel_context,
                                )
                                await conn.execute(
                                    """
                                    INSERT INTO follow_edges
                                        (platform, owner_account, target_uid, direction,
                                         target_username, first_seen, last_seen)
                                    VALUES ('strava', $1, $2, $3, $4, now(), now())
                                    ON CONFLICT (platform, owner_account, target_uid, direction)
                                    DO UPDATE SET
                                        last_seen = now(),
                                        target_username = COALESCE(EXCLUDED.target_username, follow_edges.target_username)
                                    """,
                                    owner_id, target_uid, direction, nm,
                                )
                        seeded += 1
                    except Exception as e:
                        logger.warning("strava roster: seed %s failed: %s",
                                       entry["athlete_id"], e)
                logger.info("strava roster: page %d -> %d athletes (running seeded=%d)",
                            page, len(discovered), seeded)
                if new_this_page:
                    self._progress_count += new_this_page
                if new_this_page == 0:
                    no_growth_pages += 1
                    if no_growth_pages >= 2:
                        logger.info(
                            "strava roster: stopping %s for %s after %d duplicate-only pages",
                            roster_type, athlete_id, no_growth_pages,
                        )
                        break
                else:
                    no_growth_pages = 0
                page += 1
        logger.info(
            "strava roster: seeded %d new spider entries from %s%s",
            seeded,
            athlete_id,
            f" ({owner_label})" if owner_label else "",
        )
        return seeded

    async def _backfill_owner_follow_edges_from_social_users(self) -> dict[str, int]:
        """Bridge historical owner roster contexts into follow_edges.

        Older Strava roster passes recorded the authenticated owner's follow graph
        in social_users.contexts only. Keep this idempotent bridge so those rows
        become proximity input without waiting for a fresh Strava web scrape.
        """
        if not self.pool or not self._my_athlete_id:
            return {"following": 0, "follower": 0}
        owner = str(self._my_athlete_id)
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                WITH expanded AS (
                    SELECT
                        uid,
                        username,
                        first_seen,
                        last_seen,
                        CASE
                            WHEN ctx = 'follow' THEN 'following'
                            WHEN ctx = 'follower' THEN 'follower'
                        END AS direction
                    FROM social_users su
                    CROSS JOIN LATERAL unnest(su.contexts) AS ctx
                    WHERE su.platform = 'strava'
                      AND ctx IN ('follow', 'follower')
                      AND su.uid IS NOT NULL
                      AND su.uid <> $1
                ),
                inserted AS (
                    INSERT INTO follow_edges
                        (platform, owner_account, target_uid, direction,
                         target_username, first_seen, last_seen)
                    SELECT
                        'strava', $1, uid, direction, username,
                        COALESCE(first_seen, now()), COALESCE(last_seen, now())
                    FROM expanded
                    WHERE direction IS NOT NULL
                    ON CONFLICT (platform, owner_account, target_uid, direction)
                    DO UPDATE SET
                        last_seen = GREATEST(follow_edges.last_seen, EXCLUDED.last_seen),
                        target_username = COALESCE(EXCLUDED.target_username, follow_edges.target_username)
                    RETURNING direction
                )
                SELECT direction, count(*) AS n
                FROM inserted
                GROUP BY direction
                """,
                owner,
            )
        out = {"following": 0, "follower": 0}
        for r in rows:
            out[r["direction"]] = int(r["n"] or 0)
        if out["following"] or out["follower"]:
            logger.info(
                "strava owner graph[%s] backfilled: followers=%d following=%d",
                owner, out["follower"], out["following"],
            )
        return out
