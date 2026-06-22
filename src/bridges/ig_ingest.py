"""Social ingest bridge — receives media scraped by the UnifiedCollector Bridge
Chrome extension (which uses your logged-in browser session) and persists it like
any other collected media: downloads the file to the media drive and upserts a
media_items row. This sidesteps the GraphQL-400 / login-wall / signing problems
the headless collectors hit, because the extension scrapes as the real logged-in
user (same-origin fetches / reading the page's own embedded state).

Module is still named ig_ingest for compose compatibility
(`python -m src.bridges.ig_ingest`) but it is now MULTI-PLATFORM.

Run:  python -m src.bridges.ig_ingest   (listens on 0.0.0.0:8765)

Generic endpoints (CORS-open so the extension service worker can call them):
  GET  /social/targets?platform=<p>  -> {"targets":[{username,hop}], "usernames":[...], "max_hop"}
  POST /social/ingest   <- {"platform","username","items":[{content_id,content_type,url,entity_name}]}
                        -> queues downloads (returns instantly) {"accepted": N}
  POST /social/discover <- {"platform","source","hop","discovered":[{username,follower_count}]}
  GET  /health          -> {"ok": true}

Back-compat instagram aliases (the older extension builds call these):
  GET /ig/targets, POST /ig/ingest, POST /ig/discover  (== platform=instagram)

NON-BLOCKING INGEST: downloads run in a background asyncio task bounded by a
semaphore, so the POST returns immediately. This is important — the MV3 service
worker that calls us gets idle-killed if a request takes too long, which produced
the "message channel closed before a response was received" errors. We ack fast
and download out-of-band.
"""
import asyncio
import json
import logging
import os
import re
import hashlib
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
from aiohttp import web

from src.db.connection import get_pool, close_pool
from src.core.media_filter import inspect as inspect_media

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("social_ingest")

MEDIA_ROOT = os.getenv("COLLECTOR_DRIVE_PATH", "/media")
PORT = int(os.getenv("IG_INGEST_PORT", "8765"))
MIN_BYTES = int(os.getenv("IG_INGEST_MIN_BYTES", "1024"))
DL_CONCURRENCY = int(os.getenv("SOCIAL_INGEST_CONCURRENCY", "4"))
_SAFE = re.compile(r"[^A-Za-z0-9._-]")

# Platforms the bridge may push. Each may carry its own famous-cap / hop config.
# Only instagram currently spiders (followers/following graph); the others scrape
# whatever the open page exposes, so they have no spider table.
KNOWN_PLATFORMS = {"instagram", "tiktok", "lemon8", "x", "threads", "facebook"}

# 2-hop spider (instagram only): the extension scrapes a target's media AND, when
# the target's hop < MAX_HOP, crawls its followers/following and POSTs them to
# discover; we store them at hop+1 in instagram_spider_targets (a channel SEPARATE
# from collection_targets so the .targets file-sync never wipes them). Famous
# accounts (follower_count > cap) are dropped — we want your network, not celebs.
IG_SPIDER_MAX_HOP = int(os.getenv("INSTA_SPIDER_HOPS", "2"))
IG_SPIDER_FAMOUS_CAP = int(os.getenv("INSTA_SPIDER_FAMOUS_CAP", "100000"))
IG_SPIDER_TARGETS_LIMIT = int(os.getenv("IG_SPIDER_TARGETS_LIMIT", "250"))

_SPIDER_DDL = """
CREATE TABLE IF NOT EXISTS instagram_spider_targets (
  username        TEXT PRIMARY KEY,
  hop             INT  NOT NULL DEFAULT 1,
  discovered_from TEXT,
  follower_count  INT,
  status          TEXT NOT NULL DEFAULT 'active',
  discovered_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_scraped_at TIMESTAMPTZ
)
"""


