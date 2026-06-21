"""Instagram ingest bridge — receives media scraped by the IG Bridge Chrome
extension (which uses your logged-in browser session) and persists it like any
other collected media: downloads the file to the media drive and upserts a
media_items row. This sidesteps the GraphQL-400 / login-wall problem the
headless instagram collector hits, because the extension scrapes as the real
logged-in user.

Run:  python -m src.bridges.ig_ingest   (listens on 0.0.0.0:8765)

Endpoints (CORS-open so the extension service worker can call them):
  GET  /ig/targets  -> {"targets": ["user1", ...]}  (instagram collection_targets)
  POST /ig/ingest   <- {"username": "...", "items": [{content_id, content_type, url, entity_name}]}
                    -> downloads each + upserts media_items; {"saved": N, "skipped": M}
  GET  /health      -> {"ok": true}
"""
import asyncio
import logging
import os
import re
import hashlib
from pathlib import Path

import aiohttp
from aiohttp import web

from src.db.connection import get_pool, close_pool

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("ig_ingest")

MEDIA_ROOT = os.getenv("COLLECTOR_DRIVE_PATH", "/media")
PORT = int(os.getenv("IG_INGEST_PORT", "8765"))
MIN_BYTES = int(os.getenv("IG_INGEST_MIN_BYTES", "1024"))
_SAFE = re.compile(r"[^A-Za-z0-9._-]")

# 2-hop spider (friends-of-friends). The extension scrapes a target's media AND,
# when the target's hop < MAX_HOP, crawls its followers/following and POSTs them
# to /ig/discover; we store them at hop+1 in instagram_spider_targets (a channel
# SEPARATE from collection_targets, so the .targets file-sync never wipes them).
# Famous accounts (follower_count > cap) are dropped — we want your network, not
# celebrities. Defaults match the tiktok/lemon8 famous cap.
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


def _cors(resp: web.Response) -> web.Response:
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


async def handle_options(request):
    return _cors(web.Response(status=204))


async def get_targets(request):
    """Return seed targets (collection_targets, hop 0) UNION spider-discovered
    targets (instagram_spider_targets, hop 1..MAX_HOP) as [{username, hop}].
    The extension scrapes each, and for hop < MAX_HOP also crawls its graph.
    Backward-compatible: also emits a flat `usernames` list."""
    pool = request.app["pool"]
    seen = set()
    out = []
    try:
        async with pool.acquire() as conn:
            seeds = await conn.fetch(
                "SELECT target_id FROM collection_targets WHERE source='instagram'"
            )
            for r in seeds:
                u = r["target_id"]
                if u and u not in seen:
                    seen.add(u)
                    out.append({"username": u, "hop": 0})
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
        logger.exception("get_targets failed")
    return _cors(web.json_response({
        "targets": out,
        "usernames": [t["username"] for t in out],  # back-compat
        "max_hop": IG_SPIDER_MAX_HOP,
    }))


async def discover(request):
    """Receive spider-discovered usernames from the extension and enqueue them at
    hop+1 (skipping famous accounts and anything past MAX_HOP)."""
    pool = request.app["pool"]
    try:
        body = await request.json()
    except Exception:
        return _cors(web.json_response({"error": "bad json"}, status=400))
    try:
        src_hop = int(body.get("hop", 0))
    except (TypeError, ValueError):
        src_hop = 0
    source = body.get("source")
    discovered = body.get("discovered") or []
    target_hop = src_hop + 1
    if target_hop > IG_SPIDER_MAX_HOP:
        return _cors(web.json_response({"added": 0, "reason": "max_hop"}))
    added = 0
    try:
        async with pool.acquire() as conn:
            for d in discovered:
                uname = (d.get("username") or "").strip().lstrip("@") if isinstance(d, dict) else str(d).strip()
                if not uname:
                    continue
                fc = d.get("follower_count") if isinstance(d, dict) else None
                if isinstance(fc, int) and fc > IG_SPIDER_FAMOUS_CAP:
                    continue  # skip celebrities — we want your network
                res = await conn.execute(
                    """
                    INSERT INTO instagram_spider_targets
                        (username, hop, discovered_from, follower_count)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (username) DO NOTHING
                    """,
                    uname, target_hop, source, fc if isinstance(fc, int) else None,
                )
                if res.endswith("1"):
                    added += 1
    except Exception:
        logger.exception("discover failed")
        return _cors(web.json_response({"added": added, "error": "db"}, status=500))
    logger.info("discover from %s (hop %d): +%d new target(s)", source, src_hop, added)
    return _cors(web.json_response({"added": added}))


