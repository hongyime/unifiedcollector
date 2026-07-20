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
import base64
import os
import re
import time
import hashlib
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
from aiohttp import web

from src.db.connection import get_pool, close_pool
from src.core.media_filter import inspect as inspect_media
from src.core.proximity import refresh_account_proximity_cache
from src.core.vault import assert_media_write_allowed, write_media_sidecar

# Follow-aware access recording (Phase 0). The extension IS the live IG path, so
# recording access outcomes here populates profile_access_{summary,attempts} far
# faster than the 429'd headless collector. Defensive import — ingest still works
# without it.
try:
    from src.core.profile_access import ProfileAccessRepository
except Exception:  # pragma: no cover
    ProfileAccessRepository = None  # type: ignore[assignment]

# Profile change-history (Tier 4). The change tracker was only wired into the
# 429'd headless collector, so the LIVE extension path never recorded bio/
# username/follower changes (instagram_user_changes = 0). Wire it here too.
try:
    from src.core.user_change_tracker import UserChangeTracker, INSTAGRAM_TRACKED_FIELDS
except Exception:  # pragma: no cover
    UserChangeTracker = None  # type: ignore[assignment]
    INSTAGRAM_TRACKED_FIELDS = None  # type: ignore[assignment]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("social_ingest")

MEDIA_ROOT = os.getenv("COLLECTOR_DRIVE_PATH", "/media")
PORT = int(os.getenv("IG_INGEST_PORT", "8765"))

# DM raw-sample capture (#35). Files land in DM_SAMPLE_DIR as
# <platform>_<n>.bin. n is a monotonically increasing index derived from the
# max existing index (NOT a count), so pruning can't cause an old-index reuse
# that would overwrite a not-yet-pruned file. Rotation keeps only the newest
# DM_SAMPLE_CAP_PER_PLATFORM files per platform by mtime, so the directory
# can't grow unbounded on active sockets (P1.1). Cap overridable via env.
DM_SAMPLE_DIR = "/tmp/dm_samples"
DM_SAMPLE_CAP_PER_PLATFORM = int(os.getenv("DM_SAMPLE_CAP", "200"))
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


_IG_EPOCH = 1314220021721  # Instagram media-id custom epoch (ms)
_IG_LONG = re.compile(r"\d{8,}")


def _ig_date_from_id(content_id: str):
    """Instagram media ids encode creation time: (id>>23)+epoch. Return YYYYMMDD."""
    m = _IG_LONG.search(content_id or "")
    if not m:
        return None
    try:
        d = datetime.fromtimestamp(((int(m.group(0)) >> 23) + _IG_EPOCH) / 1000, tz=timezone.utc)
        if 2010 <= d.year <= datetime.now(tz=timezone.utc).year + 1:
            return d.strftime("%Y%m%d")
    except Exception:
        return None
    return None


# Instagram/Threads shortcode alphabet — numeric media pk → the ~11-char code in
# the public URL (instagram.com/p/<code>, threads.com/post/<code>). The extension
# sometimes stores a wrong/long "code"; we derive the canonical one from the pk so
# verification URLs always open.
_SC_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def _shortcode_from_id(media_id: str):
    """Reverse Instagram's base64 media-id encoding into the public shortcode."""
    m = _IG_LONG.search(str(media_id or ""))
    if not m:
        return None
    try:
        n = int(m.group(0))
    except Exception:
        return None
    if n <= 0:
        return None
    out = []
    while n > 0:
        out.append(_SC_ALPHABET[n & 63])
        n >>= 6
    return "".join(reversed(out))


def _verify_url(platform, media_id):
    """Canonical openable URL for a post, for manual spot-checking."""
    sc = _shortcode_from_id(media_id)
    if not sc:
        return None
    if platform == "instagram":
        return f"https://instagram.com/p/{sc}/"
    if platform == "threads":
        return f"https://threads.com/post/{sc}/"
    return None


def _date_prefix(item, platform="") -> str:
    """YYYYMMDD for the filename so files sort chronologically. Prefer the post's
    own taken_at; for instagram fall back to the date encoded in the media id;
    finally fall back to today (collection)."""
    epoch = item.get("taken_at")
    if epoch is None:
        meta = item.get("meta") or {}
        epoch = meta.get("taken_at") if isinstance(meta, dict) else None
    try:
        if epoch:
            return datetime.fromtimestamp(float(epoch), tz=timezone.utc).strftime("%Y%m%d")
    except (TypeError, ValueError, OSError):
        pass
    if platform == "instagram":
        d = _ig_date_from_id(str(item.get("content_id") or ""))
        if d:
            return d
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
        await refresh_account_proximity_cache(pool)
        async with pool.acquire() as conn:
            seeds = await conn.fetch(
                """
                SELECT ct.target_id, MIN(ap.tier) AS proximity_tier, ct.priority
                FROM collection_targets ct
                LEFT JOIN account_proximity_cache ap
                  ON ap.platform = ct.source
                 AND ap.account_id = lower(ct.target_id)
                WHERE ct.source = $1
                GROUP BY ct.target_id, ct.priority, ct.created_at
                ORDER BY
                    CASE
                        WHEN MIN(ap.tier) IN (1, 2) THEN 2
                        WHEN MIN(ap.tier) = 3 THEN 1
                        ELSE 0
                    END DESC,
                    ct.priority DESC,
                    ct.created_at ASC
                """,
                platform,
            )
            for r in seeds:
                u = r["target_id"]
                if u and u not in seen:
                    seen.add(u)
                    out.append({"username": u, "hop": 0})
            if platform == "instagram":
                spider = await conn.fetch(
                    """
                    SELECT s.username, s.hop, prox.proximity_tier
                    FROM instagram_spider_targets s
                    LEFT JOIN LATERAL (
                        SELECT MIN(ap.tier) AS proximity_tier
                        FROM account_proximity_cache ap
                        WHERE ap.platform = 'instagram'
                          AND ap.account_id = lower(s.username)
                    ) prox ON TRUE
                    WHERE s.status='active' AND s.hop <= $1
                    ORDER BY
                        CASE
                            WHEN prox.proximity_tier IN (1, 2) THEN 2
                            WHEN prox.proximity_tier = 3 THEN 1
                            ELSE 0
                        END DESC,
                        s.hop ASC,
                        s.last_scraped_at ASC NULLS FIRST,
                        s.discovered_at ASC
                    LIMIT $2
                    """,
                    IG_SPIDER_MAX_HOP, IG_SPIDER_TARGETS_LIMIT,
                )
                for r in spider:
                    u = r["username"]
                    if u and u not in seen:
                        seen.add(u)
                        out.append({"username": u, "hop": int(r["hop"])})
            elif platform == "threads":
                # REVERSE cross-pollination: a Threads handle IS an Instagram handle,
                # so the real people we know on Instagram (your follow graph + spider)
                # are scrapeable Threads profiles. Hand them to the Threads tab to visit.
                ig = await conn.fetch(
                    """
                    SELECT s.username, prox.proximity_tier
                    FROM instagram_spider_targets s
                    LEFT JOIN LATERAL (
                        SELECT MIN(ap.tier) AS proximity_tier
                        FROM account_proximity_cache ap
                        WHERE ap.platform = 'instagram'
                          AND ap.account_id = lower(s.username)
                    ) prox ON TRUE
                    WHERE s.status='active'
                    ORDER BY
                        CASE
                            WHEN prox.proximity_tier IN (1, 2) THEN 2
                            WHEN prox.proximity_tier = 3 THEN 1
                            ELSE 0
                        END DESC,
                        s.last_scraped_at ASC NULLS FIRST,
                        s.discovered_at ASC
                    LIMIT $1
                    """,
                    IG_SPIDER_TARGETS_LIMIT,
                )
                ig2 = await conn.fetch(
                    """
                    SELECT ct.target_id AS username, MIN(ap.tier) AS proximity_tier
                    FROM collection_targets ct
                    LEFT JOIN account_proximity_cache ap
                      ON ap.platform = 'instagram'
                     AND ap.account_id = lower(ct.target_id)
                    WHERE ct.source='instagram'
                    GROUP BY ct.target_id, ct.priority, ct.created_at
                    ORDER BY
                        CASE
                            WHEN MIN(ap.tier) IN (1, 2) THEN 2
                            WHEN MIN(ap.tier) = 3 THEN 1
                            ELSE 0
                        END DESC,
                        ct.priority DESC,
                        ct.created_at ASC
                    """
                )
                for r in list(ig) + list(ig2):
                    u = (r["username"] or "").strip().lstrip("@")
                    if u and u not in seen:
                        seen.add(u)
                        out.append({"username": u, "hop": 1})
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
# IG throttle coordination — the headless collector persists its 429 cooldown
# (exponential, up to 4h) to service_cursors as "<expiry_epoch>:<streak>". Expose
# it so the EXTENSION can rest in sync: both paths share the same IG account, so if
# headless got rate-limited the extension must back off too (anti-ban). Cooperative
# throttling beats two independent clients each probing a flagged account.
#
# RESTART-SAFE (P2 review §4, verified 2026-07-03): this cooldown wall lives in the
# DB (service_cursors), NOT in process memory, so neither an ig_ingest restart nor a
# collector restart clears an active cooldown — the surviving row is re-read on the
# next request/cycle. The only in-memory state here is the download-concurrency
# Semaphore, which is a steady-state cap, not a backoff timer. No change needed.
# ---------------------------------------------------------------------------
async def ig_cooldown(request):
    pool = request.app["pool"]
    cooling, secs_left, streak = False, 0, 0
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchval(
                "SELECT last_processed_id FROM service_cursors WHERE service='instagram_rate_limit'"
            )
        if row and ":" in str(row):
            exp_s, streak_s = str(row).split(":", 1)
            expiry = float(exp_s)
            streak = int(float(streak_s))
            left = expiry - time.time()
            if left > 0:
                cooling, secs_left = True, int(left)
    except Exception:
        logger.debug("ig_cooldown read failed", exc_info=True)
    return _cors(web.json_response({"cooling": cooling, "secs_left": secs_left, "streak": streak}))


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
    # every discovered follower/following is a user we've now seen
    await _record_users(pool, platform, discovered, "follow")
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


