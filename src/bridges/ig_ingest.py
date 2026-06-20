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


def _cors(resp: web.Response) -> web.Response:
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


async def handle_options(request):
    return _cors(web.Response(status=204))


async def get_targets(request):
    pool = request.app["pool"]
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT target_id FROM collection_targets WHERE source='instagram'"
            )
        targets = [r["target_id"] for r in rows if r["target_id"]]
    except Exception:
        logger.exception("get_targets failed")
        targets = []
    return _cors(web.json_response({"targets": targets}))


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
    logger.info("ingest %s: %d/%d saved", username, saved, len(items))
    return _cors(web.json_response({"saved": saved, "skipped": len(items) - saved}))


async def health(request):
    return _cors(web.json_response({"ok": True}))


async def _on_startup(app):
    app["pool"] = await get_pool()
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
    app.router.add_get("/health", health)
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    return app


if __name__ == "__main__":
    web.run_app(make_app(), host="0.0.0.0", port=PORT)