async def _download_and_save(pool, session, username, item) -> bool:
    url = item.get("url")
    cid = str(item.get("content_id") or "")
    ctype = item.get("content_type") or "photo"
    if not url or not cid:
        return False
    ext = "mp4" if ctype == "video" else "jpg"
    safe_user = _SAFE.sub("_", username)[:80]
    safe_cid = _SAFE.sub("_", cid)[:120]
    dest_dir = Path(MEDIA_ROOT) / "instagram" / f"account_{safe_user}" / ctype
    dest = dest_dir / f"instagram_{safe_user}_{safe_cid}.{ext}"
    try:
        # Skip if we already have this content_id (dedup authority is media_items).
        async with pool.acquire() as conn:
            seen = await conn.fetchval(
                "SELECT 1 FROM media_items WHERE source='instagram' AND content_id=$1", cid
            )
        if seen and dest.exists():
            return False

        async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as r:
            if r.status != 200:
                return False
            data = await r.read()
        if len(data) < MIN_BYTES:
            return False

        dest_dir.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, dest)

        sha = hashlib.sha256(data).hexdigest()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO media_items
                  (source, entity_id, entity_name, content_type, content_id,
                   filename, file_path, file_size, sha256, source_url, metadata)
                VALUES ('instagram',$1,$2,$3,$4,$5,$6,$7,$8,$9,'{}'::jsonb)
                ON CONFLICT (source, content_id) DO UPDATE SET
                   file_path = EXCLUDED.file_path,
                   file_size = EXCLUDED.file_size,
                   sha256 = EXCLUDED.sha256,
                   source_url = EXCLUDED.source_url
                """,
                safe_user, item.get("entity_name") or username, ctype, cid,
                dest.name, str(dest), len(data), sha, url,
            )
        return True
    except Exception:
        logger.debug("save failed cid=%s", cid, exc_info=True)
        return False


async def ingest(request):
    pool = request.app["pool"]
    session = request.app["session"]
    try:
        body = await request.json()
    except Exception:
        return _cors(web.json_response({"error": "bad json"}, status=400))
    username = body.get("username") or "unknown"
    items = body.get("items") or []
    saved = 0
    for it in items:
        if await _download_and_save(pool, session, username, it):
            saved += 1
    # Mark a spider target as scraped so the round-robin (ORDER BY last_scraped_at
    # NULLS FIRST) moves on to others next cycle. No-op for seed accounts.
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE instagram_spider_targets SET last_scraped_at=now() WHERE username=$1",
                username,
            )
    except Exception:
        pass
    logger.info("ingest %s: %d/%d saved", username, saved, len(items))
    return _cors(web.json_response({"saved": saved, "skipped": len(items) - saved}))


async def health(request):
    return _cors(web.json_response({"ok": True}))


async def _on_startup(app):
    app["pool"] = await get_pool()
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
    await app["session"].close()
    await close_pool()


def make_app():
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.router.add_route("OPTIONS", "/{tail:.*}", handle_options)
    app.router.add_get("/ig/targets", get_targets)
    app.router.add_post("/ig/ingest", ingest)
    app.router.add_post("/ig/discover", discover)
    app.router.add_get("/health", health)
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    return app


if __name__ == "__main__":
    web.run_app(make_app(), host="0.0.0.0", port=PORT)