async def target_status_handler(request):
    """Let the extension retire bad spider targets.

    Instagram returns HTTP 400/404 for some unavailable/private/deleted profile
    lookups. Those are not throttles and should not keep rotating through the
    active spider queue forever.
    """
    body = await _safe_json(request)
    platform = _norm_platform(body.get("platform"))
    username = (body.get("username") or "").strip().lstrip("@")
    status = (body.get("status") or "").strip().lower()
    reason = str(body.get("reason") or "")[:120]
    if platform != "instagram" or not username:
        return _cors(web.json_response({"updated": 0}))
    if status not in {"unavailable", "skipped"}:
        status = "unavailable"
    updated = 0
    async with request.app["pool"].acquire() as conn:
        result = await conn.execute(
            """
            UPDATE instagram_spider_targets
            SET status = $2,
                last_scraped_at = now()
            WHERE username = $1
              AND status = 'active'
            """,
            username, status,
        )
        if result.endswith("1"):
            updated = 1
    if updated:
        logger.info("target-status[%s] %s -> %s (%s)", platform, username, status, reason)
    return _cors(web.json_response({"updated": updated, "status": status}))


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
    if media_kind not in ("post", "story", "highlight", "tagged", "profile"):
        media_kind = "post"
    # DB dedup id stays namespaced so a story/highlight/tagged/profile can't collide.
    store_cid = cid if media_kind == "post" else f"{media_kind}_{cid}"
    safe_user = _SAFE.sub("_", username)[:80] or "unknown"
    raw_cid = _SAFE.sub("_", cid)[:100]
    # filename kind label (no subfolders anymore — kind is encoded in the name)
    kindtag = {"story": "story_", "highlight": "hl_", "tagged": "tagged_", "profile": "profile_"}.get(media_kind, "")
    datestr = _date_prefix(item, platform)

    async def _record_vault_pause(reason: str) -> None:
        logger.warning(
            "vault/media unavailable before extension media write platform=%s cid=%s: %s",
            platform,
            store_cid,
            reason,
        )
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO dead_letter_queue (source, entity_id, content_id, error_message)
                    VALUES ($1, $2, $3, $4)
                    """,
                    platform,
                    safe_user,
                    store_cid,
                    f"vault/media unavailable before extension media write: {reason}"[:500],
                )
        except Exception:
            logger.debug(
                "vault/media unavailable DLQ insert failed for %s/%s",
                platform,
                store_cid,
                exc_info=True,
            )

    try:
        # dedup authority is media_items (source, content_id)
        async with pool.acquire() as conn:
            seen = await conn.fetchval(
                "SELECT 1 FROM media_items WHERE source=$1 AND content_id=$2", platform, store_cid
            )
        if seen:
            return False

        try:
            assert_media_write_allowed(
                Path(MEDIA_ROOT) / platform / f"account_{safe_user}" / ".extension_write_check",
                media_root=MEDIA_ROOT,
            )
        except RuntimeError as exc:
            await _record_vault_pause(str(exc))
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
        sha = hashlib.sha256(data).hexdigest()
        # CONTENT DEDUP: if these exact bytes are already stored for this source
        # (e.g. the same For-You image re-scraped under a different DOM content_id),
        # skip — no duplicate file or row. This is what kills the lemon8/tiktok
        # re-scrape duplication. Needs the (source, sha256) index for speed.
        async with pool.acquire() as conn:
            if await conn.fetchval("SELECT 1 FROM media_items WHERE source=$1 AND sha256=$2 LIMIT 1", platform, sha):
                return False
        ctype = "video" if mtype == "video" else ("pdf" if mtype == "pdf" else "photo")
        # Flat layout: /<platform>/account_<user>/<ctype>/  — kind + date live in the
        # filename: <YYYYMMDD>_<platform>_<user>_<kindtag><cid>.<ext> (sortable by date).
        dest_dir = Path(MEDIA_ROOT) / platform / f"account_{safe_user}" / ctype
        dest = dest_dir / f"{datestr}_{platform}_{safe_user}_{kindtag}{raw_cid}.{ext}"
        try:
            assert_media_write_allowed(dest, media_root=MEDIA_ROOT)
        except RuntimeError as exc:
            await _record_vault_pause(str(exc))
            return False
        if dest.exists():
            return False

        dest_dir.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, dest)

        # caption + likes/comments/views/location come along free from the scrape
        meta = item.get("meta") or {}
        meta_obj = meta if isinstance(meta, dict) else {}
        meta_json = json.dumps(meta_obj)
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO media_items
                  (source, entity_id, entity_name, content_type, content_id,
                   filename, file_path, file_size, sha256, source_url, metadata, kind,
                   ingest_path)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb,$12,'extension')
                ON CONFLICT (source, content_id) DO UPDATE SET
                   file_path = EXCLUDED.file_path,
                   file_size = EXCLUDED.file_size,
                   sha256 = EXCLUDED.sha256,
                   source_url = EXCLUDED.source_url,
                   metadata = EXCLUDED.metadata,
                   kind = EXCLUDED.kind,
                   ingest_path = 'extension'
                """,
                platform, safe_user, item.get("entity_name") or username, ctype, store_cid,
                dest.name, str(dest), len(data), sha, url, meta_json, media_kind,
            )
        sidecar = write_media_sidecar(
            source=platform,
            entity_id=safe_user,
            entity_name=item.get("entity_name") or username,
            content_type=ctype,
            content_id=store_cid,
            filename=dest.name,
            file_path=str(dest),
            file_size=len(data),
            width=None,
            height=None,
            sha256=sha,
            source_url=url,
            metadata=meta_obj,
            ingest_path="extension",
            kind=media_kind,
        )
        sidecar_meta = {
            "vault_sidecar": {
                "enabled": sidecar.enabled,
                "ok": sidecar.ok,
                "path": sidecar.relative_path,
                "error": sidecar.error,
            }
        }
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE media_items
                    SET metadata = COALESCE(metadata, '{}'::jsonb) || $3::jsonb
                    WHERE source = $1 AND content_id = $2
                    """,
                    platform,
                    store_cid,
                    json.dumps(sidecar_meta, default=str),
                )
                if sidecar.enabled and not sidecar.ok:
                    await conn.execute(
                        """
                        INSERT INTO dead_letter_queue (source, entity_id, content_id, error_message)
                        VALUES ($1, $2, $3, $4)
                        """,
                        platform,
                        safe_user,
                        store_cid,
                        f"vault sidecar write failed: {sidecar.error}",
                    )
        except Exception:
            logger.warning(
                "vault sidecar status update failed for %s/%s",
                platform,
                store_cid,
                exc_info=True,
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


_HANDLE_RE = re.compile(r"^[A-Za-z0-9._]{2,30}$")


def _derive_handle_from_caption(caption):
    """Recover a threads/facebook author handle when the extension left
    author_username empty but dumped the whole post block into the caption
    ("<handle>\\n<relative time>\\n<caption>"). Returns the first line only when
    it's a clean handle (no spaces) — this rejects repost headers like
    "x reposted 1h ago". Mirrors tmp/backfill_threads_author.py so new posts
    self-heal instead of landing with NULL author. Returns None if unsure."""
    if not caption:
        return None
    first = caption.split("\n", 1)[0].strip()
    return first if _HANDLE_RE.match(first) else None


def _ig_author_uid(ppid: str) -> str | None:
    """Recover the Instagram author's numeric user id from a platform_post_id of
    the shape `<media_id>_<author_uid>` (IDENTITY_KEYS.md). Returns None when the
    id has no embedded uid (a bare media id / shortcode). This trailing id equals
    instagram_profiles.platform_user_id."""
    if not ppid:
        return None
    uid = ppid.rsplit("_", 1)[-1] if "_" in ppid else ""
    return uid if uid.isdigit() else None


async def _ensure_ig_profile(conn, ppid: str, p: dict) -> str | None:
    """Resolve (or create a minimal stub for) the instagram_profiles row for a
    post's author, keyed on the numeric uid embedded in platform_post_id, and
    return its id (uuid). This is the root-cause fix for the recurring NULL-FK
    bug: this ingest path historically OMITTED profile_id, so ~19k posts landed
    with NULL profile_id and were invisible to every consumer that joins
    instagram_posts.profile_id -> instagram_profiles.id (analyzer timelines,
    /geo). We now attach the FK at insert time.

    A stub is created (ON CONFLICT DO NOTHING) when the author profile hasn't
    been collected yet, so spidered-from-post authors still get a stable FK; the
    real profile collector later enriches the same row (unique platform_user_id).
    Returns None only when the uid can't be recovered (bare media id) — those
    posts remain genuinely unattributable, same as before."""
    uid = _ig_author_uid(ppid)
    if not uid:
        return None
    row = await conn.fetchrow(
        "SELECT id FROM instagram_profiles WHERE platform_user_id = $1", uid
    )
    if row:
        return str(row["id"])
    # No profile row yet — create a minimal stub keyed on the uid. Use any author
    # username the extension happened to send; the profile collector fills the
    # rest later (matched on the unique platform_user_id).
    username = (p.get("author_username") or p.get("username") or None)
    new_id = await conn.fetchval(
        """
        INSERT INTO instagram_profiles (platform_user_id, username)
        VALUES ($1, $2)
        ON CONFLICT (platform_user_id) DO UPDATE SET
            username = COALESCE(instagram_profiles.username, EXCLUDED.username)
        RETURNING id
        """,
        uid, username,
    )
    return str(new_id) if new_id else None


# ---------------------------------------------------------------------------
# structured post + comment metadata (captions/likes/comments threads)
# ---------------------------------------------------------------------------
async def _save_posts(pool, platform, posts) -> int:
    """Upsert post metadata (caption, engagement, hashtags, mentions) into the
    platform's posts table. instagram_posts has the richest schema; threads_posts
    and facebook_posts mirror the essentials. tiktok/lemon8 posts are owned by
    their headless collectors, so we don't double-write them here."""
    if platform not in ("instagram", "threads", "facebook", "x"):
        return 0
    n = 0
    async with pool.acquire() as conn:
        for p in posts:
            ppid = str(p.get("platform_post_id") or "")
            if not ppid:
                continue
            # stamp a canonical, openable verification URL + correct shortcode into
            # metadata (overrides any wrong/long "code" the extension may have sent).
            vurl = _verify_url(platform, ppid)
            if vurl:
                _md = p.get("metadata")
                if not isinstance(_md, dict):
                    _md = {}
                _md["verify_url"] = vurl
                _md["shortcode"] = vurl.rstrip("/").rsplit("/", 1)[-1]
                p["metadata"] = _md
            try:
                if platform == "instagram":
                    # Root-cause fix (NULL-FK bug): resolve/create the author
                    # profile and attach profile_id at insert time so the post is
                    # never orphaned. COALESCE on UPDATE so a later collection that
                    # already set profile_id is never clobbered back to NULL.
                    profile_id = await _ensure_ig_profile(conn, ppid, p)
                    await conn.execute(
                        """
                        INSERT INTO instagram_posts
                          (id, platform_post_id, profile_id, media_type, caption, hashtags, mentions,
                           location_name, location_lat, location_lng, music_title, music_author,
                           likes_count, comments_count, video_duration,
                           platform_created_at, collected_at, metadata)
                        VALUES (gen_random_uuid(),$1,$2::uuid,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,
                                to_timestamp($15), now(), $16::jsonb)
                        ON CONFLICT (platform_post_id) DO UPDATE SET
                           profile_id=COALESCE(instagram_posts.profile_id, EXCLUDED.profile_id),
                           caption=EXCLUDED.caption, likes_count=EXCLUDED.likes_count,
                           comments_count=EXCLUDED.comments_count, hashtags=EXCLUDED.hashtags,
                           mentions=EXCLUDED.mentions, location_name=EXCLUDED.location_name,
                           location_lat=COALESCE(EXCLUDED.location_lat, instagram_posts.location_lat),
                           location_lng=COALESCE(EXCLUDED.location_lng, instagram_posts.location_lng),
                           music_title=COALESCE(EXCLUDED.music_title, instagram_posts.music_title),
                           music_author=COALESCE(EXCLUDED.music_author, instagram_posts.music_author),
                           collected_at=now(), metadata=EXCLUDED.metadata
                        """,
                        ppid, profile_id, p.get("media_type"), p.get("caption"),
                        p.get("hashtags") or [], p.get("mentions") or [],
                        p.get("location"), _num(p.get("location_lat")), _num(p.get("location_lng")),
                        p.get("music_title"), p.get("music_author"),
                        _int(p.get("likes_count")), _int(p.get("comments_count")),
                        _int(p.get("video_duration")), _num(p.get("taken_at")),
                        json.dumps(p.get("metadata") or {}),
                    )
                elif platform == "x":
                    await conn.execute(
                        """
                        INSERT INTO x_posts
                          (platform_post_id, author_username, caption, hashtags, mentions,
                           likes_count, comments_count, reposts_count, quote_count, views_count,
                           media_type, platform_created_at, collected_at, metadata)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,to_timestamp($12),now(),$13::jsonb)
                        ON CONFLICT (platform_post_id) DO UPDATE SET
                           caption=EXCLUDED.caption, likes_count=EXCLUDED.likes_count,
                           comments_count=EXCLUDED.comments_count, reposts_count=EXCLUDED.reposts_count,
                           quote_count=EXCLUDED.quote_count, views_count=EXCLUDED.views_count,
                           collected_at=now(), metadata=EXCLUDED.metadata
                        """,
                        ppid, p.get("author_username"), p.get("caption"),
                        p.get("hashtags") or [], p.get("mentions") or [],
                        _int(p.get("likes_count")), _int(p.get("comments_count")), _int(p.get("reposts_count")),
                        _int(p.get("quote_count")), _int(p.get("views_count")), p.get("media_type"),
                        _num(p.get("taken_at")), json.dumps(p.get("metadata") or {}),
                    )
                else:  # threads / facebook
                    table = "threads_posts" if platform == "threads" else "facebook_posts"
                    extra_col = "reposts_count" if platform == "threads" else "shares_count"
                    # Self-heal: the threads/facebook extension scraper sometimes
                    # leaves author_username empty and puts "<handle>\n<time>\n..."
                    # in the caption. Recover the handle so the row isn't orphaned
                    # (was ~56% of threads posts). See _derive_handle_from_caption.
                    if not p.get("author_username"):
                        _h = _derive_handle_from_caption(p.get("caption"))
                        if _h:
                            p["author_username"] = _h
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