def _date_prefix(item) -> str:
    """YYYYMMDD for the filename so files sort chronologically. Prefer the post's
    own taken_at (epoch, from item or its meta); fall back to today (collection)."""
    epoch = item.get("taken_at")
    if epoch is None:
        meta = item.get("meta") or {}
        epoch = meta.get("taken_at") if isinstance(meta, dict) else None
    try:
        if epoch:
            return datetime.fromtimestamp(float(epoch), tz=timezone.utc).strftime("%Y%m%d")
    except (TypeError, ValueError, OSError):
        pass
    return datetime.now(tz=timezone.utc).strftime("%Y%m%d")


def _norm_platform(p):
    p = (p or "instagram").strip().lower()
    if p == "twitter":
        p = "x"
    return p if p in KNOWN_PLATFORMS else "instagram"


def _cors(resp: web.Response) -> web.Response:
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


async def handle_options(request):
    return _cors(web.Response(status=204))


# ---------------------------------------------------------------------------
# targets
# ---------------------------------------------------------------------------
async def _targets_for(pool, platform):
    """seed targets (collection_targets) UNION instagram spider targets (IG only)."""
    seen = set()
    out = []
    try:
        async with pool.acquire() as conn:
            seeds = await conn.fetch(
                "SELECT target_id FROM collection_targets WHERE source=$1", platform
            )
            for r in seeds:
                u = r["target_id"]
                if u and u not in seen:
                    seen.add(u)
                    out.append({"username": u, "hop": 0})
            if platform == "instagram":
                spider = await conn.fetch(
                    """
                    SELECT username, hop FROM instagram_spider_targets
                    WHERE status='active' AND hop <= $1
                    ORDER BY hop ASC, last_scraped_at ASC NULLS FIRST, discovered_at ASC
                    LIMIT $2
                    """,
                    IG_SPIDER_MAX_HOP, IG_SPIDER_TARGETS_LIMIT,
                )
                for r in spider:
                    u = r["username"]
                    if u and u not in seen:
                        seen.add(u)
                        out.append({"username": u, "hop": int(r["hop"])})
    except Exception:
        logger.exception("targets query failed (%s)", platform)
    return out


async def get_targets(request):
    platform = _norm_platform(request.query.get("platform"))
    out = await _targets_for(request.app["pool"], platform)
    return _cors(web.json_response({
        "platform": platform,
        "targets": out,
        "usernames": [t["username"] for t in out],  # back-compat
        "max_hop": IG_SPIDER_MAX_HOP if platform == "instagram" else 0,
    }))


async def get_targets_ig(request):  # /ig/targets alias
    request.query  # noqa
    out = await _targets_for(request.app["pool"], "instagram")
    return _cors(web.json_response({
        "targets": out,
        "usernames": [t["username"] for t in out],
        "max_hop": IG_SPIDER_MAX_HOP,
    }))


# ---------------------------------------------------------------------------
# discover (instagram spider only)
# ---------------------------------------------------------------------------
async def _discover(pool, platform, body):
    if platform != "instagram":
        return {"added": 0, "reason": "no spider for " + platform}
    try:
        src_hop = int(body.get("hop", 0))
    except (TypeError, ValueError):
        src_hop = 0
    source = body.get("source")
    discovered = body.get("discovered") or []
    target_hop = src_hop + 1
    if target_hop > IG_SPIDER_MAX_HOP:
        return {"added": 0, "reason": "max_hop"}
    added = 0
    async with pool.acquire() as conn:
        for d in discovered:
            uname = (d.get("username") or "").strip().lstrip("@") if isinstance(d, dict) else str(d).strip()
            if not uname:
                continue
            fc = d.get("follower_count") if isinstance(d, dict) else None
            if isinstance(fc, int) and fc > IG_SPIDER_FAMOUS_CAP:
                continue
            res = await conn.execute(
                """
                INSERT INTO instagram_spider_targets (username, hop, discovered_from, follower_count)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (username) DO NOTHING
                """,
                uname, target_hop, source, fc if isinstance(fc, int) else None,
            )
            if res.endswith("1"):
                added += 1
    logger.info("discover[%s] from %s (hop %d): +%d new", platform, source, src_hop, added)
    return {"added": added}


