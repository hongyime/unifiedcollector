"""Browser ingest entry points (queue + upload + batch).

Split out of ``__init__.py`` (split step 11); re-exported there.
"""
import logging
from ._handles import _executemany_with_fallback
from ._ids import _VERIFIED_FILTER_PLATFORMS
from ._ids import _explicitly_verified_capture
from ._media import _BROWSER_MEDIA_CANDIDATE_UPSERT_SQL
from ._media import _BROWSER_MEDIA_REVISIT_UPSERT_SQL
from ._media import _TIKTOK_BROWSER_MEDIA_CANDIDATE_UPSERT_SQL
from ._media import _TIKTOK_BROWSER_REVISIT_UPSERT_SQL
from ._media import _prepare_browser_media_candidate
from ._media import _prepare_tiktok_browser_candidate
from ._util import _reject_count
from src.core.collection_surface import normalize_collection_surface
import asyncio

logger = logging.getLogger("social_ingest")


async def _ingest(app, platform, body):
    from . import _drain, _record_browser_ingest_event, mark_recovery_observation
    username = body.get("username") or "unknown"
    surface = normalize_collection_surface(body.get("collection_surface") or body.get("surface"))
    items = body.get("items") or []
    if platform in _VERIFIED_FILTER_PLATFORMS and _explicitly_verified_capture(body):
        return {
            "accepted": 0,
            "queued": False,
            "stored": 0,
            "platform": platform,
            "excluded_verified": True,
        }
    if isinstance(items, list):
        if platform in _VERIFIED_FILTER_PLATFORMS:
            items = [item for item in items if not _explicitly_verified_capture(item)]
    if items:
        task = asyncio.create_task(_drain(
            app, platform, username, items, body.get("extension_version"), surface,
        ))
        app["tasks"].add(task)
        task.add_done_callback(app["tasks"].discard)
    elif body.get("record_empty"):
        meta = {}
        if body.get("extension_version"):
            meta["extension_version"] = body.get("extension_version")
        if body.get("probe_reason"):
            meta["probe_reason"] = body.get("probe_reason")
        if isinstance(body.get("probe_meta"), dict):
            meta["probe_meta"] = body.get("probe_meta")
        await _record_browser_ingest_event(
            app["pool"],
            platform,
            "media",
            username,
            observed_count=0,
            stored_count=0,
            collection_surface=surface,
            outcome="empty",
            metadata=meta or None,
        )
        if (
            platform == "instagram"
            and body.get("probe_reason") == "restricted_following_feed_dom_observation"
        ):
            await mark_recovery_observation(app["pool"], "instagram", account=username)
    # Outcome states: queued = drain task scheduled below; stored is always 0
    # here because _drain persists after this response is already sent.
    return {
        "accepted": len(items),
        "queued": bool(items),
        "stored": 0,
        "platform": platform,
    }


async def _ingest_uploaded_media(app, platform, body):
    from . import _download_and_save, _record_browser_ingest_event, _record_browser_media_candidate
    username = body.get("username") or "unknown"
    surface = normalize_collection_surface(body.get("collection_surface") or body.get("surface"))
    item = body.get("item") or {}
    if not isinstance(item, dict):
        item = {}
    item = dict(item)
    if platform in _VERIFIED_FILTER_PLATFORMS and (
        _explicitly_verified_capture(body) or _explicitly_verified_capture(item)
    ):
        return {
            "accepted": 0,
            "stored": 0,
            "saved": 0,
            "deduped": False,
            "failed": False,
            "platform": platform,
            "excluded_verified": True,
        }
    item["collection_surface"] = normalize_collection_surface(
        item.get("collection_surface") or surface
    )
    if body.get("data_b64") and not item.get("data_b64"):
        item["data_b64"] = body.get("data_b64")
    if body.get("mime_type") and not item.get("mime_type"):
        item["mime_type"] = body.get("mime_type")
    if body.get("url") and not item.get("url"):
        item["url"] = body.get("url")
    extension_version = body.get("extension_version")
    meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
    item["meta"] = {
        **meta,
        "extension_version": extension_version,
        "browser_upload": True,
        "browser_upload_size": body.get("file_size"),
        "browser_upload_mime_type": body.get("mime_type"),
    }
    reject_stats: dict = {}
    saved = False
    save_error: BaseException | None = None
    try:
        saved = await _download_and_save(app["pool"], app["session"], platform, username, item, reject_stats)
    except Exception as exc:
        # The HTTP response already reported queued=true/stored=0; letting this
        # raise would die in _schedule_app_task's debug log with no event row,
        # hiding the failure behind an accepted-as-queued 200.
        save_error = exc
        saved = False
        logger.warning(
            "browser-upload[%s] %s: save failed: %s",
            platform,
            username,
            exc.__class__.__name__,
            exc_info=True,
        )
    await _record_browser_media_candidate(
        app["pool"],
        platform,
        username,
        item,
        ingest_mode="browser_upload",
        saved=bool(saved),
        reject_stats=reject_stats,
        extension_version=extension_version,
    )
    dedupe_reasons = {"duplicate_content_id", "duplicate_sha256"}
    deduped = save_error is None and any(
        _reject_count(reject_stats.get(reason)) > 0 for reason in dedupe_reasons
    )
    accepted = bool(saved or deduped)
    failed = not accepted
    reason = None
    if not accepted and reject_stats:
        reason = max(reject_stats.items(), key=lambda kv: _reject_count(kv[1]))[0]
    if save_error is not None:
        reason = reason or save_error.__class__.__name__
    event_meta = {
        "extension_version": extension_version,
        "ingest_mode": "browser_upload",
        "accepted": accepted,
        "saved": bool(saved),
        "deduped": deduped,
        "failed": failed,
    }
    if save_error is not None:
        event_meta["error_class"] = save_error.__class__.__name__
    if item.get("content_id"):
        event_meta["content_id"] = str(item.get("content_id"))[:200]
    if body.get("file_size") is not None:
        event_meta["file_size"] = body.get("file_size")
    if body.get("mime_type"):
        event_meta["mime_type"] = body.get("mime_type")
    if reason:
        event_meta["reason"] = reason
    if reject_stats:
        event_meta["reject_stats"] = reject_stats
    await _record_browser_ingest_event(
        app["pool"],
        platform,
        "media",
        username,
        observed_count=1,
        stored_count=1 if saved else 0,
        collection_surface=surface,
        deduped_count=1 if deduped else 0,
        failed_count=1 if failed else 0,
        outcome="success" if accepted else "failed",
        error_class=None if accepted else str(reason or "media_rejected").split(":", 1)[0],
        metadata=event_meta,
    )
    logger.info(
        "browser-upload[%s] %s: accepted=%d saved=%d deduped=%d failed=%d reason=%s",
        platform,
        username,
        1 if accepted else 0,
        1 if saved else 0,
        1 if deduped else 0,
        1 if failed else 0,
        reason or "ok",
    )
    # Dedupe is its own terminal state: it never counts as stored.
    return {
        "accepted": 1 if accepted else 0,
        "stored": 1 if saved else 0,
        "saved": 1 if saved else 0,
        "deduped": deduped,
        "failed": failed,
        "platform": platform,
        "reason": reason,
        "reject_stats": reject_stats,
    }