# ---------------------------------------------------------------------------
# universal user registry — every user/id we encounter anywhere (follows graph,
# comment authors, tagged users, post authors, reactors) lands in social_users.
# ---------------------------------------------------------------------------
# A Threads handle IS the same Meta account's Instagram handle. So real people we
# see on Threads (your Following feed, comment authors, etc.) are scrapeable on
# Instagram — which IS target/profile-driven. Cross-seed them into the IG spider
# queue. Gated to non-"foryou" contexts so we don't import algorithmic brand spam.
async def _cross_seed_instagram(conn, usernames, source) -> int:
    added = 0
    for uname in usernames:
        uname = (uname or "").strip().lstrip("@")
        if not uname or len(uname) > 30:
            continue
        try:
            res = await conn.execute(
                """
                INSERT INTO instagram_spider_targets (username, hop, discovered_from)
                VALUES ($1, 1, $2)
                ON CONFLICT (username) DO NOTHING
                """,
                uname, source,
            )
            if res.endswith("1"):
                added += 1
        except Exception:
            logger.debug("cross-seed ig target failed %s", uname, exc_info=True)
    if added:
        logger.info("cross-seed instagram <- %s: +%d real handles", source, added)
    return added


async def _record_users(pool, platform, users, context, owner=None) -> int:
    if not users:
        return 0
    n = 0
    _cross = []  # threads handles to push into the IG spider queue
    # PER-ACCOUNT follow graph: when the extension sends an owner (the logged-in
    # account) with a follow/follower context, also record a directional edge in
    # follow_edges so each of your accounts' graphs is distinct (multi-account).
    owner_account = None
    direction = None
    if isinstance(owner, dict) and (context or "").lower() in ("follow", "follower"):
        owner_account = (owner.get("username") or owner.get("id") or "") or None
        direction = "follower" if context.lower() == "follower" else "following"
    async with pool.acquire() as conn:
        for u in users:
            if isinstance(u, str):
                u = {"username": u}
            if not isinstance(u, dict):
                continue
            user_id = u.get("user_id") or u.get("pk") or u.get("id")
            username = (u.get("username") or "").strip().lstrip("@") or None
            uid = str(user_id) if user_id else username
            if not uid:
                continue
            try:
                await conn.execute(
                    """
                    INSERT INTO social_users (platform, uid, platform_user_id, username, display_name, profile_photo_url, contexts)
                    VALUES ($1,$2,$3,$4,$5,$6,$7)
                    ON CONFLICT (platform, uid) DO UPDATE SET
                       last_seen = now(),
                       times_seen = social_users.times_seen + 1,
                       username = COALESCE(EXCLUDED.username, social_users.username),
                       platform_user_id = COALESCE(EXCLUDED.platform_user_id, social_users.platform_user_id),
                       display_name = COALESCE(EXCLUDED.display_name, social_users.display_name),
                       profile_photo_url = COALESCE(EXCLUDED.profile_photo_url, social_users.profile_photo_url),
                       contexts = (SELECT array_agg(DISTINCT c) FROM unnest(social_users.contexts || EXCLUDED.contexts) AS c)
                    """,
                    platform, uid, str(user_id) if user_id else None, username,
                    u.get("display_name") or u.get("full_name") or None,
                    u.get("profile_pic_url") or u.get("profile_photo_url") or u.get("avatar_url") or None, [context],
                )
                n += 1
                if owner_account and direction:
                    await conn.execute(
                        """
                        INSERT INTO follow_edges
                            (platform, owner_account, target_uid, direction, target_username, first_seen, last_seen)
                        VALUES ($1, $2, $3, $4, $5, now(), now())
                        ON CONFLICT (platform, owner_account, target_uid, direction) DO UPDATE SET
                            last_seen = now(),
                            target_username = COALESCE(EXCLUDED.target_username, follow_edges.target_username)
                        """,
                        platform, str(owner_account), uid, direction, username,
                    )
                if platform == "threads" and username and (context or "").lower() != "foryou":
                    _cross.append(username)
            except Exception:
                logger.debug("record user failed %s", uid, exc_info=True)
        if _cross:
            await _cross_seed_instagram(conn, _cross, "threads:" + (context or "feed"))
    return n