async def discover(request):
    platform = _norm_platform((await _safe_json(request)).get("platform"))
    body = await _safe_json(request)
    try:
        return _cors(web.json_response(await _discover(request.app["pool"], platform, body)))
    except Exception:
        logger.exception("discover failed")
        return _cors(web.json_response({"added": 0, "error": "db"}, status=500))


async def discover_ig(request):  # /ig/discover alias
    body = await _safe_json(request)
    try:
        return _cors(web.json_response(await _discover(request.app["pool"], "instagram", body)))
    except Exception:
        logger.exception("discover failed")
        return _cors(web.json_response({"added": 0, "error": "db"}, status=500))


# ---------------------------------------------------------------------------
# download + persist (generic over platform)
# ---------------------------------------------------------------------------
async def _download_and_save(pool, session, platform, username, item) -> bool:
    url = item.get("url")
    cid = str(item.get("content_id") or "")
    if not url or not cid:
        return False
    # media kind: post (default) | story | highlight. Stories/highlights live in
    # their own subtree and get a namespaced content_id so they never collide with
    # a feed post that happens to share an id.
    media_kind = (item.get("kind") or "post").lower()
    if media_kind not in ("post", "story", "highlight"):
        media_kind = "post"
    # DB dedup id stays namespaced so a story/highlight can't collide with a post.
    store_cid = cid if media_kind == "post" else f"{media_kind}_{cid}"
    safe_user = _SAFE.sub("_", username)[:80] or "unknown"
    raw_cid = _SAFE.sub("_", cid)[:100]
    # filename kind label (no subfolders anymore — kind is encoded in the name)
    kindtag = {"story": "story_", "highlight": "hl_"}.get(media_kind, "")
    datestr = _date_prefix(item)
    try:
        # dedup authority is media_items (source, content_id)
        async with pool.acquire() as conn:
            seen = await conn.fetchval(
                "SELECT 1 FROM media_items WHERE source=$1 AND content_id=$2", platform, store_cid
            )
        if seen:
            return False

        async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as r:
            if r.status != 200:
                return False
            ct_header = r.headers.get("content-type")
            data = await r.read()

        # GATE: keep only real PDF/image/video/audio above min size — drop
        # favicons, thumbnails, tracking pixels, sprite sheets, HTML error pages.
        ok, ext, mtype, reason = inspect_media(data, ct_header)
        if not ok:
            logger.debug("reject %s %s: %s", platform, store_cid, reason)
            return False
        ctype = "video" if mtype == "video" else ("pdf" if mtype == "pdf" else "photo")
        # Flat layout: /<platform>/account_<user>/<ctype>/  — kind + date live in the
        # filename: <YYYYMMDD>_<platform>_<user>_<kindtag><cid>.<ext> (sortable by date).
        dest_dir = Path(MEDIA_ROOT) / platform / f"account_{safe_user}" / ctype
        dest = dest_dir / f"{datestr}_{platform}_{safe_user}_{kindtag}{raw_cid}.{ext}"
        if dest.exists():
            return False

        dest_dir.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, dest)

        sha = hashlib.sha256(data).hexdigest()
        # caption + likes/comments/views/location come along free from the scrape
        meta = item.get("meta") or {}
        meta_json = json.dumps(meta) if isinstance(meta, dict) else "{}"
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO media_items
                  (source, entity_id, entity_name, content_type, content_id,
                   filename, file_path, file_size, sha256, source_url, metadata, kind)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb,$12)
                ON CONFLICT (source, content_id) DO UPDATE SET
                   file_path = EXCLUDED.file_path,
                   file_size = EXCLUDED.file_size,
                   sha256 = EXCLUDED.sha256,
                   source_url = EXCLUDED.source_url,
                   metadata = EXCLUDED.metadata,
                   kind = EXCLUDED.kind
                """,
                platform, safe_user, item.get("entity_name") or username, ctype, store_cid,
                dest.name, str(dest), len(data), sha, url, meta_json, media_kind,
            )
        return True
    except Exception:
        logger.debug("save failed platform=%s cid=%s", platform, cid, exc_info=True)
        return False


async def _drain(app, platform, username, items):
    """Background download worker — bounded concurrency, never blocks the POST."""
    pool, session, sem = app["pool"], app["session"], app["sem"]
    saved = 0

    async def one(it):
        nonlocal saved
        async with sem:
            if await _download_and_save(pool, session, platform, username, it):
                saved += 1

    await asyncio.gather(*(one(it) for it in items), return_exceptions=True)
    if platform == "instagram":
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE instagram_spider_targets SET last_scraped_at=now() WHERE username=$1",
                    username,
                )
        except Exception:
            pass
    logger.info("ingest[%s] %s: %d/%d saved", platform, username, saved, len(items))


async def _ingest(app, platform, body):
    username = body.get("username") or "unknown"
    items = body.get("items") or []
    if items:
        task = asyncio.create_task(_drain(app, platform, username, items))
        app["tasks"].add(task)
        task.add_done_callback(app["tasks"].discard)
    return {"accepted": len(items), "platform": platform}


def _int(v):
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# structured post + comment metadata (captions/likes/comments threads)
# ---------------------------------------------------------------------------
async def _save_posts(pool, platform, posts) -> int:
    """Upsert post metadata (caption, engagement, hashtags, mentions) into the
    platform's posts table. instagram_posts has the richest schema; threads_posts
    and facebook_posts mirror the essentials. tiktok/lemon8 posts are owned by
    their headless collectors, so we don't double-write them here."""
    if platform not in ("instagram", "threads", "facebook"):
        return 0
    n = 0
    async with pool.acquire() as conn:
        for p in posts:
            ppid = str(p.get("platform_post_id") or "")
            if not ppid:
                continue
            try:
                if platform == "instagram":
                    await conn.execute(
                        """
                        INSERT INTO instagram_posts
                          (id, platform_post_id, media_type, caption, hashtags, mentions,
                           location_name, likes_count, comments_count, video_duration,
                           platform_created_at, collected_at, metadata)
                        VALUES (gen_random_uuid(),$1,$2,$3,$4,$5,$6,$7,$8,$9,
                                to_timestamp($10), now(), $11::jsonb)
                        ON CONFLICT (platform_post_id) DO UPDATE SET
                           caption=EXCLUDED.caption, likes_count=EXCLUDED.likes_count,
                           comments_count=EXCLUDED.comments_count, hashtags=EXCLUDED.hashtags,
                           mentions=EXCLUDED.mentions, location_name=EXCLUDED.location_name,
                           collected_at=now(), metadata=EXCLUDED.metadata
                        """,
                        ppid, p.get("media_type"), p.get("caption"),
                        p.get("hashtags") or [], p.get("mentions") or [],
                        p.get("location"), _int(p.get("likes_count")), _int(p.get("comments_count")),
                        _int(p.get("video_duration")), _num(p.get("taken_at")),
                        json.dumps(p.get("metadata") or {}),
                    )
                else:  # threads / facebook
                    table = "threads_posts" if platform == "threads" else "facebook_posts"
                    extra_col = "reposts_count" if platform == "threads" else "shares_count"
                    await conn.execute(
                        f"""
                        INSERT INTO {table}
                          (platform_post_id, author_username, caption, hashtags, mentions,
                           likes_count, comments_count, {extra_col}, media_type,
                           platform_created_at, collected_at, metadata)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,to_timestamp($10),now(),$11::jsonb)
                        ON CONFLICT (platform_post_id) DO UPDATE SET
                           caption=EXCLUDED.caption, likes_count=EXCLUDED.likes_count,
                           comments_count=EXCLUDED.comments_count,
                           {extra_col}=EXCLUDED.{extra_col}, collected_at=now(),
                           metadata=EXCLUDED.metadata
                        """,
                        ppid, p.get("author_username"), p.get("caption"),
                        p.get("hashtags") or [], p.get("mentions") or [],
                        _int(p.get("likes_count")), _int(p.get("comments_count")),
                        _int(p.get("reposts_count") if platform == "threads" else p.get("shares_count")),
                        p.get("media_type"), _num(p.get("taken_at")),
                        json.dumps(p.get("metadata") or {}),
                    )
                n += 1
            except Exception:
                logger.debug("save post failed %s/%s", platform, ppid, exc_info=True)
    return n