async def _record_browser_media_candidate_batch(app, platform, username, raw_items, extension_version):
    recorded = 0
    candidate_rows = []
    revisit_rows = []
    tiktok_candidate_rows = []
    tiktok_revisit_rows = []
    for entry in raw_items[:500]:
        if not isinstance(entry, dict):
            continue
        item = entry.get("item") if isinstance(entry.get("item"), dict) else entry
        result = entry.get("result") if isinstance(entry.get("result"), dict) else {}
        if not isinstance(item, dict):
            continue
        ingest_mode = str(entry.get("ingest_mode") or "browser_upload")[:40]
        saved = bool(result.get("saved") or result.get("stored"))
        reject_stats = result.get("reject_stats") if isinstance(result.get("reject_stats"), dict) else None
        prepared = _prepare_browser_media_candidate(
            platform,
            username,
            item,
            ingest_mode=ingest_mode,
            saved=saved,
            reject_stats=reject_stats,
            extension_version=extension_version,
            browser_result=result,
        )
        recorded += 1
        if prepared is not None:
            candidate_args, revisit_args = prepared
            candidate_rows.append(candidate_args)
            if revisit_args is not None:
                revisit_rows.append(revisit_args)
        if platform == "tiktok":
            tiktok_prepared = _prepare_tiktok_browser_candidate(
                username,
                item,
                ingest_mode=ingest_mode,
                saved=saved,
                reject_stats=reject_stats,
                extension_version=extension_version,
                browser_result=result,
            )
            if tiktok_prepared is not None:
                tiktok_candidate_args, tiktok_revisit_args = tiktok_prepared
                tiktok_candidate_rows.append(tiktok_candidate_args)
                if tiktok_revisit_args is not None:
                    tiktok_revisit_rows.append(tiktok_revisit_args)

    if candidate_rows or revisit_rows or tiktok_candidate_rows or tiktok_revisit_rows:
        try:
            async with app["pool"].acquire() as conn:
                if candidate_rows:
                    try:
                        await conn.executemany(_BROWSER_MEDIA_CANDIDATE_UPSERT_SQL, candidate_rows)
                    except Exception:
                        logger.debug("browser media candidate batch failed platform=%s count=%d", platform, len(candidate_rows), exc_info=True)
                        for args in candidate_rows:
                            try:
                                await conn.execute(_BROWSER_MEDIA_CANDIDATE_UPSERT_SQL, *args)
                            except Exception:
                                logger.debug("browser media candidate record failed platform=%s content_id=%s", platform, args[1], exc_info=True)
                if revisit_rows:
                    try:
                        await conn.executemany(_BROWSER_MEDIA_REVISIT_UPSERT_SQL, revisit_rows)
                    except Exception:
                        logger.debug("browser media revisit batch failed platform=%s count=%d", platform, len(revisit_rows), exc_info=True)
                        for args in revisit_rows:
                            try:
                                await conn.execute(_BROWSER_MEDIA_REVISIT_UPSERT_SQL, *args)
                            except Exception:
                                logger.debug("browser media revisit record failed platform=%s content_id=%s", platform, args[1], exc_info=True)
                if tiktok_candidate_rows:
                    await _executemany_with_fallback(
                        conn,
                        _TIKTOK_BROWSER_MEDIA_CANDIDATE_UPSERT_SQL,
                        tiktok_candidate_rows,
                        "enqueue TikTok browser media candidates",
                    )
                if tiktok_revisit_rows:
                    await _executemany_with_fallback(
                        conn,
                        _TIKTOK_BROWSER_REVISIT_UPSERT_SQL,
                        tiktok_revisit_rows,
                        "enqueue TikTok browser revisits",
                    )
        except Exception:
            logger.debug("browser media candidate batch acquire failed platform=%s", platform, exc_info=True)
    if recorded:
        logger.info("browser media candidate ledger[%s] %s: recorded=%d", platform, username, recorded)