async def users_handler(request):
    body = await _safe_json(request)
    platform = _norm_platform(body.get("platform"))
    n = await _record_users(request.app["pool"], platform, body.get("users") or [],
                            body.get("context") or "seen", owner=body.get("owner"))
    return _cors(web.json_response({"recorded": n}))


async def _record_dms(pool, threads, owner) -> int:
    """Persist Instagram DM threads + messages observed by the extension."""
    if not threads:
        return 0
    owner_id = str((owner or {}).get("id") or "")
    owner_acct = (owner or {}).get("username") or owner_id or None
    n = 0
    async with pool.acquire() as conn:
        for th in threads:
            if not isinstance(th, dict):
                continue
            tid = str(th.get("thread_id") or th.get("thread_v2_id") or "")
            if not tid:
                continue
            users = th.get("users") or []
            umap = {str(u.get("pk") or u.get("id") or ""): (u.get("username") or None) for u in users if isinstance(u, dict)}
            parts = [v or k for k, v in umap.items()]
            try:
                await conn.execute(
                    "INSERT INTO instagram_dm_thread (thread_id, title, participants, owner_account, last_activity, updated_at) "
                    "VALUES ($1,$2,$3,$4, now(), now()) "
                    "ON CONFLICT (thread_id) DO UPDATE SET title=COALESCE(EXCLUDED.title, instagram_dm_thread.title), "
                    "participants=EXCLUDED.participants, owner_account=COALESCE(EXCLUDED.owner_account, instagram_dm_thread.owner_account), updated_at=now()",
                    tid, th.get("thread_title") or th.get("title"), parts, owner_acct)
            except Exception:
                logger.debug("dm thread upsert failed %s", tid, exc_info=True)
            for it in (th.get("items") or []):
                if not isinstance(it, dict):
                    continue
                mid = str(it.get("item_id") or it.get("id") or "")
                if not mid:
                    continue
                sender = str(it.get("user_id") or "")
                # text lives in .text (text items) or nested link/clip/etc — best-effort.
                txt = it.get("text")
                if not txt and isinstance(it.get("link"), dict):
                    txt = it["link"].get("text")
                ts = it.get("timestamp")
                try:
                    ts_s = float(ts) / 1_000_000 if ts and float(ts) > 1e14 else (float(ts) if ts else None)
                except Exception:
                    ts_s = None
                try:
                    await conn.execute(
                        "INSERT INTO instagram_dm (message_id, thread_id, sender_id, sender_username, text, item_type, \"timestamp\", is_from_me, owner_account, collected_at) "
                        "VALUES ($1,$2,$3,$4,$5,$6, CASE WHEN $7::float8 IS NULL THEN NULL ELSE to_timestamp($7) END, $8, $9, now()) "
                        "ON CONFLICT (message_id) DO NOTHING",
                        mid, tid, sender, umap.get(sender), txt, it.get("item_type"),
                        ts_s, bool(owner_id and sender == owner_id), owner_acct)
                    n += 1
                except Exception:
                    logger.debug("dm item upsert failed %s", mid, exc_info=True)
    return n


async def dms_handler(request):
    body = await _safe_json(request)
    try:
        n = await _record_dms(request.app["pool"], body.get("threads") or [], body.get("owner"))
        return _cors(web.json_response({"recorded": n}))
    except Exception:
        logger.exception("dms handler failed")
        return _cors(web.json_response({"recorded": 0, "error": "db"}, status=500))


async def _dm_probe_log_write(pool, platform, event_type, body, frame_size=None):
    """Best-effort write to dm_probe_log for the dashboard telemetry panel
    (P1.2). Swallows every error — telemetry must never break capture.
    frame_size is a separate arg because the sample handler already knows the
    decoded length, while probes pull it from body.get('frame_size').
    """
    if not pool:
        return
    try:
        size = frame_size
        if size is None:
            raw = body.get("frame_size")
            try:
                size = int(raw) if raw is not None else None
            except (TypeError, ValueError):
                size = None
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO dm_probe_log
                    (platform, event_type, url, transport, frame_kind,
                     frame_size, owner_account)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                str(platform)[:64] if platform else "unknown",
                event_type,
                (body.get("url") or None),
                (body.get("transport") or None),
                (body.get("frame_kind") or None),
                size,
                (body.get("owner") or body.get("owner_account") or None),
            )
    except Exception:
        logger.debug("dm_probe_log write failed", exc_info=True)