async def _save_comments(pool, platform, post_pid, comments) -> int:
    """Upsert comment threads into instagram_comments, linked to their post."""
    if platform != "instagram":
        return 0
    n = 0
    async with pool.acquire() as conn:
        for c in comments:
            cpid = str(c.get("platform_comment_id") or "")
            if not cpid:
                continue
            try:
                await conn.execute(
                    """
                    INSERT INTO instagram_comments
                      (id, platform_comment_id, post_id, author_username, author_platform_id,
                       text, like_count, parent_comment_id, is_reply, platform_created_at, collected_at)
                    VALUES (gen_random_uuid(),$1,
                       (SELECT id FROM instagram_posts WHERE platform_post_id=$2),
                       $3,$4,$5,$6,$7,$8,to_timestamp($9),now())
                    ON CONFLICT (platform_comment_id) DO UPDATE SET
                       text = EXCLUDED.text, like_count = EXCLUDED.like_count, collected_at = now()
                    """,
                    cpid, str(post_pid or ""), c.get("author_username"), c.get("author_platform_id"),
                    c.get("text"), _int(c.get("like_count")), c.get("parent_comment_id"),
                    bool(c.get("is_reply")), _num(c.get("created_at")),
                )
                n += 1
            except Exception:
                logger.debug("save comment failed %s", cpid, exc_info=True)
    return n


