"""DM hook route handlers for the ig_ingest bridge.

Extracted from ``__init__.py`` per PERF-003 step 7
(``docs/plans/perf-file-splits.md`` §4B).

The extension's ``inject.js`` DM WebSocket hook (observe-only) POSTs
probes, raw samples, and decoded payloads here. Endpoints:

- POST /social/dm-heartbeat  → ``dm_hook_heartbeat_handler``
- POST /social/dm-probe      → ``dm_probe_handler``
- POST /social/dm-sample     → ``dm_sample_handler``
- POST /social/dm-frame      → ``dm_frame_handler``
- POST /social/dm-decoded    → ``dm_decoded_handler``

Heavy helpers (``_dm_probe_log_write``, ``_upsert_tt_decoded``,
``_upsert_ig_decoded``, ``_archive_browser_capture``) still live in
``__init__.py`` and are imported lazily.
"""
import asyncio
import base64
import json
import logging
import os
import re

from aiohttp import web

from .cors import _cors


logger = logging.getLogger("social_ingest")


async def dm_hook_heartbeat_handler(request):
    """Heartbeat from the browser extension's DM WebSocket hook (P1.3)."""
    from . import DM_HOOK_HEARTBEAT_WRITE_TIMEOUT_SECONDS, _safe_json

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
        return _cors(web.json_response({"ok": True, "recorded": False, "telemetry_degraded": True}))
    try:
        async with asyncio.timeout(DM_HOOK_HEARTBEAT_WRITE_TIMEOUT_SECONDS):
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
    except TimeoutError:
        logger.info(
            "dm_hook_heartbeat write timed out after %.2fs platform=%s owner=%s",
            DM_HOOK_HEARTBEAT_WRITE_TIMEOUT_SECONDS,
            platform,
            owner,
        )
        return _cors(web.json_response({
            "ok": True,
            "recorded": False,
            "telemetry_degraded": True,
            "reason": "db_write_timeout",
        }))
    except Exception:
        logger.debug("dm_hook_heartbeat upsert failed", exc_info=True)
        return _cors(web.json_response({
            "ok": True,
            "recorded": False,
            "telemetry_degraded": True,
            "reason": "db_write_failed",
        }))
    return _cors(web.json_response({"ok": True, "recorded": True}))


async def dm_probe_handler(request):
    """One-time investigation probe (#38)."""
    from . import _archive_browser_capture, _dm_probe_log_write, _safe_json, _schedule_app_task

    body = await _safe_json(request)
    logger.info(
        "DM probe: platform=%s transport=%s kind=%s size=%s url=%s",
        body.get("platform"), body.get("transport"), body.get("frame_kind"),
        body.get("frame_size"), body.get("url"),
    )
    pool = request.app.get("pool")
    platform = body.get("platform") or "unknown"
    _schedule_app_task(
        request.app,
        _dm_probe_log_write(pool, platform, "probe", body),
        "dm_probe_log",
    )
    _schedule_app_task(
        request.app,
        _archive_browser_capture(pool, platform, "dm_probe", body),
        "dm_probe_archive",
    )
    return _cors(web.json_response({"ok": True}))


async def dm_sample_handler(request):
    """Save a raw DM-socket frame sample (base64) for decoder development (#35)."""
    from . import (
        DM_SAMPLE_CAP_PER_PLATFORM,
        DM_SAMPLE_DIR,
        _archive_browser_capture,
        _dm_probe_log_write,
        _safe_json,
    )

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
    archive_body = dict(body)
    archive_body["decoded_bytes"] = len(raw)
    archive_body["debug_sample_path"] = path
    await _archive_browser_capture(
        request.app.get("pool"), platform, "dm_sample", archive_body,
    )
    await _dm_probe_log_write(
        request.app.get("pool"), platform, "sample", body, frame_size=len(raw),
    )
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
    """Capture a DM JSON frame if one is ever observed over a WS (#35)."""
    from . import _archive_browser_capture, _safe_json

    body = await _safe_json(request)
    try:
        logger.info("DM JSON frame (%s): %s", body.get("platform"), json.dumps(body.get("frame"))[:1000])
    except Exception:
        logger.info("DM frame observed (unserializable)")
    await _archive_browser_capture(
        request.app.get("pool"), body.get("platform") or "unknown", "dm_frame", body,
    )
    return _cors(web.json_response({"ok": True}))


async def dm_decoded_handler(request):
    """Client-decoded DM payload from the extension (Option B of #39)."""
    from . import (
        _archive_browser_capture,
        _safe_json,
        _upsert_ig_decoded,
        _upsert_tt_decoded,
    )

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
    await _archive_browser_capture(pool, platform, "dm_decoded", body)
    if platform == "tiktok":
        thread_n, msg_n = await _upsert_tt_decoded(pool, owner, threads, messages)
    else:
        thread_n, msg_n = await _upsert_ig_decoded(pool, owner, threads, messages)
    if thread_n or msg_n:
        logger.info("DM decoded[%s]: %d threads, %d messages", platform, thread_n, msg_n)
    return _cors(web.json_response({"ok": True, "threads": thread_n, "messages": msg_n}))
