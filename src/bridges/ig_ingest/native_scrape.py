"""Public-page browser scraping for Pinterest, Snapchat, and Airbnb.

The extension reads only content rendered in its active tab. This bridge owns
the small, paced target queue and writes each platform's public records to its
native tables; it does not fetch media or call private platform APIs.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from urllib.parse import urlparse

from aiohttp import web

from .cors import _cors

logger = logging.getLogger("social_ingest")

_PLATFORMS = {"pinterest", "snapchat", "airbnb"}
_MAX_ITEMS = {"pinterest": 50, "snapchat": 25, "airbnb": 30}
_MAX_ATTEMPTS = 5
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")
_AIRBNB_HOSTS = ("airbnb.com", "airbnb.com.sg", "airbnb.co.uk", "airbnb.co.kr")


def _text(value: object, limit: int = 1000) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:limit] or None


def _integer(value: object, minimum: int = 0) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if minimum <= number <= 2_147_483_647 else None


def _rating(value: object) -> float | None:
    try:
        rating = float(value)
    except (TypeError, ValueError):
        return None
    return rating if 0 <= rating <= 5 else None


def _decimal(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if 0 <= number <= 999.9 else None


def _timestamp(value: object) -> str | None:
    text = _text(value, 64)
    if not text:
        return None
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return text


def _metadata(value: object) -> str:
    if not isinstance(value, dict):
        value = {}
    safe = {
        str(key)[:64]: (val[:1000] if isinstance(val, str) else val)
        for key, val in list(value.items())[:30]
        if isinstance(val, (str, int, float, bool, type(None)))
    }
    encoded = json.dumps(safe, ensure_ascii=False, default=str)
    return encoded if len(encoded) <= 8000 else "{}"


def _https_url(value: object, limit: int = 2048) -> str | None:
    text = _text(value, limit)
    if not text:
        return None
    parsed = urlparse(text)
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    return text


def _platform_url_allowed(platform: str, value: object) -> bool:
    url = _https_url(value)
    if not url:
        return False
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    if platform == "pinterest":
        return host == "pinterest.com" or host.endswith(".pinterest.com")
    if platform == "snapchat":
        return host == "snapchat.com" or host.endswith(".snapchat.com")
    if platform == "airbnb":
        return any(host == root or host.endswith("." + root) for root in _AIRBNB_HOSTS)
    return False


def _same_target(platform: str, page_url: str, target_url: str) -> bool:
    try:
        page = urlparse(page_url)
        target = urlparse(target_url)
        if platform == "airbnb":
            page_match = re.search(r"/(rooms|users/(?:show|profile))/([^/?#]+)", page.path, re.IGNORECASE)
            target_match = re.search(r"/(rooms|users/(?:show|profile))/([^/?#]+)", target.path, re.IGNORECASE)
            return bool(
                page_match and target_match
                and page_match.group(1).lower() == target_match.group(1).lower()
                and page_match.group(2) == target_match.group(2)
            )
        page_host = (page.hostname or "").lower().removeprefix("www.")
        target_host = (target.hostname or "").lower().removeprefix("www.")
        return page_host == target_host and page.path.rstrip("/") == target.path.rstrip("/")
    except Exception:
        return False


async def _seed_targets(conn, platform: str) -> None:
    if platform == "pinterest":
        await conn.execute(
            """
            INSERT INTO browser_native_scrape_queue (platform, target_url, username, source)
            SELECT 'pinterest', 'https://www.pinterest.com/' || btrim(username) || '/',
                   btrim(username), 'pinterest_profiles'
              FROM pinterest_profiles p
             WHERE btrim(p.username) ~ '^[A-Za-z0-9._-]{1,100}$'
               AND NOT EXISTS (
                    SELECT 1 FROM browser_native_scrape_queue q
                     WHERE q.platform='pinterest'
                       AND q.username=btrim(p.username)
               )
             ORDER BY p.probed_at DESC NULLS LAST
             LIMIT 100
            ON CONFLICT (platform, target_url) DO NOTHING
            """
        )
    elif platform == "snapchat":
        await conn.execute(
            """
            INSERT INTO browser_native_scrape_queue (platform, target_url, username, source)
            SELECT 'snapchat', 'https://www.snapchat.com/add/' || btrim(username) || '/',
                   btrim(username), 'snapchat_profiles'
              FROM snapchat_profiles p
             WHERE btrim(p.username) ~ '^[A-Za-z0-9._-]{1,100}$'
               AND NOT EXISTS (
                    SELECT 1 FROM browser_native_scrape_queue q
                     WHERE q.platform='snapchat'
                       AND q.username=btrim(p.username)
               )
             ORDER BY p.probed_at DESC NULLS LAST
             LIMIT 100
            ON CONFLICT (platform, target_url) DO NOTHING
            """
        )
    else:
        await conn.execute(
            """
            INSERT INTO browser_native_scrape_queue (platform, target_url, username, source, priority)
            SELECT 'airbnb', l.url, NULL, 'discovered_links', 10
              FROM discovered_links l
             WHERE l.domain ~* '(^|\\.)airbnb\\.(com|com\\.sg|co\\.uk|co\\.kr)$'
               AND l.url ~* '^https://([^/]+\\.)?airbnb\\.(com|com\\.sg|co\\.uk|co\\.kr)/rooms/[0-9]+'
               AND COALESCE(l.status, '') NOT IN ('404', 'error', 'ignored')
               AND NOT EXISTS (
                    SELECT 1 FROM browser_native_scrape_queue q
                     WHERE q.platform='airbnb' AND q.target_url=l.url
               )
             ORDER BY l.discovered_at DESC NULLS LAST
             LIMIT 100
            ON CONFLICT (platform, target_url) DO NOTHING
            """
        )
        await conn.execute(
            """
            INSERT INTO browser_native_scrape_queue (platform, target_url, username, source, priority)
            SELECT 'airbnb', 'https://www.airbnb.com/users/show/' || btrim(username),
                   btrim(username), 'airbnb_profiles', 5
              FROM airbnb_profiles p
             WHERE btrim(p.username) ~ '^[0-9]{1,20}$'
               AND NOT EXISTS (
                    SELECT 1 FROM browser_native_scrape_queue q
                     WHERE q.platform='airbnb' AND q.username=btrim(p.username)
               )
             ORDER BY p.probed_at DESC NULLS LAST
             LIMIT 100
            ON CONFLICT (platform, target_url) DO NOTHING
            """
        )


async def native_scrape_target(request: web.Request) -> web.Response:
    from . import get_pool

    platform = str(request.query.get("platform") or "").strip().lower()
    if platform not in _PLATFORMS:
        return _cors(web.json_response({"ok": False, "error": "unsupported_platform"}, status=400))
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", f"browser-native-scrape:{platform}")
                await _seed_targets(conn, platform)
                row = await conn.fetchrow(
                    """
                    UPDATE browser_native_scrape_queue q
                       SET status='claimed', attempts=q.attempts+1,
                           claim_expires_at=now()+interval '20 minutes',
                           updated_at=now(), last_error=NULL
                     WHERE q.id=(
                        SELECT due.id
                          FROM browser_native_scrape_queue due
                         WHERE due.platform=$1
                           AND due.attempts < $2
                           AND due.next_visit_at <= now()
                           AND (due.status IN ('pending','failed')
                                OR (due.status='claimed' AND due.claim_expires_at <= now()))
                           AND NOT EXISTS (
                                SELECT 1 FROM browser_native_scrape_queue active
                                 WHERE active.platform=$1
                                   AND active.status='claimed'
                                   AND active.claim_expires_at > now()
                           )
                         ORDER BY due.priority DESC, due.next_visit_at, due.id
                         LIMIT 1
                         FOR UPDATE SKIP LOCKED
                     )
                    RETURNING q.id, q.target_url, q.username, q.attempts
                    """,
                    platform,
                    _MAX_ATTEMPTS,
                )
        if row is None:
            return _cors(web.json_response({"ok": True, "target": None}))
        return _cors(web.json_response({"ok": True, "target": dict(row)}))
    except Exception as exc:
        logger.warning("native scrape target failed for %s: %s", platform, type(exc).__name__)
        return _cors(web.json_response({"ok": False, "error": "target_unavailable"}, status=503))


def _validated_item(platform: str, item: object) -> tuple[str, tuple] | None:
    if not isinstance(item, dict):
        return None
    item_type = str(item.get("type") or "").strip().lower()
    meta = _metadata(item.get("metadata"))
    username = _text(item.get("username"), 100)

    if platform == "pinterest" and item_type == "pin":
        pin_id = _text(item.get("id"), 200)
        pin_url = _https_url(item.get("url"))
        if not pin_id or not _ID_RE.fullmatch(pin_id) or not pin_url or not _platform_url_allowed(platform, pin_url):
            return None
        return "pin", (
            pin_id, username, _text(item.get("board_id"), 200),
            _https_url(item.get("board_url")) if _platform_url_allowed(platform, item.get("board_url")) else None,
            _text(item.get("title"), 500), _text(item.get("description"), 4000),
            _https_url(item.get("image_url")), _https_url(item.get("destination_url")), pin_url, meta,
        )
    if platform == "pinterest" and item_type == "board":
        board_id = _text(item.get("id"), 200)
        board_url = _https_url(item.get("url"))
        if not board_id or not _ID_RE.fullmatch(board_id) or not board_url or not _platform_url_allowed(platform, board_url):
            return None
        return "board", (
            board_id, username, _text(item.get("title"), 500), board_url,
            _integer(item.get("pin_count")), meta,
        )
    if platform == "snapchat" and item_type == "story":
        story_id = _text(item.get("id"), 200)
        story_url = _https_url(item.get("url"))
        if not story_id or not _ID_RE.fullmatch(story_id) or not story_url or not _platform_url_allowed(platform, story_url):
            return None
        return "story", (
            story_id, username, _text(item.get("title"), 500), story_url,
            _https_url(item.get("media_url")), _https_url(item.get("thumbnail_url")),
            _timestamp(item.get("posted_at")), _timestamp(item.get("expires_at")), meta,
        )
    if platform == "airbnb" and item_type == "listing":
        listing_id = _text(item.get("id"), 200)
        listing_url = _https_url(item.get("url"))
        if not listing_id or not _ID_RE.fullmatch(listing_id) or not listing_url or not _platform_url_allowed(platform, listing_url):
            return None
        return "listing", (
            listing_id, _text(item.get("host_id"), 200), _text(item.get("host_name"), 300),
            _https_url(item.get("host_profile_url")), _text(item.get("title"), 500), listing_url,
            _text(item.get("location"), 500), _https_url(item.get("image_url")), _rating(item.get("rating")),
            _integer(item.get("reviews_count")), _text(item.get("property_type"), 200),
            _decimal(item.get("bedrooms")), _decimal(item.get("bathrooms")), _integer(item.get("max_guests")), meta,
        )
    if platform == "airbnb" and item_type == "host":
        host_id = _text(item.get("id"), 200)
        profile_url = _https_url(item.get("url"))
        if not host_id or not _ID_RE.fullmatch(host_id) or not profile_url or not _platform_url_allowed(platform, profile_url):
            return None
        return "host", (
            host_id, _text(item.get("display_name"), 300), profile_url, _https_url(item.get("avatar_url")),
            _rating(item.get("rating")), _integer(item.get("reviews_count")), _text(item.get("host_since"), 64), meta,
        )
    return None


async def native_scrape_ingest(request: web.Request) -> web.Response:
    from . import _safe_json, get_pool

    body = await _safe_json(request)
    platform = str(body.get("platform") or "").strip().lower()
    if platform not in _PLATFORMS or not _platform_url_allowed(platform, body.get("page_url")):
        return _cors(web.json_response({"ok": False, "error": "invalid_platform_or_page"}, status=400))
    target_id = _integer(body.get("target_id"), minimum=1)
    status = str(body.get("status") or "success").strip().lower()
    raw_items = body.get("items") if isinstance(body.get("items"), list) else []
    valid = [row for row in (_validated_item(platform, item) for item in raw_items[:_MAX_ITEMS[platform]]) if row]
    accepted = {"pin": 0, "board": 0, "story": 0, "listing": 0, "host": 0}

    statements = {
        "pin": """
            INSERT INTO pinterest_pins
                (pin_id, username, board_id, board_url, title, description, image_url,
                 destination_url, pin_url, collected_at, metadata)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,now(),$10::jsonb)
            ON CONFLICT (pin_id) DO UPDATE SET
                username=COALESCE(EXCLUDED.username,pinterest_pins.username),
                board_id=COALESCE(EXCLUDED.board_id,pinterest_pins.board_id),
                board_url=COALESCE(EXCLUDED.board_url,pinterest_pins.board_url),
                title=COALESCE(EXCLUDED.title,pinterest_pins.title),
                description=COALESCE(EXCLUDED.description,pinterest_pins.description),
                image_url=COALESCE(EXCLUDED.image_url,pinterest_pins.image_url),
                destination_url=COALESCE(EXCLUDED.destination_url,pinterest_pins.destination_url),
                pin_url=EXCLUDED.pin_url,collected_at=now(),metadata=pinterest_pins.metadata||EXCLUDED.metadata
        """,
        "board": """
            INSERT INTO pinterest_boards (board_id,username,name,board_url,pin_count,collected_at,metadata)
            VALUES ($1,$2,$3,$4,$5,now(),$6::jsonb)
            ON CONFLICT (board_id) DO UPDATE SET
                username=COALESCE(EXCLUDED.username,pinterest_boards.username),
                name=COALESCE(EXCLUDED.name,pinterest_boards.name),
                board_url=EXCLUDED.board_url,pin_count=COALESCE(EXCLUDED.pin_count,pinterest_boards.pin_count),
                collected_at=now(),metadata=pinterest_boards.metadata||EXCLUDED.metadata
        """,
        "story": """
            INSERT INTO snapchat_stories
                (story_id,username,title,story_url,media_url,thumbnail_url,posted_at,expires_at,collected_at,metadata)
            VALUES ($1,$2,$3,$4,$5,$6,$7::timestamptz,$8::timestamptz,now(),$9::jsonb)
            ON CONFLICT (story_id) DO UPDATE SET
                username=COALESCE(EXCLUDED.username,snapchat_stories.username),
                title=COALESCE(EXCLUDED.title,snapchat_stories.title),story_url=EXCLUDED.story_url,
                media_url=COALESCE(EXCLUDED.media_url,snapchat_stories.media_url),
                thumbnail_url=COALESCE(EXCLUDED.thumbnail_url,snapchat_stories.thumbnail_url),
                posted_at=COALESCE(EXCLUDED.posted_at,snapchat_stories.posted_at),
                expires_at=COALESCE(EXCLUDED.expires_at,snapchat_stories.expires_at),
                collected_at=now(),metadata=snapchat_stories.metadata||EXCLUDED.metadata
        """,
        "listing": """
            INSERT INTO airbnb_listings
                (listing_id,host_id,host_name,host_profile_url,title,listing_url,location,image_url,
                 rating,reviews_count,property_type,bedrooms,bathrooms,max_guests,collected_at,metadata)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,now(),$15::jsonb)
            ON CONFLICT (listing_id) DO UPDATE SET
                host_id=COALESCE(EXCLUDED.host_id,airbnb_listings.host_id),
                host_name=COALESCE(EXCLUDED.host_name,airbnb_listings.host_name),
                host_profile_url=COALESCE(EXCLUDED.host_profile_url,airbnb_listings.host_profile_url),
                title=COALESCE(EXCLUDED.title,airbnb_listings.title),listing_url=EXCLUDED.listing_url,
                location=COALESCE(EXCLUDED.location,airbnb_listings.location),
                image_url=COALESCE(EXCLUDED.image_url,airbnb_listings.image_url),
                rating=COALESCE(EXCLUDED.rating,airbnb_listings.rating),
                reviews_count=COALESCE(EXCLUDED.reviews_count,airbnb_listings.reviews_count),
                property_type=COALESCE(EXCLUDED.property_type,airbnb_listings.property_type),
                bedrooms=COALESCE(EXCLUDED.bedrooms,airbnb_listings.bedrooms),
                bathrooms=COALESCE(EXCLUDED.bathrooms,airbnb_listings.bathrooms),
                max_guests=COALESCE(EXCLUDED.max_guests,airbnb_listings.max_guests),
                collected_at=now(),metadata=airbnb_listings.metadata||EXCLUDED.metadata
        """,
        "host": """
            INSERT INTO airbnb_hosts
                (host_id,display_name,profile_url,avatar_url,rating,reviews_count,host_since,collected_at,metadata)
            VALUES ($1,$2,$3,$4,$5,$6,$7,now(),$8::jsonb)
            ON CONFLICT (host_id) DO UPDATE SET
                display_name=COALESCE(EXCLUDED.display_name,airbnb_hosts.display_name),
                profile_url=EXCLUDED.profile_url,avatar_url=COALESCE(EXCLUDED.avatar_url,airbnb_hosts.avatar_url),
                rating=COALESCE(EXCLUDED.rating,airbnb_hosts.rating),
                reviews_count=COALESCE(EXCLUDED.reviews_count,airbnb_hosts.reviews_count),
                host_since=COALESCE(EXCLUDED.host_since,airbnb_hosts.host_since),
                collected_at=now(),metadata=airbnb_hosts.metadata||EXCLUDED.metadata
        """,
    }

    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                if target_id is not None:
                    target = await conn.fetchrow(
                        "SELECT target_url,status FROM browser_native_scrape_queue WHERE id=$1 AND platform=$2 FOR UPDATE",
                        target_id,
                        platform,
                    )
                    if (target is None or target["status"] != "claimed"
                            or not _same_target(platform, str(body.get("page_url") or ""), target["target_url"])):
                        return _cors(web.json_response({"ok": False, "error": "stale_target"}, status=409))
                for kind, args in valid:
                    await conn.execute(statements[kind], *args)
                    accepted[kind] += 1
                if target_id is not None:
                    has_rows = sum(accepted.values()) > 0
                    if status == "success" and has_rows:
                        await conn.execute(
                            """UPDATE browser_native_scrape_queue SET status='completed',last_error=NULL,
                               claim_expires_at=NULL,last_result_at=now(),updated_at=now() WHERE id=$1 AND platform=$2""",
                            target_id,
                            platform,
                        )
                    else:
                        reason = "empty_public_page" if status == "success" else "page_unavailable"
                        await conn.execute(
                            """UPDATE browser_native_scrape_queue SET status='failed',last_error=$3,
                               next_visit_at=now()+CASE WHEN attempts < $4 THEN interval '6 hours' ELSE interval '1 day' END,
                               claim_expires_at=NULL,last_result_at=now(),updated_at=now()
                               WHERE id=$1 AND platform=$2""",
                            target_id,
                            platform,
                            reason,
                            _MAX_ATTEMPTS,
                        )
        return _cors(web.json_response({"ok": True, "accepted": accepted, "target_id": target_id}))
    except Exception as exc:
        logger.warning("native scrape write failed for %s: %s", platform, type(exc).__name__)
        return _cors(web.json_response({"ok": False, "error": "write_failed"}, status=503))