async def posts_handler(request):
    body = await _safe_json(request)
    platform = _norm_platform(body.get("platform"))
    n = await _save_posts(request.app["pool"], platform, body.get("posts") or [])
    return _cors(web.json_response({"saved": n}))


async def comments_handler(request):
    body = await _safe_json(request)
    platform = _norm_platform(body.get("platform"))
    n = await _save_comments(request.app["pool"], platform, body.get("post_id"), body.get("comments") or [])
    return _cors(web.json_response({"saved": n}))


async def ingest(request):
    body = await _safe_json(request)
    platform = _norm_platform(body.get("platform"))
    return _cors(web.json_response(await _ingest(request.app, platform, body)))


async def ingest_ig(request):  # /ig/ingest alias
    body = await _safe_json(request)
    return _cors(web.json_response(await _ingest(request.app, "instagram", body)))


# ---------------------------------------------------------------------------
async def _safe_json(request):
    if request.get("_json_cache") is not None:
        return request["_json_cache"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    request["_json_cache"] = body
    return body


async def health(request):
    return _cors(web.json_response({"ok": True}))


async def _on_startup(app):
    app["pool"] = await get_pool()
    app["tasks"] = set()
    app["sem"] = asyncio.Semaphore(DL_CONCURRENCY)
    try:
        async with app["pool"].acquire() as conn:
            await conn.execute(_SPIDER_DDL)
    except Exception:
        logger.exception("spider table DDL failed")
    app["session"] = aiohttp.ClientSession(
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
    )


async def _on_cleanup(app):
    for t in list(app.get("tasks", [])):
        t.cancel()
    await app["session"].close()
    await close_pool()


def make_app():
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.router.add_route("OPTIONS", "/{tail:.*}", handle_options)
    # generic multi-platform
    app.router.add_get("/social/targets", get_targets)
    app.router.add_post("/social/ingest", ingest)
    app.router.add_post("/social/discover", discover)
    app.router.add_post("/social/posts", posts_handler)
    app.router.add_post("/social/comments", comments_handler)
    # instagram back-compat aliases
    app.router.add_get("/ig/targets", get_targets_ig)
    app.router.add_post("/ig/ingest", ingest_ig)
    app.router.add_post("/ig/discover", discover_ig)
    app.router.add_get("/health", health)
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    return app


if __name__ == "__main__":
    web.run_app(make_app(), host="0.0.0.0", port=PORT)