async def dm_hook_heartbeat_handler(request):
    """Heartbeat from the browser extension's DM WebSocket hook (P1.3).

    The hook lives inside inject.js and is passive/send-nothing (see #35/#38).
    If Instagram or TikTok update their bundle and break the wrapper we have
    no server-side signal today — samples just stop arriving and it's
    indistinguishable from "user isn't DMing". This endpoint records a beat
    per (platform, owner) so:
      (a) src/watchdog/freshness.py can alert on Telegram if the newest
          heartbeat per platform goes stale (no container restart possible —
          the hook lives in the browser), and
      (b) the dashboard telemetry panel can show extension_version and
          time-since-last-beat.
    Best-effort — failures are logged at DEBUG and don't affect return.
    """
    body = await _safe_json(request)
    platform = (body.get("platform") or "").strip()
    if not platform:
        return _cors(web.json_response({"ok": False, "error": "no_platform"}, status=400))
    owner = (body.get("owner") or body.get("owner_account") or "").strip()
    try:
        probes = int(body.get("probes_sent") or 0)
    except (TypeError, ValueError):
        probes = 0
    try:
        samples = int(body.get("samples_shipped") or 0)
    except (TypeError, ValueError):
        samples = 0
    ext_version = (body.get("extension_version") or None)
    ua = request.headers.get("User-Agent") or None

    pool = request.app.get("pool")
    if not pool:
        return _cors(web.json_response({"ok": True, "recorded": False}))
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO dm_hook_heartbeat
                    (platform, owner_account, last_seen, probes_sent,
                     samples_shipped, extension_version, user_agent)
                VALUES ($1, $2, now(), $3, $4, $5, $6)
                ON CONFLICT (platform, owner_account) DO UPDATE SET
                    last_seen         = now(),
                    probes_sent       = EXCLUDED.probes_sent,
                    samples_shipped   = EXCLUDED.samples_shipped,
                    extension_version = COALESCE(EXCLUDED.extension_version,
                                                 dm_hook_heartbeat.extension_version),
                    user_agent        = COALESCE(EXCLUDED.user_agent,
                                                 dm_hook_heartbeat.user_agent)
                """,
                platform[:64], owner[:128], probes, samples, ext_version, ua,
            )
    except Exception:
        logger.debug("dm_hook_heartbeat upsert failed", exc_info=True)
        return _cors(web.json_response({"ok": False, "error": "db"}, status=500))
    return _cors(web.json_response({"ok": True}))


async def dm_probe_handler(request):
    """One-time investigation probe (#38): the extension's observe-only hooks
    report the transport + format of each platform's DM channel so we can confirm
    the wire format before committing to a decoder/schema. Logged to stderr and
    (P1.2) to dm_probe_log for the dashboard telemetry panel.
    Confirmed so far: TikTok = binary protobuf over wss://im-ws-…/ws/v2; IG is
    binary MQTT over wss://edge-chat.instagram.com/chat — both reasons the
    fetch/XHR JSON observation path can't capture them.
    """
    body = await _safe_json(request)
    logger.info(
        "DM probe: platform=%s transport=%s kind=%s size=%s url=%s",
        body.get("platform"), body.get("transport"), body.get("frame_kind"),
        body.get("frame_size"), body.get("url"),
    )
    await _dm_probe_log_write(
        request.app.get("pool"), body.get("platform") or "unknown", "probe", body,
    )
    return _cors(web.json_response({"ok": True}))


async def dm_sample_handler(request):
    """Save a raw DM-socket frame sample (base64) for decoder development (#35).
    Observe-only: these are bytes the page already received; we send nothing to
    the platform. Written to /tmp/dm_samples/<platform>_<n>.bin so the exact
    MQTT (IG) / protobuf (TikTok) payloads can be inspected offline.

    Rotation (P1.1): after each successful write we prune to the newest
    DM_SAMPLE_CAP_PER_PLATFORM files per platform (by mtime), so this dir
    can't grow unbounded on high-traffic sockets (TikTok emitted +6/hour in
    passive tests). The filename index is derived from `max existing index +
    1`, NOT the file count, so pruning never causes a fresh write to reuse
    an index that still exists on disk. Concurrent writes race-safe via
    O_EXCL retry loop.
    """
    body = await _safe_json(request)
    platform = (body.get("platform") or "unknown").replace("/", "_")[:20]
    b64 = body.get("b64") or ""
    try:
        raw = base64.b64decode(b64)
    except Exception:
        return _cors(web.json_response({"ok": False, "error": "bad_b64"}, status=400))
    d = DM_SAMPLE_DIR
    os.makedirs(d, exist_ok=True)
    import glob as _glob
    existing = _glob.glob(f"{d}/{platform}_*.bin")
    # Derive next index from the max existing index across BOTH old 3-digit
    # (`_NNN.bin`) and new 6-digit (`_NNNNNN.bin`) naming — the regex matches
    # any run of digits before `.bin`, so we don't lose track when the format
    # widens.
    max_idx = -1
    _idx_re = re.compile(rf"{re.escape(platform)}_(\d+)\.bin$")
    for p in existing:
        m = _idx_re.search(p)
        if m:
            try:
                max_idx = max(max_idx, int(m.group(1)))
            except ValueError:
                pass
    n = max_idx + 1
    path = None
    for _ in range(5):
        candidate = f"{d}/{platform}_{n:06d}.bin"
        try:
            fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            n += 1
            continue
        except Exception:
            logger.exception("dm sample open failed")
            break
        try:
            os.write(fd, raw)
            path = candidate
        except Exception:
            logger.exception("dm sample write failed")
        finally:
            os.close(fd)
        break
    if path is None:
        return _cors(web.json_response({"ok": False, "error": "write_failed"}, status=500))
    logger.info("DM sample saved: %s (%d bytes) url=%s", path, len(raw), body.get("url"))
    # Telemetry (P1.2): record the sample event so the dashboard panel can
    # count IG-vs-TikTok samples per 24h and surface last-seen timestamps.
    await _dm_probe_log_write(
        request.app.get("pool"), platform, "sample", body, frame_size=len(raw),
    )
    # Rotate: keep newest DM_SAMPLE_CAP_PER_PLATFORM files per platform by
    # mtime. Uses a fresh glob so we count the file we just wrote.
    try:
        files = sorted(
            _glob.glob(f"{d}/{platform}_*.bin"),
            key=lambda p: os.path.getmtime(p),
        )
        excess = len(files) - DM_SAMPLE_CAP_PER_PLATFORM
        if excess > 0:
            pruned = 0
            for old in files[:excess]:
                try:
                    os.unlink(old)
                    pruned += 1
                except OSError:
                    pass
            if pruned:
                logger.info(
                    "DM sample rotated: pruned %d old %s samples (cap=%d)",
                    pruned, platform, DM_SAMPLE_CAP_PER_PLATFORM,
                )
    except Exception:
        logger.exception("dm sample rotation failed")
    return _cors(web.json_response({"ok": True, "bytes": len(raw)}))


async def dm_frame_handler(request):
    """Capture a DM JSON frame if one is ever observed over a WS (#35). These
    channels almost always send protobuf/MQTT, so this is a best-effort path:
    log the raw frame so it can be inspected. No dedicated table yet — a schema
    is added once the frame shape is confirmed via the probe above.
    """
    body = await _safe_json(request)
    try:
        logger.info("DM JSON frame (%s): %s", body.get("platform"), json.dumps(body.get("frame"))[:1000])
    except Exception:
        logger.info("DM frame observed (unserializable)")
    return _cors(web.json_response({"ok": True}))


def _parse_ts_ms(v):
    """Convert an int/str ms-epoch or None to a timezone-aware datetime.
    Falls back to None on garbage so a bad timestamp never fails the upsert."""
    if v is None:
        return None
    try:
        ms = int(v)
        if ms <= 0:
            return None
        # Sanity: TikTok values are 13-digit ms since epoch. Reject > 4102444800000
        # (year ~2100) so a us/ns-scaled leak from the decoder can't corrupt rows.
        if ms > 4102444800000:
            return None
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def _conv_participants(conversation_id):
    """`0:1:UID_A:UID_B` -> ['UID_A','UID_B']. None on anything unparseable."""
    if not conversation_id:
        return None
    parts = str(conversation_id).split(":")
    if len(parts) >= 4:
        out = [p for p in parts[2:] if p]
        return out or None
    return None


def _int_or_none(v):
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _bool_or_none(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("true", "1", "yes", "on"):
        return True
    if s in ("false", "0", "no", "off"):
        return False
    return None


async def dm_decoded_handler(request):
    """Client-decoded DM payload from the extension (Option B of #39).

    The extension's WS hook uses a minimal in-tab protobuf parser to walk
    TikTok frontier frames on wss://im-ws-sg.tiktok.com/ws/v2 (see
    extension/inject.js `_ttDecode`). When it identifies a real message
    frame (method=5, inner has field 500 → field 5 with content JSON), it
    extracts the structured payload and POSTs here. Raw sample capture
    continues in parallel via /social/dm-sample as a schema-drift canary.

    Body shape:
      {
        "platform": "tiktok",
        "owner":    "<owner_uid_or_empty>",
        "threads":  [ {conversation_id, conversation_type, participants,
                       last_activity_ms} ],
        "messages": [ {message_id, conversation_id, sender_uid, sender_secuid,
                       text, aweType, message_type, create_time_ms,
                       client_message_id, is_stranger, raw_content} ]
      }

    Message upsert keyed on message_id (idempotent — the TikTok frontier
    pushes each message ~6× across topic subscriptions).
    """
    body = await _safe_json(request)
    platform = (body.get("platform") or "").strip().lower()
    if platform not in ("tiktok", "instagram"):
        return _cors(web.json_response(
            {"ok": False, "error": "unsupported_platform"}, status=400,
        ))
    owner = (body.get("owner") or "").strip()
    threads = body.get("threads") or []
    messages = body.get("messages") or []
    if not isinstance(threads, list) or not isinstance(messages, list):
        return _cors(web.json_response(
            {"ok": False, "error": "bad_shape"}, status=400,
        ))

    pool = request.app.get("pool")
    if not pool:
        return _cors(web.json_response({"ok": True, "recorded": 0}))
    if platform == "tiktok":
        thread_n, msg_n = await _upsert_tt_decoded(pool, owner, threads, messages)
    else:
        thread_n, msg_n = await _upsert_ig_decoded(pool, owner, threads, messages)
    if thread_n or msg_n:
        logger.info("DM decoded[%s]: %d threads, %d messages", platform, thread_n, msg_n)
    return _cors(web.json_response({"ok": True, "threads": thread_n, "messages": msg_n}))


async def _upsert_tt_decoded(pool, owner, threads, messages):
    thread_n = 0
    msg_n = 0
    try:
        async with pool.acquire() as conn:
            for t in threads:
                if not isinstance(t, dict):
                    continue
                cid = (t.get("conversation_id") or "").strip()
                if not cid:
                    continue
                await conn.execute(
                    """
                    INSERT INTO tiktok_dm_thread
                        (conversation_id, conversation_type, participants,
                         owner_account, last_activity)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (conversation_id) DO UPDATE SET
                        conversation_type = COALESCE(EXCLUDED.conversation_type,
                                                     tiktok_dm_thread.conversation_type),
                        participants      = COALESCE(EXCLUDED.participants,
                                                     tiktok_dm_thread.participants),
                        owner_account     = COALESCE(EXCLUDED.owner_account,
                                                     tiktok_dm_thread.owner_account),
                        last_activity     = GREATEST(EXCLUDED.last_activity,
                                                     tiktok_dm_thread.last_activity),
                        updated_at        = now()
                    """,
                    cid,
                    _int_or_none(t.get("conversation_type")),
                    (t.get("participants") or _conv_participants(cid) or None),
                    (t.get("owner_account") or owner or None),
                    _parse_ts_ms(t.get("last_activity_ms")),
                )
                thread_n += 1
            for m in messages:
                if not isinstance(m, dict):
                    continue
                mid = str(m.get("message_id") or "").strip()
                cid = (m.get("conversation_id") or "").strip()
                if not mid or not cid:
                    continue
                sender_uid = m.get("sender_uid")
                sender_uid_s = str(sender_uid) if sender_uid is not None else None
                is_from_me = bool(owner) and sender_uid_s == owner
                raw = m.get("raw_content")
                if raw is not None and not isinstance(raw, (dict, list)):
                    raw = {"raw": raw}
                await conn.execute(
                    """
                    INSERT INTO tiktok_dm
                        (message_id, conversation_id, sender_uid, sender_secuid,
                         text, awe_type, message_type, "timestamp", is_from_me,
                         owner_account, client_message_id, is_stranger, media_url,
                         raw_content)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
                    ON CONFLICT (message_id) DO UPDATE SET
                        text         = COALESCE(EXCLUDED.text, tiktok_dm.text),
                        awe_type     = COALESCE(EXCLUDED.awe_type, tiktok_dm.awe_type),
                        message_type = COALESCE(EXCLUDED.message_type, tiktok_dm.message_type),
                        "timestamp"  = COALESCE(EXCLUDED."timestamp", tiktok_dm."timestamp"),
                        media_url    = COALESCE(EXCLUDED.media_url, tiktok_dm.media_url),
                        raw_content  = COALESCE(EXCLUDED.raw_content, tiktok_dm.raw_content)
                    """,
                    mid, cid, sender_uid_s, m.get("sender_secuid"),
                    m.get("text"),
                    _int_or_none(m.get("aweType")),
                    _int_or_none(m.get("message_type")),
                    _parse_ts_ms(m.get("create_time_ms")),
                    is_from_me,
                    owner or None,
                    m.get("client_message_id"),
                    _bool_or_none(m.get("is_stranger")),
                    (m.get("media_url") or None),
                    json.dumps(raw) if raw is not None else None,
                )
                msg_n += 1
    except Exception:
        logger.exception("tt dm_decoded upsert failed")
        raise
    return thread_n, msg_n


async def _upsert_ig_decoded(pool, owner, threads, messages):
    """Instagram DM upsert into instagram_dm{,_thread}. The wire path is
    intentionally different from TikTok: IG uses MQTT-over-WSS + Thrift on
    edge-chat.instagram.com/chat, AND a GraphQL/direct_v2 HTTP path the
    extension already observes. Both paths funnel through here.

    Scaffolding today — no real MQTT/Thrift decoder in inject.js, so most IG
    decoded payloads will come from the GraphQL harvester (see extension/
    inject.js:harvestIGGraphQL) which extracts inbox/thread structures from
    /api/graphql/ and /graphql/query/ responses.
    """
    thread_n = 0
    msg_n = 0
    try:
        async with pool.acquire() as conn:
            for t in threads:
                if not isinstance(t, dict):
                    continue
                tid = (t.get("thread_id") or t.get("conversation_id") or "").strip()
                if not tid:
                    continue
                await conn.execute(
                    """
                    INSERT INTO instagram_dm_thread
                        (thread_id, title, participants, owner_account, last_activity)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (thread_id) DO UPDATE SET
                        title         = COALESCE(EXCLUDED.title,
                                                 instagram_dm_thread.title),
                        participants  = COALESCE(EXCLUDED.participants,
                                                 instagram_dm_thread.participants),
                        owner_account = COALESCE(EXCLUDED.owner_account,
                                                 instagram_dm_thread.owner_account),
                        last_activity = GREATEST(EXCLUDED.last_activity,
                                                 instagram_dm_thread.last_activity),
                        updated_at    = now()
                    """,
                    tid,
                    (t.get("title") or None),
                    (t.get("participants") or None),
                    (t.get("owner_account") or owner or None),
                    _parse_ts_ms(t.get("last_activity_ms")),
                )
                thread_n += 1
            for m in messages:
                if not isinstance(m, dict):
                    continue
                mid = str(m.get("message_id") or "").strip()
                tid = (m.get("thread_id") or m.get("conversation_id") or "").strip()
                if not mid or not tid:
                    continue
                sender_id = m.get("sender_id")
                sender_id_s = str(sender_id) if sender_id is not None else None
                # is_from_me: prefer client hint (extension had cookie-derived
                # ds_user_id), fall back to sender_id vs owner match.
                if isinstance(m.get("is_from_me"), bool):
                    is_from_me = m["is_from_me"]
                else:
                    is_from_me = bool(owner) and sender_id_s == owner
                await conn.execute(
                    """
                    INSERT INTO instagram_dm
                        (message_id, thread_id, sender_id, sender_username,
                         text, item_type, "timestamp", is_from_me, owner_account)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                    ON CONFLICT (message_id) DO UPDATE SET
                        text            = COALESCE(EXCLUDED.text, instagram_dm.text),
                        item_type       = COALESCE(EXCLUDED.item_type, instagram_dm.item_type),
                        "timestamp"     = COALESCE(EXCLUDED."timestamp", instagram_dm."timestamp"),
                        sender_username = COALESCE(EXCLUDED.sender_username, instagram_dm.sender_username)
                    """,
                    mid, tid, sender_id_s,
                    m.get("sender_username"),
                    m.get("text"),
                    m.get("item_type"),
                    _parse_ts_ms(m.get("timestamp_ms") or m.get("create_time_ms")),
                    is_from_me,
                    owner or None,
                )
                msg_n += 1
    except Exception:
        logger.exception("ig dm_decoded upsert failed")
        raise
    return thread_n, msg_n


async def _save_profile(pool, platform, p) -> bool:
    """Upsert a full profile. Instagram is API-rich; X/Facebook are browser DOM
    best-effort profiles keyed on the visible handle."""
    if platform not in ("instagram", "x", "facebook"):
        return False
    uname = (p.get("username") or "").strip().lstrip("@")
    if not uname:
        return False
    pic = p.get("profile_pic_url")
    if platform == "x":
        async with pool.acquire() as conn:
            # Preserve the exact handle casing already present in x_posts when
            # possible; entity_platform_links for X were historically keyed on
            # x_posts.author_username.
            canonical = await conn.fetchval(
                """
                SELECT author_username
                FROM x_posts
                WHERE lower(author_username) = lower($1)
                ORDER BY collected_at DESC NULLS LAST
                LIMIT 1
                """,
                uname,
            )
            pid = str(p.get("platform_user_id") or p.get("user_id") or canonical or uname).strip().lstrip("@")
            username = str(canonical or uname).strip().lstrip("@")
            await conn.execute(
                """
                INSERT INTO x_profiles
                  (id, platform_user_id, username, display_name, bio, followers_count,
                   following_count, posts_count, is_verified, is_private, profile_pic_url,
                   external_url, location, joined_text, collected_at, updated_at, metadata)
                VALUES (gen_random_uuid(),$1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,now(),now(),$14::jsonb)
                ON CONFLICT (platform_user_id) DO UPDATE SET
                   username=COALESCE(NULLIF(EXCLUDED.username, ''), x_profiles.username),
                   display_name=COALESCE(EXCLUDED.display_name, x_profiles.display_name),
                   bio=COALESCE(EXCLUDED.bio, x_profiles.bio),
                   followers_count=COALESCE(EXCLUDED.followers_count, x_profiles.followers_count),
                   following_count=COALESCE(EXCLUDED.following_count, x_profiles.following_count),
                   posts_count=COALESCE(EXCLUDED.posts_count, x_profiles.posts_count),
                   is_verified=COALESCE(EXCLUDED.is_verified, x_profiles.is_verified),
                   is_private=COALESCE(EXCLUDED.is_private, x_profiles.is_private),
                   profile_pic_url=COALESCE(EXCLUDED.profile_pic_url, x_profiles.profile_pic_url),
                   external_url=COALESCE(EXCLUDED.external_url, x_profiles.external_url),
                   location=COALESCE(EXCLUDED.location, x_profiles.location),
                   joined_text=COALESCE(EXCLUDED.joined_text, x_profiles.joined_text),
                   metadata=x_profiles.metadata || EXCLUDED.metadata,
                   updated_at=now()
                """,
                pid, username, p.get("display_name") or p.get("full_name"), p.get("bio"),
                _int(p.get("followers_count")), _int(p.get("following_count")), _int(p.get("posts_count")),
                bool(p.get("is_verified")) if p.get("is_verified") is not None else None,
                bool(p.get("is_private")) if p.get("is_private") is not None else None,
                pic, p.get("external_url"), p.get("location"), p.get("joined_text"),
                json.dumps(p.get("metadata") or {}),
            )
        await _record_users(pool, platform, [{"user_id": pid, "username": username,
                                              "display_name": p.get("display_name") or p.get("full_name"),
                                              "profile_pic_url": pic}], "profile")
        return True
    if platform == "facebook":
        async with pool.acquire() as conn:
            pid = str(p.get("platform_user_id") or p.get("user_id") or uname).strip().lstrip("@")
            await conn.execute(
                """
                INSERT INTO facebook_profiles
                  (id, platform_user_id, username, display_name, bio, followers_count,
                   following_count, friends_count, is_person, profile_pic_url, external_url,
                   collected_at, updated_at, metadata)
                VALUES (gen_random_uuid(),$1,$2,$3,$4,$5,$6,$7,$8,$9,$10,now(),now(),$11::jsonb)
                ON CONFLICT (platform_user_id) DO UPDATE SET
                   username=COALESCE(NULLIF(EXCLUDED.username, ''), facebook_profiles.username),
                   display_name=COALESCE(EXCLUDED.display_name, facebook_profiles.display_name),
                   bio=COALESCE(EXCLUDED.bio, facebook_profiles.bio),
                   followers_count=COALESCE(EXCLUDED.followers_count, facebook_profiles.followers_count),
                   following_count=COALESCE(EXCLUDED.following_count, facebook_profiles.following_count),
                   friends_count=COALESCE(EXCLUDED.friends_count, facebook_profiles.friends_count),
                   is_person=COALESCE(EXCLUDED.is_person, facebook_profiles.is_person),
                   profile_pic_url=COALESCE(EXCLUDED.profile_pic_url, facebook_profiles.profile_pic_url),
                   external_url=COALESCE(EXCLUDED.external_url, facebook_profiles.external_url),
                   metadata=facebook_profiles.metadata || EXCLUDED.metadata,
                   updated_at=now()
                """,
                pid, uname, p.get("display_name") or p.get("full_name"), p.get("bio"),
                _int(p.get("followers_count")), _int(p.get("following_count")), _int(p.get("friends_count")),
                bool(p.get("is_person")) if p.get("is_person") is not None else None,
                pic, p.get("external_url"), json.dumps(p.get("metadata") or {}),
            )
        await _record_users(pool, platform, [{"user_id": pid, "username": uname,
                                              "display_name": p.get("display_name") or p.get("full_name"),
                                              "profile_pic_url": pic}], "profile")
        return True
    # Tier 4 change-history: snapshot the row BEFORE the upsert so we can diff
    # old -> new and log bio/username/follower/etc. changes. Best-effort.
    prev_row = None
    if UserChangeTracker is not None:
        try:
            async with pool.acquire() as conn:
                prev_row = await conn.fetchrow(
                    "SELECT username, full_name, bio, followers_count, following_count, "
                    "posts_count, is_verified, is_private, profile_pic_url, external_url "
                    "FROM instagram_profiles WHERE platform_user_id = $1",
                    str(p.get("user_id") or uname),
                )
        except Exception:
            logger.debug("ig change-tracker prev-row fetch failed for %s", uname, exc_info=True)
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO instagram_profiles
                  (id, platform_user_id, username, full_name, bio, followers_count,
                   following_count, posts_count, is_verified, is_private, profile_pic_url,
                   external_url, collected_at, updated_at)
                VALUES (gen_random_uuid(),$1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,now(),now())
                ON CONFLICT (platform_user_id) DO UPDATE SET
                   username=EXCLUDED.username, full_name=EXCLUDED.full_name, bio=EXCLUDED.bio,
                   followers_count=EXCLUDED.followers_count, following_count=EXCLUDED.following_count,
                   posts_count=EXCLUDED.posts_count, is_verified=EXCLUDED.is_verified,
                   is_private=EXCLUDED.is_private, profile_pic_url=COALESCE(EXCLUDED.profile_pic_url, instagram_profiles.profile_pic_url),
                   external_url=EXCLUDED.external_url, updated_at=now()
                """,
                str(p.get("user_id") or uname), uname, p.get("full_name"), p.get("bio"),
                _int(p.get("followers_count")), _int(p.get("following_count")), _int(p.get("posts_count")),
                bool(p.get("is_verified")), bool(p.get("is_private")), pic, p.get("external_url"),
            )
    except Exception:
        logger.debug("save profile failed %s", uname, exc_info=True)
    await _track_ig_profile_change(pool, p, uname, prev_row)
    await _record_users(pool, platform, [{"user_id": p.get("user_id"), "username": uname,
                                          "display_name": p.get("full_name"), "profile_pic_url": pic}], "profile")
    return True


async def _track_ig_profile_change(pool, p, uname, prev_row) -> None:
    """Diff the prior instagram_profiles row against the incoming extension
    payload and log field changes into instagram_user_changes (Tier 4). Maps the
    extension's field names onto INSTAGRAM_TRACKED_FIELDS. Best-effort — never
    breaks ingest."""
    if UserChangeTracker is None or INSTAGRAM_TRACKED_FIELDS is None:
        return
    try:
        pk_val = int(p.get("user_id") or 0)
    except (TypeError, ValueError):
        pk_val = 0
    if not pk_val:
        return
    try:
        current_normalized = None
        if prev_row is not None:
            pr = dict(prev_row)
            current_normalized = {
                "username": pr.get("username"), "full_name": pr.get("full_name"),
                "biography": pr.get("bio"), "is_verified": pr.get("is_verified"),
                "is_private": pr.get("is_private"), "profile_pic_url": pr.get("profile_pic_url"),
                "follower_count": pr.get("followers_count"),
                "following_count": pr.get("following_count"),
                "post_count": pr.get("posts_count"), "external_url": pr.get("external_url"),
            }
        new_snapshot = {
            "username": p.get("username"), "full_name": p.get("full_name"),
            "biography": p.get("bio"), "is_verified": bool(p.get("is_verified")),
            "is_private": bool(p.get("is_private")), "profile_pic_url": p.get("profile_pic_url"),
            "follower_count": _int(p.get("followers_count")),
            "following_count": _int(p.get("following_count")),
            "post_count": _int(p.get("posts_count")), "external_url": p.get("external_url"),
        }
        await UserChangeTracker(pool).detect_and_log(
            table="instagram_user_changes", pk_col="user_id", pk_val=pk_val,
            current_row=current_normalized, new_row=new_snapshot,
            fields=INSTAGRAM_TRACKED_FIELDS,
        )
    except Exception:
        logger.debug("ig change-tracker detect_and_log failed for %s", uname, exc_info=True)


async def _record_ig_access(pool, target_username, owner, profile) -> None:
    """Record that ``owner`` (the extension's logged-in IG account) could see
    ``target_username`` into profile_access_{summary,attempts} — the follow-aware
    selector (Phase 0). Because the extension is the live IG path (the headless
    collector is 429'd), this is where the routing data actually accumulates.

    A successful profile ingest means the owner CAN access the target. A private
    profile that still yielded data implies the owner FOLLOWS it (private data is
    only visible to followers). Best-effort — never breaks ingest.
    """
    if ProfileAccessRepository is None or not pool or not target_username:
        return
    account = None
    if isinstance(owner, dict):
        account = (owner.get("username") or owner.get("id") or "") or None
    elif isinstance(owner, str):
        account = owner.strip() or None
    # Fallback label when the extension didn't send which account it used: still
    # records the public/private signal, but won't match a routable cookie
    # account (so it can't mis-route — routing only trusts real account names).
    if not account:
        account = "ig_extension"
    _priv = profile.get("is_private")
    is_private = bool(_priv) if _priv is not None else None
    _fbv = profile.get("followed_by_viewer")
    is_followed = bool(_fbv) if _fbv is not None else bool(is_private)
    try:
        repo = ProfileAccessRepository(pool)
        await repo.record_attempt(
            source="instagram",
            target_id=str(target_username).lstrip("@"),
            account=str(account),
            can_access=True,
            is_public=(None if is_private is None else (not is_private)),
            is_followed=is_followed,
        )
    except Exception:
        logger.debug("ig access-record failed for %s", target_username, exc_info=True)


CREDENTIALS_ROOT = os.getenv("CREDENTIALS_ROOT", "/app/credentials")


async def cookies_handler(request):
    """Self-healing sessions: the extension pushes its LIVE logged-in cookies here,
    and we write a Netscape cookies.txt the headless collector auto-discovers. So the
    headless backup never runs on a dead/expired session (which causes 401 retry
    storms that look bot-like). Only instagram for now."""
    body = await _safe_json(request)
    platform = _norm_platform(body.get("platform"))
    if platform != "instagram":
        return _cors(web.json_response({"ok": False, "reason": "unsupported"}))
    cookies = body.get("cookies") or []
    have = {c.get("name") for c in cookies if isinstance(c, dict)}
    if "sessionid" not in have:  # not logged in — don't clobber a good file with junk
        return _cors(web.json_response({"ok": False, "reason": "no sessionid"}))
    account = re.sub(r"[^A-Za-z0-9_.-]", "_", str(body.get("account") or "extension_live"))[:60]
    lines = ["# Netscape HTTP Cookie File", "# generated by the UnifiedCollector extension", ""]
    for c in cookies:
        if not isinstance(c, dict) or not c.get("name"):
            continue
        domain = c.get("domain") or ".instagram.com"
        inc_sub = "TRUE" if domain.startswith(".") else "FALSE"
        path = c.get("path") or "/"
        secure = "TRUE" if c.get("secure") else "FALSE"
        expiry = int(c.get("expirationDate") or 0)
        lines.append(f"{domain}\t{inc_sub}\t{path}\t{secure}\t{expiry}\t{c['name']}\t{c.get('value','')}")
    try:
        d = Path(CREDENTIALS_ROOT) / "instagram"
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / (account + ".txt.tmp")
        tmp.write_text("\n".join(lines) + "\n")
        os.replace(tmp, d / (account + ".txt"))
        logger.info("cookies: wrote live session for instagram/%s (%d cookies)", account, len(cookies))
    except Exception as e:
        logger.warning("cookies write failed: %s", e)
        return _cors(web.json_response({"ok": False, "error": str(e)}, status=500))
    return _cors(web.json_response({"ok": True, "account": account}))


async def seed_handler(request):
    """Seed the spider from the user's own followers/following as hop-0 targets."""
    body = await _safe_json(request)
    platform = _norm_platform(body.get("platform"))
    users = body.get("users") or []
    added = 0
    if platform == "instagram":
        pool = request.app["pool"]
        async with pool.acquire() as conn:
            for u in users:
                uname = (u.get("username") if isinstance(u, dict) else str(u) or "").strip().lstrip("@")
                if not uname:
                    continue
                res = await conn.execute(
                    """
                    INSERT INTO instagram_spider_targets (username, hop, discovered_from)
                    VALUES ($1, 0, 'self') ON CONFLICT (username) DO NOTHING
                    """,
                    uname,
                )
                if res.endswith("1"):
                    added += 1
        await _record_users(request.app["pool"], platform, users, "follow")
    return _cors(web.json_response({"added": added}))


async def profile_handler(request):
    body = await _safe_json(request)
    platform = _norm_platform(body.get("platform"))
    p = body.get("profile") or {}
    await _save_profile(request.app["pool"], platform, p)
    # Follow-aware access recording (Phase 0): the extension just successfully
    # fetched this profile with its logged-in account (body["owner"]) — so that
    # account CAN see the target. Populates profile_access for the selector.
    if platform == "instagram":
        await _record_ig_access(
            request.app["pool"], (p.get("username") or "").strip().lstrip("@"),
            body.get("owner"), p,
        )
    # download the profile photo as a kind=profile media item
    pic = p.get("profile_pic_url")
    uname = (p.get("username") or "").strip().lstrip("@")
    if pic and uname:
        await _ingest(request.app, platform, {"username": uname, "items": [
            {"url": pic, "content_id": str(p.get("user_id") or uname), "content_type": "photo",
             "kind": "profile", "entity_name": uname}]})
    return _cors(web.json_response({"ok": True}))


async def posts_handler(request):
    body = await _safe_json(request)
    platform = _norm_platform(body.get("platform"))
    n = await _save_posts(request.app["pool"], platform, body.get("posts") or [])
    # post authors (threads/facebook carry author_username) count as seen users
    authors = [{"username": p.get("author_username")} for p in (body.get("posts") or []) if p.get("author_username")]
    await _record_users(request.app["pool"], platform, authors, "author")
    return _cors(web.json_response({"saved": n}))


async def comments_handler(request):
    body = await _safe_json(request)
    platform = _norm_platform(body.get("platform"))
    comments = body.get("comments") or []
    n = await _save_comments(request.app["pool"], platform, body.get("post_id"), comments)
    # every commenter is a user we've seen
    authors = [{"username": c.get("author_username"), "user_id": c.get("author_platform_id")} for c in comments if c.get("author_username") or c.get("author_platform_id")]
    await _record_users(request.app["pool"], platform, authors, "comment")
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
    app.router.add_get("/social/ig_cooldown", ig_cooldown)
    app.router.add_post("/social/ingest", ingest)
    app.router.add_post("/social/discover", discover)
    app.router.add_post("/social/target-status", target_status_handler)
    app.router.add_post("/social/posts", posts_handler)
    app.router.add_post("/social/comments", comments_handler)
    app.router.add_post("/social/users", users_handler)
    app.router.add_post("/social/profile", profile_handler)
    app.router.add_post("/social/seed", seed_handler)
    app.router.add_post("/social/dms", dms_handler)
    app.router.add_post("/social/cookies", cookies_handler)
    app.router.add_post("/social/dm-frame", dm_frame_handler)
    app.router.add_post("/social/dm-sample", dm_sample_handler)
    app.router.add_post("/social/dm-probe", dm_probe_handler)
    app.router.add_post("/social/dm-heartbeat", dm_hook_heartbeat_handler)
    app.router.add_post("/social/dm-decoded", dm_decoded_handler)
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
